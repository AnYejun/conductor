"""Quota monitor + gate tests — fake transcripts, no API, no real ~/.claude."""
import datetime as dt
import json
from pathlib import Path

import pytest

from conductor.ledger import Ledger
from conductor.quota import QuotaMonitor
from conductor.schema import Subscription


def _write_transcript(root: Path, entries: list[tuple[dt.datetime, int, int]]) -> None:
    """entries: (ts, input_tokens, output_tokens)"""
    root.mkdir(parents=True, exist_ok=True)
    lines = []
    for ts, inp, out in entries:
        lines.append(json.dumps({
            "type": "assistant",
            "timestamp": ts.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "message": {"usage": {"input_tokens": inp, "output_tokens": out,
                                  "cache_creation_input_tokens": 0}},
        }))
    (root / "session.jsonl").write_text("\n".join(lines) + "\n")


NOW = dt.datetime.now().astimezone()


def test_windows_and_resets(tmp_path: Path):
    _write_transcript(tmp_path / "proj", [
        (NOW - dt.timedelta(hours=1), 40_000, 10_000),   # in 5h + weekly
        (NOW - dt.timedelta(hours=4), 30_000, 20_000),   # in 5h + weekly
        (NOW - dt.timedelta(hours=20), 100_000, 50_000),  # weekly only
        (NOW - dt.timedelta(days=9), 999_999, 0),         # outside both
    ])
    mon = QuotaMonitor(transcript_root=tmp_path)
    snap = mon.snapshot(Subscription(five_hour_tokens=200_000, weekly_tokens=1_000_000), now=NOW)

    assert snap.five_hour.burn == 100_000
    assert snap.weekly.burn == 250_000
    # 5h reset = oldest in-window entry (4h ago) + 5h ≈ 1h from now
    assert snap.five_hour.resets_at is not None
    delta = (snap.five_hour.resets_at - NOW).total_seconds()
    assert 3500 < delta < 3700
    assert snap.five_hour.remaining_fraction == pytest.approx(0.5)


def test_gate_fires_below_reserve(tmp_path: Path):
    _write_transcript(tmp_path / "proj", [(NOW - dt.timedelta(hours=1), 90_000, 5_000)])
    mon = QuotaMonitor(transcript_root=tmp_path)
    # 95k of 100k burned → 5% left < 15% reserve → gate
    ok, worst = mon.gate(Subscription(five_hour_tokens=100_000, reserve=0.15), now=NOW)
    assert not ok and worst is not None and worst.name == "5h"
    # generous ceiling → no gate
    ok, worst = mon.gate(Subscription(five_hour_tokens=1_000_000, reserve=0.15), now=NOW)
    assert ok and worst is None


def test_unset_ceilings_never_gate(tmp_path: Path):
    _write_transcript(tmp_path / "proj", [(NOW - dt.timedelta(hours=1), 10_000_000, 0)])
    mon = QuotaMonitor(transcript_root=tmp_path)
    ok, worst = mon.gate(Subscription(), now=NOW)
    assert ok and worst is None


def test_remote_ledger_entries_counted(tmp_path: Path):
    (tmp_path / "empty").mkdir()
    led = Ledger(tmp_path / "ledger.json")
    led.add("t-remote", "claude-code/sonnet", 0.0,
            {"input_tokens": 50_000, "output_tokens": 10_000,
             "cache_creation_input_tokens": 0, "node": "homebox"})
    led.add("t-local", "claude-code/sonnet", 0.0,
            {"input_tokens": 30_000, "output_tokens": 5_000,
             "cache_creation_input_tokens": 0})  # no node → in transcripts already
    mon = QuotaMonitor(transcript_root=tmp_path / "empty", ledger=led)
    snap = mon.snapshot(Subscription(five_hour_tokens=100_000))
    assert snap.five_hour.burn == 60_000  # only the remote entry


def test_quota_downgrade_chain(tmp_path: Path):
    """Executor-level: gated claude task downgrades opus→sonnet, sonnet→haiku."""
    import asyncio
    from unittest.mock import patch

    from conductor.executor import gate_and_run
    from conductor.quota import WindowState
    from conductor.schema import load_plan

    plan_file = tmp_path / "plan.yaml"
    plan_file.write_text("""
budget: {daily_usd: 1.00}
subscription: {five_hour_tokens: 100000, reserve: 0.15}
models:
  haiku: {id: claude-haiku-4-5, price_in: 1.00, price_out: 5.00}
tasks:
  - id: t
    kind: claude
    claude_model: opus
    prompt: "hi"
    on_budget_exceeded: downgrade
""")
    plan = load_plan(plan_file)
    led = Ledger(tmp_path / "ledger.json")
    captured: dict = {}

    def fake_run_claude(payload, workdir=None):
        captured.update(payload)
        return {"is_error": False, "returncode": 0, "text": "ok",
                "reported_cost_usd": 0.01, "num_turns": 1,
                "usage": {"input_tokens": 1, "output_tokens": 1,
                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}

    gated = WindowState(name="5h", burn=95_000, ceiling=100_000,
                        resets_at=dt.datetime.now().astimezone() + dt.timedelta(hours=2))
    with patch("conductor.quota.QuotaMonitor.gate", return_value=(False, gated)), \
         patch("conductor.claude_exec.run_claude", side_effect=fake_run_claude):
        result = asyncio.run(gate_and_run(None, plan, plan.tasks[0], led, tmp_path / "out"))

    assert result.outcome.value == "done"
    assert captured["claude_model"] == "sonnet"  # opus → sonnet
    assert "quota-downgraded" in result.detail


def test_quota_defer_carries_reset_time(tmp_path: Path):
    import asyncio
    from unittest.mock import patch

    from conductor.executor import gate_and_run
    from conductor.quota import WindowState
    from conductor.schema import load_plan

    plan_file = tmp_path / "plan.yaml"
    plan_file.write_text("""
budget: {daily_usd: 1.00}
models:
  haiku: {id: claude-haiku-4-5, price_in: 1.00, price_out: 5.00}
tasks:
  - id: t
    kind: claude
    prompt: "hi"
""")
    plan = load_plan(plan_file)
    resets = dt.datetime.now().astimezone() + dt.timedelta(hours=3)
    gated = WindowState(name="weekly", burn=1, ceiling=1, resets_at=resets)
    with patch("conductor.quota.QuotaMonitor.gate", return_value=(False, gated)):
        result = asyncio.run(gate_and_run(None, plan, plan.tasks[0],
                                          Ledger(tmp_path / "l.json"), tmp_path / "out"))
    assert result.outcome.value == "deferred"
    assert result.retry_after == resets
