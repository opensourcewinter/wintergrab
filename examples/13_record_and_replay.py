"""Keep a crawl's pages, change the spider, and see what the change does to the data,
without touching the network again.

    python examples/13_record_and_replay.py
    wintergrab crawl ... --record; wintergrab replay last      # the same from the command line
"""

from __future__ import annotations

import tempfile
from typing import Any

from wintergrab import Spider
from wintergrab.runs import ReplayResult, replay


class Books(Spider):
    """Titles and prices from the catalogue."""

    log_level = "WARNING"

    def parse(self, response: Any) -> Any:
        for book in response.css("article.product_pod"):
            yield {
                "url": response.urljoin(book.css("h3 a::attr(href)").get()),  # what a record is compared by
                "title": book.css("h3 a::attr(title)").get(),
                "price": book.css(".price_color::text").get(),
            }


class BooksWithNumbers(Books):
    """The same crawl after a change: prices as numbers."""

    def parse(self, response: Any) -> Any:
        for item in super().parse(response):
            item["price"] = float(item["price"].lstrip("£"))
            yield item


def main(start: str = "https://books.toscrape.com/", workspace: str | None = None) -> ReplayResult:
    workspace = workspace or tempfile.mkdtemp(prefix="wintergrab-")
    Books(start_urls=[start], record=True, run_registry=workspace).run()  # one page, kept
    result = replay("last", BooksWithNumbers, registry=workspace)  # offline, from the recording
    print(result.summary())
    return result


if __name__ == "__main__":
    main()
