"""Support tickets: raise, triage, escalate, resolve; SLA clocks; sync events for ServiceNow / Jira.

Every change is published on `mes.tickets.v1` with the ticket snapshot and the list of changed fields. The
itsm-adapter applies only the changed fields to ServiceNow (so it never overwrites what an engineer did there
meanwhile) and opens the linked Jira issue once a ticket reaches L3. Changes made in ServiceNow / Jira come back
through `apply_external` (deduplicated by their external change id).
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mes_common.events import Topics
from mes_core.modules.org.public import get_equipment, get_line, get_terminal
from mes_core.modules.planning.public import get_work_order_brief
from mes_core.modules.ticketing import domain
from mes_core.modules.ticketing.models import (
    CATEGORIES,
    IMPACTS,
    OPEN_STATUSES,
    SEARCH_DOCUMENT,
    TICKET_SEQ,
    SupportMember,
    Ticket,
    TicketActivity,
)
from mes_core.shared.errors import DomainError, NotFound, ValidationFailed
from mes_core.shared.messaging import messaging

EVENT_TICKET_CREATED = "mes.ticket.created"
EVENT_TICKET_UPDATED = "mes.ticket.updated"
EVENT_TICKET_SLA_BREACHED = "mes.ticket.sla_breached"
EVENT_TICKET_SLA_AT_RISK = "mes.ticket.sla_at_risk"
# the support agent's approved actions carry this in their note (support_agent.actions._by)
AI_PROPOSED = "proposed by the AI support agent"
SYSTEM = ("system", "MES (automatic)")
# ServiceNow incident state -> MES status (1 New, 2 In Progress, 3 On Hold, 6 Resolved, 7 Closed, 8 Canceled)
SN_STATES = {"1": "NEW", "2": "IN_PROGRESS", "3": "ON_HOLD", "6": "RESOLVED", "7": "CLOSED", "8": "CANCELLED"}


def _now() -> datetime:
    return datetime.now(UTC)


async def get_ticket(s: AsyncSession, ticket_id: UUID, *, lock: bool = False) -> Ticket:
    q = select(Ticket).where(Ticket.id == ticket_id)
    if lock:
        q = q.with_for_update()
    t = (await s.scalars(q)).first()
    if t is None:
        raise NotFound("ticket not found")
    return t


async def _context(s: AsyncSession, t: Ticket) -> dict[str, Any]:
    ctx: dict[str, Any] = {"line_code": None, "equipment_code": None, "erp_order_number": None, "terminal_code": None}
    if t.line_id:
        ctx["line_code"] = (await get_line(s, t.line_id)).code
    if t.equipment_id:
        ctx["equipment_code"] = (await get_equipment(s, t.equipment_id)).code
    if t.work_order_id:
        ctx["erp_order_number"] = (await get_work_order_brief(s, t.work_order_id)).erp_order_number
    if t.terminal_id:
        ctx["terminal_code"] = (await get_terminal(s, t.terminal_id)).code
    return ctx


async def _activity(
    s: AsyncSession, t: Ticket, kind: str, text: str | None, *, actor: str | None, origin: str = "MES",
    data: dict[str, Any] | None = None, external_id: str | None = None, at: datetime | None = None,
) -> TicketActivity:
    a = TicketActivity(plant_id=t.plant_id, ticket_id=t.id, at=at or _now(), kind=kind, origin=origin,
                       actor_name=actor, text=text, data=data, external_id=external_id)
    s.add(a)
    await s.flush()
    return a


async def _assignee(s: AsyncSession, t: Ticket) -> dict[str, Any] | None:
    if not t.assignee_name:
        return None
    m = await s.get(SupportMember, t.assignee_id) if t.assignee_id else None
    return {"name": t.assignee_name, "username": m.username if m else None, "email": m.email if m else None}


async def _emit(
    s: AsyncSession, t: Ticket, event_type: str, changes: list[str], *, origin: str = "MES",
    comment: TicketActivity | None = None, demo: str | None = None,
) -> None:
    ctx = await _context(s, t)
    await messaging.publish(
        s, topic=Topics.MES_TICKETS, key=str(t.id), event_type=event_type,
        payload={
            "ticket_id": str(t.id), "plant_id": str(t.plant_id), "number": t.number, "title": t.title,
            "description": t.description, "category": t.category, "impact": t.impact, "severity": t.severity,
            "tier": t.tier, "status": t.status, "source": t.source, "reported_by": t.reported_by_name,
            "reported_at": t.reported_at.isoformat(), "resolution_note": t.resolution_note,
            "response_due_at": t.response_due_at.isoformat(),
            "resolution_due_at": t.resolution_due_at.isoformat() if t.resolution_due_at else None,
            "sn_sys_id": t.sn_sys_id, "sn_number": t.sn_number, "jira_key": t.jira_key, **ctx,
            "assignee": await _assignee(s, t), "changes": changes, "origin": origin,
            # demo data: "history" (backdated, already closed) or "current"; the AI agent leaves demo tickets alone
            # and the notifier ignores history
            **({"demo": demo} if demo else {}),
            "comment": {"id": str(comment.id), "text": comment.text, "author": comment.actor_name} if comment else None,
        },
    )


# ------------------------------------------------------------------------------------------------ create


async def create_ticket(
    s: AsyncSession, *, plant_id: UUID, reporter: tuple[str, str], title: str, description: str, category: str,
    impact: str, severity: str | None = None, source: str = "ADMIN", line_id: UUID | None = None,
    work_order_id: UUID | None = None, equipment_id: UUID | None = None, terminal_id: UUID | None = None,
    nonconformance_id: UUID | None = None, maintenance_order_id: UUID | None = None,
    integration_ref: str | None = None, client_event_id: UUID | None = None, reported_at: datetime | None = None,
) -> Ticket:
    if client_event_id:
        existing = (await s.scalars(select(Ticket).where(Ticket.client_event_id == client_event_id))).first()
        if existing:
            return existing
    if category not in CATEGORIES:
        raise ValidationFailed(f"unknown category {category}")
    if impact not in IMPACTS:
        raise ValidationFailed(f"unknown impact {impact}")
    if not title.strip():
        raise ValidationFailed("give the ticket a short title")
    suggested = domain.suggest_severity(impact, category)
    if severity is not None and severity not in domain.SEVERITY_TARGETS:
        raise ValidationFailed(f"unknown severity {severity}")
    sev = severity or suggested
    if equipment_id and line_id is None:
        line_id = (await get_equipment(s, equipment_id)).line_id
    if terminal_id and line_id is None:
        line_id = (await get_terminal(s, terminal_id)).line_id
    at = min(reported_at or _now(), _now())
    resp_due, fix_due = domain.due_dates(sev, at)
    seq = (await s.execute(select(TICKET_SEQ.next_value()))).scalar_one()
    t = Ticket(
        plant_id=plant_id, line_id=line_id, number=f"MES-{seq:06d}", title=title.strip()[:160],
        description=description.strip() or title.strip(), category=category, impact=impact, severity=sev,
        suggested_severity=suggested, tier=domain.initial_tier(category, source), status="NEW", source=source,
        reported_at=at, reported_by=reporter[0], reported_by_name=reporter[1], client_event_id=client_event_id,
        work_order_id=work_order_id, equipment_id=equipment_id, terminal_id=terminal_id,
        nonconformance_id=nonconformance_id, maintenance_order_id=maintenance_order_id, integration_ref=integration_ref,
        response_due_at=resp_due, resolution_due_at=fix_due, held_s=0, itsm_sync_status="PENDING",
    )
    s.add(t)
    await s.flush()
    note = f"severity {sev}" + (f" (suggested {suggested})" if sev != suggested else "") + f", tier {t.tier}"
    await _activity(s, t, "CREATED", note, actor=reporter[1], data={"severity": sev, "suggested": suggested})
    await _emit(s, t, EVENT_TICKET_CREATED, ["created"])
    return t


async def raise_automatic(
    s: AsyncSession, *, plant_id: UUID, title: str, description: str, integration_ref: str,
    category: str = "INTEGRATION", impact: str = "MINOR",
) -> Ticket:
    """A system alarm (e.g. SAP rejected a posting). One open ticket per cause: a repeat adds a comment."""
    existing = (await s.scalars(select(Ticket).where(Ticket.integration_ref == integration_ref,
                                                     Ticket.status.in_(OPEN_STATUSES)))).first()
    if existing:
        a = await _activity(s, existing, "COMMENT", f"Occurred again: {description}", actor=SYSTEM[1], origin="SYSTEM")
        await _emit(s, existing, EVENT_TICKET_UPDATED, ["comment"], comment=a)
        return existing
    return await create_ticket(
        s, plant_id=plant_id, reporter=SYSTEM, title=title, description=description, category=category, impact=impact,
        source="AUTOMATIC", integration_ref=integration_ref,
    )


# ------------------------------------------------------------------------------------------------ workflow


def _enter_status(t: Ticket, target: str, now: datetime) -> None:
    if t.status == "ON_HOLD" and t.on_hold_since:
        t.held_s += int((now - t.on_hold_since).total_seconds())
        t.on_hold_since = None
    if target == "ON_HOLD":
        t.on_hold_since = now
    if target in ("ACKNOWLEDGED", "IN_PROGRESS", "ON_HOLD", "RESOLVED") and t.acknowledged_at is None:
        t.acknowledged_at = now
    if target == "RESOLVED":
        t.resolved_at = now
    if target == "IN_PROGRESS" and t.status == "RESOLVED":  # reopened
        t.resolved_at = None
    if target in ("CLOSED", "CANCELLED"):
        t.closed_at = now
    t.status = target


async def change_status(
    s: AsyncSession, actor: str, ticket_id: UUID, target: str, note: str | None = None, *, origin: str = "MES",
) -> Ticket:
    t = await get_ticket(s, ticket_id, lock=True)
    if not domain.can_transition(t.status, target):
        raise DomainError(f"a {t.status.lower()} ticket can't go to {target.lower()}", code="invalid_transition")
    if target == "RESOLVED" and not (note and note.strip()):
        raise ValidationFailed("describe the resolution", code="resolution_required")
    old = t.status
    _enter_status(t, target, _now())
    if target == "RESOLVED":
        t.resolution_note = note.strip() if note else None
    await s.flush()
    await _activity(s, t, "STATUS", note, actor=actor, origin=origin, data={"from": old, "to": target})
    await _emit(s, t, EVENT_TICKET_UPDATED, ["status"])
    return t


async def set_severity(
    s: AsyncSession, actor: str, ticket_id: UUID, severity: str, reason: str, *, origin: str = "MES",
) -> Ticket:
    t = await get_ticket(s, ticket_id, lock=True)
    if severity not in domain.SEVERITY_TARGETS:
        raise ValidationFailed(f"unknown severity {severity}")
    if t.status not in OPEN_STATUSES:
        raise DomainError("only open tickets can be re-prioritised", code="ticket_closed")
    if not reason.strip():
        raise ValidationFailed("give a reason for the change")
    if severity == t.severity:
        return t
    old = t.severity
    t.severity = severity
    t.response_due_at, t.resolution_due_at = domain.due_dates(severity, t.reported_at)
    t.response_breached = t.resolution_breached = False
    await s.flush()
    await _activity(s, t, "SEVERITY", reason, actor=actor, origin=origin, data={"from": old, "to": severity})
    await _emit(s, t, EVENT_TICKET_UPDATED, ["severity"])
    return t


async def escalate(s: AsyncSession, actor: str, ticket_id: UUID, reason: str, *, origin: str = "MES") -> Ticket:
    """L1 -> L2 (technical escalation) -> L3 (code bug / architecture: a linked Jira issue is opened)."""
    t = await get_ticket(s, ticket_id, lock=True)
    if t.status not in OPEN_STATUSES:
        raise DomainError("only open tickets can be escalated", code="ticket_closed")
    nxt = domain.next_tier(t.tier)
    if nxt is None:
        raise DomainError("already at L3", code="max_tier")
    if not reason.strip():
        raise ValidationFailed("say why it needs the next tier")
    old = t.tier
    t.tier = nxt
    if t.status == "NEW":
        _enter_status(t, "IN_PROGRESS", _now())
    was = t.assignee_name
    # the next tier's team picks it up (ServiceNow also clears "Assigned to" when the group changes)
    t.assignee_id, t.assignee_name, t.assigned_at = None, None, None
    await s.flush()
    a = await _activity(s, t, "ESCALATED", reason, actor=actor, origin=origin,
                        data={"from": old, "to": nxt, **({"unassigned": was} if was else {})})
    # the reason travels with the change: a ServiceNow work note, and at L3 a comment on the new Jira issue
    await _emit(s, t, EVENT_TICKET_UPDATED, ["tier", "status", *(["assignee"] if was else [])], comment=a)
    return t


async def support_team(s: AsyncSession, plant_id: UUID, *, active_only: bool = True) -> list[SupportMember]:
    q = select(SupportMember).where(SupportMember.plant_id == plant_id)
    if active_only:
        q = q.where(SupportMember.active.is_(True))
    return list((await s.scalars(q.order_by(SupportMember.tier.nulls_last(), SupportMember.display_name))).all())


async def save_member(
    s: AsyncSession, plant_id: UUID, *, member_id: UUID | None, username: str, display_name: str, email: str | None,
    tier: str | None, active: bool,
) -> SupportMember:
    """Add or change a support engineer. Deactivated engineers keep their tickets but can't get new ones."""
    username, display_name = username.strip().lower(), display_name.strip()
    email = (email or "").strip().lower() or None
    if not username or not display_name:
        raise ValidationFailed("user name and name are required")
    if tier not in (None, "L1", "L2", "L3"):
        raise ValidationFailed(f"unknown tier {tier}")
    if email and ("@" not in email or " " in email):
        raise ValidationFailed(f"{email} is not an e-mail address")
    team = await support_team(s, plant_id, active_only=False)
    for other in team:
        if other.id == member_id:
            continue
        if other.username == username:
            raise ValidationFailed(f"user name {username} is already used by {other.display_name}", code="duplicate")
        if email and (other.email or "").lower() == email:
            raise ValidationFailed(f"e-mail {email} is already used by {other.display_name}", code="duplicate")
        if other.display_name.lower() == display_name.lower():
            raise ValidationFailed(f"there is already a {other.display_name}", code="duplicate")
    if member_id is None:
        m = SupportMember(plant_id=plant_id, username=username, display_name=display_name, email=email, tier=tier,
                          active=active)
        s.add(m)
    else:
        m = next((x for x in team if x.id == member_id), None)
        if m is None:
            raise NotFound("support engineer not found")
        renamed = m.display_name != display_name
        m.username, m.display_name, m.email, m.tier, m.active = username, display_name, email, tier, active
        if renamed:  # open tickets show the new name
            for t in (await s.scalars(select(Ticket).where(Ticket.assignee_id == m.id))).all():
                t.assignee_name = display_name
    await s.flush()
    return m


