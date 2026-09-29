from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, Index, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from mes_core.shared.db.base import Entity

SCHEMA = "identity"


class AppUser(Entity):
    """Local mirror of an IdP identity. Created on first login (or pre-seeded by username)."""

    __tablename__ = "app_user"
    __table_args__ = {"schema": SCHEMA}

    idp_subject: Mapped[str | None] = mapped_column(unique=True)
    username: Mapped[str] = mapped_column(unique=True)
    display_name: Mapped[str | None]
    email: Mapped[str | None]
    active: Mapped[bool] = mapped_column(default=True)
    last_login_at: Mapped[datetime | None]


class UserScope(Entity):
    """Where a user may act. line_id NULL = the whole plant."""

    __tablename__ = "user_scope"
    __table_args__ = (
        UniqueConstraint("user_id", "plant_id", "line_id", postgresql_nulls_not_distinct=True),
        {"schema": SCHEMA},
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("identity.app_user.id"), index=True)
    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"))
    line_id: Mapped[UUID | None] = mapped_column(ForeignKey("org.line.id"))


class OperatorCredential(Entity):
    """Shop-floor PIN for a user (stands in for a badge/RFID). The PIN alone identifies the operator within a
    plant, so it is looked up via a keyed HMAC (unique) and verified against a salted scrypt hash."""

    __tablename__ = "operator_credential"
    __table_args__ = (UniqueConstraint("plant_id", "pin_lookup"), UniqueConstraint("user_id", "plant_id"), {"schema": SCHEMA})

    user_id: Mapped[UUID] = mapped_column(ForeignKey("identity.app_user.id"))
    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"))
    pin_lookup: Mapped[str]  # HMAC-SHA256(pepper, plant:pin); redacted in the audit log
    pin_hash: Mapped[str]  # scrypt; redacted in the audit log
    pin_set_at: Mapped[datetime]
    # rights of this person's PIN sessions: always operator, optionally maintenance technician
    terminal_roles: Mapped[list[str]] = mapped_column(ARRAY(Text), default=lambda: ["operator"],
                                                      server_default="{operator}")


class DeviceEnrollment(Entity):
    """Binds a physical device to a terminal. The device keeps a random token; we store only its SHA-256."""

    __tablename__ = "device_enrollment"
    __table_args__ = (
        Index("uq_device_enrollment_active", "terminal_id", unique=True, postgresql_where=text("revoked_at IS NULL")),
        {"schema": SCHEMA},
    )

    terminal_id: Mapped[UUID] = mapped_column(ForeignKey("org.terminal.id"))
    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"))
    token_hash: Mapped[str] = mapped_column(unique=True)  # redacted in the audit log
    enrolled_by: Mapped[str]
    enrolled_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    revoked_by: Mapped[str | None]


class TerminalSession(Entity):
    """An operator's PIN login on a terminal. The session token (JWT) carries this id; ending the row revokes it."""

    __tablename__ = "terminal_session"
    __table_args__ = (
        Index("uq_terminal_session_open", "terminal_id", unique=True, postgresql_where=text("ended_at IS NULL")),
        {"schema": SCHEMA},
    )

    terminal_id: Mapped[UUID] = mapped_column(ForeignKey("org.terminal.id"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("identity.app_user.id"), index=True)
    plant_id: Mapped[UUID] = mapped_column(ForeignKey("org.plant.id"))
    started_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]
    end_reason: Mapped[str | None]  # LOGOUT | REPLACED | REVOKED | ENDED_BY_SUPPORT
    roles: Mapped[list[str]] = mapped_column(ARRAY(Text), default=lambda: ["operator"], server_default="{operator}")
