"""The external calls (REST requests or MCP tool calls) of the current delivery attempt, for the integration monitor."""

import contextvars
import time
from typing import Any

call_log: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar("itsm_call_log", default=None)


def begin(system: str, tool: str, args: dict[str, Any] | None = None) -> tuple[dict[str, Any], float]:
    entry: dict[str, Any] = {"system": system, "tool": tool, "args": short(args or {})}
    log_list = call_log.get()
    if log_list is not None:
        log_list.append(entry)
    return entry, time.monotonic()


def ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def short(args: dict[str, Any]) -> dict[str, Any]:
    return {k: (v[:80] + "…" if isinstance(v, str) and len(v) > 80 else v) for k, v in args.items()}
