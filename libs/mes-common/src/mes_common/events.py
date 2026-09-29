"""Event envelope and topic names shared by all services.

Payload schemas live in /contracts/events as JSON Schema; this module only defines the envelope.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from mes_common.ids import new_id


class Topics:
    # Produced by erp-adapter
    ERP_WORK_ORDERS = "erp.work-orders.v1"
    ERP_CONFIRMATION_RESULTS = "erp.confirmation-results.v1"
    ERP_GOODS_MOVEMENT_RESULTS = "erp.goods-movement-results.v1"
    ERP_MAINTENANCE_RESULTS = "erp.maintenance-results.v1"
    ERP_MAINTENANCE_ORDERS = "erp.maintenance-orders.v1"
    # Produced by itsm-adapter
    ITSM_TICKET_RESULTS = "itsm.ticket-results.v1"
    ITSM_TICKET_CHANGES = "itsm.ticket-changes.v1"
    # Produced by core
    MES_CONFIRMATIONS = "mes.confirmations.v1"
    MES_GOODS_MOVEMENTS = "mes.goods-movements.v1"
    MES_PLANNING = "mes.planning.v1"
    MES_EXECUTION = "mes.execution.v1"
    MES_QUALITY = "mes.quality.v1"
    MES_MAINTENANCE = "mes.maintenance.v1"
    MES_TICKETS = "mes.tickets.v1"
    # Produced by the support agent (proposals waiting for approval, handovers, agent health)
    AI_SUPPORT = "ai.support.v1"

    ALL = (
        ERP_WORK_ORDERS,
        ERP_CONFIRMATION_RESULTS,
        ERP_GOODS_MOVEMENT_RESULTS,
        ERP_MAINTENANCE_RESULTS,
        ERP_MAINTENANCE_ORDERS,
        ITSM_TICKET_RESULTS,
        ITSM_TICKET_CHANGES,
        MES_CONFIRMATIONS,
        MES_GOODS_MOVEMENTS,
        MES_PLANNING,
        MES_EXECUTION,
        MES_QUALITY,
        MES_MAINTENANCE,
        MES_TICKETS,
        AI_SUPPORT,
    )


class EventEnvelope(BaseModel):
    event_id: UUID = Field(default_factory=new_id)
    event_type: str
    event_version: int = 1
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: str
    # Partition key. Events for the same aggregate (e.g. "1000:1000042") keep their order.
    key: str
    correlation_id: str | None = None
    payload: dict[str, Any]


class PermanentError(Exception):
    """Raised by an event handler when retrying cannot help (bad data, unknown reference).

    The consumer moves the message to the dead-letter table instead of retrying.
    """
