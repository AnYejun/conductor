"""Subscription quota monitor — the budget gate's twin for `kind: claude` tasks.

API tasks are gated in dollars; subscription tasks are gated in *quota*: the
rolling 5-hour window and the weekly window your Claude plan enforces. This
module estimates current burn the way the open-source ecosystem does it
(ccusage et al.): by aggregating the usage entries Claude Code writes to its
local transcripts (~/.claude/projects/**/*.jsonl). Remote `kind: claude` runs
executed on mesh nodes are added from the conductor ledger (the worker reports
their usage back).

"Burn units" = input + output + cache-creation tokens (cache reads excluded).
Anthropic doesn't publish absolute per-plan ceilings, so you calibrate the
ceilings once in plan.yaml; the *relative* burn and reset math is what powers
automatic defer/downgrade decisions.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .ledger import Ledger
from .schema import Subscription

FIVE_HOURS = dt.timedelta(hours=5)
ONE_WEEK = dt.timedelta(days=7)


def _burn(usage: dict[str, Any]) -> int:
    return (
        (usage.get("input_tokens") or 0)
        + (usage.get("output_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
    )


@dataclass
class WindowState:
    name: str
    burn: int
    ceiling: Optional[int]
    resets_at: Optional[dt.datetime]  # when the oldest in-window entry ages out

    @property
    def remaining_fraction(self) -> Optional[float]:
        if not self.ceiling:
            return None
        return max(0.0, 1.0 - self.burn / self.ceiling)


@dataclass
class QuotaSnapshot:
    five_hour: WindowState
    weekly: WindowState

    def worst(self, reserve: float) -> Optional[WindowState]:
        """The window that violates the reserve threshold, if any."""
        candidates = [
            w for w in (self.five_hour, self.weekly)
            if w.remaining_fraction is not None and w.remaining_fraction <= reserve
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda w: w.remaining_fraction or 0.0)


class QuotaMonitor:
    def __init__(self, transcript_root: Optional[Path] = None, ledger: Optional[Ledger] = None):
        self.transcript_root = transcript_root or Path.home() / ".claude" / "projects"
        self.ledger = ledger

    # -- collection -------------------------------------------------------

    def _transcript_entries(self, since: dt.datetime) -> list[tuple[dt.datetime, int]]:
        out: list[tuple[dt.datetime, int]] = []
        if not self.transcript_root.exists():
            return out
        cutoff_ts = since.timestamp()
        for path in self.transcript_root.rglob("*.jsonl"):
            try:
                if path.stat().st_mtime < cutoff_ts:
                    continue
                with path.open() as f:
                    for line in f:
                        if '"usage"' not in line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts_raw = rec.get("timestamp")
                        usage = (rec.get("message") or {}).get("usage")
                        if not ts_raw or not usage:
                            continue
                        try:
                            ts = dt.datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                        except ValueError:
                            continue
                        if ts < since:
                            continue
                        b = _burn(usage)
                        if b > 0:
                            out.append((ts, b))
            except OSError:
                continue
        return out

    def _remote_ledger_entries(self, since: dt.datetime) -> list[tuple[dt.datetime, int]]:
        """Remote kind=claude runs — reported by workers, absent from local transcripts."""
        out: list[tuple[dt.datetime, int]] = []
        if self.ledger is None:
            return out
        for e in self.ledger.entries:
            if not str(e.get("model", "")).startswith("claude-code/"):
                continue
            if not (e.get("usage") or {}).get("node"):
                continue  # local runs appear in transcripts already
            ts = dt.datetime.fromisoformat(e["ts"])
            if ts >= since:
                out.append((ts, _burn(e["usage"])))
        return out

    # -- aggregation -------------------------------------------------------

    def snapshot(self, sub: Subscription, now: Optional[dt.datetime] = None) -> QuotaSnapshot:
        now = now or dt.datetime.now().astimezone()
        entries = self._transcript_entries(now - ONE_WEEK)
        entries += self._remote_ledger_entries(now - ONE_WEEK)

        def window(name: str, span: dt.timedelta, ceiling: Optional[int]) -> WindowState:
            start = now - span
            in_win = [(ts, b) for ts, b in entries if ts >= start]
            burn = sum(b for _, b in in_win)
            resets = (min(ts for ts, _ in in_win) + span) if in_win else None
            return WindowState(name=name, burn=burn, ceiling=ceiling, resets_at=resets)

        return QuotaSnapshot(
            five_hour=window("5h", FIVE_HOURS, sub.five_hour_tokens),
            weekly=window("weekly", ONE_WEEK, sub.weekly_tokens),
        )

    def gate(self, sub: Subscription, now: Optional[dt.datetime] = None
             ) -> tuple[bool, Optional[WindowState]]:
        """(ok, violating_window). ok=True when no configured ceiling is below
        the reserve threshold — unconfigured ceilings never gate."""
        snap = self.snapshot(sub, now)
        worst = snap.worst(sub.reserve)
        return (worst is None), worst
