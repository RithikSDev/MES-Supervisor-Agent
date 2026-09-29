"""Equipment maintenance: downtime log, maintenance notifications and orders, preventive plans.

Notification and order numbers come from MES number ranges and are used as the SAP PM numbers (external
numbering), so the SAP mirror is idempotent by key and spare-part issues (261) can reference the order at once.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Sequence, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from mes_core.shared.db.base import Base, Entity

SCHEMA = "maintenance"

NOTIFICATION_SEQ = Sequence("notification_seq", schema=SCHEMA, metadata=Base.metadata, start=20000001)
ORDER_SEQ = Sequence("order_seq", schema=SCHEMA, metadata=Base.metadata, start=4000001)

ERP_SYNC = "erp_sync_status IN ('PENDING','POSTED','FAILED','NOT_REQUIRED')"


class DowntimeEvent(Entity):
    __tablename__ = "downtime_event"
    __table_args__ = (
        CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="period"),
        Index("ix_downtime_equipment_started", "equipment_id", "started_at"),
        # at most one open downtime per machine
        Index("uq_downtime_open", "equipment_id", unique=True, postgresql_where="ended_at IS NULL"),
        {"schema": SCHEMA},
    )

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"), index=True)
    line_id: Mapped[UUID] = mapped_column(ForeignKey("org.line.id"))
    equipment_id: Mapped[UUID] = mapped_column(ForeignKey("org.equipment.id"))
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]
    planned: Mapped[bool] = mapped_column(default=False)  # preventive maintenance vs. breakdown
    reason_code: Mapped[str]
    description: Mapped[str | None]
    reported_by: Mapped[str]
    reported_by_name: Mapped[str | None]
    ended_by_name: Mapped[str | None]
    run_id: Mapped[UUID | None]  # execution.operation_run that was interrupted (no FK: execution is upstream)
    notification_id: Mapped[UUID | None] = mapped_column(ForeignKey("maintenance.notification.id"))
    maintenance_order_id: Mapped[UUID | None] = mapped_column(ForeignKey("maintenance.maintenance_order.id"))
    client_event_id: Mapped[UUID | None] = mapped_column(unique=True)


class Notification(Entity):
    __tablename__ = "notification"
    __table_args__ = (
        UniqueConstraint("plant_id", "number"),
        CheckConstraint("notif_type IN ('BREAKDOWN','REQUEST')", name="notif_type"),
        CheckConstraint("priority BETWEEN 1 AND 4", name="priority"),
        CheckConstraint("status IN ('OPEN','IN_PROCESS','COMPLETED')", name="status"),
        CheckConstraint(ERP_SYNC, name="erp_sync_status"),
        Index("ix_notification_plant_status", "plant_id", "status"),
        {"schema": SCHEMA},
    )

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"))
    line_id: Mapped[UUID] = mapped_column(ForeignKey("org.line.id"))
    equipment_id: Mapped[UUID] = mapped_column(ForeignKey("org.equipment.id"), index=True)
    number: Mapped[str]
    notif_type: Mapped[str]
    priority: Mapped[int]  # SAP: 1 very high .. 4 low
    reason_code: Mapped[str]
    description: Mapped[str]
    status: Mapped[str] = mapped_column(default="OPEN")
    reported_at: Mapped[datetime]
    reported_by: Mapped[str]
    reported_by_name: Mapped[str | None]
    machine_down: Mapped[bool] = mapped_column(default=False)
    client_event_id: Mapped[UUID | None] = mapped_column(unique=True)
    completed_at: Mapped[datetime | None]
    erp_sync_status: Mapped[str] = mapped_column(default="PENDING")
    erp_error: Mapped[str | None]
    erp_synced_at: Mapped[datetime | None]


class MaintenancePlan(Entity):
    __tablename__ = "maintenance_plan"
    __table_args__ = (
        UniqueConstraint("plant_id", "equipment_id", "name"),
        CheckConstraint("interval_type IN ('CALENDAR','RUNTIME')", name="interval_type"),
        CheckConstraint(
            "(interval_type = 'CALENDAR' AND interval_days > 0) OR (interval_type = 'RUNTIME' AND interval_hours > 0)",
            name="interval",
        ),
        {"schema": SCHEMA},
    )

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"))
    equipment_id: Mapped[UUID] = mapped_column(ForeignKey("org.equipment.id"), index=True)
    name: Mapped[str]
    interval_type: Mapped[str]
    interval_days: Mapped[int | None]
    interval_hours: Mapped[Decimal | None]  # machine running hours (from operation runs)
    lead_days: Mapped[int] = mapped_column(default=3)  # create the order this long before it is due
    duration_min: Mapped[int]
    checklist: Mapped[list[Any]] = mapped_column(JSONB, default=list)  # list of task texts
    active: Mapped[bool] = mapped_column(default=True)
    last_done_at: Mapped[datetime | None]


class MaintenanceOrder(Entity):
    __tablename__ = "maintenance_order"
    __table_args__ = (
        UniqueConstraint("plant_id", "number"),
        CheckConstraint("order_type IN ('CORRECTIVE','PREVENTIVE')", name="order_type"),
        CheckConstraint("status IN ('CREATED','IN_PROGRESS','COMPLETED','CANCELLED')", name="status"),
        CheckConstraint("priority BETWEEN 1 AND 4", name="priority"),
        CheckConstraint(ERP_SYNC, name="erp_sync_status"),
        Index("ix_mo_plant_status", "plant_id", "status"),
        Index("ix_mo_equipment", "equipment_id"),
        # one open order per preventive plan
        Index("uq_mo_open_plan", "plan_id", unique=True,
              postgresql_where="plan_id IS NOT NULL AND status IN ('CREATED','IN_PROGRESS')"),
        {"schema": SCHEMA},
    )

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"))
    line_id: Mapped[UUID] = mapped_column(ForeignKey("org.line.id"))
    equipment_id: Mapped[UUID] = mapped_column(ForeignKey("org.equipment.id"))
    number: Mapped[str]
    order_type: Mapped[str]
    status: Mapped[str] = mapped_column(default="CREATED")
    priority: Mapped[int]
    description: Mapped[str]
    notification_id: Mapped[UUID | None] = mapped_column(ForeignKey("maintenance.notification.id"))
    plan_id: Mapped[UUID | None] = mapped_column(ForeignKey("maintenance.maintenance_plan.id"))
    planned_start: Mapped[datetime]
    planned_duration_min: Mapped[int]
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    technician_id: Mapped[str | None]
    technician_name: Mapped[str | None]
    labor_min: Mapped[Decimal] = mapped_column(default=Decimal(0))
    checklist: Mapped[list[Any]] = mapped_column(JSONB, default=list)  # [{text, done, note}]
    findings: Mapped[str | None]
    signature_id: Mapped[UUID | None] = mapped_column(ForeignKey("compliance.e_signature.id"))
    erp_sync_status: Mapped[str] = mapped_column(default="PENDING")
    erp_error: Mapped[str | None]
    erp_synced_at: Mapped[datetime | None]
    erp_system_status: Mapped[str | None]  # SAP side: CRTD / REL / TECO
