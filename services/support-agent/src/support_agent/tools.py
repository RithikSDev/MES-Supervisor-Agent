"""The agent's tools. Read tools run freely; every write goes through the action policy (off / suggest / auto).

Safety properties enforced here, whatever the model asks for:
  - only the tools of the current context exist (a ticket run can't create tickets; a chat can't retry SAP postings);
  - arguments are validated against the tool schema before anything runs;
  - write tools act on the ticket being worked, never on one named in a message;
  - with policy `suggest` a write becomes a proposal for a human; `off` refuses it;
  - every MES call runs with MES permissions (service account, approver or operator token), so MES still decides.
"""

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from mes_common.ids import new_id
from support_agent import engineer as eng
from support_agent.actions import ACTIONS, AUTO, OFF, SUGGEST
from support_agent.clients import Api, ApiError
from support_agent.db import proposal, run
from support_agent.knowledge import Knowledge
from support_agent.llm import short
from support_agent.store import Recorder

Emit = Callable[[dict[str, Any]], Awaitable[None]]
log = logging.getLogger(__name__)


@dataclass
class Context:
    api: Api
    knowledge: Knowledge
    recorder: Recorder
    plant_id: str
    mode: str  # "ticket" | "l2" | "chat" | "engineer"
    policies: dict[str, str]
    ticket: dict[str, Any] | None = None  # ticket runs: id, number, plant_id, status, tier
    chat: dict[str, Any] | None = None  # chat: session row (context, user, terminal)
    operator_token: str | None = None
    emit: Emit | None = None
    outcomes: list[str] = field(default_factory=list)
    sf: Any = None  # engineer chat: the agent's own tables (AI runs and proposals of a ticket)
    mcp: Any = None  # Toolset: tools of the MCP servers allowed for this agent (live data), or None
    hub: Any = None  # McpHub that calls them
    docs: Any = None  # KnowledgeStore: machine guides, manuals, fault codes, the MES user guide
    engineer_name: str | None = None  # engineer chat: who is asking ("my tickets")


