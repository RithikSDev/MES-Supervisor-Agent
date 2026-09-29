from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mes_core.modules.inventory.public import get_lot_info, stock_of_lot
from mes_core.modules.masterdata.public import get_materials
from mes_core.modules.planning.public import get_work_order_brief, operations_brief
from mes_core.modules.quality import domain
from mes_core.modules.quality import service as q
from mes_core.modules.quality.models import (
    InspectionCharacteristic,
    InspectionLot,
    InspectionPlan,
    InspectionResult,
    Nonconformance,
)
from mes_core.shared.errors import ValidationFailed
from mes_core.shared.permissions import Perm
from mes_core.web.deps import DbDep, request_id, require

router = APIRouter(tags=["quality"])


# ------------------------------------------------------------------------------------------------ models


class CodeLabel(BaseModel):
    code: str
    label: str


class QualityMeta(BaseModel):
    defect_codes: list[CodeLabel]
    severities: list[str]
    dispositions: list[CodeLabel]


class CharacteristicIO(BaseModel):
    id: UUID | None = None
    name: str = Field(min_length=1)
    kind: Literal["QUANTITATIVE", "QUALITATIVE"]
    target: Decimal | None = None
    lower_limit: Decimal | None = None
    upper_limit: Decimal | None = None
    uom: str | None = None


class PlanOut(BaseModel):
    id: UUID
    plant_id: UUID
    material_id: UUID
    material_number: str
    material_description: str
    inspection_type: str
    op_no: str | None
    name: str
    sample_size: int
    blocking: bool
    active: bool
    characteristics: list[CharacteristicIO]


class PlanIn(BaseModel):
    plant_id: UUID
    material_id: UUID
    inspection_type: Literal["INCOMING", "IN_PROCESS", "FINAL"]
    op_no: str | None = None
    name: str = Field(min_length=1)
    sample_size: int = Field(1, ge=1, le=50)
    blocking: bool = True
    active: bool = True
    characteristics: list[CharacteristicIO] = Field(min_length=1)


class InspectionLotOut(BaseModel):
    id: UUID
    plant_id: UUID
    number: str
    inspection_type: str
    status: str
    plan_id: UUID
    plan_name: str
    blocking: bool
    material_id: UUID
    material_number: str
    material_description: str
    work_order_id: UUID | None
    erp_order_number: str | None
    operation_id: UUID | None
    op_no: str | None
    lot_id: UUID | None
    lot_number: str | None
    quantity: Decimal | None
    uom: str | None
    sample_size: int
    results_recorded: int
    results_expected: int
    created_at: datetime
    evaluated_at: datetime | None
    usage_decision: str | None
    decision_reason: str | None
    decided_at: datetime | None
    decided_by: str | None


class ResultOut(BaseModel):
    characteristic_id: UUID
    sample_no: int
    measured_value: Decimal | None
    attribute_ok: bool | None
    conforming: bool
    note: str | None
    recorded_at: datetime
    recorded_by_name: str | None


class NcOut(BaseModel):
    id: UUID
    plant_id: UUID
    number: str
    status: str
    source: str
    severity: str
    defect_code: str
    description: str
    material_id: UUID | None
    material_number: str | None
    work_order_id: UUID | None
    erp_order_number: str | None
    operation_id: UUID | None
    op_no: str | None
    lot_id: UUID | None
    lot_number: str | None
    lot_origin: str | None
    inspection_lot_id: UUID | None
    inspection_lot_number: str | None
    quantity: Decimal | None
    uom: str | None
    blocked_qty: Decimal
    reported_at: datetime
    reported_by_name: str | None
    disposition: str | None
    disposition_reason: str | None
    dispositioned_at: datetime | None
    dispositioned_by: str | None
    rework_good_qty: Decimal | None
    rework_scrap_qty: Decimal | None
    closed_at: datetime | None


class LotStockOut(BaseModel):
    location_code: str
    status: str
    quantity: Decimal


class InspectionLotDetail(InspectionLotOut):
    characteristics: list[CharacteristicIO]
    results: list[ResultOut]
    nonconformances: list[NcOut]
    stock: list[LotStockOut]


class NcDetail(NcOut):
    stock: list[LotStockOut]