async def find_member(s: AsyncSession, plant_id: UUID, who: str) -> SupportMember | None:
    """A support engineer by user name, e-mail or (unique) display name, case-insensitive."""
    w = who.strip().lower()
    team = await support_team(s, plant_id)
    exact = [m for m in team if w in (m.username.lower(), (m.email or "").lower(), m.display_name.lower())]
    if exact:
        return exact[0]
    partial = [m for m in team if w and w in m.display_name.lower()]
    return partial[0] if len(partial) == 1 else None


async def assign(
    s: AsyncSession, actor: str, ticket_id: UUID, who: str | None, note: str | None = None, *, origin: str = "MES",
    at: datetime | None = None,
) -> Ticket:
    """Assign an open ticket to a support engineer (who = user name / e-mail / name), or unassign it (None)."""
    t = await get_ticket(s, ticket_id, lock=True)
    if t.status not in OPEN_STATUSES:
        raise DomainError("only open tickets can be assigned", code="ticket_closed")
    member = None
    if who:
        member = await find_member(s, t.plant_id, who)
        if member is None:
            raise ValidationFailed(f"{who} is not in the support team", code="unknown_assignee")
        if member.id == t.assignee_id:
            return t
    elif t.assignee_name is None:
        return t
    old = t.assignee_name
    t.assignee_id = member.id if member else None
    t.assignee_name = member.display_name if member else None
    t.assigned_at = (at or _now()) if member else None
    await s.flush()
    text = (f"Assigned to {member.display_name}" if member else "Unassigned") + (f" (was {old})" if old else "")
    a = await _activity(s, t, "ASSIGNED", text + (f": {note.strip()}" if note and note.strip() else ""), actor=actor,
                        origin=origin, data={"from": old, "to": t.assignee_name}, at=at)
    await _emit(s, t, EVENT_TICKET_UPDATED, ["assignee"], comment=a if note and note.strip() else None)
    return t


