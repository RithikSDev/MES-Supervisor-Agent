"""Support engineer chat (admin UI): what the assistant may put on screen, and the changes it may suggest.

The assistant never changes anything itself here. It shows a confirmation card; when the engineer confirms, the
change runs with the engineer's own token (MES checks their rights and audits them), exactly as if they had clicked
the button in the ticket screen. Cards carry a query (filters or ids), not data: the UI fetches live data itself.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from support_agent.actions import ACTIONS
from support_agent.clients import UserApi

CARD_KINDS = ["tickets", "ticket", "approvals", "sla", "sla_report", "forecast", "team", "alerts", "ai_run", "machines",
              "integration"]
SEVERITIES, TIERS = ["P1", "P2", "P3", "P4"], ["L1", "L2", "L3"]
STATUSES = ["NEW", "ACKNOWLEDGED", "IN_PROGRESS", "ON_HOLD", "RESOLVED", "CLOSED", "CANCELLED"]
OPEN_STATUSES = ("NEW", "ACKNOWLEDGED", "IN_PROGRESS", "ON_HOLD")
CATEGORIES = ["MES_APP", "HARDWARE", "INTEGRATION", "DATA", "ACCESS", "CHANGE_REQUEST", "OTHER"]
AT_RISK_SHARE = 0.25  # an SLA target is at risk with a quarter of its time left (core flags at 75% used)


def _clock_state(t: dict[str, Any], clock: dict[str, Any]) -> str:
    if clock.get("breached"):
        return "breached"
    if clock.get("met_at") or not clock.get("due") or clock.get("remaining_s") is None:
        return "ok"
    total = (datetime.fromisoformat(clock["due"]) - datetime.fromisoformat(t["reported_at"])).total_seconds()
    return "at_risk" if total > 0 and clock["remaining_s"] <= total * AT_RISK_SHARE else "ok"


def sla_state(t: dict[str, Any]) -> str:
    """breached | at_risk | ok, over both clocks (the UI computes the same)."""
    states = {_clock_state(t, t["response"]), _clock_state(t, t["resolution"])}
    return "breached" if "breached" in states else "at_risk" if "at_risk" in states else "ok"


def ticket_filter(a: dict[str, Any], me: str | None = None) -> dict[str, Any]:
    """The filter a ticket-list card carries (only what was asked for). assignee: a name (part of it), "me",
    "others" (assigned to someone other than the engineer) or "unassigned"; me = the engineer's name."""
    f: dict[str, Any] = {"scope": a.get("scope") or "open"}
    for key in ("severity", "tier", "status", "category"):
        if a.get(key):
            f[key] = [v for v in a[key] if v]
    for key in ("sla", "text", "machine", "line", "assignee"):
        if a.get(key):
            f[key] = a[key].strip() if isinstance(a[key], str) else a[key]
    if f.get("assignee", "").lower() in ("me", "others") and me:
        f["me"] = me
    return f


def assignee_matches(t: dict[str, Any], want: str, me: str | None) -> bool:
    name = (t.get("assignee_name") or "").lower()
    w = want.strip().lower()
    if w == "unassigned":
        return not name
    if w == "me":
        return bool(name) and name == (me or "").lower()
    if w == "others":
        return bool(name) and name != (me or "").lower()
    return bool(name) and w in name


def ticket_matches(t: dict[str, Any], f: dict[str, Any]) -> bool:
    if f.get("scope", "open") == "open" and t["status"] not in OPEN_STATUSES:
        return False
    if f.get("scope") == "resolved" and t["status"] not in ("RESOLVED", "CLOSED"):
        return False
    for key in ("severity", "tier", "status", "category"):
        if f.get(key) and t[key] not in f[key]:
            return False
    if f.get("sla") and sla_state(t) != f["sla"]:
        return False
    if f.get("assignee") and not assignee_matches(t, f["assignee"], f.get("me")):
        return False
    if f.get("machine") and (t.get("equipment_code") or "").upper() != f["machine"].upper():
        return False
    if f.get("line") and (t.get("line_code") or "").upper() != f["line"].upper():
        return False
    if f.get("text"):
        needle = f["text"].lower()
        hay = " ".join(str(t.get(k) or "") for k in ("number", "title", "description", "sn_number", "jira_key",
                                                      "equipment_code", "line_code", "erp_order_number")).lower()
        if needle not in hay:
            return False
    return True


