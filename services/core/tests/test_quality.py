"""Phase 3: inspections, usage decisions, nonconformances, serial numbers and genealogy (through the real API)."""

from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import AsyncClient

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


async def _machine(client: AsyncClient, term: dict, op_id: str) -> str:
    q = (await client.get("/execution/queue", headers=term)).json()
    item = next(i for i in q["items"] if i["operation_id"] == op_id)
    free = {m["id"] for m in q["machines"] if m["work_center_id"] == item["work_center_id"] and not m["busy_run_id"] and m["status"] == "AVAILABLE"}
    return item["planned_equipment_id"] if item["planned_equipment_id"] in free else sorted(free)[0]


async def _start(client: AsyncClient, term: dict, wo: dict, op: dict) -> dict:
    r = await client.post(
        f"/execution/work-orders/{wo['id']}/operations/{op['id']}/runs",
        json={"equipment_id": await _machine(client, term, op["id"]), "phase": "RUN"}, headers=term,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _lot_detail(client: AsyncClient, il_id: str, who: str = "quality") -> dict:
    r = await client.get(f"/quality/inspection-lots/{il_id}", headers=auth(who))
    assert r.status_code == 200, r.text
    return r.json()


def _results(detail: dict, values: dict[str, object], sample: int = 1) -> dict:
    out = []
    for c in detail["characteristics"]:
        v = next(val for key, val in values.items() if c["name"].startswith(key))
        out.append({"characteristic_id": c["id"], "sample_no": sample,
                    **({"measured_value": str(v)} if c["kind"] == "QUANTITATIVE" else {"attribute_ok": v})})
    return {"results": out}


async def _stock_rows(client: AsyncClient, lot_number: str) -> list[dict]:
    pid = await plant_id(client)
    rows = (await client.get("/inventory/stock", params={"plant_id": pid}, headers=auth("admin"))).json()
    return [r for r in rows if r["lot_number"] == lot_number]


async def _movements(client: AsyncClient, wo_id: str | None = None) -> list[dict]:
    pid = await plant_id(client)
    params = {"plant_id": pid, **({"work_order_id": wo_id} if wo_id else {})}
    return (await client.get("/inventory/movements", params=params, headers=auth("admin"))).json()


async def test_in_process_and_final_inspection_serials_and_genealogy(client: AsyncClient, consumer: InboxConsumer) -> None:
    wo = await dispatched_order(client, consumer, work_order_payload(qty="4"))
    op10, op20 = wo["operations"]
    term = await terminal_login(client, "T-L1", "1111")

    # issue raw material so the genealogy has a component lot
    comp = (await client.get(f"/work-orders/{wo['id']}/materials", headers=term)).json()["components"][0]
    rm = next(a for a in comp["available"] if a["location_code"] == "0002")
    r = await client.post(f"/work-orders/{wo['id']}/components/{comp['component_id']}/issues",
                          json={"lot_id": rm["lot_id"], "from_location_id": rm["location_id"], "quantity": "7.2"}, headers=term)
    assert r.status_code == 201, r.text

    # op 0010 has no inspection plan
    run10 = await _start(client, term, wo, op10)
    assert run10["inspection"] is None
    r = await client.post(f"/execution/runs/{run10['id']}/report", json={"yield_qty": "4", "final": True}, headers=term)
    assert r.status_code == 200, r.text

    # op 0020 (milling) has a blocking first-off inspection: reporting is refused until it passes
    run20 = await _start(client, term, wo, op20)
    insp = run20["inspection"]
    assert insp and insp["status"] == "OPEN" and insp["blocking"]
    r = await client.post(f"/execution/runs/{run20['id']}/report", json={"yield_qty": "1"}, headers=term)
    assert r.status_code == 409 and r.json()["code"] == "inspection_required"

    # the operator measures on the terminal: out of tolerance -> failed, nonconformance, order on hold
    detail = await _lot_detail(client, insp["id"], "operator")
    r = await client.post(f"/quality/inspection-lots/{insp['id']}/results",
                          json=_results(detail, {"Flange": "120.10", "Port": True}), headers=term)
    assert r.status_code == 200, r.text
    failed = r.json()
    assert failed["status"] == "FAILED" and failed["nonconformances"][0]["source"] == "INSPECTION"
    assert failed["nonconformances"][0]["defect_code"] == "DIMENSION"
    wo_now = (await client.get(f"/work-orders/{wo['id']}", headers=auth("supervisor"))).json()
    assert wo_now["status"] == "ON_HOLD"

    # results can't be changed after evaluation; the run can pause for the quality check but not resume while held
    r = await client.post(f"/quality/inspection-lots/{insp['id']}/results",
                          json=_results(detail, {"Flange": "120.00", "Port": True}), headers=term)
    assert r.status_code == 409
    r = await client.post(f"/execution/runs/{run20['id']}/pause", json={"reason_code": "QUALITY_CHECK"}, headers=term)
    assert r.status_code == 200, r.text
    r = await client.post(f"/execution/runs/{run20['id']}/resume", json={}, headers=term)
    assert r.status_code == 409 and r.json()["code"] == "work_order_on_hold"

    # supervisor releases the hold after correcting the program; resuming opens a new first-off check
    r = await client.post(f"/work-orders/{wo['id']}/resume", json={"row_version": wo_now["row_version"], "reason": "offset corrected"},
                          headers=auth("supervisor"))
    assert r.status_code == 200, r.text
    r = await client.post(f"/execution/runs/{run20['id']}/resume", json={}, headers=term)
    assert r.status_code == 200, r.text
    insp2 = r.json()["inspection"]
    assert insp2["status"] == "OPEN" and insp2["number"] != insp["number"]
    r = await client.post(f"/quality/inspection-lots/{insp2['id']}/results",
                          json=_results(detail, {"Flange": "120.01", "Port": True}), headers=term)
    assert r.json()["status"] == "PASSED"

    # final confirmation: goods receipt into quality inspection stock, one serial per unit, final inspection lot
    r = await client.post(f"/execution/runs/{run20['id']}/report", json={"yield_qty": "4", "final": True}, headers=term)
    assert r.status_code == 200, r.text
    serials = r.json()["serials"]
    assert len(serials) == 4 and all(x.startswith("FG-1001-") for x in serials)
    gr = next(m for m in await _movements(client, wo["id"]) if m["movement_type"] == "101")
    assert gr["to_status"] == "QUALITY"
    assert [(x["status"], Decimal(x["quantity"])) for x in await _stock_rows(client, gr["lot_number"])] == [("QUALITY", 4)]
    pid = await plant_id(client)
    final = (await client.get("/quality/inspection-lots", params={"plant_id": pid, "work_order_id": wo["id"],
                                                                   "awaiting_decision": True}, headers=auth("quality"))).json()
    assert [x["inspection_type"] for x in final] == ["FINAL"] and final[0]["lot_number"] == gr["lot_number"]
    fid = final[0]["id"]

    # usage decision: operators may not decide; an open lot can't be accepted; quality accepts with e-signature
    assert (await client.post(f"/quality/inspection-lots/{fid}/usage-decision",
                              json={"decision": "ACCEPTED", "reason": "ok"}, headers=term)).status_code == 403
    r = await client.post(f"/quality/inspection-lots/{fid}/usage-decision", json={"decision": "ACCEPTED", "reason": "ok"},
                          headers=auth("quality"))
    assert r.status_code == 409 and r.json()["code"] == "inspection_not_passed"
    fdetail = await _lot_detail(client, fid)
    r = await client.post(f"/quality/inspection-lots/{fid}/results",
                          json=_results(fdetail, {"Visual": True, "Overall": "85.02"}), headers=auth("quality"))
    assert r.json()["status"] == "PASSED"
    r = await client.post(f"/quality/inspection-lots/{fid}/usage-decision",
                          json={"decision": "ACCEPTED", "reason": "final inspection passed"}, headers=auth("quality"))
    assert r.status_code == 200, r.text
    assert r.json()["usage_decision"] == "ACCEPTED" and r.json()["decided_by"] == "Quinn Quality"
    assert [(x["status"], Decimal(x["quantity"])) for x in await _stock_rows(client, gr["lot_number"])] == [("UNRESTRICTED", 4)]
    ud = next(m for m in await _movements(client) if m["movement_type"] == "321" and m["lot_number"] == gr["lot_number"])
    assert ud["erp_sync_status"] == "PENDING" and (ud["from_status"], ud["to_status"]) == ("QUALITY", "UNRESTRICTED")
    sigs = (await client.get("/signatures", params={"subject_type": "inspection_lot", "subject_id": fid}, headers=auth("admin"))).json()
    assert sigs[0]["meaning"] == "APPROVED" and sigs[0]["reason"] == "final inspection passed"

    # genealogy backward: serial -> FG lot -> order (machines, operators, inspections) -> raw material lot
    hits = (await client.get("/trace/search", params={"plant_id": pid, "q": serials[0]}, headers=auth("quality"))).json()
    assert hits[0]["type"] == "SERIAL" and hits[0]["label"] == serials[0]
    t = (await client.get(f"/trace/serials/{serials[0]}", params={"plant_id": pid}, headers=auth("quality"))).json()
    root = t["root"]
    assert root["type"] == "LOT" and root["label"] == gr["lot_number"] and root["lot"]["serial_count"] == 4
    order = root["children"][0]
    assert order["type"] == "ORDER" and order["id"] == wo["id"]
    ops = {o["op_no"]: o for o in order["order"]["operations"]}
    assert ops["0020"]["steps"][0]["equipment_code"] and ops["0020"]["steps"][0]["operator_name"]
    assert {i["status"] for i in order["inspections"]} == {"FAILED", "PASSED"}
    assert [n["defect_code"] for n in order["nonconformances"]] == ["DIMENSION"]
    rm_node = order["children"][0]
    assert rm_node["label"] == rm["lot_number"] and Decimal(rm_node["quantity"]) == Decimal("7.2")

    # genealogy forward from the raw material lot finds the finished lot; a suspect raw lot quarantines it
    fwd = (await client.get(f"/trace/lots/{rm['lot_id']}", params={"direction": "forward"}, headers=auth("quality"))).json()
    ours = next(o for o in fwd["root"]["children"] if o["id"] == wo["id"])
    assert Decimal(ours["quantity"]) == Decimal("7.2") and [x["label"] for x in ours["children"]] == [gr["lot_number"]]
    r = await client.post(f"/trace/lots/{rm['lot_id']}/quarantine-affected",
                          json={"reason": "supplier alert: wrong alloy", "include_root": False}, headers=auth("quality"))
    assert r.status_code == 200, r.text
    blocked = r.json()
    assert (gr["lot_number"], Decimal(4)) in [(b["lot_number"], Decimal(b["blocked_qty"])) for b in blocked]
    assert [x["status"] for x in await _stock_rows(client, gr["lot_number"])] == ["BLOCKED"]
    t = (await client.get(f"/trace/lots/{gr['lot_id']}", headers=auth("quality"))).json()
    assert t["root"]["nonconformances"][-1]["id"] == next(b["nc_id"] for b in blocked if b["lot_number"] == gr["lot_number"])

    # disposition rework, then all four reworked to good: blocked -> unrestricted (343)
    nc_id = next(b["nc_id"] for b in blocked if b["lot_number"] == gr["lot_number"])
    r = await client.post(f"/quality/nonconformances/{nc_id}/disposition", json={"disposition": "REWORK", "reason": "re-machine"},
                          headers=auth("quality"))
    assert r.status_code == 200 and r.json()["status"] == "REWORK", r.text
    r = await client.post(f"/quality/nonconformances/{nc_id}/rework-complete",
                          json={"good_qty": "3", "scrap_qty": "0", "reason": "done"}, headers=auth("quality"))
    assert r.status_code == 422 and r.json()["code"] == "rework_quantity_mismatch"
    r = await client.post(f"/quality/nonconformances/{nc_id}/rework-complete",
                          json={"good_qty": "4", "scrap_qty": "0", "reason": "alloy verified by lab"}, headers=auth("quality"))
    assert r.status_code == 200 and r.json()["status"] == "CLOSED", r.text
    assert [x["status"] for x in await _stock_rows(client, gr["lot_number"])] == ["UNRESTRICTED"]


async def test_incoming_inspection_reject_and_return(client: AsyncClient) -> None:
    pid = await plant_id(client)
    locs = {x["code"]: x for x in (await client.get("/inventory/locations", params={"plant_id": pid}, headers=auth("supervisor"))).json()}
    seal = (await client.get("/materials", params={"search": "CP-SEAL-12"}, headers=auth("supervisor"))).json()[0]

    async def receive(qty: str) -> dict:
        r = await client.post("/inventory/receipts", json={"plant_id": pid, "material_id": seal["id"], "quantity": qty,
                                                           "location_id": locs["0001"]["id"], "reason": "DN-4711"},
                              headers=auth("supervisor"))
        assert r.status_code == 201, r.text
        return r.json()

    rec = await receive("100")
    assert rec["to_status"] == "QUALITY" and rec["inspection_lot_number"]
    assert [x["status"] for x in await _stock_rows(client, rec["lot_number"])] == ["QUALITY"]
    detail = await _lot_detail(client, rec["inspection_lot_id"])
    assert detail["inspection_type"] == "INCOMING" and detail["sample_size"] == 2 and detail["results_expected"] == 4
    r = await client.post(f"/quality/inspection-lots/{rec['inspection_lot_id']}/results",
                          json=_results(detail, {"Inner": "12.0", "Surface": True}, 1), headers=auth("quality"))
    assert r.json()["status"] == "OPEN" and r.json()["results_recorded"] == 2
    r = await client.post(f"/quality/inspection-lots/{rec['inspection_lot_id']}/results",
                          json=_results(detail, {"Inner": "12.3", "Surface": True}, 2), headers=auth("quality"))
    assert r.json()["status"] == "FAILED"
    nc = r.json()["nonconformances"][0]

    # a failed lot can't be accepted by usage decision; it is dispositioned: back to the supplier (350 + 122)
    r = await client.post(f"/quality/inspection-lots/{rec['inspection_lot_id']}/usage-decision",
                          json={"decision": "ACCEPTED", "reason": "x"}, headers=auth("quality"))
    assert r.status_code == 409
    assert (await client.post(f"/quality/nonconformances/{nc['id']}/disposition",
                              json={"disposition": "RETURN_TO_SUPPLIER", "reason": "x"}, headers=auth("supervisor"))).status_code == 403
    r = await client.post(f"/quality/nonconformances/{nc['id']}/disposition",
                          json={"disposition": "RETURN_TO_SUPPLIER", "reason": "ID out of tolerance, supplier notified"},
                          headers=auth("quality"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "CLOSED" and r.json()["stock"] == []
    types = [m["movement_type"] for m in await _movements(client) if m["lot_number"] == rec["lot_number"]]
    assert sorted(types) == ["122", "350", "501"]
    il = await _lot_detail(client, rec["inspection_lot_id"])
    assert il["usage_decision"] == "REJECTED"

    # a good delivery is released by usage decision
    rec2 = await receive("50")
    d2 = await _lot_detail(client, rec2["inspection_lot_id"])
    for n in (1, 2):
        await client.post(f"/quality/inspection-lots/{d2['id']}/results",
                          json=_results(d2, {"Inner": "12.05", "Surface": True}, n), headers=auth("quality"))
    r = await client.post(f"/quality/inspection-lots/{d2['id']}/usage-decision",
                          json={"decision": "ACCEPTED", "reason": "conforming"}, headers=auth("quality"))
    assert r.status_code == 200, r.text
    assert [x["status"] for x in await _stock_rows(client, rec2["lot_number"])] == ["UNRESTRICTED"]


async def test_operator_defect_report_is_idempotent_and_critical_holds(client: AsyncClient, consumer: InboxConsumer) -> None:
    wo = await dispatched_order(client, consumer, work_order_payload(qty="3"))
    term = await terminal_login(client, "T-L1", "2222")
    cid = str(uuid4())
    body = {"work_order_id": wo["id"], "operation_id": wo["operations"][0]["id"], "defect_code": "SURFACE",
            "severity": "MINOR", "description": "Chatter marks on face", "quantity": "1", "client_event_id": cid}
    r1 = await client.post("/quality/nonconformances", json=body, headers=term)
    r2 = await client.post("/quality/nonconformances", json=body, headers=term)
    assert r1.status_code == 201 and r2.status_code == 201, r1.text
    assert r1.json()["id"] == r2.json()["id"] and r1.json()["source"] == "OPERATOR" and r1.json()["op_no"] == "0010"
    assert (await client.get(f"/work-orders/{wo['id']}", headers=term)).json()["status"] == "DISPATCHED"

    r = await client.post("/quality/nonconformances", json={**body, "severity": "CRITICAL", "client_event_id": str(uuid4()),
                                                             "description": "Crack in housing"}, headers=term)
    assert r.status_code == 201, r.text
    assert (await client.get(f"/work-orders/{wo['id']}", headers=term)).json()["status"] == "ON_HOLD"
    r = await client.post("/quality/nonconformances", json={**body, "defect_code": "NOPE", "client_event_id": str(uuid4())}, headers=term)
    assert r.status_code == 422

    # without a lot there is no stock to move: the disposition just records the decision
    r = await client.post(f"/quality/nonconformances/{r1.json()['id']}/disposition",
                          json={"disposition": "SCRAP", "reason": "reported as scrap in the confirmation"}, headers=auth("quality"))
    assert r.status_code == 200 and r.json()["status"] == "CLOSED" and r.json()["dispositioned_by"] == "Quinn Quality"


async def test_blocked_and_quality_stock_is_not_issuable(client: AsyncClient, consumer: InboxConsumer) -> None:
    wo = await dispatched_order(client, consumer, work_order_payload(qty="2"))
    comp = (await client.get(f"/work-orders/{wo['id']}/materials", headers=auth("supervisor"))).json()["components"][0]
    lot = comp["available"][0]
    r = await client.post(f"/quality/lots/{lot['lot_id']}/quarantine", json={"reason": "suspect heat number"}, headers=auth("quality"))
    assert r.status_code == 200, r.text
    nc = r.json()
    assert nc["source"] == "MANUAL" and Decimal(nc["blocked_qty"]) > 0
    comp2 = (await client.get(f"/work-orders/{wo['id']}/materials", headers=auth("supervisor"))).json()["components"][0]
    assert lot["lot_id"] not in {a["lot_id"] for a in comp2["available"]}
    r = await client.post(f"/work-orders/{wo['id']}/components/{comp['component_id']}/issues",
                          json={"lot_id": lot["lot_id"], "from_location_id": lot["location_id"], "quantity": "1"},
                          headers=auth("supervisor"))
    assert r.status_code == 409 and r.json()["code"] == "insufficient_stock"
    # released again (use as is): stock is back and issuable
    r = await client.post(f"/quality/nonconformances/{nc['id']}/disposition",
                          json={"disposition": "USE_AS_IS", "reason": "lab: heat number within spec"}, headers=auth("quality"))
    assert r.status_code == 200, r.text
    r = await client.post(f"/work-orders/{wo['id']}/components/{comp['component_id']}/issues",
                          json={"lot_id": lot["lot_id"], "from_location_id": lot["location_id"], "quantity": "1"},
                          headers=auth("supervisor"))
    assert r.status_code == 201, r.text
