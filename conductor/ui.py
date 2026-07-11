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

    # tasks + scheduler state
    run_state: dict[str, Any] = {}
    state_file = state_dir / "state.json"
    if state_file.exists():
        try:
            run_state = json.loads(state_file.read_text())
        except json.JSONDecodeError:
            run_state = {}
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
            "agentic": t.agentic,
            "source": "inbox" if t.id in inbox_ids else "plan",
        })

    # ledger recents + totals
    recents = [{
        "ts": e["ts"], "task_id": e["task_id"], "model": e["model"],
        "cost_usd": e["cost_usd"],
        "in": e["usage"].get("input_tokens", 0),
        "out": e["usage"].get("output_tokens", 0),
        "reported": e["usage"].get("reported_cost_usd"),
    } for e in ledger.entries[-25:]][::-1]

    memories = [{
        "summary": m.summary, "tags": m.tags, "source": m.source, "created": m.created,
    } for m in memory.all()[::-1][:30]]

    return {
        "plan": plan_path.name,
        "generated_at": dt.datetime.now().astimezone().isoformat(),
        "scheduler_updated_at": run_state.get("updated_at"),
        "budget": budget, "quota": quota, "tasks": tasks,
        "ledger": recents, "memories": memories,
        "hub": plan.mesh.hub,
    }


PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>conductor — ceci n'est pas un cron.</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 128 128'%3E%3Crect x='4' y='4' width='120' height='120' rx='26' fill='%23FDF6E3' stroke='%23111' stroke-width='4'/%3E%3Cpath d='M50 30 H74 L95 104 Q95 108 91 108 L33 108 Q29 108 29 104 Z' fill='%232456F5' stroke='%23111' stroke-width='4.5'/%3E%3Cpath d='M42 76 Q62 58 82 76 Q62 94 42 76 Z' fill='%23fff' stroke='%23111' stroke-width='4'/%3E%3Ccircle cx='62' cy='76' r='9.5' fill='%2327DBA2' stroke='%23111' stroke-width='3'/%3E%3Ccircle cx='62' cy='76' r='4.5' fill='%23111'/%3E%3C/svg%3E">
<style>
:root{--cream:#FDF6E3;--ink:#111111;--cobalt:#2456F5;--teal:#27DBA2;--gold:#FFB020;--red:#FF5A5A;--paper:#FFFCF3}
*{box-sizing:border-box;margin:0}
body{background:var(--cream);color:var(--ink);font:15px/1.6 -apple-system,"Helvetica Neue",Arial,sans-serif;padding:28px 20px 60px}
.wrap{max-width:1040px;margin:0 auto}
header{display:flex;align-items:center;gap:18px;margin-bottom:6px}
header svg{flex:none}
h1{font-size:30px;letter-spacing:-.5px;font-weight:700}
.tagline{font-family:Georgia,serif;font-style:italic;color:#555;margin-bottom:26px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px;margin-bottom:16px}
.card{background:var(--paper);border:3px solid var(--ink);border-radius:16px;padding:18px 20px}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.12em;margin-bottom:12px;display:flex;justify-content:space-between;align-items:baseline}
.card h2 small{font-weight:400;text-transform:none;letter-spacing:0;color:#777;font-family:Georgia,serif;font-style:italic}
.gauge{margin:10px 0 4px}
.gauge .lbl{display:flex;justify-content:space-between;font-size:13px;margin-bottom:5px}
.bar{height:16px;border:2.5px solid var(--ink);border-radius:9px;background:#fff;overflow:hidden}
.bar i{display:block;height:100%;transition:width .6s}
.muted{color:#777;font-size:12.5px}
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
footer{margin-top:26px;text-align:center;color:#999;font-size:12px;font-family:Georgia,serif;font-style:italic}
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
</style></head><body><div class="wrap">
<header>
<svg width="64" height="64" viewBox="0 0 128 128"><rect x="4" y="4" width="120" height="120" rx="26" fill="#FDF6E3" stroke="#111" stroke-width="4"/><path d="M50 30 H74 L95 104 Q95 108 91 108 L33 108 Q29 108 29 104 Z" fill="#2456F5" stroke="#111" stroke-width="4.5"/><path d="M42 76 Q62 58 82 76 Q62 94 42 76 Z" fill="#fff" stroke="#111" stroke-width="4"/><circle cx="62" cy="76" r="9.5" fill="#27DBA2" stroke="#111" stroke-width="3"/><circle cx="62" cy="76" r="4.5" fill="#111"/><circle cx="64.5" cy="73" r="1.8" fill="#fff"/><line x1="86" y1="33" x2="100" y2="19" stroke="#111" stroke-width="6" stroke-linecap="round"/><circle cx="106" cy="13" r="7" fill="#FFB020" stroke="#111" stroke-width="4"/></svg>
<div><h1>conductor</h1><div class="tagline">ceci n'est pas un cron.</div></div>
</header>
<div class="grid">
<div class="card"><h2>api budget <small>usd, hard gate</small></h2><div id="budget"></div></div>
<div class="card"><h2>subscription quota <small>burn units, auto defer/downgrade</small></h2><div id="quota"></div></div>
</div>
<div class="card" style="margin-bottom:16px"><h2>today's timeline <small>windows over 24h · red line = now</small></h2><div id="timeline"></div></div>
<div class="card" style="margin-bottom:16px"><h2>schedule a task <small>lands in .conductor/inbox.yaml — a running scheduler picks it up live</small></h2>
<form class="sched" id="schedform" onsubmit="return schedule(event)">
<div><label>id</label><input name="id" placeholder="nightly-report" required></div>
<div><label>kind</label><select name="kind" onchange="kindswap(this.value)"><option value="claude">claude (subscription)</option><option value="llm">llm (api)</option><option value="shell">shell</option></select></div>
<div id="f-model"><label>model</label><input name="model" placeholder="sonnet" value="sonnet"></div>
<div><label>earliest</label><input name="earliest" type="time"></div>
<div><label>deadline</label><input name="deadline" type="time"></div>
<div><label>if over budget</label><select name="policy"><option>defer</option><option>downgrade</option><option>skip</option></select></div>
<div><label>runs on</label><input name="runs_on" placeholder="local"></div>
<textarea name="prompt" id="f-prompt" placeholder="What should the agent do?" required></textarea>
<button>schedule it</button><div id="formmsg"></div>
</form></div>
<div class="card" style="margin-bottom:16px"><h2>tasks <small id="planname"></small></h2><div id="tasks"></div></div>
<div class="grid">
<div class="card"><h2>recent runs</h2><div id="ledger"></div></div>
<div class="card"><h2>long-term memory <small>what it learned</small></h2><div id="memory"></div></div>
</div>
<footer>the metronome that watches your spend — refreshed <span id="ts">…</span></footer>
</div>
<script>
const fmt=n=>n>=1e6?(n/1e6).toFixed(1)+"M":n>=1e3?(n/1e3).toFixed(1)+"k":String(n);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function gauge(label,used,cap,unit,reset){
  if(cap==null||!cap) return `<div class="gauge"><div class="lbl"><span>${label}</span><span>${fmt(used)} ${unit} · no ceiling set</span></div><div class="muted">calibrate in plan.yaml to enable the gate</div></div>`;
  const p=Math.min(100,100*used/cap);
  const col=p<60?"var(--teal)":p<85?"var(--gold)":"var(--red)";
  const r=reset?` · resets ${new Date(reset).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"})}`:"";
  return `<div class="gauge"><div class="lbl"><span>${label}</span><span>${fmt(used)} / ${fmt(cap)} ${unit}${r}</span></div><div class="bar"><i style="width:${p}%;background:${col}"></i></div></div>`;
}
function chip(s){return `<span class="chip s-${esc(s)}">${esc(s)}</span>`}
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
kindswap("claude");
async function tick(){
  let d;try{d=await (await fetch("/api/state")).json()}catch(e){return}
  document.getElementById("planname").textContent=d.plan;
  document.getElementById("ts").textContent=new Date(d.generated_at).toLocaleTimeString();
  const b=d.budget;
  document.getElementById("budget").innerHTML=
    gauge("today",b.spent_today,b.daily_usd,"$")+(b.hourly_usd?gauge("rolling hour",b.spent_hour,b.hourly_usd,"$"):"");
  const q=d.quota;
  document.getElementById("quota").innerHTML=
    gauge("5-hour window",q.five_hour.burn,q.five_hour.ceiling,"tok",q.five_hour.resets_at)+
    gauge("weekly window",q.weekly.burn,q.weekly.ceiling,"tok",q.weekly.resets_at)+
    `<div class="muted">reserve ${(q.reserve*100).toFixed(0)}% kept for interactive use</div>`;
  document.getElementById("timeline").innerHTML=d.tasks.length?timeline(d.tasks):'<div class="empty">no tasks yet — schedule one above</div>';
  document.getElementById("tasks").innerHTML=d.tasks.length?`<table><tr><th>task</th><th>kind</th><th>where</th><th>window</th><th>deps</th><th>state</th><th></th></tr>`+
    d.tasks.map(t=>`<tr><td><b>${esc(t.id)}</b>${t.agentic?' <span class="tag">agentic</span>':''}${t.source==="inbox"?' <span class="tag" style="background:var(--gold)">inbox</span>':''}</td>
    <td><span class="chip ${t.kind==="claude"?"k-claude":""}">${esc(t.kind)}${t.model?" · "+esc(t.model):""}</span></td>
    <td>${esc(t.runs_on||"local")}</td><td>${esc(t.window)}</td>
    <td>${t.depends_on.map(esc).join(", ")||"—"}</td><td>${chip(t.state)}</td>
    <td>${t.source==="inbox"?`<button class="del" onclick="unschedule('${esc(t.id)}')" title="remove from inbox">✕</button>`:""}</td></tr>`).join("")+"</table>":'<div class="empty">no tasks in plan</div>';
  document.getElementById("ledger").innerHTML=d.ledger.length?`<table><tr><th>when</th><th>task</th><th>tok in/out</th><th>cost</th></tr>`+
    d.ledger.map(e=>`<tr><td class="muted">${new Date(e.ts).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"})}</td>
    <td><b>${esc(e.task_id)}</b><br><span class="muted">${esc(e.model)}</span></td>
    <td>${fmt(e.in)} / ${fmt(e.out)}</td>
    <td>${e.cost_usd?"$"+e.cost_usd.toFixed(4):(e.reported!=null?`<span class="tag">sub $${e.reported.toFixed(3)}</span>`:"$0")}</td></tr>`).join("")+"</table>":'<div class="empty">nothing has run yet</div>';
  document.getElementById("memory").innerHTML=d.memories.length?d.memories.map(m=>
    `<div style="padding:7px 0;border-bottom:1px solid #e4dcc6">
     <svg class="eye" width="15" height="11" viewBox="0 0 40 26"><path d="M2 13 Q20 -3 38 13 Q20 29 2 13 Z" fill="#fff" stroke="#111" stroke-width="3"/><circle cx="20" cy="13" r="6" fill="#27DBA2" stroke="#111" stroke-width="2"/><circle cx="20" cy="13" r="2.6" fill="#111"/></svg>
     <b>${esc(m.summary)}</b><br><span class="muted">${m.tags.map(t=>`<span class="tag">${esc(t)}</span>`).join("")} ${esc(m.source)} · ${m.created?new Date(m.created).toLocaleDateString():""}</span></div>`).join(""):'<div class="empty">the agent hasn\\'t learned anything yet — run an agentic task</div>';
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
            return self._json(404, {})

        def do_POST(self) -> None:
            path = self.path.partition("?")[0]
            if path != "/api/tasks":
                return self._json(404, {})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = json.loads(self.rfile.read(n) or b"{}")
                task = add_inbox_task(plan_path, raw)
                return self._json(201, {"ok": True, "id": task.id})
            except Exception as exc:
                return self._json(400, {"error": str(exc)})

        def do_DELETE(self) -> None:
            path = self.path.partition("?")[0]
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
