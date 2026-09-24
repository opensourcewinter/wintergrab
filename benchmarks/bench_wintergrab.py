"""Crawl the benchmark site with a ``wintergrab.Spider`` and print one RESULT line.

python benchmarks/bench_wintergrab.py --url http://127.0.0.1:PORT --concurrency 64 --impersonate chrome
"""

from __future__ import annotations

import sys
from typing import Any
from urllib.parse import urlsplit

from _common import ITEM_LINKS, ITEM_NAME, ITEM_PRICE, PAGE_LINKS, Meter, base_parser, check_item, emit, versions

from wintergrab import Spider


class BenchSpider(Spider):
    name = "bench"
    autothrottle = False
    obey_robots_txt = False
    keep_items = False
    log_level = None
    download_delay = 0.0
    cache = None  # no HTTP cache (setting exists in newer wintergrab versions)

    def __init__(self, **overrides: Any) -> None:
        super().__init__(**overrides)
        self.listing_pages = 0
        self.item_count = 0
        self.bad_items = 0

    def parse(self, response):  # type: ignore[no-untyped-def]
        self.listing_pages += 1
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

    def process_item(self, item: Any) -> Any:
        self.item_count += 1
        if not check_item(item):
            self.bad_items += 1
        return item


def make_spider(url: str, concurrency: int, impersonate: str | None, loop: str = "default") -> BenchSpider:
    start = url.rstrip("/") + "/page/0"
    overrides: dict[str, Any] = {}
    if loop == "asyncio" and hasattr(Spider, "use_uvloop"):
        overrides["use_uvloop"] = False  # wintergrab picks uvloop by default when it is installed
    return BenchSpider(
        start_urls=[start],
        allowed_domains=[urlsplit(start).hostname or ""],
        concurrency=concurrency,
        concurrency_per_domain=concurrency,
        impersonate=impersonate,
        **overrides,
    )


def main() -> None:
    parser = base_parser(__doc__.splitlines()[0])
    parser.add_argument("--impersonate", default="chrome", help='browser to impersonate, or "none" for plain curl')
    parser.add_argument(
        "--loop",
        choices=["default", "asyncio"],
        default="default",
        help="default = wintergrab's choice (uvloop if installed)",
    )
    args = parser.parse_args()
    impersonate = None if args.impersonate.lower() in ("none", "") else args.impersonate

    spider = make_spider(args.url, args.concurrency, impersonate, args.loop)
    meter = Meter()
    result = spider.run()
    stats = result.stats
    emit(
        meter.result(
            ("wintergrab" if impersonate else "wintergrab-noimp") + ("-asyncio" if args.loop == "asyncio" else ""),
            listing_pages=spider.listing_pages,
            items=spider.item_count,
            bad_items=spider.bad_items,
            concurrency=args.concurrency,
            impersonate=impersonate,
            event_loop="uvloop" if "uvloop" in sys.modules else "asyncio",
            status=result.status,
            engine_pages=stats.get("pages", 0),
            errors=stats.get("errors", 0) + stats.get("callback_errors", 0) + stats.get("failed", 0),
            versions=versions("wintergrab", "curl_cffi", "lxml", "cssselect", "uvloop"),
        )
    )


if __name__ == "__main__":
    main()
