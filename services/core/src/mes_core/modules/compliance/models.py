from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    Table,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from mes_core.shared.db.base import Entity

SCHEMA = "compliance"

SIGNATURE_MEANINGS = ("AUTHORED", "REVIEWED", "APPROVED", "RELEASED")


class ESignature(Entity):
    """Electronic signature (ISO 9001 level): who, when, what it means, why. No cryptographic signing."""

    __tablename__ = "e_signature"
    __table_args__ = (
        CheckConstraint(f"meaning IN {SIGNATURE_MEANINGS}", name="meaning"),
        CheckConstraint("length(trim(reason)) > 0", name="reason_required"),
        Index("ix_e_signature_subject", "subject_type", "subject_id"),
        {"schema": SCHEMA},
    )

    plant_id: Mapped[UUID | None] = mapped_column(ForeignKey("org.plant.id"))
    signer_user_id: Mapped[UUID] = mapped_column(ForeignKey("identity.app_user.id"))
    signer_username: Mapped[str]
    signer_display_name: Mapped[str | None]
    meaning: Mapped[str]
    subject_type: Mapped[str]
    subject_id: Mapped[UUID]
    reason: Mapped[str]
    signed_at: Mapped[datetime]


# The audit log is created by hand-written DDL (partitioned, trigger-fed). This read-only mapping lives on its own
# MetaData so Alembic autogenerate never tries to manage it.
audit_metadata = MetaData()
audit_log = Table(
    "audit_log",
    audit_metadata,
    Column("id", BigInteger, primary_key=True),
    Column("occurred_at", DateTime(timezone=True), primary_key=True),
    Column("tx_id", BigInteger),
    Column("plant_id", Uuid),
    Column("actor_id", Text),
    Column("actor_name", Text),
    Column("actor_type", Text),
    Column("request_id", Text),
    Column("schema_name", Text),
    Column("table_name", Text),
    Column("record_id", Text),
    Column("action", Text),
    Column("changed_fields", ARRAY(Text)),
    Column("before", JSONB),
    Column("after", JSONB),
    Column("reason", Text),
    Column("signature_id", Uuid),
    Column("seal_seq", BigInteger),
    Column("chain_hash", Text),
    schema="audit",
)
