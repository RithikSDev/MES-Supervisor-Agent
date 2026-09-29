"""Phase 4: breakdowns from the terminal, maintenance orders (technician mode), spare parts, sign-off, preventive
plans, scheduling around maintenance, SAP PM mirror events."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text

from mes_common.events import EventEnvelope, Topics
from mes_common.messaging import InboxConsumer
from mes_core.modules.erp_sync.handlers import HANDLERS, on_session
from mes_core.shared.db.session import Database
from mes_core.shared.messaging import messaging
from tests.conftest import auth
from tests.factories import work_order_payload
from tests.helpers import dispatched_order, plant_id, terminal_login

pytestmark = pytest.mark.db


@pytest.fixture
def consumer(db: Database) -> InboxConsumer:
    return InboxConsumer(
        messaging, db.session_factory, bootstrap_servers="unused", group_id="test", topics=[], handlers=HANDLERS,
        on_session=on_session, max_attempts=1,
    )


async def _machines(client: AsyncClient) -> dict[str, dict]:
    pid = await plant_id(client)
    return {m["code"]: m for m in (await client.get("/maintenance/equipment", params={"plant_id": pid}, headers=auth("supervisor"))).json()}


async def _events(db: Database, event_type: str) -> list[dict]:
    async with db.read() as s:
        rows = (await s.execute(select(messaging.outbox.c.envelope).where(messaging.outbox.c.event_type == event_type)
                                .order_by(messaging.outbox.c.seq))).scalars().all()
    return [r["payload"] if isinstance(r, dict) else EventEnvelope.model_validate_json(r).payload for r in rows]


@pytest.mark.usefixtures("no_quality_plans")
async def test_breakdown_repair_and_release(client: AsyncClient, consumer: InboxConsumer, db: Database) -> None:
    wo = await dispatched_order(client, consumer, work_order_payload(qty="5"))
    op10 = wo["operations"][0]
    term = await terminal_login(client, "T-L1", "1111")
    q = (await client.get("/execution/queue", headers=term)).json()
    item = next(i for i in q["items"] if i["operation_id"] == op10["id"])
    machine = next(m for m in q["machines"] if m["work_center_id"] == item["work_center_id"] and not m["busy_run_id"])
    run = (await client.post(f"/execution/work-orders/{wo['id']}/operations/{op10['id']}/runs",
                             json={"equipment_id": machine["id"], "phase": "RUN"}, headers=term)).json()

    # operator: the machine is down (spindle drive fault)
    cid = str(uuid4())
    body = {"equipment_id": machine["id"], "reason_code": "ELECTRICAL", "description": "Spindle drive fault F07",
            "machine_down": True, "client_event_id": cid}
    r = await client.post("/maintenance/breakdowns", json=body, headers=term)
    assert r.status_code == 201, r.text
    notif = r.json()
    assert notif["notif_type"] == "BREAKDOWN" and notif["priority"] == 1 and notif["number"].startswith("2")
    again = await client.post("/maintenance/breakdowns", json=body, headers=term)  # offline replay
    assert again.json()["id"] == notif["id"]
    assert (await client.get(f"/execution/runs/{run['id']}", headers=term)).json()["status"] == "PAUSED"
    health = (await _machines(client))[machine["code"]]
    assert health["status"] == "DOWN" and health["open_downtime"]["reason_code"] == "ELECTRICAL"
    r = await client.post(f"/execution/runs/{run['id']}/resume", json={}, headers=term)
    assert r.status_code == 409 and r.json()["code"] == "machine_unavailable"
    r = await client.post("/maintenance/breakdowns", json={**body, "client_event_id": str(uuid4())}, headers=term)
    assert r.status_code == 409 and r.json()["code"] == "already_down"
    sap = [p for p in await _events(db, "mes.maintenance.notification_created") if p["notification_id"] == notif["id"]]
    assert sap and sap[0]["notif_type"] == "M2" and sap[0]["breakdown"] is True and sap[0]["equipment"] == machine["code"]

    # operators can't plan maintenance work; the supervisor opens the order
    assert (await client.post(f"/maintenance/notifications/{notif['id']}/order", json={}, headers=term)).status_code == 403
    r = await client.post(f"/maintenance/notifications/{notif['id']}/order", json={"checklist": ["Replace drive fuse", "Test spindle"]},
                          headers=auth("supervisor"))
    assert r.status_code == 201, r.text
    mo = r.json()
    assert mo["order_type"] == "CORRECTIVE" and mo["notification_number"] == notif["number"] and mo["number"].startswith("4")

    # technician mode on the terminal (maint1, PIN 5555)
    tech = await terminal_login(client, "T-L1", "5555")
    r = await client.post(f"/maintenance/orders/{mo['id']}/start", headers=tech)
    assert r.status_code == 200, r.text
    assert (await _machines(client))[machine["code"]]["status"] == "MAINTENANCE"
    pid = await plant_id(client)
    parts = (await client.get("/maintenance/spare-parts", params={"plant_id": pid}, headers=tech)).json()
    fuse = next(p for p in parts if p["material_number"] == "SP-BEARING-6205")
    r = await client.post(f"/maintenance/orders/{mo['id']}/parts", json={"material_id": fuse["material_id"], "quantity": "2"}, headers=tech)
    assert r.status_code == 201, r.text
    assert [(p["material_number"], float(p["quantity"])) for p in r.json()["parts"]] == [("SP-BEARING-6205", 2.0)]
    mvs = (await client.get("/inventory/movements", params={"plant_id": pid}, headers=auth("admin"))).json()
    gi = next(m for m in mvs if m["material_number"] == "SP-BEARING-6205" and m["movement_type"] == "261")
    assert gi["erp_sync_status"] == "PENDING" and gi["work_order_id"] is None
    gm = [p for p in await _events(db, "mes.goods_movement.created") if p["movement_id"] == gi["id"]][0]
    assert gm["erp_order_number"] == mo["number"]

    r = await client.post(f"/maintenance/orders/{mo['id']}/complete", json={"findings": "fuse", "reason": "ok"}, headers=tech)
    assert r.status_code == 409 and r.json()["code"] == "checklist_open"
    items = [{"text": c["text"], "done": True, "note": None} for c in mo["checklist"]]
    r = await client.put(f"/maintenance/orders/{mo['id']}/work", json={"checklist": items, "labor_min": "35"}, headers=tech)
    assert r.status_code == 200, r.text
    r = await client.post(f"/maintenance/orders/{mo['id']}/complete",
                          json={"findings": "Blown drive fuse replaced, spindle tested at 8000 rpm", "reason": "machine safe to run"},
                          headers=tech)
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "COMPLETED" and done["notification"]["status"] == "COMPLETED" and float(done["labor_min"]) == 35
    health = (await _machines(client))[machine["code"]]
    assert health["status"] == "AVAILABLE" and health["open_downtime"] is None and health["reliability"]["failures"] >= 1
    sigs = (await client.get("/signatures", params={"subject_type": "maintenance_order", "subject_id": mo["id"]}, headers=auth("admin"))).json()
    assert sigs[0]["meaning"] == "RELEASED" and sigs[0]["signer_username"] == "maint1"
    statuses = [p["status"] for p in await _events(db, "mes.maintenance.order_status") if p["order_id"] == mo["id"]]
    assert statuses == ["REL", "TECO"]
    # the technician's PIN login replaced the operator's session on this terminal (one person per kiosk)
    assert (await client.post(f"/execution/runs/{run['id']}/resume", json={}, headers=term)).status_code == 401
    term = await terminal_login(client, "T-L1", "1111")
    assert (await client.post(f"/execution/runs/{run['id']}/resume", json={}, headers=term)).status_code == 200
    await client.post(f"/execution/runs/{run['id']}/stop", json={}, headers=term)

    downtime = (await client.get("/maintenance/downtime", params={"plant_id": pid}, headers=auth("supervisor"))).json()
    d = next(x for x in downtime if x["notification_number"] == notif["number"])
    assert d["ended_at"] and not d["planned"] and d["ended_by_name"] == "Max Maintenance"

    # SAP answers: the notification is mirrored
    env = EventEnvelope(event_type="erp.maintenance.result", source="erp-adapter", key=f"1000:PMN:{notif['number']}",
                        payload={"message_type": "MAINT_NOTIFICATION", "notification_id": notif["id"], "status": "POSTED",
                                 "attempts": 1, "reconciled": False})
    await consumer.process(Topics.ERP_MAINTENANCE_RESULTS, env.model_dump_json().encode())
    ns = (await client.get("/maintenance/notifications", params={"plant_id": pid}, headers=auth("supervisor"))).json()
    assert next(n for n in ns if n["id"] == notif["id"])["erp_sync_status"] == "POSTED"


async def test_operator_can_release_a_jam_without_an_order(client: AsyncClient) -> None:
    m = (await _machines(client))["TURN-02"]
    term = await terminal_login(client, "T-L1", "2222")
    r = await client.post("/maintenance/breakdowns", json={"equipment_id": m["id"], "reason_code": "TOOLING",
                                                            "description": "chip jam in turret"}, headers=term)
    assert r.status_code == 201, r.text
    assert (await _machines(client))["TURN-02"]["status"] == "DOWN"
    r = await client.post(f"/maintenance/equipment/{m['id']}/back-up", json={"note": "jam cleared"}, headers=term)
    assert r.status_code == 204, r.text
    assert (await _machines(client))["TURN-02"]["status"] == "AVAILABLE"


async def test_preventive_order_blocks_the_schedule(client: AsyncClient, db: Database) -> None:
    pid = await plant_id(client)
    # the weekly washer plan was last done 6 days ago (seed): it is due, the scheduler opens a preventive order
    r = await client.post("/maintenance/plans/generate", params={"plant_id": pid}, headers=auth("admin"))
    assert r.status_code == 200, r.text
    orders = (await client.get("/maintenance/orders", params={"plant_id": pid, "status": ["CREATED"]}, headers=auth("supervisor"))).json()
    pm = next(o for o in orders if o["equipment_code"] == "DEB-01" and o["order_type"] == "PREVENTIVE")
    assert pm["checklist"][1]["text"] == "Replace filter bag"
    again = await client.post("/maintenance/plans/generate", params={"plant_id": pid}, headers=auth("admin"))
    assert all(o["equipment_code"] != "DEB-01" for o in again.json())  # one open order per plan

    from mes_core.modules.maintenance.public import blocked_windows

    async with db.read() as s:
        blocks = await blocked_windows(s, (await s.execute(text("SELECT id FROM org.plant"))).scalar_one(),
                                       datetime.now(UTC) - timedelta(days=1), datetime.now(UTC) + timedelta(days=14))
    assert blocks.get(UUID(pm["equipment_id"])), "a planned maintenance window blocks scheduling"

    tech = await terminal_login(client, "T-L1", "5555")
    r = await client.post(f"/maintenance/orders/{pm['id']}/start", headers=tech)
    assert r.status_code == 200, r.text
    items = [{"text": c["text"], "done": True} for c in r.json()["checklist"]]
    await client.put(f"/maintenance/orders/{pm['id']}/work", json={"checklist": items}, headers=tech)
    r = await client.post(f"/maintenance/orders/{pm['id']}/complete", json={"findings": "filter bag changed", "reason": "done"}, headers=tech)
    assert r.status_code == 200, r.text
    downtime = (await client.get("/maintenance/downtime", params={"plant_id": pid}, headers=auth("supervisor"))).json()
    planned = next(d for d in downtime if d["maintenance_order_number"] == pm["number"])
    assert planned["planned"] and planned["ended_at"]
    plans = (await client.get("/maintenance/plans", params={"plant_id": pid}, headers=auth("supervisor"))).json()
    plan = next(p for p in plans if p["equipment_code"] == "DEB-01")
    assert plan["last_done_at"] and plan["open_order_number"] is None
    r = await client.post("/maintenance/plans/generate", params={"plant_id": pid}, headers=auth("admin"))
    assert all(o["equipment_code"] != "DEB-01" for o in r.json())  # next one only in a week
