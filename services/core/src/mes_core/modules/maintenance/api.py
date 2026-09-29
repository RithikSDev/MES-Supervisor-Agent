from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mes_core.modules.execution.public import equipment_runtime_hours, state_for_equipment
from mes_core.modules.inventory.public import available_by_material
from mes_core.modules.maintenance import domain
from mes_core.modules.maintenance import service as m
from mes_core.modules.maintenance.models import DowntimeEvent, MaintenanceOrder, MaintenancePlan, Notification
from mes_core.modules.masterdata.public import get_materials, search_material_ids
from mes_core.modules.org.public import get_equipment, list_equipment
from mes_core.shared.errors import NotFound, ValidationFailed
from mes_core.shared.permissions import Perm
from mes_core.web.deps import DbDep, request_id, require

router = APIRouter(tags=["maintenance"])


# ------------------------------------------------------------------------------------------------ models


class MaintCodeLabel(BaseModel):
    code: str
    label: str


class MaintenanceMeta(BaseModel):
    downtime_reasons: list[MaintCodeLabel]


class Reliability(BaseModel):
    failures: int
    downtime_h: float
    mttr_h: float | None
    mtbf_h: float | None
    runtime_h: float


class OpenDowntime(BaseModel):
    id: UUID
    started_at: datetime
    reason_code: str
    planned: bool
    description: str | None
    reported_by_name: str | None


class EquipmentHealth(BaseModel):
    id: UUID
    code: str
    name: str
    equipment_type: str
    status: str
    line_id: UUID
    line_code: str
    work_center_code: str
    open_downtime: OpenDowntime | None
    open_notifications: int
    open_orders: int
    next_preventive: datetime | None
    reliability: Reliability


class NotificationOut(BaseModel):
    id: UUID
    plant_id: UUID
    number: str
    notif_type: str
    priority: int
    status: str
    equipment_id: UUID
    equipment_code: str
    line_code: str
    reason_code: str
    description: str
    machine_down: bool
    reported_at: datetime
    reported_by_name: str | None
    completed_at: datetime | None
    maintenance_order_id: UUID | None
    maintenance_order_number: str | None
    erp_sync_status: str
    erp_error: str | None


class ChecklistItem(BaseModel):
    text: str
    done: bool = False
    note: str | None = None


class PartOut(BaseModel):
    movement_id: UUID
    material_number: str
    material_description: str
    quantity: Decimal
    uom: str
    posted_at: datetime
    erp_sync_status: str


class MaintOrderOut(BaseModel):
    id: UUID
    plant_id: UUID
    number: str
    order_type: str
    status: str
    priority: int
    description: str
    equipment_id: UUID
    equipment_code: str
    line_code: str
    notification_id: UUID | None
    notification_number: str | None
    plan_id: UUID | None
    planned_start: datetime
    planned_duration_min: int
    started_at: datetime | None
    completed_at: datetime | None
    technician_name: str | None
    labor_min: Decimal
    checklist: list[ChecklistItem]
    findings: str | None
    erp_sync_status: str
    erp_error: str | None
    erp_system_status: str | None
    overdue: bool


class MaintOrderDetail(MaintOrderOut):
    parts: list[PartOut]
    notification: NotificationOut | None


class DowntimeOut(BaseModel):
    id: UUID
    equipment_id: UUID
    equipment_code: str
    line_code: str
    started_at: datetime
    ended_at: datetime | None
    duration_min: float
    planned: bool
    reason_code: str
    description: str | None
    reported_by_name: str | None
    ended_by_name: str | None
    notification_number: str | None
    maintenance_order_number: str | None


class MaintPlanOut(BaseModel):
    id: UUID
    plant_id: UUID
    equipment_id: UUID
    equipment_code: str
    name: str
    interval_type: str
    interval_days: int | None
    interval_hours: Decimal | None
    lead_days: int
    duration_min: int
    checklist: list[str]
    active: bool
    last_done_at: datetime | None
    next_due_at: datetime | None
    runtime_since_last_h: float | None
    open_order_number: str | None


