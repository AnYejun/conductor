"""Mesh hub — a small work-queue HTTP server (stdlib only, zero extra deps).

Workers on your other machines connect OUTBOUND (long-poll), so devices behind
NAT/home routers need no port forwarding — only the hub needs to be reachable
(bind it to a tailnet/VPN address, or run it on the machine that schedules).

Security model:
- Loopback bind (default) → token optional.
- Non-loopback bind → a bearer token is REQUIRED (refuses to start without).
- Recommend running over Tailscale/WireGuard rather than the open internet.

Routes (all JSON):
  GET  /v0/health                    → {ok, queued, running, nodes}
  GET  /v0/nodes                     → {name: {last_seen, current}}
  POST /v0/work {node,kind,payload}  → {id}
  GET  /v0/work/poll?node=X          → 200 work item | 204 (after ~25s)
  GET  /v0/work/<id>                 → work item (status/result)
  POST /v0/work/<id>/result          → {ok}
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

POLL_HOLD_SECONDS = 25
LEASE_GRACE_SECONDS = 60  # requeue running items after timeout_seconds + grace


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat()


class HubState:
    def __init__(self, state_path: Path):
        self.path = state_path
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.items: dict[str, dict[str, Any]] = {}
        self.nodes: dict[str, dict[str, Any]] = {}
        if state_path.exists():
            data = json.loads(state_path.read_text())
            self.items = data.get("items", {})
            # anything mid-run when the hub died goes back to the queue
            for it in self.items.values():
                if it["status"] == "running":
                    it["status"] = "queued"
                    it["claimed_at"] = None

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump({"items": self.items}, f, indent=2)
        os.replace(tmp, self.path)

    # -- operations ------------------------------------------------------

    def enqueue(self, node: str, kind: str, payload: dict[str, Any]) -> str:
        item_id = uuid.uuid4().hex[:12]
        with self.cond:
            self.items[item_id] = {
                "id": item_id, "node": node, "kind": kind, "payload": payload,
                "status": "queued", "result": None,
                "created_at": _now(), "claimed_at": None, "finished_at": None,
            }
            self._save_locked()
            self.cond.notify_all()
        return item_id

    def _requeue_stale_locked(self) -> None:
        now = dt.datetime.now().astimezone()
        for it in self.items.values():
            if it["status"] != "running" or not it["claimed_at"]:
                continue
            lease = it["payload"].get("timeout_seconds", 600) + LEASE_GRACE_SECONDS
            claimed = dt.datetime.fromisoformat(it["claimed_at"])
            if (now - claimed).total_seconds() > lease:
                it["status"] = "queued"
                it["claimed_at"] = None

    def poll(self, node: str, hold: float = POLL_HOLD_SECONDS) -> Optional[dict[str, Any]]:
        deadline = dt.datetime.now() + dt.timedelta(seconds=hold)
        with self.cond:
            self.nodes[node] = {"last_seen": _now()}
            while True:
                self._requeue_stale_locked()
                for it in self.items.values():
                    if it["status"] == "queued" and it["node"] == node:
                        it["status"] = "running"
                        it["claimed_at"] = _now()
                        self._save_locked()
                        return dict(it)
                remaining = (deadline - dt.datetime.now()).total_seconds()
                if remaining <= 0:
                    return None
                self.cond.wait(timeout=min(remaining, 5))

    def submit_result(self, item_id: str, ok: bool, result: dict[str, Any]) -> bool:
        with self.cond:
            it = self.items.get(item_id)
            if it is None:
                return False
            it["status"] = "done" if ok else "error"
            it["result"] = result
            it["finished_at"] = _now()
            self._save_locked()
            self.cond.notify_all()
            return True

    def get(self, item_id: str) -> Optional[dict[str, Any]]:
        with self.lock:
            it = self.items.get(item_id)
            return dict(it) if it else None

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            by_status: dict[str, int] = {}
            for it in self.items.values():
                by_status[it["status"]] = by_status.get(it["status"], 0) + 1
            return {"ok": True, "items": by_status, "nodes": dict(self.nodes)}


def make_handler(state: HubState, token: Optional[str]):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet
            pass

        def _send(self, code: int, body: Optional[dict] = None) -> None:
            data = json.dumps(body or {}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authed(self) -> bool:
            if token is None:
                return True
            got = self.headers.get("Authorization", "")
            return got == f"Bearer {token}"

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self) -> None:
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            path, _, query = self.path.partition("?")
            if path == "/v0/health" or path == "/v0/nodes":
                snap = state.snapshot()
                return self._send(200, snap if path == "/v0/health" else snap["nodes"])
            if path == "/v0/work/poll":
                params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
                node = params.get("node")
                if not node:
                    return self._send(400, {"error": "missing node param"})
                item = state.poll(node)
                return self._send(200, item) if item else self._send(204)
            if path.startswith("/v0/work/"):
                item = state.get(path.rsplit("/", 1)[1])
                return self._send(200, item) if item else self._send(404, {"error": "not found"})
            return self._send(404, {"error": "unknown route"})

        def do_POST(self) -> None:
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            path = self.path.partition("?")[0]
            if path == "/v0/work":
                body = self._body()
                node, kind, payload = body.get("node"), body.get("kind"), body.get("payload")
                if not node or kind not in ("llm", "shell", "claude") or not isinstance(payload, dict):
                    return self._send(400, {"error": "need node, kind(llm|shell|claude), payload"})
                return self._send(201, {"id": state.enqueue(node, kind, payload)})
            if path.startswith("/v0/work/") and path.endswith("/result"):
                item_id = path.split("/")[3]
                body = self._body()
                ok = state.submit_result(item_id, bool(body.get("ok")), body.get("result") or {})
                return self._send(200, {"ok": True}) if ok else self._send(404, {"error": "not found"})
            return self._send(404, {"error": "unknown route"})

    return Handler


def serve(host: str, port: int, state_path: Path, token: Optional[str]) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise SystemExit(
            "refusing to bind a non-loopback address without a token — "
            "set CONDUCTOR_TOKEN (workers send it as a Bearer token)"
        )
    server = ThreadingHTTPServer((host, port), make_handler(HubState(state_path), token))
    return server
