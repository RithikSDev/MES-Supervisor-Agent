"""The agents as MCP clients: servers registered through the API (token write-only), their tools offered only to
the agents allowed to use them, called over real MCP (streamable HTTP), results returned as data."""

import json
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from conftest import PLANT, Container, FakeMes, Script, bearer
from cryptography.fernet import Fernet
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import select, text
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount

from support_agent.db import mcp_server, step
from support_agent.mcp_client import McpHub

pytestmark = pytest.mark.db
KEY = "machine-data-key"


def _machine_server() -> Starlette:
    mcp = MCPServer("machines")

    @mcp.tool()
    async def machine_history(machine: str) -> dict:
        """Breakdowns of a machine and what fixed them."""
        return {"machine": machine, "breakdowns": [{"reason": "SPINDLE_OVERTEMP", "fix": "coolant filter replaced"}]}

    app = mcp.streamable_http_app(stateless_http=True, json_response=True,
                                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    class Key(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):  # type: ignore[no-untyped-def]
            if request.headers.get("authorization") != f"Bearer {KEY}":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    outer = Starlette(routes=[Mount("/", app)], lifespan=lambda _a: mcp.session_manager.run())
    outer.add_middleware(Key)
    return outer


@pytest.fixture(scope="module")
def machine_mcp() -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(_machine_server(), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True


async def test_registered_server_tools_reach_the_engineer_chat_only(
    env: tuple[Container, FakeMes], http: httpx.AsyncClient, machine_mcp: str,
) -> None:
    c, fake = env
    c.agent.mcp = McpHub(c.settings.model_copy(update={"agent_secret_key": Fernet.generate_key().decode()}),
                         c.session_factory)
    async with c.session_factory() as s, s.begin():
        await s.execute(text("DELETE FROM agent.mcp_server"))

    # the admin tests the server, then registers it for the engineer chat; the token never comes back
    t = await http.post("/api/v1/agent/mcp-servers/test", json={"url": machine_mcp, "token": KEY}, headers=bearer("admin-token"))
    assert t.json()["ok"] and [x["name"] for x in t.json()["tools"]] == ["machine_history"]
    bad = await http.post("/api/v1/agent/mcp-servers/test", json={"url": machine_mcp, "token": "wrong"}, headers=bearer("admin-token"))
    assert bad.json()["ok"] is False and bad.json()["error"]
    body = {"name": "machines", "label": "Machine data", "url": machine_mcp, "token": KEY, "modes": ["engineer"]}
    assert (await http.post("/api/v1/agent/mcp-servers", json=body, headers=bearer("sup-token"))).status_code == 403
    r = await http.post("/api/v1/agent/mcp-servers", json=body, headers=bearer("admin-token"))
    assert r.status_code == 201 and r.json()["token_set"] and KEY not in r.text
    listed = (await http.get("/api/v1/agent/mcp-servers", headers=bearer("sup-token"))).json()
    assert KEY not in json.dumps(listed) and listed["servers"][0]["name"] == "machines"
    async with c.session_factory() as s:
        stored = (await s.execute(select(mcp_server.c.token))).scalar_one()
    assert KEY not in stored

    # the engineer asks; the model sees the tool and calls it over MCP
    chat = (await http.post("/api/v1/agent/engineer/chats", json={"plant_id": PLANT}, headers=bearer("sup-token"))).json()
    model = Script([[("tool", "machines__machine_history", {"machine": "MILL-02"})],
                    [("text", "Last time the coolant filter fixed it.")]])
    c.agent.model = model
    r = await http.post(f"/api/v1/agent/engineer/chats/{chat['id']}/messages", json={"text": "MILL-02 spindle alarm"},
                        headers=bearer("sup-token"))
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    assert {"type": "status", "text": "Querying Machine data"} in events and events[-1]["type"] == "done"
    assert "machines__machine_history" in model.seen[0]["tools"]
    result = model.seen[1]["messages"][-1]["content"][0]
    assert result["content"].startswith('<data source="Machine data">') and "coolant filter replaced" in result["content"]
    async with c.session_factory() as s:
        names = (await s.execute(select(step.c.name).where(step.c.kind == "TOOL").order_by(step.c.id.desc()).limit(1))).scalar()
    assert names == "machines__machine_history"
    assert "machines__machine_history" not in [t.name for t in (await c.agent.mcp.toolset("chat")).tools.values()]

    # disabled or unreachable: left out, the run goes on
    sid = listed["servers"][0]["id"]
    await http.put(f"/api/v1/agent/mcp-servers/{sid}", json={**body, "token": None, "url": "http://127.0.0.1:9/mcp"},
                   headers=bearer("admin-token"))
    ts = await c.agent.mcp.toolset("engineer")
    assert not ts.tools and ts.unavailable == ["Machine data"]
