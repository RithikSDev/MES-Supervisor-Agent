from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from mes_core.shared.db.base import Entity

SCHEMA = "planning"

_WO_STATUSES = "('IMPORTED','RELEASED','SCHEDULED','DISPATCHED','IN_PROGRESS','COMPLETED','CLOSED','ON_HOLD','CANCELLED')"


class WorkOrder(Entity):
    __tablename__ = "work_order"
    __table_args__ = (
        UniqueConstraint("plant_id", "erp_order_number"),
        CheckConstraint(f"status IN {_WO_STATUSES}", name="status"),
        CheckConstraint("priority BETWEEN 1 AND 5", name="priority"),
        CheckConstraint("planned_qty > 0", name="planned_qty_positive"),
        Index("ix_work_order_plant_status", "plant_id", "status"),
        {"schema": SCHEMA},
    )

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"))
    line_id: Mapped[UUID | None] = mapped_column(ForeignKey("org.line.id"))
    erp_order_number: Mapped[str]
    order_type: Mapped[str]
    material_id: Mapped[UUID] = mapped_column(ForeignKey("masterdata.material.id"))
    planned_qty: Mapped[Decimal]
    uom: Mapped[str]
    # running totals, maintained from confirmations: yield of the last operation, scrap of all
    yield_qty: Mapped[Decimal] = mapped_column(default=Decimal(0))
    scrap_qty: Mapped[Decimal] = mapped_column(default=Decimal(0))
    erp_basic_start: Mapped[date]  # ERP-owned
    erp_basic_end: Mapped[date]  # ERP-owned: the due date
    sched_start: Mapped[datetime | None]  # MES-owned, from the published schedule
    sched_end: Mapped[datetime | None]
    status: Mapped[str]
    hold_prev_status: Mapped[str | None]
    erp_status: Mapped[str] = mapped_column(default="")
    priority: Mapped[int] = mapped_column(default=3)  # 1 = most urgent
    source: Mapped[str] = mapped_column(default="ERP")
    erp_last_changed_at: Mapped[datetime | None]


class WorkOrderOperation(Entity):
    __tablename__ = "work_order_operation"
    __table_args__ = (
        UniqueConstraint("work_order_id", "op_no"),
        CheckConstraint("status IN ('PENDING','IN_PROGRESS','CONFIRMED')", name="status"),
        {"schema": SCHEMA},
    )

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"))
    work_order_id: Mapped[UUID] = mapped_column(ForeignKey("planning.work_order.id"), index=True)
    op_no: Mapped[str]
    sequence: Mapped[int]
    description: Mapped[str]
    work_center_id: Mapped[UUID] = mapped_column(ForeignKey("org.work_center.id"))
    control_key: Mapped[str | None]
    planned_qty: Mapped[Decimal]
    uom: Mapped[str]
    setup_min: Mapped[Decimal]
    run_min_per_unit: Mapped[Decimal]
    labor_min_per_unit: Mapped[Decimal]
    status: Mapped[str] = mapped_column(default="PENDING")
    yield_qty: Mapped[Decimal] = mapped_column(default=Decimal(0))
    scrap_qty: Mapped[Decimal] = mapped_column(default=Decimal(0))
    erp_routing_no: Mapped[str | None]
    erp_routing_counter: Mapped[str | None]


class WorkOrderComponent(Entity):
    __tablename__ = "work_order_component"
    __table_args__ = (
        UniqueConstraint("work_order_id", "erp_reservation_no", "erp_reservation_item"),
        {"schema": SCHEMA},
    )

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"))
    work_order_id: Mapped[UUID] = mapped_column(ForeignKey("planning.work_order.id"), index=True)
    operation_id: Mapped[UUID | None] = mapped_column(ForeignKey("planning.work_order_operation.id"))
    material_id: Mapped[UUID] = mapped_column(ForeignKey("masterdata.material.id"))
    required_qty: Mapped[Decimal]
    uom: Mapped[str]
    erp_reservation_no: Mapped[str | None]
    erp_reservation_item: Mapped[str | None]
    storage_location: Mapped[str | None]
    backflush: Mapped[bool] = mapped_column(default=False)


class WorkOrderStatusHistory(Entity):
    __tablename__ = "work_order_status_history"
    __table_args__ = {"schema": SCHEMA}

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"))
    work_order_id: Mapped[UUID] = mapped_column(ForeignKey("planning.work_order.id"), index=True)
    from_status: Mapped[str | None]
    to_status: Mapped[str]
    reason: Mapped[str | None]
    changed_at: Mapped[datetime]
    changed_by: Mapped[str]
    changed_by_name: Mapped[str | None]


class Schedule(Entity):
    __tablename__ = "schedule"
    __table_args__ = (
        CheckConstraint("status IN ('DRAFT','PUBLISHED','SUPERSEDED','DISCARDED')", name="status"),
        CheckConstraint("horizon_end > horizon_start", name="horizon"),
        # One published and one draft schedule per plant (+ line) at a time.
        Index(
            "uq_schedule_one_published",
            "plant_id",
            text("coalesce(line_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            unique=True,
            postgresql_where=text("status = 'PUBLISHED'"),
        ),
        Index(
            "uq_schedule_one_draft",
            "plant_id",
            text("coalesce(line_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            unique=True,
            postgresql_where=text("status = 'DRAFT'"),
        ),
        {"schema": SCHEMA},
    )

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"))
    line_id: Mapped[UUID | None] = mapped_column(ForeignKey("org.line.id"))
    name: Mapped[str]
    version: Mapped[int]
    horizon_start: Mapped[datetime]
    horizon_end: Mapped[datetime]
    status: Mapped[str] = mapped_column(default="DRAFT")
    based_on_id: Mapped[UUID | None] = mapped_column(ForeignKey("planning.schedule.id"))
    published_at: Mapped[datetime | None]
    published_by: Mapped[str | None]
    signature_id: Mapped[UUID | None] = mapped_column(ForeignKey("compliance.e_signature.id"))


class ScheduleEntry(Entity):
    """One operation on one machine. Overlaps on a machine within a schedule are rejected by an exclusion
    constraint (created in the migration: EXCLUDE USING gist (schedule_id =, equipment_id =, tstzrange &&))."""

    __tablename__ = "schedule_entry"
    __table_args__ = (
        UniqueConstraint("schedule_id", "operation_id"),
        CheckConstraint("planned_end > planned_start", name="interval"),
        {"schema": SCHEMA},
    )

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"))
    schedule_id: Mapped[UUID] = mapped_column(ForeignKey("planning.schedule.id", ondelete="CASCADE"), index=True)
    work_order_id: Mapped[UUID] = mapped_column(ForeignKey("planning.work_order.id"), index=True)
    operation_id: Mapped[UUID] = mapped_column(ForeignKey("planning.work_order_operation.id"))
    equipment_id: Mapped[UUID] = mapped_column(ForeignKey("org.equipment.id"))
    planned_start: Mapped[datetime]
    planned_end: Mapped[datetime]
    locked: Mapped[bool] = mapped_column(default=False)
