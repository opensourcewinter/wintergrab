"""A frontier in PostgreSQL, shared by the processes (on one machine or several) that crawl together.

::

    class Shop(Spider):
        frontier = "postgresql://crawler@db.internal/crawls"            # the same in every process
        output = "postgresql://crawler@db.internal/shop?table=products"  # their items: one table (or a file each)
        unique_key = "url"

    Shop().run()        # in as many processes, on as many machines, as wanted

Each process takes the best request that may start now, leases it, and deletes it once done (its follow-up
requests queued first): a request is fetched by one process at a time, and a process renews its leases while it
runs, so the requests of one that died go back to the queue when their lease runs out (a minute). The duplicate
filter is shared: a URL one process queued is not queued again by another. So is each site's pace: a request to a
site starts once the site's delay since the last request to it, by any process, has passed, and a site that asked
to slow down (``Retry-After``, a 429) is slowed down for all of them. (Each process's concurrency per site counts on
its own: with a delay of 0, several processes send several requests at once.)

A crawl is named by its spider (or ``?crawl=NAME``). The first process to start it seeds it; the others join it
and add to its output. Once its queue is empty and no request is leased, it is finished, and the next start begins
it again. ``run(resume=False)`` (``--fresh``) empties it for every process.

Requests are kept as JSON, never as pickles: the database holds data, not code another process would run. Their
``meta``, ``cb_kwargs`` and bodies must be JSON values (bytes are kept too); anything else is an error when the
request is queued. Each process keeps its own stats, budgets (``max_pages`` counts its own pages), checkpoint
and output. The connection is read like a PostgreSQL output's: keep the password out of the URL (``PGPASSWORD``,
``~/.pgpass``).
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import socket
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..errors import ConfigurationError, WintergrabError
from ..redact import redact_url
from ..request import Request

if TYPE_CHECKING:
    from .spider import Spider
    from .throttle import AutoThrottle

__all__ = ["SharedScheduler"]

log = logging.getLogger(__name__)
_T = TypeVar("_T")

_SETUP = """
CREATE TABLE IF NOT EXISTS wintergrab_crawls (
    crawl text PRIMARY KEY, status text NOT NULL, started timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS wintergrab_frontier (
    id bigserial PRIMARY KEY, crawl text NOT NULL, domain text NOT NULL, priority integer NOT NULL,
    retries integer NOT NULL DEFAULT 0, state smallint NOT NULL DEFAULT 0, worker text, leased_until timestamptz,
    request text NOT NULL
);
CREATE INDEX IF NOT EXISTS wintergrab_frontier_order ON wintergrab_frontier (crawl, priority DESC, id);
CREATE TABLE IF NOT EXISTS wintergrab_frontier_seen (
    crawl text NOT NULL, fingerprint bytea NOT NULL, PRIMARY KEY (crawl, fingerprint)
);
CREATE TABLE IF NOT EXISTS wintergrab_frontier_domains (
    crawl text NOT NULL, domain text NOT NULL, next_at timestamptz NOT NULL DEFAULT '-infinity',
    PRIMARY KEY (crawl, domain)
);
"""
_TABLES = ("wintergrab_frontier", "wintergrab_frontier_seen", "wintergrab_frontier_domains")
#: A lease is renewed at each commit (about every second); one not renewed for this long is free again.
LEASE_SECONDS = 60.0


class SharedScheduler:
    """The engine's queue for ``frontier = "postgresql://..."`` (see the module docs), with the interface of the
    disk frontier: ``pop_ready`` leases a request, ``ack`` forgets it, ``commit`` writes what changed.

    Args:
        url: The database, and optionally ``?crawl=NAME`` (the spider's name by default).
        spider: The spider whose methods are the requests' callbacks.
        dedupe: Drop requests whose fingerprint was queued before (by any process).
        lifo: Newest first among equal priorities (depth-first).
        fresh: Empty the crawl (for every process) instead of joining it.
        lease: Seconds a lease holds without being renewed.
    """

    persistent = True

    def __init__(
        self,
        url: str,
        spider: Spider | None,
        *,
        dedupe: bool = True,
        lifo: bool = False,
        fresh: bool = False,
        lease: float = LEASE_SECONDS,
    ) -> None:
        parts = urlsplit(url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        self.crawl = dict(query).get("crawl") or (spider.name if spider is not None else "crawl")
        self.url = redact_url(url)
        self.spider = spider
        self.dedupe = dedupe
        self.lifo = lifo
        self.lease = lease
        self.duplicates = 0
        self.retry_count = 0  # (the engine finishes its own retries; the others' are the other processes')
        self.worker = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self._pushes: list[tuple[bytes | None, str, int, int, str]] = []  # fingerprint, domain, priority...
        self._fingerprints: list[bytes] = []  # marked as seen (restore_seen)
        self._acks: list[int] = []
        self._leases: dict[int, tuple[Request, int]] = {}  # id(request) -> (request, row id)
        self._local_seen: set[bytes] = set()
        self._count = 0
        self._counted = 0.0
        self._throttle: AutoThrottle | None = None
        self._closed = False
        from ..storage.postgres import _psycopg, connect_options

        self._psycopg = _psycopg()
        self._dsn = urlunsplit(parts._replace(query=urlencode([(k, v) for k, v in query if k != "crawl"])))
        self._options = connect_options(self._dsn)
        self._db = self._connect()
        self.seen = _Seen(self)
        #: Whether the crawl was running already (this process joins it).
        self.existed = self._start(fresh)

    # -- the database --------------------------------------------------------------------------- #
    def _connect(self) -> Any:
        try:
            db = self._psycopg.connect(self._dsn, autocommit=False, **self._options)
            # Commits need not wait for the disk: what a server crash could lose of the last moments is
            # redone (a request is deleted in the same transaction as the requests that came of it are queued).
            db.execute("SET synchronous_commit TO off")
            db.commit()
            return db
        except self._psycopg.Error as exc:
            raise ConfigurationError(f"frontier: cannot connect to {self.url}: {str(exc).strip()}") from None

    def _run(self, work: Callable[[Any], _T]) -> _T:
        """``work(cursor)`` in a transaction; once more, on a new connection, when the database hung up (the
        transaction was rolled back, so nothing is done twice)."""
        for attempt in (1, 2):
            try:
                with self._db.cursor() as cur:
                    result = work(cur)
                self._db.commit()
                return result
            except self._psycopg.OperationalError as exc:
                with contextlib.suppress(Exception):
                    self._db.close()
                reason = str(exc).strip()
                if attempt == 2:
                    raise WintergrabError(
                        f"frontier {self.url}: the database stopped answering ({reason}). What this process leased "
                        f"goes back to the queue within {self.lease:.0f} seconds; start it again to continue."
                    ) from None
                log.warning("frontier %s: the database hung up (%s); connecting again", self.url, reason)
                self._db = self._connect()
        raise AssertionError("unreachable")  # pragma: no cover

    def _start(self, fresh: bool) -> bool:
        def start(cur: Any) -> bool:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('wintergrab_frontier'))")  # (one setup at a time)
            cur.execute(_SETUP)
            cur.execute("SELECT status FROM wintergrab_crawls WHERE crawl = %s FOR UPDATE", (self.crawl,))
            row = cur.fetchone()
            joining = row is not None and row[0] == "running" and not fresh
            if not joining:
                for table in _TABLES:
                    cur.execute(f"DELETE FROM {table} WHERE crawl = %s", (self.crawl,))
                cur.execute(
                    "INSERT INTO wintergrab_crawls (crawl, status) VALUES (%s, 'running') ON CONFLICT (crawl) "
                    "DO UPDATE SET status = 'running', started = now()",
                    (self.crawl,),
                )
            return joining

        joining = self._run(start)
        log.info("frontier %s, crawl %r: %s", self.url, self.crawl, "joined" if joining else "started")
        return joining

    # -- the queue ---------------------------------------------------------------------------- #
    def __len__(self) -> int:
        """Requests of the crawl queued or leased, by any process (read at most twice a second)."""
        if time.monotonic() - self._counted >= 0.5:
            self._flush()

            def count(cur: Any) -> int:
                cur.execute("SELECT count(*) FROM wintergrab_frontier WHERE crawl = %s", (self.crawl,))
                return int(cur.fetchone()[0])

            self._count = self._run(count)
            self._counted = time.monotonic()
        return self._count

    def restore_seen(self, fingerprints: Iterable[bytes]) -> None:
        """Mark fingerprints as seen, for every process."""
        for fp in fingerprints:
            if fp not in self._local_seen:
                self._local_seen.add(fp)
                self._fingerprints.append(fp)

    def push(self, request: Request, *, force: bool = False) -> bool:
        """Queue a request (written at the next :meth:`commit`, or before the next pop). ``False``: a duplicate this
        process saw; one another process queued is dropped when written (and counted in :attr:`duplicates`)."""
        fp: bytes | None = None
        if self.dedupe and not force and not request.dont_filter:
            fp = request.fingerprint()
            if fp in self._local_seen:
                self.duplicates += 1
                return False
        data = json.dumps(_encode(request.to_dict(self.spider), request.url), ensure_ascii=False)
        if fp is not None:
            self._local_seen.add(fp)
        self._pushes.append((fp, request.host, request.priority, request.retries, data))
        return True

    def pop_ready(
        self, throttle: AutoThrottle, now: float, *, retries_only: bool = False, min_priority: int | None = None
    ) -> tuple[Request | None, float | None]:
        """The best request that may start now, leased; or ``(None, seconds until one might)``. A site this
        process is already busy with, or whose pace (its own, or the crawl's: any process's last request to
        it) says wait, is left for later."""
        self._throttle = throttle
        if self._pushes or self._fingerprints:  # (acks alone wait for the commit: those requests stay ours)
            self._flush()
        busy: list[str] = []
        wait: float | None = None
        for domain, slot in throttle.slots.items():
            ready = slot.ready_at()
            if slot.active >= slot.concurrency or now < ready:
                busy.append(domain)
                if slot.active < slot.concurrency:
                    wait = ready - now if wait is None else min(wait, ready - now)
        known = list(throttle.slots)  # (a site this process has not seen yet: the throttle's first delay)
        values = {
            "crawl": self.crawl, "busy": busy, "retries": retries_only,
            "floor": -(2**31) if min_priority is None else min_priority, "worker": self.worker, "lease": self.lease,
            "domains": known, "delays": [throttle.slots[d].delay for d in known], "delay": throttle.base_delay,
        }  # fmt: skip

        def pop(cur: Any) -> tuple[Any, Any]:
            cur.execute(_POP.format(order="DESC" if self.lifo else "ASC"), values)
            row = cur.fetchone()
            if row is not None:
                return row, None
            cur.execute(_NEXT, (self.crawl, busy))  # when might one be ready: the pace of sites with queued ones
            return None, cur.fetchone()[0]

        row, later = self._run(pop)
        if row is None:
            if later is not None:
                wait = float(later) if wait is None else min(wait, float(later))
            return None, wait
        request = Request.from_dict(_decode(json.loads(row[2])), self.spider)
        self._leases[id(request)] = (request, int(row[0]))
        return request, None

    def pending(self) -> list[Request]:
        """Every queued request of the crawl (highest priority first), without taking them."""
        self._flush()

        def queued(cur: Any) -> list[Any]:
            cur.execute(
                "SELECT request FROM wintergrab_frontier WHERE crawl = %s AND state = 0 ORDER BY priority DESC, id",
                (self.crawl,),
            )
            return list(cur.fetchall())

        return [Request.from_dict(_decode(json.loads(r[0])), self.spider) for r in self._run(queued)]

    def clear(self) -> None:
        """Drop every queued request of the crawl (for every process; leased ones stay until acked)."""
        self._pushes.clear()
        self._run(lambda cur: cur.execute("DELETE FROM wintergrab_frontier WHERE crawl = %s AND state = 0",
                                          (self.crawl,)))  # fmt: skip
        self._counted = 0.0

    # -- persistence -------------------------------------------------------------------------- #
    def ack(self, request: Request) -> bool:
        """The engine is done with a popped request: it is deleted at the next commit (after the requests
        queued since, so a crash never loses what came of it). ``False``: not leased here."""
        lease = self._leases.pop(id(request), None)
        if lease is None or lease[0] is not request:
            return False
        self._acks.append(lease[1])
        return True

    def leased(self) -> int:
        """Requests this process popped and has not acked."""
        return len(self._leases)

    def commit(self) -> None:
        """Write what changed (queued requests, then acks), renew this process's leases, and tell the crawl
        which sites this process was told to leave alone for a while."""
        self._flush()
        throttle = self._throttle
        now = time.monotonic()
        paused = (
            [(d, s.paused_until - now) for d, s in throttle.slots.items() if s.paused_until > now] if throttle else []
        )
        if not self._leases and not paused:
            return

        def renew(cur: Any) -> None:
            if self._leases:
                cur.execute(
                    "UPDATE wintergrab_frontier SET leased_until = now() + %s * interval '1 second' "
                    "WHERE crawl = %s AND worker = %s AND state = 1",
                    (self.lease, self.crawl, self.worker),
                )
            for domain, seconds in paused:
                cur.execute(
                    "UPDATE wintergrab_frontier_domains SET next_at = greatest(next_at, now() + %s * interval "
                    "'1 second') WHERE crawl = %s AND domain = %s",
                    (seconds, self.crawl, domain),
                )

        self._run(renew)

    def _flush(self) -> None:
        """Write the queued requests, the fingerprints marked seen, then the acks: one transaction, after which
        (and only then) they are forgotten here."""
        if self._closed or not (self._pushes or self._fingerprints or self._acks):
            return
        pushes, fingerprints, acks = list(self._pushes), list(self._fingerprints), list(self._acks)

        def write(cur: Any) -> int:
            wanted = [p[0] for p in pushes if p[0] is not None] + fingerprints
            new: set[bytes] = set()
            if wanted:
                cur.execute(
                    "INSERT INTO wintergrab_frontier_seen (crawl, fingerprint) SELECT %s, fp FROM unnest(%s::bytea[]) "
                    "AS t(fp) ON CONFLICT DO NOTHING RETURNING fingerprint",
                    (self.crawl, wanted),
                )
                new = {bytes(r[0]) for r in cur.fetchall()}
            rows = [p for p in pushes if p[0] is None or p[0] in new]
            if rows:
                cur.execute(
                    "INSERT INTO wintergrab_frontier_domains (crawl, domain) SELECT %s, d FROM unnest(%s::text[]) "
                    "AS t(d) ON CONFLICT DO NOTHING",
                    (self.crawl, sorted({r[1] for r in rows})),
                )
                cur.executemany(
                    "INSERT INTO wintergrab_frontier (crawl, domain, priority, retries, request) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    [(self.crawl, r[1], r[2], r[3], r[4]) for r in rows],
                )
            if acks:  # (one whose lease ran out and that another process took is left to it)
                cur.execute(
                    "DELETE FROM wintergrab_frontier WHERE id = ANY(%s::bigint[]) AND worker = %s",
                    (acks, self.worker),
                )
            return len(pushes) - len(rows)

        self.duplicates += self._run(write)  # (queued by another process meanwhile)
        del self._pushes[: len(pushes)], self._fingerprints[: len(fingerprints)], self._acks[: len(acks)]
        self._counted = 0.0  # (the count moved)

    def close(self, *, finished: bool = False) -> None:
        """Write what changed, give back what this process leased and did not finish, and, when the crawl is
        ``finished`` (nothing queued or leased by anyone), say so: the next start begins it again."""
        if self._closed:
            return

        def end(cur: Any) -> None:
            cur.execute(
                "UPDATE wintergrab_frontier SET state = 0, worker = NULL, leased_until = NULL "
                "WHERE crawl = %s AND worker = %s AND state = 1",
                (self.crawl, self.worker),
            )
            if finished:
                cur.execute("SELECT 1 FROM wintergrab_crawls WHERE crawl = %s FOR UPDATE", (self.crawl,))
                cur.execute("SELECT count(*) FROM wintergrab_frontier WHERE crawl = %s", (self.crawl,))
                if not cur.fetchone()[0]:
                    cur.execute("UPDATE wintergrab_crawls SET status = 'finished' WHERE crawl = %s", (self.crawl,))
                    for table in _TABLES[1:]:
                        cur.execute(f"DELETE FROM {table} WHERE crawl = %s", (self.crawl,))

        try:
            self._flush()
            self._run(end)
        finally:
            with contextlib.suppress(Exception):
                self._db.close()
            self._closed = True
            self._leases.clear()


_POP = """
WITH picked AS (
    SELECT f.id, f.domain FROM wintergrab_frontier f
    JOIN wintergrab_frontier_domains d ON d.crawl = f.crawl AND d.domain = f.domain
    WHERE f.crawl = %(crawl)s AND (f.state = 0 OR f.leased_until < now()) AND d.next_at <= now()
      AND NOT (f.domain = ANY(%(busy)s::text[])) AND (NOT %(retries)s OR f.retries > 0) AND f.priority >= %(floor)s
    ORDER BY f.priority DESC, f.id {order}
    LIMIT 1
    FOR UPDATE OF f, d SKIP LOCKED
), leased AS (
    UPDATE wintergrab_frontier f
    SET state = 1, worker = %(worker)s, leased_until = now() + %(lease)s * interval '1 second'
    FROM picked WHERE f.id = picked.id
    RETURNING f.id, f.domain, f.request
), paced AS (
    UPDATE wintergrab_frontier_domains d
    SET next_at = now() + COALESCE(
        (SELECT t.delay FROM unnest(%(domains)s::text[], %(delays)s::float8[]) AS t(domain, delay)
         WHERE t.domain = d.domain), %(delay)s) * interval '1 second'
    FROM leased WHERE d.crawl = %(crawl)s AND d.domain = leased.domain
)
SELECT id, domain, request FROM leased
"""
_NEXT = (
    "SELECT EXTRACT(EPOCH FROM min(d.next_at) - now()) FROM wintergrab_frontier_domains d "
    "WHERE d.crawl = %s AND NOT (d.domain = ANY(%s::text[])) AND d.next_at > now() AND EXISTS ("
    "SELECT 1 FROM wintergrab_frontier f WHERE f.crawl = d.crawl AND f.domain = d.domain "
    "AND (f.state = 0 OR f.leased_until < now()))"
)


class _Seen:
    """``fingerprint in scheduler.seen``: queued before, by this process or another."""

    def __init__(self, scheduler: SharedScheduler) -> None:
        self._scheduler = scheduler

    def __contains__(self, fingerprint: object) -> bool:
        scheduler = self._scheduler
        if fingerprint in scheduler._local_seen:
            return True

        def look(cur: Any) -> bool:
            cur.execute(
                "SELECT 1 FROM wintergrab_frontier_seen WHERE crawl = %s AND fingerprint = %s",
                (scheduler.crawl, fingerprint),
            )
            return cur.fetchone() is not None

        return scheduler._run(look)


def _encode(value: Any, url: str, where: str = "") -> Any:
    """A request's values as JSON values (bytes tagged), or an error saying which one is not."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes__": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, (list, tuple)):
        return [_encode(v, url, f"{where}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, Mapping) and all(isinstance(k, str) for k in value):
        encoded = {k: _encode(v, url, f"{where}[{k!r}]" if where else k) for k, v in value.items()}
        return {"__dict__": encoded} if "__bytes__" in value or "__dict__" in value else encoded
    raise ConfigurationError(
        f"a shared frontier keeps requests as JSON, and the {where or 'request'} of {url} is a "
        f"{type(value).__name__}: keep JSON values (or bytes) in meta and cb_kwargs"
    )


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode(v) for v in value]
    if isinstance(value, dict):
        if len(value) == 1 and "__bytes__" in value:
            return base64.b64decode(value["__bytes__"])
        if len(value) == 1 and "__dict__" in value:
            return {k: _decode(v) for k, v in value["__dict__"].items()}
        return {k: _decode(v) for k, v in value.items()}
    return value
