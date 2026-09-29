"""Worker process: ticket event intake, dispatcher (MES -> ServiceNow/Jira), poller (back to MES), outbox relay."""

import asyncio
import logging

from itsm_adapter.container import build_container
from itsm_adapter.db import messaging
from itsm_adapter.settings import get_settings
from itsm_adapter.sync import EVENT_TICKET_CREATED, EVENT_TICKET_UPDATED, Intake
from mes_common.events import Topics
from mes_common.kafka import ensure_topics, start_producer
from mes_common.logs import setup_logging
from mes_common.messaging import InboxConsumer, OutboxRelay
from mes_common.runner import periodic, run_tasks

log = logging.getLogger(__name__)


async def main() -> None:
    setup_logging("itsm-adapter-worker")
    settings = get_settings()
    c = build_container(settings)
    await c.registry.import_env_once()
    await c.registry.refresh(force=True)
    for port in (c.servicenow, c.jira):
        log.info("%s via %s: %s", port.system, port.mode,
                 port.endpoint if port.configured else "not connected (Integrations -> ServiceNow / Jira)")
    await ensure_topics(settings.kafka_bootstrap, Topics.ALL)
    producer = await start_producer(settings.kafka_bootstrap)
    relay = OutboxRelay(messaging, c.session_factory, producer)
    intake = Intake(settings)
    consumer = InboxConsumer(
        messaging, c.session_factory, bootstrap_servers=settings.kafka_bootstrap, group_id="itsm-adapter",
        topics=[Topics.MES_TICKETS], handlers={EVENT_TICKET_CREATED: intake, EVENT_TICKET_UPDATED: intake},
    )
    try:
        await run_tasks([
            relay.run,
            consumer.run,
            lambda stop: periodic(stop, settings.dispatch_interval_s, c.dispatcher.run_once, "ticket dispatcher"),
            lambda stop: periodic(stop, settings.poll_interval_s, c.poller.poll_all, "ServiceNow/Jira poller"),
            lambda stop: periodic(stop, settings.config_refresh_s, c.registry.refresh, "connection settings"),
        ])
    finally:
        await producer.stop()
        await c.aclose()


if __name__ == "__main__":
    asyncio.run(main())
