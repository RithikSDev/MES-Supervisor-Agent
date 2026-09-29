"""ITSM adapter tests: the mock ServiceNow and Jira run as real HTTP servers (their MCP endpoints over Streamable
HTTP, uvicorn in threads) and a throwaway Postgres holds the adapter's tables."""

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

from itsm_adapter.container import Container, build_container
from itsm_adapter.settings import Settings
from mock_itsm import jira as jira_mod
from mock_itsm import servicenow as sn_mod

ADAPTER = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with PostgresContainer("postgres:17-alpine", username="integ", password="integ", dbname="mes_integration") as pg:
        url = f"postgresql+asyncpg://integ:integ@{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/mes_integration"
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ADAPTER,
                       env={**os.environ, "MIGRATION_DATABASE_URL": url}, check=True)
        yield url


def _serve(app) -> Iterator[str]:  # type: ignore[no-untyped-def]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("mock did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


@pytest.fixture(scope="session")
def servicenow() -> sn_mod.ServiceNow:
    return sn_mod.ServiceNow(None, public_url="http://sn.test")


@pytest.fixture(scope="session")
def jira() -> jira_mod.Jira:
    return jira_mod.Jira(None, public_url="http://jira.test")


@pytest.fixture(scope="session")
def sn_url(servicenow: sn_mod.ServiceNow) -> Iterator[str]:
    yield from _serve(sn_mod.create_app(servicenow))


@pytest.fixture(scope="session")
def jira_url(jira: jira_mod.Jira) -> Iterator[str]:
    yield from _serve(jira_mod.create_app(jira))


@pytest.fixture(autouse=True)
def reset_faults(servicenow: sn_mod.ServiceNow, jira: jira_mod.Jira) -> None:
    servicenow.state.faults.fail_next = 0
    jira.state.faults.fail_next = 0


@pytest.fixture(scope="session")
def settings(database_url: str, sn_url: str, jira_url: str) -> Settings:
    return Settings(database_url=database_url, itsm_mode="mcp", servicenow_mcp_url=f"{sn_url}/mcp",
                    servicenow_mcp_key="servicenow-mcp-key", jira_mcp_url=f"{jira_url}/mcp", jira_mcp_key="jira-mcp-key",
                    mcp_timeout_s=5, poll_overlap_s=120)


@pytest_asyncio.fixture(scope="session")
async def container(settings: Settings) -> AsyncIterator[Container]:
    c = build_container(settings)
    yield c
    await c.aclose()
