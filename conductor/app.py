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
import os
import socket
import threading
import time
from pathlib import Path
from typing import Optional

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
            # serve mode: never returns — handles dashboard retries, inbox
            # pickup, and daily re-runs internally
            asyncio.run(sched.run(serve=True))
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
        "conductor",
        f"http://127.0.0.1:{port}/?app=1",   # ?app=1 → UI leaves room for traffic lights
        width=1180, height=880, min_size=(820, 600),
        background_color="#FDF6E3",
        easy_drag=False,  # we drive dragging via CSS drag regions + movable background
    )
    log_path = plan_path.parent / ".conductor" / "app.log"

    def _chrome_log(msg: str) -> None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a") as f:
                f.write(f"{dt.datetime.now().strftime('%H:%M:%S')} chrome   {msg}\n")
        except OSError:
            pass

    # apply native chrome once the window is realized (reliable timing)
    try:
        window.events.shown += lambda: _apply_native_chrome(window, _chrome_log)
    except Exception:
        pass
    webview.start()  # blocks until the window closes
    server.shutdown()
    return 0


def _apply_native_chrome(window, log) -> None:
    """macOS: unified titlebar — transparent, full-size content, hidden title,
    so the traffic lights sit ON the cream canvas (OrbStack-style). Uses only
    AppKit (bundled with pywebview's cocoa backend — no PyObjCTools, which
    PyInstaller doesn't trace) and dispatches to the main thread via
    NSOperationQueue. Logs the outcome so failures are never silent."""
    try:
        import AppKit
        from webview.platforms.cocoa import BrowserView

        def apply() -> None:
            try:
                inst = BrowserView.instances.get(window.uid)
                if inst is None and BrowserView.instances:
                    inst = list(BrowserView.instances.values())[0]
                nswindow = getattr(inst, "window", None) if inst else None
                if nswindow is None:
                    log("no NSWindow found — titlebar left as-is")
                    return
                nswindow.setStyleMask_(nswindow.styleMask() | (1 << 15))  # FullSizeContentView
                nswindow.setTitlebarAppearsTransparent_(True)
                nswindow.setTitleVisibility_(1)  # NSWindowTitleHidden
                nswindow.setMovableByWindowBackground_(True)
                nswindow.setBackgroundColor_(
                    AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.992, 0.965, 0.890, 1.0))
                log("unified titlebar applied ✓")
            except Exception as exc:
                log(f"titlebar styling failed: {type(exc).__name__}: {exc}")

        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(apply)
    except Exception as exc:
        log(f"chrome unavailable: {type(exc).__name__}: {exc}")


def desktop_main() -> int:
    """Entry point for the packaged .app: default plan + embedded scheduler."""
    return run_app(ensure_default_plan(), embed_scheduler=True)


def run_headless(plan_path: Path, port: int, parent_pid: Optional[int] = None) -> int:
    """The engine without a window — what the Tauri shell runs as a sidecar.
    UI server + serve-mode scheduler in this process; prints a READY line so
    the shell knows when to show the window. If parent_pid is given, exits
    when that process dies (no orphaned engines)."""
    server = ui_serve(load_plan(plan_path), plan_path, host="127.0.0.1", port=port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=_scheduler_forever, args=(plan_path,), daemon=True).start()
    print(f"CONDUCTOR_SERVE_READY port={port}", flush=True)

    try:
        while True:
            if parent_pid is not None:
                try:
                    os.kill(parent_pid, 0)   # signal 0 = existence check
                except OSError:
                    break                     # shell died — follow it
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    server.shutdown()
    return 0
