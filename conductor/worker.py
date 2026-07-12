"""Mesh worker — run this on each machine you want to lend to the mesh.

    conductor worker --hub http://100.64.0.3:4747 --allow-shell

The worker connects OUTBOUND to the hub (long-poll), so it works behind NAT
with no port forwarding. It executes two kinds of work:

- llm   — a Claude API call, made from THIS machine with its own credentials.
- shell — a command run on this machine. Disabled unless --allow-shell is
          passed (executing queue-supplied commands is remote code execution
          by design — opt in explicitly). With `container`, the command runs
          inside `docker run --rm <image>` instead of the host shell.
"""
from __future__ import annotations

import platform
import subprocess
import time
from typing import Any, Optional

import httpx


def ensure_workspace(ws: dict[str, Any], timeout: int = 600) -> str:
    """Make sure the persistent agent container exists and is running.
    Created once from ws['image'], set up once with ws['setup'], then reused
    forever — the agent's state (installs, files, sessions) survives tasks."""
    name = f"conductor-ws-{ws['name']}"
    state = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name],
                           capture_output=True, text=True, timeout=30)
    if state.returncode != 0:  # doesn't exist yet → create + one-time setup
        created = subprocess.run(
            ["docker", "run", "-d", "--name", name, "--restart", "unless-stopped",
             ws["image"], "sleep", "infinity"],
            capture_output=True, text=True, timeout=timeout)
        if created.returncode != 0:
            raise RuntimeError(f"workspace create failed: {created.stderr.strip()[:400]}")
        if ws.get("setup"):
            setup = subprocess.run(["docker", "exec", name, "sh", "-lc", ws["setup"]],
                                   capture_output=True, text=True, timeout=timeout)
            if setup.returncode != 0:
                raise RuntimeError(f"workspace setup failed: {setup.stderr.strip()[:400]}")
    elif state.stdout.strip() != "true":  # exists but stopped
        subprocess.run(["docker", "start", name], capture_output=True, text=True, timeout=60)
    return name


def execute_shell(payload: dict[str, Any], workdir: Optional[str] = None) -> dict[str, Any]:
    """Run a shell payload locally. Used by both the worker and local kind=shell tasks."""
    command = payload["command"]
    timeout = payload.get("timeout_seconds", 600)
    container = payload.get("container")
    workspace = payload.get("workspace")
    if workspace:  # persistent agent room — exec inside it
        try:
            name = ensure_workspace(workspace, timeout=timeout)
        except (RuntimeError, subprocess.SubprocessError, FileNotFoundError) as exc:
            return {"returncode": 125, "stdout": "", "stderr": str(exc)[:4000]}
        proc = subprocess.run(["docker", "exec", name, "sh", "-lc", command],
                              capture_output=True, text=True, timeout=timeout)
    elif container:  # one-off isolation
        argv = ["docker", "run", "--rm", container, "sh", "-lc", command]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    else:
        proc = subprocess.run(command, shell=True, capture_output=True, text=True,
                              timeout=timeout, cwd=workdir)
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout[-200_000:],
        "stderr": proc.stderr[-200_000:],
    }


def execute_llm(payload: dict[str, Any]) -> dict[str, Any]:
    """Run a Claude call with this machine's credentials; report text + usage."""
    import anthropic

    client = anthropic.Anthropic()
    kwargs: dict[str, Any] = {
        "model": payload["model_id"],
        "max_tokens": payload["max_output_tokens"],
        "messages": [{"role": "user", "content": payload["prompt"]}],
    }
    if payload.get("system"):
        kwargs["system"] = payload["system"]
    resp = client.messages.create(**kwargs)
    u = resp.usage
    return {
        "text": "\n\n".join(b.text for b in resp.content if b.type == "text"),
        "stop_reason": resp.stop_reason,
        "usage": {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        },
    }


def start_background_worker(hub_url: str, token: Optional[str] = None,
                            node: Optional[str] = None, log=print) -> None:
    """Join a hub from inside a running app: a daemon thread that lends this
    machine (shell + claude + rooms) to whoever owns that hub."""
    import threading
    threading.Thread(
        target=run_worker,
        kwargs=dict(hub_url=hub_url, node=node, token=token, allow_shell=True, log=log),
        daemon=True,
    ).start()


def run_worker(
    hub_url: str,
    node: Optional[str] = None,
    token: Optional[str] = None,
    allow_shell: bool = False,
    workdir: Optional[str] = None,
    log=print,
    max_items: Optional[int] = None,  # None = run forever; N = exit after N (tests)
) -> None:
    node = node or platform.node().split(".")[0]
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    log(f"worker '{node}' → {hub_url}  (shell: {'on' if allow_shell else 'OFF'})")

    done = 0
    with httpx.Client(base_url=hub_url, headers=headers, timeout=40) as client:
        while max_items is None or done < max_items:
            try:
                r = client.get("/v0/work/poll", params={"node": node})
            except httpx.HTTPError as exc:
                log(f"hub unreachable ({type(exc).__name__}); retrying in 5s")
                time.sleep(5)
                continue
            if r.status_code == 401:
                raise SystemExit("hub rejected token (401) — check CONDUCTOR_TOKEN")
            if r.status_code != 200:
                continue  # 204 = no work in this window

            item = r.json()
            item_id, kind, payload = item["id"], item["kind"], item["payload"]
            log(f"claimed {item_id} ({kind}: {payload.get('task_id', '?')})")
            try:
                if kind == "shell":
                    if not allow_shell:
                        raise PermissionError(
                            "this worker was started without --allow-shell"
                        )
                    result = execute_shell(payload, workdir=workdir)
                    ok = result["returncode"] == 0
                elif kind == "claude":
                    # full Claude Code harness = shell-grade power → same opt-in
                    if not allow_shell:
                        raise PermissionError(
                            "kind=claude needs a worker started with --allow-shell"
                        )
                    from .claude_exec import run_claude
                    result = run_claude(payload, workdir=workdir)
                    ok = not result.get("is_error")
                else:
                    result = execute_llm(payload)
                    ok = True
            except Exception as exc:
                ok, result = False, {"error": f"{type(exc).__name__}: {exc}"}

            client.post(f"/v0/work/{item_id}/result", json={"ok": ok, "result": result})
            log(f"reported {item_id} → {'ok' if ok else 'error'}")
            done += 1
