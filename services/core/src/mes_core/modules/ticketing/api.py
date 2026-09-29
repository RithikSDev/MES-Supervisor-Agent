from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mes_core.modules.ticketing import domain, reports
from mes_core.modules.ticketing import service as tk
from mes_core.modules.ticketing.models import (
    CATEGORIES,
    IMPACTS,
    OPEN_STATUSES,
    STATUSES,
    Ticket,
    TicketActivity,
)
from mes_core.shared.errors import ValidationFailed
from mes_core.shared.permissions import Perm
from mes_core.web.deps import DbDep, request_id, require

router = APIRouter(tags=["tickets"])

Severity = Literal["P1", "P2", "P3", "P4"]


class SeverityInfo(BaseModel):
    code: str
    label: str
    response_min: int
    resolution_h: float | None


class Labelled(BaseModel):
    code: str
    label: str


class TierInfo(BaseModel):
    code: str
    label: str
    team: str
    target: str


class TicketMeta(BaseModel):
    severities: list[SeverityInfo]
    impacts: list[Labelled]
    impact_severity: dict[str, str]
    categories: list[Labelled]
    tiers: list[TierInfo]
    statuses: list[str]
    transitions: dict[str, list[str]]


class ClockOut(BaseModel):
    due: datetime | None
    met_at: datetime | None
    remaining_s: float | None
    breached: bool
    paused: bool


class TicketOut(BaseModel):
    id: UUID
    plant_id: UUID
    number: str
    title: str
    description: str
    category: str
    impact: str
    severity: str
    suggested_severity: str
    tier: str
    status: str
    source: str
    reported_at: datetime
    reported_by_name: str | None
    line_code: str | None
    equipment_code: str | None
    erp_order_number: str | None
    terminal_code: str | None
    work_order_id: UUID | None
    equipment_id: UUID | None
    nonconformance_id: UUID | None
    maintenance_order_id: UUID | None
    integration_ref: str | None
    acknowledged_at: datetime | None
    resolved_at: datetime | None
    closed_at: datetime | None
    resolution_note: str | None
    response: ClockOut
    resolution: ClockOut
    itsm_sync_status: str
    itsm_error: str | None
    sn_number: str | None
    sn_sys_id: str | None
    sn_state: str | None
    sn_group: str | None
    jira_key: str | None
    jira_status: str | None
    sn_url: str | None
    jira_url: str | None
    assignee_id: UUID | None
    assignee_name: str | None
    assigned_at: datetime | None


class SupportMemberOut(BaseModel):
    id: UUID
    username: str
    display_name: str
    email: str | None
    tier: str | None
    active: bool
    open_tickets: int


class SupportMemberIn(BaseModel):
    plant_id: UUID
    username: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(None, max_length=200)
    tier: Literal["L1", "L2", "L3"] | None = None
    active: bool = True


class AssignIn(BaseModel):
    assignee: str | None = Field(None, description="User name, e-mail or name of a support team member; null = unassign")
    note: str | None = None


class ActivityOut(BaseModel):
    id: UUID
    at: datetime
    kind: str
    origin: str
    actor_name: str | None
    text: str | None
    data: dict[str, Any] | None


class TicketDetail(TicketOut):
    activity: list[ActivityOut]


class TicketIn(BaseModel):
    plant_id: UUID
    title: str = Field(min_length=1, max_length=160)
    description: str = ""
    category: Literal[CATEGORIES]  # type: ignore[valid-type]
    impact: Literal[IMPACTS]  # type: ignore[valid-type]
    severity: Severity | None = Field(None, description="Override of the suggested severity")
    line_id: UUID | None = None
    work_order_id: UUID | None = None
    equipment_id: UUID | None = None
    nonconformance_id: UUID | None = None
    maintenance_order_id: UUID | None = None
    client_event_id: UUID | None = None
    occurred_at: datetime | None = None


class StatusIn(BaseModel):
    status: Literal[STATUSES]  # type: ignore[valid-type]
    note: str | None = None


class SeverityIn(BaseModel):
    severity: Severity
    reason: str = Field(min_length=1)


class ReasonIn(BaseModel):
    reason: str = Field(min_length=1)


class DetailsIn(BaseModel):
    """Only the fields sent are changed (send null to unlink the machine / order)."""
    title: str | None = Field(None, max_length=160)
    description: str | None = None
    category: Literal[CATEGORIES] | None = None  # type: ignore[valid-type]
    impact: Literal[IMPACTS] | None = None  # type: ignore[valid-type]
    equipment_id: UUID | None = None
    work_order_id: UUID | None = None
    reason: str = Field(min_length=1)


class CommentIn(BaseModel):
    text: str = Field(min_length=1)
    client_event_id: UUID | None = None
    occurred_at: datetime | None = None


