"""Inspections, usage decisions and nonconformances.

Stock consequences go through inventory (stock type changes 321/350/344/343, scrap 555, return 122) so SAP sees
every quality decision as a goods movement. Decisions that release or dispose of stock are e-signed.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mes_common.events import Topics
from mes_core.modules.compliance.public import sign
from mes_core.modules.inventory.public import (
    BLOCKED,
    QUALITY,
    UNRESTRICTED,
    change_status,
    get_lot_info,
    receive,
    scrap_blocked,
    stock_of_lot,
)
from mes_core.modules.masterdata.public import get_materials
from mes_core.modules.planning.public import get_work_order_brief, hold_for_quality, operations_brief
from mes_core.modules.quality import domain
from mes_core.modules.quality.models import (
    INSPECTION_LOT_SEQ,
    NC_SEQ,
    InspectionCharacteristic,
    InspectionLot,
    InspectionPlan,
    InspectionResult,
    Nonconformance,
)
from mes_core.shared.errors import DomainError, NotFound, ValidationFailed
from mes_core.shared.messaging import messaging
from mes_core.shared.users import CurrentUser

EVENT_INSPECTION_CHANGED = "mes.quality.inspection_changed"
EVENT_NC_CHANGED = "mes.quality.nc_changed"


def _now() -> datetime:
    return datetime.now(UTC)


def _name(user: CurrentUser) -> str:
    return user.display_name or user.username


async def _emit(s: AsyncSession, event_type: str, plant_id: UUID, payload: dict[str, Any]) -> None:
    await messaging.publish(
        s, topic=Topics.MES_QUALITY, key=str(plant_id), event_type=event_type,
        payload={"plant_id": str(plant_id), **{k: str(v) if isinstance(v, UUID | Decimal) else v for k, v in payload.items()}},
    )


# ------------------------------------------------------------------------------------------------ plans


async def find_plan(
    s: AsyncSession, plant_id: UUID, material_id: UUID, inspection_type: str, op_no: str | None = None
) -> InspectionPlan | None:
    q = select(InspectionPlan).where(
        InspectionPlan.plant_id == plant_id, InspectionPlan.material_id == material_id,
        InspectionPlan.inspection_type == inspection_type, InspectionPlan.active.is_(True),
    )
    q = q.where(InspectionPlan.op_no == op_no) if op_no else q.where(InspectionPlan.op_no.is_(None))
    return (await s.scalars(q)).first()


async def characteristics(s: AsyncSession, plan_id: UUID) -> list[InspectionCharacteristic]:
    return list(
        (await s.scalars(select(InspectionCharacteristic).where(InspectionCharacteristic.plan_id == plan_id)
                         .order_by(InspectionCharacteristic.seq))).all()
    )


@dataclass(frozen=True)
class CharacteristicInput:
    name: str
    kind: str
    target: Decimal | None = None
    lower_limit: Decimal | None = None
    upper_limit: Decimal | None = None
    uom: str | None = None


async def upsert_plan(
    s: AsyncSession, *, plant_id: UUID, material_id: UUID, inspection_type: str, op_no: str | None, name: str,
    sample_size: int, blocking: bool, active: bool, chars: list[CharacteristicInput],
) -> InspectionPlan:
    """Create or replace a plan. Characteristics are versioned by replacement only while no lot uses the plan."""
    if inspection_type not in ("INCOMING", "IN_PROCESS", "FINAL"):
        raise ValidationFailed("unknown inspection type")
    if (inspection_type == "IN_PROCESS") != bool(op_no):
        raise ValidationFailed("an in-process plan needs an operation number (and only that type does)")
    if not chars:
        raise ValidationFailed("a plan needs at least one characteristic")
    for c in chars:
        if c.kind == "QUANTITATIVE" and c.lower_limit is None and c.upper_limit is None:
            raise ValidationFailed(f"{c.name}: a quantitative characteristic needs a tolerance")
        if c.lower_limit is not None and c.upper_limit is not None and c.lower_limit > c.upper_limit:
            raise ValidationFailed(f"{c.name}: lower limit above upper limit")
    plan = (
        await s.scalars(
            select(InspectionPlan).where(
                InspectionPlan.plant_id == plant_id, InspectionPlan.material_id == material_id,
                InspectionPlan.inspection_type == inspection_type,
                InspectionPlan.op_no == op_no if op_no else InspectionPlan.op_no.is_(None),
            )
        )
    ).first()
    if plan is None:
        plan = InspectionPlan(plant_id=plant_id, material_id=material_id, inspection_type=inspection_type, op_no=op_no,
                              name=name, sample_size=sample_size, blocking=blocking, active=active)
        s.add(plan)
        await s.flush()
    else:
        plan.name, plan.sample_size, plan.blocking, plan.active = name, sample_size, blocking, active
    existing = await characteristics(s, plan.id)
    new = [(c.name, c.kind, c.target, c.lower_limit, c.upper_limit, c.uom) for c in chars]
    old = [(c.name, c.kind, c.target, c.lower_limit, c.upper_limit, c.uom) for c in existing]
    if new != old:
        used = (await s.execute(select(func.count()).select_from(InspectionLot).where(InspectionLot.plan_id == plan.id))).scalar_one()
        if used and existing:
            raise DomainError("this plan's characteristics are used by inspection lots; create a new plan instead",
                              code="plan_in_use")
        for c in existing:
            await s.delete(c)
        await s.flush()
        for i, c in enumerate(chars, start=1):
            s.add(InspectionCharacteristic(plan_id=plan.id, seq=i, name=c.name, kind=c.kind, target=c.target,
                                           lower_limit=c.lower_limit, upper_limit=c.upper_limit, uom=c.uom))
    await s.flush()
    return plan


# ------------------------------------------------------------------------------------------------ inspection lots


async def _create_lot(
    s: AsyncSession, plan: InspectionPlan, *, material_id: UUID, work_order_id: UUID | None = None,
    operation_id: UUID | None = None, run_id: UUID | None = None, lot_id: UUID | None = None,
    quantity: Decimal | None = None,
) -> InspectionLot:
    seq = (await s.execute(select(INSPECTION_LOT_SEQ.next_value()))).scalar_one()
    il = InspectionLot(
        plant_id=plan.plant_id, number=f"Q{seq:08d}", plan_id=plan.id, inspection_type=plan.inspection_type,
        status="OPEN", material_id=material_id, work_order_id=work_order_id, operation_id=operation_id, run_id=run_id,
        lot_id=lot_id, quantity=quantity, sample_size=plan.sample_size,
    )
    s.add(il)
    await s.flush()
    await _emit(s, EVENT_INSPECTION_CHANGED, il.plant_id, {"inspection_lot_id": il.id, "number": il.number,
                                                           "status": il.status, "inspection_type": il.inspection_type,
                                                           "work_order_id": work_order_id, "operation_id": operation_id})
    return il


async def get_inspection_lot(s: AsyncSession, inspection_lot_id: UUID, *, lock: bool = False) -> InspectionLot:
    q = select(InspectionLot).where(InspectionLot.id == inspection_lot_id)
    if lock:
        q = q.with_for_update()
    il = (await s.scalars(q)).first()
    if il is None:
        raise NotFound("inspection lot not found")
    return il


async def _latest_for_operation(s: AsyncSession, operation_id: UUID) -> InspectionLot | None:
    return (
        await s.scalars(
            select(InspectionLot)
            .where(InspectionLot.operation_id == operation_id, InspectionLot.inspection_type == "IN_PROCESS",
                   InspectionLot.status != "CANCELLED")
            .order_by(InspectionLot.created_at.desc())
        )
    ).first()


async def ensure_in_process_inspection(
    s: AsyncSession, *, work_order_id: UUID, operation_id: UUID, run_id: UUID | None = None
) -> InspectionLot | None:
    """First-off inspection of an operation, if its material has an in-process plan for that operation.

    Re-created after a failure (the order was on hold; once a supervisor resumes it, the check is repeated)."""
    wo = await get_work_order_brief(s, work_order_id)
    op = next((o for o in await operations_brief(s, work_order_id) if o.id == operation_id), None)
    if op is None:
        return None
    plan = await find_plan(s, wo.plant_id, wo.material_id, "IN_PROCESS", op.op_no)
    if plan is None:
        return None
    latest = await _latest_for_operation(s, operation_id)
    if latest is not None and latest.status in ("OPEN", "PASSED"):
        return latest
    return await _create_lot(s, plan, material_id=wo.material_id, work_order_id=work_order_id, operation_id=operation_id,
                             run_id=run_id)


async def check_report_allowed(s: AsyncSession, *, work_order_id: UUID, operation_id: UUID) -> None:
    """Good quantity can't be reported on an operation whose blocking first-off inspection has not passed."""
    il = await ensure_in_process_inspection(s, work_order_id=work_order_id, operation_id=operation_id)
    if il is None or il.status == "PASSED":
        return
    plan = await s.get(InspectionPlan, il.plan_id)
    if plan and plan.blocking:
        raise DomainError(
            f"first-off inspection {il.number} must pass before quantities can be reported",
            code="inspection_required", details={"inspection_lot_id": str(il.id), "number": il.number},
        )


