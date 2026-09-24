from __future__ import annotations

import gc
import hashlib
import random
import subprocess
import sys
import textwrap
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path

import pytest

from wintergrab import AutoThrottle, Request
from wintergrab.errors import CheckpointError
from wintergrab.spider.frontier import BloomFilter, DiskScheduler, ScalableBloomFilter
from wintergrab.spider.scheduler import Scheduler


class FakeSpider:
    name = "fake"

    def parse(self, response: object) -> None:
        pass

    def parse_item(self, response: object) -> None:
        pass


class FakeThrottle(AutoThrottle):
    """A throttle whose per-domain readiness the test scrambles directly."""

    def __init__(self) -> None:
        super().__init__(randomize=False)

    def scramble(self, rng: random.Random, now: float) -> None:
        for slot in self.slots.values():
            slot.concurrency = rng.choice((1, 2))
            slot.active = rng.choice((0, 0, 0, 1, 2))
            slot.next_start = now + rng.choice((0.0, 0.0, 0.0, -1.0, 0.25, 1.5))
            slot.paused_until = now + 3.0 if rng.random() < 0.05 else 0.0


def keys(n: int, salt: str) -> list[bytes]:
    return [hashlib.sha1(f"{salt}{i}".encode()).digest() for i in range(n)]


def req(url: str, *, priority: int = 0, retries: int = 0, **kw: object) -> Request:
    meta = {"retry_times": retries} if retries else {}
    return Request(url, priority=priority, meta=meta, **kw)  # type: ignore[arg-type]


def drain(sched: Scheduler | DiskScheduler, *, ack: bool = True) -> list[str]:
    throttle = AutoThrottle(randomize=False)
    out: list[str] = []
    while True:
        request, _ = sched.pop_ready(throttle, time.monotonic())
        if request is None:
            return out
        out.append(request.url)
        if ack and isinstance(sched, DiskScheduler):
            sched.ack(request)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "crawl" / "frontier.sqlite3"


# --------------------------------------------------------------------------- #
# Bloom filters
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("error_rate", [1e-2, 1e-3])
def test_bloom_false_positive_rate_close_to_target(error_rate: float) -> None:
    n = 200_000
    bf = BloomFilter(n, error_rate)
    added = keys(n, "in")
    assert sum(bf.add(k) for k in added) >= n * (1 - error_rate)  # False only for false positives
    assert all(k in bf for k in added)  # no false negatives
    fp_rate = sum(k in bf for k in keys(n, "out")) / n
    print(f"bloom p={error_rate}: measured {fp_rate:.5f}, {bf.nbytes / n:.2f} bytes/key, k={bf.num_hashes}")
    assert 0.5 * error_rate < fp_rate < 1.5 * error_rate
    assert abs(len(bf) - n) <= n * error_rate * 2


def test_bloom_memory_per_key_and_serialization() -> None:
    bf = BloomFilter(100_000, 1e-4)
    assert 2.2 < bf.nbytes / 100_000 < 2.6  # ~19.2 bits per key
    added = keys(1000, "k")
    for k in added:
        bf.add(k)
    assert not bf.add(added[0])
    copy = BloomFilter.from_bytes(bf.to_bytes())
    assert (copy.capacity, copy.error_rate, copy.num_bits, copy.num_hashes, len(copy)) == (
        bf.capacity,
        bf.error_rate,
        bf.num_bits,
        bf.num_hashes,
        len(bf),
    )
    assert all(k in copy for k in added) and copy.to_bytes() == bf.to_bytes()
    assert "not bytes" not in bf and 42 not in bf
    with pytest.raises(ValueError):
        BloomFilter.from_bytes(bf.to_bytes()[:-1])
    with pytest.raises(ValueError):
        BloomFilter.from_bytes(b"nope" * 20)


