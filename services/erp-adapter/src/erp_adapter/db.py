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

SCHEMA = "integration"

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)
messaging = Messaging(metadata, source="erp-adapter")

poll_state = Table(
    "poll_state",
    metadata,
    Column("name", Text, primary_key=True),
    Column("watermark", DateTime(timezone=True), nullable=False),
    Column("last_run_at", DateTime(timezone=True)),
    Column("last_success_at", DateTime(timezone=True)),
    Column("last_error", Text),
    Column("last_published", Integer, nullable=False, server_default="0"),
    schema=SCHEMA,
)

inbound_message = Table(
    "inbound_message",
    metadata,
    Column("id", Uuid, primary_key=True),
    # Dedup key: the same SAP change (object + change timestamp) is published once.
    Column("message_key", Text, nullable=False, unique=True),
    Column("erp_object", Text, nullable=False),
    Column("erp_key", Text, nullable=False, index=True),
    Column("erp_changed_at", DateTime(timezone=True)),
    Column("payload", JSONB, nullable=False),
    Column("event_id", Uuid, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    schema=SCHEMA,
)

outbound_message = Table(
    "outbound_message",
    metadata,
    Column("id", Uuid, primary_key=True),  # = MES confirmation id: the idempotency key
    Column("seq", BigInteger, Identity(), nullable=False, unique=True),
    Column("message_type", Text, nullable=False),
    Column("erp_key", Text, nullable=False),  # plant:order, messages for one order are sent in order
    Column("payload", JSONB, nullable=False),
    Column("status", Text, nullable=False),  # PENDING SENDING RETRY_WAIT POSTED FAILED DEAD
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("max_attempts", Integer, nullable=False),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("lease_until", DateTime(timezone=True)),
    Column("last_error", Text),
    Column("erp_result", JSONB),
    Column("reconciled", Boolean, nullable=False, server_default="false"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("completed_at", DateTime(timezone=True)),
    Index("ix_outbound_due", "status", "next_attempt_at"),
    Index("ix_outbound_erp_key_seq", "erp_key", "seq"),
    schema=SCHEMA,
)

outbound_attempt = Table(
    "outbound_attempt",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("message_id", Uuid, ForeignKey(f"{SCHEMA}.outbound_message.id"), nullable=False, index=True),
    Column("attempt_no", Integer, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True), nullable=False),
    Column("outcome", Text, nullable=False),  # POSTED RECONCILED RETRYABLE AMBIGUOUS PERMANENT
    Column("error", Text),
    schema=SCHEMA,
)

MESSAGE_CONFIRMATION = "PRODUCTION_CONFIRMATION"
MESSAGE_GOODS_MOVEMENT = "GOODS_MOVEMENT"
MESSAGE_MAINT_NOTIFICATION = "MAINT_NOTIFICATION"
MESSAGE_MAINT_ORDER = "MAINT_ORDER"
MESSAGE_MAINT_ORDER_STATUS = "MAINT_ORDER_STATUS"
OPEN_STATUSES = ("PENDING", "SENDING", "RETRY_WAIT")
