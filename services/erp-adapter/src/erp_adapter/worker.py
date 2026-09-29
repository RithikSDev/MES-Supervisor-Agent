"""Worker process: order poller, confirmation intake consumer, dispatcher, outbox relay."""

import asyncio

from erp_adapter.container import build_container
from erp_adapter.db import messaging
from erp_adapter.outbound import (
    EVENT_CONFIRMATION_CREATED,
    EVENT_GOODS_MOVEMENT_CREATED,
    INTAKE_EVENTS,
    KINDS,
    ConfirmationIntake,
    GoodsMovementIntake,
    Intake,
)
from erp_adapter.settings import get_settings
from mes_common.events import Topics
from mes_common.kafka import ensure_topics, start_producer
from mes_common.logs import setup_logging
from mes_common.messaging import InboxConsumer, OutboxRelay
from mes_common.runner import periodic, run_tasks


async def main() -> None:
    setup_logging("erp-adapter-worker")
    settings = get_settings()
    c = build_container(settings)
    await ensure_topics(settings.kafka_bootstrap, Topics.ALL)
    producer = await start_producer(settings.kafka_bootstrap)
    relay = OutboxRelay(messaging, c.session_factory, producer)
    consumer = InboxConsumer(
        messaging,
        c.session_factory,
        bootstrap_servers=settings.kafka_bootstrap,
        group_id="erp-adapter",
        topics=[Topics.MES_CONFIRMATIONS, Topics.MES_GOODS_MOVEMENTS, Topics.MES_MAINTENANCE],
        handlers={
            EVENT_CONFIRMATION_CREATED: ConfirmationIntake(settings),
            EVENT_GOODS_MOVEMENT_CREATED: GoodsMovementIntake(settings),
            **{ev: Intake(settings, KINDS[kind]) for ev, kind in INTAKE_EVENTS.items()},
        },
    )
    try:
        await run_tasks(
            [
                relay.run,
                consumer.run,
                lambda stop: periodic(stop, settings.poll_interval_s, c.poller.poll_all, "order poller"),
                lambda stop: periodic(stop, settings.poll_interval_s, c.pm_poller.poll_all, "PM order poller"),
                lambda stop: periodic(stop, settings.dispatch_interval_s, c.dispatcher.run_once, "dispatcher"),
            ]
        )
    finally:
        await producer.stop()
        await c.aclose()


if __name__ == "__main__":
    asyncio.run(main())
