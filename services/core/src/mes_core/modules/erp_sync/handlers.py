"""Core side of the integration contracts: consumes canonical events from erp-adapter (erp.*) and itsm-adapter
(itsm.*) and applies them via module APIs.

This module is the only place in core that understands those event payloads. It knows no SAP, ServiceNow or Jira
field names.
"""

import logging
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from mes_common.events import EventEnvelope, PermanentError, Topics
from mes_core.modules.execution.public import apply_erp_result
from mes_core.modules.inventory.public import apply_erp_result as apply_goods_movement_result
from mes_core.modules.maintenance.public import apply_erp_order_update
from mes_core.modules.maintenance.public import apply_erp_result as apply_maintenance_result
from mes_core.modules.masterdata.public import upsert_material
from mes_core.modules.org.public import find_plant_by_erp_code, find_work_center_by_erp
from mes_core.modules.planning.public import (
    ErpComponentData,
    ErpOperationData,
    ErpWorkOrderData,
    upsert_from_erp,
)
from mes_core.modules.ticketing.public import apply_external, apply_sync_result, raise_automatic
from mes_core.shared.db.context import Actor, set_actor
from mes_core.shared.db.session import apply_audit_context

log = logging.getLogger(__name__)

EVENT_WORK_ORDER_UPSERTED = "erp.work_order.upserted"
EVENT_CONFIRMATION_RESULT = "erp.confirmation.result"
EVENT_GOODS_MOVEMENT_RESULT = "erp.goods_movement.result"
EVENT_MAINTENANCE_RESULT = "erp.maintenance.result"
EVENT_MAINTENANCE_ORDER_UPDATED = "erp.maintenance_order.updated"
EVENT_TICKET_SYNC_RESULT = "itsm.ticket.sync_result"
EVENT_TICKET_CHANGED = "itsm.ticket.changed"
TOPICS = [
    Topics.ERP_WORK_ORDERS, Topics.ERP_CONFIRMATION_RESULTS, Topics.ERP_GOODS_MOVEMENT_RESULTS,
    Topics.ERP_MAINTENANCE_RESULTS, Topics.ERP_MAINTENANCE_ORDERS, Topics.ITSM_TICKET_RESULTS, Topics.ITSM_TICKET_CHANGES,
]
ACTOR = Actor.service("erp-sync")


async def on_session(s: AsyncSession) -> None:
    """Runs at the start of each consumer transaction: this task writes as the erp-sync service account."""
    set_actor(ACTOR)  # the consumer runs in its own task, so this never leaks into request handling
    await apply_audit_context(s, ACTOR)


async def _material(s: AsyncSession, m: dict):  # type: ignore[no-untyped-def]
    return await upsert_material(
        s,
        material_number=m["number"],
        description=m["description"],
        base_uom=m["base_uom"],
        material_type=m["material_type"],
        serial_profile=m.get("serial_profile"),
    )


