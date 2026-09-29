# CLAUDE.md

This file gives Claude Code persistent context on this project. Read it at the
start of every session. Update the **Current State** section at the end of
each phase/session so the next session picks up accurately — this file is
the single source of truth for "where things stand," not the commit history
or the original plan doc.

---

## Project Overview

A production-grade, scalable Manufacturing Execution System (MES) that
integrates with ERP/SAP for work orders and covers: Production Planning &
Scheduling, Production Execution, Material Management, Quality Management,
Traceability & Genealogy, Ticketing/Issues, Equipment & Maintenance,
Workforce & Work Instructions, Performance & Analytics, Integration
(ERP/PLM/SCADA/PLC/IoT), and AI & Advanced Manufacturing, plus cross-cutting
Compliance & Audit and Alert & Notification management.

## Architecture

- **Backend**: Python, FastAPI (async). Modular monolith — domain modules
  map 1:1 to the business modules above, each with its own router/service/
  repository layer, so any module can be split into a standalone service
  later without a rewrite.
- **Separate standalone services** (not part of the core monolith):
  - Integration/adapter services — `erp-adapter` (ERP/SAP; later PLC, SCADA, IoT) and `itsm-adapter`
    (real ServiceNow + Jira Cloud over REST by default; MCP switchable)
  - `mes-mcp` — MES as an **MCP server** for AI agents (acts via the core API as a Keycloak service account)
  - AI & Advanced Manufacturing service — predictive maintenance, AI quality
    inspection, production optimization, RCA assistant
  - Alerts/Notifications service
- **Database**: PostgreSQL as system of record (relational integrity for
  work orders, genealogy, inventory ledgers, audit trail). TimescaleDB (or
  similar) for machine telemetry / OEE time-series data. Redis for caching
  and real-time dashboard state.
- **Messaging**: Kafka API via **Redpanda** (single binary, dev-container mode)
  as the event bus between shop-floor/integration events and core services.
- **Frontend**: React, two distinct UIs:
  - Admin/planning/analytics dashboard (desktop-oriented, data-dense)
  - Shop-floor operator terminal (touch-friendly, minimal, high-contrast,
    large tap targets — assume gloved hands and shared/kiosk devices)
- **Auth**: OAuth2/OIDC, RBAC (roles: operator, supervisor, quality
  engineer, maintenance tech, admin). Electronic signature support for
  approvals.
- **Deployment**: Docker, Kubernetes-ready.

## Conventions

### Multi-tenancy / scoping
Every core business table carries `plant_id` from day one. `line_id` is stored
where an entity belongs to one line (work center, equipment, work order,
schedule, shift pattern); operations/confirmations derive their line through
the work center. Never write a query that assumes a single plant/line
implicitly. Users are scoped to plants via `identity.user_scope` (admin = all);
API handlers call `user.ensure_plant()` / filter by `user.plant_ids`.

### Audit trail
Every write to a core business table (work orders, inventory movements,
quality records, genealogy, approvals) must generate an immutable audit log
entry: who, what, when, before/after state. This is implemented with **Postgres triggers**
(`audit.log_change()`, see `services/core/src/mes_core/modules/compliance/audit_sql.py`) — do not
bypass it with direct writes.
- All writes go through `Database.unit_of_work(actor, request_id, reason)`,
  which sets `mes.actor_id`/`mes.reason`/... via `set_config(..., true)`. The
  trigger **rejects** writes without an actor. Services use `Actor.service("name")`.
- Reasons/e-signatures: `set_change_reason(s, reason, signature_id)` or
  `compliance.public.sign(...)` inside the transaction.
- Every new business table needs `attach_audit_trigger()` in its migration;
  `tests/test_audit.py::test_every_business_table_has_the_audit_trigger` enforces it.
- `audit.audit_log` is INSERT-only for the app role, guard-triggered, monthly
  partitioned, hash-chained by the worker's sealer (`audit.seal()` / `audit.verify_chain()`).
- Events: transactional outbox (`messaging.publish(session, ...)`) + inbox dedup.

### SAP/integration isolation
Core business logic never calls SAP (or any external system) directly.
All external integration goes through the adapter service behind a defined
internal interface. Retries must be idempotent — no duplicate work orders
or inventory movements on redelivery.
- Contract = canonical events (`contracts/events/*.schema.json`, envelope in
  `libs/mes-common`). SAP field names exist only in `services/erp-adapter/src/erp_adapter/sap/`.
- Outbound: MES record id = idempotency key, written to SAP (`ConfirmationText`
  = `MES:<uuid>`); every retry reconciles (looks it up in SAP) before re-posting.
- Inbound: dedup on (object, SAP LastChangeDateTime); core ignores stale snapshots.
- SAP PM numbers come from MES number ranges (external numbering), so notification/order creates are idempotent by
  key; a PM order and its spare-part 261s share the ordering key `plant:order`.
