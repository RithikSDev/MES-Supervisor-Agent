"""Declarative base + the conventions every business table follows.

Every Entity has: UUIDv7 `id`, `row_version` (optimistic locking, exposed to clients), created/updated
at/by. Business tables also get the audit trigger (see compliance/audit_sql.py). Hard deletes are rare and audited.
"""

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Date, DateTime, Integer, MetaData, Numeric, Text, Time, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

from mes_common.ids import new_id
from mes_core.shared.db.context import current_actor_id

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

Qty = Numeric(18, 6)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {
        str: Text,
        Decimal: Qty,
        datetime: DateTime(timezone=True),
        date: Date,
        time: Time,
        dict[str, Any]: JSONB,
    }


class Entity(Base):
    __abstract__ = True

    id: Mapped[UUID] = mapped_column(primary_key=True, default=new_id)
    row_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    created_by: Mapped[str] = mapped_column(nullable=False, default=current_actor_id)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now(), onupdate=func.now())
    updated_by: Mapped[str] = mapped_column(nullable=False, default=current_actor_id, onupdate=current_actor_id)

    @declared_attr.directive
    def __mapper_args__(cls) -> dict[str, Any]:
        # eager_defaults: fetch server-set columns (updated_at) via RETURNING, so entities stay usable after commit
        return {"version_id_col": cls.row_version, "eager_defaults": True}
