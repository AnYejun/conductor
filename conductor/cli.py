"""CLI — conductor run / status / cost."""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .estimator import estimate
from .ledger import Ledger
from .scheduler import Scheduler, State
from .schema import Plan, load_plan

console = Console()


def _state_dir(plan_path: Path) -> Path:
    return plan_path.parent / ".conductor"


def _ledger(plan_path: Path) -> Ledger:
    return Ledger(_state_dir(plan_path) / "ledger.json")


def _budget_line(plan: Plan, ledger: Ledger) -> str:
    spent = ledger.spent_today()
    line = f"today ${spent:.4f} / ${plan.budget.daily_usd:.2f}"
    if plan.budget.hourly_usd:
        line += f"  ·  last hour ${ledger.spent_last_hour():.4f} / ${plan.budget.hourly_usd:.2f}"
    return line


# -- commands ------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    plan_path = Path(args.plan).resolve()
    plan = load_plan(plan_path)
    ledger = _ledger(plan_path)
    console.print(f"[bold]conductor[/bold] · {len(plan.tasks)} tasks · budget: {_budget_line(plan, ledger)}")

    sched = Scheduler(
        plan, ledger,
        outputs_dir=_state_dir(plan_path) / "outputs",
        console=console,
        tick_seconds=args.tick,
        plan_path=plan_path,  # live pickup of dashboard-scheduled tasks
    )
    final = asyncio.run(sched.run(once=args.once))

    counts: dict[str, int] = {}
    for s in final.values():
        counts[s.value] = counts.get(s.value, 0) + 1
    summary = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    console.print(f"\n[bold]finished[/bold] · {summary} · spend {_budget_line(plan, ledger)}")
    return 1 if counts.get("failed") else 0


def cmd_status(args: argparse.Namespace) -> int:
    plan_path = Path(args.plan).resolve()
    plan = load_plan(plan_path)
    ledger = _ledger(plan_path)

    async def _estimates() -> dict[str, tuple[int, float]]:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic()
        out: dict[str, tuple[int, float]] = {}
        for t in plan.tasks:
            if t.kind.value != "llm":
                continue  # shell tasks have no API cost
            m = plan.models[t.model]
            e = await estimate(client, t, m)
            out[t.id] = (e.input_tokens, e.est_usd)
        return out

    estimates: dict[str, tuple[int, float]] = {}
    if not args.no_preflight:
        try:
            estimates = asyncio.run(_estimates())
        except Exception as exc:
            console.print(f"[yellow]pre-flight estimation unavailable ({type(exc).__name__}) "
                          f"— showing plan without estimates[/yellow]")

    table = Table(title=f"plan: {plan_path.name}")
    table.add_column("task", style="bold")
    table.add_column("kind/model")
    table.add_column("runs on")
    table.add_column("window")
    table.add_column("prio")
    table.add_column("policy")
    table.add_column("deps")
    table.add_column("est in-tok", justify="right")
    table.add_column("worst-case $", justify="right")

    total_worst = 0.0
    for t in plan.tasks:
        w = t.window
        win = f"{w.earliest or '·'}–{w.deadline or '·'}" if (w.earliest or w.deadline) else "anytime"
        est_tok, est_usd = ("—", "—"), None
        if t.id in estimates:
            tok, usd = estimates[t.id]
            est_tok = f"{tok:,}"
            est_usd = usd
            total_worst += usd
        if t.kind.value == "llm":
            what = f"{t.model} ({plan.models[t.model].id})"
        elif t.kind.value == "claude":
            what = f"claude·sub ({t.claude_model or 'default'})"
        else:
            what = "shell" + (f" [{t.container}]" if t.container else "")
        free_label = {"shell": "$0", "claude": "sub"}.get(t.kind.value, "—")
        table.add_row(
            t.id, what, t.runs_on or "local", win,
            t.priority.value, t.on_budget_exceeded.value,
            ", ".join(t.depends_on) or "—",
            est_tok if isinstance(est_tok, str) else "—",
            f"${est_usd:.4f}" if est_usd is not None else free_label,
        )
    console.print(table)
    if estimates:
        verdict = "[green]fits[/green]" if total_worst <= plan.budget.daily_usd else "[red]exceeds[/red]"
        console.print(f"worst-case total ${total_worst:.4f} vs daily budget "
                      f"${plan.budget.daily_usd:.2f} → {verdict}")
    console.print(f"budget: {_budget_line(plan, ledger)}")
    return 0


