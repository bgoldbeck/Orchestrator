"""Run blocking / foreign-loop producers in a thread and consume them as an
async iterator on the caller's loop. Keeps engines independent of qasync."""
from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from typing import Any

_END = object()


class ThreadBridge:
    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue[Any] = asyncio.Queue()

    def put(self, item: Any) -> None:
        """Thread-safe: hand an item to the consumer loop."""
        self.loop.call_soon_threadsafe(self.queue.put_nowait, item)

    def start(self, target: Callable[["ThreadBridge"], None]) -> threading.Thread:
        def runner() -> None:
            try:
                target(self)
            except BaseException as exc:  # surfaced to consumer
                self.put(exc)
            finally:
                self.put(_END)

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        return t

    async def items(self) -> AsyncIterator[Any]:
        while True:
            item = await self.queue.get()
            if item is _END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
