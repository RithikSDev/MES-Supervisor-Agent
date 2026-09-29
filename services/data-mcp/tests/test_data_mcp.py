"""data-mcp: the SQL check, the read-only database role (writes fail even past the check), SAP reads over OData
against the mock SAP, and the MCP endpoints with their keys."""

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from data_mcp.mes_db import MesDb, QueryRejected, check_sql
from data_mcp.sap import Sap, SapError
from data_mcp.server import create_app
from data_mcp.settings import Settings
from testcontainers.postgres import PostgresContainer

from mock_sap.app import create_app as mock_sap_app

READONLY_SQL = (Path(__file__).resolve().parents[3] / "infra" / "postgres" / "readonly.sql").read_text()


@pytest.mark.parametrize("sql", [
    "select * from org.equipment", "  WITH x AS (select 1) SELECT * FROM x;", "select 'delete' as word",
    "select updated_at, pin_locked_until from org.terminal -- comment; drop table x",
])
def test_select_queries_pass(sql: str) -> None:
    assert check_sql(sql)


@pytest.mark.parametrize(("sql", "why"), [
    ("delete from ticketing.ticket", "only SELECT"),
    ("select 1; delete from ticketing.ticket", "one statement"),
    ("with d as (delete from ticketing.ticket returning *) select * from d", "DELETE"),
    ("select pg_sleep(60)", "PG_SLEEP"),
    ("select * into t2 from org.equipment", "INTO"),
    ("select 1 /* ; */ ; update x set y = 1", "one statement"),
])
def test_writes_and_tricks_are_rejected(sql: str, why: str) -> None:
    with pytest.raises(QueryRejected, match=why):
        check_sql(sql)


@pytest.fixture(scope="module")
def pg() -> Iterator[str]:
    with PostgresContainer("postgres:17-alpine", username="postgres", password="pw", dbname="mes_core") as c:
        host, port = c.get_container_host_ip(), c.get_exposed_port(5432)
        yield f"{host}:{port}"


@pytest.fixture(scope="module")
def db_url(pg: str) -> str:
    import asyncio

    import asyncpg

    async def setup() -> None:
        con = await asyncpg.connect(f"postgresql://postgres:pw@{pg}/mes_core")
        await con.execute("""
            CREATE ROLE mes_owner; CREATE SCHEMA org; CREATE SCHEMA identity; CREATE SCHEMA audit;
            CREATE TABLE org.equipment (id uuid PRIMARY KEY, code text, name text, status text);
            INSERT INTO org.equipment VALUES (gen_random_uuid(), 'MILL-02', '5-axis mill 2', 'DOWN');
            CREATE TABLE identity.app_user (id uuid, username text, display_name text, active bool,
              last_login_at timestamptz, created_at timestamptz, email text);
            INSERT INTO identity.app_user VALUES (gen_random_uuid(), 'op1', 'Olga', true, now(), now(), 'olga@x');
            CREATE TABLE identity.terminal_session (id uuid, terminal_id uuid, user_id uuid, plant_id uuid, roles text[],
              started_at timestamptz, expires_at timestamptz, ended_at timestamptz, end_reason text, token_hash text);
            CREATE TABLE identity.operator_credential (id uuid, user_id uuid, plant_id uuid, pin_set_at timestamptz,
              terminal_roles text[], pin_hash text);
            CREATE TABLE identity.user_scope (id uuid, user_id uuid, plant_id uuid, line_id uuid);
            CREATE TABLE audit.audit_log (id bigint, occurred_at timestamptz, plant_id uuid, actor_name text,
              actor_type text, request_id text, schema_name text, table_name text, record_id text, action text,
              changed_fields text[], before jsonb, after jsonb, reason text, chain_hash text);
        """)
        for schema in ("masterdata", "planning", "execution", "inventory", "quality", "maintenance", "ticketing",
                       "messaging"):
            await con.execute(f"CREATE SCHEMA {schema}")
        sql = READONLY_SQL.replace(":'ro_password'", "'ro-pw'").replace(
            "SELECT format('CREATE ROLE mes_readonly LOGIN PASSWORD %L', 'ro-pw')\n"
            "WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mes_readonly') \\gexec",
            "CREATE ROLE mes_readonly LOGIN PASSWORD 'ro-pw';")
        await con.execute(sql)
        await con.close()

    asyncio.run(setup())
    return f"postgresql://mes_readonly:ro-pw@{pg}/mes_core"


