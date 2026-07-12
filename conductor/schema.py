"""plan.yaml schema — pydantic models + loader."""
from __future__ import annotations

import datetime as dt
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, model_validator


class Priority(str, Enum):
    high = "high"
    med = "med"
    low = "low"


PRIORITY_ORDER = {Priority.high: 0, Priority.med: 1, Priority.low: 2}


class BudgetPolicy(str, Enum):
    downgrade = "downgrade"  # step down to a cheaper model that fits
    defer = "defer"          # stay pending, retry next tick (until deadline)
    skip = "skip"            # give up immediately


class Kind(str, Enum):
    llm = "llm"        # a Claude API call (budget-gated)
    shell = "shell"    # a shell command (no API cost; optionally in a container)
    claude = "claude"  # headless Claude Code (`claude -p`) on your SUBSCRIPTION login


class Budget(BaseModel):
    daily_usd: float = Field(gt=0)
    hourly_usd: Optional[float] = Field(default=None, gt=0)


class ModelSpec(BaseModel):
    id: str
    price_in: float = Field(description="USD per 1M input tokens")
    price_out: float = Field(description="USD per 1M output tokens")

    @model_validator(mode="after")
    def _prices_filled(self) -> "ModelSpec":
        if self.price_in <= 0 or self.price_out <= 0:
            raise ValueError(
                f"model '{self.id}': price_in/price_out must be > 0. "
                "Fill in current rates from https://platform.claude.com/docs/en/pricing "
                "— a 0.00 price would make the budget gate pass everything."
            )
        return self

    @property
    def price_total(self) -> float:
        return self.price_in + self.price_out


class Window(BaseModel):
    """Time-of-day window (local time). Both bounds optional."""
    earliest: Optional[dt.time] = None
    deadline: Optional[dt.time] = None

    def contains(self, now: dt.time) -> bool:
        if self.earliest and now < self.earliest:
            return False
        if self.deadline and now > self.deadline:
            return False
        return True

    def expired(self, now: dt.time) -> bool:
        return self.deadline is not None and now > self.deadline


class Task(BaseModel):
    id: str
    kind: Kind = Kind.llm
    model: Optional[str] = None          # required for kind=llm
    prompt: Optional[str] = None
    prompt_file: Optional[Path] = None
    system: Optional[str] = None
    command: Optional[str] = None        # required for kind=shell
    container: Optional[str] = None      # docker image for ONE-OFF isolation (--rm)
    workspace: Optional[str] = None      # persistent agent room (key into plan.workspaces)
    runs_on: Optional[str] = None        # mesh node name; None = run locally
    timeout_seconds: int = Field(default=600, gt=0)
    window: Window = Field(default_factory=Window)
    max_output_tokens: int = Field(default=2000, gt=0)
    priority: Priority = Priority.med
    on_budget_exceeded: BudgetPolicy = BudgetPolicy.defer
    depends_on: list[str] = Field(default_factory=list)
    # -- agentic (llm only) --
    agentic: bool = False                # multi-step tool-use loop vs single call
    tools: list[str] = Field(default_factory=list)   # subset of the tool set (empty → safe default)
    workdir: Optional[Path] = None       # root for file/bash tools (default: plan dir)
    memory: bool = True                  # recall before + remember tool during
    max_steps: int = Field(default=12, gt=0)
    # -- claude (subscription executor) --
    claude_model: Optional[str] = None   # alias passed to `claude --model` (haiku|sonnet|opus)
    claude_tools: list[str] = Field(default_factory=list)  # --allowedTools, e.g. ["Read", "Bash(git:*)"]

    @model_validator(mode="after")
    def _kind_fields(self) -> "Task":
        if self.kind is Kind.llm:
            if not self.model:
                raise ValueError(f"task '{self.id}': kind=llm needs 'model'")
            if not self.prompt and not self.prompt_file:
                raise ValueError(f"task '{self.id}': needs either 'prompt' or 'prompt_file'")
        elif self.kind is Kind.claude:
            if self.agentic:
                raise ValueError(f"task '{self.id}': kind=claude is already agentic — drop 'agentic'")
            if not self.prompt and not self.prompt_file:
                raise ValueError(f"task '{self.id}': needs either 'prompt' or 'prompt_file'")
        else:  # shell
            if self.agentic:
                raise ValueError(f"task '{self.id}': agentic requires kind=llm")
            if not self.command:
                raise ValueError(f"task '{self.id}': kind=shell needs 'command'")
            if self.container is not None and not self.container.strip():
                raise ValueError(f"task '{self.id}': empty 'container'")
        if self.workspace and self.kind is not Kind.shell:
            raise ValueError(f"task '{self.id}': 'workspace' needs kind=shell (a command to run in the room)")
        if self.workspace and self.container:
            raise ValueError(f"task '{self.id}': pick 'workspace' (persistent) or 'container' (one-off), not both")
        if self.tools:
            from .tools import ALL_TOOLS
            bad = [t for t in self.tools if t not in ALL_TOOLS]
            if bad:
                raise ValueError(f"task '{self.id}': unknown tools {bad}; valid: {ALL_TOOLS}")
        return self

    def prompt_text(self) -> str:
        if self.prompt:
            return self.prompt
        assert self.prompt_file is not None
        return self.prompt_file.read_text()


