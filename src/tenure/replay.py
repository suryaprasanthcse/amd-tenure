"""Async replay with a bounded reorder buffer: concurrent inference, chronological commits (master §16)."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Generic, TypeVar

C = TypeVar("C")
O = TypeVar("O")


class ReplayEngine(Generic[C, O]):
    """Runs decide() concurrently and calls commit() strictly in input order.

    decide() must not raise: transport failures are returned as invalid outcomes. At most
    max_ahead cases are in flight or waiting for commit, so memory stays bounded and the
    lowest uncommitted case always holds a slot (no deadlock).
    """

    def __init__(self, decide: Callable[[C], Awaitable[O]], commit: Callable[[int, C, O], None],
                 concurrency: int = 128, max_ahead: int = 4_096):
        if concurrency < 1 or max_ahead < concurrency:
            raise ValueError("need 1 <= concurrency <= max_ahead")
        self.decide = decide
        self.commit = commit
        self.concurrency = concurrency
        self.max_ahead = max_ahead
        self.committed = 0

    async def run(self, cases: Sequence[C]) -> int:
        slots = asyncio.Semaphore(self.max_ahead)
        done: dict[int, O] = {}
        ready = asyncio.Event()
        next_index = 0
        lock = asyncio.Lock()

        async def worker() -> None:
            nonlocal next_index
            while True:
                await slots.acquire()  # hold a slot before taking work: bounds the reorder buffer
                async with lock:
                    if next_index >= len(cases):
                        slots.release()
                        return
                    i = next_index
                    next_index += 1
                done[i] = await self.decide(cases[i])
                ready.set()

        async def committer() -> None:
            while self.committed < len(cases):
                await ready.wait()
                ready.clear()
                while self.committed in done:
                    i = self.committed
                    self.commit(i, cases[i], done.pop(i))
                    self.committed += 1
                    slots.release()

        await asyncio.gather(committer(), *(worker() for _ in range(self.concurrency)))
        return self.committed
