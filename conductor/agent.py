"""Agentic executor — a Hermes-style tool-use loop, grounded in long-term memory.

Per task:
  1. RECALL   — pull relevant memories, inject as a "what you know" briefing.
  2. LOOP     — messages.create ↔ execute tools, until the model stops (end_turn),
                the step cap is hit, or the budget runs dry. Every turn's actual
                usage is reconciled into the ledger immediately, so a runaway
                agent stops at the budget line instead of after the bill.
  3. CAPTURE  — the `remember` tool lets the agent persist lessons as it goes.

This is a manual loop (not the SDK tool runner) precisely so the budget gate
can sit between turns and so tools compose with the memory store and workspace.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .estimator import build_messages, cost_usd
from .ledger import Ledger
from .memory import MemoryStore
from .schema import ModelSpec, Plan, Task
from .tools import ToolContext, execute, tool_defs

AGENT_SYSTEM = """You are an autonomous agent completing a task without a human watching.

- When you have enough information to act, act. Don't over-plan or narrate options you won't pursue.
- Use your tools to gather ground truth rather than guessing; verify your own work before finishing.
- You have a long-term memory. Consult it (it's summarized below when relevant), and use the
  `remember` tool to save durable lessons — corrections, confirmed approaches, why something mattered.
  Save one insight per call; don't record what the workspace already makes obvious.
- Finish with a short, plain-language summary of the outcome — what you did and what you found."""


@dataclass
class AgentResult:
    ok: bool
    steps: int
    total_usd: float
    final_text: str
    tool_calls: list[str] = field(default_factory=list)
    memories_written: int = 0
    stop: str = ""  # end_turn | max_steps | budget_exhausted | error
    detail: str = ""


def _compose_system(task: Task, briefing: str) -> str:
    parts = [AGENT_SYSTEM]
    if task.system:
        parts.append(task.system)
    if briefing:
        parts.append(briefing)
    return "\n\n".join(parts)


async def run_agent(
    client: Any,           # AsyncAnthropic (or a compatible stub in tests)
    plan: Plan,
    task: Task,
    model: ModelSpec,
    ledger: Ledger,
    workspace: Path,
    memory: MemoryStore,
) -> AgentResult:
    names = task.tools or None
    from .tools import DEFAULT_TOOLS
    tool_names = names or DEFAULT_TOOLS
    tools = tool_defs(tool_names)
    ctx = ToolContext(workspace=workspace, memory=memory, task_id=task.id)

    briefing = memory.briefing(task.prompt_text()) if task.memory else ""
    system = _compose_system(task, briefing)
    messages: list[dict[str, Any]] = build_messages(task)

    total = 0.0
    calls: list[str] = []
    mem_writes = 0
    final_text = ""

    for step in range(task.max_steps):
        # per-turn budget gate: stop before a turn we can't afford
        remaining = ledger.remaining(plan.budget.daily_usd, plan.budget.hourly_usd)
        if remaining <= 0:
            return AgentResult(True, step, total, final_text, calls, mem_writes,
                               stop="budget_exhausted",
                               detail=f"stopped after {step} step(s): daily/hourly budget spent")

        try:
            resp = await client.messages.create(
                model=model.id,
                max_tokens=task.max_output_tokens,
                system=system,
                tools=tools,
                messages=messages,
            )
        except Exception as exc:
            return AgentResult(False, step, total, final_text, calls, mem_writes,
                               stop="error", detail=f"{type(exc).__name__}: {exc}")

        u = resp.usage
        cost = cost_usd(
            model, u.input_tokens, u.output_tokens,
            cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_creation=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        total += cost
        ledger.add(task.id, model.id, cost, {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "step": step,
        })

        messages.append({"role": "assistant", "content": resp.content})
        text = "\n".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        if text:
            final_text = text

        if resp.stop_reason == "end_turn":
            return AgentResult(True, step + 1, total, final_text, calls, mem_writes,
                               stop="end_turn")
        if resp.stop_reason == "pause_turn":
            continue  # server-tool turn paused; resend to resume

        tool_uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
        if not tool_uses:
            return AgentResult(True, step + 1, total, final_text, calls, mem_writes,
                               stop="end_turn")

        results = []
        for tu in tool_uses:
            calls.append(tu.name)
            if tu.name == "remember":
                mem_writes += 1
            out, is_err = execute(tu.name, tu.input, ctx)
            results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": out,
                "is_error": is_err,
            })
        messages.append({"role": "user", "content": results})

    return AgentResult(True, task.max_steps, total, final_text, calls, mem_writes,
                       stop="max_steps", detail=f"hit max_steps={task.max_steps}")
