"""Goods movements. Every movement updates lot-level stock in the same transaction and, if SAP must know about
it, writes a `mes.goods_movement.created` event to the outbox (the ERP adapter posts it idempotently)."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mes_common.events import PermanentError, Topics
from mes_core.modules.inventory.models import (
    LOT_NUMBER_SEQ,
    SERIAL_SEQ,
    Lot,
    Movement,
    SerialNumber,
    Stock,
    StorageLocation,
)
from mes_core.modules.masterdata.public import get_materials
from mes_core.modules.org.public import get_plant
from mes_core.modules.planning.public import ComponentContext, get_component_context
from mes_core.shared.db.context import current_actor
from mes_core.shared.errors import DomainError, NotFound, ValidationFailed
from mes_core.shared.messaging import messaging

EVENT_GOODS_MOVEMENT_CREATED = "mes.goods_movement.created"
# 561 = opening balance taken over from SAP (SAP already has that stock): never posted back.
ERP_POSTED_TYPES = {"501", "311", "261", "262", "101", "102", "321", "350", "344", "343", "555", "122"}
UNRESTRICTED, QUALITY, BLOCKED = "UNRESTRICTED", "QUALITY", "BLOCKED"
# stock type changes: movement type -> (from status, to status)
STATUS_MOVES = {"321": (QUALITY, UNRESTRICTED), "350": (QUALITY, BLOCKED), "344": (UNRESTRICTED, BLOCKED),
                "343": (BLOCKED, UNRESTRICTED)}
Q = Decimal("0.001")


# ------------------------------------------------------------------------------------------------ helpers


async def get_location(s: AsyncSession, location_id: UUID, plant_id: UUID | None = None) -> StorageLocation:
    loc = await s.get(StorageLocation, location_id)
    if loc is None or (plant_id and loc.plant_id != plant_id):
        raise NotFound("storage location not found")
    return loc


async def locations(s: AsyncSession, plant_id: UUID) -> list[StorageLocation]:
    return list((await s.scalars(select(StorageLocation).where(StorageLocation.plant_id == plant_id).order_by(StorageLocation.code))).all())


async def new_lot(
    s: AsyncSession, *, plant_id: UUID, material_id: UUID, uom: str, origin: str, work_order_id: UUID | None = None,
    lot_number: str | None = None,
) -> Lot:
    if lot_number is None:
        seq = (await s.execute(select(LOT_NUMBER_SEQ.next_value()))).scalar_one()
        lot_number = f"L{seq:09d}"
    elif len(lot_number) > 10:
        raise ValidationFailed("lot number: at most 10 characters (SAP batch)")
    lot = Lot(plant_id=plant_id, material_id=material_id, lot_number=lot_number, origin=origin, uom=uom, work_order_id=work_order_id)
    s.add(lot)
    await s.flush()
    return lot


async def _change_stock(
    s: AsyncSession, *, lot: Lot, location: StorageLocation, delta: Decimal, status: str = UNRESTRICTED
) -> None:
    row = (
        await s.scalars(
            select(Stock)
            .where(Stock.location_id == location.id, Stock.lot_id == lot.id, Stock.status == status)
            .with_for_update()
        )
    ).first()
    have = row.quantity if row else Decimal(0)
    if have + delta < 0:
        mats = await get_materials(s, {lot.material_id})
        mat = mats.get(lot.material_id)
        raise DomainError(
            f"not enough {status.lower()} {mat.material_number if mat else 'material'} lot {lot.lot_number} in {location.code}: "
            f"need {abs(delta):f}, have {have:f} {lot.uom}",
            code="insufficient_stock",
            details={"location": location.code, "lot": lot.lot_number, "available": str(have), "requested": str(abs(delta))},
        )
    if row is None:
        s.add(Stock(plant_id=lot.plant_id, location_id=location.id, material_id=lot.material_id, lot_id=lot.id,
                    status=status, quantity=delta, uom=lot.uom))
    else:
        row.quantity = have + delta
    await s.flush()


@dataclass(frozen=True)
class OrderRef:
    work_order_id: UUID | None  # None for a maintenance order (see maintenance_order_id)
    erp_order_number: str
    operation_id: UUID | None = None
    component: ComponentContext | None = None
    confirmation_id: UUID | None = None
    maintenance_order_id: UUID | None = None


async def post_movement(
    s: AsyncSession,
    *,
    movement_type: str,
    lot: Lot,
    quantity: Decimal,
    from_location: StorageLocation | None = None,
    to_location: StorageLocation | None = None,
    order: OrderRef | None = None,
    backflush: bool = False,
    reason: str | None = None,
    client_event_id: UUID | None = None,
    occurred_at: datetime | None = None,
    from_status: str = UNRESTRICTED,
    to_status: str = UNRESTRICTED,
    serials: list[str] | None = None,
) -> Movement:
    """The single write path for stock. Idempotent on `client_event_id` (offline terminal replays).

    A stock type change (e.g. 321 quality -> unrestricted) has the same location on both sides."""
    if client_event_id:
        existing = (await s.scalars(select(Movement).where(Movement.client_event_id == client_event_id))).first()
        if existing:
            return existing
    quantity = Decimal(quantity).quantize(Q)
    if quantity <= 0:
        raise ValidationFailed("quantity must be positive")
    if from_location:
        await _change_stock(s, lot=lot, location=from_location, delta=-quantity, status=from_status)
    if to_location:
        await _change_stock(s, lot=lot, location=to_location, delta=quantity, status=to_status)

    actor = current_actor()
    assert actor is not None, "movement outside unit of work"
    erp = movement_type in ERP_POSTED_TYPES
    mv = Movement(
        plant_id=lot.plant_id,
        movement_type=movement_type,
        material_id=lot.material_id,
        lot_id=lot.id,
        quantity=quantity,
        uom=lot.uom,
        from_location_id=from_location.id if from_location else None,
        to_location_id=to_location.id if to_location else None,
        from_status=from_status if from_location else None,
        to_status=to_status if to_location else None,
        work_order_id=order.work_order_id if order else None,
        maintenance_order_id=order.maintenance_order_id if order else None,
        operation_id=order.operation_id if order else None,
        component_id=order.component.component_id if order and order.component else None,
        confirmation_id=order.confirmation_id if order else None,
        backflush=backflush,
        reason=reason,
        posted_at=occurred_at or datetime.now(UTC),
        posted_by=actor.id,
        posted_by_name=actor.name,
        client_event_id=client_event_id,
        erp_sync_status="PENDING" if erp else "NOT_REQUIRED",
    )
    s.add(mv)
    await s.flush()
    if erp:
        await _publish(s, mv, lot, from_location, to_location, order, serials)
    return mv


async def _publish(
    s: AsyncSession, mv: Movement, lot: Lot, src: StorageLocation | None, dst: StorageLocation | None,
    order: OrderRef | None, serials: list[str] | None = None,
) -> None:
    plant = await get_plant(s, mv.plant_id)
    mat = (await get_materials(s, {mv.material_id}))[mv.material_id]
    # SAP semantics: the item's StorageLocation is where stock goes out (261/311) or comes in (101/501)
    primary = src if mv.movement_type in ("261", "311", "102", "555", "122") else (dst or src)
    comp = order.component if order else None
    key = f"{plant.erp_plant_code}:{order.erp_order_number}" if order else f"{plant.erp_plant_code}:MAT:{mat.material_number}"
    await messaging.publish(
        s,
        topic=Topics.MES_GOODS_MOVEMENTS,
        key=key,
        event_type=EVENT_GOODS_MOVEMENT_CREATED,
        payload={
            "movement_id": str(mv.id),
            "erp_plant": plant.erp_plant_code,
            "movement_type": mv.movement_type,
            "material": mat.material_number,
            "quantity": str(mv.quantity),
            "uom": mv.uom,
            "storage_location": primary.code if primary else None,
            "destination_storage_location": dst.code if mv.movement_type == "311" and dst else None,
            "batch": lot.lot_number,
            "erp_order_number": order.erp_order_number if order else None,
            "reservation_no": comp.reservation_no if comp else None,
            "reservation_item": comp.reservation_item if comp else None,
            "posting_date": mv.posted_at.date().isoformat(),
            # receipts into quality inspection stock; SAP InventoryStockType 02
            "stock_type": mv.to_status if mv.movement_type in ("101", "501") else None,
            "serial_numbers": serials or [],
            "reason": mv.reason,
        },
    )


# ------------------------------------------------------------------------------------------------ use cases


async def receive(
    s: AsyncSession, *, plant_id: UUID, material_id: UUID, quantity: Decimal, location_id: UUID,
    lot_number: str | None = None, opening_balance: bool = False, reason: str | None = None,
    to_status: str = UNRESTRICTED,
) -> Movement:
    loc = await get_location(s, location_id, plant_id)
    mat = (await get_materials(s, {material_id})).get(material_id)
    if mat is None:
        raise NotFound("material not found")
    lot = await new_lot(
        s, plant_id=plant_id, material_id=material_id, uom=mat.base_uom,
        origin="OPENING" if opening_balance else "RECEIPT", lot_number=lot_number,
    )
    return await post_movement(
        s, movement_type="561" if opening_balance else "501", lot=lot, quantity=quantity, to_location=loc, reason=reason,
        to_status=to_status,
    )


async def transfer(
    s: AsyncSession, *, plant_id: UUID, lot_id: UUID, quantity: Decimal, from_location_id: UUID, to_location_id: UUID,
    reason: str | None = None,
) -> Movement:
    if from_location_id == to_location_id:
        raise ValidationFailed("source and destination are the same location")
    lot = await _lot(s, lot_id, plant_id)
    return await post_movement(
        s, movement_type="311", lot=lot, quantity=quantity,
        from_location=await get_location(s, from_location_id, plant_id),
        to_location=await get_location(s, to_location_id, plant_id), reason=reason,
    )


async def _lot(s: AsyncSession, lot_id: UUID, plant_id: UUID | None = None) -> Lot:
    lot = await s.get(Lot, lot_id)
    if lot is None or (plant_id and lot.plant_id != plant_id):
        raise NotFound("lot not found")
    return lot


async def issue_to_order(
    s: AsyncSession, *, component_id: UUID, lot_id: UUID, from_location_id: UUID, quantity: Decimal,
    client_event_id: UUID | None = None, occurred_at: datetime | None = None,
) -> Movement:
    comp = await get_component_context(s, component_id)
    if comp.wo_status not in ("DISPATCHED", "IN_PROGRESS"):
        raise DomainError(f"work order is {comp.wo_status}; material can be issued once it is dispatched", code="invalid_transition")
    lot = await _lot(s, lot_id, comp.plant_id)
    if lot.material_id != comp.material_id:
        raise ValidationFailed("lot is not the component's material")
    return await post_movement(
        s, movement_type="261", lot=lot, quantity=quantity,
        from_location=await get_location(s, from_location_id, comp.plant_id),
        order=OrderRef(comp.work_order_id, comp.erp_order_number, comp.operation_id, comp),
        client_event_id=client_event_id, occurred_at=occurred_at,
    )


async def source_locations(s: AsyncSession, plant_id: UUID, line_id: UUID | None) -> list[StorageLocation]:
    """Where to take components from, in preference order: this line's line-side, then semi-finished, then stores."""
    locs = await locations(s, plant_id)
    rank = {"LINESIDE": 0, "SEMI": 1, "STORE": 2}
    return sorted(
        (loc for loc in locs if loc.kind in rank and (loc.kind != "LINESIDE" or loc.line_id == line_id)),
        key=lambda loc: (rank[loc.kind], loc.code),
    )


