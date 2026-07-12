"""Scheduler-side mesh client — enqueue a task on the hub and await its result.

The budget gate stays on the scheduler: llm tasks are pre-flighted with
count_tokens BEFORE enqueue (in executor.gate_and_run), and the worker's
reported response.usage is reconciled into the local ledger afterward, so
remote work draws from the same daily/hourly budget as local work.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import re
from pathlib import Path
from typing import Any, Optional

import httpx

from .schema import Plan, Task

RESULT_POLL_SECONDS = 2.0


def hub_url(plan: Plan) -> Optional[str]:
    return plan.mesh.hub or os.environ.get("CONDUCTOR_HUB")


def _headers() -> dict[str, str]:
    token = os.environ.get("CONDUCTOR_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def build_payload(plan: Plan, task: Task, model_key: Optional[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {"task_id": task.id, "timeout_seconds": task.timeout_seconds}
    if task.kind.value == "shell":
        payload["command"] = task.command
        if task.container:
            payload["container"] = task.container
        if task.workspace:
            ws = plan.workspaces[task.workspace]
            payload["workspace"] = {"name": task.workspace, "image": ws.image,
                                    "setup": ws.setup}
    else:
        assert model_key is not None
        payload["model_id"] = plan.models[model_key].id
        payload["max_output_tokens"] = task.max_output_tokens
        payload["prompt"] = task.prompt_text()
        if task.system:
            payload["system"] = task.system
    return payload


async def dispatch_and_wait(
    base: str, task: Task, kind: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Enqueue on the hub, then poll until the item settles or times out.
    Returns the finished work item. Raises on hub errors/timeout."""
    assert task.runs_on is not None
    # generous wall clock: task's own timeout + queue slack
    deadline = dt.datetime.now() + dt.timedelta(seconds=task.timeout_seconds + 120)

    async with httpx.AsyncClient(base_url=base, headers=_headers(), timeout=30) as client:
        r = await client.post("/v0/work", json={
            "node": task.runs_on, "kind": kind, "payload": payload,
        })
        r.raise_for_status()
        item_id = r.json()["id"]

        while True:
            item = (await client.get(f"/v0/work/{item_id}")).json()
            if item.get("status") in ("done", "error"):
                return item
            if dt.datetime.now() > deadline:
                raise TimeoutError(
                    f"remote task '{task.id}' on node '{task.runs_on}' did not finish "
                    f"within {task.timeout_seconds + 120}s (item {item_id}, "
                    f"status={item.get('status')}) — is a worker running on that node?"
                )
            await asyncio.sleep(RESULT_POLL_SECONDS)


def write_remote_output(outputs_dir: Path, task: Task, item: dict[str, Any],
                        cost: float, model_id: Optional[str]) -> Path:
    outputs_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_id = re.sub(r"[^\w\-.]", "_", task.id)
    path = outputs_dir / f"{safe_id}-{stamp}.md"
    res = item.get("result") or {}
    if task.kind.value == "shell":
        body = (
            f"## stdout\n\n```\n{res.get('stdout', '')}\n```\n\n"
            f"## stderr\n\n```\n{res.get('stderr', '')}\n```\n"
        )
        extra = f"returncode: {res.get('returncode')}\n"
    else:
        body = res.get("text", "")
        extra = f"stop_reason: {res.get('stop_reason')}\n"
    header = (
        f"---\ntask: {task.id}\nkind: {task.kind.value}\nnode: {task.runs_on}\n"
        f"model: {model_id or '—'}\ncost_usd: {cost:.6f}\n{extra}"
        f"ran_at: {dt.datetime.now().astimezone().isoformat()}\n---\n\n"
    )
    path.write_text(header + body)
    return path