@pytest.mark.db
async def test_read_only_role_reads_business_data_but_not_secrets_and_never_writes(db_url: str) -> None:
    db = MesDb(Settings(mes_db_url=db_url))
    try:
        r = await db.query("select code, status from org.equipment")
        assert r["rows"] == [["MILL-02", "DOWN"]] and r["columns"] == ["code", "status"]
        users = await db.query("select * from support_ro.app_user")
        assert "email" not in users["columns"] and users["rows"][0][1] == "op1"
        with pytest.raises(Exception, match="permission denied"):
            await db.query("select pin_hash from identity.operator_credential")
        con = await (await db.pool()).acquire()  # past the SQL check, straight on a connection: the database refuses
        with pytest.raises(Exception, match="read-only"):
            await con.execute("UPDATE org.equipment SET status = 'AVAILABLE'")
        await (await db.pool()).release(con)
        tables = {(t["schema"], t["name"]) for t in await db.tables()}
        assert ("org", "equipment") in tables and ("support_ro", "app_user") in tables
        assert not any(s == "identity" for s, _ in tables)
        d = await db.describe("org.equipment")
        assert [c["column"] for c in d["columns"]] == ["id", "code", "name", "status"]
    finally:
        await db.close()


def _sap() -> Sap:
    app = mock_sap_app(seed_orders=3)
    return Sap(Settings(sap_base_url="http://sap.test"),
               http=httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sap.test"))


async def test_sap_reads_named_collections_only() -> None:
    sap = _sap()
    r = await sap.query("production_orders", top=2)
    assert r["count"] == 2 and "__metadata" not in r["results"][0] and r["results"][0]["ManufacturingOrder"]
    order = r["results"][0]["ManufacturingOrder"]
    one = await sap.get("production_order", order, expand=["to_ProductionOrderOperation"])
    assert one["ManufacturingOrder"] == order and isinstance(one["to_ProductionOrderOperation"], list)
    p = await sap.get("product", "FG-1001", expand=["to_Description"])
    assert p["to_Description"][0]["ProductDescription"]
    with pytest.raises(SapError, match="unknown collection"):
        await sap.query("users")
    with pytest.raises(SapError, match="unknown expand"):
        await sap.get("product", "FG-1001", expand=["to_Anything"])
    with pytest.raises(SapError, match="404"):
        await sap.get("production_order", "999999")


async def test_mcp_endpoints_need_their_own_key() -> None:
    app = create_app(Settings(mes_db_mcp_key="k-mes", sap_mcp_key="k-sap"), sap=_sap())
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        hdr = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        assert (await c.post("/sap/mcp", json=body, headers=hdr)).status_code == 401
        assert (await c.post("/sap/mcp", json=body, headers={**hdr, "Authorization": "Bearer k-mes"})).status_code == 401
        r = await c.post("/sap/mcp", json=body, headers={**hdr, "Authorization": "Bearer k-sap"})
        assert r.status_code == 200 and {t["name"] for t in r.json()["result"]["tools"]} == {"sap_query", "sap_get"}
        r = await c.post("/mes/mcp", json=body, headers={**hdr, "Authorization": "Bearer k-mes"})
        assert {t["name"] for t in r.json()["result"]["tools"]} >= {"run_query", "machine_history", "describe_table"}
        assert (await c.get("/health")).json()["status"] == "UP"
