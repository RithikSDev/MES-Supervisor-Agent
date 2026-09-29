from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from mes_common.messaging import InboxConsumer
from mes_core.modules.erp_sync.handlers import HANDLERS, on_session
from mes_core.modules.masterdata.models import Material
from mes_core.modules.planning.models import WorkOrder, WorkOrderComponent, WorkOrderOperation
from mes_core.shared.db.session import Database
from mes_core.shared.messaging import messaging
from tests.factories import TOPIC, envelope, work_order_payload

pytestmark = pytest.mark.db


@pytest.fixture
def consumer(db: Database) -> InboxConsumer:
    return InboxConsumer(
        messaging, db.session_factory, bootstrap_servers="unused", group_id="test", topics=[], handlers=HANDLERS,
        on_session=on_session, max_attempts=1,
    )


async def _wo(db: Database, order: str) -> WorkOrder:
    async with db.read() as s:
        return (await s.scalars(select(WorkOrder).where(WorkOrder.erp_order_number == order))).one()


async def test_released_order_is_imported_with_operations_and_components(db: Database, consumer: InboxConsumer) -> None:
    p = work_order_payload()
    await consumer.process(TOPIC, envelope(p))
    wo = await _wo(db, p["erp_order_number"])
    assert wo.status == "RELEASED"
    assert wo.created_by == "svc:erp-sync"
    async with db.read() as s:
        ops = (await s.scalars(select(WorkOrderOperation).where(WorkOrderOperation.work_order_id == wo.id).order_by(WorkOrderOperation.sequence))).all()
        comps = (await s.scalars(select(WorkOrderComponent).where(WorkOrderComponent.work_order_id == wo.id))).all()
        mats = (await s.scalars(select(Material.material_number))).all()
    assert [(o.op_no, o.sequence) for o in ops] == [("0010", 1), ("0020", 2)]
    assert len(comps) == 1 and comps[0].operation_id == ops[0].id
    assert {"FG-1001", "RM-AL-6061"} <= set(mats)


async def test_redelivered_event_is_ignored(db: Database, consumer: InboxConsumer) -> None:
    p = work_order_payload()
    raw = envelope(p)
    await consumer.process(TOPIC, raw)
    wo = await _wo(db, p["erp_order_number"])
    await consumer.process(TOPIC, raw)  # same event_id
    assert (await _wo(db, p["erp_order_number"])).row_version == wo.row_version


async def test_out_of_order_snapshot_is_discarded(db: Database, consumer: InboxConsumer) -> None:
    now = datetime.now(UTC)
    p_new = work_order_payload(changed_at=now, qty="30")
    p_old = {**p_new, "planned_qty": "10", "erp_last_changed_at": (now - timedelta(minutes=5)).isoformat()}
    await consumer.process(TOPIC, envelope(p_new))
    await consumer.process(TOPIC, envelope(p_old))
    assert (await _wo(db, p_new["erp_order_number"])).planned_qty == 30


async def test_erp_lifecycle_created_released_teco(db: Database, consumer: InboxConsumer) -> None:
    t0 = datetime.now(UTC)
    p = work_order_payload(status=["CRTD"], changed_at=t0)
    await consumer.process(TOPIC, envelope(p))
    assert (await _wo(db, p["erp_order_number"])).status == "IMPORTED"
    await consumer.process(TOPIC, envelope({**p, "erp_status": ["REL"], "erp_last_changed_at": (t0 + timedelta(seconds=1)).isoformat()}))
    assert (await _wo(db, p["erp_order_number"])).status == "RELEASED"
    await consumer.process(TOPIC, envelope({**p, "erp_status": ["REL", "TECO"], "erp_last_changed_at": (t0 + timedelta(seconds=2)).isoformat()}))
    wo = await _wo(db, p["erp_order_number"])
    assert wo.status == "CLOSED"
    async with db.read() as s:
        events = (
            await s.execute(
                select(func.count()).select_from(messaging.outbox).where(
                    messaging.outbox.c.event_type == "mes.work_order.status_changed",
                    messaging.outbox.c.key.like(f"%:{p['erp_order_number']}"),
                )
            )
        ).scalar_one()
    assert events == 2  # IMPORTED->RELEASED, RELEASED->CLOSED


async def test_unknown_work_center_goes_to_dead_letter(db: Database, consumer: InboxConsumer) -> None:
    p = work_order_payload()
    p["operations"][0]["work_center"] = "WC-NOPE"
    await consumer.process(TOPIC, envelope(p))
    async with db.read() as s:
        err = (
            await s.execute(select(messaging.dead_letter.c.error).order_by(messaging.dead_letter.c.id.desc()).limit(1))
        ).scalar_one()
        assert "WC-NOPE" in err
        assert (await s.scalars(select(WorkOrder).where(WorkOrder.erp_order_number == p["erp_order_number"]))).first() is None
