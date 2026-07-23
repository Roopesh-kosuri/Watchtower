from __future__ import annotations

import asyncio


class EventBus:
    """Simple fan-out pub/sub. Publishers never block on a slow subscriber --
    a full queue just drops the event for that one subscriber rather than
    backing up the writer that's trying to publish it."""

    def __init__(self, max_queue_size: int = 1000):
        self._subscribers: set[asyncio.Queue] = set()
        self._max_queue_size = max_queue_size

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue_size)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
