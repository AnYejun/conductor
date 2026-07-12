"""Scheduler — asyncio loop: window ∈ now && deps done && budget OK → dispatch.

Hybrid model: tasks are not pinned to a clock time. Each declares a window
(earliest..deadline); within it, the task runs as soon as dependencies are
done and the budget gate passes. Over-budget tasks defer and retry each tick
until their deadline expires.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from enum import Enum
from pathlib import Path
from typing import Optional

from anthropic import AsyncAnthropic
from rich.console import Console

from .executor import Outcome, gate_and_run
from .ledger import Ledger
from .schema import PRIORITY_ORDER, Plan, Task


class State(str, Enum):
    pending = "pending"
    running = "running"
    done = "done"
    skipped = "skipped"
    failed = "failed"
    expired = "expired"  # deadline passed while pending/deferred


TERMINAL = {State.done, State.skipped, State.failed, State.expired}
# dependency satisfied only by successful completion
DEP_OK = {State.done}


class Scheduler:
    def __init__(
        self,
        plan: Plan,
        ledger: Ledger,
        outputs_dir: Path,
        console: Optional[Console] = None,
        tick_seconds: int = 60,
        client: Optional[AsyncAnthropic] = None,
        plan_path: Optional[Path] = None,  # enables live pickup of UI-scheduled tasks
    ):
        self.plan = plan
        self.ledger = ledger
        self.outputs_dir = outputs_dir
        self.console = console or Console()
        self.tick_seconds = tick_seconds
        self.client = client or AsyncAnthropic()
        self.plan_path = plan_path
        self.state: dict[str, State] = {t.id: State.pending for t in plan.tasks}
        self._running: set[asyncio.Task] = set()
        self._defer_until: dict[str, dt.datetime] = {}  # budget/quota-deferred backoff
        self._detail: dict[str, str] = {}  # last human-readable note per task
        self.state_path: Optional[Path] = outputs_dir.parent / "state.json"
        self._write_state()

    def _write_state(self) -> None:
        """Persist run state for observers (the `conductor ui` dashboard)."""
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "updated_at": dt.datetime.now().astimezone().isoformat(),
                "tasks": {tid: s.value for tid, s in self.state.items()},
                "defer_until": {tid: t.isoformat() for tid, t in self._defer_until.items()},
                "details": dict(self._detail),
            }
            self.state_path.write_text(json.dumps(payload, indent=2))
        except OSError:
            pass

    def _apply_control(self) -> None:
        """Pick up dashboard control requests (e.g. retry a failed task).
        The UI writes .conductor/control.json; we consume and delete it."""
        if self.plan_path is None:
            return
        ctl = self.plan_path.parent / ".conductor" / "control.json"
        if not ctl.exists():
            return
        try:
            data = json.loads(ctl.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
        try:
            ctl.unlink()
        except OSError:
            pass
        for tid in data.get("retry", []):
            if self.state.get(tid) in (State.failed, State.expired, State.skipped):
                self.state[tid] = State.pending
                self._defer_until.pop(tid, None)
                self._detail.pop(tid, None)
                self._log(tid, "scheduled", "retry requested from dashboard")
        self._write_state()

    def _refresh_inbox(self) -> None:
        """Pick up tasks AND rooms created from the dashboard while running."""
        if self.plan_path is None:
            return
        from .schema import Workspace, load_inbox_tasks, rooms_path
        rp = rooms_path(self.plan_path)
        if rp.exists():  # hot-pickup new rooms, else tasks referencing them get skipped
            try:
                import yaml as _yaml
                for name, spec in (_yaml.safe_load(rp.read_text()) or {}).items():
                    if name not in self.plan.workspaces:
                        self.plan.workspaces[name] = Workspace.model_validate(spec)
                        self._log(name, "scheduled", "new agent room from dashboard")
            except Exception:
                pass
        try:
            fresh = load_inbox_tasks(self.plan, self.plan_path)
        except Exception:
            return  # malformed inbox entry — ignore until fixed
        for t in fresh:
            if t.id in self.state:
                continue
            self.plan.tasks.append(t)
            self.state[t.id] = State.pending
            self._log(t.id, "scheduled", f"from dashboard inbox ({t.kind.value})")

    # -- helpers -----------------------------------------------------------

    def _task(self, task_id: str) -> Task:
        return next(t for t in self.plan.tasks if t.id == task_id)

    def _deps_done(self, task: Task) -> bool:
        return all(self.state[d] in DEP_OK for d in task.depends_on)

    def _deps_dead(self, task: Task) -> bool:
        """A dependency ended in a state that can never become done."""
        return any(self.state[d] in (TERMINAL - DEP_OK) for d in task.depends_on)

    def _eligible(self, now: dt.time) -> list[Task]:
        out = []
        wall = dt.datetime.now()
        for t in self.plan.tasks:
            if self.state[t.id] is not State.pending:
                continue
            if t.window.expired(now):
                self.state[t.id] = State.expired
                self._log(t.id, "expired", "deadline passed")
                continue
            if self._deps_dead(t):
                self.state[t.id] = State.skipped
                self._log(t.id, "skipped", "dependency failed/skipped/expired")
                continue
            if self._defer_until.get(t.id, wall) > wall:
                continue  # budget-deferred: back off until next tick
            if t.window.contains(now) and self._deps_done(t):
                out.append(t)
        out.sort(key=lambda t: PRIORITY_ORDER[t.priority])
        return out

    def _log(self, task_id: str, status: str, detail: str = "") -> None:
        from rich.markup import escape
        color = {
            "done": "green", "running": "cyan", "deferred": "yellow",
            "skipped": "dim", "failed": "red", "expired": "red",
        }.get(status, "white")
        ts = dt.datetime.now().strftime("%H:%M:%S")
        msg = f"[dim]{ts}[/dim] [{color}]{status:8s}[/{color}] {escape(task_id)}"
        if detail:
            msg += f" [dim]— {escape(detail)}[/dim]"
        self.console.print(msg)

    # -- execution -----------------------------------------------------------

    async def _run_one(self, task: Task) -> None:
        self.state[task.id] = State.running
        self._write_state()
        what = {"llm": f"model={task.model}", "claude": "claude·subscription"}.get(
            task.kind.value, "shell")
        if task.runs_on:
            what += f" @ {task.runs_on}"
        self._log(task.id, "running", what)
        result = await gate_and_run(self.client, self.plan, task, self.ledger, self.outputs_dir)

        if result.outcome is Outcome.done:
            self.state[task.id] = State.done
            detail = f"${result.actual_usd:.4f} (est ${result.est_usd:.4f}) → {result.output_path}"
            if result.detail:
                detail += f" [{result.detail}]"
            self._log(task.id, "done", detail)
        elif result.outcome is Outcome.deferred:
            self.state[task.id] = State.pending  # retry after backoff
            until = dt.datetime.now() + dt.timedelta(seconds=self.tick_seconds)
            if result.retry_after is not None:
                ra = result.retry_after
                if ra.tzinfo is not None:
                    ra = ra.astimezone().replace(tzinfo=None)
                until = max(until, ra)
            self._defer_until[task.id] = until
            why = result.detail or f"est ${result.est_usd:.4f} > remaining budget"
            self._log(task.id, "deferred", f"{why} · retry {until.strftime('%H:%M')}")
        elif result.outcome is Outcome.skipped:
            self.state[task.id] = State.skipped
            self._log(task.id, "skipped", result.detail)
        else:
            self.state[task.id] = State.failed
            self._log(task.id, "failed", result.detail)
        if result.detail:
            self._detail[task.id] = result.detail
        self._write_state()

    def _dispatch(self, tasks: list[Task]) -> None:
        for t in tasks:
            at = asyncio.create_task(self._run_one(t))
            self._running.add(at)
            at.add_done_callback(self._running.discard)

    def _all_settled(self) -> bool:
        return all(s in TERMINAL for s in self.state.values()) and not self._running

    # -- entry points ----------------------------------------------------

    async def run(self, once: bool = False, serve: bool = False) -> dict[str, State]:
        """Run until every task reaches a terminal state.

        once=True: run everything that can run *now* — including tasks whose
        dependencies complete during this pass — but don't wait for future
        windows or budget headroom. Budget-deferred tasks stay pending.

        serve=True (the desktop app): never exit — keep watching for dashboard
        retries and newly scheduled inbox tasks, and when the calendar day
        changes, reset finished tasks so daily windows run again.
        """
        dispatched: set[str] = set()
        day = dt.date.today()
        while True:
            if serve and dt.date.today() != day:
                day = dt.date.today()
                for tid, s in list(self.state.items()):
                    if s in TERMINAL:
                        self.state[tid] = State.pending
                self._defer_until.clear()
                self._detail.clear()
                self._log("plan", "scheduled", "new day — daily windows re-run")
            self._refresh_inbox()
            self._apply_control()
            now = dt.datetime.now().time()
            eligible = self._eligible(now)
            self._write_state()  # _eligible can expire/skip tasks
            if once:
                eligible = [t for t in eligible if t.id not in dispatched]

            if eligible:
                dispatched.update(t.id for t in eligible)
                self._dispatch(eligible)

            if self._running:
                await asyncio.gather(*list(self._running), return_exceptions=True)
                continue  # something finished — deps may have unlocked more work

            if once:
                # settle states one last time (dead deps, expired windows),
                # then stop — deferred tasks stay pending by design
                self._eligible(dt.datetime.now().time())
                break
            if self._all_settled() and not serve:
                break

            # nothing running, nothing eligible: sleep until next tick
            await asyncio.sleep(self.tick_seconds)

        return dict(self.state)
