"""ServiceNow / Jira connections set up in the UI: stored encrypted, write-only secrets, tested before saving, picked
up without a restart, imported once from .env, and tickets staying on the instance they were created on."""

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from rest_fakes import FakeJira, FakeServiceNow
from sqlalchemy import select, text
from test_itsm import drain, msg, send, ticket

from itsm_adapter.api import create_app
from itsm_adapter.connections import validate_url
from itsm_adapter.container import Container, build_container
from itsm_adapter.db import connection, connection_change, ticket_link
from itsm_adapter.settings import Settings
from mes_common.auth import Principal, StaticTokenVerifier
from mes_common.ids import new_id

pytestmark = pytest.mark.db
GROUPS = ("MES L1 Operations", "MES L2 App & DB Engineering", "MES L3 Core Dev")
PRINCIPALS = {"admin": Principal("s-admin", "admin1", frozenset({"admin"}), "Ada Admin"),
              "sup": Principal("s-sup", "supervisor1", frozenset({"supervisor"}), "Sam Supervisor")}
SN_BODY = {"url": FakeServiceNow.base, "username": "mes.integration", "secret": "pw",
           "options": {"close_code": "Solution provided", "contact_type": "", "group_l1": GROUPS[0],
                       "group_l2": GROUPS[1], "group_l3": GROUPS[2]}}
JIRA_BODY = {"url": FakeJira.site, "username": "mes@example.com", "secret": "tok",
             "options": {"project": "MES", "issue_type_bug": "Bug", "issue_type_task": "Task"}}


def auth(who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {who}"}


class World:
    def __init__(self) -> None:
        self.sn, self.jira = FakeServiceNow(groups=GROUPS), FakeJira()

    def http(self, system: str) -> httpx.AsyncClient:
        return self.sn.client() if system == "servicenow" else self.jira.client()


@pytest_asyncio.fixture
async def world(settings: Settings) -> AsyncIterator[tuple[Container, httpx.AsyncClient, World]]:
    s = settings.model_copy(update={"itsm_mode": "rest", "itsm_secret_key": Fernet.generate_key().decode(),
                                    "servicenow_instance_url": "", "jira_site_url": ""})
    w = World()
    c = build_container(s, http=w.http)
    async with c.session_factory() as ses, ses.begin():
        await ses.execute(text("TRUNCATE itsm.connection, itsm.connection_change, itsm.ticket_link"))
        await ses.execute(text("UPDATE itsm.outbound_message SET status = 'SYNCED' WHERE status <> 'SYNCED'"))
    app = create_app(c, verifier=StaticTokenVerifier(PRINCIPALS))
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client):
        yield c, client, w
    await c.aclose()


def test_only_real_cloud_addresses() -> None:
    assert validate_url("servicenow", "https://dev1.service-now.com", False) is None
    assert "https://" in validate_url("servicenow", "http://dev1.service-now.com", False)  # type: ignore[operator]
    assert "not a ServiceNow" in validate_url("servicenow", "https://evil.example.com", False)  # type: ignore[operator]
    assert validate_url("servicenow", "https://itsm.corp.example.com", True) is None
    assert validate_url("jira", "https://acme.atlassian.net", False) is None
    assert "without a path" in validate_url("jira", "https://acme.atlassian.net/jira", False)  # type: ignore[operator]
    assert validate_url("jira", "http://localhost:8080", False) is None


async def test_admin_tests_and_saves_connections_secrets_stay_secret(
    world: tuple[Container, httpx.AsyncClient, World],
) -> None:
    c, http, w = world
    view = (await http.get("/api/v1/itsm/connections", headers=auth("sup"))).json()
    assert view["vault_ready"] and not view["servicenow"]["configured"] and view["managed_here"]
    assert not c.servicenow.configured

    # test before saving: a wrong password fails at sign-in, the right one passes every check
    bad = await http.post("/api/v1/itsm/connections/servicenow/test", json={**SN_BODY, "secret": "nope"}, headers=auth("admin"))
    assert bad.json()["ok"] is False and bad.json()["checks"][0]["name"] == "Sign in"
    good = (await http.post("/api/v1/itsm/connections/servicenow/test", json=SN_BODY, headers=auth("admin"))).json()
    assert good["ok"] and [x["name"] for x in good["checks"]] == ["Sign in", "Read work notes", "L1 group", "L2 group", "L3 group"]
    assert (await http.post("/api/v1/itsm/connections/servicenow/test", json=SN_BODY, headers=auth("sup"))).status_code == 403
    wrong = await http.post("/api/v1/itsm/connections/servicenow/test", json={**SN_BODY, "url": "https://evil.example.com"},
                            headers=auth("admin"))
    assert wrong.status_code == 422

    assert (await http.put("/api/v1/itsm/connections/servicenow", json=SN_BODY, headers=auth("sup"))).status_code == 403
    r = await http.put("/api/v1/itsm/connections/servicenow", json=SN_BODY, headers=auth("admin"))
    assert r.status_code == 200 and r.json()["secret_set"] and "pw" not in r.text and r.json()["secret_set_by"] == "Ada Admin"
    r = await http.put("/api/v1/itsm/connections/jira", json=JIRA_BODY, headers=auth("admin"))
    assert r.status_code == 200 and "tok" not in r.text
    # used at once, without a restart
    assert c.servicenow.configured and c.jira.configured and c.registry.cfg.sn_instance == "dev00000.service-now.com"
    async with c.session_factory() as s:
        stored = (await s.execute(select(connection.c.secret).where(connection.c.system == "servicenow"))).scalar_one()
    assert stored and "pw" not in stored and c.registry.vault.open(stored) == "pw"

    # edit without the secret keeps it; the history names fields, never values
    r = await http.put("/api/v1/itsm/connections/servicenow", json={**SN_BODY, "secret": None, "options": {
        **SN_BODY["options"], "contact_type": "integration"}}, headers=auth("admin"))
    assert r.status_code == 200 and r.json()["options"]["contact_type"] == "integration" and r.json()["version"] == 2
    view = (await http.get("/api/v1/itsm/connections", headers=auth("sup"))).json()
    assert view["history"][0]["changes"] == ["options.contact_type"] and "pw" not in str(view)
    ok = (await http.post("/api/v1/itsm/connections/servicenow/test", json={**SN_BODY, "secret": None},
                          headers=auth("admin"))).json()
    assert ok["ok"]  # the stored password was used

    tid = new_id()
    await send(c, ticket(tid), "mes.ticket.created")
    await drain(c)
    assert w.sn.by_correlation(f"MES:{tid}") is not None
    assert (await http.get("/api/v1/itsm/status", headers=auth("sup"))).json()["connections"][0]["reachable"]


