"""Executor — budget gate + API call + usage reconciliation."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from anthropic import AsyncAnthropic

import asyncio

from .estimator import Estimate, build_messages, cost_usd, estimate
from .ledger import Ledger
from .remote import build_payload, dispatch_and_wait, hub_url, write_remote_output
from .schema import BudgetPolicy, Kind, Plan, Task


class Outcome(str, Enum):
    done = "done"
    deferred = "deferred"   # over budget, retry next tick
    skipped = "skipped"     # over budget, policy said give up
    failed = "failed"       # API error


@dataclass
class RunResult:
    outcome: Outcome
    model_key: Optional[str] = None    # model that actually ran (may be a downgrade)
    est_usd: float = 0.0
    actual_usd: float = 0.0
    output_path: Optional[Path] = None
    detail: str = ""
    retry_after: Optional[dt.datetime] = None  # deferred: earliest sensible retry


CLAUDE_MODEL_CHAIN = ["opus", "sonnet", "haiku"]  # subscription downgrade order


async def gate_and_run(
    client: AsyncAnthropic,
    plan: Plan,
    task: Task,
    ledger: Ledger,
    outputs_dir: Path,
) -> RunResult:
    """Pre-flight estimate → budget gate (with downgrade/defer/skip policy) →
    execute (locally or on a mesh node) → reconcile actual cost into the ledger."""

    # shell tasks have no API cost → no budget gate
    if task.kind is Kind.shell:
        return await _run_shell(plan, task, outputs_dir)

    # claude tasks run on your subscription login → no USD budget gate either
    if task.kind is Kind.claude:
        return await _run_claude(plan, task, ledger, outputs_dir)

    # 1. pre-flight: find a model whose worst-case cost fits the remaining budget
    candidates = [task.model]
    if task.on_budget_exceeded == BudgetPolicy.downgrade:
        candidates += plan.downgrade_chain(task.model)

    chosen_key: Optional[str] = None
    est: Optional[Estimate] = None
    last_est_usd = 0.0
    try:
        for key in candidates:
            model = plan.models[key]
            # re-estimate per candidate: tokenizers differ between models
            e = await estimate(client, task, model)
            last_est_usd = e.est_usd
            if e.est_usd <= ledger.remaining(plan.budget.daily_usd, plan.budget.hourly_usd):
                chosen_key, est = key, e
                break
    except Exception as exc:
        return RunResult(Outcome.failed, detail=f"pre-flight failed — {type(exc).__name__}: {exc}")

    if chosen_key is None or est is None:
        if task.on_budget_exceeded == BudgetPolicy.skip:
            return RunResult(Outcome.skipped, est_usd=last_est_usd,
                             detail="over budget; policy=skip")
        return RunResult(Outcome.deferred, est_usd=last_est_usd,
                         detail="over budget; will retry")

    model = plan.models[chosen_key]

    # agentic llm: multi-step tool-use loop, grounded in long-term memory
    if task.agentic:
        return await _run_agentic(client, plan, task, ledger, outputs_dir, chosen_key)

    # remote llm: same gate, but execution happens on a mesh node
    if task.runs_on:
        return await _run_remote_llm(plan, task, ledger, outputs_dir, chosen_key, est)

    # 2. reserve the worst-case estimate so concurrent tasks can't jointly overspend
    ledger.reserve(task.id, est.est_usd)
    try:
        kwargs: dict = {
            "model": model.id,
            "max_tokens": task.max_output_tokens,
            "messages": build_messages(task),
        }
        if task.system:
            kwargs["system"] = task.system
        resp = await client.messages.create(**kwargs)
    except Exception as exc:
        ledger.release(task.id)
        return RunResult(Outcome.failed, model_key=chosen_key, est_usd=est.est_usd,
                         detail=f"{type(exc).__name__}: {exc}")

    # 3. reconcile: estimate -> actual, from real usage
    u = resp.usage
    actual = cost_usd(
        model,
        u.input_tokens,
        u.output_tokens,
        cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_creation=getattr(u, "cache_creation_input_tokens", 0) or 0,
    )
    # 4. save the deliverable, then reconcile (the ledger entry links the output)
    text = "\n\n".join(b.text for b in resp.content if b.type == "text")
    out_path = _write_output(outputs_dir, task, model.id, resp.stop_reason, actual, text)
    ledger.add(task.id, model.id, actual, {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "output": out_path.name,
    })

    detail = ""
    if resp.stop_reason == "max_tokens":
        detail = "hit max_output_tokens — output may be truncated"
    if chosen_key != task.model:
        detail = (detail + "; " if detail else "") + f"downgraded {task.model} → {chosen_key}"

    return RunResult(Outcome.done, model_key=chosen_key, est_usd=est.est_usd,
                     actual_usd=actual, output_path=out_path, detail=detail)


async def _run_agentic(client, plan: Plan, task: Task, ledger: Ledger,
                       outputs_dir: Path, chosen_key: str) -> RunResult:
    """Multi-step tool-use agent with a LAPLAS-style long-term memory store.
    Budget is enforced per turn inside run_agent; usage reconciles as it goes."""
    from .agent import run_agent
    from .memory import MemoryStore

    if task.runs_on:
        return RunResult(Outcome.failed, model_key=chosen_key,
                         detail="agentic tasks run locally in v0.3 (remote agentic is roadmap)")

    workspace = task.workspace or outputs_dir.parent.parent  # default: plan dir
    workspace.mkdir(parents=True, exist_ok=True)
    memory = MemoryStore(outputs_dir.parent / "memory")
    model = plan.models[chosen_key]

    result = await run_agent(client, plan, task, model, ledger, workspace, memory)

    item = {"result": {"text": result.final_text, "stop_reason": result.stop},
            "status": "done" if result.ok else "error"}
    out_path = write_remote_output(outputs_dir, task, item, result.total_usd, model.id)

    tool_summary = ", ".join(sorted(set(result.tool_calls))) or "none"
    detail = (f"{result.steps} steps · tools: {tool_summary} · "
              f"{result.memories_written} memories · stop={result.stop}")
    if result.detail:
        detail += f" ({result.detail})"
    if chosen_key != task.model:
        detail += f" · downgraded {task.model} → {chosen_key}"

    outcome = Outcome.done if result.ok else Outcome.failed
    return RunResult(outcome, model_key=chosen_key, actual_usd=result.total_usd,
                     output_path=out_path, detail=detail)


async def _run_claude(plan: Plan, task: Task, ledger: Ledger, outputs_dir: Path) -> RunResult:
    """kind=claude — headless Claude Code on the machine's subscription login.
    No USD gate (subscription); reported cost + tokens land in the ledger at $0
    for observability. Remote nodes need `claude` installed + logged in there."""
    from .claude_exec import build_payload as build_claude_payload
    from .claude_exec import run_claude
    from .memory import MemoryStore

    memory = MemoryStore(outputs_dir.parent / "memory")
    briefing = memory.briefing(task.prompt_text()) if task.memory else ""

    # quota gate: rolling 5h + weekly windows vs the plan's calibrated ceilings
    from .quota import QuotaMonitor
    model_override: Optional[str] = None
    ok, worst = QuotaMonitor(ledger=ledger).gate(plan.subscription)
    if not ok and worst is not None:
        frac = f"{(worst.remaining_fraction or 0) * 100:.0f}%"
        why = f"{worst.name} window at {frac} remaining (reserve {plan.subscription.reserve:.0%})"
        if task.on_budget_exceeded == BudgetPolicy.skip:
            return RunResult(Outcome.skipped, detail=f"quota: {why}; policy=skip")
        if task.on_budget_exceeded == BudgetPolicy.downgrade:
            current = task.claude_model or "sonnet"
            idx = CLAUDE_MODEL_CHAIN.index(current) if current in CLAUDE_MODEL_CHAIN else 1
            if idx + 1 < len(CLAUDE_MODEL_CHAIN):
                model_override = CLAUDE_MODEL_CHAIN[idx + 1]
            else:
                return RunResult(Outcome.deferred, retry_after=worst.resets_at,
                                 detail=f"quota: {why}; already at cheapest model")
        else:  # defer until the window frees up
            return RunResult(Outcome.deferred, retry_after=worst.resets_at,
                             detail=f"quota: {why}")

    if task.runs_on:
        base = hub_url(plan)
        if not base:
            return RunResult(Outcome.failed,
                             detail=f"runs_on={task.runs_on} but no mesh.hub / $CONDUCTOR_HUB")
        # briefing text travels; the remember-dir is local-only, so skip it remotely
        payload = build_claude_payload(task, briefing=briefing, memory_dir=None,
                                       model_override=model_override)
        try:
            item = await dispatch_and_wait(base, task, "claude", payload)
        except Exception as exc:
            return RunResult(Outcome.failed, detail=f"{type(exc).__name__}: {exc}")
        res = item.get("result") or {}
    else:
        workspace = task.workspace or outputs_dir.parent.parent
        payload = build_claude_payload(task, briefing=briefing, memory_dir=memory.dir,
                                       model_override=model_override)
        res = await asyncio.to_thread(run_claude, payload, str(workspace))
        item = {"result": res, "status": "error" if res.get("is_error") else "done"}

    effective_model = model_override or task.claude_model or "default"
    reported = res.get("reported_cost_usd", 0.0)
    out_path = write_remote_output(outputs_dir, task, item, 0.0, effective_model)
    if res.get("usage"):
        # $0 against the budget (subscription); tokens + reported cost kept for
        # observability — and, for remote runs, for the quota monitor ("node" marks
        # entries that local transcripts don't already cover)
        usage = {**res["usage"], "reported_cost_usd": reported,
                 "num_turns": res.get("num_turns", 0), "output": out_path.name}
        if task.runs_on:
            usage["node"] = task.runs_on
        ledger.add(task.id, f"claude-code/{effective_model}", 0.0, usage)
    where = f"on {task.runs_on}" if task.runs_on else "local"
    if item["status"] != "done" or res.get("is_error"):
        return RunResult(Outcome.failed, output_path=out_path,
                         detail=res.get("error") or f"claude run failed ({where})")
    detail = (f"{where} · subscription (reported ${reported:.4f}, not billed) · "
              f"{res.get('num_turns', 0)} turns")
    if model_override:
        detail += f" · quota-downgraded {task.claude_model or 'default'} → {model_override}"
    return RunResult(Outcome.done, actual_usd=0.0, output_path=out_path, detail=detail)


async def _run_shell(plan: Plan, task: Task, outputs_dir: Path) -> RunResult:
    """kind=shell — local subprocess, or dispatched to a mesh node. Zero API cost."""
    payload = build_payload(plan, task, None)

    if task.runs_on:
        base = hub_url(plan)
        if not base:
            return RunResult(Outcome.failed,
                             detail=f"runs_on={task.runs_on} but no mesh.hub / $CONDUCTOR_HUB")
        try:
            item = await dispatch_and_wait(base, task, "shell", payload)
        except Exception as exc:
            return RunResult(Outcome.failed, detail=f"{type(exc).__name__}: {exc}")
        out_path = write_remote_output(outputs_dir, task, item, 0.0, None)
        res = item.get("result") or {}
        if item["status"] != "done":
            return RunResult(Outcome.failed, output_path=out_path,
                             detail=res.get("error") or f"exit {res.get('returncode')} on {task.runs_on}")
        return RunResult(Outcome.done, output_path=out_path,
                         detail=f"on {task.runs_on}, exit 0")

    # local shell
    from .worker import execute_shell
    try:
        res = await asyncio.to_thread(execute_shell, payload)
    except Exception as exc:
        return RunResult(Outcome.failed, detail=f"{type(exc).__name__}: {exc}")
    item = {"result": res, "status": "done" if res["returncode"] == 0 else "error"}
    out_path = write_remote_output(outputs_dir, task, item, 0.0, None)
    if res["returncode"] != 0:
        return RunResult(Outcome.failed, output_path=out_path,
                         detail=f"exit {res['returncode']}: {res['stderr'][:200].strip()}")
    return RunResult(Outcome.done, output_path=out_path, detail="local shell, exit 0")


async def _run_remote_llm(
    plan: Plan, task: Task, ledger: Ledger, outputs_dir: Path,
    chosen_key: str, est: Estimate,
) -> RunResult:
    """Budget-gated llm task executed on a mesh node; worker-reported usage
    reconciles into the local ledger so remote spend counts against the budget."""
    base = hub_url(plan)
    if not base:
        return RunResult(Outcome.failed,
                         detail=f"runs_on={task.runs_on} but no mesh.hub / $CONDUCTOR_HUB")
    model = plan.models[chosen_key]
    ledger.reserve(task.id, est.est_usd)
    try:
        item = await dispatch_and_wait(base, task, "llm", build_payload(plan, task, chosen_key))
    except Exception as exc:
        ledger.release(task.id)
        return RunResult(Outcome.failed, model_key=chosen_key, est_usd=est.est_usd,
                         detail=f"{type(exc).__name__}: {exc}")

    res = item.get("result") or {}
    if item["status"] != "done":
        ledger.release(task.id)
        return RunResult(Outcome.failed, model_key=chosen_key, est_usd=est.est_usd,
                         detail=res.get("error") or "remote worker error")

    u = res.get("usage") or {}
    actual = cost_usd(
        model,
        u.get("input_tokens", 0), u.get("output_tokens", 0),
        cache_read=u.get("cache_read_input_tokens", 0),
        cache_creation=u.get("cache_creation_input_tokens", 0),
    )
    out_path = write_remote_output(outputs_dir, task, item, actual, model.id)
    ledger.add(task.id, model.id, actual, {**u, "output": out_path.name})

    detail = f"on {task.runs_on}"
    if res.get("stop_reason") == "max_tokens":
        detail += "; hit max_output_tokens — output may be truncated"
    if chosen_key != task.model:
        detail += f"; downgraded {task.model} → {chosen_key}"
    return RunResult(Outcome.done, model_key=chosen_key, est_usd=est.est_usd,
                     actual_usd=actual, output_path=out_path, detail=detail)


def _write_output(outputs_dir: Path, task: Task, model_id: str,
                  stop_reason: Optional[str], cost: float, text: str) -> Path:
    outputs_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_id = re.sub(r"[^\w\-.]", "_", task.id)
    path = outputs_dir / f"{safe_id}-{stamp}.md"
    header = (
        f"---\ntask: {task.id}\nmodel: {model_id}\nstop_reason: {stop_reason}\n"
        f"cost_usd: {cost:.6f}\nran_at: {dt.datetime.now().astimezone().isoformat()}\n---\n\n"
    )
    path.write_text(header + text)
    return path
