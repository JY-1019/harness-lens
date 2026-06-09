"""Single ledger-writer coroutine — serialise every mutation through one queue.

The design requires ledger writes to be serialised by a single writer (SQLite WAL). All
mutations are submitted as zero-arg callables onto an :class:`asyncio.Queue` and executed
one at a time by :meth:`_run`; the submitter awaits the result via a future, so a control
handler can read back the row it just wrote. (The underlying :class:`DaemonLedger` is also
lock-guarded, so reads from request handlers stay safe alongside the writer.)
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

_SENTINEL = object()


class LedgerWriter:
    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="harness-lens-ledger-writer")

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is _SENTINEL:
                    return
                fn, fut = item
                try:
                    result = fn()
                except Exception as exc:  # noqa: BLE001 — propagate to the awaiting submitter
                    if fut is not None and not fut.done():
                        fut.set_exception(exc)
                else:
                    if fut is not None and not fut.done():
                        fut.set_result(result)
            finally:
                self._queue.task_done()

    async def submit(self, fn: Callable[[], Any]) -> Any:
        """Run ``fn`` on the writer coroutine and return its result."""
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        await self._queue.put((fn, fut))
        return await fut

    def enqueue(self, fn: Callable[[], Any]) -> None:
        """Fire-and-forget variant for writes whose result the caller does not need."""
        self._queue.put_nowait((fn, None))

    async def stop(self) -> None:
        if self._task is None:
            return
        await self._queue.put(_SENTINEL)
        await self._task
        self._task = None