async def final_inspection_plan(s: AsyncSession, plant_id: UUID, material_id: UUID) -> InspectionPlan | None:
    return await find_plan(s, plant_id, material_id, "FINAL")


async def create_final_inspection(
    s: AsyncSession, *, plan_id: UUID, work_order_id: UUID, lot_id: UUID, material_id: UUID, quantity: Decimal
) -> InspectionLot:
    plan = await s.get(InspectionPlan, plan_id)
    assert plan is not None
    return await _create_lot(s, plan, material_id=material_id, work_order_id=work_order_id, lot_id=lot_id, quantity=quantity)


async def receive_with_inspection(
    s: AsyncSession, *, plant_id: UUID, material_id: UUID, quantity: Decimal, location_id: UUID,
    lot_number: str | None, reason: str | None,
) -> tuple[Any, InspectionLot | None]:
    """Goods receipt from a supplier (501). With an incoming plan the stock goes to quality inspection stock."""
    plan = await find_plan(s, plant_id, material_id, "INCOMING")
    mv = await receive(s, plant_id=plant_id, material_id=material_id, quantity=quantity, location_id=location_id,
                       lot_number=lot_number, reason=reason, to_status=QUALITY if plan else UNRESTRICTED)
    il = None
    if plan:
        il = await _create_lot(s, plan, material_id=material_id, lot_id=mv.lot_id, quantity=mv.quantity)
    return mv, il