- ITSM (ServiceNow/Jira) is reached only by `services/itsm-adapter`, through two ports (`ports.py`:
  `ServiceNowPort`, `JiraPort`) with normalized records. Connectors: `connectors/servicenow_rest.py` (Table API:
  incident, sys_user_group, sys_journal_field; relative `RELATIVEGE` time filters) and `connectors/jira_rest.py`
  (Cloud REST v3: `/search/jql`, ADF bodies, changelog, `/myself`) = `ITSM_MODE=rest` (default); `connectors/mcp.py`
  over MCP tools = `ITSM_MODE=mcp` (`mcp` SDK v2 `Client` + `streamable_http_client`). Deliveries are desired-state
  and idempotent (lookup by `correlation_id = MES:<ticket id>` / Jira label `mes-ticket-<id>`, `[MES:<activity id>]`
  markers, only changed fields); changes by the integration user are not echoed back. Errors (`errors.py`):
  429/5xx/connect = RETRYABLE, write timeout = AMBIGUOUS (next delivery looks up first), 401/403/other 4xx =
  PERMANENT. Every REST/MCP call is logged per attempt (`calls.py`). ServiceNow/Jira field names exist only in the
  itsm-adapter.
- **ServiceNow / Jira connections** (one of each for the whole MES) are set up by admins in the UI (Integrations ->
  ServiceNow / Jira: test, save, create missing groups) and stored in `itsm.connection`; password / API token
  encrypted with `ITSM_SECRET_KEY` (Fernet, in `.env`, created by `python -m itsm_adapter.keygen` which writes it
  without printing it), write-only in the API. `connections.Registry` rebuilds the ports when a row's version changes
  (API on save, worker every `config_refresh_s`). URLs https + *.service-now.com / *.servicenowservices.com /
  *.atlassian.net unless `ITSM_ALLOW_ANY_HOST`. Old `SERVICENOW_*` / `JIRA_*` / `GROUP_*` env values are imported once
  when no row exists. `itsm.ticket_link` keeps each ticket on the instance it was created on (after a switch its
  messages are SKIPPED, never duplicated). History in `itsm.connection_change` (field names only). MCP mode (tests)
  still configures from settings.
- **Credentials**: never commit them, never ask the user to paste them into chat, and never type them anywhere
  yourself; the user enters them in the UI form (or `.env` for other systems). `python -m itsm_adapter.setup
  [--create-groups]` checks the stored connections.

### Naming & structure
- uv workspace: `libs/mes-common`, `services/{core,erp-adapter,mock-sap}`; npm workspace `frontend/apps/*`.
- Core module layout: `services/core/src/mes_core/modules/<module>/{models,service,api,schemas,public}.py`
  (+ `domain/` for pure logic). Other modules import **only** `<module>.public`; enforced by
  import-linter (`cd services/core && uv run lint-imports`). One Postgres schema per module.
- Entities inherit `shared.db.base.Entity` (UUIDv7 id, `row_version` optimistic lock, created/updated at/by).
- Alembic per service (`services/<svc>/migrations`), revision ids `0001`, `0002`, ... Autogenerate, then
  hand-add what autogenerate can't express (exclusion constraints, audit triggers, grants).
- API: `/api/v1/...`; errors are `{code, message, details}`; writes take `row_version` for optimistic locking.

### Testing
- pytest + pytest-asyncio; DB tests use testcontainers (TimescaleDB image + the real init script + migrations).
- Must have tests before merge: audit capture for new write paths, every adapter retry/reconcile path,
  state-machine transitions.
- Adapter tests run against mock SAP over real HTTP (timeouts matter). UI: Playwright smoke
  (`frontend/apps/admin/e2e`). `scripts/smoke_test.py` = end-to-end against the running compose stack.

## UI Conventions

- Admin dashboard: React 19 + Vite + TypeScript, Mantine 8, TanStack Query, react-router 7,
  react-oidc-context, vis-timeline (schedule board). Typed API client generated from the core OpenAPI
  (`openapi-typescript` + `openapi-fetch`). Chart library: not chosen yet (Phase 6).
