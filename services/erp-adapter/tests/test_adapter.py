from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import func, select, update

from erp_adapter.container import Container
from erp_adapter.db import messaging, outbound_attempt, outbound_message
from erp_adapter.outbound import (
    EVENT_CONFIRMATION_CREATED,
    EVENT_CONFIRMATION_RESULT,
    EVENT_GOODS_MOVEMENT_CREATED,
    EVENT_GOODS_MOVEMENT_RESULT,
    ConfirmationIntake,
    GoodsMovementIntake,
)
from mes_common.events import EventEnvelope
from mes_common.ids import new_id
from mock_sap.store import Store

pytestmark = pytest.mark.db


# ------------------------------------------------------------------------------------------------ helpers


async def _events(c: Container, event_type: str, key_suffix: str) -> list[dict]:
    async with c.session_factory() as s:
        rows = (
            await s.execute(
                select(messaging.outbox.c.envelope)
                .where(messaging.outbox.c.event_type == event_type, messaging.outbox.c.key.like(f"%{key_suffix}"))
                .order_by(messaging.outbox.c.seq)
            )
        ).scalars().all()
    return list(rows)


async def _intake(c: Container, order: str, op: str = "0010", **overrides: object) -> UUID:
    conf_id = new_id()
    payload = {
        "confirmation_id": str(conf_id), "erp_plant": "1000", "erp_order_number": order, "op_no": op,
        "work_center": "WC-TURN", "yield_qty": "5", "scrap_qty": "1", "rework_qty": "0", "uom": "PC",
        "setup_min": "30", "run_min": "20", "labor_min": "5", "final": False, **overrides,
    }
    env = EventEnvelope(event_type=EVENT_CONFIRMATION_CREATED, source="mes-core", key=f"1000:{order}", payload=payload)
    async with c.session_factory() as s, s.begin():
        await ConfirmationIntake(c.settings)(s, env)
    return conf_id


async def _message(c: Container, msg_id: UUID):  # type: ignore[no-untyped-def]
    async with c.session_factory() as s:
        return (await s.execute(select(outbound_message).where(outbound_message.c.id == msg_id))).one()


async def _make_due(c: Container, msg_id: UUID) -> None:
    async with c.session_factory() as s, s.begin():
        await s.execute(update(outbound_message).where(outbound_message.c.id == msg_id).values(next_attempt_at=func.now()))


def _sap_confirmations(store: Store, conf_id: UUID) -> int:
    return sum(1 for c in store.confirmations if c.text == f"MES:{conf_id}")


def _released_order(store: Store) -> str:
    return store.generate_orders(1, release_ratio=1.0)[0]


# ------------------------------------------------------------------------------------------------ inbound


async def test_poll_maps_orders_and_publishes_each_change_once(container: Container, sap_store: Store) -> None:
    orders = sap_store.generate_orders(7, release_ratio=1.0)  # > page size: exercises paging
    await container.poller.poll_all()
    for o in orders:
        evs = await _events(container, "erp.work_order.upserted", f":{o}")
        assert len(evs) == 1
    wo = (await _events(container, "erp.work_order.upserted", f":{orders[0]}"))[0]["payload"]
    po = sap_store.orders[orders[0]]
    assert wo["erp_plant"] == "1000" and wo["erp_status"] == ["REL"]
    assert wo["material"]["number"] == po.material and wo["material"]["description"]
    assert [op["op_no"] for op in wo["operations"]] == [op.op for op in po.operations]
    assert Decimal(wo["operations"][0]["setup_min"]) == Decimal(str(po.operations[0].setup_min))
    assert wo["components"] and wo["components"][0]["reservation_no"] == po.components[0].reservation

    assert await container.poller.poll_all() == 0  # nothing changed: nothing published

    po.technically_completed = True
    po.touch()
    assert await container.poller.poll_all() == 1
    latest = (await _events(container, "erp.work_order.upserted", f":{orders[0]}"))[-1]["payload"]
    assert "TECO" in latest["erp_status"]


# ------------------------------------------------------------------------------------------------ outbound


async def test_confirmation_is_posted_and_result_published(container: Container, sap_store: Store) -> None:
    order = _released_order(sap_store)
    conf_id = await _intake(container, order)
    await _intake_again_is_noop(container, conf_id, order)

    assert await container.dispatcher.run_once() >= 1
    msg = await _message(container, conf_id)
    assert msg.status == "POSTED" and msg.attempts == 1 and not msg.reconciled
    assert _sap_confirmations(sap_store, conf_id) == 1
    result = (await _events(container, EVENT_CONFIRMATION_RESULT, f":{order}"))[-1]["payload"]
    assert result["confirmation_id"] == str(conf_id) and result["status"] == "POSTED"
    assert result["erp_confirmation_group"] == msg.erp_result["erp_confirmation_group"]


