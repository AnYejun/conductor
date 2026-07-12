"""Dashboard inbox — schedule-from-UI overlay + scheduler hot pickup."""
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import yaml

from conductor.schema import inbox_path, load_inbox_tasks, load_plan
from conductor.ui import add_inbox_task, make_handler, remove_inbox_task

PLAN = """
budget: {daily_usd: 1.00}
models:
  haiku: {id: claude-haiku-4-5, price_in: 1.00, price_out: 5.00}
tasks:
  - id: base-task
    kind: shell
    command: "true"
"""


@pytest.fixture
def plan_file(tmp_path: Path) -> Path:
    p = tmp_path / "plan.yaml"
    p.write_text(PLAN)
    return p


def test_inbox_merges_into_plan(plan_file: Path):
    add_inbox_task(plan_file, {"id": "ui-task", "kind": "claude", "prompt": "hi"})
    plan = load_plan(plan_file)
    assert [t.id for t in plan.tasks] == ["base-task", "ui-task"]


def test_inbox_rejects_duplicates_and_bad_refs(plan_file: Path):
    with pytest.raises(ValueError, match="already exists"):
        add_inbox_task(plan_file, {"id": "base-task", "kind": "claude", "prompt": "x"})
    with pytest.raises(ValueError, match="unknown model"):
        add_inbox_task(plan_file, {"id": "t2", "kind": "llm", "model": "gpt", "prompt": "x"})
    with pytest.raises(ValueError, match="unknown dependency"):
        add_inbox_task(plan_file, {"id": "t3", "kind": "claude", "prompt": "x",
                                   "depends_on": ["ghost"]})


def test_inbox_remove(plan_file: Path):
    add_inbox_task(plan_file, {"id": "ui-task", "kind": "claude", "prompt": "hi"})
    assert remove_inbox_task(plan_file, "ui-task")
    assert not remove_inbox_task(plan_file, "ui-task")
    assert load_inbox_tasks(load_plan(plan_file, include_inbox=False), plan_file) == []


def test_malformed_dep_chain_dropped(plan_file: Path):
    # hand-write an inbox entry with an unresolvable dep — loader must drop it
    ip = inbox_path(plan_file)
    ip.parent.mkdir(parents=True, exist_ok=True)
    ip.write_text(yaml.safe_dump([
        {"id": "ok", "kind": "claude", "prompt": "x"},
        {"id": "orphan", "kind": "claude", "prompt": "x", "depends_on": ["nope"]},
    ]))
    plan = load_plan(plan_file)
    assert [t.id for t in plan.tasks] == ["base-task", "ok"]


def test_scheduler_hot_pickup(plan_file: Path, tmp_path: Path):
    import asyncio

    from conductor.ledger import Ledger
    from conductor.scheduler import Scheduler, State

    plan = load_plan(plan_file)
    sched = Scheduler(plan, Ledger(tmp_path / "l.json"),
                      outputs_dir=plan_file.parent / ".conductor" / "outputs",
                      tick_seconds=1, plan_path=plan_file)
    # schedule from the "dashboard" AFTER the scheduler was constructed
    add_inbox_task(plan_file, {"id": "late-arrival", "kind": "shell", "command": "echo hot"})
    final = asyncio.run(sched.run(once=True))
    assert final["late-arrival"] is State.done


def test_http_schedule_roundtrip(plan_file: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(plan_file))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        r = httpx.post(f"{url}/api/tasks", json={
            "id": "from-ui", "kind": "claude", "prompt": "do the thing",
            "window": {"earliest": "02:00"}, "on_budget_exceeded": "downgrade",
        })
        assert r.status_code == 201
        state = httpx.get(f"{url}/api/state").json()
        row = next(t for t in state["tasks"] if t["id"] == "from-ui")
        assert row["source"] == "inbox" and row["earliest"] == "02:00"

        bad = httpx.post(f"{url}/api/tasks", json={"id": "from-ui", "kind": "claude",
                                                   "prompt": "dup"})
        assert bad.status_code == 400 and "already exists" in bad.json()["error"]

        assert httpx.delete(f"{url}/api/tasks/from-ui").status_code == 200
        assert httpx.delete(f"{url}/api/tasks/base-task").status_code == 409
    finally:
        server.shutdown()