class MaintPlanIn(BaseModel):
    plant_id: UUID
    equipment_id: UUID
    name: str = Field(min_length=1)
    interval_type: Literal["CALENDAR", "RUNTIME"]
    interval_days: int | None = Field(None, gt=0)
    interval_hours: Decimal | None = Field(None, gt=0)
    lead_days: int = Field(3, ge=0, le=60)
    duration_min: int = Field(60, gt=0, le=24 * 60)
    checklist: list[str] = Field(min_length=1)
    active: bool = True


class BreakdownIn(BaseModel):
    equipment_id: UUID
    reason_code: str
    description: str = ""
    machine_down: bool = True
    client_event_id: UUID | None = None
    occurred_at: datetime | None = None


class RequestIn(BaseModel):
    equipment_id: UUID
    description: str = Field(min_length=1)
    priority: int = Field(3, ge=1, le=4)


class BackUpIn(BaseModel):
    note: str = ""


class CreateOrderIn(BaseModel):
    checklist: list[str] | None = None
    duration_min: int = Field(120, gt=0, le=24 * 60)


class WorkIn(BaseModel):
    checklist: list[ChecklistItem]
    labor_min: Decimal | None = Field(None, ge=0)


class PartIn(BaseModel):
    material_id: UUID
    quantity: Decimal = Field(gt=0)
    client_event_id: UUID | None = None
    occurred_at: datetime | None = None


class CompleteIn(BaseModel):
    findings: str = Field(min_length=1)
    reason: str = Field(min_length=1, description="E-signature reason (meaning RELEASED)")


class SparePart(BaseModel):
    material_id: UUID
    material_number: str
    description: str
    uom: str
    in_stores: Decimal


# ------------------------------------------------------------------------------------------------ row builders


async def _eq_codes(s: AsyncSession, plant_id: UUID) -> dict[UUID, Any]:
    return {e.id: e for e in await list_equipment(s, plant_id)}


async def _notification_rows(s: AsyncSession, plant_id: UUID, rows: list[Notification]) -> list[dict[str, Any]]:
    eqs = await _eq_codes(s, plant_id)
    orders = {o.notification_id: o for o in (await s.scalars(select(MaintenanceOrder).where(
        MaintenanceOrder.notification_id.in_([n.id for n in rows])))).all()} if rows else {}
    out = []
    for n in rows:
        eq = eqs.get(n.equipment_id)
        mo = orders.get(n.id)
        out.append({**{c.key: getattr(n, c.key) for c in Notification.__table__.columns},
                    "equipment_code": eq.code if eq else "?", "line_code": eq.line_code if eq else "?",
                    "maintenance_order_id": mo.id if mo else None, "maintenance_order_number": mo.number if mo else None})
    return out


async def _order_rows(s: AsyncSession, plant_id: UUID, rows: list[MaintenanceOrder]) -> list[dict[str, Any]]:
    eqs = await _eq_codes(s, plant_id)
    notifs = {n.id: n.number for n in (await s.scalars(select(Notification).where(
        Notification.id.in_([o.notification_id for o in rows if o.notification_id])))).all()} if rows else {}
    now = datetime.now(UTC)
    out = []
    for o in rows:
        eq = eqs.get(o.equipment_id)
        out.append({**{c.key: getattr(o, c.key) for c in MaintenanceOrder.__table__.columns},
                    "equipment_code": eq.code if eq else "?", "line_code": eq.line_code if eq else "?",
                    "notification_number": notifs.get(o.notification_id) if o.notification_id else None,
                    "overdue": o.status == "CREATED" and o.planned_start + timedelta(minutes=o.planned_duration_min) < now})
    return out


