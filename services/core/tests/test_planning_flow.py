"""End-to-end through the API: ERP order -> draft schedule -> auto-sequence -> publish (e-sign) -> dispatch -> confirm."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text

from mes_common.messaging import InboxConsumer
from mes_core.modules.erp_sync.handlers import EVENT_CONFIRMATION_RESULT, HANDLERS, on_session
from mes_core.shared.db.session import Database
from mes_core.shared.messaging import messaging
from tests.conftest import auth
from tests.factories import TOPIC, envelope, work_order_payload

pytestmark = pytest.mark.db


@pytest.fixture
def consumer(db: Database) -> InboxConsumer:
    return InboxConsumer(
        messaging, db.session_factory, bootstrap_servers="unused", group_id="test", topics=[], handlers=HANDLERS,
        on_session=on_session, max_attempts=1,
    )


async def _plant_id(client: AsyncClient) -> str:
    r = await client.get("/plants", headers=auth("supervisor"))
    assert r.status_code == 200, r.text
    return r.json()[0]["id"]


async def _wo(client: AsyncClient, order: str) -> dict:
    r = await client.get("/work-orders", params={"search": order}, headers=auth("supervisor"))
    return (await client.get(f"/work-orders/{r.json()['items'][0]['id']}", headers=auth("supervisor"))).json()


async def test_me_provisions_user_with_scopes(client: AsyncClient) -> None:
    r = await client.get("/me", headers=auth("supervisor"))
    assert r.status_code == 200
    me = r.json()
    assert me["username"] == "supervisor1" and "schedule:publish" in me["permissions"]
    assert len(me["plant_ids"]) == 1
    assert (await client.get("/me")).status_code == 401


async def test_user_without_plant_scope_sees_nothing(client: AsyncClient) -> None:
    assert (await client.get("/plants", headers=auth("stranger"))).json() == []
    assert (await client.get("/work-orders", headers=auth("stranger"))).json()["total"] == 0


@pytest.mark.usefixtures("no_quality_plans")
async def test_full_planning_flow(client: AsyncClient, db: Database, consumer: InboxConsumer) -> None:
    plant_id = await _plant_id(client)
    p1, p2 = work_order_payload(days_until_due=2), work_order_payload(days_until_due=3)
    for p in (p1, p2):
        await consumer.process(TOPIC, envelope(p))

    # --- draft + auto-sequence
    r = await client.post("/schedules", json={"plant_id": plant_id, "horizon_days": 14}, headers=auth("supervisor"))
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    r = await client.post(f"/schedules/{sid}/auto-sequence", json={"reset_unlocked": True}, headers=auth("supervisor"))
    assert r.status_code == 200, r.text
    view = (await client.get(f"/schedules/{sid}", headers=auth("supervisor"))).json()
    mine = {(e["erp_order_number"], e["op_no"]): e for e in view["entries"] if e["erp_order_number"] in (p1["erp_order_number"], p2["erp_order_number"])}
    assert len(mine) == 4
    for order in (p1["erp_order_number"], p2["erp_order_number"]):
        assert mine[(order, "0020")]["planned_start"] >= mine[(order, "0010")]["planned_end"]
    assert not [c for c in view["conflicts"] if c["severity"] == "error"]

    # --- overlapping move is rejected
    a, b = mine[(p1["erp_order_number"], "0020")], mine[(p2["erp_order_number"], "0020")]
    r = await client.put(
        f"/schedules/{sid}/entries/{b['id']}",
        json={"equipment_id": a["equipment_id"], "planned_start": a["planned_start"], "row_version": b["row_version"]},
        headers=auth("supervisor"),
    )
    assert r.status_code == 409 and r.json()["code"] == "schedule_overlap"

    # --- a precedence violation blocks publishing
    first = mine[(p1["erp_order_number"], "0010")]
    last_on_machine = max(e["planned_end"] for e in view["entries"] if e["equipment_id"] == first["equipment_id"])
    r = await client.put(
        f"/schedules/{sid}/entries/{first['id']}",
        json={"equipment_id": first["equipment_id"], "planned_start": max(a["planned_end"], last_on_machine),
              "row_version": first["row_version"]},
        headers=auth("supervisor"),
    )
    assert r.status_code == 200, r.text
    r = await client.post(f"/schedules/{sid}/publish", json={"reason": "weekly plan"}, headers=auth("supervisor"))
    assert r.status_code == 409 and r.json()["code"] == "schedule_has_errors"
    await client.post(f"/schedules/{sid}/auto-sequence", json={"reset_unlocked": True}, headers=auth("supervisor"))

    # --- publish needs permission and a reason
    assert (await client.post(f"/schedules/{sid}/publish", json={"reason": "x"}, headers=auth("operator"))).status_code == 403
    assert (await client.post(f"/schedules/{sid}/publish", json={"reason": ""}, headers=auth("supervisor"))).status_code == 422
    r = await client.post(f"/schedules/{sid}/publish", json={"reason": "Approved weekly plan"}, headers=auth("supervisor"))
    assert r.status_code == 200, r.text
    sched = r.json()
    assert sched["status"] == "PUBLISHED" and sched["signature_id"]

    sigs = (await client.get("/signatures", params={"subject_id": sid}, headers=auth("supervisor"))).json()
    assert sigs[0]["meaning"] == "APPROVED" and sigs[0]["reason"] == "Approved weekly plan"
    async with db.read() as s:
        signed = (
            await s.execute(
                text("SELECT count(*) FROM audit.audit_log WHERE signature_id = :sig AND table_name = 'work_order'"),
                {"sig": sched["signature_id"]},
            )
        ).scalar_one()
    assert signed >= 2  # both work orders' RELEASED -> SCHEDULED changes carry the signature

    # --- dispatch with optimistic locking
    wo = await _wo(client, p1["erp_order_number"])
    assert wo["status"] == "SCHEDULED" and wo["sched_start"]
    r = await client.post(f"/work-orders/{wo['id']}/dispatch", json={"row_version": wo["row_version"] - 1}, headers=auth("supervisor"))
    assert r.status_code == 409 and r.json()["code"] == "stale_version"
    r = await client.post(f"/work-orders/{wo['id']}/dispatch", json={"row_version": wo["row_version"]}, headers=auth("supervisor"))
    assert r.status_code == 200 and r.json()["status"] == "DISPATCHED"

    # --- confirm operation 0010 partially -> outbox event for the ERP adapter
    op10, op20 = wo["operations"]
    r = await client.post(
        f"/work-orders/{wo['id']}/operations/{op10['id']}/confirmations",
        json={"yield_qty": "5", "scrap_qty": "1", "run_min": "22"},
        headers=auth("operator"),
    )
    assert r.status_code == 201, r.text
    conf = r.json()
    assert conf["erp_sync_status"] == "PENDING"
    async with db.read() as s:
        env = (
            await s.execute(select(messaging.outbox.c.envelope).where(messaging.outbox.c.event_id.isnot(None))
                            .where(messaging.outbox.c.event_type == "mes.confirmation.created")
                            .order_by(messaging.outbox.c.seq.desc()).limit(1))
        ).scalar_one()
    assert env["key"] == f"1000:{p1['erp_order_number']}"
    assert (env["payload"]["confirmation_id"], env["payload"]["op_no"], env["payload"]["work_center"]) == (conf["id"], "0010", "WC-TURN")
    assert (await _wo(client, p1["erp_order_number"]))["status"] == "IN_PROGRESS"

    # --- result from the adapter lands on the confirmation
    await consumer.process(
        "erp.confirmation-results.v1",
        envelope({"confirmation_id": conf["id"], "status": "POSTED", "erp_confirmation_group": "0000100001",
                  "erp_confirmation_count": "00000001", "error": None, "attempts": 2, "reconciled": True},
                 EVENT_CONFIRMATION_RESULT),
    )
    confs = (await client.get(f"/work-orders/{wo['id']}/confirmations", headers=auth("supervisor"))).json()
    assert confs[0]["erp_sync_status"] == "POSTED" and confs[0]["erp_reconciled"] is True

    # --- final confirmations complete the order
    for op in (op10, op20):
        r = await client.post(
            f"/work-orders/{wo['id']}/operations/{op['id']}/confirmations",
            json={"yield_qty": "14", "final": True}, headers=auth("supervisor"),
        )
        assert r.status_code == 201, r.text
    done = await _wo(client, p1["erp_order_number"])
    assert done["status"] == "COMPLETED"
    assert float(done["yield_qty"]) == 14 and float(done["scrap_qty"]) == 1


async def test_cannot_confirm_undispatched_order(client: AsyncClient, consumer: InboxConsumer) -> None:
    p = work_order_payload()
    await consumer.process(TOPIC, envelope(p))
    wo = await _wo(client, p["erp_order_number"])
    r = await client.post(
        f"/work-orders/{wo['id']}/operations/{wo['operations'][0]['id']}/confirmations",
        json={"yield_qty": "1"}, headers=auth("supervisor"),
    )
    assert r.status_code == 409 and r.json()["code"] == "invalid_transition"


async def test_hold_requires_reason_and_resume_restores_status(client: AsyncClient, consumer: InboxConsumer) -> None:
    p = work_order_payload()
    await consumer.process(TOPIC, envelope(p))
    wo = await _wo(client, p["erp_order_number"])
    r = await client.post(f"/work-orders/{wo['id']}/hold", json={"reason": " "}, headers=auth("supervisor"))
    assert r.status_code == 422
    r = await client.post(f"/work-orders/{wo['id']}/hold", json={"reason": "material shortage"}, headers=auth("supervisor"))
    assert r.json()["status"] == "ON_HOLD"
    r = await client.post(f"/work-orders/{wo['id']}/resume", json={}, headers=auth("supervisor"))
    body = r.json()
    assert body["status"] == "RELEASED"
    assert [h["to_status"] for h in body["history"]] == ["RELEASED", "ON_HOLD", "RELEASED"]
    assert body["history"][1]["reason"] == "material shortage"