def test_scalable_bloom_grows_and_keeps_error_rate() -> None:
    sbf = ScalableBloomFilter(initial_capacity=1000, error_rate=1e-3)
    assert len(sbf.filters) == 1
    added = keys(60_000, "in")
    new = sum(sbf.add(k) for k in added)
    assert len(sbf.filters) == 6  # 1k + 2k + 4k + 8k + 16k + 32k >= 60k
    assert [f.capacity for f in sbf.filters] == [1000, 2000, 4000, 8000, 16000, 32000]
    assert sbf.filters[1].error_rate < sbf.filters[0].error_rate
    assert all(k in sbf for k in added)
    assert new >= 60_000 * (1 - 1e-3) and len(sbf) == new
    assert not sbf.add(added[123])
    fp_rate = sum(k in sbf for k in keys(100_000, "out")) / 100_000
    print(f"scalable bloom: {len(sbf.filters)} filters, measured p={fp_rate:.5f}, {sbf.nbytes / len(sbf):.2f} B/key")
    assert fp_rate < 1e-3
    copy = ScalableBloomFilter.from_bytes(sbf.to_bytes())
    assert copy.params == sbf.params and len(copy) == len(sbf)
    assert all(k in copy for k in added[::97]) and copy.to_bytes() == sbf.to_bytes()
    with pytest.raises(ValueError):
        ScalableBloomFilter.from_bytes(sbf.to_bytes() + b"x")


def test_scalable_bloom_memory_vs_set() -> None:
    sbf = ScalableBloomFilter(initial_capacity=200_000, error_rate=1e-4)
    for k in keys(200_000, "k"):
        sbf.add(k)
    assert len(sbf.filters) == 1
    assert sbf.nbytes / 200_000 < 3.1  # ~24 bits/key (first filter runs at error_rate * (1 - tightening))


# --------------------------------------------------------------------------- #
# DiskScheduler vs Scheduler
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_matches_in_memory_scheduler(db: Path, seed: int) -> None:
    rng = random.Random(seed)
    spider = FakeSpider()
    mem = Scheduler()
    disk = DiskScheduler(db, spider, buffer_per_domain=rng.choice((1, 2, 3, 5)), commit_every=7)
    throttle = FakeThrottle()
    domains = [f"d{i}.test" for i in range(5)]
    urls: list[str] = []
    leased: list[Request] = []
    old_duplicates = 0
    now = 1000.0
    for step in range(2500):
        op = rng.random()
        if op < 0.5:
            if urls and rng.random() < 0.2:
                url = rng.choice(urls)  # a duplicate, unless dont_filter/force
            else:
                url = f"https://{rng.choice(domains)}/p{step}"
                urls.append(url)
            request = req(
                url,
                priority=rng.choice((-1, 0, 0, 1, 2)),
                retries=rng.choice((0, 0, 0, 1)),
                dont_filter=rng.random() < 0.05,
            )
            force = rng.random() < 0.05
            assert mem.push(request, force=force) == disk.push(request, force=force)
        elif op < 0.93:
            now += 0.1
            throttle.scramble(rng, now)
            retries_only = rng.random() < 0.2
            a, wait_a = mem.pop_ready(throttle, now, retries_only=retries_only)
            b, wait_b = disk.pop_ready(throttle, now, retries_only=retries_only)
            assert (a and (a.url, a.priority, a.retries)) == (b and (b.url, b.priority, b.retries)), step
            assert wait_a == wait_b
            if b is not None:
                leased.append(b)
        elif op < 0.99 and leased:
            assert disk.ack(leased.pop(rng.randrange(len(leased))))
        else:  # everything acked: reopening must not change anything
            for request in leased:
                assert disk.ack(request)
            leased.clear()
            old_duplicates += disk.duplicates
            disk.close()
            disk = DiskScheduler(db, spider, buffer_per_domain=disk.buffer_per_domain, commit_every=7)
        assert len(mem) == len(disk)
        assert mem.retry_count == disk.retry_count
        assert mem.duplicates == disk.duplicates + old_duplicates
        assert disk.leased() == len(leased)
    assert [r.url for r in mem.pending()] == [r.url for r in disk.pending()]
    assert drain(mem) == drain(disk)
    disk.close()


