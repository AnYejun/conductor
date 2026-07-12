"""Mesh tests — hub/worker roundtrip over real HTTP on localhost. No API key needed."""
import threading
from pathlib import Path

import httpx
import pytest

from conductor.hub import HubState, make_handler
from conductor.worker import run_worker
from http.server import ThreadingHTTPServer


@pytest.fixture
def hub(tmp_path: Path):
    state = HubState(tmp_path / "hub-state.json")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state, token="secret"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    yield url, state
    server.shutdown()


AUTH = {"Authorization": "Bearer secret"}


def test_auth_required(hub):
    url, _ = hub
    assert httpx.get(f"{url}/v0/health").status_code == 401
    assert httpx.get(f"{url}/v0/health", headers=AUTH).status_code == 200


def test_shell_roundtrip(hub):
    url, _ = hub
    r = httpx.post(f"{url}/v0/work", headers=AUTH, json={
        "node": "testbox", "kind": "shell",
        "payload": {"task_id": "t", "command": "echo hello-mesh", "timeout_seconds": 30},
    })
    assert r.status_code == 201
    item_id = r.json()["id"]

    # worker claims, executes, reports — exits after 1 item
    run_worker(url, node="testbox", token="secret", allow_shell=True,
               log=lambda *a: None, max_items=1)

    item = httpx.get(f"{url}/v0/work/{item_id}", headers=AUTH).json()
    assert item["status"] == "done"
    assert item["result"]["returncode"] == 0
    assert "hello-mesh" in item["result"]["stdout"]


def test_shell_denied_without_optin(hub):
    url, _ = hub
    item_id = httpx.post(f"{url}/v0/work", headers=AUTH, json={
        "node": "lockedbox", "kind": "shell",
        "payload": {"task_id": "t", "command": "echo nope", "timeout_seconds": 30},
    }).json()["id"]

    run_worker(url, node="lockedbox", token="secret", allow_shell=False,
               log=lambda *a: None, max_items=1)

    item = httpx.get(f"{url}/v0/work/{item_id}", headers=AUTH).json()
    assert item["status"] == "error"
    assert "allow-shell" in item["result"]["error"]


def test_node_isolation(hub):
    """Work queued for node A must not be claimable by node B."""
    url, state = hub
    httpx.post(f"{url}/v0/work", headers=AUTH, json={
        "node": "machine-a", "kind": "shell",
        "payload": {"task_id": "t", "command": "true", "timeout_seconds": 30},
    })
    assert state.poll("machine-b", hold=0.1) is None
    assert state.poll("machine-a", hold=0.1) is not None


def _docker_available() -> bool:
    import shutil
    import subprocess
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


@pytest.mark.skipif(not _docker_available(), reason="docker daemon not running")
def test_container_execution():
    from conductor.worker import execute_shell
    res = execute_shell({
        "command": "echo in-container && cat /etc/os-release | head -1",
        "container": "alpine:latest",
        "timeout_seconds": 120,
    })
    assert res["returncode"] == 0
    assert "in-container" in res["stdout"]
    assert "Alpine" in res["stdout"]


def test_local_shell_task_via_scheduler(tmp_path: Path):
    """kind=shell with no runs_on executes locally through the normal scheduler —
    zero API cost, no key required."""
    import asyncio

    from conductor.ledger import Ledger
    from conductor.scheduler import Scheduler, State
    from conductor.schema import load_plan

    plan_file = tmp_path / "plan.yaml"
    plan_file.write_text("""
budget: {daily_usd: 1.00}
models:
  haiku: {id: claude-haiku-4-5, price_in: 1.00, price_out: 5.00}
tasks:
  - id: local-shell
    kind: shell
    command: "echo scheduled-locally && uname -s"
""")
    plan = load_plan(plan_file)
    sched = Scheduler(plan, Ledger(tmp_path / "ledger.json"),
                      outputs_dir=tmp_path / "out", tick_seconds=1)
    final = asyncio.run(sched.run(once=True))
    assert final["local-shell"] is State.done

    outputs = list((tmp_path / "out").glob("local-shell-*.md"))
    assert len(outputs) == 1
    assert "scheduled-locally" in outputs[0].read_text()


def test_workspace_schema(tmp_path: Path):
    from conductor.schema import load_plan as lp
    p = tmp_path / "plan.yaml"
    p.write_text("""
budget: {daily_usd: 1.00}
models:
  haiku: {id: claude-haiku-4-5, price_in: 1.00, price_out: 5.00}
workspaces:
  openclaw: {image: node:22, setup: "npm i -g openclaw", node: homebox}
tasks:
  - id: sweep
    kind: shell
    command: "openclaw status"
    workspace: openclaw
""")
    plan = lp(p)
    assert plan.tasks[0].runs_on == "homebox"  # follows the room's machine

    bad = p.read_text().replace("workspace: openclaw", "workspace: ghost")
    p.write_text(bad)
    with pytest.raises(ValueError, match="unknown workspace"):
        lp(p)


def test_workspace_exec_with_fake_docker(tmp_path: Path, monkeypatch):
    """First use: inspect(miss) → run(create) → exec(setup) → exec(command)."""
    import os
    import stat

    from conductor.worker import execute_shell

    log = tmp_path / "docker.log"
    fake = tmp_path / "bin" / "docker"
    fake.parent.mkdir()
    fake.write_text(f"""#!/bin/bash
echo "$@" >> {log}
case "$1" in
  inspect) exit 1;;
  run) exit 0;;
  exec) [ "$2" = "conductor-ws-room1" ] && echo "from-the-room"; exit 0;;
  start) exit 0;;
esac
""")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")

    res = execute_shell({
        "command": "agent do-thing",
        "timeout_seconds": 60,
        "workspace": {"name": "room1", "image": "node:22", "setup": "npm i -g agent"},
    })
    assert res["returncode"] == 0
    assert "from-the-room" in res["stdout"]
    calls = log.read_text().splitlines()
    assert calls[0].startswith("inspect")
    assert calls[1].startswith("run -d --name conductor-ws-room1 --restart unless-stopped node:22")
    assert "npm i -g agent" in calls[2]        # one-time setup
    assert "agent do-thing" in calls[3]        # the actual task
