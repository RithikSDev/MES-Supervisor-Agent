"""Support agent tests: a throwaway Postgres for the agent's tables, a scripted model instead of Claude (no API
calls, no cost) and an in-memory fake of the MES APIs that records every call and the token it was made with."""

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import pytest_asyncio
from sqlalchemy import text
from testcontainers.postgres import PostgresContainer

from mes_common.auth import Principal, StaticTokenVerifier
from mes_common.ids import new_id
from support_agent.api import create_app
from support_agent.clients import Api, ApiError
from support_agent.container import Container, build_container
from support_agent.llm import Turn, Usage
from support_agent.settings import Settings
from support_agent.store import ensure_defaults

SERVICE = Path(__file__).resolve().parents[1]
PLANT = "01a0d247-5aac-7059-beb5-d63add203ce1"
OPERATOR_ID = "01a0d247-0000-7000-8000-000000000001"
OTHER_OPERATOR_ID = "01a0d247-0000-7000-8000-000000000002"


def terminal_token(sub: str) -> str:
    return jwt.encode({"iss": "mes-core:terminal", "sub": sub}, "test-secret-test-secret-test-secret", algorithm="HS256")


OPERATOR = terminal_token(OPERATOR_ID)
OTHER_OPERATOR = terminal_token(OTHER_OPERATOR_ID)
PRINCIPALS = {
    "sup-token": Principal("sub-sup", "supervisor1", frozenset({"supervisor"}), "Sam Supervisor"),
    "admin-token": Principal("sub-admin", "admin1", frozenset({"admin"}), "Ada Admin"),
    "op-user-token": Principal("sub-op", "operator1", frozenset({"operator"}), "Olga Operator"),
}


