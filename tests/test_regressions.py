"""Regression tests for crawl-engine edge cases (lost requests, hangs, corrupt state)."""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest

import wintergrab as wg
from wintergrab import CheckpointError, Request, Spider
from wintergrab.spider.checkpoint import Checkpoint
from wintergrab.spider.exporters import open_exporter


class Quiet(Spider):
    log_level = None
    obey_robots_txt = False


def test_stream_early_exit_with_a_full_queue_does_not_hang(site) -> None:
    class Many(Quiet):
        start_urls = [site.url + "/product/1"]

        def parse(self, response):
            for i in range(1000):
                yield {"i": i}

    async def first_only():
        async for item in Many().stream():
            return item

    started = time.monotonic()
    assert asyncio.run(asyncio.wait_for(first_only(), timeout=30)) == {"i": 0}
    assert time.monotonic() - started < 10


def test_failed_resume_keeps_the_checkpoint(site, tmp_path) -> None:
    class Pausing(Quiet):
        fail_on_start = False

        def on_start(self):
            if self.fail_on_start:
                raise RuntimeError("login failed")

        def parse(self, response):
            self.pause()
            yield from response.follow_all(".product .name")

    crawl_dir = tmp_path / "c"
    first = Pausing(start_urls=[site.url + "/products/page/1"], crawl_dir=str(crawl_dir)).run()
    assert first.status == "paused"
    with pytest.raises(RuntimeError, match="login failed"):
        Pausing(start_urls=[], crawl_dir=str(crawl_dir), fail_on_start=True).run()
    assert Checkpoint(crawl_dir).load()["pending"]  # still there for the next attempt


@pytest.mark.parametrize(
    "tail",
    [
        "",  # crashed after the last complete item, before "]"
        ',\n{"i": 3, "tags": ["a", "b',  # crashed in the middle of an item
        "\n]\n",  # clean close
    ],
)
def test_json_output_survives_a_crash(tmp_path, tail) -> None:
    path = tmp_path / "items.json"
    body = '[\n{"i": 1, "tags": ["x"]},\n{"i": 2, "tags": ["y", "z"]}'
    path.write_text(body + tail)
    exporter = open_exporter(path, append=True)
    exporter.write({"i": 99})
    exporter.close()
    assert [row["i"] for row in json.loads(path.read_text())] == [1, 2, 99]


def test_json_output_refuses_foreign_files(tmp_path) -> None:
    path = tmp_path / "items.json"
    path.write_text('{"not": "an array"}')
    with pytest.raises(ValueError, match=r"not a JSON array"):
        open_exporter(path, append=True)
    assert path.read_text() == '{"not": "an array"}'


def test_cancelled_crawl_saves_every_in_flight_request(site, tmp_path) -> None:
    class Slow(Quiet):
        concurrency = 4
        start_urls = [site.url + f"/item/{i}?delay=2" for i in range(4)]

        def parse(self, response):
            yield {"url": response.url}

    crawl_dir = tmp_path / "c"

    async def cancel_soon():
        task = asyncio.ensure_future(Slow(crawl_dir=str(crawl_dir)).arun())
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_soon())
    pending = Checkpoint(crawl_dir).load()["pending"]
    assert sorted(p["url"] for p in pending) == sorted(Slow.start_urls)


def test_fatal_error_requeues_the_request_that_caused_it(site, tmp_path) -> None:
    class NoSuchSession(Quiet):
        concurrency = 3

        def start_requests(self):
            for i in range(6):
                yield Request(site.url + f"/item/{i}", session="nope" if i == 0 else None)

        def parse(self, response):
            yield {"url": response.url}

    crawl_dir = tmp_path / "c"
    with pytest.raises(LookupError):
        NoSuchSession(crawl_dir=str(crawl_dir)).run()
    state = Checkpoint(crawl_dir).load()
    done = {site.url + f"/item/{i}" for i in range(6)} - {p["url"] for p in state["pending"]}
    # Every URL is either saved for later or was fully processed.
    assert site.url + "/item/0" not in done


def test_max_pages_still_finishes_retries(fresh_site, tmp_path) -> None:
    class Limited(Quiet):
        concurrency = 1
        start_urls = [fresh_site.url + "/flaky/m?fail=1&code=500", *(fresh_site.url + f"/item/{i}" for i in range(5))]

        def parse(self, response):
            yield {"url": response.url}

    result = Limited(max_pages=3, crawl_dir=str(tmp_path / "c")).run()
    assert result.status == "limit"
    assert len(result.items) == 3 and result.stats.get("failed", 0) == 0
    # The unvisited pages are kept, so raising the limit continues the crawl.
    assert len(Checkpoint(tmp_path / "c").load()["pending"]) == 3
    more = Limited(max_pages=10, crawl_dir=str(tmp_path / "c")).run()
    assert more.status == "finished" and len(more.items) == 3


