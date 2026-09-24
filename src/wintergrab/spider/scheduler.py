"""Request queue with priorities, per-domain fairness and duplicate filtering."""

from __future__ import annotations

import heapq
import itertools
from collections.abc import Iterable

from ..request import Request
from ..utils import host_of
from .throttle import AutoThrottle

_Entry = tuple[int, int, Request]


class Scheduler:
    """Holds pending requests, one heap per domain.

    ``pop_ready`` picks the highest-priority request (FIFO among equals)
    from any domain whose throttle slot is free, so one slow or backed-off
    site never blocks the others.
    """

    def __init__(self, *, dedupe: bool = True) -> None:
        self.dedupe = dedupe
        self._queues: dict[str, list[_Entry]] = {}
        self._seen: set[bytes] = set()
        self._counter = itertools.count()
        self._size = 0
        self.duplicates = 0

    def __len__(self) -> int:
        return self._size

    @property
    def seen(self) -> set[bytes]:
        return self._seen

    def restore_seen(self, fingerprints: Iterable[bytes]) -> None:
        self._seen.update(fingerprints)

    def push(self, request: Request, *, force: bool = False) -> bool:
        """Queue a request. Returns ``False`` if it was a duplicate."""
        if self.dedupe and not force and not request.dont_filter:
            fp = request.fingerprint()
            if fp in self._seen:
                self.duplicates += 1
                return False
            self._seen.add(fp)
        domain = host_of(request.url)
        heapq.heappush(self._queues.setdefault(domain, []), (-request.priority, next(self._counter), request))
        self._size += 1
        return True

    def pop_ready(self, throttle: AutoThrottle, now: float) -> tuple[Request | None, float | None]:
        """Next request that may start now, or ``(None, seconds_until_one_might)``."""
        best: tuple[int, int] | None = None
        best_domain = ""
        wait: float | None = None
        for domain, queue in self._queues.items():
            if not queue:
                continue
            slot = throttle.slot(domain)
            if slot.active >= slot.concurrency:
                continue  # woken up again when a request finishes
            ready = slot.ready_at()
            if now < ready:
                wait = ready - now if wait is None else min(wait, ready - now)
                continue
            key = (queue[0][0], queue[0][1])
            if best is None or key < best:
                best, best_domain = key, domain
        if best is None:
            return None, wait
        queue = self._queues[best_domain]
        _, _, request = heapq.heappop(queue)
        if not queue:
            del self._queues[best_domain]
        self._size -= 1
        return request, None

    def pending(self) -> list[Request]:
        """Every queued request (highest priority first) without removing them."""
        entries = [e for q in self._queues.values() for e in q]
        entries.sort(key=lambda e: (e[0], e[1]))
        return [e[2] for e in entries]

    def clear(self) -> None:
        self._queues.clear()
        self._size = 0
