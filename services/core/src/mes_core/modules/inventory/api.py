from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import aliased

from mes_core.modules.inventory import service as inv
from mes_core.modules.inventory.models import Lot, Movement, Stock, StorageLocation
from mes_core.modules.masterdata.public import get_materials, search_material_ids
from mes_core.modules.org.public import list_lines, work_centers
from mes_core.modules.planning.public import (
    components_of_work_order,
    get_work_order_brief,
    open_component_demand,
    operations_brief,
)
from mes_core.modules.quality.public import receive_with_inspection
from mes_core.shared.errors import NotFound
from mes_core.shared.permissions import Perm
from mes_core.web.deps import DbDep, request_id, require

router = APIRouter(tags=["inventory"])


class LocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    plant_id: UUID
    line_id: UUID | None
    code: str
    name: str
    kind: str


class StockOut(BaseModel):
    location_id: UUID
    location_code: str
    location_kind: str
    material_id: UUID
    material_number: str
    material_description: str
    lot_id: UUID
    lot_number: str
    lot_origin: str
    status: str
    quantity: Decimal
    uom: str
    received_at: datetime


class MovementOut(BaseModel):
    id: UUID
    movement_type: str
    material_id: UUID
    material_number: str | None
    lot_id: UUID
    lot_number: str | None
    quantity: Decimal
    uom: str
    from_location: str | None
    to_location: str | None
    from_status: str | None = None
    to_status: str | None = None
    work_order_id: UUID | None
    erp_order_number: str | None = None
    operation_id: UUID | None
    component_id: UUID | None
    confirmation_id: UUID | None
    backflush: bool
    reason: str | None
    posted_at: datetime
    posted_by_name: str | None
    erp_sync_status: str
    erp_material_document: str | None
    erp_document_year: str | None
    erp_error: str | None
    erp_reconciled: bool


class ReceiptOut(MovementOut):
    inspection_lot_id: UUID | None = None
    inspection_lot_number: str | None = None


class ReceiptIn(BaseModel):
    plant_id: UUID
    material_id: UUID
    quantity: Decimal = Field(gt=0)
    location_id: UUID
    lot_number: str | None = None
    reason: str | None = None


class TransferIn(BaseModel):
    plant_id: UUID
    lot_id: UUID
    quantity: Decimal = Field(gt=0)
    from_location_id: UUID
    to_location_id: UUID
    reason: str | None = None


class IssueIn(BaseModel):
    lot_id: UUID
    from_location_id: UUID
    quantity: Decimal = Field(gt=0)
    client_event_id: UUID | None = None
    occurred_at: datetime | None = None


@router.get("/inventory/locations", response_model=list[LocationOut])
async def get_locations(plant_id: UUID, db: DbDep, user=require(Perm.INVENTORY_READ)) -> list:  # type: ignore[no-untyped-def]
    user.ensure_plant(plant_id)
    async with db.read() as s:
        return await inv.locations(s, plant_id)


@router.get("/inventory/stock", response_model=list[StockOut])
async def get_stock(  # type: ignore[no-untyped-def]
    plant_id: UUID,
    db: DbDep,
    user=require(Perm.INVENTORY_READ),
    location_id: UUID | None = None,
    material_id: UUID | None = None,
    search: str | None = None,
    include_empty: bool = False,
) -> list:
    user.ensure_plant(plant_id)
    q = (
        select(Stock, StorageLocation, Lot)
        .join(StorageLocation, StorageLocation.id == Stock.location_id)
        .join(Lot, Lot.id == Stock.lot_id)
        .where(Stock.plant_id == plant_id)
        .order_by(StorageLocation.code, Lot.created_at)
    )
    if not include_empty:
        q = q.where(Stock.quantity > 0)
    if location_id:
        q = q.where(Stock.location_id == location_id)
    if material_id:
        q = q.where(Stock.material_id == material_id)
    async with db.read() as s:
        if search:
            q = q.where(Stock.material_id.in_(await search_material_ids(s, search)))
        rows = (await s.execute(q)).all()
        mats = await get_materials(s, {st.material_id for st, _, _ in rows})
    return [
        {
            "location_id": loc.id,
            "location_code": loc.code,
            "location_kind": loc.kind,
            "material_id": st.material_id,
            "material_number": mats[st.material_id].material_number,
            "material_description": mats[st.material_id].description,
            "lot_id": lot.id,
            "lot_number": lot.lot_number,
            "lot_origin": lot.origin,
            "status": st.status,
            "quantity": st.quantity,
            "uom": st.uom,
            "received_at": lot.created_at,
        }
        for st, loc, lot in rows
    ]


