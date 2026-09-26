from __future__ import annotations

import asyncio
import csv
import itertools
import json
from dataclasses import dataclass

import pytest

import wintergrab as wg
from wintergrab import AsyncFetcher, ProxyRotator, Request, Spider
from wintergrab.errors import ConfigurationError


class ProductSpider(Spider):
    log_level = None

    def parse(self, response):
        for card in response.css(".product"):
            yield response.follow(
                card.css(".name a")[0], callback=self.parse_product, meta={"list_price": card.css(".price::text").get()}
            )
        next_link = response.css("a.next")
        if next_link:
            yield response.follow(next_link[0])

    async def parse_product(self, response):
        yield {
            "name": response.css("h1::text").get(),
            "price": float(response.css(".price::text").get().strip("$")),
            "list_price": response.meta["list_price"],
            "depth": response.meta["depth"],
        }
        yield response.follow(response.url)  # a duplicate: filtered out


def product_spider(site, **kw) -> ProductSpider:
    return ProductSpider(start_urls=[site.url + "/products/page/1"], **kw)


def test_crawl_follows_pagination_and_details(site) -> None:
    result = product_spider(site).run()
    assert result.status == "finished" and result.finished
    assert len(result.items) == 20
    names = sorted(i["name"] for i in result.items)
    assert names[0] == "Product 1" and len(set(names)) == 20
    first = next(i for i in result.items if i["name"] == "Product 1")
    assert first["price"] == 6.25 and first["list_price"] == "$6.25"
    assert first["depth"] == 1
    assert result.stats["pages"] == 25
    assert result.stats["duplicates_filtered"] >= 20
    assert result.stats["status/200"] == 25


def test_callback_styles(site) -> None:
    class Styles(Spider):
        log_level = None

        def start_requests(self):
            yield Request(site.url + "/product/1", callback=self.returns_dict)
            yield Request(site.url + "/product/2", callback="returns_list")
            yield Request(site.url + "/product/3", callback=self.async_returns)
            yield site.url + "/product/4"  # plain URL -> parse()

        def returns_dict(self, response):
            return {"style": "dict"}

        def returns_list(self, response):
            return [{"style": "list"}, {"style": "list"}]

        async def async_returns(self, response):
            await asyncio.sleep(0)
            return {"style": "async"}

        async def parse(self, response):
            yield {"style": "asyncgen"}

    styles = sorted(i["style"] for i in Styles().run().items)
    assert styles == ["async", "asyncgen", "dict", "list", "list"]


def test_items_can_be_dataclasses_and_are_processed(site, tmp_path) -> None:
    @dataclass
    class Product:
        name: str

    class Pipeline(Spider):
        log_level = None
        start_urls = [site.url + "/product/1", site.url + "/product/2"]

        def parse(self, response):
            yield Product(response.css("h1::text").get())

        async def process_item(self, item):
            return None if item.name.endswith("2") else item

    out = tmp_path / "items.jsonl"
    result = Pipeline(output=str(out)).run()
    assert result.items == [Product("Product 1")]
    assert result.stats["items_dropped"] == 1
    assert [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()] == [{"name": "Product 1"}]


@pytest.mark.parametrize("suffix", [".jsonl", ".json", ".csv"])
def test_output_formats(site, tmp_path, suffix) -> None:
    out = tmp_path / f"items{suffix}"
    product_spider(site, output=str(out)).run()
    text = out.read_text(encoding="utf-8")
    if suffix == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines()]
    elif suffix == ".json":
        rows = json.loads(text)
    else:
        rows = list(csv.DictReader(text.splitlines()))
    assert len(rows) == 20
    assert {"name", "price", "list_price", "depth"} <= set(rows[0])


def test_limits(site) -> None:
    assert product_spider(site, max_pages=3).run().stats["pages"] == 3
    result = product_spider(site, max_items=5, concurrency=1).run()
    assert result.status == "limit" and len(result.items) == 5

    class Deep(Spider):
        log_level = None
        start_urls = [site.url + "/deep/0"]

        def parse(self, response):
            yield {"url": response.url}
            yield response.follow(response.css("a[href^='/deep/']")[0])

    assert len(Deep(max_depth=3).run().items) == 4


def test_robots_and_offsite(site) -> None:
    class Links(Spider):
        log_level = None
        start_urls = [site.url + "/links?n=5"]
        allowed_domains = ["127.0.0.1"]

        def parse(self, response):
            yield {"url": response.url}
            if "links" in response.url:
                yield from response.follow_all()

    polite = Links().run()
    assert polite.stats["robots_blocked"] == 1
    assert polite.stats["offsite_filtered"] == 1
    assert not any("/private/" in i["url"] for i in polite.items)
    rude = Links(obey_robots_txt=False).run()
    assert any("/private/" in i["url"] for i in rude.items)

    class Told(Spider):  # an errback hears of the refusal, and may do without the page
        log_level = None

        def start_requests(self):
            yield Request(site.url + "/private/1", errback="refused")

        def refused(self, request, error):
            yield {"refused": request.url, "why": type(error).__name__, "policy": error.policy}
            yield Request(site.url + "/links?n=1")

    told = Told().run()
    assert told.items[0] == {"refused": site.url + "/private/1", "why": "RobotsPolicyError", "policy": "robots"}
    assert told.stats["robots_blocked"] == 1 and told.stats["pages"] >= 1