def cmd_cost(args: argparse.Namespace) -> int:
    plan_path = Path(args.plan).resolve()
    plan = load_plan(plan_path)
    ledger = _ledger(plan_path)

    agg = ledger.by_task()
    if not agg:
        console.print("no spend recorded yet")
        return 0

    table = Table(title="spend by task (all time)")
    table.add_column("task", style="bold")
    table.add_column("runs", justify="right")
    table.add_column("in-tok", justify="right")
    table.add_column("out-tok", justify="right")
    table.add_column("cost", justify="right")
    table.add_column("last model")
    table.add_column("last run")
    for task_id, row in sorted(agg.items(), key=lambda kv: -kv[1]["cost_usd"]):
        last = dt.datetime.fromisoformat(row["last_run"]).strftime("%m-%d %H:%M")
        table.add_row(
            task_id, str(row["runs"]),
            f"{row['input_tokens']:,}", f"{row['output_tokens']:,}",
            f"${row['cost_usd']:.4f}", row["model"], last,
        )
    console.print(table)
    console.print(f"budget: {_budget_line(plan, ledger)}")
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    from .memory import MemoryStore

    plan_path = Path(args.plan).resolve()
    store = MemoryStore(_state_dir(plan_path) / "memory")

    if args.query:
        mems = store.recall(args.query, k=args.k)
        title = f"memory · recall: {args.query!r}"
    else:
        mems = store.all()
        title = "long-term memory (all)"

    if not mems:
        console.print("no memories yet — agentic tasks write them via the `remember` tool")
        return 0

    table = Table(title=title)
    table.add_column("summary", style="bold")
    table.add_column("tags")
    table.add_column("source")
    table.add_column("created")
    for m in mems:
        created = m.created[:16].replace("T", " ") if m.created else "—"
        table.add_row(m.summary, ", ".join(m.tags) or "—", m.source or "—", created)
    console.print(table)
    console.print(f"{len(mems)} memories · store: {store.dir}")
    return 0


