"""MES tickets -> ServiceNow incident (L1/L2 groups) -> Jira issue at L3, over MCP; engineers' changes back to MES."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select, update

from itsm_adapter.connections import Registry
from itsm_adapter.connectors.mcp import JiraMcp
from itsm_adapter.container import Container
from itsm_adapter.db import messaging, outbound_attempt, outbound_message
from itsm_adapter.errors import ItsmError, ItsmErrorKind
from itsm_adapter.settings import Settings
from itsm_adapter.sync import Intake, TicketSync
from mes_common.events import EventEnvelope
from mes_common.ids import new_id
from mock_itsm.jira import Jira
from mock_itsm.servicenow import ServiceNow

pytestmark = pytest.mark.db


def ticket(tid: UUID, **kw: object) -> dict:
    now = datetime.now(UTC)
    return {
        "ticket_id": str(tid), "plant_id": str(new_id()), "number": "MES-000042", "title": "Scanner drops at station 4",
        "description": "Barcode scanner goes offline every few minutes", "category": "HARDWARE", "impact": "DEGRADED",
        "severity": "P2", "tier": "L1", "status": "NEW", "source": "TERMINAL", "reported_by": "Oscar Operator",
        "reported_at": now.isoformat(), "resolution_note": None, "response_due_at": (now + timedelta(minutes=30)).isoformat(),
        "resolution_due_at": (now + timedelta(hours=6)).isoformat(), "sn_sys_id": None, "sn_number": None, "jira_key": None,
        "line_code": "L2", "equipment_code": "ASSY-01", "erp_order_number": None, "terminal_code": "T-L2",
        "changes": ["created"], "origin": "MES", "comment": None, **kw,
    }


async def send(c: Container, payload: dict, event_type: str = "mes.ticket.updated") -> UUID:
    env = EventEnvelope(event_type=event_type, source="mes-core", key=payload["ticket_id"], payload=payload)
    async with c.session_factory() as s, s.begin():
        await Intake(c.settings)(s, env)
    return env.event_id


async def drain(c: Container) -> None:
    """Messages of one ticket go out one per dispatcher pass (in order): run until nothing is due."""
    for _ in range(20):
        if not await c.dispatcher.run_once():
            return


async def msg(c: Container, msg_id: UUID):  # type: ignore[no-untyped-def]
    async with c.session_factory() as s:
        return (await s.execute(select(outbound_message).where(outbound_message.c.id == msg_id))).one()


async def results(c: Container, event_type: str, ticket_id: UUID) -> list[dict]:
    async with c.session_factory() as s:
        rows = (await s.execute(select(messaging.outbox.c.envelope).where(
            messaging.outbox.c.event_type == event_type, messaging.outbox.c.key == str(ticket_id))
            .order_by(messaging.outbox.c.seq))).scalars().all()
    return [r["payload"] for r in rows]


async def test_ticket_lifecycle_across_servicenow_and_jira(container: Container, servicenow: ServiceNow, jira: Jira) -> None:
    tid = new_id()
    created = await send(container, ticket(tid), "mes.ticket.created")
    again = await send(container, ticket(tid), "mes.ticket.created")  # e.g. a replayed event with a new id
    await drain(container)
    assert (await msg(container, created)).status == "SYNCED" and (await msg(container, again)).status == "SYNCED"
    incs = [i for i in servicenow.incidents.values() if i["correlation_id"] == f"MES:{tid}"]
    assert len(incs) == 1  # looked up before creating
    inc = incs[0]
    assert (inc["assignment_group"], inc["priority"], inc["cmdb_ci"], inc["state"]) == ("MES L1 Operations", "2", "ASSY-01", "1")
    assert inc["short_description"].startswith("[MES-000042]") and "T-L2" in inc["description"]
    res = (await results(container, "itsm.ticket.sync_result", tid))[-1]
    assert res["status"] == "SYNCED" and res["sn_number"] == inc["number"] and res["jira_key"] is None

    # escalate to L2, comment twice (same activity id -> one work note), acknowledge
    comment = {"id": str(new_id()), "text": "Happens on both scanners", "author": "Sam Supervisor"}
    for p in (ticket(tid, tier="L2", status="IN_PROGRESS", changes=["tier", "status"]),
              ticket(tid, tier="L2", status="IN_PROGRESS", changes=["comment"], comment=comment),
              ticket(tid, tier="L2", status="IN_PROGRESS", changes=["comment"], comment=comment)):
        await send(container, p)
    await drain(container)
    inc = servicenow.incidents[inc["sys_id"]]
    assert (inc["assignment_group"], inc["state"]) == ("MES L2 App & DB Engineering", "2")
    notes = [j for j in servicenow.journal(inc["sys_id"]) if comment["id"] in j["value"]]
    assert len(notes) == 1 and "Sam Supervisor" in notes[0]["value"] and notes[0]["sys_created_by"] == "mes.integration"

    # L3: a Jira bug linked to the incident; later comments go to Jira too
    await send(container, ticket(tid, tier="L3", status="IN_PROGRESS", changes=["tier"]))
    c2 = {"id": str(new_id()), "text": "Stack trace attached in SN", "author": "Lars"}
    await send(container, ticket(tid, tier="L3", status="IN_PROGRESS", changes=["comment"], comment=c2))
    await drain(container)
    issues = [i for i in jira.issues.values() if f"mes-ticket-{tid}" in i["labels"]]
    assert len(issues) == 1
    issue = issues[0]
    assert issue["issuetype"] == "Bug" and issue["priority"] == "High" and inc["number"] in issue["summary"]
    assert any(c2["id"] in c["body"] for c in issue["comments"])
    assert any(issue["key"] in j["value"] for j in servicenow.journal(inc["sys_id"]))
    assert (await results(container, "itsm.ticket.sync_result", tid))[-1]["jira_key"] == issue["key"]

    # resolved in MES: incident resolved with the resolution note
    await send(container, ticket(tid, tier="L3", status="RESOLVED", resolution_note="Driver 1.3.2 deployed", changes=["status"]))
    await drain(container)
    inc = servicenow.incidents[inc["sys_id"]]
    assert inc["state"] == "6" and inc["close_notes"] == "Driver 1.3.2 deployed"


async def test_unavailable_is_retried_and_rejection_fails(container: Container, servicenow: ServiceNow, settings: Settings) -> None:
    tid = new_id()
    servicenow.state.faults.fail_next = 1
    m = await send(container, ticket(tid, title="Label printer offline", severity="P3", impact="MINOR"), "mes.ticket.created")
    await drain(container)
    row = await msg(container, m)
    assert row.status == "RETRY_WAIT" and "UNAVAILABLE" in row.last_error
    async with container.session_factory() as s, s.begin():
        await s.execute(update(outbound_message).where(outbound_message.c.id == m).values(next_attempt_at=func.now()))
    await drain(container)
    assert (await msg(container, m)).status == "SYNCED"
    async with container.session_factory() as s:
        attempts = (await s.execute(select(outbound_attempt).where(outbound_attempt.c.message_id == m)
                                    .order_by(outbound_attempt.c.attempt_no))).all()
    assert [a.outcome for a in attempts] == ["RETRYABLE", "SYNCED"]
    assert [c["tool"] for c in attempts[1].calls][:2] == ["list_incidents", "create_incident"]

    # a business rejection (unknown Jira project) is permanent
    bad_jira = JiraMcp(container.jira.tools, settings.jira_mcp_user, "NOPE")  # type: ignore[attr-defined]
    bad = TicketSync(Registry(settings, None, static=(container.servicenow, bad_jira)), settings)
    with pytest.raises(ItsmError) as e:
        await bad.apply(ticket(tid, tier="L3", changes=["tier"]))
    assert e.value.kind == ItsmErrorKind.PERMANENT and "INVALID_ARGUMENT" in e.value.message


async def test_engineer_changes_flow_back(container: Container, servicenow: ServiceNow, jira: Jira) -> None:
    tid = new_id()
    await send(container, ticket(tid, tier="L3", status="IN_PROGRESS", title="Terminal crash on report"), "mes.ticket.created")
    await drain(container)
    inc = next(i for i in servicenow.incidents.values() if i["correlation_id"] == f"MES:{tid}")
    issue = next(i for i in jira.issues.values() if f"mes-ticket-{tid}" in i["labels"])
    await container.poller.poll_all()  # our own writes are not echoed

    servicenow.update("lars.l2", inc["sys_id"], {"state": "3"})
    servicenow.journal_add("lars.l2", inc["sys_id"], "work_notes", "Waiting for the terminal vendor")
    jira.comment("dana.dev", issue["key"], "Null pointer in report export")
    jira.transition("dana.dev", issue["key"], "Start progress")
    jira.transition("dana.dev", issue["key"], "Done")
    n = await container.poller.poll_all()
    changes = await results(container, "itsm.ticket.changed", tid)
    assert n == len(changes) == 5
    by = {(c["origin"], "comment" if c.get("comment") else "state" if c.get("sn_state") else c.get("jira_status")) for c in changes}
    assert ("SERVICENOW", "state") in by and ("SERVICENOW", "comment") in by and ("JIRA", "Done") in by and ("JIRA", "comment") in by
    sn_change = next(c for c in changes if c.get("sn_state"))
    assert (sn_change["sn_state"], sn_change["tier"], sn_change["author"]) == ("3", "L3", "lars.l2")
    # Jira done -> work note on the incident for L2 (by the integration user, so not echoed)
    assert any("is Done" in j["value"] for j in servicenow.journal(inc["sys_id"]))
    assert await container.poller.poll_all() == 0
