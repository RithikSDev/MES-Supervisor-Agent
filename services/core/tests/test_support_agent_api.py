"""Phase 5, core side of the AI support agent: its activity is marked AI, similar-ticket search, terminal context on
/me, and ending a stuck terminal session."""

import pytest
from httpx import AsyncClient

from tests.conftest import auth
from tests.helpers import plant_id, terminal_login

pytestmark = pytest.mark.db


async def test_agent_activity_is_marked_ai_and_search_finds_similar(client: AsyncClient) -> None:
    pid = await plant_id(client)
    body = {"plant_id": pid, "category": "HARDWARE", "impact": "DEGRADED"}
    old = (await client.post("/tickets", json={**body, "title": "Label printer prints blank labels at end of line",
                                                "description": "ribbon ok, labels come out empty"},
                             headers=auth("operator"))).json()
    await client.post(f"/tickets/{old['id']}/status", json={"status": "RESOLVED", "note": "Reloaded the label template"},
                      headers=auth("supervisor"))
    new = (await client.post("/tickets", json={**body, "title": "Blank labels from printer on line 2"},
                             headers=auth("operator"))).json()

    # the agent's notes and actions are origin AI (ServiceNow still gets them as MES work notes)
    r = await client.post(f"/tickets/{new['id']}/comments", json={"text": "Similar to an earlier ticket"},
                          headers=auth("support_agent"))
    assert r.status_code == 201
    r = await client.post(f"/tickets/{new['id']}/escalate", json={"reason": "needs L2"}, headers=auth("support_agent"))
    acts = {a["kind"]: a for a in r.json()["activity"]}
    assert acts["COMMENT"]["origin"] == "AI" and acts["COMMENT"]["actor_name"] == "AI Support Agent"
    assert acts["ESCALATED"]["origin"] == "AI" and acts["CREATED"]["origin"] == "MES"

    hits = (await client.get("/tickets/search", params={"plant_id": pid, "q": "printer blank labels", "scope": "resolved"},
                             headers=auth("support_agent"))).json()
    assert hits and hits[0]["id"] == old["id"] and hits[0]["resolution_note"] == "Reloaded the label template"
    by_number = (await client.get("/tickets/search", params={"plant_id": pid, "q": new["number"]},
                                  headers=auth("supervisor"))).json()
    assert by_number[0]["id"] == new["id"]
    assert (await client.get("/tickets/search", params={"plant_id": pid, "q": "zzzz qqqq"},
                             headers=auth("supervisor"))).json() == []


async def test_me_has_terminal_context_and_support_can_end_a_session(client: AsyncClient) -> None:
    pid = await plant_id(client)
    session = await terminal_login(client, "T-L1", "1111")
    me = (await client.get("/me", headers=session)).json()
    assert me["terminal_code"] == "T-L1" and me["line_code"] and me["terminal_id"]
    assert (await client.get("/me", headers=auth("supervisor"))).json()["terminal_id"] is None

    tid = me["terminal_id"]
    r = await client.post(f"/terminals/{tid}/end-session", json={"reason": "operator left it logged in"},
                          headers=auth("support_agent"))
    assert r.status_code == 204
    assert (await client.get("/me", headers=session)).status_code == 401  # the session is gone
    r = await client.post(f"/terminals/{tid}/end-session", json={"reason": "again"}, headers=auth("support_agent"))
    assert r.status_code == 409
    # operators can't end sessions
    other = await terminal_login(client, "T-L1", "1111")
    assert (await client.post(f"/terminals/{tid}/end-session", json={"reason": "x"}, headers=other)).status_code == 403
    audit = (await client.get("/audit", params={"plant_id": pid, "table_name": "identity.terminal_session", "limit": 5},
                              headers=auth("support_agent"))).json()["items"]
    assert any(a["reason"] == "operator left it logged in" for a in audit)


async def test_event_failures_are_listed_for_support(client: AsyncClient, db) -> None:  # type: ignore[no-untyped-def]
    from mes_core.shared.messaging import messaging

    async with db.session_factory() as s, s.begin():
        await s.execute(messaging.dead_letter.insert().values(topic="erp.work-orders.v1", key="1000:42",
                                                              event_type="erp.work_order.upserted", raw="{}",
                                                              error="retries exhausted: KeyError('plant')"))
    rows = (await client.get("/ops/event-failures", headers=auth("support_agent"))).json()
    assert rows[0]["error"].startswith("retries exhausted") and rows[0]["topic"] == "erp.work-orders.v1"
    assert (await client.get("/ops/event-failures", headers=auth("operator"))).status_code == 403