def _clock(c: domain.Clock) -> dict[str, Any]:
    return {"due": c.due, "met_at": c.met_at, "remaining_s": c.remaining_s, "breached": c.breached, "paused": c.paused}


async def _rows(s: AsyncSession, tickets: list[Ticket]) -> list[dict[str, Any]]:
    out = []
    for t in tickets:
        resp, fix = tk.clocks(t)
        out.append({**{c.key: getattr(t, c.key) for c in Ticket.__table__.columns}, **await tk._context(s, t),
                    "response": _clock(resp), "resolution": _clock(fix)})
    return out


async def _detail(s: AsyncSession, t: Ticket) -> dict[str, Any]:
    acts = (await s.scalars(select(TicketActivity).where(TicketActivity.ticket_id == t.id).order_by(TicketActivity.at))).all()
    return {**(await _rows(s, [t]))[0], "activity": [{c.key: getattr(a, c.key) for c in TicketActivity.__table__.columns} for a in acts]}


def _actor(user) -> str:  # type: ignore[no-untyped-def]
    return user.display_name or user.username


AI_ROLES = {"ai_agent", "ai_support_agent"}


def _origin(user) -> str:  # type: ignore[no-untyped-def]
    """Activity by an AI agent's service account is shown (and counted) as AI; everything else as MES."""
    return "AI" if AI_ROLES & set(user.roles) else "MES"