async def backflush(
    s: AsyncSession, *, component: ComponentContext, line_id: UUID | None, quantity: Decimal, confirmation_id: UUID,
    occurred_at: datetime | None = None,
) -> list[Movement]:
    """Consume a backflushed component FIFO by lot across source locations. Fails if stock is short."""
    quantity = quantity.quantize(Q)
    if quantity <= 0:
        return []
    remaining = quantity
    picks: list[tuple[Stock, StorageLocation]] = []
    for loc in await source_locations(s, component.plant_id, line_id):
        rows = (
            await s.execute(
                select(Stock)
                .join(Lot, Lot.id == Stock.lot_id)
                .where(
                    Stock.location_id == loc.id, Stock.material_id == component.material_id, Stock.quantity > 0,
                    Stock.status == UNRESTRICTED,
                )
                .order_by(Lot.created_at)
            )
        ).scalars().all()
        for st in rows:
            if remaining <= 0:
                break
            take = min(st.quantity, remaining)
            picks.append((st, loc))
            remaining -= take
    if remaining > 0:
        mats = await get_materials(s, {component.material_id})
        raise DomainError(
            f"backflush of {mats[component.material_id].material_number} short by {remaining:f} {component.uom} "
            "(replenish the line-side location)",
            code="insufficient_stock",
            details={"material": mats[component.material_id].material_number, "short": str(remaining)},
        )
    remaining = quantity
    out = []
    order = OrderRef(component.work_order_id, component.erp_order_number, component.operation_id, component, confirmation_id)
    for st, loc in picks:
        take = min(st.quantity, remaining)
        lot = await _lot(s, st.lot_id)
        out.append(
            await post_movement(
                s, movement_type="261", lot=lot, quantity=take, from_location=loc, order=order, backflush=True,
                occurred_at=occurred_at,
            )
        )
        remaining -= take
    return out


