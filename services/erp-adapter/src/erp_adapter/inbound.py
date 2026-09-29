"""Inbound: poll SAP for changed production orders and publish canonical events (deduplicated)."""

import hashlib
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_adapter.db import inbound_message, messaging, poll_state
from erp_adapter.ports import CanonicalWorkOrder, ErpPort
from erp_adapter.settings import Settings
from mes_common.events import Topics
from mes_common.ids import new_id

log = logging.getLogger(__name__)

EVENT_WORK_ORDER_UPSERTED = "erp.work_order.upserted"
POLL_LOCK_ID = 7310001  # pg advisory lock: only one poller at a time (worker or manual "poll now")


def order_key(wo: CanonicalWorkOrder) -> str:
    return f"{wo.erp_plant}:{wo.erp_order_number}"


class OrderPoller:
    def __init__(self, erp: ErpPort, session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self._erp = erp
        self._sf = session_factory
        self._s = settings

    async def poll_all(self) -> int:
        total = 0
        for plant in self._s.plants:
            total += await self.poll_plant(plant)
        return total

    async def poll_plant(self, plant: str) -> int:
        name = f"production_orders:{plant}"
        async with self._sf() as s, s.begin():
            got = (await s.execute(text("SELECT pg_try_advisory_xact_lock(:id)"), {"id": POLL_LOCK_ID})).scalar()
            if not got:
                log.info("poll already running elsewhere, skipping")
                return 0
            watermark = await self._watermark(s, name)
            since = watermark - timedelta(seconds=self._s.poll_overlap_s)
            published = 0
            max_seen = watermark
            skip = 0
            try:
                while True:
                    page = await self._erp.fetch_changed_orders(plant, since, skip=skip, top=self._s.poll_page_size)
                    for wo in page:
                        if await self.publish_if_new(s, wo):
                            published += 1
                        max_seen = max(max_seen, wo.erp_last_changed_at)
                    if len(page) < self._s.poll_page_size:
                        break
                    skip += len(page)
            except Exception as e:
                # Keep what we already published in this run (same tx) but do not move the watermark.
                await s.execute(
                    update(poll_state)
                    .where(poll_state.c.name == name)
                    .values(last_run_at=func.now(), last_error=str(e)[:2000])
                )
                log.warning("poll of plant %s failed: %s", plant, e)
                return published
            await s.execute(
                update(poll_state)
                .where(poll_state.c.name == name)
                .values(
                    watermark=max_seen,
                    last_run_at=func.now(),
                    last_success_at=func.now(),
                    last_error=None,
                    last_published=published,
                )
            )
        if published:
            log.info("published %d work order change(s) for plant %s", published, plant)
        return published

    async def publish_if_new(self, s: AsyncSession, wo: CanonicalWorkOrder, *, force: bool = False) -> bool:
        key = f"PRODUCTION_ORDER|{wo.erp_plant}|{wo.erp_order_number}|{wo.erp_last_changed_at.isoformat()}"
        if force:
            key += f"|resync:{new_id()}"
        payload = wo.model_dump(mode="json")
        event_id = new_id()
        inserted = (
            await s.execute(
                insert(inbound_message)
                .values(
                    id=new_id(),
                    message_key=hashlib.sha256(key.encode()).hexdigest(),
                    erp_object="PRODUCTION_ORDER",
                    erp_key=order_key(wo),
                    erp_changed_at=wo.erp_last_changed_at,
                    payload=payload,
                    event_id=event_id,
                )
                .on_conflict_do_nothing(index_elements=["message_key"])
                .returning(inbound_message.c.id)
            )
        ).first()
        if inserted is None:
            return False
        await messaging.publish(
            s,
            topic=Topics.ERP_WORK_ORDERS,
            key=order_key(wo),
            event_type=EVENT_WORK_ORDER_UPSERTED,
            payload=payload,
        )
        return True

    async def _watermark(self, s: AsyncSession, name: str) -> datetime:
        row = (await s.execute(select(poll_state.c.watermark).where(poll_state.c.name == name))).first()
        if row:
            return row.watermark
        initial = datetime.now(UTC) - timedelta(days=self._s.initial_lookback_days)
        await s.execute(insert(poll_state).values(name=name, watermark=initial).on_conflict_do_nothing())
        return initial


EVENT_MAINT_ORDER_UPDATED = "erp.maintenance_order.updated"
PM_POLL_LOCK_ID = 7310002


class MaintOrderPoller:
    """SAP-side changes of PM orders (e.g. a planner sets TECO in SAP) -> `erp.maintenance_order.updated`.
    Our own status changes come back too; core applies them idempotently."""

    def __init__(self, erp: ErpPort, session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self._erp = erp
        self._sf = session_factory
        self._s = settings

    async def poll_all(self) -> int:
        total = 0
        for plant in self._s.plants:
            total += await self.poll_plant(plant)
        return total

    async def poll_plant(self, plant: str) -> int:
        name = f"maintenance_orders:{plant}"
        async with self._sf() as s, s.begin():
            if not (await s.execute(text("SELECT pg_try_advisory_xact_lock(:id)"), {"id": PM_POLL_LOCK_ID})).scalar():
                return 0
            row = (await s.execute(select(poll_state.c.watermark).where(poll_state.c.name == name))).first()
            if row is None:
                await s.execute(insert(poll_state).values(name=name, watermark=datetime.now(UTC) - timedelta(days=1))
                                .on_conflict_do_nothing())
                watermark = datetime.now(UTC) - timedelta(days=1)
            else:
                watermark = row.watermark
            try:
                changes = await self._erp.fetch_changed_maint_orders(plant, watermark - timedelta(seconds=self._s.poll_overlap_s))
            except Exception as e:
                await s.execute(update(poll_state).where(poll_state.c.name == name)
                                .values(last_run_at=func.now(), last_error=str(e)[:2000]))
                log.warning("PM poll of plant %s failed: %s", plant, e)
                return 0
            published = 0
            max_seen = watermark
            for ch in changes:
                key = f"MAINT_ORDER|{plant}|{ch.number}|{ch.changed_at.isoformat()}"
                payload = ch.model_dump(mode="json")
                inserted = (await s.execute(
                    insert(inbound_message).values(
                        id=new_id(), message_key=hashlib.sha256(key.encode()).hexdigest(), erp_object="MAINT_ORDER",
                        erp_key=f"{plant}:{ch.number}", erp_changed_at=ch.changed_at, payload=payload, event_id=new_id(),
                    ).on_conflict_do_nothing(index_elements=["message_key"]).returning(inbound_message.c.id)
                )).first()
                if inserted is not None:
                    await messaging.publish(s, topic=Topics.ERP_MAINTENANCE_ORDERS, key=f"{plant}:{ch.number}",
                                            event_type=EVENT_MAINT_ORDER_UPDATED, payload=payload)
                    published += 1
                max_seen = max(max_seen, ch.changed_at)
            await s.execute(update(poll_state).where(poll_state.c.name == name).values(
                watermark=max_seen, last_run_at=func.now(), last_success_at=func.now(), last_error=None,
                last_published=published))
        return published
