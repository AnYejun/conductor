"""Offline tests — schema, ledger, window, downgrade chain, cost math.

No API key needed. Run: python -m pytest tests/ -q
"""
import datetime as dt
import json
from pathlib import Path

import pytest

from conductor.estimator import cost_usd
from conductor.ledger import Ledger
from conductor.schema import Plan, Window, load_plan

PLAN_YAML = """
budget: {daily_usd: 5.00, hourly_usd: 1.00}
models:
  opus:   {id: claude-opus-4-8,  price_in: 5.00, price_out: 25.00}
  sonnet: {id: claude-sonnet-5,  price_in: 3.00, price_out: 15.00}
  haiku:  {id: claude-haiku-4-5, price_in: 1.00, price_out: 5.00}
tasks:
  - id: a
    prompt: "hello"
    model: opus
    max_output_tokens: 100
  - id: b
    prompt: "world"
    model: haiku
    depends_on: [a]
    window: {earliest: "07:00", deadline: "09:00"}
"""


@pytest.fixture
def plan(tmp_path: Path) -> Plan:
    p = tmp_path / "plan.yaml"
    p.write_text(PLAN_YAML)
    return load_plan(p)


def test_plan_loads(plan: Plan):
    assert len(plan.tasks) == 2
    assert plan.tasks[1].window.earliest == dt.time(7, 0)


def test_downgrade_chain(plan: Plan):
    assert plan.downgrade_chain("opus") == ["sonnet", "haiku"]
    assert plan.downgrade_chain("haiku") == []


def test_unknown_model_rejected(tmp_path: Path):
    bad = PLAN_YAML.replace("model: opus", "model: gpt5")
    p = tmp_path / "plan.yaml"
    p.write_text(bad)
    with pytest.raises(ValueError, match="unknown model"):
        load_plan(p)


def test_zero_price_rejected(tmp_path: Path):
    bad = PLAN_YAML.replace("price_in: 5.00", "price_in: 0.00")
    p = tmp_path / "plan.yaml"
    p.write_text(bad)
    with pytest.raises(ValueError, match="must be > 0"):
        load_plan(p)


def test_window():
    w = Window(earliest=dt.time(7), deadline=dt.time(9))
    assert not w.contains(dt.time(6, 59))
    assert w.contains(dt.time(8))
    assert not w.contains(dt.time(9, 1))
    assert w.expired(dt.time(9, 1))
    assert Window().contains(dt.time(3))  # no bounds → always eligible


def test_cost_math(plan: Plan):
    opus = plan.models["opus"]
    # 1M in + 1M out at $5/$25
    assert cost_usd(opus, 1_000_000, 1_000_000) == pytest.approx(30.0)
    # cache read bills at 0.1x input, cache write at 1.25x
    assert cost_usd(opus, 0, 0, cache_read=1_000_000) == pytest.approx(0.5)
    assert cost_usd(opus, 0, 0, cache_creation=1_000_000) == pytest.approx(6.25)


def test_ledger_reserve_and_reconcile(tmp_path: Path):
    led = Ledger(tmp_path / "ledger.json")
    assert led.remaining(5.0, 1.0) == pytest.approx(1.0)  # hourly is the binding cap

    led.reserve("t1", 0.4)
    assert led.remaining(5.0, 1.0) == pytest.approx(0.6)

    led.add("t1", "claude-haiku-4-5", 0.05, {"input_tokens": 10, "output_tokens": 20})
    # reservation released, actual recorded
    assert led.reserved_total == 0
    assert led.spent_today() == pytest.approx(0.05)
    assert led.remaining(5.0, 1.0) == pytest.approx(0.95)

    # persisted
    led2 = Ledger(tmp_path / "ledger.json")
    assert led2.spent_today() == pytest.approx(0.05)
    assert led2.by_task()["t1"]["runs"] == 1


def test_ledger_daily_boundary(tmp_path: Path):
    led = Ledger(tmp_path / "ledger.json")
    yesterday = (dt.datetime.now().astimezone() - dt.timedelta(days=1)).isoformat()
    led.entries.append({"ts": yesterday, "task_id": "old", "model": "m",
                        "cost_usd": 3.0, "usage": {}})
    assert led.spent_today() == 0.0
    assert led.spent_last_hour() == 0.0
