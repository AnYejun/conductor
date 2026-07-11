"""`conductor app` — the dashboard as a native desktop window.

The dashboard is already a local web app (`conductor ui`); this wraps it in a
native window via pywebview (WKWebView on macOS, WebView2 on Windows, GTK on
Linux) — web-app feel, no Electron, no JS toolchain, ~one extra dependency.

    pip install "conductor-agent[app]"
    conductor app

The UI server runs in a background thread on a loopback port; closing the
window stops everything.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import socket
import threading
import time
from pathlib import Path

from .schema import load_plan
from .ui import serve as ui_serve

STARTER_PLAN = """\
# conductor plan — created on first launch. Edit freely; the app reloads live.
budget:
  daily_usd: 5.00

# Calibrate with the quota view, then uncomment to gate kind=claude tasks:
# subscription:
#   five_hour_tokens: 6000000
#   weekly_tokens: 300000000
#   reserve: 0.15

models:
  # USD per 1M tokens — verify at https://platform.claude.com/docs/en/pricing
  opus:   { id: claude-opus-4-8,  price_in: 5.00, price_out: 25.00 }
  sonnet: { id: claude-sonnet-5,  price_in: 3.00, price_out: 15.00 }
  haiku:  { id: claude-haiku-4-5, price_in: 1.00, price_out: 5.00 }

tasks:
  - id: hello-conductor
    kind: claude          # runs on your Claude subscription (claude /login)
    claude_model: haiku
    prompt: "In one sentence, greet your new owner and explain what you are: a scheduled agent run by conductor."
    on_budget_exceeded: defer
"""


def default_plan_path() -> Path:
    return Path.home() / ".conductor" / "plan.yaml"


def ensure_default_plan() -> Path:
    """First-run bootstrap: a real app double-clicked from Finder has no cwd
    plan — give it a home at ~/.conductor/plan.yaml with a starter template."""
    path = default_plan_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(STARTER_PLAN)
    return path


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _scheduler_forever(plan_path: Path) -> None:
    """Embedded scheduler: the .app is self-sufficient — no terminal needed.
    Runs the plan; when everything settles, waits for a new calendar day
    (daily windows re-run) or new dashboard-scheduled tasks, then goes again.
    Logs to ~/.conductor/app.log."""
    from rich.console import Console

    from .ledger import Ledger
    from .schema import load_inbox_tasks
    from .scheduler import Scheduler

    state_dir = plan_path.parent / ".conductor"
    state_dir.mkdir(parents=True, exist_ok=True)
    log = (state_dir / "app.log").open("a")
    console = Console(file=log, width=110)

    while True:
        try:
            plan = load_plan(plan_path)
            sched = Scheduler(
                plan, Ledger(state_dir / "ledger.json"),
                outputs_dir=state_dir / "outputs",
                console=console, tick_seconds=30, plan_path=plan_path,
            )
            known = set(sched.state)
            day = dt.date.today()
            asyncio.run(sched.run())
            log.flush()
            # settled — wake on a new day (windows re-run) or new inbox work
            while dt.date.today() == day:
                try:
                    fresh = load_inbox_tasks(load_plan(plan_path, include_inbox=False), plan_path)
                except Exception:
                    fresh = []
                if any(t.id not in known for t in fresh):
                    break
                time.sleep(30)
        except Exception as exc:  # keep the app alive whatever the plan does
            console.print(f"[red]scheduler error:[/red] {exc}")
            log.flush()
            time.sleep(60)


def run_app(plan_path: Path, port: int = 0, embed_scheduler: bool = False) -> int:
    try:
        import webview
    except ImportError:
        raise SystemExit(
            'the desktop app needs pywebview — install with:\n'
            '  pip install "conductor-agent[app]"'
        )

    plan = load_plan(plan_path)
    port = port or _free_port()
    server = ui_serve(plan, plan_path, host="127.0.0.1", port=port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if embed_scheduler:
        threading.Thread(target=_scheduler_forever, args=(plan_path,), daemon=True).start()

    window = webview.create_window(
        "conductor — ceci n'est pas un cron.",
        f"http://127.0.0.1:{port}/?app=1",   # ?app=1 → UI leaves room for traffic lights
        width=1180, height=880, min_size=(760, 560),
        background_color="#FDF6E3",
    )
    webview.start(func=_unify_titlebar)  # blocks until the window closes
    server.shutdown()
    return 0


def _unify_titlebar() -> None:
    """macOS: make the title bar transparent and let content flow under it, so
    the traffic lights sit directly on the cream canvas (Hermes-style unified
    chrome). No-op anywhere it can't apply — the app still works framed."""
    try:
        import AppKit
        from PyObjCTools import AppHelper

        def apply() -> None:
            for w in AppKit.NSApp.windows():
                w.setStyleMask_(w.styleMask() | AppKit.NSWindowStyleMaskFullSizeContentView)
                w.setTitlebarAppearsTransparent_(True)
                w.setTitleVisibility_(AppKit.NSWindowTitleHidden)
                w.setBackgroundColor_(
                    AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.9922, 0.9647, 0.8902, 1.0))

        AppHelper.callAfter(apply)
    except Exception:
        pass


def desktop_main() -> int:
    """Entry point for the packaged .app: default plan + embedded scheduler."""
    return run_app(ensure_default_plan(), embed_scheduler=True)
