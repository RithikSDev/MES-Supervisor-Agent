"""Demo data reset helper: empty a service's own tables (the admin UI's "Reset demo data" calls every service)."""

from collections.abc import Iterable

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import AsyncSession


def own_tables(metadata: MetaData, keep: Iterable[str] = ()) -> list[str]:
    """Qualified names of the service's tables (incl. its outbox / inbox), except those in `keep` (schema.table)."""
    skip = set(keep)
    return [t.fullname for t in metadata.sorted_tables if t.fullname not in skip]


async def truncate(s: AsyncSession, tables: list[str]) -> None:
    if tables:
        await s.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
