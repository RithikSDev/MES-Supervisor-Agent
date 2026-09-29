from sqlalchemy import CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column

from mes_core.shared.db.base import Entity

SCHEMA = "masterdata"


class Material(Entity):
    """Mirrored from SAP (SAP is master). Client-level, so no plant_id."""

    __tablename__ = "material"
    __table_args__ = (
        CheckConstraint("material_type IN ('ROH','HALB','FERT','HIBE','VERP','ERSA')", name="material_type"),
        {"schema": SCHEMA},
    )

    material_number: Mapped[str] = mapped_column(unique=True)
    description: Mapped[str]
    base_uom: Mapped[str]
    material_type: Mapped[str]
    serial_profile: Mapped[str | None]
    is_serialized: Mapped[bool] = mapped_column(default=False)
