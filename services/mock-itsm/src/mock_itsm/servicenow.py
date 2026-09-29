"""Mock ServiceNow ITSM: incidents with assignment groups, work notes / comments, and an MCP server whose tools look
like the common ServiceNow MCP servers (create_incident, update_incident, add_work_note, ...).

Headless: used only by the automated tests (the demo connects to a real ServiceNow instance). Tests act as engineers
through `/_mock/incidents/{sys_id}/act`; everything done through MCP is attributed to the integration user
`mes.integration`, which is how the itsm-adapter tells its own changes from an engineer's.
"""

import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from mock_itsm.common import BearerKey, JsonState, fail, mcp_lifespan, mount_mcp, now

INTEGRATION_USER = "mes.integration"
GROUPS = ["MES L1 Operations", "MES L2 App & DB Engineering", "MES L3 Core Dev"]
ENGINEERS = {
    "lena.l1": ("Lena (L1)", "MES L1 Operations"),
    "lars.l2": ("Lars (L2)", "MES L2 App & DB Engineering"),
    "leo.l3": ("Leo (L3)", "MES L3 Core Dev"),
}
STATES = {"1": "New", "2": "In Progress", "3": "On Hold", "6": "Resolved", "7": "Closed", "8": "Canceled"}
# ServiceNow priority lookup: (impact, urgency) -> priority 1..5
PRIORITY = {("1", "1"): "1", ("1", "2"): "2", ("2", "1"): "2", ("1", "3"): "3", ("2", "2"): "3", ("3", "1"): "3",
            ("2", "3"): "4", ("3", "2"): "4", ("3", "3"): "5"}


def sn_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _empty() -> dict[str, Any]:
    return {"incidents": {}, "journal": [], "next_number": 10001}


class Incident(BaseModel):
    sys_id: str
    number: str
    short_description: str
    description: str
    state: str
    state_label: str
    impact: str
    urgency: str
    priority: str
    category: str
    assignment_group: str | None
    assigned_to: str | None
    caller_id: str
    correlation_id: str | None
    correlation_display: str | None
    cmdb_ci: str | None
    close_code: str | None
    close_notes: str | None
    opened_at: str
    resolved_at: str | None
    sys_created_on: str
    sys_updated_on: str
    sys_updated_by: str
    url: str


class JournalEntry(BaseModel):
    sys_id: str
    element_id: str
    element: str  # work_notes | comments
    value: str
    sys_created_on: str
    sys_created_by: str


class Group(BaseModel):
    name: str
    tier: str


