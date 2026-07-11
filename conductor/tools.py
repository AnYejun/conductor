"""Agent tools — the hands the agentic executor works with.

Two families:
- Workspace tools (read/write/edit/grep/bash) — confined to a workspace root.
- Memory tools (remember/recall) — the LAPLAS long-term layer, so the agent
  can persist and retrieve lessons across runs.

Security: file tools resolve every path inside `workspace` and refuse escapes
(.., symlinks out, absolute paths). `bash` is genuinely powerful — the plan
must opt in by listing "bash" in the task's tools, same as `--allow-shell` on
a mesh worker. Run untrusted plans in a container (`container:` on the task).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .memory import MemoryStore

# JSON-schema tool definitions, keyed by name.
TOOL_DEFS: dict[str, dict[str, Any]] = {
    "read": {
        "name": "read",
        "description": "Read a UTF-8 text file from the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path relative to the workspace root."}},
            "required": ["path"],
        },
    },
    "write": {
        "name": "write",
        "description": "Create or overwrite a text file in the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    "edit": {
        "name": "edit",
        "description": "Replace the first exact occurrence of old_str with new_str in a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_str": {"type": "string"},
                "new_str": {"type": "string"},
            },
            "required": ["path", "old_str", "new_str"],
        },
    },
    "grep": {
        "name": "grep",
        "description": "Search the workspace for a regex (ripgrep if available, else Python). Returns matching lines.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "Optional subdir/file (default: whole workspace)."},
            },
            "required": ["pattern"],
        },
    },
    "bash": {
        "name": "bash",
        "description": "Run a shell command in the workspace. Powerful — the plan must opt in to this tool.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    "remember": {
        "name": "remember",
        "description": (
            "Save a lesson to long-term memory so future tasks benefit. Store one "
            "insight per call: a correction, a confirmed approach, or a durable fact. "
            "Include WHY it matters. Don't save what the workspace already records."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "One-line takeaway."},
                "body": {"type": "string", "description": "Detail + why it matters + how to apply."},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary"],
        },
    },
    "recall": {
        "name": "recall",
        "description": "Search long-term memory for lessons relevant to a query. Use before diving into unfamiliar work.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}

READ_ONLY = {"read", "grep", "recall"}
DEFAULT_TOOLS = ["read", "grep", "remember", "recall"]  # safe default: no write/bash
ALL_TOOLS = list(TOOL_DEFS.keys())


@dataclass
class ToolContext:
    workspace: Path
    memory: MemoryStore
    task_id: str
    bash_timeout: int = 120


def tool_defs(names: list[str]) -> list[dict[str, Any]]:
    return [TOOL_DEFS[n] for n in names if n in TOOL_DEFS]


def _safe(ctx: ToolContext, rel: str) -> Path:
    root = ctx.workspace.resolve()
    full = (root / rel).resolve()
    if not full.is_relative_to(root):
        raise ValueError(f"path '{rel}' escapes the workspace")
    return full


def execute(name: str, tool_input: dict[str, Any], ctx: ToolContext) -> tuple[str, bool]:
    """Run a tool. Returns (result_text, is_error)."""
    try:
        fn = _DISPATCH[name]
    except KeyError:
        return f"unknown tool '{name}'", True
    try:
        return fn(tool_input, ctx), False
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}", True


# -- workspace tools -----------------------------------------------------


def _read(inp: dict, ctx: ToolContext) -> str:
    p = _safe(ctx, inp["path"])
    if not p.exists():
        raise FileNotFoundError(inp["path"])
    return p.read_text()[:100_000]


def _write(inp: dict, ctx: ToolContext) -> str:
    p = _safe(ctx, inp["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(inp["content"])
    return f"wrote {len(inp['content'])} bytes to {inp['path']}"


def _edit(inp: dict, ctx: ToolContext) -> str:
    p = _safe(ctx, inp["path"])
    text = p.read_text()
    old = inp["old_str"]
    if text.count(old) == 0:
        raise ValueError("old_str not found")
    if text.count(old) > 1:
        raise ValueError(f"old_str matches {text.count(old)}× — make it unique")
    p.write_text(text.replace(old, inp["new_str"], 1))
    return f"edited {inp['path']}"


def _grep(inp: dict, ctx: ToolContext) -> str:
    import shutil
    target = _safe(ctx, inp.get("path", "."))
    if shutil.which("rg"):
        proc = subprocess.run(
            ["rg", "-n", "--no-heading", inp["pattern"], str(target)],
            capture_output=True, text=True, timeout=30,
        )
        return (proc.stdout or "(no matches)")[:50_000]
    # python fallback
    import re
    rx = re.compile(inp["pattern"])
    files = [target] if target.is_file() else target.rglob("*")
    hits = []
    for f in files:
        if not f.is_file():
            continue
        try:
            for i, line in enumerate(f.read_text().splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{f.relative_to(ctx.workspace)}:{i}:{line}")
        except (UnicodeDecodeError, OSError):
            continue
    return ("\n".join(hits) or "(no matches)")[:50_000]


def _bash(inp: dict, ctx: ToolContext) -> str:
    proc = subprocess.run(
        inp["command"], shell=True, capture_output=True, text=True,
        cwd=ctx.workspace, timeout=ctx.bash_timeout,
    )
    out = f"[exit {proc.returncode}]\n"
    if proc.stdout:
        out += "stdout:\n" + proc.stdout[-30_000:]
    if proc.stderr:
        out += "\nstderr:\n" + proc.stderr[-10_000:]
    return out


# -- memory tools --------------------------------------------------------


def _remember(inp: dict, ctx: ToolContext) -> str:
    mem = ctx.memory.remember(
        summary=inp["summary"],
        body=inp.get("body", ""),
        tags=inp.get("tags", []),
        source=ctx.task_id,
    )
    return f"remembered ({mem.id})"


def _recall(inp: dict, ctx: ToolContext) -> str:
    hits = ctx.memory.recall(inp["query"], k=5)
    if not hits:
        return "(no relevant memories)"
    return "\n".join(m.as_briefing_line() for m in hits)


_DISPATCH: dict[str, Callable[[dict, ToolContext], str]] = {
    "read": _read, "write": _write, "edit": _edit, "grep": _grep, "bash": _bash,
    "remember": _remember, "recall": _recall,
}
