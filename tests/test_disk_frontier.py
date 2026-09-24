"""Crawls on the SQLite frontier: same results, pause/resume, crash recovery."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from wintergrab import CheckpointError, Spider


class Catalog(Spider):
    log_level = None
    obey_robots_txt = False
    concurrency = 2
    pause_after: int | None = None

    def parse(self, response):
        for link in response.css(".product .name a"):
            yield response.follow(link, callback=self.parse_product)
        yield response.follow_next()

    def parse_product(self, response):
        if self.pause_after is not None and self.stats.get("items", 0) + 1 >= self.pause_after:
            self.pause()
        yield {"name": response.css("h1::text").get()}


def names(items) -> list[str]:
    return sorted(i["name"] for i in items)


EXPECTED = sorted(f"Product {i}" for i in range(1, 21))


def test_disk_frontier_crawls_like_memory(site, tmp_path) -> None:
    start = [site.url + "/products/page/1"]
    memory = Catalog(start_urls=start).run()
    disk = Catalog(start_urls=start, frontier="disk", crawl_dir=str(tmp_path / "c")).run()
    assert names(disk.items) == names(memory.items) == EXPECTED
    assert disk.stats["pages"] == memory.stats["pages"]
    assert not (tmp_path / "c" / "frontier.sqlite3").exists()  # cleaned up when finished


def test_disk_frontier_pause_and_resume(site, tmp_path) -> None:
    kwargs = {"start_urls": [site.url + "/products/page/1"], "frontier": "disk", "crawl_dir": str(tmp_path / "c")}
    out = tmp_path / "items.jsonl"
    first = Catalog(pause_after=6, output=str(out), **kwargs).run()
    assert first.status == "paused"
    assert (tmp_path / "c" / "frontier.sqlite3").exists()
    second = Catalog(output=str(out), **kwargs).run()
    assert second.status == "finished"
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert sorted(r["name"] for r in rows) == EXPECTED  # nothing lost, nothing twice
    assert second.stats["runs"] == 2


def test_disk_frontier_requires_crawl_dir(site) -> None:
    with pytest.raises(ValueError, match="crawl_dir"):
        Catalog(start_urls=[site.url + "/"], frontier="disk").run()


def test_memory_frontier_cannot_resume_a_disk_crawl(site, tmp_path) -> None:
    kwargs = {"start_urls": [site.url + "/products/page/1"], "crawl_dir": str(tmp_path / "c")}
    Catalog(pause_after=3, frontier="disk", **kwargs).run()
    with pytest.raises(CheckpointError, match="frontier='disk'"):
        Catalog(**kwargs).run()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_disk_frontier_survives_a_hard_crash(site, tmp_path) -> None:
    """kill -9 in the middle of a crawl, then resume: every page is still crawled."""
    crawl_dir = tmp_path / "c"
    out = tmp_path / "items.jsonl"
    script = tmp_path / "crawl.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(Path(__file__).parent)!r})
            from wintergrab import Spider

            class Slow(Spider):
                name = "slow"
                log_level = None
                obey_robots_txt = False
                concurrency = 2
                frontier = "disk"
                crawl_dir = {str(crawl_dir)!r}
                output = {str(out)!r}
                checkpoint_interval = 0.2
                start_urls = [{site.url + "/links?n=40"!r}]

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
    while time.monotonic() < deadline:
        if out.exists() and len(out.read_text(encoding="utf-8").splitlines()) >= 8:
            break
        time.sleep(0.05)
    proc.kill()  # SIGKILL; TerminateProcess on Windows
    proc.wait()
    crashed = len(out.read_text(encoding="utf-8").splitlines())
    assert 0 < crashed < 40

    subprocess.run([sys.executable, str(script)], check=True, timeout=120)
    urls = {json.loads(line)["url"] for line in out.read_text(encoding="utf-8").splitlines()}
    assert urls == {site.url + f"/item/{i}" for i in range(40)}  # at-least-once: nothing missing