async def _order_detail(s: AsyncSession, mo: MaintenanceOrder) -> dict[str, Any]:
    row = (await _order_rows(s, mo.plant_id, [mo]))[0]
    notif = None
    if mo.notification_id:
        notif = (await _notification_rows(s, mo.plant_id, [await m.get_notification(s, mo.notification_id)]))[0]
    return {**row, "parts": await m.parts_of_order(s, mo), "notification": notif}


async def _push(request: Request, db, plant_id: UUID, equipment_id: UUID) -> None:  # type: ignore[no-untyped-def]
    async with db.read() as s:
        state = await state_for_equipment(s, equipment_id)
    await request.app.state.live.push(plant_id, state)


# ------------------------------------------------------------------------------------------------ endpoints


@router.get("/maintenance/meta", response_model=MaintenanceMeta)
async def meta(user=require(Perm.MAINT_READ)) -> dict:  # type: ignore[no-untyped-def]
    return {"downtime_reasons": [{"code": k, "label": v} for k, v in domain.DOWNTIME_REASONS.items()]}


@router.get("/maintenance/equipment", response_model=list[EquipmentHealth])
async def equipment_health(plant_id: UUID, db: DbDep, days: int = Query(30, ge=1, le=365), user=require(Perm.MAINT_READ)) -> list:  # type: ignore[no-untyped-def]
    user.ensure_plant(plant_id)
    async with db.read() as s:
        eqs = await list_equipment(s, plant_id)
        rel = await m.reliability(s, plant_id, days)
        runtime = await equipment_runtime_hours(s, [e.id for e in eqs], datetime.now(UTC) - timedelta(days=days))
        downs = {d.equipment_id: d for d in (await s.scalars(select(DowntimeEvent).where(
            DowntimeEvent.plant_id == plant_id, DowntimeEvent.ended_at.is_(None)))).all()}
        notifs = (await s.scalars(select(Notification.equipment_id).where(
            Notification.plant_id == plant_id, Notification.status == "OPEN"))).all()
        orders = (await s.scalars(select(MaintenanceOrder).where(
            MaintenanceOrder.plant_id == plant_id, MaintenanceOrder.status.in_(m.OPEN_ORDER)))).all()
    out = []
    for e in eqs:
        d = downs.get(e.id)
        r = rel[e.id]
        mine = [o for o in orders if o.equipment_id == e.id]
        prev = [o.planned_start for o in mine if o.order_type == "PREVENTIVE"]
        out.append({
            "id": e.id, "code": e.code, "name": e.name, "equipment_type": e.equipment_type, "status": e.status,
            "line_id": e.line_id, "line_code": e.line_code, "work_center_code": e.work_center_code,
            "open_downtime": {c: getattr(d, c) for c in ("id", "started_at", "reason_code", "planned", "description",
                                                         "reported_by_name")} if d else None,
            "open_notifications": sum(1 for x in notifs if x == e.id), "open_orders": len(mine),
            "next_preventive": min(prev) if prev else None,
            "reliability": {"failures": r.failures, "downtime_h": r.downtime_h, "mttr_h": r.mttr_h, "mtbf_h": r.mtbf_h,
                            "runtime_h": round(runtime.get(e.id, 0.0), 2)},
        })
    return out


@router.post("/maintenance/breakdowns", response_model=NotificationOut, status_code=201)
async def report_breakdown(body: BreakdownIn, request: Request, db: DbDep, user=require(Perm.MAINT_REPORT)) -> dict:  # type: ignore[no-untyped-def]
    """Operator: "the machine has a problem". Machine down = run paused, downtime starts, machine unavailable."""
    async with db.unit_of_work(user.actor, request_id=request_id(request), reason=body.description or None) as s:
        n = await m.report_breakdown(
            s, user, equipment_id=body.equipment_id, reason_code=body.reason_code, description=body.description,
            machine_down=body.machine_down, client_event_id=body.client_event_id, occurred_at=body.occurred_at,
        )
        row = (await _notification_rows(s, n.plant_id, [n]))[0]
    await _push(request, db, n.plant_id, n.equipment_id)
    return row