async def issue_for_maintenance(
    s: AsyncSession, *, plant_id: UUID, maintenance_order_id: UUID, erp_order_number: str, material_id: UUID,
    quantity: Decimal, client_event_id: UUID | None = None,
) -> list[Movement]:
    """Spare parts to a maintenance order (261 against the PM order): FIFO by lot from stores, free stock only."""
    if client_event_id:
        existing = (await s.scalars(select(Movement).where(Movement.client_event_id == client_event_id))).first()
        if existing:
            return [existing]
    quantity = Decimal(quantity).quantize(Q)
    if quantity <= 0:
        raise ValidationFailed("quantity must be positive")
    remaining = quantity
    picks: list[tuple[Stock, StorageLocation, Decimal]] = []
    for loc in (x for x in await locations(s, plant_id) if x.kind == "STORE"):
        rows = (
            await s.execute(
                select(Stock).join(Lot, Lot.id == Stock.lot_id)
                .where(Stock.location_id == loc.id, Stock.material_id == material_id, Stock.quantity > 0,
                       Stock.status == UNRESTRICTED)
                .order_by(Lot.created_at)
            )
        ).scalars().all()
        for st in rows:
            if remaining <= 0:
                break
            take = min(st.quantity, remaining)
            picks.append((st, loc, take))
            remaining -= take
    if remaining > 0:
        mat = (await get_materials(s, {material_id})).get(material_id)
        raise DomainError(
            f"not enough {mat.material_number if mat else 'material'} in stores: short by {remaining:f}",
            code="insufficient_stock", details={"short": str(remaining)},
        )
    order = OrderRef(None, erp_order_number, maintenance_order_id=maintenance_order_id)
    out = []
    for i, (st, loc, take) in enumerate(picks):
        out.append(await post_movement(
            s, movement_type="261", lot=await _lot(s, st.lot_id), quantity=take, from_location=loc, order=order,
            client_event_id=client_event_id if i == 0 else None, reason=f"spare parts for maintenance order {erp_order_number}",
        ))
    return out


