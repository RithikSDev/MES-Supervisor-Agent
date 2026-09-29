"""The notifier's tables (schema `notify` in the integration database): who is on call, which alerts are on, every
alert with its escalation level and acknowledgement, and every delivery attempt."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

from mes_common.messaging import Messaging

SCHEMA = "notify"

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)
messaging = Messaging(metadata, source="notifier", schema="notify_messaging")

# on-call rota: per role (L1, L2, L3, SUPERVISOR) a primary (position 1) and backups (2, 3, ...)
rota = Table(
    "rota",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("role", Text, nullable=False),
    Column("position", Integer, nullable=False),
    Column("name", Text, nullable=False),
    Column("email", Text, nullable=False),
    Column("teams", Boolean, nullable=False, server_default="true"),  # also post to the Teams channel
    Column("active", Boolean, nullable=False, server_default="true"),
    schema=SCHEMA,
)

# which alerts are on, on which channels, and how long before an unacknowledged alert moves up the chain
rule = Table(
    "rule",
    metadata,
    Column("key", Text, primary_key=True),
    Column("enabled", Boolean, nullable=False),
    Column("channels", JSONB, nullable=False),  # ["email", "teams"] (in-app is always on)
    Column("ack_minutes", Integer, nullable=False),
    Column("updated_by", Text),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    schema=SCHEMA,
)

notification = Table(
    "notification",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("seq", BigInteger, Identity(), nullable=False, unique=True),
    Column("rule", Text, nullable=False),
    Column("dedup_key", Text, nullable=False, unique=True),  # one alert per cause (event redelivery, repeats)
    Column("severity", Text),  # P1..P4, or null for agent alerts
    Column("ticket_id", Uuid, index=True),
    Column("ticket_number", Text),
    Column("title", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("roles", JSONB, nullable=False),  # rota roles to alert, e.g. ["L1", "SUPERVISOR"]
    Column("status", Text, nullable=False),  # NEW -> SENT -> ACKED | CLOSED (the cause went away)
    Column("level", Integer, nullable=False, server_default="0"),  # 0 primary, 1 backup, 2 supervisor
    Column("recipients", JSONB, nullable=False, server_default="[]"),  # everyone alerted so far
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("sent_at", DateTime(timezone=True)),
    Column("acked_by", Text),
    Column("acked_at", DateTime(timezone=True)),
    Column("closed_reason", Text),
    Index("ix_notify_notification_status", "status", "sent_at"),
    schema=SCHEMA,
)

delivery = Table(
    "delivery",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("notification_id", Uuid, ForeignKey(f"{SCHEMA}.notification.id"), nullable=False, index=True),
    Column("level", Integer, nullable=False),
    Column("channel", Text, nullable=False),  # email | teams
    Column("recipient", Text, nullable=False),
    Column("ok", Boolean, nullable=False),
    Column("error", Text),
    Column("at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    schema=SCHEMA,
)