@dataclass(frozen=True)
class ResultInput:
    characteristic_id: UUID
    sample_no: int
    measured_value: Decimal | None = None
    attribute_ok: bool | None = None
    note: str | None = None


async def record_results(
    s: AsyncSession, user: CurrentUser, inspection_lot_id: UUID, results: list[ResultInput]
) -> InspectionLot:
    """Record (or correct) measured values while the lot is open; evaluates the lot when complete.

    A failed lot opens a nonconformance and puts the work order on hold."""
    il = await get_inspection_lot(s, inspection_lot_id, lock=True)
    user.ensure_plant(il.plant_id)
    if il.status != "OPEN":
        raise DomainError(f"inspection lot {il.number} is {il.status}", code="inspection_closed")
    if not results:
        raise ValidationFailed("no results")
    chars = {c.id: c for c in await characteristics(s, il.plan_id)}
    existing = {(r.characteristic_id, r.sample_no): r for r in (
        await s.scalars(select(InspectionResult).where(InspectionResult.inspection_lot_id == il.id))).all()}
    now = _now()
    for r in results:
        c = chars.get(r.characteristic_id)
        if c is None:
            raise ValidationFailed("characteristic is not part of this inspection plan")
        if not 1 <= r.sample_no <= il.sample_size:
            raise ValidationFailed(f"sample number must be 1..{il.sample_size}")
        try:
            ok = domain.conforming(domain.Characteristic(c.id, c.kind, c.lower_limit, c.upper_limit),
                                   r.measured_value, r.attribute_ok)
        except ValueError as e:
            raise ValidationFailed(f"{c.name}: {e}") from e
        row = existing.get((c.id, r.sample_no))
        if row is None:
            row = InspectionResult(plant_id=il.plant_id, inspection_lot_id=il.id, characteristic_id=c.id,
                                   sample_no=r.sample_no, conforming=ok, recorded_at=now, recorded_by=str(user.id))
            s.add(row)
            existing[(c.id, r.sample_no)] = row
        row.measured_value = r.measured_value if c.kind == "QUANTITATIVE" else None
        row.attribute_ok = r.attribute_ok if c.kind == "QUALITATIVE" else None
        row.conforming, row.note = ok, r.note
        row.recorded_at, row.recorded_by, row.recorded_by_name = now, str(user.id), _name(user)
    await s.flush()

    verdict = domain.evaluate(list(chars), il.sample_size, {k: v.conforming for k, v in existing.items()})
    if verdict:
        il.status, il.evaluated_at = verdict, now
        if verdict == "FAILED":
            await _on_failed(s, user, il, chars, existing)
    await s.flush()
    await _emit(s, EVENT_INSPECTION_CHANGED, il.plant_id, {"inspection_lot_id": il.id, "number": il.number,
                                                           "status": il.status, "inspection_type": il.inspection_type,
                                                           "work_order_id": il.work_order_id,
                                                           "operation_id": il.operation_id})
    return il


