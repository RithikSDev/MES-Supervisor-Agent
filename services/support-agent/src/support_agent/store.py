"""Persistence helpers: policies and settings, and the run recorder (every step, token usage and cost)."""

import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mes_common.events import Topics
from mes_common.ids import new_id
from support_agent.actions import default_policies
from support_agent.db import messaging, policy, proposal, run, setting, step
from support_agent.llm import Usage

DEFAULT_SETTINGS: dict[str, Any] = {
    "kill_switch": False,  # stops every agent call: tickets go to humans, the chat offers the plain form
    "max_cost_per_ticket_usd": 1.0,  # a ticket run that reaches this is stopped and handed to a human
    "max_cost_per_chat_usd": 0.5,  # per chat conversation
    "max_cost_per_l2_usd": 2.0,  # an L2 root-cause run reads more (logs, SAP history): its own limit
    "max_cost_per_engineer_chat_usd": 2.0,  # per support engineer conversation (admin UI)
    # set by the worker: {"ok": bool, "error": str | None, "at": iso}. A rejected API key or unknown model pauses the
    # agent (tickets are left alone, not handed over one by one) until the next successful call.
    "health": {"ok": True, "error": None, "at": None},
}


async def ensure_defaults(sf: async_sessionmaker[AsyncSession]) -> None:
    async with sf() as s, s.begin():
        for action, mode in default_policies().items():
            await s.execute(insert(policy).values(action=action, mode=mode, updated_by="default")
                            .on_conflict_do_nothing(index_elements=["action"]))
        for key, value in DEFAULT_SETTINGS.items():
            await s.execute(insert(setting).values(key=key, value=value, updated_by="default")
                            .on_conflict_do_nothing(index_elements=["key"]))


async def load_policies(s: AsyncSession) -> dict[str, str]:
    rows = dict((await s.execute(select(policy.c.action, policy.c.mode))).all())
    return {**default_policies(), **rows}


async def load_settings(s: AsyncSession) -> dict[str, Any]:
    rows = dict((await s.execute(select(setting.c.key, setting.c.value))).all())
    return {**DEFAULT_SETTINGS, **rows}


def jsonable(value: Any) -> Any:
    """Tool inputs/outputs as plain JSON (timestamps, ids, decimals become strings) for the JSONB step log."""
    return json.loads(json.dumps(value, default=str)) if value is not None else None


def _now() -> datetime:
    return datetime.now(UTC)


EVENT_PROPOSAL = "ai.proposal.created"
EVENT_HANDOVER = "ai.run.handed_over"
EVENT_HEALTH = "ai.agent.health_changed"


async def publish(s: AsyncSession, event_type: str, key: str, payload: dict[str, Any]) -> None:
    """Agent events for the notifier (outbox: sent by the worker's relay after the transaction commits)."""
    await messaging.publish(s, topic=Topics.AI_SUPPORT, key=key, event_type=event_type, payload=payload)


async def set_health(sf: async_sessionmaker[AsyncSession], ok: bool, error: str | None = None) -> None:
    async with sf() as s, s.begin():
        before = (await s.execute(select(setting.c.value).where(setting.c.key == "health").with_for_update())).scalar()
        await s.execute(update(setting).where(setting.c.key == "health").values(
            value={"ok": ok, "error": error, "at": _now().isoformat()}, updated_by="support-agent", updated_at=_now()))
        if before is None or bool(before.get("ok", True)) != ok:  # only a change of state is news
            await publish(s, EVENT_HEALTH, "agent", {"ok": ok, "error": error})


class Recorder:
    """One run's trail. Each write commits on its own, so the admin UI can follow a run live."""

    def __init__(self, sf: async_sessionmaker[AsyncSession], run_id: UUID, model: str) -> None:
        self._sf = sf
        self.run_id = run_id
        self.model = model
        self.seq = 0
        self.usage = Usage()
        self.cost = Decimal(0)
        self.outcomes: list[str] = []

    async def step(self, kind: str, name: str | None, input: Any = None, output: Any = None, *,
                   is_error: bool = False, started: float | None = None) -> None:
        self.seq += 1
        ms = int((time.monotonic() - started) * 1000) if started is not None else None
        async with self._sf() as s, s.begin():
            await s.execute(step.insert().values(run_id=self.run_id, seq=self.seq, kind=kind, name=name,
                                                 input=jsonable(input), output=jsonable(output), is_error=is_error, ms=ms))
            await s.execute(update(run).where(run.c.id == self.run_id).values(steps=self.seq))

    async def add_usage(self, u: Usage, model: str) -> None:
        self.usage.input += u.input
        self.usage.output += u.output
        self.usage.cache_read += u.cache_read
        self.usage.cache_write += u.cache_write
        self.cost += u.cost(model)
        async with self._sf() as s, s.begin():
            await s.execute(update(run).where(run.c.id == self.run_id).values(
                input_tokens=self.usage.input, output_tokens=self.usage.output, cache_read_tokens=self.usage.cache_read,
                cache_write_tokens=self.usage.cache_write, cost_usd=self.cost, model=model))

    async def propose(self, *, ticket: dict[str, Any], action: str, args: dict[str, Any], summary: str,
                      rationale: str | None) -> UUID:
        pid = new_id()
        async with self._sf() as s, s.begin():
            # one pending proposal per ticket and action: a re-run replaces nothing, it just doesn't duplicate
            existing = (await s.execute(select(proposal.c.id).where(
                proposal.c.ticket_id == UUID(str(ticket["id"])), proposal.c.action == action,
                proposal.c.status == "PENDING"))).scalar()
            if existing:
                return existing
            await s.execute(proposal.insert().values(
                id=pid, run_id=self.run_id, plant_id=UUID(str(ticket["plant_id"])), ticket_id=UUID(str(ticket["id"])),
                ticket_number=ticket["number"], action=action, args=args, summary=summary, rationale=rationale,
                status="PENDING"))
            await publish(s, EVENT_PROPOSAL, str(ticket["id"]), {
                "proposal_id": str(pid), "ticket_id": str(ticket["id"]), "number": ticket["number"],
                "plant_id": str(ticket["plant_id"]), "tier": ticket.get("tier"), "action": action, "summary": summary,
                "rationale": rationale})
        return pid

    async def set_rca(self, rca: dict[str, Any]) -> None:
        async with self._sf() as s, s.begin():
            await s.execute(update(run).where(run.c.id == self.run_id).values(rca=rca))

    async def finish(self, status: str, *, outcome: str | None = None, summary: str | None = None,
                     error: str | None = None) -> None:
        async with self._sf() as s, s.begin():
            await s.execute(update(run).where(run.c.id == self.run_id).values(
                status=status, outcome=outcome, summary=summary, error=error, finished_at=_now(), lease_until=None))
