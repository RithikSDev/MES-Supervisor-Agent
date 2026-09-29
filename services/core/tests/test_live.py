import asyncio
import json

import pytest
from testcontainers.redis import RedisContainer

from mes_core.modules.execution.live import LiveBus

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def redis_url():  # type: ignore[no-untyped-def]
    with RedisContainer("redis:7.4-alpine") as r:
        yield f"redis://{r.get_container_host_ip()}:{r.get_exposed_port(6379)}/0"


async def test_push_snapshot_and_stream(redis_url: str) -> None:
    bus = LiveBus(redis_url)
    plant = "p-1"
    received: list[dict] = []

    async def listen() -> None:
        async for data in bus.stream(plant, heartbeat_s=0.2):
            if data:
                received.append(json.loads(data))
                return

    task = asyncio.create_task(listen())
    await asyncio.sleep(0.5)  # let the subscription start
    await bus.push(plant, {"equipment_id": "m1", "state": "RUNNING"})
    await asyncio.wait_for(task, 5)
    assert received == [{"equipment_id": "m1", "state": "RUNNING"}]

    await bus.push(plant, {"equipment_id": "m1", "state": "PAUSED"})
    await bus.push(plant, {"equipment_id": "m2", "state": "IDLE"})
    snap = {m["equipment_id"]: m["state"] for m in await bus.snapshot(plant) or []}
    assert snap == {"m1": "PAUSED", "m2": "IDLE"}

    await bus.rebuild(plant, [{"equipment_id": "m3", "state": "SETUP"}])
    assert [m["equipment_id"] for m in await bus.snapshot(plant) or []] == ["m3"]
    await bus.aclose()


async def test_unreachable_redis_is_not_fatal() -> None:
    bus = LiveBus("redis://127.0.0.1:1/0")
    await bus.push("p", {"equipment_id": "m1"})  # logs a warning, does not raise
    assert await bus.snapshot("p") is None
    await bus.aclose()
