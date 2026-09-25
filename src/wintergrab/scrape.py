"""One call from a listing URL to complete, typed records: :func:`scrape`.

::

    books = wintergrab.scrape("https://books.toscrape.com/", pages=None, deep=True)

reads every listing page, finds the records on each (no selectors), follows each
record to its own page, and merges what that page adds (stock count, identifiers,
category, description, specifications...). It is a normal :class:`Spider` inside,
so it is polite (robots.txt, AutoThrottle), concurrent, retrying and cacheable.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import urlsplit

from .request import Request
from .spider.spider import Spider

__all__ = ["AutoSpider", "merge_details", "scrape"]

# Keys whose value is long: kept at the end of a record so spreadsheets stay readable.
_TAIL = ("description", "image", "url")


def merge_details(record: Mapping[str, Any], details: Mapping[str, Any]) -> dict[str, Any]:
    """A listing record completed by its detail page.

    The detail page usually knows more (the full title, the exact stock count), so
    its values win; anything only the listing had is kept.
    """
    merged = dict(details)
    for key, value in record.items():
        if merged.get(key) in (None, "", [], {}):
            merged[key] = value
    tail = [k for k in _TAIL if k in merged]
    return {**{k: v for k, v in merged.items() if k not in tail}, **{k: merged[k] for k in tail}}


class AutoSpider(Spider):
    """Listing pages -> records via ``auto_extract``; with ``deep``, each record's own page too.

    Settings (besides every :class:`Spider` setting):
        pages: How many listing pages to read by following next-page links
            (``None``: all of them).
        deep: Follow each record's ``url`` and merge :meth:`~wintergrab.Response.extract_details`.
        clean: Typed values (``False``: raw text).

    If the start page has no list of records, it is treated as a single item's page.
    """

    name = "auto"
    pages: int | None = 1
    deep: bool = False
    clean: bool = True
    log_level = "WARNING"

    def __init__(self, **settings: Any) -> None:
        super().__init__(**settings)
        self._rank: dict[int, tuple[int, int]] = {}

    def start_requests(self) -> Iterator[Request]:
        for url in self.start_urls:
            yield Request(url, callback=self.parse, meta={"listing_page": 1})

    def _ranked(self, item: dict[str, Any], rank: tuple[int, int]) -> dict[str, Any]:
        self._rank[id(item)] = rank
        return item

    def parse(self, response: Any) -> Any:
        page = int(response.meta.get("listing_page", 1))
        records = response.auto_extract(clean=self.clean)
        if not records and page == 1:
            yield self._ranked(response.extract_details(clean=self.clean), (page, 0))
            return
        for index, record in enumerate(records):
            rank = (page, index)
            if self.deep and isinstance(record.get("url"), str):
                yield response.follow(
                    record["url"],
                    callback=self.parse_details,
                    errback=self.details_failed,
                    cb_kwargs={"record": record, "rank": list(rank)},
                )
            else:
                yield self._ranked(record, rank)
        if self.pages is None or page < self.pages:
            next_url = response.next_page()
            if next_url:
                yield response.follow(next_url, callback=self.parse, meta={"listing_page": page + 1})

    def parse_details(self, response: Any, record: dict[str, Any], rank: list[int]) -> Any:
        yield self._ranked(merge_details(record, response.extract_details(clean=self.clean)), (rank[0], rank[1]))

    def details_failed(self, request: Request, error: BaseException) -> Any:
        """Keep the listing record when its own page can't be fetched."""
        self.logger.warning("could not read %s (%s); keeping the listing record", request.url, error)
        rank = request.cb_kwargs.get("rank") or [0, 0]
        yield self._ranked(dict(request.cb_kwargs["record"]), (rank[0], rank[1]))

    def sorted_items(self, items: list[Any]) -> list[Any]:
        """Items in page order (the crawl finishes them in whatever order is fastest)."""
        last = (1 << 30, 0)
        return sorted(items, key=lambda item: self._rank.get(id(item), last))


def _site(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def scrape(
    url: str,
    *,
    pages: int | None = 1,
    deep: bool = False,
    max_items: int | None = None,
    clean: bool = True,
    **settings: Any,
) -> list[dict[str, Any]]:
    """Every record from a listing (products, results, posts), as typed dicts, without selectors.

    Args:
        url: A listing page (a product grid, search results, a blog index...). A single
            item's page works too: you get one record with :meth:`~wintergrab.Response.extract_details`.
        pages: Listing pages to read by following next-page links (``None``: all).
        deep: Also open each record's own page and merge what it adds.
        max_items: Stop after this many records.
        clean: Typed values: ``"£51.77"`` -> ``price=51.77, currency="GBP"`` (``False``: raw text).
        **settings: Any :class:`Spider` setting, e.g. ``output="books.csv"``, ``cache=".cache"``,
            ``concurrency=8``, ``use_browser=True``, ``proxies=[...]``.

    Returns:
        The records in page order.

    Example::

        books = wintergrab.scrape("https://books.toscrape.com/", pages=2, deep=True, output="books.csv")
        books[0]  # {"title": "A Light in the Attic", "price": 51.77, "currency": "GBP", "in_stock": True,
                  #  "stock": 22, "rating": 3.0, "upc": "a897fe39b1053632", "category": "Poetry", ...}
    """
    settings.setdefault("allowed_domains", [_site(url)])
    settings.setdefault("concurrency", 8)
    settings["keep_items"] = True
    # Written at the end, in page order - unless the crawl is resumable, then streamed.
    output = settings.pop("output", None) if not settings.get("crawl_dir") else None
    spider = AutoSpider(start_urls=[url], pages=pages, deep=deep, clean=clean, max_items=max_items, **settings)
    items = spider.sorted_items(spider.run().items)
    if output:
        from .spider.exporters import write_items

        write_items(output, items)
    return items
