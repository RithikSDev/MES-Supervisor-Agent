"""Shop-floor execution: operation runs driven from the terminal.

Every action carries an optional `client_event_id` (generated on the terminal). A replayed action (offline queue
flushing after an outage, or a retry after a lost response) returns the current state instead of acting twice.
Runs may also be created with a client-generated id, so an offline terminal can queue "start" and then
"pause"/"report" for that run before the server has seen either.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mes_common.events import Topics
from mes_common.ids import new_id
from mes_core.modules.execution.domain import runs as rd
from mes_core.modules.execution.models import OperationEvent, OperationRun, ProductionConfirmation
from mes_core.modules.execution.service import ConfirmationInput, create_confirmation
from mes_core.modules.inventory.public import serials_of_work_order
from mes_core.modules.masterdata.public import get_materials
from mes_core.modules.org.public import EquipmentRef, get_equipment, get_terminal, list_equipment
from mes_core.modules.planning.public import (
    get_operation_context,
    get_work_order_brief,
    mark_operation_started,
    operations_brief,
)
from mes_core.modules.quality.public import ensure_in_process_inspection, inspection_of_operation
from mes_core.shared.errors import DomainError, NotFound, ValidationFailed
from mes_core.shared.messaging import messaging
from mes_core.shared.users import CurrentUser

log = logging.getLogger(__name__)

EVENT_RUN_CHANGED = "mes.execution.run_changed"
MAX_BACKDATE = timedelta(hours=24)
MAX_CLOCK_SKEW = timedelta(minutes=2)


@dataclass(frozen=True)
class Meta:
    client_event_id: UUID | None = None
    occurred_at: datetime | None = None


def effective_time(occurred_at: datetime | None) -> datetime:
    """Trust the terminal's timestamp for replayed events, within limits (no future, no older than a day)."""
    now = datetime.now(UTC)
    if occurred_at is None:
        return now
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    if occurred_at > now + MAX_CLOCK_SKEW or occurred_at < now - MAX_BACKDATE:
        log.warning("ignoring implausible terminal timestamp %s", occurred_at)
        return now
    return min(occurred_at, now)


def _clock(run: OperationRun) -> rd.Clock:
    return rd.Clock(
        status=rd.RunStatus(run.status),
        phase_started_at=run.phase_started_at,
        paused_from=rd.RunStatus(run.paused_from) if run.paused_from else None,
        setup_s=run.setup_s,
        run_s=run.run_s,
        paused_s=run.paused_s,
        setup_reported_s=run.setup_reported_s,
        run_reported_s=run.run_reported_s,
    )


def _store(run: OperationRun, c: rd.Clock) -> None:
    run.status = c.status.value
    run.phase_started_at = c.phase_started_at
    run.paused_from = c.paused_from.value if c.paused_from else None
    run.setup_s, run.run_s, run.paused_s = c.setup_s, c.run_s, c.paused_s
    run.setup_reported_s, run.run_reported_s = c.setup_reported_s, c.run_reported_s


async def _replayed(s: AsyncSession, client_event_id: UUID | None) -> OperationEvent | None:
    if client_event_id is None:
        return None
    return (await s.scalars(select(OperationEvent).where(OperationEvent.client_event_id == client_event_id))).first()


async def _event(
    s: AsyncSession, run: OperationRun, user: CurrentUser, event_type: str, at: datetime, meta: Meta,
    reason_code: str | None = None, note: str | None = None, details: dict[str, Any] | None = None,
) -> None:
    s.add(
        OperationEvent(
            plant_id=run.plant_id, run_id=run.id, work_order_id=run.work_order_id, operation_id=run.operation_id,
            equipment_id=run.equipment_id, terminal_id=user.terminal_id, operator_id=user.id,
            operator_name=user.display_name or user.username, event_type=event_type, reason_code=reason_code,
            note=note, details=details, occurred_at=at, recorded_at=datetime.now(UTC),
            client_event_id=meta.client_event_id,
        )
    )
    await s.flush()
    await messaging.publish(
        s,
        topic=Topics.MES_EXECUTION,
        key=str(run.equipment_id),
        event_type=EVENT_RUN_CHANGED,
        payload={
            "run_id": str(run.id), "event_type": event_type, "status": run.status, "plant_id": str(run.plant_id),
            "work_order_id": str(run.work_order_id), "operation_id": str(run.operation_id),
            "equipment_id": str(run.equipment_id), "operator_id": str(user.id), "occurred_at": at.isoformat(),
            "reason_code": reason_code, **(details or {}),
        },
    )


