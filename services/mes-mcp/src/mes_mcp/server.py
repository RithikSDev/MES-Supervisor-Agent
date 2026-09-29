"""MES MCP server: lets AI agents (Claude, the Phase 7 RCA assistant, ...) read the shop floor and raise and triage
support tickets. Tools return compact JSON meant for a model to read.

Run: `uvicorn mes_mcp.server:create_app --factory --port 8040`; clients connect to http://localhost:8040/mcp with
`Authorization: Bearer <MCP_API_KEY>`.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from mes_mcp.core import Core, CoreError, Settings, get_settings

Category = Literal["MES_APP", "HARDWARE", "INTEGRATION", "DATA", "ACCESS", "CHANGE_REQUEST", "OTHER"]
Impact = Literal["LINE_STOP", "DEGRADED", "MINOR", "SERVICE_REQUEST"]
Severity = Literal["P1", "P2", "P3", "P4"]

INSTRUCTIONS = """MES (manufacturing execution system) of plant P1000: two lines (L1 machining, L2 assembly).
Support tickets follow a severity matrix: P1 line stop (respond 15 min, fix 2 h), P2 degraded production with a
workaround (30 min / 6 h), P3 minor, non-blocking (2 h / 24 h), P4 service request or change (8 h / scheduled).
Tiers: L1 first line (ServiceNow group "MES L1 Operations"), L2 app & DB engineering, L3 core development (a Jira
issue is opened automatically at L3). Prefer reading before writing; say why when you escalate or re-prioritise."""


def _mins(s: float | None) -> int | None:
    return None if s is None else int(s // 60)


def _ticket_brief(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": t["number"], "title": t["title"], "status": t["status"], "severity": t["severity"], "tier": t["tier"],
        "category": t["category"], "impact": t["impact"], "line": t.get("line_code"), "machine": t.get("equipment_code"),
        "order": t.get("erp_order_number"), "reported": t["reported_at"], "reported_by": t.get("reported_by_name"),
        "response_minutes_left": _mins(t["response"]["remaining_s"]), "response_breached": t["response"]["breached"],
        "resolution_minutes_left": _mins(t["resolution"]["remaining_s"]), "resolution_breached": t["resolution"]["breached"],
        "servicenow": t.get("sn_number"), "jira": t.get("jira_key"),
    }


def build_server(core: Core, settings: Settings) -> MCPServer:
    mcp = MCPServer("mes", instructions=INSTRUCTIONS)

    async def call(coro):  # type: ignore[no-untyped-def]
        try:
            return await coro
        except CoreError as e:
            raise ToolError(f"{e.code}: {e.message}") from e

    async def find_ticket(ref: str) -> dict[str, Any]:
        pid = await core.plant_id()
        tickets = await call(core.get("/tickets", plant_id=pid, limit=1000))
        t = next((x for x in tickets if x["number"] == ref or x["id"] == ref or x.get("sn_number") == ref
                  or x.get("jira_key") == ref), None)
        if t is None:
            raise ToolError(f"not_found: no ticket {ref}")
        return t

    # ------------------------------------------------------------------ tickets
    @mcp.tool()
    async def list_tickets(scope: Literal["open", "all"] = "open", severity: Severity | None = None,
                           tier: Literal["L1", "L2", "L3"] | None = None) -> list[dict[str, Any]]:
        """Support tickets of the plant, most severe first, with SLA minutes left (negative = overdue)."""
        pid = await core.plant_id()
        rows = await call(core.get("/tickets", plant_id=pid, open_only=scope == "open", severity=severity, tier=tier))
        return [_ticket_brief(t) for t in rows]

    @mcp.tool()
    async def get_ticket(ticket: str) -> dict[str, Any]:
        """One ticket (MES number, ServiceNow INC number or Jira key) with its full activity log."""
        t = await find_ticket(ticket)
        d = await call(core.get(f"/tickets/{t['id']}"))
        return {**_ticket_brief(d), "description": d["description"], "resolution_note": d.get("resolution_note"),
                "link": f"{settings.admin_ui_url}/tickets?ticket={d['id']}",
                "activity": [{"at": a["at"], "kind": a["kind"], "origin": a["origin"], "by": a["actor_name"], "text": a["text"]}
                             for a in d["activity"]]}

    @mcp.tool()
    async def sla_overview() -> dict[str, Any]:
        """Open tickets per severity and tier, breaches, and what is due in the next hour."""
        pid = await core.plant_id()
        rows = [_ticket_brief(t) for t in await call(core.get("/tickets", plant_id=pid, open_only=True))]
        by_sev: dict[str, int] = {}
        by_tier: dict[str, int] = {}
        for r in rows:
            by_sev[r["severity"]] = by_sev.get(r["severity"], 0) + 1
            by_tier[r["tier"]] = by_tier.get(r["tier"], 0) + 1
        breached = [r for r in rows if r["response_breached"] or r["resolution_breached"]]
        soon = [r for r in rows if not r["resolution_breached"] and r["resolution_minutes_left"] is not None
                and r["resolution_minutes_left"] <= 60]
        return {"open": len(rows), "by_severity": by_sev, "by_tier": by_tier, "breached": breached, "due_within_1h": soon}

    @mcp.tool()
    async def create_ticket(title: str, description: str, category: Category, impact: Impact,
                            severity: Severity | None = None, machine: str | None = None,
                            order: str | None = None) -> dict[str, Any]:
        """Raise a support ticket (it is mirrored to ServiceNow). Severity is suggested from the impact unless given.
        `machine` is an equipment code (e.g. MILL-02), `order` an order number."""
        pid = await core.plant_id()
        body: dict[str, Any] = {"plant_id": pid, "title": title, "description": description, "category": category,
                                "impact": impact, "severity": severity}
        if machine:
            eqs = await call(core.get("/maintenance/equipment", plant_id=pid))
            eq = next((e for e in eqs if e["code"].lower() == machine.lower()), None)
            if eq is None:
                raise ToolError(f"not_found: no machine {machine}")
            body["equipment_id"] = eq["id"]
        if order:
            items = (await call(core.get("/work-orders", plant_id=pid, search=order)))["items"]
            wo = next((w for w in items if w["erp_order_number"] == order), None)
            if wo is None:
                raise ToolError(f"not_found: no order {order}")
            body["work_order_id"] = wo["id"]
        return _ticket_brief(await call(core.post("/tickets", body)))

    @mcp.tool()
    async def comment_on_ticket(ticket: str, text: str) -> dict[str, Any]:
        """Add a comment (it becomes a work note in ServiceNow, and a Jira comment at L3)."""
        t = await find_ticket(ticket)
        return _ticket_brief(await call(core.post(f"/tickets/{t['id']}/comments", {"text": text})))

    @mcp.tool()
    async def escalate_ticket(ticket: str, reason: str) -> dict[str, Any]:
        """Move a ticket to the next support tier (L1 -> L2 -> L3; L3 opens a Jira issue)."""
        t = await find_ticket(ticket)
        return _ticket_brief(await call(core.post(f"/tickets/{t['id']}/escalate", {"reason": reason})))

    @mcp.tool()
    async def set_ticket_severity(ticket: str, severity: Severity, reason: str) -> dict[str, Any]:
        """Re-prioritise a ticket (recomputes its SLA targets)."""
        t = await find_ticket(ticket)
        return _ticket_brief(await call(core.post(f"/tickets/{t['id']}/severity", {"severity": severity, "reason": reason})))

    @mcp.tool()
    async def set_ticket_status(ticket: str, status: Literal["ACKNOWLEDGED", "IN_PROGRESS", "ON_HOLD", "RESOLVED"],
                                note: str | None = None) -> dict[str, Any]:
        """Acknowledge, work, hold or resolve a ticket (RESOLVED needs a note describing the fix)."""
        t = await find_ticket(ticket)
        return _ticket_brief(await call(core.post(f"/tickets/{t['id']}/status", {"status": status, "note": note})))

    # ------------------------------------------------------------------ shop floor
    @mcp.tool()
    async def line_status(line: str | None = None) -> list[dict[str, Any]]:
        """Live state of every machine (running / setup / paused / idle, availability, current order and operator)."""
        pid = await core.plant_id()
        live = await call(core.get("/execution/live", plant_id=pid))
        keys = ("equipment_code", "line_code", "equipment_status", "state", "erp_order_number", "op_no", "operator_name",
                "pause_reason", "since", "op_yield_qty", "op_planned_qty")
        return [{k: m.get(k) for k in keys} for m in live["machines"] if not line or m["line_code"] == line]

    @mcp.tool()
    async def equipment_health(days: int = 30) -> list[dict[str, Any]]:
        """Machines with availability, open downtime, open maintenance, failures, MTBF and MTTR over `days`."""
        pid = await core.plant_id()
        rows = await call(core.get("/maintenance/equipment", plant_id=pid, days=days))
        return [{"machine": e["code"], "line": e["line_code"], "status": e["status"],
                 "down_since": (e["open_downtime"] or {}).get("started_at"), "down_reason": (e["open_downtime"] or {}).get("reason_code"),
                 "open_orders": e["open_orders"], **{k: e["reliability"][k] for k in ("failures", "downtime_h", "mtbf_h", "mttr_h", "runtime_h")}}
                for e in rows]

    @mcp.tool()
    async def maintenance_orders(scope: Literal["open", "all"] = "open") -> list[dict[str, Any]]:
        """Maintenance orders (corrective / preventive) with machine, status, technician and SAP status."""
        pid = await core.plant_id()
        rows = await call(core.get("/maintenance/orders", plant_id=pid,
                                   status=["CREATED", "IN_PROGRESS"] if scope == "open" else None))
        return [{k: o[k] for k in ("number", "order_type", "status", "priority", "equipment_code", "description",
                                   "planned_start", "technician_name", "notification_number", "erp_system_status", "overdue")}
                for o in rows]

    @mcp.tool()
    async def get_work_order(order: str) -> dict[str, Any]:
        """A production order by number: status, quantities, operations and their progress."""
        pid = await core.plant_id()
        items = (await call(core.get("/work-orders", plant_id=pid, search=order)))["items"]
        wo = next((w for w in items if w["erp_order_number"] == order), None)
        if wo is None:
            raise ToolError(f"not_found: no order {order}")
        d = await call(core.get(f"/work-orders/{wo['id']}"))
        return {"order": d["erp_order_number"], "material": d.get("material_number"), "status": d["status"],
                "planned_qty": d["planned_qty"], "yield_qty": d.get("yield_qty"), "scrap_qty": d.get("scrap_qty"),
                "due": d.get("erp_basic_end"), "operations": [{k: o.get(k) for k in ("op_no", "description", "status", "yield_qty", "scrap_qty")}
                                                             for o in d["operations"]]}

    @mcp.tool()
    async def open_nonconformances() -> list[dict[str, Any]]:
        """Open quality nonconformances (defects) with severity, order and lot."""
        pid = await core.plant_id()
        rows = await call(core.get("/quality/nonconformances", plant_id=pid, status=["OPEN", "REWORK"]))
        return [{k: n.get(k) for k in ("number", "status", "severity", "defect_code", "description", "material_number",
                                       "erp_order_number", "lot_number", "reported_at")} for n in rows]

    @mcp.tool()
    async def trace(ref: str, direction: Literal["backward", "forward"] = "backward") -> list[str]:
        """Genealogy of a lot, serial number or order as indented lines (backward = where it came from)."""
        pid = await core.plant_id()
        hits = await call(core.get("/trace/search", plant_id=pid, q=ref))
        hit = next((h for h in hits if h["label"] == ref), hits[0] if hits else None)
        if hit is None:
            raise ToolError(f"not_found: nothing matches {ref}")
        if hit["type"] == "SERIAL":
            res = await call(core.get(f"/trace/serials/{hit['label']}", plant_id=pid, direction=direction))
        elif hit["type"] == "LOT":
            res = await call(core.get(f"/trace/lots/{hit['id']}", direction=direction))
        else:
            res = await call(core.get(f"/trace/orders/{hit['id']}", direction=direction))
        lines: list[str] = []

        def walk(n: dict[str, Any], depth: int) -> None:
            qty = f" qty {n['quantity']}" if n.get("quantity") else ""
            extra = ""
            if n["type"] == "ORDER" and n.get("order"):
                ops = "; ".join(f"{o['op_no']} " + ", ".join(f"{s['equipment_code']}/{s['operator_name']}" for s in o["steps"])
                                for o in n["order"]["operations"])
                extra = f" [{n['order']['status']}] {ops}"
            if n.get("inspections"):
                extra += " inspections: " + ", ".join(f"{i['number']} {i['status']}" for i in n["inspections"])
            if n.get("nonconformances"):
                extra += " NCs: " + ", ".join(f"{x['number']} {x['status']}" for x in n["nonconformances"])
            lines.append(f"{'  ' * depth}{n['type']} {n['label']} {n['material_number']}{qty}{extra}")
            for ch in n["children"]:
                walk(ch, depth + 1)

        walk(res["root"], 0)
        return lines

    return mcp


class BearerKey(BaseHTTPMiddleware):
    def __init__(self, app: Any, key: str) -> None:
        super().__init__(app)
        self._key = key

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path.startswith("/mcp") and request.headers.get("authorization") != f"Bearer {self._key}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def create_app(core: Core | None = None, settings: Settings | None = None) -> Starlette:
    settings = settings or get_settings()
    core = core or Core(settings)
    mcp = build_server(core, settings)
    mcp_app = mcp.streamable_http_app(
        stateless_http=True, json_response=True, host="0.0.0.0",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "UP", "time": datetime.now(UTC).isoformat()})

    @asynccontextmanager
    async def lifespan(_app: Starlette):  # type: ignore[no-untyped-def]
        async with mcp.session_manager.run():
            yield

    app = Starlette(routes=[Route("/health", health), Mount("/", mcp_app)], lifespan=lifespan)
    app.add_middleware(BearerKey, key=settings.mcp_api_key)
    app.state.mcp = mcp
    return app
