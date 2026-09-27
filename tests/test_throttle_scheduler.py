from __future__ import annotations

import time

from wintergrab import AutoThrottle, Request
from wintergrab.spider.scheduler import Scheduler
from wintergrab.utils import canonicalize_url, parse_retry_after


def test_pushback_halves_concurrency_and_grows_delay() -> None:
    throttle = AutoThrottle(max_concurrency=8, min_backoff_delay=1.0, randomize=False)
    slot = throttle.slot("a.test")
    assert slot.concurrency == 8 and slot.delay == 0.0
    throttle.on_pushback("a.test")
    assert slot.concurrency == 4 and slot.delay == 1.0
    throttle.on_pushback("a.test")
    assert slot.concurrency == 2 and slot.delay == 2.0
    for _ in range(3):
        throttle.on_pushback("a.test")
    assert slot.concurrency == 1 and slot.delay == 16.0


def test_retry_after_pauses_the_domain() -> None:
    throttle = AutoThrottle(randomize=False)
    throttle.on_pushback("a.test", retry_after=5)
    slot = throttle.slot("a.test")
    assert slot.delay == 5.0
    assert not throttle.can_start(slot, time.monotonic())
    assert throttle.can_start(slot, time.monotonic() + 5.1)


def test_recovery_after_success() -> None:
    throttle = AutoThrottle(max_concurrency=4, increase_every=2, randomize=False)
    throttle.on_pushback("a.test")
    slot = throttle.slot("a.test")
    assert (slot.concurrency, slot.delay) == (2, 1.0)
    for _ in range(4):
        throttle.on_success("a.test", latency=0.1)
    assert slot.concurrency == 4
    assert slot.delay < 1.0
    for _ in range(50):
        throttle.on_success("a.test", latency=0.1)
    assert slot.delay == 0.1 / 4  # latency / target concurrency


def test_disabled_throttle_keeps_fixed_speed() -> None:
    throttle = AutoThrottle(enabled=False, base_delay=0.5, max_concurrency=3)
    throttle.on_pushback("a.test")
    throttle.on_success("a.test", 5.0)
    slot = throttle.slot("a.test")
    assert (slot.concurrency, slot.delay) == (3, 0.5)


def test_crawl_delay_floor_and_snapshot() -> None:
    throttle = AutoThrottle()
    throttle.set_min_delay("a.test", 2.0)
    throttle.on_success("a.test", 0.01)
    assert throttle.slot("a.test").delay == 2.0
    other = AutoThrottle()
    other.restore(throttle.snapshot())
    assert other.slot("a.test").delay == 2.0


def test_rate_limit_headers_pause_or_pace_the_domain() -> None:
    from wintergrab.utils import parse_rate_limit

    assert parse_rate_limit({"ratelimit": "limit=100, remaining=5, reset=30"}) == (100, 5, 30.0)
    assert parse_rate_limit({"ratelimit-remaining": "0", "ratelimit-reset": "12"}) == (None, 0, 12.0)
    assert parse_rate_limit({"x-ratelimit-limit": "60", "x-ratelimit-remaining": "7", "x-ratelimit-reset": "9"}) == (
        60,
        7,
        9.0,
    )
    limit, remaining, reset = parse_rate_limit(
        {"x-ratelimit-remaining": "3", "x-ratelimit-reset": str(int(time.time()) + 20)}
    )
    assert (limit, remaining) == (None, 3) and reset is not None and 18 <= reset <= 20  # (a Unix time)
    assert parse_rate_limit({"x-ratelimit-reset": "99999"}, cap=600) == (None, None, 600.0)
    assert parse_rate_limit({"content-type": "text/html"}) == (None, None, None)
    assert parse_rate_limit({"ratelimit-remaining": "lots"}) == (None, None, None)

    throttle = AutoThrottle(randomize=False)
    assert throttle.on_rate_limit("a.test", 0, 5.0)  # used up: paused until the window resets
    slot = throttle.slot("a.test")
    assert not throttle.can_start(slot, time.monotonic()) and throttle.can_start(slot, time.monotonic() + 5.1)
    assert throttle.state("a.test")["rate_limit"] == {"remaining": 0, "reset": 5.0, "pauses": 1}
    assert not throttle.on_rate_limit("b.test", 4, 8.0)  # four left for eight seconds: one every two
    assert throttle.slot("b.test").delay == 2.0
    assert not throttle.on_rate_limit("b.test", 40, 8.0) and throttle.slot("b.test").delay == 2.0  # (never faster)
    fixed = AutoThrottle(enabled=False)
    assert fixed.on_rate_limit("c.test", 0, 3.0) and fixed.slot("c.test").delay == 0.0  # the site's word holds


def test_errors_that_keep_coming_halve_concurrency() -> None:
    throttle = AutoThrottle(max_concurrency=8, randomize=False)
    slot = throttle.slot("a.test")
    for _ in range(4):
        throttle.on_error("a.test")
    assert slot.concurrency == 8 and throttle.state("a.test")["error_rate"] == 1.0  # too few seen to judge
    for _ in range(5):
        throttle.on_success("a.test", 0.1)
    throttle.on_error("a.test")  # 5 of 10 failed
    assert slot.concurrency == 4 and throttle.error_rate(slot) is None  # (judged again on the next ten)
    for _ in range(9):
        throttle.on_success("a.test", 0.1)
    throttle.on_error("a.test")  # one of ten: no
    assert slot.concurrency == 4


def test_scheduler_priority_dedupe_and_domains() -> None:
    sched = Scheduler()
    throttle = AutoThrottle(max_concurrency=1, randomize=False)
    assert sched.push(Request("https://a.test/1"))
    assert not sched.push(Request("https://a.test/1#frag"))  # same page
    assert sched.push(Request("https://a.test/1", dont_filter=True))
    sched.push(Request("https://a.test/2", priority=5))
    sched.push(Request("https://b.test/1"))
    assert len(sched) == 4 and sched.duplicates == 1

    now = time.monotonic()
    first, _ = sched.pop_ready(throttle, now)
    assert first.url == "https://a.test/2"  # highest priority
    throttle.on_start(throttle.slot("a.test"), now)
    second, _ = sched.pop_ready(throttle, now)
    assert second.url == "https://b.test/1"  # a.test is busy (concurrency 1)
    throttle.on_start(throttle.slot("b.test"), now)
    nothing, wait = sched.pop_ready(throttle, now)
    assert nothing is None and wait is None  # both slots busy
    throttle.on_finish(throttle.slot("a.test"))
    third, _ = sched.pop_ready(throttle, now)
    assert third.url == "https://a.test/1"
    assert [r.url for r in sched.pending()] == ["https://a.test/1"]


def test_scheduler_reports_wait_for_delayed_domains() -> None:
    sched = Scheduler()
    throttle = AutoThrottle(base_delay=1.0, randomize=False)
    sched.push(Request("https://a.test/1"))
    sched.push(Request("https://a.test/2"))
    now = time.monotonic()
    req, _ = sched.pop_ready(throttle, now)
    throttle.on_start(throttle.slot("a.test"), now)
    req, wait = sched.pop_ready(throttle, now)
    # (now + 1.0) - now is not exactly 1.0 when the monotonic clock reads high
    assert req is None and abs(wait - 1.0) < 1e-6


def test_url_helpers() -> None:
    assert canonicalize_url("HTTP://Example.COM:80/a b?z=1&a=2#x") == "http://example.com/a%20b?a=2&z=1"
    assert canonicalize_url("https://x.test") == "https://x.test/"
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after("garbage") is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0  # in the past
