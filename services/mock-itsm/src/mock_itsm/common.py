"""Shared plumbing for the mock ITSM systems: JSON-file state, the MCP endpoint (bearer key), fault injection."""

import json
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware


def now() -> datetime:
    n = datetime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)


# Tool error convention understood by the itsm-adapter: "<CODE>: message". UNAVAILABLE is retryable.
def fail(code: str, message: str) -> ToolError:
    return ToolError(f"{code}: {message}")


@dataclass
class Faults:
    fail_next: int = 0  # next N MCP tool calls fail with UNAVAILABLE (retryable)


@dataclass
class JsonState:
    """Everything a mock system knows, persisted as one JSON document."""

    path: Path | None
    data: dict[str, Any] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)
    faults: Faults = field(default_factory=Faults)

    def load(self, empty: Callable[[], dict[str, Any]]) -> None:
        if self.path and self.path.exists():
            self.data = json.loads(self.path.read_text())
        else:
            self.data = empty()

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, default=str))
        tmp.replace(self.path)

    def check_fault(self) -> None:
        with self.lock:
            if self.faults.fail_next > 0:
                self.faults.fail_next -= 1
                raise fail("UNAVAILABLE", "service temporarily unavailable (injected fault)")


class BearerKey(BaseHTTPMiddleware):
    """The MCP endpoint needs `Authorization: Bearer <key>` (a service credential, as with a real ITSM MCP server)."""

    def __init__(self, app: Any, key: str, path: str = "/mcp") -> None:
        super().__init__(app)
        self._key = key
        self._path = path

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path.rstrip("/") == self._path and request.headers.get("authorization") != f"Bearer {self._key}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def mount_mcp(app: FastAPI, mcp: MCPServer) -> None:
    """Serve `mcp` at /mcp (Streamable HTTP, stateless, JSON responses) inside `app`; call after adding routes."""
    mcp_app = mcp.streamable_http_app(
        stateless_http=True, json_response=True, host="0.0.0.0",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    app.mount("/", mcp_app)


def mcp_lifespan(mcp: MCPServer):  # type: ignore[no-untyped-def]
    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        async with mcp.session_manager.run():
            yield

    return lifespan
