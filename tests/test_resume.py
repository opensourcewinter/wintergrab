from __future__ import annotations

import json
import signal
import sys
import threading

import pytest

from wintergrab import CheckpointError, Request, Spider
from wintergrab.spider.checkpoint import Checkpoint


class Catalog(Spider):
    """Crawls 5 listing pages x 4 products. Pauses itself after `pause_after` items."""

    log_level = None
    concurrency = 2
    pause_after: int | None = None

    def parse(self, response):
        for link in response.css(".product .name a"):
            yield response.follow(link, callback=self.parse_product)
        nxt = response.css("a.next")
        if nxt:
            yield response.follow(nxt[0])

    def parse_product(self, response):
        if self.pause_after is not None and self.stats.get("items", 0) + 1 >= self.pause_after:
            self.pause()
        yield {"name": response.css("h1::text").get()}


def read_jsonl(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_pause_and_resume(site, tmp_path) -> None:
    crawl_dir = tmp_path / "crawl"
    out = tmp_path / "items.jsonl"
    start = [site.url + "/products/page/1"]

    first = Catalog(start_urls=start, crawl_dir=str(crawl_dir), output=str(out), pause_after=5).run()
    assert first.status == "paused" and first.paused
    assert (crawl_dir / "state.pickle").exists()
    saved = Checkpoint(crawl_dir).load()
    assert saved["pending"] and saved["spider"] == "Catalog"
    count_after_pause = len(read_jsonl(out))
    assert 5 <= count_after_pause < 20

    second = Catalog(start_urls=start, crawl_dir=str(crawl_dir), output=str(out)).run()
    assert second.status == "finished"
    assert not (crawl_dir / "state.pickle").exists()
    assert json.loads((crawl_dir / "summary.json").read_text(encoding="utf-8"))["status"] == "finished"
    names = [row["name"] for row in read_jsonl(out)]
    assert sorted(names) == sorted(f"Product {i}" for i in range(1, 21))  # nothing lost, nothing twice
    assert second.stats["runs"] == 2
    assert second.stats["items"] == 20  # stats are cumulative across runs


def test_resume_false_starts_over(site, tmp_path) -> None:
    crawl_dir = tmp_path / "crawl"
    start = [site.url + "/products/page/1"]
    Catalog(start_urls=start, crawl_dir=str(crawl_dir), pause_after=3).run()
    fresh = Catalog(start_urls=start, crawl_dir=str(crawl_dir)).run(resume=False)
    assert fresh.status == "finished" and len(fresh.items) == 20 and fresh.stats["runs"] == 1


def test_pause_without_crawl_dir_just_stops(site) -> None:
    result = Catalog(start_urls=[site.url + "/products/page/1"], pause_after=2).run()
    assert result.status == "stopped"


def test_json_output_is_extended_on_resume(site, tmp_path) -> None:
    crawl_dir, out = tmp_path / "crawl", tmp_path / "items.json"
    start = [site.url + "/products/page/1"]
    Catalog(start_urls=start, crawl_dir=str(crawl_dir), output=str(out), pause_after=4).run()
    assert len(json.loads(out.read_text(encoding="utf-8"))) >= 4
    Catalog(start_urls=start, crawl_dir=str(crawl_dir), output=str(out)).run()
    assert len(json.loads(out.read_text(encoding="utf-8"))) == 20


def test_lambda_callbacks_are_rejected_when_resumable(site, tmp_path) -> None:
    class Bad(Spider):
        log_level = None

        def start_requests(self):
            yield Request(site.url + "/", callback=lambda r: None)

    with pytest.raises(CheckpointError, match="not a method of the spider"):
        Bad(crawl_dir=str(tmp_path / "c")).run()
    # Without crawl_dir any callable is fine.
    Bad().run()


def test_state_of_another_spider_is_refused(site, tmp_path) -> None:
    crawl_dir = tmp_path / "crawl"
    Catalog(start_urls=[site.url + "/products/page/1"], crawl_dir=str(crawl_dir), pause_after=2).run()

    class Other(Catalog):
        pass

    with pytest.raises(CheckpointError, match="belongs to spider"):
        Other(start_urls=[site.url + "/"], crawl_dir=str(crawl_dir)).run()
    assert (crawl_dir / "state.pickle").exists()  # untouched


def test_crash_keeps_state_for_resuming(site, tmp_path) -> None:
    crawl_dir = tmp_path / "crawl"

    class Crashy(Catalog):
        def parse_product(self, response):
            if response.url.endswith("/product/3"):
                self.fatal(RuntimeError("boom"))
            yield {"name": response.css("h1::text").get()}

    with pytest.raises(RuntimeError, match="boom"):
        Crashy(start_urls=[site.url + "/products/page/1"], crawl_dir=str(crawl_dir), concurrency=1).run()
    assert (crawl_dir / "state.pickle").exists()
    resumed = Catalog(start_urls=[], crawl_dir=str(crawl_dir), name="Crashy").run()
    assert resumed.status == "finished" and resumed.items


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_ctrl_c_pauses_the_crawl(site, tmp_path) -> None:
    crawl_dir = tmp_path / "crawl"

    class Slow(Spider):
        log_level = None
        concurrency = 1
        start_urls = [site.url + f"/item/{i}?delay=0.2" for i in range(20)]

        def parse(self, response):
            yield {"url": response.url}

    timer = threading.Timer(0.5, lambda: signal.raise_signal(signal.SIGINT))
    timer.start()
    try:
        result = Slow(crawl_dir=str(crawl_dir)).run()
    finally:
        timer.cancel()
    assert result.status == "paused"
    assert 0 < len(result.items) < 20
    resumed = Slow(crawl_dir=str(crawl_dir)).run()
    assert len(result.items) + len(resumed.items) == 20
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
