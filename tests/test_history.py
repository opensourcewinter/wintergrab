"""Crawl history: page snapshots, change reports between runs, freshness, and the spider integration."""

from __future__ import annotations

import json
import math
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from wintergrab import Spider
from wintergrab.cli import main
from wintergrab.errors import ConfigurationError
from wintergrab.fetchers.response import Response
from wintergrab.history import PageHistory, compare_snapshots, snapshot_page

DAY = 86_400.0


def product_page(
    name: str, price: float, availability: str = "InStock", *, extra: str = "", nav: str = "", version: int = 1
) -> str:
    data = {"@context": "https://schema.org", "@type": "Product", "name": name,
            "offers": {"@type": "Offer", "price": str(price), "priceCurrency": "USD",
                       "availability": f"https://schema.org/{availability}"}}  # fmt: skip
    return (
        f"<html><head><title>{name}</title><meta name='description' content='About {name}'>"
        f"<meta name='csrf-token' content='token-{version}'><script type='application/ld+json'>{json.dumps(data)}</script>"
        f"</head><body><nav><a href='/'>Home</a>{nav}</nav><h1>{name}</h1><img src='/img/{name}.jpg?v={version}'>"
        f"<p class='price'>${price:.2f}</p>{extra}</body></html>"
    )


def response(path: str, html: str, status: int = 200) -> Response:
    return Response(f"https://shop.example{path}", status=status, body=html.encode(), headers={"etag": '"x"'})


# ------------------------------------------------------------------------------------------ snapshots


def test_snapshot_fingerprints() -> None:
    snap = snapshot_page(response("/p/1", product_page("Phone", 299.99)), items=[{"name": "Phone", "_confidence": 0.9}])
    assert (snap.url, snap.status, snap.title, snap.description) == (
        "https://shop.example/p/1",
        200,
        "Phone",
        "About Phone",
    )
    assert (snap.price, snap.currency, snap.availability) == (299.99, "USD", "InStock")
    assert snap.types == ["Offer", "Product"]
    assert (snap.image_count, snap.nav_count, snap.item_count, snap.etag) == (1, 1, 1, '"x"')
    # the same page again: a new CSRF token, a cache-busting image query and extra spaces change nothing
    again = snapshot_page(
        response("/p/1", product_page("Phone", 299.99, extra=" ", version=2)), items=[{"name": "Phone"}]
    )
    assert again.body != snap.body and compare_snapshots(snap, again) is None
    same = snapshot_page(response("/p/1", product_page("Phone", 299.99)), items=[{"name": "Phone", "_confidence": 0.1}])
    assert compare_snapshots(snap, same) is None
    assert snap.content == same.content
    og = snapshot_page(
        "<html><head><meta property='product:price:amount' content='12.50'>"
        "<meta property='product:price:currency' content='EUR'>"
        "<meta property='product:availability' content='out of stock'></head><body>x</body></html>",
        url="https://shop.example/og",
    )
    assert (og.price, og.currency, og.availability, og.structured) == (12.5, "EUR", "OutOfStock", None)


def test_what_changed() -> None:
    before = snapshot_page(response("/p/1", product_page("Phone", 299.0)), items=[{"price": 299}])
    after = snapshot_page(response("/p/1", product_page("Phone", 279.0, "OutOfStock")), items=[{"price": 279}])
    change = compare_snapshots(before, after)
    assert change.kinds == ["text", "structured-data", "price", "availability", "items"]  # the price is on the page
    assert change.details["price"] == [299.0, 279.0] and change.details["availability"] == ["InStock", "OutOfStock"]
    assert "price 299.0 -> 279.0" in str(change)
    section = (
        "<section><div><ul><li><a href='/x'>x</a></li></ul></div><table><tr><td><b>1</b></td></tr></table></section>"
    )
    redesigned = snapshot_page(
        response("/p/1", product_page("Phone", 299.0, extra=section * 3, nav="<a href='/new'>New</a>"))
    )
    change = compare_snapshots(snapshot_page(response("/p/1", product_page("Phone", 299.0))), redesigned)
    assert {"text", "layout", "navigation"} <= set(change.kinds)
    assert change.details["layout_similarity"] < 0.9 and change.details["text_similarity"] < 1
    gone = snapshot_page(response("/p/1", "<h1>Not found</h1>", 404))
    assert "status" in compare_snapshots(before, gone).kinds
    retitled = snapshot_page(response("/p/1", product_page("Phone 2", 299.0)))
    assert compare_snapshots(before, retitled).details["title"] == ["Phone", "Phone 2"]
    no_schema = snapshot_page(response("/p/1", "<html><head><title>Phone</title></head><body>Phone</body></html>"))
    assert compare_snapshots(before, no_schema).details["schema"] == {"added": [], "removed": ["Offer", "Product"]}


