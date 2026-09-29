"""Write the event contracts (JSON Schema) to /contracts/events.

The canonical ERP models live in the adapter (erp_adapter.ports); the MES-side payloads are declared here.
Run after changing any event payload: `uv run python scripts/export_contracts.py`.
"""

import json
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel

from erp_adapter.ports import (
    CanonicalMaintOrderChange,
    CanonicalWorkOrder,
    ConfirmationCommand,
    GoodsMovementCommand,
    MaintNotificationCommand,
    MaintOrderCommand,
    MaintOrderStatusCommand,
)
from mes_common.events import EventEnvelope, Topics

OUT = Path(__file__).resolve().parents[1] / "contracts" / "events"


class ConfirmationResult(BaseModel):
    confirmation_id: UUID
    status: str  # POSTED | FAILED
    erp_confirmation_group: str | None
    erp_confirmation_count: str | None
    error: str | None
    attempts: int
    reconciled: bool


class WorkOrderStatusChanged(BaseModel):
    work_order_id: UUID
    plant_id: UUID
    erp_order_number: str
    from_status: str
    to_status: str
    reason: str | None


class GoodsMovementResult(BaseModel):
    movement_id: UUID
    status: str  # POSTED | FAILED
    erp_material_document: str | None
    erp_document_year: str | None
    error: str | None
    attempts: int
    reconciled: bool


class RunChanged(BaseModel):
    run_id: UUID
    event_type: str  # START_SETUP START_RUN START_PRODUCTION PAUSE RESUME REPORT COMPLETE STOP
    status: str  # SETUP RUNNING PAUSED ENDED
    plant_id: UUID
    work_order_id: UUID
    operation_id: UUID
    equipment_id: UUID
    operator_id: UUID
    occurred_at: str
    reason_code: str | None


class SchedulePublished(BaseModel):
    schedule_id: UUID
    plant_id: UUID
    version: int
    entries: int
    signature_id: UUID


class InspectionChanged(BaseModel):
    plant_id: UUID
    inspection_lot_id: UUID
    number: str
    status: str  # OPEN PASSED FAILED CANCELLED
    inspection_type: str | None = None  # INCOMING IN_PROCESS FINAL
    usage_decision: str | None = None  # ACCEPTED REJECTED
    work_order_id: UUID | None = None
    operation_id: UUID | None = None


class NonconformanceChanged(BaseModel):
    plant_id: UUID
    nc_id: UUID
    number: str
    status: str  # OPEN REWORK CLOSED
    severity: str | None = None
    disposition: str | None = None
    work_order_id: UUID | None = None


class MaintenanceResult(BaseModel):
    message_type: str  # MAINT_NOTIFICATION | MAINT_ORDER | MAINT_ORDER_STATUS
    number: str
    notification_id: UUID | None = None
    order_id: UUID | None = None
    message_id: UUID | None = None
    status: str  # POSTED | FAILED
    system_status: str | None = None
    error: str | None = None
    attempts: int
    reconciled: bool


class TicketComment(BaseModel):
    id: UUID
    text: str
    author: str | None


class TicketEvent(BaseModel):
    """mes.ticket.created / mes.ticket.updated: the ticket snapshot plus what changed."""

    ticket_id: UUID
    plant_id: UUID
    number: str
    title: str
    description: str
    category: str
    impact: str  # LINE_STOP DEGRADED MINOR SERVICE_REQUEST
    severity: str  # P1..P4
    tier: str  # L1 L2 L3
    status: str
    source: str
    reported_by: str | None
    reported_at: str
    resolution_note: str | None
    response_due_at: str
    resolution_due_at: str | None
    sn_sys_id: str | None
    sn_number: str | None
    jira_key: str | None
    line_code: str | None
    equipment_code: str | None
    erp_order_number: str | None
    terminal_code: str | None
    changes: list[str]  # created | status | severity | tier | comment
    origin: str  # MES | SERVICENOW | JIRA
    comment: TicketComment | None


class TicketSlaBreached(BaseModel):
    ticket_id: UUID
    number: str
    severity: str
    tier: str
    which: str  # response | resolution
    plant_id: UUID


class TicketSyncResult(BaseModel):
    ticket_id: UUID
    status: str  # SYNCED | FAILED
    sn_number: str | None = None
    sn_sys_id: str | None = None
    sn_state: str | None = None
    sn_group: str | None = None
    sn_url: str | None = None
    jira_key: str | None = None
    jira_status: str | None = None
    jira_url: str | None = None
    error: str | None = None