async def _on_failed(
    s: AsyncSession, user: CurrentUser, il: InspectionLot, chars: dict[UUID, InspectionCharacteristic],
    results: dict[tuple[UUID, int], InspectionResult],
) -> None:
    bad = [r for r in results.values() if not r.conforming]
    names = sorted({chars[r.characteristic_id].name for r in bad})
    quantitative = any(chars[r.characteristic_id].kind == "QUANTITATIVE" for r in bad)
    details = "; ".join(
        f"{chars[r.characteristic_id].name} #{r.sample_no}: "
        + (f"{r.measured_value:f}" if r.measured_value is not None else "not OK")
        for r in bad
    )
    lot_uom = (await get_lot_info(s, il.lot_id)).uom if il.lot_id else None
    await _new_nc(
        s, user, source="INSPECTION", severity="MAJOR", defect_code="DIMENSION" if quantitative else "FUNCTION",
        description=f"Inspection {il.number} failed ({', '.join(names)}): {details}", material_id=il.material_id,
        work_order_id=il.work_order_id, operation_id=il.operation_id, lot_id=il.lot_id, inspection_lot_id=il.id,
        quantity=il.quantity, uom=lot_uom,
    )
    if il.work_order_id and il.inspection_type in ("IN_PROCESS", "FINAL"):
        await hold_for_quality(s, il.work_order_id, f"quality hold: inspection {il.number} failed")