async def movements_of_maintenance_order(s: AsyncSession, maintenance_order_id: UUID) -> list[Movement]:
    return list((await s.scalars(
        select(Movement).where(Movement.maintenance_order_id == maintenance_order_id).order_by(Movement.posted_at)
    )).all())


async def goods_receipt(
    s: AsyncSession, *, plant_id: UUID, work_order_id: UUID, erp_order_number: str, material_id: UUID, uom: str,
    quantity: Decimal, confirmation_id: UUID, operation_id: UUID, occurred_at: datetime | None = None,
    to_status: str = UNRESTRICTED,
) -> tuple[Movement, list[str]]:
    """Receive produced good quantity into stock as a new production lot (101). Serialised materials get one
    serial number per unit. Returns the movement and the serial numbers."""
    mat = (await get_materials(s, {material_id}))[material_id]
    kind = {"FERT": "FG", "HALB": "SEMI"}.get(mat.material_type, "STORE")
    dest = next((loc for loc in await locations(s, plant_id) if loc.kind == kind), None)
    if dest is None:
        raise PermanentError(f"no {kind} storage location configured for goods receipt")
    lot = await new_lot(s, plant_id=plant_id, material_id=material_id, uom=uom, origin="PRODUCTION", work_order_id=work_order_id)
    serials: list[str] = []
    if mat.is_serialized:
        if quantity != quantity.to_integral_value():
            raise ValidationFailed(f"{mat.material_number} is serialised: report whole units")
        serials = await _assign_serials(
            s, lot=lot, material_number=mat.material_number, work_order_id=work_order_id,
            confirmation_id=confirmation_id, count=int(quantity),
            status="IN_QUALITY" if to_status == QUALITY else "AVAILABLE",
        )
    mv = await post_movement(
        s, movement_type="101", lot=lot, quantity=quantity, to_location=dest, to_status=to_status,
        order=OrderRef(work_order_id, erp_order_number, operation_id, None, confirmation_id), occurred_at=occurred_at,
        serials=serials,
    )
    return mv, serials