DETAIL_FIELDS = ("title", "description", "category", "impact", "equipment_id", "work_order_id")


async def update_details(
    s: AsyncSession, actor: str, ticket_id: UUID, changes: dict[str, Any], reason: str, *, origin: str = "MES",
) -> Ticket:
    """Correct what a ticket is about: title, description, category, impact, linked machine / order. The severity
    stays as set (a new impact only changes the suggested severity); ServiceNow gets the new text, Jira at L3 a comment."""
    t = await get_ticket(s, ticket_id, lock=True)
    if t.status not in OPEN_STATUSES:
        raise DomainError("only open tickets can be changed", code="ticket_closed")
    if not reason.strip():
        raise ValidationFailed("say why the details change")
    unknown = set(changes) - set(DETAIL_FIELDS)
    if unknown:
        raise ValidationFailed(f"can't change {', '.join(sorted(unknown))}")
    if "title" in changes and not (changes["title"] or "").strip():
        raise ValidationFailed("the title can't be empty")
    if "category" in changes and changes["category"] not in CATEGORIES:
        raise ValidationFailed(f"unknown category {changes['category']}")
    if "impact" in changes and changes["impact"] not in IMPACTS:
        raise ValidationFailed(f"unknown impact {changes['impact']}")
    before = await _context(s, t)
    changed: dict[str, dict[str, Any]] = {}
    for field in DETAIL_FIELDS:
        if field not in changes:
            continue
        new = changes[field].strip() if isinstance(changes[field], str) else changes[field]
        if field == "title":
            new = new[:160]
        if field == "description" and not new:
            new = t.title
        old = getattr(t, field)
        if new != old:
            setattr(t, field, new)
            changed[field] = {"from": str(old) if old is not None else None, "to": str(new) if new is not None else None}
    if not changed:
        return t
    if "impact" in changed or "category" in changed:
        t.suggested_severity = domain.suggest_severity(t.impact, t.category)
    if "equipment_id" in changed and t.equipment_id:
        t.line_id = (await get_equipment(s, t.equipment_id)).line_id
    await s.flush()
    after = await _context(s, t)
    for field, ctx_key in (("equipment_id", "equipment_code"), ("work_order_id", "erp_order_number")):
        if field in changed:
            changed[field] = {"from": before[ctx_key], "to": after[ctx_key]}
    names = {"equipment_id": "machine", "work_order_id": "order"}
    parts = [f"{names.get(f, f)}: {v['from'] or '-'} -> {v['to'] or '-'}" if f not in ("description",)
             else "description updated" for f, v in changed.items()]
    a = await _activity(s, t, "DETAILS", f"Details changed ({'; '.join(parts)}). Reason: {reason.strip()}",
                        actor=actor, origin=origin, data=changed)
    await _emit(s, t, EVENT_TICKET_UPDATED, ["details", *changed], comment=a)
    return t


