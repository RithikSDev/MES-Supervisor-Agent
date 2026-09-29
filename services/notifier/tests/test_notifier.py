"""Alerts from ticket and AI-agent events, delivery to the on-call rota, escalation, automatic closing, the API,
and real SMTP delivery."""

import asyncio
import socket
import threading
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from uuid import uuid4

import httpx
import pytest
from conftest import PRINCIPALS, Recorder, bearer
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mes_common.auth import StaticTokenVerifier
from mes_common.events import EventEnvelope
from notifier.alerts import Dispatcher, Intake
from notifier.api import create_app
from notifier.channels import Email
from notifier.db import notification
from notifier.settings import Settings

pytestmark = pytest.mark.db
S = Settings(mes_ui_url="http://mes.test")


def ev(event_type: str, **payload: object) -> EventEnvelope:
    return EventEnvelope(event_type=event_type, source="test", key=str(payload.get("ticket_id", "k")), payload=payload)


async def feed(sf: async_sessionmaker[AsyncSession], *events: EventEnvelope) -> None:
    for e in events:
        async with sf() as s, s.begin():
            await Intake(S)(s, e)


async def alerts(sf: async_sessionmaker[AsyncSession]) -> list:
    async with sf() as s:
        return list((await s.execute(select(notification).order_by(notification.c.seq))).all())


async def test_p1_alert_goes_to_l1_and_supervisor_and_escalates_until_acknowledged(
    sf: async_sessionmaker[AsyncSession], http: httpx.AsyncClient,
) -> None:
    tid = str(uuid4())
    created = ev("mes.ticket.created", ticket_id=tid, number="MES-000100", title="Line 1 stopped", severity="P1",
                 tier="L1", category="MES_APP", reported_by="Olga", line_code="L1", response_due_at="10:15")
    await feed(sf, created, created)  # redelivery: one alert
    rec = Recorder()
    d = Dispatcher(sf, {"email": rec, "teams": rec})
    assert await d.run_once() == 1
    assert {(x["channel"], x["to"]) for x in rec.sent} == {
        ("email", "l1.oncall@plant.local"), ("email", "supervisor@plant.local"),
        ("teams", "l1.oncall@plant.local"), ("teams", "supervisor@plant.local")}
    a = (await alerts(sf))[0]
    assert a.status == "SENT" and a.rule == "P1_RAISED" and "MES-000100" in a.title and "http://mes.test/tickets" in a.body

    # nobody acknowledged within 10 min -> the backups (level 1), then the supervisor again (level 2), then it stops
    for level in (1, 2):
        async with sf() as s, s.begin():
            await s.execute(update(notification).values(sent_at=datetime.now(UTC) - timedelta(minutes=11)))
        assert await d.escalate() == 1
        rec.sent.clear()
        await d.run_once()
        tos = {x["to"] for x in rec.sent if x["channel"] == "email"}
        assert tos == ({"l1.backup@plant.local", "supervisor@plant.local"} if level == 1 else {"supervisor@plant.local"})
        assert all(x["level"] == level for x in rec.sent)
    async with sf() as s, s.begin():
        await s.execute(update(notification).values(sent_at=datetime.now(UTC) - timedelta(minutes=30)))
    assert await d.escalate() == 0  # top of the chain

    # acknowledged in MES: the bell count drops
    assert (await http.get("/api/v1/notify/summary", headers=bearer("sup"))).json()["open"] == 1
    r = await http.post(f"/api/v1/notify/notifications/{a.id}/ack", json={}, headers=bearer("sup"))
    assert r.status_code == 200 and r.json()["acked_by"] == "Sam Supervisor"
    assert (await http.post(f"/api/v1/notify/notifications/{a.id}/ack", json={}, headers=bearer("sup"))).status_code == 409
    one = (await http.get(f"/api/v1/notify/notifications/{a.id}", headers=bearer("sup"))).json()
    assert one["status"] == "ACKED" and len(one["deliveries"]) == 4 + 4 + 2
    assert (await http.get("/api/v1/notify/notifications", headers=bearer("op"))).status_code == 403


