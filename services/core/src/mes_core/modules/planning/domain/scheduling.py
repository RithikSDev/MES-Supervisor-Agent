"""Finite-capacity scheduling helpers (pure, no I/O).

auto_sequence: earliest-due-date, forward, greedy. Orders arrive pre-sorted (priority, due date). Each operation goes
to the machine in its work center that finishes it earliest, after its predecessor ends, inside working time and
without overlapping anything already on that machine. Good enough to demo and easy to explain; an optimiser
(OR-Tools CP-SAT) can replace it behind the same inputs later.
"""

import bisect
import math
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from mes_core.shared.calendar import Interval, WorkingCalendar

MIN_OPERATION_MINUTES = 5


def operation_duration_min(
    setup_min: float, run_min_per_unit: float, planned_qty: float, done_qty: float
) -> int:
    remaining = max(planned_qty - done_qty, 0)
    return max(MIN_OPERATION_MINUTES, math.ceil(setup_min + remaining * run_min_per_unit))


@dataclass(frozen=True)
class OpRequest:
    operation_id: UUID
    work_order_id: UUID
    sequence: int
    work_center_id: UUID
    duration_min: int


@dataclass(frozen=True)
class FixedEntry:
    operation_id: UUID
    equipment_id: UUID
    start: datetime
    end: datetime


@dataclass
class OrderRequest:
    work_order_id: UUID
    ops: list[OpRequest]  # open operations in routing sequence
    fixed: dict[UUID, FixedEntry] = field(default_factory=dict)  # already on the board (kept)
    not_before: datetime | None = None  # e.g. end of an already-confirmed predecessor


@dataclass
class Machine:
    id: UUID
    work_center_id: UUID
    calendar: WorkingCalendar
    busy: list[Interval] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.busy.sort()


@dataclass(frozen=True)
class Placement:
    operation_id: UUID
    work_order_id: UUID
    equipment_id: UUID
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Unplaced:
    operation_id: UUID
    work_order_id: UUID
    reason: str


def earliest_slot(
    machine: Machine, earliest: datetime, duration_min: int, horizon_end: datetime
) -> tuple[datetime, datetime] | None:
    t = machine.calendar.next_working(earliest)
    while t is not None and t < horizon_end:
        end = machine.calendar.add_working(t, duration_min)
        if end is None or end > horizon_end:
            return None
        clash = next((b for b in machine.busy if b.start < end and t < b.end), None)
        if clash is None:
            return t, end
        t = machine.calendar.next_working(clash.end)
    return None


def auto_sequence(
    orders: list[OrderRequest], machines: list[Machine], not_before: datetime, horizon_end: datetime
) -> tuple[list[Placement], list[Unplaced]]:
    by_wc: dict[UUID, list[Machine]] = {}
    for m in machines:
        by_wc.setdefault(m.work_center_id, []).append(m)

    placements: list[Placement] = []
    unplaced: list[Unplaced] = []
    for order in orders:
        prev_end = max(not_before, order.not_before or not_before)
        blocked_reason: str | None = None
        for op in order.ops:
            fixed = order.fixed.get(op.operation_id)
            if fixed is not None:
                prev_end = max(prev_end, fixed.end)
                continue
            if blocked_reason:
                unplaced.append(Unplaced(op.operation_id, op.work_order_id, blocked_reason))
                continue
            candidates = by_wc.get(op.work_center_id, [])
            if not candidates:
                blocked_reason = "predecessor could not be placed"
                unplaced.append(Unplaced(op.operation_id, op.work_order_id, "no available equipment in work center"))
                continue
            best: tuple[Machine, tuple[datetime, datetime]] | None = None
            for m in candidates:
                slot = earliest_slot(m, prev_end, op.duration_min, horizon_end)
                if slot and (best is None or slot[1] < best[1][1]):
                    best = (m, slot)
            if best is None:
                blocked_reason = "predecessor could not be placed"
                unplaced.append(Unplaced(op.operation_id, op.work_order_id, "does not fit in the schedule horizon"))
                continue
            machine, (start, end) = best
            bisect.insort(machine.busy, Interval(start, end))
            placements.append(Placement(op.operation_id, op.work_order_id, machine.id, start, end))
            prev_end = end
    return placements, unplaced


