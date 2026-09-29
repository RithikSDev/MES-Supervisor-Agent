"""The REST connectors (ServiceNow Table API, Jira Cloud REST v3) end to end through TicketSync, the dispatcher and
the poller, against in-memory fakes of both APIs (tests/rest_fakes.py)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from rest_fakes import FakeJira, FakeServiceNow
from sqlalchemy import select
from test_itsm import drain, msg, results, send, ticket

from itsm_adapter.connections import Registry
from itsm_adapter.connectors.jira_rest import JiraRest
from itsm_adapter.connectors.servicenow_rest import ServiceNowRest
from itsm_adapter.container import Container, build_container
from itsm_adapter.db import outbound_attempt
from itsm_adapter.errors import ItsmError, ItsmErrorKind
from itsm_adapter.settings import Settings
from itsm_adapter.sync import TicketSync
from mes_common.ids import new_id

pytestmark = pytest.mark.db


@pytest.fixture
def rest_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"itsm_mode": "rest"})


@pytest.fixture
def fsn(rest_settings: Settings) -> FakeServiceNow:
    return FakeServiceNow(groups=tuple(rest_settings.groups.values()))


@pytest.fixture
def fjira() -> FakeJira:
    return FakeJira()


def sn_port(f: FakeServiceNow, password: str = "pw") -> ServiceNowRest:
    return ServiceNowRest(f.base, f.user, password, contact_type="integration", http=f.client())


def jira_port(f: FakeJira) -> JiraRest:
    return JiraRest(f.site, f.email, f.token, f.project, http=f.client())


@pytest_asyncio.fixture
async def rest(rest_settings: Settings, fsn: FakeServiceNow, fjira: FakeJira) -> AsyncIterator[Container]:
    c = build_container(rest_settings, ports=(sn_port(fsn), jira_port(fjira)))
    yield c
    await c.aclose()


async def test_rest_ticket_flow(rest: Container, fsn: FakeServiceNow, fjira: FakeJira) -> None:
    tid = new_id()
    m = await send(rest, ticket(tid), "mes.ticket.created")
    await drain(rest)
    row = await msg(rest, m)
    assert row.status == "SYNCED", row.last_error
    inc = fsn.by_correlation(f"MES:{tid}")
    assert inc["assignment_group"] == fsn.group("MES L1 Operations")  # name resolved to the group's sys_id
    assert (inc["impact"], inc["urgency"], inc["priority"]) == ("1", "2", "2")  # P2
    assert inc["correlation_display"] == "MES" and inc["contact_type"] == "integration"
    assert row.result["sn_number"] == inc["number"] and "nav_to.do" in row.result["sn_url"]
    async with rest.session_factory() as s:
        calls = (await s.execute(select(outbound_attempt.c.calls).where(outbound_attempt.c.message_id == m))).scalar_one()
    assert [c["tool"] for c in calls] == ["GET /api/now/table/incident", "GET /api/now/table/sys_user_group",
                                          "POST /api/now/table/incident"]
    assert all(c["outcome"] == "OK" and c["status"] < 300 and "ms" in c for c in calls)

    # escalate to L3: incident moves group, Jira issue created with the MES label, the incident gets a work note
    await send(rest, ticket(tid, tier="L3", status="IN_PROGRESS", changes=["tier", "status"]))
    await drain(rest)
    assert inc["assignment_group"] == fsn.group("MES L3 Core Dev") and inc["state"] == "2"
    issue = fjira.by_label(f"mes-ticket-{tid}")
    assert issue["priority"] == "High" and issue["issuetype"] == "Bug" and inc["number"] in issue["summary"]
    assert any(issue["key"] in j["value"] for j in fsn.journal if j["element_id"] == inc["sys_id"])
    sync_results = await results(rest, "itsm.ticket.sync_result", tid)
    assert sync_results[-1]["jira_key"] == issue["key"]
    assert sync_results[-1]["jira_url"] == f"{fjira.site}/browse/{issue['key']}"

    # replaying the escalation changes nothing (lookups by correlation id / label, notes by marker)
    n_incidents, n_issues, n_notes = len(fsn.incidents), len(fjira.issues), len(fsn.journal)
    await send(rest, ticket(tid, tier="L3", status="IN_PROGRESS", changes=["tier", "status"]))
    await drain(rest)
    assert (len(fsn.incidents), len(fjira.issues), len(fsn.journal)) == (n_incidents, n_issues, n_notes)

    # our own writes are not echoed back
    assert await rest.poller.poll_all() == 0

    # engineers work the records
    fsn.act("lars.l2", inc["sys_id"], {"state": "3"}, work_note="Waiting for the terminal vendor")
    fjira.act("acc-dana", issue["key"], comment="Null pointer in report export")
    fjira.act("acc-dana", issue["key"], transition="Start progress")
    fjira.act("acc-dana", issue["key"], transition="Done")
    assert await rest.poller.poll_all() == 5
    changes = await results(rest, "itsm.ticket.changed", tid)
    kinds = {(c["origin"], "comment" if c.get("comment") else "state" if c.get("sn_state") else c.get("jira_status"))
             for c in changes}
    assert kinds == {("SERVICENOW", "state"), ("SERVICENOW", "comment"), ("JIRA", "comment"), ("JIRA", "In Progress"),
                     ("JIRA", "Done")}
    sn_change = next(c for c in changes if c.get("sn_state"))
    assert sn_change["sn_state"] == "3" and sn_change["tier"] == "L3" and sn_change["severity"] == "P2"
    assert any("is Done" in j["value"] and j["sys_created_by"] == fsn.user for j in fsn.journal)  # told L2
    assert await rest.poller.poll_all() == 0  # overlap window re-read: nothing twice

    # MES resolves: state 6 with a resolution code and notes (the instance requires both)
    await send(rest, ticket(tid, tier="L3", status="RESOLVED", resolution_note="Driver 1.3.2 deployed", changes=["status"]))
    await drain(rest)
    assert inc["state"] == "6" and inc["close_code"] == "Solution provided" and inc["close_notes"] == "Driver 1.3.2 deployed"


async def test_rest_errors(rest_settings: Settings, fsn: FakeServiceNow, fjira: FakeJira) -> None:
    tid = new_id()
    p = ticket(tid)

    bad = TicketSync(Registry(rest_settings, None, static=(sn_port(fsn, password="wrong"), jira_port(fjira))), rest_settings)
    with pytest.raises(ItsmError) as e:
        await bad.apply(p)
    assert e.value.kind == ItsmErrorKind.PERMANENT and "HTTP 401" in e.value.message and "integration user" in e.value.message

    ok = TicketSync(Registry(rest_settings, None, static=(sn_port(fsn), jira_port(fjira))), rest_settings)
    fsn.fail_next = [503]
    with pytest.raises(ItsmError) as e:
        await ok.apply(p)
    assert e.value.kind == ItsmErrorKind.RETRYABLE

    other = rest_settings.model_copy(update={"group_l1": "No Such Group"})
    missing = TicketSync(Registry(other, None, static=(sn_port(fsn), jira_port(fjira))), other)
    with pytest.raises(ItsmError) as e:
        await missing.apply(p)
    assert e.value.kind == ItsmErrorKind.PERMANENT and "does not exist" in e.value.message
    assert not fsn.incidents

    # team-managed Jira projects often have no Priority on the create screen: the issue is created without it
    fjira.priority_on_screen = False
    res = await ok.apply(ticket(tid, tier="L3", changes=["tier"]))
    assert res["jira_key"] and fjira.issues[res["jira_key"]]["priority"] == "Medium"


async def test_rest_setup_and_health(fsn: FakeServiceNow, fjira: FakeJira) -> None:
    sn = sn_port(fsn)
    assert await sn.ensure_group("MES L1 Operations", create=False) == "exists"
    assert await sn.ensure_group("MES L4 Vendor", create=False) == "missing"
    assert await sn.ensure_group("MES L4 Vendor", create=True) == "created"
    assert "MES L4 Vendor" in fsn.groups.values()
    assert (await sn.health())["detail"]
    assert await sn.can_read_journal() is None  # no work notes anywhere yet
    inc = await sn.create({"short_description": "x", "impact": 3, "urgency": 3, "assignment_group": "MES L1 Operations",
                           "correlation_id": "MES:setup-check"})
    await sn.add_work_note(inc["sys_id"], "hello")
    assert await sn.can_read_journal() is True

    jira = jira_port(fjira)
    assert await jira.issue_types() == ["Bug", "Task", "Epic"]
    assert "project MES" in (await jira.health())["detail"][1]
    other = JiraRest(fjira.site, fjira.email, fjira.token, "NOPE", http=fjira.client())
    with pytest.raises(ItsmError) as e:
        await other.health()
    assert e.value.kind == ItsmErrorKind.PERMANENT and "No project" in e.value.message


async def test_details_reach_the_incident_and_late_notes_skip_canceled_ones(rest: Container, fsn: FakeServiceNow) -> None:
    tid = new_id()
    await send(rest, ticket(tid), "mes.ticket.created")
    await drain(rest)
    inc = fsn.by_correlation(f"MES:{tid}")
    # details corrected in MES: the incident's short description and description follow
    await send(rest, ticket(tid, title="Scanner drops at station 5", impact="MINOR",
                            changes=["details", "title", "impact"],
                            comment={"id": str(new_id()), "text": "Details changed (title ...)", "author": "Sam"}))
    await drain(rest)
    assert inc["short_description"] == "[MES-000042] Scanner drops at station 5" and "impact MINOR" in inc["description"]
    # cancelled, then a late note (an AI run still finishing): ServiceNow refuses updates to canceled incidents,
    # so the note is not sent and nothing fails
    await send(rest, ticket(tid, status="CANCELLED", changes=["status"]))
    await drain(rest)
    assert inc["state"] == "8"
    notes = lambda: [j for j in fsn.journal if j["element_id"] == inc["sys_id"]]  # noqa: E731
    notes_before = len(notes())
    late = await send(rest, ticket(tid, status="CANCELLED", changes=["comment"],
                                   comment={"id": str(new_id()), "text": "L1 triage note", "author": "AI Support Agent"}))
    await drain(rest)
    assert (await msg(rest, late)).status == "SYNCED"
    assert len(notes()) == notes_before


async def test_assignee_and_backdated_history(rest: Container, fsn: FakeServiceNow) -> None:
    priya = fsn.add_user("priya.nair", "Priya Nair", "priya.nair@mes.local")
    tid = new_id()
    await send(rest, ticket(tid, assignee={"name": "Priya Nair", "username": "l1.priya", "email": "priya.nair@mes.local"}),
               "mes.ticket.created")
    await drain(rest)
    inc = fsn.by_correlation(f"MES:{tid}")
    assert inc["assigned_to"] == priya  # matched by e-mail
    # reassigned in MES to someone without a ServiceNow user: left unassigned there, with a work note
    await send(rest, ticket(tid, assignee={"name": "Tom Becker", "username": "l1.tom", "email": "tom.becker@mes.local"},
                            changes=["assignee"]))
    await drain(rest)
    assert inc["assigned_to"] == "" and any("Assigned in MES to Tom Becker" in j["value"] for j in fsn.journal)
    # an engineer assigns it in ServiceNow: comes back to MES with name, user name and e-mail
    wei = fsn.add_user("wei.chen", "Wei Chen", "wei.chen@mes.local")
    fsn.act("sn.engineer", inc["sys_id"], {"assigned_to": wei})
    await rest.poller.poll_servicenow()
    changes = await results(rest, "itsm.ticket.changed", tid)
    assert changes[-1]["assignee"] == {"name": "Wei Chen", "user_name": "wei.chen", "email": "wei.chen@mes.local"}

    # a backdated ticket already resolved (demo history): opened at its MES time, straight to Resolved
    old = new_id()
    reported = datetime.now(UTC) - timedelta(days=20)
    await send(rest, ticket(old, reported_at=reported.isoformat(), status="RESOLVED", resolution_note="fixed",
                            demo="history"), "mes.ticket.created")
    await drain(rest)
    hist = fsn.by_correlation(f"MES:{old}")
    assert hist["opened_at"] == reported.strftime("%Y-%m-%d %H:%M:%S") and hist["state"] == "6"

    # the setup creates the MES support team as ServiceNow users in their tier's group
    sn = rest.registry.sn
    g1 = "MES L1 Operations"
    assert (await sn.ensure_user("l1.tom", "Tom Becker", "tom.becker@mes.local", g1, create=False))["status"] == "missing"
    made = await sn.ensure_user("l1.tom", "Tom Becker", "tom.becker@mes.local", g1, create=True)
    assert (made["status"], made["itil"], made["in_group"]) == ("created", True, True)
    tom = next(u for u in fsn.users.values() if u["user_name"] == "l1.tom")
    assert (tom["sys_id"], "itil") in fsn.roles and (tom["sys_id"], fsn.group(g1)) in fsn.members
    assert (await sn.ensure_user("l1.tom", "Tom Becker", "tom.becker@mes.local", g1, create=False)) == {
        "status": "exists", "user_name": "l1.tom", "itil": True, "in_group": True, "fixed": []}
    # an existing user without itil and not in the group (e.g. an admin added by hand) can't be picked as Assigned to
    check = await sn.ensure_user("priya.nair", "Priya Nair", "priya.nair@mes.local", g1, create=False)
    assert (check["status"], check["itil"], check["in_group"]) == ("exists", False, False)
    fix = await sn.ensure_user("priya.nair", "Priya Nair", "priya.nair@mes.local", g1, create=True)
    assert fix["fixed"] == ["itil role", f"member of {g1}"] and (priya, "itil") in fsn.roles