# ------------------------------------------------------------------------------------------ the store


def test_runs_and_change_reports(tmp_path) -> None:
    with PageHistory(tmp_path / "shop.history", keep_html=True) as history:
        first = history.start_run("shop")
        for i in (1, 2, 3, 4):
            history.observe(first, response(f"/p/{i}", product_page(f"P{i}", 10.0 * i)), items=[{"n": i}])
        history.finish_run(first, "finished")
        report = history.compare(name="shop")
        assert report.old is None and len(report.added) == 4

        second = history.start_run("shop")
        history.observe(second, response("/p/1", product_page("P1", 8.0)), items=[{"n": 1}])  # price drop
        history.observe(second, response("/p/2", product_page("P2", 20.0)), items=[{"n": 2}])  # unchanged
        history.observe_status(second, "https://shop.example/p/3", 404)  # gone
        history.mark_skipped(second, "https://shop.example/p/4")  # still fresh
        history.observe(second, response("/p/5", product_page("P5", 50.0)))  # new
        history.finish_run(second, "finished")
        report = history.compare(name="shop")
        assert report.old.id == first and report.new.id == second
        assert report.added == ["https://shop.example/p/5"] and report.removed == ["https://shop.example/p/3"]
        assert report.skipped == ["https://shop.example/p/4"] and report.unchanged == 1
        [price] = report.modified
        assert price.url == "https://shop.example/p/1" and price.details["price"] == [10.0, 8.0]
        assert report.counts() == {"added": 1, "removed": 1, "modified": 1, "unchanged": 1, "missing": 0, "skipped": 1}
        assert report.summary().splitlines()[:4] == [
            "+ 1 page",
            "- 1 page",
            "~ 1 page modified (text 1, structured-data 1, price 1)",
            "= 1 page unchanged",
        ]
        assert report.to_dict()["modified_pages"][0]["kinds"] == ["text", "structured-data", "price"]

        # a run that stopped early: pages it did not reach are "not reached", not "removed"
        third = history.start_run("shop")
        history.observe(third, response("/p/1", product_page("P1", 8.0)))
        history.finish_run(third, "limit")
        partial = history.compare(name="shop")
        assert partial.removed == [] and "https://shop.example/p/2" in partial.missing
        # /p/4 was skipped in run 2: it still compares with what run 1 saw
        fourth = history.start_run("shop")
        history.observe(fourth, response("/p/4", product_page("P4", 45.0)))
        history.finish_run(fourth, "limit")
        assert history.compare(name="shop").modified[0].details["price"] == [40.0, 45.0]
        assert history.compare(first, second).counts() == report.counts()

        assert [r.status for r in history.runs("shop")] == ["finished", "finished", "limit", "limit"]
        assert history.runs("other") == []
        assert [run for run, _ in history.snapshots("https://shop.example/p/1")] == [first, second, third]
        assert "<h1>P1</h1>" in history.page_html(first, "https://shop.example/p/1")
        assert history.page_items(first, "https://shop.example/p/1") == [{"n": 1}]
        with pytest.raises(ConfigurationError, match="no run 99"):
            history.run(99)
    with pytest.raises(ConfigurationError, match="no runs"):
        PageHistory(tmp_path / "empty.history").compare()
    (tmp_path / "junk.history").write_bytes(b"not a database, not at all" * 100)
    with pytest.raises(ConfigurationError, match="not a history file"):
        PageHistory(tmp_path / "junk.history")


