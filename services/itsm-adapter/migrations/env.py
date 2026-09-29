import asyncio
import os

from alembic import context
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from itsm_adapter.db import metadata
from itsm_adapter.settings import get_settings

OWNED_SCHEMAS = {"itsm", "itsm_messaging"}


def include_name(name, type_, parent_names):  # type: ignore[no-untyped-def]
    if type_ == "schema":
        return name in OWNED_SCHEMAS
    return True


def do_run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(
        connection=connection,
        target_metadata=metadata,
        include_schemas=True,
        include_name=include_name,
        version_table_schema="itsm",
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run() -> None:
    url = os.environ.get("MIGRATION_DATABASE_URL") or get_settings().database_url
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        for schema in sorted(OWNED_SCHEMAS):
            await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
        await conn.commit()
        await conn.run_sync(do_run_migrations)
    await engine.dispose()


asyncio.run(run())
