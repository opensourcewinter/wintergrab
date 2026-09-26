"""The crawl optimizer: learned priorities, barren patterns, parameters that change nothing."""

from __future__ import annotations

import json
from typing import Any

from wintergrab import Spider
from wintergrab.fetchers.response import Response
from wintergrab.request import Request
from wintergrab.spider.optimizer import CrawlOptimizer


class Shop(Spider):
    """The test site's /shop/: 150 products in 5 categories, ?ref= links, a tag cloud that leads nowhere."""

    def parse(self, response: Response) -> Any:
        if "/shop/p/" in response.url:
            yield {"name": response.css("h1::text").get(), "url": response.url.split("?")[0]}
        for link in response.links(same_domain=True, allow=r"/shop/"):
            yield response.follow(link)


def crawl(site: Any, **settings: Any) -> Any:
    return Shop(start_urls=[site.url + "/shop/"], log_level="WARNING", unique_key="url", **settings).run(resume=False)


def fetched(hits: Any, prefix: str) -> int:
    return sum(n for path, n in hits.items() if path.startswith(prefix))


def request(url: str, depth: int = 1, **options: Any) -> Request:
    made = Request(url, **options)
    made.meta["depth"] = depth
    return made


def html(url: str, body: str) -> Response:
    return Response(url, headers={"content-type": "text/html"}, body=f"<html><body>{body}</body></html>".encode())


def test_words_side_by_side_count_as_one() -> None:
    optimizer = CrawlOptimizer()
    words = ("red", "blue", "green", "black", "white", "gold", "pink")
    tags = [optimizer.pattern(f"https://shop.example/tag/{w}") for w in words]
    assert tags[:5] == [f"shop.example/tag/{w}" for w in words[:5]]
    assert tags[5:] == ["shop.example/tag/{word}"] * 2  # a sixth word: they count as one from now on
    sections = [
        optimizer.pattern(f"https://shop.example/{w}") for w in ("about", "blog", "cart", "help", "news", "shop")
    ]
    assert sections[-1] == "shop.example/shop"  # a site's sections are never merged
    assert optimizer.pattern("https://shop.example/p/123?ref=x") == "shop.example/p/{int}?ref"


def test_barren_patterns_are_skipped_productive_ones_never() -> None:
    optimizer = CrawlOptimizer(min_pages=3, probe_every=4)
    home = request("https://s.example/", depth=0)
    optimizer.enqueued(home, None)
    optimizer.page(home, None, 0)
    optimizer.done(home)  # as the engine does: its links are queued by then
    tags = [request(f"https://s.example/tag/{i}") for i in range(3)]
    for tag in tags:
        optimizer.enqueued(tag, home)
    for tag in tags:  # tag pages lead to nothing new: settled at once, and nothing given
        optimizer.page(tag, None, 0)
        optimizer.done(tag)
    assert optimizer.patterns["s.example/tag/{int}"].barren(3)
    assert not optimizer.skip(request("https://s.example/tag/9"))  # nothing found anywhere yet: no judging
    listing = request("https://s.example/c/1")
    optimizer.enqueued(listing, home)
    optimizer.page(listing, None, 0)
    products = [request(f"https://s.example/p/{i}", depth=2) for i in range(3)]
    for product in products:
        optimizer.enqueued(product, listing)
    optimizer.done(listing)
    assert optimizer.patterns["s.example/c/{int}"].settled == 0  # waits for the products it led to
    for product in products:
        optimizer.page(product, None, 1)
        optimizer.done(product)
    assert optimizer.patterns["s.example/c/{int}"].productive == 1 and optimizer.found == 3
    later = [request(f"https://s.example/tag/{i}", depth=2) for i in range(10, 18)]
    assert [optimizer.skip(r) for r in later] == [True, True, True, False] * 2  # one in four is still fetched
    assert not optimizer.skip(later[3])  # ... and not skipped when its turn comes
    assert optimizer.skipped == 6 and optimizer.patterns["s.example/tag/{int}"].skipped == 6
    assert not optimizer.skip(request("https://s.example/tag/30", dont_filter=True))
    assert not optimizer.skip(request("https://s.example/tag/31", depth=0))
    for i in range(2, 7):  # later listings only link to products found already: the pattern stays productive
        again = request(f"https://s.example/c/{i}")
        optimizer.enqueued(again, home)
        optimizer.page(again, None, 0)
        optimizer.done(again)
    listings = optimizer.patterns["s.example/c/{int}"]
    assert (listings.settled, listings.productive) == (6, 1) and not listings.barren(3)
    assert optimizer.boost("https://s.example/p/7") == 20 and optimizer.boost("https://s.example/c/9") == 0
    queued = request("https://s.example/p/8", depth=2)
    optimizer.enqueued(queued, listing)
    assert optimizer.forecast() == {"queued": 1.0, "items": 1.0}
    assert "barren (none of 3 settled pages led to an item)" in optimizer.describe()


