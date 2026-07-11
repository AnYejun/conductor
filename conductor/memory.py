"""LAPLAS-grade long-term memory layer.

A persistent, inspectable, editable store of what the agent has learned —
one memory per file, with provenance. Two verbs, mirroring LAPLAS:

- recall(query)  → the memories most relevant to what you're about to do,
                   scored by keyword overlap + recency. Injected into the
                   agent's system prompt as a "what you already know" briefing.
- remember(...)  → persist a lesson (correction, confirmed approach, fact),
                   tagged and stamped with the task that produced it.

Files live at <state>/memory/*.md with frontmatter — the same shape as a
hand-written knowledge note, so a human can read, edit, or delete them.
The `recall()` method is the pluggable seam: swap the local scorer for the
LAPLAS MCP recall (recall_compose) to get derivation-chain briefings.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_STOP = {
    "the", "a", "an", "and", "or", "but", "for", "of", "to", "in", "on", "at",
    "is", "are", "was", "were", "be", "with", "as", "by", "it", "this", "that",
    "you", "your", "i", "we", "they", "he", "she", "do", "does", "did", "not",
    "can", "will", "would", "should", "from", "into", "then", "than", "so",
}


def _tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 3 and w not in _STOP]


def _slug(text: str, n: int = 6) -> str:
    words = re.findall(r"[A-Za-z0-9]+", text.lower())[:n]
    return "-".join(words) or "note"


@dataclass
class Memory:
    id: str
    summary: str
    body: str
    tags: list[str] = field(default_factory=list)
    source: str = ""
    created: str = ""
    path: Optional[Path] = None

    def as_briefing_line(self) -> str:
        tagstr = f"  ({', '.join(self.tags)})" if self.tags else ""
        line = f"- {self.summary}{tagstr}"
        if self.body.strip():
            line += f"\n  {self.body.strip()}"
        return line

    def render(self) -> str:
        fm = [
            "---",
            f"id: {self.id}",
            f"summary: {self.summary}",
            f"tags: [{', '.join(self.tags)}]",
            f"source: {self.source}",
            f"created: {self.created}",
            "---",
            "",
        ]
        return "\n".join(fm) + self.body.rstrip() + "\n"


_FM = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def _parse(path: Path) -> Optional[Memory]:
    m = _FM.match(path.read_text())
    if not m:
        return None
    head, body = m.groups()
    meta: dict[str, str] = {}
    for line in head.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    tags_raw = meta.get("tags", "").strip("[]")
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    return Memory(
        id=meta.get("id", path.stem),
        summary=meta.get("summary", ""),
        body=body,
        tags=tags,
        source=meta.get("source", ""),
        created=meta.get("created", ""),
        path=path,
    )


class MemoryStore:
    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    # -- read ------------------------------------------------------------

    def all(self) -> list[Memory]:
        out = []
        for p in sorted(self.dir.glob("*.md")):
            mem = _parse(p)
            if mem:
                out.append(mem)
        return out

    def recall(self, query: str, k: int = 5) -> list[Memory]:
        """Top-k memories relevant to `query`. Score = keyword overlap
        (summary/tags weighted heavier than body) + a gentle recency bonus."""
        q = set(_tokens(query))
        if not q:
            return []
        mems = self.all()
        now = dt.datetime.now().astimezone()
        scored: list[tuple[float, Memory]] = []
        for mem in mems:
            hay = (
                _tokens(mem.summary) * 3
                + [t.lower() for t in mem.tags] * 3
                + _tokens(mem.body)
            )
            if not hay:
                continue
            hits = sum(1 for w in hay if w in q)
            if hits == 0:
                continue
            overlap = hits / math.sqrt(len(hay))
            recency = 0.0
            try:
                age_days = (now - dt.datetime.fromisoformat(mem.created)).total_seconds() / 86400
                recency = 1.0 / (1.0 + age_days / 14.0)  # ~half-weight after two weeks
            except Exception:
                pass
            scored.append((overlap + 0.3 * recency, mem))
        scored.sort(key=lambda s: s[0], reverse=True)
        return [m for _, m in scored[:k]]

    def briefing(self, query: str, k: int = 5) -> str:
        """Render a recall as a system-prompt block, or '' if nothing relevant."""
        hits = self.recall(query, k)
        if not hits:
            return ""
        lines = "\n".join(m.as_briefing_line() for m in hits)
        return (
            "## What you already know (from long-term memory)\n"
            "These are lessons you saved on earlier tasks. Use them; don't rediscover them. "
            "If one turns out wrong, say so and save a correction.\n\n"
            f"{lines}"
        )

    # -- write -----------------------------------------------------------

    def remember(self, summary: str, body: str = "", tags: Optional[list[str]] = None,
                 source: str = "") -> Memory:
        stamp = dt.datetime.now().astimezone()
        mem_id = stamp.strftime("%Y%m%d%H%M%S") + "-" + _slug(summary)
        mem = Memory(
            id=mem_id, summary=summary.strip(), body=body.strip(),
            tags=tags or [], source=source, created=stamp.isoformat(),
        )
        path = self.dir / f"{mem_id}.md"
        path.write_text(mem.render())
        mem.path = path
        return mem
