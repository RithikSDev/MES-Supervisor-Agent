"""Phase 7, core side: SLA "at risk" warnings (75% of a target used, flagged once) and support analytics."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from mes_core.modules.ticketing.public import check_sla
from mes_core.shared.db.context import Actor
from mes_core.shared.db.session import Database
from tests.conftest import auth
from tests.helpers import plant_id

pytestmark = pytest.mark.db


async def test_sla_at_risk_is_flagged_once_before_the_breach(client: AsyncClient, db: Database) -> None:
    pid = await plant_id(client)
    # P1: response due 15 min after reporting; reported 12 min ago = 80% used, not breached yet
    t = (await client.post("/tickets", json={
        "plant_id": pid, "title": "Line 1 stopped: terminal frozen", "category": "MES_APP", "impact": "LINE_STOP",
        "occurred_at": (datetime.now(UTC) - timedelta(minutes=12)).isoformat()}, headers=auth("operator"))).json()
    for _ in range(2):
        async with db.unit_of_work(Actor.service("sla-monitor")) as s:
            await check_sla(s)
    d = (await client.get(f"/tickets/{t['id']}", headers=auth("supervisor"))).json()
    warnings = [a for a in d["activity"] if a["kind"] == "SLA_AT_RISK"]
    assert len(warnings) == 1 and "response target P1 at risk" in warnings[0]["text"]
    assert not [a for a in d["activity"] if a["kind"] == "SLA_BREACH"]


async def test_analytics_counts_sla_times_and_who_resolved(client: AsyncClient) -> None:
    pid = await plant_id(client)
    body = {"plant_id": pid, "category": "HARDWARE", "impact": "MINOR", "title": "Scanner drops out"}
    human = (await client.post("/tickets", json=body, headers=auth("operator"))).json()
    await client.post(f"/tickets/{human['id']}/status", json={"status": "RESOLVED", "note": "Cable replaced"},
                      headers=auth("supervisor"))
    ai = (await client.post("/tickets", json={**body, "title": "Printer blank labels"}, headers=auth("operator"))).json()
    await client.post(f"/tickets/{ai['id']}/status", json={"status": "RESOLVED", "note": "Ribbon reseated"},
                      headers=auth("support_agent"))
    before = (await client.get("/tickets/analytics", params={"plant_id": pid, "days": 7}, headers=auth("supervisor"))).json()
    approved = (await client.post("/tickets", json={**body, "title": "Scanner again"}, headers=auth("operator"))).json()
    await client.post(f"/tickets/{approved['id']}/status", headers=auth("supervisor"), json={
        "status": "RESOLVED", "note": "Cable reseated (proposed by the AI support agent, approved by Sam Supervisor)"})
    a = (await client.get("/tickets/analytics", params={"plant_id": pid, "days": 7}, headers=auth("supervisor"))).json()
    assert a["totals"]["raised"] >= 3 and a["totals"]["resolved"] >= 3
    assert a["resolved_by"]["AI"] == before["resolved_by"]["AI"] + 1  # approved AI proposal counts as AI
    assert a["resolved_by"]["AI"] >= 2 and a["resolved_by"]["PEOPLE"] >= 1
    p3 = next(x for x in a["by_severity"] if x["severity"] == "P3")
    assert p3["raised"] >= 2 and p3["mtta_min"] is not None and p3["mttr_h"] is not None
    assert a["by_day"][-1]["raised"] >= 2 and any(c["key"] == "HARDWARE" for c in a["by_category"])
    assert (await client.get("/tickets/analytics", params={"plant_id": pid}, headers=auth("stranger"))).status_code == 403
