"""Several sessions in one spider: HTTP for most pages, a browser for the page that needs JavaScript.

* ``fast``: plain HTTP with a browser's TLS and headers - used for most pages.
* ``browser``: a headless browser - used for the page that builds its content
  with JavaScript, chosen per request with ``session="browser"``.

A page that is blocked is not sent to the browser to get past the block: it is
reported, and the spider slows down for that site (docs/responsible-access.md).

    python examples/08_sessions.py
"""

from wintergrab import AsyncBrowserFetcher, AsyncFetcher, Request, Spider


class MixedSpider(Spider):
    name = "mixed"
    start_urls = ["https://quotes.toscrape.com/"]
    js_url = "https://quotes.toscrape.com/js/"
    concurrency = 6

    def configure_sessions(self, sessions):
        sessions.add("fast", AsyncFetcher(impersonate="chrome", retries=0), default=True)
        # Sessions are lazy: the browser only starts if a request needs it.
        sessions.add("browser", AsyncBrowserFetcher(headless=True, max_pages=2, retries=0))

    def start_requests(self):
        yield from super().start_requests()
        # Route one request explicitly through the browser, waiting for content.
        yield Request(self.js_url, session="browser", options={"wait_for": ".quote"}, callback=self.parse)

    def parse(self, response):
        for quote in response.css(".quote"):
            yield {
                "text": quote.css(".text::text").get(),
                "via": response.request.session or "fast",
                "page": response.url,
            }


if __name__ == "__main__":
    result = MixedSpider(max_pages=5).run()
    for item in result.items[:5]:
        print(item)
    print(result.stats)
