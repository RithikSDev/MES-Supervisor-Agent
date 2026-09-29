"""End-to-end smoke test against the running docker-compose stack (real Keycloak, Redpanda, mock SAP).

Walks the demo script:
  Phase 1: SAP orders -> MES work orders -> draft schedule -> auto-sequence -> publish (e-signature) -> dispatch ->
    confirm an operation while SAP "commits then times out" -> adapter reconciles -> exactly one SAP posting.
  Phase 2: terminal PIN login, material issue, runs on every operation (first-off inspections passed), goods
    movements and confirmations posted to SAP, live board from Redis.
  Phase 3: incoming inspection + usage decision, final inspection of the produced lot, quarantine and
    disposition, genealogy; every stock type change posted to SAP.
  Phase 4: breakdown -> SAP PM notification and order, spare parts, technician sign-off -> TECO; a P1 support
    ticket -> ServiceNow incident -> escalated to L3 -> Jira issue; engineers' changes back in MES; MES MCP server.

Usage (repo root, stack running):  uv run python scripts/smoke_test.py
"""

import sys
import time
import uuid

import httpx

KEYCLOAK = "http://localhost:8080/realms/mes/protocol/openid-connect/token"
CORE = "http://localhost:8000/api/v1"
ERP = "http://localhost:8020/api/v1"
MOCK = "http://localhost:8030/_mock"
ITSM = "http://localhost:8070/api/v1/itsm"
MES_MCP = "http://localhost:8040/mcp"


def token(user: str) -> dict[str, str]:
    r = httpx.post(KEYCLOAK, data={"grant_type": "password", "client_id": "mes-cli", "username": user, "password": "demo"})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def wait_for(what: str, fn, timeout: float = 60, interval: float = 1.5):  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(interval)
    sys.exit(f"FAIL: timed out waiting for {what}")


def step(msg: str) -> None:
    print(f"-> {msg}")


def main() -> None:
    sup = token("supervisor1")
    op = token("operator1")
    c = httpx.Client(timeout=30)

    step("create 3 released production orders in (mock) SAP")
    created = c.post(f"{MOCK}/orders/generate", json={"count": 3, "release_ratio": 1.0}).json()["created"]
    plant_id = c.get(f"{CORE}/plants", headers=sup).json()[0]["id"]

    def arrived():  # type: ignore[no-untyped-def]
        items = c.get(f"{CORE}/work-orders", params={"plant_id": plant_id, "limit": 500}, headers=sup).json()["items"]
        got = {w["erp_order_number"]: w for w in items if w["erp_order_number"] in created}
        return got if len(got) == len(created) else None

    orders = wait_for("orders to arrive via adapter + Redpanda", arrived)
    step(f"arrived in MES: {sorted(orders)} (status {[w['status'] for w in orders.values()]})")

    step("draft schedule + auto-sequence (the order under test at priority 1: the shared stack's horizon fills up)")
    first = c.get(f"{CORE}/work-orders/{orders[created[0]]['id']}", headers=sup).json()
    c.put(f"{CORE}/work-orders/{first['id']}/priority", json={"row_version": first["row_version"], "priority": 1},
          headers=sup).raise_for_status()
    sched = c.post(f"{CORE}/schedules", json={"plant_id": plant_id, "horizon_days": 14}, headers=sup).json()
    res = c.post(f"{CORE}/schedules/{sched['id']}/auto-sequence", json={"reset_unlocked": True}, headers=sup).json()
    step(f"placed {res['placed']}, unplaced {len(res['unplaced'])}")

    step("publish with e-signature")
    r = c.post(f"{CORE}/schedules/{sched['id']}/publish", json={"reason": "Smoke test plan approval"}, headers=sup)
    if r.status_code != 200:
        sys.exit(f"FAIL: publish {r.status_code} {r.text}")

    target = orders[created[0]]
    wo = c.get(f"{CORE}/work-orders/{target['id']}", headers=sup).json()
    assert wo["status"] == "SCHEDULED", wo["status"]
    wo = c.post(f"{CORE}/work-orders/{wo['id']}/dispatch", json={"row_version": wo["row_version"]}, headers=sup).json()
    step(f"dispatched {wo['erp_order_number']} -> {wo['status']}")

    # the fault hits the next SAP POST of any kind: let the adapter's queue drain first
    wait_for("ERP adapter queue to be idle", lambda: not c.get(f"{ERP}/outbound", params={"status": ["PENDING", "SENDING"]},
                                                               headers=sup).json(), timeout=90)
    step("inject fault: SAP commits the next confirmation but answers too late")
    c.put(f"{MOCK}/faults", json={"commit_then_timeout_next": 1, "hang_seconds": 8, "fault_scope": "confirmation"})

    first_op = wo["operations"][0]
    conf = c.post(
        f"{CORE}/work-orders/{wo['id']}/operations/{first_op['id']}/confirmations",
        json={"yield_qty": "5", "scrap_qty": "1", "run_min": "20"},
        headers=op,
    ).json()
    step(f"operator confirmed {wo['erp_order_number']}/{first_op['op_no']}: confirmation {conf['id']}")

    def posted():  # type: ignore[no-untyped-def]
        cs = c.get(f"{CORE}/work-orders/{wo['id']}/confirmations", headers=sup).json()
        mine = next(x for x in cs if x["id"] == conf["id"])
        return mine if mine["erp_sync_status"] != "PENDING" else None

    result = wait_for("SAP posting result (includes a ~5 s timeout + backoff)", posted, timeout=90)
    msg = c.get(f"{ERP}/outbound/{conf['id']}", headers=sup).json()
    outcomes = [a["outcome"] for a in msg["attempts_log"]]
    postings = [x for x in c.get(f"{MOCK}/confirmations").json() if x["text"] == f"MES:{conf['id']}"]
    step(f"MES sees: {result['erp_sync_status']} {result['erp_confirmation_group']}/{result['erp_confirmation_count']} reconciled={result['erp_reconciled']}")
    step(f"adapter attempts: {outcomes}")
    step(f"SAP postings with this MES key: {len(postings)}")

    results = {"phase 1": result["erp_sync_status"] == "POSTED" and result["erp_reconciled"] and len(postings) == 1}
    results["phase 2"], wo2 = phase2(c, sup, plant_id)
    results["phase 3"] = phase3(c, sup, plant_id, wo2)
    results["phase 4"] = phase4(c, sup, plant_id)
    verify = c.get(f"{CORE}/audit/verify", headers=token("admin1")).json()
    step(f"audit chain: ok={verify['ok']} checked={verify['checked']} unsealed={verify['unsealed']}")
    results["audit chain"] = verify["ok"]
    ok = all(results.values())
    print("PASS" if ok else "FAIL: " + ", ".join(k for k, v in results.items() if not v))
    sys.exit(0 if ok else 1)