# --------------------------------------------------------------------------------------------- conflicts


@dataclass(frozen=True)
class EntryView:
    entry_id: UUID
    operation_id: UUID
    work_order_id: UUID
    equipment_id: UUID
    start: datetime
    end: datetime


@dataclass(frozen=True)
class OpView:
    operation_id: UUID
    work_order_id: UUID
    sequence: int
    op_no: str
    work_center_id: UUID
    status: str


@dataclass(frozen=True)
class OrderView:
    work_order_id: UUID
    erp_order_number: str
    status: str
    due: datetime
    schedulable: bool


@dataclass(frozen=True)
class Conflict:
    kind: str
    severity: str  # "error" blocks publishing, "warning" does not
    message: str
    work_order_id: UUID
    operation_id: UUID | None = None
    entry_id: UUID | None = None


def evaluate_conflicts(
    entries: list[EntryView],
    ops: dict[UUID, OpView],
    orders: dict[UUID, OrderView],
    equipment_wc: dict[UUID, UUID],
    equipment_calendar: dict[UUID, WorkingCalendar],
    horizon: Interval,
) -> list[Conflict]:
    out: list[Conflict] = []
    by_op = {e.operation_id: e for e in entries}
    for e in entries:
        op = ops.get(e.operation_id)
        wo = orders.get(e.work_order_id)
        if op is None or wo is None:
            continue
        label = f"{wo.erp_order_number}/{op.op_no}"
        if not wo.schedulable:
            out.append(Conflict("WO_NOT_SCHEDULABLE", "error", f"{label}: work order is {wo.status}",
                                e.work_order_id, e.operation_id, e.entry_id))
        if equipment_wc.get(e.equipment_id) != op.work_center_id:
            out.append(Conflict("WORK_CENTER_MISMATCH", "error", f"{label}: machine is not in the operation's work center",
                                e.work_order_id, e.operation_id, e.entry_id))
        if e.end > wo.due:
            out.append(Conflict("LATE", "warning", f"{label}: finishes after due date {wo.due:%Y-%m-%d}",
                                e.work_order_id, e.operation_id, e.entry_id))
        cal = equipment_calendar.get(e.equipment_id)
        if cal is not None and not cal.is_working(e.start):
            out.append(Conflict("OUTSIDE_SHIFT", "warning", f"{label}: starts outside working time",
                                e.work_order_id, e.operation_id, e.entry_id))
        if e.start < horizon.start or e.end > horizon.end:
            out.append(Conflict("OUTSIDE_HORIZON", "warning", f"{label}: outside the schedule horizon",
                                e.work_order_id, e.operation_id, e.entry_id))
        if op.status == "CONFIRMED":
            out.append(Conflict("ALREADY_CONFIRMED", "warning", f"{label}: operation is already confirmed",
                                e.work_order_id, e.operation_id, e.entry_id))

    # precedence within each work order
    ops_by_wo: dict[UUID, list[OpView]] = {}
    for op in ops.values():
        ops_by_wo.setdefault(op.work_order_id, []).append(op)
    for wo_ops in ops_by_wo.values():
        wo_ops.sort(key=lambda o: o.sequence)
        prev_end: datetime | None = None
        prev_label: str | None = None
        for op in wo_ops:
            e = by_op.get(op.operation_id)
            if e is None:
                continue
            wo = orders[op.work_order_id]
            if prev_end is not None and e.start < prev_end:
                out.append(Conflict("PRECEDENCE", "error",
                                    f"{wo.erp_order_number}/{op.op_no} starts before {prev_label} ends",
                                    op.work_order_id, op.operation_id, e.entry_id))
            prev_end = e.end if prev_end is None else max(prev_end, e.end)
            prev_label = f"{wo.erp_order_number}/{op.op_no}"
    return out