@router.get("/tickets/meta", response_model=TicketMeta)
async def ticket_meta(user=require(Perm.TICKET_READ)) -> dict:  # type: ignore[no-untyped-def]
    return {
        "severities": [{"code": k, "label": domain.SEVERITY_LABELS[k], "response_min": int(r.total_seconds() // 60),
                        "resolution_h": f.total_seconds() / 3600 if f else None} for k, (r, f) in domain.SEVERITY_TARGETS.items()],
        "impacts": [{"code": k, "label": v} for k, v in domain.IMPACT_LABELS.items()],
        "impact_severity": domain.IMPACT_SEVERITY,
        "categories": [{"code": k, "label": v} for k, v in domain.CATEGORY_LABELS.items()],
        "tiers": [{"code": k, "label": a, "team": b, "target": c} for k, (a, b, c) in domain.TIERS.items()],
        "statuses": list(STATUSES),
        "transitions": {k: sorted(v) for k, v in domain.TRANSITIONS.items()},
    }


@router.get("/tickets", response_model=list[TicketOut])
async def list_tickets(  # type: ignore[no-untyped-def]
    plant_id: UUID, db: DbDep, user=require(Perm.TICKET_READ),
    status: Annotated[list[str] | None, Query()] = None, severity: Annotated[list[str] | None, Query()] = None,
    tier: Annotated[list[str] | None, Query()] = None, open_only: bool = False, equipment_id: UUID | None = None,
    work_order_id: UUID | None = None, assignee_id: UUID | None = None, unassigned: bool = False,
    limit: int = Query(200, le=1000),
) -> list:
    user.ensure_plant(plant_id)
    q = select(Ticket).where(Ticket.plant_id == plant_id).order_by(Ticket.severity, Ticket.reported_at.desc()).limit(limit)
    if open_only:
        q = q.where(Ticket.status.in_(OPEN_STATUSES))
    if status:
        q = q.where(Ticket.status.in_(status))
    if severity:
        q = q.where(Ticket.severity.in_(severity))
    if tier:
        q = q.where(Ticket.tier.in_(tier))
    if equipment_id:
        q = q.where(Ticket.equipment_id == equipment_id)
    if work_order_id:
        q = q.where(Ticket.work_order_id == work_order_id)
    if assignee_id:
        q = q.where(Ticket.assignee_id == assignee_id)
    if unassigned:
        q = q.where(Ticket.assignee_name.is_(None))
    async with db.read() as s:
        return await _rows(s, list((await s.scalars(q)).all()))


@router.get("/tickets/analytics")
async def ticket_analytics(  # type: ignore[no-untyped-def]
    plant_id: UUID, db: DbDep, user=require(Perm.TICKET_READ), days: int = Query(30, ge=1, le=365),
) -> dict:
    """Support performance for the last `days`: volumes, SLA compliance, MTTA/MTTR per severity, categories,
    recurring machines, and who resolved (AI, people, ServiceNow/Jira)."""
    from datetime import UTC, datetime, timedelta

    user.ensure_plant(plant_id)
    since = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    async with db.read() as s:
        return await tk.analytics(s, plant_id, since)


@router.get("/tickets/forecast")
async def ticket_forecast(  # type: ignore[no-untyped-def]
    plant_id: UUID, db: DbDep, user=require(Perm.TICKET_READ),
    horizon_days: int = Query(7, ge=1, le=reports.MAX_HORIZON_DAYS),
    weeks: int = Query(8, ge=3, le=26),
) -> dict:
    """Expected new tickets per day for the next `horizon_days` (with an 80% range), from the last `weeks` weeks:
    trend, weekday pattern, severity / category mix, backlog outlook and a backtest on last week."""
    user.ensure_plant(plant_id)
    async with db.read() as s:
        return await reports.forecast(s, plant_id, horizon_days=horizon_days, weeks=weeks)


@router.get("/tickets/sla-report")
async def ticket_sla_report(  # type: ignore[no-untyped-def]
    plant_id: UUID, db: DbDep, user=require(Perm.TICKET_READ), days: int = Query(7, ge=1, le=90),
    since: datetime | None = None, until: datetime | None = None,
) -> dict:
    """SLA breaches (targets that fell due in the period and were missed) and exits (tickets resolved, closed or
    cancelled in the period, met or missed), per severity and assignee, plus what is at risk now."""
    from datetime import UTC, timedelta

    user.ensure_plant(plant_id)
    end = until or datetime.now(UTC)
    start = since or end - timedelta(days=days)
    if start >= end:
        raise ValidationFailed("since must be before until")
    async with db.read() as s:
        return await reports.sla_report(s, plant_id, start, end)


@router.get("/tickets/support-team", response_model=list[SupportMemberOut])
async def support_team(  # type: ignore[no-untyped-def]
    plant_id: UUID, db: DbDep, user=require(Perm.TICKET_READ), include_inactive: bool = False,
) -> list:
    """The support engineers tickets can be assigned to, with their open-ticket count."""
    user.ensure_plant(plant_id)
    async with db.read() as s:
        return await _team_out(s, plant_id, include_inactive)


async def _team_out(s: AsyncSession, plant_id: UUID, include_inactive: bool) -> list[dict[str, Any]]:
    team = await tk.support_team(s, plant_id, active_only=not include_inactive)
    counts: dict[Any, int] = {r[0]: r[1] for r in (await s.execute(select(Ticket.assignee_id, func.count()).where(
        Ticket.plant_id == plant_id, Ticket.status.in_(OPEN_STATUSES), Ticket.assignee_id.is_not(None))
        .group_by(Ticket.assignee_id))).all()}
    return [{**{c.key: getattr(m, c.key) for c in m.__table__.columns}, "open_tickets": counts.get(m.id, 0)}
            for m in team]


@router.post("/tickets/support-team", response_model=SupportMemberOut, status_code=201)
async def add_support_member(body: SupportMemberIn, request: Request, db: DbDep, user=require(Perm.SUPPORT_TEAM)) -> dict:  # type: ignore[no-untyped-def]
    """Add a support engineer (matched to their ServiceNow user by e-mail, then user name)."""
    user.ensure_plant(body.plant_id)
    async with db.unit_of_work(user.actor, request_id=request_id(request), reason="support team: engineer added") as s:
        m = await tk.save_member(s, body.plant_id, member_id=None, **body.model_dump(exclude={"plant_id"}))
        return next(x for x in await _team_out(s, body.plant_id, True) if x["id"] == m.id)


@router.put("/tickets/support-team/{member_id}", response_model=SupportMemberOut)
async def update_support_member(member_id: UUID, body: SupportMemberIn, request: Request, db: DbDep,  # type: ignore[no-untyped-def]
                                user=require(Perm.SUPPORT_TEAM)) -> dict:
    """Change a support engineer, or (active=false) take them off the team; their tickets stay assigned."""
    user.ensure_plant(body.plant_id)
    async with db.unit_of_work(user.actor, request_id=request_id(request), reason="support team: engineer changed") as s:
        m = await tk.save_member(s, body.plant_id, member_id=member_id, **body.model_dump(exclude={"plant_id"}))
        return next(x for x in await _team_out(s, body.plant_id, True) if x["id"] == m.id)


@router.get("/tickets/search", response_model=list[TicketOut])
async def search_tickets(  # type: ignore[no-untyped-def]
    plant_id: UUID, q: str, db: DbDep, user=require(Perm.TICKET_READ),
    scope: Literal["all", "open", "resolved"] = "all", limit: int = Query(10, le=50),
) -> list:
    """Tickets similar to a text (full-text, any word, ranked), or the one with that number."""
    user.ensure_plant(plant_id)
    async with db.read() as s:
        return await _rows(s, await tk.search(s, plant_id, q, scope=scope, limit=limit))


@router.get("/tickets/{ticket_id}", response_model=TicketDetail)
async def get_ticket(ticket_id: UUID, db: DbDep, user=require(Perm.TICKET_READ)) -> dict:  # type: ignore[no-untyped-def]
    async with db.read() as s:
        t = await tk.get_ticket(s, ticket_id)
        user.ensure_plant(t.plant_id)
        return await _detail(s, t)


@router.post("/tickets", response_model=TicketDetail, status_code=201)
async def create_ticket(body: TicketIn, request: Request, db: DbDep, user=require(Perm.TICKET_CREATE)) -> dict:  # type: ignore[no-untyped-def]
    """Raise a support ticket. From a terminal the terminal and its line are attached; the severity is suggested from
    the impact (the reporter may override it)."""
    user.ensure_plant(body.plant_id)
    source = "TERMINAL" if user.terminal_id else ("MCP" if "ai_agent" in user.roles else "ADMIN")
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        t = await tk.create_ticket(
            s, plant_id=body.plant_id, reporter=(str(user.id), _actor(user)), title=body.title,
            description=body.description, category=body.category, impact=body.impact, severity=body.severity,
            source=source, line_id=body.line_id, work_order_id=body.work_order_id, equipment_id=body.equipment_id,
            terminal_id=user.terminal_id, nonconformance_id=body.nonconformance_id,
            maintenance_order_id=body.maintenance_order_id, client_event_id=body.client_event_id,
            reported_at=body.occurred_at,
        )
        return await _detail(s, t)


async def _act(request: Request, db, user, ticket_id: UUID, fn, reason: str | None = None) -> dict:  # type: ignore[no-untyped-def]
    async with db.unit_of_work(user.actor, request_id=request_id(request), reason=reason) as s:
        t = await tk.get_ticket(s, ticket_id)
        user.ensure_plant(t.plant_id)
        await fn(s)
        return await _detail(s, await tk.get_ticket(s, ticket_id))


@router.post("/tickets/{ticket_id}/status", response_model=TicketDetail)
async def set_status(ticket_id: UUID, body: StatusIn, request: Request, db: DbDep, user=require(Perm.TICKET_MANAGE)) -> dict:  # type: ignore[no-untyped-def]
    return await _act(request, db, user, ticket_id, lambda s: tk.change_status(s, _actor(user), ticket_id, body.status, body.note, origin=_origin(user)),
                      body.note)


@router.post("/tickets/{ticket_id}/severity", response_model=TicketDetail)
async def set_severity(ticket_id: UUID, body: SeverityIn, request: Request, db: DbDep, user=require(Perm.TICKET_MANAGE)) -> dict:  # type: ignore[no-untyped-def]
    return await _act(request, db, user, ticket_id, lambda s: tk.set_severity(s, _actor(user), ticket_id, body.severity, body.reason,
                                                                      origin=_origin(user)), body.reason)


@router.post("/tickets/{ticket_id}/escalate", response_model=TicketDetail)
async def escalate(ticket_id: UUID, body: ReasonIn, request: Request, db: DbDep, user=require(Perm.TICKET_MANAGE)) -> dict:  # type: ignore[no-untyped-def]
    return await _act(request, db, user, ticket_id, lambda s: tk.escalate(s, _actor(user), ticket_id, body.reason, origin=_origin(user)),
                      body.reason)


@router.post("/tickets/{ticket_id}/assign", response_model=TicketDetail)
async def assign(ticket_id: UUID, body: AssignIn, request: Request, db: DbDep, user=require(Perm.TICKET_MANAGE)) -> dict:  # type: ignore[no-untyped-def]
    """Assign an open ticket to a support engineer (ServiceNow "Assigned to"), or unassign it."""
    return await _act(request, db, user, ticket_id, lambda s: tk.assign(s, _actor(user), ticket_id, body.assignee, body.note,
                                                                origin=_origin(user)), body.note)


@router.post("/tickets/{ticket_id}/details", response_model=TicketDetail)
async def set_details(ticket_id: UUID, body: DetailsIn, request: Request, db: DbDep, user=require(Perm.TICKET_MANAGE)) -> dict:  # type: ignore[no-untyped-def]
    """Correct title, description, category, impact or the linked machine / order of an open ticket (audited;
    ServiceNow and Jira are updated)."""
    changes = {k: getattr(body, k) for k in body.model_fields_set if k != "reason"}
    for k in ("title", "category", "impact"):
        if k in changes and changes[k] is None:
            raise ValidationFailed(f"{k} can't be empty")
    return await _act(request, db, user, ticket_id, lambda s: tk.update_details(s, _actor(user), ticket_id, changes,
                                                                        body.reason, origin=_origin(user)), body.reason)


@router.post("/tickets/{ticket_id}/comments", response_model=TicketDetail, status_code=201)
async def comment(ticket_id: UUID, body: CommentIn, request: Request, db: DbDep, user=require(Perm.TICKET_CREATE)) -> dict:  # type: ignore[no-untyped-def]
    if not body.text.strip():
        raise ValidationFailed("empty comment")
    return await _act(request, db, user, ticket_id, lambda s: tk.add_comment(s, _actor(user), ticket_id, body.text, origin=_origin(user)))
