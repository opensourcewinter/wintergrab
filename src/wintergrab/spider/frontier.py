"""Disk-backed crawl frontier: Bloom-filter dedupe and a SQLite request queue.

:class:`~wintergrab.spider.scheduler.Scheduler` keeps every queued request (~700 bytes each) and every seen
fingerprint (~95 bytes each) in memory. :class:`DiskScheduler` is a drop-in replacement that keeps the queue in
SQLite, holds only a small per-domain buffer in memory and dedupes with a :class:`ScalableBloomFilter` (~3 bytes
per URL), so a crawl of tens of millions of URLs runs in flat memory and resumes instantly after a pause or crash.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import pickle
import sqlite3
import struct
import time
from collections.abc import Iterable
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any

from ..errors import CheckpointError
from ..request import Request
from .throttle import AutoThrottle

if TYPE_CHECKING:
    from .spider import Spider

__all__ = ["BloomFilter", "DiskScheduler", "ScalableBloomFilter"]

# --------------------------------------------------------------------------- #
# Bloom filters
# --------------------------------------------------------------------------- #
_BF_HEADER = struct.Struct("<4sBQdQIQ")  # magic, version, capacity, error_rate, num_bits, num_hashes, count
_BF_MAGIC = b"WGBF"
_SBF_HEADER = struct.Struct("<4sBQdddI")  # magic, version, initial_capacity, error_rate, growth, tightening, filters
_SBF_MAGIC = b"WGSB"
_LEN = struct.Struct("<Q")
_LN2 = math.log(2)


def _hash_pair(key: bytes | bytearray | memoryview) -> tuple[int, int]:
    """Two 64-bit hashes of ``key`` for double hashing."""
    digest = hashlib.blake2b(key, digest_size=16).digest()
    return int.from_bytes(digest[:8], "little"), int.from_bytes(digest[8:], "little")


def _is_prime(n: int) -> bool:
    """Deterministic Miller-Rabin (exact for n < 3.3e24)."""
    bases = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)
    if n < 2:
        return False
    for p in bases:
        if n % p == 0:
            return n == p
    d, s = n - 1, 0
    while not d & 1:
        d, s = d >> 1, s + 1
    for a in bases:
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = x * x % n
            if x == n - 1:
                break
        else:
            return False
    return True


def _next_prime(n: int) -> int:
    while not _is_prime(n):
        n += 1
    return n


class BloomFilter:
    """Fixed-size Bloom filter over ``bytes`` keys (no false negatives, ~``error_rate`` false positives).

    Sized for ``capacity`` keys with ``m = -n*ln(p)/ln(2)^2`` bits and ``k = m/n*ln(2)`` hash functions derived by
    double hashing one blake2b digest (``m`` is rounded up to a prime so the ``k`` positions are always distinct).
    That is ``1.44*log2(1/p)`` bits per key: ~2.4 bytes at ``p = 1e-4``, versus ~95 bytes per key for a Python
    ``set`` of 20-byte fingerprints.
    """

    def __init__(self, capacity: int, error_rate: float) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        if not 0.0 < error_rate < 1.0:
            raise ValueError("error_rate must be between 0 and 1")
        self.capacity = int(capacity)
        self.error_rate = float(error_rate)
        self.num_bits = _next_prime(max(64, math.ceil(-self.capacity * math.log(self.error_rate) / _LN2**2)))
        self.num_hashes = max(1, round(self.num_bits / self.capacity * _LN2))
        self.count = 0
        self._bits = bytearray((self.num_bits + 7) // 8)

    @property
    def nbytes(self) -> int:
        """Size of the bit array in bytes."""
        return len(self._bits)

    def __len__(self) -> int:
        """Approximate number of keys added (keys that were false positives are not counted)."""
        return self.count

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, (bytes, bytearray, memoryview)):
            return False
        return self._contains(*_hash_pair(key))

    def add(self, key: bytes) -> bool:
        """Add ``key``; ``True`` if it was not (as far as the filter can tell) present before."""
        return self._add(*_hash_pair(key))

    def _contains(self, h1: int, h2: int) -> bool:
        bits, m = self._bits, self.num_bits
        for i in range(self.num_hashes):
            pos = (h1 + i * h2) % m
            if not bits[pos >> 3] & (1 << (pos & 7)):
                return False
        return True

    def _add(self, h1: int, h2: int) -> bool:
        bits, m = self._bits, self.num_bits
        added = False
        for i in range(self.num_hashes):
            pos = (h1 + i * h2) % m
            mask = 1 << (pos & 7)
            if not bits[pos >> 3] & mask:
                bits[pos >> 3] |= mask
                added = True
        if added:
            self.count += 1
        return added

    def to_bytes(self) -> bytes:
        """Serialize (a small parameter header followed by the bit array)."""
        header = _BF_HEADER.pack(
            _BF_MAGIC, 1, self.capacity, self.error_rate, self.num_bits, self.num_hashes, self.count
        )
        return header + self._bits

    @classmethod
    def from_bytes(cls, data: bytes | bytearray | memoryview) -> BloomFilter:
        """Inverse of :meth:`to_bytes`."""
        view = memoryview(data)
        if len(view) < _BF_HEADER.size:
            raise ValueError("not a serialized BloomFilter")
        magic, version, capacity, error_rate, num_bits, num_hashes, count = _BF_HEADER.unpack_from(view)
        if magic != _BF_MAGIC or version != 1 or len(view) - _BF_HEADER.size != (num_bits + 7) // 8:
            raise ValueError("not a serialized BloomFilter (or a truncated one)")
        bf = cls.__new__(cls)
        bf.capacity, bf.error_rate, bf.num_bits, bf.num_hashes, bf.count = (
            capacity,
            error_rate,
            num_bits,
            num_hashes,
            count,
        )
        bf._bits = bytearray(view[_BF_HEADER.size :])
        return bf


class ScalableBloomFilter:
    """Bloom filter that grows with the number of keys, keeping the overall false-positive rate below ``error_rate``.

    Starts with one :class:`BloomFilter` for ``initial_capacity`` keys at ``error_rate * (1 - tightening)``; when
    it is full, a new one ``growth`` times larger with a ``tightening`` times lower error rate is added (the rates
    form a geometric series summing to ``error_rate``). A key is present if any filter contains it.

    Memory at ``error_rate=1e-4``: 24-26 bits (~3 bytes) per key of allocated capacity, allocated in steps of
    ``growth``, so 1-2x that per stored key: 10 M URLs take ~46 MB, versus ~1 GB for a ``set`` of fingerprints.
    """

    def __init__(
        self,
        initial_capacity: int = 1_000_000,
        error_rate: float = 1e-4,
        growth: float = 2,
        tightening: float = 0.9,
    ) -> None:
        if initial_capacity < 1:
            raise ValueError("initial_capacity must be at least 1")
        if not 0.0 < error_rate < 1.0:
            raise ValueError("error_rate must be between 0 and 1")
        if growth < 1:
            raise ValueError("growth must be at least 1")
        if not 0.0 < tightening < 1.0:
            raise ValueError("tightening must be between 0 and 1")
        self.initial_capacity = int(initial_capacity)
        self.error_rate = float(error_rate)
        self.growth = float(growth)
        self.tightening = float(tightening)
        self.filters: list[BloomFilter] = []
        self._grow()

    def _grow(self) -> BloomFilter:
        i = len(self.filters)
        capacity = max(1, int(self.initial_capacity * self.growth**i))
        bf = BloomFilter(capacity, self.error_rate * (1 - self.tightening) * self.tightening**i)
        self.filters.append(bf)
        return bf

    @property
    def params(self) -> dict[str, Any]:
        """Constructor arguments (to recreate an empty filter of the same kind)."""
        return {
            "initial_capacity": self.initial_capacity,
            "error_rate": self.error_rate,
            "growth": self.growth,
            "tightening": self.tightening,
        }

    @property
    def capacity(self) -> int:
        """Keys the allocated filters hold before the next growth step."""
        return sum(bf.capacity for bf in self.filters)

    @property
    def nbytes(self) -> int:
        """Total size of the bit arrays in bytes."""
        return sum(bf.nbytes for bf in self.filters)

    def __len__(self) -> int:
        """Approximate number of keys added."""
        return sum(bf.count for bf in self.filters)

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, (bytes, bytearray, memoryview)):
            return False
        return self._absent(key) is None

    def add(self, key: bytes) -> bool:
        """Add ``key``; ``True`` if it was not (as far as the filter can tell) present before."""
        hashes = self._absent(key)
        return hashes is not None and self._add_absent(hashes)

    def _absent(self, key: bytes | bytearray | memoryview) -> tuple[int, int] | None:
        """The key's hashes if it is absent (for :meth:`_add_absent`), ``None`` if present."""
        h1, h2 = _hash_pair(key)
        for bf in reversed(self.filters):
            if bf._contains(h1, h2):
                return None
        return h1, h2

    def _add_absent(self, hashes: tuple[int, int]) -> bool:
        current = self.filters[-1]
        if current.count >= current.capacity:
            current = self._grow()
        return current._add(*hashes)

    def to_bytes(self) -> bytes:
        """Serialize (parameter header, then each filter length-prefixed)."""
        parts = [
            _SBF_HEADER.pack(
                _SBF_MAGIC, 1, self.initial_capacity, self.error_rate, self.growth, self.tightening, len(self.filters)
            )
        ]
        for bf in self.filters:
            blob = bf.to_bytes()
            parts += [_LEN.pack(len(blob)), blob]
        return b"".join(parts)

    @classmethod
    def from_bytes(cls, data: bytes | bytearray | memoryview) -> ScalableBloomFilter:
        """Inverse of :meth:`to_bytes`."""
        view = memoryview(data)
        if len(view) < _SBF_HEADER.size:
            raise ValueError("not a serialized ScalableBloomFilter")
        magic, version, initial_capacity, error_rate, growth, tightening, n = _SBF_HEADER.unpack_from(view)
        if magic != _SBF_MAGIC or version != 1:
            raise ValueError("not a serialized ScalableBloomFilter")
        offset, filters = _SBF_HEADER.size, []
        for _ in range(n):
            (size,) = _LEN.unpack_from(view, offset)
            offset += _LEN.size
            filters.append(BloomFilter.from_bytes(view[offset : offset + size]))
            offset += size
        if offset != len(view):
            raise ValueError("trailing data after a serialized ScalableBloomFilter")
        return cls._from_parts(filters, initial_capacity, error_rate, growth, tightening)

    @classmethod
    def _from_parts(
        cls, filters: list[BloomFilter], initial_capacity: int, error_rate: float, growth: float, tightening: float
    ) -> ScalableBloomFilter:
        sbf = cls.__new__(cls)
        sbf.initial_capacity, sbf.error_rate, sbf.growth, sbf.tightening = (
            initial_capacity,
            error_rate,
            growth,
            tightening,
        )
        sbf.filters = list(filters)
        if not sbf.filters:
            sbf._grow()
        return sbf


