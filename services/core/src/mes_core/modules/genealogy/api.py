from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from mes_core.modules.genealogy import service as g
from mes_core.modules.inventory.public import find_lots, find_serials, get_lot_info
from mes_core.modules.masterdata.public import get_materials
from mes_core.modules.planning.public import find_work_orders, get_work_order_brief
from mes_core.shared.errors import NotFound
from mes_core.shared.permissions import Perm
from mes_core.web.deps import DbDep, request_id, require

router = APIRouter(tags=["genealogy"])

Direction = Literal["backward", "forward"]


class TraceHit(BaseModel):
    type: Literal["LOT", "SERIAL", "ORDER"]
    id: UUID
    label: str
    lot_id: UUID | None = None
    work_order_id: UUID | None = None
    material_number: str
    material_description: str
    created_at: datetime | None = None


class TraceStock(BaseModel):
    location_code: str
    status: str
    quantity: Decimal


class TraceLot(BaseModel):
    lot_number: str
    origin: str
    created_at: datetime
    received_qty: Decimal | None
    work_order_id: UUID | None
    stock: list[TraceStock]
    serial_count: int
    serials: list[str]


class TraceStep(BaseModel):
    equipment_code: str | None
    operator_name: str | None
    started_at: datetime
    ended_at: datetime | None
    yield_qty: Decimal
    scrap_qty: Decimal
    source: str


class TraceOperation(BaseModel):
    op_no: str
    description: str
    status: str
    work_center: str | None
    yield_qty: Decimal
    scrap_qty: Decimal
    steps: list[TraceStep]


class TraceOrder(BaseModel):
    erp_order_number: str
    status: str
    planned_qty: Decimal
    yield_qty: Decimal
    scrap_qty: Decimal
    operations: list[TraceOperation]


class TraceInspection(BaseModel):
    id: UUID
    number: str
    inspection_type: str
    status: str
    usage_decision: str | None


class TraceNc(BaseModel):
    id: UUID
    number: str
    status: str
    severity: str
    defect_code: str
    description: str
    disposition: str | None


class TraceNode(BaseModel):
    type: Literal["LOT", "ORDER"]
    id: UUID
    label: str
    material_id: UUID
    material_number: str
    material_description: str
    quantity: Decimal | None = Field(None, description="Quantity on the edge to the parent (consumed / produced)")
    uom: str
    lot: TraceLot | None
    order: TraceOrder | None
    inspections: list[TraceInspection]
    nonconformances: list[TraceNc]
    children: list[TraceNode]
    truncated: bool = Field(description="Branch not expanded (already shown elsewhere in the tree, or size limit)")


class TraceResult(BaseModel):
    direction: Direction
    root: TraceNode
    serial: str | None = None


class QuarantineAffectedIn(BaseModel):
    reason: str = Field(min_length=1, description="E-signature reason")
    include_root: bool = True


class QuarantinedLot(BaseModel):
    lot_id: UUID
    lot_number: str
    material_number: str
    blocked_qty: Decimal
    uom: str
    nc_id: UUID
    nc_number: str


@router.get("/trace/search", response_model=list[TraceHit])
async def trace_search(  # type: ignore[no-untyped-def]
    plant_id: UUID, db: DbDep, q: str = Query(min_length=2), user=require(Perm.TRACE_READ)
) -> list:
    """Find lots, serial numbers and orders by (part of) their number."""
    user.ensure_plant(plant_id)
    text = q.strip()
    async with db.read() as s:
        lots = await find_lots(s, plant_id, text, 15)
        serials = await find_serials(s, plant_id, text, 15)
        orders = await find_work_orders(s, plant_id, text, 15)
        mats = await get_materials(s, {x.material_id for x in lots} | {x.material_id for x in serials} | {x.material_id for x in orders})
    hits = [
        {"type": "SERIAL", "id": x.id, "label": x.serial, "lot_id": x.lot_id, "work_order_id": x.work_order_id,
         "material_number": mats[x.material_id].material_number, "material_description": mats[x.material_id].description,
         "created_at": x.created_at}
        for x in serials
    ]
    hits += [
        {"type": "LOT", "id": x.id, "label": x.lot_number, "lot_id": x.id, "work_order_id": x.work_order_id,
         "material_number": mats[x.material_id].material_number, "material_description": mats[x.material_id].description,
         "created_at": x.created_at}
        for x in lots
    ]
    hits += [
        {"type": "ORDER", "id": x.id, "label": x.erp_order_number, "work_order_id": x.id,
         "material_number": mats[x.material_id].material_number, "material_description": mats[x.material_id].description}
        for x in orders
    ]
    return hits


@router.get("/trace/lots/{lot_id}", response_model=TraceResult)
async def trace_lot(lot_id: UUID, db: DbDep, direction: Direction = "backward", user=require(Perm.TRACE_READ)) -> dict:  # type: ignore[no-untyped-def]
    async with db.read() as s:
        user.ensure_plant((await get_lot_info(s, lot_id)).plant_id)
        return {"direction": direction, "root": await g.trace_lot(s, lot_id, direction)}


@router.get("/trace/orders/{wo_id}", response_model=TraceResult)
async def trace_order(wo_id: UUID, db: DbDep, direction: Direction = "backward", user=require(Perm.TRACE_READ)) -> dict:  # type: ignore[no-untyped-def]
    async with db.read() as s:
        user.ensure_plant((await get_work_order_brief(s, wo_id)).plant_id)
        return {"direction": direction, "root": await g.trace_order(s, wo_id, direction)}


@router.get("/trace/serials/{serial}", response_model=TraceResult)
async def trace_serial(  # type: ignore[no-untyped-def]
    serial: str, plant_id: UUID, db: DbDep, direction: Direction = "backward", user=require(Perm.TRACE_READ)
) -> dict:
    """Trace the production lot a serial number was built in (serial -> order -> component lots)."""
    user.ensure_plant(plant_id)
    async with db.read() as s:
        hit = next((x for x in await find_serials(s, plant_id, serial, 5) if x.serial == serial), None)
        if hit is None:
            raise NotFound(f"serial number {serial} not found")
        return {"direction": direction, "serial": hit.serial, "root": await g.trace_lot(s, hit.lot_id, direction)}


@router.post("/trace/lots/{lot_id}/quarantine-affected", response_model=list[QuarantinedLot])
async def quarantine_affected(  # type: ignore[no-untyped-def]
    lot_id: UUID, body: QuarantineAffectedIn, request: Request, db: DbDep, user=require(Perm.QUALITY_DECIDE)
) -> list:
    """Suspect lot: block it and every downstream lot that still has free or in-inspection stock (e-signed)."""
    async with db.unit_of_work(user.actor, request_id=request_id(request), reason=body.reason) as s:
        user.ensure_plant((await get_lot_info(s, lot_id)).plant_id)
        return await g.quarantine_affected(s, user, lot_id, reason=body.reason, include_root=body.include_root)
