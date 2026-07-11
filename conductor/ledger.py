"""Budget ledger — persisted spend records + in-memory reservations.

Reservations exist because tasks run concurrently: two tasks that each fit the
remaining budget individually could exceed it together. The scheduler reserves
the *estimated* cost at dispatch and settles it with the *actual* cost from
`response.usage` after the call returns.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self.entries: list[dict[str, Any]] = []
        self._reserved: dict[str, float] = {}  # task_id -> estimated USD (in-flight)
        if path.exists():
            self.entries = json.loads(path.read_text())

    # -- persistence -----------------------------------------------------

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(self.entries, f, indent=2)
        os.replace(tmp, self.path)

    # -- reservations ----------------------------------------------------

    def reserve(self, task_id: str, est_usd: float) -> None:
        self._reserved[task_id] = est_usd

    def release(self, task_id: str) -> None:
        self._reserved.pop(task_id, None)

    @property
    def reserved_total(self) -> float:
        return sum(self._reserved.values())

    # -- recording -------------------------------------------------------

    def add(self, task_id: str, model_id: str, cost_usd: float, usage: dict[str, Any]) -> None:
        self.entries.append({
            "ts": dt.datetime.now().astimezone().isoformat(),
            "task_id": task_id,
            "model": model_id,
            "cost_usd": round(cost_usd, 6),
            "usage": usage,
        })
        self.release(task_id)
        self._save()

    # -- queries -----------------------------------------------------------

    def _spent_since(self, cutoff: dt.datetime) -> float:
        total = 0.0
        for e in self.entries:
            if dt.datetime.fromisoformat(e["ts"]) >= cutoff:
                total += e["cost_usd"]
        return total

    def spent_today(self) -> float:
        """Spend since local midnight (calendar day)."""
        now = dt.datetime.now().astimezone()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return self._spent_since(midnight)

    def spent_last_hour(self) -> float:
        """Rolling 60-minute spend."""
        cutoff = dt.datetime.now().astimezone() - dt.timedelta(hours=1)
        return self._spent_since(cutoff)

    def remaining(self, daily_usd: float, hourly_usd: Optional[float]) -> float:
        """Budget headroom right now, net of in-flight reservations."""
        rem_daily = daily_usd - self.spent_today() - self.reserved_total
        if hourly_usd is None:
            return rem_daily
        rem_hourly = hourly_usd - self.spent_last_hour() - self.reserved_total
        return min(rem_daily, rem_hourly)

    def by_task(self) -> dict[str, dict[str, Any]]:
        """Aggregate cost + token totals per task id."""
        agg: dict[str, dict[str, Any]] = {}
        for e in self.entries:
            row = agg.setdefault(e["task_id"], {
                "runs": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0,
                "last_run": None, "model": e["model"],
            })
            row["runs"] += 1
            row["cost_usd"] += e["cost_usd"]
            row["input_tokens"] += e["usage"].get("input_tokens", 0)
            row["output_tokens"] += e["usage"].get("output_tokens", 0)
            row["last_run"] = e["ts"]
            row["model"] = e["model"]
        return agg
