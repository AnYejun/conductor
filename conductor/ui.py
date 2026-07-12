"""`conductor ui` — local observability dashboard (stdlib HTTP, zero deps).

Read-only view over the same files the CLI reads: the plan, the ledger,
scheduler state, the memory store, quota burn, and (if configured) hub nodes.
Auto-refreshes every 3 seconds. Styled after the Magritte brand: cream, ink,
cobalt, teal, marigold — ceci n'est pas un cron.
"""
from __future__ import annotations

import datetime as dt
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

import yaml

from .ledger import Ledger
from .memory import MemoryStore
from .quota import QuotaMonitor
from .schema import Plan, Task, inbox_path, load_plan


def add_inbox_task(plan_path: Path, raw: dict[str, Any]) -> Task:
    """Validate and append a dashboard-scheduled task to the inbox overlay.
    Raises ValueError with a human message on anything invalid."""
    plan = load_plan(plan_path)  # includes current inbox → collision check is complete
    task = Task.model_validate(raw)
    if any(t.id == task.id for t in plan.tasks):
        raise ValueError(f"task id '{task.id}' already exists")
    if task.model is not None and task.model not in plan.models:
        raise ValueError(f"unknown model key '{task.model}' — plan has: {list(plan.models)}")
    for d in task.depends_on:
        if not any(t.id == d for t in plan.tasks):
            raise ValueError(f"unknown dependency '{d}'")
    path = inbox_path(plan_path)
    entries = (yaml.safe_load(path.read_text()) or []) if path.exists() else []
    entries.append(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(entries, sort_keys=False, allow_unicode=True))
    return task


def remove_inbox_task(plan_path: Path, task_id: str) -> bool:
    path = inbox_path(plan_path)
    if not path.exists():
        return False
    entries = yaml.safe_load(path.read_text()) or []
    kept = [e for e in entries if e.get("id") != task_id]
    if len(kept) == len(entries):
        return False
    path.write_text(yaml.safe_dump(kept, sort_keys=False, allow_unicode=True))
    return True


def collect_state(plan: Plan, plan_path: Path) -> dict[str, Any]:
    state_dir = plan_path.parent / ".conductor"
    ipath = inbox_path(plan_path)
    inbox_ids = set()
    if ipath.exists():
        inbox_ids = {e.get("id") for e in (yaml.safe_load(ipath.read_text()) or [])}
    ledger = Ledger(state_dir / "ledger.json")
    memory = MemoryStore(state_dir / "memory")

    # budget
    budget = {
        "daily_usd": plan.budget.daily_usd,
        "hourly_usd": plan.budget.hourly_usd,
        "spent_today": round(ledger.spent_today(), 4),
        "spent_hour": round(ledger.spent_last_hour(), 4),
    }

    # quota (subscription)
    snap = QuotaMonitor(ledger=ledger).snapshot(plan.subscription)
    def win(w) -> dict[str, Any]:
        return {
            "burn": w.burn, "ceiling": w.ceiling,
            "remaining_fraction": w.remaining_fraction,
            "resets_at": w.resets_at.isoformat() if w.resets_at else None,
        }
    quota = {"five_hour": win(snap.five_hour), "weekly": win(snap.weekly),
             "reserve": plan.subscription.reserve}
    try:
        from .quota_live import fetch_live
        quota["live"] = fetch_live()  # real plan utilization, same source as /usage
    except Exception:
        quota["live"] = None

    # tasks + scheduler state
    run_state: dict[str, Any] = {}
    state_file = state_dir / "state.json"
    if state_file.exists():
        try:
            run_state = json.loads(state_file.read_text())
        except json.JSONDecodeError:
            run_state = {}
    details = run_state.get("details") or {}
    tasks = []
    for t in plan.tasks:
        w = t.window
        tasks.append({
            "id": t.id, "kind": t.kind.value,
            "model": t.claude_model if t.kind.value == "claude" else t.model,
            "runs_on": t.runs_on, "priority": t.priority.value,
            "window": (f"{w.earliest or '·'}–{w.deadline or '·'}"
                       if (w.earliest or w.deadline) else "anytime"),
            "earliest": w.earliest.strftime("%H:%M") if w.earliest else None,
            "deadline": w.deadline.strftime("%H:%M") if w.deadline else None,
            "depends_on": t.depends_on,
            "state": (run_state.get("tasks") or {}).get(t.id, "—"),
            "detail": details.get(t.id, ""),
            "agentic": t.agentic,
            "source": "inbox" if t.id in inbox_ids else "plan",
        })

    # actionable health signals for claude-task failures (first-run snags)
    failed_claude = [t for t in tasks if t["kind"] == "claude" and t["state"] == "failed"]
    claude_missing = any("not found" in t["detail"].lower() for t in failed_claude)
    login_needed = (not claude_missing) and any(
        "authentication" in t["detail"].lower() or "/login" in t["detail"].lower()
        for t in failed_claude
    )
    has_failed = any(t["state"] in ("failed", "expired") for t in tasks)

    # ledger recents + totals
    recents = [{
        "ts": e["ts"], "task_id": e["task_id"], "model": e["model"],
        "cost_usd": e["cost_usd"],
        "in": e["usage"].get("input_tokens", 0),
        "out": e["usage"].get("output_tokens", 0),
        "cache": e["usage"].get("cache_read_input_tokens", 0),
        "turns": e["usage"].get("num_turns"),
        "node": e["usage"].get("node"),
        "output": e["usage"].get("output"),
        "reported": e["usage"].get("reported_cost_usd"),
    } for e in ledger.entries[-40:]][::-1]

    memories = [{
        "summary": m.summary, "tags": m.tags, "source": m.source, "created": m.created,
    } for m in memory.all()[::-1][:30]]

    # per-agent totals: who is eating the tokens
    totals = sorted(({
        "task_id": tid, "runs": row["runs"],
        "in": row["input_tokens"], "out": row["output_tokens"],
        "cost_usd": round(row["cost_usd"], 4), "last": row["last_run"],
    } for tid, row in ledger.by_task().items()),
        key=lambda r: (r["in"] + r["out"]), reverse=True)[:10]

    rooms = [{"name": name, "image": ws.image, "node": ws.node,
              "setup": bool(ws.setup)} for name, ws in plan.workspaces.items()]

    return {
        "plan": plan_path.name,
        "generated_at": dt.datetime.now().astimezone().isoformat(),
        "scheduler_updated_at": run_state.get("updated_at"),
        "budget": budget, "quota": quota, "tasks": tasks,
        "ledger": recents, "memories": memories,
        "hub": plan.mesh.hub or __import__("os").environ.get("CONDUCTOR_HUB"),
        "nodes": _hub_nodes(plan.mesh.hub or __import__("os").environ.get("CONDUCTOR_HUB")),
        "login_needed": login_needed,
        "claude_missing": claude_missing,
        "has_failed": has_failed,
        "totals": totals,
        "rooms": rooms,
        "pairing": pairing_info(),
    }


