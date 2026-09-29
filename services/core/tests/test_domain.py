"""Pure unit tests (no database): working calendar, scheduling heuristic, state machine."""

from datetime import UTC, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from mes_core.modules.planning.domain.scheduling import (
    Machine,
    OpRequest,
    OrderRequest,
    auto_sequence,
    operation_duration_min,
)
from mes_core.modules.planning.domain.states import WOStatus, check_transition, erp_target_status
from mes_core.shared.calendar import Interval, ShiftDef, WorkingCalendar, expand_shifts
from mes_core.shared.errors import InvalidTransition

MON = datetime(2026, 9, 21, tzinfo=UTC)  # a Monday


def cal() -> WorkingCalendar:
    shifts = [ShiftDef(time(6), time(14), (1, 2, 3, 4, 5)), ShiftDef(time(14), time(22), (1, 2, 3, 4, 5))]
    return WorkingCalendar(expand_shifts(shifts, ZoneInfo("UTC"), MON, MON + timedelta(days=14)))


def test_shifts_merge_into_one_window_per_weekday() -> None:
    c = cal()
    assert c.intervals[0] == Interval(MON.replace(hour=6), MON.replace(hour=22))
    assert len(c.intervals) == 10  # two work weeks


def test_overnight_shift_ends_next_day() -> None:
    ivs = expand_shifts([ShiftDef(time(22), time(6), (1,))], ZoneInfo("UTC"), MON, MON + timedelta(days=2))
    assert ivs == [Interval(MON.replace(hour=22), MON.replace(hour=6) + timedelta(days=1))]


def test_add_working_minutes_skips_nights_and_weekends() -> None:
    c = cal()
    friday_21 = MON + timedelta(days=4, hours=21)
    assert c.add_working(friday_21, 120) == MON + timedelta(days=7, hours=7)  # 60 min Fri + 60 min Mon from 06:00
    assert c.next_working(MON + timedelta(days=5)) == MON + timedelta(days=7, hours=6)
    assert c.is_working(MON.replace(hour=6)) and not c.is_working(MON.replace(hour=22))


def test_duration_uses_remaining_quantity() -> None:
    assert operation_duration_min(30, 4, 20, 0) == 110
    assert operation_duration_min(30, 4, 20, 15) == 50
    assert operation_duration_min(0, 0, 1, 1) == 5  # floor


def test_auto_sequence_respects_precedence_capacity_and_priority() -> None:
    wc_a, wc_b = uuid4(), uuid4()
    m1, m2 = Machine(uuid4(), wc_a, cal()), Machine(uuid4(), wc_b, cal())
    orders = []
    for _ in range(2):
        wo = uuid4()
        orders.append(OrderRequest(wo, [OpRequest(uuid4(), wo, 1, wc_a, 120), OpRequest(uuid4(), wo, 2, wc_b, 60)]))
    start = MON.replace(hour=6)
    placements, unplaced = auto_sequence(orders, [m1, m2], start, MON + timedelta(days=14))
    assert not unplaced
    p = {x.operation_id: x for x in placements}
    o1a, o1b = (p[o.operation_id] for o in orders[0].ops)
    o2a, o2b = (p[o.operation_id] for o in orders[1].ops)
    assert o1a.start == start and o1a.end == start + timedelta(minutes=120)
    assert o1b.start == o1a.end  # successor right after predecessor
    assert o2a.start == o1a.end  # same machine: waits for the first order
    assert o2b.start >= o2a.end


def test_auto_sequence_reports_ops_without_equipment() -> None:
    wo = uuid4()
    order = OrderRequest(wo, [OpRequest(uuid4(), wo, 1, uuid4(), 30), OpRequest(uuid4(), wo, 2, uuid4(), 30)])
    placements, unplaced = auto_sequence([order], [], MON, MON + timedelta(days=1))
    assert not placements
    assert [u.reason for u in unplaced] == ["no available equipment in work center", "predecessor could not be placed"]


def test_state_machine() -> None:
    check_transition(WOStatus.SCHEDULED, WOStatus.DISPATCHED)
    with pytest.raises(InvalidTransition):
        check_transition(WOStatus.RELEASED, WOStatus.COMPLETED)
    assert erp_target_status(None, {"CRTD"}) == WOStatus.IMPORTED
    assert erp_target_status(WOStatus.IMPORTED, {"REL"}) == WOStatus.RELEASED
    assert erp_target_status(WOStatus.IN_PROGRESS, {"REL", "TECO"}) == WOStatus.CLOSED
    assert erp_target_status(WOStatus.CLOSED, {"DLFL"}) is None
    assert erp_target_status(WOStatus.SCHEDULED, {"REL", "PCNF"}) is None