async def _movement_rows(s, q) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    src, dst = aliased(StorageLocation), aliased(StorageLocation)
    rows = (
        await s.execute(
            q.add_columns(Lot.lot_number, src.code, dst.code)
            .join(Lot, Lot.id == Movement.lot_id)
            .outerjoin(src, src.id == Movement.from_location_id)
            .outerjoin(dst, dst.id == Movement.to_location_id)
        )
    ).all()
    mats = await get_materials(s, {r[0].material_id for r in rows})
    orders = {wo_id: (await get_work_order_brief(s, wo_id)).erp_order_number for wo_id in {r[0].work_order_id for r in rows} if wo_id}
    out = []
    for mv, lot_number, src_code, dst_code in rows:
        d = {c.key: getattr(mv, c.key) for c in Movement.__table__.columns}
        mat = mats.get(mv.material_id)
        out.append({**d, "material_number": mat.material_number if mat else None, "lot_number": lot_number,
                    "erp_order_number": orders.get(mv.work_order_id),
                    "from_location": src_code, "to_location": dst_code})
    return out


@router.get("/inventory/movements", response_model=list[MovementOut])
async def get_movements(  # type: ignore[no-untyped-def]
    plant_id: UUID,
    db: DbDep,
    user=require(Perm.INVENTORY_READ),
    work_order_id: UUID | None = None,
    erp_sync_status: Annotated[list[str] | None, Query()] = None,
    limit: int = Query(200, le=1000),
) -> list:
    user.ensure_plant(plant_id)
    q = select(Movement).where(Movement.plant_id == plant_id).order_by(Movement.posted_at.desc(), Movement.id.desc()).limit(limit)
    if work_order_id:
        q = q.where(Movement.work_order_id == work_order_id)
    if erp_sync_status:
        q = q.where(Movement.erp_sync_status.in_(erp_sync_status))
    async with db.read() as s:
        return await _movement_rows(s, q)


@router.post("/inventory/receipts", response_model=ReceiptOut, status_code=201)
async def post_receipt(body: ReceiptIn, request: Request, db: DbDep, user=require(Perm.INVENTORY_MANAGE)) -> dict:  # type: ignore[no-untyped-def]
    """Goods receipt without order (501). Materials with an incoming inspection plan go to quality inspection stock
    and get an inspection lot; they can't be issued until the usage decision releases them."""
    user.ensure_plant(body.plant_id)
    async with db.unit_of_work(user.actor, request_id=request_id(request), reason=body.reason) as s:
        mv, il = await receive_with_inspection(
            s, plant_id=body.plant_id, material_id=body.material_id, quantity=body.quantity,
            location_id=body.location_id, lot_number=body.lot_number, reason=body.reason,
        )
        row = (await _movement_rows(s, select(Movement).where(Movement.id == mv.id)))[0]
        return {**row, "inspection_lot_id": il.id if il else None, "inspection_lot_number": il.number if il else None}


@router.post("/inventory/transfers", response_model=MovementOut, status_code=201)
async def post_transfer(body: TransferIn, request: Request, db: DbDep, user=require(Perm.INVENTORY_MANAGE)) -> dict:  # type: ignore[no-untyped-def]
    user.ensure_plant(body.plant_id)
    async with db.unit_of_work(user.actor, request_id=request_id(request), reason=body.reason) as s:
        mv = await inv.transfer(
            s, plant_id=body.plant_id, lot_id=body.lot_id, quantity=body.quantity,
            from_location_id=body.from_location_id, to_location_id=body.to_location_id, reason=body.reason,
        )
        return (await _movement_rows(s, select(Movement).where(Movement.id == mv.id)))[0]