def request_retry(plan_path: Path, ids: Optional[list[str]] = None) -> list[str]:
    """Queue failed/expired/skipped tasks for retry — the scheduler consumes
    .conductor/control.json on its next tick (same process or not)."""
    state_dir = plan_path.parent / ".conductor"
    if ids is None:
        run_state: dict[str, Any] = {}
        sf = state_dir / "state.json"
        if sf.exists():
            try:
                run_state = json.loads(sf.read_text())
            except json.JSONDecodeError:
                pass
        ids = [tid for tid, s in (run_state.get("tasks") or {}).items()
               if s in ("failed", "expired", "skipped")]
    if not ids:
        return []
    ctl = state_dir / "control.json"
    existing: dict[str, Any] = {}
    if ctl.exists():
        try:
            existing = json.loads(ctl.read_text())
        except json.JSONDecodeError:
            pass
    merged = sorted(set(existing.get("retry", [])) | set(ids))
    ctl.parent.mkdir(parents=True, exist_ok=True)
    ctl.write_text(json.dumps({"retry": merged}))
    return merged


ROOM_NAME_RE = __import__("re").compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def add_room(plan_path: Path, raw: dict[str, Any]) -> str:
    """Create an agent room from the dashboard → .conductor/rooms.yaml overlay."""
    from .schema import Workspace, rooms_path
    name = str(raw.get("name") or "").strip()
    if not ROOM_NAME_RE.match(name):
        raise ValueError("room name must be kebab-case (a-z, 0-9, dashes)")
    ws = Workspace.model_validate({k: raw.get(k) for k in ("image", "setup", "node") if raw.get(k)})
    if not ws.image:
        raise ValueError("room needs an image")
    plan = load_plan(plan_path)
    if name in plan.workspaces:
        raise ValueError(f"room '{name}' already exists")
    rp = rooms_path(plan_path)
    overlay = (yaml.safe_load(rp.read_text()) or {}) if rp.exists() else {}
    overlay[name] = ws.model_dump(exclude_none=True)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(yaml.safe_dump(overlay, sort_keys=False, allow_unicode=True))
    return name


def remove_room(plan_path: Path, name: str) -> bool:
    from .schema import rooms_path
    rp = rooms_path(plan_path)
    if not rp.exists():
        return False
    overlay = yaml.safe_load(rp.read_text()) or {}
    if name not in overlay:
        return False
    del overlay[name]
    rp.write_text(yaml.safe_dump(overlay, sort_keys=False, allow_unicode=True))
    return True


def _lan_ip() -> Optional[str]:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def pairing_info() -> Optional[dict[str, str]]:
    """This machine's pairing code — paste it on another Conductor to connect."""
    import base64
    import os
    token = os.environ.get("CONDUCTOR_TOKEN")
    hub = os.environ.get("CONDUCTOR_HUB")
    if not (token and hub):
        return None
    ip = _lan_ip() or "127.0.0.1"
    url = f"http://{ip}:4747"
    code = base64.urlsafe_b64encode(
        json.dumps({"hub": url, "token": token}).encode()).decode()
    return {"code": code, "hub": url}


def join_mesh(plan_path: Path, code: str, start: bool = True) -> str:
    """Consume a pairing code from another machine: persist it and start
    lending this machine to that hub immediately."""
    import base64
    try:
        data = json.loads(base64.urlsafe_b64decode(code.strip().encode()))
        hub, token = data["hub"], data["token"]
    except Exception:
        raise ValueError("that doesn't look like a conductor pairing code")
    f = plan_path.parent / ".conductor" / "mesh.json"
    state = {"joins": []}
    if f.exists():
        try:
            state = json.loads(f.read_text()) or {"joins": []}
        except json.JSONDecodeError:
            pass
    if not any(j.get("hub") == hub for j in state["joins"]):
        state["joins"].append({"hub": hub, "token": token})
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(state))
    if start:
        from .worker import start_background_worker
        start_background_worker(hub, token=token, log=lambda *a: None)
    return hub


PLANNER_SYSTEM = """You are the planning assistant inside conductor, a scheduler that runs \
Claude tasks on a budget. Help the user design scheduled tasks by conversing naturally \
(reply in the user's language). Ask at most one clarifying question at a time.

When the user has settled on one or more tasks, include a fenced ```json block:
{"tasks": [{"id": "kebab-case-id", "kind": "claude", "claude_model": "sonnet",
 "prompt": "...", "window": {"earliest": "HH:MM", "deadline": "HH:MM"},
 "on_budget_exceeded": "defer", "runs_on": null}]}
Rules: kind is "claude" (subscription, preferred), "llm" (needs model key from the plan), \
or "shell" (command field instead of prompt). claude_model ∈ opus|sonnet|haiku. window is \
optional (omit = run anytime); both bounds are optional. runs_on must be one of the \
connected machines or omitted. Keep prompts self-contained — the task runs unattended. \
Emit the json only when the user confirms they want it scheduled."""


def plan_chat(plan_path: Path, history: list[dict[str, str]]) -> str:
    """One conversational turn with the user's own subscription Claude, primed
    with the current plan + machines so it designs schedulable tasks."""
    from .claude_exec import run_claude

    plan = load_plan(plan_path)
    ctx_tasks = [f"- {t.id} ({t.kind.value}, {t.window.earliest or 'anytime'})"
                 for t in plan.tasks]
    nodes = [n["name"] for n in _hub_nodes(plan.mesh.hub) if n.get("online")]
    context = (f"Current plan tasks:\n" + ("\n".join(ctx_tasks) or "(none)")
               + f"\nConnected machines: {', '.join(nodes) or '(none — local only)'}"
               + f"\nModel keys in plan: {', '.join(plan.models)}")

    convo = "\n\n".join(
        ("User: " if m.get("role") == "user" else "Assistant: ") + m.get("content", "")
        for m in history[-16:]
    )
    payload = {
        "task_id": "plan-chat",
        "prompt": f"{context}\n\n{convo}\n\nAssistant:",
        "system": PLANNER_SYSTEM,
        "claude_model": "sonnet",
        "timeout_seconds": 120,
    }
    res = run_claude(payload)
    if res.get("is_error"):
        raise RuntimeError(res.get("error") or "claude call failed")
    return res.get("text", "")


def open_terminal_login() -> None:
    """macOS: open Terminal and type `claude /login` for the user — the
    in-app sign-in pipeline. (The OAuth itself still happens in their browser;
    we just save them the terminal gymnastics.)"""
    import subprocess
    import sys
    if sys.platform != "darwin":
        raise RuntimeError("in-app sign-in is macOS-only for now")
    script = 'tell application "Terminal"\nactivate\ndo script "claude /login"\nend tell'
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15, check=True)


