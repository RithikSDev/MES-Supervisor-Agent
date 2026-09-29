"""Worker: ticket and AI-agent events -> alerts; sends them; escalates unacknowledged ones."""

import asyncio
import logging

from mes_common.db import make_engine, make_session_factory
from mes_common.events import Topics
from mes_common.logs import setup_logging
from mes_common.messaging import InboxConsumer
from mes_common.runner import periodic, run_tasks
from notifier.alerts import Dispatcher, Intake, ensure_defaults
from notifier.channels import Email, Teams
from notifier.db import messaging
from notifier.settings import get_settings

log = logging.getLogger(__name__)
EVENTS = ("mes.ticket.created", "mes.ticket.updated", "mes.ticket.sla_at_risk", "mes.ticket.sla_breached",
          "ai.proposal.created", "ai.proposal.decided", "ai.run.handed_over", "ai.agent.health_changed")


async def main() -> None:
    setup_logging("notifier-worker")
    s = get_settings()
    engine = make_engine(s.database_url, pool_size=5)
    sf = make_session_factory(engine)
    await ensure_defaults(sf)
    email, teams = Email(s), Teams(s)
    log.info("notifier: e-mail %s, Teams %s", "on" if email.configured else "off (no SMTP_HOST)",
             "on" if teams.configured else "off (no TEAMS_WEBHOOK_URL)")
    dispatcher = Dispatcher(sf, {"email": email, "teams": teams})
    intake = Intake(s)
    consumer = InboxConsumer(messaging, sf, bootstrap_servers=s.kafka_bootstrap, group_id="notifier",
                             topics=[Topics.MES_TICKETS, Topics.AI_SUPPORT], handlers={e: intake for e in EVENTS})
    try:
        await run_tasks([
            consumer.run,
            lambda stop: periodic(stop, s.dispatch_interval_s, dispatcher.run_once, "alert dispatcher"),
            lambda stop: periodic(stop, s.escalation_interval_s, dispatcher.escalate, "alert escalation"),
        ])
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