@router.post("/work-orders/{wo_id}/components/{component_id}/issues", response_model=MovementOut, status_code=201)
async def post_issue(  # type: ignore[no-untyped-def]
    wo_id: UUID, component_id: UUID, body: IssueIn, request: Request, db: DbDep, user=require(Perm.MATERIAL_ISSUE)
) -> dict:
    async with db.unit_of_work(user.actor, request_id=request_id(request)) as s:
        wo = await get_work_order_brief(s, wo_id)
        user.ensure_plant(wo.plant_id)
        if component_id not in {c.component_id for c in await components_of_work_order(s, wo_id)}:
            raise NotFound("component not found in this work order")
        mv = await inv.issue_to_order(
            s, component_id=component_id, lot_id=body.lot_id, from_location_id=body.from_location_id,
            quantity=body.quantity, client_event_id=body.client_event_id, occurred_at=body.occurred_at,
        )
        return (await _movement_rows(s, select(Movement).where(Movement.id == mv.id)))[0]


# ------------------------------------------------------------------------------------------------ order materials


class LotAvailability(BaseModel):
    lot_id: UUID
    lot_number: str
    location_id: UUID
    location_code: str
    location_kind: str
    quantity: Decimal


class ComponentStatus(BaseModel):
    component_id: UUID
    material_id: UUID
    material_number: str
    material_description: str
    operation_id: UUID | None
    op_no: str | None
    required_qty: Decimal
    issued_qty: Decimal
    remaining_qty: Decimal
    uom: str
    backflush: bool
    available: list[LotAvailability]


class OpWip(BaseModel):
    operation_id: UUID
    op_no: str
    description: str
    status: str
    yield_qty: Decimal
    scrap_qty: Decimal
    waiting_qty: Decimal  # good parts from the previous operation not yet processed here


class OrderMaterials(BaseModel):
    work_order_id: UUID
    components: list[ComponentStatus]
    wip: list[OpWip]
    movements: list[MovementOut]


@router.get("/work-orders/{wo_id}/materials", response_model=OrderMaterials)
async def order_materials(wo_id: UUID, db: DbDep, user=require(Perm.INVENTORY_READ)) -> dict:  # type: ignore[no-untyped-def]
    async with db.read() as s:
        wo = await get_work_order_brief(s, wo_id)
        user.ensure_plant(wo.plant_id)
        comps = await components_of_work_order(s, wo_id)
        issued = await inv.issued_by_component(s, [c.component_id for c in comps])
        mats = await get_materials(s, {c.material_id for c in comps})
        wcs = await work_centers(s, wo.plant_id)
        components = []
        for c in comps:
            line_id = wcs[c.op_work_center_id].line_id if c.op_work_center_id in wcs else wo.line_id
            sources = await inv.source_locations(s, wo.plant_id, line_id)
            avail = []
            for loc in sources:
                rows = (
                    await s.execute(
                        select(Stock, Lot)
                        .join(Lot, Lot.id == Stock.lot_id)
                        .where(
                            Stock.location_id == loc.id, Stock.material_id == c.material_id, Stock.quantity > 0,
                            Stock.status == "UNRESTRICTED",
                        )
                        .order_by(Lot.created_at)
                    )
                ).all()
                avail += [
                    {"lot_id": lot.id, "lot_number": lot.lot_number, "location_id": loc.id, "location_code": loc.code,
                     "location_kind": loc.kind, "quantity": st.quantity}
                    for st, lot in rows
                ]
            done = issued.get(c.component_id, Decimal(0))
            components.append(
                {
                    "component_id": c.component_id,
                    "material_id": c.material_id,
                    "material_number": mats[c.material_id].material_number,
                    "material_description": mats[c.material_id].description,
                    "operation_id": c.operation_id,
                    "op_no": c.op_no,
                    "required_qty": c.required_qty,
                    "issued_qty": done,
                    "remaining_qty": max(c.required_qty - done, Decimal(0)),
                    "uom": c.uom,
                    "backflush": c.backflush,
                    "available": avail,
                }
            )
        ops = await operations_brief(s, wo_id)
        wip, prev_yield = [], None
        for op in ops:
            waiting = max((prev_yield - op.yield_qty - op.scrap_qty), Decimal(0)) if prev_yield is not None else Decimal(0)
            wip.append({"operation_id": op.id, "op_no": op.op_no, "description": op.description, "status": op.status,
                        "yield_qty": op.yield_qty, "scrap_qty": op.scrap_qty, "waiting_qty": waiting})
            prev_yield = op.yield_qty
        movements = await _movement_rows(
            s, select(Movement).where(Movement.work_order_id == wo_id).order_by(Movement.posted_at.desc())
        )
    return {"work_order_id": wo_id, "components": components, "wip": wip, "movements": movements}