# --------------------------------------------------------------------------- #
# SQLite-backed scheduler
# --------------------------------------------------------------------------- #
_QUEUED, _BUFFERED, _LEASED = 0, 1, 2
_SCHEMA_VERSION = 1
_JOURNAL_ROW_BYTES = 32  # rough on-disk cost of one seen_log row
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY, domain TEXT NOT NULL, priority INTEGER NOT NULL, seq INTEGER NOT NULL,
        retries INTEGER NOT NULL, state INTEGER NOT NULL, data BLOB NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS requests_next ON requests (domain, state, priority DESC, seq)",
    "CREATE INDEX IF NOT EXISTS requests_retries ON requests (domain, state, priority DESC, seq) WHERE retries > 0",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value BLOB)",
    "CREATE TABLE IF NOT EXISTS bloom (idx INTEGER PRIMARY KEY, data BLOB NOT NULL)",
    "CREATE TABLE IF NOT EXISTS seen_log (fp BLOB NOT NULL)",
)
_INSERT = "INSERT INTO requests (id, domain, priority, seq, retries, state, data) VALUES (?, ?, ?, ?, ?, ?, ?)"
_SET_STATE = "UPDATE requests SET state = ? WHERE id = ?"
_DELETE = "DELETE FROM requests WHERE id = ?"
_NEXT_ROWS = (
    "SELECT id, priority, retries, data FROM requests WHERE domain = ? AND state = 0 "
    "ORDER BY priority DESC, seq LIMIT ?"
)
_NEXT_RETRY = (
    "SELECT id, priority, data FROM requests WHERE domain = ? AND state = 0 AND retries > 0 "
    "ORDER BY priority DESC, seq LIMIT 1"
)

