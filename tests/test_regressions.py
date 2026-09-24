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
def _interrupt_like_a_notebook() -> None:
    if sys.platform == "win32":
        import _thread

        _thread.interrupt_main()  # how ipykernel interrupts on Windows
    else:
        os.kill(os.getpid(), signal.SIGINT)  # a terminal's Ctrl+C reaches the whole process


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
    timer = threading.Timer(0.5, _interrupt_like_a_notebook)
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


# --------------------------------------------------------------------------- #
# 0.2 review fixes
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_disk_frontier_crash_in_the_first_seconds_loses_nothing(site, tmp_path) -> None:
    crawl_dir, out = tmp_path / "c", tmp_path / "items.jsonl"
    script = tmp_path / "crawl.py"
    script.write_text(
        textwrap.dedent(
            f"""
            from wintergrab import Spider

            class Slow(Spider):
                name = "slow"
                log_level = None
                obey_robots_txt = False
                concurrency = 2
                frontier = "disk"
                crawl_dir = {str(crawl_dir)!r}
                output = {str(out)!r}
                start_urls = [{site.url + "/links?n=30"!r}]   # default checkpoint_interval (60s)

                def parse(self, response):
                    if "links" in response.url:
                        for link in response.css("a[href^='/item/']"):
                            yield response.follow(link.attr("href") + "?delay=0.05")
                    else:
                        yield {{"url": response.url.split("?")[0]}}

            Slow().run()
            """
        )
    )
    proc = subprocess.Popen([sys.executable, str(script)])
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not (out.exists() and len(out.read_text().splitlines()) >= 6):
        time.sleep(0.05)
    time.sleep(0.3)  # let at least one frontier commit happen
    proc.kill()  # SIGKILL; TerminateProcess on Windows
    proc.wait()
    subprocess.run([sys.executable, str(script)], check=True, timeout=120)
    urls = {json.loads(line)["url"] for line in out.read_text().splitlines()}
    assert urls == {site.url + f"/item/{i}" for i in range(30)}


def test_disk_frontier_pause_with_only_delayed_retries(fresh_site, tmp_path) -> None:
    class Flaky(Quiet):
        frontier = "disk"
        retries = 3

        def start_requests(self):
            yield Request(fresh_site.url + "/flaky/p?fail=1&code=500")

        def parse(self, response):
            yield {"ok": True}

        def on_error(self, request, error):  # pragma: no cover - should not happen
            raise AssertionError(error)

    spider = Flaky(crawl_dir=str(tmp_path / "c"))

    async def pause_while_retry_waits():
        task = asyncio.ensure_future(spider.arun())
        while not spider.stats.get("retries"):
            await asyncio.sleep(0.01)
        spider.pause()
        return await task

    first = asyncio.run(pause_while_retry_waits())
    assert first.status == "paused"
    second = Flaky(crawl_dir=str(tmp_path / "c")).run()
    assert second.status == "finished" and second.items == [{"ok": True}] and second.stats["runs"] == 2


def test_sqlite_exporter_handles_awkward_keys(tmp_path) -> None:
    import sqlite3

    db = tmp_path / "x.db"
    exporter = open_exporter(db, unique_key="url")
    exporter.write({"url": "u1", "Name": "A", "name": "a", "a-b": 1, "a b": 2, "a_b": 3, "_wg_rowid": 9, "1st": "x"})
    exporter.write({"url": "u1", "Name": "B"})  # upsert
    exporter.close()
    conn = sqlite3.connect(db)
    mapping = dict(conn.execute("SELECT key, col FROM _wintergrab_columns"))
    assert len({c.lower() for c in mapping.values()}) == len(mapping)  # no collisions
    rows = conn.execute("SELECT * FROM items").fetchall()
    assert len(rows) == 1
    cols = [d[0] for d in conn.execute("SELECT * FROM items").description]
    record = dict(zip(cols, rows[0], strict=True))
    assert record[mapping["Name"]] == "B" and record[mapping["name"]] == "a"
    assert {record[mapping[k]] for k in ("a-b", "a b", "a_b")} == {1, 2, 3}
    conn.close()

    # unique_key can change between runs
    exporter = open_exporter(db, append=True, unique_key="Name")
    exporter.write({"url": "u2", "Name": "B"})
    exporter.close()
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_sqlite_exporter_refuses_foreign_tables(tmp_path) -> None:
    import sqlite3

    db = tmp_path / "mine.db"
    sqlite3.connect(db).execute("CREATE TABLE items (x)")
    with pytest.raises(ValueError, match="did not create"):
        open_exporter(db)


def test_sqlite_exporter_rejects_duplicate_unique_values(tmp_path) -> None:
    db = tmp_path / "d.db"
    exporter = open_exporter(db, unique_key=None)
    exporter.write({"k": 1})
    exporter.write({"k": 1})
    exporter.close()
    with pytest.raises(ValueError, match="duplicate values"):
        open_exporter(db, append=True, unique_key="k")


