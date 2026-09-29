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

SCHEMA = "itsm"

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)
# same database as erp-adapter, own outbox/inbox
messaging = Messaging(metadata, source="itsm-adapter", schema="itsm_messaging")

# one row per MES ticket event: applied to ServiceNow / Jira in order per ticket
outbound_message = Table(
    "outbound_message",
    metadata,
    Column("id", Uuid, primary_key=True),  # = MES event id
    Column("seq", BigInteger, Identity(), nullable=False, unique=True),
    Column("ticket_id", Uuid, nullable=False),
    Column("ticket_number", Text, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("status", Text, nullable=False),  # PENDING SENDING RETRY_WAIT SYNCED FAILED DEAD SKIPPED
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("max_attempts", Integer, nullable=False),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("lease_until", DateTime(timezone=True)),
    Column("last_error", Text),
    Column("result", JSONB),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("completed_at", DateTime(timezone=True)),
    Index("ix_itsm_outbound_due", "status", "next_attempt_at"),
    Index("ix_itsm_outbound_ticket_seq", "ticket_id", "seq"),
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
    Column("outcome", Text, nullable=False),  # SYNCED RETRYABLE AMBIGUOUS PERMANENT
    Column("calls", JSONB),  # the MCP tool calls made in this attempt
    Column("error", Text),
    schema=SCHEMA,
)

poll_state = Table(
    "poll_state",
    metadata,
    Column("name", Text, primary_key=True),  # servicenow | jira
    Column("watermark", DateTime(timezone=True), nullable=False),
    Column("last_run_at", DateTime(timezone=True)),
    Column("last_success_at", DateTime(timezone=True)),
    Column("last_error", Text),
    Column("last_published", Integer, nullable=False, server_default="0"),
    schema=SCHEMA,
)

# external changes already published to MES (restart-safe dedup; MES dedups too)
inbound_change = Table(
    "inbound_change",
    metadata,
    Column("external_id", Text, primary_key=True),
    Column("ticket_id", Uuid, nullable=False, index=True),
    Column("origin", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    schema=SCHEMA,
)

OPEN_STATUSES = ("PENDING", "SENDING", "RETRY_WAIT")

# the ServiceNow and the Jira connection, set up in the admin UI (one row each). The secret (ServiceNow password,
# Jira API token) is stored encrypted (Fernet, key ITSM_SECRET_KEY) and never returned by the API.
connection = Table(
    "connection",
    metadata,
    Column("system", Text, primary_key=True),  # servicenow | jira
    Column("url", Text, nullable=False),
    Column("username", Text, nullable=False),  # ServiceNow user / Jira account e-mail
    Column("secret", Text),  # encrypted
    Column("secret_set_at", DateTime(timezone=True)),
    Column("secret_set_by", Text),
    Column("options", JSONB, nullable=False, server_default="{}"),  # groups, close code, project, issue types ...
    Column("enabled", Boolean, nullable=False, server_default="true"),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_by", Text, nullable=False),
    schema=SCHEMA,
)

# who changed a connection and what (field names only, never secret values)
connection_change = Table(
    "connection_change",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("system", Text, nullable=False),
    Column("at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("by", Text, nullable=False),
    Column("changes", JSONB, nullable=False),  # ["url", "password", "options.group_l1", ...]
    Column("note", Text),
    schema=SCHEMA,
)

# where a ticket's incident / issue lives: a ticket stays on the instance it was created on, so switching the
# connection to another instance never duplicates older tickets there (their later changes are not synced)
ticket_link = Table(
    "ticket_link",
    metadata,
    Column("ticket_id", Uuid, primary_key=True),
    Column("sn_instance", Text),  # host, e.g. dev318787.service-now.com
    Column("sn_sys_id", Text),
    Column("sn_number", Text),
    Column("jira_site", Text),  # host, e.g. acme.atlassian.net
    Column("jira_key", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    schema=SCHEMA,
)
