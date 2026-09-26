"""Adaptive fetching: HTTP first, a browser for the pages that need JavaScript, learned per URL pattern."""

from __future__ import annotations

import json

import pytest

import wintergrab as wg
from wintergrab.errors import ConfigurationError
from wintergrab.fetchers.response import Response
from wintergrab.fetchers.strategy import FetchStrategy, PatternStats, needs_javascript


def html(body: str, head: str = "") -> Response:
    page = f"<html><head><title>t</title>{head}</head><body>{body}</body></html>"
    return Response("https://app.example/p", headers={"content-type": "text/html"}, body=page.encode())


NAV = "<nav><a href='/'>Home</a> <a href='/about'>About</a></nav>"
SCRIPTS = "<script src='a.js'></script><script src='b.js'></script><script src='c.js'></script>"
TEXT = "A great phone with a big screen and a long battery life. " * 10


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (f"{NAV}<div id='root'>Loading...</div><script>fetch('/api')</script>", "its app mount point (#root) is empty"),
        (f"<div id='__next'></div>{SCRIPTS}", "its app mount point (#__next) is empty"),
        ("<div data-reactroot=''></div>", "its app mount point (<div>) is empty"),
        ("<app-root></app-root><script src='main.js'></script>", "its app mount point (<app-root>) is empty"),
        ("<noscript><p>You need to enable JavaScript to run this app.</p></noscript><p>Hi</p>", "<noscript>"),
        (f"<p>Hi</p>{SCRIPTS}", "little text, and 3 scripts"),
        # the content is there
        (f"<div id='root'><h1>Phone X</h1><p>{TEXT}</p></div>", None),  # rendered on the server
        (f"<article>{TEXT * 3}</article><div id='app'></div>", None),  # a widget next to an article
        (f"<div id='app'><script>var x = 1</script>{TEXT}</div>", None),
        ("<h1>About</h1><p>We sell phones.</p>", None),  # short, but a plain page
        ("<div id='root'><p>API item 1</p><p>API item 2</p></div>", None),  # little, but not empty
        # the data is in the HTML: embedded state or JSON-LD
        (f"<div id='__next'></div><script id='__NEXT_DATA__' type='application/json'>{json.dumps({'x': 'y' * 3000})}</script>", None),
        (f"<div id='__nuxt'></div><script>window.__NUXT__ = {json.dumps({'x': 'y' * 3000})}</script>", None),
    ],
)  # fmt: skip
def test_needs_javascript(body: str, reason: str | None) -> None:
    found = needs_javascript(html(body))
    if reason is None:
        assert found is None
    else:
        assert found is not None and reason in found
    assert (
        needs_javascript(Response("https://a.example/x.json", headers={"content-type": "application/json"}, body=b"{}"))
        is None
    )


def test_fetch_strategy_learns_per_pattern(tmp_path) -> None:
    strategy = FetchStrategy(tmp_path / "fetch.json", probe_every=4)
    url = "https://app.example/search?q={}"
    assert not strategy.browser_first(url.format("a"))  # nothing known: HTTP first
    for word in ("a", "b"):
        strategy.record(url.format(word), "http", False)
    assert not strategy.browser_first(url.format("c"))  # two pages are not enough to decide
    strategy.record(url.format("c"), "http", False)
    # three pages needed a browser: the pattern goes to the browser, except one page in four (a probe)
    assert [strategy.browser_first(url.format(w)) for w in "defgh"] == [True, True, True, False, True]
    assert strategy.pattern(url.format("z")) == "app.example/search?q"
    # other patterns of the host keep trying HTTP until the host has nine pages of evidence
    assert not strategy.browser_first("https://app.example/p/123")
    for i in range(6):
        strategy.record(f"https://app.example/list/{i}", "http", False)
    assert strategy.browser_first("https://app.example/p/123")
    strategy.record("https://app.example/p/1", "http", True)
    for i in range(3):
        strategy.record(f"https://app.example/p/{i}", "http", True)
    assert not strategy.browser_first("https://app.example/p/9")  # its own pages were fine over HTTP

    strategy.record(url.format("x"), "browser", True)
    assert strategy.patterns["app.example/search?q"].describe() == (
        "HTTP enough for 0% of 3 page(s); browser: 1 page(s), 100% with content"
    )
    assert "app.example/search?q: HTTP enough for 0% of 3 page(s)" in strategy.describe()
    strategy.save()
    again = FetchStrategy(tmp_path / "fetch.json")
    assert again.patterns.keys() == strategy.patterns.keys() and again.hosts["app.example"].http_short == 9
    assert again.browser_first(url.format("y"))

    stats = PatternStats()
    for _ in range(250):
        stats.add("http_ok")
    assert stats.http_ok < 201  # old counts are halved: recent pages weigh more

    assert FetchStrategy.coerce(False) is None and isinstance(FetchStrategy.coerce(True), FetchStrategy)
    with pytest.raises(ConfigurationError):
        FetchStrategy.coerce(3)
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="cannot read fetch statistics"):
        FetchStrategy(tmp_path / "broken.json")
    with pytest.raises(ValueError):
        strategy.record(url, "carrier-pigeon", True)
    strategy.available = False
    assert not strategy.browser_first(url.format("q"))  # no browser: everything stays on HTTP