@router.post("/maintenance/requests", response_model=NotificationOut, status_code=201)
async def create_request(body: RequestIn, request: Request, db: DbDep, user=require(Perm.MAINT_REPORT)) -> dict:  # type: ignore[no-untyped-def]
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        n = await m.create_request(s, user, equipment_id=body.equipment_id, description=body.description, priority=body.priority)
        return (await _notification_rows(s, n.plant_id, [n]))[0]


@router.post("/maintenance/equipment/{equipment_id}/back-up", status_code=204)
async def back_up(equipment_id: UUID, body: BackUpIn, request: Request, db: DbDep, user=require(Perm.MAINT_REPORT)) -> None:  # type: ignore[no-untyped-def]
    async with db.unit_of_work(user.actor, request_id=request_id(request), reason=body.note or "machine back up") as s:
        await m.machine_back_up(s, user, equipment_id, body.note)
        eq = await get_equipment(s, equipment_id)
    assert eq.plant_id
    await _push(request, db, eq.plant_id, equipment_id)


@router.get("/maintenance/notifications", response_model=list[NotificationOut])
async def notifications(  # type: ignore[no-untyped-def]
    plant_id: UUID, db: DbDep, user=require(Perm.MAINT_READ), status: Annotated[list[str] | None, Query()] = None,
    limit: int = Query(200, le=1000),
) -> list:
    user.ensure_plant(plant_id)
    q = select(Notification).where(Notification.plant_id == plant_id).order_by(Notification.reported_at.desc()).limit(limit)
    if status:
        q = q.where(Notification.status.in_(status))
    async with db.read() as s:
        return await _notification_rows(s, plant_id, list((await s.scalars(q)).all()))


@router.post("/maintenance/notifications/{notification_id}/order", response_model=MaintOrderDetail, status_code=201)
async def order_from_notification(  # type: ignore[no-untyped-def]
    notification_id: UUID, body: CreateOrderIn, request: Request, db: DbDep, user=require(Perm.MAINT_EXECUTE)
) -> dict:
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        mo = await m.create_order_from_notification(s, user, notification_id, checklist=body.checklist, duration_min=body.duration_min)
        return await _order_detail(s, mo)


@router.get("/maintenance/orders", response_model=list[MaintOrderOut])
async def orders(  # type: ignore[no-untyped-def]
    plant_id: UUID, db: DbDep, user=require(Perm.MAINT_READ), status: Annotated[list[str] | None, Query()] = None,
    line_id: UUID | None = None, equipment_id: UUID | None = None, limit: int = Query(200, le=1000),
) -> list:
    user.ensure_plant(plant_id)
    q = select(MaintenanceOrder).where(MaintenanceOrder.plant_id == plant_id).order_by(
        MaintenanceOrder.priority, MaintenanceOrder.planned_start).limit(limit)
    if status:
        q = q.where(MaintenanceOrder.status.in_(status))
    if line_id:
        q = q.where(MaintenanceOrder.line_id == line_id)
    if equipment_id:
        q = q.where(MaintenanceOrder.equipment_id == equipment_id)
    async with db.read() as s:
        return await _order_rows(s, plant_id, list((await s.scalars(q)).all()))


@router.get("/maintenance/orders/{order_id}", response_model=MaintOrderDetail)
async def order_detail(order_id: UUID, db: DbDep, user=require(Perm.MAINT_READ)) -> dict:  # type: ignore[no-untyped-def]
    async with db.read() as s:
        mo = await m.get_order(s, order_id)
        user.ensure_plant(mo.plant_id)
        return await _order_detail(s, mo)


@router.post("/maintenance/orders/{order_id}/start", response_model=MaintOrderDetail)
async def start_order(order_id: UUID, request: Request, db: DbDep, user=require(Perm.MAINT_EXECUTE)) -> dict:  # type: ignore[no-untyped-def]
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        mo = await m.start_order(s, user, order_id)
        out = await _order_detail(s, mo)
    await _push(request, db, mo.plant_id, mo.equipment_id)
    return out