async def usage_decision(
    s: AsyncSession, user: CurrentUser, inspection_lot_id: UUID, *, decision: str, reason: str
) -> InspectionLot:
    """Accept (321: quality -> unrestricted) or reject (350: quality -> blocked) the inspected stock. E-signed.

    A failed lot can only be rejected here; accepting nonconforming stock is a nonconformance disposition
    (use as is), which carries its own justification."""
    il = await get_inspection_lot(s, inspection_lot_id, lock=True)
    user.ensure_plant(il.plant_id)
    if il.usage_decision:
        raise DomainError(f"inspection lot {il.number} already has a usage decision", code="decision_made")
    if decision not in ("ACCEPTED", "REJECTED"):
        raise ValidationFailed("decision must be ACCEPTED or REJECTED")
    if decision == "ACCEPTED" and il.status != "PASSED":
        raise DomainError("only a passed inspection lot can be accepted; disposition its nonconformance instead",
                          code="inspection_not_passed")
    sig = await sign(s, user, meaning="APPROVED", subject_type="inspection_lot", subject_id=il.id,
                     reason=reason, plant_id=il.plant_id)
    await _decide(s, user, il, decision, sig.reason, sig.id)
    if il.lot_id:
        if decision == "ACCEPTED":
            await change_status(s, lot_id=il.lot_id, movement_type="321", reason=f"UD {il.number}: accepted")
        else:
            moved = await change_status(s, lot_id=il.lot_id, movement_type="350", reason=f"UD {il.number}: rejected")
            nc = await _open_nc_of_inspection(s, il.id)
            if nc is None:
                nc = await _new_nc(
                    s, user, source="INSPECTION", severity="MAJOR", defect_code="OTHER",
                    description=f"Inspection lot {il.number} rejected: {reason}", material_id=il.material_id,
                    work_order_id=il.work_order_id, lot_id=il.lot_id, inspection_lot_id=il.id, quantity=il.quantity,
                    uom=(await get_lot_info(s, il.lot_id)).uom,
                )
            nc.blocked_qty += sum((m.quantity for m in moved), Decimal(0))
    if il.status == "OPEN":  # rejected without complete results
        il.status, il.evaluated_at = "FAILED", _now()
    await s.flush()
    await _emit(s, EVENT_INSPECTION_CHANGED, il.plant_id, {"inspection_lot_id": il.id, "number": il.number,
                                                           "status": il.status, "usage_decision": decision})
    return il


async def _decide(s: AsyncSession, user: CurrentUser, il: InspectionLot, decision: str, reason: str, sig_id: UUID) -> None:
    il.usage_decision, il.decision_reason = decision, reason
    il.decided_at, il.decided_by, il.signature_id = _now(), _name(user), sig_id
    await s.flush()


# ------------------------------------------------------------------------------------------------ nonconformances


async def _new_nc(
    s: AsyncSession, user: CurrentUser, *, source: str, severity: str, defect_code: str, description: str,
    material_id: UUID | None = None, work_order_id: UUID | None = None, operation_id: UUID | None = None,
    lot_id: UUID | None = None, inspection_lot_id: UUID | None = None, quantity: Decimal | None = None,
    uom: str | None = None, client_event_id: UUID | None = None, plant_id: UUID | None = None,
    reported_at: datetime | None = None,
) -> Nonconformance:
    if plant_id is None:
        if work_order_id:
            plant_id = (await get_work_order_brief(s, work_order_id)).plant_id
        elif lot_id:
            plant_id = (await get_lot_info(s, lot_id)).plant_id
    if plant_id is None:
        raise ValidationFailed("a nonconformance needs a plant, work order or lot")
    seq = (await s.execute(select(NC_SEQ.next_value()))).scalar_one()
    nc = Nonconformance(
        plant_id=plant_id, number=f"NC{seq:07d}", status="OPEN", source=source, severity=severity,
        defect_code=defect_code, description=description, material_id=material_id, work_order_id=work_order_id,
        operation_id=operation_id, lot_id=lot_id, inspection_lot_id=inspection_lot_id, quantity=quantity, uom=uom,
        blocked_qty=Decimal(0), reported_at=reported_at or _now(), reported_by=str(user.id),
        reported_by_name=_name(user), client_event_id=client_event_id,
    )
    s.add(nc)
    await s.flush()
    await _emit(s, EVENT_NC_CHANGED, plant_id, {"nc_id": nc.id, "number": nc.number, "status": nc.status,
                                                "severity": severity, "work_order_id": work_order_id})
    return nc


async def _open_nc_of_inspection(s: AsyncSession, inspection_lot_id: UUID) -> Nonconformance | None:
    return (
        await s.scalars(select(Nonconformance).where(Nonconformance.inspection_lot_id == inspection_lot_id,
                                                     Nonconformance.status != "CLOSED"))
    ).first()


