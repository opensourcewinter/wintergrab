"""A large, long-running crawl: sitemap discovery, disk frontier, HTTP cache, SQLite upserts.

* Pages come from the site's sitemaps (listed in robots.txt).
* The queue lives on disk (flat memory, survives kill -9); Ctrl+C pauses.
* The HTTP cache revalidates, so the next run only re-downloads what changed.
  Run with --offline to replay the whole crawl from the cache.
* Items are upserted into SQLite on their URL, so re-crawls update rows.

    python examples/10_big_crawl.py
    python examples/10_big_crawl.py --offline
"""

import sys

from wintergrab import Spider


class ShopCrawl(Spider):
    name = "shop"
    sitemap_urls = ["https://books.toscrape.com/robots.txt"]
    start_urls = ["https://books.toscrape.com/"]  # also walk the listing, in case the sitemap is missing
    sitemap_rules = [(r"/catalogue/[^/]+/index\.html$", "parse_book")]

    frontier = "disk"
    crawl_dir = ".crawl/shop"
    cache = ".cache/shop"
    cache_mode = "revalidate"
    output = "shop.db"
    unique_key = "url"

    concurrency = 32
    concurrency_per_domain = 8
    max_pages = 500  # stay friendly while experimenting

    def parse(self, response):
        for link in response.css("article.product_pod h3 a"):
            yield response.follow(link, callback=self.parse_book)
        yield response.follow_next()

    def parse_book(self, response):
        product = response.css(".product_main")
        if not product:
            return
        yield {
            "url": response.url,
            "title": product.css("h1::text").get(),
            "price": product.css(".price_color::text").get(),
            "stock": product.css(".availability").re_first(r"(\d+) available"),
            "from_cache": response.from_cache,
        }


if __name__ == "__main__":
    offline = "--offline" in sys.argv
    result = ShopCrawl(cache_mode="offline" if offline else "revalidate").run()
    print(result)
    print({k: v for k, v in result.stats.items() if k.startswith(("cache", "pages", "items"))})
