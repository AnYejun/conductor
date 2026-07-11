#!/usr/bin/env python3
"""conductor node — a zero-dependency worker.

Drop this ONE file on any machine with Python 3.8+ (a home server, an OrbStack
Linux VM, a friend's box) and it joins your conductor mesh — no pip install, no
conductor package, stdlib only. It connects OUTBOUND to your hub (long-poll), so
it works behind NAT with no port forwarding.

    python3 conductor_worker.py --hub http://100.64.0.3:4747 --node homebox --allow-shell

Auth: pass --token or set CONDUCTOR_TOKEN (must match the hub's token).
It executes two kinds of work:
  shell  — a command on this machine (needs --allow-shell; it's remote code exec)
  claude — headless Claude Code on this machine's own `claude /login` session

`claude` runs through the login shell with nested agent-session variables
stripped, so it authenticates as THIS machine, exactly like `conductor worker`.
"""
import argparse
import json
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request

MAX_OUT = 200_000


def _sanitized_env():
    return {k: v for k, v in os.environ.items()
            if not (k.startswith("CLAUDE") or k in ("BAGGAGE", "AI_AGENT", "ANTHROPIC_BASE_URL"))}


def _resolve_claude():
    """Find the claude CLI even under a bare service PATH (systemd, launchd)."""
    import shutil
    home = os.path.expanduser("~")
    cands = [shutil.which("claude"),
             os.path.join(home, ".claude", "local", "claude"),
             "/opt/homebrew/bin/claude", "/usr/local/bin/claude",
             os.path.join(home, ".local", "bin", "claude")]
    nvm = os.path.join(home, ".nvm", "versions", "node")
    if os.path.isdir(nvm):
        vs = sorted(os.listdir(nvm),
                    key=lambda v: os.path.getmtime(os.path.join(nvm, v)), reverse=True)
        cands += [os.path.join(nvm, v, "bin", "claude") for v in vs]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def run_shell(payload, workdir=None):
    proc = subprocess.run(payload["command"], shell=True, capture_output=True, text=True,
                          timeout=payload.get("timeout_seconds", 600), cwd=workdir)
    return {"returncode": proc.returncode,
            "stdout": proc.stdout[-MAX_OUT:], "stderr": proc.stderr[-30_000:]}


def run_claude(payload, workdir=None):
    claude = _resolve_claude()
    if not claude:
        return {"is_error": True, "error": "claude CLI not found — install Claude Code and run /login"}
    cmd = [claude, "-p", payload["prompt"], "--output-format", "json"]
    if payload.get("claude_model"):
        cmd += ["--model", payload["claude_model"]]
    if payload.get("claude_tools"):
        cmd += ["--allowedTools", ",".join(payload["claude_tools"])]
    sys_parts = [p for p in (payload.get("system"), payload.get("briefing")) if p]
    if sys_parts:
        cmd += ["--append-system-prompt", "\n\n".join(sys_parts)]
    env = _sanitized_env()
    env["PATH"] = os.path.dirname(claude) + ":" + env.get("PATH", "/usr/bin:/bin")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=payload.get("timeout_seconds", 600),
                              cwd=workdir, env=env)
    except subprocess.TimeoutExpired:
        return {"is_error": True, "error": "claude -p timed out"}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"is_error": True, "error": (proc.stderr or proc.stdout or "no output")[-2000:]}
    u = data.get("usage") or {}
    err = str(data.get("result", ""))[:400] if data.get("is_error") else None
    out = {"is_error": bool(data.get("is_error")), "text": data.get("result", ""),
           "reported_cost_usd": data.get("total_cost_usd", 0.0),
           "num_turns": data.get("num_turns", 0),
           "usage": {"input_tokens": u.get("input_tokens", 0),
                     "output_tokens": u.get("output_tokens", 0),
                     "cache_read_input_tokens": u.get("cache_read_input_tokens", 0),
                     "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0)}}
    if err:
        out["error"] = err
    return out


def _req(url, token, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    return req


def main(argv=None):
    p = argparse.ArgumentParser(description="conductor mesh worker (zero-dependency)")
    p.add_argument("--hub", default=os.environ.get("CONDUCTOR_HUB"), required=not os.environ.get("CONDUCTOR_HUB"))
    p.add_argument("--node", default=platform.node().split(".")[0])
    p.add_argument("--token", default=os.environ.get("CONDUCTOR_TOKEN"))
    p.add_argument("--allow-shell", action="store_true")
    p.add_argument("--workdir")
    a = p.parse_args(argv)
    base = a.hub.rstrip("/")

    print(f"conductor node '{a.node}' -> {base}  (shell: {'on' if a.allow_shell else 'OFF'})",
          flush=True)
    while True:
        try:
            with urllib.request.urlopen(
                    _req(f"{base}/v0/work/poll?node={a.node}", a.token), timeout=40) as r:
                if r.status != 200:
                    continue
                item = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 204:
                continue
            if e.code == 401:
                sys.exit("hub rejected token (401) — check CONDUCTOR_TOKEN")
            time.sleep(5); continue
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"hub unreachable ({type(e).__name__}); retry in 5s", flush=True)
            time.sleep(5); continue

        kind, payload = item["kind"], item["payload"]
        print(f"claimed {item['id']} ({kind}: {payload.get('task_id','?')})", flush=True)
        try:
            if kind == "shell":
                if not a.allow_shell:
                    raise PermissionError("worker started without --allow-shell")
                res = run_shell(payload, a.workdir); ok = res["returncode"] == 0
            elif kind == "claude":
                if not a.allow_shell:
                    raise PermissionError("kind=claude needs --allow-shell")
                res = run_claude(payload, a.workdir); ok = not res.get("is_error")
            else:
                res = {"error": f"this lightweight worker can't run kind={kind} "
                       "(llm needs the full `conductor worker`)"}; ok = False
        except Exception as exc:
            res = {"error": f"{type(exc).__name__}: {exc}"}; ok = False

        try:
            urllib.request.urlopen(
                _req(f"{base}/v0/work/{item['id']}/result", a.token, "POST",
                     {"ok": ok, "result": res}), timeout=30).read()
            print(f"reported {item['id']} -> {'ok' if ok else 'error'}", flush=True)
        except Exception as exc:
            print(f"failed to report {item['id']}: {exc}", flush=True)


if __name__ == "__main__":
    main()