def test_a_pattern_that_gave_something_is_never_barren() -> None:
    optimizer = CrawlOptimizer(min_pages=3)
    stats = optimizer._stats("s.example/page/{int}")
    stats.settled, stats.productive = 400.0, 1.0
    for _ in range(10):
        stats.pages = 250.0
        stats.halve()
    assert stats.productive == 1.0 and not stats.barren(3)


def test_parameters_that_change_nothing_are_dropped() -> None:
    optimizer = CrawlOptimizer()

    def visit(url: str, body: str) -> None:
        page = request(url)
        optimizer.enqueued(page, None)
        optimizer.page(page, html(url, body), 1)

    product = "<h1>{0}</h1><p>$10</p><a href='/p/{0}?ref=related'>more</a>"
    visit("https://s.example/p/1?ref=a", product.format(1))
    visit("https://s.example/p/1?ref=b", product.format(1))  # the same page
    assert optimizer.rewrite("https://s.example/p/3?ref=c") == "https://s.example/p/3?ref=c"  # once is not enough
    visit("https://s.example/p/2?ref=a", product.format(2))
    visit("https://s.example/p/2", product.format(2))  # the same without it
    assert optimizer.rewrite("https://s.example/p/3?ref=c&size=m") == "https://s.example/p/3?size=m"
    assert optimizer.duplicate("https://s.example/p/1")  # fetched already, as ?ref=a
    assert not optimizer.duplicate("https://s.example/p/9")
    # pages that differ keep their parameter
    visit("https://s.example/c/phones?page=1", "<h1>phones</h1><a href='/p/1'>1</a>")
    visit("https://s.example/c/phones?page=2", "<h1>phones</h1><a href='/p/2'>2</a>")
    visit("https://s.example/c/phones?page=3", "<h1>phones</h1><a href='/p/3'>3</a>")
    assert optimizer.rewrite("https://s.example/c/phones?page=4") == "https://s.example/c/phones?page=4"
    assert optimizer.evidence["s.example/c/phones"]["page"] == [0, 3]  # 2 vs 1, 3 vs 1, 3 vs 2
    # a JavaScript app shell is the same whatever the URL: it teaches nothing
    shell = "<div id='root'></div><script src='/a.js'></script><script src='/b.js'></script><script>go()</script>"
    for n in range(1, 5):
        visit(f"https://s.example/app?view={n}", shell)
    assert "s.example/app" not in optimizer.evidence
    report = optimizer.describe()
    assert "parameter dropped: ref on s.example/p/{int} (2 pair(s) of pages alike, none different)" in report


