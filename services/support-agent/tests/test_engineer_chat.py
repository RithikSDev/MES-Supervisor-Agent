"""Support engineer chat (admin UI): answers with interactive cards; changes only through confirmation cards that
run with the engineer's own token."""

import json

import httpx
import pytest
from conftest import PLANT, Container, FakeMes, Script, bearer
from sqlalchemy import select, text

from support_agent.db import run, ticket_override
from support_agent.engineer import sla_state, ticket_matches

pytestmark = pytest.mark.db
SUP = "sup-token"


def _events(body: str) -> list[dict]:
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


async def _chat(http: httpx.AsyncClient, token: str = SUP) -> dict:
    r = await http.post("/api/v1/agent/engineer/chats", json={"plant_id": PLANT}, headers=bearer(token))
    assert r.status_code == 201, r.text
    return r.json()


async def _say(http: httpx.AsyncClient, cid: str, text: str, token: str = SUP) -> list[dict]:
    r = await http.post(f"/api/v1/agent/engineer/chats/{cid}/messages", json={"text": text}, headers=bearer(token))
    return _events(r.text)


def test_ticket_filter_and_sla_state() -> None:
    base = {"number": "MES-1", "title": "Scanner", "status": "NEW", "severity": "P1", "tier": "L1", "category": "HARDWARE",
            "reported_at": "2026-09-25T10:00:00+00:00",
            "response": {"due": "2026-09-25T10:15:00+00:00", "met_at": None, "remaining_s": 120.0, "breached": False},
            "resolution": {"due": "2026-09-25T12:00:00+00:00", "met_at": None, "remaining_s": 6000.0, "breached": False}}
    assert sla_state(base) == "at_risk"  # 2 of 15 min left on the response target
    assert sla_state({**base, "resolution": {**base["resolution"], "breached": True}}) == "breached"
    assert ticket_matches(base, {"scope": "open", "severity": ["P1"], "sla": "at_risk", "text": "scan"})
    assert not ticket_matches({**base, "status": "RESOLVED"}, {"scope": "open"})
    assert not ticket_matches(base, {"scope": "open", "tier": ["L2", "L3"]})


