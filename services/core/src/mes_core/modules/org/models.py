"""ISA-95 site model: plant > area > line > work center > equipment, plus shift patterns."""

from datetime import datetime, time
from uuid import UUID

from sqlalchemy import ARRAY, CheckConstraint, ForeignKey, SmallInteger, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from mes_core.shared.db.base import Entity

SCHEMA = "org"


class Plant(Entity):
    __tablename__ = "plant"
    __table_args__ = {"schema": SCHEMA}

    code: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str]
    timezone: Mapped[str]
    erp_plant_code: Mapped[str | None] = mapped_column(unique=True)


class Area(Entity):
    __tablename__ = "area"
    __table_args__ = (UniqueConstraint("plant_id", "code"), {"schema": SCHEMA})

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"), index=True)
    code: Mapped[str]
    name: Mapped[str]


class Line(Entity):
    __tablename__ = "line"
    __table_args__ = (UniqueConstraint("plant_id", "code"), {"schema": SCHEMA})

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"), index=True)
    area_id: Mapped[UUID] = mapped_column(ForeignKey("org.area.id"))
    code: Mapped[str]
    name: Mapped[str]


class WorkCenter(Entity):
    __tablename__ = "work_center"
    __table_args__ = (
        UniqueConstraint("plant_id", "code"),
        UniqueConstraint("plant_id", "erp_work_center"),
        {"schema": SCHEMA},
    )

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"), index=True)
    line_id: Mapped[UUID] = mapped_column(ForeignKey("org.line.id"), index=True)
    code: Mapped[str]
    name: Mapped[str]
    erp_work_center: Mapped[str | None]


class Equipment(Entity):
    __tablename__ = "equipment"
    __table_args__ = (
        UniqueConstraint("plant_id", "code"),
        CheckConstraint("status IN ('AVAILABLE','DOWN','MAINTENANCE','DECOMMISSIONED')", name="status"),
        {"schema": SCHEMA},
    )

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"), index=True)
    line_id: Mapped[UUID] = mapped_column(ForeignKey("org.line.id"), index=True)
    work_center_id: Mapped[UUID] = mapped_column(ForeignKey("org.work_center.id"), index=True)
    code: Mapped[str]
    name: Mapped[str]
    equipment_type: Mapped[str]
    status: Mapped[str] = mapped_column(default="AVAILABLE")


class ShiftPattern(Entity):
    """Recurring shift, e.g. Early 06:00-14:00 Mon-Fri. line_id NULL = applies to every line of the plant."""

    __tablename__ = "shift_pattern"
    __table_args__ = (UniqueConstraint("plant_id", "line_id", "code", postgresql_nulls_not_distinct=True), {"schema": SCHEMA})

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"), index=True)
    line_id: Mapped[UUID | None] = mapped_column(ForeignKey("org.line.id"))
    code: Mapped[str]
    name: Mapped[str]
    start_time: Mapped[time]
    end_time: Mapped[time]
    days_of_week: Mapped[list[int]] = mapped_column(ARRAY(SmallInteger))


class Terminal(Entity):
    """A shop-floor terminal (kiosk/tablet) installed at a line. Operators log in on it with a PIN."""

    __tablename__ = "terminal"
    __table_args__ = (UniqueConstraint("plant_id", "code"), {"schema": SCHEMA})

    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"), index=True)
    line_id: Mapped[UUID] = mapped_column(ForeignKey("org.line.id"))
    code: Mapped[str]
    name: Mapped[str]
    active: Mapped[bool] = mapped_column(default=True)
    # brute-force protection for PIN entry on this device
    pin_failures: Mapped[int] = mapped_column(default=0)
    pin_locked_until: Mapped[datetime | None]
