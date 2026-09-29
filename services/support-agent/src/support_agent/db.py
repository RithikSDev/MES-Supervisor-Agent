"""The agent's own tables (schema `agent` in the integration database): runs with every step, proposals waiting for a
human, operator chat sessions, and the action policy. MES business data stays in MES; the agent changes it only
through the MES APIs, so MES permissions and the audit trail apply."""

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
    Numeric,
    Table,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

from mes_common.messaging import Messaging

SCHEMA = "agent"

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)
# inbox for mes.tickets.v1 (the agent publishes nothing itself: it acts through the MES APIs)
messaging = Messaging(metadata, source="support-agent", schema="agent_messaging")

# one agent run: a ticket being worked (kind TICKET) or one chat turn (kind CHAT)
run = Table(
    "run",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("kind", Text, nullable=False),  # TICKET | CHAT
    Column("tier", Text),  # ticket runs: L1 (first line) | L2 (root cause, approved fixes, L3 handover)
    Column("trigger", Text, nullable=False),  # CREATED | MANUAL | CHAT
    Column("trigger_key", Text, unique=True),  # MES event id: a redelivered event doesn't run twice
    Column("status", Text, nullable=False),  # QUEUED RUNNING DONE FAILED STOPPED SKIPPED
    Column("outcome", Text),  # RESOLVED RESOLVE_PROPOSED ESCALATED NOTED TICKET_CREATED FIXED_IN_CHAT CHAT ...
    Column("plant_id", Uuid),
    Column("ticket_id", Uuid, index=True),
    Column("ticket_number", Text),
    Column("chat_session_id", Uuid, index=True),
    Column("summary", Text),
    Column("rca", JSONB),  # L2: the recorded root-cause analysis
    Column("model", Text),
    Column("input_tokens", BigInteger, nullable=False, server_default="0"),
    Column("output_tokens", BigInteger, nullable=False, server_default="0"),
    Column("cache_read_tokens", BigInteger, nullable=False, server_default="0"),
    Column("cache_write_tokens", BigInteger, nullable=False, server_default="0"),
    Column("cost_usd", Numeric(12, 6), nullable=False, server_default="0"),
    Column("steps", Integer, nullable=False, server_default="0"),
    Column("error", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("started_at", DateTime(timezone=True)),
    Column("lease_until", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Index("ix_agent_run_status", "status", "created_at"),
    schema=SCHEMA,
)

# what the agent did, in order: model turns (its text), tool calls with input/output, proposals, errors
step = Table(
    "step",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("run_id", Uuid, ForeignKey(f"{SCHEMA}.run.id"), nullable=False, index=True),
    Column("seq", Integer, nullable=False),
    Column("at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("kind", Text, nullable=False),  # MODEL | TOOL | ACTION | PROPOSAL | ERROR
    Column("name", Text),
    Column("input", JSONB),
    Column("output", JSONB),
    Column("is_error", Boolean, nullable=False, server_default="false"),
    Column("ms", Integer),
    schema=SCHEMA,
)

# an action the policy says a human must approve
proposal = Table(
    "proposal",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("run_id", Uuid, ForeignKey(f"{SCHEMA}.run.id"), nullable=False),
    Column("plant_id", Uuid),
    Column("ticket_id", Uuid, nullable=False, index=True),
    Column("ticket_number", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("args", JSONB, nullable=False),
    Column("summary", Text, nullable=False),  # what will happen, in words
    Column("rationale", Text),  # why the agent proposes it
    Column("status", Text, nullable=False),  # PENDING EXECUTED FAILED REJECTED
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("decided_by", Text),
    Column("decided_at", DateTime(timezone=True)),
    Column("decision_note", Text),
    Column("result", JSONB),
    Column("error", Text),
    Index("ix_agent_proposal_status", "status", "created_at"),
    schema=SCHEMA,
)

# an operator's conversation with the assistant on a terminal
chat_session = Table(
    "chat_session",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("plant_id", Uuid, nullable=False),
    Column("user_id", Uuid, nullable=False),
    Column("user_name", Text),
    Column("terminal_id", Uuid),
    Column("terminal_code", Text),
    Column("line_code", Text),
    Column("context", JSONB),  # what the terminal showed (machine, order, operation)
    Column("status", Text, nullable=False),  # OPEN | FIXED | TICKET_CREATED | CLOSED
    Column("ticket_id", Uuid),
    Column("ticket_number", Text),
    Column("messages", JSONB, nullable=False),  # the model conversation (append-only)
    Column("transcript", JSONB, nullable=False),  # what the operator saw: [{role, text, at, choices}]
    Column("cost_usd", Numeric(12, 6), nullable=False, server_default="0"),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("last_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("ix_agent_chat_session_started", "started_at"),
    schema=SCHEMA,
)

# a support engineer's conversation with the AI support assistant (admin UI): answers with interactive cards; any
# change the assistant suggests is shown as a confirmation card and runs with the engineer's own rights when confirmed
engineer_chat = Table(
    "engineer_chat",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("plant_id", Uuid, nullable=False),
    Column("user_sub", Text, nullable=False),  # Keycloak subject of the engineer
    Column("user_name", Text),
    Column("title", Text),  # the first question, shortened
    Column("messages", JSONB, nullable=False),  # the model conversation (append-only)
    # what the engineer saw: [{role, at, parts: [{type: text, text} | {type: card, card}]}]
    Column("transcript", JSONB, nullable=False),
    Column("cost_usd", Numeric(12, 6), nullable=False, server_default="0"),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("last_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("ix_agent_engineer_chat_user", "user_sub", "last_at"),
    schema=SCHEMA,
)

# MCP servers the agents may use (live data: MES database, SAP, ...), set up in the admin UI. The token is stored
# encrypted (AGENT_SECRET_KEY) and never returned; `modes` says which agents get the server's tools.
mcp_server = Table(
    "mcp_server",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("name", Text, nullable=False, unique=True),  # tool prefix: <name>__<tool>
    Column("label", Text, nullable=False),
    Column("description", Text),
    Column("url", Text, nullable=False),
    Column("token", Text),  # encrypted bearer token
    Column("enabled", Boolean, nullable=False, server_default="true"),
    Column("modes", JSONB, nullable=False),  # ["engineer", "l2", "ticket", "chat"]
    Column("tools", JSONB),  # tools found at the last test: [{name, description}]
    Column("last_check_at", DateTime(timezone=True)),
    Column("last_error", Text),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("updated_by", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    schema=SCHEMA,
)

# knowledge the agents search: built-in machine guides and the MES user guide (files under knowledge/), and manuals,
# fault-code lists and notes uploaded by admins. Chunks are searched with PostgreSQL full-text search.
knowledge_doc = Table(
    "knowledge_doc",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("title", Text, nullable=False),
    Column("kind", Text, nullable=False),  # MACHINE_GUIDE | MANUAL | ALARM_LIST | USER_GUIDE | NOTE
    Column("equipment_type", Text),  # CNC_MILL, CNC_LATHE, ... (machine documents)
    Column("screen", Text),  # user guide: admin UI path of the module (/schedule, ...)
    Column("source", Text, nullable=False),  # file name (built-in: knowledge/<path>)
    Column("builtin", Boolean, nullable=False, server_default="false"),
    Column("checksum", Text, nullable=False),
    Column("pages", Integer),
    Column("chars", Integer, nullable=False, server_default="0"),
    Column("uploaded_by", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    schema=SCHEMA,
)

knowledge_chunk = Table(
    "knowledge_chunk",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("doc_id", Uuid, ForeignKey(f"{SCHEMA}.knowledge_doc.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("seq", Integer, nullable=False),
    Column("page", Integer),
    Column("heading", Text),
    Column("text", Text, nullable=False),
    schema=SCHEMA,
)

# per action: off | suggest (a human approves) | auto
policy = Table(
    "policy",
    metadata,
    Column("action", Text, primary_key=True),
    Column("mode", Text, nullable=False),
    Column("updated_by", Text),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    schema=SCHEMA,
)

# a person took the ticket over: the agent does not act on it (until handed back)
ticket_override = Table(
    "ticket_override",
    metadata,
    Column("ticket_id", Uuid, primary_key=True),
    Column("ticket_number", Text),
    Column("taken_by", Text, nullable=False),
    Column("note", Text),
    Column("taken_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    schema=SCHEMA,
)

# a supervisor's verdict on an agent run (feeds the support analytics and the evaluation set)
rating = Table(
    "rating",
    metadata,
    Column("run_id", Uuid, ForeignKey(f"{SCHEMA}.run.id"), primary_key=True),
    Column("score", Integer, nullable=False),  # +1 helpful / -1 wrong
    Column("comment", Text),
    Column("rated_by", Text, nullable=False),
    Column("rated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    schema=SCHEMA,
)

# evaluation runs: the scenario set run against the real model, graded by rules (python -m support_agent.evals)
eval_run = Table(
    "eval_run",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("model", Text, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True)),
    Column("total", Integer, nullable=False, server_default="0"),
    Column("passed", Integer, nullable=False, server_default="0"),
    Column("cost_usd", Numeric(12, 6), nullable=False, server_default="0"),
    Column("results", JSONB, nullable=False, server_default="[]"),
    schema=SCHEMA,
)

# kill switch, cost limits
setting = Table(
    "setting",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", JSONB, nullable=False),
    Column("updated_by", Text),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    schema=SCHEMA,
)
