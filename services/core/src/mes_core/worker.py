"""Worker process: outbox relay, ERP event consumer, audit sealer."""

import asyncio
import logging

from mes_common.events import Topics
from mes_common.kafka import ensure_topics, start_producer
from mes_common.logs import setup_logging
from mes_common.messaging import InboxConsumer, OutboxRelay
from mes_common.runner import periodic, run_tasks
from mes_core import models  # noqa: F401
from mes_core.modules.compliance.public import ensure_audit_partitions, seal_audit_log
from mes_core.modules.erp_sync.handlers import HANDLERS, TOPICS, on_session
from mes_core.modules.execution.live import LiveBus
from mes_core.modules.execution.public import equipment_states
from mes_core.modules.maintenance.public import generate_preventive_orders
from mes_core.modules.org.public import list_plants
from mes_core.modules.ticketing.public import check_sla
from mes_core.settings import get_settings
from mes_core.shared.db.context import Actor
from mes_core.shared.db.session import Database
from mes_core.shared.messaging import messaging

log = logging.getLogger(__name__)


async def main() -> None:
    setup_logging("mes-core-worker")
    settings = get_settings()
    db = Database(settings.database_url, pool_size=5)
    await ensure_topics(settings.kafka_bootstrap, Topics.ALL)
    producer = await start_producer(settings.kafka_bootstrap)

    relay = OutboxRelay(messaging, db.session_factory, producer)
    consumer = InboxConsumer(
        messaging,
        db.session_factory,
        bootstrap_servers=settings.kafka_bootstrap,
        group_id="mes-core",
        topics=TOPICS,
        handlers=HANDLERS,
        on_session=on_session,
    )

    async def seal() -> None:
        async with db.session_factory() as s, s.begin():
            n = await seal_audit_log(s)
        if n:
            log.info("sealed %d audit row(s)", n)

    async def rebuild_live() -> None:
        """Live board snapshot in Redis is a cache of the database; refresh it (start-up and periodically)."""
        live = LiveBus(settings.redis_url or None)
        try:
            async with db.read() as s:
                for plant in await list_plants(s):
                    await live.rebuild(plant.id, await equipment_states(s, plant.id))
        finally:
            await live.aclose()

    async def preventive() -> None:
        async with db.unit_of_work(Actor.service("maintenance-planner")) as s:
            for plant in await list_plants(s):
                created = await generate_preventive_orders(s, plant.id)
                if created:
                    log.info("opened %d preventive maintenance order(s) in %s", len(created), plant.code)

    async def sla() -> None:
        async with db.unit_of_work(Actor.service("sla-monitor")) as s:
            n = await check_sla(s)
        if n:
            log.info("%d ticket SLA target(s) breached", n)

    async def partitions() -> None:
        async with db.session_factory() as s, s.begin():
            await ensure_audit_partitions(s)

    try:
        await run_tasks(
            [
                relay.run,
                consumer.run,
                lambda stop: periodic(stop, settings.audit_seal_interval_s, seal, "audit sealer"),
                lambda stop: periodic(stop, 3600, partitions, "audit partition maintenance"),
                lambda stop: periodic(stop, 300, rebuild_live, "live snapshot rebuild"),
                lambda stop: periodic(stop, settings.maintenance_interval_s, preventive, "preventive maintenance"),
                lambda stop: periodic(stop, settings.sla_check_interval_s, sla, "ticket SLA monitor"),
            ]
        )
    finally:
        await producer.stop()
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