class TicketChanged(BaseModel):
    """A change an engineer made in ServiceNow or Jira."""

    ticket_id: UUID
    origin: str  # SERVICENOW | JIRA
    external_id: str  # dedup key
    author: str | None = None
    at: str | None = None
    sn_state: str | None = None  # 1 New, 2 In Progress, 3 On Hold, 6 Resolved, 7 Closed, 8 Canceled
    sn_group: str | None = None
    tier: str | None = None
    severity: str | None = None
    resolution_note: str | None = None
    jira_key: str | None = None
    jira_status: str | None = None
    comment: str | None = None


CONTRACTS = [
    ("envelope", None, EventEnvelope, "Wrapper around every event on every topic"),
    ("erp.work_order.upserted.v1", Topics.ERP_WORK_ORDERS, CanonicalWorkOrder, "erp-adapter -> core: full order snapshot"),
    ("mes.confirmation.created.v1", Topics.MES_CONFIRMATIONS, ConfirmationCommand, "core -> erp-adapter: post to SAP"),
    ("erp.confirmation.result.v1", Topics.ERP_CONFIRMATION_RESULTS, ConfirmationResult, "erp-adapter -> core: outcome"),
    ("mes.work_order.status_changed.v1", Topics.MES_PLANNING, WorkOrderStatusChanged, "core -> anyone"),
    ("mes.goods_movement.created.v1", Topics.MES_GOODS_MOVEMENTS, GoodsMovementCommand, "core -> erp-adapter: post to SAP"),
    ("erp.goods_movement.result.v1", Topics.ERP_GOODS_MOVEMENT_RESULTS, GoodsMovementResult, "erp-adapter -> core: outcome"),
    ("mes.execution.run_changed.v1", Topics.MES_EXECUTION, RunChanged, "core -> anyone (analytics, alerts)"),
    ("mes.schedule.published.v1", Topics.MES_PLANNING, SchedulePublished, "core -> anyone"),
    ("mes.quality.inspection_changed.v1", Topics.MES_QUALITY, InspectionChanged, "core -> anyone (alerts, analytics)"),
    ("mes.quality.nc_changed.v1", Topics.MES_QUALITY, NonconformanceChanged, "core -> anyone (alerts, analytics)"),
    ("mes.maintenance.notification_created.v1", Topics.MES_MAINTENANCE, MaintNotificationCommand, "core -> erp-adapter: SAP PM notification"),
    ("mes.maintenance.order_created.v1", Topics.MES_MAINTENANCE, MaintOrderCommand, "core -> erp-adapter: SAP PM order"),
    ("mes.maintenance.order_status.v1", Topics.MES_MAINTENANCE, MaintOrderStatusCommand, "core -> erp-adapter: PM order REL / TECO"),
    ("erp.maintenance.result.v1", Topics.ERP_MAINTENANCE_RESULTS, MaintenanceResult, "erp-adapter -> core: outcome"),
    ("erp.maintenance_order.updated.v1", Topics.ERP_MAINTENANCE_ORDERS, CanonicalMaintOrderChange, "erp-adapter -> core: PM order changed in SAP"),
    ("mes.ticket.created.v1", Topics.MES_TICKETS, TicketEvent, "core -> itsm-adapter (and anyone)"),
    ("mes.ticket.updated.v1", Topics.MES_TICKETS, TicketEvent, "core -> itsm-adapter (and anyone)"),
    ("mes.ticket.sla_breached.v1", Topics.MES_TICKETS, TicketSlaBreached, "core -> anyone (phase 6 notifications)"),
    ("itsm.ticket.sync_result.v1", Topics.ITSM_TICKET_RESULTS, TicketSyncResult, "itsm-adapter -> core: ServiceNow / Jira refs"),
    ("itsm.ticket.changed.v1", Topics.ITSM_TICKET_CHANGES, TicketChanged, "itsm-adapter -> core: engineers' changes"),
]

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, topic, model, description in CONTRACTS:
        schema = model.model_json_schema()
        schema["$id"] = f"urn:mes:events:{name}"
        schema["description"] = description + (f" (topic {topic}, payload of the envelope)" if topic else "")
        (OUT / f"{name}.schema.json").write_text(json.dumps(schema, indent=2) + "\n")
        print(f"wrote {name}")