async def test_sla_escalation_ai_and_agent_alerts_open_and_close_themselves(sf: async_sessionmaker[AsyncSession]) -> None:
    tid = str(uuid4())
    base = {"ticket_id": tid, "number": "MES-000101", "title": "Scanner", "severity": "P2", "tier": "L2"}
    await feed(sf,
               ev("mes.ticket.sla_at_risk", **base, which="response", due="10:30", minutes_left=7),
               ev("mes.ticket.sla_breached", **base, which="response", due="10:30"),
               ev("mes.ticket.updated", **base, status="IN_PROGRESS", changes=["tier", "status"],
                  comment={"text": "needs the SAP team"}),
               ev("ai.proposal.created", proposal_id="p-1", ticket_id=tid, number="MES-000101", action="retry_sap",
                  summary="Retry SAP message", rationale="SAP was locked"),
               ev("ai.run.handed_over", ticket_id=tid, number="MES-000101", tier="L2", reason="cost limit", run_id="r1"),
               ev("ai.agent.health_changed", ok=False, error="config: AuthenticationError"))
    by_rule = {a.rule: a for a in await alerts(sf)}
    assert by_rule["SLA_AT_RISK"].status == "CLOSED" and "missed" in by_rule["SLA_AT_RISK"].closed_reason
    assert by_rule["SLA_BREACHED"].roles == ["L2", "SUPERVISOR"] and "7 min left" not in by_rule["SLA_BREACHED"].body
    assert by_rule["ESCALATED"].roles == ["L2"] and "needs the SAP team" in by_rule["ESCALATED"].body
    assert by_rule["APPROVAL_WAITING"].roles == ["SUPERVISOR"] and "approvals" in by_rule["APPROVAL_WAITING"].body
    assert by_rule["AI_HANDOVER"].status == "NEW" and by_rule["AGENT_DOWN"].status == "NEW"

    await feed(sf, ev("ai.proposal.decided", proposal_id="p-1", ticket_id=tid, number="MES-000101", decision="APPROVED",
                      decided_by="Sam Supervisor"),
               ev("ai.agent.health_changed", ok=True, error=None),
               ev("mes.ticket.updated", **base, status="RESOLVED", changes=["status"]))
    by_rule = {a.rule: a for a in await alerts(sf)}
    assert all(a.status == "CLOSED" for a in by_rule.values()), {k: a.status for k, a in by_rule.items()}
    assert "approved by Sam Supervisor" in by_rule["APPROVAL_WAITING"].closed_reason


async def test_rules_and_rota_are_editable_by_admins_only(sf: async_sessionmaker[AsyncSession], http: httpx.AsyncClient) -> None:
    rules = (await http.get("/api/v1/notify/rules", headers=bearer("sup"))).json()
    assert {r["key"] for r in rules} >= {"P1_RAISED", "SLA_BREACHED", "APPROVAL_WAITING"} and rules[0]["label"]
    body = {"enabled": False, "channels": ["email"], "ack_minutes": 5}
    assert (await http.put("/api/v1/notify/rules/P1_RAISED", json=body, headers=bearer("sup"))).status_code == 403
    assert (await http.put("/api/v1/notify/rules/P1_RAISED", json=body, headers=bearer("admin"))).status_code == 200
    await feed(sf, ev("mes.ticket.created", ticket_id=str(uuid4()), number="MES-1", severity="P1", tier="L1"))
    assert not await alerts(sf)  # rule switched off
    rota = [{"role": "L1", "position": 1, "name": "Nina", "email": "nina@plant.local"},
            {"role": "SUPERVISOR", "position": 1, "name": "Sam", "email": "sam@plant.local", "teams": False}]
    r = await http.put("/api/v1/notify/rota", json=rota, headers=bearer("admin"))
    assert r.status_code == 200 and [x["name"] for x in r.json()] == ["Nina", "Sam"]
    bad = await http.put("/api/v1/notify/rota", json=[{**rota[0], "role": "CEO"}], headers=bearer("admin"))
    assert bad.status_code == 422
    assert (await http.post("/api/v1/notify/test", headers=bearer("admin"))).status_code == 202
    rec = Recorder()
    await Dispatcher(sf, {"email": rec}).run_once()
    assert [x["to"] for x in rec.sent] == ["nina@plant.local"]


