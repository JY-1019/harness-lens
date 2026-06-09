"""Event bus — node-level patches for live consumers (WebSocket GUI, ``tail``).

The design forbids re-sending the whole tree: subscribers get small ``upsert`` patches
({entity, data}) and approval/mode events, each carrying a monotonic ``rev`` so a client
that loaded a REST snapshot can apply only patches newer than the snapshot. The WebSocket
surface itself is Phase 2; this bus is the in-process pub/sub it (and ``harness-lens tail``)
will consume.
"""

from __future__ import annotations

import asyncio
from typing import Optional


class EventBus:
    def __init__(self, max_queue: int = 1000) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._rev = 0
        self._max_queue = max_queue

    @property
    def rev(self) -> int:
        return self._rev

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, op: str, entity: Optional[str] = None, data: Optional[dict] = None) -> dict:
        """Fan a patch out to every subscriber; returns the message (with its ``rev``)."""
        self._rev += 1
        msg: dict = {"op": op, "rev": self._rev}
        if entity is not None:
            msg["entity"] = entity
        if data is not None:
            msg["data"] = data
        for q in list(self._subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # A slow consumer must not stall the daemon. Drop the oldest, enqueue newest;
                # the consumer can re-snapshot via REST if it detects a rev gap.
                try:
                    q.get_nowait()
                    q.put_nowait(msg)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
        return msg
