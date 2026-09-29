"""Adapter tests run against a real HTTP mock SAP (uvicorn in a thread, so timeouts are real) and a throwaway Postgres."""

import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
import uvicorn
from testcontainers.postgres import PostgresContainer

from erp_adapter.container import Container, build_container
from erp_adapter.settings import Settings
from mock_sap.app import create_app
from mock_sap.store import Store

ADAPTER = Path(__file__).resolve().parents[1]
SAP_TIMEOUT_S = 1.0


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with PostgresContainer("postgres:17-alpine", username="integ", password="integ", dbname="mes_integration") as pg:
        url = f"postgresql+asyncpg://integ:integ@{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/mes_integration"
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ADAPTER,
            env={**os.environ, "MIGRATION_DATABASE_URL": url},
            check=True,
        )
        yield url


@pytest.fixture(scope="session")
def sap_store() -> Store:
    return Store(None)


@pytest.fixture(scope="session")
def sap_url(sap_store: Store) -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(sap_store, seed_orders=0), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("mock SAP did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


@pytest.fixture(autouse=True)
def reset_faults(sap_store: Store) -> None:
    f = sap_store.faults
    f.latency_ms = f.fail_next_503 = f.fail_next_lock = f.commit_then_timeout_next = 0
    f.hang_seconds = SAP_TIMEOUT_S * 3
    f.fault_scope = ""


@pytest_asyncio.fixture(scope="session")
async def container(database_url: str, sap_url: str) -> AsyncIterator[Container]:
    settings = Settings(
        database_url=database_url, sap_base_url=sap_url, sap_timeout_s=SAP_TIMEOUT_S, poll_page_size=5
    )
    c = build_container(settings)
    yield c
    await c.aclose()