class ResultIn(BaseModel):
    characteristic_id: UUID
    sample_no: int = Field(ge=1)
    measured_value: Decimal | None = None
    attribute_ok: bool | None = None
    note: str | None = None


class ResultsIn(BaseModel):
    results: list[ResultIn] = Field(min_length=1)


class DecisionIn(BaseModel):
    decision: Literal["ACCEPTED", "REJECTED"]
    reason: str = Field(min_length=1, description="E-signature reason")


class NcIn(BaseModel):
    plant_id: UUID | None = None
    work_order_id: UUID | None = None
    operation_id: UUID | None = None
    lot_id: UUID | None = None
    defect_code: str
    severity: Literal["MINOR", "MAJOR", "CRITICAL"] = "MINOR"
    description: str = Field(min_length=1)
    quantity: Decimal | None = Field(None, gt=0)
    client_event_id: UUID | None = None
    occurred_at: datetime | None = None


class DispositionIn(BaseModel):
    disposition: Literal["USE_AS_IS", "REWORK", "SCRAP", "RETURN_TO_SUPPLIER"]
    reason: str = Field(min_length=1, description="E-signature reason")


class ReworkIn(BaseModel):
    good_qty: Decimal = Field(ge=0)
    scrap_qty: Decimal = Field(ge=0)
    reason: str = Field(min_length=1, description="E-signature reason")


class QuarantineIn(BaseModel):
    reason: str = Field(min_length=1, description="E-signature reason")


# ------------------------------------------------------------------------------------------------ row builders


class _Names:
    """Caches names of orders, operations and lots while building rows."""

    def __init__(self, s: AsyncSession) -> None:
        self.s = s
        self.orders: dict[UUID, str] = {}
        self.ops: dict[UUID, str] = {}
        self.lots: dict[UUID, Any] = {}

    async def order(self, wo_id: UUID | None) -> str | None:
        if wo_id is None:
            return None
        if wo_id not in self.orders:
            self.orders[wo_id] = (await get_work_order_brief(self.s, wo_id)).erp_order_number
            for o in await operations_brief(self.s, wo_id):
                self.ops[o.id] = o.op_no
        return self.orders[wo_id]

    async def op(self, wo_id: UUID | None, op_id: UUID | None) -> str | None:
        if op_id is None:
            return None
        await self.order(wo_id)
        return self.ops.get(op_id)

    async def lot(self, lot_id: UUID | None) -> Any:
        if lot_id is None:
            return None
        if lot_id not in self.lots:
            self.lots[lot_id] = await get_lot_info(self.s, lot_id)
        return self.lots[lot_id]


async def _lot_rows(s: AsyncSession, lots: list[InspectionLot]) -> list[dict[str, Any]]:
    names = _Names(s)
    mats = await get_materials(s, {x.material_id for x in lots})
    plans = {p.id: p for p in (await s.scalars(select(InspectionPlan).where(InspectionPlan.id.in_({x.plan_id for x in lots})))).all()}
    n_chars = dict((await s.execute(
        select(InspectionCharacteristic.plan_id, func.count()).where(InspectionCharacteristic.plan_id.in_(plans))
        .group_by(InspectionCharacteristic.plan_id))).all())
    n_results = dict((await s.execute(
        select(InspectionResult.inspection_lot_id, func.count()).where(InspectionResult.inspection_lot_id.in_([x.id for x in lots]))
        .group_by(InspectionResult.inspection_lot_id))).all())
    out = []
    for il in lots:
        lot = await names.lot(il.lot_id)
        plan = plans[il.plan_id]
        out.append({
            **{c.key: getattr(il, c.key) for c in InspectionLot.__table__.columns},
            "plan_name": plan.name, "blocking": plan.blocking,
            "material_number": mats[il.material_id].material_number,
            "material_description": mats[il.material_id].description,
            "erp_order_number": await names.order(il.work_order_id),
            "op_no": await names.op(il.work_order_id, il.operation_id),
            "lot_number": lot.lot_number if lot else None, "uom": lot.uom if lot else mats[il.material_id].base_uom,
            "results_recorded": n_results.get(il.id, 0), "results_expected": n_chars.get(il.plan_id, 0) * il.sample_size,
        })
    return out