class FakeMes(Api):
    """Routes MES API calls to canned data and records them: (service, method, path, params/body, token)."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.calls: list[tuple[str, str, str, Any, str | None]] = []
        self.tickets: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []
        self.fail: dict[tuple[str, str], ApiError] = {}

    async def service_token(self) -> str:
        return "agent-service-token"

    async def loki(self, query: str, *, start_ns: int, end_ns: int, limit: int, direction: str = "backward") -> Any:
        self.calls.append(("loki", "GET", "query_range", {"query": query, "limit": limit}, None))
        line = ('{"ts": "2026-09-25T10:00:01.000+00:00", "level": "ERROR", "service": "erp-adapter-worker", '
                '"logger": "erp_adapter.outbound", "msg": "SAP rejected confirmation for 1000123: order is TECO", '
                '"request_id": "event:abc"}')
        return [{"stream": {"service": "erp-adapter-worker", "level": "ERROR"},
                 "values": [["1790000000000000000", line], ["1790000000000000001", "plain text line"]]}]

    async def health(self, service: str) -> tuple[bool, str]:  # type: ignore[override]
        return True, "HTTP 200"

    def add_ticket(self, **kw: Any) -> dict[str, Any]:
        n = len(self.tickets) + 1
        t = {"id": str(new_id()), "number": f"MES-{n:06d}", "plant_id": PLANT, "status": "NEW",
             "tier": "L1", "severity": "P2", "suggested_severity": "P2", "category": "HARDWARE", "impact": "DEGRADED",
             "source": "TERMINAL", "title": "Label printer prints blank labels", "description": "Labels come out empty",
             "reported_by_name": "Olga Operator", "reported_at": "2026-09-25T10:00:00Z", "line_code": "L1",
             "equipment_code": None, "activity": [],
             "response": {"due": "2026-09-25T10:30:00Z", "met_at": None, "remaining_s": 900.0, "breached": False,
                          "paused": False},
             "resolution": {"due": "2026-09-25T16:00:00Z", "met_at": None, "remaining_s": 18000.0, "breached": False,
                            "paused": False}, **kw}
        self.tickets[t["id"]] = t
        return t

    async def call(self, service: str, method: str, path: str, *, params: dict[str, Any] | None = None,
                   json: Any = None, token: str | None = None) -> Any:
        self.calls.append((service, method, path, json if method != "GET" else params, token))
        if (method, path) in self.fail:
            raise self.fail[(method, path)]
        if path == "/me":
            who = {OPERATOR: OPERATOR_ID, OTHER_OPERATOR: OTHER_OPERATOR_ID}.get(token or "")
            if who is None:
                raise ApiError(401, "session ended")
            return {"id": who, "username": "operator1", "display_name": "Olga Operator", "roles": ["operator"],
                    "plant_ids": [PLANT], "terminal_id": "01a0d27f-d15b-737f-a80d-8138abf76ee8", "terminal_code": "T-L1",
                    "line_id": "01a0d247-5ab4-76f9-a505-4ea002275915", "line_code": "L1"}
        if path == "/plants":
            return [{"id": PLANT, "code": "P1000"}]
        if path == "/tickets/support-team":
            return [{"id": "m1", "username": "l1.priya", "display_name": "Priya Nair", "email": "priya.nair@mes.local",
                     "tier": "L1", "active": True, "open_tickets": 1},
                    {"id": "m2", "username": "l2.wei", "display_name": "Wei Chen", "email": "wei.chen@mes.local",
                     "tier": "L2", "active": True, "open_tickets": 0}]
        if path == "/tickets/forecast":
            h = int((params or {}).get("horizon_days", 7))
            return {"horizon_days": h, "history_weeks": 8, "history_tickets": 120, "method": "trend x weekday",
                    "totals": {"expected": 2.5 * h, "low": 2 * h, "high": 3 * h, "per_week": [], "last_7_days": 17},
                    "trend_per_week": 1.1, "peak_day": {"day": "2026-09-29", "expected": 4.1}, "by_severity": [],
                    "expected_p1_p2": 3.2, "by_category": [], "backlog": {"open_now": 13}, "backtest": None,
                    "enough_history": True, "weekly_history": [], "forecast": [{"day": "2026-09-28"}] * h}
        if path == "/tickets/sla-report":
            return {"since": "2026-09-21T00:00:00Z", "until": "2026-09-28T00:00:00Z",
                    "summary": {"breaches": 4, "exits": 20, "resolution_met_pct": 85.0}, "by_severity": [],
                    "by_assignee": [], "breaches": [], "exits": [], "at_risk": []}
        if path.startswith("/tickets/") and path != "/tickets/search" and path.split("/")[2] not in self.tickets:
            raise ApiError(404, "ticket not found")
        if method == "GET" and path == "/tickets":
            open_only = (params or {}).get("open_only")
            return [t for t in self.tickets.values()
                    if not open_only or t["status"] in ("NEW", "ACKNOWLEDGED", "IN_PROGRESS", "ON_HOLD")]
        if method == "GET" and path.startswith("/tickets/") and path != "/tickets/search":
            return self.tickets[path.split("/")[2]]
        if path == "/tickets/search":
            return [t for t in self.tickets.values() if t["number"] == (params or {}).get("q")]
        if method == "POST" and path == "/tickets":
            t = self.add_ticket(**{k: v for k, v in json.items() if k != "plant_id"}, source="TERMINAL")
            self.created.append(t)
            return t
        if method == "POST" and path.endswith("/status"):
            t = self.tickets[path.split("/")[2]]
            t["status"] = json["status"]
            return t
        if method == "POST" and path.endswith("/escalate"):
            t = self.tickets[path.split("/")[2]]
            t["tier"], t["status"] = {"L1": "L2", "L2": "L3"}[t["tier"]], "IN_PROGRESS"
            return t
        if method == "POST" and path.endswith("/details"):
            tk = self.tickets[path.split("/")[2]]
            tk.update({k: v for k, v in json.items() if k != "reason"})
            return tk
        if method == "POST" and path.endswith("/severity"):
            t = self.tickets[path.split("/")[2]]
            t["severity"] = json["severity"]
            return t
        if method == "POST" and path.endswith("/assign"):
            t = self.tickets[path.split("/")[2]]
            t["assignee_name"] = json["assignee"]
            return t
        if method == "POST" and path.endswith("/comments"):
            return self.tickets[path.split("/")[2]]
        if path == "/maintenance/equipment":
            return [{"id": "eq-1", "code": "TURN-01", "name": "CNC lathe", "status": "AVAILABLE", "line_code": "L1",
                     "open_downtime": None, "open_notifications": 0, "open_orders": 0, "reliability": {}}]
        if path == "/execution/live":
            return {"machines": [{"equipment_code": "TURN-01", "line_code": "L1", "equipment_status": "AVAILABLE",
                                  "state": "RUNNING", "erp_order_number": "1000123", "op_no": "0010",
                                  "operator_name": "Olga Operator", "pause_reason": None, "since": None}]}
        if path.startswith("/work-orders/"):
            return {"status": "IN_PROGRESS", "erp_status": "REL", "planned_qty": "50", "operations": [
                {"op_no": "0010", "status": "IN_PROGRESS", "planned_qty": "50", "yield_qty": "10"}], "history": []}
        if path == "/work-orders":
            return {"items": [{"id": "wo-1", "erp_order_number": "1000123"}]}
        if path == "/terminals":
            return [{"id": "term-1", "code": "T-L1", "name": "Terminal L1", "line_code": "L1", "active": True,
                     "enrolled": True, "session_operator": "Night Shift", "session_started_at": "2026-09-24T22:00:00Z",
                     "pin_locked_until": None}]
        if path == "/ops/event-failures":
            return [{"created_at": "2026-09-25T10:00:00Z", "topic": "erp.work-orders.v1", "event_type": "x", "key": "k",
                     "error": "boom"}]
        if service == "erp" and path.startswith("/outbound/"):
            return {"id": "msg-1", "message_type": "CONFIRMATION", "erp_key": "1000123", "status": "FAILED",
                    "last_error": "order is technically completed (TECO)", "attempts_log": [{"attempt_no": 1}]}
        if service == "erp" and path == "/inbound":
            return [{"received_at": "2026-09-25T09:00:00Z", "erp_changed_at": "2026-09-25T08:59:00Z",
                     "payload": {"status": "TECO"}}]
        if service == "erp" and path == "/outbound":
            return [{"id": "msg-1", "message_type": "CONFIRMATION", "erp_key": "1000123", "status": "FAILED",
                     "attempts": 8, "max_attempts": 8, "last_error": "SAP locked by user PLANNER01", "payload": {}}]
        if method == "POST":
            return {"ok": True}
        return []


class Script:
    """A scripted model: each turn is a list of blocks; ("text", s) or ("tool", name, input). Records what it saw."""

    name = "claude-opus-5"

    def __init__(self, turns: list[list[tuple[Any, ...]]], usage: Usage | None = None) -> None:
        self.turns = list(turns)
        self.usage = usage or Usage(input=2000, output=300)
        self.seen: list[dict[str, Any]] = []

    async def turn(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], effort: str,
                   on_text: Any = None) -> Turn:
        self.seen.append({"system": system, "messages": [dict(m) for m in messages], "tools": [t["name"] for t in tools],
                          "effort": effort})
        blocks = self.turns.pop(0) if self.turns else [("text", "done")]
        content: list[dict[str, Any]] = []
        for i, b in enumerate(blocks):
            if b[0] == "text":
                content.append({"type": "text", "text": b[1]})
                if on_text:
                    await on_text(b[1])
            else:
                content.append({"type": "tool_use", "id": f"toolu_{len(self.seen)}_{i}", "name": b[1], "input": b[2]})
        stop = "tool_use" if any(c["type"] == "tool_use" for c in content) else "end_turn"
        return Turn(content, stop, self.usage, self.name)


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with PostgresContainer("postgres:17-alpine", username="integ", password="integ", dbname="mes_integration") as pg:
        url = f"postgresql+asyncpg://integ:integ@{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/mes_integration"
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=SERVICE,
                       env={**os.environ, "MIGRATION_DATABASE_URL": url}, check=True)
        yield url


@pytest.fixture(scope="session")
def settings(database_url: str) -> Settings:
    return Settings(database_url=database_url, anthropic_api_key="")


@pytest_asyncio.fixture
async def env(settings: Settings) -> AsyncIterator[tuple[Container, FakeMes]]:
    fake = FakeMes(settings)
    c = build_container(settings, api=fake, use_default_model=False)
    c.agent.model = Script([])  # "configured"; tests replace it with their script
    await ensure_defaults(c.session_factory)
    async with c.session_factory() as s, s.begin():
        await s.execute(text("UPDATE agent.setting SET value = 'false' WHERE key = 'kill_switch'"))
        await s.execute(text("DELETE FROM agent.mcp_server"))  # MCP servers only where a test registers them
        await s.execute(text("UPDATE agent.run SET status = 'SKIPPED' WHERE status IN ('QUEUED', 'RUNNING')"))
        await s.execute(text("UPDATE agent.policy SET mode = CASE WHEN action IN ('add_note','escalate','create_ticket',"
                             "'resolve_confirmed') THEN 'auto' ELSE 'suggest' END"))
    yield c, fake
    await c.aclose()


@pytest_asyncio.fixture
async def http(env: tuple[Container, FakeMes]) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(env[0], verifier=StaticTokenVerifier(PRINCIPALS))
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client):
        yield client


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
