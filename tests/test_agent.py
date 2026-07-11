"""Agentic loop + memory tests — fully offline (fake Anthropic client)."""
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from conductor.agent import run_agent
from conductor.ledger import Ledger
from conductor.memory import MemoryStore
from conductor.schema import Plan, load_plan
from conductor.tools import ToolContext, execute


# -- memory ---------------------------------------------------------------

def test_memory_remember_and_recall(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory")
    store.remember("Use tabs not spaces in this repo", "The linter enforces tabs.",
                   tags=["style", "python"], source="task-a")
    store.remember("The staging DB resets nightly at 3am UTC", tags=["infra"], source="task-b")

    hits = store.recall("python formatting and indentation style")
    assert hits and hits[0].summary.startswith("Use tabs")

    hits2 = store.recall("database staging environment")
    assert hits2 and "staging DB" in hits2[0].summary

    # unrelated query → no false matches
    assert store.recall("quantum chromodynamics") == []


def test_memory_persists_and_parses(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory")
    store.remember("Deploys need a clean git tree", "Otherwise the tag is ambiguous.",
                   tags=["deploy"], source="t1")
    reopened = MemoryStore(tmp_path / "memory")
    mems = reopened.all()
    assert len(mems) == 1
    assert mems[0].tags == ["deploy"]
    assert "ambiguous" in mems[0].body


def test_briefing_shape(tmp_path: Path):
    store = MemoryStore(tmp_path / "memory")
    store.remember("Prefer httpx over requests here", tags=["http"], source="t")
    b = store.briefing("which http client should I use")
    assert "What you already know" in b and "httpx" in b
    assert store.briefing("totally unrelated astrophysics") == ""


# -- tools ----------------------------------------------------------------

def test_tool_path_confinement(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = ToolContext(workspace=ws, memory=MemoryStore(tmp_path / "m"), task_id="t")

    out, err = execute("write", {"path": "note.txt", "content": "hi"}, ctx)
    assert not err and (ws / "note.txt").read_text() == "hi"

    out, err = execute("read", {"path": "note.txt"}, ctx)
    assert not err and out == "hi"

    # escape attempt is refused
    out, err = execute("read", {"path": "../../etc/passwd"}, ctx)
    assert err and "escape" in out


def test_edit_tool_uniqueness(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "f.txt").write_text("alpha alpha beta")
    ctx = ToolContext(workspace=ws, memory=MemoryStore(tmp_path / "m"), task_id="t")
    _, err = execute("edit", {"path": "f.txt", "old_str": "alpha", "new_str": "X"}, ctx)
    assert err  # 2 matches → refused
    _, err = execute("edit", {"path": "f.txt", "old_str": "beta", "new_str": "Y"}, ctx)
    assert not err and (ws / "f.txt").read_text() == "alpha alpha Y"


# -- agentic loop (fake client) ------------------------------------------

@dataclass
class _Text:
    text: str
    type: str = "text"


@dataclass
class _ToolUse:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass
class _Usage:
    input_tokens: int = 100
    output_tokens: int = 50


@dataclass
class _Resp:
    content: list
    stop_reason: str
    usage: _Usage


class FakeMessages:
    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    async def create(self, **kwargs) -> _Resp:
        resp = self._script[self.calls]
        self.calls += 1
        return resp


class FakeClient:
    def __init__(self, script):
        self.messages = FakeMessages(script)


def _plan(tmp_path: Path) -> Plan:
    p = tmp_path / "plan.yaml"
    p.write_text("""
budget: {daily_usd: 5.00}
models:
  haiku: {id: claude-haiku-4-5, price_in: 1.00, price_out: 5.00}
tasks:
  - id: agent-task
    agentic: true
    tools: [write, remember]
    prompt: "Create out.txt and remember a lesson."
    model: haiku
    max_steps: 5
""")
    return load_plan(p)


def test_agentic_loop_executes_tools_and_writes_memory(tmp_path: Path):
    plan = _plan(tmp_path)
    task = plan.tasks[0]
    ws = tmp_path / "ws"
    ws.mkdir()
    memory = MemoryStore(tmp_path / "memory")
    ledger = Ledger(tmp_path / "ledger.json")

    # turn 1: model calls two tools; turn 2: model finishes
    script = [
        _Resp(
            content=[
                _Text("Working on it."),
                _ToolUse("tu1", "write", {"path": "out.txt", "content": "done"}),
                _ToolUse("tu2", "remember",
                         {"summary": "out.txt needs a trailing newline", "tags": ["fmt"]}),
            ],
            stop_reason="tool_use",
            usage=_Usage(),
        ),
        _Resp(content=[_Text("Created out.txt and saved a lesson.")],
              stop_reason="end_turn", usage=_Usage()),
    ]
    client = FakeClient(script)
    result = asyncio.run(run_agent(client, plan, task, plan.models["haiku"],
                                   ledger, ws, memory))

    assert result.ok and result.stop == "end_turn"
    assert result.steps == 2
    assert (ws / "out.txt").read_text() == "done"
    assert result.memories_written == 1
    assert memory.all()[0].source == "agent-task"
    # budget reconciled: 2 turns recorded
    assert ledger.by_task()["agent-task"]["runs"] == 2
    assert result.total_usd > 0


def test_agentic_stops_at_budget(tmp_path: Path):
    plan = _plan(tmp_path)
    task = plan.tasks[0]
    ledger = Ledger(tmp_path / "ledger.json")
    # pre-spend the whole daily budget
    ledger.add("x", "m", 5.00, {"input_tokens": 0, "output_tokens": 0})

    client = FakeClient([_Resp(content=[_Text("hi")], stop_reason="end_turn", usage=_Usage())])
    result = asyncio.run(run_agent(client, plan, task, plan.models["haiku"],
                                   ledger, tmp_path, MemoryStore(tmp_path / "m")))
    assert result.stop == "budget_exhausted"
    assert client.messages.calls == 0  # gate fired before any API call


def test_agentic_respects_max_steps(tmp_path: Path):
    plan = _plan(tmp_path)
    task = plan.tasks[0]
    task.max_steps = 3
    ledger = Ledger(tmp_path / "ledger.json")
    ws = tmp_path / "ws"
    ws.mkdir()
    # model always asks for another tool → never emits end_turn
    loop_turn = _Resp(
        content=[_ToolUse("tu", "write", {"path": "f.txt", "content": "x"})],
        stop_reason="tool_use", usage=_Usage(),
    )
    client = FakeClient([loop_turn] * 10)
    result = asyncio.run(run_agent(client, plan, task, plan.models["haiku"],
                                   ledger, ws, MemoryStore(tmp_path / "m")))
    assert result.stop == "max_steps" and result.steps == 3
    assert client.messages.calls == 3