def test_freshness(tmp_path) -> None:
    history = PageHistory(tmp_path / "h.history")
    url = "https://shop.example/p/1"
    start = 1_700_000_000.0
    for day in range(11):  # fetched every day for ten days, changed every other day
        run = history.start_run("shop")
        price = 10.0 + (day // 2)
        history.observe(run, response("/p/1", product_page("P", price)), fetched_at=start + day * DAY)
        history.finish_run(run, "finished")
    info = history.freshness(url, now=start + 11 * DAY)
    assert (info.observations, info.changes, info.last_changed) == (11, 5, start + 10 * DAY)
    # Cho & Garcia-Molina with half a change added: -ln((n - X + 0.5) / (n + 0.5)) per mean interval
    rate = -math.log((11 - 5.5 + 0.5) / (11 + 0.5))
    assert info.rate == pytest.approx(rate)
    assert info.recrawl_after == pytest.approx(round(math.log(2) / rate * DAY, 1))
    assert info.fresh == pytest.approx(round(math.exp(-rate), 4))
    assert info.due(start + 12 * DAY) and not info.due(start + 11 * DAY)  # after ln(2) / rate: about 1.07 days
    assert history.due(url, now=start + 12 * DAY) and history.due("https://shop.example/never-seen")

    steady = "https://shop.example/p/2"
    for day in range(30):  # never changes: fetch it rarely
        run = history.start_run("shop")
        history.observe(run, response("/p/2", product_page("Q", 5.0)), fetched_at=start + day * DAY)
    slow = history.freshness(steady, now=start + 30 * DAY)
    assert slow.changes == 0 and slow.recrawl_after > 30 * DAY / 2
    once = PageHistory(tmp_path / "once.history")
    run = once.start_run("x")
    once.observe(run, response("/p/3", product_page("R", 1.0)), fetched_at=start)
    first = once.freshness("https://shop.example/p/3", now=start)
    assert first.rate is None and first.recrawl_after == DAY and first.fresh == 1.0
    assert once.freshness("https://shop.example/unknown") is None
    with pytest.raises(ConfigurationError, match="target"):
        PageHistory(tmp_path / "x.history", target=1.5)


# ------------------------------------------------------------------------------------------ crawls


class _Site(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.pages: dict[str, tuple[int, str]] = {}
        self.hits: dict[str, int] = {}
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Handler(BaseHTTPRequestHandler):
    server: _Site
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        self.server.hits[path] = self.server.hits.get(path, 0) + 1
        status, body = self.server.pages.get(path, (404, "<h1>Not found</h1>"))
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def shop():
    site = _Site()
    links = "".join(f"<a href='/p/{i}'>P{i}</a>" for i in range(1, 5))
    site.pages = {"/": (200, f"<html><body>{links}</body></html>")}
    site.pages.update({f"/p/{i}": (200, product_page(f"Product {i}", 10.0 * i)) for i in range(1, 5)})
    yield site
    site.shutdown()
    site.server_close()


class ShopSpider(Spider):
    name = "shop"
    obey_robots_txt = False
    autothrottle = False
    log_level = None
    progress = False

    def parse(self, response):
        for link in response.css("a::attr(href)").getall():
            yield response.follow(link, callback=self.parse_product)

    def parse_product(self, response):
        yield {"url": response.url, "name": response.css("h1::text").get(), "price": response.css(".price::text").get()}


def test_spiders_report_changes(shop, tmp_path) -> None:
    history = str(tmp_path / "shop.history")
    events: list[dict] = []
    first = ShopSpider(start_urls=[shop.url + "/"], history=history).run()
    assert first.changes.old is None and len(first.changes.added) == 5
    shop.pages["/p/1"] = (200, product_page("Product 1", 8.0))
    shop.pages["/p/2"] = (200, product_page("Product 2", 20.0, "OutOfStock"))
    shop.pages["/p/3"] = (404, "gone")
    shop.pages["/p/5"] = (200, product_page("Product 5", 50.0))
    shop.pages["/"] = (200, shop.pages["/"][1].replace("</body>", "<a href='/p/5'>P5</a></body>"))
    spider = ShopSpider(start_urls=[shop.url + "/"], history=history)
    spider.events.subscribe(events.append, "changes_detected")
    second = spider.run()
    changes = second.changes
    assert changes.added == [shop.url + "/p/5"] and changes.removed == [shop.url + "/p/3"]
    by_url = {change.url: change for change in changes.modified}
    assert by_url[shop.url + "/p/1"].details["price"] == [10.0, 8.0] and "items" in by_url[shop.url + "/p/1"].kinds
    assert by_url[shop.url + "/p/2"].details["availability"] == ["InStock", "OutOfStock"]
    assert second.stats["changes"] == changes.counts()
    [event] = events
    assert event["added"] == 1 and event["kinds"]["price"] == 1

    # the product pages were just fetched twice: they are not due again for a while
    hits = dict(shop.hits)
    third = ShopSpider(start_urls=[shop.url + "/"], history=history, skip_fresh=True).run()
    assert third.stats["history_skipped"] == 5 and shop.hits["/"] == hits["/"] + 1
    assert shop.hits["/p/1"] == hits["/p/1"]  # not fetched
    assert third.changes.counts()["skipped"] == 4 and third.changes.removed == []
    with pytest.raises(ConfigurationError, match="needs a history"):
        ShopSpider(start_urls=[shop.url + "/"], skip_fresh=True).run()


def test_resumed_crawls_continue_their_history_run(shop, tmp_path) -> None:
    history, crawl_dir = str(tmp_path / "shop.history"), str(tmp_path / "state")
    partial = ShopSpider(start_urls=[shop.url + "/"], history=history, crawl_dir=crawl_dir, max_pages=2).run()
    assert partial.status == "limit" and partial.changes.old is None
    finished = ShopSpider(start_urls=[shop.url + "/"], history=history, crawl_dir=crawl_dir).run()
    assert finished.status == "finished"
    with PageHistory(history) as h:
        [run] = h.runs("shop")  # the resumed crawl kept recording into the same run
        assert run.pages == 5 and run.status == "finished"


def test_cli_history(shop, tmp_path, capsys) -> None:
    history = str(tmp_path / "shop.history")
    args = ["-q", "crawl", shop.url + "/", "--follow", "a", "--field", "name=h1::text", "--no-robots",
            "--no-autothrottle", "--history", history, "-o", str(tmp_path / "items.jsonl")]  # fmt: skip
    assert main(args) == 0
    shop.pages["/p/1"] = (200, product_page("Product 1", 8.0))
    assert main(args[1:]) == 0  # not quiet: the changes are printed
    err = capsys.readouterr().err
    assert "changes since run 1" in err and "~ 1 page modified" in err

    assert main(["history", history]) == 0
    out = capsys.readouterr().out
    assert "run 1 (" in out and "run 2 (" in out and "run 2 (" in out and "~ 1 page modified" in out
    assert main(["history", history, "--compare", "1", "2", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["changes"]["modified"] == 1
    assert main(["history", history, "--url", shop.url + "/p/1"]) == 0
    out = capsys.readouterr().out
    assert "first seen" in out and "changed: " in out and "price 8" in out and "look again after" in out
    assert main(["history", history, "--due"]) == 0
    assert "page(s) due" in capsys.readouterr().out
    assert main(["history", str(tmp_path / "missing.history")]) == 2
    assert main(["history", history, "--url", "https://elsewhere.example/"]) == 2


def test_history_errors_never_break_a_crawl(shop, tmp_path) -> None:
    class Broken(PageHistory):
        def observe(self, *args, **kwargs):
            raise RuntimeError("disk full")

        def observe_status(self, *args, **kwargs):
            raise RuntimeError("disk full")

    shop.pages["/p/4"] = (404, "gone")
    result = ShopSpider(start_urls=[shop.url + "/"], history=Broken(tmp_path / "b.history")).run()
    assert result.status == "finished" and result.stats["items"] == 3
    assert result.stats["history_errors"] == 5 and result.stats["http_errors"] == 1  # the 404 is still reported