def test_priority_fifo_and_domain_readiness(db: Path) -> None:
    disk = DiskScheduler(db, FakeSpider(), buffer_per_domain=2)
    throttle = AutoThrottle(max_concurrency=1, randomize=False)
    assert disk.push(req("https://a.test/1"))
    assert disk.push(req("https://a.test/2", priority=5))
    assert disk.push(req("https://b.test/1"))
    for i in range(3, 8):
        disk.push(req(f"https://a.test/{i}"))
    now = time.monotonic()
    first, _ = disk.pop_ready(throttle, now)
    assert first is not None and first.url == "https://a.test/2"
    throttle.on_start(throttle.slot("a.test"), now)
    second, _ = disk.pop_ready(throttle, now)
    assert second is not None and second.url == "https://b.test/1"  # a.test is busy
    throttle.on_start(throttle.slot("b.test"), now)
    assert disk.pop_ready(throttle, now) == (None, None)
    throttle.on_finish(throttle.slot("a.test"))
    throttle.slot("a.test").next_start = now + 2.0
    request, wait = disk.pop_ready(throttle, now)
    assert request is None and wait == pytest.approx(2.0)
    assert drain(disk) == [f"https://a.test/{i}" for i in (1, 3, 4, 5, 6, 7)]  # FIFO among equals
    assert len(disk) == 0 and disk.leased() == 2
    disk.close()


def test_high_priority_push_evicts_buffered_request(db: Path) -> None:
    disk = DiskScheduler(db, FakeSpider(), buffer_per_domain=2)
    for i in range(4):
        disk.push(req(f"https://a.test/{i}"))
    disk.push(req("https://a.test/urgent", priority=9))
    disk.push(req("https://a.test/soon", priority=1))
    assert [r.url for r in disk.pending()] == [
        "https://a.test/urgent",
        "https://a.test/soon",
        *(f"https://a.test/{i}" for i in range(4)),
    ]
    assert drain(disk) == ["https://a.test/urgent", "https://a.test/soon", *(f"https://a.test/{i}" for i in range(4))]
    disk.close()


def test_dedupe_dont_filter_and_force(db: Path) -> None:
    disk = DiskScheduler(db, FakeSpider(), bloom_capacity=1000)
    assert disk.push(req("https://a.test/1"))
    assert not disk.push(req("https://a.test/1#fragment"))  # same page
    assert not disk.push(req("https://A.test:443/1"))
    assert disk.push(req("https://a.test/1", dont_filter=True))
    assert disk.push(req("https://a.test/1"), force=True)
    assert disk.duplicates == 2 and len(disk) == 3
    assert req("https://a.test/1").fingerprint() in disk.seen
    assert req("https://a.test/2").fingerprint() not in disk.seen
    disk.restore_seen([req("https://a.test/2").fingerprint()])
    disk.restore_seen(disk.seen)  # no-op
    assert not disk.push(req("https://a.test/2"))
    nodedupe = DiskScheduler(db.with_name("other.sqlite3"), FakeSpider(), dedupe=False)
    assert nodedupe.push(req("https://a.test/1")) and nodedupe.push(req("https://a.test/1"))
    for sched in (disk, nodedupe):
        sched.close()


def test_retries_only_uses_buffer_and_disk(db: Path) -> None:
    mem = Scheduler()
    disk = DiskScheduler(db, FakeSpider(), buffer_per_domain=2)
    requests = [req(f"https://a.test/{i}", priority=1) for i in range(6)]
    requests += [req("https://a.test/retry-low", priority=-1, retries=1)]  # behind the buffer: on disk
    requests += [req("https://b.test/retry", retries=2), req("https://b.test/new")]
    for request in requests:
        mem.push(request)
        disk.push(request)
    assert mem.retry_count == disk.retry_count == 2
    throttle = AutoThrottle(randomize=False)
    now = time.monotonic()
    for _ in range(3):
        a, wait_a = mem.pop_ready(throttle, now, retries_only=True)
        b, wait_b = disk.pop_ready(throttle, now, retries_only=True)
        assert (a and a.url) == (b and b.url) and wait_a == wait_b
    assert disk.retry_count == 0 and len(disk) == 7 and disk.leased() == 2
    assert disk.pop_ready(throttle, now, retries_only=True) == (None, None)
    assert drain(mem) == drain(disk)
    disk.close()