def test_an_optimized_crawl(fresh_site, tmp_path) -> None:
    hits = fresh_site.site.hits
    plain = crawl(fresh_site)
    assert (plain.stats["pages"], plain.stats["items"]) == (376, 150)
    assert (fetched(hits, "/shop/p/"), fetched(hits, "/shop/tag/")) == (300, 60)  # each product twice, every tag

    hits.clear()
    path = tmp_path / "shop.optimizer.json"
    first = crawl(fresh_site, optimize=str(path))
    assert first.stats["items"] == 150 and first.stats["pages"] <= 215
    assert fetched(hits, "/shop/p/") <= 160 and first.stats["optimizer/duplicates"] > 0  # ?ref= changes nothing
    assert fetched(hits, "/shop/tag/") <= 40 and first.stats["optimizer/skipped"] >= 20
    report = first.optimizer.describe()
    assert "/shop/tag/{word}: " in report and "barren" in report
    assert "parameter dropped: ref on 127.0.0.1/shop/p/{int}" in report
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["parameters"]["127.0.0.1/shop/p/{int}"]["ref"][1] == 0 and "127.0.0.1/shop/tag" in saved["words"]

    hits.clear()
    second = crawl(fresh_site, optimize=str(path))  # knows it all from the start
    assert second.stats["items"] == 150 and fetched(hits, "/shop/p/") == 150 and fetched(hits, "/shop/tag/") <= 8

    hits.clear()
    limited = crawl(fresh_site, optimize=str(path), max_items=50)  # products first
    assert limited.stats["items"] == 50 and limited.stats["pages"] <= 70
    hits.clear()
    assert crawl(fresh_site, max_items=50).stats["pages"] >= 85


def test_the_optimizer_survives_a_pause(fresh_site, tmp_path) -> None:
    first = crawl(fresh_site, optimize=True, crawl_dir=str(tmp_path / "crawl"), max_pages=60)
    assert first.status == "limit" and (tmp_path / "crawl" / "optimizer.json").exists()
    second = Shop(start_urls=[fresh_site.url + "/shop/"], log_level="WARNING", optimize=True,
                  crawl_dir=str(tmp_path / "crawl")).run()  # fmt: skip
    assert second.status == "finished" and second.stats["items"] > 0
    assert len({i["url"] for i in [*first.items, *second.items]}) == 150


def test_goals_on_a_section(fresh_site) -> None:
    from wintergrab.goals import parse_goal, plan_goal

    goal = parse_goal(f"Find all products with name and price on {fresh_site.url}/shop/")
    plan = plan_goal(goal, sample=15)
    assert plan.sites[0].target == ["/shop/p/*"]  # the section's record pages, not the sitemap's /product/*
    result = plan.run(log_level="WARNING", progress=False)
    assert result.counts["records"] == 150 and result.crawl.optimizer is not None
    first = plan_goal(
        parse_goal(f"Find the first 40 products with name and price on {fresh_site.url}/shop/"), sample=15
    )
    limited = first.run(log_level="WARNING", progress=False, optimize=False)
    assert limited.counts["records"] == 40 and limited.counts["pages"] <= 60  # record pages first


def test_crawl_optimize_option(site, tmp_path, capsys) -> None:
    from wintergrab.cli import main

    path = tmp_path / "opt.json"
    out = tmp_path / "out.jsonl"
    args = ["crawl", site.url + "/shop/", "--allow", "/shop/", "--optimize", str(path), "--max-pages", "80",
            "--no-progress", "-o", str(out)]  # fmt: skip
    assert main(args) == 0
    assert "127.0.0.1/shop/" in json.loads(path.read_text(encoding="utf-8"))["patterns"]


def test_start_requests_are_always_fetched(fresh_site) -> None:
    paths = ("/shop/p/1?ref=a", "/shop/p/1?ref=b", "/shop/p/2?ref=a", "/shop/p/2", "/shop/p/3?ref=a", "/shop/p/3?ref=b")
    spider = Shop(start_urls=[fresh_site.url + p for p in paths], optimize=True, log_level="WARNING", concurrency=1,
                  max_depth=0)  # fmt: skip
    result = spider.run(resume=False)
    assert "ref" in result.optimizer._dropped["127.0.0.1/shop/p/{int}"]  # learned on the way...
    assert result.stats["pages"] == 6 and fetched(fresh_site.site.hits, "/shop/p/") == 6  # ... yet all were asked for