def _hub_nodes(hub: Optional[str]) -> list[dict[str, Any]]:
    """Query the mesh hub for connected machines. Best-effort + short timeout so
    a slow/absent hub never stalls the dashboard."""
    if not hub:
        return []
    import os
    import urllib.request

    req = urllib.request.Request(f"{hub.rstrip('/')}/v0/health")
    token = os.environ.get("CONDUCTOR_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            health = json.loads(r.read())
    except Exception:
        return []
    now = dt.datetime.now().astimezone()
    out = []
    for name, info in sorted((health.get("nodes") or {}).items()):
        try:
            seen = dt.datetime.fromisoformat(info["last_seen"])
            age = (now - seen).total_seconds()
        except Exception:
            age = None
        out.append({"name": name, "age_seconds": age,
                    "online": age is not None and age < 90})
    return out


PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>conductor — ceci n'est pas un cron.</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 128 128'%3E%3Crect x='4' y='4' width='120' height='120' rx='26' fill='%23FDF6E3' stroke='%23111' stroke-width='4'/%3E%3Cpath d='M50 30 H74 L95 104 Q95 108 91 108 L33 108 Q29 108 29 104 Z' fill='%232456F5' stroke='%23111' stroke-width='4.5'/%3E%3Cpath d='M42 76 Q62 58 82 76 Q62 94 42 76 Z' fill='%23fff' stroke='%23111' stroke-width='4'/%3E%3Ccircle cx='62' cy='76' r='9.5' fill='%2327DBA2' stroke='%23111' stroke-width='3'/%3E%3Ccircle cx='62' cy='76' r='4.5' fill='%23111'/%3E%3C/svg%3E">
<style>
:root{--cream:#FDF6E3;--ink:#111111;--cobalt:#2456F5;--teal:#27DBA2;--gold:#FFB020;--red:#FF5A5A;--paper:#FFFCF3}
*{box-sizing:border-box;margin:0}
html,body{height:100%}
body{background:var(--cream);color:var(--ink);font:15px/1.6 -apple-system,"Helvetica Neue",Arial,sans-serif;overflow:hidden}
.drag{position:fixed;top:0;left:0;right:0;height:38px;-webkit-app-region:drag;z-index:50}
nav a,button,input,select,textarea,.card{-webkit-app-region:no-drag}
body:not(.app) .drag{display:none}
.shell{display:flex;height:100vh}
aside{width:224px;flex:none;background:#FAF3E0;border-right:2px solid var(--ink);display:flex;flex-direction:column;padding:18px 14px 16px}
body.app aside{padding-top:48px}
.brand{display:flex;align-items:center;gap:11px;padding:0 6px 8px}
.brand b{font-size:20px;letter-spacing:-.5px}
.brand .v{display:block;font-size:11px;color:#a89f86;font-weight:500}
.navgroup{font-size:10px;font-weight:700;letter-spacing:.13em;color:#b3a98d;text-transform:uppercase;padding:14px 8px 5px}
nav a{display:flex;justify-content:space-between;align-items:center;padding:7px 12px;margin:1px 0;border-radius:10px;font-weight:600;font-size:14px;cursor:pointer;border:2px solid transparent;user-select:none;transition:background .1s}
nav a:hover{background:#fff}
nav a.active{background:var(--gold);border-color:var(--ink)}
nav a .n{font-size:11px;font-weight:700;background:#fff;border:2px solid var(--ink);border-radius:99px;padding:0 7px;min-width:22px;text-align:center}
.side-foot{margin-top:auto;padding:0 8px;font-family:Georgia,serif;font-style:italic;font-size:12.5px;color:#b3a98d}
main{flex:1;overflow-y:auto;padding:22px 30px 60px}
body.app main{padding-top:48px}
.livedot{display:inline-block;width:8px;height:8px;border-radius:99px;background:var(--teal);border:1.5px solid var(--ink);margin-right:6px;vertical-align:1px}
.banner{display:flex;align-items:center;gap:12px;background:var(--gold);border:3px solid var(--ink);border-radius:14px;padding:13px 18px;margin-bottom:18px;font-size:14px}
.banner b{font-weight:700}
.banner code{background:#fff}
.bbtn{font:inherit;font-size:13px;font-weight:700;padding:6px 14px;border:2.5px solid var(--ink);border-radius:10px;background:#fff;cursor:pointer;white-space:nowrap}
.bbtn:hover{background:var(--teal)}
.view{display:none;max-width:960px;margin:0 auto}
.view.active{display:block}
.vtitle{font-size:24px;font-weight:700;letter-spacing:-.4px;margin-bottom:4px}
.vsub{font-family:Georgia,serif;font-style:italic;color:#777;margin-bottom:20px;font-size:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px;margin-bottom:16px}
.card{background:var(--paper);border:3px solid var(--ink);border-radius:16px;padding:18px 20px;margin-bottom:16px}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.12em;margin-bottom:12px;display:flex;justify-content:space-between;align-items:baseline}
.card h2 small{font-weight:400;text-transform:none;letter-spacing:0;color:#777;font-family:Georgia,serif;font-style:italic}
.gauge{margin:10px 0 4px}
.gauge .lbl{display:flex;justify-content:space-between;font-size:13px;margin-bottom:5px}
.bar{height:16px;border:2.5px solid var(--ink);border-radius:9px;background:#fff;overflow:hidden}
.bar i{display:block;height:100%;transition:width .6s}
.muted{color:#777;font-size:12.5px}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}
.stat{background:var(--paper);border:3px solid var(--ink);border-radius:14px;padding:10px 18px;text-align:center}
.stat b{display:block;font-size:22px}
.stat span{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#666}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:#666;padding:4px 8px 6px;border-bottom:2px solid var(--ink)}
td{padding:7px 8px;border-bottom:1px solid #e4dcc6;vertical-align:top}
tr:last-child td{border-bottom:none}
.chip{display:inline-block;padding:1px 10px;border:2px solid var(--ink);border-radius:99px;font-size:11.5px;font-weight:600;background:#fff}
.s-done{background:var(--teal)} .s-running{background:var(--cobalt);color:#fff}
.s-pending{background:#fff} .s-failed,.s-expired{background:var(--red);color:#fff}
.s-skipped{background:#ddd} .k-claude{background:var(--gold)}
.tag{display:inline-block;background:#fff;border:1.5px solid var(--ink);border-radius:99px;padding:0 8px;font-size:11px;margin:1px 2px}
.eye{display:inline-block;vertical-align:-2px;margin-right:2px}
.empty{color:#999;font-style:italic;font-family:Georgia,serif;padding:8px 0}
.lane{display:flex;align-items:center;gap:10px;margin:7px 0}
.lane .nm{flex:none;width:150px;font-size:13px;font-weight:600;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.track{position:relative;flex:1;height:22px;background:#fff;border:2px solid var(--ink);border-radius:7px;overflow:hidden}
.track .win{position:absolute;top:0;bottom:0;border-radius:4px;opacity:.9}
.hours{display:flex;margin-left:160px;font-size:10px;color:#999}
.hours span{flex:1}
.nowline{position:absolute;top:-3px;bottom:-3px;width:2.5px;background:var(--red);z-index:2}
form.sched{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;align-items:end}
form.sched label{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#666;display:block;margin-bottom:3px}
form.sched input,form.sched select,form.sched textarea{width:100%;font:inherit;font-size:13.5px;padding:7px 9px;border:2.5px solid var(--ink);border-radius:9px;background:#fff}
form.sched textarea{grid-column:1/-1;resize:vertical;min-height:56px}
form.sched button{grid-column:1/-1;justify-self:start;font:inherit;font-weight:700;font-size:14px;padding:9px 22px;border:3px solid var(--ink);border-radius:12px;background:var(--gold);cursor:pointer}
form.sched button:hover{background:var(--teal)}
#formmsg{grid-column:1/-1;font-size:13px;font-family:Georgia,serif;font-style:italic}
.del{cursor:pointer;border:none;background:none;font-size:14px;color:#999;padding:0 4px}
.del:hover{color:var(--red)}
code{font:12.5px ui-monospace,Menlo,monospace;background:#fff;border:1.5px solid var(--ink);border-radius:5px;padding:1px 6px}
#meshhelp b{font-size:14px}#meshhelp{line-height:2.1}
</style></head><body>
<div class="drag" data-tauri-drag-region></div>
<div class="shell">
<aside>
  <div class="brand">
    <svg width="40" height="40" viewBox="0 0 128 128"><rect x="4" y="4" width="120" height="120" rx="26" fill="#FDF6E3" stroke="#111" stroke-width="4"/><path d="M50 30 H74 L95 104 Q95 108 91 108 L33 108 Q29 108 29 104 Z" fill="#2456F5" stroke="#111" stroke-width="4.5"/><path d="M42 76 Q62 58 82 76 Q62 94 42 76 Z" fill="#fff" stroke="#111" stroke-width="4"/><circle cx="62" cy="76" r="9.5" fill="#27DBA2" stroke="#111" stroke-width="3"/><circle cx="62" cy="76" r="4.5" fill="#111"/><circle cx="64.5" cy="73" r="1.8" fill="#fff"/><line x1="86" y1="33" x2="98" y2="21" stroke="#111" stroke-width="6" stroke-linecap="round"/><circle cx="104" cy="15" r="7" fill="#FFB020" stroke="#111" stroke-width="4"/></svg>
    <div><b>conductor</b><span class="v" id="planname">plan.yaml</span></div>
  </div>
  <nav id="nav">
    <div class="navgroup">monitor</div>
    <a data-v="overview" class="active">overview</a>
    <a data-v="timeline">timeline</a>
    <a data-v="runs">runs <span class="n" id="n-runs">0</span></a>
    <div class="navgroup">control</div>
    <a data-v="compose">compose ✦</a>
    <a data-v="schedule">schedule</a>
    <a data-v="tasks">tasks <span class="n" id="n-tasks">0</span></a>
    <div class="navgroup">system</div>
    <a data-v="memory">memory <span class="n" id="n-mem">0</span></a>
    <a data-v="nodes">machines <span class="n" id="n-nodes">0</span></a>
  </nav>
  <div class="side-foot"><span class="livedot"></span><span id="ts">…</span></div>
</aside>
<main>
  <section class="view active" id="v-overview">
    <div class="vtitle">overview</div><div class="vsub">the metronome that watches your spend</div>
    <div id="banner"></div>
    <div class="stats" id="counts"></div>
    <div class="grid">
      <div class="card" style="margin:0"><h2>api budget <small>usd, hard gate</small></h2><div id="budget"></div></div>
      <div class="card" style="margin:0"><h2>subscription quota <small>auto defer/downgrade</small></h2><div id="quota"></div></div>
    </div>
    <div class="card"><h2>agents <small>who is eating the tokens</small></h2><div id="agents"></div></div>
  </section>
  <section class="view" id="v-timeline">
    <div class="vtitle">timeline</div><div class="vsub">every task's window over 24h — the red line is now</div>
    <div class="card"><div id="timeline"></div></div>
  </section>
  <section class="view" id="v-compose">
    <div class="vtitle">compose</div><div class="vsub">plan the schedule by talking to your own Claude — it knows your tasks and machines</div>
    <div class="card" style="display:flex;flex-direction:column;height:calc(100vh - 230px);min-height:360px">
      <div id="chat" style="flex:1;overflow-y:auto;padding-right:6px"></div>
      <form onsubmit="return composeSend(event)" style="display:flex;gap:10px;margin-top:12px">
        <input id="composeInput" autocomplete="off" placeholder="e.g. summarize my repo's TODOs every morning at 7" style="flex:1;font:inherit;font-size:14px;padding:10px 12px;border:2.5px solid var(--ink);border-radius:11px;background:#fff">
        <button class="bbtn" style="background:var(--gold)">send</button>
      </form>
    </div>
  </section>
  <section class="view" id="v-schedule">
    <div class="vtitle">schedule</div><div class="vsub">lands in .conductor/inbox.yaml — a running scheduler picks it up live</div>
    <div class="card">
    <form class="sched" id="schedform" onsubmit="return schedule(event)">
    <div><label>id</label><input name="id" placeholder="nightly-report" required></div>
    <div><label>kind</label><select name="kind" onchange="kindswap(this.value)"><option value="claude">claude (subscription)</option><option value="llm">llm (api)</option><option value="shell">shell</option></select></div>
    <div id="f-model"><label>model</label><input name="model" placeholder="sonnet" value="sonnet"></div>
    <div><label>earliest</label><input name="earliest" type="time"></div>
    <div><label>deadline</label><input name="deadline" type="time"></div>
    <div><label>if over budget</label><select name="policy"><option>defer</option><option>downgrade</option><option>skip</option></select></div>
    <div><label>runs on</label><input name="runs_on" placeholder="local" list="nodelist"><datalist id="nodelist"></datalist></div>
    <textarea name="prompt" id="f-prompt" placeholder="What should the agent do?" required></textarea>
    <button>schedule it</button><div id="formmsg"></div>
    </form></div>
  </section>
  <section class="view" id="v-tasks">
    <div class="vtitle">tasks</div><div class="vsub">plan.yaml + dashboard inbox</div>
    <div class="card"><div id="tasks"></div></div>
  </section>
  <section class="view" id="v-runs">
    <div class="vtitle">runs</div><div class="vsub">what actually happened, and what it cost</div>
    <div class="card"><div id="ledger"></div></div>
  </section>
  <section class="view" id="v-memory">
    <div class="vtitle">memory</div><div class="vsub">what the agents learned — inspect, edit, or delete the files anytime</div>
    <div class="card"><div id="memory"></div></div>
  </section>
  <section class="view" id="v-nodes">
    <div class="vtitle">machines &amp; rooms</div><div class="vsub">your computers as one orchestra — and the rooms your agents live in</div>
    <div class="grid">
      <div class="card" style="margin:0"><h2>this machine <small>share to connect</small></h2>
        <div id="pairing"></div></div>
      <div class="card" style="margin:0"><h2>join another conductor <small>paste its pairing code</small></h2>
        <form onsubmit="return meshJoin(event)" style="display:flex;gap:8px">
          <input id="joincode" placeholder="pairing code…" style="flex:1;font:12px ui-monospace,Menlo,monospace;padding:8px 10px;border:2.5px solid var(--ink);border-radius:9px;background:#fff">
          <button class="bbtn" style="background:var(--gold)">join</button>
        </form>
        <div class="muted" id="joinmsg" style="margin-top:8px">joining lends this machine (shell + claude + rooms) to that conductor — persists across restarts</div>
      </div>
    </div>
    <div class="card"><h2>connected machines</h2><div id="nodes"></div></div>
    <div class="card"><h2>agent rooms <small>persistent containers where agents live</small></h2>
      <div id="rooms"></div>
      <form class="sched" id="roomform" onsubmit="return roomCreate(event)" style="margin-top:12px">
        <div><label>name</label><input name="name" placeholder="openclaw-room" required></div>
        <div><label>preset</label><select name="preset" onchange="roomPreset(this.value)">
          <option value="blank">blank (alpine)</option>
          <option value="openclaw">OpenClaw</option>
          <option value="hermes">Hermes</option>
        </select></div>
        <div><label>machine</label><input name="node" placeholder="(this one)" list="nodelist"></div>
        <div><label>image</label><input name="image" id="room-image" value="alpine:latest" required></div>
        <textarea name="setup" id="room-setup" placeholder="one-time setup command (optional)"></textarea>
        <button>create room</button><div id="roommsg"></div>
      </form>
    </div>
    <div class="card"><h2>add a machine without the app</h2>
      <div id="meshhelp" class="muted"></div>
    </div>
  </section>
</main>
</div>
<script>
if(new URLSearchParams(location.search).has("app"))document.body.classList.add("app");
const fmt=n=>n>=1e6?(n/1e6).toFixed(1)+"M":n>=1e3?(n/1e3).toFixed(1)+"k":String(n);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function nav(v){
  document.querySelectorAll("nav a").forEach(a=>a.classList.toggle("active",a.dataset.v===v));
  document.querySelectorAll(".view").forEach(s=>s.classList.toggle("active",s.id==="v-"+v));
  history.replaceState(null,"",location.pathname+location.search+"#"+v);
}
document.querySelectorAll("nav a").forEach(a=>a.onclick=()=>nav(a.dataset.v));
if(location.hash)nav(location.hash.slice(1));
function gauge(label,used,cap,unit,reset){
  if(cap==null||!cap) return `<div class="gauge"><div class="lbl"><span>${label}</span><span>${fmt(used)} ${unit} · no ceiling set</span></div><div class="muted">calibrate in plan.yaml to enable the gate</div></div>`;
  const p=Math.min(100,100*used/cap);
  const col=p<60?"var(--teal)":p<85?"var(--gold)":"var(--red)";
  const r=reset?` · resets ${new Date(reset).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"})}`:"";
  return `<div class="gauge"><div class="lbl"><span>${label}</span><span>${fmt(used)} / ${fmt(cap)} ${unit}${r}</span></div><div class="bar"><i style="width:${p}%;background:${col}"></i></div></div>`;
}
function chip(s){return `<span class="chip s-${esc(s)}">${esc(s)}</span>`}
function gaugePct(label,pct,reset){
  const p=Math.min(100,Math.max(0,pct));
  const col=p<60?"var(--teal)":p<85?"var(--gold)":"var(--red)";
  const r=reset?` · resets ${new Date(reset).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"})}`:"";
  return `<div class="gauge"><div class="lbl"><span>${label}</span><span>${p.toFixed(0)}% used${r}</span></div><div class="bar"><i style="width:${p}%;background:${col}"></i></div></div>`;
}
const mins=t=>{if(!t)return null;const[h,m]=t.split(":").map(Number);return h*60+m};
const SCOL={done:"var(--teal)",running:"var(--cobalt)",failed:"var(--red)",expired:"var(--red)",skipped:"#ccc"};
function timeline(tasks){
  const now=new Date(),nowp=100*(now.getHours()*60+now.getMinutes())/1440;
  const lanes=tasks.map(t=>{
    const a=mins(t.earliest)??0,b=mins(t.deadline)??1440;
    const l=100*a/1440,w=Math.max(1.2,100*(b-a)/1440);
    const col=SCOL[t.state]||(t.kind==="claude"?"var(--gold)":"#bcd0ff");
    const open=t.earliest||t.deadline?"":";border:2px dashed #999;background:repeating-linear-gradient(45deg,#f4edd8,#f4edd8 6px,#fff 6px,#fff 12px)";
    return `<div class="lane"><div class="nm">${esc(t.id)}</div><div class="track">
      <div class="win" style="left:${l}%;width:${w}%;background:${col}${open}"></div>
      <div class="nowline" style="left:${nowp}%"></div></div></div>`;
  }).join("");
  const marks=[0,3,6,9,12,15,18,21].map(h=>`<span>${String(h).padStart(2,"0")}</span>`).join("");
  return lanes+`<div class="hours">${marks}<span style="flex:0">24</span></div>`;
}
function kindswap(k){
  document.getElementById("f-prompt").placeholder=k==="shell"?"Shell command to run":"What should the agent do?";
  document.getElementById("f-model").style.display=k==="llm"?"":"none";
}
async function schedule(ev){
  ev.preventDefault();
  const f=ev.target,fd=new FormData(f),k=fd.get("kind");
  const body={id:fd.get("id"),kind:k,priority:"med",on_budget_exceeded:fd.get("policy")};
  if(k==="shell")body.command=fd.get("prompt");else body.prompt=fd.get("prompt");
  if(k==="llm")body.model=fd.get("model")||"sonnet";
  if(k==="claude"&&fd.get("model"))body.claude_model=fd.get("model");
  if(fd.get("runs_on"))body.runs_on=fd.get("runs_on");
  const w={};if(fd.get("earliest"))w.earliest=fd.get("earliest");if(fd.get("deadline"))w.deadline=fd.get("deadline");
  if(Object.keys(w).length)body.window=w;
  const msg=document.getElementById("formmsg");
  const r=await fetch("/api/tasks",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const d=await r.json();
  if(r.ok){msg.style.color="var(--ink)";msg.textContent=`scheduled "${d.id}" — the conductor will pick it up.`;f.reset();kindswap("claude");tick();}
  else{msg.style.color="var(--red)";msg.textContent=d.error;}
  return false;
}
async function unschedule(id){
  if(!confirm(`remove "${id}" from the inbox?`))return;
  await fetch("/api/tasks/"+encodeURIComponent(id),{method:"DELETE"});tick();
}
async function retryTask(id){
  await fetch("/api/retry",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ids:[id]})});
  tick();
}
async function retryFailed(btn){
  if(btn){btn.textContent="queued ✓";setTimeout(()=>btn.textContent="retry failed ↻",2500);}
  await fetch("/api/retry",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
  tick();
}
async function claudeLogin(btn){
  if(btn){btn.textContent="opening Terminal…";setTimeout(()=>btn.textContent="sign in →",4000);}
  const r=await fetch("/api/claude-login",{method:"POST"});
  if(!r.ok){const d=await r.json();alert(d.error||"could not open Terminal");}
}
kindswap("claude");

let chatHistory=[],composeTasks=[];
const INTRO="What should go on the schedule? Tell me in plain words — e.g. “triage my repo's TODOs every morning at 7” or “back up the home server at 3am”. I know your existing tasks and connected machines.";
function bubble(role,html){return `<div style="margin:8px 0;display:flex;${role==="user"?"justify-content:flex-end":""}"><div style="max-width:80%;padding:9px 13px;border:2.5px solid var(--ink);border-radius:13px;background:${role==="user"?"var(--gold)":"#fff"};font-size:14px;white-space:pre-wrap;overflow-wrap:anywhere">${html}</div></div>`}
function taskCard(t){
  composeTasks.push(t);const i=composeTasks.length-1;
  return `<div style="border:2.5px solid var(--ink);border-radius:11px;background:var(--paper);padding:10px 12px;margin:8px 0">
   <b>${esc(t.id||"task")}</b> <span class="chip k-claude">${esc(t.kind||"claude")}${t.claude_model?" · "+esc(t.claude_model):""}</span><br>
   <span class="muted">${esc((t.prompt||t.command||"").slice(0,220))}</span><br>
   <span class="muted">${t.window?esc((t.window.earliest||"·")+"–"+(t.window.deadline||"·")):"anytime"} · ${esc(t.runs_on||"local")} · ${esc(t.on_budget_exceeded||"defer")}</span><br>
   <button class="bbtn" style="margin-top:7px" onclick="scheduleFromCard(this,${i})">schedule it →</button></div>`;
}
function renderAssistant(text){
  return esc(text).replace(/```json([\\s\\S]*?)```/g,(m,body)=>{
    try{const d=JSON.parse(body.replace(/&quot;/g,'"').replace(/&amp;/g,"&").replace(/&lt;/g,"<").replace(/&gt;/g,">"));
        if(d.tasks&&d.tasks.length)return d.tasks.map(taskCard).join("");}catch(e){}
    return `<pre style="white-space:pre-wrap">${body}</pre>`;});
}
function renderChat(extra){
  const el=document.getElementById("chat");
  const msgs=chatHistory.length?chatHistory.map(m=>bubble(m.role,m.role==="assistant"?renderAssistant(m.content):esc(m.content))).join(""):bubble("assistant",esc(INTRO));
  el.innerHTML=msgs+(extra||"");el.scrollTop=el.scrollHeight;
}
async function composeSend(ev){
  ev.preventDefault();
  const inp=document.getElementById("composeInput"),msg=inp.value.trim();
  if(!msg)return false;
  inp.value="";chatHistory.push({role:"user",content:msg});
  renderChat(bubble("assistant",'<i class="muted">the metronome is thinking…</i>'));
  try{
    const r=await fetch("/api/plan-chat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({history:chatHistory})});
    const d=await r.json();
    chatHistory.push({role:"assistant",content:r.ok?d.text:("⚠ "+(d.error||"error"))});
  }catch(e){chatHistory.push({role:"assistant",content:"⚠ engine unreachable"});}
  renderChat();return false;
}
async function scheduleFromCard(btn,i){
  const r=await fetch("/api/tasks",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(composeTasks[i])});
  const d=await r.json();
  btn.textContent=r.ok?"scheduled ✓":(d.error||"failed");
  if(r.ok){btn.disabled=true;tick();}
}
renderChat();

const PRESETS={blank:{image:"alpine:latest",setup:""},
  openclaw:{image:"node:22-bookworm",setup:"npm install -g openclaw"},
  hermes:{image:"python:3.12-slim",setup:"pip install hermes-agent"}};
function roomPreset(p){const d=PRESETS[p]||PRESETS.blank;
  document.getElementById("room-image").value=d.image;
  document.getElementById("room-setup").value=d.setup;}
async function roomCreate(ev){
  ev.preventDefault();
  const fd=new FormData(ev.target),msg=document.getElementById("roommsg");
  const body={name:fd.get("name"),image:fd.get("image")};
  if(fd.get("setup"))body.setup=fd.get("setup");
  if(fd.get("node"))body.node=fd.get("node");
  const r=await fetch("/api/rooms",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const d=await r.json();
  msg.style.color=r.ok?"var(--ink)":"var(--red)";
  msg.textContent=r.ok?`room "${d.name}" ready — assign it in schedule or compose`:(d.error||"failed");
  if(r.ok){ev.target.reset();roomPreset("blank");tick();}
  return false;
}
async function roomDelete(name){
  if(!confirm(`remove room "${name}"? (the container is left as-is)`))return;
  await fetch("/api/rooms/"+encodeURIComponent(name),{method:"DELETE"});tick();
}
async function meshJoin(ev){
  ev.preventDefault();
  const inp=document.getElementById("joincode"),msg=document.getElementById("joinmsg");
  const r=await fetch("/api/mesh/join",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({code:inp.value})});
  const d=await r.json();
  msg.style.color=r.ok?"var(--ink)":"var(--red)";
  msg.textContent=r.ok?`joined ${d.hub} — this machine now takes its work`:(d.error||"failed");
  if(r.ok)inp.value="";
  return false;
}
function copyPairing(code,btn){navigator.clipboard.writeText(code);btn.textContent="copied ✓";setTimeout(()=>btn.textContent="copy",2000);}

let expandedRun=null;
async function toggleRun(i,out){
  if(!out)return;
  const row=document.getElementById("rundetail-"+i);if(!row)return;
  const show=row.style.display==="none";
  row.style.display=show?"":"none";expandedRun=show?i:null;
  if(show){const pre=document.getElementById("runpre-"+i);
    if(pre.dataset.loaded!=="1"){const r=await fetch("/api/output/"+out);
      pre.textContent=r.ok?await r.text():"output unavailable";pre.dataset.loaded="1";}}
}
async function tick(){
  let d;try{d=await (await fetch("/api/state")).json()}catch(e){return}
  document.getElementById("planname").textContent=d.plan;
  document.getElementById("ts").textContent="refreshed "+new Date(d.generated_at).toLocaleTimeString();
  document.getElementById("n-tasks").textContent=d.tasks.length;
  document.getElementById("n-runs").textContent=d.ledger.length;
  document.getElementById("n-mem").textContent=d.memories.length;
  const nodes=d.nodes||[];
  document.getElementById("n-nodes").textContent=nodes.length+(d.rooms?d.rooms.length:0);
  document.getElementById("pairing").innerHTML=d.pairing?
    `<div class="muted">other Conductors paste this code to send work here:</div>
     <div style="display:flex;gap:8px;align-items:center;margin-top:8px">
       <code style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px">${esc(d.pairing.code)}</code>
       <button class="bbtn" onclick="copyPairing('${esc(d.pairing.code)}',this)">copy</button></div>
     <div class="muted" style="margin-top:6px">hub ${esc(d.pairing.hub)} · embedded, always on with the app</div>`
    :'<div class="empty">embedded hub not running (port 4747 busy?)</div>';
  document.getElementById("rooms").innerHTML=(d.rooms&&d.rooms.length)?
    `<table><tr><th>room</th><th>image</th><th>machine</th><th></th></tr>`+
    d.rooms.map(w=>`<tr><td><b>${esc(w.name)}</b>${w.setup?' <span class="tag">setup</span>':''}</td>
      <td class="muted">${esc(w.image)}</td><td>${esc(w.node||"this machine")}</td>
      <td><button class="del" onclick="roomDelete('${esc(w.name)}')" title="remove">✕</button></td></tr>`).join("")+"</table>"
    :'<div class="empty">no rooms yet — create one below (OpenClaw, Hermes, or blank)</div>';
  document.getElementById("agents").innerHTML=(d.totals&&d.totals.length)?
    `<table><tr><th>agent / task</th><th>runs</th><th>tokens in/out</th><th>cost</th></tr>`+
    d.totals.map(t=>`<tr><td><b>${esc(t.task_id)}</b></td><td>${t.runs}</td>
      <td>${fmt(t.in)} / ${fmt(t.out)}</td><td>$${t.cost_usd.toFixed(4)}</td></tr>`).join("")+"</table>"
    :'<div class="empty">no runs yet</div>';
  document.getElementById("nodelist").innerHTML=nodes.map(n=>`<option value="${esc(n.name)}">`).join("");
  const dot=on=>`<span style="display:inline-block;width:9px;height:9px;border-radius:99px;border:2px solid var(--ink);background:${on?"var(--teal)":"#ccc"};margin-right:7px"></span>`;
  document.getElementById("nodes").innerHTML=!d.hub?
    '<div class="empty">no mesh hub configured — add <code>mesh: { hub: "http://…:4747" }</code> to your plan, then run <code>conductor hub</code></div>':
    (nodes.length?`<table><tr><th>machine</th><th>status</th><th>last seen</th></tr>`+
      nodes.map(n=>`<tr><td>${dot(n.online)}<b>${esc(n.name)}</b></td>
      <td>${n.online?'<span class="chip s-done">online</span>':'<span class="chip">idle</span>'}</td>
      <td class="muted">${n.age_seconds==null?"—":n.age_seconds<90?Math.round(n.age_seconds)+"s ago":Math.round(n.age_seconds/60)+"m ago"}</td></tr>`).join("")+"</table>"
      :'<div class="empty">hub is up, but no workers have joined yet — run <code>conductor worker --hub '+esc(d.hub)+' --node &lt;name&gt; --allow-shell</code> on each machine</div>');
  document.getElementById("meshhelp").innerHTML=
    `<b>1.</b> On this machine (the hub): <code>conductor hub --host &lt;tailnet-ip&gt;</code><br>`+
    `<b>2.</b> Grab the zero-install worker: <code>conductor node-script -o conductor_worker.py</code>, copy it to any machine (a home server, an OrbStack Linux VM, a friend's box — just needs Python 3, no pip).<br>`+
    `<b>3.</b> There, <code>claude /login</code> once, then <code>python3 conductor_worker.py --hub ${d.hub?esc(d.hub):"http://&lt;ip&gt;:4747"} --node home --allow-shell</code><br>`+
    `<b>4.</b> Schedule any task with <b>runs on = home</b> — it runs there, on that machine's own Claude login. Your credentials never leave it.`;
  const bannerBtns=`<span style="flex:none;margin-left:auto;display:flex;gap:8px">
    <button class="bbtn" onclick="claudeLogin(this)">sign in →</button>
    <button class="bbtn" onclick="retryFailed(this)">retry failed ↻</button></span>`;
  document.getElementById("banner").innerHTML=
    d.claude_missing?`<div class="banner"><span style="font-size:20px">⚑</span><div>The <b>claude</b> CLI wasn't found on this machine. Install Claude Code first, then retry.</div>${bannerBtns}</div>`:
    d.login_needed?`<div class="banner"><span style="font-size:20px">⚑</span><div>A <b>claude</b> task failed because this machine isn't signed in. Click <b>sign in</b> — I'll open Terminal and type <code>claude /login</code> for you — finish in the browser, then hit <b>retry</b>.</div>${bannerBtns}</div>`:
    d.has_failed?`<div class="banner"><span style="font-size:20px">⚑</span><div>Some tasks failed — details in the tasks view.</div>${bannerBtns}</div>`:"";
  const cnt={};d.tasks.forEach(t=>cnt[t.state]=(cnt[t.state]||0)+1);
  document.getElementById("counts").innerHTML=["done","running","pending","failed"].map(s=>
    `<div class="stat"><b>${cnt[s]||0}</b><span>${s}</span></div>`).join("")+
    `<div class="stat"><b>$${d.budget.spent_today.toFixed(2)}</b><span>spent today</span></div>`;
  const b=d.budget;
  document.getElementById("budget").innerHTML=
    gauge("today",b.spent_today,b.daily_usd,"$")+(b.hourly_usd?gauge("rolling hour",b.spent_hour,b.hourly_usd,"$"):"");
  const q=d.quota,lv=q.live;
  if(lv&&lv.windows&&(lv.windows.five_hour||lv.windows.weekly)){
    const w5=lv.windows.five_hour,w7=lv.windows.weekly,wo=lv.windows.weekly_opus;
    document.getElementById("quota").innerHTML=
      (w5&&w5.pct!=null?gaugePct("5-hour window",w5.pct,w5.resets_at):"")+
      (w7&&w7.pct!=null?gaugePct("weekly window",w7.pct,w7.resets_at):"")+
      (wo&&wo.pct!=null?gaugePct("weekly · opus",wo.pct,wo.resets_at):"")+
      `<div class="muted"><span class="tag" style="background:var(--teal)">live</span> from your Claude account`+
      (lv.plan?` · plan <b>${esc(String(lv.plan).replace("claude_","claude "))}</b>`:"")+
      ` · local est ${fmt(q.five_hour.burn)} tok / 5h</div>`;
  }else{
    document.getElementById("quota").innerHTML=
      gauge("5-hour window",q.five_hour.burn,q.five_hour.ceiling,"tok",q.five_hour.resets_at)+
      gauge("weekly window",q.weekly.burn,q.weekly.ceiling,"tok",q.weekly.resets_at)+
      `<div class="muted">local estimate — live account data unavailable · reserve ${(q.reserve*100).toFixed(0)}%</div>`;
  }
  document.getElementById("timeline").innerHTML=d.tasks.length?timeline(d.tasks):'<div class="empty">no tasks yet — schedule one</div>';
  document.getElementById("tasks").innerHTML=d.tasks.length?`<table><tr><th>task</th><th>kind</th><th>where</th><th>window</th><th>deps</th><th>state</th><th></th></tr>`+
    d.tasks.map(t=>`<tr><td><b>${esc(t.id)}</b>${t.agentic?' <span class="tag">agentic</span>':''}${t.source==="inbox"?' <span class="tag" style="background:var(--gold)">inbox</span>':''}</td>
    <td><span class="chip ${t.kind==="claude"?"k-claude":""}">${esc(t.kind)}${t.model?" · "+esc(t.model):""}</span></td>
    <td>${esc(t.runs_on||"local")}</td><td>${esc(t.window)}</td>
    <td>${t.depends_on.map(esc).join(", ")||"—"}</td>
    <td>${chip(t.state)}${t.detail&&(t.state==="failed"||t.state==="skipped")?`<br><span class="muted" style="font-size:11.5px">${esc(t.detail.slice(0,90))}</span>`:""}</td>
    <td style="white-space:nowrap">${["failed","expired","skipped"].includes(t.state)?`<button class="del" onclick="retryTask('${esc(t.id)}')" title="retry">↻</button>`:""}${t.source==="inbox"?`<button class="del" onclick="unschedule('${esc(t.id)}')" title="remove from inbox">✕</button>`:""}</td></tr>`).join("")+"</table>":'<div class="empty">no tasks in plan</div>';
  if(expandedRun===null){
  document.getElementById("ledger").innerHTML=d.ledger.length?`<table><tr><th>when</th><th>task</th><th>tokens in/out</th><th>cost</th></tr>`+
    d.ledger.map((e,i)=>{
      const t=new Date(e.ts);
      const when=t.toLocaleDateString([],{month:"2-digit",day:"2-digit"})+" "+t.toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"});
      return `<tr style="cursor:${e.output?"pointer":"default"}" ${e.output?`onclick="toggleRun(${i},'${encodeURIComponent(e.output)}')" title="click for the output"`:""}>
      <td class="muted">${when}</td>
      <td><b>${esc(e.task_id)}</b>${e.node?` <span class="tag">@ ${esc(e.node)}</span>`:""}${e.output?' <span class="muted">▸</span>':''}<br>
        <span class="muted">${esc(e.model)}${e.turns?` · ${e.turns} turn${e.turns>1?"s":""}`:""}</span></td>
      <td>${fmt(e.in)} / ${fmt(e.out)}${e.cache?`<br><span class="muted">cache ${fmt(e.cache)}</span>`:""}</td>
      <td>${e.cost_usd?"$"+e.cost_usd.toFixed(4):(e.reported!=null?`<span class="tag">sub $${e.reported.toFixed(3)}</span>`:"$0")}</td></tr>
      <tr id="rundetail-${i}" style="display:none"><td colspan="4"><pre id="runpre-${i}" data-loaded="0" style="white-space:pre-wrap;font-size:12px;background:#fff;border:2px solid var(--ink);border-radius:9px;padding:10px;max-height:320px;overflow:auto;margin:4px 0">…</pre></td></tr>`;
    }).join("")+"</table>":'<div class="empty">nothing has run yet</div>';
  }
  document.getElementById("memory").innerHTML=d.memories.length?d.memories.map(m=>
    `<div style="padding:7px 0;border-bottom:1px solid #e4dcc6">
     <svg class="eye" width="15" height="11" viewBox="0 0 40 26"><path d="M2 13 Q20 -3 38 13 Q20 29 2 13 Z" fill="#fff" stroke="#111" stroke-width="3"/><circle cx="20" cy="13" r="6" fill="#27DBA2" stroke="#111" stroke-width="2"/><circle cx="20" cy="13" r="2.6" fill="#111"/></svg>
     <b>${esc(m.summary)}</b><br><span class="muted">${m.tags.map(t=>`<span class="tag">${esc(t)}</span>`).join("")} ${esc(m.source)} · ${m.created?new Date(m.created).toLocaleDateString():""}</span></div>`).join(""):'<div class="empty">no lessons learned yet — run an agentic task</div>';
}
tick();setInterval(tick,3000);
</script></body></html>"""


def make_handler(plan_path: Path):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")  # dashboards must never go stale
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: dict) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self) -> None:
            path = self.path.partition("?")[0]
            if path == "/":
                return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            if path == "/api/state":
                try:
                    # reload every request so inbox additions + plan edits show live
                    data = collect_state(load_plan(plan_path), plan_path)
                    return self._json(200, data)
                except Exception as exc:
                    return self._json(500, {"error": str(exc)})
            if path.startswith("/api/output/"):
                import urllib.parse
                name = Path(urllib.parse.unquote(path.rsplit("/", 1)[1])).name  # basename only
                f = plan_path.parent / ".conductor" / "outputs" / name
                if f.exists() and f.suffix == ".md":
                    return self._send(200, f.read_bytes()[:200_000],
                                      "text/plain; charset=utf-8")
                return self._json(404, {"error": "output not found"})
            return self._json(404, {})

        def do_POST(self) -> None:
            path = self.path.partition("?")[0]
            if path == "/api/tasks":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    raw = json.loads(self.rfile.read(n) or b"{}")
                    task = add_inbox_task(plan_path, raw)
                    return self._json(201, {"ok": True, "id": task.id})
                except Exception as exc:
                    return self._json(400, {"error": str(exc)})
            if path == "/api/retry":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n) or b"{}")
                    queued = request_retry(plan_path, body.get("ids"))
                    return self._json(200, {"ok": True, "queued": queued})
                except Exception as exc:
                    return self._json(400, {"error": str(exc)})
            if path == "/api/claude-login":
                try:
                    open_terminal_login()
                    return self._json(200, {"ok": True})
                except Exception as exc:
                    return self._json(400, {"error": str(exc)})
            if path == "/api/plan-chat":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n) or b"{}")
                    text = plan_chat(plan_path, body.get("history") or [])
                    return self._json(200, {"text": text})
                except Exception as exc:
                    return self._json(400, {"error": str(exc)})
            if path == "/api/rooms":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    name = add_room(plan_path, json.loads(self.rfile.read(n) or b"{}"))
                    return self._json(201, {"ok": True, "name": name})
                except Exception as exc:
                    return self._json(400, {"error": str(exc)})
            if path == "/api/mesh/join":
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n) or b"{}")
                    hub = join_mesh(plan_path, body.get("code") or "")
                    return self._json(200, {"ok": True, "hub": hub})
                except Exception as exc:
                    return self._json(400, {"error": str(exc)})
            return self._json(404, {})

        def do_DELETE(self) -> None:
            path = self.path.partition("?")[0]
            if path.startswith("/api/rooms/"):
                if remove_room(plan_path, path.rsplit("/", 1)[1]):
                    return self._json(200, {"ok": True})
                return self._json(409, {"error": "not a dashboard-created room"})
            if not path.startswith("/api/tasks/"):
                return self._json(404, {})
            task_id = path.rsplit("/", 1)[1]
            if remove_inbox_task(plan_path, task_id):
                return self._json(200, {"ok": True})
            return self._json(409, {"error": "not an inbox task (plan.yaml tasks are read-only here)"})

    return Handler


def serve(plan: Plan, plan_path: Path, host: str = "127.0.0.1",
          port: int = 4748) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(plan_path))