async def _nc_rows(s: AsyncSession, ncs: list[Nonconformance]) -> list[dict[str, Any]]:
    names = _Names(s)
    mats = await get_materials(s, {x.material_id for x in ncs if x.material_id})
    ils = {x.id: x.number for x in (await s.scalars(select(InspectionLot).where(
        InspectionLot.id.in_({x.inspection_lot_id for x in ncs if x.inspection_lot_id})))).all()}
    out = []
    for nc in ncs:
        lot = await names.lot(nc.lot_id)
        mat = mats.get(nc.material_id) if nc.material_id else None
        out.append({
            **{c.key: getattr(nc, c.key) for c in Nonconformance.__table__.columns},
            "material_number": mat.material_number if mat else None,
            "erp_order_number": await names.order(nc.work_order_id),
            "op_no": await names.op(nc.work_order_id, nc.operation_id),
            "lot_number": lot.lot_number if lot else None, "lot_origin": lot.origin if lot else None,
            "inspection_lot_number": ils.get(nc.inspection_lot_id) if nc.inspection_lot_id else None,
        })
    return out


async def _stock(s: AsyncSession, lot_id: UUID | None) -> list[dict[str, Any]]:
    if lot_id is None:
        return []
    return [{"location_code": loc.code, "status": st, "quantity": qty} for loc, st, qty in await stock_of_lot(s, lot_id)]


def _chars(rows: list[InspectionCharacteristic]) -> list[dict[str, Any]]:
    return [{"id": c.id, "name": c.name, "kind": c.kind, "target": c.target, "lower_limit": c.lower_limit,
             "upper_limit": c.upper_limit, "uom": c.uom} for c in rows]


async def _lot_detail(s: AsyncSession, il: InspectionLot) -> dict[str, Any]:
    row = (await _lot_rows(s, [il]))[0]
    results = (await s.scalars(select(InspectionResult).where(InspectionResult.inspection_lot_id == il.id)
                               .order_by(InspectionResult.sample_no))).all()
    ncs = (await s.scalars(select(Nonconformance).where(Nonconformance.inspection_lot_id == il.id)
                           .order_by(Nonconformance.reported_at))).all()
    return {
        **row, "characteristics": _chars(await q.characteristics(s, il.plan_id)),
        "results": [{c.key: getattr(r, c.key) for c in InspectionResult.__table__.columns} for r in results],
        "nonconformances": await _nc_rows(s, list(ncs)), "stock": await _stock(s, il.lot_id),
    }


async def _nc_detail(s: AsyncSession, nc: Nonconformance) -> dict[str, Any]:
    return {**(await _nc_rows(s, [nc]))[0], "stock": await _stock(s, nc.lot_id)}


# ------------------------------------------------------------------------------------------------ endpoints


@router.get("/quality/meta", response_model=QualityMeta)
async def quality_meta(user=require(Perm.QUALITY_READ)) -> dict:  # type: ignore[no-untyped-def]
    return {
        "defect_codes": [{"code": k, "label": v} for k, v in domain.DEFECT_CODES.items()],
        "severities": list(domain.SEVERITIES),
        "dispositions": [{"code": k, "label": v} for k, v in domain.DISPOSITIONS.items()],
    }


@router.get("/quality/plans", response_model=list[PlanOut])
async def list_plans(plant_id: UUID, db: DbDep, user=require(Perm.QUALITY_READ)) -> list:  # type: ignore[no-untyped-def]
    user.ensure_plant(plant_id)
    async with db.read() as s:
        plans = (await s.scalars(select(InspectionPlan).where(InspectionPlan.plant_id == plant_id)
                                 .order_by(InspectionPlan.inspection_type, InspectionPlan.op_no))).all()
        mats = await get_materials(s, {p.material_id for p in plans})
        out = []
        for p in plans:
            out.append({
                **{c.key: getattr(p, c.key) for c in InspectionPlan.__table__.columns},
                "material_number": mats[p.material_id].material_number,
                "material_description": mats[p.material_id].description,
                "characteristics": _chars(await q.characteristics(s, p.id)),
            })
    out.sort(key=lambda r: (r["material_number"], r["inspection_type"], r["op_no"] or ""))
    return out


