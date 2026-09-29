"""Support team and assignment, ticket forecast, SLA breach / exit report, demo tickets.

Runs after the other ticket tests (it loads ~130 demo tickets into the shared test database) and before the reset.
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from mes_common.events import EventEnvelope, Topics
from mes_common.messaging import InboxConsumer
from mes_core.modules.erp_sync.handlers import HANDLERS, on_session
from mes_core.settings import get_settings
from mes_core.shared.db.session import Database
from mes_core.shared.messaging import messaging
from tests.conftest import auth
from tests.helpers import plant_id

pytestmark = pytest.mark.db


async def _raise(client: AsyncClient, **kw: object) -> dict:
    body = {"plant_id": await plant_id(client), "title": "Scanner offline", "category": "HARDWARE", "impact": "DEGRADED", **kw}
    r = await client.post("/tickets", json=body, headers=auth("supervisor"))
    assert r.status_code == 201, r.text
    return r.json()


async def _payloads(db: Database, event_type: str) -> list[dict]:
    async with db.read() as s:
        rows = (await s.execute(select(messaging.outbox.c.envelope).where(messaging.outbox.c.event_type == event_type)
                                .order_by(messaging.outbox.c.seq))).scalars().all()
    return [r["payload"] if isinstance(r, dict) else EventEnvelope.model_validate_json(r).payload for r in rows]


async def test_support_team_and_assignment(client: AsyncClient, db: Database) -> None:
    pid = await plant_id(client)
    team = (await client.get("/tickets/support-team", params={"plant_id": pid}, headers=auth("supervisor"))).json()
    assert {m["username"] for m in team} >= {"l1.priya", "l2.wei", "admin1"}
    t = await _raise(client)
    tid = t["id"]
    assert t["assignee_name"] is None
    # by name, e-mail or user name; operators can't assign
    r = await client.post(f"/tickets/{tid}/assign", json={"assignee": "Priya"}, headers=auth("supervisor"))
    assert r.status_code == 200 and r.json()["assignee_name"] == "Priya Nair"
    assert r.json()["activity"][-1]["kind"] == "ASSIGNED"
    assert (await client.post(f"/tickets/{tid}/assign", json={"assignee": "nobody"},
                              headers=auth("supervisor"))).json()["code"] == "unknown_assignee"
    assert (await client.post(f"/tickets/{tid}/assign", json={"assignee": "Priya"},
                              headers=auth("operator"))).status_code == 403
    ev = [p for p in await _payloads(db, "mes.ticket.updated") if p["ticket_id"] == tid][-1]
    assert ev["changes"] == ["assignee"] and ev["assignee"] == {"name": "Priya Nair", "username": "l1.priya",
                                                               "email": "priya.nair@mes.local"}
    mine = (await client.get("/tickets", params={"plant_id": pid, "assignee_id": r.json()["assignee_id"]},
                             headers=auth("supervisor"))).json()
    assert [x["id"] for x in mine] == [tid]
    # escalation hands it to the next tier's team: unassigned
    esc = (await client.post(f"/tickets/{tid}/escalate", json={"reason": "needs L2"}, headers=auth("supervisor"))).json()
    assert esc["tier"] == "L2" and esc["assignee_name"] is None
    # ServiceNow "Assigned to" comes back: a team member by e-mail, or someone outside the team by name
    consumer = InboxConsumer(messaging, db.session_factory, bootstrap_servers="unused", group_id="t", topics=[],
                             handlers=HANDLERS, on_session=on_session, max_attempts=1)
    for ext, assignee, expected in (("a1", {"name": "W. Chen", "user_name": "wchen", "email": "wei.chen@mes.local"}, "Wei Chen"),
                                    ("a2", {"name": "Vendor Tech", "user_name": "vtech", "email": None}, "Vendor Tech"),
                                    ("a3", {}, None)):
        env = EventEnvelope(event_type="itsm.ticket.changed", source="itsm-adapter", key=tid, payload={
            "ticket_id": tid, "origin": "SERVICENOW", "external_id": f"sn:x:{ext}", "author": "sn.engineer",
            "at": datetime.now(UTC).isoformat(), "assignee": assignee})
        await consumer.process(Topics.ITSM_TICKET_CHANGES, env.model_dump_json().encode())
        got = (await client.get(f"/tickets/{tid}", headers=auth("supervisor"))).json()
        assert got["assignee_name"] == expected
    # closed tickets can't be assigned
    await client.post(f"/tickets/{tid}/status", json={"status": "RESOLVED", "note": "done"}, headers=auth("supervisor"))
    assert (await client.post(f"/tickets/{tid}/assign", json={"assignee": "Tom"},
                              headers=auth("supervisor"))).json()["code"] == "ticket_closed"


async def test_demo_tickets_forecast_and_sla_report(client: AsyncClient, db: Database) -> None:
    pid = await plant_id(client)
    body = {"plant_id": pid}
    assert (await client.post("/admin/demo-tickets", json=body, headers=auth("supervisor"))).status_code == 403
    r = await client.post("/admin/demo-tickets", json=body, headers=auth("admin"))
    assert r.status_code == 403 and "ALLOW_DATA_RESET" in r.text
    s = get_settings()
    s.allow_data_reset = True
    try:
        r = await client.post("/admin/demo-tickets", json=body, headers=auth("admin"))
        assert r.status_code == 200, r.text
        again = await client.post("/admin/demo-tickets", json=body, headers=auth("admin"))
    finally:
        s.allow_data_reset = False
    res = r.json()
    assert 90 <= res["history"] <= 180 and res["open"] == 13 and 3 <= res["l3"] <= 5
    assert again.json()["code"] == "demo_loaded"

    tickets = (await client.get("/tickets", params={"plant_id": pid, "limit": 1000}, headers=auth("supervisor"))).json()
    demo = [t for t in tickets if t["reported_at"] < (datetime.now(UTC) - timedelta(days=3)).isoformat()]
    assert demo and all(t["status"] in ("RESOLVED", "CLOSED", "CANCELLED") for t in demo)
    assert sum(t["status"] == "CLOSED" for t in demo) > len(demo) / 2
    open_demo = [t for t in tickets if t["status"] in ("NEW", "ACKNOWLEDGED", "IN_PROGRESS", "ON_HOLD")]
    assert {t["severity"] for t in open_demo} >= {"P1", "P2", "P3", "P4"}
    assert len({t["assignee_name"] for t in open_demo if t["assignee_name"]}) >= 5
    # one event per demo ticket, final state, flagged
    created = [p for p in await _payloads(db, "mes.ticket.created") if p.get("demo")]
    assert len(created) == res["history"] + res["open"]
    assert {p["demo"] for p in created} == {"history", "current"}
    assert any(p["status"] == "CLOSED" for p in created) and any(p["assignee"] for p in created)

    for horizon in (7, 14, 61):
        f = (await client.get("/tickets/forecast", params={"plant_id": pid, "horizon_days": horizon},
                              headers=auth("supervisor"))).json()
        assert len(f["forecast"]) == horizon and f["enough_history"]
        assert f["totals"]["low"] <= f["totals"]["expected"] <= f["totals"]["high"]
        weeks = f["totals"]["per_week"]
        assert len(weeks) == -(-horizon // 7) and sum(w["days"] for w in weeks) == horizon
        assert all(w["low"] <= w["expected"] <= w["high"] for w in weeks)
        assert f["granularity"] == ("week" if horizon > 21 else "day")
        assert f["trend_per_week"] > 0  # the demo history rises slowly
        weekend = [p["expected"] for p in f["forecast"][:7] if p["weekday"] in ("Sat", "Sun")]
        weekday = [p["expected"] for p in f["forecast"][:7] if p["weekday"] not in ("Sat", "Sun")]
        assert max(weekend) < min(weekday)
        if horizon == 61:  # damped trend: later weeks grow less than the first ones; the range widens
            full = [w for w in weeks if w["days"] == 7]
            assert full[-1]["expected"] - full[-2]["expected"] < full[1]["expected"] - full[0]["expected"]
            assert full[-1]["high"] - full[-1]["low"] > full[0]["high"] - full[0]["low"]
        assert f["backtest"]["actual"] > 0 and {x["key"] for x in f["by_severity"]} == {"P1", "P2", "P3", "P4"}
    assert (await client.get("/tickets/forecast", params={"plant_id": pid, "horizon_days": 120},
                             headers=auth("supervisor"))).status_code == 422

    rep = (await client.get("/tickets/sla-report", params={"plant_id": pid, "days": 28}, headers=auth("supervisor"))).json()
    sm = rep["summary"]
    assert sm["breaches"] > 0 and sm["exits"] > 0 and sm["resolved"] > 0
    assert sm["breaches"] == sm["response_breaches"] + sm["resolution_breaches"] == len(rep["breaches"])
    assert 50 < sm["resolution_met_pct"] < 100
    assert all(b["late_min"] > 0 for b in rep["breaches"])
    assert {e["exit"] for e in rep["exits"]} <= {"RESOLVED", "CLOSED", "CANCELLED"}
    assert sum(x["exits"] for x in rep["by_severity"]) == sm["exits"]
    assert sum(x["breaches"] for x in rep["by_assignee"]) == sm["breaches"]
    week = (await client.get("/tickets/sla-report", params={"plant_id": pid, "days": 7}, headers=auth("supervisor"))).json()
    assert week["summary"]["exits"] < sm["exits"]


async def test_admin_maintains_the_support_team(client: AsyncClient) -> None:
    pid = await plant_id(client)
    body = {"plant_id": pid, "display_name": "Ayyappan Veeraiyan", "username": "Ayyappan.Veeraiyan",
            "email": "Ayyappan.V@example.com", "tier": "L2"}
    assert (await client.post("/tickets/support-team", json=body, headers=auth("operator"))).status_code == 403
    r = await client.post("/tickets/support-team", json=body, headers=auth("supervisor"))
    assert r.status_code == 201, r.text
    m = r.json()
    assert (m["username"], m["email"], m["active"], m["open_tickets"]) == ("ayyappan.veeraiyan", "ayyappan.v@example.com", True, 0)
    dup = await client.post("/tickets/support-team", json={**body, "display_name": "Someone Else"}, headers=auth("admin"))
    assert dup.json()["code"] == "duplicate"
    # assignable by name; a rename follows onto the ticket; off the team = no new tickets, the old one stays
    t = (await client.post("/tickets", json={"plant_id": pid, "title": "Report totals wrong", "category": "DATA",
                                             "impact": "MINOR"}, headers=auth("supervisor"))).json()
    assert (await client.post(f"/tickets/{t['id']}/assign", json={"assignee": "ayyappan"},
                              headers=auth("supervisor"))).json()["assignee_name"] == "Ayyappan Veeraiyan"
    r = await client.put(f"/tickets/support-team/{m['id']}", json={**body, "display_name": "Ayyappan V.", "active": False},
                         headers=auth("admin"))
    assert r.status_code == 200 and r.json()["active"] is False and r.json()["open_tickets"] == 1
    assert (await client.get(f"/tickets/{t['id']}", headers=auth("supervisor"))).json()["assignee_name"] == "Ayyappan V."
    team = (await client.get("/tickets/support-team", params={"plant_id": pid}, headers=auth("supervisor"))).json()
    assert "ayyappan.veeraiyan" not in {x["username"] for x in team}
    everyone = (await client.get("/tickets/support-team", params={"plant_id": pid, "include_inactive": True},
                                 headers=auth("supervisor"))).json()
    assert "ayyappan.veeraiyan" in {x["username"] for x in everyone}
    t2 = (await client.post("/tickets", json={"plant_id": pid, "title": "x", "category": "OTHER", "impact": "MINOR"},
                            headers=auth("supervisor"))).json()
    assert (await client.post(f"/tickets/{t2['id']}/assign", json={"assignee": "ayyappan"},
                              headers=auth("supervisor"))).json()["code"] == "unknown_assignee"


async def test_forecast_counts_plant_local_days(client: AsyncClient, db: Database) -> None:
    """A ticket raised at 00:30 in Kolkata (19:00 UTC the day before) counts on the plant's day."""
    from sqlalchemy import text

    from mes_core.shared.db.context import Actor

    pid = await plant_id(client)
    async with db.unit_of_work(Actor.service("test"), reason="plant time zone for the test") as s:
        await s.execute(text("UPDATE org.plant SET timezone = 'Asia/Kolkata' WHERE id = :p"), {"p": pid})
    try:
        f = (await client.get("/tickets/forecast", params={"plant_id": pid}, headers=auth("supervisor"))).json()
        assert f["timezone"] == "Asia/Kolkata"
        async with db.read() as s:
            rows = (await s.execute(text(
                "SELECT (reported_at AT TIME ZONE 'Asia/Kolkata')::date AS d, count(*) FROM ticketing.ticket "
                "WHERE plant_id = :p GROUP BY 1"), {"p": pid})).all()
        local = {r.d.isoformat(): r[1] for r in rows}
        for h in f["history"]:
            assert h["count"] == local.get(h["day"], 0), h
    finally:
        async with db.unit_of_work(Actor.service("test"), reason="plant time zone back") as s:
            await s.execute(text("UPDATE org.plant SET timezone = 'UTC' WHERE id = :p"), {"p": pid})