async def test_engineer_asks_for_tickets_and_gets_a_live_card(env: tuple[Container, FakeMes], http: httpx.AsyncClient) -> None:
    c, fake = env
    p1 = fake.add_ticket(severity="P1", title="Line 1 stopped")
    fake.add_ticket(severity="P3")
    fake.add_ticket(severity="P1", status="RESOLVED")
    chat = await _chat(http)
    greeting = chat["transcript"][0]
    assert greeting["role"] == "assistant" and "Bring up all the open tickets" in greeting["suggestions"]

    model = Script([
        [("tool", "show_card", {"kind": "tickets", "severity": ["P1"], "title": "Open P1 tickets"})],
        [("text", f"One P1 is open: {p1['number']}, line 1 stopped.")],
    ])
    c.agent.model = model
    ev = _events((await http.post(f"/api/v1/agent/engineer/chats/{chat['id']}/messages",
                                  json={"text": "Bring up the open P1 tickets"}, headers=bearer(SUP))).text)
    card = next(e["card"] for e in ev if e["type"] == "card")
    assert card == {"kind": "tickets", "title": "Open P1 tickets", "filter": {"scope": "open", "severity": ["P1"]}}
    assert ev[-1]["type"] == "done"
    # the model got the counts back (not the rows), and was told not to repeat them
    result = model.seen[1]["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and '"shown":1' in result["content"] and "Do not repeat" in result["content"]
    assert "propose_change" in model.seen[0]["tools"] and "resolve_ticket" not in model.seen[0]["tools"]
    assert "<engineer_message>" in model.seen[0]["messages"][0]["content"]
    # every MES read ran as the engineer, not as the agent's service account
    reads = [x for x in fake.calls if x[2] == "/tickets"]
    assert reads and all(x[4] == SUP for x in reads)

    d = (await http.get(f"/api/v1/agent/engineer/chats/{chat['id']}", headers=bearer(SUP))).json()
    answer = d["transcript"][-1]
    assert [p["type"] for p in answer["parts"]] == ["card", "text"] and d["title"] == "Bring up the open P1 tickets"
    assert [x["id"] for x in (await http.get("/api/v1/agent/engineer/chats", headers=bearer(SUP))).json()][0] == chat["id"]
    # someone else's conversation stays private; operators have no engineer chat
    assert (await http.get(f"/api/v1/agent/engineer/chats/{chat['id']}", headers=bearer("admin-token"))).status_code == 403
    assert (await http.post("/api/v1/agent/engineer/chats", json={"plant_id": PLANT},
                            headers=bearer("op-user-token"))).status_code == 403
    async with c.session_factory() as s:
        kinds = {r.kind for r in (await s.execute(select(run).where(run.c.chat_session_id == chat["id"]))).all()}
    assert kinds == {"ENGINEER"}


async def test_changes_wait_for_the_engineer_and_run_with_their_token(
    env: tuple[Container, FakeMes], http: httpx.AsyncClient,
) -> None:
    c, fake = env
    t = fake.add_ticket(severity="P2")
    chat = await _chat(http)
    model = Script([
        [("tool", "propose_change", {"action": "escalate", "number": t["number"], "summary": "Needs the SAP team",
                                     "reason": "SAP rejects the posting"}),
         ("tool", "propose_change", {"action": "take_over", "number": t["number"]}),
         ("tool", "propose_change", {"action": "resolve", "number": t["number"]})],  # missing resolution
        [("text", "Two changes are ready for you to confirm.")],
    ])
    c.agent.model = model
    ev = await _say(http, chat["id"], f"Escalate {t['number']} to L2 and take it over")
    cards = [e["card"] for e in ev if e["type"] == "card"]
    assert [x["action"] for x in cards] == ["escalate", "take_over"] and all(x["status"] == "PENDING" for x in cards)
    assert "missing resolution" in model.seen[1]["messages"][-1]["content"][2]["content"]
    assert not [x for x in fake.calls if x[2].endswith("/escalate")]  # nothing changed yet

    esc, take = cards
    r = await http.post(f"/api/v1/agent/engineer/chats/{chat['id']}/actions/{esc['action_id']}", json={"confirm": True},
                        headers=bearer(SUP))
    assert r.status_code == 200 and r.json()["status"] == "EXECUTED" and r.json()["decided_by"] == "Sam Supervisor"
    call = next(x for x in fake.calls if x[2].endswith("/escalate"))
    assert call[4] == SUP and call[3]["reason"] == "Needs the SAP team" and fake.tickets[t["id"]]["tier"] == "L2"
    again = await http.post(f"/api/v1/agent/engineer/chats/{chat['id']}/actions/{esc['action_id']}",
                            json={"confirm": True}, headers=bearer(SUP))
    assert again.status_code == 409
    r = await http.post(f"/api/v1/agent/engineer/chats/{chat['id']}/actions/{take['action_id']}", json={"confirm": False},
                        headers=bearer(SUP))
    assert r.json()["status"] == "CANCELLED"
    async with c.session_factory() as s:
        assert (await s.execute(select(ticket_override).where(ticket_override.c.ticket_id == t["id"]))).first() is None

    # the next turn tells the model what happened to its cards (once)
    model.turns = [[("text", "Done: escalated; you kept it with the AI.")], [("text", "ok")]]
    await _say(http, chat["id"], "thanks")
    first = model.seen[2]["messages"][-1]["content"]
    assert "[Engineer confirmed] Escalate on" in first and "[Engineer cancelled] Take over from the AI" in first
    await _say(http, chat["id"], "anything else?")
    assert "[Engineer" not in model.seen[3]["messages"][-1]["content"]
    d = (await http.get(f"/api/v1/agent/engineer/chats/{chat['id']}", headers=bearer(SUP))).json()
    saved = [p["card"] for e in d["transcript"] for p in e.get("parts", []) if p["type"] == "card"]
    assert [x["status"] for x in saved] == ["EXECUTED", "CANCELLED"]


async def test_take_over_from_the_chat_stops_the_ai(env: tuple[Container, FakeMes], http: httpx.AsyncClient) -> None:
    c, fake = env
    t = fake.add_ticket()
    chat = await _chat(http)
    c.agent.model = Script([[("tool", "propose_change", {"action": "take_over", "number": t["number"]})],
                            [("text", "Ready to confirm.")]])
    card = next(e["card"] for e in await _say(http, chat["id"], "I'll take this one") if e["type"] == "card")
    r = await http.post(f"/api/v1/agent/engineer/chats/{chat['id']}/actions/{card['action_id']}", json={"confirm": True},
                        headers=bearer(SUP))
    assert r.json()["status"] == "EXECUTED"
    async with c.session_factory() as s:
        row = (await s.execute(select(ticket_override).where(ticket_override.c.ticket_id == t["id"]))).one()
    assert row.taken_by == "Sam Supervisor"


async def test_lookups_with_timestamps_do_not_break_the_answer(env: tuple[Container, FakeMes], http: httpx.AsyncClient) -> None:
    """Regression: tool results with datetimes (AI runs, waiting approvals) failed the whole turn (StatementError)."""
    c, fake = env
    t = fake.add_ticket(severity="P1", title="MILL-02 spindle alarm, machine stopped")
    async with c.session_factory() as s, s.begin():
        await c.agent.enqueue(s, ticket_id=t["id"], number=t["number"], plant_id=None, trigger="CREATED",
                              trigger_key=None)
    chat = await _chat(http)
    model = Script([
        [("tool", "get_ai_activity", {"number": t["number"]}), ("tool", "list_pending_approvals", {})],
        [("text", "The AI queued a run; nothing is waiting for approval.")],
    ])
    c.agent.model = model
    ev = await _say(http, chat["id"], "MILL-02 has a spindle alarm, what do we know?")
    assert "Sorry" not in "".join(e.get("delta", "") for e in ev) and ev[-1]["type"] == "done"
    results = model.seen[1]["messages"][-1]["content"]
    assert '"trigger":"CREATED"' in results[0]["content"] and not results[0].get("is_error")


async def test_create_edit_and_cancel_tickets_from_the_chat(env: tuple[Container, FakeMes], http: httpx.AsyncClient) -> None:
    c, fake = env
    existing = fake.add_ticket(title="TURN-01 spindle alarm", equipment_code="TURN-01")
    chat = await _chat(http)
    model = Script([
        [("tool", "propose_new_ticket", {"title": "TURN-01 spindle drive alarm 2031 again", "description": "Fuse blew",
                                         "category": "HARDWARE", "impact": "DEGRADED", "machine": "TURN-01",
                                         "order": "1000123", "reason": "repeat failure"}),
         ("tool", "propose_change", {"action": "edit_details", "number": existing["number"], "title": "TURN-01 spindle",
                                     "machine": "none", "reason": "wrong machine linked"}),
         ("tool", "propose_change", {"action": "cancel", "number": existing["number"], "note": "raised twice"}),
         ("tool", "propose_change", {"action": "cancel", "number": existing["number"]}),  # no reason: refused
         ("tool", "propose_change", {"action": "machine_back_up", "number": existing["number"], "machine": "TURN-01",
                                     "note": "fuse replaced, tested"})],
        [("text", "Four changes are ready.")],
    ])
    c.agent.model = model
    cards = [e["card"] for e in await _say(http, chat["id"], "raise a ticket for TURN-01 and clean up the old one")
             if e["type"] == "card"]
    new, edit, cancel, back_up = cards
    assert new["action"] == "create_ticket" and new["number"] == "new ticket"
    assert new["args"]["equipment_id"] == "eq-1" and new["args"]["work_order_id"] == "wo-1" and new["args"]["plant_id"] == PLANT
    assert [d["number"] for d in new["duplicates"]] == [existing["number"]]
    assert edit["args"] == {"title": "TURN-01 spindle", "reason": "wrong machine linked", "machine": "none",
                            "equipment_id": None}
    assert back_up["args"] == {"machine": "TURN-01", "note": "fuse replaced, tested"}
    assert "missing note" in model.seen[1]["messages"][-1]["content"][3]["content"]
    assert not [x for x in fake.calls if x[1] == "POST" and x[2] == "/tickets"]  # nothing created before confirming

    confirm = lambda card: http.post(f"/api/v1/agent/engineer/chats/{chat['id']}/actions/{card['action_id']}",  # noqa: E731
                                     json={"confirm": True}, headers=bearer(SUP))
    r = (await confirm(new)).json()
    created = next(x for x in fake.calls if x[1] == "POST" and x[2] == "/tickets")
    assert r["status"] == "EXECUTED" and created[4] == SUP and created[3]["equipment_id"] == "eq-1"
    assert r["number"] == r["result"]["number"] != "new ticket" and r["ticket_id"]
    r = (await confirm(edit)).json()
    details = next(x for x in fake.calls if x[2].endswith("/details"))
    assert r["status"] == "EXECUTED" and details[3] == {"title": "TURN-01 spindle", "equipment_id": None,
                                                        "reason": "wrong machine linked"} and details[4] == SUP
    r = (await confirm(cancel)).json()
    assert r["status"] == "EXECUTED" and fake.tickets[existing["id"]]["status"] == "CANCELLED"


async def test_reset_clears_ai_history_and_restores_defaults(env: tuple[Container, FakeMes], http: httpx.AsyncClient) -> None:
    c, fake = env
    await _chat(http)
    async with c.session_factory() as s, s.begin():
        await s.execute(text("UPDATE agent.policy SET mode = 'off' WHERE action = 'add_note'"))
    assert (await http.post("/api/v1/agent/admin/reset", headers=bearer("admin-token"))).status_code == 403
    c.settings.allow_data_reset = True
    try:
        assert (await http.post("/api/v1/agent/admin/reset", headers=bearer(SUP))).status_code == 403
        r = await http.post("/api/v1/agent/admin/reset", headers=bearer("admin-token"))
    finally:
        c.settings.allow_data_reset = False
    assert r.status_code == 200
    async with c.session_factory() as s:
        assert (await s.execute(text("SELECT count(*) FROM agent.engineer_chat"))).scalar() == 0
        assert (await s.execute(text("SELECT mode FROM agent.policy WHERE action = 'add_note'"))).scalar() == "auto"
        assert (await s.execute(text("SELECT count(*) FROM agent.knowledge_doc WHERE builtin"))).scalar() >= 8


def test_assignee_filter() -> None:
    t = {"number": "MES-1", "title": "x", "status": "NEW", "severity": "P2", "tier": "L1", "category": "HARDWARE",
         "assignee_name": "Wei Chen", "reported_at": "2026-09-25T10:00:00+00:00",
         "response": {"due": None, "met_at": None, "remaining_s": None, "breached": False},
         "resolution": {"due": None, "met_at": None, "remaining_s": None, "breached": False}}
    from support_agent.engineer import ticket_filter
    assert ticket_filter({"assignee": "others"}, "Sam Supervisor") == {"scope": "open", "assignee": "others",
                                                                      "me": "Sam Supervisor"}
    assert ticket_matches(t, ticket_filter({"assignee": "others"}, "Sam Supervisor"))
    assert not ticket_matches(t, ticket_filter({"assignee": "me"}, "Sam Supervisor"))
    assert ticket_matches(t, ticket_filter({"assignee": "me"}, "Wei Chen"))
    assert ticket_matches(t, {"scope": "open", "assignee": "wei"})
    assert not ticket_matches(t, {"scope": "open", "assignee": "unassigned"})
    assert ticket_matches({**t, "assignee_name": None}, {"scope": "open", "assignee": "unassigned"})
    assert not ticket_matches({**t, "assignee_name": None}, ticket_filter({"assignee": "others"}, "Sam Supervisor"))


async def test_forecast_report_team_assign_and_bulk_severity(env: tuple[Container, FakeMes], http: httpx.AsyncClient) -> None:
    c, fake = env
    a = fake.add_ticket(severity="P2", line_code="L1", assignee_name="Wei Chen")
    b = fake.add_ticket(severity="P2", line_code="L1")
    p3 = fake.add_ticket(severity="P3")
    chat = await _chat(http)
    model = Script([
        [("tool", "show_card", {"kind": "tickets", "assignee": "others", "title": "Assigned to others"}),
         ("tool", "show_card", {"kind": "forecast", "horizon_days": 61}),
         ("tool", "show_card", {"kind": "sla_report", "days": 7}),
         ("tool", "get_ticket_forecast", {"horizon_days": 7}),
         ("tool", "propose_change", {"action": "set_severity", "numbers": [a["number"], b["number"]], "severity": "P3",
                                     "reason": "Workaround in place on line 1"}),
         ("tool", "propose_change", {"action": "set_severity", "numbers": [a["number"], p3["number"]], "severity": "P3",
                                     "reason": "x"}),
         ("tool", "propose_change", {"action": "assign", "number": b["number"], "assignee": "priya"}),
         ("tool", "propose_change", {"action": "assign", "number": b["number"], "assignee": "Zed"})],
        [("text", "Ready.")],
    ])
    c.agent.model = model
    ev = await _say(http, chat["id"], "Assigned to others, forecast, SLA report, downgrade line 1 P2s, give MES-2 to Priya")
    cards = [e["card"] for e in ev if e["type"] == "card"]
    assert [x["kind"] for x in cards] == ["tickets", "forecast", "sla_report", "confirm", "confirm"]
    assert cards[0]["filter"] == {"scope": "open", "assignee": "others", "me": "Sam Supervisor"}
    assert cards[1]["horizon_days"] == 61 and cards[2]["days"] == 7
    results = [r["content"] for r in model.seen[1]["messages"][-1]["content"]]
    assert '"expected_p1_p2":3.2' in results[3].replace(" ", "")
    bulk, assign = cards[3], cards[4]
    assert bulk["ticket_id"] is None and [t["number"] for t in bulk["tickets"]] == [a["number"], b["number"]]
    assert "already P3" in results[5] and p3["number"] in results[5]
    assert assign["args"]["assignee"] == "Priya Nair" and assign["summary"] == "Assign to Priya Nair"
    assert "no support engineers match 'Zed'" in results[7]
    assert not [x for x in fake.calls if x[2].endswith(("/severity", "/assign"))]

    r = await http.post(f"/api/v1/agent/engineer/chats/{chat['id']}/actions/{bulk['action_id']}", json={"confirm": True},
                        headers=bearer(SUP))
    assert r.json()["status"] == "EXECUTED" and r.json()["result"] == {"done": [a["number"], b["number"]], "failed": []}
    assert fake.tickets[a["id"]]["severity"] == fake.tickets[b["id"]]["severity"] == "P3"
    assert all(x[4] == SUP for x in fake.calls if x[2].endswith("/severity"))
    r = await http.post(f"/api/v1/agent/engineer/chats/{chat['id']}/actions/{assign['action_id']}", json={"confirm": True},
                        headers=bearer(SUP))
    assert r.json()["status"] == "EXECUTED" and fake.tickets[b["id"]]["assignee_name"] == "Priya Nair"


async def test_create_and_assign_in_one_confirmation(env: tuple[Container, FakeMes], http: httpx.AsyncClient) -> None:
    c, fake = env
    chat = await _chat(http)
    c.agent.model = Script([
        [("tool", "propose_new_ticket", {"title": "Printer at packing offline", "description": "No labels since 10:00",
                                         "category": "HARDWARE", "impact": "DEGRADED", "assignee": "wei"})],
        [("text", "Ready for you to confirm.")],
    ])
    ev = await _say(http, chat["id"], "Create a ticket for the packing printer and assign it to Wei")
    card = next(e["card"] for e in ev if e["type"] == "card")
    assert card["action"] == "create_ticket" and card["args"]["assignee"] == "Wei Chen"
    r = await http.post(f"/api/v1/agent/engineer/chats/{chat['id']}/actions/{card['action_id']}", json={"confirm": True},
                        headers=bearer(SUP))
    assert r.json()["status"] == "EXECUTED" and r.json()["result"]["assignee"] == "Wei Chen"
    new = fake.created[-1]
    assert new["assignee_name"] == "Wei Chen" and "assignee" not in {k for k in new if k != "assignee_name"}
