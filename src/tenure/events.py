"""In-process event bus and JSONL recorder for the dashboard (master §20.2, §20.3, Appendix B).

Publishers call `publish(name, data)` from the run's event loop. Each subscriber (an SSE connection in
T07) gets its own asyncio queue; sinks such as the Recorder are called synchronously, in publish order.
Coalescing follows §20.2: `progress` at most 10 times a second, `cell` at most once per cell per tick.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

EVENT_NAMES: tuple[str, ...] = ("run", "progress", "cell", "level", "cusum", "gpu", "business", "ledger", "alarm",
                                "override")

Sink = Callable[[str, dict[str, Any]], None]


class EventBus:
    def __init__(self, clock: Callable[[], float] = time.monotonic, progress_hz: float = 10.0):
        self._clock = clock
        self._progress_gap = 1.0 / progress_hz
        self._last_progress = float("-inf")
        self._tick_cells: set[str] = set()
        self._queues: list[asyncio.Queue[tuple[str, dict[str, Any]]]] = []
        self._sinks: list[Sink] = []

    def subscribe(self, maxsize: int = 1_024) -> asyncio.Queue[tuple[str, dict[str, Any]]]:
        q: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[tuple[str, dict[str, Any]]]) -> None:
        if q in self._queues:
            self._queues.remove(q)

    def add_sink(self, sink: Sink) -> None:
        self._sinks.append(sink)

    def remove_sink(self, sink: Sink) -> None:
        if sink in self._sinks:
            self._sinks.remove(sink)

    def progress_due(self) -> bool:
        """Whether a progress event would be delivered now; lets publishers skip building one."""
        return self._clock() - self._last_progress >= self._progress_gap

    def tick(self) -> None:
        """Start a new ladder tick: every cell may publish once more."""
        self._tick_cells.clear()

    def publish(self, name: str, data: dict[str, Any], *, force: bool = False) -> bool:
        """Deliver an event unless coalescing drops it; returns whether it was delivered."""
        if name not in EVENT_NAMES:
            raise ValueError(f"unknown event {name!r}; expected one of {EVENT_NAMES}")
        if name == "progress":
            now = self._clock()
            if not force and now - self._last_progress < self._progress_gap:
                return False
            self._last_progress = now
        elif name == "cell":
            if data["cell"] in self._tick_cells:
                return False
            self._tick_cells.add(data["cell"])
        for sink in self._sinks:
            sink(name, data)
        for q in self._queues:
            if q.full():  # a slow subscriber loses its oldest event, never blocks the run
                q.get_nowait()
            q.put_nowait((name, data))
        return True


def _last_t_ms(path: Path) -> int:
    last = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    last = max(last, int(json.loads(line)["t_ms"]))
                except (ValueError, KeyError, TypeError):
                    continue
    return last


class Recorder:
    """A bus sink writing `{t_ms, event, data}` lines, t_ms relative to the run start (§20.3).

    Appending to an existing file (a resumed run) continues from its last t_ms, so t_ms stays monotone.
    """

    def __init__(self, path: str | Path, clock: Callable[[], float] = time.monotonic):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._offset = _last_t_ms(self.path) if self.path.exists() else 0
        self._last = self._offset
        self._t0 = clock()
        self._f = open(self.path, "a", encoding="utf-8", newline="\n")

    def __call__(self, name: str, data: dict[str, Any]) -> None:
        t = max(self._last, self._offset + int((self._clock() - self._t0) * 1000))
        self._last = t
        self._f.write(json.dumps({"t_ms": t, "event": name, "data": data}, ensure_ascii=False) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()