async def _intake_again_is_noop(c: Container, conf_id: UUID, order: str) -> None:
    env = EventEnvelope(
        event_type=EVENT_CONFIRMATION_CREATED, source="mes-core", key=f"1000:{order}",
        payload={"confirmation_id": str(conf_id), "erp_plant": "1000", "erp_order_number": order, "op_no": "0010",
                 "yield_qty": "999", "uom": "PC"},
    )
    async with c.session_factory() as s, s.begin():
        await ConfirmationIntake(c.settings)(s, env)
    assert Decimal((await _message(c, conf_id)).payload["yield_qty"]) == 5  # first write wins


async def test_lock_error_is_retried(container: Container, sap_store: Store) -> None:
    order = _released_order(sap_store)
    conf_id = await _intake(container, order)
    sap_store.faults.fail_next_lock = 1
    await container.dispatcher.run_once()
    msg = await _message(container, conf_id)
    assert msg.status == "RETRY_WAIT" and "CO/469" in msg.last_error
    await _make_due(container, conf_id)
    await container.dispatcher.run_once()
    msg = await _message(container, conf_id)
    assert msg.status == "POSTED" and msg.attempts == 2 and not msg.reconciled
    assert _sap_confirmations(sap_store, conf_id) == 1


async def test_timeout_after_sap_commit_is_reconciled_not_reposted(container: Container, sap_store: Store) -> None:
    """The case that makes naive retries dangerous: SAP committed, but the adapter never got the answer."""
    order = _released_order(sap_store)
    conf_id = await _intake(container, order)
    sap_store.faults.commit_then_timeout_next = 1
    await container.dispatcher.run_once()
    msg = await _message(container, conf_id)
    assert msg.status == "RETRY_WAIT" and "AMBIGUOUS" in msg.last_error
    assert _sap_confirmations(sap_store, conf_id) == 1  # SAP has it already

    await _make_due(container, conf_id)
    await container.dispatcher.run_once()
    msg = await _message(container, conf_id)
    assert msg.status == "POSTED" and msg.reconciled
    assert _sap_confirmations(sap_store, conf_id) == 1  # and still exactly once
    async with container.session_factory() as s:
        outcomes = (
            await s.execute(
                select(outbound_attempt.c.outcome)
                .where(outbound_attempt.c.message_id == conf_id)
                .order_by(outbound_attempt.c.attempt_no)
            )
        ).scalars().all()
    assert outcomes == ["AMBIGUOUS", "RECONCILED"]


async def test_business_rejection_fails_permanently(container: Container, sap_store: Store) -> None:
    order = _released_order(sap_store)
    conf_id = await _intake(container, order, op="0990")
    await container.dispatcher.run_once()
    msg = await _message(container, conf_id)
    assert msg.status == "FAILED" and "RU/002" in msg.last_error
    result = (await _events(container, EVENT_CONFIRMATION_RESULT, f":{order}"))[-1]["payload"]
    assert result["status"] == "FAILED"


async def test_expired_csrf_token_is_refetched(container: Container, sap_store: Store) -> None:
    order = _released_order(sap_store)
    conf_id = await _intake(container, order)
    sap_store.csrf_tokens.clear()  # SAP session expired
    await container.dispatcher.run_once()
    assert (await _message(container, conf_id)).status == "POSTED"


async def test_messages_for_one_order_go_out_in_order(container: Container, sap_store: Store) -> None:
    order = _released_order(sap_store)
    first = await _intake(container, order)
    second = await _intake(container, order, op="0020")
    sap_store.faults.fail_next_503 = 1
    await container.dispatcher.run_once()  # first fails (503); second must wait behind it
    assert (await _message(container, first)).status == "RETRY_WAIT"
    assert (await _message(container, second)).status == "PENDING"
    await _make_due(container, first)
    await container.dispatcher.run_once()
    await container.dispatcher.run_once()
    assert (await _message(container, first)).status == "POSTED"
    assert (await _message(container, second)).status == "POSTED"


# ------------------------------------------------------------------------------------------------ goods movements


