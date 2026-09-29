"""Material management: storage locations, lots, lot-level stock and the goods movement ledger.

`stock` is a balance table maintained transactionally from `movement` (the ledger is the history; stock is
the current state). Quantities can never go negative (CHECK constraint, plus a friendly check in code).
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Sequence, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from mes_core.shared.db.base import Base, Entity

SCHEMA = "inventory"

LOT_NUMBER_SEQ = Sequence("lot_number_seq", schema=SCHEMA, metadata=Base.metadata)
SERIAL_SEQ = Sequence("serial_seq", schema=SCHEMA, metadata=Base.metadata)

LOCATION_KINDS = ("STORE", "LINESIDE", "SEMI", "FG")
# SAP stock types: only UNRESTRICTED stock can be issued, backflushed or shipped.
STOCK_STATUSES = ("UNRESTRICTED", "QUALITY", "BLOCKED")
MOVEMENT_TYPES = (
    "561", "501", "311", "261", "262", "101", "102",
    # stock type changes (Phase 3): 321 QI->unrestricted, 350 QI->blocked, 344 unrestricted->blocked,
    # 343 blocked->unrestricted; 555 scrap from blocked stock
    "321", "350", "344", "343", "555", "122",
)
SERIAL_STATUSES = ("IN_QUALITY", "AVAILABLE", "BLOCKED", "SCRAPPED")


class StorageLocation(Entity):
    __tablename__ = "storage_location"
    __table_args__ = (
        UniqueConstraint("plant_id", "code"),
        CheckConstraint(f"kind IN {LOCATION_KINDS}", name="kind"),
        {"schema": SCHEMA},
    )

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"), index=True)
    line_id: Mapped[UUID | None] = mapped_column(ForeignKey("org.line.id"))  # set for line-side supermarkets
    code: Mapped[str]  # = SAP storage location
    name: Mapped[str]
    kind: Mapped[str]


class Lot(Entity):
    """A batch of one material. Produced lots remember their work order (genealogy builds on this in Phase 3)."""

    __tablename__ = "lot"
    __table_args__ = (
        UniqueConstraint("plant_id", "lot_number"),
        CheckConstraint("origin IN ('OPENING','RECEIPT','PRODUCTION')", name="origin"),
        {"schema": SCHEMA},
    )

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"))
    material_id: Mapped[UUID] = mapped_column(ForeignKey("masterdata.material.id"), index=True)
    lot_number: Mapped[str]  # = SAP batch (max 10 chars)
    origin: Mapped[str]
    uom: Mapped[str]
    work_order_id: Mapped[UUID | None] = mapped_column(ForeignKey("planning.work_order.id"))


class Stock(Entity):
    __tablename__ = "stock"
    __table_args__ = (
        UniqueConstraint("location_id", "lot_id", "status"),
        CheckConstraint("quantity >= 0", name="non_negative"),
        CheckConstraint(f"status IN {STOCK_STATUSES}", name="status"),
        Index("ix_stock_plant_material", "plant_id", "material_id"),
        {"schema": SCHEMA},
    )

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"))
    location_id: Mapped[UUID] = mapped_column(ForeignKey("inventory.storage_location.id"))
    material_id: Mapped[UUID] = mapped_column(ForeignKey("masterdata.material.id"))
    lot_id: Mapped[UUID] = mapped_column(ForeignKey("inventory.lot.id"))
    status: Mapped[str] = mapped_column(default="UNRESTRICTED", server_default="UNRESTRICTED")
    quantity: Mapped[Decimal]
    uom: Mapped[str]


class Movement(Entity):
    """Goods movement ledger (append-only). Types follow SAP: 561 opening balance, 501 receipt w/o order,
    311 transfer, 261 issue to order, 101 goods receipt from order (262/102 reversals reserved)."""

    __tablename__ = "movement"
    __table_args__ = (
        CheckConstraint(f"movement_type IN {MOVEMENT_TYPES}", name="movement_type"),
        CheckConstraint("quantity > 0", name="positive"),
        CheckConstraint("erp_sync_status IN ('PENDING','POSTED','FAILED','NOT_REQUIRED')", name="erp_sync_status"),
        Index("ix_movement_plant_posted", "plant_id", "posted_at"),
        Index("ix_movement_work_order", "work_order_id"),
        Index("ix_movement_maintenance_order", "maintenance_order_id"),
        Index("ix_movement_component", "component_id"),
        {"schema": SCHEMA},
    )

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"))
    movement_type: Mapped[str]
    material_id: Mapped[UUID] = mapped_column(ForeignKey("masterdata.material.id"))
    lot_id: Mapped[UUID] = mapped_column(ForeignKey("inventory.lot.id"))
    quantity: Mapped[Decimal]
    uom: Mapped[str]
    from_location_id: Mapped[UUID | None] = mapped_column(ForeignKey("inventory.storage_location.id"))
    to_location_id: Mapped[UUID | None] = mapped_column(ForeignKey("inventory.storage_location.id"))
    from_status: Mapped[str | None]
    to_status: Mapped[str | None]
    work_order_id: Mapped[UUID | None] = mapped_column(ForeignKey("planning.work_order.id"))
    # spare parts issued to a maintenance order (no FK: maintenance depends on inventory, not the reverse)
    maintenance_order_id: Mapped[UUID | None]
    operation_id: Mapped[UUID | None] = mapped_column(ForeignKey("planning.work_order_operation.id"))
    component_id: Mapped[UUID | None] = mapped_column(ForeignKey("planning.work_order_component.id"))
    confirmation_id: Mapped[UUID | None]  # execution.production_confirmation (no FK: execution depends on us)
    backflush: Mapped[bool] = mapped_column(default=False)
    reason: Mapped[str | None]
    posted_at: Mapped[datetime]
    posted_by: Mapped[str]
    posted_by_name: Mapped[str | None]
    client_event_id: Mapped[UUID | None] = mapped_column(unique=True)  # offline-terminal idempotency key
    erp_sync_status: Mapped[str] = mapped_column(default="PENDING")
    erp_material_document: Mapped[str | None]
    erp_document_year: Mapped[str | None]
    erp_error: Mapped[str | None]
    erp_reconciled: Mapped[bool] = mapped_column(default=False)
    erp_synced_at: Mapped[datetime | None]


class SerialNumber(Entity):
    """One serialised unit. Assigned at goods receipt of a serialised material; belongs to its production lot,
    so everything traced for the lot (operations, machines, operators, component lots) applies to the serial."""

    __tablename__ = "serial_number"
    __table_args__ = (
        UniqueConstraint("material_id", "serial"),
        CheckConstraint(f"status IN {SERIAL_STATUSES}", name="status"),
        Index("ix_serial_lot", "lot_id"),
        {"schema": SCHEMA},
    )

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"))
    material_id: Mapped[UUID] = mapped_column(ForeignKey("masterdata.material.id"))
    serial: Mapped[str]  # = SAP serial number (max 18 chars)
    lot_id: Mapped[UUID] = mapped_column(ForeignKey("inventory.lot.id"))
    work_order_id: Mapped[UUID | None] = mapped_column(ForeignKey("planning.work_order.id"), index=True)
    confirmation_id: Mapped[UUID | None]
    status: Mapped[str]