async def test_switching_instance_keeps_old_tickets_where_they_are(world: tuple[Container, httpx.AsyncClient, World]) -> None:
    c, http, w = world
    await http.put("/api/v1/itsm/connections/servicenow", json=SN_BODY, headers=auth("admin"))
    old_tid = new_id()
    await send(c, ticket(old_tid), "mes.ticket.created")
    await drain(c)
    async with c.session_factory() as s:
        link = (await s.execute(select(ticket_link).where(ticket_link.c.ticket_id == old_tid))).one()
    assert link.sn_instance == "dev00000.service-now.com" and link.sn_number

    first = w.sn
    w.sn = FakeServiceNow(groups=GROUPS)  # another instance (same fake transport, new records)
    r = await http.put("/api/v1/itsm/connections/servicenow", json={**SN_BODY, "url": "https://dev11111.service-now.com"},
                       headers=auth("admin"))
    assert r.status_code == 200 and c.registry.cfg.sn_instance == "dev11111.service-now.com"
    async with c.session_factory() as s:
        note = (await s.execute(select(connection_change.c.note).order_by(connection_change.c.id.desc()).limit(1))).scalar()
    assert "stay there" in note

    upd = await send(c, ticket(old_tid, status="IN_PROGRESS", changes=["status"]), "mes.ticket.updated")
    new_tid = new_id()
    await send(c, ticket(new_tid), "mes.ticket.created")
    await drain(c)
    row = await msg(c, upd)
    assert row.status == "SKIPPED" and "dev00000.service-now.com" in row.last_error
    cids = lambda f: {i["correlation_id"] for i in f.incidents.values()}  # noqa: E731
    assert f"MES:{old_tid}" not in cids(w.sn)  # no duplicate on the new instance
    assert f"MES:{new_tid}" in cids(w.sn) and f"MES:{new_tid}" not in cids(first)


async def test_env_settings_are_imported_once(settings: Settings) -> None:
    key = Fernet.generate_key().decode()
    s = settings.model_copy(update={"itsm_mode": "rest", "itsm_secret_key": key,
                                    "servicenow_instance_url": FakeServiceNow.base, "servicenow_user": "mes.integration",
                                    "servicenow_password": "pw", "jira_site_url": ""})
    w = World()
    c = build_container(s, http=w.http)
    try:
        async with c.session_factory() as ses, ses.begin():
            await ses.execute(text("TRUNCATE itsm.connection, itsm.connection_change"))
        await c.registry.import_env_once()
        await c.registry.refresh()
        assert c.servicenow.configured and not c.jira.configured
        async with c.session_factory() as ses, ses.begin():
            await ses.execute(text("UPDATE itsm.connection SET username = 'changed.in.ui', version = version + 1"))
        await c.registry.import_env_once()  # already stored: .env is not read again
        await c.registry.refresh()
        assert c.servicenow.user == "changed.in.ui"  # type: ignore[attr-defined]
        # a wrong key can't read the stored password: reported, the connection is not used
        other = build_container(s.model_copy(update={"itsm_secret_key": Fernet.generate_key().decode()}), http=w.http)
        await other.registry.refresh()
        assert not other.servicenow.configured and "decrypted" in other.registry.error["servicenow"]
        await other.aclose()
    finally:
        await c.aclose()


async def test_reset_forgets_syncs_but_keeps_the_connections(world: tuple[Container, httpx.AsyncClient, World]) -> None:
    c, http, w = world
    await http.put("/api/v1/itsm/connections/servicenow", json=SN_BODY, headers=auth("admin"))
    tid = new_id()
    await send(c, ticket(tid), "mes.ticket.created")
    await drain(c)
    assert (await http.post("/api/v1/itsm/admin/reset", headers=auth("admin"))).status_code == 403  # off by default
    c.settings.allow_data_reset = True
    try:
        assert (await http.post("/api/v1/itsm/admin/reset", headers=auth("sup"))).status_code == 403
        r = await http.post("/api/v1/itsm/admin/reset", headers=auth("admin"))
    finally:
        c.settings.allow_data_reset = False
    assert r.status_code == 200 and r.json()["kept"] == ["itsm.connection", "itsm.connection_change"]
    async with c.session_factory() as s:
        assert (await s.execute(text("SELECT count(*) FROM itsm.outbound_message"))).scalar() == 0
        assert (await s.execute(text("SELECT count(*) FROM itsm.ticket_link"))).scalar() == 0
        assert (await s.execute(text("SELECT count(*) FROM itsm.connection"))).scalar() == 1
        wm = (await s.execute(text("SELECT count(*) FROM itsm.poll_state WHERE watermark > now() - interval '1 minute'"))).scalar()
    assert wm == 2  # only changes from now on are read back
    await c.registry.refresh(force=True)
    assert c.servicenow.configured
