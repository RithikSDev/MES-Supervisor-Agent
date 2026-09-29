"""Maintenance rules (pure)."""

from dataclasses import dataclass
from datetime import datetime, timedelta

DOWNTIME_REASONS: dict[str, str] = {
    "MECHANICAL": "Mechanical failure",
    "ELECTRICAL": "Electrical failure",
    "HYDRAULIC": "Hydraulic / pneumatic",
    "CONTROLS": "Controls / PLC / software",
    "TOOLING": "Tooling / fixture",
    "COOLANT": "Coolant / lubrication",
    "UTILITIES": "Utilities (air, power, water)",
    "OTHER": "Other",
}
PLANNED_REASON = "PLANNED_MAINTENANCE"
# expected repair time used to block a machine that is down in scheduling until it is back
BREAKDOWN_BLOCK = timedelta(hours=4)


def breakdown_priority(machine_down: bool) -> int:
    """SAP PM priority: a machine that is down is 1 (very high), a degraded one 2 (high)."""
    return 1 if machine_down else 2


@dataclass(frozen=True)
class PlanState:
    interval_type: str  # CALENDAR | RUNTIME
    interval_days: int | None
    interval_hours: float | None
    lead_days: int
    last_done_at: datetime | None
    created_at: datetime


def calendar_due(p: PlanState) -> datetime | None:
    if p.interval_type != "CALENDAR" or not p.interval_days:
        return None
    return (p.last_done_at or p.created_at) + timedelta(days=p.interval_days)


def is_due(p: PlanState, now: datetime, runtime_since_last_h: float) -> bool:
    """A calendar plan is due `lead_days` before its date; a runtime plan at 90 % of its running hours."""
    if p.interval_type == "CALENDAR":
        due = calendar_due(p)
        return due is not None and now >= due - timedelta(days=p.lead_days)
    return bool(p.interval_hours) and runtime_since_last_h >= 0.9 * float(p.interval_hours or 0)


@dataclass(frozen=True)
class Reliability:
    failures: int
    downtime_h: float
    mttr_h: float | None  # mean time to repair (closed breakdowns)
    mtbf_h: float | None  # mean running time between failures


def reliability(breakdowns: list[tuple[datetime, datetime | None]], runtime_h: float, now: datetime) -> Reliability:
    """breakdowns: (start, end or None if still down) of unplanned downtime in the window."""
    closed = [(e - s).total_seconds() / 3600 for s, e in breakdowns if e is not None]
    total = sum(((e or now) - s).total_seconds() / 3600 for s, e in breakdowns)
    n = len(breakdowns)
    return Reliability(
        failures=n,
        downtime_h=round(total, 2),
        mttr_h=round(sum(closed) / len(closed), 2) if closed else None,
        mtbf_h=round(runtime_h / n, 2) if n else None,
    )