async def _machine(s: AsyncSession, plant_id: UUID, equipment_id: UUID) -> EquipmentRef:
    eq = next((e for e in await list_equipment(s, plant_id) if e.id == equipment_id), None)
    if eq is None:
        raise NotFound("machine not found")
    return eq


# ------------------------------------------------------------------------------------------------ actions


async def start_run(
    s: AsyncSession, user: CurrentUser, *, wo_id: UUID, op_id: UUID, equipment_id: UUID, phase: str,
    run_id: UUID | None, meta: Meta,
) -> tuple[OperationRun, bool]:
    """Returns (run, replayed)."""
    if (ev := await _replayed(s, meta.client_event_id)) is not None:
        return await get_run(s, ev.run_id), True
    if run_id and (existing := await s.get(OperationRun, run_id)) is not None:
        return existing, True
    ctx = await get_operation_context(s, wo_id, op_id)
    user.ensure_plant(ctx.plant_id)
    eq = await _machine(s, ctx.plant_id, equipment_id)
    if eq.work_center_id != ctx.work_center_id:
        raise ValidationFailed(f"{eq.code} is not in this operation's work center", code="work_center_mismatch")
    if eq.status != "AVAILABLE":
        raise DomainError(f"{eq.code} is {eq.status}", code="machine_unavailable")
    if user.terminal_id:
        terminal = await get_terminal(s, user.terminal_id)
        if terminal.line_id != eq.line_id:
            raise DomainError(f"{eq.code} is not on this terminal's line ({terminal.line_code})", code="wrong_line")
    busy = (
        await s.scalars(
            select(OperationRun).where(
                OperationRun.status != "ENDED",
                (OperationRun.equipment_id == equipment_id) | (OperationRun.operation_id == op_id),
            )
        )
    ).first()
    if busy:
        what = "this operation is already running" if busy.operation_id == op_id else f"{eq.code} is busy with another operation"
        raise DomainError(what, code="run_conflict", details={"run_id": str(busy.id)})

    at = effective_time(meta.occurred_at)
    await mark_operation_started(s, wo_id, op_id)
    clock = rd.start(rd.RunStatus.SETUP if phase == "SETUP" else rd.RunStatus.RUNNING, at)
    run = OperationRun(
        id=run_id or new_id(), plant_id=ctx.plant_id, work_order_id=wo_id, operation_id=op_id, equipment_id=equipment_id,
        terminal_id=user.terminal_id, operator_id=user.id, operator_name=user.display_name or user.username,
        status=clock.status.value, started_at=at, phase_started_at=at,
    )
    s.add(run)
    await s.flush()
    await _event(s, run, user, "START_SETUP" if phase == "SETUP" else "START_RUN", at, meta)
    await ensure_in_process_inspection(s, work_order_id=wo_id, operation_id=op_id, run_id=run.id)
    return run, False


async def _ensure_not_on_hold(s: AsyncSession, run: OperationRun) -> None:
    wo = await get_work_order_brief(s, run.work_order_id)
    if wo.status == "ON_HOLD":
        raise DomainError(f"order {wo.erp_order_number} is on hold (quality); a supervisor must release it first",
                          code="work_order_on_hold")
    eq = await get_equipment(s, run.equipment_id)
    if eq.status != "AVAILABLE":
        raise DomainError(f"{eq.code} is {eq.status.lower()}: maintenance must release it first", code="machine_unavailable")
    # after a failed first-off check and the hold's release, the check is repeated
    await ensure_in_process_inspection(s, work_order_id=run.work_order_id, operation_id=run.operation_id, run_id=run.id)


async def prepare_report(s: AsyncSession, run_id: UUID) -> None:
    """Open the operation's first-off inspection lot if one is due, in its own transaction before a report (the
    report itself is refused until the inspection has passed, and a refusal must not roll the lot back)."""
    run = await get_run(s, run_id)
    if run.status != "ENDED" and (await get_work_order_brief(s, run.work_order_id)).status != "ON_HOLD":
        await ensure_in_process_inspection(s, work_order_id=run.work_order_id, operation_id=run.operation_id, run_id=run.id)