@router.put("/maintenance/orders/{order_id}/work", response_model=MaintOrderDetail)
async def record_work(order_id: UUID, body: WorkIn, request: Request, db: DbDep, user=require(Perm.MAINT_EXECUTE)) -> dict:  # type: ignore[no-untyped-def]
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        mo = await m.update_checklist(s, user, order_id, [i.model_dump() for i in body.checklist], body.labor_min)
        return await _order_detail(s, mo)


@router.post("/maintenance/orders/{order_id}/parts", response_model=MaintOrderDetail, status_code=201)
async def issue_parts(order_id: UUID, body: PartIn, request: Request, db: DbDep, user=require(Perm.MAINT_EXECUTE)) -> dict:  # type: ignore[no-untyped-def]
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        await m.issue_parts(s, user, order_id, material_id=body.material_id, quantity=body.quantity,
                            client_event_id=body.client_event_id)
        return await _order_detail(s, await m.get_order(s, order_id))


@router.post("/maintenance/orders/{order_id}/complete", response_model=MaintOrderDetail)
async def complete_order(order_id: UUID, body: CompleteIn, request: Request, db: DbDep, user=require(Perm.MAINT_EXECUTE)) -> dict:  # type: ignore[no-untyped-def]
    """Technician sign-off (e-signature): the machine is released to production."""
    async with db.unit_of_work(user.actor, request_id=request_id(request), reason=body.reason) as s:
        mo = await m.complete_order(s, user, order_id, findings=body.findings, reason=body.reason)
        out = await _order_detail(s, mo)
    await _push(request, db, mo.plant_id, mo.equipment_id)
    return out


@router.get("/maintenance/downtime", response_model=list[DowntimeOut])
async def downtime(  # type: ignore[no-untyped-def]
    plant_id: UUID, db: DbDep, user=require(Perm.MAINT_READ), days: int = Query(14, ge=1, le=365),
    equipment_id: UUID | None = None,
) -> list:
    user.ensure_plant(plant_id)
    since = datetime.now(UTC) - timedelta(days=days)
    q = select(DowntimeEvent).where(DowntimeEvent.plant_id == plant_id,
                                    (DowntimeEvent.ended_at.is_(None)) | (DowntimeEvent.started_at >= since)
                                    ).order_by(DowntimeEvent.started_at.desc())
    if equipment_id:
        q = q.where(DowntimeEvent.equipment_id == equipment_id)
    now = datetime.now(UTC)
    async with db.read() as s:
        rows = (await s.scalars(q)).all()
        eqs = await _eq_codes(s, plant_id)
        notifs = {n.id: n.number for n in (await s.scalars(select(Notification).where(
            Notification.id.in_([r.notification_id for r in rows if r.notification_id])))).all()}
        orders = {o.id: o.number for o in (await s.scalars(select(MaintenanceOrder).where(
            MaintenanceOrder.id.in_([r.maintenance_order_id for r in rows if r.maintenance_order_id])))).all()}
    return [{
        **{c.key: getattr(r, c.key) for c in DowntimeEvent.__table__.columns},
        "equipment_code": eqs[r.equipment_id].code if r.equipment_id in eqs else "?",
        "line_code": eqs[r.equipment_id].line_code if r.equipment_id in eqs else "?",
        "duration_min": round(((r.ended_at or now) - r.started_at).total_seconds() / 60, 1),
        "notification_number": notifs.get(r.notification_id) if r.notification_id else None,
        "maintenance_order_number": orders.get(r.maintenance_order_id) if r.maintenance_order_id else None,
    } for r in rows]


