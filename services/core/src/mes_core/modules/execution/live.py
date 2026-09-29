"""Live machine state for dashboards, in Redis.

The database is the source of truth (operation_run). After each committed change the API pushes the machine's
new state into a Redis hash (snapshot) and publishes it on a channel. Every API replica relays the channel to
its connected browsers over SSE, so this scales horizontally. The worker rebuilds the hash from the database
at start-up. If Redis is down, snapshots fall back to the database and pushes are skipped (logged).
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import redis.asyncio as redis

log = logging.getLogger(__name__)


def _key(plant_id: UUID | str) -> str:
    return f"mes:live:equipment:{plant_id}"


def _channel(plant_id: UUID | str) -> str:
    return f"mes:live:{plant_id}"


class LiveBus:
    def __init__(self, url: str | None) -> None:
        self._url = url
        self._r = redis.from_url(url, decode_responses=True, socket_connect_timeout=1, socket_timeout=2) if url else None

    @property
    def enabled(self) -> bool:
        return self._r is not None

    async def push(self, plant_id: UUID | str, state: dict[str, Any]) -> None:
        if not self._r:
            return
        data = json.dumps(state, default=str)
        try:
            async with self._r.pipeline(transaction=False) as p:
                p.hset(_key(plant_id), state["equipment_id"], data)
                p.publish(_channel(plant_id), data)
                await p.execute()
        except Exception as e:  # live view is best effort; the DB already has the truth
            log.warning("live push failed: %s", e)

    async def snapshot(self, plant_id: UUID | str) -> list[dict[str, Any]] | None:
        if not self._r:
            return None
        try:
            raw = await self._r.hgetall(_key(plant_id))
        except Exception as e:
            log.warning("live snapshot unavailable: %s", e)
            return None
        return [json.loads(v) for v in raw.values()] if raw else None

    async def rebuild(self, plant_id: UUID | str, states: list[dict[str, Any]]) -> None:
        if not self._r:
            return
        async with self._r.pipeline(transaction=True) as p:
            p.delete(_key(plant_id))
            if states:
                p.hset(_key(plant_id), mapping={st["equipment_id"]: json.dumps(st, default=str) for st in states})
            await p.execute()

    async def stream(self, plant_id: UUID | str, heartbeat_s: float = 15) -> AsyncIterator[str | None]:
        """Yields raw JSON machine states as they are published, and None every `heartbeat_s` of silence.

        Uses its own connection (no socket timeout): a subscription is idle most of the time.
        """
        if not self._url:
            return
        conn = redis.from_url(self._url, decode_responses=True)
        pubsub = conn.pubsub()
        await pubsub.subscribe(_channel(plant_id))
        try:
            while True:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=heartbeat_s)
                yield msg["data"] if msg and msg.get("type") == "message" else None
        finally:
            await pubsub.unsubscribe()
            await pubsub.aclose()
            await conn.aclose()

    async def aclose(self) -> None:
        if self._r:
            await self._r.aclose()
