"""The model behind the agent, behind a small interface (`Model.turn`) so tests can script it.

`ClaudeModel` streams one Messages API turn with the tools, caches the stable prefix (tools + system prompt, then
the growing conversation), and opts into the server-side refusal fallback. Conversation content is kept as plain
dicts (what the API returned), appended as-is, so the history stays append-only.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

import anthropic

OnText = Callable[[str], Awaitable[None]]

# $ per million tokens (input, output); cache writes 1.25x input, cache reads 0.1x input
PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "claude-fable-5-1": (Decimal(10), Decimal(50)),
    "claude-opus-5-5": (Decimal(4), Decimal(20)),
    "claude-opus-5": (Decimal(5), Decimal(25)),
    "claude-opus-4-8": (Decimal(5), Decimal(25)),
    "claude-sonnet-5": (Decimal(2), Decimal(10)),
    "claude-haiku-4-5": (Decimal(1), Decimal(5)),
}
# models that take the server-side refusal fallback (`fallbacks: "default"`)
FALLBACK_MODELS = {"claude-opus-5", "claude-opus-5-5", "claude-fable-5-1"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def cost(self, model: str) -> Decimal:
        pin, pout = PRICES.get(model, PRICES["claude-opus-5"])
        m = Decimal(1_000_000)
        return (Decimal(self.input) * pin + Decimal(self.output) * pout + Decimal(self.cache_write) * pin * Decimal("1.25")
                + Decimal(self.cache_read) * pin * Decimal("0.1")) / m


@dataclass
class Turn:
    content: list[dict[str, Any]]
    stop_reason: str | None
    usage: Usage
    model: str
    refusal: str | None = None  # stop_details category when the whole chain declined
    notes: list[str] = field(default_factory=list)


class Model(Protocol):
    name: str

    async def turn(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], effort: str,
                   on_text: OnText | None = None) -> Turn: ...


def history_content(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What to append to the conversation. After a mid-output fallback, the declined partial's non-text blocks
    (thinking, tool calls, unknown block types) before the last `fallback` marker are dropped, as the API requires;
    the marker itself is only an audit record."""
    idx = max((i for i, b in enumerate(content) if b.get("type") == "fallback"), default=-1)
    if idx < 0:
        return content
    before = [b for b in content[:idx] if b.get("type") == "text"]
    return before + content[idx + 1:]


class ClaudeModel:
    def __init__(self, *, api_key: str, model: str, max_tokens: int, refusal_fallback: bool = True,
                 timeout_s: float = 300) -> None:
        self.name = model
        self._max_tokens = max_tokens
        self._fallback = refusal_fallback and model in FALLBACK_MODELS
        self._client = anthropic.AsyncAnthropic(api_key=api_key or None, timeout=timeout_s, max_retries=3)

    async def turn(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], effort: str,
                   on_text: OnText | None = None) -> Turn:
        kwargs: dict[str, Any] = {
            "model": self.name, "max_tokens": self._max_tokens, "system": system, "messages": messages,
            "tools": tools, "output_config": {"effort": effort},
            # caches tools + system + conversation so far; each later turn re-reads it at a tenth of the price
            "cache_control": {"type": "ephemeral"},
        }
        if self._fallback:
            kwargs |= {"betas": [FALLBACK_BETA], "fallbacks": "default"}
        for attempt in range(3):
            try:
                async with self._client.beta.messages.stream(**kwargs) as stream:
                    async for event in stream:
                        if event.type == "text" and on_text is not None:
                            await on_text(event.text)
                    final = await stream.get_final_message()
                break
            except ValueError:
                # a tool input the SDK could not parse at all (streamed tool input): re-issue the turn
                if attempt == 2:
                    raise
        u = final.usage
        usage = Usage(u.input_tokens or 0, u.output_tokens or 0, getattr(u, "cache_read_input_tokens", 0) or 0,
                      getattr(u, "cache_creation_input_tokens", 0) or 0)
        content = [b.to_dict() for b in final.content]
        refusal = None
        if final.stop_reason == "refusal":
            details = getattr(final, "stop_details", None)
            refusal = (getattr(details, "category", None) or "unspecified") if details else "unspecified"
        notes = [f"answered by {b['to']['model']} (fallback)" for b in content
                 if b.get("type") == "fallback" and isinstance(b.get("to"), dict)]
        return Turn(content, final.stop_reason, usage, final.model, refusal, notes)


def text_of(content: list[dict[str, Any]]) -> str:
    return "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()


def short(value: Any, limit: int = 6000) -> str:
    """Tool results go back to the model as compact JSON, capped."""
    s = value if isinstance(value, str) else json.dumps(value, default=str, separators=(",", ":"))
    return s if len(s) <= limit else s[:limit] + f"... [truncated, {len(s) - limit} more characters]"