async def get_nc(s: AsyncSession, nc_id: UUID, *, lock: bool = False) -> Nonconformance:
    q = select(Nonconformance).where(Nonconformance.id == nc_id)
    if lock:
        q = q.with_for_update()
    nc = (await s.scalars(q)).first()
    if nc is None:
        raise NotFound("nonconformance not found")
    return nc


async def report_defect(
    s: AsyncSession, user: CurrentUser, *, work_order_id: UUID | None, operation_id: UUID | None, lot_id: UUID | None,
    defect_code: str, severity: str, description: str, quantity: Decimal | None, source: str = "OPERATOR",
    client_event_id: UUID | None = None, reported_at: datetime | None = None, plant_id: UUID | None = None,
) -> Nonconformance:
    """A defect found on the floor or at receiving. A critical defect on an order puts the order on hold."""
    if client_event_id:
        existing = (await s.scalars(select(Nonconformance).where(Nonconformance.client_event_id == client_event_id))).first()
        if existing:
            return existing
    if defect_code not in domain.DEFECT_CODES:
        raise ValidationFailed(f"unknown defect code {defect_code}")
    if severity not in domain.SEVERITIES:
        raise ValidationFailed(f"unknown severity {severity}")
    if not description.strip():
        raise ValidationFailed("describe the defect")
    material_id, uom = None, None
    if work_order_id:
        wo = await get_work_order_brief(s, work_order_id)
        user.ensure_plant(wo.plant_id)
        material_id, uom = wo.material_id, wo.uom
        if operation_id and operation_id not in {o.id for o in await operations_brief(s, work_order_id)}:
            raise NotFound("operation not found in this work order")
    if lot_id:
        lot = await get_lot_info(s, lot_id)
        user.ensure_plant(lot.plant_id)
        material_id, uom = lot.material_id, lot.uom
    if plant_id:
        user.ensure_plant(plant_id)
    nc = await _new_nc(
        s, user, source=source, severity=severity, defect_code=defect_code, description=description.strip(),
        material_id=material_id, work_order_id=work_order_id, operation_id=operation_id, lot_id=lot_id,
        quantity=quantity, uom=uom, client_event_id=client_event_id, plant_id=plant_id, reported_at=reported_at,
    )
    if severity == "CRITICAL" and work_order_id:
        await hold_for_quality(s, work_order_id, f"quality hold: critical defect {nc.number}")
    return nc


async def quarantine_lot(
    s: AsyncSession, user: CurrentUser, *, lot_id: UUID, reason: str, source: str = "MANUAL",
    nc: Nonconformance | None = None, signed: bool = True,
) -> Nonconformance:
    """Block all unrestricted and quality-inspection stock of a lot (344/350) under a nonconformance."""
    lot = await get_lot_info(s, lot_id)
    user.ensure_plant(lot.plant_id)
    if nc is None:
        nc = await _new_nc(s, user, source=source, severity="MAJOR", defect_code="OTHER",
                           description=f"Quarantine of lot {lot.lot_number}: {reason}", material_id=lot.material_id,
                           lot_id=lot_id, uom=lot.uom, plant_id=lot.plant_id)
    if signed:
        await sign(s, user, meaning="APPROVED", subject_type="lot_quarantine", subject_id=lot_id, reason=reason,
                   plant_id=lot.plant_id)
    moved = await change_status(s, lot_id=lot_id, movement_type="344", reason=f"{nc.number}: quarantine")
    moved += await change_status(s, lot_id=lot_id, movement_type="350", reason=f"{nc.number}: quarantine")
    nc.blocked_qty += sum((m.quantity for m in moved), Decimal(0))
    if nc.quantity is None:
        nc.quantity = nc.blocked_qty
    await s.flush()
    return nc


