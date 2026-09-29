"""Demo data reset (runs last: it empties the shared test database and re-creates the demo plant)."""

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from mes_core.settings import get_settings
from mes_core.shared.db.session import Database
from tests.conftest import auth
from tests.helpers import plant_id

pytestmark = pytest.mark.db


async def test_reset_is_admin_only_and_off_unless_allowed(client: AsyncClient) -> None:
    body = {"confirm": "RESET"}
    assert (await client.post("/admin/reset-demo-data", json=body, headers=auth("supervisor"))).status_code == 403
    r = await client.post("/admin/reset-demo-data", json=body, headers=auth("admin"))
    assert r.status_code == 403 and "ALLOW_DATA_RESET" in r.text  # default: off


async def test_reset_empties_everything_and_recreates_the_demo_plant(client: AsyncClient, db: Database,
                                                                       seeded: dict[str, str]) -> None:
    pid = await plant_id(client)
    await client.post("/tickets", json={"plant_id": pid, "title": "before reset", "category": "OTHER", "impact": "MINOR"},
                      headers=auth("operator"))
    s = get_settings()
    s.allow_data_reset, s.reset_database_url, s.seed_plant_timezone = True, seeded["owner"], "UTC"
    try:
        assert (await client.post("/admin/reset-demo-data", json={"confirm": "yes"}, headers=auth("admin"))).status_code == 422
        r = await client.post("/admin/reset-demo-data", json={"confirm": "RESET"}, headers=auth("admin"))
    finally:
        s.allow_data_reset, s.reset_database_url = False, ""
    assert r.status_code == 200, r.text
    assert r.json()["tables"] > 30 and r.json()["reset_by"] == "Ada Admin"
    async with db.read() as ses:
        assert (await ses.execute(text("SELECT count(*) FROM ticketing.ticket"))).scalar() == 0
        assert (await ses.execute(text("SELECT count(*) FROM org.equipment"))).scalar() == 9
        # the audit trail restarted with the seed's own entries, chain intact
        assert (await ses.execute(text("SELECT count(*) FROM audit.audit_log WHERE reason LIKE 'demo seed%'"))).scalar() > 0
    new_pid = await plant_id(client)
    assert new_pid != pid
    t = (await client.post("/tickets", json={"plant_id": new_pid, "title": "after reset", "category": "OTHER",
                                             "impact": "MINOR"}, headers=auth("operator"))).json()
    assert t.get("number") == "MES-000001", t  # number ranges restart; cached users see the new plant at once