class Mesh(BaseModel):
    """Optional compute mesh: a hub URL where remote workers poll for work."""
    hub: Optional[str] = None  # e.g. http://100.64.0.3:4747 (falls back to $CONDUCTOR_HUB)


class Workspace(BaseModel):
    """A persistent, named container on one of your machines — a safe room
    where an agent (OpenClaw, Hermes, anything) lives across tasks. Created
    on first use (docker), set up once, then tasks exec into it."""
    image: str                          # e.g. node:22-bookworm
    setup: Optional[str] = None         # one-time install command inside the container
    node: Optional[str] = None          # default mesh node for tasks using this workspace


class Subscription(BaseModel):
    """Quota ceilings for `kind: claude` tasks (burn units = in+out+cache-write
    tokens). Anthropic doesn't publish absolute per-plan numbers — run
    `conductor quota` for a few days and calibrate. Unset ceilings never gate."""
    five_hour_tokens: Optional[int] = Field(default=None, gt=0)
    weekly_tokens: Optional[int] = Field(default=None, gt=0)
    # keep this fraction as headroom for your own interactive use
    reserve: float = Field(default=0.15, ge=0.0, lt=1.0)


class Plan(BaseModel):
    budget: Budget
    models: dict[str, ModelSpec]
    tasks: list[Task]
    mesh: Mesh = Field(default_factory=Mesh)
    subscription: Subscription = Field(default_factory=Subscription)
    workspaces: dict[str, Workspace] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _cross_check(self) -> "Plan":
        seen: set[str] = set()
        for t in self.tasks:
            if t.id in seen:
                raise ValueError(f"duplicate task id '{t.id}'")
            seen.add(t.id)
        for t in self.tasks:
            if t.model is not None and t.model not in self.models:
                raise ValueError(f"task '{t.id}': unknown model key '{t.model}'")
            if t.workspace is not None:
                if t.workspace not in self.workspaces:
                    raise ValueError(f"task '{t.id}': unknown workspace '{t.workspace}' "
                                     f"— plan has: {list(self.workspaces) or 'none'}")
                ws = self.workspaces[t.workspace]
                if t.runs_on is None and ws.node:
                    t.runs_on = ws.node  # tasks follow their room's machine by default
            for dep in t.depends_on:
                if dep not in seen:
                    raise ValueError(f"task '{t.id}': unknown dependency '{dep}'")
                if dep == t.id:
                    raise ValueError(f"task '{t.id}': depends on itself")
        return self

    def downgrade_chain(self, from_key: str) -> list[str]:
        """Model keys strictly cheaper than `from_key`, most expensive first."""
        start = self.models[from_key]
        cheaper = [
            k for k, m in self.models.items()
            if m.price_total < start.price_total
        ]
        return sorted(cheaper, key=lambda k: self.models[k].price_total, reverse=True)


def inbox_path(plan_path: Path) -> Path:
    """UI-scheduled tasks live in a machine-owned overlay file, so the
    dashboard never has to rewrite (and risk mangling) your plan.yaml."""
    return plan_path.parent / ".conductor" / "inbox.yaml"


def load_inbox_tasks(plan: Plan, plan_path: Path) -> list[Task]:
    """Validated tasks from the inbox overlay. Plan-file ids win on collision."""
    path = inbox_path(plan_path)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or []
    known = {t.id for t in plan.tasks}
    out: list[Task] = []
    for raw in data:
        t = Task.model_validate(raw)
        if t.id in known:
            continue
        if t.model is not None and t.model not in plan.models:
            continue
        _resolve_paths(t, plan_path)
        known.add(t.id)
        out.append(t)
    # drop tasks whose dependencies don't resolve (would wedge the scheduler)
    while True:
        valid = {t.id for t in plan.tasks} | {t.id for t in out}
        kept = [t for t in out if all(d in valid for d in t.depends_on)]
        if len(kept) == len(out):
            return kept
        out = kept


def _resolve_paths(t: Task, plan_path: Path) -> None:
    if t.prompt_file and not t.prompt_file.is_absolute():
        t.prompt_file = (plan_path.parent / t.prompt_file).resolve()
    if t.prompt_file and not t.prompt_file.exists():
        raise FileNotFoundError(f"task '{t.id}': prompt_file not found: {t.prompt_file}")
    if t.workdir and not t.workdir.is_absolute():
        t.workdir = (plan_path.parent / t.workdir).resolve()


def load_plan(path: Path | str, include_inbox: bool = True) -> Plan:
    path = Path(path)
    data = yaml.safe_load(path.read_text())
    plan = Plan.model_validate(data)
    for t in plan.tasks:
        _resolve_paths(t, path)
    if include_inbox:
        plan.tasks.extend(load_inbox_tasks(plan, path))
    return plan
