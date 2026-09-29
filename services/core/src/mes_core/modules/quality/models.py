"""Quality management: inspection plans and lots, results, nonconformances.

Inspection types (SAP QM equivalents in brackets):
  INCOMING   on goods receipt without order (01): stock is received into quality inspection
  IN_PROCESS first-off check of an operation (03): gates quantity reporting on the terminal
  FINAL      on goods receipt from an order (04): produced lot is received into quality inspection
A usage decision (accept / reject, e-signed) releases or blocks the inspected stock.
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Sequence, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from mes_core.shared.db.base import Base, Entity

SCHEMA = "quality"

INSPECTION_LOT_SEQ = Sequence("inspection_lot_seq", schema=SCHEMA, metadata=Base.metadata)
NC_SEQ = Sequence("nc_seq", schema=SCHEMA, metadata=Base.metadata)

INSPECTION_TYPES = ("INCOMING", "IN_PROCESS", "FINAL")


class InspectionPlan(Entity):
    __tablename__ = "inspection_plan"
    __table_args__ = (
        UniqueConstraint("plant_id", "material_id", "inspection_type", "op_no", postgresql_nulls_not_distinct=True),
        CheckConstraint(f"inspection_type IN {INSPECTION_TYPES}", name="inspection_type"),
        CheckConstraint("sample_size > 0", name="sample_size"),
        {"schema": SCHEMA},
    )

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"))
    material_id: Mapped[UUID] = mapped_column(ForeignKey("masterdata.material.id"))
    inspection_type: Mapped[str]
    op_no: Mapped[str | None]  # IN_PROCESS only
    name: Mapped[str]
    sample_size: Mapped[int]
    blocking: Mapped[bool] = mapped_column(default=True)  # IN_PROCESS: quantities can't be reported until passed
    active: Mapped[bool] = mapped_column(default=True)


class InspectionCharacteristic(Entity):
    __tablename__ = "inspection_characteristic"
    __table_args__ = (
        UniqueConstraint("plan_id", "seq"),
        CheckConstraint("kind IN ('QUANTITATIVE','QUALITATIVE')", name="kind"),
        {"schema": SCHEMA},
    )

    plan_id: Mapped[UUID] = mapped_column(ForeignKey("quality.inspection_plan.id"), index=True)
    seq: Mapped[int]
    name: Mapped[str]
    kind: Mapped[str]
    target: Mapped[Decimal | None]
    lower_limit: Mapped[Decimal | None]
    upper_limit: Mapped[Decimal | None]
    uom: Mapped[str | None]


class InspectionLot(Entity):
    __tablename__ = "inspection_lot"
    __table_args__ = (
        UniqueConstraint("plant_id", "number"),
        CheckConstraint("status IN ('OPEN','PASSED','FAILED','CANCELLED')", name="status"),
        CheckConstraint("usage_decision IN ('ACCEPTED','REJECTED')", name="usage_decision"),
        Index("ix_inspection_lot_plant_status", "plant_id", "status"),
        Index("ix_inspection_lot_operation", "operation_id"),
        Index("ix_inspection_lot_lot", "lot_id"),
        {"schema": SCHEMA},
    )

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"))
    number: Mapped[str]
    plan_id: Mapped[UUID] = mapped_column(ForeignKey("quality.inspection_plan.id"))
    inspection_type: Mapped[str]
    status: Mapped[str] = mapped_column(default="OPEN")
    material_id: Mapped[UUID] = mapped_column(ForeignKey("masterdata.material.id"))
    work_order_id: Mapped[UUID | None] = mapped_column(ForeignKey("planning.work_order.id"), index=True)
    operation_id: Mapped[UUID | None] = mapped_column(ForeignKey("planning.work_order_operation.id"))
    run_id: Mapped[UUID | None]  # execution.operation_run (no FK: execution depends on quality)
    lot_id: Mapped[UUID | None] = mapped_column(ForeignKey("inventory.lot.id"))
    quantity: Mapped[Decimal | None]
    sample_size: Mapped[int]
    evaluated_at: Mapped[datetime | None]
    usage_decision: Mapped[str | None]
    decision_reason: Mapped[str | None]
    decided_at: Mapped[datetime | None]
    decided_by: Mapped[str | None]
    signature_id: Mapped[UUID | None] = mapped_column(ForeignKey("compliance.e_signature.id"))


class InspectionResult(Entity):
    __tablename__ = "inspection_result"
    __table_args__ = (
        UniqueConstraint("inspection_lot_id", "characteristic_id", "sample_no"),
        {"schema": SCHEMA},
    )

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"))
    inspection_lot_id: Mapped[UUID] = mapped_column(ForeignKey("quality.inspection_lot.id"), index=True)
    characteristic_id: Mapped[UUID] = mapped_column(ForeignKey("quality.inspection_characteristic.id"))
    sample_no: Mapped[int]
    measured_value: Mapped[Decimal | None]
    attribute_ok: Mapped[bool | None]
    conforming: Mapped[bool]
    note: Mapped[str | None]
    recorded_at: Mapped[datetime]
    recorded_by: Mapped[str]
    recorded_by_name: Mapped[str | None]


class Nonconformance(Entity):
    __tablename__ = "nonconformance"
    __table_args__ = (
        UniqueConstraint("plant_id", "number"),
        CheckConstraint("status IN ('OPEN','REWORK','CLOSED')", name="status"),
        CheckConstraint("severity IN ('MINOR','MAJOR','CRITICAL')", name="severity"),
        CheckConstraint("source IN ('INSPECTION','OPERATOR','RECEIVING','TRACEABILITY','MANUAL')", name="source"),
        CheckConstraint("disposition IN ('USE_AS_IS','REWORK','SCRAP','RETURN_TO_SUPPLIER')", name="disposition"),
        Index("ix_nc_plant_status", "plant_id", "status"),
        {"schema": SCHEMA},
    )

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"))
    number: Mapped[str]
    status: Mapped[str] = mapped_column(default="OPEN")
    source: Mapped[str]
    severity: Mapped[str]
    defect_code: Mapped[str]
    description: Mapped[str]
    material_id: Mapped[UUID | None] = mapped_column(ForeignKey("masterdata.material.id"))
    work_order_id: Mapped[UUID | None] = mapped_column(ForeignKey("planning.work_order.id"), index=True)
    operation_id: Mapped[UUID | None] = mapped_column(ForeignKey("planning.work_order_operation.id"))
    lot_id: Mapped[UUID | None] = mapped_column(ForeignKey("inventory.lot.id"), index=True)
    inspection_lot_id: Mapped[UUID | None] = mapped_column(ForeignKey("quality.inspection_lot.id"))
    quantity: Mapped[Decimal | None]
    uom: Mapped[str | None]
    blocked_qty: Mapped[Decimal] = mapped_column(default=Decimal(0))  # stock moved to BLOCKED for this NC
    reported_at: Mapped[datetime]
    reported_by: Mapped[str]
    reported_by_name: Mapped[str | None]
    client_event_id: Mapped[UUID | None] = mapped_column(unique=True)
    disposition: Mapped[str | None]
    disposition_reason: Mapped[str | None]
    dispositioned_at: Mapped[datetime | None]
    dispositioned_by: Mapped[str | None]
    signature_id: Mapped[UUID | None] = mapped_column(ForeignKey("compliance.e_signature.id"))
    rework_good_qty: Mapped[Decimal | None]
    rework_scrap_qty: Mapped[Decimal | None]
    closed_at: Mapped[datetime | None]