async def get_run(s: AsyncSession, run_id: UUID, *, lock: bool = False) -> OperationRun:
    q = select(OperationRun).where(OperationRun.id == run_id)
    if lock:
        q = q.with_for_update()
    run = (await s.scalars(q)).first()
    if run is None:
        raise NotFound("run not found")
    return run


async def _load_active(s: AsyncSession, user: CurrentUser, run_id: UUID) -> OperationRun:
    run = await get_run(s, run_id, lock=True)
    user.ensure_plant(run.plant_id)
    if run.status == "ENDED":
        raise DomainError("this run has already ended", code="run_ended")
    # whoever acts now is the operator on the machine
    name = user.display_name or user.username
    if run.operator_id != user.id:
        run.operator_id, run.operator_name = user.id, name
    return run


async def start_production(s: AsyncSession, user: CurrentUser, run_id: UUID, meta: Meta) -> tuple[OperationRun, bool]:
    if (ev := await _replayed(s, meta.client_event_id)) is not None:
        return await get_run(s, ev.run_id), True
    run = await _load_active(s, user, run_id)
    await _ensure_not_on_hold(s, run)
    at = effective_time(meta.occurred_at)
    _store(run, rd.start_production(_clock(run), at))
    await _event(s, run, user, "START_PRODUCTION", at, meta)
    return run, False


async def pause(s: AsyncSession, user: CurrentUser, run_id: UUID, reason_code: str, note: str | None, meta: Meta) -> tuple[OperationRun, bool]:
    if (ev := await _replayed(s, meta.client_event_id)) is not None:
        return await get_run(s, ev.run_id), True
    if reason_code not in rd.PAUSE_REASONS:
        raise ValidationFailed(f"unknown pause reason {reason_code}")
    run = await _load_active(s, user, run_id)
    at = effective_time(meta.occurred_at)
    _store(run, rd.pause(_clock(run), at))
    run.pause_reason = reason_code
    await _event(s, run, user, "PAUSE", at, meta, reason_code=reason_code, note=note)
    return run, False


async def resume(s: AsyncSession, user: CurrentUser, run_id: UUID, meta: Meta) -> tuple[OperationRun, bool]:
    if (ev := await _replayed(s, meta.client_event_id)) is not None:
        return await get_run(s, ev.run_id), True
    run = await _load_active(s, user, run_id)
    await _ensure_not_on_hold(s, run)
    at = effective_time(meta.occurred_at)
    _store(run, rd.resume(_clock(run), at))
    run.pause_reason = None
    await _event(s, run, user, "RESUME", at, meta)
    return run, False


async def report(
    s: AsyncSession, user: CurrentUser, run_id: UUID, *, yield_qty: Decimal, scrap_qty: Decimal,
    scrap_reason: str | None, final: bool, note: str | None, meta: Meta,
) -> tuple[OperationRun, bool]:
    """Report quantities (partial confirmation) or complete the operation (final confirmation, run ends)."""
    if (ev := await _replayed(s, meta.client_event_id)) is not None:
        return await get_run(s, ev.run_id), True
    if yield_qty < 0 or scrap_qty < 0:
        raise ValidationFailed("quantities must not be negative")
    if scrap_qty > 0 and scrap_reason not in rd.SCRAP_REASONS:
        raise ValidationFailed("choose a scrap reason", code="scrap_reason_required")
    run = await _load_active(s, user, run_id)
    at = effective_time(meta.occurred_at)
    clock, setup_min, run_min = rd.report(_clock(run), at)
    if final:
        clock = rd.end(clock, at)
    _store(run, clock)

    def minutes(m: float) -> Decimal:
        return Decimal(str(round(m, 2)))

    conf = await create_confirmation(
        s, user, run.work_order_id, run.operation_id,
        ConfirmationInput(
            yield_qty=yield_qty, scrap_qty=scrap_qty, setup_min=minutes(setup_min), run_min=minutes(run_min),
            labor_min=minutes(setup_min + run_min), final=final, equipment_id=run.equipment_id, comment=note,
            source="TERMINAL" if user.terminal_id else "MANUAL", run_id=run.id,
            scrap_reason=scrap_reason if scrap_qty > 0 else None,
            client_event_id=meta.client_event_id, reported_at=at,
        ),
    )
    run.yield_qty += yield_qty
    run.scrap_qty += scrap_qty
    if final:
        run.ended_at = at
    await _event(
        s, run, user, "COMPLETE" if final else "REPORT", at, meta, reason_code=scrap_reason if scrap_qty > 0 else None,
        note=note, details={"yield_qty": str(yield_qty), "scrap_qty": str(scrap_qty), "confirmation_id": str(conf.id),
                            "setup_min": round(setup_min, 2), "run_min": round(run_min, 2)},
    )
    return run, False