class ServiceNow:
    def __init__(self, state_file: str | None = None, public_url: str | None = None) -> None:
        self.state = JsonState(Path(state_file) if state_file else None)
        self.state.load(_empty)
        self.public_url = (public_url or os.environ.get("SERVICENOW_PUBLIC_URL") or "http://localhost:8050").rstrip("/")

    @property
    def incidents(self) -> dict[str, dict[str, Any]]:
        return self.state.data["incidents"]

    def view(self, inc: dict[str, Any]) -> Incident:
        return Incident(**inc, state_label=STATES.get(inc["state"], inc["state"]),
                        url=f"{self.public_url}/nav/incident/{inc['sys_id']}")

    def find(self, key: str) -> dict[str, Any]:
        inc = self.incidents.get(key) or next((i for i in self.incidents.values() if i["number"] == key), None)
        if inc is None:
            raise fail("NOT_FOUND", f"incident {key} not found")
        return inc

    # ------------------------------------------------------------------ operations (MCP and UI)
    def create(self, user: str, *, short_description: str, description: str, impact: str, urgency: str, category: str,
               assignment_group: str | None, caller_id: str, correlation_id: str | None, cmdb_ci: str | None) -> dict:
        if not short_description.strip():
            raise fail("INVALID_ARGUMENT", "short_description is mandatory")
        if impact not in ("1", "2", "3") or urgency not in ("1", "2", "3"):
            raise fail("INVALID_ARGUMENT", "impact and urgency are 1 (high) .. 3 (low)")
        if assignment_group and assignment_group not in GROUPS:
            raise fail("INVALID_ARGUMENT", f"unknown assignment group {assignment_group}")
        with self.state.lock:
            t = sn_time(now())
            number = f"INC{self.state.data['next_number']:07d}"
            self.state.data["next_number"] += 1
            inc = {
                "sys_id": secrets.token_hex(16), "number": number, "short_description": short_description[:160],
                "description": description, "state": "1", "impact": impact, "urgency": urgency,
                "priority": PRIORITY[(impact, urgency)], "category": category, "assignment_group": assignment_group,
                "assigned_to": None, "caller_id": caller_id, "correlation_id": correlation_id,
                "correlation_display": "MES" if correlation_id else None, "cmdb_ci": cmdb_ci, "close_code": None,
                "close_notes": None, "opened_at": t, "resolved_at": None, "sys_created_on": t, "sys_updated_on": t,
                "sys_updated_by": user,
            }
            self.incidents[inc["sys_id"]] = inc
            self.state.save()
            return inc

    def update(self, user: str, key: str, changes: dict[str, Any]) -> dict:
        with self.state.lock:
            inc = self.find(key)
            new = {k: str(v) if v is not None else None for k, v in changes.items() if v is not None}
            if "state" in new and new["state"] not in STATES:
                raise fail("INVALID_ARGUMENT", f"state must be one of {sorted(STATES)}")
            if "assignment_group" in new and new["assignment_group"] not in GROUPS:
                raise fail("INVALID_ARGUMENT", f"unknown assignment group {new['assignment_group']}")
            if new.get("state") in ("6", "7") and not (new.get("close_notes") or inc.get("close_notes")):
                raise fail("INVALID_ARGUMENT", "close_notes are mandatory to resolve an incident")
            changed = {k: v for k, v in new.items() if inc.get(k) != v}
            if not changed:
                return inc  # nothing to do: no update stamp, no echo
            inc.update(changed)
            if "impact" in changed or "urgency" in changed:
                inc["priority"] = PRIORITY[(inc["impact"], inc["urgency"])]
            if changed.get("state") == "6":
                inc["resolved_at"] = sn_time(now())
                inc["close_code"] = inc.get("close_code") or "Solved (Permanently)"
            if "assignment_group" in changed:
                inc["assigned_to"] = None
            inc["sys_updated_on"], inc["sys_updated_by"] = sn_time(now()), user
            self.state.save()
            return inc

    def journal_add(self, user: str, key: str, element: str, text: str) -> dict:
        if not text.strip():
            raise fail("INVALID_ARGUMENT", "empty journal entry")
        with self.state.lock:
            inc = self.find(key)
            t = sn_time(now())
            entry = {"sys_id": secrets.token_hex(16), "element_id": inc["sys_id"], "element": element, "value": text,
                     "sys_created_on": t, "sys_created_by": user}
            self.state.data["journal"].append(entry)
            inc["sys_updated_on"], inc["sys_updated_by"] = t, user
            self.state.save()
            return entry

    def journal(self, sys_id: str, since: str | None = None) -> list[dict]:
        return [j for j in self.state.data["journal"] if j["element_id"] == sys_id and (not since or j["sys_created_on"] >= since)]


