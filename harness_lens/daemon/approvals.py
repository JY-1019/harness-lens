"""Approval queue — coordinate a blocked hook request with a human resolution.

When the policy engine returns ``escalate``, the hook's HTTP request blocks. The daemon
registers an :class:`asyncio.Future` here, publishes ``approval.requested`` on the event
bus, and awaits the future (bounded by ``approval_timeout_sec``). A GUI/terminal answer
calls :meth:`resolve`, which completes the future and unblocks the hook.

This class owns only the in-memory wait/notify coordination; the daemon owns the durable
``approvals`` row and the bus publish, so this stays a small, synchronously-testable unit.
On daemon restart the in-memory futures are gone (the blocked hook processes died with the
old daemon), and the ledger separately denies any row left pending.
"""

from __future__ import annotations

import asyncio
from typing import Optional

# Resolutions a waiter can receive.
APPROVED = "approved"
DENIED = "denied"
TIMEOUT = "timeout"


class ApprovalQueue:
    def __init__(self) -> None:
        self._waiters: dict[str, asyncio.Future] = {}

    def register(self, approval_id: str) -> "asyncio.Future[str]":
        """Create (and track) the future a hook request will await for ``approval_id``."""
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._waiters[approval_id] = fut
        return fut

    async def wait(self, approval_id: str, timeout: float) -> str:
        """Block until resolved or ``timeout`` elapses; returns the resolution or ``TIMEOUT``."""
        fut = self._waiters.get(approval_id)
        if fut is None:
            return TIMEOUT
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except asyncio.TimeoutError:
            return TIMEOUT
        finally:
            self._waiters.pop(approval_id, None)

    def resolve(self, approval_id: str, resolution: str) -> bool:
        """Complete a waiting request. Returns False if nothing was waiting (already gone)."""
        fut = self._waiters.get(approval_id)
        if fut is not None and not fut.done():
            fut.set_result(resolution)
            return True
        return False

    def pending_ids(self) -> list[str]:
        return list(self._waiters)

    def fail_all(self, resolution: str = DENIED) -> int:
        """Resolve every outstanding waiter (e.g. on shutdown) so no request hangs forever."""
        count = 0
        for approval_id in list(self._waiters):
            if self.resolve(approval_id, resolution):
                count += 1
        return count
