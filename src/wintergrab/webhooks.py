"""Webhooks: a crawl's events, posted to URLs as they happen.

::

    class Shop(Spider):
        webhooks = [{"url": "https://hooks.example/wintergrab",
                     "events": ["crawl_finished", "record_updated", "job_failed"],
                     "secret": os.environ["WINTERGRAB_WEBHOOK_SECRET"]}]

Each delivery is a ``POST`` of JSON: ``{"events": [...]}``, the events in the form of
:meth:`Event.to_dict <wintergrab.events.Event.to_dict>` (``{"event": "record_updated", "time": ...,
"origin": "shop", "url": ..., ...}``). Events are gathered for a second (at most 100 per
delivery), so a crawl that changes ten thousand records does not make ten thousand requests.

With a ``secret``, the body is signed: ``X-Wintergrab-Signature: sha256=<hex HMAC-SHA256 of the
body>``; compare it (in constant time) before trusting a delivery. A delivery that fails (no
answer, a 5xx or 429) is tried again after 1 and 4 seconds, then given up (logged); other statuses
are not retried. Webhooks never slow a crawl down or break it: deliveries happen on their own
thread, and at most ``max_queue`` events wait (more are dropped and counted).

Which events there are: see :data:`wintergrab.events.EVENT_KINDS`: the crawl's (``crawl_started``,
``crawl_finished``, ``request_failed``, ``blocked``, ``budget_exhausted``...), the changes a
``history`` finds (``changes_detected``, ``site_changed``, and per page ``record_created``,
``record_updated``, ``record_deleted``), the data's (``extraction_failed`` per page,
``quality_degraded``, ``schema_changed``), and a project's jobs (``job_started``, ``job_finished``,
``job_failed``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from typing import Any

from . import __version__
from .errors import ConfigurationError
from .events import EVENT_KINDS, Event

__all__ = ["PER_PAGE", "SIGNATURE_HEADER", "Webhook", "sign", "verify"]

log = logging.getLogger("wintergrab.webhooks")

#: The header that carries a delivery's signature.
SIGNATURE_HEADER = "X-Wintergrab-Signature"
_RETRY_AFTER = (1.0, 4.0)  # seconds before the second and the third attempt
#: One per page or item: posted only when asked for by name.
PER_PAGE = frozenset(
    {"response", "item_scraped", "record_created", "record_updated", "record_deleted", "extraction_failed"}
)


def sign(body: bytes, secret: str) -> str:
    """The signature of ``body``: ``sha256=<hex HMAC-SHA256>``."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify(body: bytes, signature: str | None, secret: str) -> bool:
    """Whether ``signature`` (the header's value) signs ``body`` with ``secret`` (constant time)."""
    return bool(signature) and hmac.compare_digest(sign(body, secret), str(signature))


