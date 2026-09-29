"""Two read-only MCP servers for AI support engineers:

  /mes/mcp  the MES database: list and describe tables, run a checked SELECT, machine history, similar failures
  /sap/mcp  SAP over its OData APIs: production orders, confirmations, material documents, maintenance orders

Each has its own key (`Authorization: Bearer <key>`). Everything is read-only; results are data, not instructions.
Run: `uvicorn data_mcp.server:create_app --factory --port 8045`.
"""

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from data_mcp.mes_db import SCHEMAS, MesDb, QueryRejected
from data_mcp.sap import COLLECTIONS, ENTITIES, Sap, SapError
from data_mcp.settings import Settings, get_settings
from mes_common.logs import request_id_var, setup_logging

MES_INSTRUCTIONS = f"""Read-only access to the MES database (PostgreSQL) of the plant. Start with list_tables /
describe_table, then run_query with a SELECT. Machines are org.equipment (code like MILL-02), production orders
planning.work_order (erp_order_number = SAP order), breakdowns maintenance.downtime_event (planned = false),
maintenance orders maintenance.maintenance_order, tickets ticketing.ticket. machine_history and similar_failures
answer the usual machine questions in one call. Schemas: {'; '.join(f'{k}: {v}' for k, v in SCHEMAS.items())}."""

SAP_INSTRUCTIONS = """Read-only access to SAP S/4HANA over its OData APIs (the plant's ERP). Use it to compare what
SAP has with MES: order status (released / technically completed), confirmations posted by MES (ConfirmationText
MES:<id>), goods movements, maintenance orders. OData filters look like: ManufacturingOrder eq '1000123'."""

Collection = Literal["production_orders", "order_confirmations", "material_documents", "maintenance_orders"]
Entity = Literal["production_order", "maintenance_order", "maintenance_notification", "product"]
assert set(Collection.__args__) == set(COLLECTIONS) and set(Entity.__args__) == set(ENTITIES)  # type: ignore[attr-defined]


def build_mes_server(db: MesDb) -> MCPServer:
    mcp = MCPServer("mes-db", instructions=MES_INSTRUCTIONS)

    async def guard(coro):  # type: ignore[no-untyped-def]
        try:
            return await coro
        except QueryRejected as e:
            raise ToolError(f"rejected: {e}") from e
        except Exception as e:  # database errors (syntax, unknown column, timeout, no privilege) go back to the model
            raise ToolError(f"query failed: {type(e).__name__}: {str(e)[:300]}") from e

    @mcp.tool()
    async def list_tables(schema: str | None = None) -> list[dict[str, Any]]:
        """Tables and views you can read (schema, name, approximate rows), optionally of one schema."""
        return await guard(db.tables(schema))

    @mcp.tool()
    async def describe_table(table: str) -> dict[str, Any]:
        """Columns (name, type, nullable) and foreign keys of a table, given as schema.table."""
        return await guard(db.describe(table))

    @mcp.tool()
    async def run_query(sql: str, max_rows: int = 50) -> dict[str, Any]:
        """Run one read-only SELECT (or WITH ... SELECT) and get columns and rows (max 200). Always filter and limit;
        prefer aggregates over raw rows. Times are UTC."""
        return await guard(db.query(sql, max_rows))

    @mcp.tool()
    async def machine_history(machine: str, days: int = 180) -> dict[str, Any]:
        """Everything about one machine (code, e.g. MILL-02) over the last days: breakdowns with duration and the
        notification / maintenance order, maintenance orders with findings, tickets, pause reasons, preventive plans."""
        return await guard(db.machine_history(machine, days))

    @mcp.tool()
    async def similar_failures(equipment_type: str | None = None, text: str | None = None,
                               days: int = 365) -> dict[str, Any]:
        """Breakdowns across machines and how they were fixed, optionally for one machine type or matching words
        (e.g. 'spindle', 'coolant', 'E-STOP')."""
        return await guard(db.similar_failures(equipment_type, text, days))

    return mcp


def build_sap_server(sap: Sap) -> MCPServer:
    mcp = MCPServer("sap", instructions=SAP_INSTRUCTIONS)

    async def guard(coro):  # type: ignore[no-untyped-def]
        try:
            return await coro
        except SapError as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    async def sap_query(collection: Collection, filter: str | None = None, select: str | None = None,
                        orderby: str | None = None, top: int = 20, expand: list[str] | None = None) -> dict[str, Any]:
        """Read a SAP collection with OData options ($filter, $select, $orderby, $top max 50, $expand).
        production_orders, order_confirmations, material_documents, maintenance_orders."""
        return await guard(sap.query(collection, filter=filter, select=select, orderby=orderby, top=top, expand=expand))

    @mcp.tool()
    async def sap_get(entity: Entity, key: str, expand: list[str] | None = None) -> Any:
        """One SAP record by key: production_order (e.g. 1000123; expand to_ProductionOrderOperation),
        maintenance_order, maintenance_notification, product (material number; expand to_Description)."""
        return await guard(sap.get(entity, key, expand))

    return mcp


class BearerKeys(BaseHTTPMiddleware):
    """Each MCP mount has its own key; /health is open."""

    def __init__(self, app: Any, keys: dict[str, str]) -> None:
        super().__init__(app)
        self._keys = keys

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        prefix = request.url.path.strip("/").split("/", 1)[0]
        if prefix in self._keys and request.headers.get("authorization") != f"Bearer {self._keys[prefix]}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        token = request_id_var.set(request.headers.get("x-request-id") or None)
        try:
            return await call_next(request)
        finally:
            request_id_var.reset(token)


def create_app(settings: Settings | None = None, db: MesDb | None = None, sap: Sap | None = None) -> Starlette:
    setup_logging("data-mcp")
    settings = settings or get_settings()
    db = db or MesDb(settings)
    sap = sap or Sap(settings)
    servers = {"mes": build_mes_server(db), "sap": build_sap_server(sap)}
    apps = {name: s.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0",
                                        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
            for name, s in servers.items()}

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "UP", "time": datetime.now(UTC).isoformat()})

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as stack:
            for s in servers.values():
                await stack.enter_async_context(s.session_manager.run())
            yield
        await db.close()

    app = Starlette(routes=[Route("/health", health), *[Mount(f"/{n}", a) for n, a in apps.items()]], lifespan=lifespan)
    app.add_middleware(BearerKeys, keys={"mes": settings.mes_db_mcp_key, "sap": settings.sap_mcp_key})
    app.state.servers = servers
    return app