def test_retries_and_errors(fresh_site) -> None:
    errors: list[str] = []

    class Flaky(Spider):
        log_level = None
        retries = 2

        def start_requests(self):
            yield Request(fresh_site.url + "/flaky/a?fail=2")
            yield Request(fresh_site.url + "/flaky/b?fail=9")
            yield Request(fresh_site.url + "/status/404", errback=self.failed)
            yield Request("http://127.0.0.1:9/unreachable", errback=self.failed)

        def parse(self, response):
            yield {"url": response.url}

        def failed(self, request, error):
            errors.append(f"{request.url} {type(error).__name__}")
            if request.url.endswith("/unreachable"):
                assert isinstance(error, wg.FetchError) and error.kind == "connect"

        def on_error(self, request, error):
            errors.append(f"on_error {request.url}")

    result = Flaky(autothrottle=False, obey_robots_txt=False).run()
    assert [i["url"] for i in result.items] == [fresh_site.url + "/flaky/a?fail=2"]
    assert fresh_site.site.hits["/flaky/b"] == 3  # 1 try + 2 retries
    assert result.stats["retries"] >= 4
    assert f"on_error {fresh_site.url}/flaky/b?fail=9" in errors
    assert f"{fresh_site.url}/status/404 HTTPStatusError" in errors
    assert "http://127.0.0.1:9/unreachable NetworkError" in errors  # a FetchError subclass


def test_allowed_statuses_reach_the_callback(site) -> None:
    class NotFound(Spider):
        log_level = None
        start_urls = [site.url + "/status/404"]
        allowed_statuses = {404}

        def parse(self, response):
            yield {"status": response.status}

    assert NotFound().run().items == [{"status": 404}]


def test_rate_limit_triggers_backoff(fresh_site) -> None:
    class Limited(Spider):
        log_level = None
        start_urls = [fresh_site.url + "/ratelimited/k?limit=2&after=0.2"]

        def parse(self, response):
            yield {"attempt": response.css("#attempt::text").get()}

    spider = Limited()
    result = spider.run()
    assert result.items == [{"attempt": "3"}]
    assert result.stats["backoffs"] == 2
    assert result.stats["status/429"] == 2


def test_a_blocked_page_is_reported_not_fetched_another_way(site) -> None:
    class Guarded(Spider):
        log_level = None
        start_urls = [site.url + "/guarded"]
        retries = 1

        def configure_sessions(self, sessions):
            super().configure_sessions(sessions)
            sessions.add("other", AsyncFetcher(headers={"X-Solved": "yes"}, retries=0))  # would get through

        def parse(self, response):
            yield {"text": response.css("#real::text").get()}

    result = Guarded().run()
    assert result.items == []  # asked twice, the same way, then given up on
    assert (result.stats["blocked"], result.stats["backoffs"], result.stats.get("status/403")) == (2, 2, 2)
    assert "likely cause: bot protection" in result.failure_report()  # reported, as likely, not certain
    # the setting that fetched blocked pages another way is gone, and says so
    with pytest.raises(ConfigurationError, match="fallback_session was removed: a blocked or rate-limited page"):
        type("Old", (Spider,), {"fallback_session": "browser"})()
    with pytest.raises(ConfigurationError, match="fallback_session was removed"):
        Spider(fallback_session="browser")


def test_a_refused_page_is_asked_again_through_the_same_proxy(fresh_site, proxies) -> None:
    class Limited(Spider):
        log_level = None
        obey_robots_txt = False
        start_urls = [fresh_site.url + "/ratelimited/pin?limit=1&after=0"]

        def parse(self, response):
            yield {"proxy": response.headers.get("x-via-proxy"), "attempt": response.css("#attempt::text").get()}

    rotator = ProxyRotator([p.url for p in proxies])
    result = Limited(proxies=rotator).run()
    assert result.items == [{"proxy": "p1", "attempt": "2"}]  # a 429 through p1: p1 again, after the pause
    assert [(s["failures"], s["uses"]) for s in rotator.stats()] == [(0, 2), (0, 0)]  # and not held against it


def test_requests_can_pick_a_session(site) -> None:
    class TwoSessions(Spider):
        log_level = None

        def configure_sessions(self, sessions):
            sessions.add("a", AsyncFetcher(headers={"X-Session": "a"}, retries=0), default=True)
            sessions.add("b", AsyncFetcher(headers={"X-Session": "b"}, retries=0))

        def start_requests(self):
            yield Request(site.url + "/headers?1")
            yield Request(site.url + "/headers?2", session="b")

        def parse(self, response):
            yield {"session": response.json()["X-Session"]}

    assert sorted(i["session"] for i in TwoSessions().run().items) == ["a", "b"]


