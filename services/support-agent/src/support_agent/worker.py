"""Worker: new MES tickets (mes.tickets.v1, ticket created) are queued for the agent; queued runs are worked."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from mes_common.events import EventEnvelope, PermanentError, Topics
from mes_common.kafka import ensure_topics, start_producer
from mes_common.logs import setup_logging
from mes_common.messaging import InboxConsumer, OutboxRelay
from mes_common.runner import periodic, run_tasks
from support_agent.agent import Agent
from support_agent.container import build_container
from support_agent.db import messaging
from support_agent.settings import get_settings
from support_agent.store import ensure_defaults

log = logging.getLogger(__name__)
EVENT_TICKET_CREATED = "mes.ticket.created"
EVENT_TICKET_UPDATED = "mes.ticket.updated"


class Intake:
    """Kafka handler: a new ticket -> one queued run (keyed by the event id, so redelivery is a no-op). Events older
    than `max_event_age_min` are ignored: replaying the topic must never make the agent work old tickets."""

    def __init__(self, max_age_min: int = 30) -> None:
        self._max_age = timedelta(minutes=max_age_min)

    async def __call__(self, s: AsyncSession, env: EventEnvelope) -> None:
        if datetime.now(UTC) - env.occurred_at > self._max_age:
            log.info("ignoring old ticket event %s (%s)", env.payload.get("number"), env.occurred_at)
            return
        p = env.payload
        if p.get("demo"):  # demo tickets are for showing; hand one to the AI with "Run the AI again"
            log.info("ignoring demo ticket %s", p.get("number"))
            return
        try:
            ticket_id, number = UUID(p["ticket_id"]), p["number"]
            plant_id = UUID(p["plant_id"]) if p.get("plant_id") else None
        except Exception as e:
            raise PermanentError(f"invalid ticket event: {e}") from e
        tier = p.get("tier") or "L1"
        if env.event_type == EVENT_TICKET_UPDATED:
            # only an escalation to L2 starts work (L2 root cause); other updates and L3 are for people
            if "tier" not in (p.get("changes") or []) or tier != "L2" or p.get("status") not in (
                    "NEW", "ACKNOWLEDGED", "IN_PROGRESS", "ON_HOLD"):
                return
            trigger = "ESCALATED"
        else:
            trigger = "CREATED"
        await Agent.enqueue(s, ticket_id=ticket_id, number=number, plant_id=plant_id, trigger=trigger,
                            trigger_key=str(env.event_id), tier=tier)


async def main() -> None:
    setup_logging("support-agent-worker")
    settings = get_settings()
    c = build_container(settings)
    await ensure_defaults(c.session_factory)
    await c.agent.mcp.ensure_defaults()
    await c.agent.docs.sync_builtin()
    log.info("AI support agent: model %s, %s", settings.agent_model,
             "API key configured" if c.agent.model else "NO ANTHROPIC_API_KEY: tickets are left to humans")
    await ensure_topics(settings.kafka_bootstrap, Topics.ALL)
    producer = await start_producer(settings.kafka_bootstrap)
    relay = OutboxRelay(messaging, c.session_factory, producer)
    intake = Intake(settings.max_event_age_min)
    consumer = InboxConsumer(
        messaging, c.session_factory, bootstrap_servers=settings.kafka_bootstrap, group_id="support-agent",
        topics=[Topics.MES_TICKETS], handlers={EVENT_TICKET_CREATED: intake, EVENT_TICKET_UPDATED: intake},
    )
    try:
        await run_tasks([
            relay.run,
            consumer.run,
            lambda stop: periodic(stop, settings.worker_interval_s, c.agent.process_once, "ticket agent"),
        ])
    finally:
        await producer.stop()
        await c.aclose()


if __name__ == "__main__":
    asyncio.run(main())