async def _assign_serials(
    s: AsyncSession, *, lot: Lot, material_number: str, work_order_id: UUID, confirmation_id: UUID, count: int, status: str
) -> list[str]:
    out = []
    for _ in range(count):
        seq = (await s.execute(select(SERIAL_SEQ.next_value()))).scalar_one()
        serial = f"{material_number}-{seq:07d}"[-18:]
        s.add(SerialNumber(plant_id=lot.plant_id, material_id=lot.material_id, serial=serial, lot_id=lot.id,
                           work_order_id=work_order_id, confirmation_id=confirmation_id, status=status))
        out.append(serial)
    await s.flush()
    return out


async def set_serial_status(s: AsyncSession, lot_id: UUID, status: str, *, only: str | None = None) -> int:
    rows = (await s.scalars(select(SerialNumber).where(SerialNumber.lot_id == lot_id))).all()
    n = 0
    for sn in rows:
        if (only is None or sn.status == only) and sn.status != status:
            sn.status = status
            n += 1
    await s.flush()
    return n


async def change_status(
    s: AsyncSession, *, lot_id: UUID, movement_type: str, reason: str | None, quantity: Decimal | None = None,
) -> list[Movement]:
    """Move a lot's stock between stock types (321/350/344/343) at every location that holds it.
    `quantity=None` moves everything in the source status."""
    frm, to = STATUS_MOVES[movement_type]
    lot = await _lot(s, lot_id)
    out = []
    remaining = quantity
    for st in (await s.scalars(select(Stock).where(Stock.lot_id == lot_id, Stock.status == frm, Stock.quantity > 0))).all():
        take = st.quantity if remaining is None else min(st.quantity, remaining)
        if take <= 0:
            break
        loc = await get_location(s, st.location_id)
        out.append(await post_movement(s, movement_type=movement_type, lot=lot, quantity=take, from_location=loc,
                                       to_location=loc, from_status=frm, to_status=to, reason=reason))
        if remaining is not None:
            remaining -= take
    serial_status = {UNRESTRICTED: "AVAILABLE", BLOCKED: "BLOCKED", QUALITY: "IN_QUALITY"}[to]
    await set_serial_status(s, lot_id, serial_status)
    return out


