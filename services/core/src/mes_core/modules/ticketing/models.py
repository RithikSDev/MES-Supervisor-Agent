"""Support tickets (MES application / system problems) with P1-P4 severity, L1-L3 support tiers and SLA clocks.

MES is where tickets are raised and their shop-floor context lives; support engineers work them in ServiceNow
(L1/L2 assignment groups) and Jira (L3). The itsm-adapter keeps both in sync (REST, or MCP).
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Sequence, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from mes_core.shared.db.base import Base, Entity

SCHEMA = "ticketing"

TICKET_SEQ = Sequence("ticket_seq", schema=SCHEMA, metadata=Base.metadata)

CATEGORIES = ("MES_APP", "HARDWARE", "INTEGRATION", "DATA", "ACCESS", "CHANGE_REQUEST", "OTHER")
IMPACTS = ("LINE_STOP", "DEGRADED", "MINOR", "SERVICE_REQUEST")
STATUSES = ("NEW", "ACKNOWLEDGED", "IN_PROGRESS", "ON_HOLD", "RESOLVED", "CLOSED", "CANCELLED")
OPEN_STATUSES = ("NEW", "ACKNOWLEDGED", "IN_PROGRESS", "ON_HOLD")
# full-text document of a ticket (expression-indexed)
SEARCH_DOCUMENT = ("to_tsvector('english', coalesce(title, '') || ' ' || coalesce(description, '') || ' ' || "
                   "coalesce(resolution_note, ''))")


class Ticket(Entity):
    __tablename__ = "ticket"
    __table_args__ = (
        UniqueConstraint("plant_id", "number"),
        CheckConstraint(f"category IN {CATEGORIES}", name="category"),
        CheckConstraint(f"impact IN {IMPACTS}", name="impact"),
        CheckConstraint("severity IN ('P1','P2','P3','P4')", name="severity"),
        CheckConstraint("tier IN ('L1','L2','L3')", name="tier"),
        CheckConstraint(f"status IN {STATUSES}", name="status"),
        CheckConstraint("source IN ('TERMINAL','ADMIN','AUTOMATIC','MCP')", name="source"),
        CheckConstraint("itsm_sync_status IN ('PENDING','SYNCED','FAILED')", name="itsm_sync_status"),
        Index("ix_ticket_plant_status", "plant_id", "status"),
        # similar-ticket search (support agent, admin UI); queries must use the same expression
        Index("ix_ticket_search", text(SEARCH_DOCUMENT), postgresql_using="gin"),
        # one open automatic ticket per failing integration message
        Index("uq_ticket_open_integration_ref", "integration_ref", unique=True,
              postgresql_where="integration_ref IS NOT NULL AND status IN ('NEW','ACKNOWLEDGED','IN_PROGRESS','ON_HOLD')"),
        {"schema": SCHEMA},
    )

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"))
    line_id: Mapped[UUID | None] = mapped_column(ForeignKey("org.line.id"))
    number: Mapped[str]
    title: Mapped[str]
    description: Mapped[str]
    category: Mapped[str]
    impact: Mapped[str]
    severity: Mapped[str]
    suggested_severity: Mapped[str]
    tier: Mapped[str] = mapped_column(default="L1")
    status: Mapped[str] = mapped_column(default="NEW")
    source: Mapped[str]
    reported_at: Mapped[datetime]
    reported_by: Mapped[str]
    reported_by_name: Mapped[str | None]
    client_event_id: Mapped[UUID | None] = mapped_column(unique=True)
    # shop-floor context
    work_order_id: Mapped[UUID | None] = mapped_column(ForeignKey("planning.work_order.id"))
    equipment_id: Mapped[UUID | None] = mapped_column(ForeignKey("org.equipment.id"))
    terminal_id: Mapped[UUID | None] = mapped_column(ForeignKey("org.terminal.id"))
    nonconformance_id: Mapped[UUID | None] = mapped_column(ForeignKey("quality.nonconformance.id"))
    maintenance_order_id: Mapped[UUID | None] = mapped_column(ForeignKey("maintenance.maintenance_order.id"))
    integration_ref: Mapped[str | None]
    # SLA
    response_due_at: Mapped[datetime]
    resolution_due_at: Mapped[datetime | None]  # None: scheduled (P4)
    acknowledged_at: Mapped[datetime | None]
    resolved_at: Mapped[datetime | None]
    closed_at: Mapped[datetime | None]
    on_hold_since: Mapped[datetime | None]
    held_s: Mapped[int] = mapped_column(default=0)  # time on hold, excluded from the resolution clock
    response_breached: Mapped[bool] = mapped_column(default=False)
    response_warned: Mapped[bool] = mapped_column(default=False)  # 75% of the response target used
    resolution_warned: Mapped[bool] = mapped_column(default=False)
    resolution_breached: Mapped[bool] = mapped_column(default=False)
    resolution_note: Mapped[str | None]
    # external systems
    itsm_sync_status: Mapped[str] = mapped_column(default="PENDING")
    itsm_error: Mapped[str | None]
    sn_number: Mapped[str | None]
    sn_sys_id: Mapped[str | None]
    sn_state: Mapped[str | None]
    sn_group: Mapped[str | None]
    jira_key: Mapped[str | None]
    jira_status: Mapped[str | None]
    sn_url: Mapped[str | None]  # links to the records, as returned by the ITSM adapter
    jira_url: Mapped[str | None]
    # the support engineer working the ticket (ServiceNow "Assigned to"); the name also covers ServiceNow users who
    # are not in the MES support team (assignee_id then NULL)
    assignee_id: Mapped[UUID | None] = mapped_column(ForeignKey("ticketing.support_member.id"), index=True)
    assignee_name: Mapped[str | None]
    assigned_at: Mapped[datetime | None]


class SupportMember(Entity):
    """A support engineer tickets can be assigned to. Matched to a ServiceNow user by e-mail, then user name."""

    __tablename__ = "support_member"
    __table_args__ = (
        UniqueConstraint("plant_id", "username"),
        CheckConstraint("tier IS NULL OR tier IN ('L1','L2','L3')", name="tier"),
        {"schema": SCHEMA},
    )

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"))
    username: Mapped[str]  # MES / Keycloak user name when they sign in to MES
    display_name: Mapped[str]
    email: Mapped[str | None]
    tier: Mapped[str | None]  # the tier they usually work (None: any)
    active: Mapped[bool] = mapped_column(default=True)


class TicketActivity(Entity):
    __tablename__ = "ticket_activity"
    __table_args__ = (
        CheckConstraint("origin IN ('MES','SERVICENOW','JIRA','SYSTEM','AI')", name="origin"),
        Index("ix_ticket_activity_ticket", "ticket_id", "at"),
        {"schema": SCHEMA},
    )

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"))
    ticket_id: Mapped[UUID] = mapped_column(ForeignKey("ticketing.ticket.id"))
    at: Mapped[datetime]
    kind: Mapped[str]  # CREATED STATUS COMMENT ESCALATED SEVERITY ASSIGNED DETAILS SLA_AT_RISK SLA_BREACH SYNC EXTERNAL
    origin: Mapped[str] = mapped_column(default="MES")
    actor_name: Mapped[str | None]
    text: Mapped[str | None]
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # dedup of changes that come back from ServiceNow / Jira (their record id + change timestamp / comment id)
    external_id: Mapped[str | None] = mapped_column(unique=True)