async def handle_work_order_upserted(s: AsyncSession, env: EventEnvelope) -> None:
    p = env.payload
    plant = await find_plant_by_erp_code(s, p["erp_plant"])
    if plant is None:
        raise PermanentError(f"ERP plant {p['erp_plant']} is not configured in MES")

    operations = []
    line_ids = set()
    for op in p["operations"]:
        wc = await find_work_center_by_erp(s, plant.id, op["work_center"])
        if wc is None:
            raise PermanentError(f"ERP work center {op['work_center']} is not mapped in plant {plant.code}")
        line_ids.add(wc.line_id)
        operations.append(
            ErpOperationData(
                op_no=op["op_no"],
                description=op["description"],
                work_center_id=wc.id,
                control_key=op.get("control_key"),
                planned_qty=Decimal(op["planned_qty"]),
                uom=op["uom"],
                setup_min=Decimal(op["setup_min"]),
                run_min_per_unit=Decimal(op["run_min_per_unit"]),
                labor_min_per_unit=Decimal(op["labor_min_per_unit"]),
                erp_routing_no=op.get("erp_routing_no"),
                erp_routing_counter=op.get("erp_routing_counter"),
            )
        )
    components = []
    for c in p["components"]:
        mat = await _material(s, c["material"])
        components.append(
            ErpComponentData(
                material_id=mat.id,
                op_no=c.get("op_no"),
                required_qty=Decimal(c["required_qty"]),
                uom=c["uom"],
                reservation_no=c["reservation_no"],
                reservation_item=c["reservation_item"],
                storage_location=c.get("storage_location"),
                backflush=bool(c.get("backflush")),
            )
        )
    product = await _material(s, p["material"])
    wo_id, outcome = await upsert_from_erp(
        s,
        ErpWorkOrderData(
            plant_id=plant.id,
            line_id=next(iter(line_ids)) if len(line_ids) == 1 else None,
            erp_order_number=p["erp_order_number"],
            order_type=p["order_type"],
            material_id=product.id,
            planned_qty=Decimal(p["planned_qty"]),
            uom=p["uom"],
            basic_start=date.fromisoformat(p["basic_start"]),
            basic_end=date.fromisoformat(p["basic_end"]),
            erp_status=list(p["erp_status"]),
            erp_last_changed_at=datetime.fromisoformat(p["erp_last_changed_at"]),
            operations=operations,
            components=components,
        ),
    )
    log.info("ERP order %s -> work order %s (%s)", p["erp_order_number"], wo_id, outcome)


async def _integration_alarm(s: AsyncSession, env: EventEnvelope, what: str, ref: str) -> None:
    """SAP finally rejected a posting (FAILED / DEAD): open an L2 support ticket so someone fixes the data."""
    p = env.payload
    if p.get("status") != "FAILED":
        return
    plant = await find_plant_by_erp_code(s, env.key.split(":")[0])
    if plant is None:
        return
    await raise_automatic(
        s, plant_id=plant.id, integration_ref=f"sap:{ref}",
        title=f"SAP rejected {what} ({env.key.split(':', 1)[-1]})",
        description=f"{what} could not be posted to SAP after {p.get('attempts')} attempt(s): {p.get('error')}. "
        "Check the SAP integration monitor, fix the cause, then retry the message.",
    )


async def handle_confirmation_result(s: AsyncSession, env: EventEnvelope) -> None:
    await apply_erp_result(s, env.payload)
    await _integration_alarm(s, env, "a production confirmation", f"confirmation:{env.payload.get('confirmation_id')}")


async def handle_goods_movement_result(s: AsyncSession, env: EventEnvelope) -> None:
    await apply_goods_movement_result(s, env.payload)
    await _integration_alarm(s, env, "a goods movement", f"goods-movement:{env.payload.get('movement_id')}")


async def handle_maintenance_result(s: AsyncSession, env: EventEnvelope) -> None:
    await apply_maintenance_result(s, env.payload)
    ref = env.payload.get("notification_id") or env.payload.get("order_id")
    await _integration_alarm(s, env, "a maintenance notification/order", f"pm:{ref}")


async def handle_maintenance_order_updated(s: AsyncSession, env: EventEnvelope) -> None:
    await apply_erp_order_update(s, env.payload)


async def handle_ticket_sync_result(s: AsyncSession, env: EventEnvelope) -> None:
    await apply_sync_result(s, env.payload)


async def handle_ticket_changed(s: AsyncSession, env: EventEnvelope) -> None:
    await apply_external(s, env.payload)


HANDLERS = {
    EVENT_WORK_ORDER_UPSERTED: handle_work_order_upserted,
    EVENT_CONFIRMATION_RESULT: handle_confirmation_result,
    EVENT_GOODS_MOVEMENT_RESULT: handle_goods_movement_result,
    EVENT_MAINTENANCE_RESULT: handle_maintenance_result,
    EVENT_MAINTENANCE_ORDER_UPDATED: handle_maintenance_order_updated,
    EVENT_TICKET_SYNC_RESULT: handle_ticket_sync_result,
    EVENT_TICKET_CHANGED: handle_ticket_changed,
}
