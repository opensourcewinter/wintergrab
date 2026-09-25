"""Events, metrics, throttle state, failure diagnoses and the dead-letter queue."""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from wintergrab import AutoThrottle, Spider
from wintergrab.events import Event, EventBus, EventRecorder, JsonlEventSink
from wintergrab.spider.deadletters import DeadLetterQueue
from wintergrab.spider.failures import FailureTracker
from wintergrab.spider.metrics import CrawlMetrics, current_rss, to_prometheus


class Base(Spider):
    log_level = None
    obey_robots_txt = False
    autothrottle = False


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #
def test_event_bus_basics() -> None:
    bus = EventBus(origin="test")
    bus.emit("nobody_listens", x=1)  # no handlers: a no-op
    got: list[Event] = []
    unsubscribe = bus.subscribe(got.append, "a")
    everything = EventRecorder()
    bus.subscribe(everything)
    assert bus.wants("a") and bus.wants("anything") and bool(bus)
    bus.emit("a", kind="field named kind", source="and one named source")
    bus.emit("b", n=2)
    assert [e.kind for e in got] == ["a"] and everything.kinds() == ["a", "b"]
    event = got[0]
    assert event["kind"] == "field named kind" and event.get("source") == "and one named source"
    assert event.origin == "test"
    as_dict = event.to_dict()
    assert as_dict["event"] == "a" and as_dict["origin"] == "test" and as_dict["time"].endswith("+00:00")
    unsubscribe()
    bus.emit("a")
    assert len(got) == 1


def test_failing_and_async_handlers() -> None:
    bus = EventBus()

    def broken(event: Event) -> None:
        raise RuntimeError("handler bug")

    seen: list[str] = []

    async def async_handler(event: Event) -> None:
        await asyncio.sleep(0)
        seen.append(event.kind)

    bus.subscribe(broken)
    bus.subscribe(async_handler)

    async def main() -> None:
        bus.emit("x")
        await bus.drain()

    asyncio.run(main())
    assert bus.errors == 1 and seen == ["x"]
    bus.emit("outside_a_loop")  # coroutine handlers still run
    assert seen == ["x", "outside_a_loop"]