class _Smtp:
    """A minimal SMTP server on a local port that keeps the messages it receives."""

    def __init__(self) -> None:
        self.messages: list[bytes] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            f = conn.makefile("rwb")

            def say(line: str, f=f) -> None:  # type: ignore[no-untyped-def]
                f.write((line + "\r\n").encode())
                f.flush()

            say("220 test")
            data = None
            while (line := f.readline()):
                cmd = line.decode().strip()
                if data is not None:
                    if cmd == ".":
                        self.messages.append(b"".join(data))
                        data = None
                        say("250 ok")
                    else:
                        data.append(line)
                elif cmd.upper().startswith(("EHLO", "HELO")):
                    say("250 test")
                elif cmd.upper() == "DATA":
                    data = []
                    say("354 go")
                elif cmd.upper() == "QUIT":
                    say("221 bye")
                    break
                else:
                    say("250 ok")
            conn.close()


async def test_email_is_sent_over_smtp_with_the_acknowledge_link() -> None:
    server = _Smtp()
    email = Email(Settings(smtp_host="127.0.0.1", smtp_port=server.port, mes_ui_url="http://mes.test"))
    people = [{"name": "Lena (L1 on call)", "email": "l1@plant.local", "role": "L1"},
              {"name": "Broken", "email": "broken@plant.local", "role": "L1"}]
    out = await email("email", people, "P1 raised: MES-000100 Line 1 stopped", "Body text",
                      {"id": "abc", "severity": "P1", "level": 1})
    assert [o[1] for o in out] == [True, True]
    await asyncio.sleep(0.1)
    msg = message_from_bytes(server.messages[0])
    assert msg["To"] == "l1@plant.local" and msg["Subject"].startswith("[P1 LINE STOP] P1 raised") and "(escalated)" in msg["Subject"]
    assert "http://mes.test/notifications?ack=abc" in msg.get_payload() and msg.get_payload().startswith("Hello Lena")
    # nothing configured: every delivery fails with a reason, nothing crashes
    off = await Email(Settings(smtp_host=""))("email", people[:1], "t", "b", {"id": "x", "level": 0})
    assert off[0][1] is False and "not configured" in off[0][2]


async def test_history_replayed_by_a_new_consumer_pages_nobody(sf: async_sessionmaker[AsyncSession]) -> None:
    old = EventEnvelope(event_type="mes.ticket.created", source="test", key="k", occurred_at=datetime.now(UTC) - timedelta(hours=5),
                        payload={"ticket_id": str(uuid4()), "number": "MES-000001", "severity": "P1", "tier": "L1"})
    await feed(sf, old)
    assert not await alerts(sf)


async def test_reset_clears_alerts_and_restores_the_rota(sf: async_sessionmaker[AsyncSession], http: httpx.AsyncClient) -> None:
    await feed(sf, ev("mes.ticket.created", ticket_id=str(uuid4()), number="MES-9", severity="P1", tier="L1"))
    await http.put("/api/v1/notify/rota", json=[{"role": "L1", "position": 1, "name": "Nina", "email": "n@x"}],
                   headers=bearer("admin"))
    assert (await http.post("/api/v1/notify/admin/reset", headers=bearer("admin"))).status_code == 403  # off by default
    app = create_app(sf, StaticTokenVerifier(PRINCIPALS), Settings(allow_data_reset=True, smtp_host=""))
    async with (app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as on):
        assert (await on.post("/api/v1/notify/admin/reset", headers=bearer("sup"))).status_code == 403
        r = await on.post("/api/v1/notify/admin/reset", headers=bearer("admin"))
        assert r.status_code == 200 and r.json()["demo_mailbox"] == "not configured"
        rota = (await on.get("/api/v1/notify/rota", headers=bearer("sup"))).json()
    assert not await alerts(sf) and "Nina" not in [x["name"] for x in rota] and len(rota) > 1
