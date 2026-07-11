"""CONDUCTOR — token-budget-aware task scheduler for the Claude API.

Declare tasks in plan.yaml (time window + token/cost budget + priority);
the runner pre-flights input cost with count_tokens, gates on remaining
budget, executes, and reconciles actual spend from response.usage.
"""
from .agent import AgentResult, run_agent
from .claude_exec import run_claude
from .estimator import cost_usd, estimate
from .executor import Outcome, RunResult, gate_and_run
from .ledger import Ledger
from .memory import Memory, MemoryStore
from .scheduler import Scheduler, State
from .schema import (
    Budget, BudgetPolicy, Kind, Mesh, ModelSpec, Plan, Priority, Task, Window, load_plan,
)
from .worker import run_worker

__version__ = "0.11.0"

__all__ = [
    "AgentResult", "Budget", "BudgetPolicy", "Kind", "Ledger", "Memory",
    "MemoryStore", "Mesh", "ModelSpec", "Outcome", "Plan", "Priority",
    "RunResult", "Scheduler", "State", "Task", "Window",
    "cost_usd", "estimate", "gate_and_run", "load_plan", "run_agent",
    "run_claude", "run_worker",
]
