"""Ticket rules (pure): the support severity matrix, tiers, SLA clocks and the status workflow."""

from dataclasses import dataclass
from datetime import datetime, timedelta

# P1..P4: (target response, target fix); None = scheduled with the change process
SEVERITY_TARGETS: dict[str, tuple[timedelta, timedelta | None]] = {
    "P1": (timedelta(minutes=15), timedelta(hours=2)),
    "P2": (timedelta(minutes=30), timedelta(hours=6)),
    "P3": (timedelta(hours=2), timedelta(hours=24)),
    "P4": (timedelta(hours=8), None),
}
SEVERITY_LABELS = {
    "P1": "Critical: line stop (total outage)",
    "P2": "High: degraded production (workaround)",
    "P3": "Medium: minor impact (non-blocking)",
    "P4": "Low: service request / change",
}
IMPACT_SEVERITY = {"LINE_STOP": "P1", "DEGRADED": "P2", "MINOR": "P3", "SERVICE_REQUEST": "P4"}
IMPACT_LABELS = {
    "LINE_STOP": "Line is stopped",
    "DEGRADED": "Production continues with a workaround",
    "MINOR": "Minor, not blocking production",
    "SERVICE_REQUEST": "Request or change (no fault)",
}
CATEGORY_LABELS = {
    "MES_APP": "MES application / terminal",
    "HARDWARE": "Scanner, printer, terminal hardware",
    "INTEGRATION": "SAP / system interface",
    "DATA": "Data problem (stuck lot, WIP, wrong totals)",
    "ACCESS": "User access / PIN",
    "CHANGE_REQUEST": "Change request (routing, recipe, report)",
    "OTHER": "Other",
}
TIERS = {
    "L1": ("L1 Support: first line & monitoring", "24/7 operations", "Response < 15 min"),
    "L2": ("L2 Support: technical escalations", "App & DB engineering", "Resolution 2-6 h"),
    "L3": ("L3 Support: code bugs & architecture", "Core dev & SME", "Resolution 8-48 h"),
}
TIER_ORDER = ("L1", "L2", "L3")

TRANSITIONS: dict[str, frozenset[str]] = {
    "NEW": frozenset({"ACKNOWLEDGED", "IN_PROGRESS", "ON_HOLD", "RESOLVED", "CANCELLED"}),
    "ACKNOWLEDGED": frozenset({"IN_PROGRESS", "ON_HOLD", "RESOLVED", "CANCELLED"}),
    "IN_PROGRESS": frozenset({"ON_HOLD", "RESOLVED", "CANCELLED"}),
    "ON_HOLD": frozenset({"IN_PROGRESS", "RESOLVED", "CANCELLED"}),
    "RESOLVED": frozenset({"CLOSED", "IN_PROGRESS"}),  # reopen
    "CLOSED": frozenset(),
    "CANCELLED": frozenset(),
}


def suggest_severity(impact: str, category: str) -> str:
    """Severity follows shop-floor impact. Access and change requests are service requests unless the line is down."""
    if category in ("ACCESS", "CHANGE_REQUEST") and impact != "LINE_STOP":
        return "P4"
    return IMPACT_SEVERITY[impact]


def initial_tier(category: str, source: str) -> str:
    """Everything starts at L1 (triage), except automatic interface/data alarms, which need L2 at once."""
    if source == "AUTOMATIC" and category in ("INTEGRATION", "DATA"):
        return "L2"
    return "L1"


def due_dates(severity: str, reported_at: datetime) -> tuple[datetime, datetime | None]:
    resp, fix = SEVERITY_TARGETS[severity]
    return reported_at + resp, (reported_at + fix if fix else None)


@dataclass(frozen=True)
class Clock:
    due: datetime | None
    met_at: datetime | None
    remaining_s: float | None  # negative when breached
    breached: bool
    paused: bool = False


def response_clock(due: datetime, acknowledged_at: datetime | None, now: datetime) -> Clock:
    if acknowledged_at is not None:
        return Clock(due, acknowledged_at, None, acknowledged_at > due)
    left = (due - now).total_seconds()
    return Clock(due, None, left, left < 0)


def resolution_clock(
    due: datetime | None, resolved_at: datetime | None, held_s: int, on_hold_since: datetime | None, now: datetime,
) -> Clock:
    """On-hold time (waiting for the reporter or a vendor) moves the target out."""
    if due is None:
        return Clock(None, resolved_at, None, False)
    held = held_s + ((now - on_hold_since).total_seconds() if on_hold_since else 0)
    effective = due + timedelta(seconds=held)
    if resolved_at is not None:
        return Clock(effective, resolved_at, None, resolved_at > effective)
    left = (effective - now).total_seconds()
    return Clock(effective, None, left, left < 0, paused=on_hold_since is not None)


AT_RISK_SHARE = 0.75  # a target is "at risk" once this share of its time is used


def at_risk(clock: Clock, reported_at: datetime, now: datetime) -> bool:
    """Not met, not breached yet, and at least 75% of the time to the (effective) due date is used."""
    if clock.due is None or clock.met_at is not None or clock.breached or clock.paused:
        return False
    total = (clock.due - reported_at).total_seconds()
    return total > 0 and (now - reported_at).total_seconds() >= AT_RISK_SHARE * total


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def next_tier(tier: str) -> str | None:
    i = TIER_ORDER.index(tier)
    return TIER_ORDER[i + 1] if i + 1 < len(TIER_ORDER) else None
