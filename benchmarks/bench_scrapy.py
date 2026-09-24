"""Crawl the benchmark site with Scrapy (the same work as bench_wintergrab.py).

    python benchmarks/bench_scrapy.py --url http://127.0.0.1:PORT --concurrency 64 --reactor asyncio

Run it from a virtualenv that has Scrapy installed (not the wintergrab one).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from _common import ITEM_LINKS, ITEM_NAME, ITEM_PRICE, PAGE_LINKS, Meter, base_parser, check_item, emit, versions

REACTORS = {
    "asyncio": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
    "epoll": "twisted.internet.epollreactor.EPollReactor",
}


class Counter:
    listing_pages = 0
    items = 0
    bad_items = 0


class CountPipeline:
    """The equivalent of wintergrab's ``process_item``: count and validate."""

    def process_item(self, item: Any, spider: Any = None) -> Any:  # ``spider`` is passed by Scrapy < 2.14
        Counter.items += 1
        if not check_item(item):
            Counter.bad_items += 1
        return item


def make_spider_class(start_url: str):  # type: ignore[no-untyped-def]
    import scrapy

    class BenchSpider(scrapy.Spider):
        name = "bench"
        allowed_domains = [urlsplit(start_url).hostname or ""]

        async def start(self):  # type: ignore[no-untyped-def]
            # dont_filter=False (unlike start_urls) so /page/0 is deduplicated like in the other tools.
            yield scrapy.Request(start_url, dont_filter=False)

        def start_requests(self):  # type: ignore[no-untyped-def]  # Scrapy < 2.13
            yield scrapy.Request(start_url, dont_filter=False)

        def parse(self, response):  # type: ignore[no-untyped-def]
            Counter.listing_pages += 1
            for href in response.css(ITEM_LINKS).getall():
                yield response.follow(href, callback=self.parse_item)
            for href in response.css(PAGE_LINKS).getall():
                yield response.follow(href, callback=self.parse)

        def parse_item(self, response):  # type: ignore[no-untyped-def]
            yield {
                "name": response.css(ITEM_NAME).get(),
                "price": response.css(ITEM_PRICE).get(),
                "url": response.url,
            }

    return BenchSpider


def settings(concurrency: int, reactor: str, loop: str = "default") -> dict[str, Any]:
    extra = {"ASYNCIO_EVENT_LOOP": "uvloop.Loop"} if loop == "uvloop" else {}
    return {
        **extra,
        "CONCURRENT_REQUESTS": concurrency,
        "CONCURRENT_REQUESTS_PER_DOMAIN": concurrency,
        "CONCURRENT_REQUESTS_PER_IP": 0,
        "DOWNLOAD_DELAY": 0,
        "RANDOMIZE_DOWNLOAD_DELAY": False,
        "AUTOTHROTTLE_ENABLED": False,
        "ROBOTSTXT_OBEY": False,
        "HTTPCACHE_ENABLED": False,
        "TELNETCONSOLE_ENABLED": False,
        "LOG_LEVEL": "ERROR",
        "LOG_ENABLED": True,
        "TWISTED_REACTOR": REACTORS[reactor],
        "ITEM_PIPELINES": {CountPipeline: 100},
        # COOKIES_ENABLED, RETRY_ENABLED, dupefilter, compression: Scrapy defaults.
    }


def main() -> None:
    parser = base_parser(__doc__.splitlines()[0])
    parser.add_argument("--reactor", choices=sorted(REACTORS), default="asyncio")
    parser.add_argument("--loop", choices=["default", "uvloop"], default="default", help="asyncio reactor only")
    args = parser.parse_args()
    if args.loop == "uvloop" and args.reactor != "asyncio":
        parser.error("--loop uvloop needs --reactor asyncio")

    from scrapy.crawler import CrawlerProcess

    start = args.url.rstrip("/") + "/page/0"
    meter = Meter()  # like bench_wintergrab: crawler construction and component loading are included
    process = CrawlerProcess(settings(args.concurrency, args.reactor, args.loop))
    logging.getLogger("scrapy").setLevel(logging.ERROR)
    crawler = process.create_crawler(make_spider_class(start))
    process.crawl(crawler)
    process.start()
    stats = crawler.stats.get_stats() if crawler.stats else {}
    emit(
        meter.result(
            "scrapy"
            + ("" if args.reactor == "asyncio" else f"-{args.reactor}")
            + ("-uvloop" if args.loop == "uvloop" else ""),
            listing_pages=Counter.listing_pages,
            items=Counter.items,
            bad_items=Counter.bad_items,
            concurrency=args.concurrency,
            reactor=args.reactor,
            event_loop=args.loop,
            status=stats.get("finish_reason"),
            engine_pages=stats.get("response_received_count", 0),
            errors=stats.get("log_count/ERROR", 0) + stats.get("downloader/exception_count", 0),
            versions=versions("scrapy", "twisted", "parsel", "lxml", "uvloop"),
        )
    )


if __name__ == "__main__":
    main()