Handler = Callable[[Context, dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    properties: dict[str, Any]
    required: tuple[str, ...]
    handler: Handler
    contexts: frozenset[str]
    status: Callable[[dict[str, Any]], str]  # what the operator sees while it runs

    def spec(self) -> dict[str, Any]:
        # tool inputs are small: no eager input streaming, so the API validates the arguments before we see them
        return {"name": self.name, "description": self.description,
                "input_schema": {"type": "object", "properties": self.properties, "required": list(self.required),
                                 "additionalProperties": False}}


def _validate(tool: Tool, args: Any) -> str | None:
    if not isinstance(args, dict):
        return "arguments must be an object"
    for key in tool.required:
        if key not in args or args[key] in (None, ""):
            return f"missing required argument '{key}'"
    for key, value in args.items():
        prop = tool.properties.get(key)
        if prop is None:
            return f"unknown argument '{key}'"
        kind = prop.get("type")
        if kind == "string" and not isinstance(value, str):
            return f"'{key}' must be a string"
        if kind == "integer" and not (isinstance(value, int) and not isinstance(value, bool)):
            return f"'{key}' must be an integer"
        if kind == "array" and not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
            return f"'{key}' must be a list of strings"
        if "enum" in prop and value not in prop["enum"]:
            return f"'{key}' must be one of {prop['enum']}"
        if kind == "array" and (allowed := prop.get("items", {}).get("enum")) and any(v not in allowed for v in value):
            return f"'{key}' items must be from {allowed}"
        if isinstance(value, str) and len(value) > prop.get("maxLength", 4000):
            return f"'{key}' is too long"
        if isinstance(value, str) and ("<parameter name=" in value or "</parameter>" in value
                                       or f"</{key}>" in value):
            return f"'{key}' contains tool-call markup; send each argument as its own field"
        if isinstance(value, list) and len(value) > prop.get("maxItems", 20):
            return f"'{key}' has too many items"
    return None


# ------------------------------------------------------------------------------------------------ read tools


def _brief(t: dict[str, Any]) -> dict[str, Any]:
    keys = ("number", "title", "status", "severity", "tier", "category", "impact", "line_code", "equipment_code",
            "erp_order_number", "terminal_code", "reported_at", "reported_by_name", "resolution_note", "sn_number")
    return {k: t.get(k) for k in keys if t.get(k) is not None}


async def _search_tickets(ctx: Context, a: dict[str, Any]) -> Any:
    rows = await ctx.api.get("/tickets/search", plant_id=ctx.plant_id, q=a["query"], scope=a.get("scope", "all"), limit=8)
    own = ctx.ticket["number"] if ctx.ticket else None
    return [_brief(t) for t in rows if t["number"] != own] or "no similar tickets"


async def _get_ticket(ctx: Context, a: dict[str, Any]) -> Any:
    number = a["number"].strip().upper()
    hits = [t for t in await ctx.api.get("/tickets/search", plant_id=ctx.plant_id, q=number, limit=1)
            if t["number"] == number]
    if not hits:
        return f"ticket {number} not found"
    d = await ctx.api.get(f"/tickets/{hits[0]['id']}")
    return {**_brief(d), "description": d["description"],
            "activity": [{"at": x["at"], "origin": x["origin"], "by": x["actor_name"], "kind": x["kind"], "text": x["text"]}
                         for x in d["activity"][-15:]]}


async def _search_knowledge(ctx: Context, a: dict[str, Any]) -> Any:
    if ctx.docs is None:
        return "no knowledge store"
    hits = await ctx.docs.search(a["query"], kind=a.get("kind"), equipment_type=a.get("equipment_type"),
                                 limit=max(1, min(int(a.get("limit", 6)), 10)))
    return hits or "nothing found; try other words (symptom, alarm code, part name) or another machine type"


async def _read_knowledge(ctx: Context, a: dict[str, Any]) -> Any:
    from uuid import UUID
    try:
        return await ctx.docs.read(UUID(a["doc_id"]), a.get("page"))
    except ValueError as e:
        return str(e)


async def _read_runbook(ctx: Context, a: dict[str, Any]) -> Any:
    rb = ctx.knowledge.get(a["runbook_id"])
    return f"# {rb.title}\n\n{rb.body}" if rb else f"no runbook '{a['runbook_id']}'"


def _match(code: str | None, value: str | None) -> bool:
    return not code or (value or "").upper() == code.strip().upper()


async def _machine_status(ctx: Context, a: dict[str, Any]) -> Any:
    code = a.get("machine")
    eqs = [e for e in await ctx.api.get("/maintenance/equipment", plant_id=ctx.plant_id) if _match(code, e["code"])]
    if not eqs:
        return f"machine {code} not found"
    live = {m["equipment_code"]: m for m in (await ctx.api.get("/execution/live", plant_id=ctx.plant_id))["machines"]}
    out = []
    for e in eqs:
        m = live.get(e["code"], {})
        item = {"machine": e["code"], "name": e["name"], "line": e["line_code"], "status": e["status"],
                "run_state": m.get("state"), "order": m.get("erp_order_number"), "operation": m.get("op_no"),
                "operator": m.get("operator_name"), "pause_reason": m.get("pause_reason"), "since": m.get("since"),
                "open_downtime": e["open_downtime"], "open_notifications": e["open_notifications"],
                "open_maintenance_orders": e["open_orders"], "reliability": e["reliability"]}
        if code and e["open_orders"]:
            orders = await ctx.api.get("/maintenance/orders", plant_id=ctx.plant_id, equipment_id=e["id"])
            item["maintenance_orders"] = [{"number": o.get("number"), "status": o.get("status"),
                                           "type": o.get("order_type"), "description": o.get("description")}
                                          for o in orders if o.get("status") not in ("COMPLETED", "CLOSED", "CANCELLED")]
        out.append({k: v for k, v in item.items() if v not in (None, [], {})})
    return out


async def _line_status(ctx: Context, a: dict[str, Any]) -> Any:
    line = a.get("line")
    machines = [m for m in (await ctx.api.get("/execution/live", plant_id=ctx.plant_id))["machines"]
                if _match(line, m["line_code"])]
    return [{k: v for k, v in {"machine": m["equipment_code"], "line": m["line_code"], "status": m["equipment_status"],
                               "state": m["state"], "order": m["erp_order_number"], "operation": m["op_no"],
                               "operator": m["operator_name"], "pause_reason": m["pause_reason"], "since": m["since"]}.items()
             if v is not None} for m in machines] or f"no machines on line {line}"


async def _work_order(ctx: Context, a: dict[str, Any]) -> Any:
    number = a["order"].strip()
    items = (await ctx.api.get("/work-orders", plant_id=ctx.plant_id, search=number))["items"]
    wo = next((w for w in items if w["erp_order_number"] == number), items[0] if items else None)
    if wo is None:
        return f"order {number} not found"
    d = await ctx.api.get(f"/work-orders/{wo['id']}")
    keep = ("erp_order_number", "material_number", "material_description", "planned_qty", "yield_qty", "scrap_qty",
            "uom", "status", "erp_status", "hold_prev_status", "sched_start", "sched_end", "priority")
    return {**{k: d.get(k) for k in keep if d.get(k) is not None},
            "operations": [{k: o.get(k) for k in ("op_no", "description", "work_center_code", "status", "yield_qty",
                                                  "scrap_qty")} for o in d.get("operations", [])],
            "recent_history": (d.get("history") or [])[-8:]}


async def _terminal_status(ctx: Context, a: dict[str, Any]) -> Any:
    code = a.get("terminal") or (ctx.chat or {}).get("terminal_code")
    rows = [t for t in await ctx.api.get("/terminals", plant_id=ctx.plant_id) if _match(code, t["code"])]
    return [{k: t.get(k) for k in ("code", "name", "line_code", "active", "enrolled", "session_operator",
                                    "session_started_at", "pin_locked_until")} for t in rows] or f"terminal {code} not found"


async def _integration_errors(ctx: Context, a: dict[str, Any]) -> Any:
    system, order = a.get("system", "both"), (a.get("order") or "").strip()
    out: dict[str, Any] = {}
    statuses = ["RETRY_WAIT", "FAILED", "DEAD"]
    if system in ("sap", "both"):
        rows = await ctx.api.get("/outbound", service="erp", status=statuses, limit=100)
        if order:
            rows = [r for r in rows if order in r["erp_key"] or order in json.dumps(r.get("payload"), default=str)]
        out["sap"] = [{k: r.get(k) for k in ("id", "message_type", "erp_key", "status", "attempts", "max_attempts",
                                              "last_error", "created_at", "next_attempt_at")} for r in rows[:20]]
    if system in ("itsm", "both"):
        rows = await ctx.api.get("/messages", service="itsm", status=statuses, limit=50)
        out["servicenow_jira"] = [{k: r.get(k) for k in ("id", "ticket_number", "status", "attempts", "last_error",
                                                          "created_at")} for r in rows[:20]]
    return out


async def _recent_changes(ctx: Context, a: dict[str, Any]) -> Any:
    since = datetime.now(UTC) - timedelta(hours=min(int(a.get("hours", 24)), 168))
    page = await ctx.api.get("/audit", plant_id=ctx.plant_id, since=since.isoformat(), table_name=a.get("table"),
                             limit=25)
    return [{"at": r["occurred_at"], "table": f"{r['schema_name']}.{r['table_name']}", "action": r["action"],
             "by": r["actor_name"], "reason": r["reason"], "changed": r["changed_fields"]} for r in page["items"]]


async def _quality_status(ctx: Context, a: dict[str, Any]) -> Any:
    rows = await ctx.api.get("/quality/nonconformances", plant_id=ctx.plant_id, status=["OPEN", "REWORK"])
    if a.get("order"):
        rows = [r for r in rows if r.get("erp_order_number") == a["order"].strip()]
    return [{k: r.get(k) for k in ("number", "status", "severity", "defect_code", "description", "erp_order_number",
                                    "op_no", "material_number", "created_at")} for r in rows[:15]] or "no open nonconformances"


async def _shortages(ctx: Context, a: dict[str, Any]) -> Any:
    rows = await ctx.api.get("/inventory/shortages", plant_id=ctx.plant_id)
    if a.get("material"):
        rows = [r for r in rows if r["material_number"].upper() == a["material"].strip().upper()]
    return [{"material": r["material_number"], "description": r["material_description"], "uom": r["uom"],
             "demand": r["demand"], "available": r["available"], "shortage": r["shortage"], "lines": r["lines"],
             "orders": r["orders"][:8]} for r in rows[:10]] or "no shortages"


# ------------------------------------------------------------------------------------------------ write tools


async def _act(ctx: Context, action: str, args: dict[str, Any]) -> Any:
    """Ticket actions: policy decides whether it runs now, becomes a proposal, or is refused."""
    t = ctx.ticket
    assert t is not None
    spec = ACTIONS[action]
    mode = ctx.policies.get(action, spec.default)
    if mode == OFF:
        return {"refused": f"'{spec.label}' is switched off by the support policy. Leave it to a human (escalate)."}
    rationale = args.pop("rationale", None)
    current = await ctx.api.get(f"/tickets/{t['id']}")  # people may have closed or cancelled it meanwhile
    if current["status"] not in ("NEW", "ACKNOWLEDGED", "IN_PROGRESS", "ON_HOLD"):
        ctx.outcomes.append("TICKET_CLOSED")
        return {"refused": f"the ticket is {current['status'].lower()} now (changed by a person meanwhile): stop working "
                           "it and don't write anything more"}
    if mode == SUGGEST:
        pid = await ctx.recorder.propose(ticket=t, action=action, args=args, summary=spec.describe(args),
                                         rationale=rationale)
        ctx.outcomes.append(f"PROPOSED:{action}")
        return {"proposed": True, "proposal_id": str(pid),
                "note": "Sent to a supervisor for approval; it has NOT been done yet. Do not repeat it."}
    started = time.monotonic()
    result = await spec.run(ctx.api, t, args, None, None)
    await ctx.recorder.step("ACTION", action, args, result, started=started)
    ctx.outcomes.append(action)
    return {"done": True, **result}


def _write(action: str) -> Handler:
    async def run(ctx: Context, a: dict[str, Any]) -> Any:
        return await _act(ctx, action, dict(a))
    return run


# ------------------------------------------------------------------------------------------------ chat tools


async def _quick_replies(ctx: Context, a: dict[str, Any]) -> Any:
    options = [o.strip()[:40] for o in a["options"] if o.strip()][:4]
    if ctx.emit:
        await ctx.emit({"type": "choices", "options": options})
    ctx.outcomes.append("CHOICES:" + "|".join(options))
    return "shown to the operator; wait for their answer"


async def _chat_ticket_ids(ctx: Context) -> dict[str, Any]:
    """Machine and order the operator's terminal showed, as MES ids for the ticket."""
    context = (ctx.chat or {}).get("context") or {}
    ids: dict[str, Any] = {}
    if context.get("equipment_code"):
        eq = next((e for e in await ctx.api.get("/maintenance/equipment", plant_id=ctx.plant_id)
                   if e["code"] == context["equipment_code"]), None)
        if eq:
            ids["equipment_id"] = eq["id"]
    if context.get("erp_order_number"):
        items = (await ctx.api.get("/work-orders", plant_id=ctx.plant_id, search=context["erp_order_number"]))["items"]
        wo = next((w for w in items if w["erp_order_number"] == context["erp_order_number"]), None)
        if wo:
            ids["work_order_id"] = wo["id"]
    return ids


async def _raise(ctx: Context, a: dict[str, Any], action: str) -> dict[str, Any] | str:
    chat = ctx.chat or {}
    if ctx.policies.get(action, AUTO) == OFF:
        return "not allowed by the support policy; tell the operator to use the Get help form or call their supervisor"
    if chat.get("ticket_number"):
        return f"a ticket was already raised in this conversation: {chat['ticket_number']}"
    body = {"plant_id": ctx.plant_id, "title": a["title"], "description": a["description"], "category": a["category"],
            "impact": a["impact"], **await _chat_ticket_ids(ctx)}
    # created as the operator (their terminal session), so MES records who reported it and from which terminal
    t = await ctx.api.post("/tickets", body, token=ctx.operator_token)
    chat["ticket_id"], chat["ticket_number"] = t["id"], t["number"]
    return t


async def _create_ticket(ctx: Context, a: dict[str, Any]) -> Any:
    t = await _raise(ctx, a, "create_ticket")
    if isinstance(t, str):
        return t
    ctx.outcomes.append("TICKET_CREATED")
    if ctx.emit:
        await ctx.emit({"type": "ticket", "number": t["number"], "status": t["status"], "severity": t["severity"]})
    return {"ticket": t["number"], "severity": t["severity"], "status": t["status"],
            "note": "The support agent will now diagnose it; tell the operator the number."}


async def _mark_fixed(ctx: Context, a: dict[str, Any]) -> Any:
    chat = ctx.chat or {}
    if chat.get("ticket_id"):  # fixed after the ticket was raised: resolve that ticket
        if ctx.policies.get("resolve_confirmed", AUTO) == OFF:
            return "not allowed by the support policy; support will close the ticket"
        t = {"id": chat["ticket_id"], "number": chat["ticket_number"], "severity": None}
    else:
        t = await _raise(ctx, {**a, "impact": a.get("impact", "MINOR")}, "resolve_confirmed")
        if isinstance(t, str):
            return t
    await ctx.api.post(f"/tickets/{t['id']}/status",
                       {"status": "RESOLVED", "note": f"Fixed in the terminal chat, confirmed by the operator: {a['resolution']}"})
    ctx.outcomes.append("FIXED_IN_CHAT")
    if ctx.emit:
        await ctx.emit({"type": "ticket", "number": t["number"], "status": "RESOLVED", "severity": t["severity"]})
    return {"ticket": t["number"], "status": "RESOLVED"}


async def _add_to_ticket(ctx: Context, a: dict[str, Any]) -> Any:
    chat = ctx.chat or {}
    if not chat.get("ticket_id"):
        return "no ticket raised in this conversation yet"
    # as the operator, so it shows as their own information on the ticket (and in ServiceNow)
    await ctx.api.post(f"/tickets/{chat['ticket_id']}/comments", {"text": a["text"]}, token=ctx.operator_token)
    return {"added_to": chat["ticket_number"]}


# ------------------------------------------------------------------------------------------------ L2 tools

SERVICES = ["core-api", "core-worker", "erp-adapter-api", "erp-adapter-worker", "itsm-adapter-api", "itsm-adapter-worker",
            "support-agent-api", "support-agent-worker", "mes-mcp", "mock-sap", "keycloak", "postgres", "redpanda"]
LEVELS = {"error": "ERROR|CRITICAL", "warning": "WARNING|ERROR|CRITICAL"}


def _logql_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _log_rows(result: Any, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for stream in result:
        svc = stream["stream"].get("service")
        for ts, line in stream["values"]:
            try:
                d = json.loads(line)
                row = {"at": d.get("ts"), "service": svc, "level": d.get("level"), "msg": d.get("msg"),
                       "logger": d.get("logger"), "request_id": d.get("request_id"),
                       "exc": (d.get("exc") or "")[-1500:] or None}
            except (ValueError, AttributeError):
                row = {"at": datetime.fromtimestamp(int(ts) / 1e9, UTC).isoformat(), "service": svc, "msg": line[:600]}
            rows.append({k: v for k, v in row.items() if v})
    rows.sort(key=lambda r: r.get("at") or "", reverse=True)
    return rows[:limit]


async def _search_logs(ctx: Context, a: dict[str, Any]) -> Any:
    minutes = max(1, min(int(a.get("minutes", 60)), 7 * 24 * 60))
    labels = [f'service="{a["service"]}"' if a.get("service") else 'service=~".+"']
    if a.get("level"):
        labels.append(f'level=~"{LEVELS[a["level"]]}"')
    query = "{" + ", ".join(labels) + "}"
    if a.get("text"):
        query += f' |= "{_logql_text(a["text"])}"'
    end = datetime.now(UTC)
    limit = max(1, min(int(a.get("limit", 40)), 100))
    result = await ctx.api.loki(query, start_ns=int((end - timedelta(minutes=minutes)).timestamp() * 1e9),
                                end_ns=int(end.timestamp() * 1e9), limit=limit)
    return _log_rows(result, limit) or f"no log lines for {query} in the last {minutes} min"


async def _trace_request(ctx: Context, a: dict[str, Any]) -> Any:
    rid = a["request_id"].strip()
    end = datetime.now(UTC)
    result = await ctx.api.loki(f'{{service=~".+"}} |= "{_logql_text(rid)}"',
                                start_ns=int((end - timedelta(hours=int(a.get("hours", 24)))).timestamp() * 1e9),
                                end_ns=int(end.timestamp() * 1e9), limit=100, direction="forward")
    rows = _log_rows(result, 100)
    rows.reverse()  # oldest first: the request as it happened
    return rows or f"no log lines mention {rid}"


async def _sap_message(ctx: Context, a: dict[str, Any]) -> Any:
    d = await ctx.api.call("erp", "GET", f"/outbound/{a['message_id']}")
    keep = ("id", "message_type", "erp_key", "status", "attempts", "max_attempts", "last_error", "erp_result",
            "reconciled", "payload", "created_at", "completed_at")
    return {**{k: d.get(k) for k in keep}, "attempts_log": d.get("attempts_log", [])[-10:]}


async def _compare_order(ctx: Context, a: dict[str, Any]) -> Any:
    number = a["order"].strip()
    items = (await ctx.api.get("/work-orders", plant_id=ctx.plant_id, search=number))["items"]
    wo = next((w for w in items if w["erp_order_number"] == number), None)
    mes = None
    if wo:
        d = await ctx.api.get(f"/work-orders/{wo['id']}")
        mes = {k: d.get(k) for k in ("status", "erp_status", "hold_prev_status", "planned_qty", "yield_qty", "scrap_qty",
                                     "uom", "erp_basic_start", "erp_basic_end", "erp_last_changed_at", "updated_at")}
        mes["operations"] = [{k: o.get(k) for k in ("op_no", "status", "planned_qty", "yield_qty")}
                             for o in d.get("operations", [])]
    inbound = await ctx.api.call("erp", "GET", "/inbound", params={"erp_key": number, "limit": 3})
    sap = [{"received_at": m["received_at"], "sap_changed_at": m["erp_changed_at"], "snapshot": m["payload"]}
           for m in inbound]
    outbound = [r for r in await ctx.api.call("erp", "GET", "/outbound", params={"limit": 200})
                if number in r["erp_key"] or number in json.dumps(r.get("payload"), default=str)][:10]
    return {"order": number, "mes": mes or "not in MES", "sap_snapshots_received": sap or "none received",
            "postings_to_sap": [{k: r.get(k) for k in ("id", "message_type", "status", "attempts", "last_error",
                                                        "created_at")} for r in outbound]}


async def _event_failures(ctx: Context, a: dict[str, Any]) -> Any:
    rows = await ctx.api.get("/ops/event-failures", limit=int(a.get("limit", 20)))
    return [{k: r.get(k) for k in ("created_at", "topic", "event_type", "key", "error")} for r in rows] or "none"


async def _service_health(ctx: Context, a: dict[str, Any]) -> Any:
    out: dict[str, Any] = {}
    for svc in ("core", "erp", "itsm"):
        ok, detail = await ctx.api.health(svc)  # type: ignore[arg-type]
        out[svc] = {"ready": ok, "detail": detail}
    try:
        out["sap_queue"] = await ctx.api.call("erp", "GET", "/outbound-stats")
        out["sap_polling"] = await ctx.api.call("erp", "GET", "/poll-state")
    except ApiError as e:
        out["sap_queue"] = f"unavailable: {e}"
    try:
        st = await ctx.api.call("itsm", "GET", "/status")
        out["servicenow_jira"] = {"connections": [{k: c.get(k) for k in ("system", "reachable", "error")}
                                                  for c in st.get("connections", [])], "counts": st.get("counts")}
    except ApiError as e:
        out["servicenow_jira"] = f"unavailable: {e}"
    return out


async def _record_rca(ctx: Context, a: dict[str, Any]) -> Any:
    rca = {k: a.get(k) for k in ("symptom", "root_cause", "category", "confidence", "evidence", "fix", "prevention")}
    await ctx.recorder.set_rca(rca)
    ctx.outcomes.append("RCA")
    lines = [f"Root-cause analysis (AI L2, confidence {rca['confidence']}, category {rca['category']})",
             f"Symptom: {rca['symptom']}", f"Root cause: {rca['root_cause']}", "Evidence:",
             *[f"- {e}" for e in rca["evidence"] or []], f"Fix: {rca['fix']}"]
    if rca.get("prevention"):
        lines.append(f"Prevention: {rca['prevention']}")
    return await _act(ctx, "add_note", {"text": "\n".join(lines)})


async def _escalate_l3(ctx: Context, a: dict[str, Any]) -> Any:
    text = "\n".join([
        f"L3 handover (AI L2): {a['summary']}",
        f"Suspected component: {a['suspected_component']}",
        "Steps to reproduce:", a["steps_to_reproduce"],
        f"Expected: {a['expected']}", f"Actual: {a['actual']}",
        "Evidence:", a["evidence"],
    ])
    return await _act(ctx, "escalate", {"summary": text[:4000], "rationale": a.get("rationale")})


# ------------------------------------------------------------------------------------------------ engineer chat


async def _ticket_by_number(ctx: Context, number: str) -> dict[str, Any] | None:
    number = number.strip().upper()
    hits = [t for t in await ctx.api.get("/tickets/search", plant_id=ctx.plant_id, q=number, limit=3)
            if t["number"] == number]
    return hits[0] if hits else None


async def _list_tickets(ctx: Context, a: dict[str, Any]) -> Any:
    f = eng.ticket_filter(a, ctx.engineer_name)
    rows = await eng.tickets_for(ctx.api, ctx.plant_id, f)  # type: ignore[arg-type]
    limit = max(1, min(int(a.get("limit", 25)), 50))
    return {"filter": f, "count": len(rows), "tickets": [eng.ticket_brief(t) for t in rows[:limit]],
            **({"note": f"only the first {limit} shown"} if len(rows) > limit else {})}


async def _latest_run(ctx: Context, ticket_id: str) -> Any:
    async with ctx.sf() as s:
        return (await s.execute(select(run).where(run.c.ticket_id == ticket_id, run.c.kind == "TICKET")
                                .order_by(run.c.created_at.desc()).limit(1))).first()


async def _ai_activity(ctx: Context, a: dict[str, Any]) -> Any:
    t = await _ticket_by_number(ctx, a["number"])
    if t is None:
        return f"ticket {a['number']} not found"
    async with ctx.sf() as s:
        runs = (await s.execute(select(run).where(run.c.ticket_id == t["id"]).order_by(run.c.created_at))).all()
        props = (await s.execute(select(proposal).where(proposal.c.ticket_id == t["id"])
                                 .order_by(proposal.c.created_at))).all()
    return {"ticket": t["number"],
            "runs": [{k: v for k, v in {"at": r.created_at, "tier": r.tier, "trigger": r.trigger, "status": r.status,
                                        "outcome": r.outcome, "summary": (r.summary or "")[:1500] or None,
                                        "root_cause": r.rca, "cost_usd": float(r.cost_usd), "error": r.error}.items()
                      if v not in (None, "")} for r in runs] or "the AI has not worked this ticket",
            "proposals": [{"id": str(p.id), "action": p.action, "summary": p.summary, "status": p.status,
                           "decided_by": p.decided_by, "rationale": p.rationale} for p in props]}


async def _pending_approvals(ctx: Context, a: dict[str, Any]) -> Any:
    async with ctx.sf() as s:
        rows = (await s.execute(select(proposal).where(proposal.c.status == "PENDING")
                                .order_by(proposal.c.created_at))).all()
    return [{"proposal_id": str(p.id), "ticket": p.ticket_number, "action": p.action, "summary": p.summary,
             "rationale": p.rationale, "waiting_since": p.created_at} for p in rows] or "nothing waiting for approval"


async def _sla_overview(ctx: Context, a: dict[str, Any]) -> Any:
    d = await ctx.api.get("/tickets/analytics", plant_id=ctx.plant_id, days=max(1, min(int(a.get("days", 7)), 90)))
    return {k: d.get(k) for k in ("days", "totals", "by_severity", "resolved_by", "by_category", "by_tier", "top_equipment")}


async def _emit_card(ctx: Context, card: dict[str, Any]) -> None:
    if ctx.emit:
        await ctx.emit({"type": "card", "card": card})
    ctx.outcomes.append(f"CARD:{card['kind']}")


async def _show_card(ctx: Context, a: dict[str, Any]) -> Any:
    kind, title = a["kind"], (a.get("title") or "").strip()[:120] or None
    card: dict[str, Any] = {"kind": kind, "title": title}
    summary: Any = "shown"
    if kind == "tickets":
        f = eng.ticket_filter(a, ctx.engineer_name)
        rows = await eng.tickets_for(ctx.api, ctx.plant_id, f)  # type: ignore[arg-type]
        card["filter"] = f
        summary = {"shown": len(rows),
                   "by_severity": {s: n for s in eng.SEVERITIES if (n := sum(1 for t in rows if t["severity"] == s))},
                   "sla_breached": sum(1 for t in rows if eng.sla_state(t) == "breached")}
    elif kind in ("ticket", "ai_run"):
        if not a.get("number"):
            return "number is required for this card"
        t = await _ticket_by_number(ctx, a["number"])
        if t is None:
            return f"ticket {a['number']} not found"
        card |= {"ticket_id": t["id"], "number": t["number"]}
        if kind == "ai_run":
            r = await _latest_run(ctx, t["id"])
            if r is None:
                return f"the AI has not worked {t['number']}"
            card["run_id"] = str(r.id)
        summary = {"shown": t["number"], "status": t["status"], "severity": t["severity"], "tier": t["tier"]}
    elif kind == "sla":
        card["days"] = max(1, min(int(a.get("days", 7)), 90))
    elif kind == "sla_report":
        card["days"] = max(1, min(int(a.get("days", 7)), 90))
        r = await ctx.api.get("/tickets/sla-report", plant_id=ctx.plant_id, days=card["days"])
        summary = r["summary"]
    elif kind == "forecast":
        card["horizon_days"] = max(1, min(int(a.get("horizon_days", 7)), MAX_HORIZON))
        f_ = await ctx.api.get("/tickets/forecast", plant_id=ctx.plant_id, horizon_days=card["horizon_days"])
        summary = {"expected": f_["totals"], "expected_p1_p2": f_["expected_p1_p2"], "trend_per_week": f_["trend_per_week"],
                   "peak_day": f_["peak_day"], "backlog": f_["backlog"], "backtest": f_["backtest"]}
    elif kind == "team":
        summary = [{"name": m["display_name"], "tier": m["tier"], "open_tickets": m["open_tickets"]}
                   for m in await ctx.api.get("/tickets/support-team", plant_id=ctx.plant_id)]
    elif kind == "machines":
        card["machine"] = (a.get("machine") or "").strip().upper() or None
    elif kind == "integration":
        card["system"] = a.get("system") or "both"
    await _emit_card(ctx, card)
    return {"card": kind, "result": summary,
            "note": "The card is on the engineer's screen with live data. Do not repeat its rows in text; add only "
                    "what matters (a sentence or two)."}


async def _resolve_links(ctx: Context, a: dict[str, Any], args: dict[str, Any]) -> str | None:
    """Machine code / order number -> MES ids (kept next to the codes, which the card shows). None or the problem."""
    if "machine" in a:
        code = (a.get("machine") or "").strip().upper()
        if code in ("", "NONE", "-"):
            args["machine"], args["equipment_id"] = "none", None
        else:
            eq = next((e for e in await ctx.api.get("/maintenance/equipment", plant_id=ctx.plant_id)
                       if e["code"].upper() == code), None)
            if eq is None:
                return f"no machine {code}"
            args["machine"], args["equipment_id"] = eq["code"], eq["id"]
    if "order" in a:
        number = (a.get("order") or "").strip()
        if number.lower() in ("", "none", "-"):
            args["order"], args["work_order_id"] = "none", None
        else:
            items = (await ctx.api.get("/work-orders", plant_id=ctx.plant_id, search=number))["items"]
            wo = next((w for w in items if w["erp_order_number"] == number), None)
            if wo is None:
                return f"no order {number}"
            args["order"], args["work_order_id"] = number, wo["id"]
    return None


async def _propose_new_ticket(ctx: Context, a: dict[str, Any]) -> Any:
    """A new ticket, raised in the engineer's name once they confirm; open tickets about the same thing are listed."""
    args: dict[str, Any] = {k: a[k] for k in ("title", "description", "category", "impact", "severity", "assignee")
                            if a.get(k)}
    if problem := await _resolve_links(ctx, {k: a[k] for k in ("machine", "order") if a.get(k)}, args):
        return problem
    if problem := await _resolve_assignee(ctx, args):
        return problem
    args["plant_id"] = ctx.plant_id
    similar = await ctx.api.get("/tickets/search", plant_id=ctx.plant_id, q=a["title"], scope="open", limit=5)
    if args.get("machine") and args["machine"] != "none":
        open_ = await ctx.api.get("/tickets", plant_id=ctx.plant_id, open_only=True, equipment_id=args["equipment_id"])
        similar += [t for t in open_ if t["id"] not in {s["id"] for s in similar}]
    duplicates = [{"number": t["number"], "title": t["title"], "status": t["status"], "ticket_id": t["id"]}
                  for t in similar[:5]]
    card = {"kind": "confirm", "action_id": str(new_id()), "action": "create_ticket", "label": "Create ticket",
            "ticket_id": None, "number": "new ticket", "args": args, "summary": f"Create ticket: {args['title']}",
            "reason": (a.get("reason") or "").strip()[:1000] or None, "status": "PENDING",
            "duplicates": duplicates}
    await _emit_card(ctx, card)
    return {"shown_for_confirmation": True, "done": False, "possible_duplicates": duplicates,
            "note": "NOT created yet: the engineer confirms or cancels it on the card. If a listed open ticket is the "
                    "same problem, say so (adding to it may be better than a new ticket)."}


async def _forecast(ctx: Context, a: dict[str, Any]) -> Any:
    f = await ctx.api.get("/tickets/forecast", plant_id=ctx.plant_id,
                          horizon_days=max(1, min(int(a.get("horizon_days", 7)), MAX_HORIZON)))
    return {k: f[k] for k in ("horizon_days", "history_weeks", "history_tickets", "method", "totals", "trend_per_week",
                              "peak_day", "by_severity", "expected_p1_p2", "by_category", "backlog", "backtest",
                              "enough_history", "weekly_history")} | (
        {"per_day": f["forecast"]} if f["horizon_days"] <= 21 else {"note": "per week in totals.per_week (days too many)"})


async def _sla_report(ctx: Context, a: dict[str, Any]) -> Any:
    r = await ctx.api.get("/tickets/sla-report", plant_id=ctx.plant_id, days=max(1, min(int(a.get("days", 7)), 90)))
    keep = ("number", "title", "severity", "tier", "status", "assignee")
    return {"since": r["since"], "until": r["until"], "summary": r["summary"], "by_severity": r["by_severity"],
            "by_assignee": r["by_assignee"],
            "breaches": [{**{k: b[k] for k in keep}, "which": b["which"], "late_min": b["late_min"],
                          "still_open": b["still_open"]} for b in r["breaches"][:40]],
            "exits": [{**{k: e[k] for k in keep}, "exit": e["exit"], "resolution_met": e["resolution_met"],
                       "time_to_resolve_h": e["time_to_resolve_h"]} for e in r["exits"][:40]],
            "at_risk_now": [{**{k: x[k] for k in keep}, "which": x["which"], "minutes_left": x["minutes_left"]}
                            for x in r["at_risk"]],
            **({"note": "lists cut to 40 rows; the card shows all"} if len(r["breaches"]) > 40 or len(r["exits"]) > 40 else {})}


async def _support_team(ctx: Context, a: dict[str, Any]) -> Any:
    return [{"name": m["display_name"], "username": m["username"], "tier": m["tier"], "open_tickets": m["open_tickets"]}
            for m in await ctx.api.get("/tickets/support-team", plant_id=ctx.plant_id)]


async def _propose_change(ctx: Context, a: dict[str, Any]) -> Any:
    action = eng.ENGINEER_ACTIONS[a["action"]]
    if not action.needs_ticket:
        return "use propose_new_ticket to raise a ticket"
    if a.get("numbers"):
        return await _propose_bulk(ctx, action, a)
    if not a.get("number"):
        return "give the ticket number (or numbers for the same change on several tickets)"
    keep = {*eng.REQUIRED_ARGS.get(a["action"], ()), *eng.OPTIONAL_ARGS.get(a["action"], ()), "note"}
    links = ("machine", "order") if action.name == "edit_details" else ()  # edit_details resolves them to ids below
    args = {k: a[k] for k in keep if a.get(k) and k not in links}
    missing = [k for k in eng.REQUIRED_ARGS.get(action.name, ()) if not args.get(k)]
    if missing:
        return f"missing {', '.join(missing)} for {action.name}"
    if problem := await _resolve_assignee(ctx, args):
        return problem
    if action.name == "edit_details":
        if problem := await _resolve_links(ctx, {k: a[k] for k in ("machine", "order") if k in a}, args):
            return problem
        if not set(args) - {"reason", "note"}:
            return "say which details change (title, description, category, impact, machine, order)"
    t = await _ticket_by_number(ctx, a["number"])
    if t is None:
        return f"ticket {a['number']} not found"
    if action.name in ("approve_proposal", "reject_proposal"):
        async with ctx.sf() as s:
            p = (await s.execute(select(proposal).where(proposal.c.id == args["proposal_id"]))).first()
        if p is None or str(p.ticket_id) != str(t["id"]) or p.status != "PENDING":
            return "no pending AI proposal with that id on this ticket (see list_pending_approvals)"
        args["summary"] = p.summary
    card = {"kind": "confirm", "action_id": str(new_id()), "action": action.name, "label": action.label,
            "ticket_id": t["id"], "number": t["number"], "args": args, "summary": action.describe(args),
            "reason": (a.get("reason") or "").strip()[:1000] or None, "status": "PENDING"}
    await _emit_card(ctx, card)
    return {"shown_for_confirmation": True, "done": False,
            "note": "NOT done yet: the engineer confirms or cancels it on the card. Do not say it was done."}


async def _resolve_assignee(ctx: Context, args: dict[str, Any]) -> str | None:
    """assign: the support engineer's name as MES knows it (None) or the problem."""
    if "assignee" not in args or args["assignee"].strip().lower() in ("nobody", "none", "unassigned"):
        return None
    w = args["assignee"].strip().lower()
    if w == "me":
        w = (ctx.engineer_name or "").lower()
    team = await ctx.api.get("/tickets/support-team", plant_id=ctx.plant_id)
    hits = [m for m in team if w in (m["username"].lower(), (m["email"] or "").lower(), m["display_name"].lower())]         or [m for m in team if w and w in m["display_name"].lower()]
    if len(hits) != 1:
        names = ", ".join(m["display_name"] for m in team)
        return f"{'several' if hits else 'no'} support engineers match '{args['assignee']}' (team: {names})"
    args["assignee"] = hits[0]["display_name"]
    return None


async def _propose_bulk(ctx: Context, action: Any, a: dict[str, Any]) -> Any:
    """One confirmation card for the same change on several tickets (e.g. all P2s on line 1 to P3)."""
    if action.name not in eng.BULK_ACTIONS:
        return f"{action.name} is one ticket at a time; bulk works for {', '.join(eng.BULK_ACTIONS)}"
    numbers = list(dict.fromkeys(n.strip().upper() for n in a["numbers"] if n and n.strip()))
    if len(numbers) > eng.MAX_BULK:
        return f"at most {eng.MAX_BULK} tickets in one change"
    keep = {*eng.REQUIRED_ARGS.get(action.name, ()), *eng.OPTIONAL_ARGS.get(action.name, ()), "note"}
    args = {k: a[k] for k in keep if a.get(k)}
    if missing := [k for k in eng.REQUIRED_ARGS.get(action.name, ()) if not args.get(k)]:
        return f"missing {', '.join(missing)} for {action.name}"
    if problem := await _resolve_assignee(ctx, args):
        return problem
    rows = {t["number"]: t for t in await ctx.api.get("/tickets", plant_id=ctx.plant_id, open_only=True, limit=1000)}
    unknown = [n for n in numbers if n not in rows]
    if unknown:
        return f"not open (or not found): {', '.join(unknown)}; only open tickets can be changed"
    if action.name == "set_severity" and (same := [n for n in numbers if rows[n]["severity"] == args["severity"]]):
        return f"already {args['severity']}: {', '.join(same)} (leave them out)"
    tickets = [{"ticket_id": rows[n]["id"], "number": n, "title": rows[n]["title"], "severity": rows[n]["severity"],
                "status": rows[n]["status"], "assignee": rows[n].get("assignee_name")} for n in numbers]
    card = {"kind": "confirm", "action_id": str(new_id()), "action": action.name, "label": action.label,
            "ticket_id": None, "number": f"{len(tickets)} tickets", "tickets": tickets, "args": args,
            "summary": action.describe(args), "reason": (a.get("reason") or "").strip()[:1000] or None,
            "status": "PENDING"}
    await _emit_card(ctx, card)
    return {"shown_for_confirmation": True, "done": False, "tickets": numbers,
            "note": "NOT done yet: the engineer confirms or cancels it on the card (one confirmation for all)."}


# ------------------------------------------------------------------------------------------------ registry

# contexts: "ticket" = L1 ticket run, "l2" = L2 root-cause run, "chat" = operator chat, "engineer" = support engineer
# chat (admin UI, runs as the engineer; changes only through confirmation cards)
BOTH = frozenset({"ticket", "l2", "chat", "engineer"})
TICKET, CHAT = frozenset({"ticket", "l2"}), frozenset({"chat"})
L1, L2, L2_READ, ENG = frozenset({"ticket"}), frozenset({"l2"}), frozenset({"l2", "engineer"}), frozenset({"engineer"})
S = {"type": "string"}
REASON = {"type": "string", "maxLength": 1000, "description": "Why, citing the evidence you found."}
CATEGORIES = ["MES_APP", "HARDWARE", "INTEGRATION", "DATA", "ACCESS", "CHANGE_REQUEST", "OTHER"]
IMPACTS = ["LINE_STOP", "DEGRADED", "MINOR", "SERVICE_REQUEST"]
MAX_HORIZON = 92  # days; MES forecasts up to about three months
HORIZON = {"type": "integer", "minimum": 1, "maximum": MAX_HORIZON,
           "description": "days ahead, as asked: a week 7, two weeks 14, a month 30, two months 61, a quarter 91; "
                          "'until <date>' = the days from today to that date"}
TICKET_FILTER: dict[str, Any] = {
    "scope": {"type": "string", "enum": ["open", "all", "resolved"]},
    "severity": {"type": "array", "items": {"type": "string", "enum": eng.SEVERITIES}, "maxItems": 4},
    "tier": {"type": "array", "items": {"type": "string", "enum": eng.TIERS}, "maxItems": 3},
    "status": {"type": "array", "items": {"type": "string", "enum": eng.STATUSES}, "maxItems": 7},
    "category": {"type": "array", "items": {"type": "string", "enum": eng.CATEGORIES}, "maxItems": 7},
    "sla": {"type": "string", "enum": ["breached", "at_risk"]},
    "text": {"type": "string", "maxLength": 100}, "machine": S, "line": S,
    "assignee": {"type": "string", "maxLength": 100,
                 "description": "a support engineer's name (or part), 'me', 'others' (assigned to someone else) or "
                                "'unassigned'"},
}

TOOLS: list[Tool] = [
    Tool("search_knowledge", "Search the plant's knowledge: machine guides and uploaded OEM manuals (fault / alarm codes, "
         "symptoms, causes, checks, safety, spare parts), fault-code lists, and the MES user guide (how to do things in "
         "MES, per screen). kind: MACHINE_GUIDE | MANUAL | ALARM_LIST | USER_GUIDE | NOTE; equipment_type: CNC_MILL, "
         "CNC_LATHE, ASSEMBLY, TESTER, WASHER (also returns general documents). Cite the document (and page).",
         {"query": {"type": "string", "maxLength": 300},
          "kind": {"type": "string", "enum": ["MACHINE_GUIDE", "MANUAL", "ALARM_LIST", "USER_GUIDE", "NOTE"]},
          "equipment_type": {"type": "string", "maxLength": 40}, "limit": {"type": "integer"}},
         ("query",), _search_knowledge, BOTH, lambda a: "Looking in the manuals and guides"),
    Tool("read_knowledge", "Read more of a document found with search_knowledge (by doc_id; optionally one PDF page).",
         {"doc_id": S, "page": {"type": "integer"}}, ("doc_id",), _read_knowledge, BOTH,
         lambda a: "Reading the document"),
    Tool("read_runbook", "Read a support runbook (steps for the operator, diagnosis, fix, when to escalate). Use an id "
         "from the runbook index.", {"runbook_id": S}, ("runbook_id",), _read_runbook, BOTH,
         lambda a: "Looking up the procedure"),
    Tool("search_tickets", "Find earlier or open tickets similar to a description (full-text), with their resolution "
         "notes. scope: all | open | resolved.",
         {"query": {"type": "string", "maxLength": 300}, "scope": {"type": "string", "enum": ["all", "open", "resolved"]}},
         ("query",), _search_tickets, BOTH, lambda a: "Checking similar tickets"),
    Tool("get_ticket", "Details and recent activity of a ticket by number (MES-000123).", {"number": S}, ("number",),
         _get_ticket, BOTH, lambda a: f"Opening ticket {a.get('number', '')}"),
    Tool("get_machine_status", "Machine status (AVAILABLE / DOWN / MAINTENANCE), current run, pause reason, open "
         "downtime, notifications and maintenance orders. Omit machine for all machines.", {"machine": S}, (),
         _machine_status, BOTH, lambda a: f"Checking machine {a.get('machine') or 'status'}"),
    Tool("get_line_status", "Live state of the machines on a line (running, paused, idle; order and operator).",
         {"line": S}, (), _line_status, BOTH, lambda a: f"Checking line {a.get('line') or ''}".strip()),
    Tool("get_work_order", "A production order by number: status (incl. quality hold), quantities, operations and "
         "recent history.", {"order": S}, ("order",), _work_order, BOTH, lambda a: f"Checking order {a.get('order', '')}"),
    Tool("get_terminal_status", "A shop-floor terminal: who is logged in and since when, enrolment, PIN lock.",
         {"terminal": S}, (), _terminal_status, BOTH, lambda a: "Checking the terminal"),
    Tool("get_integration_errors", "SAP postings and ServiceNow/Jira ticket syncs that are waiting to retry, failed or "
         "dead, with the last error. system: sap | itsm | both; optionally filter SAP by order number.",
         {"system": {"type": "string", "enum": ["sap", "itsm", "both"]}, "order": S}, (), _integration_errors, BOTH,
         lambda a: "Checking the SAP interface"),
    Tool("get_recent_changes", "Recent changes from the audit trail (who changed what, with reasons). Optionally a table "
         "like 'planning.work_order' or 'identity.terminal_session', and hours back (default 24).",
         {"table": S, "hours": {"type": "integer"}}, (), _recent_changes, BOTH, lambda a: "Checking recent changes"),
    Tool("get_quality_status", "Open nonconformances (quality problems, holds), optionally for one order.",
         {"order": S}, (), _quality_status, BOTH, lambda a: "Checking quality records"),
    Tool("get_shortages", "Material shortages: demand vs available stock, affected lines and orders.",
         {"material": S}, (), _shortages, BOTH, lambda a: "Checking material availability"),
    # ---- ticket work (policy-controlled)
    Tool("add_ticket_note", "Add a work note to this ticket (visible in MES and as a ServiceNow work note).",
         {"text": {"type": "string", "maxLength": 4000}, "rationale": REASON}, ("text",), _write("add_note"), TICKET,
         lambda a: "Writing a note"),
    Tool("escalate_ticket", "Hand this ticket to the next support tier (humans). The summary must say what you checked, "
         "what you ruled out and the suspected cause.", {"summary": {"type": "string", "maxLength": 4000}, "rationale": REASON},
         ("summary",), _write("escalate"), L1, lambda a: "Escalating"),
    Tool("set_ticket_severity", "Change this ticket's severity (P1 line stop, P2 degraded, P3 minor, P4 request).",
         {"severity": {"type": "string", "enum": ["P1", "P2", "P3", "P4"]}, "reason": REASON, "rationale": REASON},
         ("severity", "reason"), _write("set_severity"), TICKET, lambda a: "Adjusting severity"),
    Tool("resolve_ticket", "Resolve this ticket. The resolution says what was wrong and what fixed it.",
         {"resolution": {"type": "string", "maxLength": 4000}, "rationale": REASON}, ("resolution",), _write("resolve"),
         TICKET, lambda a: "Resolving"),
    Tool("close_as_duplicate", "Close this ticket as a duplicate of an open ticket about the same problem.",
         {"duplicate_of": S, "reason": REASON, "rationale": REASON}, ("duplicate_of", "reason"),
         _write("close_duplicate"), TICKET, lambda a: "Linking the duplicate"),
    Tool("retry_sap_message", "Retry a failed SAP posting by its message id (from get_integration_errors). Only for "
         "transient failures; the adapter checks SAP first, so it never posts twice.",
         {"message_id": S, "reason": REASON, "rationale": REASON}, ("message_id", "reason"), _write("retry_sap"), TICKET,
         lambda a: "Retrying the SAP posting"),
    Tool("retry_itsm_sync", "Retry a failed ServiceNow/Jira ticket sync by its message id.",
         {"message_id": S, "reason": REASON, "rationale": REASON}, ("message_id", "reason"), _write("retry_itsm"), TICKET,
         lambda a: "Retrying the ServiceNow sync"),
    Tool("end_terminal_session", "End the operator session left open on a terminal (the device stays enrolled).",
         {"terminal": S, "reason": REASON, "rationale": REASON}, ("terminal", "reason"), _write("end_terminal_session"),
         TICKET, lambda a: "Ending the terminal session"),
    Tool("report_machine_back_up", "Report a machine back up after a breakdown that was cleared without a maintenance "
         "order in progress.", {"machine": S, "note": REASON, "rationale": REASON}, ("machine", "note"),
         _write("machine_back_up"), TICKET, lambda a: "Releasing the machine"),
    # ---- L2: deeper diagnosis, root cause, approved fixes, L3 handover
    Tool("search_logs", "Search the service logs (JSON lines with level, message, request_id, exception). service: one "
         f"of {', '.join(SERVICES)} (omit for all); level: error | warning; text: substring to match; minutes back "
         "(default 60, max 7 days).",
         {"service": {"type": "string", "enum": SERVICES}, "level": {"type": "string", "enum": ["error", "warning"]},
          "text": {"type": "string", "maxLength": 200}, "minutes": {"type": "integer"}, "limit": {"type": "integer"}},
         (), _search_logs, L2_READ, lambda a: "Searching the logs"),
    Tool("trace_request", "Every log line, across all services, of one request or event: a request id from a log line "
         "or the audit trail (also 'event:<id>' for event handling, 'agent-run:<id>' for agent runs).",
         {"request_id": {"type": "string", "maxLength": 120}, "hours": {"type": "integer"}}, ("request_id",),
         _trace_request, L2_READ, lambda a: "Tracing the request"),
    Tool("get_sap_message", "One SAP posting message in full: payload, SAP result, and every attempt with its error.",
         {"message_id": S}, ("message_id",), _sap_message, L2_READ, lambda a: "Reading the SAP message"),
    Tool("compare_order_with_sap", "An order as MES has it next to the SAP snapshots the adapter received, plus the "
         "postings MES sent to SAP for it. Shows data mismatches (status, quantities, dates).", {"order": S}, ("order",),
         _compare_order, L2_READ, lambda a: "Comparing the order with SAP"),
    Tool("get_event_failures", "Events MES could not process (dead letters), with the error.", {"limit": {"type": "integer"}},
         (), _event_failures, L2_READ, lambda a: "Checking failed events"),
    Tool("get_service_health", "Readiness of the MES services, the SAP queue and polling, and the ServiceNow/Jira "
         "connections.", {}, (), _service_health, L2_READ, lambda a: "Checking service health"),
    Tool("record_root_cause", "Record your root-cause analysis (stored with the run and posted as a work note). Do "
         "this once you have the evidence, before fixing, resolving or escalating.",
         {"symptom": {"type": "string", "maxLength": 600}, "root_cause": {"type": "string", "maxLength": 1500},
          "category": {"type": "string", "enum": ["CODE_BUG", "CONFIGURATION", "DATA", "INTEGRATION", "INFRASTRUCTURE",
                                                  "HARDWARE", "USER_PROCESS", "UNKNOWN"]},
          "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
          "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
          "fix": {"type": "string", "maxLength": 1500}, "prevention": {"type": "string", "maxLength": 1000}},
         ("symptom", "root_cause", "category", "confidence", "evidence", "fix"), _record_rca, L2,
         lambda a: "Recording the root cause"),
    Tool("resync_order", "Re-read an order from SAP into MES (when MES has stale order data). Needs approval.",
         {"order": S, "reason": REASON, "rationale": REASON}, ("order", "reason"), _write("resync_order"), L2,
         lambda a: "Re-reading the order from SAP"),
    Tool("escalate_to_l3", "Hand the ticket to L3 (developers; a Jira issue is opened) when the root cause is a code "
         "bug or needs a change. The fields become the Jira handover.",
         {"summary": {"type": "string", "maxLength": 600}, "suspected_component": {"type": "string", "maxLength": 200},
          "steps_to_reproduce": {"type": "string", "maxLength": 1500}, "expected": {"type": "string", "maxLength": 500},
          "actual": {"type": "string", "maxLength": 800}, "evidence": {"type": "string", "maxLength": 1500},
          "rationale": REASON},
         ("summary", "suspected_component", "steps_to_reproduce", "expected", "actual", "evidence"), _escalate_l3, L2,
         lambda a: "Handing over to L3"),
    # ---- operator chat
    Tool("show_quick_replies", "Show up to 4 short answer buttons under your message (the operator can still type). "
         "Write the question itself in your message text first; the buttons only hold the answers (each under 40 "
         "characters). Calling it ends your turn until the operator answers.",
         {"options": {"type": "array", "items": {"type": "string"}, "maxItems": 4}}, ("options",), _quick_replies, CHAT,
         lambda a: ""),
    Tool("create_ticket", "Raise a support ticket for the operator when the problem is not fixed in the chat. The "
         "description must include what the operator reported, what you checked and what you found.",
         {"title": {"type": "string", "maxLength": 150}, "description": {"type": "string", "maxLength": 4000},
          "category": {"type": "string", "enum": CATEGORIES}, "impact": {"type": "string", "enum": IMPACTS}},
         ("title", "description", "category", "impact"), _create_ticket, CHAT, lambda a: "Raising a ticket"),
    Tool("mark_fixed", "Record that the problem was fixed in the chat, after the operator confirmed it works. Creates "
         "the ticket for the record and resolves it.",
         {"title": {"type": "string", "maxLength": 150}, "description": {"type": "string", "maxLength": 4000},
          "resolution": {"type": "string", "maxLength": 2000}, "category": {"type": "string", "enum": CATEGORIES},
          "impact": {"type": "string", "enum": IMPACTS}},
         ("title", "description", "resolution", "category"), _mark_fixed, CHAT, lambda a: "Recording the fix"),
    Tool("add_to_my_ticket", "Add information from the operator to the ticket raised in this conversation (new "
         "symptoms, what they tried, a workaround).", {"text": {"type": "string", "maxLength": 2000}}, ("text",),
         _add_to_ticket, CHAT, lambda a: "Adding it to the ticket"),
    # ---- support engineer chat (admin UI)
    Tool("list_tickets", "Tickets matching filters, as data for you to reason about (count + brief rows with SLA state "
         "and assignee). scope: open (default) | all | resolved; sla: breached | at_risk; assignee: a name, me, others, "
         "unassigned. To show tickets to the engineer use show_card instead.", TICKET_FILTER | {"limit": {"type": "integer"}}, (), _list_tickets, ENG,
         lambda a: "Looking up tickets"),
    Tool("show_card", "Put an interactive card on the engineer's screen (live data, clickable rows and buttons). kinds: "
         "tickets (a filtered ticket list), ticket (one ticket: details, SLA, activity, AI work, actions; needs number), "
         "approvals (AI proposals waiting), sla (SLA and speed overview; days), sla_report (SLA breach / exit report "
         "for the last days: breaches, exits met/missed, per severity and assignee, at risk now, CSV download), "
         "forecast (expected new tickets for the next horizon_days, 1-92: the period the engineer asked for; with "
         "range, trend, severity mix and "
         "backlog outlook), team (support engineers and their open tickets), alerts (open on-call alerts), ai_run "
         "(the AI's latest run on a ticket, step by step; needs number), machines (machine status; optional machine), "
         "integration (SAP postings and ServiceNow/Jira syncs waiting or failed; system). Prefer a card over listing "
         "rows in text.",
         {"kind": {"type": "string", "enum": eng.CARD_KINDS}, "title": {"type": "string", "maxLength": 120},
          "number": S, "days": {"type": "integer"}, "horizon_days": HORIZON,
          "system": {"type": "string", "enum": ["sap", "itsm", "both"]},
          **TICKET_FILTER}, ("kind",), _show_card, ENG, lambda a: "Preparing the view"),
    Tool("get_ticket_forecast", "Forecast of new tickets for the next horizon_days (the period asked for, 1-92 days) from "
         "the last 8 weeks: expected total with an 80% range, per week and per day, trend per week, busiest day, "
         "expected P1/P2 and category mix, backlog outlook (open now, expected resolved, projected open) and how "
         "well the method predicted last week (backtest). Numbers come from MES; don't invent your own.",
         {"horizon_days": HORIZON}, (), _forecast, ENG,
         lambda a: f"Forecasting the next {a.get('horizon_days', 7)} days"),
    Tool("get_sla_report", "SLA breach / exit report for the last days (default 7): breaches (response or resolution "
         "target missed, how late, still open), exits (tickets resolved / closed / cancelled, target met or missed), "
         "met % per severity, breaches per assignee, and tickets at risk right now.",
         {"days": {"type": "integer", "minimum": 1, "maximum": 90}}, (), _sla_report, ENG,
         lambda a: "Building the SLA report"),
    Tool("list_support_team", "The support engineers tickets can be assigned to: name, user name, usual tier and how "
         "many open tickets each has.", {}, (), _support_team, ENG, lambda a: "Checking the support team"),
    Tool("get_ai_activity", "What the AI support agents did on a ticket: every run (tier, outcome, summary, root cause, "
         "cost) and its proposals with their status.", {"number": S}, ("number",), _ai_activity, ENG,
         lambda a: f"Checking the AI's work on {a.get('number', '')}"),
    Tool("list_pending_approvals", "AI proposals waiting for a human decision (id, ticket, action, rationale).", {}, (),
         _pending_approvals, ENG, lambda a: "Checking approvals"),
    Tool("get_sla_overview", "Support performance over the last days: raised/resolved, SLA met and speed per severity, "
         "who resolved (AI, people, ServiceNow/Jira), categories, recurring machines.", {"days": {"type": "integer"}}, (),
         _sla_overview, ENG, lambda a: "Checking SLA performance"),
    Tool("propose_new_ticket", "Prepare a new support ticket from what the engineer describes. It is shown as a "
         "confirmation card (with open tickets that may be the same problem) and is only created, in the engineer's "
         "name, when they confirm. Look up the facts first (machine, order, what happened) and write a clear title and "
         "description; impact decides the suggested severity (LINE_STOP P1, DEGRADED P2, MINOR P3, SERVICE_REQUEST P4). "
         "assignee: a support engineer (name, or 'me') to assign it to in the same confirmation.",
         {"title": {"type": "string", "maxLength": 160}, "description": {"type": "string", "maxLength": 4000},
          "category": {"type": "string", "enum": CATEGORIES}, "impact": {"type": "string", "enum": IMPACTS},
          "severity": {"type": "string", "enum": eng.SEVERITIES}, "machine": S, "order": S,
          "assignee": {"type": "string", "maxLength": 100}, "reason": REASON},
         ("title", "description", "category", "impact"), _propose_new_ticket, ENG,
         lambda a: "Preparing a new ticket for you to confirm"),
    Tool("propose_change", "Suggest a change on a ticket. It is shown to the engineer as a confirmation card and only "
         "happens if they confirm (with their own rights). Put the arguments the action needs in the matching fields: "
         "put_on_hold note; add_note text; escalate summary; set_severity severity + reason; resolve resolution; "
         "close_duplicate duplicate_of + reason; retry_sap / retry_itsm message_id + reason; end_terminal_session "
         "terminal + reason; machine_back_up machine + note; resync_order order + reason; approve_proposal / "
         "reject_proposal proposal_id; edit_details reason + any of title, description, category, impact, machine "
         "(code, or 'none' to unlink), order (number or 'none'); cancel note (why; a cancelled ticket stays in the "
         "history, there is no deletion); assign assignee (support engineer's name, or 'nobody') + optional note. "
         "reason = why you suggest it (shown on the card). The same change on several open tickets (e.g. 'all P2 on "
         f"line 1 to P3', 'assign these to Wei'): numbers instead of number, one card for all; only for "
         f"{', '.join(eng.BULK_ACTIONS)}, at most {eng.MAX_BULK}.",
         {"action": {"type": "string", "enum": [n for n, x in eng.ENGINEER_ACTIONS.items() if x.needs_ticket]},
          "number": S, "numbers": {"type": "array", "items": S, "maxItems": eng.MAX_BULK}, "reason": REASON,
          "assignee": {"type": "string", "maxLength": 100}, "title": {"type": "string", "maxLength": 160},
          "description": {"type": "string", "maxLength": 4000}, "category": {"type": "string", "enum": CATEGORIES},
          "impact": {"type": "string", "enum": IMPACTS},
          "note": {"type": "string", "maxLength": 2000}, "text": {"type": "string", "maxLength": 4000},
          "summary": {"type": "string", "maxLength": 4000}, "severity": {"type": "string", "enum": eng.SEVERITIES},
          "resolution": {"type": "string", "maxLength": 4000}, "duplicate_of": S, "message_id": S, "terminal": S,
          "machine": S, "order": S, "proposal_id": S},
         ("action",), _propose_change, ENG, lambda a: "Preparing a change for you to confirm"),
]
BY_NAME = {t.name: t for t in TOOLS}


def tools_for(mode: str) -> list[Tool]:
    return [t for t in TOOLS if mode in t.contexts]


async def execute(ctx: Context, name: str, args: Any) -> tuple[str, bool]:
    """Run one tool call. Returns (result for the model, is_error). Never raises for tool-level problems."""
    started = time.monotonic()
    if ctx.mcp is not None and name in ctx.mcp.tools:
        remote = ctx.mcp.tools[name]
        if not isinstance(args, dict):
            return "arguments must be an object", True
        if ctx.emit:
            await ctx.emit({"type": "status", "text": f"Querying {remote.label}"})
        out, is_error = await ctx.hub.call(remote, args)
        await ctx.recorder.step("TOOL", name, args, {"error": out} if is_error else {"result": out[:20000]},
                                is_error=is_error, started=started)
        return short(out, 12000), is_error
    tool = BY_NAME.get(name)
    if tool is None or ctx.mode not in tool.contexts:
        await ctx.recorder.step("TOOL", name, args, {"error": "not available"}, is_error=True, started=started)
        return f"tool '{name}' is not available here", True
    problem = _validate(tool, args)
    if problem:
        await ctx.recorder.step("TOOL", name, args, {"error": problem}, is_error=True, started=started)
        return f"invalid arguments: {problem}", True
    if ctx.emit and (text := tool.status(args)):
        await ctx.emit({"type": "status", "text": text})
    try:
        result = await tool.handler(ctx, args)
    except ApiError as e:
        await ctx.recorder.step("TOOL", name, args, {"error": str(e)}, is_error=True, started=started)
        return f"MES refused or failed: {e}", True
    except Exception as e:  # a tool bug or an unexpected answer must not end the run: the model can work around it
        log.exception("tool %s failed", name)
        await ctx.recorder.step("TOOL", name, args, {"error": f"{type(e).__name__}: {e}"}, is_error=True, started=started)
        return f"the tool failed ({type(e).__name__}); try another way to get this information", True
    await ctx.recorder.step("TOOL", name, args, result, started=started)
    return short(result), False
