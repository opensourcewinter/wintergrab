"""Structured events: what a crawl is doing, as a stream of small dicts.

Every spider has an :class:`EventBus` (``spider.events``). The engine and the
data pipeline emit events on it; subscribers receive :class:`Event` objects::

    spider = MySpider()
    spider.events.subscribe(print, "request_failed")      # one kind
    spider.events.subscribe(JsonlEventSink("events.jsonl"))  # everything
    spider.run()

Handlers may be plain functions or coroutines. A handler that raises is logged
and otherwise ignored: observing a crawl never breaks it. High-volume events
(``response``, ``item_scraped``) are only built when someone subscribed to them,
so an unobserved crawl pays nothing.

Event kinds emitted by wintergrab (see :data:`EVENT_KINDS`)::

    crawl_started      spider, resumed, queued
    crawl_finished     spider, status, stats, limit_reason
    response           url, status, bytes, latency, source, cache      (high volume)
    item_scraped       item                                             (high volume)
    item_dropped       reason, item
    request_retried    url, reason, attempt, delay
    request_failed     url, error, category, kind, status
    blocked            url, status, domain
    throttle_backoff   domain, delay, concurrency, retry_after
    policy_refused     url, reason, policy
    budget_exhausted   budget, used, limit
    ...and from the data layer: record_created, record_updated, record_deleted,
    extraction_failed, schema_changed, quality_degraded, site_changed, job_failed.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

__all__ = ["EVENT_KINDS", "Event", "EventBus", "EventRecorder", "JsonlEventSink", "LoggingEventSink"]

log = logging.getLogger("wintergrab.events")

#: Every event kind wintergrab itself emits.
EVENT_KINDS: frozenset[str] = frozenset(
    {
        "crawl_started", "crawl_finished", "response", "item_scraped", "item_dropped", "request_retried",
        "request_failed", "blocked", "throttle_backoff", "policy_refused", "budget_exhausted",
        "record_created", "record_updated", "record_deleted", "extraction_failed", "schema_changed",
        "quality_degraded", "site_changed", "job_started", "job_finished", "job_failed",
    }
)  # fmt: skip
#: Kinds that fire once per response or item; only built when subscribed to.
HIGH_VOLUME = frozenset({"response", "item_scraped"})

Handler = Callable[["Event"], Any]


@dataclass(frozen=True)
class Event:
    """One thing that happened.

    Attributes:
        kind: What happened (``"request_failed"``...).
        data: Details; always JSON-serialisable for wintergrab's own events.
        time: Wall-clock time (seconds since the epoch).
        origin: Who emitted it (the spider name, a job name...).
    """

    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    time: float = field(default_factory=time.time)
    origin: str | None = None

    def to_dict(self) -> dict[str, Any]:
        stamp = datetime.fromtimestamp(self.time, tz=timezone.utc).isoformat(timespec="milliseconds")
        out: dict[str, Any] = {"event": self.kind, "time": stamp}
        if self.origin:
            out["origin"] = self.origin
        out.update({k: v for k, v in self.data.items() if k not in ("event", "time", "origin")})
        return out

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


class EventBus:
    """Delivers events to subscribers (thread-safe to subscribe; emit from the crawl's loop or any thread)."""

    def __init__(self, origin: str | None = None) -> None:
        self.origin = origin
        self._handlers: list[tuple[frozenset[str] | None, Handler]] = []
        self._lock = threading.Lock()
        self._tasks: set[asyncio.Future[Any]] = set()
        self.errors = 0

    def subscribe(self, handler: Handler, kinds: str | Iterable[str] | None = None) -> Callable[[], None]:
        """Call ``handler(event)`` for events of ``kinds`` (all when ``None``). Returns an unsubscribe function."""
        selected = frozenset([kinds] if isinstance(kinds, str) else kinds) if kinds is not None else None
        entry = (selected, handler)
        with self._lock:
            self._handlers = [*self._handlers, entry]

        def unsubscribe() -> None:
            with self._lock:
                self._handlers = [h for h in self._handlers if h is not entry]

        return unsubscribe

    def wants(self, kind: str) -> bool:
        """Whether anyone listens to ``kind`` (check before building an expensive event)."""
        return any(kinds is None or kind in kinds for kinds, _ in self._handlers)

    def __bool__(self) -> bool:
        return bool(self._handlers)

    def emit(self, kind: str, /, **data: Any) -> None:
        """Deliver an event now. Coroutine handlers are scheduled on the running loop.

        ``kind`` is positional-only, so events may carry fields named ``kind`` or ``source``.
        """
        handlers = self._handlers
        if not handlers:
            return
        event: Event | None = None
        for kinds, handler in handlers:
            if kinds is not None and kind not in kinds:
                continue
            if event is None:
                event = Event(kind, data, time.time(), self.origin)
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    self._schedule(result)
            except Exception as exc:
                self.errors += 1
                log.warning("event handler %r failed on %s: %s", handler, kind, exc)

    def _schedule(self, awaitable: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # emitted outside the event loop: run the handler to completion here
            if inspect.iscoroutine(awaitable):
                with contextlib.suppress(Exception):
                    asyncio.run(awaitable)
            return
        task = asyncio.ensure_future(awaitable, loop=loop)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Future[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.errors += 1
            log.warning("async event handler failed: %s", task.exception())

    async def drain(self, timeout: float = 10.0) -> None:
        """Wait (up to ``timeout``) for scheduled coroutine handlers to finish."""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)

    def close(self) -> None:
        """Close subscribers that have a ``close()`` method (file sinks)."""
        for _, handler in self._handlers:
            closer = getattr(handler, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception as exc:  # pragma: no cover - best effort
                    log.debug("could not close %r: %s", handler, exc)


def _json_default(value: Any) -> Any:
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if isinstance(value, BaseException):
        return f"{type(value).__name__}: {value}"
    return str(value)


class JsonlEventSink:
    """Appends events as JSON lines to a file (a subscriber; pass it to :meth:`EventBus.subscribe`).

    Lines are flushed every ``flush_every`` events and on :meth:`close`, so a
    crash loses at most a few events.
    """

    def __init__(self, path: str | os.PathLike[str], *, flush_every: int = 32) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh: TextIO | None = open(self.path, "a", encoding="utf-8")  # noqa: SIM115 - closed in close()
        self._lock = threading.Lock()
        self._pending = 0
        self.flush_every = max(1, flush_every)
        self.count = 0

    def __call__(self, event: Event) -> None:
        line = json.dumps(event.to_dict(), ensure_ascii=False, default=_json_default)
        with self._lock:
            if self._fh is None:
                return
            self._fh.write(line + "\n")
            self.count += 1
            self._pending += 1
            if self._pending >= self.flush_every or event.kind in ("crawl_finished", "job_failed"):
                self._fh.flush()
                self._pending = 0

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None

    def __repr__(self) -> str:
        return f"JsonlEventSink({str(self.path)!r})"


class LoggingEventSink:
    """Logs events on the ``wintergrab.events`` logger (one line each)."""

    def __init__(self, level: int = logging.INFO) -> None:
        self.level = level

    def __call__(self, event: Event) -> None:
        details = ", ".join(f"{k}={v!r}" for k, v in event.data.items() if k != "stats")
        log.log(self.level, "%s: %s", event.kind, details)


class EventRecorder:
    """Keeps the last ``maxlen`` events in memory (tests, dashboards, debugging)."""

    def __init__(self, maxlen: int | None = 10_000) -> None:
        self.events: deque[Event] = deque(maxlen=maxlen)

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    def of(self, kind: str) -> list[Event]:
        return [e for e in self.events if e.kind == kind]

    def __len__(self) -> int:
        return len(self.events)