async def disposition(
    s: AsyncSession, user: CurrentUser, nc_id: UUID, *, disposition: str, reason: str
) -> Nonconformance:
    """Decide what happens to nonconforming material. E-signed. Stock of the NC's lot that is still in quality
    inspection or blocked is released (use as is), scrapped (555), returned to the supplier (122) or held blocked
    for rework."""
    nc = await get_nc(s, nc_id, lock=True)
    user.ensure_plant(nc.plant_id)
    if nc.status != "OPEN":
        raise DomainError(f"{nc.number} is {nc.status}", code="nc_not_open")
    if disposition not in domain.DISPOSITIONS:
        raise ValidationFailed(f"unknown disposition {disposition}")
    if disposition == "RETURN_TO_SUPPLIER" and nc.lot_id and (await get_lot_info(s, nc.lot_id)).origin not in ("RECEIPT", "OPENING"):
        raise DomainError("only purchased lots can be returned to the supplier", code="not_purchased")
    sig = await sign(s, user, meaning="APPROVED", subject_type="nonconformance", subject_id=nc.id, reason=reason,
                     plant_id=nc.plant_id)
    nc.disposition, nc.disposition_reason = disposition, sig.reason
    nc.dispositioned_at, nc.dispositioned_by, nc.signature_id = _now(), _name(user), sig.id

    il = await s.get(InspectionLot, nc.inspection_lot_id) if nc.inspection_lot_id else None
    if il is not None and il.usage_decision is None:
        await _decide(s, user, il, "ACCEPTED" if disposition == "USE_AS_IS" else "REJECTED",
                      f"{nc.number} {domain.DISPOSITIONS[disposition].lower()}: {sig.reason}", sig.id)
    tag = f"{nc.number} {disposition.lower().replace('_', ' ')}"
    if nc.lot_id:
        if disposition == "USE_AS_IS":
            await change_status(s, lot_id=nc.lot_id, movement_type="321", reason=tag)
            await change_status(s, lot_id=nc.lot_id, movement_type="343", reason=tag)
        else:
            moved = await change_status(s, lot_id=nc.lot_id, movement_type="350", reason=tag)
            nc.blocked_qty += sum((m.quantity for m in moved), Decimal(0))
            if disposition in ("SCRAP", "RETURN_TO_SUPPLIER"):
                await scrap_blocked(s, lot_id=nc.lot_id, quantity=None, reason=tag,
                                    movement_type="555" if disposition == "SCRAP" else "122")
    nc.status = "REWORK" if disposition == "REWORK" else "CLOSED"
    if nc.status == "CLOSED":
        nc.closed_at = _now()
    await s.flush()
    await _emit(s, EVENT_NC_CHANGED, nc.plant_id, {"nc_id": nc.id, "number": nc.number, "status": nc.status,
                                                   "disposition": disposition})
    return nc


async def complete_rework(
    s: AsyncSession, user: CurrentUser, nc_id: UUID, *, good_qty: Decimal, scrap_qty: Decimal, reason: str
) -> Nonconformance:
    """Rework finished: reworked good quantity goes back to unrestricted stock (343), the rest is scrapped (555)."""
    nc = await get_nc(s, nc_id, lock=True)
    user.ensure_plant(nc.plant_id)
    if nc.status != "REWORK":
        raise DomainError(f"{nc.number} is not in rework", code="nc_not_in_rework")
    if good_qty < 0 or scrap_qty < 0:
        raise ValidationFailed("quantities must not be negative")
    if nc.lot_id:
        blocked = sum((q for _, st, q in await stock_of_lot(s, nc.lot_id) if st == BLOCKED), Decimal(0))
        if good_qty + scrap_qty != blocked:
            raise ValidationFailed(f"good + scrap must account for the blocked quantity ({blocked:f} {nc.uom or ''})".strip(),
                                   code="rework_quantity_mismatch")
        mats = await get_materials(s, {nc.material_id}) if nc.material_id else {}
        mat = mats.get(nc.material_id) if nc.material_id else None
        if mat and mat.is_serialized and scrap_qty > 0 and good_qty > 0:
            raise ValidationFailed("serialised lot: rework all units to good or all to scrap (split by serial is not supported)")
    sig = await sign(s, user, meaning="APPROVED", subject_type="nonconformance", subject_id=nc.id, reason=reason,
                     plant_id=nc.plant_id)
    tag = f"{nc.number} rework"
    if nc.lot_id:
        if good_qty > 0:
            await change_status(s, lot_id=nc.lot_id, movement_type="343", reason=tag, quantity=good_qty)
        if scrap_qty > 0:
            await scrap_blocked(s, lot_id=nc.lot_id, quantity=scrap_qty, reason=tag)
    nc.rework_good_qty, nc.rework_scrap_qty = good_qty, scrap_qty
    nc.status, nc.closed_at = "CLOSED", _now()
    nc.disposition_reason = f"{nc.disposition_reason} | rework done: {sig.reason}"
    await s.flush()
    await _emit(s, EVENT_NC_CHANGED, nc.plant_id, {"nc_id": nc.id, "number": nc.number, "status": nc.status})
    return nc