def cmd_quota(args: argparse.Namespace) -> int:
    from .quota import QuotaMonitor

    plan_path = Path(args.plan).resolve()
    plan = load_plan(plan_path)
    snap = QuotaMonitor(ledger=_ledger(plan_path)).snapshot(plan.subscription)

    table = Table(title="subscription quota (burn = in + out + cache-write tokens)")
    table.add_column("window", style="bold")
    table.add_column("burn", justify="right")
    table.add_column("ceiling", justify="right")
    table.add_column("remaining", justify="right")
    table.add_column("resets")
    for w in (snap.five_hour, snap.weekly):
        rem = f"{w.remaining_fraction:.0%}" if w.remaining_fraction is not None else "—"
        resets = w.resets_at.astimezone().strftime("%m-%d %H:%M") if w.resets_at else "—"
        ceiling = f"{w.ceiling:,}" if w.ceiling else "[dim]not set[/dim]"
        table.add_row(w.name, f"{w.burn:,}", ceiling, rem, resets)
    console.print(table)
    if not (plan.subscription.five_hour_tokens or plan.subscription.weekly_tokens):
        console.print("[yellow]no ceilings configured[/yellow] — watch burn for a few days, "
                      "then set subscription.five_hour_tokens / weekly_tokens in the plan "
                      "to enable automatic defer/downgrade for kind=claude tasks")
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    from .ui import serve as ui_serve

    plan_path = Path(args.plan).resolve()
    plan = load_plan(plan_path)
    server = ui_serve(plan, plan_path, host=args.host, port=args.port)
    console.print(f"[bold]conductor ui[/bold] → http://{args.host}:{args.port}  "
                  f"(plan: {plan_path.name}, refreshes live)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("bye")
    return 0


def cmd_app(args: argparse.Namespace) -> int:
    from .app import ensure_default_plan, run_app

    plan_path = Path(args.plan).resolve()
    if not plan_path.exists() and args.plan == "plan.yaml":
        plan_path = ensure_default_plan()   # app-style: fall back to ~/.conductor
        console.print(f"[dim]no plan.yaml here — using {plan_path}[/dim]")
    console.print("[bold]conductor app[/bold] — opening the dashboard in a native window…")
    return run_app(plan_path, port=args.port, embed_scheduler=args.scheduler)


def cmd_hub(args: argparse.Namespace) -> int:
    import os
    from .hub import serve

    token = os.environ.get("CONDUCTOR_TOKEN")
    state_path = Path(args.state).expanduser()
    server = serve(args.host, args.port, state_path, token)
    console.print(f"[bold]conductor hub[/bold] on http://{args.host}:{args.port}  "
                  f"(auth: {'token' if token else 'none — loopback only'}, state: {state_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("bye")
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    import os
    from .worker import run_worker

    run_worker(
        hub_url=args.hub or os.environ.get("CONDUCTOR_HUB") or "http://127.0.0.1:4747",
        node=args.node,
        token=os.environ.get("CONDUCTOR_TOKEN"),
        allow_shell=args.allow_shell,
        workdir=args.workdir,
        log=console.print,
    )
    return 0


def cmd_node_script(args: argparse.Namespace) -> int:
    """Emit the zero-dependency worker so it can be dropped on any machine."""
    src = (Path(__file__).parent / "standalone_worker.py").read_text()
    if args.out:
        out = Path(args.out).expanduser()
        out.write_text(src)
        out.chmod(0o755)
        console.print(f"wrote portable worker → {out}")
        console.print("[dim]copy it to any machine (Python 3.8+, no pip) and run:[/dim]")
        console.print(f"  python3 {out.name} --hub <hub-url> --node <name> --allow-shell")
    else:
        sys.stdout.write(src)
    return 0


def cmd_nodes(args: argparse.Namespace) -> int:
    import os

    import httpx

    base = args.hub or os.environ.get("CONDUCTOR_HUB") or "http://127.0.0.1:4747"
    token = os.environ.get("CONDUCTOR_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    health = httpx.get(f"{base}/v0/health", headers=headers, timeout=10).json()

    table = Table(title=f"mesh @ {base}")
    table.add_column("node", style="bold")
    table.add_column("last seen")
    for name, info in sorted(health.get("nodes", {}).items()):
        last = dt.datetime.fromisoformat(info["last_seen"])
        age = (dt.datetime.now().astimezone() - last).total_seconds()
        table.add_row(name, f"{int(age)}s ago" if age < 120 else last.strftime("%m-%d %H:%M"))
    console.print(table)
    items = health.get("items", {})
    console.print("queue: " + (", ".join(f"{k}={v}" for k, v in sorted(items.items())) or "empty"))
    return 0


# -- entry -------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="conductor",
        description="Token-budget-aware task scheduler for the Claude API.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run the scheduler loop")
    p_run.add_argument("plan", nargs="?", default="plan.yaml")
    p_run.add_argument("--once", action="store_true",
                       help="single pass: dispatch what's eligible now, then exit")
    p_run.add_argument("--tick", type=int, default=60, help="scheduler tick seconds (default 60)")
    p_run.set_defaults(fn=cmd_run)

    p_status = sub.add_parser("status", help="show plan + pre-flight cost estimates")
    p_status.add_argument("plan", nargs="?", default="plan.yaml")
    p_status.add_argument("--no-preflight", action="store_true",
                          help="skip count_tokens estimation (no API access needed)")
    p_status.set_defaults(fn=cmd_status)

    p_cost = sub.add_parser("cost", help="show ledger: what ran, when, and what it cost")
    p_cost.add_argument("plan", nargs="?", default="plan.yaml")
    p_cost.set_defaults(fn=cmd_cost)

    p_hub = sub.add_parser("hub", help="run the mesh hub (work queue for your other machines)")
    p_hub.add_argument("--host", default="127.0.0.1",
                       help="bind address; non-loopback requires $CONDUCTOR_TOKEN")
    p_hub.add_argument("--port", type=int, default=4747)
    p_hub.add_argument("--state", default="~/.conductor/hub-state.json")
    p_hub.set_defaults(fn=cmd_hub)

    p_worker = sub.add_parser("worker", help="lend this machine to the mesh (polls the hub)")
    p_worker.add_argument("--hub", help="hub URL (default $CONDUCTOR_HUB or http://127.0.0.1:4747)")
    p_worker.add_argument("--node", help="node name (default: hostname)")
    p_worker.add_argument("--allow-shell", action="store_true",
                          help="allow kind=shell tasks on this machine (opt-in RCE — read the docs)")
    p_worker.add_argument("--workdir", help="working directory for shell tasks")
    p_worker.set_defaults(fn=cmd_worker)

    p_nodes = sub.add_parser("nodes", help="show mesh nodes and queue state")
    p_nodes.add_argument("--hub", help="hub URL (default $CONDUCTOR_HUB or http://127.0.0.1:4747)")
    p_nodes.set_defaults(fn=cmd_nodes)

    p_ns = sub.add_parser("node-script",
                          help="emit a zero-dependency worker to drop on any machine (Python-only, no pip)")
    p_ns.add_argument("--out", "-o", help="write to a file (default: print to stdout)")
    p_ns.set_defaults(fn=cmd_node_script)

    p_mem = sub.add_parser("memory", help="inspect the agent's long-term memory")
    p_mem.add_argument("plan", nargs="?", default="plan.yaml")
    p_mem.add_argument("-q", "--query", help="recall memories relevant to a query")
    p_mem.add_argument("-k", type=int, default=8, help="max results for --query")
    p_mem.set_defaults(fn=cmd_memory)

    p_quota = sub.add_parser("quota", help="show subscription burn: 5h + weekly windows")
    p_quota.add_argument("plan", nargs="?", default="plan.yaml")
    p_quota.set_defaults(fn=cmd_quota)

    p_ui = sub.add_parser("ui", help="open the live dashboard (budget, quota, tasks, memory)")
    p_ui.add_argument("plan", nargs="?", default="plan.yaml")
    p_ui.add_argument("--host", default="127.0.0.1")
    p_ui.add_argument("--port", type=int, default=4748)
    p_ui.set_defaults(fn=cmd_ui)

    p_app = sub.add_parser("app", help="the dashboard as a native desktop window (pywebview)")
    p_app.add_argument("plan", nargs="?", default="plan.yaml")
    p_app.add_argument("--port", type=int, default=0, help="UI port (default: auto)")
    p_app.add_argument("--scheduler", action="store_true",
                       help="also run the scheduler inside the app (what the packaged .app does)")
    p_app.set_defaults(fn=cmd_app)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]error:[/red] {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