async def _plan_rows(s: AsyncSession, plant_id: UUID, plans: list[MaintenancePlan]) -> list[dict[str, Any]]:
    eqs = await _eq_codes(s, plant_id)
    status = await m.plan_status(s, plans)
    open_orders = {o.plan_id: o.number for o in (await s.scalars(select(MaintenanceOrder).where(
        MaintenanceOrder.plan_id.in_([p.id for p in plans]), MaintenanceOrder.status.in_(m.OPEN_ORDER)))).all()} if plans else {}
    return [{
        **{c.key: getattr(p, c.key) for c in MaintenancePlan.__table__.columns},
        "equipment_code": eqs[p.equipment_id].code if p.equipment_id in eqs else "?",
        "next_due_at": status[p.id]["next_due_at"], "runtime_since_last_h": status[p.id]["runtime_since_last_h"],
        "open_order_number": open_orders.get(p.id),
    } for p in plans]


@router.get("/maintenance/plans", response_model=list[MaintPlanOut])
async def plans(plant_id: UUID, db: DbDep, user=require(Perm.MAINT_READ)) -> list:  # type: ignore[no-untyped-def]
    user.ensure_plant(plant_id)
    async with db.read() as s:
        rows = list((await s.scalars(select(MaintenancePlan).where(MaintenancePlan.plant_id == plant_id)
                                     .order_by(MaintenancePlan.name))).all())
        return await _plan_rows(s, plant_id, rows)


@router.put("/maintenance/plans", response_model=MaintPlanOut)
async def put_plan(body: MaintPlanIn, request: Request, db: DbDep, user=require(Perm.MAINT_PLAN)) -> dict:  # type: ignore[no-untyped-def]
    user.ensure_plant(body.plant_id)
    if body.interval_type == "CALENDAR" and not body.interval_days:
        raise ValidationFailed("a calendar plan needs an interval in days")
    if body.interval_type == "RUNTIME" and not body.interval_hours:
        raise ValidationFailed("a runtime plan needs an interval in running hours")
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        eq = await get_equipment(s, body.equipment_id)
        if eq.plant_id != body.plant_id:
            raise NotFound("machine not found in this plant")
        p = (await s.scalars(select(MaintenancePlan).where(MaintenancePlan.plant_id == body.plant_id,
                                                           MaintenancePlan.equipment_id == body.equipment_id,
                                                           MaintenancePlan.name == body.name))).first()
        if p is None:
            p = MaintenancePlan(plant_id=body.plant_id, equipment_id=body.equipment_id, name=body.name)
            s.add(p)
        p.interval_type = body.interval_type
        p.interval_days = body.interval_days if body.interval_type == "CALENDAR" else None
        p.interval_hours = body.interval_hours if body.interval_type == "RUNTIME" else None
        p.lead_days, p.duration_min, p.checklist, p.active = body.lead_days, body.duration_min, body.checklist, body.active
        await s.flush()
        return (await _plan_rows(s, body.plant_id, [p]))[0]


@router.post("/maintenance/plans/generate", response_model=list[MaintOrderOut])
async def generate(plant_id: UUID, request: Request, db: DbDep, user=require(Perm.MAINT_PLAN)) -> list:  # type: ignore[no-untyped-def]
    """Run the preventive scheduler now (the worker also runs it periodically)."""
    user.ensure_plant(plant_id)
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        created = await m.generate_preventive_orders(s, plant_id)
        return await _order_rows(s, plant_id, created)


@router.get("/maintenance/spare-parts", response_model=list[SparePart])
async def spare_parts(plant_id: UUID, db: DbDep, search: str = "SP-", user=require(Perm.MAINT_READ)) -> list:  # type: ignore[no-untyped-def]
    user.ensure_plant(plant_id)
    async with db.read() as s:
        ids = await search_material_ids(s, search)
        mats = await get_materials(s, set(ids))
        avail = await available_by_material(s, plant_id, set(ids))
    return sorted(({
        "material_id": mid, "material_number": mat.material_number, "description": mat.description, "uom": mat.base_uom,
        "in_stores": sum((v for k, v in avail.get(mid, {}).items() if k == "STORE"), Decimal(0)),
    } for mid, mat in mats.items()), key=lambda r: r["material_number"])
