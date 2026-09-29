"""Units of work. Every write goes through `Database.unit_of_work`, which:

1. opens a transaction,
2. publishes the actor/request/reason into transaction-local Postgres settings (`SET LOCAL` via set_config),
   which the audit trigger reads. A write without them is rejected by the database itself,
3. commits before returning control, so API handlers answer only after the data is durable.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from mes_common.db import make_engine, make_session_factory
from mes_core.shared.db.context import Actor, reset_actor, set_actor

_SET_CONTEXT = text(
    "SELECT set_config('mes.actor_id', :actor_id, true),"
    " set_config('mes.actor_name', :actor_name, true),"
    " set_config('mes.actor_type', :actor_type, true),"
    " set_config('mes.request_id', :request_id, true),"
    " set_config('mes.reason', :reason, true),"
    " set_config('mes.signature_id', '', true)"
)


class Database:
    def __init__(self, url: str, *, pool_size: int = 10) -> None:
        self.engine: AsyncEngine = make_engine(url, pool_size=pool_size)
        self.session_factory: async_sessionmaker[AsyncSession] = make_session_factory(self.engine)

    @asynccontextmanager
    async def read(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as s:
            yield s

    @asynccontextmanager
    async def unit_of_work(
        self, actor: Actor, *, request_id: str | None = None, reason: str | None = None
    ) -> AsyncIterator[AsyncSession]:
        token = set_actor(actor)
        try:
            async with self.session_factory() as s, s.begin():
                await apply_audit_context(s, actor, request_id=request_id, reason=reason)
                yield s
        finally:
            reset_actor(token)

    async def dispose(self) -> None:
        await self.engine.dispose()


async def apply_audit_context(
    s: AsyncSession, actor: Actor, *, request_id: str | None = None, reason: str | None = None
) -> None:
    await s.execute(
        _SET_CONTEXT,
        {
            "actor_id": actor.id,
            "actor_name": actor.name,
            "actor_type": actor.type,
            "request_id": request_id or "",
            "reason": reason or "",
        },
    )


async def set_change_reason(s: AsyncSession, reason: str | None, signature_id: UUID | None = None) -> None:
    """Attach a reason (and optionally an e-signature) to all audit rows written after this point in the tx."""
    await s.execute(
        text("SELECT set_config('mes.reason', :r, true), set_config('mes.signature_id', :sig, true)"),
        {"r": reason or "", "sig": str(signature_id) if signature_id else ""},
    )
