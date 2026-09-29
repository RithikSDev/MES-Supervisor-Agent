"""Read-only access to the MES database.

Guards, from the outside in:
  1. `check_sql`: one statement, SELECT / WITH only, no data-changing or session-changing keywords;
  2. the query runs in a READ ONLY transaction with a statement timeout, wrapped in a row limit;
  3. the database role `mes_readonly` (infra/postgres/readonly.sql) can only SELECT business schemas and the
     support_ro views: no PIN hashes, device tokens, e-signatures; writes fail even if 1 and 2 were bypassed.
"""

import json
import logging
import re
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg

from data_mcp.settings import Settings

log = logging.getLogger(__name__)

SCHEMAS = {
    "org": "plant structure: plants, areas, lines, work centers, equipment (machines), terminals, shift patterns",
    "masterdata": "materials",
    "planning": "production orders from SAP (work_order, operations, components, status history), schedules",
    "execution": "shop-floor runs (operation_run), events (operation_event: start, pause with reason, resume, end), "
                 "production confirmations and their SAP posting status",
    "inventory": "storage locations, lots, stock, serial numbers, goods movements (SAP movement types)",
    "quality": "inspection plans / lots / results, nonconformances",
    "maintenance": "breakdown and maintenance notifications, maintenance orders (PM01/PM02) with findings and "
                   "labour, downtime events, preventive plans",
    "ticketing": "support tickets (severity, tier, SLA) and their activity log",
    "messaging": "event outbox, inbox and dead letters (events MES could not process)",
    "support_ro": "safe views: app_user, terminal_session, operator_pin (no secrets), user_scope, audit_log",
}

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|upsert|alter|drop|create|grant|revoke|truncate|copy|call|do|execute|prepare|"
    r"deallocate|listen|notify|unlisten|lock|vacuum|analyze|cluster|reindex|refresh|set|reset|"
    r"begin|commit|rollback|savepoint|discard|pg_sleep|pg_read_file|pg_read_binary_file|pg_ls_dir|"
    r"pg_terminate_backend|pg_cancel_backend|lo_import|lo_export|dblink|into)\b", re.IGNORECASE)


class QueryRejected(ValueError):
    pass


def _strip(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return sql.strip().rstrip(";").strip()


def check_sql(sql: str) -> str:
    """The query as it will run, or QueryRejected with the reason (shown to the model so it can fix the query)."""
    q = _strip(sql)
    if not q:
        raise QueryRejected("empty query")
    if ";" in re.sub(r"'(?:[^']|'')*'", "''", q):
        raise QueryRejected("one statement only")
    if not re.match(r"(?is)^\s*(select|with)\b", q):
        raise QueryRejected("only SELECT (or WITH ... SELECT) queries are allowed")
    literals_removed = re.sub(r"'(?:[^']|'')*'", "''", q)
    if m := FORBIDDEN.search(literals_removed):
        raise QueryRejected(f"'{m.group(1).upper()}' is not allowed: this is read-only access")
    return q


def _cell(v: Any, limit: int) -> Any:
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v) if v == v.to_integral_value() or abs(v) < 1e15 else str(v)
    if isinstance(v, UUID):
        return str(v)
    if isinstance(v, dict | list):
        s = json.dumps(v, default=str)
        return v if len(s) <= limit else s[:limit] + "…"
    if isinstance(v, str) and len(v) > limit:
        return v[:limit] + "…"
    if isinstance(v, bytes | memoryview):
        return f"<{len(v)} bytes>"
    return v