async def add_comment(
    s: AsyncSession, actor: str, ticket_id: UUID, text: str, *, origin: str = "MES",
) -> TicketActivity:
    t = await get_ticket(s, ticket_id, lock=True)
    if not text.strip():
        raise ValidationFailed("empty comment")
    a = await _activity(s, t, "COMMENT", text.strip(), actor=actor, origin=origin)
    await _emit(s, t, EVENT_TICKET_UPDATED, ["comment"], comment=a)
    return a


# ------------------------------------------------------------------------------------------------ external systems


async def apply_sync_result(s: AsyncSession, payload: dict[str, Any]) -> None:
    """itsm-adapter delivered (or gave up on) a ticket change."""
    t = await s.get(Ticket, UUID(payload["ticket_id"]))
    if t is None:
        return
    if payload["status"] == "SYNCED":
        first_sn = t.sn_number is None and payload.get("sn_number")
        first_jira = t.jira_key is None and payload.get("jira_key")
        t.itsm_sync_status, t.itsm_error = "SYNCED", None
        t.sn_number = payload.get("sn_number") or t.sn_number
        t.sn_sys_id = payload.get("sn_sys_id") or t.sn_sys_id
        t.sn_state = payload.get("sn_state") or t.sn_state
        t.sn_group = payload.get("sn_group") or t.sn_group
        t.jira_key = payload.get("jira_key") or t.jira_key
        t.jira_status = payload.get("jira_status") or t.jira_status
        t.sn_url = payload.get("sn_url") or t.sn_url
        t.jira_url = payload.get("jira_url") or t.jira_url
        if first_sn:
            await _activity(s, t, "SYNC", f"ServiceNow incident {t.sn_number} ({t.sn_group})", actor=None, origin="SERVICENOW")
        if first_jira:
            await _activity(s, t, "SYNC", f"Jira issue {t.jira_key} opened for L3", actor=None, origin="JIRA")
    else:
        t.itsm_sync_status, t.itsm_error = "FAILED", payload.get("error")


