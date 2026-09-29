"""The agents as MCP clients: live data from MCP servers registered in the admin UI (the MES database and SAP by
default, any other MCP server later: machine data, a historian, a maintenance system).

For a run, `McpHub.toolset(mode)` offers the tools of every enabled server allowed for that agent as
`<server>__<tool>`, with the server's own input schema. Each call opens a short MCP session (streamable HTTP, the
server's bearer token) and returns the result wrapped as data from that source. Tool lists are cached briefly; a
server that can't be reached is left out of the run (and shown as unreachable in the UI).
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx2
from cryptography.fernet import Fernet, InvalidToken
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mes_common.ids import new_id
from mes_common.logs import request_id_var
from support_agent.db import mcp_server
from support_agent.settings import Settings

log = logging.getLogger(__name__)
MODES = ("engineer", "l2", "ticket", "chat")
NAME = re.compile(r"^[a-z][a-z0-9_]{1,19}$")
TOOL_TTL_S = 300


class Vault:
    def __init__(self, key: str) -> None:
        try:
            self._f: Fernet | None = Fernet(key.encode()) if key else None
            self.error = None if key else "AGENT_SECRET_KEY (the ITSM_SECRET_KEY) is not set"
        except (ValueError, TypeError):
            self._f, self.error = None, "AGENT_SECRET_KEY is not a valid key"

    @property
    def ready(self) -> bool:
        return self._f is not None

    def seal(self, secret: str) -> str:
        if self._f is None:
            raise ValueError(self.error)
        return self._f.encrypt(secret.encode()).decode()

    def open(self, token: str | None) -> str:
        if not token:
            return ""
        if self._f is None:
            raise ValueError(self.error)
        try:
            return self._f.decrypt(token.encode()).decode()
        except InvalidToken as e:
            raise ValueError("the stored token can't be decrypted (key changed?): enter it again") from e


@dataclass(frozen=True)
class McpTool:
    name: str  # <server>__<tool>
    server: str
    label: str
    tool: str
    description: str
    schema: dict[str, Any]
    url: str
    token: str

    def spec(self) -> dict[str, Any]:
        schema = self.schema if self.schema.get("type") == "object" else {"type": "object", "properties": {}}
        return {"name": self.name, "description": f"[{self.label}, live data] {self.description}"[:1024],
                "input_schema": schema}


@dataclass
class Toolset:
    tools: dict[str, McpTool] = field(default_factory=dict)
    unavailable: list[str] = field(default_factory=list)  # servers left out of this run (unreachable)

    def specs(self) -> list[dict[str, Any]]:
        return [t.spec() for t in self.tools.values()]


async def _session_call(url: str, token: str, wait_s: float, tool: str | None = None,
                        args: dict[str, Any] | None = None) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if rid := request_id_var.get():
        headers["X-Request-ID"] = rid
    async with (
        httpx2.AsyncClient(headers=headers, timeout=wait_s) as http,
        Client(streamable_http_client(url, http_client=http), read_timeout_seconds=wait_s) as client,
    ):
        if tool is None:
            return [{"name": t.name, "description": t.description or "", "schema": t.input_schema or {}}
                    for t in (await client.list_tools()).tools]
        return await client.call_tool(tool, args or {})


def _error_text(e: BaseException) -> str:
    inner = getattr(e, "exceptions", None)  # anyio task groups wrap transport errors
    if inner:
        return _error_text(inner[0])
    return f"{type(e).__name__}: {str(e)[:200]}"


class McpHub:
    def __init__(self, settings: Settings, sf: async_sessionmaker[AsyncSession]) -> None:
        self.s, self._sf = settings, sf
        self.vault = Vault(settings.agent_secret_key)
        self._tools: dict[tuple[str, int], tuple[float, list[dict[str, Any]]]] = {}

    async def rows(self) -> list[Any]:
        async with self._sf() as s:
            return list((await s.execute(select(mcp_server).order_by(mcp_server.c.name))).all())

    async def discover(self, url: str, token: str) -> list[dict[str, Any]]:
        return await _session_call(url, token, self.s.mcp_timeout_s)

    async def toolset(self, mode: str) -> Toolset:
        out = Toolset()
        for row in await self.rows():
            if not row.enabled or mode not in (row.modes or []):
                continue
            try:
                token = self.vault.open(row.token)
                key = (str(row.id), row.version)
                cached = self._tools.get(key)
                if cached and time.monotonic() - cached[0] < TOOL_TTL_S:
                    tools = cached[1]
                else:
                    tools = await self.discover(row.url, token)
                    self._tools[key] = (time.monotonic(), tools)
                    await self._checked(row.id, tools, None)
            except Exception as e:  # noqa: BLE001 - one server down must not stop the run
                log.warning("MCP server %s left out: %s", row.name, _error_text(e))
                await self._checked(row.id, None, _error_text(e))
                out.unavailable.append(row.label)
                continue
            for t in tools:
                name = f"{row.name}__{t['name']}"[:64]
                out.tools[name] = McpTool(name, row.name, row.label, t["name"], t["description"], t["schema"], row.url,
                                          token)
        return out

    async def _checked(self, server_id: Any, tools: list[dict[str, Any]] | None, error: str | None) -> None:
        values: dict[str, Any] = {"last_check_at": datetime.now(UTC), "last_error": error}
        if tools is not None:
            values["tools"] = [{"name": t["name"], "description": t["description"][:300]} for t in tools]
        async with self._sf() as s, s.begin():
            await s.execute(update(mcp_server).where(mcp_server.c.id == server_id).values(**values))

    async def call(self, tool: McpTool, args: dict[str, Any]) -> tuple[str, bool]:
        """(text for the model, is_error). Results are wrapped as data from their source."""
        try:
            r = await _session_call(tool.url, tool.token, self.s.mcp_timeout_s, tool.tool, args)
        except Exception as e:  # noqa: BLE001
            return f"{tool.label} could not be reached ({_error_text(e)})", True
        if r.structured_content is not None:
            data = r.structured_content
            if isinstance(data, dict) and set(data) == {"result"}:
                data = data["result"]
            text = json.dumps(data, default=str, separators=(",", ":"))
        else:
            text = " ".join(getattr(c, "text", "") for c in r.content).strip()
        if r.is_error:
            return text.removeprefix(f"Error executing tool {tool.tool}: "), True
        return f'<data source="{tool.label}">\n{text}\n</data>', False

    async def ensure_defaults(self) -> None:
        """First start: register the stack's read-only data MCP servers for the engineer chat and the L2 agent."""
        if not self.vault.ready:
            return
        defaults = [
            ("mes_db", "MES database", "Read-only SQL on the MES database: machines, breakdowns, maintenance, orders, "
             "runs, stock, quality, tickets, audit", self.s.default_mes_db_mcp_url, self.s.default_mes_db_mcp_key),
            ("sap", "SAP", "Read-only SAP OData APIs: production orders, confirmations, goods movements, maintenance "
             "orders", self.s.default_sap_mcp_url, self.s.default_sap_mcp_key),
        ]
        async with self._sf() as s, s.begin():
            if (await s.execute(select(mcp_server.c.id).limit(1))).first():
                return
            for name, label, desc, url, key in defaults:
                if url:
                    await s.execute(insert(mcp_server).values(
                        id=new_id(), name=name, label=label, description=desc, url=url,
                        token=self.vault.seal(key) if key else None, modes=["engineer", "l2"], updated_by="default")
                        .on_conflict_do_nothing(index_elements=["name"]))
