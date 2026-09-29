"""Phase 4: support tickets (P1-P4 severity matrix, L1-L3 tiers, SLA clocks), sync events for ServiceNow/Jira and
changes coming back from them, automatic tickets for rejected SAP postings."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from mes_common.events import EventEnvelope, Topics
from mes_common.messaging import InboxConsumer
from mes_core.modules.erp_sync.handlers import HANDLERS, on_session
from mes_core.modules.ticketing.public import check_sla
from mes_core.shared.db.context import Actor
from mes_core.shared.db.session import Database
from mes_core.shared.messaging import messaging
from tests.conftest import auth
from tests.helpers import plant_id, terminal_login

pytestmark = pytest.mark.db


@pytest.fixture
def consumer(db: Database) -> InboxConsumer:
    return InboxConsumer(
        messaging, db.session_factory, bootstrap_servers="unused", group_id="test", topics=[], handlers=HANDLERS,
        on_session=on_session, max_attempts=1,
    )


async def _events(db: Database, event_type: str, ticket_id: str) -> list[dict]:
    async with db.read() as s:
        rows = (await s.execute(select(messaging.outbox.c.envelope).where(messaging.outbox.c.event_type == event_type)
                                .order_by(messaging.outbox.c.seq))).scalars().all()
    payloads = [r["payload"] if isinstance(r, dict) else EventEnvelope.model_validate_json(r).payload for r in rows]
    return [p for p in payloads if p["ticket_id"] == ticket_id]


async def _feed(consumer: InboxConsumer, topic: str, event_type: str, payload: dict) -> None:
    env = EventEnvelope(event_type=event_type, source="itsm-adapter", key=payload.get("ticket_id", "x"), payload=payload)
    await consumer.process(topic, env.model_dump_json().encode())


async def _raise(client: AsyncClient, headers: dict, **kw: object) -> dict:
    pid = await plant_id(client)
    body = {"plant_id": pid, "title": "Terminal frozen", "description": "Screen does not react", "category": "MES_APP",
            "impact": "LINE_STOP", **kw}
    r = await client.post("/tickets", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


async def test_severity_matrix_and_terminal_context(client: AsyncClient, db: Database) -> None:
    term = await terminal_login(client, "T-L2", "2222")
    t = await _raise(client, term, client_event_id=str(uuid4()))
    assert (t["severity"], t["suggested_severity"], t["tier"], t["status"], t["source"]) == ("P1", "P1", "L1", "NEW", "TERMINAL")
    assert t["terminal_code"] == "T-L2" and t["line_code"] == "L2" and t["number"].startswith("MES-")
    reported = datetime.fromisoformat(t["reported_at"])
    assert datetime.fromisoformat(t["response"]["due"]) - reported == timedelta(minutes=15)
    assert datetime.fromisoformat(t["resolution"]["due"]) - reported == timedelta(hours=2)
    created = await _events(db, "mes.ticket.created", t["id"])
    assert created and created[0]["line_code"] == "L2" and created[0]["severity"] == "P1" and created[0]["tier"] == "L1"

    degraded = await _raise(client, term, title="Scanner offline at station 4", category="HARDWARE", impact="DEGRADED")
    assert degraded["severity"] == "P2"
    access = await _raise(client, auth("supervisor"), title="Access for 5 new operators", category="ACCESS",
                          impact="MINOR")
    assert access["severity"] == "P4" and access["resolution"]["due"] is None  # scheduled
    override = await _raise(client, auth("supervisor"), title="Scrap report totals wrong", category="DATA", impact="MINOR",
                            severity="P2")
    assert (override["severity"], override["suggested_severity"]) == ("P2", "P3")
    # operators raise tickets but don't triage them
    assert (await client.post(f"/tickets/{t['id']}/escalate", json={"reason": "x"}, headers=term)).status_code == 403


async def test_workflow_escalation_and_sla_clock(client: AsyncClient, db: Database) -> None:
    t = await _raise(client, auth("supervisor"), impact="MINOR", title="Label printer skips duplicates")
    tid = t["id"]
    assert t["severity"] == "P3"
    sup = auth("supervisor")
    r = await client.post(f"/tickets/{tid}/status", json={"status": "ACKNOWLEDGED"}, headers=sup)
    assert r.status_code == 200 and r.json()["response"]["met_at"] and not r.json()["response"]["breached"]
    r = await client.post(f"/tickets/{tid}/status", json={"status": "ON_HOLD", "note": "waiting for printer vendor"}, headers=sup)
    assert r.json()["resolution"]["paused"] is True
    r = await client.post(f"/tickets/{tid}/status", json={"status": "IN_PROGRESS"}, headers=sup)
    assert r.json()["resolution"]["paused"] is False
    r = await client.post(f"/tickets/{tid}/status", json={"status": "CLOSED"}, headers=sup)
    assert r.status_code == 409 and r.json()["code"] == "invalid_transition"

    # L1 -> L2 -> L3 (a Jira issue will be opened), no further
    for tier in ("L2", "L3"):
        r = await client.post(f"/tickets/{tid}/escalate", json={"reason": f"needs {tier}"}, headers=sup)
        assert r.status_code == 200 and r.json()["tier"] == tier
    r = await client.post(f"/tickets/{tid}/escalate", json={"reason": "more"}, headers=sup)
    assert r.status_code == 409 and r.json()["code"] == "max_tier"
    ups = await _events(db, "mes.ticket.updated", tid)
    assert [u["tier"] for u in ups if "tier" in u["changes"]] == ["L2", "L3"]

    r = await client.post(f"/tickets/{tid}/severity", json={"severity": "P2", "reason": "affects all label printers"}, headers=sup)
    body = r.json()
    assert datetime.fromisoformat(body["resolution"]["due"]) >= datetime.fromisoformat(body["reported_at"]) + timedelta(hours=6)
    r = await client.post(f"/tickets/{tid}/status", json={"status": "RESOLVED"}, headers=sup)
    assert r.status_code == 422 and r.json()["code"] == "resolution_required"
    r = await client.post(f"/tickets/{tid}/status", json={"status": "RESOLVED", "note": "firmware 2.4 installed"}, headers=sup)
    assert r.json()["status"] == "RESOLVED" and r.json()["resolution"]["met_at"]
    r = await client.post(f"/tickets/{tid}/comments", json={"text": "confirmed by the line"}, headers=auth("operator"))
    assert r.status_code == 201
    kinds = [a["kind"] for a in r.json()["activity"]]
    assert kinds[0] == "CREATED" and "ESCALATED" in kinds and "SEVERITY" in kinds and kinds[-1] == "COMMENT"


async def test_servicenow_and_jira_changes_come_back(client: AsyncClient, consumer: InboxConsumer, db: Database) -> None:
    t = await _raise(client, auth("supervisor"), impact="DEGRADED", category="HARDWARE", title="Scanner drops")
    tid = t["id"]
    await _feed(consumer, Topics.ITSM_TICKET_RESULTS, "itsm.ticket.sync_result",
                {"ticket_id": tid, "status": "SYNCED", "sn_number": "INC0010042", "sn_sys_id": "a" * 32, "sn_state": "1",
                 "sn_group": "MES L1 Operations", "sn_url": "https://dev1.service-now.com/nav_to.do?uri=incident.do"})
    # an L1 engineer picks it up in ServiceNow, then the dedup id repeats (poller overlap)
    change = {"ticket_id": tid, "origin": "SERVICENOW", "external_id": "sn:aaaa:2026-09-24T10:00:00", "sn_state": "2",
              "author": "Lena L1", "at": datetime.now(UTC).isoformat()}
    await _feed(consumer, Topics.ITSM_TICKET_CHANGES, "itsm.ticket.changed", change)
    await _feed(consumer, Topics.ITSM_TICKET_CHANGES, "itsm.ticket.changed", {**change, "sn_state": "6"})
    d = (await client.get(f"/tickets/{tid}", headers=auth("supervisor"))).json()
    assert d["sn_number"] == "INC0010042" and d["status"] == "ACKNOWLEDGED" and d["response"]["met_at"]
    assert d["sn_url"].startswith("https://dev1.service-now.com/")
    # work note from ServiceNow, escalation to L3 there, then Jira says done
    await _feed(consumer, Topics.ITSM_TICKET_CHANGES, "itsm.ticket.changed",
                {"ticket_id": tid, "origin": "SERVICENOW", "external_id": "sn:note:1", "comment": "Driver crash, needs a code fix",
                 "author": "Lars L2", "tier": "L3", "sn_group": "MES L3 Core Dev"})
    await _feed(consumer, Topics.ITSM_TICKET_RESULTS, "itsm.ticket.sync_result",
                {"ticket_id": tid, "status": "SYNCED", "jira_key": "MES-7", "jira_status": "To Do",
                 "jira_url": "https://x.atlassian.net/browse/MES-7"})
    await _feed(consumer, Topics.ITSM_TICKET_CHANGES, "itsm.ticket.changed",
                {"ticket_id": tid, "origin": "JIRA", "external_id": "jira:MES-7:done", "jira_status": "Done",
                 "author": "Dana Dev", "comment": "Fixed in scanner-bridge 1.3.2"})
    d = (await client.get(f"/tickets/{tid}", headers=auth("supervisor"))).json()
    assert d["tier"] == "L3" and d["jira_key"] == "MES-7" and d["jira_status"] == "Done"
    assert d["jira_url"] == "https://x.atlassian.net/browse/MES-7" and d["sn_url"]  # kept when a result omits it
    origins = [(a["origin"], a["kind"]) for a in d["activity"]]
    assert ("SERVICENOW", "EXTERNAL") in origins and ("JIRA", "SYNC") in origins and ("JIRA", "EXTERNAL") in origins
    # an external tier change is echoed (origin SERVICENOW) so the adapter opens the Jira issue
    ups = await _events(db, "mes.ticket.updated", tid)
    assert any(u["origin"] == "SERVICENOW" and u["tier"] == "L3" for u in ups)


async def test_sla_breach_is_flagged_once(client: AsyncClient, db: Database) -> None:
    old = (datetime.now(UTC) - timedelta(minutes=40)).isoformat()
    t = await _raise(client, auth("supervisor"), title="MES down on line 2", occurred_at=old)
    async with db.unit_of_work(Actor.service("sla-monitor")) as s:
        n1 = await check_sla(s)
    async with db.unit_of_work(Actor.service("sla-monitor")) as s:
        await check_sla(s)
    assert n1 >= 1
    d = (await client.get(f"/tickets/{t['id']}", headers=auth("supervisor"))).json()
    assert d["response"]["breached"] and not d["resolution"]["breached"]
    assert [a["kind"] for a in d["activity"]].count("SLA_BREACH") == 1
    assert await _events(db, "mes.ticket.sla_breached", t["id"])


async def test_rejected_sap_posting_opens_one_l2_ticket(client: AsyncClient, consumer: InboxConsumer) -> None:
    pid = await plant_id(client)
    locs = {x["code"]: x for x in (await client.get("/inventory/locations", params={"plant_id": pid}, headers=auth("supervisor"))).json()}
    mat = (await client.get("/materials", params={"search": "CP-BOLT-M6"}, headers=auth("supervisor"))).json()[0]
    mv = (await client.post("/inventory/receipts", json={"plant_id": pid, "material_id": mat["id"], "quantity": "5",
                                                         "location_id": locs["0001"]["id"]}, headers=auth("supervisor"))).json()["id"]
    payload = {"movement_id": mv, "status": "FAILED", "error": "M7/021 Material X does not exist", "attempts": 1,
               "reconciled": False, "erp_material_document": None, "erp_document_year": None}
    env = EventEnvelope(event_type="erp.goods_movement.result", source="erp-adapter", key="1000:MAT:X", payload=payload)
    await consumer.process(Topics.ERP_GOODS_MOVEMENT_RESULTS, env.model_dump_json().encode())
    env2 = EventEnvelope(event_type="erp.goods_movement.result", source="erp-adapter", key="1000:MAT:X", payload=payload)
    await consumer.process(Topics.ERP_GOODS_MOVEMENT_RESULTS, env2.model_dump_json().encode())
    tickets = [t for t in (await client.get("/tickets", params={"plant_id": pid, "open_only": True}, headers=auth("supervisor"))).json()
               if t["integration_ref"] == f"sap:goods-movement:{mv}"]
    assert len(tickets) == 1
    t = tickets[0]
    assert (t["category"], t["severity"], t["tier"], t["source"]) == ("INTEGRATION", "P3", "L2", "AUTOMATIC")
    d = (await client.get(f"/tickets/{t['id']}", headers=auth("supervisor"))).json()
    assert any(a["kind"] == "COMMENT" and a["text"].startswith("Occurred again") for a in d["activity"])


async def test_details_are_corrected_audited_and_sent_to_servicenow(client: AsyncClient, db: Database) -> None:
    pid = await plant_id(client)
    t = await _raise(client, auth("operator"), title="Scanner", category="HARDWARE", impact="MINOR")
    eq = next(e for e in (await client.get("/maintenance/equipment", params={"plant_id": pid}, headers=auth("supervisor"))).json()
              if e["code"] == "MILL-03")
    body = {"title": "MILL-03 clamp pressure keeps dropping", "impact": "DEGRADED", "category": "OTHER",
            "equipment_id": eq["id"], "reason": "operator described the wrong device"}
    assert (await client.post(f"/tickets/{t['id']}/details", json=body, headers=auth("operator"))).status_code == 403
    r = await client.post(f"/tickets/{t['id']}/details", json=body, headers=auth("supervisor"))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["title"] == body["title"] and d["impact"] == "DEGRADED" and d["equipment_code"] == "MILL-03"
    assert d["severity"] == t["severity"] and d["suggested_severity"] != t["suggested_severity"]  # severity kept
    act = next(a for a in d["activity"] if a["kind"] == "DETAILS")
    assert "machine: - -> MILL-03" in act["text"] and act["data"]["title"]["from"] == "Scanner"
    ev = (await _events(db, "mes.ticket.updated", t["id"]))[-1]
    assert set(ev["changes"]) >= {"details", "title", "impact", "equipment_id"} and ev["comment"]["text"] == act["text"]
    # nothing changed: no activity, no event; a missing reason or an empty title is refused
    same = await client.post(f"/tickets/{t['id']}/details", json={"title": body["title"], "reason": "x"}, headers=auth("supervisor"))
    assert len([a for a in same.json()["activity"] if a["kind"] == "DETAILS"]) == 1
    assert (await client.post(f"/tickets/{t['id']}/details", json={"title": "x"}, headers=auth("supervisor"))).status_code == 422
    assert (await client.post(f"/tickets/{t['id']}/details", json={"title": None, "reason": "x"},
                              headers=auth("supervisor"))).status_code == 422

    # "delete" = cancel: the ticket stays in history, can't be edited any more
    r = await client.post(f"/tickets/{t['id']}/status", json={"status": "CANCELLED", "note": "raised by mistake"},
                          headers=auth("supervisor"))
    assert r.json()["status"] == "CANCELLED" and r.json()["closed_at"]
    late = await client.post(f"/tickets/{t['id']}/details", json={"title": "again", "reason": "x"}, headers=auth("supervisor"))
    assert late.status_code == 409