L1_MATERIALS = {"FG-1001", "FG-1002", "SF-1003"}  # routed through the machining line


def pass_inspection(c: httpx.Client, headers: dict[str, str], il_id: str) -> dict:
    """Record conforming results for every characteristic and sample (target, else mid-tolerance, else OK)."""
    d = c.get(f"{CORE}/quality/inspection-lots/{il_id}", headers=headers).json()
    results = []
    for ch in d["characteristics"]:
        for n in range(1, d["sample_size"] + 1):
            if ch["kind"] == "QUALITATIVE":
                results.append({"characteristic_id": ch["id"], "sample_no": n, "attribute_ok": True})
            else:
                lo, hi = ch["lower_limit"], ch["upper_limit"]
                v = ch["target"] or (str((float(lo) + float(hi)) / 2) if lo and hi else lo or hi)
                results.append({"characteristic_id": ch["id"], "sample_no": n, "measured_value": str(v)})
    r = c.post(f"{CORE}/quality/inspection-lots/{il_id}/results", json={"results": results}, headers=headers)
    r.raise_for_status()
    return r.json()


def phase2(c: httpx.Client, sup: dict[str, str], plant_id: str) -> tuple[bool, dict]:
    print("-- phase 2: shop floor")
    admin = token("admin1")
    term = next(t for t in c.get(f"{CORE}/terminals", params={"plant_id": plant_id}, headers=admin).json() if t["code"] == "T-L1")
    device = c.post(f"{CORE}/terminals/{term['id']}/enroll", headers=admin).json()["device_token"]
    login = c.post(f"{CORE}/terminal/login", json={"pin": "1111"}, headers={"X-Terminal-Device": device}).json()
    op = {"Authorization": f"Bearer {login['access_token']}"}
    step(f"enrolled {term['code']}, PIN login as {login['operator']['display_name']}")

    # an order that runs on line L1
    order = None
    while order is None:
        created = c.post(f"{MOCK}/orders/generate", json={"count": 3, "release_ratio": 1.0}).json()["created"]
        materials = {x["order"]: x["material"] for x in c.get(f"{MOCK}/orders").json()}
        order = next((o for o in created if materials[o] in L1_MATERIALS), None)

    def arrived():  # type: ignore[no-untyped-def]
        items = c.get(f"{CORE}/work-orders", params={"search": order}, headers=sup).json()["items"]
        return items[0] if items else None

    wo = wait_for("L1 order to arrive", arrived)
    wo = c.get(f"{CORE}/work-orders/{wo['id']}", headers=sup).json()
    c.put(f"{CORE}/work-orders/{wo['id']}/priority", json={"row_version": wo["row_version"], "priority": 1},
          headers=sup).raise_for_status()
    sid = c.post(f"{CORE}/schedules", json={"plant_id": plant_id, "horizon_days": 14}, headers=sup).json()["id"]
    c.post(f"{CORE}/schedules/{sid}/auto-sequence", json={"reset_unlocked": True}, headers=sup)
    r = c.post(f"{CORE}/schedules/{sid}/publish", json={"reason": "Smoke test phase 2"}, headers=sup)
    assert r.status_code == 200, r.text
    wo = c.get(f"{CORE}/work-orders/{wo['id']}", headers=sup).json()
    c.post(f"{CORE}/work-orders/{wo['id']}/dispatch", json={"row_version": wo["row_version"]}, headers=sup).raise_for_status()
    step(f"order {order} ({wo['material_number']}) scheduled and dispatched")

    # issue the raw material, then work every operation on the terminal
    mats = c.get(f"{CORE}/work-orders/{wo['id']}/materials", headers=op).json()
    for comp in (x for x in mats["components"] if not x["backflush"]):
        lot = comp["available"][0]
        need = min(float(comp["remaining_qty"]), float(lot["quantity"]))
        r = c.post(
            f"{CORE}/work-orders/{wo['id']}/components/{comp['component_id']}/issues",
            json={"lot_id": lot["lot_id"], "from_location_id": lot["location_id"], "quantity": str(need)},
            headers=op,
        )
        r.raise_for_status()
        step(f"issued {need} {comp['uom']} {comp['material_number']} lot {lot['lot_number']} from {lot['location_code']}")
    good = int(float(wo["planned_qty"]))
    for o in wo["operations"]:
        queue = c.get(f"{CORE}/execution/queue", headers=op).json()
        item = next(i for i in queue["items"] if i["operation_id"] == o["id"])
        free = [m["id"] for m in queue["machines"] if m["work_center_id"] == item["work_center_id"] and not m["busy_run_id"]]
        machine = item["planned_equipment_id"] if item["planned_equipment_id"] in free else free[0]
        run = c.post(
            f"{CORE}/execution/work-orders/{wo['id']}/operations/{o['id']}/runs",
            json={"equipment_id": machine, "phase": "SETUP"}, headers=op,
        ).json()
        started = c.post(f"{CORE}/execution/runs/{run['id']}/start-production", json={}, headers=op)
        started.raise_for_status()
        if (insp := started.json()["inspection"]) and insp["status"] == "OPEN":
            res = pass_inspection(c, op, insp["id"])
            step(f"op {o['op_no']}: first-off inspection {res['number']} {res['status']} (entered on the terminal)")
        report_body = {"yield_qty": "2", "client_event_id": str(uuid.uuid4())}
        c.post(f"{CORE}/execution/runs/{run['id']}/report", json=report_body, headers=op).raise_for_status()
        replay = c.post(f"{CORE}/execution/runs/{run['id']}/report", json=report_body, headers=op).json()
        done = c.post(f"{CORE}/execution/runs/{run['id']}/report", json={"yield_qty": str(good - 2), "final": True}, headers=op).json()
        step(f"op {o['op_no']} on {run['equipment_code']}: setup, run, report 2 (replay ignored: {replay['replayed']}), complete ({done['status']})")
    confs = c.get(f"{CORE}/work-orders/{wo['id']}/confirmations", headers=sup).json()
    ok = len(confs) == 2 * len(wo["operations"])

    def all_posted():  # type: ignore[no-untyped-def]
        m = c.get(f"{CORE}/work-orders/{wo['id']}/materials", headers=sup).json()["movements"]
        cs = c.get(f"{CORE}/work-orders/{wo['id']}/confirmations", headers=sup).json()
        pending = [x for x in m if x["erp_sync_status"] == "PENDING"] + [x for x in cs if x["erp_sync_status"] == "PENDING"]
        return (m, cs) if not pending else None

    movements, confs = wait_for("goods movements + confirmations to post in SAP", all_posted, timeout=90)
    types = sorted({f"{m['movement_type']}:{m['erp_sync_status']}" for m in movements})
    step(f"SAP: movements {types}; confirmations {sorted({x['erp_sync_status'] for x in confs})}")
    docs = {d["header_text"] for d in c.get(f"{MOCK}/material-documents").json()}
    ok = ok and all(f"MES:{m['id']}" in docs for m in movements)
    ok = ok and all(m["erp_sync_status"] == "POSTED" for m in movements) and any(m["movement_type"] == "101" for m in movements)
    final = c.get(f"{CORE}/work-orders/{wo['id']}", headers=sup).json()
    step(f"work order {final['status']}, good {final['yield_qty']}")
    live = c.get(f"{CORE}/execution/live", params={"plant_id": plant_id}, headers=sup).json()
    step(f"live board source: {live['source']} ({len(live['machines'])} machines)")
    ok = ok and final["status"] == "COMPLETED" and live["source"] == "redis"
    c.post(f"{CORE}/terminal/logout", headers=op)
    return ok, final


