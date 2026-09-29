from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from mes_common.messaging import InboxConsumer
from mes_core.modules.erp_sync.handlers import EVENT_GOODS_MOVEMENT_RESULT, HANDLERS, on_session
from mes_core.shared.db.session import Database
from mes_core.shared.messaging import messaging
from tests.conftest import auth
from tests.factories import assembly_order_payload, envelope, work_order_payload
from tests.helpers import dispatched_order, plant_id, terminal_login

pytestmark = pytest.mark.db


@pytest.fixture
def consumer(db: Database) -> InboxConsumer:
    return InboxConsumer(
        messaging, db.session_factory, bootstrap_servers="unused", group_id="test", topics=[], handlers=HANDLERS,
        on_session=on_session, max_attempts=1,
    )


_T0 = datetime.now(UTC)  # one fixed "now" per test module: durations between events are exact, whatever the speed


def ago(minutes: int) -> str:
    return (_T0 - timedelta(minutes=minutes)).isoformat()


async def _stock(client: AsyncClient, material: str, location: str) -> Decimal:
    pid = await plant_id(client)
    rows = (await client.get("/inventory/stock", params={"plant_id": pid, "search": material}, headers=auth("admin"))).json()
    return sum((Decimal(r["quantity"]) for r in rows if r["material_number"] == material and r["location_code"] == location), Decimal(0))


async def _outbox(db: Database, event_type: str, key_suffix: str) -> list[dict]:
    async with db.read() as s:
        return list(
            (
                await s.execute(
                    select(messaging.outbox.c.envelope)
                    .where(messaging.outbox.c.event_type == event_type, messaging.outbox.c.key.like(f"%{key_suffix}"))
                    .order_by(messaging.outbox.c.seq)
                )
            ).scalars()
        )