_Entry = tuple[int, int, Request]  # (-priority, row id = FIFO sequence, request)


class DiskScheduler:
    """SQLite-backed drop-in replacement for :class:`~wintergrab.spider.scheduler.Scheduler`.

    Every queued request is a row in the database at ``path``; at most ``buffer_per_domain`` requests per domain
    (always that domain's best ones) are held in memory, so ordering is exactly the in-memory Scheduler's: highest
    priority first, FIFO among equals, only from domains whose throttle slot is free. Seen fingerprints live in a
    :class:`ScalableBloomFilter` (``seen``).

    Crash semantics (at-least-once): writes are batched and committed every ``commit_every`` operations or
    ``commit_interval`` seconds (checked on each operation) and on :meth:`commit`/:meth:`close`. A commit is one
    SQLite transaction holding the queue *and* the new fingerprints, so the file always reflects a prefix of the
    operations. A popped request stays in the file (leased) until :meth:`ack`; on open, buffered and leased rows
    are queued again, so work in flight when the process died is delivered again and acked work is not. Work
    after the last commit is lost together with its fingerprints, so it is redone. For this to hold the engine
    must ack a request only after pushing its children, and should commit after seeding. Requests are stored as
    pushed; the file is locked while open (one crawl per file) and, like any pickle, is trusted input.

    Args:
        path: Database file (created with its parent directories if missing).
        spider: Spider whose methods are the requests' callbacks (for serialization).
        dedupe: Drop requests whose fingerprint was seen before.
        buffer_per_domain: Requests per domain kept in memory; refilled from disk below half.
        bloom_capacity: Initial capacity of the fingerprint filter (it grows as needed).
        bloom_error_rate: Overall false-positive rate of the filter (a false positive drops a new URL).
        commit_every: Commit after this many push/pop/ack operations...
        commit_interval: ...or this many seconds, whichever comes first.
    """

    persistent = True

    def __init__(
        self,
        path: str | os.PathLike[str],
        spider: Spider | None,
        *,
        dedupe: bool = True,
        buffer_per_domain: int = 64,
        bloom_capacity: int = 1_000_000,
        bloom_error_rate: float = 1e-4,
        commit_every: int = 500,
        commit_interval: float = 1.0,
    ) -> None:
        if buffer_per_domain < 1:
            raise ValueError("buffer_per_domain must be at least 1")
        self.path = os.fspath(path)
        self.spider = spider
        self.dedupe = dedupe
        self.buffer_per_domain = buffer_per_domain
        self.commit_every = max(1, commit_every)
        self.commit_interval = commit_interval
        self.duplicates = 0
        self.retry_count = 0  # queued requests that are retries
        self._low_water = max(1, buffer_per_domain // 2)
        self._buffers: dict[str, list[_Entry]] = {}  # sorted; non-empty for every domain with queued requests
        self._disk: dict[str, int] = {}  # queued rows per domain that are not buffered
        self._disk_retries: dict[str, int] = {}  # ...of which are retries
        self._size = 0
        self._leases: dict[int, tuple[Request, list[int]]] = {}  # id(request) -> (request, row ids)
        self._leased = 0
        self._next_id = 1
        # Pending writes, coalesced until the next flush.
        self._inserts: dict[int, list[Any]] = {}
        self._states: dict[int, int] = {}
        self._deletes: list[int] = []
        self._new_fps: list[bytes] = []
        self._journal_rows = 0
        self._bloom_saved = 0  # filters before this index are on disk and frozen
        self._ops = 0
        self._last_commit = time.monotonic()
        self._closed = False
        self._db = self._connect()
        try:
            self._seen = self._open(bloom_capacity, bloom_error_rate)
        except BaseException:
            self._db.close()
            raise

    # -- setup ------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, isolation_level=None, timeout=1.0)
        try:
            db.execute("PRAGMA locking_mode = EXCLUSIVE")
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA synchronous = NORMAL")
            db.execute("PRAGMA cache_size = -16384")
            db.execute("BEGIN IMMEDIATE")  # takes the (exclusive, held until close) lock now
        except sqlite3.OperationalError as exc:
            db.close()
            if "locked" in str(exc):
                raise CheckpointError(f"{self.path} is in use by another crawl") from exc
            raise
        return db

    def _open(self, bloom_capacity: int, bloom_error_rate: float) -> ScalableBloomFilter:
        """Create or load the database; requeue buffered/leased rows and rebuild the in-memory index."""
        db = self._db
        for statement in _SCHEMA:
            db.execute(statement)
        meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
        if "schema" not in meta:
            db.execute("INSERT INTO meta (key, value) VALUES ('schema', ?)", (_SCHEMA_VERSION,))
        elif int(meta["schema"]) != _SCHEMA_VERSION:
            raise CheckpointError(f"{self.path} was written by an incompatible version of wintergrab")

        if "bloom" in meta:
            filters = [BloomFilter.from_bytes(blob) for (blob,) in db.execute("SELECT data FROM bloom ORDER BY idx")]
            seen = ScalableBloomFilter._from_parts(filters, **json.loads(meta["bloom"]))
            self._bloom_saved = max(0, len(filters) - 1)
        else:
            seen = ScalableBloomFilter(bloom_capacity, bloom_error_rate)
            db.execute("INSERT INTO meta (key, value) VALUES ('bloom', ?)", (json.dumps(seen.params),))
        for (fp,) in db.execute("SELECT fp FROM seen_log ORDER BY rowid"):
            seen.add(fp)  # replays exactly the adds made after the last snapshot
            self._journal_rows += 1

        groups = db.execute("SELECT domain, state, COUNT(*) FROM requests GROUP BY domain, state").fetchall()
        for domain, state, count in groups:
            self._disk[domain] = self._disk.get(domain, 0) + count
            if state != _QUEUED:
                db.execute("UPDATE requests SET state = 0 WHERE domain = ? AND state = ?", (domain, state))
        self._disk_retries = dict(
            db.execute("SELECT domain, COUNT(*) FROM requests WHERE retries > 0 GROUP BY domain").fetchall()
        )
        self._size = sum(self._disk.values())
        self.retry_count = sum(self._disk_retries.values())
        self._next_id = (db.execute("SELECT MAX(id) FROM requests").fetchone()[0] or 0) + 1
        db.execute("COMMIT")
        for domain in list(self._disk):
            buf = self._buffers[domain] = []
            self._refill(domain, buf)
            if not buf:
                del self._buffers[domain]
        return seen

    # -- Scheduler interface ---------------------------------------------- #
    def __len__(self) -> int:
        """Queued requests (leased ones are not counted)."""
        return self._size

    @property
    def seen(self) -> ScalableBloomFilter:
        """Fingerprints of every request pushed with dedupe (``fp in scheduler.seen``)."""
        return self._seen

    def restore_seen(self, fingerprints: Iterable[bytes]) -> None:
        """Mark fingerprints as seen (a no-op when given :attr:`seen` itself)."""
        if fingerprints is self._seen:
            return
        self._check_open()
        for fp in fingerprints:
            if self._seen.add(fp):
                self._new_fps.append(fp)
                self._note_op()

    def push(self, request: Request, *, force: bool = False) -> bool:
        """Queue a request. Returns ``False`` if it was a duplicate."""
        self._check_open()
        fp, hashes = b"", None
        if self.dedupe and not force and not request.dont_filter:
            fp = request.fingerprint()
            hashes = self._seen._absent(fp)
            if hashes is None:
                self.duplicates += 1
                return False
        blob = pickle.dumps(request.to_dict(self.spider), protocol=pickle.HIGHEST_PROTOCOL)  # may raise: no changes yet
        if hashes is not None and self._seen._add_absent(hashes):
            self._new_fps.append(fp)
        domain = request.host
        row_id = self._next_id
        self._next_id += 1
        retries = request.retries
        entry: _Entry = (-request.priority, row_id, request)
        buf = self._buffers.get(domain)
        if buf is None:
            buf = self._buffers[domain] = []
        # Buffered entries always sort before every row left on disk, so a new request goes to memory only if
        # it beats the buffer's worst entry (or the whole domain fits in the buffer).
        if (len(buf) < self.buffer_per_domain and domain not in self._disk) or (buf and entry < buf[-1]):
            bisect.insort(buf, entry)
            state = _BUFFERED
            if len(buf) > self.buffer_per_domain:
                self._evict(domain, buf.pop())
        else:
            state = _QUEUED
            self._disk[domain] = self._disk.get(domain, 0) + 1
            if retries:
                self._disk_retries[domain] = self._disk_retries.get(domain, 0) + 1
        self._inserts[row_id] = [row_id, domain, request.priority, row_id, retries, state, blob]
        self._size += 1
        if retries:
            self.retry_count += 1
        if not buf:  # only if the in-memory index was out of step with the file
            self._refill(domain, buf)
            if not buf:
                del self._buffers[domain]
        self._note_op()
        return True

    def pop_ready(
        self, throttle: AutoThrottle, now: float, *, retries_only: bool = False
    ) -> tuple[Request | None, float | None]:
        """Next request that may start now, or ``(None, seconds_until_one_might)``.

        The popped request is leased: it stays in the file until :meth:`ack`. With ``retries_only`` only
        requests that are retries are considered.
        """
        self._check_open()
        if retries_only:
            return self._pop_retry(throttle, now)
        best: _Entry | None = None
        best_domain = ""
        wait: float | None = None
        for domain, buf in self._buffers.items():
            slot = throttle.slot(domain)
            if slot.active >= slot.concurrency:
                continue
            ready = slot.ready_at()
            if now < ready:
                wait = ready - now if wait is None else min(wait, ready - now)
                continue
            head = buf[0]
            if best is None or head < best:  # row ids are unique, so Requests are never compared
                best, best_domain = head, domain
        if best is None:
            return None, wait
        del self._buffers[best_domain][0]
        return self._lease(best_domain, best, buffered=True), None

    def _pop_retry(self, throttle: AutoThrottle, now: float) -> tuple[Request | None, float | None]:
        if not self.retry_count:
            return None, None
        best: _Entry | None = None
        best_domain = ""
        best_buffered = True
        wait: float | None = None
        for domain, buf in self._buffers.items():
            head = next((e for e in buf if e[2].retries), None)
            if head is None and not self._disk_retries.get(domain):
                continue
            slot = throttle.slot(domain)
            if slot.active >= slot.concurrency:
                continue
            ready = slot.ready_at()
            if now < ready:
                wait = ready - now if wait is None else min(wait, ready - now)
                continue
            buffered = head is not None
            if head is None:  # a buffered retry always beats one on disk, so only look there if needed
                head = self._disk_retry_head(domain)
                if head is None:
                    continue
            if best is None or head < best:
                best, best_domain, best_buffered = head, domain, buffered
        if best is None:
            return None, wait
        if best_buffered:
            buf = self._buffers[best_domain]
            del buf[next(i for i, e in enumerate(buf) if e is best)]
        else:
            self._dec(self._disk, best_domain)
            self._dec(self._disk_retries, best_domain)
        return self._lease(best_domain, best, buffered=best_buffered), None

    def pending(self) -> list[Request]:
        """Every queued request (highest priority first) without removing them. Reads the whole queue."""
        self._check_open()
        self._flush()
        buffered = {e[1]: e[2] for buf in self._buffers.values() for e in buf}
        rows = self._db.execute("SELECT id, data FROM requests WHERE state != 2 ORDER BY priority DESC, seq")
        return [buffered.get(row_id) or self._decode(blob) for row_id, blob in rows]

    def clear(self) -> None:
        """Drop every queued request (leased ones stay until acked; ``seen`` is kept)."""
        self._check_open()
        self._flush()
        self._begin()
        self._db.execute("DELETE FROM requests WHERE state != 2")
        self._buffers.clear()
        self._disk.clear()
        self._disk_retries.clear()
        self._size = 0
        self.retry_count = 0
        self._note_op()

    # -- persistence API -------------------------------------------------- #
    def ack(self, request: Request) -> bool:
        """The engine is done with a popped request (processed, given up or re-queued): forget it for good.

        Returns ``False`` if ``request`` is not currently leased from this scheduler.
        """
        lease = self._leases.get(id(request))
        if lease is None or lease[0] is not request:
            return False
        row_id = lease[1].pop()
        if not lease[1]:
            del self._leases[id(request)]
        self._leased -= 1
        if self._inserts.pop(row_id, None) is None:  # never written: nothing to delete
            self._states.pop(row_id, None)
            self._deletes.append(row_id)
        self._note_op()
        return True

    def leased(self) -> int:
        """Requests popped but not yet acked."""
        return self._leased

    def commit(self) -> None:
        """Write pending changes and new fingerprints to disk in one transaction."""
        self._check_open()
        self._flush()
        self._save_seen(snapshot=False)
        if self._db.in_transaction:
            self._db.execute("COMMIT")
        self._ops = 0
        self._last_commit = time.monotonic()

    def close(self) -> None:
        """Commit (with a full filter snapshot, so the next open needs no replay) and close the database."""
        if self._closed:
            return
        try:
            self._flush()
            self._save_seen(snapshot=True)
            if self._db.in_transaction:
                self._db.execute("COMMIT")
        finally:
            self._db.close()
            self._closed = True
            self._buffers.clear()
            self._disk.clear()
            self._disk_retries.clear()
            self._leases.clear()
            self._size = self._leased = self.retry_count = 0

    def __enter__(self) -> DiskScheduler:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    # -- internals -------------------------------------------------------- #
    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError(f"DiskScheduler for {self.path} is closed")

    def _decode(self, blob: bytes) -> Request:
        return Request.from_dict(pickle.loads(blob), self.spider)

    @staticmethod
    def _dec(counts: dict[str, int], domain: str, n: int = 1) -> None:
        left = counts.get(domain, 0) - n
        if left > 0:
            counts[domain] = left
        else:
            counts.pop(domain, None)

    def _lease(self, domain: str, entry: _Entry, *, buffered: bool) -> Request:
        row_id, request = entry[1], entry[2]
        self._size -= 1
        if request.retries:
            self.retry_count -= 1
        self._set_state(row_id, _LEASED)
        lease = self._leases.get(id(request))
        if lease is None:
            self._leases[id(request)] = (request, [row_id])
        else:
            lease[1].append(row_id)
        self._leased += 1
        if buffered:
            buf = self._buffers[domain]
            try:
                if len(buf) < self._low_water and domain in self._disk:
                    self._refill(domain, buf)
            finally:
                if not buf:
                    del self._buffers[domain]
        self._note_op()
        return request

    def _evict(self, domain: str, entry: _Entry) -> None:
        """Move a buffer's worst entry back to disk (a higher-priority request took its place)."""
        self._set_state(entry[1], _QUEUED)
        self._disk[domain] = self._disk.get(domain, 0) + 1
        if entry[2].retries:
            self._disk_retries[domain] = self._disk_retries.get(domain, 0) + 1

    def _refill(self, domain: str, buf: list[_Entry]) -> None:
        """Top up a domain's buffer with its best rows from disk."""
        want = self.buffer_per_domain - len(buf)
        if want <= 0:
            return
        self._flush()
        rows = self._db.execute(_NEXT_ROWS, (domain, want)).fetchall()
        entries = [(-priority, row_id, self._decode(blob)) for row_id, priority, _, blob in rows]
        for entry in entries:
            bisect.insort(buf, entry)
            self._set_state(entry[1], _BUFFERED)
        if len(rows) < want:  # the disk part of this domain is exhausted
            self._disk.pop(domain, None)
            self._disk_retries.pop(domain, None)
        else:
            self._dec(self._disk, domain, len(rows))
            self._dec(self._disk_retries, domain, sum(1 for row in rows if row[2] > 0))

    def _disk_retry_head(self, domain: str) -> _Entry | None:
        self._flush()
        row = self._db.execute(_NEXT_RETRY, (domain,)).fetchone()
        if row is None:
            self._disk_retries.pop(domain, None)
            return None
        return (-row[1], row[0], self._decode(row[2]))

    def _set_state(self, row_id: int, state: int) -> None:
        row = self._inserts.get(row_id)
        if row is not None:
            row[5] = state
        else:
            self._states[row_id] = state

    def _begin(self) -> None:
        if not self._db.in_transaction:
            self._db.execute("BEGIN")

    def _flush(self) -> None:
        """Execute pending writes inside the open transaction (committed by :meth:`commit`)."""
        if not (self._inserts or self._states or self._deletes):
            return
        self._begin()
        db = self._db
        if self._inserts:
            db.executemany(_INSERT, self._inserts.values())
            self._inserts.clear()
        if self._states:
            db.executemany(_SET_STATE, [(state, row_id) for row_id, state in self._states.items()])
            self._states.clear()
        if self._deletes:
            db.executemany(_DELETE, [(row_id,) for row_id in self._deletes])
            self._deletes.clear()

    def _save_seen(self, *, snapshot: bool) -> None:
        """Journal new fingerprints, or rewrite the non-frozen filters once the journal gets large."""
        new = self._new_fps
        if not new and not (snapshot and self._journal_rows):
            return
        self._begin()
        filters = self._seen.filters
        if snapshot or (self._journal_rows + len(new)) * _JOURNAL_ROW_BYTES * 4 >= filters[-1].nbytes:
            self._db.executemany(
                "INSERT OR REPLACE INTO bloom (idx, data) VALUES (?, ?)",
                ((i, filters[i].to_bytes()) for i in range(self._bloom_saved, len(filters))),
            )
            self._db.execute("DELETE FROM seen_log")
            self._journal_rows = 0
            self._bloom_saved = len(filters) - 1
        else:
            self._db.executemany("INSERT INTO seen_log (fp) VALUES (?)", [(fp,) for fp in new])
            self._journal_rows += len(new)
        new.clear()

    def _note_op(self) -> None:
        self._ops += 1
        if self._ops >= self.commit_every or time.monotonic() - self._last_commit >= self.commit_interval:
            self.commit()