# ------------------------------------------------------------------------------------------------ shortages


class LineGap(BaseModel):
    line_id: UUID | None
    line_code: str
    demand: Decimal
    lineside_available: Decimal
    gap: Decimal


class ShortageRow(BaseModel):
    material_id: UUID
    material_number: str
    material_description: str
    uom: str
    demand: Decimal
    available: Decimal
    shortage: Decimal
    lines: list[LineGap]
    orders: list[str]


@router.get("/inventory/shortages", response_model=list[ShortageRow])
async def shortages(  # type: ignore[no-untyped-def]
    plant_id: UUID,
    db: DbDep,
    user=require(Perm.INVENTORY_READ),
    include_released: bool = False,
) -> list:
    """Open component demand of scheduled/dispatched/running orders vs. stock (line-side and stores)."""
    user.ensure_plant(plant_id)
    statuses = ["SCHEDULED", "DISPATCHED", "IN_PROGRESS"] + (["RELEASED"] if include_released else [])
    async with db.read() as s:
        comps = await open_component_demand(s, plant_id, statuses)
        issued = await inv.issued_by_component(s, [c.component_id for c in comps])
        wcs = await work_centers(s, plant_id)
        lines = {ln.id: ln.code for ln in await list_lines(s, plant_id)}
        demand: dict[UUID, dict[UUID | None, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        orders: dict[UUID, set[str]] = defaultdict(set)
        uoms: dict[UUID, str] = {}
        for c in comps:
            rem = c.required_qty - issued.get(c.component_id, Decimal(0))
            if rem <= 0:
                continue
            line_id = wcs[c.op_work_center_id].line_id if c.op_work_center_id in wcs else None
            demand[c.material_id][line_id] += rem
            orders[c.material_id].add(c.erp_order_number)
            uoms[c.material_id] = c.uom
        avail = await inv.available_by_material(s, plant_id, set(demand))
        mats = await get_materials(s, set(demand))
    out = []
    for mat_id, by_line in demand.items():
        a = avail.get(mat_id, {})
        total_demand = sum(by_line.values(), Decimal(0))
        total_avail = sum((v for k, v in a.items() if k != "FG"), Decimal(0))
        gaps = []
        for line_id, d in by_line.items():
            ls = a.get(f"LINESIDE:{line_id}", Decimal(0))
            gaps.append({"line_id": line_id, "line_code": lines.get(line_id, "—") if line_id else "—",
                         "demand": d, "lineside_available": ls, "gap": max(d - ls, Decimal(0))})
        out.append(
            {
                "material_id": mat_id,
                "material_number": mats[mat_id].material_number,
                "material_description": mats[mat_id].description,
                "uom": uoms[mat_id],
                "demand": total_demand,
                "available": total_avail,
                "shortage": max(total_demand - total_avail, Decimal(0)),
                "lines": sorted(gaps, key=lambda g: g["line_code"]),
                "orders": sorted(orders[mat_id]),
            }
        )
    out.sort(key=lambda r: (-r["shortage"], -sum(g["gap"] for g in r["lines"]), r["material_number"]))
    return out
