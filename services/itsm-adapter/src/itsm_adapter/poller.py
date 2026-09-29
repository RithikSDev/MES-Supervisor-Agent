"""Inbound: what support engineers did in ServiceNow and Jira -> `itsm.ticket.changed` events for MES.

Changes made by the integration user itself are skipped (they came from MES). Each change has a stable external
id (record + change timestamp / journal entry / changelog id), so re-reading an overlapping window publishes
nothing twice. When a developer moves the Jira issue to Done, the ServiceNow incident gets a work note so L2 can
verify and resolve.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from itsm_adapter.connections import Registry
from itsm_adapter.db import inbound_change, messaging, poll_state
from itsm_adapter.ports import JiraPort, ServiceNowPort
from itsm_adapter.settings import Settings
from itsm_adapter.sync import TicketSync
from mes_common.events import Topics

log = logging.getLogger(__name__)

EVENT_TICKET_CHANGED = "itsm.ticket.changed"
SN_PRIORITY_SEVERITY = {"1": "P1", "2": "P2", "3": "P3", "4": "P4", "5": "P4"}


def sn_time(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def parse_sn(v: str) -> datetime:
    return datetime.strptime(v, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


class ItsmPoller:
    def __init__(self, registry: Registry, sync: TicketSync, session_factory: async_sessionmaker[AsyncSession],
                 settings: Settings) -> None:
        self.reg = registry
        self.sync = sync
        self._sf = session_factory
        self._s = settings

    @property
    def sn(self) -> ServiceNowPort:
        return self.reg.sn

    @property
    def jira(self) -> JiraPort:
        return self.reg.jira

    @property
    def _tier_of(self) -> dict[str, str]:
        return {g: t for t, g in self.reg.cfg.groups.items()}

    async def poll_all(self) -> int:
        n = await self.poll_servicenow() if self.sn.configured else 0
        return n + (await self.poll_jira() if self.jira.configured else 0)

    async def _watermark(self, s: AsyncSession, name: str) -> datetime:
        row = (await s.execute(select(poll_state.c.watermark).where(poll_state.c.name == name))).first()
        if row:
            return row.watermark
        start = datetime.now(UTC) - timedelta(days=1)
        await s.execute(insert(poll_state).values(name=name, watermark=start).on_conflict_do_nothing())
        return start

    async def _publish(self, s: AsyncSession, change: dict[str, Any]) -> bool:
        inserted = (await s.execute(insert(inbound_change).values(
            external_id=change["external_id"], ticket_id=change["ticket_id"], origin=change["origin"], payload=change,
        ).on_conflict_do_nothing(index_elements=["external_id"]).returning(inbound_change.c.external_id))).first()
        if inserted is None:
            return False
        await messaging.publish(s, topic=Topics.ITSM_TICKET_CHANGES, key=change["ticket_id"],
                                event_type=EVENT_TICKET_CHANGED, payload=change)
        return True

    async def _finish(self, s: AsyncSession, name: str, watermark: datetime, published: int, error: str | None) -> None:
        values: dict[str, Any] = {"last_run_at": func.now(), "last_error": error}
        if error is None:
            values |= {"watermark": watermark, "last_success_at": func.now(), "last_published": published}
        await s.execute(update(poll_state).where(poll_state.c.name == name).values(**values))

    # ------------------------------------------------------------------ ServiceNow
    async def poll_servicenow(self) -> int:
        async with self._sf() as s, s.begin():
            wm = await self._watermark(s, "servicenow")
            since_dt = wm - timedelta(seconds=self._s.poll_overlap_s)
            published, max_seen = 0, wm
            try:
                for inc in await self.sn.changed_since(since_dt):
                    cid = inc.get("correlation_id") or ""
                    if not cid.startswith("MES:"):
                        continue
                    ticket_id = cid[4:]
                    max_seen = max(max_seen, parse_sn(inc["sys_updated_on"]))
                    if not self.sn.is_mine(inc["sys_updated_by"]):
                        # "Assigned to" only when someone is set: MES assignees without a ServiceNow user are left
                        # empty there, and that must not unassign them in MES
                        assignee = None
                        if inc.get("assigned_to"):
                            u = await self.sn.get_user(inc["assigned_to"]["sys_id"]) or {}
                            assignee = {"name": u.get("name") or inc["assigned_to"].get("name"),
                                        "user_name": u.get("user_name"), "email": u.get("email") or None}
                        published += await self._publish(s, {
                            **({"assignee": assignee} if assignee else {}),
                            "ticket_id": ticket_id, "origin": "SERVICENOW",
                            "external_id": f"sn:{inc['sys_id']}:{inc['sys_updated_on']}", "author": inc["sys_updated_by"],
                            "at": parse_sn(inc["sys_updated_on"]).isoformat(), "sn_state": inc["state"],
                            "sn_group": inc.get("assignment_group"), "tier": self._tier_of.get(inc.get("assignment_group") or ""),
                            "severity": SN_PRIORITY_SEVERITY.get(inc["priority"]), "resolution_note": inc.get("close_notes"),
                        })
                    for j in await self.sn.journal(inc["sys_id"], since_dt):
                        if self.sn.is_mine(j["sys_created_by"]):
                            continue
                        kind = "Work note" if j["element"] == "work_notes" else "Comment"
                        published += await self._publish(s, {
                            "ticket_id": ticket_id, "origin": "SERVICENOW", "external_id": f"sn:journal:{j['sys_id']}",
                            "author": j["sys_created_by"], "at": parse_sn(j["sys_created_on"]).isoformat(),
                            "comment": f"{kind} in {inc['number']}: {j['value']}",
                        })
            except Exception as e:
                await self._finish(s, "servicenow", wm, 0, str(e)[:2000])
                log.warning("ServiceNow poll failed: %s", e)
                return 0
            await self._finish(s, "servicenow", max_seen, published, None)
        return published

    # ------------------------------------------------------------------ Jira
    async def poll_jira(self) -> int:
        async with self._sf() as s, s.begin():
            wm = await self._watermark(s, "jira")
            since = wm - timedelta(seconds=self._s.poll_overlap_s)
            published, max_seen = 0, wm
            done_notes: list[tuple[str, str, str]] = []
            try:
                for issue in await self.jira.changed_since(since):
                    ticket_id = next((lbl[len("mes-ticket-"):] for lbl in issue["labels"] if lbl.startswith("mes-ticket-")), None)
                    if ticket_id is None:
                        continue
                    max_seen = max(max_seen, datetime.fromisoformat(issue["updated"]))
                    for ch in issue.get("changelog", []):
                        if await self.jira.is_mine(ch["author_id"]) or datetime.fromisoformat(ch["created"]) < since:
                            continue
                        for item in ch["items"]:
                            if item["field"] != "status":
                                continue
                            published += await self._publish(s, {
                                "ticket_id": ticket_id, "origin": "JIRA", "external_id": f"jira:{issue['key']}:{ch['id']}",
                                "author": ch["author"], "at": ch["created"], "jira_key": issue["key"],
                                "jira_status": item["to_value"],
                            })
                            if item["to_value"] == "Done":
                                done_notes.append((ticket_id, issue["key"], ch["id"]))
                    for c in issue.get("comments", []):
                        if await self.jira.is_mine(c["author_id"]) or datetime.fromisoformat(c["created"]) < since:
                            continue
                        published += await self._publish(s, {
                            "ticket_id": ticket_id, "origin": "JIRA", "external_id": f"jira:comment:{c['id']}",
                            "author": c["author"], "at": c["created"], "jira_key": issue["key"],
                            "comment": f"Comment on {issue['key']}: {c['body']}",
                        })
            except Exception as e:
                await self._finish(s, "jira", wm, 0, str(e)[:2000])
                log.warning("Jira poll failed: %s", e)
                return 0
            await self._finish(s, "jira", max_seen, published, None)
        for ticket_id, key, change_id in done_notes:  # tell L2 in ServiceNow (idempotent: marker)
            try:
                inc = await self.sync.find_incident(ticket_id)
                if inc:
                    await self.sync.note_once(inc["sys_id"], f"jira-done:{change_id}",
                                               f"Jira {key} is Done: the fix is ready. Verify and resolve the incident.")
            except Exception as e:
                log.warning("could not note Jira completion on the incident: %s", e)
        return published
