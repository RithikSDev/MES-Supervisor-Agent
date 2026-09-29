"""The things the agent can change, each behind a policy: off, suggest (a human approves) or auto.

One executor per action is used both when the policy lets the agent act on its own (agent's service account) and when
a supervisor approves a proposal (the approver's own token: MES checks that person may do it and audits them).
Lookups (finding a terminal or machine by code) always use the agent's read access.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from support_agent.clients import Api, ApiError

Ticket = dict[str, Any]  # id, number, plant_id (+ status, tier)
Executor = Callable[[Api, Ticket, dict[str, Any], str | None, str | None], Awaitable[dict[str, Any]]]

OFF, SUGGEST, AUTO = "off", "suggest", "auto"


@dataclass(frozen=True)
class Action:
    name: str
    label: str
    default: str
    modes: tuple[str, ...]
    describe: Callable[[dict[str, Any]], str]  # the proposal, in words
    run: Executor


def _by(text: str, approved_by: str | None) -> str:
    return f"{text} (proposed by the AI support agent, approved by {approved_by})" if approved_by else text


async def _note(api: Api, t: Ticket, a: dict[str, Any], token: str | None, by: str | None) -> dict[str, Any]:
    await api.post(f"/tickets/{t['id']}/comments", {"text": _by(a["text"], by)}, token=token)
    return {"noted": True}


async def _escalate(api: Api, t: Ticket, a: dict[str, Any], token: str | None, by: str | None) -> dict[str, Any]:
    r = await api.post(f"/tickets/{t['id']}/escalate", {"reason": _by(a["summary"], by)}, token=token)
    return {"tier": r["tier"], "status": r["status"]}


async def _severity(api: Api, t: Ticket, a: dict[str, Any], token: str | None, by: str | None) -> dict[str, Any]:
    r = await api.post(f"/tickets/{t['id']}/severity", {"severity": a["severity"], "reason": _by(a["reason"], by)},
                       token=token)
    return {"severity": r["severity"]}


async def _resolve(api: Api, t: Ticket, a: dict[str, Any], token: str | None, by: str | None) -> dict[str, Any]:
    r = await api.post(f"/tickets/{t['id']}/status", {"status": "RESOLVED", "note": _by(a["resolution"], by)},
                       token=token)
    return {"status": r["status"]}


async def _duplicate(api: Api, t: Ticket, a: dict[str, Any], token: str | None, by: str | None) -> dict[str, Any]:
    number = a["duplicate_of"].strip().upper()
    hits = await api.get("/tickets/search", plant_id=t["plant_id"], q=number, limit=1)
    original = next((h for h in hits if h["number"] == number), None)
    if original is None or original["id"] == t["id"]:
        raise ApiError(404, f"ticket {number} not found")
    await api.post(f"/tickets/{original['id']}/comments",
                   {"text": _by(f"Duplicate report {t['number']} closed: {a['reason']}", by)}, token=token)
    r = await api.post(f"/tickets/{t['id']}/status",
                       {"status": "CANCELLED", "note": _by(f"Duplicate of {number}: {a['reason']}", by)}, token=token)
    return {"status": r["status"], "duplicate_of": number}


async def _retry_sap(api: Api, t: Ticket, a: dict[str, Any], token: str | None, by: str | None) -> dict[str, Any]:
    return await api.call("erp", "POST", f"/outbound/{a['message_id']}/retry", json={"extra_attempts": 3}, token=token)


async def _retry_itsm(api: Api, t: Ticket, a: dict[str, Any], token: str | None, by: str | None) -> dict[str, Any]:
    return await api.call("itsm", "POST", f"/messages/{a['message_id']}/retry", token=token)


async def _end_session(api: Api, t: Ticket, a: dict[str, Any], token: str | None, by: str | None) -> dict[str, Any]:
    code = a["terminal"].strip().upper()
    terminal = next((x for x in await api.get("/terminals", plant_id=t["plant_id"]) if x["code"].upper() == code), None)
    if terminal is None:
        raise ApiError(404, f"terminal {code} not found")
    await api.post(f"/terminals/{terminal['id']}/end-session", {"reason": _by(a["reason"], by)}, token=token)
    return {"terminal": code, "session_ended_for": terminal.get("session_operator")}


async def _resync_order(api: Api, t: Ticket, a: dict[str, Any], token: str | None, by: str | None) -> dict[str, Any]:
    order = a["order"].strip()
    r = await api.call("erp", "POST", f"/orders/{order}/resync", token=token)
    await api.post(f"/tickets/{t['id']}/comments", {"text": _by(f"Order {order} re-read from SAP: {a['reason']}", by)},
                   token=token)
    return {"order": order, **(r or {})}


async def _machine_up(api: Api, t: Ticket, a: dict[str, Any], token: str | None, by: str | None) -> dict[str, Any]:
    code = a["machine"].strip().upper()
    eq = next((x for x in await api.get("/maintenance/equipment", plant_id=t["plant_id"]) if x["code"].upper() == code),
              None)
    if eq is None:
        raise ApiError(404, f"machine {code} not found")
    await api.post(f"/maintenance/equipment/{eq['id']}/back-up", {"note": _by(a["note"], by)}, token=token)
    return {"machine": code, "status": "AVAILABLE"}


TICKET_MODES = (OFF, SUGGEST, AUTO)

ACTIONS: dict[str, Action] = {a.name: a for a in (
    Action("add_note", "Add a work note", AUTO, TICKET_MODES,
           lambda a: f"Add note: {a['text']}", _note),
    Action("escalate", "Escalate to the next tier (human)", AUTO, TICKET_MODES,
           lambda a: f"Escalate to the next tier: {a['summary']}", _escalate),
    Action("set_severity", "Change severity", SUGGEST, TICKET_MODES,
           lambda a: f"Change severity to {a['severity']}: {a['reason']}", _severity),
    Action("resolve", "Resolve the ticket", SUGGEST, TICKET_MODES,
           lambda a: f"Resolve: {a['resolution']}", _resolve),
    Action("close_duplicate", "Close as duplicate", SUGGEST, TICKET_MODES,
           lambda a: f"Close as duplicate of {a['duplicate_of']}: {a['reason']}", _duplicate),
    Action("retry_sap", "Retry a SAP posting", SUGGEST, TICKET_MODES,
           lambda a: f"Retry SAP message {a['message_id']}: {a['reason']}", _retry_sap),
    Action("retry_itsm", "Retry a ServiceNow/Jira sync", SUGGEST, TICKET_MODES,
           lambda a: f"Retry ServiceNow/Jira sync {a['message_id']}: {a['reason']}", _retry_itsm),
    Action("end_terminal_session", "End a terminal session", SUGGEST, TICKET_MODES,
           lambda a: f"End the operator session on {a['terminal']}: {a['reason']}", _end_session),
    Action("machine_back_up", "Report a machine back up", SUGGEST, TICKET_MODES,
           lambda a: f"Report {a['machine']} back up: {a['note']}", _machine_up),
    Action("resync_order", "Re-read an order from SAP (L2)", SUGGEST, TICKET_MODES,
           lambda a: f"Re-read order {a['order']} from SAP into MES: {a['reason']}", _resync_order),
)}

# operator chat: the operator is in the loop, so these are either allowed or not
CHAT_ACTIONS = {
    "create_ticket": ("Raise a ticket from the chat", AUTO),
    "resolve_confirmed": ("Record a fix the operator confirmed in the chat", AUTO),
}


def default_policies() -> dict[str, str]:
    return {**{a.name: a.default for a in ACTIONS.values()}, **{k: v[1] for k, v in CHAT_ACTIONS.items()}}


def allowed_modes(action: str) -> tuple[str, ...]:
    if action in ACTIONS:
        return ACTIONS[action].modes
    if action in CHAT_ACTIONS:
        return (OFF, AUTO)
    return ()


def label(action: str) -> str:
    return ACTIONS[action].label if action in ACTIONS else CHAT_ACTIONS.get(action, (action,))[0]