async def apply_external(s: AsyncSession, payload: dict[str, Any]) -> None:
    """A change made in ServiceNow or Jira. Deduplicated by `external_id`; applied fields are echoed back on the
    event stream with origin != MES (the adapter only acts on a new L3 tier there)."""
    t = await s.get(Ticket, UUID(payload["ticket_id"]))
    if t is None:
        return
    ext = payload["external_id"]
    if (await s.scalars(select(TicketActivity.id).where(TicketActivity.external_id == ext))).first():
        return
    origin = payload["origin"]
    actor = payload.get("author")
    at = datetime.fromisoformat(payload["at"]) if payload.get("at") else _now()
    changes: list[str] = []
    notes: list[str] = []
    state = payload.get("sn_state")
    if state is not None:
        t.sn_state = state
        target = SN_STATES.get(state)
        if target == "IN_PROGRESS" and t.status == "NEW":
            target = "ACKNOWLEDGED"
        if target and target != t.status and domain.can_transition(t.status, target):
            old = t.status
            _enter_status(t, target, at)
            if target == "RESOLVED":
                t.resolution_note = payload.get("resolution_note") or t.resolution_note
            changes.append("status")
            notes.append(f"status {old.lower()} -> {target.lower()}")
    tier = payload.get("tier")
    if tier and tier != t.tier:
        notes.append(f"tier {t.tier} -> {tier}")
        t.tier = tier
        changes.append("tier")
    if payload.get("sn_group"):
        t.sn_group = payload["sn_group"]
    sev = payload.get("severity")
    if sev and sev != t.severity and sev in domain.SEVERITY_TARGETS:
        notes.append(f"severity {t.severity} -> {sev}")
        t.severity = sev
        t.response_due_at, t.resolution_due_at = domain.due_dates(sev, t.reported_at)
        changes.append("severity")
    if "assignee" in payload:  # ServiceNow "Assigned to" ({} = nobody)
        a_ = payload["assignee"] or {}
        member = None
        for key in (a_.get("email"), a_.get("user_name")):
            if key and member is None:
                member = await find_member(s, t.plant_id, key)
        name = member.display_name if member else a_.get("name") or None
        if name != t.assignee_name:
            notes.append(f"assigned to {name}" if name else "unassigned")
            t.assignee_id, t.assignee_name, t.assigned_at = (member.id if member else None), name, (at if name else None)
            changes.append("assignee")
    if payload.get("jira_status") and payload["jira_status"] != t.jira_status:
        notes.append(f"Jira {t.jira_key or payload.get('jira_key')}: {payload['jira_status']}")
        t.jira_status = payload["jira_status"]
    if payload.get("jira_key") and not t.jira_key:
        t.jira_key = payload["jira_key"]
    await s.flush()
    kind = "COMMENT" if payload.get("comment") and not notes else "EXTERNAL"
    text = "; ".join(notes + ([payload["comment"]] if payload.get("comment") else []))
    await _activity(s, t, kind, text or "updated", actor=actor, origin=origin, external_id=ext, at=at,
                    data={k: payload.get(k) for k in ("sn_state", "tier", "severity", "jira_status") if payload.get(k)})
    if changes:
        await _emit(s, t, EVENT_TICKET_UPDATED, changes, origin=origin)