def test_pending_and_clear(db: Path) -> None:
    disk = DiskScheduler(db, FakeSpider(), buffer_per_domain=2)
    buffered = req("https://a.test/0", priority=3)
    disk.push(buffered)
    for i in range(1, 6):
        disk.push(req(f"https://{'ab'[i % 2]}.test/{i}", priority=i % 3, callback="parse_item"))
    pending = disk.pending()
    assert [r.url for r in pending] == [
        "https://a.test/0",
        "https://a.test/2",
        "https://b.test/5",
        "https://b.test/1",
        "https://a.test/4",
        "https://b.test/3",
    ]
    assert pending[0] is buffered  # buffered requests are returned as-is
    assert pending[1].callback == "parse_item"  # buffered: the pushed object
    assert pending[-1].callback == disk.spider.parse_item  # evicted to disk and rebuilt
    popped, _ = disk.pop_ready(AutoThrottle(), time.monotonic())
    disk.clear()
    assert len(disk) == 0 and disk.pending() == [] and disk.leased() == 1
    assert popped is not None and disk.ack(popped) and not disk.ack(popped)
    assert disk.push(req("https://c.test/1")) and not disk.push(req("https://a.test/2"))  # seen survives clear
    disk.close()
    with pytest.raises(RuntimeError):
        disk.push(req("https://c.test/2"))


# --------------------------------------------------------------------------- #
# persistence and crashes
# --------------------------------------------------------------------------- #
def test_reopen_returns_queued_and_leased_but_not_acked(db: Path) -> None:
    spider = FakeSpider()
    disk = DiskScheduler(db, spider, buffer_per_domain=3, bloom_capacity=1000)
    urls = [f"https://{'ab'[i % 2]}.test/{i}" for i in range(12)]
    for i, url in enumerate(urls):
        disk.push(req(url, retries=1 if i == 11 else 0, callback="parse_item"))
    throttle = AutoThrottle(randomize=False)
    popped = [disk.pop_ready(throttle, time.monotonic())[0] for _ in range(5)]
    assert all(popped) and disk.leased() == 5 and len(disk) == 7
    acked = {r.url for r in popped[:2] if r is not None and disk.ack(r)}
    assert len(acked) == 2
    disk.commit()
    disk.close()

    again = DiskScheduler(db, spider, buffer_per_domain=3)
    assert len(again) == 10 and again.leased() == 0 and again.retry_count == 1
    remaining = [u for u in urls if u not in acked]
    assert sorted(r.url for r in again.pending()) == sorted(remaining)
    assert all(r.callback == spider.parse_item for r in again.pending())
    assert not again.push(req(next(iter(acked))))  # fingerprints were persisted
    assert again.push(req("https://a.test/new"))
    assert sorted(drain(again)) == sorted([*remaining, "https://a.test/new"])
    again.close()
    with DiskScheduler(db, spider) as final:
        assert len(final) == 0 and final.pending() == []  # everything was acked


def test_crash_after_commit_keeps_committed_state(db: Path) -> None:
    spider = FakeSpider()
    disk = DiskScheduler(db, spider, buffer_per_domain=2, commit_every=10**9, commit_interval=1e9)
    for i in range(10):
        disk.push(req(f"https://a.test/{i}"))
    throttle = AutoThrottle(randomize=False)
    first = [disk.pop_ready(throttle, time.monotonic())[0] for _ in range(4)]
    assert first[0] is not None and disk.ack(first[0])
    disk.commit()
    # Work after the commit is lost in the crash: pushes, pops and acks.
    for i in range(10, 15):
        disk.push(req(f"https://a.test/{i}"))
    for _ in range(3):
        request, _ = disk.pop_ready(throttle, time.monotonic())
        assert request is not None and disk.ack(request)
    for request in first[1:]:
        assert request is not None and disk.ack(request)
    del disk, request, first  # crash: the connection goes away without close() or commit()
    gc.collect()

    again = DiskScheduler(db, spider, buffer_per_domain=2)
    assert len(again) == 9 and again.leased() == 0
    assert [r.url for r in again.pending()] == [f"https://a.test/{i}" for i in range(1, 10)]
    assert not again.push(req("https://a.test/5"))  # committed fingerprint
    assert again.push(req("https://a.test/12"))  # never committed: accepted again
    again.close()


def test_seen_survives_snapshots_journal_and_growth(db: Path) -> None:
    disk = DiskScheduler(db, FakeSpider(), bloom_capacity=500, commit_every=37)
    urls = [f"https://a.test/{i}" for i in range(3000)]
    for url in urls:
        assert disk.push(req(url))
    disk.commit()
    assert len(disk.seen.filters) == 3  # grew twice; snapshots and journal entries interleaved
    expected = disk.seen.to_bytes()
    del disk
    gc.collect()  # crash
    again = DiskScheduler(db, FakeSpider())
    assert again.seen.to_bytes() == expected  # rebuilt exactly: last snapshot + replayed journal
    assert not any(again.push(req(url)) for url in urls[::7])
    again.close()
    with DiskScheduler(db, FakeSpider()) as final:  # after close(): a full snapshot, nothing to replay
        assert final.seen.to_bytes() == expected