async def scrap_blocked(
    s: AsyncSession, *, lot_id: UUID, quantity: Decimal | None, reason: str, movement_type: str = "555"
) -> list[Movement]:
    """Take blocked stock of a lot out of stock after a nonconformance disposition: scrap (555) or return to the
    supplier (122, purchased lots only)."""
    lot = await _lot(s, lot_id)
    if movement_type == "122" and lot.origin not in ("RECEIPT", "OPENING"):
        raise DomainError("only purchased lots can be returned to the supplier", code="not_purchased")
    out = []
    remaining = quantity
    for st in (await s.scalars(select(Stock).where(Stock.lot_id == lot_id, Stock.status == BLOCKED, Stock.quantity > 0))).all():
        take = st.quantity if remaining is None else min(st.quantity, remaining)
        if take <= 0:
            break
        loc = await get_location(s, st.location_id)
        out.append(await post_movement(s, movement_type=movement_type, lot=lot, quantity=take, from_location=loc,
                                       from_status=BLOCKED, reason=reason))
        if remaining is not None:
            remaining -= take
    if quantity is None or not await stock_of_lot(s, lot_id):
        await set_serial_status(s, lot_id, "SCRAPPED", only="BLOCKED")
    return out


async def stock_of_lot(s: AsyncSession, lot_id: UUID) -> list[tuple[StorageLocation, str, Decimal]]:
    rows = (
        await s.execute(
            select(Stock, StorageLocation)
            .join(StorageLocation, StorageLocation.id == Stock.location_id)
            .where(Stock.lot_id == lot_id, Stock.quantity > 0)
        )
    ).all()
    return [(loc, st.status, st.quantity) for st, loc in rows]


async def apply_erp_result(s: AsyncSession, payload: dict[str, Any]) -> None:
    mv = (await s.scalars(select(Movement).where(Movement.id == UUID(payload["movement_id"])).with_for_update())).first()
    if mv is None:
        raise PermanentError(f"goods movement {payload['movement_id']} unknown to MES")
    if payload["status"] == "POSTED":
        mv.erp_sync_status = "POSTED"
        mv.erp_material_document = payload.get("erp_material_document")
        mv.erp_document_year = payload.get("erp_document_year")
        mv.erp_reconciled = bool(payload.get("reconciled"))
        mv.erp_error = None
    elif payload["status"] == "FAILED":
        if mv.erp_sync_status == "POSTED":
            return
        mv.erp_sync_status = "FAILED"
        mv.erp_error = payload.get("error")
    else:
        raise PermanentError(f"unknown goods movement result {payload['status']!r}")
    mv.erp_synced_at = datetime.now(UTC)
    await s.flush()


# ------------------------------------------------------------------------------------------------ queries


async def issued_by_component(s: AsyncSession, component_ids: list[UUID]) -> dict[UUID, Decimal]:
    if not component_ids:
        return {}
    signed = func.sum(
        case((Movement.movement_type == "262", -Movement.quantity), else_=Movement.quantity)
    )
    rows = (
        await s.execute(
            select(Movement.component_id, signed)
            .where(Movement.component_id.in_(component_ids), Movement.movement_type.in_(["261", "262"]))
            .group_by(Movement.component_id)
        )
    ).all()
    return {r[0]: r[1] for r in rows}


async def available_by_material(s: AsyncSession, plant_id: UUID, material_ids: set[UUID]) -> dict[UUID, dict[str, Decimal]]:
    """material -> {location kind or 'LINESIDE:<line_id>' -> quantity}."""
    if not material_ids:
        return {}
    rows = (
        await s.execute(
            select(Stock.material_id, StorageLocation.kind, StorageLocation.line_id, func.sum(Stock.quantity))
            .join(StorageLocation, StorageLocation.id == Stock.location_id)
            .where(Stock.plant_id == plant_id, Stock.material_id.in_(material_ids), Stock.status == UNRESTRICTED)
            .group_by(Stock.material_id, StorageLocation.kind, StorageLocation.line_id)
        )
    ).all()
    out: dict[UUID, dict[str, Decimal]] = {}
    for mat, kind, line_id, qty in rows:
        key = f"LINESIDE:{line_id}" if kind == "LINESIDE" else kind
        out.setdefault(mat, {})[key] = out.get(mat, {}).get(key, Decimal(0)) + qty
    return out


# ------------------------------------------------------------------------------------------------ traceability reads


