"""Outbound: MES ticket events -> ServiceNow incident (+ Jira issue at L3), through the ITSM ports (REST or MCP).

Every delivery re-derives what ServiceNow / Jira should look like from the event, so it is idempotent and a retry
after a timeout is also its reconciliation:
  - the incident is found by `correlation_id = MES:<ticket id>` before one is created,
  - the Jira issue by the label `mes-ticket-<ticket id>`,
  - notes and comments carry `[MES:<activity id>]` and are only added when not there yet,
  - only the fields the MES change touched are written (an engineer's edits in ServiceNow are not overwritten),
    and an unchanged value is not written at all (no update stamp, nothing echoed back).
Events that originated in ServiceNow / Jira (origin != MES) only ensure the records exist (e.g. open the Jira issue
after an escalation to L3 made in ServiceNow).

Status flow per message: PENDING -> SENDING -> SYNCED | RETRY_WAIT -> ... | FAILED (rejected) | DEAD (gave up) |
SKIPPED (the ticket lives on a ServiceNow instance the connection no longer points to: tickets stay on the instance
they were created on, so switching instances never duplicates them).
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from itsm_adapter.calls import call_log
from itsm_adapter.connections import Registry
from itsm_adapter.db import messaging, outbound_attempt, outbound_message, ticket_link
from itsm_adapter.errors import ItsmError, ItsmErrorKind
from itsm_adapter.ports import JiraPort, ServiceNowPort
from itsm_adapter.settings import Settings
from mes_common.events import EventEnvelope, PermanentError, Topics

log = logging.getLogger(__name__)

EVENT_TICKET_CREATED = "mes.ticket.created"
EVENT_TICKET_UPDATED = "mes.ticket.updated"
EVENT_SYNC_RESULT = "itsm.ticket.sync_result"

# MES status -> ServiceNow incident state
SN_STATE = {"NEW": "1", "ACKNOWLEDGED": "2", "IN_PROGRESS": "2", "ON_HOLD": "3", "RESOLVED": "6", "CLOSED": "7",
            "CANCELLED": "8"}
# severity -> ServiceNow (impact, urgency): the priority lookup then gives P1..P4
SN_IMPACT_URGENCY = {"P1": (1, 1), "P2": (1, 2), "P3": (2, 2), "P4": (2, 3)}
JIRA_PRIORITY = {"P1": "Highest", "P2": "High", "P3": "Medium", "P4": "Low"}
SN_CATEGORY = {"MES_APP": "software", "HARDWARE": "hardware", "INTEGRATION": "software", "DATA": "database",
               "ACCESS": "inquiry", "CHANGE_REQUEST": "request", "OTHER": "inquiry"}


def backoff(attempt: int) -> timedelta:
    return timedelta(seconds=min(5 * 2 ** (attempt - 1), 300))


def marker(ref: str) -> str:
    return f"[MES:{ref}]"


class TicketSync:
    def __init__(self, registry: Registry, settings: Settings) -> None:
        self.reg = registry
        self.s = settings

    @property
    def sn(self) -> ServiceNowPort:
        return self.reg.sn

    @property
    def jira(self) -> JiraPort:
        return self.reg.jira

    def _description(self, p: dict[str, Any]) -> str:
        ctx = [f"{k}: {v}" for k, v in (("Line", p.get("line_code")), ("Machine", p.get("equipment_code")),
                                          ("Order", p.get("erp_order_number")), ("Terminal", p.get("terminal_code"))) if v]
        return "\n".join([
            p.get("description") or p["title"], "",
            f"MES ticket {p['number']} · severity {p['severity']} · impact {p['impact']} · category {p['category']}",
            f"Reported by {p.get('reported_by')} at {p['reported_at']} (source {p.get('source')})",
            *ctx,
            f"Response due {p['response_due_at']}; resolution due {p.get('resolution_due_at') or 'scheduled'}",
            f"{self.s.mes_ui_url}/tickets?ticket={p['ticket_id']}",
        ])

    async def _assigned_to(self, p: dict[str, Any]) -> tuple[str, str | None]:
        """(sys_id or "" for nobody, note when the MES assignee has no ServiceNow user)."""
        a = p.get("assignee")
        if not a:
            return "", None
        user = await self.sn.find_user(a.get("email"), a.get("username"))
        if user:
            return user["sys_id"], None
        return "", (f"Assigned in MES to {a['name']} (no ServiceNow user with e-mail {a.get('email') or '-'} or user "
                    f"name {a.get('username') or '-'}; left unassigned here)")

    async def find_incident(self, ticket_id: str) -> dict[str, Any] | None:
        return await self.sn.find_by_correlation(f"MES:{ticket_id}")

    async def find_issue(self, ticket_id: str) -> dict[str, Any] | None:
        return await self.jira.find_by_label(f"mes-ticket-{ticket_id}")

    async def note_once(self, sys_id: str, ref: str, text: str) -> None:
        m = marker(ref)
        if not any(m in (j.get("value") or "") for j in await self.sn.journal(sys_id)):
            await self.sn.add_work_note(sys_id, f"{text}\n{m}")

    async def _jira_comment_once(self, issue: dict[str, Any], ref: str, text: str) -> None:
        m = marker(ref)
        if not any(m in (c.get("body") or "") for c in issue.get("comments", [])):
            await self.jira.comment(issue["key"], f"{text}\n{m}")

    async def apply(self, p: dict[str, Any], *, jira_allowed: bool = True) -> dict[str, Any]:
        """jira_allowed=False: the ticket's Jira issue lives on another site than the one connected now."""
        tid = p["ticket_id"]
        cfg = self.reg.cfg
        from_mes = p.get("origin", "MES") == "MES"
        changes = set(p.get("changes") or [])
        impact, urgency = SN_IMPACT_URGENCY[p["severity"]]
        group = cfg.groups[p["tier"]]

        inc = await self.find_incident(tid)
        assign_note = None
        if inc is None:
            fields = {
                "short_description": f"[{p['number']}] {p['title']}"[:160], "description": self._description(p),
                "impact": impact, "urgency": urgency, "category": SN_CATEGORY.get(p["category"], "inquiry"),
                "assignment_group": group, "correlation_id": f"MES:{tid}",
                "cmdb_ci": p.get("equipment_code") or p.get("terminal_code") or p.get("line_code"),
            }
            reported = datetime.fromisoformat(p["reported_at"])
            if datetime.now(UTC) - reported > timedelta(minutes=10):  # backdated (e.g. demo history, offline terminal)
                fields["opened_at"] = reported.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
            if p.get("assignee"):
                fields["assigned_to"], assign_note = await self._assigned_to(p)
            inc = await self.sn.create(fields)
            changes |= {"status", "tier", "severity"}  # a late create: bring it to the current MES state
        if from_mes:
            upd: dict[str, Any] = {}
            if "status" in changes and p["status"] != "NEW":
                upd["state"] = int(SN_STATE[p["status"]])
                if p["status"] in ("RESOLVED", "CLOSED"):
                    upd["close_notes"] = p.get("resolution_note") or "Resolved in MES"
            if "severity" in changes:
                upd["impact"], upd["urgency"] = impact, urgency
            if "tier" in changes:
                upd["assignment_group"] = group  # also clears "Assigned to"; the assignee below goes back in
            if "assignee" in changes or ("tier" in changes and p.get("assignee")):
                want_user, assign_note = await self._assigned_to(p)
                current = (inc.get("assigned_to") or {}).get("sys_id") or ""
                if want_user != current or "tier" in changes:
                    upd["assigned_to"] = want_user
            if "title" in changes:
                upd["short_description"] = f"[{p['number']}] {p['title']}"[:160]
            if changes & {"description", "impact", "equipment_id", "work_order_id", "category"}:
                upd["description"] = self._description(p)
            if "category" in changes:
                upd["category"] = SN_CATEGORY.get(p["category"], "inquiry")
            if assign_note and p.get("demo") == "history":
                assign_note = None  # backdated history: no note per closed demo incident
            if assign_note:  # before a state change: closed incidents take no more notes
                await self.note_once(inc["sys_id"], f"assignee:{tid}:{p['assignee']['name']}", assign_note)
            want = {k: str(v) for k, v in upd.items()}
            if any(str(inc.get(k)) != v for k, v in want.items() if k not in ("close_notes", "description", "assigned_to")) \
                    or "description" in upd or "assigned_to" in upd:
                inc = await self.sn.update(inc["sys_id"], upd)
            if p.get("comment") and str(inc.get("state")) in ("7", "8") and not changes & {"status", "created"}:
                # ServiceNow refuses updates to closed / canceled incidents: a late note stays in MES only
                log.info("%s: note not sent, incident %s is closed or canceled", p["number"], inc["number"])
            elif p.get("comment"):
                c = p["comment"]
                await self.note_once(inc["sys_id"], c["id"], f"{c.get('author') or 'MES'} (MES {p['number']}): {c['text']}")

        issue = None
        if p["tier"] == "L3" and self.jira.configured and jira_allowed:
            issue = await self.find_issue(tid)
            if issue is None:
                issue = await self.jira.create(
                    issue_type=cfg.jira_issue_type_task if p["category"] in ("CHANGE_REQUEST", "ACCESS") else cfg.jira_issue_type_bug,
                    summary=f"[{p['severity']}] {p['title']} ({inc['number']})",
                    description=f"Escalated to L3 from ServiceNow {inc['number']} ({inc.get('url', '')}).\n\n" + self._description(p),
                    priority=JIRA_PRIORITY[p["severity"]], labels=["mes", f"mes-ticket-{tid}"],
                )
                if str(inc.get("state")) not in ("7", "8"):  # closed incidents take no notes (e.g. backdated history)
                    await self.note_once(inc["sys_id"], f"jira:{issue['key']}",
                                          f"Escalated to L3: Jira {issue['key']} {issue.get('url', '')}")
                if from_mes and p.get("comment"):  # the escalation reason (handover) goes onto the new issue
                    c = p["comment"]
                    await self._jira_comment_once(issue, c["id"], f"{c.get('author') or 'MES'} (MES {p['number']}): {c['text']}")
            elif from_mes and p.get("comment"):
                c = p["comment"]
                await self._jira_comment_once(issue, c["id"], f"{c.get('author') or 'MES'} (MES {p['number']}): {c['text']}")
        return {
            "sn_number": inc["number"], "sn_sys_id": inc["sys_id"], "sn_state": inc["state"],
            "sn_group": inc.get("assignment_group"), "sn_url": inc.get("url"),
            "jira_key": issue["key"] if issue else None, "jira_status": issue["status"] if issue else None,
            "jira_url": issue.get("url") if issue else None,
        }