- Admin visual language (user, 2026-09-26): light workspace, white (major) + blue (brand/primary #1f5fd1), navy text,
  navy top/bottom bands. No side navbar: the landing page (`pages/HomePage.tsx`) shows every module as a numbered
  card (registry `src/modules.ts`, filtered by permission, group chips) plus a "Needs attention" panel; inside a
  module a breadcrumb ("← Home / Module") and a Modules switcher menu. Titles: Source Serif 4 with the last word in
  italic blue (`PageHeader`, `.page-title`, `.display em`); labels IBM Plex Mono uppercase spaced; body IBM Plex
  Sans (all @fontsource, offline). Status colours keep their meaning (red abnormal, amber = attention (darkened
  #b37f00 for white), green confirmed). Admin theme = `apps/admin/src/theme.ts` + `styles.css` (overrides the shared
  dark tokens). Chart palette on white: #2a78d6/#eb6834/#1baf7a (validated all-pairs). Keep the MES "M" logo.
  Playwright navigates with `e2e/nav.ts` `openModule(page, name)`.
- The AI Support Assistant (`/ai-support` without `view=console` / `tab`) is a full-screen app: Layout renders only
  the outlet; `AssistantChat` has its own left sidebar (conversations grouped Today / Yesterday / Previous 7 days /
  Older, search, delete with a confirmation dialog, collapsible), a header (Home, title, cost, Agent console) and a
  scrolling thread with the composer fixed at the bottom (`.assistant-*` classes in styles.css).
- Terminal keeps the dark high-contrast HMI look (ISA-101 spirit, `@mes/ui` theme/tokens, Barlow Condensed labels).
- Operator terminal (`frontend/apps/terminal`): same tokens, no router, Mantine + custom `.t-*` classes in
  `terminal.css`; touch targets >= 64 px (action buttons 84 px), keypad for all numbers, colour = state
  (green running, cyan setup, amber paused/offline, red rejected). Header always shows ONLINE/OFFLINE and
  "n to sync". Offline: actions go through `state/queue.ts` (client_event_id + occurred_at) and are shown
  optimistically via `state/optimistic.ts`. Idle logout after 10 min.
- Shared packages: `frontend/packages/ui` (theme, tokens.css, fonts) and `frontend/packages/api-client`
  (generated types, `core` typed client, `coreRaw`/`erp`/`mockSap` helpers, `streamEvents` for SSE).

## Current State

> Update this section at the end of every phase/session. This is what a new
> session should read to know what's real vs. planned.

**Last updated:** 2026-09-28 (demo tickets; forecast, SLA breach/exit report, assignees, bulk P1/P2 changes in the assistant)

**Phase status:**
- [x] Phase 1 — Core data model, auth/RBAC, Work Order/Planning module, SAP
      adapter (inbound work orders, outbound confirmations, idempotency)
- [x] Phase 2 — Production Execution + Material Management
- [x] Phase 3 — Quality Management + Traceability/Genealogy
- [x] Phase 4 — Equipment & Maintenance + Ticketing/Issues (real ServiceNow/Jira Cloud connectors, MES MCP server)
- [x] Phase 5 — AI L1 support agent + operator chat (re-scoped 2026-09-25, see below)
- [x] Phase 6 — AI L2 agent + human-in-the-loop fixes (log store, approved fixes, L3 handover, takeover)
- [x] Phase 7 — Alerts/notifications (SLA breach, on-call handover) + support analytics + agent evaluation
- [ ] Phase 8 — Governance & hardening (per-action permissions, prompt-injection defence, OAuth, deployment)

Re-scope (user, 2026-09-25): the core of this MES is **support tickets handled by AI support engineers**. The
original Phases 5-8 (workforce/work instructions, OEE analytics/TimescaleDB, predictive maintenance/optimisation,
PLC/SCADA) were dropped or deferred; the plan above replaced them.

**What's actually built and working right now:**
- `docker compose up -d --build` runs everything: TimescaleDB (PG17), Redpanda + Console, Redis, Keycloak
  (realm `mes` auto-imported), mock-sap (:8030), erp-adapter (api :8020 + worker), mes-core (migrate, api :8000,
  worker), **itsm-adapter (api :8070 + worker)**, **mes-mcp (:8040)**, admin UI (:3080), operator terminal (:3081).
  The UIs' nginx re-resolves backend containers. No mock ServiceNow/Jira in the stack: the itsm-adapter connects to
  the user's own instances from `.env` (unset = "not configured"; ticket syncs wait as PENDING and go out later).
- Core modules: org, identity (JIT users, plant scopes, operator PINs with optional **maintenance technician**
  terminal rights, terminal enrolment + PIN sessions), masterdata, planning (work orders, scheduling with e-signed
  publish, quality hold; machine calendars minus maintenance windows / down machines), execution (runs, time
  accounting, confirmations, queue, live state; runs can't start/resume unless the machine is AVAILABLE),
  inventory (lot stock by stock type, SAP movement types incl. 261 to maintenance orders, serials), quality,
  genealogy, **maintenance** (operator-entered breakdowns: run paused, downtime opened, machine DOWN; notifications
  M1/M2 and orders PM01/PM02 with MES number ranges = SAP numbers; technician start/checklist/labour/spare parts;
  e-signed sign-off (meaning RELEASED) releases the machine; preventive plans by calendar or running hours, generated
  by the worker; downtime log; MTBF/MTTR), **ticketing** (P1-P4 severity matrix suggested from impact, L1/L2/L3
  tiers + escalation, status workflow, response/resolution SLA clocks with on-hold pause, breach detection in the
  worker, activity log with origin MES/ServiceNow/Jira/System; automatic L2 ticket when SAP finally rejects a
  posting), compliance, erp_sync (also consumes erp.maintenance-* and itsm.* events).
- ERP adapter: + PM kinds (notification create, order create, order status REL/TECO) with reconcile, PM order change
  poller. Mock SAP: + `API_MAINTNOTIFICATION` / `API_MAINTENANCEORDER` look-alikes, spare-part products.
- ITSM: itsm-adapter consumes `mes.tickets.v1`, syncs to ServiceNow (incident with correlation_id/display,
  optional contact_type (SERVICENOW_CONTACT_TYPE), impact/urgency from severity, L1/L2/L3 assignment groups by name -> sys_id, On Hold
  with hold_reason, Resolved with close_code + close_notes, work notes) and at L3 to Jira Cloud (Bug/Task, priority
  with fallback when not on the create screen, comments); per-ticket ordering, retry, attempt log with the REST
  calls (method, path, status, ms); status endpoint reports each connection (mode, configured, reachable, detail);
  setup CLI checks credentials, groups (can create them), journal read access and Jira issue types; poller (15 s in
  compose) publishes engineers' changes (state, group, priority, notes, Jira status/comments) to
  `itsm.ticket-changes.v1`; Jira Done adds a ServiceNow work note for L2. Own tables in `mes_integration`
  (schemas `itsm`, `itsm_messaging`; `Messaging(..., schema=)` is now configurable).
- **support-agent** (Phase 5, `services/support-agent`, api :8090 + worker): Claude (`claude-opus-5`, Anthropic
  Python SDK 1.x, beta messages stream with `fallbacks: "default"`, top-level prompt caching, effort high for tickets /
  medium for chat). Ticket runs: `mes.ticket.created` (events older than 30 min ignored) -> queued run -> agent loop
  (`runner.py`) with tools (`tools.py`): read (runbooks, similar tickets via core `/tickets/search`, machine/line/
  order/terminal status, SAP + ITSM queues, audit, NCs, shortages) and write actions (`actions.py`: add_note,
  escalate, set_severity, resolve, close_duplicate, retry_sap, retry_itsm, end_terminal_session, machine_back_up)
  behind per-action policy off/suggest/auto (defaults: note + escalate auto, rest suggest). Proposals are approved in
  the admin UI and executed with the approver's token. Limits (steps, $ per ticket/chat) and model refusals/failures
  -> human handover (escalate at L1, else note). Claude setup errors (401/403/404/400) pause the agent (setting
  `health`), leave tickets untouched and retry after 5 min; transient errors requeue the run. Chat: terminal token
  (validated via core `/me`, which now carries terminal/line), SSE events text/status/choices/ticket/closed/done;
  `show_quick_replies` ends the turn without another model call; `create_ticket` runs with the operator's token,
  `mark_fixed` creates + resolves. Runbooks: `knowledge/runbooks/*.md` (11). Tables: schema `agent` (run, step,
  proposal, chat_session, policy, setting) + `agent_messaging` inbox.
- **Phase 6 (AI L2)**: runs have `tier` (L1/L2) and `rca`. Worker: `mes.ticket.created` at L2 or
  `mes.ticket.updated` with `tier` in changes and tier L2 -> L2 run (L3 and other updates are for people). L2 mode:
  `prompts.l2_system` (+ `knowledge/architecture.md` system guide), effort `EFFORT_L2`, cost limit
  `max_cost_per_l2_usd` ($2), +12 steps. L2-only tools: search_logs / trace_request (Loki LogQL, JSON lines),
  get_sap_message, compare_order_with_sap (MES order vs adapter's SAP snapshots + postings), get_event_failures (core
  `/ops/event-failures`, dead letters), get_service_health, record_root_cause (stored on the run + work note),
  resync_order (action, suggest), escalate_to_l3 (Jira-ready handover text); L1's escalate_ticket is L1-only. A tool
  exception no longer ends the run (returned to the model as a tool error). Human takeover: table
  `agent.ticket_override` (POST/DELETE `/tickets/{id}/takeover`; queued runs skipped; "Run again" hands back).
  Runbooks +3 (sap-order-mismatch, sap-posting-rejected, mes-error-500). Core: escalation reason emitted as the event
  comment (ServiceNow work note; the itsm-adapter adds it as a comment on the new Jira issue at L3).
- **Phase 7**: **notifier** service (`services/notifier`, api :8095 + worker; schema `notify`): consumes
  `mes.tickets.v1` + new topic `ai.support.v1` (agent outbox: `ai.proposal.created/decided`, `ai.run.handed_over`,
  `ai.agent.health_changed`), events older than 30 min ignored. Rules (P1_RAISED, SLA_AT_RISK, SLA_BREACHED,
  ESCALATED, APPROVAL_WAITING, AI_HANDOVER, AGENT_DOWN) -> alert per cause (dedup key) naming rota roles; dispatcher
  sends level 0 = role primaries, escalator moves unacknowledged alerts up after `ack_minutes` (level 1 backups,
  level 2 supervisor); alerts close when the cause goes away (ticket acknowledged/resolved, proposal decided, agent
  healthy). Channels: e-mail (SMTP; compose uses Mailpit :8025/:1025 unless SMTP_* set), Teams (TEAMS_WEBHOOK_URL,
  adaptive card), in-app (admin header bell + Notifications page with rota and rule editors, `?ack=<id>` link).
  Core: SLA at-risk flags (migration 0007, 75% of a target, `mes.ticket.sla_at_risk`), `GET /tickets/analytics`
  (volumes, SLA met, MTTA/MTTR per severity, categories, machines, resolved by AI/people/ServiceNow-Jira; an approved
  AI proposal counts as AI via its "proposed by the AI support agent" note). Agent: ratings (`agent.rating`, PUT
  `/runs/{id}/rating`), `GET /analytics`, outbox relay in the worker; evaluation set `support_agent.evals` (12
  scenarios, `WorldApi` simulated MES, in-memory recorder, rule grading; results in `agent.eval_run`, AI support ->
  Evaluation tab). Admin: **Support analytics** page (@mantine/charts ~8.3 on Recharts; palette = reference dark steps
  #3987e5/#d95926/#199e70 validated against #181c20, max 3 series), **Notifications** page, run 👍/👎.
- **UI redesign + engineer chat (2026-09-26, user request; Phase 8 on hold)**: admin UI light theme + landing page
  (see UI Conventions). AI Support Assistant (card 09, `pages/AssistantChat.tsx`; the old AI support page is its
  "Agent console" view, `?view=console` / `?tab=`): support engineers (admin/supervisor) chat with Claude; answers
  stream with interactive cards (`components/chatCards.tsx`: tickets list with severity chips and row -> ticket
  drawer, ticket, approvals, sla, alerts, ai_run, machines, integration, confirm). Agent side: mode `engineer`
  (`support_agent/engineer.py`, tools list_tickets / show_card / get_ai_activity / list_pending_approvals /
  get_sla_overview / propose_change + all read and L2 read tools), every MES call as the engineer (`Api.as_user`);
  cards carry a query (filter/ids), the UI fetches live data (filter rules mirrored in engineer.py and chatCards.tsx).
  Changes only via confirm cards: POST `/engineer/chats/{id}/actions/{action_id}` {confirm} runs with the engineer's
  token (ticket status/note/escalate/severity/resolve/duplicate/retries/..., take over, run AI again,
  approve/reject proposal via `Agent.decide_proposal`, shared with the console); the next turn tells the model what
  was confirmed/cancelled. Table `agent.engineer_chat` (migration 0005), runs kind ENGINEER, setting
  `max_cost_per_engineer_chat_usd` ($2). Verified live: open tickets card + acknowledge confirm card, $0.21.
- **Live data + knowledge for the AI (2026-09-26)**: `services/data-mcp` (:8045) = two read-only MCP servers:
  `/mes/mcp` (list_tables, describe_table, run_query = one checked SELECT in a READ ONLY transaction with timeout and
  row limit, machine_history, similar_failures) as DB role `mes_readonly` (compose job `db-readonly` applies
  `infra/postgres/readonly.sql` after the migrations: SELECT on business schemas, identity/audit only via
  `support_ro` views, no PIN hashes / tokens / e-signatures), and `/sap/mcp` (sap_query / sap_get on named OData
  collections, GET only, SAP_* technical user; mock SAP in the demo). Keys MES_DB_MCP_KEY / SAP_MCP_KEY. The agent
  is an MCP client (`mcp_client.McpHub`): servers in `agent.mcp_server` (migration 0006; Integrations -> AI data
  connections; token encrypted with AGENT_SECRET_KEY = ITSM_SECRET_KEY; `modes` = which agents: engineer, l2,
  ticket, chat), tools offered as `<server>__<tool>`, results wrapped `<data source=...>`, unreachable servers left
  out of the run. Defaults on first start: MES database + SAP for engineer chat and L2. Knowledge store
  (`knowledge_store.py`, migration 0007, Postgres FTS): built-in `knowledge/machines/*.md` (sample machine guides:
  safety/LOTO, CNC lathe alarm 2031 etc., CNC mill, assembly/tester/washer) and `knowledge/guide/*.md` (MES user
  guide per module, `screen` path), plus admin uploads (PDF via pypdf, CSV fault codes one row per chunk, md/txt;
  Agent console -> Knowledge). Tools search_knowledge / read_knowledge for all agents. Fixed: tool results with
  datetimes broke the step log (StatementError) - `store.jsonable`.
- **Ticket CRUD from the engineer chat (2026-09-26)**: core `POST /tickets/{id}/details` (title, description,
  category, impact, equipment_id, work_order_id; open tickets only, reason required, activity DETAILS with from/to,
  severity kept and suggested severity recomputed, event changes ["details", fields] + comment; the itsm-adapter
  updates the incident's short_description / description / category and adds the note; Jira gets the comment).
  No deletion: cancel = status CANCELLED (ServiceNow Canceled). Assistant: `propose_new_ticket` (confirm card with
  possible duplicates, created with the engineer's token, card then points at the new number) and propose_change
  `edit_details` / `cancel`. Guards: an automatic agent write re-checks the ticket is still open (refused otherwise);
  the itsm-adapter doesn't send notes to closed/canceled incidents (ServiceNow answers 403), it marks them synced.
- **Reset demo data (2026-09-26)**: admin account menu -> "Reset demo data…" (permission `system:reset`, admin only;
  type RESET) runs, in order: agent `POST /api/v1/agent/admin/reset` (all agent tables; defaults, MCP defaults,
  built-in knowledge), notifier `/api/v1/notify/admin/reset` (+ empties Mailpit), itsm-adapter
  `/api/v1/itsm/admin/reset` (keeps itsm.connection + connection_change; poll watermarks = now), core
  `POST /api/v1/admin/reset-demo-data` (`mes_core/reset.py`: TRUNCATE every table of schemas owned by mes_owner,
  restart sequences, re-seed, clear the user cache; runs as RESET_DATABASE_URL = owner), mock SAP `/_mock/reset`
  (40 orders), erp-adapter `/api/v1/admin/reset` (orders read in again). Every endpoint refuses unless
  ALLOW_DATA_RESET (compose default true for this demo, code default false). Shared helper `mes_common.reset`.
  Keycloak accounts and Loki logs are not reset; terminals must be enrolled again. A manual full reset was done the
  same day (backup in ../MES-backups).
- **Demo tickets, forecast, SLA report, assignees (2026-09-28)**: core `ticketing.support_member` (support team,
  seeded: Priya Nair / Tom Becker L1, Wei Chen / Maria Rossi / Sam Supervisor L2, Arjun Rao L3, Ada Admin any; e-mails
  @mes.local) and ticket `assignee_id` / `assignee_name` / `assigned_at` (migration 0008). `GET /tickets/support-team`,
  `POST /tickets/{id}/assign` {assignee: user name / e-mail / name, or null}, list filter `assignee_id` / `unassigned`;
  escalation clears the assignee (next tier's team). Event payload `assignee` {name, username, email}; itsm-adapter sets
  ServiceNow "Assigned to" by sys_user e-mail then user name (none found: left empty + work note, not for demo history);
  poller reports ServiceNow assignments back only when someone is set (an empty "Assigned to" never unassigns in MES).
  Connection drawer -> "Support team in ServiceNow" (check / create missing users with role itil in their tier's group,
  `POST /api/v1/itsm/servicenow/users`). `ticketing/reports.py`: `GET /tickets/forecast?horizon_days=1..92&weeks=`
  (plant-local days; least-squares weekly trend, damped 0.9 per week ahead, x weekday profile, 80% range widening with distance,
  per-week totals with ranges, severity/category mix of 4 weeks, backlog outlook, backtest
  on last week) and `GET /tickets/sla-report?days=` (breaches = targets that fell due in the period and were missed,
  exits = resolved/closed/cancelled in the period met or missed, per severity / assignee, at risk now).
  `ticketing/demo.py` + `POST /api/v1/admin/demo-tickets` (system:reset + ALLOW_DATA_RESET; admin menu "Load demo
  tickets…"): 8 weeks of backdated history (~145, mostly closed, rising trend, weekday pattern, one SAP outage day,
  ~4 L3) + 13 open tickets relative to now; one `mes.ticket.created` per ticket in its final state with `demo`
  = history | current (agent ignores demo tickets, notifier ignores history; P1_RAISED only for status NEW); adapter
  backdates `opened_at` and delivers 4 messages in parallel (`dispatch_concurrency`; one per ticket per batch).
  Refused when loaded (reported_by `demo:*`): Reset, then Load. The data ages (no new tickets arrive): reload right
  before a demo. Assistant: cards `forecast` (horizon = the period asked, e.g. two months = 61; by day / by week view, weekly
  first beyond 3 weeks; chart actual vs forecast) (every list / summary / report card has a CSV button: tickets, sla, sla_report,
  forecast, team, approvals, alerts, machines, integration; `lib/csv.ts`, UTF-8 BOM for Excel, all rows), `sla_report` (tabs +
  CSV download), `team`; tools get_ticket_forecast / get_sla_report / list_support_team; filter `assignee` (name, me,
  others, unassigned; "me" = the viewer); propose_change `assign` and `numbers` for one card over several tickets
  (set_severity, assign, acknowledge, start_work, add_note, put_on_hold; max 25). Verified live 2026-09-28 (4 demo
  scenarios, $0.31; `e2e/demo-scenarios.spec.ts` with RUN_AI_DEMO=1); 159 demo tickets synced to the user's ServiceNow
  (INC0010036+) and Jira.
- **Support team page (2026-09-28)**: card "Support Team" (`/support-team`, `pages/SupportTeamPage.tsx`; permission
  `support:team` for admin + supervisor to edit): add / edit engineers (name, e-mail, user name, tier), take them off
  the team (active=false: no new tickets, assigned ones stay; a rename follows onto their tickets). Core
  `POST /tickets/support-team`, `PUT /tickets/support-team/{id}`, `GET ...?include_inactive=`. "Check" = read-only
  ServiceNow readiness per engineer (user by e-mail / user name, role itil, member of their tier's group: what
  ServiceNow's Assigned to needs); "Fix in ServiceNow" (confirmation dialog) creates missing users and adds a missing
  itil role / group membership. The assistant creates and assigns in one card (propose_new_ticket assignee).
  Demo login **support1 / demo** ("Sid Support", Keycloak role supervisor; realm file + seed DEMO_USERS + SUPPORT_TEAM,
  tier any); created live in Keycloak with kcadm on 2026-09-28.
- **Logs**: all Python services log JSON lines (`mes_common.logs`: ts, level, service, logger, msg, request_id, exc;
  `LOG_FORMAT=text` for plain); `install_request_id` middleware on every API (X-Request-ID in/out); inbox handlers log
  with `event:<event id>`; the agent sends `agent-run:<run id>` as X-Request-ID (so MES audit request_id links to
  the run). Compose: `loki` (:3100, 7-day retention), `alloy` (docker socket -> Loki, labels service + level),
  `grafana` (:3090, anonymous viewer, Loki datasource provisioned).
- Core for Phase 5: role `ai_support_agent` (Keycloak client `mes-support-agent`, secret in compose defaults),
  seeded user `service-account-mes-support-agent` ("AI Support Agent"); ticket activity origin `AI` (migration 0006)
  for ai_agent / ai_support_agent callers; `GET /tickets/search` (GIN full-text index); `POST
  /terminals/{id}/end-session`; ERP/ITSM adapter APIs accept the agent role.
- Admin UI: **AI support** page (status + health, counters, runs with step timeline and cost, approvals, operator
  chats, policy editor with kill switch/cost limits); AI panel in the ticket drawer (latest run, proposals, run
  again, the terminal chat); activity origin AI. Terminal: Get help -> **Talk to the support assistant** (streamed
  chat, status line, answer buttons, on-screen keyboard, fallback to the form when off/offline/paused).
- mes-mcp: 14 tools (tickets list/get/create/comment/escalate/severity/status, SLA overview, line status, equipment
  health, maintenance orders, work order, open NCs, genealogy trace); API key for MCP clients; Keycloak client
  `mes-mcp` (client credentials, realm role `ai_agent`, pre-seeded user scope). `.mcp.json` connects Claude Code.
- Events: + `mes.maintenance.v1`, `mes.tickets.v1`, `erp.maintenance-results.v1`, `erp.maintenance-orders.v1`,
  `itsm.ticket-results.v1`, `itsm.ticket-changes.v1` (contracts in /contracts/events).
- Admin UI: + **Maintenance** (machines, notifications, orders drawer with e-signed release, downtime, plans),
  **Support tickets** (severity/tier cards, SLA countdowns, triage drawer, links to the real records via
  `ticket.sn_url` / `jira_url` from the adapter, migration 0005), **Integrations** (renamed; + ServiceNow/Jira tab
  with connection cards and the sync log), technician flag on PINs, hatched maintenance windows on the
  schedule board.
- Terminal UI: + **Get help** (machine problem -> breakdown; MES problem -> ticket with impact -> P1..P4; quality
  -> defect), pause "Machine issue" -> call maintenance, machine-down banner / "machine back up", **technician
  mode** (maintenance tab: take notification, start, checklist, spare parts, e-signed sign-off).
- Seed (phase 4): spare parts (ERSA) with stock, 6 preventive plans (washer plan due at once), maint1 PIN 5555
  (technician), MCP service user.
- Tests (all passing): 58 core, 12 erp-adapter, 31 support-agent (scripted model, fake MES API; engineer chat, MCP client against a real MCP server, knowledge store), 13 data-mcp, 6 notifier, 13 itsm-adapter (4 UI-managed connections; 4 REST against stateful httpx.MockTransport fakes
  of the Table API and Jira v3 in `tests/rest_fakes.py`; 3 MCP mode against the headless mocks over real HTTP),
  1 mes-mcp, 8 Playwright (phase7.spec: analytics + notifications; maintenance spec checks ServiceNow/Jira only when connected; ai.spec: AI support page
  and the terminal assistant entry, accepting the fallback when the agent is off), `scripts/smoke_test.py`
  (Phases 1–4; ITSM part only when connected: creates, escalates and then resolves a smoke ticket in the real
  instance; prints which phase failed).
- Verified 2026-09-25 against the user's ServiceNow PDI (dev318787) and Jira Cloud site (project MES): setup check
  READY, smoke test and all Playwright specs pass (MES -> incident -> L3 Jira issue -> resolve). The PDI needed a
  read ACL on `sys_journal_field` for role itil (created by the user). Inbound verified by the user (INC0010002 put On
  Hold in ServiceNow -> MES-000020 ON_HOLD); the Jira token is the user's own account, so their own Jira edits are
  not echoed back. Tickets MES-000001..009 had mock-era INC/Jira refs; cleared (audited, actor itsm-cleanup).

**What's stubbed/mocked (not real yet):**
- SAP is `services/mock-sap`. `services/mock-itsm` = headless ServiceNow/Jira MCP servers for the adapter's MCP-mode
  tests only (no UI, not in compose). Real ITSM auth is basic auth (ServiceNow user/password, Jira e-mail + API
  token); no OAuth yet. Changes come back by polling (no inbound webhooks to a laptop).
- Badge/RFID = typed PIN. Opening stock is seeded; stock is not reconciled with SAP.
- Material-document idempotency marker in `MaterialDocumentHeaderText` (real S/4 limit 25 chars).
- SAP QM not integrated; 122 without PO reference; PM status change is a mock function import
  (`ChangeMaintOrderStatus`); PM orders have no operations/costs in SAP; spare parts come from MES stores only.
- No machine signals yet: downtime is operator-entered (by decision); runtime-based PM uses run times.
- Alert channels in the demo: Mailpit (no real mail unless SMTP_* set); Teams only with TEAMS_WEBHOOK_URL. The on-call
  rota is a static list (no shift calendar); no SMS/phone paging.
- No TimescaleDB hypertables yet.

**Known deviations from the original plan:**
- Ports: Postgres **5433**, admin UI **3080**, terminal **3081**, ITSM adapter 8070, MES MCP 8040.
- `line_id` only on entities that belong to one line; only plant-wide schedules.
- Schedule board shades only time when *all* lines are off shift (vis-timeline limitation).
- Front-end pinned to Mantine 8 / React Router 7 / TypeScript 5.9. MCP Python SDK is 2.x (`MCPServer`, not FastMCP).
- PIN hashing uses stdlib scrypt. Terminal sessions get operator rights, plus maintenance technician rights only
  when the PIN is flagged as technician (supervisor/admin rights never on a kiosk). One person per terminal: a new
  PIN login ends the previous session.
- Every confirmation posts its backflush and last-operation goods receipt in the same transaction.
- Cross-module FKs allowed where a module references another's rows (e.g. ticketing -> planning/org/quality/
  maintenance); modules still only import each other's `public.py`. No-FK references where the dependency would
  point the wrong way: `quality.inspection_lot.run_id`, `maintenance.downtime_event.run_id`,
  `inventory.movement.maintenance_order_id`.
- Serials assigned at goods receipt; serialised lots reworked all-good or all-scrap.
- A quality hold is released with the normal work-order resume (supervisor).
- Support tiers live in ServiceNow/Jira (assignment groups); MES users with `ticket:manage` (supervisor, admin,
  ai_agent) can also acknowledge/escalate/resolve in MES. All tickets start at L1 except automatic interface/data
  alarms (L2).
- Playwright runs with one worker (shared stack). The smoke test scopes the mock SAP "commit then timeout" fault to
  confirmations (`fault_scope`), restocks the seal kits it consumes and resets MILL-03 left over from an interrupted
  run.
- The ServiceNow/Jira mapping follows the common MES <-> ITSM pattern (correlation id, assignment groups, work
  notes, Jira for L3); it is not a copy of Siemens Opcenter's connector, whose internals weren't available.

- Incident 2026-09-25 during the Phase 5 rollout: on first start the agent's consumer replayed all old
  `mes.ticket.created` events and the (rejected) API key made every run fail; the failure handover escalated/noted
  19 old test tickets, and the ITSM adapter created INC0010004-INC0010011 and Jira MES-3..MES-6 in the user's
  instances before both workers were stopped; 11 further sync messages were set DEAD (not sent). Fixed: event age
  guard, setup errors pause instead of handing over. Cleaned up on the user's request: the 8 MES tickets cancelled
  (incidents INC0010004-11 now Canceled), Jira MES-3..MES-6 closed with a comment. Cause of the 401: a stale ANTHROPIC_API_KEY in the user's Windows *user* environment overrides `.env` in
  compose (user updated it to the current key on 2026-09-25; plain `docker compose up -d` works again). Verified
  live after that: ticket run (terminal session case, $0.15, correct no-fault diagnosis), operator chat (asks impact,
  P2, stays open after the ticket, adds details), duplicate detection across chat tickets.
- Playwright specs that need a dispatched order set it to priority 1 and re-plan (`reset_unlocked: true`): the shared
  stack accumulates test orders and the 14-day horizon fills up. `test_execution.py` timestamps now come from one fixed
  time (the old timing flakiness is fixed).
- Verified live (Phase 6): SAP rejection of a confirmation on an order TECO'd in SAP during a retry -> automatic L2
  ticket -> AI L2 run ($0.20): correct timeline and root cause (business decision, MES payload valid), precise note for
  the SAP planner, no retry and no L3 escalation. Log search from the agent container returns the adapter's lines.

**Open questions / decisions still needed:**
- Phase 8 scope confirmation (governance & hardening: per-action permissions, prompt-injection defence, OAuth,
  deployment). Full evaluation run (12 scenarios, ~$2-3) awaits the user's go; one scenario verified live ($0.12).
- Product idea from the L2 agent's RCA: warn/block in MES when SAP closes (TECO) an order that has an active run.
- Should releasing a quality hold require `quality:decide` instead of a supervisor?
- Should MES push rescheduled dates back to SAP (currently no)? Reversals (262/102, cancellation) not built.
- Should backflush be allowed to go negative (post and flag)?
- Real ServiceNow/Jira: OAuth instead of basic auth; whether L1/L2 groups map 1:1 to the plant's support teams;
  inbound webhooks (Business Rule / Jira webhook) once MES is reachable from the instances.
