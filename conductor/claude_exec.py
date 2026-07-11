"""Claude Code executor — run tasks on your Claude SUBSCRIPTION, no API key.

`claude -p` (headless Claude Code) authenticates with the login session you
created via `claude /login` — i.e. your Pro/Max subscription. CONDUCTOR shells
out to it, so:

- zero marginal USD cost (subscription quota + rate windows apply instead),
- the full Claude Code agentic harness (Read/Grep/Edit/Bash/WebSearch...)
  becomes the task's toolset, controlled per task via `claude_tools`,
- it works on any mesh node where `claude` is installed and logged in.

Reported `total_cost_usd` from the CLI is recorded in the ledger entry for
observability but does NOT count against the plan's USD budget.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

REMEMBER_INSTRUCTION = """\
You have a long-term memory directory at: {memory_dir}
If you learn something durable (a correction, a confirmed approach, a fact worth
keeping), save it as a new markdown file there named <YYYYMMDDHHMMSS>-<slug>.md:

---
id: <filename without .md>
summary: <one-line takeaway>
tags: [tag1, tag2]
source: {task_id}
created: <ISO 8601 timestamp>
---
<why it matters and how to apply it>

Save one lesson per file. Don't record what the workspace already makes obvious."""


def _sanitized_env() -> dict[str, str]:
    """Strip nested-harness variables so the spawned claude authenticates as
    THIS machine's own login — not as a child of whatever agent session spawned
    us (running conductor from inside Claude Code would otherwise leak its
    session auth into scheduled tasks, with confusing results)."""
    return {
        k: v for k, v in os.environ.items()
        if not (k.startswith("CLAUDE") or k in ("BAGGAGE", "AI_AGENT", "ANTHROPIC_BASE_URL"))
    }


_CLAUDE_PATH: Optional[str] = None


def resolve_claude() -> Optional[str]:
    """Absolute path to the claude CLI, robust to GUI launches.

    A Finder-launched .app gets launchd's bare PATH (no nvm/homebrew dirs) and a
    NON-interactive login shell doesn't read .zshrc where nvm usually lives —
    so we search the places Claude Code actually installs to, directly."""
    global _CLAUDE_PATH
    if _CLAUDE_PATH and Path(_CLAUDE_PATH).exists():
        return _CLAUDE_PATH

    home = Path.home()
    candidates: list[Path] = []
    w = shutil.which("claude")
    if w:
        candidates.append(Path(w))
    candidates += [
        home / ".claude" / "local" / "claude",   # native installer
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
        home / ".local" / "bin" / "claude",
    ]
    nvm = home / ".nvm" / "versions" / "node"
    if nvm.is_dir():  # newest-used nvm node first
        for v in sorted(nvm.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            candidates.append(v / "bin" / "claude")

    for c in candidates:
        try:
            if c.exists():
                _CLAUDE_PATH = str(c)
                return _CLAUDE_PATH
        except OSError:
            continue
    return None


def claude_available() -> bool:
    return resolve_claude() is not None


def build_command(payload: dict[str, Any]) -> list[str]:
    """Assemble the headless invocation from a task payload."""
    cmd = ["claude", "-p", payload["prompt"], "--output-format", "json"]
    if payload.get("claude_model"):
        cmd += ["--model", payload["claude_model"]]
    if payload.get("claude_tools"):
        cmd += ["--allowedTools", ",".join(payload["claude_tools"])]
    system_parts = [p for p in (payload.get("system"), payload.get("briefing"),
                                payload.get("remember_instruction")) if p]
    if system_parts:
        cmd += ["--append-system-prompt", "\n\n".join(system_parts)]
    return cmd


def run_claude(payload: dict[str, Any], workdir: Optional[str] = None) -> dict[str, Any]:
    """Execute headless Claude Code. Returns a worker-style result dict:
    {text, reported_cost_usd, num_turns, usage, returncode, is_error, [error]}."""
    claude = resolve_claude()
    if claude is None:
        return {"is_error": True, "returncode": 127,
                "error": "claude CLI not found on this machine — install Claude Code and run /login"}

    cmd = build_command(payload)
    cmd[0] = claude  # absolute path — immune to GUI-launch PATH problems
    env = _sanitized_env()
    # the claude script needs `node` next to it (nvm installs) on PATH
    env["PATH"] = f"{Path(claude).parent}:{env.get('PATH', '/usr/bin:/bin')}"
    timeout = payload.get("timeout_seconds", 600)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, cwd=workdir, env=env)
    except subprocess.TimeoutExpired:
        return {"is_error": True, "returncode": -1,
                "error": f"claude -p timed out after {timeout}s"}

    if proc.returncode != 0 and not proc.stdout.strip():
        return {"is_error": True, "returncode": proc.returncode,
                "error": (proc.stderr or "claude exited non-zero")[-4000:]}

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"is_error": True, "returncode": proc.returncode,
                "error": "unparseable claude output: " + proc.stdout[-2000:]}

    usage = data.get("usage") or {}
    is_error = bool(data.get("is_error"))
    out: dict[str, Any] = {}
    if is_error:
        # claude reports runtime failures (e.g. auth: "Please run /login")
        # inside `result` — surface them as the error detail
        out["error"] = str(data.get("result", ""))[:400]
    return {
        **out,
        "is_error": is_error,
        "returncode": proc.returncode,
        "text": data.get("result", ""),
        "reported_cost_usd": data.get("total_cost_usd", 0.0),
        "num_turns": data.get("num_turns", 0),
        "session_id": data.get("session_id", ""),
        "usage": {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
        },
    }


def build_payload(task: Any, briefing: str = "", memory_dir: Optional[Path] = None,
                  model_override: Optional[str] = None) -> dict[str, Any]:
    """Task → wire payload (also used for mesh dispatch). model_override lets the
    quota gate downgrade the model without mutating the task."""
    payload: dict[str, Any] = {
        "task_id": task.id,
        "prompt": task.prompt_text(),
        "timeout_seconds": task.timeout_seconds,
    }
    if task.system:
        payload["system"] = task.system
    effective_model = model_override or task.claude_model
    if effective_model:
        payload["claude_model"] = effective_model
    if task.claude_tools:
        payload["claude_tools"] = task.claude_tools
    if briefing:
        payload["briefing"] = briefing
    if memory_dir is not None and task.memory:
        can_write = any(t.split("(")[0] in ("Write", "Edit", "Bash") for t in task.claude_tools)
        if can_write:
            payload["remember_instruction"] = REMEMBER_INSTRUCTION.format(
                memory_dir=memory_dir, task_id=task.id)
    return payload