def test_hard_crash_in_subprocess(db: Path) -> None:
    script = textwrap.dedent(
        f"""
        import os, time
        from wintergrab import AutoThrottle, Request
        from wintergrab.spider.frontier import DiskScheduler

        class Spider:
            def parse(self, response):
                pass

        disk = DiskScheduler({str(db)!r}, Spider(), buffer_per_domain=4, bloom_capacity=5000,
                             commit_every=10**9, commit_interval=1e9)
        for i in range(300):
            disk.push(Request(f"https://d{{i % 3}}.test/{{i}}"))
        throttle = AutoThrottle(randomize=False)
        popped = []
        for _ in range(60):
            popped.append(disk.pop_ready(throttle, time.monotonic())[0])
        for request in popped[:40]:
            disk.ack(request)
        disk.commit()
        for i in range(300, 330):
            disk.push(Request(f"https://d0.test/{{i}}"))
        for request in popped[40:]:
            disk.ack(request)
        disk._flush()  # uncommitted writes in the WAL when the process dies
        os._exit(3)
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 3, result.stderr
    again = DiskScheduler(db, FakeSpider(), buffer_per_domain=4)
    assert len(again) == 260 and again.leased() == 0
    assert not again.push(Request("https://d1.test/1"))
    assert again.push(Request("https://d0.test/300"))  # its push was never committed
    assert len(set(drain(again))) == 261
    again.close()


def test_one_scheduler_per_file(db: Path) -> None:
    disk = DiskScheduler(db, FakeSpider())
    with pytest.raises(CheckpointError, match="in use"):
        DiskScheduler(db, FakeSpider())
    disk.close()
    with DiskScheduler(db, FakeSpider()) as again:
        assert len(again) == 0


# --------------------------------------------------------------------------- #
# volume
# --------------------------------------------------------------------------- #
def _volume_run(sched: Scheduler | DiskScheduler, n: int, *, pop: bool = True) -> tuple[float, float]:
    start = time.perf_counter()
    for i in range(n):
        sched.push(Request(f"https://site{i % 20}.test/item/{i}?page={i // 20}", meta={"depth": 1}))
    pushed = time.perf_counter()
    assert len(sched) == n
    if not pop:
        return pushed - start, 0.0
    throttle = AutoThrottle(randomize=False)
    now = time.monotonic()
    count = 0
    while True:
        request, _ = sched.pop_ready(throttle, now)
        if request is None:
            break
        count += 1
        if isinstance(sched, DiskScheduler):
            sched.ack(request)
    assert count == n and len(sched) == 0
    return pushed - start, time.perf_counter() - pushed


def _traced_peak(run: Callable[[], object]) -> int:
    gc.collect()
    tracemalloc.start()
    try:
        run()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_volume_throughput_and_flat_memory(db: Path) -> None:
    n = 200_000  # 20 domains
    disk = DiskScheduler(db, FakeSpider())
    push_s, pop_s = _volume_run(disk, n)
    disk.close()
    print(f"\nDiskScheduler, {n:,} requests: push {n / push_s:,.0f}/s, pop+ack {n / pop_s:,.0f}/s")
    assert push_s + pop_s < 120

    # tracemalloc makes these runs ~4x slower, so compare memory on a smaller load. The in-memory Scheduler peaks
    # once everything is pushed; the DiskScheduler run includes the pops (refills) and its 1M-key Bloom filter.
    n = 50_000

    def disk_run() -> None:
        with DiskScheduler(db.with_name("traced.sqlite3"), FakeSpider()) as sched:
            _volume_run(sched, n)

    disk_peak = _traced_peak(disk_run)
    memory_peak = _traced_peak(lambda: _volume_run(Scheduler(), n, pop=False))
    print(f"tracemalloc peak, {n:,} requests: DiskScheduler {disk_peak / 1e6:.1f} MB, ", end="")
    print(f"Scheduler {memory_peak / 1e6:.1f} MB")
    assert disk_peak < 16e6
    assert disk_peak * 4 < memory_peak