async def _movement(c: Container, **fields: object) -> UUID:
    mv_id = new_id()
    payload = {"movement_id": str(mv_id), "erp_plant": "1000", "uom": "PC", "posting_date": "2026-09-24", **fields}
    key = f"1000:{fields.get('erp_order_number') or 'MAT:' + str(fields['material'])}"
    env = EventEnvelope(event_type=EVENT_GOODS_MOVEMENT_CREATED, source="mes-core", key=key, payload=payload)
    async with c.session_factory() as s, s.begin():
        await GoodsMovementIntake(c.settings)(s, env)
    return mv_id


def _sap_documents(store: Store, mv_id: UUID) -> list:
    return [d for d in store.material_documents if d.header_text == f"MES:{mv_id}"]


async def test_goods_issue_and_receipt_are_posted_as_material_documents(container: Container, sap_store: Store) -> None:
    order = _released_order(sap_store)
    po = sap_store.orders[order]
    comp = po.components[0]
    issue = await _movement(
        container, movement_type="261", material=comp.material, quantity="5", uom=comp.unit, storage_location="0002",
        batch="L000000001", erp_order_number=order, reservation_no=comp.reservation, reservation_item=comp.reservation_item,
    )
    receipt = await _movement(
        container, movement_type="101", material=po.material, quantity="3", storage_location="0010", batch="L000000002",
        erp_order_number=order,
    )
    await container.dispatcher.run_once()
    # same order key: the receipt waits until the issue is through (in-order per order), then goes next pass
    assert (await _message(container, issue)).status == "POSTED"
    assert (await _message(container, receipt)).status == "PENDING"
    await container.dispatcher.run_once()
    for mv in (issue, receipt):
        assert (await _message(container, mv)).status == "POSTED"
        assert len(_sap_documents(sap_store, mv)) == 1
    assert Decimal(comp.withdrawn_qty) == 5 and Decimal(po.delivered_qty) == 3
    doc = _sap_documents(sap_store, issue)[0]
    assert doc.movement_code == "03" and doc.items[0]["Reservation"] == comp.reservation
    result = (await _events(container, EVENT_GOODS_MOVEMENT_RESULT, f":{order}"))[-1]["payload"]
    assert result["movement_id"] == str(receipt) and result["erp_material_document"] == _sap_documents(sap_store, receipt)[0].document


async def test_goods_movement_timeout_after_commit_is_reconciled(container: Container, sap_store: Store) -> None:
    mv = await _movement(
        container, movement_type="311", material="CP-SEAL-12", quantity="40", storage_location="0001",
        destination_storage_location="0003", batch="L000000003",
    )
    sap_store.faults.commit_then_timeout_next = 1
    await container.dispatcher.run_once()
    assert (await _message(container, mv)).status == "RETRY_WAIT"
    await _make_due(container, mv)
    await container.dispatcher.run_once()
    msg = await _message(container, mv)
    assert msg.status == "POSTED" and msg.reconciled
    docs = _sap_documents(sap_store, mv)
    assert len(docs) == 1 and docs[0].items[0]["IssuingOrReceivingStorageLoc"] == "0003"


async def test_goods_movement_business_error_fails(container: Container, sap_store: Store) -> None:
    mv = await _movement(container, movement_type="501", material="NOT-A-MATERIAL", quantity="1", storage_location="0001")
    await container.dispatcher.run_once()
    msg = await _message(container, mv)
    assert msg.status == "FAILED" and "M7/021" in msg.last_error


async def test_quality_stock_serials_and_status_changes(container: Container, sap_store: Store) -> None:
    order = _released_order(sap_store)
    po = sap_store.orders[order]
    receipt = await _movement(
        container, movement_type="101", material=po.material, quantity="2", storage_location="0010", batch="L000000009",
        erp_order_number=order, stock_type="QUALITY", serial_numbers=["FG-1001-0000001", "FG-1001-0000002"],
    )
    release = await _movement(container, movement_type="321", material=po.material, quantity="2", storage_location="0010",
                              batch="L000000009", reason="UD Q00000001: accepted")
    scrap = await _movement(container, movement_type="555", material="CP-SEAL-12", quantity="10", storage_location="0001",
                            batch="L000000010", reason="NC0000001 scrap")
    bad = await _movement(container, movement_type="101", material=po.material, quantity="2", storage_location="0010",
                          erp_order_number=order, serial_numbers=["ONLY-ONE"])
    for _ in range(3):
        await container.dispatcher.run_once()
    for mv in (receipt, release, scrap):
        assert (await _message(container, mv)).status == "POSTED", mv
    item = _sap_documents(sap_store, receipt)[0].items[0]
    assert item["InventoryStockType"] == "02"
    assert [x["SerialNumber"] for x in item["to_SerialNumbers"]["results"]] == ["FG-1001-0000001", "FG-1001-0000002"]
    doc = _sap_documents(sap_store, release)[0]
    assert doc.movement_code == "04" and doc.items[0]["MaterialDocumentItemText"] == "UD Q00000001: accepted"
    doc = _sap_documents(sap_store, scrap)[0]
    assert doc.movement_code == "03" and doc.items[0]["InventoryStockType"] == "07"
    msg = await _message(container, bad)
    assert msg.status == "FAILED" and "IO/220" in msg.last_error


