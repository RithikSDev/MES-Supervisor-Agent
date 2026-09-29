from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mes_common.events import PermanentError, Topics
from mes_core.modules.execution.models import ProductionConfirmation
from mes_core.modules.inventory.public import QUALITY, UNRESTRICTED, backflush, goods_receipt
from mes_core.modules.org.public import get_plant, work_centers
from mes_core.modules.planning.public import (
    components_of_work_order,
    get_operation_context,
    get_work_order_brief,
    operations_brief,
    record_operation_progress,
)
from mes_core.modules.quality.public import (
    check_report_allowed,
    create_final_inspection,
    final_inspection_plan,
)
from mes_core.shared.errors import ValidationFailed
from mes_core.shared.messaging import messaging
from mes_core.shared.users import CurrentUser

EVENT_CONFIRMATION_CREATED = "mes.confirmation.created"


@dataclass(frozen=True)
class ConfirmationInput:
    yield_qty: Decimal
    scrap_qty: Decimal = Decimal(0)
    rework_qty: Decimal = Decimal(0)
    setup_min: Decimal = Decimal(0)
    run_min: Decimal = Decimal(0)
    labor_min: Decimal = Decimal(0)
    final: bool = False
    equipment_id: UUID | None = None
    comment: str | None = None
    source: str = "MANUAL"
    run_id: UUID | None = None
    scrap_reason: str | None = None
    client_event_id: UUID | None = None
    reported_at: datetime | None = None


async def create_confirmation(
    s: AsyncSession, user: CurrentUser, wo_id: UUID, op_id: UUID, inp: ConfirmationInput
) -> ProductionConfirmation:
    if inp.client_event_id:
        existing = (
            await s.scalars(select(ProductionConfirmation).where(ProductionConfirmation.client_event_id == inp.client_event_id))
        ).first()
        if existing:
            return existing  # replay of an offline terminal action
    ctx = await get_operation_context(s, wo_id, op_id)
    user.ensure_plant(ctx.plant_id)
    if min(inp.setup_min, inp.run_min, inp.labor_min) < 0:
        raise ValidationFailed("activity times must not be negative")
    if inp.yield_qty > 0:
        await check_report_allowed(s, work_order_id=wo_id, operation_id=op_id)
    await record_operation_progress(
        s, wo_id, op_id, yield_qty=inp.yield_qty, scrap_qty=inp.scrap_qty, final=inp.final
    )
    plant = await get_plant(s, ctx.plant_id)
    erp_required = ctx.source == "ERP" and plant.erp_plant_code is not None
    conf = ProductionConfirmation(
        plant_id=ctx.plant_id,
        work_order_id=wo_id,
        operation_id=op_id,
        equipment_id=inp.equipment_id,
        yield_qty=inp.yield_qty,
        scrap_qty=inp.scrap_qty,
        rework_qty=inp.rework_qty,
        uom=ctx.uom,
        setup_min=inp.setup_min,
        run_min=inp.run_min,
        labor_min=inp.labor_min,
        final=inp.final,
        comment=inp.comment,
        source=inp.source,
        reported_at=inp.reported_at or datetime.now(UTC),
        reported_by=str(user.id),
        reported_by_name=user.display_name or user.username,
        erp_sync_status="PENDING" if erp_required else "NOT_REQUIRED",
        run_id=inp.run_id,
        terminal_id=user.terminal_id,
        scrap_reason=inp.scrap_reason,
        client_event_id=inp.client_event_id,
    )
    s.add(conf)
    await s.flush()
    await _post_materials(s, conf, ctx.work_center_id)
    if erp_required:
        wc = (await work_centers(s, plant.id)).get(ctx.work_center_id)
        await messaging.publish(
            s,
            topic=Topics.MES_CONFIRMATIONS,
            key=f"{plant.erp_plant_code}:{ctx.erp_order_number}",
            event_type=EVENT_CONFIRMATION_CREATED,
            payload={
                "confirmation_id": str(conf.id),
                "erp_plant": plant.erp_plant_code,
                "erp_order_number": ctx.erp_order_number,
                "op_no": ctx.op_no,
                "work_center": wc.erp_work_center if wc else None,
                "yield_qty": str(inp.yield_qty),
                "scrap_qty": str(inp.scrap_qty),
                "rework_qty": str(inp.rework_qty),
                "uom": ctx.uom,
                "setup_min": str(inp.setup_min),
                "run_min": str(inp.run_min),
                "labor_min": str(inp.labor_min),
                "final": inp.final,
            },
        )
    return conf


async def _post_materials(s: AsyncSession, conf: ProductionConfirmation, work_center_id: UUID) -> None:
    """Goods movements implied by a confirmation, in the same transaction:
    backflushed components of this operation (261) and, on the last operation, receipt of good quantity (101) —
    into quality inspection stock with a final inspection lot if the material has a final inspection plan."""
    produced = conf.yield_qty + conf.scrap_qty
    wc = (await work_centers(s, conf.plant_id)).get(work_center_id)
    line_id = wc.line_id if wc else None
    if produced > 0:
        for comp in await components_of_work_order(s, conf.work_order_id):
            if comp.backflush and comp.operation_id == conf.operation_id:
                await backflush(
                    s, component=comp, line_id=line_id, quantity=comp.qty_for(produced), confirmation_id=conf.id,
                    occurred_at=conf.reported_at,
                )
    ops = await operations_brief(s, conf.work_order_id)
    if conf.yield_qty > 0 and ops and ops[-1].id == conf.operation_id:
        wo = await get_work_order_brief(s, conf.work_order_id)
        plan = await final_inspection_plan(s, conf.plant_id, wo.material_id)
        mv, _ = await goods_receipt(
            s, plant_id=conf.plant_id, work_order_id=wo.id, erp_order_number=wo.erp_order_number,
            material_id=wo.material_id, uom=wo.uom, quantity=conf.yield_qty, confirmation_id=conf.id,
            operation_id=conf.operation_id, occurred_at=conf.reported_at, to_status=QUALITY if plan else UNRESTRICTED,
        )
        if plan:
            await create_final_inspection(s, plan_id=plan.id, work_order_id=wo.id, lot_id=mv.lot_id,
                                          material_id=wo.material_id, quantity=mv.quantity)


async def apply_erp_result(s: AsyncSession, payload: dict) -> None:
    conf_id = UUID(payload["confirmation_id"])
    conf = (
        await s.scalars(select(ProductionConfirmation).where(ProductionConfirmation.id == conf_id).with_for_update())
    ).first()
    if conf is None:
        raise PermanentError(f"confirmation {conf_id} unknown to MES")
    status = payload["status"]
    if status == "POSTED":
        conf.erp_sync_status = "POSTED"
        conf.erp_confirmation_group = payload.get("erp_confirmation_group")
        conf.erp_confirmation_count = payload.get("erp_confirmation_count")
        conf.erp_reconciled = bool(payload.get("reconciled"))
        conf.erp_error = None
    elif status == "FAILED":
        if conf.erp_sync_status == "POSTED":
            return  # a late failure report never overrides a confirmed posting
        conf.erp_sync_status = "FAILED"
        conf.erp_error = payload.get("error")
    else:
        raise PermanentError(f"unknown confirmation result status {status!r}")
    conf.erp_attempts = payload.get("attempts")
    conf.erp_synced_at = datetime.now(UTC)
    await s.flush()
