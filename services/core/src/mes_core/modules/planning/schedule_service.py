"""Schedule use cases: drafts, the board view, manual moves, auto-sequencing, publishing with e-signature."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mes_common.events import Topics
from mes_core.modules.compliance.public import sign
from mes_core.modules.maintenance.public import blocked_windows
from mes_core.modules.masterdata.public import get_materials
from mes_core.modules.org.public import EquipmentRef, PlantRef, get_plant, line_calendars, list_equipment
from mes_core.modules.planning.domain.scheduling import (
    EntryView,
    FixedEntry,
    Machine,
    OpRequest,
    OpView,
    OrderRequest,
    OrderView,
    auto_sequence,
    earliest_slot,
    evaluate_conflicts,
    operation_duration_min,
)
from mes_core.modules.planning.domain.states import SCHEDULABLE, OpStatus, ScheduleStatus, WOStatus
from mes_core.modules.planning.models import Schedule, ScheduleEntry, WorkOrder, WorkOrderOperation
from mes_core.modules.planning.service import due_datetime, transition
from mes_core.shared.calendar import Interval, WorkingCalendar, complement, subtract
from mes_core.shared.errors import DomainError, NotFound, StaleVersion, ValidationFailed
from mes_core.shared.messaging import messaging
from mes_core.shared.users import CurrentUser

EVENT_SCHEDULE_PUBLISHED = "mes.schedule.published"
_SCHEDULABLE_VALUES = [s.value for s in SCHEDULABLE]


@dataclass
class ScheduleContext:
    plant: PlantRef
    schedule: Schedule
    equipment: dict[UUID, EquipmentRef]
    line_calendars: dict[UUID, WorkingCalendar]
    work_orders: dict[UUID, WorkOrder]
    ops: dict[UUID, WorkOrderOperation]
    entries: list[ScheduleEntry]
    # maintenance windows and machines that are down: not available for production
    blocked: dict[UUID, list[Interval]] = field(default_factory=dict)
    _calendars: dict[UUID, WorkingCalendar] = field(default_factory=dict)

    def calendar_for(self, equipment_id: UUID) -> WorkingCalendar:
        cal = self._calendars.get(equipment_id)
        if cal is None:
            eq = self.equipment[equipment_id]
            line = self.line_calendars.get(eq.line_id) or WorkingCalendar([])
            blocks = self.blocked.get(equipment_id)
            cal = WorkingCalendar(subtract(line.intervals, blocks)) if blocks else line
            self._calendars[equipment_id] = cal
        return cal

    def duration(self, op: WorkOrderOperation) -> int:
        return operation_duration_min(
            float(op.setup_min), float(op.run_min_per_unit), float(op.planned_qty), float(op.yield_qty)
        )

    def due(self, wo: WorkOrder) -> datetime:
        return due_datetime(wo.erp_basic_end, self.plant.tz)

    @property
    def horizon(self) -> Interval:
        return Interval(self.schedule.horizon_start, self.schedule.horizon_end)


async def _get_schedule(s: AsyncSession, schedule_id: UUID, user: CurrentUser, *, lock: bool = False) -> Schedule:
    q = select(Schedule).where(Schedule.id == schedule_id)
    if lock:
        q = q.with_for_update()
    sch = (await s.scalars(q)).first()
    if sch is None or not user.can_access_plant(sch.plant_id):
        raise NotFound("schedule not found")
    return sch


def _require_draft(sch: Schedule) -> None:
    if sch.status != ScheduleStatus.DRAFT:
        raise DomainError(f"schedule is {sch.status}; only drafts can be edited", code="schedule_not_draft")


async def load_context(s: AsyncSession, sch: Schedule) -> ScheduleContext:
    plant = await get_plant(s, sch.plant_id)
    equipment = {e.id: e for e in await list_equipment(s, plant.id)}
    calendars = await line_calendars(s, plant, sch.horizon_start - timedelta(days=1), sch.horizon_end + timedelta(days=1))
    entries = list(
        (await s.scalars(select(ScheduleEntry).where(ScheduleEntry.schedule_id == sch.id).order_by(ScheduleEntry.planned_start))).all()
    )
    entry_wo_ids = {e.work_order_id for e in entries}
    wo_q = select(WorkOrder).where(WorkOrder.plant_id == plant.id, WorkOrder.status.in_(_SCHEDULABLE_VALUES))
    if entry_wo_ids:
        wo_q = select(WorkOrder).where(
            WorkOrder.plant_id == plant.id,
            WorkOrder.status.in_(_SCHEDULABLE_VALUES) | WorkOrder.id.in_(entry_wo_ids),
        )
    work_orders = {w.id: w for w in (await s.scalars(wo_q)).all()}
    ops = {}
    if work_orders:
        ops = {
            o.id: o
            for o in (
                await s.scalars(select(WorkOrderOperation).where(WorkOrderOperation.work_order_id.in_(work_orders.keys())))
            ).all()
        }
    blocked = await blocked_windows(s, plant.id, sch.horizon_start - timedelta(days=1), sch.horizon_end + timedelta(days=1))
    return ScheduleContext(plant, sch, equipment, calendars, work_orders, ops, entries, blocked)


def _conflicts(ctx: ScheduleContext) -> list:
    entries = [
        EntryView(e.id, e.operation_id, e.work_order_id, e.equipment_id, e.planned_start, e.planned_end)
        for e in ctx.entries
    ]
    ops = {
        o.id: OpView(o.id, o.work_order_id, o.sequence, o.op_no, o.work_center_id, o.status) for o in ctx.ops.values()
    }
    orders = {
        w.id: OrderView(w.id, w.erp_order_number, w.status, ctx.due(w), WOStatus(w.status) in SCHEDULABLE)
        for w in ctx.work_orders.values()
    }
    return evaluate_conflicts(
        entries,
        ops,
        orders,
        {e.id: e.work_center_id for e in ctx.equipment.values()},
        {eid: ctx.calendar_for(eid) for eid in ctx.equipment},
        ctx.horizon,
    )


def _unscheduled_ops(ctx: ScheduleContext) -> list[WorkOrderOperation]:
    scheduled = {e.operation_id for e in ctx.entries}
    return sorted(
        (
            o
            for o in ctx.ops.values()
            if o.id not in scheduled
            and o.status != OpStatus.CONFIRMED
            and WOStatus(ctx.work_orders[o.work_order_id].status) in SCHEDULABLE
        ),
        key=lambda o: (
            ctx.work_orders[o.work_order_id].priority,
            ctx.work_orders[o.work_order_id].erp_basic_end,
            ctx.work_orders[o.work_order_id].erp_order_number,
            o.sequence,
        ),
    )


# ------------------------------------------------------------------------------------------------ queries


async def list_schedules(s: AsyncSession, user: CurrentUser, plant_id: UUID, statuses: list[str] | None) -> list[Schedule]:
    user.ensure_plant(plant_id)
    q = select(Schedule).where(Schedule.plant_id == plant_id).order_by(Schedule.version.desc()).limit(50)
    if statuses:
        q = q.where(Schedule.status.in_(statuses))
    return list((await s.scalars(q)).all())


async def schedule_view(s: AsyncSession, user: CurrentUser, schedule_id: UUID) -> dict[str, Any]:
    sch = await _get_schedule(s, schedule_id, user)
    ctx = await load_context(s, sch)
    materials = await get_materials(s, {w.material_id for w in ctx.work_orders.values()})
    conflicts = _conflicts(ctx)
    unscheduled = _unscheduled_ops(ctx)

    def op_info(o: WorkOrderOperation) -> dict[str, Any]:
        wo = ctx.work_orders[o.work_order_id]
        mat = materials.get(wo.material_id)
        return {
            "operation_id": o.id,
            "work_order_id": wo.id,
            "erp_order_number": wo.erp_order_number,
            "material_number": mat.material_number if mat else None,
            "material_description": mat.description if mat else None,
            "op_no": o.op_no,
            "op_description": o.description,
            "work_center_id": o.work_center_id,
            "duration_min": ctx.duration(o),
            "planned_qty": o.planned_qty,
            "uom": o.uom,
            "due": ctx.due(wo),
            "priority": wo.priority,
            "wo_status": wo.status,
            "op_status": o.status,
        }

    off_shift = []
    for line_id, cal in ctx.line_calendars.items():
        for gap in complement(cal.intervals, sch.horizon_start, sch.horizon_end):
            off_shift.append({"line_id": line_id, "start": gap.start, "end": gap.end})

    return {
        "schedule": sch,
        "plant_timezone": ctx.plant.timezone,
        "resources": list(ctx.equipment.values()),
        "entries": [
            {
                **op_info(ctx.ops[e.operation_id]),
                "id": e.id,
                "equipment_id": e.equipment_id,
                "planned_start": e.planned_start,
                "planned_end": e.planned_end,
                "locked": e.locked,
                "row_version": e.row_version,
            }
            for e in ctx.entries
            if e.operation_id in ctx.ops
        ],
        "unscheduled": [op_info(o) for o in unscheduled],
        "conflicts": [c.__dict__ for c in conflicts],
        "off_shift": off_shift,
        "blocked": [{"equipment_id": eq, "start": iv.start, "end": iv.end} for eq, ivs in ctx.blocked.items() for iv in ivs],
    }


# ------------------------------------------------------------------------------------------------ commands


async def create_draft(s: AsyncSession, user: CurrentUser, plant_id: UUID, horizon_days: int) -> Schedule:
    user.ensure_plant(plant_id)
    existing = (
        await s.scalars(
            select(Schedule).where(
                Schedule.plant_id == plant_id, Schedule.line_id.is_(None), Schedule.status == ScheduleStatus.DRAFT
            )
        )
    ).first()
    if existing:
        return existing
    plant = await get_plant(s, plant_id)
    today = datetime.now(plant.tz).date()
    start = datetime.combine(today, time(0), plant.tz)
    published = (
        await s.scalars(
            select(Schedule).where(
                Schedule.plant_id == plant_id, Schedule.line_id.is_(None), Schedule.status == ScheduleStatus.PUBLISHED
            )
        )
    ).first()
    version = (await s.execute(select(func.coalesce(func.max(Schedule.version), 0)).where(Schedule.plant_id == plant_id))).scalar_one() + 1
    sch = Schedule(
        plant_id=plant_id,
        line_id=None,
        name=f"Plant {plant.code} schedule v{version}",
        version=version,
        horizon_start=start,
        horizon_end=start + timedelta(days=horizon_days),
        status=ScheduleStatus.DRAFT.value,
        based_on_id=published.id if published else None,
    )
    s.add(sch)
    await s.flush()
    if published:
        ctx = await load_context(s, published)
        for e in ctx.entries:
            op = ctx.ops.get(e.operation_id)
            wo = ctx.work_orders.get(e.work_order_id)
            if op is None or wo is None or op.status == OpStatus.CONFIRMED or WOStatus(wo.status) not in SCHEDULABLE:
                continue
            s.add(
                ScheduleEntry(
                    plant_id=plant_id,
                    schedule_id=sch.id,
                    work_order_id=e.work_order_id,
                    operation_id=e.operation_id,
                    equipment_id=e.equipment_id,
                    planned_start=e.planned_start,
                    planned_end=e.planned_end,
                    locked=e.locked,
                )
            )
        await s.flush()
    return sch


def _check_overlap(ctx: ScheduleContext, equipment_id: UUID, start: datetime, end: datetime, ignore: UUID | None) -> None:
    for e in ctx.entries:
        if e.id != ignore and e.equipment_id == equipment_id and e.planned_start < end and start < e.planned_end:
            other = ctx.ops.get(e.operation_id)
            wo = ctx.work_orders.get(e.work_order_id)
            raise DomainError(
                f"overlaps {wo.erp_order_number if wo else '?'}/{other.op_no if other else '?'} on "
                f"{ctx.equipment[equipment_id].code}",
                code="schedule_overlap",
                details={"entry_id": str(e.id)},
            )


def _validate_placement(ctx: ScheduleContext, op: WorkOrderOperation, equipment_id: UUID) -> None:
    eq = ctx.equipment.get(equipment_id)
    if eq is None:
        raise ValidationFailed("unknown machine")
    if eq.work_center_id != op.work_center_id:
        raise ValidationFailed(
            f"{eq.code} is not in the operation's work center", code="work_center_mismatch"
        )
    wo = ctx.work_orders.get(op.work_order_id)
    if wo is None or WOStatus(wo.status) not in SCHEDULABLE:
        raise ValidationFailed("work order is not schedulable in its current status")


def _timing(ctx: ScheduleContext, op: WorkOrderOperation, equipment_id: UUID, start: datetime) -> tuple[datetime, datetime]:
    cal = ctx.calendar_for(equipment_id)
    snapped = cal.next_working(start)
    if snapped is None:
        raise ValidationFailed("no working time after that start in the calendar")
    end = cal.add_working(snapped, ctx.duration(op))
    if end is None:
        raise ValidationFailed("operation does not fit before the end of the calendar")
    return snapped, end


async def add_entry(
    s: AsyncSession, user: CurrentUser, schedule_id: UUID, operation_id: UUID, equipment_id: UUID | None, start: datetime | None
) -> ScheduleEntry:
    sch = await _get_schedule(s, schedule_id, user, lock=True)
    _require_draft(sch)
    ctx = await load_context(s, sch)
    op = ctx.ops.get(operation_id)
    if op is None:
        raise NotFound("operation not found or its work order is not schedulable")
    if any(e.operation_id == operation_id for e in ctx.entries):
        raise DomainError("operation is already on this schedule", code="already_scheduled")

    if equipment_id is None or start is None:
        # Earliest feasible slot after the predecessor, on the best machine of the work center.
        prev_end = max(sch.horizon_start, datetime.now(UTC))
        for e in ctx.entries:
            other = ctx.ops.get(e.operation_id)
            if other and other.work_order_id == op.work_order_id and other.sequence < op.sequence:
                prev_end = max(prev_end, e.planned_end)
        candidates = [
            eq for eq in ctx.equipment.values()
            if eq.work_center_id == op.work_center_id and eq.status == "AVAILABLE"
            and (equipment_id is None or eq.id == equipment_id)
        ]
        best = None
        for eq in candidates:
            m = Machine(eq.id, eq.work_center_id, ctx.calendar_for(eq.id),
                        [Interval(e.planned_start, e.planned_end) for e in ctx.entries if e.equipment_id == eq.id])
            slot = earliest_slot(m, prev_end, ctx.duration(op), sch.horizon_end)
            if slot and (best is None or slot[1] < best[1][1]):
                best = (eq.id, slot)
        if best is None:
            raise ValidationFailed("no free slot in the horizon for this operation")
        equipment_id, (start_at, end_at) = best
    else:
        _validate_placement(ctx, op, equipment_id)
        start_at, end_at = _timing(ctx, op, equipment_id, start)
        _check_overlap(ctx, equipment_id, start_at, end_at, None)

    entry = ScheduleEntry(
        plant_id=sch.plant_id,
        schedule_id=sch.id,
        work_order_id=op.work_order_id,
        operation_id=op.id,
        equipment_id=equipment_id,
        planned_start=start_at,
        planned_end=end_at,
        locked=False,
    )
    s.add(entry)
    await s.flush()
    return entry


async def move_entry(
    s: AsyncSession,
    user: CurrentUser,
    schedule_id: UUID,
    entry_id: UUID,
    *,
    equipment_id: UUID,
    start: datetime,
    locked: bool | None,
    row_version: int | None,
) -> ScheduleEntry:
    sch = await _get_schedule(s, schedule_id, user, lock=True)
    _require_draft(sch)
    ctx = await load_context(s, sch)
    entry = next((e for e in ctx.entries if e.id == entry_id), None)
    if entry is None:
        raise NotFound("schedule entry not found")
    if row_version is not None and entry.row_version != row_version:
        raise StaleVersion("entry was changed by someone else; reload the board")
    op = ctx.ops[entry.operation_id]
    _validate_placement(ctx, op, equipment_id)
    start_at, end_at = _timing(ctx, op, equipment_id, start)
    _check_overlap(ctx, equipment_id, start_at, end_at, entry.id)
    entry.equipment_id = equipment_id
    entry.planned_start = start_at
    entry.planned_end = end_at
    if locked is not None:
        entry.locked = locked
    await s.flush()
    return entry


async def set_entry_lock(s: AsyncSession, user: CurrentUser, schedule_id: UUID, entry_id: UUID, locked: bool) -> ScheduleEntry:
    sch = await _get_schedule(s, schedule_id, user, lock=True)
    _require_draft(sch)
    entry = (await s.scalars(select(ScheduleEntry).where(ScheduleEntry.id == entry_id, ScheduleEntry.schedule_id == sch.id))).first()
    if entry is None:
        raise NotFound("schedule entry not found")
    entry.locked = locked
    await s.flush()
    return entry


async def remove_entry(s: AsyncSession, user: CurrentUser, schedule_id: UUID, entry_id: UUID) -> None:
    sch = await _get_schedule(s, schedule_id, user, lock=True)
    _require_draft(sch)
    res = await s.execute(delete(ScheduleEntry).where(ScheduleEntry.id == entry_id, ScheduleEntry.schedule_id == sch.id))
    if res.rowcount == 0:
        raise NotFound("schedule entry not found")


async def run_auto_sequence(
    s: AsyncSession, user: CurrentUser, schedule_id: UUID, *, reset_unlocked: bool
) -> dict[str, Any]:
    sch = await _get_schedule(s, schedule_id, user, lock=True)
    _require_draft(sch)
    if reset_unlocked:
        await s.execute(delete(ScheduleEntry).where(ScheduleEntry.schedule_id == sch.id, ScheduleEntry.locked.is_(False)))
        await s.flush()
    ctx = await load_context(s, sch)

    busy: dict[UUID, list[Interval]] = defaultdict(list)
    fixed_by_wo: dict[UUID, dict[UUID, FixedEntry]] = defaultdict(dict)
    for e in ctx.entries:
        busy[e.equipment_id].append(Interval(e.planned_start, e.planned_end))
        fixed_by_wo[e.work_order_id][e.operation_id] = FixedEntry(e.operation_id, e.equipment_id, e.planned_start, e.planned_end)

    machines = [
        Machine(eq.id, eq.work_center_id, ctx.calendar_for(eq.id), busy[eq.id])
        for eq in ctx.equipment.values()
        if eq.status == "AVAILABLE"
    ]
    ops_by_wo: dict[UUID, list[WorkOrderOperation]] = defaultdict(list)
    for o in ctx.ops.values():
        ops_by_wo[o.work_order_id].append(o)
    schedulable = sorted(
        (w for w in ctx.work_orders.values() if WOStatus(w.status) in SCHEDULABLE),
        key=lambda w: (w.priority, w.erp_basic_end, w.erp_order_number),
    )
    orders = [
        OrderRequest(
            work_order_id=w.id,
            ops=[
                OpRequest(o.id, w.id, o.sequence, o.work_center_id, ctx.duration(o))
                for o in sorted(ops_by_wo[w.id], key=lambda o: o.sequence)
                if o.status != OpStatus.CONFIRMED
            ],
            fixed=fixed_by_wo.get(w.id, {}),
        )
        for w in schedulable
    ]
    not_before = max(sch.horizon_start, datetime.now(UTC))
    placements, unplaced = auto_sequence(orders, machines, not_before, sch.horizon_end)
    for p in placements:
        s.add(
            ScheduleEntry(
                plant_id=sch.plant_id,
                schedule_id=sch.id,
                work_order_id=p.work_order_id,
                operation_id=p.operation_id,
                equipment_id=p.equipment_id,
                planned_start=p.start,
                planned_end=p.end,
                locked=False,
            )
        )
    await s.flush()
    return {
        "placed": len(placements),
        "unplaced": [
            {
                "operation_id": u.operation_id,
                "work_order_id": u.work_order_id,
                "erp_order_number": ctx.work_orders[u.work_order_id].erp_order_number,
                "op_no": ctx.ops[u.operation_id].op_no,
                "reason": u.reason,
            }
            for u in unplaced
        ],
    }


async def publish(s: AsyncSession, user: CurrentUser, schedule_id: UUID, *, reason: str, row_version: int | None) -> Schedule:
    sch = await _get_schedule(s, schedule_id, user, lock=True)
    _require_draft(sch)
    if row_version is not None and sch.row_version != row_version:
        raise StaleVersion("schedule was changed by someone else; reload")
    ctx = await load_context(s, sch)
    errors = [c for c in _conflicts(ctx) if c.severity == "error"]
    if errors:
        raise DomainError(
            f"schedule has {len(errors)} blocking conflict(s)",
            code="schedule_has_errors",
            details=[{"kind": c.kind, "message": c.message} for c in errors],
        )
    sig = await sign(
        s, user, meaning="APPROVED", subject_type="planning.schedule", subject_id=sch.id, reason=reason, plant_id=sch.plant_id
    )

    previous = (
        await s.scalars(
            select(Schedule)
            .where(Schedule.plant_id == sch.plant_id, Schedule.line_id.is_(None), Schedule.status == ScheduleStatus.PUBLISHED)
            .with_for_update()
        )
    ).first()
    if previous:
        previous.status = ScheduleStatus.SUPERSEDED.value
        await s.flush()
    sch.status = ScheduleStatus.PUBLISHED.value
    sch.published_at = datetime.now(UTC)
    sch.published_by = user.username
    sch.signature_id = sig.id

    entries_by_wo: dict[UUID, list[ScheduleEntry]] = defaultdict(list)
    for e in ctx.entries:
        entries_by_wo[e.work_order_id].append(e)
    for wo in ctx.work_orders.values():
        wo_entries = entries_by_wo.get(wo.id, [])
        open_ops = [o for o in ctx.ops.values() if o.work_order_id == wo.id and o.status != OpStatus.CONFIRMED]
        if wo_entries:
            start = min(e.planned_start for e in wo_entries)
            end = max(e.planned_end for e in wo_entries)
            if wo.sched_start != start:
                wo.sched_start = start
            if wo.sched_end != end:
                wo.sched_end = end
            fully = {e.operation_id for e in wo_entries} >= {o.id for o in open_ops}
            if wo.status == WOStatus.RELEASED and fully:
                await transition(s, wo, WOStatus.SCHEDULED, reason=f"schedule v{sch.version} published")
        elif wo.status == WOStatus.SCHEDULED:
            wo.sched_start = None
            wo.sched_end = None
            await transition(s, wo, WOStatus.RELEASED, reason=f"not in schedule v{sch.version}")
    await s.flush()
    await messaging.publish(
        s,
        topic=Topics.MES_PLANNING,
        key=str(sch.plant_id),
        event_type=EVENT_SCHEDULE_PUBLISHED,
        payload={
            "schedule_id": str(sch.id),
            "plant_id": str(sch.plant_id),
            "version": sch.version,
            "entries": len(ctx.entries),
            "signature_id": str(sig.id),
        },
    )
    return sch


async def discard(s: AsyncSession, user: CurrentUser, schedule_id: UUID) -> Schedule:
    sch = await _get_schedule(s, schedule_id, user, lock=True)
    _require_draft(sch)
    sch.status = ScheduleStatus.DISCARDED.value
    await s.flush()
    return sch
