"""MCP client for the ITSM tools. The adapter talks to ServiceNow and Jira only through their MCP servers'
tools, so pointing it at the real ServiceNow / Atlassian MCP servers is a configuration change.

Tool errors follow "<CODE>: message"; UNAVAILABLE is retryable, every other code is a business rejection.
Transport failures are retried too: every sync is idempotent (look up before create, markers on notes).
"""

import logging
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from itsm_adapter.calls import begin, ms
from itsm_adapter.errors import ItsmError, ItsmErrorKind

log = logging.getLogger(__name__)


class McpTools:
    def __init__(self, system: str, url: str, key: str, timeout_s: float = 10.0) -> None:
        self.system = system
        self.url = url
        self._key = key
        self._timeout = timeout_s

    async def call(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        args = {k: v for k, v in (args or {}).items() if v is not None}
        entry, started = begin(self.system, tool, args)
        try:
            async with (
                httpx2.AsyncClient(headers={"Authorization": f"Bearer {self._key}"}, timeout=self._timeout) as http,
                Client(streamable_http_client(self.url, http_client=http), read_timeout_seconds=self._timeout) as client,
            ):
                result = await client.call_tool(tool, args)
        except Exception as e:
            entry.update(outcome="TRANSPORT", ms=ms(started))
            raise ItsmError(ItsmErrorKind.AMBIGUOUS, f"{self.system}.{tool}: {e!r}") from e
        entry["ms"] = ms(started)
        if result.is_error:
            text = " ".join(getattr(c, "text", "") for c in result.content).strip()
            message = text.split(": ", 1)[1] if text.startswith("Error executing tool") and ": " in text else text
            code = message.split(":", 1)[0].strip() if ":" in message else ""
            entry.update(outcome="ERROR", error=message[:300])
            kind = ItsmErrorKind.RETRYABLE if code == "UNAVAILABLE" else ItsmErrorKind.PERMANENT
            raise ItsmError(kind, f"{self.system}.{tool}: {message}")
        entry["outcome"] = "OK"
        data = result.structured_content
        if isinstance(data, dict) and set(data) == {"result"}:
            data = data["result"]
        return data

    async def ping(self) -> list[str]:
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {self._key}"}, timeout=self._timeout) as http,
            Client(streamable_http_client(self.url, http_client=http), read_timeout_seconds=self._timeout) as client,
        ):
            return [t.name for t in (await client.list_tools()).tools]

