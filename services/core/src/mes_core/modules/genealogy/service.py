"""Genealogy: backward (where did this come from) and forward (where did it go) traces.

The graph is lots and orders: an order consumes component lots (261 - 262) and produces lots (101). Every node is
enriched with what an auditor asks for: operations, machines and operators, inspections and nonconformances,
current stock by stock type and serial numbers. Read-only; no own tables.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from mes_core.modules.execution.public import execution_steps
from mes_core.modules.inventory.public import (
    QUALITY,
    UNRESTRICTED,
    LotInfo,
    consumers_of_lot,
    consumption_of_work_order,
    first_movement_of_lot,
    get_lot_info,
    lots_produced_by,
    serials_of_lot,
    stock_of_lot,
)
from mes_core.modules.masterdata.public import get_materials
from mes_core.modules.org.public import list_equipment, work_centers
from mes_core.modules.planning.public import get_work_order_brief, operations_brief
from mes_core.modules.quality.public import (
    inspections_of_lot,
    inspections_of_work_order,
    ncs_of,
    quarantine_lot,
)
from mes_core.shared.users import CurrentUser

MAX_DEPTH = 8
MAX_NODES = 300


@dataclass
class _Ctx:
    s: AsyncSession
    seen: set[tuple[str, UUID]] = field(default_factory=set)
    nodes: int = 0
    lots: dict[UUID, LotInfo] = field(default_factory=dict)

    async def lot(self, lot_id: UUID) -> LotInfo:
        if lot_id not in self.lots:
            self.lots[lot_id] = await get_lot_info(self.s, lot_id)
        return self.lots[lot_id]


async def _material(s: AsyncSession, material_id: UUID) -> tuple[str, str]:
    m = (await get_materials(s, {material_id})).get(material_id)
    return (m.material_number, m.description) if m else ("?", "")


async def _quality(s: AsyncSession, *, lot_id: UUID | None = None, wo_id: UUID | None = None) -> dict[str, Any]:
    insp = await (inspections_of_lot(s, lot_id) if lot_id else inspections_of_work_order(s, wo_id))  # type: ignore[arg-type]
    ncs = await ncs_of(s, lot_ids=[lot_id] if lot_id else None, work_order_ids=[wo_id] if wo_id else None)
    return {
        "inspections": [{"id": i.id, "number": i.number, "inspection_type": i.inspection_type, "status": i.status,
                         "usage_decision": i.usage_decision} for i in insp],
        "nonconformances": [{"id": n.id, "number": n.number, "status": n.status, "severity": n.severity,
                             "defect_code": n.defect_code, "description": n.description,
                             "disposition": n.disposition} for n in ncs],
    }


async def _lot_node(c: _Ctx, lot_id: UUID, qty: Decimal | None) -> dict[str, Any]:
    lot = await c.lot(lot_id)
    number, desc = await _material(c.s, lot.material_id)
    first = await first_movement_of_lot(c.s, lot_id)
    stock = [{"location_code": loc.code, "status": st, "quantity": q} for loc, st, q in await stock_of_lot(c.s, lot_id)]
    serials = await serials_of_lot(c.s, lot_id)
    return {
        "type": "LOT", "id": lot.id, "label": lot.lot_number, "material_id": lot.material_id,
        "material_number": number, "material_description": desc, "quantity": qty, "uom": lot.uom,
        "lot": {
            "lot_number": lot.lot_number, "origin": lot.origin, "created_at": lot.created_at,
            "received_qty": first.quantity if first else None, "work_order_id": lot.work_order_id,
            "stock": stock, "serial_count": len(serials), "serials": [x.serial for x in serials[:50]],
        },
        "order": None, **await _quality(c.s, lot_id=lot_id), "children": [], "truncated": False,
    }


async def _order_node(c: _Ctx, wo_id: UUID, qty: Decimal | None) -> dict[str, Any]:
    wo = await get_work_order_brief(c.s, wo_id)
    number, desc = await _material(c.s, wo.material_id)
    ops = await operations_brief(c.s, wo_id)
    steps = await execution_steps(c.s, wo_id)
    eq = {e.id: e.code for e in await list_equipment(c.s, wo.plant_id)}
    wcs = await work_centers(c.s, wo.plant_id)
    operations = [
        {
            "op_no": o.op_no, "description": o.description, "status": o.status,
            "work_center": wcs[o.work_center_id].code if o.work_center_id in wcs else None,
            "yield_qty": o.yield_qty, "scrap_qty": o.scrap_qty,
            "steps": [{"equipment_code": eq.get(x.equipment_id) if x.equipment_id else None,
                       "operator_name": x.operator_name, "started_at": x.started_at, "ended_at": x.ended_at,
                       "yield_qty": x.yield_qty, "scrap_qty": x.scrap_qty, "source": x.source}
                      for x in steps if x.operation_id == o.id],
        }
        for o in ops
    ]
    return {
        "type": "ORDER", "id": wo.id, "label": wo.erp_order_number, "material_id": wo.material_id,
        "material_number": number, "material_description": desc, "quantity": qty, "uom": wo.uom, "lot": None,
        "order": {
            "erp_order_number": wo.erp_order_number, "status": wo.status, "planned_qty": wo.planned_qty,
            "yield_qty": ops[-1].yield_qty if ops else Decimal(0),
            "scrap_qty": sum((o.scrap_qty for o in ops), Decimal(0)), "operations": operations,
        },
        **await _quality(c.s, wo_id=wo_id), "children": [], "truncated": False,
    }


def _enter(c: _Ctx, kind: str, id_: UUID, depth: int, node: dict[str, Any]) -> bool:
    """False (and the node marked truncated) when the branch must stop: cycle, depth or size limit."""
    if (kind, id_) in c.seen or depth >= MAX_DEPTH or c.nodes >= MAX_NODES:
        node["truncated"] = True
        return False
    c.seen.add((kind, id_))
    c.nodes += 1
    return True


async def _backward_lot(c: _Ctx, lot_id: UUID, qty: Decimal | None, depth: int) -> dict[str, Any]:
    node = await _lot_node(c, lot_id, qty)
    if _enter(c, "LOT", lot_id, depth, node):
        lot = await c.lot(lot_id)
        if lot.work_order_id:
            node["children"].append(await _backward_order(c, lot.work_order_id, None, depth + 1))
    return node


async def _backward_order(c: _Ctx, wo_id: UUID, qty: Decimal | None, depth: int) -> dict[str, Any]:
    node = await _order_node(c, wo_id, qty)
    if _enter(c, "ORDER", wo_id, depth, node):
        for lot_id, consumed in await consumption_of_work_order(c.s, wo_id):
            node["children"].append(await _backward_lot(c, lot_id, consumed, depth + 1))
    return node


async def _forward_lot(c: _Ctx, lot_id: UUID, qty: Decimal | None, depth: int) -> dict[str, Any]:
    node = await _lot_node(c, lot_id, qty)
    if _enter(c, "LOT", lot_id, depth, node):
        for wo_id, consumed in await consumers_of_lot(c.s, lot_id):
            node["children"].append(await _forward_order(c, wo_id, consumed, depth + 1))
    return node


async def _forward_order(c: _Ctx, wo_id: UUID, qty: Decimal | None, depth: int) -> dict[str, Any]:
    node = await _order_node(c, wo_id, qty)
    if _enter(c, "ORDER", wo_id, depth, node):
        for lot in await lots_produced_by(c.s, wo_id):
            first = await first_movement_of_lot(c.s, lot.id)
            node["children"].append(await _forward_lot(c, lot.id, first.quantity if first else None, depth + 1))
    return node


async def trace_lot(s: AsyncSession, lot_id: UUID, direction: str) -> dict[str, Any]:
    c = _Ctx(s)
    return await (_backward_lot(c, lot_id, None, 0) if direction == "backward" else _forward_lot(c, lot_id, None, 0))


async def trace_order(s: AsyncSession, wo_id: UUID, direction: str) -> dict[str, Any]:
    c = _Ctx(s)
    return await (_backward_order(c, wo_id, None, 0) if direction == "backward" else _forward_order(c, wo_id, None, 0))


def flatten_lots(node: dict[str, Any]) -> list[dict[str, Any]]:
    out, stack, seen = [], [node], set()
    while stack:
        n = stack.pop()
        if n["type"] == "LOT" and n["id"] not in seen:
            seen.add(n["id"])
            out.append(n)
        stack.extend(n["children"])
    return out


def free_qty(lot_node: dict[str, Any]) -> Decimal:
    return sum((x["quantity"] for x in lot_node["lot"]["stock"] if x["status"] in (UNRESTRICTED, QUALITY)), Decimal(0))


async def quarantine_affected(
    s: AsyncSession, user: CurrentUser, lot_id: UUID, *, reason: str, include_root: bool = True
) -> list[dict[str, Any]]:
    """Block every lot downstream of a suspect lot that still has free or in-inspection stock (344/350), one
    nonconformance per lot. Returns what was blocked."""
    root = await trace_lot(s, lot_id, "forward")
    out = []
    for n in flatten_lots(root):
        if (n["id"] == lot_id and not include_root) or free_qty(n) <= 0:
            continue
        nc = await quarantine_lot(s, user, lot_id=n["id"], reason=reason, source="TRACEABILITY")
        out.append({"lot_id": n["id"], "lot_number": n["label"], "material_number": n["material_number"],
                    "blocked_qty": nc.blocked_qty, "uom": n["uom"], "nc_id": nc.id, "nc_number": nc.number})
    return out


