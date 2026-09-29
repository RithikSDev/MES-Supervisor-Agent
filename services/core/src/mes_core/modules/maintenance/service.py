"""Maintenance use cases. Every notification and order (and its completion) is mirrored to SAP PM through the
outbox; the MES number is the SAP number, so the adapter can always tell whether SAP already has it."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mes_common.events import Topics
from mes_core.modules.compliance.public import sign
from mes_core.modules.execution.public import active_run_on, equipment_runtime_hours, pause_for_breakdown
from mes_core.modules.inventory.public import issue_for_maintenance, movements_of_maintenance_order
from mes_core.modules.maintenance import domain
from mes_core.modules.maintenance.models import (
    NOTIFICATION_SEQ,
    ORDER_SEQ,
    DowntimeEvent,
    MaintenanceOrder,
    MaintenancePlan,
    Notification,
)
from mes_core.modules.masterdata.public import get_materials
from mes_core.modules.org.public import (
    EquipmentRef,
    get_equipment,
    get_plant,
    list_equipment,
    set_equipment_status,
)
from mes_core.shared.calendar import Interval
from mes_core.shared.errors import DomainError, NotFound, ValidationFailed
from mes_core.shared.messaging import messaging
from mes_core.shared.users import CurrentUser

EVENT_NOTIFICATION_CREATED = "mes.maintenance.notification_created"
EVENT_ORDER_CREATED = "mes.maintenance.order_created"
EVENT_ORDER_STATUS = "mes.maintenance.order_status"
OPEN_ORDER = ("CREATED", "IN_PROGRESS")


def _now() -> datetime:
    return datetime.now(UTC)


def _name(user: CurrentUser) -> str:
    return user.display_name or user.username


async def _equipment(s: AsyncSession, user: CurrentUser, equipment_id: UUID) -> EquipmentRef:
    eq = await get_equipment(s, equipment_id)
    plant_id = (await _plant_of(s, eq)).id
    user.ensure_plant(plant_id)
    return eq


async def _plant_of(s: AsyncSession, eq: EquipmentRef):  # type: ignore[no-untyped-def]
    assert eq.plant_id is not None
    return await get_plant(s, eq.plant_id)


async def _publish(s: AsyncSession, plant_id: UUID, key: str, event_type: str, payload: dict[str, Any]) -> None:
    plant = await get_plant(s, plant_id)
    if plant.erp_plant_code is None:
        return
    await messaging.publish(
        s, topic=Topics.MES_MAINTENANCE, key=f"{plant.erp_plant_code}:{key}", event_type=event_type,
        payload={"erp_plant": plant.erp_plant_code, **payload},
    )


# ------------------------------------------------------------------------------------------------ notifications


async def _new_notification(
    s: AsyncSession, user: CurrentUser, eq: EquipmentRef, *, plant_id: UUID, notif_type: str, priority: int,
    reason_code: str, description: str, machine_down: bool, reported_at: datetime,
    client_event_id: UUID | None = None,
) -> Notification:
    seq = (await s.execute(select(NOTIFICATION_SEQ.next_value()))).scalar_one()
    n = Notification(
        plant_id=plant_id, line_id=eq.line_id, equipment_id=eq.id, number=str(seq), notif_type=notif_type,
        priority=priority, reason_code=reason_code, description=description, status="OPEN", reported_at=reported_at,
        reported_by=str(user.id), reported_by_name=_name(user), machine_down=machine_down,
        client_event_id=client_event_id, erp_sync_status="PENDING",
    )
    s.add(n)
    await s.flush()
    await _publish(s, plant_id, f"PMN:{n.number}", EVENT_NOTIFICATION_CREATED, {
        "notification_id": str(n.id), "number": n.number, "notif_type": "M2" if notif_type == "BREAKDOWN" else "M1",
        "priority": str(priority), "text": description[:40], "long_text": description, "equipment": eq.code,
        "breakdown": machine_down, "malfunction_start": reported_at.isoformat(), "reported_by": _name(user),
    })
    return n


async def report_breakdown(
    s: AsyncSession, user: CurrentUser, *, equipment_id: UUID, reason_code: str, description: str,
    machine_down: bool = True, client_event_id: UUID | None = None, occurred_at: datetime | None = None,
) -> Notification:
    """Operator reports a machine problem. Machine down: its run is paused, a downtime starts, the machine is DOWN
    (no runs until maintenance releases it). Always: a breakdown notification, mirrored to SAP."""
    if client_event_id:
        existing = (await s.scalars(select(Notification).where(Notification.client_event_id == client_event_id))).first()
        if existing:
            return existing
    if reason_code not in domain.DOWNTIME_REASONS:
        raise ValidationFailed(f"unknown reason {reason_code}")
    eq = await _equipment(s, user, equipment_id)
    plant = await _plant_of(s, eq)
    at = min(occurred_at or _now(), _now())
    if machine_down:
        open_dt = await _open_downtime(s, eq.id)
        if open_dt is not None:
            raise DomainError(f"{eq.code} is already reported down", code="already_down",
                              details={"notification_id": str(open_dt.notification_id)})
    n = await _new_notification(
        s, user, eq, plant_id=plant.id, notif_type="BREAKDOWN", priority=domain.breakdown_priority(machine_down),
        reason_code=reason_code, description=description.strip() or domain.DOWNTIME_REASONS[reason_code],
        machine_down=machine_down, reported_at=at, client_event_id=client_event_id,
    )
    if machine_down:
        run_id = await pause_for_breakdown(s, user, eq.id, description)
        s.add(DowntimeEvent(
            plant_id=plant.id, line_id=eq.line_id, equipment_id=eq.id, started_at=at, planned=False,
            reason_code=reason_code, description=description, reported_by=str(user.id), reported_by_name=_name(user),
            run_id=run_id, notification_id=n.id,
        ))
        await set_equipment_status(s, eq.id, "DOWN")
        await s.flush()
    return n


async def create_request(
    s: AsyncSession, user: CurrentUser, *, equipment_id: UUID, description: str, priority: int = 3,
) -> Notification:
    """Maintenance request (M1): something to look at, the machine keeps running."""
    if not description.strip():
        raise ValidationFailed("describe the request")
    eq = await _equipment(s, user, equipment_id)
    plant = await _plant_of(s, eq)
    return await _new_notification(
        s, user, eq, plant_id=plant.id, notif_type="REQUEST", priority=priority, reason_code="OTHER",
        description=description.strip(), machine_down=False, reported_at=_now(),
    )


async def _open_downtime(s: AsyncSession, equipment_id: UUID) -> DowntimeEvent | None:
    return (await s.scalars(select(DowntimeEvent).where(DowntimeEvent.equipment_id == equipment_id,
                                                        DowntimeEvent.ended_at.is_(None)).with_for_update())).first()


async def _end_downtime(s: AsyncSession, user: CurrentUser, equipment_id: UUID) -> DowntimeEvent | None:
    dt = await _open_downtime(s, equipment_id)
    if dt is not None:
        dt.ended_at, dt.ended_by_name = max(_now(), dt.started_at), _name(user)
        await s.flush()
    return dt


async def machine_back_up(s: AsyncSession, user: CurrentUser, equipment_id: UUID, note: str) -> DowntimeEvent | None:
    """Release a machine without a maintenance order (e.g. the operator cleared a jam). Refused while a
    maintenance order is being worked on it."""
    eq = await _equipment(s, user, equipment_id)
    busy = (await s.scalars(select(MaintenanceOrder).where(MaintenanceOrder.equipment_id == eq.id,
                                                           MaintenanceOrder.status == "IN_PROGRESS"))).first()
    if busy:
        raise DomainError(f"maintenance order {busy.number} is in progress on {eq.code}; complete it to release the machine",
                          code="maintenance_in_progress")
    dt = await _end_downtime(s, user, eq.id)
    if dt and note:
        dt.description = f"{dt.description or ''} | back up: {note}".strip(" |")
    await set_equipment_status(s, eq.id, "AVAILABLE")
    return dt


# ------------------------------------------------------------------------------------------------ orders


async def get_order(s: AsyncSession, order_id: UUID, *, lock: bool = False) -> MaintenanceOrder:
    q = select(MaintenanceOrder).where(MaintenanceOrder.id == order_id)
    if lock:
        q = q.with_for_update()
    mo = (await s.scalars(q)).first()
    if mo is None:
        raise NotFound("maintenance order not found")
    return mo


async def get_notification(s: AsyncSession, notification_id: UUID, *, lock: bool = False) -> Notification:
    q = select(Notification).where(Notification.id == notification_id)
    if lock:
        q = q.with_for_update()
    n = (await s.scalars(q)).first()
    if n is None:
        raise NotFound("notification not found")
    return n


async def _new_order(
    s: AsyncSession, eq: EquipmentRef, *, plant_id: UUID, order_type: str, priority: int, description: str,
    planned_start: datetime, duration_min: int, checklist: list[str], notification: Notification | None = None,
    plan_id: UUID | None = None,
) -> MaintenanceOrder:
    seq = (await s.execute(select(ORDER_SEQ.next_value()))).scalar_one()
    mo = MaintenanceOrder(
        plant_id=plant_id, line_id=eq.line_id, equipment_id=eq.id, number=str(seq), order_type=order_type,
        status="CREATED", priority=priority, description=description, notification_id=notification.id if notification else None,
        plan_id=plan_id, planned_start=planned_start, planned_duration_min=duration_min,
        checklist=[{"text": c, "done": False, "note": None} for c in checklist], labor_min=Decimal(0),
        erp_sync_status="PENDING",
    )
    s.add(mo)
    await s.flush()
    # same key as the order's goods movements (plant:order) so SAP gets the order before its spare parts
    await _publish(s, plant_id, mo.number, EVENT_ORDER_CREATED, {
        "order_id": str(mo.id), "number": mo.number, "order_type": "PM01" if order_type == "CORRECTIVE" else "PM02",
        "description": description[:40], "equipment": eq.code, "work_center": eq.work_center_code,
        "notification_number": notification.number if notification else None, "priority": str(priority),
        "planned_start": planned_start.isoformat(), "duration_min": duration_min,
    })
    return mo


async def create_order_from_notification(
    s: AsyncSession, user: CurrentUser, notification_id: UUID, *, checklist: list[str] | None = None,
    duration_min: int = 120,
) -> MaintenanceOrder:
    n = await get_notification(s, notification_id, lock=True)
    user.ensure_plant(n.plant_id)
    if n.status != "OPEN":
        raise DomainError(f"notification {n.number} is {n.status.lower()}", code="notification_not_open")
    eq = await get_equipment(s, n.equipment_id)
    items = checklist or ["Find the cause and repair", "Test the machine", "Clean up the work area"]
    mo = await _new_order(
        s, eq, plant_id=n.plant_id, order_type="CORRECTIVE", priority=n.priority, description=n.description,
        planned_start=_now(), duration_min=duration_min, checklist=items, notification=n,
    )
    n.status = "IN_PROCESS"
    await s.flush()
    return mo


async def start_order(s: AsyncSession, user: CurrentUser, order_id: UUID) -> MaintenanceOrder:
    """Technician starts work: the machine goes to MAINTENANCE (a preventive order also opens planned downtime and
    needs the machine idle)."""
    mo = await get_order(s, order_id, lock=True)
    user.ensure_plant(mo.plant_id)
    if mo.status != "CREATED":
        raise DomainError(f"order {mo.number} is {mo.status.lower()}", code="invalid_transition")
    eq = await get_equipment(s, mo.equipment_id)
    if mo.order_type == "PREVENTIVE":
        run = await active_run_on(s, eq.id)
        if run is not None and run.status != "PAUSED":
            raise DomainError(f"{eq.code} is running an operation; pause or stop it before maintenance",
                              code="machine_busy")
        if await _open_downtime(s, eq.id) is None:
            s.add(DowntimeEvent(
                plant_id=mo.plant_id, line_id=mo.line_id, equipment_id=eq.id, started_at=_now(), planned=True,
                reason_code=domain.PLANNED_REASON, description=mo.description, reported_by=str(user.id),
                reported_by_name=_name(user), maintenance_order_id=mo.id,
            ))
    mo.status, mo.started_at = "IN_PROGRESS", _now()
    mo.technician_id, mo.technician_name = str(user.id), _name(user)
    await set_equipment_status(s, eq.id, "MAINTENANCE")
    await s.flush()
    await _publish(s, mo.plant_id, mo.number, EVENT_ORDER_STATUS, {
        "message_id": str(uuid4()), "order_id": str(mo.id), "number": mo.number, "status": "REL",
    })
    return mo


async def update_checklist(
    s: AsyncSession, user: CurrentUser, order_id: UUID, items: list[dict[str, Any]], labor_min: Decimal | None = None,
) -> MaintenanceOrder:
    mo = await get_order(s, order_id, lock=True)
    user.ensure_plant(mo.plant_id)
    if mo.status != "IN_PROGRESS":
        raise DomainError("start the order before recording work", code="invalid_transition")
    if len(items) != len(mo.checklist):
        raise ValidationFailed("checklist items don't match the order")
    mo.checklist = [
        {"text": old["text"], "done": bool(new.get("done")), "note": (new.get("note") or None)}
        for old, new in zip(mo.checklist, items, strict=True)
    ]
    if labor_min is not None:
        if labor_min < 0:
            raise ValidationFailed("labour time must not be negative")
        mo.labor_min = labor_min
    await s.flush()
    return mo


async def issue_parts(
    s: AsyncSession, user: CurrentUser, order_id: UUID, *, material_id: UUID, quantity: Decimal,
    client_event_id: UUID | None = None,
) -> list:
    mo = await get_order(s, order_id)
    user.ensure_plant(mo.plant_id)
    if mo.status not in OPEN_ORDER:
        raise DomainError(f"order {mo.number} is {mo.status.lower()}", code="invalid_transition")
    return await issue_for_maintenance(
        s, plant_id=mo.plant_id, maintenance_order_id=mo.id, erp_order_number=mo.number, material_id=material_id,
        quantity=quantity, client_event_id=client_event_id,
    )


async def complete_order(
    s: AsyncSession, user: CurrentUser, order_id: UUID, *, findings: str, reason: str,
) -> MaintenanceOrder:
    """Technician signs the work off (e-signature, meaning RELEASED: the machine is released to production).
    Downtime ends, the machine is AVAILABLE, the notification is completed, SAP gets TECO."""
    mo = await get_order(s, order_id, lock=True)
    user.ensure_plant(mo.plant_id)
    if mo.status != "IN_PROGRESS":
        raise DomainError(f"order {mo.number} is {mo.status.lower()}", code="invalid_transition")
    if not findings.strip():
        raise ValidationFailed("record what was found and done", code="findings_required")
    open_items = [c["text"] for c in mo.checklist if not c.get("done")]
    if open_items:
        raise DomainError(f"{len(open_items)} checklist item(s) still open: {', '.join(open_items)}", code="checklist_open")
    sig = await sign(s, user, meaning="RELEASED", subject_type="maintenance_order", subject_id=mo.id, reason=reason,
                     plant_id=mo.plant_id)
    now = _now()
    mo.status, mo.completed_at, mo.findings, mo.signature_id = "COMPLETED", now, findings.strip(), sig.id
    if not mo.labor_min and mo.started_at:
        mo.labor_min = Decimal(str(round((now - mo.started_at).total_seconds() / 60, 1)))
    await _end_downtime(s, user, mo.equipment_id)
    await set_equipment_status(s, mo.equipment_id, "AVAILABLE")
    if mo.notification_id:
        n = await get_notification(s, mo.notification_id, lock=True)
        n.status, n.completed_at = "COMPLETED", now
    if mo.plan_id:
        plan = await s.get(MaintenancePlan, mo.plan_id)
        if plan is not None:
            plan.last_done_at = now
    await s.flush()
    await _publish(s, mo.plant_id, mo.number, EVENT_ORDER_STATUS, {
        "message_id": str(uuid4()), "order_id": str(mo.id), "number": mo.number, "status": "TECO",
        "labor_min": str(mo.labor_min), "findings": mo.findings[:132],
    })
    return mo


# ------------------------------------------------------------------------------------------------ preventive plans


async def generate_preventive_orders(s: AsyncSession, plant_id: UUID) -> list[MaintenanceOrder]:
    """Worker job: open a preventive order for every due plan that has none open."""
    plans = (await s.scalars(select(MaintenancePlan).where(MaintenancePlan.plant_id == plant_id,
                                                           MaintenancePlan.active.is_(True)))).all()
    if not plans:
        return []
    open_plan_ids = set((await s.scalars(select(MaintenanceOrder.plan_id).where(
        MaintenanceOrder.plan_id.in_([p.id for p in plans]), MaintenanceOrder.status.in_(OPEN_ORDER)))).all())
    now = _now()
    out = []
    for p in plans:
        if p.id in open_plan_ids:
            continue
        state = _plan_state(p)
        runtime = 0.0
        if p.interval_type == "RUNTIME":
            runtime = (await equipment_runtime_hours(s, [p.equipment_id], p.last_done_at or p.created_at))[p.equipment_id]
        if not domain.is_due(state, now, runtime):
            continue
        eq = await get_equipment(s, p.equipment_id)
        start = domain.calendar_due(state) or now + timedelta(days=1)
        out.append(await _new_order(
            s, eq, plant_id=plant_id, order_type="PREVENTIVE", priority=3, description=p.name,
            planned_start=max(start, now), duration_min=p.duration_min, checklist=list(p.checklist), plan_id=p.id,
        ))
    return out


def _plan_state(p: MaintenancePlan) -> domain.PlanState:
    return domain.PlanState(
        p.interval_type, p.interval_days, float(p.interval_hours) if p.interval_hours else None, p.lead_days,
        p.last_done_at, p.created_at,
    )


async def plan_status(s: AsyncSession, plans: list[MaintenancePlan]) -> dict[UUID, dict[str, Any]]:
    runtime = await equipment_runtime_hours(s, list({p.equipment_id for p in plans})) if plans else {}
    out = {}
    for p in plans:
        since = (await equipment_runtime_hours(s, [p.equipment_id], p.last_done_at or p.created_at))[p.equipment_id] \
            if p.interval_type == "RUNTIME" else None
        out[p.id] = {"next_due_at": domain.calendar_due(_plan_state(p)), "runtime_since_last_h": since,
                     "total_runtime_h": runtime.get(p.equipment_id)}
    return out


# ------------------------------------------------------------------------------------------------ reads for other modules


async def blocked_windows(s: AsyncSession, plant_id: UUID, start: datetime, end: datetime) -> dict[UUID, list[Interval]]:
    """Time a machine can't be scheduled: open maintenance orders (planned window) and machines that are down
    (until an expected repair time)."""
    out: dict[UUID, list[Interval]] = {}
    orders = (await s.scalars(select(MaintenanceOrder).where(MaintenanceOrder.plant_id == plant_id,
                                                             MaintenanceOrder.status.in_(OPEN_ORDER)))).all()
    now = _now()
    for mo in orders:
        begin = mo.started_at or mo.planned_start
        finish = max(begin + timedelta(minutes=mo.planned_duration_min), now + timedelta(minutes=30) if mo.started_at else begin)
        if finish > start and begin < end:
            out.setdefault(mo.equipment_id, []).append(Interval(max(begin, start), min(finish, end)))
    for dt in (await s.scalars(select(DowntimeEvent).where(DowntimeEvent.plant_id == plant_id,
                                                           DowntimeEvent.ended_at.is_(None)))).all():
        finish = max(dt.started_at + domain.BREAKDOWN_BLOCK, now + timedelta(hours=1))
        if finish > start:
            out.setdefault(dt.equipment_id, []).append(Interval(max(dt.started_at, start), min(finish, end)))
    return out


async def reliability(s: AsyncSession, plant_id: UUID, days: int = 30) -> dict[UUID, domain.Reliability]:
    since = _now() - timedelta(days=days)
    eqs = await list_equipment(s, plant_id)
    runtime = await equipment_runtime_hours(s, [e.id for e in eqs], since)
    rows = (await s.scalars(select(DowntimeEvent).where(
        DowntimeEvent.plant_id == plant_id, DowntimeEvent.planned.is_(False),
        (DowntimeEvent.ended_at.is_(None)) | (DowntimeEvent.ended_at >= since)))).all()
    by_eq: dict[UUID, list[tuple[datetime, datetime | None]]] = {}
    for r in rows:
        by_eq.setdefault(r.equipment_id, []).append((max(r.started_at, since), r.ended_at))
    now = _now()
    return {e.id: domain.reliability(by_eq.get(e.id, []), runtime.get(e.id, 0.0), now) for e in eqs}


async def parts_of_order(s: AsyncSession, mo: MaintenanceOrder) -> list[dict[str, Any]]:
    mvs = await movements_of_maintenance_order(s, mo.id)
    mats = await get_materials(s, {m.material_id for m in mvs})
    return [{"movement_id": m.id, "material_number": mats[m.material_id].material_number,
             "material_description": mats[m.material_id].description, "quantity": m.quantity, "uom": m.uom,
             "posted_at": m.posted_at, "erp_sync_status": m.erp_sync_status} for m in mvs]


async def apply_erp_result(s: AsyncSession, payload: dict[str, Any]) -> None:
    """Outcome of the SAP PM mirror (adapter result event)."""
    kind = payload.get("message_type")
    status = payload["status"]
    now = _now()
    if kind == "MAINT_NOTIFICATION":
        obj: Any = await s.get(Notification, UUID(payload["notification_id"]))
    elif kind in ("MAINT_ORDER", "MAINT_ORDER_STATUS"):
        obj = await s.get(MaintenanceOrder, UUID(payload["order_id"]))
    else:
        return
    if obj is None:
        return
    if kind == "MAINT_ORDER_STATUS":
        if status == "POSTED":
            obj.erp_system_status = payload.get("system_status") or obj.erp_system_status
        else:
            obj.erp_error = payload.get("error")
        return
    if status == "POSTED":
        obj.erp_sync_status, obj.erp_error, obj.erp_synced_at = "POSTED", None, now
        if kind == "MAINT_ORDER":
            obj.erp_system_status = payload.get("system_status") or "CRTD"
    elif obj.erp_sync_status != "POSTED":
        obj.erp_sync_status, obj.erp_error = "FAILED", payload.get("error")


async def apply_erp_order_update(s: AsyncSession, payload: dict[str, Any]) -> None:
    """A PM order changed in SAP (poller): keep its SAP system status visible in MES."""
    mo = (await s.scalars(select(MaintenanceOrder).where(MaintenanceOrder.number == payload["number"]))).first()
    if mo is not None and payload.get("system_status"):
        mo.erp_system_status = payload["system_status"]
