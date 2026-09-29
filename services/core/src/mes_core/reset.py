"""Demo data reset: back to a freshly installed MES (admin UI -> account menu -> Reset demo data).

Empties every MES table (orders, runs, stock, quality, maintenance, tickets, users, the audit trail, event outbox and
inbox), restarts the number ranges and re-creates the demo plant with `seed`. Runs with the owner role
(RESET_DATABASE_URL, the migration role) because the runtime role may not truncate, and only when ALLOW_DATA_RESET is
on (demo / test installations). The other services reset their own data through their own endpoints; the admin UI
calls them in order.
"""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text

from mes_core.modules.ticketing import public as ticketing
from mes_core.seed import seed
from mes_core.settings import get_settings
from mes_core.shared.db.session import Database
from mes_core.shared.permissions import Perm
from mes_core.shared.users import CurrentUser
from mes_core.web.deps import request_id, require

log = logging.getLogger(__name__)
router = APIRouter(tags=["admin"])
# schemas the MES owns (one per module, plus audit and messaging); `public` only holds alembic_version
SKIP_SCHEMAS = ("public", "information_schema", "support_ro")


class ResetIn(BaseModel):
    confirm: str


async def reset_database(url: str) -> dict[str, int]:
    """Truncate every MES table and restart its sequences (one transaction)."""
    db = Database(url, pool_size=1)
    try:
        async with db.engine.begin() as con:
            tables = (await con.execute(text(
                "SELECT format('%I.%I', n.nspname, c.relname) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_roles r ON r.oid = n.nspowner WHERE c.relkind IN ('r', 'p') AND NOT c.relispartition "
                "AND r.rolname = current_user AND n.nspname <> ALL(:skip)"), {"skip": list(SKIP_SCHEMAS)})).scalars().all()
            await con.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
            seqs = (await con.execute(text(
                "SELECT format('%I.%I', schemaname, sequencename) FROM pg_sequences WHERE sequenceowner = current_user "
                "AND schemaname <> ALL(:skip)"), {"skip": list(SKIP_SCHEMAS)})).scalars().all()
            for s in seqs:
                await con.execute(text(f"ALTER SEQUENCE {s} RESTART"))
            await con.execute(text("INSERT INTO audit.seal_state (id, last_seq, last_hash) VALUES (1, 0, repeat('0', 64))"))
        return {"tables": len(tables), "sequences": len(seqs)}
    finally:
        await db.dispose()


@router.post("/admin/reset-demo-data")
async def reset_demo_data(body: ResetIn, request: Request,
                          user: Annotated[CurrentUser, require(Perm.SYSTEM_RESET)]) -> dict:
    s = get_settings()
    if not s.allow_data_reset:
        raise HTTPException(403, "data reset is switched off on this installation (ALLOW_DATA_RESET)")
    if body.confirm != "RESET":
        raise HTTPException(422, "type RESET to confirm")
    log.warning("DEMO DATA RESET requested by %s: emptying all MES tables", (user.display_name or user.username))
    counts = await reset_database(s.reset_database_url or s.migration_database_url)
    db = Database(s.reset_database_url or s.migration_database_url, pool_size=1)
    try:
        await seed(db, s.seed_plant_timezone, s)
    finally:
        await db.dispose()
    # signed-in users are cached with their MES user id and plants: those rows were just re-created
    request.app.state.user_cache.clear()
    log.warning("demo data reset done by %s: %s", (user.display_name or user.username), counts)
    return {"reset_by": (user.display_name or user.username), **counts}


class DemoTicketsIn(BaseModel):
    plant_id: UUID


@router.post("/admin/demo-tickets")
async def load_demo_tickets(body: DemoTicketsIn, request: Request,
                            user: Annotated[CurrentUser, require(Perm.SYSTEM_RESET)]) -> dict:
    """Eight weeks of support-ticket history plus open tickets for demos (forecast, SLA reports, assignment). They go
    to ServiceNow / Jira like any ticket. Refused when already loaded."""
    s = get_settings()
    if not s.allow_data_reset:
        raise HTTPException(403, "demo data is switched off on this installation (ALLOW_DATA_RESET)")
    user.ensure_plant(body.plant_id)
    db: Database = request.app.state.db
    async with db.unit_of_work(user.actor, request_id=request_id(request),
                               reason="demo support tickets loaded") as session:
        result = await ticketing.load_demo_tickets(session, body.plant_id)
    log.warning("demo tickets loaded by %s: %s", user.display_name or user.username, result)
    return result
