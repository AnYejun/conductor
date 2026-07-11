"""Claude subscription executor — offline tests (command assembly, payload, schema)."""
from pathlib import Path

import pytest

from conductor.claude_exec import build_command, build_payload
from conductor.schema import load_plan


def _plan(tmp_path: Path):
    p = tmp_path / "plan.yaml"
    p.write_text("""
budget: {daily_usd: 1.00}
models:
  haiku: {id: claude-haiku-4-5, price_in: 1.00, price_out: 5.00}
tasks:
  - id: sub-task
    kind: claude
    prompt: "Do the thing."
    system: "Be terse."
    claude_model: haiku
    claude_tools: ["Read", "Bash(git:*)"]
""")
    return load_plan(p)


def test_schema_accepts_claude_kind(tmp_path: Path):
    plan = _plan(tmp_path)
    t = plan.tasks[0]
    assert t.kind.value == "claude"
    assert t.model is None  # no models-table entry needed — subscription


def test_schema_rejects_claude_without_prompt(tmp_path: Path):
    p = tmp_path / "plan.yaml"
    p.write_text("""
budget: {daily_usd: 1.00}
models:
  haiku: {id: claude-haiku-4-5, price_in: 1.00, price_out: 5.00}
tasks:
  - id: bad
    kind: claude
""")
    with pytest.raises(ValueError, match="prompt"):
        load_plan(p)


def test_build_command_full(tmp_path: Path):
    plan = _plan(tmp_path)
    payload = build_payload(plan.tasks[0], briefing="## What you already know\n- x",
                            memory_dir=tmp_path / "mem")
    cmd = build_command(payload)

    assert cmd[:3] == ["claude", "-p", "Do the thing."]
    assert "--output-format" in cmd and "json" in cmd
    i = cmd.index("--model")
    assert cmd[i + 1] == "haiku"
    i = cmd.index("--allowedTools")
    assert cmd[i + 1] == "Read,Bash(git:*)"
    i = cmd.index("--append-system-prompt")
    appended = cmd[i + 1]
    assert "Be terse." in appended
    assert "What you already know" in appended
    # Bash is allowed → remember instruction included, pointing at the memory dir
    assert "long-term memory directory" in appended
    assert str(tmp_path / "mem") in appended


def test_remember_instruction_skipped_when_readonly(tmp_path: Path):
    plan = _plan(tmp_path)
    t = plan.tasks[0]
    t.claude_tools = ["Read", "Grep"]  # no write-capable tool
    payload = build_payload(t, memory_dir=tmp_path / "mem")
    assert "remember_instruction" not in payload


def test_minimal_command(tmp_path: Path):
    plan = _plan(tmp_path)
    t = plan.tasks[0]
    t.system = None
    t.claude_model = None
    t.claude_tools = []
    payload = build_payload(t)
    cmd = build_command(payload)
    assert "--model" not in cmd
    assert "--allowedTools" not in cmd
    assert "--append-system-prompt" not in cmd