@dataclass(frozen=True)
class LotInfo:
    id: UUID
    plant_id: UUID
    lot_number: str
    material_id: UUID
    origin: str
    uom: str
    work_order_id: UUID | None
    created_at: datetime


def _info(lot: Lot) -> LotInfo:
    return LotInfo(lot.id, lot.plant_id, lot.lot_number, lot.material_id, lot.origin, lot.uom, lot.work_order_id, lot.created_at)


async def get_lot_info(s: AsyncSession, lot_id: UUID) -> LotInfo:
    return _info(await _lot(s, lot_id))


async def find_lots(s: AsyncSession, plant_id: UUID, text: str, limit: int = 20) -> list[LotInfo]:
    rows = (
        await s.scalars(
            select(Lot).where(Lot.plant_id == plant_id, Lot.lot_number.ilike(f"%{text}%")).order_by(Lot.created_at.desc()).limit(limit)
        )
    ).all()
    return [_info(x) for x in rows]


async def lots_produced_by(s: AsyncSession, work_order_id: UUID) -> list[LotInfo]:
    rows = (await s.scalars(select(Lot).where(Lot.work_order_id == work_order_id).order_by(Lot.created_at))).all()
    return [_info(x) for x in rows]


def _signed_issue():  # type: ignore[no-untyped-def]
    return func.sum(case((Movement.movement_type == "262", -Movement.quantity), else_=Movement.quantity))


async def consumption_of_work_order(s: AsyncSession, work_order_id: UUID) -> list[tuple[UUID, Decimal]]:
    """(component lot, net quantity) issued to an order (261 minus 262)."""
    rows = (
        await s.execute(
            select(Movement.lot_id, _signed_issue())
            .where(Movement.work_order_id == work_order_id, Movement.movement_type.in_(["261", "262"]))
            .group_by(Movement.lot_id)
        )
    ).all()
    return [(r[0], r[1]) for r in rows if r[1] > 0]


async def consumers_of_lot(s: AsyncSession, lot_id: UUID) -> list[tuple[UUID, Decimal]]:
    """(work order, net quantity) for every order this lot was issued to."""
    rows = (
        await s.execute(
            select(Movement.work_order_id, _signed_issue())
            .where(Movement.lot_id == lot_id, Movement.movement_type.in_(["261", "262"]))
            .group_by(Movement.work_order_id)
        )
    ).all()
    return [(r[0], r[1]) for r in rows if r[0] and r[1] > 0]


async def first_movement_of_lot(s: AsyncSession, lot_id: UUID) -> Movement | None:
    return (
        await s.scalars(
            select(Movement).where(Movement.lot_id == lot_id, Movement.movement_type.in_(["561", "501", "101"])).order_by(Movement.posted_at)
        )
    ).first()


@dataclass(frozen=True)
class SerialInfo:
    id: UUID
    serial: str
    material_id: UUID
    lot_id: UUID
    work_order_id: UUID | None
    status: str
    created_at: datetime


def _serial(x: SerialNumber) -> SerialInfo:
    return SerialInfo(x.id, x.serial, x.material_id, x.lot_id, x.work_order_id, x.status, x.created_at)


async def serials_of_lot(s: AsyncSession, lot_id: UUID) -> list[SerialInfo]:
    rows = (await s.scalars(select(SerialNumber).where(SerialNumber.lot_id == lot_id).order_by(SerialNumber.serial))).all()
    return [_serial(x) for x in rows]


async def serials_of_work_order(s: AsyncSession, work_order_id: UUID) -> list[SerialInfo]:
    rows = (
        await s.scalars(select(SerialNumber).where(SerialNumber.work_order_id == work_order_id).order_by(SerialNumber.serial))
    ).all()
    return [_serial(x) for x in rows]


async def find_serials(s: AsyncSession, plant_id: UUID, text: str, limit: int = 20) -> list[SerialInfo]:
    rows = (
        await s.scalars(
            select(SerialNumber).where(SerialNumber.plant_id == plant_id, SerialNumber.serial.ilike(f"%{text}%"))
            .order_by(SerialNumber.serial).limit(limit)
        )
    ).all()
    return [_serial(x) for x in rows]