# ------------------------------------------------------------------------------------------------ plant maintenance


async def _pm(c: Container, event_type: str, payload: dict) -> None:
    from erp_adapter.outbound import INTAKE_EVENTS, KINDS, Intake

    env = EventEnvelope(event_type=event_type, source="mes-core", key="1000:pm", payload={"erp_plant": "1000", **payload})
    async with c.session_factory() as s, s.begin():
        await Intake(c.settings, KINDS[INTAKE_EVENTS[event_type]])(s, env)


async def test_pm_notification_order_parts_and_teco(container: Container, sap_store: Store) -> None:
    notif_id, order_id, status_id = new_id(), new_id(), new_id()
    await _pm(container, "mes.maintenance.notification_created", {
        "notification_id": str(notif_id), "number": "20000901", "notif_type": "M2", "priority": "1",
        "text": "Spindle drive fault F07", "equipment": "MILL-02", "breakdown": True,
        "malfunction_start": "2026-09-24T08:00:00+00:00", "reported_by": "Olga Operator"})
    await _pm(container, "mes.maintenance.order_created", {
        "order_id": str(order_id), "number": "4000901", "order_type": "PM01", "description": "Spindle drive fault F07",
        "equipment": "MILL-02", "work_center": "WC-MILL", "notification_number": "20000901", "priority": "1",
        "planned_start": "2026-09-24T08:10:00+00:00", "duration_min": 120})
    parts = await _movement(container, movement_type="261", material="SP-BEARING-6205", quantity="2", storage_location="0001",
                            batch="L000000099", erp_order_number="4000901")
    # SAP commits the notification but the answer times out: the retry must find it, not create it twice
    sap_store.faults.commit_then_timeout_next = 1
    for _ in range(3):
        await container.dispatcher.run_once()
    await _make_due(container, notif_id)
    for _ in range(3):
        await container.dispatcher.run_once()
    msg = await _message(container, notif_id)
    assert msg.status == "POSTED" and msg.reconciled
    assert (await _message(container, order_id)).status == "POSTED"
    assert (await _message(container, parts)).status == "POSTED"  # same key as the order: after it
    assert sap_store.maint_orders["4000901"].notification == "20000901"
    assert sap_store.maint_notifications["20000901"].status == "NOPR"

    await _pm(container, "mes.maintenance.order_status", {
        "message_id": str(status_id), "order_id": str(order_id), "number": "4000901", "status": "TECO",
        "labor_min": "35", "findings": "Drive fuse replaced"})
    await container.dispatcher.run_once()
    assert (await _message(container, status_id)).status == "POSTED"
    o = sap_store.maint_orders["4000901"]
    assert o.system_status == "TECO" and o.findings == "Drive fuse replaced" and o.labor_min == "35"
    assert sap_store.maint_notifications["20000901"].status == "NOCO"
    result = (await _events(container, "erp.maintenance.result", ":4000901"))[-1]["payload"]
    assert result["message_type"] == "MAINT_ORDER_STATUS" and result["order_id"] == str(order_id)
    assert result["system_status"] == "TECO"

    # a preventive order TECO'd directly in SAP comes back through the PM poller
    await _pm(container, "mes.maintenance.order_created", {
        "order_id": str(new_id()), "number": "4000902", "order_type": "PM02", "description": "Weekly washer filter",
        "equipment": "DEB-01", "priority": "3", "planned_start": "2026-09-25T06:00:00+00:00", "duration_min": 30})
    await container.dispatcher.run_once()
    from mock_sap.pm import _set_status

    with sap_store.lock:
        _set_status(sap_store, sap_store.maint_orders["4000902"], "TECO", {})
    await container.pm_poller.poll_all()
    updates = [e["payload"] for e in await _events(container, "erp.maintenance_order.updated", ":4000902")]
    assert updates and updates[-1]["system_status"] == "TECO"
    assert await container.pm_poller.poll_all() == 0  # nothing new
