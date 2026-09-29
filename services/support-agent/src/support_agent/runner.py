"""The agent loop: model turn -> tool calls -> results -> next turn, until the model answers without tools.

Limits: a step cap and a cost cap per run (a run that hits either is stopped and handed to a human by the caller).
The conversation is append-only: assistant content is appended as returned, tool results as one user turn.
"""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from support_agent.llm import Model, history_content, text_of
from support_agent.tools import Context, execute, tools_for


class LimitReached(Exception):
    pass


# tools after which the agent waits for the operator instead of calling the model again (saves a round trip); the
# tool results stay as the last (user) message and the operator's answer is added to it
WAIT_FOR_OPERATOR = {"show_quick_replies"}


@dataclass
class LoopResult:
    text: str
    stop_reason: str | None
    refusal: str | None = None


async def run_loop(
    *, model: Model, system: str, messages: list[dict[str, Any]], ctx: Context, effort: str, max_steps: int,
    budget_usd: Decimal, on_text: Callable[[str], Awaitable[None]] | None = None,
) -> LoopResult:
    specs = [t.spec() for t in tools_for(ctx.mode)] + (ctx.mcp.specs() if ctx.mcp is not None else [])
    spoke, fresh = False, True

    async def turn_text(delta: str) -> None:
        nonlocal spoke, fresh
        if on_text is None or not delta:
            return
        if fresh and spoke:
            await on_text("\n\n")  # text of a new model turn starts a new paragraph
        fresh, spoke = False, True
        await on_text(delta)

    for _ in range(max_steps):
        fresh = True
        if ctx.recorder.cost >= budget_usd:
            raise LimitReached(f"cost limit reached (${ctx.recorder.cost:.2f} of ${budget_usd:.2f})")
        started = time.monotonic()
        turn = await model.turn(system=system, messages=messages, tools=specs, effort=effort, on_text=turn_text)
        await ctx.recorder.add_usage(turn.usage, turn.model)
        content = history_content(turn.content)
        messages.append({"role": "assistant", "content": content})
        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        await ctx.recorder.step("MODEL", turn.model, None, {
            "text": text_of(content), "tool_calls": [b["name"] for b in tool_uses], "stop_reason": turn.stop_reason,
            "usage": vars(turn.usage), "notes": turn.notes or None}, started=started)
        if turn.stop_reason == "refusal":
            return LoopResult(text_of(content), "refusal", turn.refusal)
        if turn.stop_reason == "pause_turn":
            continue
        if not tool_uses:
            return LoopResult(text_of(content), turn.stop_reason)
        if turn.stop_reason == "max_tokens":
            # a tool input cut off at the token limit parses as a partial object: never run it
            results = [{"type": "tool_result", "tool_use_id": b["id"], "is_error": True,
                        "content": "your output was cut off; issue the call again, shorter"} for b in tool_uses]
        else:
            results = []
            for b in tool_uses:
                out, is_error = await execute(ctx, b["name"], b.get("input"))
                results.append({"type": "tool_result", "tool_use_id": b["id"], "content": out,
                                **({"is_error": True} if is_error else {})})
        messages.append({"role": "user", "content": results})
        if any(b["name"] in WAIT_FOR_OPERATOR for b in tool_uses):
            return LoopResult(text_of(content), "awaiting_operator")
    raise LimitReached(f"step limit reached ({max_steps} model turns)")
