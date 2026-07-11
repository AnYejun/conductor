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

import socket
import threading
from pathlib import Path

from .schema import load_plan
from .ui import serve as ui_serve


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_app(plan_path: Path, port: int = 0) -> int:
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

    window = webview.create_window(
        "conductor — ceci n'est pas un cron.",
        f"http://127.0.0.1:{port}",
        width=1180, height=880, min_size=(760, 560),
        background_color="#FDF6E3",
    )
    webview.start()  # blocks until the window closes
    server.shutdown()
    return 0
