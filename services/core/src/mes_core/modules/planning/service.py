"""Work order use cases: queries, manual actions (dispatch/hold/resume/priority), ERP upsert, progress."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mes_common.events import Topics
from mes_core.modules.planning.domain.states import (
    CONFIRMABLE,
    HOLDABLE,
    OpStatus,
    WOStatus,
    check_transition,
    erp_target_status,
)
from mes_core.modules.planning.models import (
    Schedule,
    ScheduleEntry,
    WorkOrder,
    WorkOrderComponent,
    WorkOrderOperation,
    WorkOrderStatusHistory,
)
from mes_core.shared.db.context import current_actor
from mes_core.shared.db.session import set_change_reason
from mes_core.shared.errors import DomainError, InvalidTransition, NotFound, StaleVersion, ValidationFailed
from mes_core.shared.messaging import messaging
from mes_core.shared.users import CurrentUser

EVENT_WO_STATUS_CHANGED = "mes.work_order.status_changed"


def due_datetime(basic_end: date, tz: ZoneInfo) -> datetime:
    """ERP due dates are days; the order is due by the end of that day, plant local time."""
    return datetime.combine(basic_end + timedelta(days=1), time(0), tz)


# ------------------------------------------------------------------------------------------------ loading


async def get_work_order(s: AsyncSession, wo_id: UUID, user: CurrentUser | None = None, *, lock: bool = False) -> WorkOrder:
    q = select(WorkOrder).where(WorkOrder.id == wo_id)
    if lock:
        q = q.with_for_update()
    wo = (await s.scalars(q)).first()
    if wo is None or (user is not None and not user.can_access_plant(wo.plant_id)):
        raise NotFound(f"work order {wo_id} not found")
    return wo


def check_version(entity: WorkOrder, row_version: int | None) -> None:
    if row_version is not None and entity.row_version != row_version:
        raise StaleVersion(
            "work order was changed by someone else; reload and try again",
            details={"expected": row_version, "actual": entity.row_version},
        )


async def operations_of(s: AsyncSession, wo_id: UUID, *, lock: bool = False) -> list[WorkOrderOperation]:
    q = select(WorkOrderOperation).where(WorkOrderOperation.work_order_id == wo_id).order_by(WorkOrderOperation.sequence)
    if lock:
        q = q.with_for_update()
    return list((await s.scalars(q)).all())


# ------------------------------------------------------------------------------------------------ transitions


async def transition(s: AsyncSession, wo: WorkOrder, target: WOStatus, *, reason: str | None = None) -> None:
    current = WOStatus(wo.status)
    if current == target:
        return
    check_transition(current, target)
    actor = current_actor()
    assert actor is not None, "transition outside unit of work"
    s.add(
        WorkOrderStatusHistory(
            plant_id=wo.plant_id,
            work_order_id=wo.id,
            from_status=current.value,
            to_status=target.value,
            reason=reason,
            changed_at=datetime.now(UTC),
            changed_by=actor.id,
            changed_by_name=actor.name,
        )
    )
    wo.status = target.value
    await messaging.publish(
        s,
        topic=Topics.MES_PLANNING,
        key=f"{wo.plant_id}:{wo.erp_order_number}",
        event_type=EVENT_WO_STATUS_CHANGED,
        payload={
            "work_order_id": str(wo.id),
            "plant_id": str(wo.plant_id),
            "erp_order_number": wo.erp_order_number,
            "from_status": current.value,
            "to_status": target.value,
            "reason": reason,
        },
    )


async def dispatch(s: AsyncSession, user: CurrentUser, wo_id: UUID, row_version: int | None) -> WorkOrder:
    wo = await get_work_order(s, wo_id, user, lock=True)
    check_version(wo, row_version)
    if wo.status != WOStatus.SCHEDULED:
        raise InvalidTransition("only scheduled work orders can be dispatched (publish a schedule first)")
    await transition(s, wo, WOStatus.DISPATCHED)
    await s.flush()
    return wo


async def hold(s: AsyncSession, user: CurrentUser, wo_id: UUID, row_version: int | None, reason: str) -> WorkOrder:
    if not reason.strip():
        raise ValidationFailed("a reason is required to put a work order on hold")
    await set_change_reason(s, reason)
    wo = await get_work_order(s, wo_id, user, lock=True)
    check_version(wo, row_version)
    if WOStatus(wo.status) not in HOLDABLE:
        raise InvalidTransition(f"a {wo.status} work order cannot be put on hold")
    wo.hold_prev_status = wo.status
    await transition(s, wo, WOStatus.ON_HOLD, reason=reason)
    await s.flush()
    return wo


async def resume(s: AsyncSession, user: CurrentUser, wo_id: UUID, row_version: int | None, reason: str | None) -> WorkOrder:
    await set_change_reason(s, reason)
    wo = await get_work_order(s, wo_id, user, lock=True)
    check_version(wo, row_version)
    if wo.status != WOStatus.ON_HOLD:
        raise InvalidTransition("work order is not on hold")
    target = WOStatus(wo.hold_prev_status or WOStatus.RELEASED)
    wo.hold_prev_status = None
    await transition(s, wo, target, reason=reason)
    await s.flush()
    return wo


async def set_priority(s: AsyncSession, user: CurrentUser, wo_id: UUID, row_version: int | None, priority: int) -> WorkOrder:
    if not 1 <= priority <= 5:
        raise ValidationFailed("priority must be between 1 (most urgent) and 5")
    wo = await get_work_order(s, wo_id, user, lock=True)
    check_version(wo, row_version)
    wo.priority = priority
    await s.flush()
    return wo


# ------------------------------------------------------------------------------------------------ ERP inbound


@dataclass(frozen=True)
class ErpOperationData:
    op_no: str
    description: str
    work_center_id: UUID
    control_key: str | None
    planned_qty: Decimal
    uom: str
    setup_min: Decimal
    run_min_per_unit: Decimal
    labor_min_per_unit: Decimal
    erp_routing_no: str | None
    erp_routing_counter: str | None


@dataclass(frozen=True)
class ErpComponentData:
    material_id: UUID
    op_no: str | None
    required_qty: Decimal
    uom: str
    reservation_no: str
    reservation_item: str
    storage_location: str | None
    backflush: bool


@dataclass(frozen=True)
class ErpWorkOrderData:
    plant_id: UUID
    line_id: UUID | None
    erp_order_number: str
    order_type: str
    material_id: UUID
    planned_qty: Decimal
    uom: str
    basic_start: date
    basic_end: date
    erp_status: list[str]
    erp_last_changed_at: datetime
    operations: list[ErpOperationData]
    components: list[ErpComponentData]


def _assign(obj: object, **values: object) -> None:
    """Set only changed attributes so unchanged rows produce no UPDATE (and no audit noise)."""
    for k, v in values.items():
        if getattr(obj, k, None) != v:
            setattr(obj, k, v)


async def upsert_from_erp(s: AsyncSession, d: ErpWorkOrderData) -> tuple[UUID, str]:
    """Apply an ERP work order snapshot. Returns (work_order_id, outcome) with outcome created|updated|stale."""
    wo = (
        await s.scalars(
            select(WorkOrder)
            .where(WorkOrder.plant_id == d.plant_id, WorkOrder.erp_order_number == d.erp_order_number)
            .with_for_update()
        )
    ).first()
    if wo is not None and wo.erp_last_changed_at is not None and d.erp_last_changed_at <= wo.erp_last_changed_at:
        return wo.id, "stale"  # out-of-order or duplicate delivery

    erp_status = set(d.erp_status)
    reason = f"ERP status {' '.join(d.erp_status)}"
    await set_change_reason(s, "ERP synchronisation")
    header = dict(
        line_id=d.line_id,
        order_type=d.order_type,
        material_id=d.material_id,
        planned_qty=d.planned_qty,
        uom=d.uom,
        erp_basic_start=d.basic_start,
        erp_basic_end=d.basic_end,
        erp_status=" ".join(d.erp_status),
        erp_last_changed_at=d.erp_last_changed_at,
    )
    if wo is None:
        initial = erp_target_status(None, erp_status) or WOStatus.IMPORTED
        wo = WorkOrder(plant_id=d.plant_id, erp_order_number=d.erp_order_number, status=initial.value, source="ERP", **header)
        s.add(wo)
        await s.flush()
        actor = current_actor()
        s.add(
            WorkOrderStatusHistory(
                plant_id=wo.plant_id,
                work_order_id=wo.id,
                from_status=None,
                to_status=initial.value,
                reason=reason,
                changed_at=datetime.now(UTC),
                changed_by=actor.id if actor else "unknown",
                changed_by_name=actor.name if actor else None,
            )
        )
        outcome = "created"
    else:
        _assign(wo, **header)
        target = erp_target_status(WOStatus(wo.status), erp_status)
        if target is not None:
            await transition(s, wo, target, reason=reason)
        outcome = "updated"

    existing_ops = {o.op_no: o for o in await operations_of(s, wo.id)}
    op_ids: dict[str, UUID] = {}
    for seq, od in enumerate(sorted(d.operations, key=lambda o: o.op_no), start=1):
        op = existing_ops.get(od.op_no)
        values = dict(
            sequence=seq,
            description=od.description,
            work_center_id=od.work_center_id,
            control_key=od.control_key,
            planned_qty=od.planned_qty,
            uom=od.uom,
            setup_min=od.setup_min,
            run_min_per_unit=od.run_min_per_unit,
            labor_min_per_unit=od.labor_min_per_unit,
            erp_routing_no=od.erp_routing_no,
            erp_routing_counter=od.erp_routing_counter,
        )
        if op is None:
            op = WorkOrderOperation(plant_id=wo.plant_id, work_order_id=wo.id, op_no=od.op_no, **values)
            s.add(op)
        elif op.status != OpStatus.CONFIRMED:
            _assign(op, **values)
        await s.flush()
        op_ids[od.op_no] = op.id

    existing_comps = {
        (c.erp_reservation_no, c.erp_reservation_item): c
        for c in (await s.scalars(select(WorkOrderComponent).where(WorkOrderComponent.work_order_id == wo.id))).all()
    }
    for cd in d.components:
        values = dict(
            operation_id=op_ids.get(cd.op_no) if cd.op_no else None,
            material_id=cd.material_id,
            required_qty=cd.required_qty,
            uom=cd.uom,
            storage_location=cd.storage_location,
            backflush=cd.backflush,
        )
        comp = existing_comps.get((cd.reservation_no, cd.reservation_item))
        if comp is None:
            s.add(
                WorkOrderComponent(
                    plant_id=wo.plant_id,
                    work_order_id=wo.id,
                    erp_reservation_no=cd.reservation_no,
                    erp_reservation_item=cd.reservation_item,
                    **values,
                )
            )
        else:
            _assign(comp, **values)
    await s.flush()
    return wo.id, outcome


# ------------------------------------------------------------------------------------------------ progress


@dataclass(frozen=True)
class OperationContext:
    work_order_id: UUID
    operation_id: UUID
    plant_id: UUID
    erp_order_number: str
    source: str
    wo_status: str
    op_no: str
    op_status: str
    work_center_id: UUID
    uom: str
    planned_qty: Decimal


async def get_operation_context(s: AsyncSession, wo_id: UUID, op_id: UUID) -> OperationContext:
    row = (
        await s.execute(
            select(WorkOrder, WorkOrderOperation)
            .join(WorkOrderOperation, WorkOrderOperation.work_order_id == WorkOrder.id)
            .where(WorkOrder.id == wo_id, WorkOrderOperation.id == op_id)
        )
    ).first()
    if row is None:
        raise NotFound("operation not found in this work order")
    wo, op = row
    return OperationContext(
        wo.id, op.id, wo.plant_id, wo.erp_order_number, wo.source, wo.status, op.op_no, op.status,
        op.work_center_id, op.uom, op.planned_qty,
    )


async def record_operation_progress(
    s: AsyncSession, wo_id: UUID, op_id: UUID, *, yield_qty: Decimal, scrap_qty: Decimal, final: bool
) -> None:
    wo = await get_work_order(s, wo_id, lock=True)
    if WOStatus(wo.status) not in CONFIRMABLE:
        raise InvalidTransition(f"cannot confirm: work order is {wo.status} (it must be dispatched)")
    ops = await operations_of(s, wo_id, lock=True)
    op = next((o for o in ops if o.id == op_id), None)
    if op is None:
        raise NotFound("operation not found in this work order")
    if op.status == OpStatus.CONFIRMED:
        raise DomainError("operation is already finally confirmed", code="operation_confirmed")
    if yield_qty < 0 or scrap_qty < 0 or (yield_qty == 0 and scrap_qty == 0 and not final):
        raise ValidationFailed("enter a yield or scrap quantity (or mark the confirmation final)")

    op.yield_qty += yield_qty
    op.scrap_qty += scrap_qty
    op.status = (OpStatus.CONFIRMED if final else OpStatus.IN_PROGRESS).value
    wo.yield_qty = ops[-1].yield_qty
    wo.scrap_qty = sum((o.scrap_qty for o in ops), Decimal(0))
    if wo.status == WOStatus.DISPATCHED:
        await transition(s, wo, WOStatus.IN_PROGRESS)
    if all(o.status == OpStatus.CONFIRMED for o in ops):
        await transition(s, wo, WOStatus.COMPLETED, reason="all operations finally confirmed")
    await s.flush()


# ------------------------------------------------------------------------------------------------ queries


@dataclass
class WorkOrderFilter:
    plant_id: UUID | None = None
    status: list[str] | None = None
    search: str | None = None
    due_before: date | None = None


def scoped_work_orders(user: CurrentUser, f: WorkOrderFilter):  # type: ignore[no-untyped-def]
    q = select(WorkOrder)
    if f.plant_id:
        user.ensure_plant(f.plant_id)
        q = q.where(WorkOrder.plant_id == f.plant_id)
    elif user.plant_ids is not None:
        q = q.where(WorkOrder.plant_id.in_(user.plant_ids))
    if f.status:
        q = q.where(WorkOrder.status.in_(f.status))
    if f.due_before:
        q = q.where(WorkOrder.erp_basic_end <= f.due_before)
    return q


async def count_operations(s: AsyncSession, wo_ids: list[UUID]) -> dict[UUID, tuple[int, int]]:
    if not wo_ids:
        return {}
    rows = (
        await s.execute(
            select(
                WorkOrderOperation.work_order_id,
                func.count(),
                func.count().filter(WorkOrderOperation.status == OpStatus.CONFIRMED.value),
            )
            .where(WorkOrderOperation.work_order_id.in_(wo_ids))
            .group_by(WorkOrderOperation.work_order_id)
        )
    ).all()
    return {r[0]: (r[1], r[2]) for r in rows}


def search_clause(search: str, material_ids: list[UUID]):  # type: ignore[no-untyped-def]
    like = f"%{search}%"
    return or_(WorkOrder.erp_order_number.ilike(like), WorkOrder.material_id.in_(material_ids))


# ------------------------------------------------------------------------------------------------ execution support


@dataclass(frozen=True)
class ComponentContext:
    component_id: UUID
    work_order_id: UUID
    plant_id: UUID
    erp_order_number: str
    wo_status: str
    wo_planned_qty: Decimal
    wo_sched_start: datetime | None
    operation_id: UUID | None
    op_no: str | None
    op_work_center_id: UUID | None
    material_id: UUID
    required_qty: Decimal
    uom: str
    reservation_no: str | None
    reservation_item: str | None
    storage_location: str | None
    backflush: bool

    def qty_for(self, produced: Decimal) -> Decimal:
        """Component quantity consumed for `produced` units of the order (yield + scrap)."""
        return (self.required_qty / self.wo_planned_qty * produced).quantize(Decimal("0.001"))


def _component_query():  # type: ignore[no-untyped-def]
    return (
        select(WorkOrderComponent, WorkOrder, WorkOrderOperation)
        .join(WorkOrder, WorkOrder.id == WorkOrderComponent.work_order_id)
        .outerjoin(WorkOrderOperation, WorkOrderOperation.id == WorkOrderComponent.operation_id)
    )


def _component_ctx(c: WorkOrderComponent, wo: WorkOrder, op: WorkOrderOperation | None) -> ComponentContext:
    return ComponentContext(
        c.id, wo.id, wo.plant_id, wo.erp_order_number, wo.status, wo.planned_qty, wo.sched_start,
        op.id if op else None, op.op_no if op else None, op.work_center_id if op else None,
        c.material_id, c.required_qty, c.uom, c.erp_reservation_no, c.erp_reservation_item, c.storage_location,
        c.backflush,
    )


async def get_component_context(s: AsyncSession, component_id: UUID) -> ComponentContext:
    row = (await s.execute(_component_query().where(WorkOrderComponent.id == component_id))).first()
    if row is None:
        raise NotFound("component not found")
    return _component_ctx(*row)


async def components_of_work_order(s: AsyncSession, wo_id: UUID) -> list[ComponentContext]:
    rows = (await s.execute(_component_query().where(WorkOrderComponent.work_order_id == wo_id))).all()
    return [_component_ctx(*r) for r in rows]


async def open_component_demand(s: AsyncSession, plant_id: UUID, statuses: list[str]) -> list[ComponentContext]:
    """Components of work orders in the given statuses (e.g. scheduled/dispatched/in progress)."""
    rows = (
        await s.execute(_component_query().where(WorkOrder.plant_id == plant_id, WorkOrder.status.in_(statuses)))
    ).all()
    return [_component_ctx(*r) for r in rows]


@dataclass(frozen=True)
class WorkOrderBrief:
    id: UUID
    plant_id: UUID
    line_id: UUID | None
    erp_order_number: str
    material_id: UUID
    planned_qty: Decimal
    uom: str
    status: str
    source: str
    erp_basic_end: date
    sched_start: datetime | None
    priority: int


def _brief(wo: WorkOrder) -> WorkOrderBrief:
    return WorkOrderBrief(
        wo.id, wo.plant_id, wo.line_id, wo.erp_order_number, wo.material_id, wo.planned_qty, wo.uom, wo.status,
        wo.source, wo.erp_basic_end, wo.sched_start, wo.priority,
    )


async def get_work_order_brief(s: AsyncSession, wo_id: UUID) -> WorkOrderBrief:
    return _brief(await get_work_order(s, wo_id))


@dataclass(frozen=True)
class OperationBrief:
    id: UUID
    work_order_id: UUID
    op_no: str
    sequence: int
    description: str
    work_center_id: UUID
    status: str
    planned_qty: Decimal
    yield_qty: Decimal
    scrap_qty: Decimal
    uom: str
    setup_min: Decimal
    run_min_per_unit: Decimal
    is_last: bool


async def operations_brief(s: AsyncSession, wo_id: UUID) -> list[OperationBrief]:
    ops = await operations_of(s, wo_id)
    last = ops[-1].id if ops else None
    return [
        OperationBrief(
            o.id, o.work_order_id, o.op_no, o.sequence, o.description, o.work_center_id, o.status, o.planned_qty,
            o.yield_qty, o.scrap_qty, o.uom, o.setup_min, o.run_min_per_unit, o.id == last,
        )
        for o in ops
    ]


@dataclass(frozen=True)
class QueueItem:
    work_order: WorkOrderBrief
    operation: OperationBrief
    previous_op_status: str | None
    planned_equipment_id: UUID | None
    planned_start: datetime | None
    planned_end: datetime | None


async def operation_queue(s: AsyncSession, plant_id: UUID, work_center_ids: set[UUID]) -> list[QueueItem]:
    """Open operations of released-to-floor work orders at these work centers, in planned sequence. Orders put on
    hold while on the floor stay listed (the terminal shows the hold instead of silently dropping the work)."""
    floor = [WOStatus.DISPATCHED.value, WOStatus.IN_PROGRESS.value]
    wos = (
        await s.scalars(
            select(WorkOrder).where(
                WorkOrder.plant_id == plant_id,
                WorkOrder.status.in_(floor)
                | ((WorkOrder.status == WOStatus.ON_HOLD.value) & WorkOrder.hold_prev_status.in_(floor)),
            )
        )
    ).all()
    if not wos:
        return []
    published = (
        await s.scalars(
            select(Schedule.id).where(Schedule.plant_id == plant_id, Schedule.status == "PUBLISHED")
        )
    ).first()
    planned: dict[UUID, ScheduleEntry] = {}
    if published:
        planned = {
            e.operation_id: e
            for e in (
                await s.scalars(
                    select(ScheduleEntry).where(
                        ScheduleEntry.schedule_id == published, ScheduleEntry.work_order_id.in_([w.id for w in wos])
                    )
                )
            ).all()
        }
    out: list[QueueItem] = []
    for wo in wos:
        ops = await operations_brief(s, wo.id)
        prev: OperationBrief | None = None
        for op in ops:
            if op.work_center_id in work_center_ids and op.status != OpStatus.CONFIRMED:
                e = planned.get(op.id)
                out.append(
                    QueueItem(
                        _brief(wo), op, prev.status if prev else None,
                        e.equipment_id if e else None, e.planned_start if e else None, e.planned_end if e else None,
                    )
                )
            prev = op
    far = datetime.max.replace(tzinfo=UTC)
    out.sort(key=lambda q: (q.planned_start or far, q.work_order.priority, q.work_order.erp_basic_end))
    return out


async def mark_operation_started(s: AsyncSession, wo_id: UUID, op_id: UUID) -> None:
    """First activity on an operation: operation -> IN_PROGRESS, work order DISPATCHED -> IN_PROGRESS."""
    wo = await get_work_order(s, wo_id, lock=True)
    if WOStatus(wo.status) not in CONFIRMABLE:
        raise InvalidTransition(f"work order is {wo.status}; it must be dispatched before work starts")
    op = next((o for o in await operations_of(s, wo_id, lock=True) if o.id == op_id), None)
    if op is None:
        raise NotFound("operation not found in this work order")
    if op.status == OpStatus.CONFIRMED:
        raise DomainError("operation is already finally confirmed", code="operation_confirmed")
    if op.status == OpStatus.PENDING:
        op.status = OpStatus.IN_PROGRESS.value
    if wo.status == WOStatus.DISPATCHED:
        await transition(s, wo, WOStatus.IN_PROGRESS, reason="work started")
    await s.flush()


async def hold_for_quality(s: AsyncSession, wo_id: UUID, reason: str) -> bool:
    """Put an order on hold because quality failed. Returns False if it was not in a holdable state."""
    wo = await get_work_order(s, wo_id, lock=True)
    if WOStatus(wo.status) not in HOLDABLE:
        return False
    await set_change_reason(s, reason)
    wo.hold_prev_status = wo.status
    await transition(s, wo, WOStatus.ON_HOLD, reason=reason)
    await s.flush()
    return True


async def find_work_orders(s: AsyncSession, plant_id: UUID, text: str, limit: int = 20) -> list[WorkOrderBrief]:
    rows = (
        await s.scalars(
            select(WorkOrder).where(WorkOrder.plant_id == plant_id, WorkOrder.erp_order_number.ilike(f"%{text}%"))
            .order_by(WorkOrder.erp_order_number.desc()).limit(limit)
        )
    ).all()
    return [_brief(x) for x in rows]