# ------------------------------------------------------------------------------------------------ SLA


def clocks(t: Ticket, now: datetime | None = None) -> tuple[domain.Clock, domain.Clock]:
    now = now or _now()
    return (
        domain.response_clock(t.response_due_at, t.acknowledged_at, now),
        domain.resolution_clock(t.resolution_due_at, t.resolved_at, t.held_s, t.on_hold_since, now),
    )


async def check_sla(s: AsyncSession) -> int:
    """Worker job: flag response / resolution targets that are at risk (75% used) or breached; the notifier alerts
    the on-call people from these events."""
    now = _now()
    n = 0
    for t in (await s.scalars(select(Ticket).where(Ticket.status.in_(OPEN_STATUSES)).with_for_update(skip_locked=True))).all():
        resp, fix = clocks(t, now)
        for which, clock, warned in (("response", resp, "response_warned"), ("resolution", fix, "resolution_warned")):
            if domain.at_risk(clock, t.reported_at, now) and not getattr(t, warned):
                setattr(t, warned, True)
                left = int((clock.remaining_s or 0) // 60)
                await _activity(s, t, "SLA_AT_RISK", f"{which} target {t.severity} at risk: {left} min left "
                                f"(due {clock.due:%d %b %H:%M} UTC)", actor=None, origin="SYSTEM")
                await messaging.publish(s, topic=Topics.MES_TICKETS, key=str(t.id), event_type=EVENT_TICKET_SLA_AT_RISK,
                                        payload={"ticket_id": str(t.id), "number": t.number, "severity": t.severity,
                                                 "tier": t.tier, "which": which, "plant_id": str(t.plant_id),
                                                 "title": t.title, "due": clock.due.isoformat(), "minutes_left": left})
                n += 1
        for which, clock, flag in (("response", resp, "response_breached"), ("resolution", fix, "resolution_breached")):
            if clock.breached and not getattr(t, flag):
                setattr(t, flag, True)
                await _activity(s, t, "SLA_BREACH", f"{which} target {t.severity} missed (due {clock.due:%d %b %H:%M} UTC)",
                                actor=None, origin="SYSTEM")
                await messaging.publish(s, topic=Topics.MES_TICKETS, key=str(t.id), event_type=EVENT_TICKET_SLA_BREACHED,
                                        payload={"ticket_id": str(t.id), "number": t.number, "severity": t.severity,
                                                 "tier": t.tier, "which": which, "plant_id": str(t.plant_id),
                                                 "title": t.title, "due": clock.due.isoformat() if clock.due else None})
                n += 1
    return n


# ------------------------------------------------------------------------------------------------ search


async def search(s: AsyncSession, plant_id: UUID, q: str, *, scope: str = "all", limit: int = 10) -> list[Ticket]:
    """Tickets similar to a text (any of its words, ranked by relevance), or the ticket with that number."""
    number = q.strip().upper()
    words = [w for w in "".join(c if c.isalnum() else " " for c in q.lower()).split() if len(w) > 2][:24]
    conds = [Ticket.number == number]
    order = [(Ticket.number == number).desc()]
    if words:
        tsq = func.to_tsquery("english", " | ".join(words))
        doc = literal_column(SEARCH_DOCUMENT)
        conds.append(doc.op("@@")(tsq))
        order.append(func.ts_rank(doc, tsq).desc())
    stmt = select(Ticket).where(Ticket.plant_id == plant_id, or_(*conds))
    if scope == "open":
        stmt = stmt.where(Ticket.status.in_(OPEN_STATUSES))
    elif scope == "resolved":
        stmt = stmt.where(Ticket.status.in_(("RESOLVED", "CLOSED")))
    return list((await s.scalars(stmt.order_by(*order, Ticket.reported_at.desc()).limit(limit))).all())


# ------------------------------------------------------------------------------------------------ analytics


async def analytics(s: AsyncSession, plant_id: UUID, since: datetime) -> dict[str, Any]:
    """Support performance for a period (demo scale: computed in Python from the period's tickets)."""
    now = _now()
    tickets = list((await s.scalars(select(Ticket).where(Ticket.plant_id == plant_id, Ticket.reported_at >= since))).all())
    ids = [t.id for t in tickets]
    acts = list((await s.scalars(select(TicketActivity).where(TicketActivity.ticket_id.in_(ids)))).all()) if ids else []
    by_ticket: dict[UUID, list[TicketActivity]] = {}
    for a in acts:
        by_ticket.setdefault(a.ticket_id, []).append(a)
    days: dict[str, dict[str, int]] = {}
    day = since.date()
    while day <= now.date():
        days[day.isoformat()] = {"raised": 0, "resolved": 0}
        day += timedelta(days=1)
    sev: dict[str, dict[str, Any]] = {k: {"raised": 0, "resp": [], "fix": [], "mtta": [], "mttr": []}
                                      for k in domain.SEVERITY_TARGETS}
    cats: dict[str, int] = {}
    tiers: dict[str, int] = {}
    sources: dict[str, int] = {}
    equipment: dict[str, int] = {}
    resolved_by = {"AI": 0, "PEOPLE": 0, "SERVICENOW_JIRA": 0}
    ai_touched = 0
    for t in tickets:
        days.setdefault(t.reported_at.date().isoformat(), {"raised": 0, "resolved": 0})["raised"] += 1
        if t.resolved_at and t.resolved_at.date().isoformat() in days:
            days[t.resolved_at.date().isoformat()]["resolved"] += 1
        resp, fix = clocks(t, now)
        s_ = sev[t.severity]
        s_["raised"] += 1
        if t.status != "CANCELLED":
            if resp.met_at or resp.breached:
                s_["resp"].append(not resp.breached)
            if fix.due and (fix.met_at or fix.breached):
                s_["fix"].append(not fix.breached)
        if t.acknowledged_at:
            s_["mtta"].append((t.acknowledged_at - t.reported_at).total_seconds() / 60)
        if t.resolved_at:
            s_["mttr"].append(((t.resolved_at - t.reported_at).total_seconds() - (t.held_s or 0)) / 3600)
        cats[t.category] = cats.get(t.category, 0) + 1
        tiers[t.tier] = tiers.get(t.tier, 0) + 1
        sources[t.source] = sources.get(t.source, 0) + 1
        mine = by_ticket.get(t.id, [])
        if any(a.origin == "AI" for a in mine):
            ai_touched += 1
        fixed = [a for a in mine if (a.kind == "STATUS" and (a.data or {}).get("to") == "RESOLVED")
                 or (a.kind == "EXTERNAL" and (a.data or {}).get("sn_state") == "6")]
        if t.status in ("RESOLVED", "CLOSED") and fixed:
            last = max(fixed, key=lambda a: a.at)
            ai = last.origin == "AI" or AI_PROPOSED in (last.text or "")  # an AI proposal a person approved counts as AI
            key = "AI" if ai else "SERVICENOW_JIRA" if last.origin in ("SERVICENOW", "JIRA") else "PEOPLE"
            resolved_by[key] += 1
    if tickets:
        eq_ids = {t.equipment_id for t in tickets if t.equipment_id}
        codes = {e: (await get_equipment(s, e)).code for e in eq_ids}
        for t in tickets:
            if t.equipment_id:
                equipment[codes[t.equipment_id]] = equipment.get(codes[t.equipment_id], 0) + 1

    def pct(xs: list[bool]) -> float | None:
        return round(100 * sum(xs) / len(xs), 1) if xs else None

    def avg(xs: list[float]) -> float | None:
        return round(sum(xs) / len(xs), 1) if xs else None

    open_now = (await s.execute(select(func.count()).select_from(Ticket).where(
        Ticket.plant_id == plant_id, Ticket.status.in_(OPEN_STATUSES)))).scalar_one()
    all_resp = [x for v in sev.values() for x in v["resp"]]
    all_fix = [x for v in sev.values() for x in v["fix"]]
    return {
        "since": since, "until": now,
        "totals": {"raised": len(tickets), "resolved": sum(1 for t in tickets if t.resolved_at), "open_now": open_now,
                   "response_met_pct": pct(all_resp), "resolution_met_pct": pct(all_fix),
                   "ai_touched_pct": round(100 * ai_touched / len(tickets), 1) if tickets else None},
        "by_day": [{"day": d, **v} for d, v in sorted(days.items())],
        "by_severity": [{"severity": k, "raised": v["raised"], "response_met_pct": pct(v["resp"]),
                         "resolution_met_pct": pct(v["fix"]), "mtta_min": avg(v["mtta"]), "mttr_h": avg(v["mttr"])}
                        for k, v in sev.items()],
        "by_category": sorted(({"key": k, "count": v} for k, v in cats.items()), key=lambda x: -x["count"]),
        "by_tier": sorted(({"key": k, "count": v} for k, v in tiers.items()), key=lambda x: x["key"]),
        "by_source": sorted(({"key": k, "count": v} for k, v in sources.items()), key=lambda x: -x["count"]),
        "top_equipment": sorted(({"key": k, "count": v} for k, v in equipment.items()), key=lambda x: -x["count"])[:8],
        "resolved_by": resolved_by,
    }
