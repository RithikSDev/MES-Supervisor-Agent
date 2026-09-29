import asyncio
import logging
from collections.abc import Iterable

from aiokafka import AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import KafkaConnectionError, TopicAlreadyExistsError

log = logging.getLogger(__name__)


async def ensure_topics(bootstrap_servers: str, topics: Iterable[str], partitions: int = 3) -> None:
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap_servers)
    await _retry_start(admin.start, "kafka admin")
    try:
        existing = set(await admin.list_topics())
        missing = [NewTopic(t, num_partitions=partitions, replication_factor=1) for t in topics if t not in existing]
        if missing:
            try:
                await admin.create_topics(missing)
                log.info("created topics %s", [t.name for t in missing])
            except TopicAlreadyExistsError:
                pass
    finally:
        await admin.close()


async def start_producer(bootstrap_servers: str) -> AIOKafkaProducer:
    producer = AIOKafkaProducer(
        bootstrap_servers=bootstrap_servers, enable_idempotence=True, acks="all", linger_ms=5
    )
    await _retry_start(producer.start, "kafka producer")
    return producer


async def _retry_start(start, what: str, attempts: int = 30) -> None:  # type: ignore[no-untyped-def]
    for i in range(1, attempts + 1):
        try:
            await start()
            return
        except KafkaConnectionError:
            if i == attempts:
                raise
            log.warning("%s: broker not reachable yet (attempt %d), retrying", what, i)
            await asyncio.sleep(2)
