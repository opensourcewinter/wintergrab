"""A bigger, resumable crawl with adaptive speed control.

* ``crawl_dir`` makes it resumable: press Ctrl+C to pause, run again to resume.
* AutoThrottle backs off when the site returns 429/503 or a block page.
* Items stream to ``books.jsonl`` as they are scraped.
* Set ``PROXIES=http://p1:8000,http://p2:8000`` to rotate proxies.

    python examples/06_books_resumable.py
    python examples/06_books_resumable.py --fresh   # ignore saved progress
"""

import os
import sys

from wintergrab import Field, Spider

RATINGS = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}

BOOK = {
    "title": ".product_main h1::text",
    "price": Field(".product_main .price_color::text", transform=lambda p: float(p.lstrip("£"))),
    "stock": Field(".product_main .availability", regex=r"(\d+) available", transform=int, default=0),
    "rating": Field(".product_main .star-rating", attr="class", regex=r"star-rating (\w+)", transform=RATINGS.get),
    "upc": "//th[text()='UPC']/following-sibling::td/text()",
    "category": "ul.breadcrumb li:nth-last-child(2) a::text",
}


class BooksSpider(Spider):
    name = "books"
    start_urls = ["https://books.toscrape.com/"]
    allowed_domains = ["books.toscrape.com"]

    # Speed: up to 16 requests at once, 4 per domain, adapting to the site.
    concurrency = 16
    concurrency_per_domain = 4
    autothrottle = True
    retries = 3

    # State and output.
    crawl_dir = ".crawl/books"
    output = "books.jsonl"
    max_pages = 200  # stay friendly while experimenting

    proxies = [p for p in os.environ.get("PROXIES", "").split(",") if p] or None

    def parse(self, response):
        for link in response.css("article.product_pod h3 a"):
            # Product pages first so items flow early; listing pages keep coming.
            yield response.follow(link, callback=self.parse_book, priority=1)
        next_page = response.css("li.next a")
        if next_page:
            yield response.follow(next_page[0])

    def parse_book(self, response):
        book = response.extract(BOOK)
        book["url"] = response.url
        yield book

    def process_item(self, item):
        # Drop anything that failed to parse; tidy the rest.
        if not item["title"]:
            return None
        item["title"] = item["title"].strip()
        return item


if __name__ == "__main__":
    result = BooksSpider().run(resume="--fresh" not in sys.argv)
    print(result)
    if result.paused:
        print("Paused. Run the same command again to continue.")