def test_control_retry_resets_failed(plan_file: Path, tmp_path: Path):
    """Dashboard retry: control.json flips failed → pending, then is consumed."""
    import json

    from conductor.ledger import Ledger
    from conductor.scheduler import Scheduler, State
    from conductor.ui import request_retry

    plan = load_plan(plan_file)
    sched = Scheduler(plan, Ledger(tmp_path / "l.json"),
                      outputs_dir=plan_file.parent / ".conductor" / "outputs",
                      tick_seconds=1, plan_path=plan_file)
    sched.state["base-task"] = State.failed
    sched._write_state()

    queued = request_retry(plan_file)          # no ids → everything failed
    assert queued == ["base-task"]
    ctl = plan_file.parent / ".conductor" / "control.json"
    assert ctl.exists()

    sched._apply_control()
    assert sched.state["base-task"] is State.pending
    assert not ctl.exists()                     # consumed

    # explicit ids merge into an existing control file
    request_retry(plan_file, ["base-task"])
    request_retry(plan_file, ["base-task"])
    data = json.loads(ctl.read_text())
    assert data["retry"] == ["base-task"]


def test_http_retry_endpoint(plan_file: Path):
    import json as _json

    state_dir = plan_file.parent / ".conductor"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "state.json").write_text(_json.dumps(
        {"tasks": {"base-task": "failed"}, "details": {}}))

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(plan_file))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        r = httpx.post(f"{url}/api/retry", json={})
        assert r.status_code == 200 and r.json()["queued"] == ["base-task"]
        assert (state_dir / "control.json").exists()
        state = httpx.get(f"{url}/api/state").json()
        assert state["has_failed"] is True
    finally:
        server.shutdown()


def test_rooms_overlay_and_api(plan_file: Path):
    """Dashboard-created rooms merge into the plan; tasks may reference them."""
    from conductor.ui import add_room, remove_room

    add_room(plan_file, {"name": "openclaw-room", "image": "node:22",
                         "setup": "npm i -g openclaw", "node": "homebox"})
    plan = load_plan(plan_file)
    assert "openclaw-room" in plan.workspaces
    assert plan.workspaces["openclaw-room"].node == "homebox"

    # an inbox task can be assigned to the room and follows its machine
    from conductor.ui import add_inbox_task
    add_inbox_task(plan_file, {"id": "sweep", "kind": "shell",
                               "command": "openclaw status", "workspace": "openclaw-room"})
    plan = load_plan(plan_file)
    t = next(t for t in plan.tasks if t.id == "sweep")
    assert t.runs_on == "homebox"

    with pytest.raises(ValueError, match="already exists"):
        add_room(plan_file, {"name": "openclaw-room", "image": "x"})
    assert remove_room(plan_file, "openclaw-room")
    assert not remove_room(plan_file, "openclaw-room")


def test_mesh_join_persists(plan_file: Path):
    import base64
    import json as _json

    from conductor.ui import join_mesh

    code = base64.urlsafe_b64encode(_json.dumps(
        {"hub": "http://100.64.0.9:4747", "token": "t0k"}).encode()).decode()
    hub = join_mesh(plan_file, code, start=False)
    assert hub == "http://100.64.0.9:4747"
    join_mesh(plan_file, code, start=False)  # idempotent
    saved = _json.loads((plan_file.parent / ".conductor" / "mesh.json").read_text())
    assert len(saved["joins"]) == 1 and saved["joins"][0]["token"] == "t0k"

    with pytest.raises(ValueError, match="pairing code"):
        join_mesh(plan_file, "garbage!!!", start=False)


def test_scheduler_hot_pickup_room_task(plan_file: Path, tmp_path: Path):
    """A room + a task using it, both created AFTER the scheduler started,
    must still get picked up (rooms overlay hot-reload)."""
    import asyncio

    from conductor.ledger import Ledger
    from conductor.scheduler import Scheduler, State
    from conductor.ui import add_inbox_task, add_room

    plan = load_plan(plan_file)
    sched = Scheduler(plan, Ledger(tmp_path / "l.json"),
                      outputs_dir=plan_file.parent / ".conductor" / "outputs",
                      tick_seconds=1, plan_path=plan_file)
    add_room(plan_file, {"name": "late-room", "image": "alpine:latest"})
    add_inbox_task(plan_file, {"id": "late-room-task", "kind": "shell",
                               "command": "echo hi", "workspace": "late-room"})
    sched._refresh_inbox()
    assert "late-room" in sched.plan.workspaces
    assert sched.state.get("late-room-task") is State.pending
