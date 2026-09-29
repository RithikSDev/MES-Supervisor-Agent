"""Test fixtures: a throwaway TimescaleDB container initialised exactly like the compose one, migrated and seeded.

API tests run in-process (httpx ASGITransport) with a static token verifier. No Kafka: events are asserted in the
outbox table, and inbound ERP events are fed straight into the InboxConsumer.
"""

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from testcontainers.postgres import PostgresContainer

from mes_common.auth import Principal, StaticTokenVerifier
from mes_core.main import create_app
from mes_core.seed import seed
from mes_core.settings import Settings
from mes_core.shared.db.session import Database

ROOT = Path(__file__).resolve().parents[3]
CORE = Path(__file__).resolve().parents[1]
INIT_SCRIPT = ROOT / "infra" / "postgres" / "init" / "10-mes.sh"

PRINCIPALS = {
    "admin": Principal("sub-admin", "admin1", frozenset({"admin"}), "Ada Admin"),
    "supervisor": Principal("sub-sup", "supervisor1", frozenset({"supervisor"}), "Sam Supervisor"),
    "operator": Principal("sub-op", "operator1", frozenset({"operator"}), "Olga Operator"),
    "quality": Principal("sub-q", "quality1", frozenset({"quality_engineer"}), "Quinn Quality"),
    "stranger": Principal("sub-x", "stranger", frozenset({"supervisor"}), "No Scope"),
    "support_agent": Principal("sub-sa", "service-account-mes-support-agent", frozenset({"ai_support_agent"}),
                               "AI Support Agent"),
}


@pytest.fixture(scope="session")
def pg() -> dict[str, str]:
    container = (
        PostgresContainer("timescale/timescaledb:latest-pg17", username="postgres", password="postgres", dbname="postgres")
        .with_env("MES_OWNER_PASSWORD", "mes_owner")
        .with_env("MES_APP_PASSWORD", "mes_app")
        .with_env("INTEG_PASSWORD", "integ")
        .with_env("KEYCLOAK_DB_PASSWORD", "keycloak")
        .with_volume_mapping(str(INIT_SCRIPT), "/docker-entrypoint-initdb.d/10-mes.sh", "ro")
    )
    container.start()
    try:
        host, port = container.get_container_host_ip(), container.get_exposed_port(5432)
        urls = {
            "owner": f"postgresql+asyncpg://mes_owner:mes_owner@{host}:{port}/mes_core",
            "app": f"postgresql+asyncpg://mes_app:mes_app@{host}:{port}/mes_core",
            "super": f"postgresql+asyncpg://postgres:postgres@{host}:{port}/mes_core",
            "integ": f"postgresql+asyncpg://integ:integ@{host}:{port}/mes_integration",
        }
        _wait_for_init(urls["owner"])
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=CORE,
            env={**os.environ, "MIGRATION_DATABASE_URL": urls["owner"]},
            check=True,
        )
        yield urls
    finally:
        container.stop()


def _wait_for_init(url: str) -> None:
    """The entrypoint restarts Postgres after init scripts; wait until our role can connect."""
    import asyncio
    import time

    from sqlalchemy.ext.asyncio import create_async_engine

    async def probe() -> None:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as c:
                await c.execute(text("SELECT 1"))
        finally:
            await engine.dispose()

    deadline = time.monotonic() + 60
    while True:
        try:
            asyncio.run(probe())
            return
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


@pytest_asyncio.fixture(scope="session")
async def seeded(pg: dict[str, str]) -> dict[str, str]:
    owner = Database(pg["owner"], pool_size=1)
    await seed(owner, "UTC")
    await owner.dispose()
    return pg


@pytest_asyncio.fixture(scope="session")
async def db(seeded: dict[str, str]) -> AsyncIterator[Database]:
    d = Database(seeded["app"], pool_size=5)
    yield d
    await d.dispose()


@pytest_asyncio.fixture(scope="session")
async def superdb(seeded: dict[str, str]) -> AsyncIterator[Database]:
    d = Database(seeded["super"], pool_size=2)
    yield d
    await d.dispose()


@pytest_asyncio.fixture(scope="session")
async def client(db: Database) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(Settings(redis_url=""), db=db, verifier=StaticTokenVerifier(PRINCIPALS))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test/api/v1") as c:
            yield c


@pytest_asyncio.fixture
async def no_quality_plans(db: Database) -> AsyncIterator[None]:
    """Phase 2 flows (no inspections): deactivate the seeded inspection plans for the duration of a test."""
    from mes_core.shared.db.context import Actor

    async def set_active(active: bool) -> None:
        async with db.unit_of_work(Actor.service("test")) as s:
            await s.execute(text("UPDATE quality.inspection_plan SET active = :a"), {"a": active})

    await set_active(False)
    yield
    await set_active(True)


def auth(who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {who}"}
