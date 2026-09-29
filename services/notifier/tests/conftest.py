"""Notifier tests: a throwaway Postgres with the notifier's tables and in-memory senders that record deliveries."""

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from testcontainers.postgres import PostgresContainer

from mes_common.auth import Principal, StaticTokenVerifier
from mes_common.db import make_engine, make_session_factory
from notifier.alerts import ensure_defaults
from notifier.api import create_app
from notifier.settings import Settings

SERVICE = Path(__file__).resolve().parents[1]
PRINCIPALS = {
    "sup": Principal("s", "supervisor1", frozenset({"supervisor"}), "Sam Supervisor"),
    "admin": Principal("a", "admin1", frozenset({"admin"}), "Ada Admin"),
    "op": Principal("o", "operator1", frozenset({"operator"}), "Olga Operator"),
}


class Recorder:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def __call__(self, channel: str, people: list[dict[str, Any]], title: str, body: str,
                       alert: dict[str, Any]) -> list[tuple[str, bool, str | None]]:
        for p in people:
            self.sent.append({"channel": channel, "to": p["email"], "role": p["role"], "title": title,
                              "level": alert["level"], "id": alert["id"]})
        return [(p["email"], True, None) for p in people]


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with PostgresContainer("postgres:17-alpine", username="integ", password="integ", dbname="mes_integration") as pg:
        url = f"postgresql+asyncpg://integ:integ@{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/mes_integration"
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=SERVICE,
                       env={**os.environ, "MIGRATION_DATABASE_URL": url}, check=True)
        yield url


@pytest_asyncio.fixture
async def sf(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(database_url, pool_size=2)
    factory = make_session_factory(engine)
    async with factory() as s, s.begin():
        await s.execute(text("TRUNCATE notify.delivery, notify.notification, notify.rota, notify.rule CASCADE"))
    await ensure_defaults(factory)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def http(sf: async_sessionmaker[AsyncSession]) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(sf, StaticTokenVerifier(PRINCIPALS), Settings(smtp_host="", teams_webhook_url=""))
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c):
        yield c


def bearer(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}