def test_crawl_emits_events(fresh_site, tmp_path) -> None:
    class Crawl(Base):
        retries = 1
        crawl_dir = str(tmp_path / "crawl")
        event_log = True
        start_urls = [fresh_site.url + "/product/1", fresh_site.url + "/status/500", fresh_site.url + "/blocked"]

        def parse(self, response):
            yield {"url": response.url}

        def on_error(self, request, error):
            pass

    spider = Crawl()
    recorder = EventRecorder()
    spider.events.subscribe(recorder)
    spider.run()
    kinds = recorder.kinds()
    assert kinds[0] == "crawl_started" and kinds[-1] == "crawl_finished"
    for kind in ("response", "item_scraped", "request_retried", "request_failed", "blocked", "throttle_backoff"):
        assert kind in kinds, kind
    failed = {e["url"]: e for e in recorder.of("request_failed")}
    assert failed[fresh_site.url + "/status/500"]["category"] == "http"
    assert failed[fresh_site.url + "/status/500"]["status"] == 500
    assert recorder.of("crawl_finished")[0]["status"] == "finished"
    # event_log=True also wrote them to crawl_dir/events.jsonl, one JSON object per line
    lines = (tmp_path / "crawl" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    logged = [json.loads(line) for line in lines]
    assert logged[0]["event"] == "crawl_started" and logged[-1]["event"] == "crawl_finished"
    assert logged[0]["origin"] == "Crawl"
    assert "response" not in {e["event"] for e in logged} or len(logged) == len(recorder)


def test_high_volume_events_cost_nothing_unless_subscribed(site) -> None:
    class Crawl(Base):
        start_urls = [site.url + "/product/1"]

        def parse(self, response):
            yield {"url": response.url}

    spider = Crawl()
    recorder = EventRecorder()
    spider.events.subscribe(recorder, ["crawl_started", "crawl_finished"])
    spider.run()
    assert recorder.kinds() == ["crawl_started", "crawl_finished"]


def test_jsonl_sink_handles_odd_values(tmp_path) -> None:
    sink = JsonlEventSink(tmp_path / "events.jsonl", flush_every=1)
    sink(Event("x", {"error": ValueError("bad"), "tags": {"a"}, "obj": object()}))
    sink.close()
    sink(Event("after_close"))  # ignored, no crash
    record = json.loads((tmp_path / "events.jsonl").read_text(encoding="utf-8"))
    assert record["error"] == "ValueError: bad" and record["tags"] == ["a"]


# --------------------------------------------------------------------------- #
# metrics and throttle state
# --------------------------------------------------------------------------- #
def test_metrics_live_and_final(site) -> None:
    snapshots: list[dict] = []

    class Crawl(Base):
        start_urls = [site.url + f"/product/{i}" for i in range(1, 9)]
        concurrency = 2

        def parse(self, response):
            snapshots.append(self.metrics())
            yield {"url": response.url}

    result = Crawl().run()
    live = snapshots[-1]
    assert live["pages"] >= 1 and "rates" in live and "latency" in live
    final = result.metrics
    assert final["pages"] == 8 and final["items"] == 8 and final["success_rate"] == 1.0
    assert final["latency"]["p50"] is not None and final["latency"]["p99"] >= final["latency"]["p50"]
    domain = final["domains"][0]
    assert domain["domain"] == "127.0.0.1" and domain["mode"] == "normal" and domain["requests"] == 8
    assert final["process"]["cpu_seconds"] > 0
    assert Crawl().metrics() == {}  # not running


def test_metrics_rates() -> None:
    metrics = CrawlMetrics(window=10)
    throttle = AutoThrottle()
    slot = throttle.slot("a.test")
    metrics.sample({"pages": 0, "items": 0, "bytes": 0, "requests": 0}, throttle, now=100.0)
    slot.requests = 20
    metrics.sample({"pages": 20, "items": 10, "bytes": 2000, "requests": 22}, throttle, now=102.0)
    rates = metrics.rates()
    assert rates == {"pages_per_second": 10.0, "items_per_second": 5.0, "bytes_per_second": 1000.0,
                     "requests_per_second": 11.0}  # fmt: skip
    assert metrics.domain_rate("a.test") == 10.0
    for latency in (0.1, 0.2, 0.3, 0.4):
        metrics.observe_latency(latency)
    assert metrics.latency()["p50"] == 0.3 and metrics.latency()["mean"] == 0.25


def test_throttle_state_and_modes() -> None:
    throttle = AutoThrottle(max_concurrency=4, randomize=False)
    for _ in range(3):
        throttle.on_success("a.test", 0.4)
    state = throttle.state("a.test")
    assert state["mode"] == "normal" and state["avg_latency"] == 0.4
    assert state["target_delay"] == 0.1  # latency / target concurrency
    throttle.on_pushback("a.test")
    state = throttle.state("a.test")
    assert state["mode"] == "backing off" and state["concurrency"] == 2 and state["backoffs"] == 1
    assert throttle.describe("a.test") == "backing off (delay 1.0s, concurrency 2/4)"
    throttle.on_pushback("b.test", retry_after=30)
    assert throttle.state("b.test")["mode"] == "paused" and throttle.state("b.test")["paused_for"] > 25
    slot = throttle.slot("a.test")
    slot.last_pushback = time.monotonic() - 60  # long ago; still slower than the target
    assert throttle.mode(slot) == "recovering"


def test_prometheus_format(site) -> None:
    class Crawl(Base):
        start_urls = [site.url + "/product/1", site.url + "/status/404"]

        def parse(self, response):
            yield {}

    result = Crawl().run()
    text = to_prometheus(result.metrics, result.stats)
    assert "# TYPE wintergrab_pages_total counter" in text
    assert "wintergrab_pages_total 2" in text
    assert 'wintergrab_latency_seconds{quantile="0.50"}' in text
    assert 'wintergrab_domain_requests{domain="127.0.0.1"} 2' in text
    assert 'wintergrab_status_by_label_total{label="404"} 1' in text
    for line in text.splitlines():
        assert line.startswith("#") or line.startswith("wintergrab_"), line


def test_current_rss_is_plausible() -> None:
    rss = current_rss()
    if sys.platform.startswith("linux") or sys.platform in ("win32", "darwin"):
        assert rss is not None
    assert rss is None or 1_000_000 < rss < 100_000_000_000


# --------------------------------------------------------------------------- #
# failure intelligence
# --------------------------------------------------------------------------- #
def test_crawl_failures_are_diagnosed(fresh_site) -> None:
    class Troubled(Base):
        obey_robots_txt = True
        retries = 1
        start_urls = [
            fresh_site.url + "/product/1",
            fresh_site.url + "/status/404",
            fresh_site.url + "/ratelimited/a?limit=9&after=0",
            fresh_site.url + "/blocked",
            fresh_site.url + "/private/secret",
            fresh_site.url + "/product/2?crash=1",
            "http://127.0.0.1:9/refused",
        ]

        def parse(self, response):
            if "crash" in response.url:
                raise KeyError("price")
            yield {"url": response.url}

        def on_error(self, request, error):
            pass

    result = Troubled().run()
    by_signature = {(d.domain, d.signature): d for d in result.failures}
    host = "127.0.0.1"
    not_found = by_signature[(host, "HTTP 404")]
    assert not_found.confirmed_cause == "the page does not exist (HTTP 404)" and not_found.likely_cause is None
    limited = by_signature[(host, "HTTP 429")]
    assert "rate limiting" in (limited.confirmed_cause or "")
    assert limited.attempts == 2 and limited.failed_urls == 1 and limited.affected_urls == 1
    assert "Retry-After: 0" in limited.evidence
    blocked = by_signature[(host, "HTTP 403")]
    assert blocked.confirmed_cause is None
    assert blocked.likely_cause == "bot protection or a web application firewall (challenge page served)"
    assert "challenge-page markers in the body" in blocked.evidence
    robots = by_signature[(host, "robots")]
    assert robots.confirmed_cause == "disallowed by robots.txt"
    callback = by_signature[(host, "callback KeyError")]
    assert callback.confirmed_cause == "the spider's callback raised KeyError: 'price'"
    refused = by_signature[(host, "NetworkError:connect")]
    assert refused.likely_cause == "the server is down or refusing connections"
    assert refused.crawler_state.startswith(("normal", "backing off", "recovering", "paused"))
    assert not_found.last_success is not None  # /product/1 succeeded before
    report = result.failure_report()
    assert "HTTP 429 on 127.0.0.1" in report and "cause (confirmed): the server is rate limiting" in report
    assert "likely cause: bot protection" in report
    assert "no running event loop" not in report


def test_failure_tracker_grouping_and_likely_versus_confirmed() -> None:
    from wintergrab.errors import FetchTimeout, NetworkError
    from wintergrab.fetchers.response import Response

    tracker = FailureTracker()
    tracker.success("shop.test", when=1000.0)
    for i in range(5):
        tracker.failure("shop.test", f"https://shop.test/p/{i}", response=Response("x", status=403,
                        headers={"Server": "cloudflare"}), final=True, when=2000.0 + i)  # fmt: skip
    tracker.failure("shop.test", "https://shop.test/p/0", response=Response("x", status=403), when=2010.0)
    tracker.failure("slow.test", "https://slow.test/", error=FetchTimeout("https://slow.test/"), when=2000.0)
    tracker.failure("gone.test", "https://gone.test/", error=NetworkError("u", "x", kind="dns"), final=True)
    diagnoses = tracker.diagnose()
    first = diagnoses[0]
    assert (first.domain, first.signature, first.affected_urls, first.attempts) == ("shop.test", "HTTP 403", 5, 6)
    assert first.likely_cause == "server-side access policy or rate limiting" and first.confirmed_cause is None
    assert first.last_success == 1000.0 and "Server: cloudflare" in first.evidence
    assert "previous success: 17 min before the first failure" in first.describe()
    by_domain = {d.domain: d for d in diagnoses}
    assert by_domain["slow.test"].likely_cause.startswith("the server is slow")
    assert by_domain["gone.test"].confirmed_cause == "the host name does not resolve (DNS)"
    assert by_domain["gone.test"].failed_urls == 1 and by_domain["slow.test"].failed_urls == 0


# --------------------------------------------------------------------------- #
# dead-letter queue
# --------------------------------------------------------------------------- #
def test_dead_letters_are_recorded_and_retried(fresh_site, tmp_path) -> None:
    class Crawl(Base):
        retries = 1
        crawl_dir = str(tmp_path / "crawl")

        def start_requests(self):
            yield from (fresh_site.url + f"/flaky/{k}?fail=2" for k in ("a", "b"))
            yield fresh_site.url + "/product/1"

        def parse(self, response):
            yield {"url": response.url}

        def on_error(self, request, error):
            pass

    first = Crawl().run()
    assert [i["url"] for i in first.items] == [fresh_site.url + "/product/1"]
    assert first.stats["dead_letters"] == 2
    queue = DeadLetterQueue(tmp_path / "crawl" / "dead_letters.jsonl")
    entries = list(queue.entries())
    assert {e["url"] for e in entries} == {fresh_site.url + "/flaky/a?fail=2", fresh_site.url + "/flaky/b?fail=2"}
    assert all(e["status"] == 503 and e["category"] == "http" and e["attempts"] == 2 for e in entries)

    # The flaky pages work on their third attempt: retry just the dead letters.
    second = Crawl(retry_dead_letters=True).run()
    assert sorted(i["url"] for i in second.items) == sorted(e["url"] for e in entries)
    assert second.stats["dead_letters_retried"] == 2
    assert not queue.path.exists()  # consumed; nothing failed this time
    assert fresh_site.site.hits["/product/1"] == 1  # the rest of the crawl was not repeated


def test_dead_letters_need_somewhere_to_live(site) -> None:
    from wintergrab.errors import ConfigurationError

    class Crawl(Base):
        start_urls = [site.url + "/product/1"]
        retry_dead_letters = True

        def parse(self, response):
            yield {}

    with pytest.raises(ConfigurationError):
        Crawl().run()


def test_unpicklable_dead_letters_keep_a_summary(tmp_path) -> None:
    from wintergrab import Request
    from wintergrab.errors import FetchError

    queue = DeadLetterQueue(tmp_path / "dl.jsonl")
    queue.add(Request("https://x.test/", callback=lambda r: None), FetchError("https://x.test/", "boom"))
    entry = next(queue.entries())
    assert entry["url"] == "https://x.test/" and "request_error" in entry
    assert queue.requests() == []