def build_mcp(sn: ServiceNow) -> MCPServer:
    mcp = MCPServer("servicenow", instructions="ServiceNow ITSM (mock): incidents for MES support. Writes are attributed "
                                               f"to {INTEGRATION_USER}.")
    u = INTEGRATION_USER

    @mcp.tool()
    def create_incident(short_description: str, description: str = "", impact: int = 3, urgency: int = 3,
                        category: str = "software", assignment_group: str | None = None, caller_id: str = u,
                        correlation_id: str | None = None, cmdb_ci: str | None = None) -> Incident:
        """Create an incident. `correlation_id` links it to the caller's record (idempotency: look it up first)."""
        sn.state.check_fault()
        return sn.view(sn.create(u, short_description=short_description, description=description, impact=str(impact),
                                 urgency=str(urgency), category=category, assignment_group=assignment_group,
                                 caller_id=caller_id, correlation_id=correlation_id, cmdb_ci=cmdb_ci))

    @mcp.tool()
    def get_incident(incident: str) -> Incident:
        """An incident by number (INC...) or sys_id."""
        sn.state.check_fault()
        return sn.view(sn.find(incident))

    @mcp.tool()
    def list_incidents(correlation_id: str | None = None, updated_since: str | None = None,
                       assignment_group: str | None = None, limit: int = 50) -> list[Incident]:
        """Incidents, oldest update first. `updated_since` is 'YYYY-MM-DD HH:MM:SS' (UTC), inclusive."""
        sn.state.check_fault()
        rows = [i for i in sn.incidents.values()
                if (correlation_id is None or i["correlation_id"] == correlation_id)
                and (updated_since is None or i["sys_updated_on"] >= updated_since)
                and (assignment_group is None or i["assignment_group"] == assignment_group)]
        rows.sort(key=lambda i: (i["sys_updated_on"], i["number"]))
        return [sn.view(i) for i in rows[:limit]]

    @mcp.tool()
    def update_incident(sys_id: str, state: int | None = None, impact: int | None = None, urgency: int | None = None,
                        assignment_group: str | None = None, close_code: str | None = None,
                        close_notes: str | None = None) -> Incident:
        """Change fields of an incident. state: 1 New, 2 In Progress, 3 On Hold, 6 Resolved (needs close_notes),
        7 Closed, 8 Canceled."""
        sn.state.check_fault()
        return sn.view(sn.update(u, sys_id, {"state": state, "impact": impact, "urgency": urgency,
                                             "assignment_group": assignment_group, "close_code": close_code,
                                             "close_notes": close_notes}))

    @mcp.tool()
    def add_work_note(sys_id: str, text: str) -> JournalEntry:
        """Internal work note (visible to support staff)."""
        sn.state.check_fault()
        return JournalEntry(**sn.journal_add(u, sys_id, "work_notes", text))

    @mcp.tool()
    def add_comment(sys_id: str, text: str) -> JournalEntry:
        """Additional comment (visible to the caller)."""
        sn.state.check_fault()
        return JournalEntry(**sn.journal_add(u, sys_id, "comments", text))

    @mcp.tool()
    def get_journal(sys_id: str, since: str | None = None) -> list[JournalEntry]:
        """Work notes and comments of an incident, oldest first; `since` inclusive ('YYYY-MM-DD HH:MM:SS')."""
        sn.state.check_fault()
        sn.find(sys_id)
        return [JournalEntry(**j) for j in sn.journal(sys_id, since)]

    @mcp.tool()
    def list_assignment_groups() -> list[Group]:
        """Support groups: L1 first line, L2 technical escalation, L3 core development."""
        return [Group(name=g, tier=f"L{i + 1}") for i, g in enumerate(GROUPS)]

    return mcp


class FaultsIn(BaseModel):
    fail_next: int = 0


def create_app(sn: ServiceNow | None = None) -> FastAPI:
    sn = sn or ServiceNow(os.environ.get("SERVICENOW_STATE_FILE"))
    mcp = build_mcp(sn)
    app = FastAPI(title="Mock ServiceNow", lifespan=mcp_lifespan(mcp))
    app.state.sn = sn
    app.state.mcp = mcp
    app.add_middleware(BearerKey, key=os.environ.get("SERVICENOW_MCP_KEY", "servicenow-mcp-key"))
    # ------------------------------------------------------------------ test control (JSON)
    @app.get("/_mock/health")
    async def health() -> dict:
        return {"status": "UP"}

    @app.get("/_mock/incidents")
    async def mock_incidents() -> list[dict]:
        return [dict(i, journal=sn.journal(i["sys_id"])) for i in sn.incidents.values()]

    @app.post("/_mock/incidents/{sys_id}/act")
    async def mock_act(sys_id: str, body: dict) -> dict:
        """Act as an engineer (tests / smoke): {"user": "lena.l1", "state": "2", "work_note": "..."}."""
        user = body.pop("user", "lena.l1")
        note = body.pop("work_note", None)
        inc = sn.update(user, sys_id, body)
        if note:
            sn.journal_add(user, sys_id, "work_notes", note)
        return inc

    @app.put("/_mock/faults")
    async def faults(f: FaultsIn) -> dict:
        sn.state.faults.fail_next = f.fail_next
        return {"fail_next": sn.state.faults.fail_next}

    @app.post("/_mock/reset")
    async def reset() -> dict:
        with sn.state.lock:
            sn.state.data = _empty()
            sn.state.save()
        return {"ok": True}

    mount_mcp(app, mcp)
    return app