async def tickets_for(api: UserApi, plant_id: str, f: dict[str, Any]) -> list[dict[str, Any]]:
    rows = await api.get("/tickets", plant_id=plant_id, open_only=f.get("scope", "open") == "open", limit=500)
    return [t for t in rows if ticket_matches(t, f)]


def ticket_brief(t: dict[str, Any]) -> dict[str, Any]:
    keep = ("number", "title", "severity", "tier", "status", "category", "line_code", "equipment_code",
            "erp_order_number", "reported_at", "reported_by_name", "assignee_name", "sn_number", "jira_key")
    return {**{k: t[k] for k in keep if t.get(k)}, "sla": sla_state(t),
            "resolution_left_min": round(t["resolution"]["remaining_s"] / 60) if t["resolution"].get("remaining_s") is not None
            and not t["resolution"].get("met_at") else None}


# ------------------------------------------------------------------------------------------------ changes


Runner = Callable[[UserApi, dict[str, Any], dict[str, Any], str], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class EngineerAction:
    name: str
    label: str
    describe: Callable[[dict[str, Any]], str]
    run: Runner | None  # None: carried out by the agent service itself (take over, run the AI, proposals)
    needs_ticket: bool = True


def _status(status: str) -> Runner:
    async def run(api: UserApi, t: dict[str, Any], a: dict[str, Any], by: str) -> dict[str, Any]:
        r = await api.post(f"/tickets/{t['id']}/status", {"status": status, "note": a.get("note") or None})
        return {"status": r["status"]}
    return run


async def _create(api: UserApi, t: dict[str, Any] | None, a: dict[str, Any], by: str) -> dict[str, Any]:
    body = {k: a[k] for k in ("plant_id", "title", "description", "category", "impact", "severity", "equipment_id",
                              "work_order_id") if a.get(k)}
    r = await api.post("/tickets", body)  # as the engineer: MES records them as the reporter
    out = {"ticket_id": r["id"], "number": r["number"], "severity": r["severity"], "tier": r["tier"]}
    if a.get("assignee"):  # created and assigned with one confirmation
        out["assignee"] = (await api.post(f"/tickets/{r['id']}/assign", {"assignee": a["assignee"]}))["assignee_name"]
    return out


async def _details(api: UserApi, t: dict[str, Any], a: dict[str, Any], by: str) -> dict[str, Any]:
    body = {k: a[k] for k in ("title", "description", "category", "impact") if a.get(k)}
    for key in ("equipment_id", "work_order_id"):
        if key in a:
            body[key] = a[key]
    r = await api.post(f"/tickets/{t['id']}/details", {**body, "reason": a["reason"]})
    return {"title": r["title"], "category": r["category"], "impact": r["impact"]}


async def _assign(api: UserApi, t: dict[str, Any], a: dict[str, Any], by: str) -> dict[str, Any]:
    who = None if a["assignee"].strip().lower() in ("nobody", "none", "unassigned") else a["assignee"]
    r = await api.post(f"/tickets/{t['id']}/assign", {"assignee": who, "note": a.get("note") or None})
    return {"assignee": r["assignee_name"]}


def _existing(name: str) -> Runner:
    async def run(api: UserApi, t: dict[str, Any], a: dict[str, Any], by: str) -> dict[str, Any]:
        # the engineer decides and acts: no "approved by" suffix, MES records them as the actor
        return await ACTIONS[name].run(api, t, a, None, None)  # type: ignore[arg-type]
    return run


ENGINEER_ACTIONS: dict[str, EngineerAction] = {a.name: a for a in (
    EngineerAction("create_ticket", "Create ticket", lambda a: f"Create ticket: {a['title']}", _create,
                   needs_ticket=False),
    EngineerAction("edit_details", "Edit ticket details", lambda a: f"Change details: {a['reason']}", _details),
    EngineerAction("cancel", "Cancel ticket", lambda a: f"Cancel: {a['note']}", _status("CANCELLED")),
    EngineerAction("assign", "Assign", lambda a: f"Assign to {a['assignee']}" if a["assignee"].lower() not in (
        "nobody", "none", "unassigned") else "Unassign", _assign),
    EngineerAction("acknowledge", "Acknowledge", lambda a: "Acknowledge the ticket", _status("ACKNOWLEDGED")),
    EngineerAction("start_work", "Start work", lambda a: "Set the ticket in progress", _status("IN_PROGRESS")),
    EngineerAction("put_on_hold", "Put on hold", lambda a: f"Put on hold: {a.get('note', '')}", _status("ON_HOLD")),
    EngineerAction("add_note", "Add a work note", lambda a: f"Add note: {a['text']}", _existing("add_note")),
    EngineerAction("escalate", "Escalate", lambda a: f"Escalate to the next tier: {a['summary']}", _existing("escalate")),
    EngineerAction("set_severity", "Change severity", lambda a: f"Set severity {a['severity']}: {a['reason']}",
                   _existing("set_severity")),
    EngineerAction("resolve", "Resolve", lambda a: f"Resolve: {a['resolution']}", _existing("resolve")),
    EngineerAction("close_duplicate", "Close as duplicate", lambda a: f"Close as duplicate of {a['duplicate_of']}: "
                   f"{a['reason']}", _existing("close_duplicate")),
    EngineerAction("retry_sap", "Retry SAP posting", lambda a: f"Retry SAP message {a['message_id']}: {a['reason']}",
                   _existing("retry_sap")),
    EngineerAction("retry_itsm", "Retry ServiceNow/Jira sync", lambda a: f"Retry sync {a['message_id']}: {a['reason']}",
                   _existing("retry_itsm")),
    EngineerAction("end_terminal_session", "End terminal session",
                   lambda a: f"End the session on {a['terminal']}: {a['reason']}", _existing("end_terminal_session")),
    EngineerAction("machine_back_up", "Report machine back up", lambda a: f"Report {a['machine']} back up: {a['note']}",
                   _existing("machine_back_up")),
    EngineerAction("resync_order", "Re-read order from SAP", lambda a: f"Re-read order {a['order']} from SAP: "
                   f"{a['reason']}", _existing("resync_order")),
    EngineerAction("take_over", "Take over from the AI", lambda a: "Take the ticket over: the AI stops working it", None),
    EngineerAction("run_ai", "Run the AI agent again", lambda a: "Hand the ticket (back) to the AI agent for a new run",
                   None),
    EngineerAction("approve_proposal", "Approve AI proposal", lambda a: f"Approve the AI's proposal: {a.get('summary', '')}",
                   None),
    EngineerAction("reject_proposal", "Reject AI proposal", lambda a: f"Reject the AI's proposal: {a.get('summary', '')}",
                   None),
)}

# arguments each change needs (checked before the confirmation card is shown)
REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    "create_ticket": ("title", "description", "category", "impact"), "edit_details": ("reason",), "cancel": ("note",),
    "put_on_hold": ("note",), "add_note": ("text",), "assign": ("assignee",), "escalate": ("summary",), "set_severity": ("severity", "reason"),
    "resolve": ("resolution",), "close_duplicate": ("duplicate_of", "reason"), "retry_sap": ("message_id", "reason"),
    "retry_itsm": ("message_id", "reason"), "end_terminal_session": ("terminal", "reason"),
    "machine_back_up": ("machine", "note"), "resync_order": ("order", "reason"), "approve_proposal": ("proposal_id",),
    "reject_proposal": ("proposal_id",),
}
# optional arguments some changes use besides their required ones
OPTIONAL_ARGS: dict[str, tuple[str, ...]] = {
    "create_ticket": ("severity", "machine", "order", "assignee"),
    "edit_details": ("title", "description", "category", "impact", "machine", "order"),
    "assign": ("note",),
}
# changes that can be confirmed for several tickets at once (one card, run ticket by ticket)
BULK_ACTIONS = ("set_severity", "assign", "acknowledge", "start_work", "add_note", "put_on_hold")
MAX_BULK = 25
ARG_NAMES = sorted({k for v in REQUIRED_ARGS.values() for k in v})
