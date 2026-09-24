from __future__ import annotations

import pytest

import wintergrab as wg
from wintergrab import AsyncFetcher, CacheMiss, Fetcher, HTTPCache


@pytest.fixture
def cache_path(tmp_path):
    return tmp_path / "cache"


def test_prefer_mode_fetches_once(fresh_site, cache_path) -> None:
    with Fetcher(cache=cache_path, cache_mode="prefer") as fetcher:
        first = fetcher.get(fresh_site.url + "/product/1")
        second = fetcher.get(fresh_site.url + "/product/1")
    assert first.cache_status == "stored" and not first.from_cache
    assert second.cache_status == "hit" and second.from_cache
    assert second.css("h1::text").get() == "Product 1"
    assert second.status == 200 and second.headers["content-type"].startswith("text/html")
    assert fresh_site.site.hits["/product/1"] == 1


def test_offline_replay_and_miss(fresh_site, cache_path) -> None:
    wg.get(fresh_site.url + "/product/2", cache=cache_path, cache_mode="prefer")
    replay = wg.get(fresh_site.url + "/product/2", cache=cache_path, cache_mode="offline")
    assert replay.from_cache and replay.css("h1::text").get() == "Product 2"
    with pytest.raises(CacheMiss):
        wg.get(fresh_site.url + "/product/3", cache=cache_path, cache_mode="offline")
    assert fresh_site.site.hits["/product/3"] == 0


def test_revalidation_with_etag(fresh_site, cache_path) -> None:
    url = fresh_site.url + "/etag/a?v=1"
    with Fetcher(cache=cache_path) as fetcher:
        assert fetcher.get(url).cache_status == "stored"
        again = fetcher.get(url)  # no max-age: must revalidate -> 304 -> cached body
    assert again.cache_status == "revalidated"
    assert again.status == 200 and again.css("#v::text").get() == "1"
    assert fresh_site.site.hits["/etag/a"] == 2


def test_max_age_is_served_without_network(fresh_site, cache_path) -> None:
    with Fetcher(cache=cache_path) as fetcher:
        fetcher.get(fresh_site.url + "/maxage")
        hit = fetcher.get(fresh_site.url + "/maxage")
    assert hit.cache_status == "hit" and hit.css("#n::text").get() == "1"
    assert fresh_site.site.hits["/maxage"] == 1


def test_no_store_is_respected_when_revalidating(fresh_site, cache_path) -> None:
    with Fetcher(cache=cache_path) as fetcher:
        fetcher.get(fresh_site.url + "/nostore")
        again = fetcher.get(fresh_site.url + "/nostore")
    assert again.cache_status is None and again.css("#n::text").get() == "2"


def test_ttl_and_refresh(fresh_site, cache_path) -> None:
    url = fresh_site.url + "/nostore"
    cache = HTTPCache(cache_path, mode="prefer")
    wg.get(url, cache=cache)
    assert wg.get(url, cache=cache).css("#n::text").get() == "1"
    refreshing = HTTPCache(cache_path, mode="refresh")
    assert wg.get(url, cache=refreshing).css("#n::text").get() == "2"
    assert wg.get(url, cache=HTTPCache(cache_path, ttl=3600)).css("#n::text").get() == "2"
    assert len(cache) == 1


def test_only_get_and_good_statuses_are_cached(fresh_site, cache_path) -> None:
    cache = HTTPCache(cache_path, mode="prefer")
    with Fetcher(cache=cache) as fetcher:
        fetcher.post(fresh_site.url + "/post", data={"a": 1})
        fetcher.get(fresh_site.url + "/status/500", retries=0)
        fetcher.get(fresh_site.url + "/status/404")
    assert len(cache) == 1  # just the 404
    assert cache.stats["stored"] == 1


def test_cached_responses_keep_encoding_and_redirects(fresh_site, cache_path) -> None:
    wg.get(fresh_site.url + "/latin1", cache=cache_path, cache_mode="prefer")
    replay = wg.get(fresh_site.url + "/latin1", cache=cache_path, cache_mode="offline")
    assert replay.css("#t::text").get() == "Café crème"
    wg.get(fresh_site.url + "/redirect?to=/product/4", cache=cache_path, cache_mode="prefer")
    moved = wg.get(fresh_site.url + "/redirect?to=/product/4", cache=cache_path, cache_mode="offline")
    assert moved.url.endswith("/product/4") and moved.history


async def test_async_fetcher_cache(fresh_site, cache_path) -> None:
    async with AsyncFetcher(cache=cache_path, cache_mode="prefer") as fetcher:
        pages = await fetcher.get_many([fresh_site.url + f"/item/{i}" for i in range(5)])
        again = await fetcher.get_many([fresh_site.url + f"/item/{i}" for i in range(5)])
    assert all(p.cache_status == "stored" for p in pages)
    assert all(p.cache_status == "hit" for p in again)


def test_spider_offline_replay(fresh_site, tmp_path) -> None:
    class Catalog(wg.Spider):
        log_level = None
        start_urls = [fresh_site.url + "/products/page/1"]

        def parse(self, response):
            for link in response.css(".product .name a"):
                yield response.follow(link, callback=self.product)
            yield from response.follow_all("a.next")

        def product(self, response):
            yield {"name": response.css("h1::text").get(), "cached": response.from_cache}

    cache = str(tmp_path / "c")
    live = Catalog(cache=cache, cache_mode="prefer").run()
    hits_after_live = sum(fresh_site.site.hits.values())
    replay = Catalog(cache=cache, cache_mode="offline").run()
    assert len(live.items) == len(replay.items) == 20
    assert all(item["cached"] for item in replay.items)
    assert sum(fresh_site.site.hits.values()) == hits_after_live  # not a single request
    assert replay.stats["cache_hits"] >= 25


@pytest.mark.browser
def test_browser_cache(fresh_site, cache_path) -> None:
    with wg.BrowserFetcher(cache=cache_path, cache_mode="prefer") as browser:
        first = browser.get(fresh_site.url + "/js", wait_for=".item")
    with wg.BrowserFetcher(cache=cache_path, cache_mode="offline") as browser:
        replay = browser.get(fresh_site.url + "/js")
    assert first.cache_status == "stored"
    assert replay.cache_status == "hit" and replay.source == "browser"
    assert replay.css(".item::text").getall() == ["Item 1", "Item 2", "Item 3"]
    # Rendered pages are namespaced: the plain-HTTP cache doesn't see them.
    with pytest.raises(CacheMiss):
        wg.get(fresh_site.url + "/js", cache=cache_path, cache_mode="offline")