async def stop(s: AsyncSession, user: CurrentUser, run_id: UUID, note: str | None, meta: Meta) -> tuple[OperationRun, bool]:
    """End the run without finishing the operation (shift end, moving to another machine)."""
    if (ev := await _replayed(s, meta.client_event_id)) is not None:
        return await get_run(s, ev.run_id), True
    run = await _load_active(s, user, run_id)
    at = effective_time(meta.occurred_at)
    _store(run, rd.end(_clock(run), at))
    run.ended_at = at
    run.pause_reason = None
    await _event(s, run, user, "STOP", at, meta, note=note)
    return run, False


# ------------------------------------------------------------------------------------------------ views


async def run_view(s: AsyncSession, run: OperationRun, *, replayed: bool = False) -> dict[str, Any]:
    wo = await get_work_order_brief(s, run.work_order_id)
    op = next(o for o in await operations_brief(s, run.work_order_id) if o.id == run.operation_id)
    mat = (await get_materials(s, {wo.material_id})).get(wo.material_id)
    eq = next((e for e in await list_equipment(s, run.plant_id) if e.id == run.equipment_id), None)
    totals = _clock(run).totals_at(datetime.now(UTC)) if run.status != "ENDED" else _clock(run)
    insp = await inspection_of_operation(s, run.operation_id)
    serials = [x.serial for x in await serials_of_work_order(s, run.work_order_id)][-20:] if op.is_last else []
    return {
        "wo_status": wo.status,
        "inspection": {"id": insp.id, "number": insp.number, "status": insp.status, "blocking": insp.blocking}
        if insp else None,
        "serials": serials,
        "id": run.id, "plant_id": run.plant_id, "work_order_id": run.work_order_id,
        "erp_order_number": wo.erp_order_number, "material_number": mat.material_number if mat else None,
        "material_description": mat.description if mat else None,
        "operation_id": run.operation_id, "op_no": op.op_no, "op_description": op.description,
        "op_status": op.status, "op_planned_qty": op.planned_qty, "op_yield_qty": op.yield_qty,
        "op_scrap_qty": op.scrap_qty, "uom": op.uom, "is_last_operation": op.is_last,
        "equipment_id": run.equipment_id, "equipment_code": eq.code if eq else None,
        "line_id": eq.line_id if eq else None, "terminal_id": run.terminal_id,
        "operator_id": run.operator_id, "operator_name": run.operator_name,
        "status": run.status, "paused_from": run.paused_from, "pause_reason": run.pause_reason,
        "started_at": run.started_at, "phase_started_at": run.phase_started_at, "ended_at": run.ended_at,
        "setup_s": round(totals.setup_s), "run_s": round(totals.run_s), "paused_s": round(totals.paused_s),
        "yield_qty": run.yield_qty, "scrap_qty": run.scrap_qty, "replayed": replayed,
    }


async def equipment_states(s: AsyncSession, plant_id: UUID, line_id: UUID | None = None) -> list[dict[str, Any]]:
    """Every machine with its active run (if any): the live board's source of truth."""
    machines = [e for e in await list_equipment(s, plant_id) if line_id is None or e.line_id == line_id]
    active = {
        r.equipment_id: r
        for r in (
            await s.scalars(select(OperationRun).where(OperationRun.plant_id == plant_id, OperationRun.status != "ENDED"))
        ).all()
    }
    out = []
    for m in machines:
        run = active.get(m.id)
        out.append(await machine_state(s, m, run))
    return out