# ------------------------------------------------------------------------------------------------ reads for other modules


@dataclass(frozen=True)
class InspectionBrief:
    id: UUID
    number: str
    inspection_type: str
    status: str
    usage_decision: str | None
    work_order_id: UUID | None
    operation_id: UUID | None
    lot_id: UUID | None
    blocking: bool
    created_at: datetime


async def _briefs(s: AsyncSession, q) -> list[InspectionBrief]:  # type: ignore[no-untyped-def]
    rows = (await s.execute(q.add_columns(InspectionPlan.blocking).join(InspectionPlan, InspectionPlan.id == InspectionLot.plan_id))).all()
    return [
        InspectionBrief(il.id, il.number, il.inspection_type, il.status, il.usage_decision, il.work_order_id,
                        il.operation_id, il.lot_id, blocking, il.created_at)
        for il, blocking in rows
    ]


async def inspection_of_operation(s: AsyncSession, operation_id: UUID) -> InspectionBrief | None:
    rows = await _briefs(
        s,
        select(InspectionLot).where(InspectionLot.operation_id == operation_id, InspectionLot.inspection_type == "IN_PROCESS",
                                    InspectionLot.status != "CANCELLED").order_by(InspectionLot.created_at.desc()).limit(1),
    )
    return rows[0] if rows else None


async def inspections_of_work_order(s: AsyncSession, work_order_id: UUID) -> list[InspectionBrief]:
    return await _briefs(s, select(InspectionLot).where(InspectionLot.work_order_id == work_order_id).order_by(InspectionLot.created_at))


async def inspections_of_lot(s: AsyncSession, lot_id: UUID) -> list[InspectionBrief]:
    return await _briefs(s, select(InspectionLot).where(InspectionLot.lot_id == lot_id).order_by(InspectionLot.created_at))


@dataclass(frozen=True)
class NcBrief:
    id: UUID
    number: str
    status: str
    severity: str
    defect_code: str
    description: str
    disposition: str | None
    work_order_id: UUID | None
    lot_id: UUID | None
    reported_at: datetime


def _nc_brief(nc: Nonconformance) -> NcBrief:
    return NcBrief(nc.id, nc.number, nc.status, nc.severity, nc.defect_code, nc.description, nc.disposition,
                   nc.work_order_id, nc.lot_id, nc.reported_at)


async def ncs_of(s: AsyncSession, *, work_order_ids: list[UUID] | None = None, lot_ids: list[UUID] | None = None) -> list[NcBrief]:
    conds = []
    if work_order_ids:
        conds.append(Nonconformance.work_order_id.in_(work_order_ids))
    if lot_ids:
        conds.append(Nonconformance.lot_id.in_(lot_ids))
    if not conds:
        return []
    rows = (await s.scalars(select(Nonconformance).where(or_(*conds)).order_by(Nonconformance.reported_at))).all()
    return [_nc_brief(x) for x in rows]
