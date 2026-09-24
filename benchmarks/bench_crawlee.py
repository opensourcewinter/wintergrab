"""Crawl the benchmark site with Crawlee's ParselCrawler (the same work as bench_wintergrab.py).

    python benchmarks/bench_crawlee.py --url http://127.0.0.1:PORT --concurrency 64

Run it from a virtualenv that has ``crawlee[parsel]`` installed. Uses Crawlee's
default HTTP client and an in-memory storage client (no request queue or
dataset files on disk, like the other tools).
"""

from __future__ import annotations

import asyncio
import logging

from _common import ITEM_LINKS, ITEM_NAME, ITEM_PRICE, PAGE_LINKS, Meter, base_parser, check_item, emit, versions


def _element_selector(css: str) -> str:
    """Crawlee's enqueue_links takes an element selector and reads its href itself."""
    return css.replace("::attr(href)", "")


async def crawl(url: str, concurrency: int) -> dict[str, int | str]:
    from crawlee import ConcurrencySettings
    from crawlee.crawlers import ParselCrawler, ParselCrawlingContext
    from crawlee.storage_clients import MemoryStorageClient

    counts = {"listing_pages": 0, "items": 0, "bad_items": 0}
    crawler = ParselCrawler(
        concurrency_settings=ConcurrencySettings(
            min_concurrency=concurrency, desired_concurrency=concurrency, max_concurrency=concurrency
        ),
        storage_client=MemoryStorageClient(),
        configure_logging=False,
        max_request_retries=3,
    )

    @crawler.router.default_handler
    async def listing(context: ParselCrawlingContext) -> None:
        counts["listing_pages"] += 1
        await context.enqueue_links(selector=_element_selector(ITEM_LINKS), label="item")
        await context.enqueue_links(selector=_element_selector(PAGE_LINKS))

    @crawler.router.handler("item")
    async def item(context: ParselCrawlingContext) -> None:
        data = {
            "name": context.selector.css(ITEM_NAME).get(),
            "price": context.selector.css(ITEM_PRICE).get(),
            "url": context.request.url,
        }
        # The equivalent of a no-op item pipeline: count and validate.
        counts["items"] += 1
        if not check_item(data):
            counts["bad_items"] += 1

    stats = await crawler.run([url.rstrip("/") + "/page/0"])
    counts["failed"] = stats.requests_failed
    counts["http_client"] = type(crawler._http_client).__name__
    return counts


def main() -> None:
    parser = base_parser(__doc__.splitlines()[0])
    parser.add_argument("--loop", choices=["asyncio", "uvloop"], default="asyncio")
    args = parser.parse_args()
    for name in ("crawlee", "httpx", "impit", "curl_cffi"):
        logging.getLogger(name).setLevel(logging.ERROR)
    meter = Meter()
    if args.loop == "uvloop":
        import uvloop

        counts = uvloop.run(crawl(args.url, args.concurrency))
    else:
        counts = asyncio.run(crawl(args.url, args.concurrency))
    emit(
        meter.result(
            "crawlee" + ("-uvloop" if args.loop == "uvloop" else ""),
            listing_pages=int(counts["listing_pages"]),
            items=int(counts["items"]),
            bad_items=int(counts["bad_items"]),
            concurrency=args.concurrency,
            errors=counts["failed"],
            http_client=counts["http_client"],
            event_loop=args.loop,
            versions=versions("crawlee", "impit", "parsel", "lxml", "uvloop"),
        )
    )


if __name__ == "__main__":
    main()