class MesDb:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._pool: asyncpg.Pool | None = None

    async def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self.s.mes_db_url, min_size=0, max_size=4, command_timeout=30,
                                                   server_settings={"default_transaction_read_only": "on",
                                                                    "application_name": "data-mcp"})
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def fetch(self, sql: str, *args: Any, max_rows: int | None = None) -> dict[str, Any]:
        """Run a read-only query (already checked or written by us) and return columns + rows."""
        limit = max(1, min(max_rows or self.s.max_rows, self.s.max_rows))
        started = time.monotonic()
        pool = await self.pool()
        async with pool.acquire() as con, con.transaction(readonly=True):
            await con.execute(f"SET LOCAL statement_timeout = {int(self.s.statement_timeout_ms)}")
            stmt = await con.prepare(f"SELECT * FROM ({sql}) AS q LIMIT {limit + 1}")
            rows = await stmt.fetch(*args)
            cols = [a.name for a in stmt.get_attributes()]
        ms = int((time.monotonic() - started) * 1000)
        out = [[_cell(v, self.s.max_cell_chars) for v in r.values()] for r in rows[:limit]]
        log.info("query %d rows in %d ms: %s", len(out), ms, " ".join(sql.split())[:300])
        return {"columns": cols, "rows": out, "row_count": len(out), "truncated": len(rows) > limit, "ms": ms}

    async def query(self, sql: str, max_rows: int = 100) -> dict[str, Any]:
        return await self.fetch(check_sql(sql), max_rows=max_rows)

    # ------------------------------------------------------------------ schema

    async def tables(self, schema: str | None = None) -> list[dict[str, Any]]:
        schemas = [schema] if schema else list(SCHEMAS)
        rows = await self.fetch(
            "SELECT n.nspname AS schema, c.relname AS name, CASE c.relkind WHEN 'v' THEN 'view' ELSE 'table' END AS kind, "
            "greatest(c.reltuples, 0)::bigint AS approx_rows, obj_description(c.oid) AS description "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = ANY($1::text[]) AND c.relkind IN ('r', 'p', 'v') AND NOT c.relispartition "
            "AND has_table_privilege(c.oid, 'SELECT') ORDER BY 1, 2", schemas, max_rows=500)
        return [dict(zip(rows["columns"], r, strict=True)) for r in rows["rows"]]

    async def describe(self, table: str) -> dict[str, Any]:
        schema, _, name = table.partition(".")
        if not name:
            raise QueryRejected("give the table as schema.table, e.g. maintenance.downtime_event")
        cols = await self.fetch(
            "SELECT a.attname AS column, format_type(a.atttypid, a.atttypmod) AS type, NOT a.attnotnull AS nullable, "
            "col_description(a.attrelid, a.attnum) AS description FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = $1 AND c.relname = $2 AND a.attnum > 0 AND NOT a.attisdropped "
            "AND has_table_privilege(c.oid, 'SELECT') ORDER BY a.attnum", schema, name, max_rows=200)
        if not cols["rows"]:
            raise QueryRejected(f"no table {table} (or no access to it); list_tables shows what exists")
        fks = await self.fetch(
            "SELECT pg_get_constraintdef(o.oid) AS foreign_key FROM pg_constraint o JOIN pg_class c ON c.oid = o.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE o.contype = 'f' AND n.nspname = $1 AND c.relname = $2",
            schema, name, max_rows=50)
        return {"table": table, "columns": [dict(zip(cols["columns"], r, strict=True)) for r in cols["rows"]],
                "foreign_keys": [r[0] for r in fks["rows"]]}

    # ------------------------------------------------------------------ ready-made queries

    async def machine_history(self, machine: str, days: int = 180) -> dict[str, Any]:
        days = max(1, min(days, 3650))
        eq = await self.fetch(
            "SELECT e.id, e.code, e.name, e.equipment_type, e.status, l.code AS line, w.code AS work_center "
            "FROM org.equipment e LEFT JOIN org.line l ON l.id = e.line_id LEFT JOIN org.work_center w ON w.id = e.work_center_id "
            "WHERE upper(e.code) = upper($1)", machine, max_rows=1)
        if not eq["rows"]:
            raise QueryRejected(f"no machine {machine}")
        m = dict(zip(eq["columns"], eq["rows"][0], strict=True))
        eid = UUID(m.pop("id"))
        since = f"now() - interval '{days} days'"
        downtime = await self.fetch(
            "SELECT d.started_at, d.ended_at, round(extract(epoch FROM coalesce(d.ended_at, now()) - d.started_at) / 60) "
            "AS minutes, d.planned, d.reason_code, d.description, d.reported_by_name, n.number AS notification, "
            "o.number AS maintenance_order FROM maintenance.downtime_event d "
            "LEFT JOIN maintenance.notification n ON n.id = d.notification_id "
            f"LEFT JOIN maintenance.maintenance_order o ON o.id = d.maintenance_order_id WHERE d.equipment_id = $1 AND d.started_at > {since} "
            "ORDER BY d.started_at DESC", eid, max_rows=50)
        orders = await self.fetch(
            "SELECT number, order_type, status, priority, description, findings, technician_name, labor_min, started_at, "
            f"completed_at FROM maintenance.maintenance_order WHERE equipment_id = $1 AND created_at > {since} "
            "ORDER BY created_at DESC", eid, max_rows=30)
        tickets = await self.fetch(
            "SELECT number, title, severity, status, category, reported_at, resolved_at, resolution_note "
            f"FROM ticketing.ticket WHERE equipment_id = $1 AND reported_at > {since} ORDER BY reported_at DESC", eid, max_rows=30)
        pauses = await self.fetch(
            "SELECT reason_code, count(*) AS times, max(occurred_at) AS last FROM execution.operation_event "
            f"WHERE equipment_id = $1 AND event_type = 'PAUSE' AND occurred_at > {since} GROUP BY reason_code ORDER BY 2 DESC",
            eid, max_rows=20)
        plans = await self.fetch(
            "SELECT name, interval_type, interval_days, interval_hours, last_done_at, active FROM maintenance.maintenance_plan "
            "WHERE equipment_id = $1", eid, max_rows=20)
        breakdowns = [r for r in downtime["rows"] if r[3] is False]
        mins = [r[2] for r in breakdowns if r[1] is not None and r[2] is not None]
        return {"machine": m, "days": days,
                "summary": {"breakdowns": len(breakdowns), "downtime_min": sum(mins),
                            "avg_repair_min": round(sum(mins) / len(mins)) if mins else None},
                "downtime": downtime, "maintenance_orders": orders, "tickets": tickets, "pause_reasons": pauses,
                "preventive_plans": plans}

    async def similar_failures(self, equipment_type: str | None = None, text: str | None = None,
                               days: int = 365) -> dict[str, Any]:
        """Breakdowns and their fixes across machines (optionally of one type, or matching words)."""
        days = max(1, min(days, 3650))
        where, args = [f"d.started_at > now() - interval '{days} days'", "NOT d.planned"], []
        if equipment_type:
            args.append(equipment_type)
            where.append(f"upper(e.equipment_type) = upper(${len(args)})")
        if text:
            args.append(f"%{text}%")
            where.append(f"(d.description ILIKE ${len(args)} OR d.reason_code ILIKE ${len(args)} OR o.findings::text ILIKE ${len(args)})")
        return await self.fetch(
            "SELECT e.code AS machine, e.equipment_type, d.started_at, round(extract(epoch FROM coalesce(d.ended_at, now()) "
            "- d.started_at) / 60) AS minutes, d.reason_code, d.description, o.number AS maintenance_order, o.findings, "
            "o.technician_name FROM maintenance.downtime_event d JOIN org.equipment e ON e.id = d.equipment_id "
            "LEFT JOIN maintenance.maintenance_order o ON o.id = d.maintenance_order_id "
            f"WHERE {' AND '.join(where)} ORDER BY d.started_at DESC", *args, max_rows=40)