@pytest.mark.usefixtures("no_quality_plans")
async def test_run_lifecycle_times_materials_and_goods_receipt(client: AsyncClient, consumer: InboxConsumer, db: Database) -> None:
    payload = work_order_payload(qty="20")
    wo = await dispatched_order(client, consumer, payload)
    op10, op20 = wo["operations"]
    term = await terminal_login(client, "T-L1", "1111")

    # the terminal's queue shows the operation with its planned machine
    q = (await client.get("/execution/queue", headers=term)).json()
    assert q["line_code"] == "L1"
    item = next(i for i in q["items"] if i["operation_id"] == op10["id"])
    machine = item["planned_equipment_id"] or next(m["id"] for m in q["machines"] if m["work_center_id"] == item["work_center_id"])

    # issue raw material (not backflushed) from line-side L1
    mats = (await client.get(f"/work-orders/{wo['id']}/materials", headers=term)).json()
    comp = mats["components"][0]
    assert comp["material_number"] == "RM-AL-6061" and not comp["backflush"] and Decimal(comp["remaining_qty"]) == 36
    lot = next(a for a in comp["available"] if a["location_code"] == "0002")
    before = await _stock(client, "RM-AL-6061", "0002")
    r = await client.post(
        f"/work-orders/{wo['id']}/components/{comp['component_id']}/issues",
        json={"lot_id": lot["lot_id"], "from_location_id": lot["location_id"], "quantity": "36", "client_event_id": str(uuid4())},
        headers=term,
    )
    assert r.status_code == 201, r.text
    assert r.json()["movement_type"] == "261" and r.json()["erp_sync_status"] == "PENDING"
    assert await _stock(client, "RM-AL-6061", "0002") == before - 36

    # setup 10 min, run 10 min, pause 10 min, run 10 min, then report
    run_id = str(uuid4())
    r = await client.post(
        f"/execution/work-orders/{wo['id']}/operations/{op10['id']}/runs",
        json={"equipment_id": machine, "phase": "SETUP", "run_id": run_id, "client_event_id": str(uuid4()), "occurred_at": ago(60)},
        headers=term,
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "SETUP" and r.json()["id"] == run_id
    assert (await client.get(f"/work-orders/{wo['id']}", headers=term)).json()["status"] == "IN_PROGRESS"

    for path, body in [
        ("start-production", {"occurred_at": ago(50)}),
        ("pause", {"reason_code": "MATERIAL_SHORTAGE", "occurred_at": ago(40)}),
        ("resume", {"occurred_at": ago(30)}),
    ]:
        r = await client.post(f"/execution/runs/{run_id}/{path}", json={**body, "client_event_id": str(uuid4())}, headers=term)
        assert r.status_code == 200, (path, r.text)

    report_id = str(uuid4())
    body = {"yield_qty": "10", "scrap_qty": "1", "scrap_reason": "DIMENSION", "client_event_id": report_id, "occurred_at": ago(20)}
    r = await client.post(f"/execution/runs/{run_id}/report", json=body, headers=term)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "RUNNING" and Decimal(r.json()["op_yield_qty"]) == 10
    # the terminal lost the response and sends it again: nothing happens twice
    again = await client.post(f"/execution/runs/{run_id}/report", json=body, headers=term)
    assert again.status_code == 200 and again.json()["replayed"] is True

    confs = (await client.get(f"/work-orders/{wo['id']}/confirmations", headers=auth("supervisor"))).json()
    assert len(confs) == 1
    c = confs[0]
    assert (Decimal(c["setup_min"]), Decimal(c["run_min"])) == (Decimal(10), Decimal(20))
    assert c["scrap_reason"] == "DIMENSION" and c["source"] == "TERMINAL" and c["terminal_id"] and c["run_id"] == run_id

    # scrap without a reason is refused
    r = await client.post(f"/execution/runs/{run_id}/report", json={"yield_qty": "0", "scrap_qty": "1"}, headers=term)
    assert r.status_code == 422 and r.json()["code"] == "scrap_reason_required"

    r = await client.post(f"/execution/runs/{run_id}/report", json={"yield_qty": "9", "final": True, "occurred_at": ago(10)}, headers=term)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ENDED" and r.json()["op_status"] == "CONFIRMED"

    # second operation; its machine can't run two things at once
    q = (await client.get("/execution/queue", headers=term)).json()
    item20 = next(i for i in q["items"] if i["operation_id"] == op20["id"])
    assert item20["previous_op_status"] == "CONFIRMED"
    mill = item20["planned_equipment_id"] or next(m["id"] for m in q["machines"] if m["work_center_id"] == item20["work_center_id"])
    r = await client.post(f"/execution/work-orders/{wo['id']}/operations/{op20['id']}/runs", json={"equipment_id": mill, "phase": "RUN"}, headers=term)
    assert r.status_code == 201, r.text
    run2 = r.json()["id"]
    other = await dispatched_order(client, consumer, work_order_payload(qty="5"))
    r = await client.post(
        f"/execution/work-orders/{other['id']}/operations/{other['operations'][1]['id']}/runs",
        json={"equipment_id": mill, "phase": "RUN"}, headers=term,
    )
    assert r.status_code == 409 and r.json()["code"] == "run_conflict"

    # completing the last operation receives the good quantity into finished goods as a new lot
    fg_before = await _stock(client, "FG-1001", "0010")
    r = await client.post(f"/execution/runs/{run2}/report", json={"yield_qty": "18", "final": True}, headers=term)
    assert r.status_code == 200, r.text
    assert (await client.get(f"/work-orders/{wo['id']}", headers=term)).json()["status"] == "COMPLETED"
    assert await _stock(client, "FG-1001", "0010") == fg_before + 18
    mats = (await client.get(f"/work-orders/{wo['id']}/materials", headers=term)).json()
    gr = next(m for m in mats["movements"] if m["movement_type"] == "101")
    assert Decimal(gr["quantity"]) == 18 and gr["to_location"] == "0010" and gr["lot_number"].startswith("L")
    events = await _outbox(db, "mes.goods_movement.created", f":{payload['erp_order_number']}")
    assert [e["payload"]["movement_type"] for e in events] == ["261", "101"]
    assert events[1]["payload"]["storage_location"] == "0010" and events[1]["payload"]["batch"] == gr["lot_number"]

    # SAP result comes back through the adapter
    await consumer.process(
        "erp.goods-movement-results.v1",
        envelope({"movement_id": gr["id"], "status": "POSTED", "erp_material_document": "4900000123",
                  "erp_document_year": "2026", "error": None, "attempts": 1, "reconciled": False}, EVENT_GOODS_MOVEMENT_RESULT),
    )
    mats = (await client.get(f"/work-orders/{wo['id']}/materials", headers=term)).json()
    assert next(m for m in mats["movements"] if m["id"] == gr["id"])["erp_material_document"] == "4900000123"

    hist = (await client.get(f"/work-orders/{wo['id']}/execution", headers=auth("supervisor"))).json()
    assert [e["event_type"] for e in reversed(hist["events"])][:6] == ["START_SETUP", "START_PRODUCTION", "PAUSE", "RESUME", "REPORT", "COMPLETE"]


async def test_backflush_across_locations_and_shortages(client: AsyncClient, consumer: InboxConsumer) -> None:
    wo = await dispatched_order(client, consumer, assembly_order_payload(qty="300"))
    op10 = wo["operations"][0]
    term = await terminal_login(client, "T-L2", "2222")

    # line L1's terminal can't run L2 machines
    l1 = await terminal_login(client, "T-L1", "3333")
    q2 = (await client.get("/execution/queue", headers=term)).json()
    assy = next(m["id"] for m in q2["machines"] if m["work_center_code"] == "WC-ASSY" and not m["busy_run_id"])
    r = await client.post(f"/execution/work-orders/{wo['id']}/operations/{op10['id']}/runs", json={"equipment_id": assy}, headers=l1)
    assert r.status_code == 409 and r.json()["code"] == "wrong_line"

    # shortages: 300 valve bodies needed, only 60 in stock
    pid = await plant_id(client)
    short = (await client.get("/inventory/shortages", params={"plant_id": pid}, headers=auth("supervisor"))).json()
    vb = next(row for row in short if row["material_number"] == "SF-1003")
    assert Decimal(vb["shortage"]) >= 240

    # more than available cannot be issued
    mats = (await client.get(f"/work-orders/{wo['id']}/materials", headers=term)).json()
    body_comp = next(c for c in mats["components"] if c["material_number"] == "SF-1003")
    lot = body_comp["available"][0]
    r = await client.post(
        f"/work-orders/{wo['id']}/components/{body_comp['component_id']}/issues",
        json={"lot_id": lot["lot_id"], "from_location_id": lot["location_id"], "quantity": str(Decimal(lot["quantity"]) + 1)},
        headers=term,
    )
    assert r.status_code == 409 and r.json()["code"] == "insufficient_stock"

    # backflush 2 seals per unit: 250 units -> 500 seals, line-side L2 first, then the main store
    lineside = await _stock(client, "CP-SEAL-12", "0003")
    store = await _stock(client, "CP-SEAL-12", "0001")
    run = (await client.post(f"/execution/work-orders/{wo['id']}/operations/{op10['id']}/runs", json={"equipment_id": assy, "phase": "RUN"}, headers=term)).json()
    r = await client.post(f"/execution/runs/{run['id']}/report", json={"yield_qty": "250"}, headers=term)
    assert r.status_code == 200, r.text
    assert await _stock(client, "CP-SEAL-12", "0003") == max(lineside - 500, Decimal(0))
    assert await _stock(client, "CP-SEAL-12", "0001") == store - max(500 - lineside, Decimal(0))
    mats = (await client.get(f"/work-orders/{wo['id']}/materials", headers=term)).json()
    seals = next(c for c in mats["components"] if c["material_number"] == "CP-SEAL-12")
    assert Decimal(seals["issued_qty"]) == 500
    assert all(m["backflush"] for m in mats["movements"] if m["movement_type"] == "261")

    # stop at shift end: run ends, operation stays open for the next shift
    r = await client.post(f"/execution/runs/{run['id']}/stop", json={"note": "shift end"}, headers=term)
    assert r.json()["status"] == "ENDED" and r.json()["op_status"] == "IN_PROGRESS"
    wip = (await client.get("/execution/wip", params={"plant_id": pid}, headers=auth("supervisor"))).json()
    test_wc = next(w for w in wip if w["work_center_code"] == "WC-TEST")
    assert Decimal(test_wc["waiting_qty"]) >= 250 and wo["erp_order_number"] in test_wc["waiting_orders"]


async def test_live_board_from_database_when_redis_is_off(client: AsyncClient) -> None:
    pid = await plant_id(client)
    live = (await client.get("/execution/live", params={"plant_id": pid}, headers=auth("supervisor"))).json()
    assert live["source"] == "database"
    assert len(live["machines"]) >= 9
    assert {m["state"] for m in live["machines"]} <= {"IDLE", "SETUP", "RUNNING", "PAUSED"}


async def test_receipts_and_transfers(client: AsyncClient, db: Database) -> None:
    pid = await plant_id(client)
    locs = {x["code"]: x for x in (await client.get("/inventory/locations", params={"plant_id": pid}, headers=auth("supervisor"))).json()}
    mat = (await client.get("/materials", params={"search": "CP-SPRING-8"}, headers=auth("supervisor"))).json()[0]
    r = await client.post(
        "/inventory/receipts",
        json={"plant_id": pid, "material_id": mat["id"], "quantity": "100", "location_id": locs["0001"]["id"], "reason": "delivery"},
        headers=auth("supervisor"),
    )
    assert r.status_code == 201, r.text
    rec = r.json()
    assert rec["movement_type"] == "501" and rec["erp_sync_status"] == "PENDING"
    assert (await client.post("/inventory/receipts", json={"plant_id": pid, "material_id": mat["id"], "quantity": "1",
                                                           "location_id": locs["0001"]["id"]}, headers=auth("operator"))).status_code == 403
    r = await client.post(
        "/inventory/transfers",
        json={"plant_id": pid, "lot_id": rec["lot_id"], "quantity": "40", "from_location_id": locs["0001"]["id"], "to_location_id": locs["0003"]["id"]},
        headers=auth("supervisor"),
    )
    assert r.status_code == 201 and r.json()["movement_type"] == "311"
    stock = (await client.get("/inventory/stock", params={"plant_id": pid, "material_id": mat["id"]}, headers=auth("supervisor"))).json()
    by_loc = {(x["location_code"], x["lot_number"]): Decimal(x["quantity"]) for x in stock}
    assert by_loc[("0001", rec["lot_number"])] == 60 and by_loc[("0003", rec["lot_number"])] == 40
    r = await client.post(
        "/inventory/transfers",
        json={"plant_id": pid, "lot_id": rec["lot_id"], "quantity": "61", "from_location_id": locs["0001"]["id"], "to_location_id": locs["0003"]["id"]},
        headers=auth("supervisor"),
    )
    assert r.status_code == 409 and r.json()["code"] == "insufficient_stock"
    events = await _outbox(db, "mes.goods_movement.created", ":MAT:CP-SPRING-8")
    assert [e["payload"]["movement_type"] for e in events][-2:] == ["501", "311"]
    assert events[-1]["payload"]["destination_storage_location"] == "0003"