def phase3(c: httpx.Client, sup: dict[str, str], plant_id: str, wo: dict) -> bool:
    print("-- phase 3: quality and traceability")
    qe = token("quality1")
    ok = True
    locs = {x["code"]: x["id"] for x in c.get(f"{CORE}/inventory/locations", params={"plant_id": plant_id}, headers=sup).json()}
    seal = c.get(f"{CORE}/materials", params={"search": "CP-SEAL-12"}, headers=sup).json()[0]
    rec = c.post(f"{CORE}/inventory/receipts", json={"plant_id": plant_id, "material_id": seal["id"], "quantity": "200",
                                                       "location_id": locs["0001"], "reason": "smoke DN-1"}, headers=sup).json()
    step(f"received 200 CP-SEAL-12 lot {rec['lot_number']} into {rec['to_status']} stock, inspection {rec['inspection_lot_number']}")
    ok = ok and rec["to_status"] == "QUALITY" and bool(rec["inspection_lot_id"])
    res = pass_inspection(c, qe, rec["inspection_lot_id"])
    ud = c.post(f"{CORE}/quality/inspection-lots/{rec['inspection_lot_id']}/usage-decision",
                json={"decision": "ACCEPTED", "reason": "smoke: conforming delivery"}, headers=qe).json()
    step(f"incoming inspection {res['status']}, usage decision {ud['usage_decision']} by {ud['decided_by']}")
    ok = ok and ud["usage_decision"] == "ACCEPTED"

    # the produced lot of the phase 2 order: final inspection (if the material has a plan), then quarantine
    finals = c.get(f"{CORE}/quality/inspection-lots", params={"plant_id": plant_id, "work_order_id": wo["id"],
                                                             "inspection_type": ["FINAL"]}, headers=qe).json()
    for f in finals:
        pass_inspection(c, qe, f["id"])
        c.post(f"{CORE}/quality/inspection-lots/{f['id']}/usage-decision",
               json={"decision": "ACCEPTED", "reason": "smoke: final inspection passed"}, headers=qe).raise_for_status()
    step(f"final inspection lots of {wo['erp_order_number']}: {[f['number'] for f in finals] or 'none (no plan)'}")
    produced = c.get(f"{CORE}/trace/orders/{wo['id']}", params={"direction": "forward"}, headers=qe).json()["root"]["children"]
    lot = produced[0]
    back = c.get(f"{CORE}/trace/lots/{lot['id']}", headers=qe).json()["root"]
    comps = [n["label"] for n in back["children"][0]["children"]]
    step(f"genealogy: lot {lot['label']} <- order {back['children'][0]['label']} <- component lots {comps}; serials {lot['lot']['serial_count']}")
    ok = ok and back["children"][0]["id"] == wo["id"] and bool(comps)
    nc = c.post(f"{CORE}/quality/lots/{lot['id']}/quarantine", json={"reason": "smoke: customer complaint"}, headers=qe).json()
    nc = c.post(f"{CORE}/quality/nonconformances/{nc['id']}/disposition",
                json={"disposition": "USE_AS_IS", "reason": "smoke: complaint not confirmed"}, headers=qe).json()
    step(f"quarantine {nc['number']} blocked {nc['blocked_qty']}, disposition {nc['disposition']} -> {nc['status']}")

    def posted():  # type: ignore[no-untyped-def]
        mv = [m for m in c.get(f"{CORE}/inventory/movements", params={"plant_id": plant_id, "limit": 300}, headers=sup).json()
              if m["lot_number"] in (rec["lot_number"], lot["label"]) and m["movement_type"] in ("501", "321", "344", "343")]
        return mv if mv and all(m["erp_sync_status"] != "PENDING" for m in mv) else None

    mvs = wait_for("quality movements to post in SAP", posted, timeout=90)
    step(f"SAP: {sorted({m['movement_type'] + ':' + m['erp_sync_status'] for m in mvs})}")
    docs = {d["header_text"]: d for d in c.get(f"{MOCK}/material-documents").json()}
    receipt = next(m for m in mvs if m["movement_type"] == "501")
    stock_type = docs[f"MES:{receipt['id']}"]["items"][0].get("InventoryStockType")
    step(f"SAP receipt of {rec['lot_number']} into stock type {stock_type} (02 = quality inspection)")
    types = {m["movement_type"] for m in mvs}
    return ok and {"501", "321", "344", "343"} <= types and all(m["erp_sync_status"] == "POSTED" for m in mvs) and stock_type == "02"