def test_unknown_session_is_fatal(site) -> None:
    class Bad(Spider):
        log_level = None

        def start_requests(self):
            yield Request(site.url + "/product/1", session="nope")

    with pytest.raises(LookupError):
        Bad().run()


def test_proxies_rotate_in_spiders(site, proxies) -> None:
    class ViaProxy(Spider):
        log_level = None
        start_urls = [site.url + f"/product/{i}" for i in range(1, 5)]
        concurrency = 1

        def parse(self, response):
            yield {"proxy": response.headers.get("x-via-proxy")}

    result = ViaProxy(proxies=[p.url for p in proxies], obey_robots_txt=False).run()
    assert sorted(i["proxy"] for i in result.items) == ["p1", "p1", "p2", "p2"]


def test_per_domain_concurrency_limit(fresh_site) -> None:
    class Slow(Spider):
        log_level = None
        start_urls = [fresh_site.url + f"/item/{i}?delay=0.15" for i in range(8)]
        concurrency = 8
        concurrency_per_domain = 2

        def parse(self, response):
            yield {"u": response.url}

    assert len(Slow().run().items) == 8
    assert fresh_site.site.max_active == 2


def test_download_delay_spaces_requests(fresh_site) -> None:
    class Polite(Spider):
        log_level = None
        start_urls = [fresh_site.url + f"/item/{i}" for i in range(4)]
        download_delay = 0.2
        obey_robots_txt = False

        def parse(self, response):
            yield {}

    Polite(autothrottle=False).run()
    times = [t for t, path, _ in fresh_site.site.log if path.startswith("/item/")]
    gaps = [b - a for a, b in itertools.pairwise(times)]
    assert len(gaps) == 3 and min(gaps) >= 0.09  # 0.2s +/- 50% jitter


def test_stream_items(site) -> None:
    async def collect():
        return [item async for item in product_spider(site).stream()]

    items = asyncio.run(collect())
    assert len(items) == 20


def test_stream_can_stop_early(site) -> None:
    async def first_three():
        out = []
        async for item in product_spider(site, concurrency=2).stream():
            out.append(item)
            if len(out) == 3:
                break
        return out

    assert len(asyncio.run(first_three())) == 3


async def test_run_inside_a_running_loop(site) -> None:
    result = product_spider(site, max_pages=2).run()  # uses a helper thread
    assert result.stats["pages"] == 2
    again = await product_spider(site, max_pages=2).arun()
    assert again.stats["pages"] == 2


def test_stop_from_a_callback(site) -> None:
    class Stopper(ProductSpider):
        async def parse_product(self, response):
            self.stop()
            yield {"name": "x"}

    result = Stopper(start_urls=[site.url + "/products/page/1"], concurrency=1).run()
    assert result.status == "stopped"
    assert len(result.items) < 20


def test_unknown_setting_is_rejected() -> None:
    with pytest.raises(TypeError, match="no setting 'concurrancy'"):
        ProductSpider(concurrancy=3)
    with pytest.raises(TypeError):
        ProductSpider(parse=None)


def test_request_basics() -> None:
    req = Request("https://x.test/a?b=2&a=1#frag", params={"c": 3})
    assert req.url == "https://x.test/a?b=2&a=1&c=3#frag"
    assert req.fingerprint() == Request("https://X.test:443/a?a=1&c=3&b=2").fingerprint()
    assert req.fingerprint() != Request("https://x.test/a?a=1&c=3&b=2", method="POST").fingerprint()
    assert req.depth == 0 and req.replace(meta={"depth": 2}).depth == 2
    with pytest.raises(ValueError):
        Request("/relative")


def test_crawl_result_save(site, tmp_path) -> None:
    result = product_spider(site, max_items=3, concurrency=1).run()
    path = result.save(tmp_path / "saved.json")
    assert len(json.loads(path.read_text(encoding="utf-8"))) == 3
    assert "CrawlResult" in repr(result)


@pytest.mark.browser
def test_a_check_that_lets_a_browser_through_by_itself(site) -> None:
    class Checked(Spider):
        log_level = None
        start_urls = [site.url + "/challenge"]
        use_browser = True

        def parse(self, response):
            yield {"text": response.css("#real::text").get(), "source": response.source}

    result = Checked().run()
    assert result.items == [{"text": "Real content", "source": "browser"}]


@pytest.mark.browser
def test_browser_default_session(site) -> None:
    class Rendered(Spider):
        log_level = None
        start_urls = [site.url + "/js"]
        use_browser = True

        def parse(self, response):
            yield {"items": response.css(".item::text").getall()}

    class Waiting(Rendered):
        def start_requests(self):
            yield Request(site.url + "/js", options={"wait_for": ".item"})

    assert Waiting().run().items == [{"items": ["Item 1", "Item 2", "Item 3"]}]


def test_wg_namespace_exports() -> None:
    for name in ("get", "aget", "render", "Spider", "Request", "Field", "ProxyRotator", "AutoThrottle"):
        assert hasattr(wg, name)
