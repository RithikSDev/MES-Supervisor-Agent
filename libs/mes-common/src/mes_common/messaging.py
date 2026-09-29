"""Transactional outbox + idempotent inbox.

Producing: business code calls `Messaging.publish(session, ...)` inside its own DB transaction. The event row
commits (or rolls back) atomically with the business change. `OutboxRelay` later ships unpublished rows to
Kafka in insertion order. Delivery is at-least-once.

Consuming: `InboxConsumer` records each event_id in `messaging.inbox_message` in the same transaction as the
handler's writes, so a redelivered event is a no-op. Offsets are committed only after the DB commit.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.structs import ConsumerRecord
from pydantic import ValidationError
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Identity,
    Index,
    MetaData,
    Table,
    Text,
    Uuid,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mes_common.events import EventEnvelope, PermanentError
from mes_common.logs import request_id_var

log = logging.getLogger(__name__)

SCHEMA = "messaging"

Handler = Callable[[AsyncSession, EventEnvelope], Awaitable[None]]
SessionHook = Callable[[AsyncSession], Awaitable[None]]


class Messaging:
    """Outbox/inbox tables bound to a service's MetaData, plus the write helpers."""

    def __init__(self, metadata: MetaData, source: str, schema: str = SCHEMA) -> None:
        """`schema` lets two services share one database with separate outbox/inbox tables."""
        self.source = source
        self.schema = schema
        self.outbox = Table(
            "outbox_event",
            metadata,
            Column("seq", BigInteger, Identity(), primary_key=True),
            Column("event_id", Uuid, nullable=False, unique=True),
            Column("topic", Text, nullable=False),
            Column("key", Text, nullable=False),
            Column("event_type", Text, nullable=False),
            Column("envelope", JSONB, nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
            Column("published_at", DateTime(timezone=True)),
            Index("ix_outbox_unpublished", "seq", postgresql_where=text("published_at IS NULL")),
            schema=schema,
        )
        self.inbox = Table(
            "inbox_message",
            metadata,
            Column("event_id", Uuid, primary_key=True),
            Column("topic", Text, nullable=False),
            Column("event_type", Text, nullable=False),
            Column("received_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
            schema=schema,
        )
        self.dead_letter = Table(
            "dead_letter",
            metadata,
            Column("id", BigInteger, Identity(), primary_key=True),
            Column("topic", Text, nullable=False),
            Column("partition", BigInteger),
            Column("offset", BigInteger),
            Column("key", Text),
            Column("event_type", Text),
            Column("raw", Text, nullable=False),
            Column("error", Text, nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
            schema=schema,
        )

    async def publish(
        self,
        session: AsyncSession,
        *,
        topic: str,
        key: str,
        event_type: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
        version: int = 1,
    ) -> EventEnvelope:
        env = EventEnvelope(
            event_type=event_type,
            event_version=version,
            source=self.source,
            key=key,
            correlation_id=correlation_id,
            payload=payload,
        )
        await session.execute(
            self.outbox.insert().values(
                event_id=env.event_id,
                topic=topic,
                key=key,
                event_type=event_type,
                envelope=env.model_dump(mode="json"),
            )
        )
        return env

    async def claim_inbox(self, session: AsyncSession, env: EventEnvelope, topic: str) -> bool:
        """True if this event has not been processed before. Must run in the handler's transaction."""
        stmt = (
            insert(self.inbox)
            .values(event_id=env.event_id, topic=topic, event_type=env.event_type)
            .on_conflict_do_nothing(index_elements=["event_id"])
            .returning(self.inbox.c.event_id)
        )
        return (await session.execute(stmt)).first() is not None


class OutboxRelay:
    def __init__(
        self,
        messaging: Messaging,
        session_factory: async_sessionmaker[AsyncSession],
        producer: AIOKafkaProducer,
        *,
        batch_size: int = 100,
        idle_sleep: float = 0.3,
    ) -> None:
        self._m = messaging
        self._sf = session_factory
        self._producer = producer
        self._batch = batch_size
        self._idle = idle_sleep

    async def run_once(self) -> int:
        ob = self._m.outbox
        async with self._sf() as s, s.begin():
            rows = (
                await s.execute(
                    select(ob.c.seq, ob.c.topic, ob.c.key, ob.c.envelope)
                    .where(ob.c.published_at.is_(None))
                    .order_by(ob.c.seq)
                    .limit(self._batch)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for r in rows:
                await self._producer.send_and_wait(
                    r.topic, value=json.dumps(r.envelope).encode(), key=r.key.encode()
                )
            if rows:
                await s.execute(
                    update(ob).where(ob.c.seq.in_([r.seq for r in rows])).values(published_at=func.now())
                )
        return len(rows)

    async def run(self, stop: asyncio.Event) -> None:
        log.info("outbox relay started")
        while not stop.is_set():
            try:
                sent = await self.run_once()
            except Exception:
                log.exception("outbox relay iteration failed")
                sent = 0
                await asyncio.sleep(2)
            if sent == 0:
                await _sleep_or_stop(stop, self._idle)


class InboxConsumer:
    def __init__(
        self,
        messaging: Messaging,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        bootstrap_servers: str,
        group_id: str,
        topics: Iterable[str],
        handlers: dict[str, Handler],
        on_session: SessionHook | None = None,
        max_attempts: int = 5,
    ) -> None:
        self._m = messaging
        self._sf = session_factory
        self._bootstrap = bootstrap_servers
        self._group = group_id
        self._topics = list(topics)
        self._handlers = handlers
        self._on_session = on_session
        self._max_attempts = max_attempts

    async def run(self, stop: asyncio.Event) -> None:
        consumer = AIOKafkaConsumer(
            *self._topics,
            bootstrap_servers=self._bootstrap,
            group_id=self._group,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        await consumer.start()
        log.info("consumer %s started on %s", self._group, self._topics)
        try:
            while not stop.is_set():
                batches = await consumer.getmany(timeout_ms=1000, max_records=50)
                for tp, records in batches.items():
                    for rec in records:
                        await self.process(rec.topic, rec.value or b"", record=rec)
                        await consumer.commit({tp: rec.offset + 1})
        finally:
            await consumer.stop()

    async def process(self, topic: str, raw: bytes, *, record: ConsumerRecord | None = None) -> None:
        """Handle one message. Public so tests can feed messages without Kafka."""
        try:
            env = EventEnvelope.model_validate_json(raw)
        except ValidationError as e:
            await self._dead_letter(topic, raw, None, f"invalid envelope: {e}", record)
            return
        handler = self._handlers.get(env.event_type)
        if handler is None:
            log.debug("no handler for %s on %s", env.event_type, topic)
            return
        token = request_id_var.set(f"event:{env.event_id}")  # log lines of this event can be found by its id
        try:
            await self._handle(topic, raw, env, handler, record)
        finally:
            request_id_var.reset(token)

    async def _handle(self, topic: str, raw: bytes, env: EventEnvelope, handler: Any, record: ConsumerRecord | None) -> None:
        last_error = ""
        for attempt in range(1, self._max_attempts + 1):
            try:
                async with self._sf() as s, s.begin():
                    if self._on_session:
                        await self._on_session(s)
                    if not await self._m.claim_inbox(s, env, topic):
                        log.info("duplicate event %s (%s) ignored", env.event_id, env.event_type)
                        return
                    await handler(s, env)
                return
            except PermanentError as e:
                await self._dead_letter(topic, raw, env.event_type, str(e), record)
                return
            except Exception as e:
                last_error = repr(e)
                log.exception("handler %s failed (attempt %d/%d)", env.event_type, attempt, self._max_attempts)
                await asyncio.sleep(min(2**attempt, 30))
        await self._dead_letter(topic, raw, env.event_type, f"retries exhausted: {last_error}", record)

    async def _dead_letter(
        self, topic: str, raw: bytes, event_type: str | None, error: str, record: ConsumerRecord | None
    ) -> None:
        log.error("dead-lettering message on %s: %s", topic, error)
        async with self._sf() as s, s.begin():
            await s.execute(
                self._m.dead_letter.insert().values(
                    topic=topic,
                    partition=record.partition if record else None,
                    offset=record.offset if record else None,
                    key=record.key.decode() if record and record.key else None,
                    event_type=event_type,
                    raw=raw.decode(errors="replace"),
                    error=error[:4000],
                )
            )


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)