def test_blocked_responses_are_not_replayed_from_cache(fresh_site, tmp_path) -> None:
    class SoftBlock(Quiet):
        start_urls = [fresh_site.url + "/flaky/sb?fail=1&code=200"]
        cache_mode = "prefer"
        retries = 2

        def is_blocked(self, response):
            return "temporarily unavailable" in response.text

        def parse(self, response):
            yield {"attempt": response.css("#attempt::text").get()}

    result = SoftBlock(cache=str(tmp_path / "cache")).run()
    assert result.items == [{"attempt": "2"}]


def test_crawl_unique_key_to_stdout_prints_each_item_once(site, capsys) -> None:
    from wintergrab.cli import main

    assert main(["-q", "crawl", site.url + "/products/page/1", "--each", ".product", "--field", "x=.nope::text",
                 "--unique-key", "url", "--no-progress"]) == 0  # fmt: skip
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == len({json.loads(line)["url"] for line in lines})


def test_offline_never_touches_the_network(fresh_site, tmp_path) -> None:
    with pytest.raises(wg.CacheMiss):
        wg.post(fresh_site.url + "/post", data={"a": 1}, cache=tmp_path / "c", cache_mode="offline")
    assert sum(fresh_site.site.hits.values()) == 0


def test_add_cookies_does_not_accumulate() -> None:
    fetcher = wg.AsyncFetcher()
    for _ in range(100):
        fetcher.add_cookies({"a": "1", "b": "2"}, url="https://x.test/")
    assert len(fetcher._pending_cookies) == 2


def test_cli_cache_and_capture_flags_do_not_eat_urls(site, capsys, tmp_path) -> None:
    from wintergrab.cli import build_parser, main

    args = build_parser().parse_args(["get", "--cache", "--capture", site.url, "b"])
    assert args.urls == [site.url, "b"] and args.cache is True and args.capture is True
    args = build_parser().parse_args(["get", site.url, "--cache"])
    assert args.urls == [site.url] and args.cache is True
    assert main(["-q", "get", "--cache-dir", str(tmp_path / "empty-cache"), "--offline", site.url]) == 1


def test_cli_learn_unknown_example_is_a_clean_error(site, capsys) -> None:
    from wintergrab.cli import main

    assert main(["-q", "get", site.url + "/books/", "--learn", "title=No such book anywhere"]) == 1
    assert "error:" in capsys.readouterr().err


@pytest.mark.browser
def test_capture_keeps_binary_post_bodies(site) -> None:
    async def post_binary(page):
        await page.evaluate("fetch('/post', {method: 'POST', body: new Uint8Array([0xff, 0xfe, 0x00, 0x81])})")
        await page.wait_for_timeout(300)

    with wg.BrowserFetcher() as browser:
        page = browser.get(site.url + "/product/1", capture="/post", page_action=post_binary)
    assert len(page.captured) == 1 and page.captured[0].method == "POST"


# The structure of Fastly's bot check as pypi.org served it (status 200, ~3 KB).
FASTLY_CHALLENGE = b"""<!DOCTYPE html><html lang="en"><head>
<link href="/_fs-ch-1T1wmsGaOgGaSxcX/assets/styles.css" rel="stylesheet" />
<title>Client Challenge</title></head><body>
<noscript><span>JavaScript is disabled in your browser.</span><p>Please enable JavaScript to proceed.</p></noscript>
<div id="loading-error" role="alert">A required part of this site couldn't load.</div>
<script src="/_fs-ch-1T1wmsGaOgGaSxcX/script.js"></script></body></html>"""


def test_fastly_client_challenge_is_detected_as_blocked() -> None:
    from wintergrab.fetchers import looks_blocked

    page = wg.Response(
        "https://pypi.org/search/?q=x", status=200, headers={"content-type": "text/html"}, body=FASTLY_CHALLENGE
    )
    assert looks_blocked(page)
    # an ordinary page that merely asks for JavaScript is not a challenge
    ordinary = FASTLY_CHALLENGE.replace(b"Client Challenge", b"Search results").replace(b"/_fs-ch-", b"/static-")
    assert not looks_blocked(wg.Response("https://pypi.org/", headers={"content-type": "text/html"}, body=ordinary))


def test_giving_up_on_a_block_page_says_why() -> None:
    from wintergrab.errors import HTTPStatusError

    page = wg.Response("https://a.test/", body=FASTLY_CHALLENGE)
    assert str(HTTPStatusError(page)) == "HTTP 200 for https://a.test/"
    assert str(HTTPStatusError(page, "looks like a bot-check page")).startswith(
        "looks like a bot-check page (HTTP 200)"
    )


def test_redirects_off_the_allowed_domains_are_not_processed(site) -> None:
    # 127.0.0.1 is allowed; "localhost" is the same server under a name that is not.
    offsite = site.url.replace("127.0.0.1", "localhost") + "/quotes/"
    seen: list[str] = []

    class Hop(Spider):
        start_urls = [site.url + "/redirect?to=" + offsite, site.url + "/redirect?to=/quotes/page/2/"]
        allowed_domains = ["127.0.0.1"]
        log_level = None

        def parse(self, response):
            seen.append(response.url)
            yield {"url": response.url}

    result = Hop().run()
    assert seen == [site.url + "/quotes/page/2/"]  # the on-site redirect still works
    assert result.stats["offsite_redirects"] == 1
