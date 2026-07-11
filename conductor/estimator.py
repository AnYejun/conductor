"""Pre-flight cost estimation via the count_tokens API.

Rules this module encodes (per Anthropic docs):
- count_tokens MUST be called with the exact model id that will execute —
  tokenizers differ across models (Opus 4.7+ counts ~30% more than older ones).
- Never use tiktoken (OpenAI tokenizer; undercounts Claude by 15-20%+).
- count_tokens is free and has a separate rate limit → fine to call per task.
- Output tokens can't be known in advance → max_output_tokens is the
  worst-case line for the budget gate. Actuals are reconciled from usage.
"""
from __future__ import annotations

from dataclasses import dataclass

from anthropic import AsyncAnthropic

from .schema import ModelSpec, Task


def build_messages(task: Task) -> list[dict]:
    """The exact messages the executor will send — estimate and run must match."""
    return [{"role": "user", "content": task.prompt_text()}]


def cost_usd(model: ModelSpec, input_tokens: int, output_tokens: int,
             cache_read: int = 0, cache_creation: int = 0) -> float:
    """USD cost given token counts. Cache reads bill ~0.1x, cache writes ~1.25x input rate."""
    return (
        input_tokens / 1e6 * model.price_in
        + output_tokens / 1e6 * model.price_out
        + cache_read / 1e6 * model.price_in * 0.1
        + cache_creation / 1e6 * model.price_in * 1.25
    )


@dataclass
class Estimate:
    input_tokens: int
    est_usd: float  # worst case: measured input + max_output_tokens output


async def estimate(client: AsyncAnthropic, task: Task, model: ModelSpec) -> Estimate:
    kwargs: dict = {"model": model.id, "messages": build_messages(task)}
    if task.system:
        kwargs["system"] = task.system
    resp = await client.messages.count_tokens(**kwargs)
    est_in = resp.input_tokens
    return Estimate(
        input_tokens=est_in,
        est_usd=cost_usd(model, est_in, task.max_output_tokens),
    )
