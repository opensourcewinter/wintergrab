"""HTTP response cache: skip repeat downloads, revalidate cheaply, replay offline.

The cache is a single SQLite file. It serves four workflows:

* ``"revalidate"`` (default) - behaves like a browser cache: fresh responses
  (``Cache-Control: max-age``, ``Expires``, or your ``ttl``) are served from
  disk, stale ones are revalidated with ``If-None-Match`` /
  ``If-Modified-Since`` so unchanged pages cost a tiny ``304``. Ideal for
  recurring crawls that only want what changed.
* ``"prefer"`` - anything cached is used, whatever its age; only misses hit the
  network. Ideal while developing a scraper: fetch once, iterate on parsing.
* ``"offline"`` - never touch the network; misses raise :class:`CacheMiss`.
  Replays a recorded crawl deterministically (tests, debugging, demos).
* ``"refresh"`` - always download and overwrite the cache.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import CacheMiss
from .response import Headers, Response

if TYPE_CHECKING:
    from ..adaptive.storage import AdaptiveStorage
    from ..request import Request

__all__ = ["CACHE_MODES", "CacheLayer", "CacheMiss", "CachedResponse", "HTTPCache"]

CACHE_MODES = ("revalidate", "prefer", "offline", "refresh")
DEFAULT_CACHE_DIR = ".wintergrab-cache"
CACHEABLE_STATUSES = frozenset({200, 203, 204, 300, 301, 308, 404, 405, 410, 414, 501})
# Headers a 304 may update on the stored response (RFC 9111 section 4.3.4).
_REFRESH_HEADERS = ("cache-control", "expires", "etag", "last-modified", "date", "vary", "content-location")


@dataclass
class CachedResponse:
    """A response as stored in the cache."""

    key: str
    url: str
    status: int
    reason: str
    headers: list[tuple[str, str]]
    body: bytes
    stored_at: float
    http_version: str | None = None
    history: list[str] = field(default_factory=list)
    encoding: str | None = None

    @property
    def header_map(self) -> Headers:
        return Headers(self.headers)

    def age(self, now: float | None = None) -> float:
        return max(0.0, (now or time.time()) - self.stored_at)

    def to_response(
        self, request: Request, adaptive_storage: AdaptiveStorage | None, cache_status: str, source: str = "http"
    ) -> Response:
        response = Response(
            self.url,
            status=self.status,
            headers=Headers(self.headers),
            body=self.body,
            request=request,
            reason=self.reason,
            encoding=self.encoding,
            history=list(self.history),
            http_version=self.http_version,
            source=source,
            adaptive_storage=adaptive_storage,
        )
        response.cache_status = cache_status
        return response


def _cache_control(headers: Headers) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for part in headers.get("cache-control", "").split(","):
        name, _, value = part.strip().partition("=")
        if name:
            out[name.lower()] = value.strip('"') if value else None
    return out


def _http_date(value: str | None) -> float | None:
    if not value:
        return None
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return when.timestamp() if when is not None else None


class HTTPCache:
    """SQLite-backed HTTP cache shared by fetchers and spiders.

    Args:
        path: Cache file, or a directory (``http.sqlite3`` is created inside).
            Defaults to ``./.wintergrab-cache/http.sqlite3``.
        mode: ``"revalidate"``, ``"prefer"``, ``"offline"`` or ``"refresh"``
            (see the module docs).
        ttl: Treat responses younger than this many seconds as fresh,
            regardless of their caching headers.
        statuses: Status codes worth caching.
        methods: Methods worth caching (``GET``/``HEAD`` by default).
        max_body: Bodies bigger than this (bytes) are not stored.
        respect_no_store: In ``"revalidate"`` mode, don't store responses
            marked ``Cache-Control: no-store``/``private``. The other modes store
            everything you fetch - it's your local copy.

    Example::

        cache = HTTPCache(".cache", mode="prefer")
        with Fetcher(cache=cache) as f:
            f.get(url)   # network
            f.get(url)   # disk, instantly
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        mode: str = "revalidate",
        ttl: float | None = None,
        statuses: Iterable[int] = CACHEABLE_STATUSES,
        methods: Iterable[str] = ("GET", "HEAD"),
        max_body: int = 50 * 1024 * 1024,
        respect_no_store: bool = True,
    ) -> None:
        if mode not in CACHE_MODES:
            raise ValueError(f"cache mode must be one of {', '.join(CACHE_MODES)}")
        target = Path(path) if path else Path(DEFAULT_CACHE_DIR)
        if target.suffix == "" or target.is_dir():
            target = target / "http.sqlite3"
        target.parent.mkdir(parents=True, exist_ok=True)
        self.path = target
        self.mode = mode
        self.ttl = ttl
        self.statuses = frozenset(statuses)
        self.methods = frozenset(m.upper() for m in methods)
        self.max_body = max_body
        self.respect_no_store = respect_no_store
        self.stats: dict[str, int] = {"hits": 0, "misses": 0, "revalidated": 0, "stored": 0}
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            str(target), check_same_thread=False, isolation_level=None
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS responses ("
            " key TEXT PRIMARY KEY, url TEXT NOT NULL, status INTEGER NOT NULL, reason TEXT, headers TEXT NOT NULL,"
            " body BLOB NOT NULL, compressed INTEGER NOT NULL, stored_at REAL NOT NULL, http_version TEXT,"
            " history TEXT, encoding TEXT)"
        )

    # ------------------------------------------------------------------ #
    # construction helpers
    # ------------------------------------------------------------------ #
    @classmethod
    def coerce(
        cls,
        value: HTTPCache | str | os.PathLike[str] | bool | None,
        *,
        mode: str | None = None,
        ttl: float | None = None,
    ) -> HTTPCache | None:
        """``True`` -> default cache, a path -> cache there, a cache -> itself."""
        if value is None or value is False:
            return None
        if isinstance(value, HTTPCache):
            return value
        kwargs: dict[str, Any] = {"ttl": ttl}
        if mode:
            kwargs["mode"] = mode
        return cls(None if value is True else value, **kwargs)

    # ------------------------------------------------------------------ #
    # lookup / store
    # ------------------------------------------------------------------ #
    def handles(self, method: str) -> bool:
        return method.upper() in self.methods

    @staticmethod
    def key(request: Request, namespace: str = "") -> str:
        return namespace + request.fingerprint().hex()

    def get(self, request: Request, namespace: str = "") -> CachedResponse | None:
        key = self.key(request, namespace)
        with self._lock:
            if self._conn is None:
                return None
            row = self._conn.execute(
                "SELECT url, status, reason, headers, body, compressed, stored_at, http_version, history, encoding"
                " FROM responses WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            self.stats["misses"] += 1
            return None
        url, status, reason, headers, body, compressed, stored_at, http_version, history, encoding = row
        return CachedResponse(
            key=key,
            url=url,
            status=status,
            reason=reason or "",
            headers=[tuple(pair) for pair in json.loads(headers)],  # type: ignore[misc]
            body=zlib.decompress(body) if compressed else bytes(body),
            stored_at=stored_at,
            http_version=http_version,
            history=json.loads(history) if history else [],
            encoding=encoding,
        )

    def storable(self, request: Request, response: Response) -> bool:
        if not self.handles(request.method) or response.status not in self.statuses:
            return False
        if len(response.body) > self.max_body:
            return False
        if self.mode == "revalidate" and self.respect_no_store:
            directives = _cache_control(response.headers)
            if "no-store" in directives or "private" in directives:
                return False
        return True

    def put(self, request: Request, response: Response, namespace: str = "") -> bool:
        """Store ``response`` for ``request`` (if cacheable). Returns whether it was stored."""
        if not self.storable(request, response):
            return False
        body = response.body
        compressed = len(body) > 512
        blob = zlib.compress(body, 3) if compressed else body
        headers = json.dumps([[k, v] for k in response.headers for v in response.headers.get_list(k)])
        encoding = response._encoding if response.source == "browser" else None
        with self._lock:
            if self._conn is None:
                return False
            self._conn.execute(
                "INSERT OR REPLACE INTO responses (key, url, status, reason, headers, body, compressed, stored_at,"
                " http_version, history, encoding) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.key(request, namespace),
                    response.url,
                    response.status,
                    response.reason,
                    headers,
                    blob,
                    int(compressed),
                    time.time(),
                    response.http_version,
                    json.dumps(response.history),
                    encoding,
                ),
            )
        self.stats["stored"] += 1
        return True

    def refresh(self, entry: CachedResponse, not_modified: Response) -> CachedResponse:
        """Apply a ``304 Not Modified`` to a stored entry and return the updated entry."""
        merged = Headers(entry.headers)
        for name in _REFRESH_HEADERS:
            if name in not_modified.headers:
                merged[name] = not_modified.headers[name]
        entry.headers = [(k, v) for k in merged for v in merged.get_list(k)]
        entry.stored_at = time.time()
        with self._lock:
            if self._conn is not None:
                self._conn.execute(
                    "UPDATE responses SET headers = ?, stored_at = ? WHERE key = ?",
                    (json.dumps([list(p) for p in entry.headers]), entry.stored_at, entry.key),
                )
        self.stats["revalidated"] += 1
        return entry

    # ------------------------------------------------------------------ #
    # freshness
    # ------------------------------------------------------------------ #
    def is_fresh(self, entry: CachedResponse, now: float | None = None) -> bool:
        """Whether ``entry`` can be served without asking the server."""
        now = now or time.time()
        age = entry.age(now)
        if self.ttl is not None:
            return age < self.ttl
        headers = entry.header_map
        directives = _cache_control(headers)
        if "no-cache" in directives or "no-store" in directives:
            return False
        for name in ("s-maxage", "max-age"):
            value = directives.get(name)
            if value is not None:
                try:
                    return age < float(value)
                except ValueError:
                    return False
        expires = _http_date(headers.get("expires"))
        if expires is not None:
            date = _http_date(headers.get("date")) or entry.stored_at
            return entry.stored_at + (expires - date) > now
        last_modified = _http_date(headers.get("last-modified"))
        if last_modified is not None:
            # Heuristic freshness: 10% of the time since the last change, capped at a day.
            date = _http_date(headers.get("date")) or entry.stored_at
            return age < min(86400.0, max(0.0, (date - last_modified) * 0.1))
        return False

    @staticmethod
    def conditional_headers(entry: CachedResponse) -> dict[str, str]:
        headers = entry.header_map
        out: dict[str, str] = {}
        if "etag" in headers:
            out["If-None-Match"] = headers["etag"]
        if "last-modified" in headers:
            out["If-Modified-Since"] = headers["last-modified"]
        return out

    # ------------------------------------------------------------------ #
    # housekeeping
    # ------------------------------------------------------------------ #
    def __len__(self) -> int:
        with self._lock:
            if self._conn is None:
                return 0
            return int(self._conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0])

    def delete(self, request: Request, namespace: str = "") -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.execute("DELETE FROM responses WHERE key = ?", (self.key(request, namespace),))

    def clear(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.execute("DELETE FROM responses")

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __repr__(self) -> str:
        return f"HTTPCache({str(self.path)!r}, mode={self.mode!r})"


class CacheLayer:
    """Glue between a fetcher and an :class:`HTTPCache` (shared by sync/async/browser)."""

    def __init__(self, cache: HTTPCache, namespace: str = "", source: str = "http") -> None:
        self.cache = cache
        self.namespace = namespace
        self.source = source

    def before(
        self, request: Request, adaptive_storage: AdaptiveStorage | None
    ) -> tuple[Response | None, CachedResponse | None, dict[str, str]]:
        """Returns ``(cached_response_to_serve, stale_entry, conditional_headers)``."""
        cache = self.cache
        if not cache.handles(request.method) or cache.mode == "refresh":
            if cache.mode == "offline":
                raise CacheMiss(request.url)  # offline means offline, whatever the method
            return None, None, {}
        entry = cache.get(request, self.namespace)
        if entry is None:
            if cache.mode == "offline":
                raise CacheMiss(request.url)
            return None, None, {}
        if cache.mode in ("prefer", "offline") or cache.is_fresh(entry):
            cache.stats["hits"] += 1
            return entry.to_response(request, adaptive_storage, "hit", self.source), None, {}
        if self.source != "http":
            return None, None, {}  # browsers cannot send conditional requests for us
        return None, entry, cache.conditional_headers(entry)

    def after(
        self,
        request: Request,
        response: Response,
        stale: CachedResponse | None,
        adaptive_storage: AdaptiveStorage | None,
    ) -> Response:
        if response.status == 304 and stale is not None:
            entry = self.cache.refresh(stale, response)
            served = entry.to_response(request, adaptive_storage, "revalidated", self.source)
            served.elapsed = response.elapsed
            return served
        if self.cache.put(request, response, self.namespace):
            response.cache_status = "stored"
        return response