@router.put("/quality/plans", response_model=PlanOut)
async def put_plan(body: PlanIn, request: Request, db: DbDep, user=require(Perm.QUALITY_PLAN)) -> dict:  # type: ignore[no-untyped-def]
    user.ensure_plant(body.plant_id)
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        plan = await q.upsert_plan(
            s, plant_id=body.plant_id, material_id=body.material_id, inspection_type=body.inspection_type,
            op_no=body.op_no or None, name=body.name, sample_size=body.sample_size, blocking=body.blocking,
            active=body.active,
            chars=[q.CharacteristicInput(c.name, c.kind, c.target, c.lower_limit, c.upper_limit, c.uom) for c in body.characteristics],
        )
        mat = (await get_materials(s, {plan.material_id}))[plan.material_id]
        return {
            **{c.key: getattr(plan, c.key) for c in InspectionPlan.__table__.columns},
            "material_number": mat.material_number, "material_description": mat.description,
            "characteristics": _chars(await q.characteristics(s, plan.id)),
        }


@router.get("/quality/inspection-lots", response_model=list[InspectionLotOut])
async def list_inspection_lots(  # type: ignore[no-untyped-def]
    plant_id: UUID,
    db: DbDep,
    user=require(Perm.QUALITY_READ),
    status: Annotated[list[str] | None, Query()] = None,
    inspection_type: Annotated[list[str] | None, Query()] = None,
    work_order_id: UUID | None = None,
    awaiting_decision: bool = False,
    limit: int = Query(200, le=1000),
) -> list:
    user.ensure_plant(plant_id)
    stmt = select(InspectionLot).where(InspectionLot.plant_id == plant_id).order_by(InspectionLot.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(InspectionLot.status.in_(status))
    if inspection_type:
        stmt = stmt.where(InspectionLot.inspection_type.in_(inspection_type))
    if work_order_id:
        stmt = stmt.where(InspectionLot.work_order_id == work_order_id)
    if awaiting_decision:
        stmt = stmt.where(InspectionLot.lot_id.is_not(None), InspectionLot.usage_decision.is_(None),
                          InspectionLot.status != "CANCELLED")
    async with db.read() as s:
        return await _lot_rows(s, list((await s.scalars(stmt)).all()))


@router.get("/quality/inspection-lots/{lot_id}", response_model=InspectionLotDetail)
async def get_inspection_lot(lot_id: UUID, db: DbDep, user=require(Perm.QUALITY_READ)) -> dict:  # type: ignore[no-untyped-def]
    async with db.read() as s:
        il = await q.get_inspection_lot(s, lot_id)
        user.ensure_plant(il.plant_id)
        return await _lot_detail(s, il)


@router.post("/quality/inspection-lots/{lot_id}/results", response_model=InspectionLotDetail)
async def post_results(  # type: ignore[no-untyped-def]
    lot_id: UUID, body: ResultsIn, request: Request, db: DbDep, user=require(Perm.QUALITY_INSPECT)
) -> dict:
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        il = await q.record_results(
            s, user, lot_id,
            [q.ResultInput(r.characteristic_id, r.sample_no, r.measured_value, r.attribute_ok, r.note) for r in body.results],
        )
        return await _lot_detail(s, il)


@router.post("/quality/inspection-lots/{lot_id}/usage-decision", response_model=InspectionLotDetail)
async def post_usage_decision(  # type: ignore[no-untyped-def]
    lot_id: UUID, body: DecisionIn, request: Request, db: DbDep, user=require(Perm.QUALITY_DECIDE)
) -> dict:
    async with db.unit_of_work(user.actor, request_id=request_id(request), reason=body.reason) as s:
        il = await q.usage_decision(s, user, lot_id, decision=body.decision, reason=body.reason)
        return await _lot_detail(s, il)


@router.get("/quality/nonconformances", response_model=list[NcOut])
async def list_ncs(  # type: ignore[no-untyped-def]
    plant_id: UUID,
    db: DbDep,
    user=require(Perm.QUALITY_READ),
    status: Annotated[list[str] | None, Query()] = None,
    work_order_id: UUID | None = None,
    lot_id: UUID | None = None,
    limit: int = Query(200, le=1000),
) -> list:
    user.ensure_plant(plant_id)
    stmt = select(Nonconformance).where(Nonconformance.plant_id == plant_id).order_by(Nonconformance.reported_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Nonconformance.status.in_(status))
    if work_order_id:
        stmt = stmt.where(Nonconformance.work_order_id == work_order_id)
    if lot_id:
        stmt = stmt.where(Nonconformance.lot_id == lot_id)
    async with db.read() as s:
        return await _nc_rows(s, list((await s.scalars(stmt)).all()))


@router.get("/quality/nonconformances/{nc_id}", response_model=NcDetail)
async def get_nc(nc_id: UUID, db: DbDep, user=require(Perm.QUALITY_READ)) -> dict:  # type: ignore[no-untyped-def]
    async with db.read() as s:
        nc = await q.get_nc(s, nc_id)
        user.ensure_plant(nc.plant_id)
        return await _nc_detail(s, nc)


@router.post("/quality/nonconformances", response_model=NcDetail, status_code=201)
async def post_nc(body: NcIn, request: Request, db: DbDep, user=require(Perm.NC_CREATE)) -> dict:  # type: ignore[no-untyped-def]
    """Report a defect. From the terminal (operator) it is tied to the running order/operation; replays with the same
    `client_event_id` return the original record."""
    if not (body.work_order_id or body.lot_id or body.plant_id):
        raise ValidationFailed("give a work order, a lot or a plant")
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        nc = await q.report_defect(
            s, user, work_order_id=body.work_order_id, operation_id=body.operation_id, lot_id=body.lot_id,
            defect_code=body.defect_code, severity=body.severity, description=body.description, quantity=body.quantity,
            source="OPERATOR" if user.terminal_id else "MANUAL", client_event_id=body.client_event_id,
            reported_at=body.occurred_at, plant_id=body.plant_id,
        )
        return await _nc_detail(s, nc)


@router.post("/quality/nonconformances/{nc_id}/disposition", response_model=NcDetail)
async def post_disposition(  # type: ignore[no-untyped-def]
    nc_id: UUID, body: DispositionIn, request: Request, db: DbDep, user=require(Perm.QUALITY_DECIDE)
) -> dict:
    async with db.unit_of_work(user.actor, request_id=request_id(request), reason=body.reason) as s:
        nc = await q.disposition(s, user, nc_id, disposition=body.disposition, reason=body.reason)
        return await _nc_detail(s, nc)


@router.post("/quality/nonconformances/{nc_id}/rework-complete", response_model=NcDetail)
async def post_rework_complete(  # type: ignore[no-untyped-def]
    nc_id: UUID, body: ReworkIn, request: Request, db: DbDep, user=require(Perm.QUALITY_DECIDE)
) -> dict:
    async with db.unit_of_work(user.actor, request_id=request_id(request), reason=body.reason) as s:
        nc = await q.complete_rework(s, user, nc_id, good_qty=body.good_qty, scrap_qty=body.scrap_qty, reason=body.reason)
        return await _nc_detail(s, nc)


@router.post("/quality/lots/{lot_id}/quarantine", response_model=NcDetail)
async def post_quarantine(  # type: ignore[no-untyped-def]
    lot_id: UUID, body: QuarantineIn, request: Request, db: DbDep, user=require(Perm.QUALITY_DECIDE)
) -> dict:
    """Block all free and in-inspection stock of a lot under a new nonconformance."""
    async with db.unit_of_work(user.actor, request_id=request_id(request), reason=body.reason) as s:
        nc = await q.quarantine_lot(s, user, lot_id=lot_id, reason=body.reason)
        return await _nc_detail(s, nc)


@router.post("/quality/work-orders/{wo_id}/operations/{op_id}/inspection", response_model=InspectionLotDetail | None)
async def open_operation_inspection(  # type: ignore[no-untyped-def]
    wo_id: UUID, op_id: UUID, request: Request, db: DbDep, user=require(Perm.QUALITY_INSPECT)
) -> dict | None:
    """The operation's current first-off inspection lot, opened if one is due (null when the material has no
    in-process plan for this operation)."""
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        user.ensure_plant((await get_work_order_brief(s, wo_id)).plant_id)
        il = await q.ensure_in_process_inspection(s, work_order_id=wo_id, operation_id=op_id)
        return await _lot_detail(s, il) if il else None
