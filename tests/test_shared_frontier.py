"""A frontier in PostgreSQL shared by several crawling processes (WINTERGRAB_TEST_POSTGRES names the server)."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any

import pytest

from testsite import BOOK_PAGES, BOOKS_PER_PAGE
from wintergrab import Request, Spider
from wintergrab.errors import ConfigurationError
from wintergrab.spider.shared import SharedScheduler
from wintergrab.spider.throttle import AutoThrottle

POSTGRES = os.environ.get("WINTERGRAB_TEST_POSTGRES")
pytestmark = pytest.mark.skipif(not POSTGRES, reason="set WINTERGRAB_TEST_POSTGRES to a PostgreSQL URL")


@pytest.fixture
def crawl():
    pytest.importorskip("psycopg")
    name = f"test_{uuid.uuid4().hex[:8]}"
    yield f"{POSTGRES}?crawl={name}"
    import psycopg

    with psycopg.connect(str(POSTGRES), autocommit=True) as connection:
        for table in ("wintergrab_frontier", "wintergrab_frontier_seen", "wintergrab_frontier_domains",
                      "wintergrab_crawls"):  # fmt: skip
            connection.execute(f"DELETE FROM {table} WHERE crawl = %s", (name,))


def test_processes_share_the_queue_the_seen_urls_and_each_site_s_pace(crawl) -> None:
    first = SharedScheduler(crawl, None)
    other = SharedScheduler(crawl, None)  # another process: it joins
    assert (first.existed, other.existed) == (False, True)
    slow = AutoThrottle(base_delay=5.0)  # (a site's delay: 5 seconds between its requests, whoever sends them)
    assert first.push(Request("https://a.example/1", priority=5))
    assert first.push(Request("https://a.example/2", priority=9))
    assert first.push(Request("https://b.example/1"))
    assert not first.push(Request("https://a.example/1"))  # seen here
    assert other.push(Request("https://a.example/2"))  # queued by the first: dropped when written
    assert len(first) == 3
    taken, _ = first.pop_ready(slow, time.monotonic())
    assert taken is not None and taken.url == "https://a.example/2"  # the best, and a.example's turn is taken
    later, _ = other.pop_ready(slow, time.monotonic())
    assert later is not None and later.url == "https://b.example/1"  # not a.example/1: a.example waits 5 s
    assert other.duplicates == 1 and "https://a.example/2" not in {r.url for r in other.pending()}
    nothing, wait = other.pop_ready(slow, time.monotonic())
    assert nothing is None and wait is not None and 4 < wait <= 5  # a.example/1, in about 5 s
    assert Request("https://a.example/1").fingerprint() in other.seen  # the duplicate filter is shared
    first.ack(taken)
    other.ack(later)
    first.commit()
    other.commit()
    assert len(first) == 1  # a.example/1 is left
    first.close()
    other.close()


def test_a_process_that_died_leaves_its_requests_to_the_others(crawl) -> None:
    dead = SharedScheduler(crawl, None, lease=0.5)
    alive = SharedScheduler(crawl, None, lease=0.5)
    dead.push(Request("https://a.example/1", data=b"\x00\xffbytes", meta={"item": {"n": 1, "tags": ["x"]}}))
    taken, _ = dead.pop_ready(AutoThrottle(), time.monotonic())
    assert taken is not None and taken.data == b"\x00\xffbytes" and taken.meta["item"] == {"n": 1, "tags": ["x"]}
    dead._db.close()  # (it dies: no ack, no release)
    assert alive.pop_ready(AutoThrottle(), time.monotonic())[0] is None  # leased
    time.sleep(0.7)
    again, _ = alive.pop_ready(AutoThrottle(), time.monotonic())
    assert again is not None and again.url == "https://a.example/1"  # its lease ran out
    with pytest.raises(ConfigurationError, match=r"as JSON, and the meta\['when'\] of https://a.example/2 is a"):
        alive.push(Request("https://a.example/2", meta={"when": object()}))
    alive.ack(again)
    alive.close(finished=True)  # nothing queued or leased: finished
    restarted = SharedScheduler(crawl, None)
    assert not restarted.existed and "https://a.example/1" not in [r.url for r in restarted.pending()]
    assert Request("https://a.example/1").fingerprint() not in restarted.seen  # a new crawl: nothing seen yet
    restarted.push(Request("https://a.example/1"))
    restarted.commit()
    fresh = SharedScheduler(crawl, None, fresh=True)  # --fresh: emptied, for everyone
    assert not fresh.existed and len(fresh) == 0
    restarted.close()
    fresh.close()


class Books(Spider):
    name = "books"
    concurrency = 2

    def parse(self, response):
        for link in response.css("article.product_pod h3 a::attr(href)").getall():
            yield response.follow(link, callback=self.parse_book)
        nxt = response.css("li.next a::attr(href)").get()
        if nxt:
            yield response.follow(nxt)

    def parse_book(self, response):
        yield {"url": response.url, "title": response.css("h1::text").get()}


def test_two_processes_crawl_one_site_together(fresh_site, crawl, tmp_path) -> None:
    outputs = [tmp_path / "one.jsonl", tmp_path / "two.jsonl"]
    results: list[Any] = [None, None]

    def work(n: int) -> None:
        spider = Books(start_urls=[fresh_site.url + "/books/"], frontier=crawl, output=str(outputs[n]),
                       log_level="WARNING")  # fmt: skip
        results[n] = spider.run()

    workers = [threading.Thread(target=work, args=(n,)) for n in range(2)]
    for worker in workers:
        worker.start()
        time.sleep(0.2)  # (the first seeds the crawl; the second joins it)
    for worker in workers:
        worker.join(60)
    items = [json.loads(line) for path in outputs if path.exists() for line in path.read_text().splitlines()]
    books = BOOK_PAGES * BOOKS_PER_PAGE
    assert sorted(i["url"] for i in items) == sorted({i["url"] for i in items}) and len(items) == books
    fetched = {path: n for path, n in fresh_site.site.hits.items() if path.startswith("/books/")}
    assert len(fetched) == books + BOOK_PAGES and set(fetched.values()) == {1}  # every page, once
    assert all(r is not None and r.stats["status"] == "finished" for r in results)
    assert sum(r.stats.get("pages", 0) for r in results) == books + BOOK_PAGES  # shared between them


def test_a_slow_process_does_not_take_back_what_another_took_over(crawl) -> None:
    slow = SharedScheduler(crawl, None, lease=0.5)
    other = SharedScheduler(crawl, None, lease=0.5)
    slow.push(Request("https://a.example/1"))
    taken, _ = slow.pop_ready(AutoThrottle(), time.monotonic())
    time.sleep(0.7)  # (its lease runs out while it works)
    again, _ = other.pop_ready(AutoThrottle(), time.monotonic())
    assert taken is not None and again is not None and again.url == taken.url
    slow.ack(taken)
    slow.commit()
    assert len(other) == 1  # still the other's to finish
    other.ack(again)
    other.commit()
    assert len(other) == 0
    slow.close()
    other.close(finished=True)


def test_a_lost_connection_is_made_again(crawl, caplog) -> None:
    import psycopg

    scheduler = SharedScheduler(crawl, None)
    scheduler.push(Request("https://a.example/1"))
    with psycopg.connect(str(POSTGRES), autocommit=True) as admin:  # (the server hangs up on it)
        admin.execute("SELECT pg_terminate_backend(%s)", (scheduler._db.info.backend_pid,))
    taken, _ = scheduler.pop_ready(AutoThrottle(), time.monotonic())  # its queued request written, then taken
    assert taken is not None and taken.url == "https://a.example/1"
    assert "the database hung up" in caplog.text and "connecting again" in caplog.text
    scheduler.ack(taken)
    scheduler.close(finished=True)
