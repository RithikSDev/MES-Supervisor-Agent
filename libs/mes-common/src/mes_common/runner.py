"""Tiny supervisor for background worker processes (outbox relay, consumers, pollers)."""

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

Task = Callable[[asyncio.Event], Awaitable[None]]


async def run_tasks(tasks: list[Task]) -> None:
    """Run all tasks until SIGTERM/SIGINT. If any task crashes the process exits (Docker restarts it)."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # Windows dev runs have no signal handlers
            loop.add_signal_handler(sig, stop.set)
    async with asyncio.TaskGroup() as tg:
        for t in tasks:
            tg.create_task(t(stop))


async def periodic(stop: asyncio.Event, interval: float, fn: Callable[[], Awaitable[object]], name: str) -> None:
    log.info("%s started (every %ss)", name, interval)
    while not stop.is_set():
        try:
            await fn()
        except Exception:
            log.exception("%s iteration failed", name)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), interval)
