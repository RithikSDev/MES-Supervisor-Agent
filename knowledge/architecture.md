# MES architecture for support engineers

## Services (log `service` label in brackets)
- **core** (`core-api`, `core-worker`): the MES itself. Modules: org (plants, lines, machines, terminals), identity
  (users, PINs, terminal sessions), masterdata, planning (work orders, schedule, quality hold), execution (runs,
  confirmations, live state), inventory (lots, stock by type, goods movements, backflush), quality (inspections,
  nonconformances), genealogy, maintenance, ticketing, compliance (audit trail, e-signatures), erp_sync (applies
  SAP results). The worker relays events (outbox), consumes SAP/ITSM events, checks SLAs, seals the audit chain,
  generates preventive maintenance orders.
- **erp-adapter** (`erp-adapter-api`, `erp-adapter-worker`): the only component that talks to SAP. It polls SAP for
  production orders (and PM orders) and posts confirmations, goods movements and PM documents. Every posting is a
  message with attempts; before every retry it looks the posting up in SAP (reconcile), so a retry never posts twice.
  Message states: PENDING, SENDING, RETRY_WAIT (transient error, retried with backoff), POSTED, FAILED (SAP rejected
  the content: needs a fix, then a manual retry), DEAD (gave up after the attempt limit).
- **itsm-adapter** (`itsm-adapter-api`, `itsm-adapter-worker`): syncs MES tickets to ServiceNow incidents (L1/L2
  groups) and Jira issues (L3), and polls their changes back.
- **support-agent**: the AI support agent (this system).
- **mock-sap**: stands in for SAP S/4HANA in this installation.
- Infrastructure: `postgres` (database), `redpanda` (event bus), `keycloak` (login), `redis` (live line state).

## Request and event ids
Every API request has a request id (X-Request-ID): it is on every log line of that request and on the audit trail
entries it wrote. Event handling logs carry `event:<event id>`; the AI agent's calls carry `agent-run:<run id>`.
Use trace_request with such an id to see the request across services.

## Typical flows
- SAP order -> erp-adapter poll -> event -> core creates/updates the work order. A MES order that differs from SAP
  (status, quantity, dates) usually means an SAP change that was not polled yet or an event that failed in core:
  compare_order_with_sap and get_event_failures show which; resync_order re-reads the order.
- Terminal confirmation -> core (backflush + goods receipt in the same transaction) -> event -> erp-adapter posts to
  SAP -> result event -> core marks the confirmation POSTED/FAILED. A FAILED posting raises an automatic L2 ticket.
- Ticket -> itsm-adapter -> ServiceNow (+ Jira at L3); engineers' changes come back by polling every 15 s.

## Common errors and what they mean
- SAP "order is technically completed" / "TECO": production continued in MES after the order was closed in SAP.
  Business decision (SAP planner): reopen the order in SAP, then retry; or confirm on another order. Not a MES bug.
- SAP "locked by user": another SAP user had the order open; transient, retrying is right.
- SAP "material/batch unknown", "posting period closed", "quantity exceeds": master data or period issue for the SAP
  team (L2 human), then retry.
- HTTP 401/403 in an adapter: credentials or roles of an integration user (configuration, human L2).
- Core `stale_data` / `invalid_transition` (HTTP 409): two users changed the same record or an action in the wrong
  state; usually user process, not a bug.
- Core HTTP 500 with a traceback in `core-api` logs: likely a code bug (L3), especially if it repeats.
- `dead-lettering message` in a worker log: an event could not be processed; see get_event_failures.