def phase4(c: httpx.Client, sup: dict[str, str], plant_id: str) -> bool:
    print("-- phase 4: maintenance and support tickets")
    tech = token("maint1")
    eqs = {e["code"]: e for e in c.get(f"{CORE}/maintenance/equipment", params={"plant_id": plant_id}, headers=sup).json()}
    mill = eqs["MILL-03"]
    if mill["status"] != "AVAILABLE":  # left over from an interrupted run: finish its orders, release the machine
        for o in c.get(f"{CORE}/maintenance/orders", params={"plant_id": plant_id, "equipment_id": mill["id"],
                                                            "status": ["IN_PROGRESS"]}, headers=sup).json():
            full = c.get(f"{CORE}/maintenance/orders/{o['id']}", headers=tech).json()
            c.put(f"{CORE}/maintenance/orders/{o['id']}/work", json={"checklist": [{"text": x["text"], "done": True}
                  for x in full["checklist"]], "labor_min": "1"}, headers=tech)
            c.post(f"{CORE}/maintenance/orders/{o['id']}/complete", json={"findings": "smoke reset", "reason": "smoke reset"},
                   headers=tech)
        c.post(f"{CORE}/maintenance/equipment/{mill['id']}/back-up", json={"note": "smoke reset"}, headers=sup)
        step("reset MILL-03 left over from an earlier run")
    r = c.post(f"{CORE}/maintenance/breakdowns", json={"equipment_id": mill["id"], "reason_code": "HYDRAULIC",
               "description": "smoke: clamp pressure low", "machine_down": True}, headers=sup)
    r.raise_for_status()
    n = r.json()
    step(f"breakdown on MILL-03: notification {n['number']} (priority {n['priority']}), machine DOWN")
    mo = c.post(f"{CORE}/maintenance/notifications/{n['id']}/order", json={"checklist": ["Replace seal kit", "Pressure test"]},
                headers=sup).json()
    c.post(f"{CORE}/maintenance/orders/{mo['id']}/start", headers=tech).raise_for_status()
    kit = next(p for p in c.get(f"{CORE}/maintenance/spare-parts", params={"plant_id": plant_id}, headers=tech).json()
               if p["material_number"] == "SP-SEAL-KIT-CYL")
    if float(kit["in_stores"]) < 1:  # repeated smoke runs use up the seeded kits: restock the maintenance stores
        store = next(x["id"] for x in c.get(f"{CORE}/inventory/locations", params={"plant_id": plant_id}, headers=sup).json()
                     if x["code"] == "0001")
        c.post(f"{CORE}/inventory/receipts", json={"plant_id": plant_id, "material_id": kit["material_id"], "quantity": "8",
                                                    "location_id": store, "reason": "smoke: restock seal kits"}, headers=sup).raise_for_status()
        step("restocked 8 SP-SEAL-KIT-CYL")
    c.post(f"{CORE}/maintenance/orders/{mo['id']}/parts", json={"material_id": kit["material_id"], "quantity": "1"}, headers=tech).raise_for_status()
    c.put(f"{CORE}/maintenance/orders/{mo['id']}/work", json={"checklist": [{"text": x["text"], "done": True} for x in mo["checklist"]],
                                                            "labor_min": "40"}, headers=tech).raise_for_status()
    done = c.post(f"{CORE}/maintenance/orders/{mo['id']}/complete", json={"findings": "seal kit replaced, 60 bar ok",
                  "reason": "smoke: machine tested"}, headers=tech).json()
    step(f"order {done['number']} by maint1: 1 seal kit, 40 min, signed off -> {done['status']}")

    def pm_synced():  # type: ignore[no-untyped-def]
        o = c.get(f"{CORE}/maintenance/orders/{mo['id']}", headers=sup).json()
        return o if o["erp_system_status"] == "TECO" and all(p["erp_sync_status"] == "POSTED" for p in o["parts"]) else None

    o = wait_for("PM order TECO and spare parts posted in SAP", pm_synced, timeout=90)
    sap = c.get(f"{MOCK}/maintenance").json()
    sap_order = next(x for x in sap["orders"] if x["MaintenanceOrder"] == o["number"])
    sap_notif = next(x for x in sap["notifications"] if x["MaintenanceNotification"] == n["number"])
    step(f"SAP PM: order {sap_order['MaintenanceOrder']} {sap_order['MaintOrdProcessingStatus']} (notification "
         f"{sap_notif['NotifProcessingPhase']}), spare parts 261 posted")
    machine_ok = c.get(f"{CORE}/maintenance/equipment", params={"plant_id": plant_id}, headers=sup).json()
    ok = sap_order["MaintOrdProcessingStatus"] == "TECO" and sap_notif["NotifProcessingPhase"] == "NOCO"
    ok = ok and next(e for e in machine_ok if e["code"] == "MILL-03")["status"] == "AVAILABLE"

    t = c.post(f"{CORE}/tickets", json={"plant_id": plant_id, "title": "smoke: MES terminal frozen on line 2",
               "description": "screen does not react", "category": "MES_APP", "impact": "LINE_STOP"}, headers=sup).json()

    def ticket(pred):  # type: ignore[no-untyped-def]
        return lambda: (d := c.get(f"{CORE}/tickets/{t['id']}", headers=sup).json()) and pred(d) and d

    conns = {x["system"]: x for x in c.get(f"{ITSM}/status", headers=sup).json()["connections"]}
    sn_ok, jira_ok = conns["servicenow"]["reachable"], conns["jira"]["reachable"]
    if not sn_ok:
        step(f"ServiceNow not connected ({conns['servicenow']['error']}): ticket {t['number']} stays queued; "
             "fill .env and run the itsm setup check to include it")
    else:
        d = wait_for("ServiceNow incident", ticket(lambda d: d["sn_number"]), timeout=60)
        step(f"ticket {t['number']} {t['severity']}/{t['tier']} -> ServiceNow {d['sn_number']} ({d['sn_group']})")
        for reason in ("L1: not a local issue", "L2: needs a code fix"):
            c.post(f"{CORE}/tickets/{t['id']}/escalate", json={"reason": reason}, headers=sup).raise_for_status()
        if jira_ok:
            d = wait_for("Jira issue at L3", ticket(lambda d: d["jira_key"]), timeout=60)
            step(f"escalated to L3 -> Jira {d['jira_key']} ({d['jira_url']})")
        else:
            d = wait_for("incident in the L3 group", ticket(lambda d: d["sn_group"] and "L3" in d["sn_group"]), timeout=60)
            step(f"escalated to L3 -> ServiceNow group {d['sn_group']} (Jira not connected)")
        # clean up in the real instance: resolve the smoke ticket
        for status in ("IN_PROGRESS", "RESOLVED"):
            c.post(f"{CORE}/tickets/{t['id']}/status", json={"status": status, "note": "smoke test: resolved automatically"},
                   headers=sup)
        d = wait_for("incident resolved in ServiceNow", ticket(lambda d: d["sn_state"] in ("6", "7")), timeout=60)
        step(f"resolved in MES -> {d['sn_number']} state {d['sn_state']}; "
             "engineers' changes flow back by polling (try it in the instance)")

    try:
        import asyncio

        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        async def mcp_check() -> dict:
            async with (httpx2.AsyncClient(headers={"Authorization": "Bearer mes-mcp-demo-key"}, timeout=20) as h,
                        Client(streamable_http_client(MES_MCP, http_client=h)) as client):
                r = await client.call_tool("get_ticket", {"ticket": t["number"]})
                return r.structured_content

        got = asyncio.run(mcp_check())
        step(f"MES MCP server: get_ticket({t['number']}) -> {got['number']} {got['severity']}/{got['tier']}, "
             f"{len(got['activity'])} activity entries")
        ok = ok and got["number"] == t["number"]
    except ImportError:
        step("MES MCP check skipped (run from the workspace venv: uv sync --all-packages)")
    return ok


if __name__ == "__main__":
    main()