class Webhook:
    """Posts events to ``url`` (an :class:`~wintergrab.events.EventBus` subscriber; see the module docs).

    Args:
        url: Where to post (``http://`` or ``https://``).
        events: The event kinds to post (``"*"``: all; by default all but the ones of
            :data:`PER_PAGE`, which come one per page or item).
        secret: Sign deliveries with this.
        batch: At most this many events per delivery.
        interval: Seconds to wait for more events before a delivery.
        timeout: Seconds for each attempt.
        max_queue: Events waiting at most (more are dropped).
        headers: More headers (``Authorization``...).
    """

    def __init__(
        self,
        url: str,
        *,
        events: Iterable[str] | None = None,
        secret: str | None = None,
        batch: int = 100,
        interval: float = 1.0,
        timeout: float = 10.0,
        max_queue: int = 10_000,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ConfigurationError(f"a webhook's url must be http(s), not {url!r}", key="webhooks")
        self.url = url
        self.events = frozenset(events) if events is not None else None
        unknown = sorted((self.events or frozenset()) - EVENT_KINDS - {"*"})
        if unknown:
            log.warning("webhook %s: unknown event kind(s) %s (they may never come)", url, ", ".join(unknown))
        self.secret = secret or None
        self.batch = max(1, batch)
        self.interval = max(0.0, interval)
        self.timeout = timeout
        self.headers = dict(headers or {})
        #: Deliveries made, events posted, and events dropped or given up on.
        self.stats = {"deliveries": 0, "delivered": 0, "failed": 0, "dropped": 0}
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=max_queue)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @classmethod
    def coerce(cls, value: Any) -> Webhook:
        """A webhook, from itself, a URL, or a mapping of its arguments (``{"url": ..., "events": [...]}``)."""
        if isinstance(value, Webhook):
            return value
        if isinstance(value, str):
            return cls(value)
        if isinstance(value, Mapping):
            known = {"url", "events", "secret", "batch", "interval", "timeout", "max_queue", "headers"}
            extra = sorted(set(value) - known)
            if extra:
                raise ConfigurationError(f"a webhook has no setting {', '.join(extra)}", key="webhooks")
            events = value.get("events")
            return cls(**{**value, "events": [events] if isinstance(events, str) else events})
        raise ConfigurationError(f"a webhook is a URL or a mapping, not {value!r}", key="webhooks")

    def kinds(self) -> frozenset[str] | None:
        """The kinds to subscribe to (``None``: all)."""
        if self.events is None or "*" in self.events:
            return None
        return self.events

    # -- receiving ------------------------------------------------------------------------------ #
    def __call__(self, event: Event) -> None:
        if self.events is None and event.kind in PER_PAGE:
            return
        self.post_event(event.to_dict())

    def post_event(self, data: dict[str, Any]) -> None:
        """Queue one event (a dict with at least ``"event"``) for delivery."""
        self._start()
        try:
            self._queue.put_nowait(data)
        except queue.Full:
            self.stats["dropped"] += 1

    def _start(self) -> None:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=f"webhook {self.url}", daemon=True)
                self._thread.start()

    # -- delivering ----------------------------------------------------------------------------- #
    def _run(self) -> None:
        while True:
            first = self._queue.get()
            if first is None:
                return
            events = [first]
            deadline = time.monotonic() + self.interval
            closing = False
            while len(events) < self.batch:
                remaining = deadline - time.monotonic()
                try:
                    more = self._queue.get(timeout=max(0.0, remaining)) if remaining > 0 else self._queue.get_nowait()
                except queue.Empty:
                    break
                if more is None:
                    closing = True
                    break
                events.append(more)
            self._deliver(events)
            if closing:
                return

    def _deliver(self, events: list[dict[str, Any]]) -> None:
        body = json.dumps({"events": events}, ensure_ascii=False, default=str).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": f"wintergrab/{__version__} (webhook)",
                   **self.headers}  # fmt: skip
        if self.secret:
            headers[SIGNATURE_HEADER] = sign(body, self.secret)
        for attempt in range(len(_RETRY_AFTER) + 1):
            request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as answer:
                    answer.read()
                self.stats["deliveries"] += 1
                self.stats["delivered"] += len(events)
                return
            except urllib.error.HTTPError as exc:
                retry = exc.code >= 500 or exc.code == 429
                reason = f"HTTP {exc.code}"
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                retry, reason = True, str(getattr(exc, "reason", exc))
            if not retry or attempt == len(_RETRY_AFTER):
                self.stats["failed"] += len(events)
                log.warning("webhook %s: %d event(s) not delivered (%s)", self.url, len(events), reason)
                return
            time.sleep(_RETRY_AFTER[attempt])

    def close(self, timeout: float = 15.0) -> None:
        """Deliver what is waiting, and stop (waits at most ``timeout`` seconds)."""
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is None:
            return
        try:
            self._queue.put(None, timeout=timeout)
        except queue.Full:
            return
        thread.join(timeout)

    def __repr__(self) -> str:
        return f"Webhook({self.url!r}, events={sorted(self.events) if self.events is not None else 'all'})"