async def machine_state(s: AsyncSession, m: EquipmentRef, run: OperationRun | None) -> dict[str, Any]:
    base = {
        "equipment_id": str(m.id), "equipment_code": m.code, "equipment_name": m.name, "work_center_code": m.work_center_code,
        "line_id": str(m.line_id), "line_code": m.line_code, "equipment_status": m.status,
        "state": "IDLE", "updated_at": datetime.now(UTC).isoformat(),
    }
    if run is None:
        return base
    v = await run_view(s, run)
    return {
        **base, "state": run.status, "run_id": str(run.id), "work_order_id": str(run.work_order_id),
        "erp_order_number": v["erp_order_number"], "op_no": v["op_no"], "op_description": v["op_description"],
        "material_number": v["material_number"], "operator_name": run.operator_name,
        "since": run.phase_started_at.isoformat(), "started_at": run.started_at.isoformat(),
        "pause_reason": run.pause_reason, "op_planned_qty": str(v["op_planned_qty"]),
        "op_yield_qty": str(v["op_yield_qty"]), "op_scrap_qty": str(v["op_scrap_qty"]), "uom": v["uom"],
    }


async def state_for_run(s: AsyncSession, run: OperationRun) -> dict[str, Any]:
    m = next(e for e in await list_equipment(s, run.plant_id) if e.id == run.equipment_id)
    return await machine_state(s, m, run if run.status != "ENDED" else None)


# ------------------------------------------------------------------------------------------------ history (genealogy)


@dataclass(frozen=True)
class ExecutionStep:
    operation_id: UUID
    equipment_id: UUID | None
    operator_name: str | None
    started_at: datetime
    ended_at: datetime | None
    yield_qty: Decimal
    scrap_qty: Decimal
    source: str  # RUN (terminal) | CONFIRMATION (reported without a run)


async def execution_steps(s: AsyncSession, work_order_id: UUID) -> list[ExecutionStep]:
    """Who worked on which operation, on which machine, when — runs plus confirmations made outside a run."""
    runs = (await s.scalars(select(OperationRun).where(OperationRun.work_order_id == work_order_id)
                            .order_by(OperationRun.started_at))).all()
    out = [ExecutionStep(r.operation_id, r.equipment_id, r.operator_name, r.started_at, r.ended_at, r.yield_qty,
                         r.scrap_qty, "RUN") for r in runs]
    confs = (await s.scalars(select(ProductionConfirmation).where(ProductionConfirmation.work_order_id == work_order_id,
                                                                   ProductionConfirmation.run_id.is_(None))
                             .order_by(ProductionConfirmation.reported_at))).all()
    out += [ExecutionStep(c.operation_id, c.equipment_id, c.reported_by_name, c.reported_at, c.reported_at, c.yield_qty,
                          c.scrap_qty, "CONFIRMATION") for c in confs]
    return sorted(out, key=lambda x: x.started_at)


async def equipment_runtime_hours(s: AsyncSession, equipment_ids: list[UUID], since: datetime | None = None) -> dict[UUID, float]:
    """Machine running hours (setup + run, pauses excluded) from operation runs started since `since`."""
    runs = (await s.scalars(select(OperationRun).where(OperationRun.equipment_id.in_(equipment_ids),
                                                       *([OperationRun.started_at >= since] if since else [])))).all()
    out = dict.fromkeys(equipment_ids, 0.0)
    now = datetime.now(UTC)
    for r in runs:
        t = _clock(r).totals_at(now) if r.status != "ENDED" else _clock(r)
        out[r.equipment_id] += (t.setup_s + t.run_s) / 3600
    return out


async def active_run_on(s: AsyncSession, equipment_id: UUID) -> OperationRun | None:
    return (await s.scalars(select(OperationRun).where(OperationRun.equipment_id == equipment_id,
                                                       OperationRun.status != "ENDED"))).first()


async def pause_for_breakdown(s: AsyncSession, user: CurrentUser, equipment_id: UUID, reason: str) -> UUID | None:
    """A machine reported down: its running/setup run is paused (MACHINE_ISSUE). Returns the run id, if any."""
    run = await active_run_on(s, equipment_id)
    if run is None:
        return None
    if run.status != "PAUSED":
        run = await get_run(s, run.id, lock=True)
        at = datetime.now(UTC)
        _store(run, rd.pause(_clock(run), at))
        run.pause_reason = "MACHINE_ISSUE"
        await _event(s, run, user, "PAUSE", at, Meta(), reason_code="MACHINE_ISSUE", note=reason)
    return run.id


async def state_for_equipment(s: AsyncSession, equipment_id: UUID) -> dict[str, Any]:
    """Live-board state of one machine (e.g. after maintenance changed its availability)."""
    return await machine_state(s, await get_equipment(s, equipment_id), await active_run_on(s, equipment_id))
