"""Live subscription usage — the same numbers Claude Code's /usage shows.

Claude Code stores its OAuth credential in the macOS Keychain and queries
Anthropic's usage endpoint for the real plan utilization (5-hour and weekly
windows, reset times). We do exactly what the ecosystem's usage monitors do:
read that credential locally and ask the same endpoint.

The token never leaves this process and is never logged or returned by any
API here — only derived, non-secret numbers (percentages, reset times, plan
name) are exposed. Falls back to None on any hiccup; callers then rely on
the local transcript-based estimate (quota.py).
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from typing import Any, Optional

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
CACHE_SECONDS = 180

_cache: dict[str, Any] = {"at": 0.0, "data": None}


def _keychain_token() -> Optional[str]:
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=8,
        )
    except Exception:
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    raw = out.stdout.strip()
    try:
        d = json.loads(raw)
        oauth = d.get("claudeAiOauth") or d
        tok = oauth.get("accessToken")
        return tok if isinstance(tok, str) and tok else None
    except json.JSONDecodeError:
        return raw or None


def _get(url: str, token: str) -> Optional[dict]:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
        "User-Agent": "conductor-dashboard",
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _pct(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)):
        return float(v) if v > 1.001 else float(v) * 100.0  # tolerate 0..1 or 0..100
    return None


def _normalize(usage: dict, profile: Optional[dict]) -> dict[str, Any]:
    """Tolerant extraction: shapes differ across releases; we hunt for the
    5h/7d windows by key name and pick utilization + reset time."""
    windows: dict[str, dict[str, Any]] = {}

    def visit(obj: Any, path: str) -> None:
        if not isinstance(obj, dict):
            return
        util = obj.get("utilization")
        resets = obj.get("resets_at") or obj.get("resetsAt")
        if util is not None:
            key = path.lower()
            if "five" in key or "5h" in key or "session" in key:
                windows.setdefault("five_hour", {"pct": _pct(util), "resets_at": resets})
            elif "seven" in key or "7d" in key or "week" in key:
                if "opus" in key:
                    windows.setdefault("weekly_opus", {"pct": _pct(util), "resets_at": resets})
                else:
                    windows.setdefault("weekly", {"pct": _pct(util), "resets_at": resets})
        for k, v in obj.items():
            visit(v, f"{path}.{k}")

    visit(usage, "")
    plan = None
    if profile:
        acct = profile.get("account") or {}
        org = profile.get("organization") or {}
        plan = (org.get("organization_type") or org.get("billing_type")
                or acct.get("rate_limit_tier") or None)
    out: dict[str, Any] = {"windows": windows}
    if plan:
        out["plan"] = str(plan)
    return out


def fetch_live(force: bool = False) -> Optional[dict[str, Any]]:
    """Cached live usage, or None if unavailable (not signed in, offline, or
    the endpoint shape changed beyond recognition)."""
    now = time.time()
    if not force and now - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]
    token = _keychain_token()
    data: Optional[dict[str, Any]] = None
    if token:
        usage = _get(USAGE_URL, token)
        if usage:
            profile = _get(PROFILE_URL, token)
            norm = _normalize(usage, profile)
            if norm["windows"]:
                data = norm
    _cache.update(at=now, data=data)
    return data