@pytest.mark.browser
def test_adaptive_crawl_renders_only_the_pages_that_need_it(fresh_site, tmp_path) -> None:
    site = fresh_site
    stats_file = tmp_path / "fetch.json"
    events: list[wg.Event] = []

    class Mixed(wg.Spider):
        log_level = None
        concurrency = 1
        adaptive_fetch = str(stats_file)
        start_urls = [site.url + f"/spa?n={i}" for i in range(1, 7)] + [site.url + f"/product/{i}" for i in range(1, 4)]

        def parse(self, response):
            yield {
                "url": response.url,
                "via": response.source,
                "rows": response.css(".row::text").getall(),
                "api": len(response.captured or []),
            }

    spider = Mixed()
    spider.events.subscribe(events.append, "browser_needed")
    result = spider.run(resume=False)
    items = {item["url"].split("/", 3)[3]: item for item in result.items}
    assert len(items) == 9
    assert all(items[f"product/{i}"]["via"] == "http" for i in range(1, 4))  # the HTML was enough
    for i in range(1, 7):
        page = items[f"spa?n={i}"]
        assert page["via"] == "browser" and page["rows"] and page["api"] == 2  # rendered, API calls recorded
    # the first three SPA pages were tried over HTTP; after that, the pattern went to the browser directly
    assert result.stats["adaptive/rendered"] == 3 and result.stats["adaptive/browser_first"] == 3
    assert [e.data["reason"] for e in events] == ["its app mount point (#root) is empty"] * 3
    assert site.site.hits["/spa"] == 9 and site.site.hits["/product/1"] == 1
    patterns = result.fetch_strategy.patterns
    assert patterns["127.0.0.1/spa?n"].http_short == 3 and patterns["127.0.0.1/spa?n"].browser_ok == 6
    assert patterns["127.0.0.1/product/{int}"].http_ok == 3

    # the next crawl knows: no SPA page is fetched over HTTP first
    before = site.site.hits["/spa"]
    again = Mixed().run(resume=False)
    assert site.site.hits["/spa"] - before == 6 and again.stats["adaptive/browser_first"] == 6
    assert "adaptive/rendered" not in again.stats

    # what a usable page has can be said with selectors; blocked pages are never rendered
    class Picky(wg.Spider):
        log_level = None
        adaptive_fetch = True
        render_if_missing = [".price-tag"]
        start_urls = [site.url + "/product/1", site.url + "/blocked"]
        retries = 0

        def parse(self, response):
            yield {"via": response.source}

    picky = Picky().run(resume=False)
    assert picky.items == [{"via": "browser"}] and picky.stats["adaptive/rendered"] == 1
    assert picky.fetch_strategy.patterns["127.0.0.1/product/{int}"].browser_short == 1  # still no .price-tag


def test_adaptive_fetch_without_a_browser_falls_back_to_http(site, monkeypatch) -> None:
    from wintergrab.errors import BrowserNotAvailable
    from wintergrab.fetchers.browser import AsyncBrowserFetcher

    async def no_browser(self, *args, **kwargs):
        raise BrowserNotAvailable("playwright is not installed")

    monkeypatch.setattr(AsyncBrowserFetcher, "request", no_browser)

    class Spa(wg.Spider):
        log_level = None
        adaptive_fetch = True
        start_urls = [site.url + "/spa?n=1"]

        def parse(self, response):
            yield {"via": response.source, "root": response.css("#root::text").get()}

    result = Spa().run(resume=False)
    assert result.status == "finished" and result.items == [{"via": "http", "root": "loading"}]
    assert result.fetch_strategy.available is False


def test_cli_adaptive_options(site, capsys, tmp_path) -> None:
    from wintergrab.cli import main

    assert main(["get", site.url + "/product/1", "--auto-browser", "--css", "h1::text"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "Product 1" and "browser" not in captured.err  # the HTML was enough
    # browser-only options (a page's API capture) are ignored by an HTTP fetch rather than failing it
    assert wg.get(site.url + "/product/1", capture=True, wait_until="networkidle").status == 200


@pytest.mark.browser
def test_cli_auto_browser(site, capsys, tmp_path) -> None:
    from wintergrab.cli import main

    assert main(["get", site.url + "/spa", "--auto-browser", "--css", ".row::text"]) == 0
    captured = capsys.readouterr()
    assert "API item" in captured.out
    assert "fetching it again in a browser (its app mount point (#root) is empty)" in captured.err

    stats = tmp_path / "fetch.json"
    args = [
        "crawl",
        site.url + "/spa",
        "--fetch-stats",
        str(stats),
        "--max-pages",
        "1",
        "-o",
        str(tmp_path / "o.jsonl"),
    ]
    assert main(args) == 0  # the browser attempt is the same page: it runs although max_pages is reached
    assert "adaptive fetching: 1 page(s) fetched in a browser" in capsys.readouterr().err
    assert json.loads(stats.read_text(encoding="utf-8"))["patterns"]["127.0.0.1/spa"]["browser_ok"] == 1
    assert json.loads((tmp_path / "o.jsonl").read_text(encoding="utf-8"))["title"] == "SPA loaded"
