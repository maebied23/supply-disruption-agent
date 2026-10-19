"""Deterministic simulated clock and event queue.

Events at the same instant come out in the order they were scheduled, so a run is fully
reproducible. Time only moves when the runner advances it.
"""

from __future__ import annotations

import heapq
import itertools
from datetime import datetime
from typing import Any, Iterator


class SimClock:
    def __init__(self, start: datetime, queue: list[tuple[datetime, int, Any]] | None = None):
        if start.tzinfo is None:
            raise ValueError("clock times must be timezone-aware")
        self._now = start
        self._queue: list[tuple[datetime, int, Any]] = list(queue or [])
        heapq.heapify(self._queue)
        self._seq = itertools.count(max((q[1] for q in self._queue), default=-1) + 1)

    @property
    def now(self) -> datetime:
        return self._now

    def schedule(self, at: datetime, payload: Any) -> None:
        if at < self._now:
            raise ValueError(f"cannot schedule in the past: {at} < {self._now}")
        heapq.heappush(self._queue, (at, next(self._seq), payload))

    def run_until(self, end: datetime) -> Iterator[Any]:
        """Yield payloads due up to and including `end`, advancing `now` to each one."""
        while self._queue and self._queue[0][0] <= end:
            at, _, payload = heapq.heappop(self._queue)
            self._now = at
            yield payload
        self._now = max(self._now, end)

    def pending(self) -> int:
        return len(self._queue)

    def queued(self) -> list[tuple[datetime, int, Any]]:
        """Everything still scheduled, in firing order (for persistence)."""
        return sorted(self._queue, key=lambda q: (q[0], q[1]))

    def next_at(self) -> datetime | None:
        return self._queue[0][0] if self._queue else None