class Intake:
    """Kafka handler: MES ticket event -> durable outbound message (id = event id, so redelivery is a no-op)."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings

    async def __call__(self, s: AsyncSession, env: EventEnvelope) -> None:
        p = env.payload
        try:
            ticket_id, number = UUID(p["ticket_id"]), p["number"]
        except Exception as e:
            raise PermanentError(f"invalid ticket event: {e}") from e
        await s.execute(
            insert(outbound_message).values(
                id=env.event_id, ticket_id=ticket_id, ticket_number=number, event_type=env.event_type, payload=p,
                status="PENDING", max_attempts=self._s.max_attempts,
            ).on_conflict_do_nothing(index_elements=["id"])
        )


class Dispatcher:
    def __init__(self, sync: TicketSync, session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self.sync = sync
        self._sf = session_factory
        self._s = settings

    async def run_once(self, limit: int = 20) -> int:
        if not self.sync.sn.configured:
            return 0  # not connected yet: events wait (PENDING) and go out once ServiceNow is configured
        ids = await self._claim(limit)
        sem = asyncio.Semaphore(max(1, self._s.dispatch_concurrency))

        async def one(msg_id: UUID) -> None:
            async with sem:
                await self.deliver(msg_id)

        await asyncio.gather(*(one(i) for i in ids))
        return len(ids)

    async def _claim(self, limit: int) -> list[UUID]:
        m = outbound_message
        earlier = aliased(outbound_message)
        now = func.now()
        due = or_(and_(m.c.status.in_(("PENDING", "RETRY_WAIT")), m.c.next_attempt_at <= now),
                  and_(m.c.status == "SENDING", m.c.lease_until < now))
        blocked = select(earlier.c.id).where(earlier.c.ticket_id == m.c.ticket_id, earlier.c.seq < m.c.seq,
                                              earlier.c.status.in_(("PENDING", "SENDING", "RETRY_WAIT"))).exists()
        async with self._sf() as s, s.begin():
            ids = (await s.execute(select(m.c.id).where(due, ~blocked).order_by(m.c.seq).limit(limit)
                                   .with_for_update(skip_locked=True))).scalars().all()
            if ids:
                await s.execute(update(m).where(m.c.id.in_(ids)).values(
                    status="SENDING", attempts=m.c.attempts + 1, lease_until=now + timedelta(seconds=self._s.lease_s),
                    updated_at=now))
        return list(ids)

    async def deliver(self, msg_id: UUID) -> None:
        async with self._sf() as s:
            row = (await s.execute(select(outbound_message).where(outbound_message.c.id == msg_id))).one()
            link = (await s.execute(select(ticket_link).where(ticket_link.c.ticket_id == row.ticket_id))).first()
        cfg = self.sync.reg.cfg
        started = datetime.now(UTC)
        if link and link.sn_instance and cfg.sn_instance and link.sn_instance != cfg.sn_instance:
            await self._skip(row, started, f"{row.ticket_number} lives on {link.sn_instance} ({link.sn_number}); the "
                                           f"connection now points to {cfg.sn_instance}, so it is not synced")
            return
        jira_allowed = not (link and link.jira_site and cfg.jira_site and link.jira_site != cfg.jira_site)
        calls: list[dict[str, Any]] = []
        token = call_log.set(calls)
        try:
            result = await self.sync.apply(row.payload, jira_allowed=jira_allowed)
        except ItsmError as e:
            await self._failure(row, started, e, calls)
            return
        except Exception as e:  # unclassified: retry (delivery is idempotent)
            await self._failure(row, started, ItsmError(ItsmErrorKind.AMBIGUOUS, repr(e)), calls)
            return
        finally:
            call_log.reset(token)
        async with self._sf() as s, s.begin():
            await s.execute(update(outbound_message).where(outbound_message.c.id == row.id).values(
                status="SYNCED", result=result, last_error=None, lease_until=None, completed_at=func.now(),
                updated_at=func.now()))
            await self._attempt(s, row, started, "SYNCED", calls, None)
            await self._link(s, row.ticket_id, result, cfg)
            await messaging.publish(s, topic=Topics.ITSM_TICKET_RESULTS, key=str(row.ticket_id),
                                    event_type=EVENT_SYNC_RESULT,
                                    payload={"ticket_id": str(row.ticket_id), "status": "SYNCED", **result})
        log.info("ticket %s synced (%s): %s %s", row.ticket_number, row.event_type, result["sn_number"], result["jira_key"] or "")

    @staticmethod
    async def _link(s: AsyncSession, ticket_id: UUID, result: dict[str, Any], cfg: Any) -> None:
        """Remember the instance (and Jira site) the ticket lives on; the first sync decides."""
        jira = {"jira_site": cfg.jira_site, "jira_key": result["jira_key"]} if result.get("jira_key") else {}
        stmt = insert(ticket_link).values(ticket_id=ticket_id, sn_instance=cfg.sn_instance, sn_sys_id=result["sn_sys_id"],
                                          sn_number=result["sn_number"], **jira)
        if jira:
            stmt = stmt.on_conflict_do_update(index_elements=["ticket_id"], set_=jira,
                                              where=ticket_link.c.jira_key.is_(None))
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=["ticket_id"])
        await s.execute(stmt)

    async def _skip(self, row: Any, started: datetime, reason: str) -> None:
        async with self._sf() as s, s.begin():
            await s.execute(update(outbound_message).where(outbound_message.c.id == row.id).values(
                status="SKIPPED", last_error=reason, lease_until=None, completed_at=func.now(), updated_at=func.now()))
            await self._attempt(s, row, started, "SKIPPED", [], reason)
        log.info("ticket %s skipped: %s", row.ticket_number, reason)

    async def _failure(self, row: Any, started: datetime, e: ItsmError, calls: list[dict[str, Any]]) -> None:
        if e.kind == ItsmErrorKind.PERMANENT:
            status = "FAILED"
        elif row.attempts >= row.max_attempts:
            status = "DEAD"
        else:
            status = "RETRY_WAIT"
        async with self._sf() as s, s.begin():
            await s.execute(update(outbound_message).where(outbound_message.c.id == row.id).values(
                status=status, last_error=str(e)[:2000], lease_until=None,
                next_attempt_at=func.now() + backoff(row.attempts),
                completed_at=func.now() if status in ("FAILED", "DEAD") else None, updated_at=func.now()))
            await self._attempt(s, row, started, e.kind.value, calls, str(e))
            if status in ("FAILED", "DEAD"):
                await messaging.publish(s, topic=Topics.ITSM_TICKET_RESULTS, key=str(row.ticket_id),
                                        event_type=EVENT_SYNC_RESULT,
                                        payload={"ticket_id": str(row.ticket_id), "status": "FAILED", "error": str(e)})
        log.warning("ticket %s attempt %d -> %s: %s", row.ticket_number, row.attempts, status, e)

    async def _attempt(self, s: AsyncSession, row: Any, started: datetime, outcome: str, calls: list, error: str | None) -> None:
        await s.execute(outbound_attempt.insert().values(
            message_id=row.id, attempt_no=row.attempts, started_at=started, finished_at=datetime.now(UTC),
            outcome=outcome, calls=calls, error=error[:2000] if error else None))


async def retry_message(s: AsyncSession, msg_id: UUID, extra_attempts: int = 3) -> bool:
    res = await s.execute(update(outbound_message).where(
        outbound_message.c.id == msg_id, outbound_message.c.status.in_(("FAILED", "DEAD", "RETRY_WAIT"))
    ).values(status="PENDING", next_attempt_at=func.now(), max_attempts=outbound_message.c.attempts + extra_attempts,
             completed_at=None, updated_at=func.now()))
    return res.rowcount > 0
