"""Scheduler — asyncio loop: window ∈ now && deps done && budget OK → dispatch.

Hybrid model: tasks are not pinned to a clock time. Each declares a window
(earliest..deadline); within it, the task runs as soon as dependencies are
done and the budget gate passes. Over-budget tasks defer and retry each tick
until their deadline expires.
"""
from __future__ import annotations

import asyncio
import datetime as dt
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
    ):
        self.plan = plan
        self.ledger = ledger
        self.outputs_dir = outputs_dir
        self.console = console or Console()
        self.tick_seconds = tick_seconds
        self.client = client or AsyncAnthropic()
        self.state: dict[str, State] = {t.id: State.pending for t in plan.tasks}
        self._running: set[asyncio.Task] = set()
        self._defer_until: dict[str, dt.datetime] = {}  # budget-deferred backoff

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
            self._defer_until[task.id] = dt.datetime.now() + dt.timedelta(seconds=self.tick_seconds)
            self._log(task.id, "deferred", f"est ${result.est_usd:.4f} > remaining budget")
        elif result.outcome is Outcome.skipped:
            self.state[task.id] = State.skipped
            self._log(task.id, "skipped", result.detail)
        else:
            self.state[task.id] = State.failed
            self._log(task.id, "failed", result.detail)

    def _dispatch(self, tasks: list[Task]) -> None:
        for t in tasks:
            at = asyncio.create_task(self._run_one(t))
            self._running.add(at)
            at.add_done_callback(self._running.discard)

    def _all_settled(self) -> bool:
        return all(s in TERMINAL for s in self.state.values()) and not self._running

    # -- entry points ----------------------------------------------------

    async def run(self, once: bool = False) -> dict[str, State]:
        """Run until every task reaches a terminal state.

        once=True: run everything that can run *now* — including tasks whose
        dependencies complete during this pass — but don't wait for future
        windows or budget headroom. Budget-deferred tasks stay pending.
        """
        dispatched: set[str] = set()
        while True:
            now = dt.datetime.now().time()
            eligible = self._eligible(now)
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
            if self._all_settled():
                break

            # nothing running, nothing eligible: sleep until next tick
            await asyncio.sleep(self.tick_seconds)

        return dict(self.state)
