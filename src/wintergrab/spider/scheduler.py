"""Request queue with priorities, per-domain fairness and duplicate filtering."""

from __future__ import annotations

import heapq
import itertools
from collections.abc import Iterable

from ..request import Request
from .throttle import AutoThrottle

_Entry = tuple[int, int, Request]


class Scheduler:
    """Holds pending requests, one heap per domain.

    ``pop_ready`` picks the highest-priority request (FIFO among equals, or
    LIFO with ``lifo=True`` for depth-first crawls) from any domain whose
    throttle slot is free, so one slow or backed-off site never blocks the others.
    """

    def __init__(self, *, dedupe: bool = True, lifo: bool = False) -> None:
        self.dedupe = dedupe
        self.lifo = lifo
        self._step = -1 if lifo else 1
        self._queues: dict[str, list[_Entry]] = {}
        self._seen: set[bytes] = set()
        self._counter = itertools.count(1)
        self._size = 0
        self.duplicates = 0
        self.retry_count = 0  # queued requests that are retries

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
        domain = request.host
        heapq.heappush(
            self._queues.setdefault(domain, []), (-request.priority, self._step * next(self._counter), request)
        )
        self._size += 1
        if request.retries:
            self.retry_count += 1
        return True

    def pop_ready(
        self, throttle: AutoThrottle, now: float, *, retries_only: bool = False, min_priority: int | None = None
    ) -> tuple[Request | None, float | None]:
        """Next request that may start now, or ``(None, seconds_until_one_might)``.

        With ``retries_only`` only requests that are retries are considered
        (used to finish off work once ``max_pages`` is reached). With
        ``min_priority`` requests of lower priority are left queued.
        """
        best: _Entry | None = None
        best_domain = ""
        wait: float | None = None
        for domain, queue in self._queues.items():
            if not queue:
                continue
            if retries_only:
                head = min((e for e in queue if e[2].retries), default=None)
                if head is None:
                    continue
            else:
                head = queue[0]
                if min_priority is not None and -head[0] < min_priority:
                    continue
            slot = throttle.slot(domain)
            if slot.active >= slot.concurrency:
                continue  # woken up again when a request finishes
            ready = slot.ready_at()
            if now < ready:
                wait = ready - now if wait is None else min(wait, ready - now)
                continue
            if best is None or head[:2] < best[:2]:
                best, best_domain = head, domain
        if best is None:
            return None, wait
        queue = self._queues[best_domain]
        if queue[0] is best:
            heapq.heappop(queue)
        else:
            queue.remove(best)
            heapq.heapify(queue)
        if not queue:
            del self._queues[best_domain]
        self._size -= 1
        request = best[2]
        if request.retries:
            self.retry_count -= 1
        return request, None

    def pending(self) -> list[Request]:
        """Every queued request (highest priority first) without removing them."""
        entries = [e for q in self._queues.values() for e in q]
        entries.sort(key=lambda e: (e[0], e[1]))
        return [e[2] for e in entries]

    def clear(self) -> None:
        self._queues.clear()
        self._size = 0
        self.retry_count = 0