def test_429_without_retry_after_is_not_hammered(fresh_site) -> None:
    class Limited(Quiet):
        start_urls = [fresh_site.url + "/ratelimited/n?limit=2&after="]
        autothrottle = False
        retries = 3

        def parse(self, response):
            yield {}

    Limited().run()
    times = [t for t, path, _ in fresh_site.site.log if path.startswith("/ratelimited/")]
    gaps = [b - a for a, b in itertools.pairwise(times)]
    assert len(gaps) == 2 and min(gaps) >= 0.3


def test_pushback_delay_applies_immediately() -> None:
    throttle = wg.AutoThrottle(randomize=False)
    slot = throttle.slot("a.test")
    throttle.on_start(slot, time.monotonic())
    throttle.on_pushback("a.test")
    assert not throttle.can_start(slot, time.monotonic() + 0.5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_signal_handlers_are_restored(site) -> None:
    def custom(signum, frame):  # pragma: no cover - never called
        pass

    previous = signal.signal(signal.SIGTERM, custom)
    try:

        class One(Quiet):
            start_urls = [site.url + "/product/1"]

            def parse(self, response):
                yield {}

        One().run()
        assert signal.getsignal(signal.SIGTERM) is custom
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    finally:
        signal.signal(signal.SIGTERM, previous)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_ctrl_c_pauses_a_crawl_started_inside_an_event_loop(site, tmp_path) -> None:
    class Slow(Quiet):
        concurrency = 1
        start_urls = [site.url + f"/item/{i}?delay=0.3" for i in range(15)]

        def parse(self, response):
            yield {"u": response.url}

    async def notebook_cell():
        return Slow(crawl_dir=str(tmp_path / "c")).run()  # runs in a helper thread

    # Like a Jupyter kernel: a loop without asyncio.run()'s own SIGINT handler,
    # so Ctrl+C surfaces as KeyboardInterrupt in the main thread.
    loop = asyncio.new_event_loop()
    timer = threading.Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGINT))
    timer.start()
    try:
        result = loop.run_until_complete(notebook_cell())
    finally:
        timer.cancel()
        loop.close()
    assert result.status == "paused"
    assert len(result.items) < 15


def test_request_options_cannot_override_engine_arguments(site, caplog) -> None:
    class Clash(Quiet):
        def start_requests(self):
            yield Request(site.url + "/product/1", options={"proxy": "http://nope:1", "retries": 9})

        def parse(self, response):
            yield {"ok": True}

    assert Clash().run().items == [{"ok": True}]


def test_unpicklable_request_data_is_rejected_early(site, tmp_path) -> None:
    class Unpicklable(Quiet):
        def start_requests(self):
            yield Request(site.url + "/", meta={"fn": lambda: None})

    with pytest.raises(CheckpointError, match="cannot be saved"):
        Unpicklable(crawl_dir=str(tmp_path / "c")).run()


def test_adaptive_css_with_child_combinators() -> None:
    store = wg.MemoryStorage()
    doc = wg.parse("<div><p>a</p><p>b<b>c</b></p></div>", url="https://x.test/", adaptive_storage=store)
    assert doc.css("div > *::text", adaptive=True).getall() == ["a", "b"]
    assert doc.css("div > ::text", adaptive=True).getall() == doc.css("div > ::text").getall()
    assert doc.css("*::text", adaptive=True).getall() == doc.css("*::text").getall()


@pytest.mark.browser
def test_unclosed_browser_does_not_hang_exit(site) -> None:
    script = textwrap.dedent(
        f"""
        from wintergrab import BrowserFetcher
        browser = BrowserFetcher()
        print(browser.get({site.url + "/product/1"!r}).status)
        """
    )
    started = time.monotonic()
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert proc.stdout.strip() == "200", proc.stderr
    assert time.monotonic() - started < 30


@pytest.mark.browser
async def test_one_browser_context_per_proxy_under_concurrency(site, proxies) -> None:
    async with wg.AsyncBrowserFetcher(max_pages=4) as browser:
        pages = await asyncio.gather(
            *(browser.get(site.url + f"/product/{i}", proxy=proxies[0].url) for i in range(1, 5))
        )
        assert all(p.status == 200 for p in pages)
        assert len(browser._contexts) == 1
        assert browser._context_users == {proxies[0].url: 0}
