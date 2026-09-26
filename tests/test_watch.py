"""Watching URLs for changes, and the jobs that run when they change or after another job."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pytest

from test_history import shop  # noqa: F401 (the fixture)
from wintergrab.errors import ConfigurationError
from wintergrab.project import Project, Scheduler
from wintergrab.watch import check


def sitemap(*urls: str, lastmod: str = "2026-09-01") -> str:
    entries = "".join(f"<url><loc>{u}</loc><lastmod>{lastmod}</lastmod></url>" for u in urls)
    return f"<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>{entries}</urlset>"


def test_a_sitemap(shop) -> None:  # noqa: F811
    url = shop.url + "/sitemap.xml"
    shop.pages["/sitemap.xml"] = (200, sitemap("https://s.example/1", "https://s.example/2", "https://s.example/3"))
    first = check(url)
    assert (first.first, first.changed, first.kind, first.summary) == (True, False, "sitemap", "3 URLs (first look)")
    same = check(url, first.state)
    assert not same.changed and same.summary == "no change"
    shop.pages["/sitemap.xml"] = (200, sitemap("https://s.example/1", "https://s.example/2", "https://s.example/4"))
    moved = check(url, same.state)
    assert moved.changed and moved.summary == "1 new URL, 1 gone"
    shop.pages["/sitemap.xml"] = (200, sitemap("https://s.example/1", "https://s.example/2", "https://s.example/4",
                                               lastmod="2026-09-20"))  # fmt: skip
    updated = check(url, moved.state)
    assert updated.changed and updated.summary == "URLs updated (lastmod)"

    shop.pages["/sitemap.xml"] = (500, "down")
    failed = check(url, updated.state)
    assert failed.error == "HTTP 500" and not failed.changed and failed.state == updated.state  # nothing learned
    shop.pages["/robots.txt"] = (200, "User-agent: *\nDisallow: /sitemap.xml\n")
    before = shop.hits.get("/sitemap.xml", 0)
    refused = check(url, updated.state)
    assert refused.error == "robots.txt does not allow it" and shop.hits.get("/sitemap.xml", 0) == before  # not fetched
    assert check(url, updated.state, obey_robots=False).error == "HTTP 500"  # (when told not to look)


def test_a_page_a_feed_and_not_modified(shop, site) -> None:  # noqa: F811
    page = shop.url + "/news"
    shop.pages["/news"] = (200, "<html><body><h1>News</h1><p>First story</p><script>var t = 1;</script></body></html>")
    first = check(page)
    assert first.kind == "page" and first.summary == "a page (first look)"
    shop.pages["/news"] = (200, "<html><body><h1>News</h1><p>First story</p><script>var t = 2;</script></body></html>")
    assert not check(page, first.state).changed  # scripts are not what a page says
    shop.pages["/news"] = (200, "<html><body><h1>News</h1><p>Second story</p></body></html>")
    assert check(page, first.state).summary == "the page changed"

    item = "<item><title>{0}</title><link>https://s.example/{0}</link></item>"
    feed = "<?xml version='1.0'?><rss version='2.0'><channel><title>T</title>{}</channel></rss>"
    shop.pages["/feed"] = (200, feed.format(item.format("a") + item.format("b")))
    seen = check(shop.url + "/feed")
    shop.pages["/feed"] = (200, feed.format(item.format("c") + item.format("a") + item.format("b")))
    later = check(shop.url + "/feed", seen.state)
    assert (seen.kind, later.changed, later.summary) == ("feed", True, "1 new item")

    tagged = check(site.url + "/etag/x?v=3")
    assert tagged.state["etag"] == '"v3"'
    assert check(site.url + "/etag/x?v=3", tagged.state).summary == "not modified (304)"  # a conditional request


def _project(tmp_path, data: dict[str, Any]) -> Project:
    path = tmp_path / "wintergrab.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return Project(path)


def test_jobs_run_when_a_url_changes_and_after_each_other(shop, tmp_path) -> None:  # noqa: F811
    shop.pages["/sitemap.xml"] = (200, sitemap("https://s.example/1"))
    project = _project(tmp_path, {"jobs": {
        "listing": {"crawl": shop.url + "/", "watch": shop.url + "/sitemap.xml", "check": "10 minutes"},
        "details": {"crawl": shop.url + "/p/1", "after": "listing"},
        "report": {"crawl": shop.url + "/p/2", "after": ["details"]},
        "manual": {"crawl": shop.url + "/p/3"},
    }})  # fmt: skip
    clock = [datetime(2026, 9, 26, 9, 0)]
    started: list[tuple[str, str, str | None]] = []
    failing: set[str] = set()

    def runner(command: list[str], *, cwd: Any, log_file: Any) -> int:
        return 1 if command[1] in failing else 0

    def make() -> Scheduler:
        scheduler = Scheduler(project, now=lambda: clock[0], sleep=lambda s: None, runner=runner, webhooks=[])
        scheduler.on_start = lambda job, trigger, reason: started.append((job.name, trigger, reason))
        return scheduler

    assert [(j.name, w) for j, w in make().plan()] == [("listing", clock[0])]  # checked at once
    results = make().run_due()  # first look, and nothing collected yet: the job runs, then those after it
    assert [r.job.name for r in results] == ["listing", "details", "report"]
    assert started == [("listing", "watch", "1 URL (first look)"), ("details", "after", "after listing"),
                       ("report", "after", "after details")]  # fmt: skip
    started.clear()
    clock[0] += timedelta(minutes=5)
    assert make().run_due() == [] and dict((j.name, w) for j, w in make().plan())["listing"] == datetime(
        2026, 9, 26, 9, 10
    )
    clock[0] += timedelta(minutes=6)
    assert make().run_due() == [] and not started  # checked: no change
    shop.pages["/sitemap.xml"] = (200, sitemap("https://s.example/1", "https://s.example/2"))
    clock[0] += timedelta(minutes=11)
    failing.add(shop.url + "/")
    results = make().run_due()
    assert [(r.job.name, r.ok) for r in results] == [("listing", False)]  # it failed: nothing runs after it
    assert started == [("listing", "watch", "1 new URL")]


def test_what_triggers_say(tmp_path, capsys) -> None:
    from wintergrab.cli import main

    base = {"crawl": "https://s.example/"}
    with pytest.raises(ConfigurationError, match="after nothere: no such job"):
        _project(tmp_path, {"jobs": {"a": {**base, "after": "nothere"}}})
    with pytest.raises(ConfigurationError, match="in a circle: a -> b -> a"):
        _project(tmp_path, {"jobs": {"a": {**base, "after": "b"}, "b": {**base, "after": "a"}}})
    with pytest.raises(ConfigurationError, match="check says how often watch"):
        _project(tmp_path, {"jobs": {"a": {**base, "check": "5 minutes"}}})
    with pytest.raises(ConfigurationError, match="once a minute at most"):
        _project(tmp_path, {"jobs": {"a": {**base, "watch": "https://s.example/sitemap.xml", "check": "10s"}}})
    with pytest.raises(ConfigurationError, match="watch is the http"):
        _project(tmp_path, {"jobs": {"a": {**base, "watch": "sitemap.xml"}}})

    project = _project(tmp_path, {"jobs": {
        "prices": {**base, "watch": "https://s.example/sitemap.xml", "check": "1 hour", "schedule": "daily at 06:00"},
        "later": {**base, "after": "prices"},
    }})  # fmt: skip
    assert project.jobs["prices"].trigger() == (
        "daily at 06:00 + when https://s.example/sitemap.xml changes (checked every 1 hour)"
    )
    assert main(["schedule", "--project", str(project.path), "--list"]) == 0
    listed = capsys.readouterr().out
    assert "prices           daily at 06:00 + when https://s.example/sitemap.xml changes" in listed
    assert "later            after prices" in listed
