"""Work order / operation / schedule state machines (pure)."""

from enum import StrEnum

from mes_core.shared.errors import InvalidTransition


class WOStatus(StrEnum):
    IMPORTED = "IMPORTED"  # known from ERP, not yet released there
    RELEASED = "RELEASED"  # released in ERP: can be scheduled
    SCHEDULED = "SCHEDULED"  # all open operations are in the published schedule
    DISPATCHED = "DISPATCHED"  # released to the shop floor
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"  # every operation finally confirmed
    CLOSED = "CLOSED"  # technically completed in ERP
    ON_HOLD = "ON_HOLD"
    CANCELLED = "CANCELLED"  # deletion flag in ERP


class OpStatus(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"  # partially confirmed
    CONFIRMED = "CONFIRMED"  # finally confirmed


class ScheduleStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    SUPERSEDED = "SUPERSEDED"
    DISCARDED = "DISCARDED"


W = WOStatus
TERMINAL = frozenset({W.CLOSED, W.CANCELLED})
SCHEDULABLE = frozenset({W.RELEASED, W.SCHEDULED, W.DISPATCHED, W.IN_PROGRESS})
CONFIRMABLE = frozenset({W.DISPATCHED, W.IN_PROGRESS})
HOLDABLE = frozenset({W.RELEASED, W.SCHEDULED, W.DISPATCHED, W.IN_PROGRESS})

TRANSITIONS: dict[WOStatus, frozenset[WOStatus]] = {
    W.IMPORTED: frozenset({W.RELEASED, W.CANCELLED, W.CLOSED}),
    W.RELEASED: frozenset({W.SCHEDULED, W.ON_HOLD, W.CANCELLED, W.CLOSED}),
    W.SCHEDULED: frozenset({W.RELEASED, W.DISPATCHED, W.ON_HOLD, W.CANCELLED, W.CLOSED}),
    W.DISPATCHED: frozenset({W.SCHEDULED, W.IN_PROGRESS, W.COMPLETED, W.ON_HOLD, W.CANCELLED, W.CLOSED}),
    W.IN_PROGRESS: frozenset({W.COMPLETED, W.ON_HOLD, W.CANCELLED, W.CLOSED}),
    W.ON_HOLD: frozenset(HOLDABLE | {W.CANCELLED, W.CLOSED}),
    W.COMPLETED: frozenset({W.CLOSED}),
    W.CLOSED: frozenset(),
    W.CANCELLED: frozenset(),
}


def check_transition(current: WOStatus, target: WOStatus) -> None:
    if target not in TRANSITIONS[current]:
        raise InvalidTransition(
            f"work order cannot go from {current} to {target}",
            details={"from": current, "to": target, "allowed": sorted(TRANSITIONS[current])},
        )


def erp_target_status(current: WOStatus | None, erp_status: set[str]) -> WOStatus | None:
    """What an ERP change implies for the MES status. None = no change.

    ERP drives only the edges of the lifecycle: release, technical completion, deletion.
    """
    if current in TERMINAL:
        return None
    if "DLFL" in erp_status:
        return W.CANCELLED
    if "TECO" in erp_status:
        return W.CLOSED
    if current is None:
        return W.RELEASED if "REL" in erp_status else W.IMPORTED
    if current == W.IMPORTED and "REL" in erp_status:
        return W.RELEASED
    return None
