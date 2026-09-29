"""MES MCP server tools against a fake core API (httpx MockTransport), called through a real MCP client in-process."""

import json

import httpx
from mcp import Client

from mes_mcp.core import Core, Settings
from mes_mcp.server import build_server

PLANT = {"id": "p1", "code": "P1000"}
TICKET = {
    "id": "t1", "number": "MES-000007", "title": "Terminal frozen", "status": "NEW", "severity": "P1", "tier": "L1",
    "category": "MES_APP", "impact": "LINE_STOP", "line_code": "L2", "equipment_code": None, "erp_order_number": None,
    "reported_at": "2026-09-24T10:00:00+00:00", "reported_by_name": "Oscar", "sn_number": "INC0010001", "jira_key": None,
    "response": {"remaining_s": 300.0, "breached": False}, "resolution": {"remaining_s": -120.0, "breached": True},
    "description": "screen stuck", "resolution_note": None, "activity": [],
}


def fake_core() -> tuple[Core, list[tuple[str, str, dict | None]]]:
    calls: list[tuple[str, str, dict | None]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content and req.headers.get("content-type") == "application/json" else None
        calls.append((req.method, req.url.path, body))
        if req.url.path.endswith("/token"):
            assert req.content and b"client_credentials" in req.content
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 300})
        assert req.headers["authorization"] == "Bearer tok"
        path = req.url.path.removeprefix("/api/v1")
        if path == "/plants":
            return httpx.Response(200, json=[PLANT])
        if path == "/tickets" and req.method == "GET":
            return httpx.Response(200, json=[TICKET])
        if path == "/maintenance/equipment":
            return httpx.Response(200, json=[{"id": "e1", "code": "MILL-02"}])
        if path == "/tickets" and req.method == "POST":
            return httpx.Response(201, json={**TICKET, "number": "MES-000008", "severity": "P2"})
        if path == "/tickets/t1/escalate":
            return httpx.Response(409, json={"code": "max_tier", "message": "already at L3"})
        return httpx.Response(404, json={"code": "not_found", "message": path})

    s = Settings(core_api_url="http://core/api/v1", token_url="http://kc/token")
    return Core(s, httpx.AsyncClient(transport=httpx.MockTransport(handler))), calls


async def test_tools_call_the_core_api_as_the_service_account() -> None:
    core, calls = fake_core()
    mcp = build_server(core, core.s)
    async with Client(mcp) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert {"list_tickets", "create_ticket", "escalate_ticket", "sla_overview", "line_status", "trace"} <= names

        r = await c.call_tool("list_tickets", {})
        rows = r.structured_content["result"]
        assert rows[0]["number"] == "MES-000007" and rows[0]["resolution_minutes_left"] == -2 and rows[0]["resolution_breached"]

        r = await c.call_tool("sla_overview", {})
        assert r.structured_content["open"] == 1 and r.structured_content["by_severity"] == {"P1": 1}

        r = await c.call_tool("create_ticket", {"title": "Spindle alarm", "description": "MILL-02 alarm 2031",
                                                "category": "HARDWARE", "impact": "DEGRADED", "machine": "mill-02"})
        assert not r.is_error and r.structured_content["number"] == "MES-000008"
        post = next(b for m, p, b in calls if m == "POST" and p == "/api/v1/tickets")
        assert post["equipment_id"] == "e1" and post["plant_id"] == "p1" and post["severity"] is None

        r = await c.call_tool("escalate_ticket", {"ticket": "INC0010001", "reason": "needs code fix"})
        assert r.is_error and "max_tier" in r.content[0].text
    assert sum(1 for _, p, _ in calls if p.endswith("/token")) == 1  # token cached
