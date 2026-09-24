# wintergrab

**Friendly web scraping that scales from one page to big crawls.**

```python
import wintergrab as wg

page = wg.get("https://quotes.toscrape.com/")
for quote in page.css(".quote"):
    print(quote.css(".text::text").get(), "-", quote.css(".author::text").get())
```

wintergrab is a Python toolkit for grabbing data from websites. Simple things
are one line. When a site is harder (JavaScript, bot checks, rate limits,
thousands of pages), the same API scales up.

- **Fetch like a real browser.** HTTP requests carry Chrome/Firefox/Safari
  TLS and HTTP/2 fingerprints (via [curl_cffi](https://github.com/lexiforest/curl_cffi)).
  A headless Chromium (via [Playwright](https://playwright.dev/python/)) is
  one flag away for JavaScript pages. It hides common automation tells and
  waits out "checking your browser" interstitials.
- **Parse with CSS or XPath.** Scrapy-style `::text` / `::attr(href)`,
  extraction schemas, search by text, "find similar elements", and
  HTML → Markdown/text conversion.
- **Selectors that adapt.** With `adaptive=True`, wintergrab remembers what a
  selector matched. After a redesign that breaks it, it finds the most
  similar elements on the new page.
- **Crawl at scale.** Async spiders with concurrency limits, multiple
  sessions (HTTP + browser, several accounts…), proxy rotation with health
  checks, **AutoThrottle** that backs off when a site pushes back,
  robots.txt support, and **pause/resume** (Ctrl+C, then run again).
- **Scrape without selectors.** Pull JSON-LD/microdata/OpenGraph, the JSON
  state that React/Next/Vue apps embed in their HTML, and every table.
  `auto_extract()` finds a page's product grid or result list and names the
  fields. `learn({"title": "…", "price": "…"})` writes the selectors for you
  from values you can see on the page.
- **Built for big, long crawls.** An HTTP cache that revalidates with `304`s
  and replays whole crawls offline. A disk-backed queue with a Bloom filter
  that keeps memory flat at millions of URLs and survives `kill -9`. Sitemap
  crawling, SQLite output with upserts, and a live progress line.
- **Browser superpowers.** Capture the JSON API calls a page makes while it
  renders. Clear a login or JS check once in the browser, then continue over
  fast HTTP with the same cookies.
- **A small CLI.** `wintergrab get` and `wintergrab crawl` cover the common
  jobs with no code at all, including `--auto`, `--learn` and `--offline`.

## Install

```bash
pip install wintergrab                 # HTTP fetching, parsing, spiders, CLI
pip install "wintergrab[browser]"      # + headless browser support
pip install "wintergrab[speed]"        # + uvloop and orjson
playwright install chromium            # one-time browser download (browser extra only)
wintergrab doctor                      # check what is installed
```

Python 3.10+.

> Until the first PyPI release, install from GitHub:
> `pip install "wintergrab[browser] @ git+https://github.com/opensourcewinter/wintergrab"`

## A quick tour

### Fetch and parse

```python
import wintergrab as wg

page = wg.get("https://books.toscrape.com/")      # looks like Chrome, retries hiccups
page.status, page.title                           # (200, 'All products | Books to Scrape')

page.css("h3 a::attr(title)").getall()            # every title
page.css(".price_color::text").get()              # first price: '£51.77'
page.xpath("//p[contains(@class, 'star-rating')]/@class").get()

for book in page.css("article.product_pod"):      # loop and query inside
    print(book.css("h3 a").attr("title"), book.css(".price_color").text)

page.links(".pager")                              # absolute URLs of links in the pager
page.find_by_text("Tipping the Velvet")           # search by visible text
page.markdown(main_content=True)                  # the page as Markdown
```

### Pull out structured data

```python
from wintergrab import Field

books = page.extract_all("article.product_pod", {
    "title": "h3 a::attr(title)",
    "price": Field(".price_color::text", transform=lambda p: float(p.lstrip("£"))),
    "rating": Field("p.star-rating", attr="class", regex=r"star-rating (\w+)"),
})
page.extract({"titles": ["h3 a::attr(title)"]})       # a one-item list = all matches
```

### Survive layout changes

```python
products = page.css(".product-card", adaptive=True)
```

The first time, wintergrab saves a fingerprint of what matched: tag,
attributes, text, position, parent and neighbours. If the site later renames
`.product-card` or wraps it in new containers, the same call scores every
element on the new page and returns the closest matches. It logs a warning
so you know to update the selector. See [docs/adaptive-selectors.md](docs/adaptive-selectors.md).

### Scrape without writing selectors

```python
page.auto_extract()          # [{"title", "url", "image", "price", "rating"...}, ...] from the main record list
schema = page.learn({"title": "A Light in the Attic", "price": "£51.77"})
schema.extract(other_page)   # the learned selectors work on every page of that template
page.structured_data()       # JSON-LD, microdata, OpenGraph, meta tags
page.embedded_json()         # __NEXT_DATA__, window.__INITIAL_STATE__, ... (SPAs without a browser)
page.tables()                # every table as records
page.next_page()             # pagination, auto-detected
```

### JavaScript pages

```python
page = wg.render("https://quotes.toscrape.com/js/", wait_for=".quote")

with wg.BrowserFetcher(headless=True) as browser:     # reuse one browser
    page = browser.get(url, scroll=True, screenshot="page.png")
```

### Many pages at once

```python
async with wg.AsyncFetcher() as fetcher:
    pages = await fetcher.get_many(urls, concurrency=10)
```

### Crawl a site

```python
from wintergrab import Spider

class BooksSpider(Spider):
    start_urls = ["https://books.toscrape.com/"]
    allowed_domains = ["books.toscrape.com"]
    concurrency = 16                 # AutoThrottle adapts the real speed per domain
    crawl_dir = ".crawl/books"       # makes it resumable: Ctrl+C pauses, re-run resumes
    output = "books.jsonl"           # items stream here (.jsonl / .json / .csv)

    def parse(self, response):
        for link in response.css("article.product_pod h3 a"):
            yield response.follow(link, callback=self.parse_book)
        yield from response.follow_all("li.next a")

    def parse_book(self, response):
        yield {
            "title": response.css("h1::text").get(),
            "price": response.css(".product_main .price_color::text").get(),
        }

result = BooksSpider().run()
print(result.status, result.stats["pages"], result.stats["items"])
```

Scaling up is a few attributes away:

```python
class BigCrawl(Spider):
    sitemap_urls = ["https://shop.example/robots.txt"]   # discover pages from sitemaps
    frontier = "disk"            # flat memory for millions of URLs, crash-safe queue
    crawl_dir = ".crawl/big"
    cache = ".cache/big"         # revalidating HTTP cache; cache_mode="offline" replays the crawl
    output = "catalog.db"        # SQLite...
    unique_key = "url"           # ...with upserts: re-crawls update rows in place
    fallback_session = "browser" # blocked page? retry it in a headless browser, share its cookies
```

Spiders also give you:

- **Sessions.** Route requests through different fetchers with
  `Request(url, session="browser")`. Set `fallback_session="browser"` to
  retry blocked pages in a headless browser automatically.
- **Proxy rotation.** `proxies = [...]` (or a `ProxyRotator`). Proxies that
  keep failing are benched for a while.
- **Speed control.** Per-domain concurrency and delays that back off on
  429/503/block pages, honour `Retry-After` and robots.txt `Crawl-delay`,
  and recover gradually.
- **Limits and hooks.** `max_pages`, `max_items`, `max_depth`,
  `process_item()`, `on_error()`, `on_start()` / `on_close()`, and
  `async for item in spider.stream()`.

### Command line

```bash
wintergrab get https://quotes.toscrape.com                          # page as Markdown
wintergrab get https://quotes.toscrape.com --css ".quote .text::text"
wintergrab get https://books.toscrape.com --each article.product_pod \
    --field title="h3 a::attr(title)" --field price=.price_color::text -o books.csv
wintergrab get https://quotes.toscrape.com/js/ --browser --wait-for .quote

wintergrab get https://books.toscrape.com --auto                     # records, no selectors
wintergrab get https://books.toscrape.com --learn "title=A Light in the Attic" --save-schema books.json
wintergrab crawl https://books.toscrape.com --schema books.json --paginate -o books.jsonl
wintergrab get https://shop.example/p/1 --structured                # JSON-LD, OpenGraph...

wintergrab crawl my_spider.py -o items.jsonl --crawl-dir .crawl/mine   # run a spider file
wintergrab crawl https://books.toscrape.com --follow "li.next a" --follow "h3 a" \
    --each ".product_main" --field title=h1::text --max-pages 50 -o books.jsonl

wintergrab shell https://quotes.toscrape.com                        # explore interactively
```

## Documentation

| Guide | What's inside |
|---|---|
| [Getting started](docs/getting-started.md) | Install, first scrape, first spider, in 10 minutes |
| [Fetching](docs/fetching.md) | `get`/`Fetcher`/`AsyncFetcher`/`BrowserFetcher`, options, errors |
| [Parsing](docs/parsing.md) | Selectors, extraction schemas, text search, Markdown |
| [Adaptive selectors](docs/adaptive-selectors.md) | How relocation works and how to tune it |
| [Spiders](docs/spiders.md) | Crawling, sessions, pause/resume, output, every setting |
| [Power features](docs/power-features.md) | Zero-selector extraction, cache & offline replay, API capture, cookie handoff, sitemaps, disk frontier, SQLite |
| [Tough sites](docs/anti-blocking.md) | Impersonation, browsers, proxies, AutoThrottle, etiquette |
| [CLI](docs/cli.md) | `get`, `crawl` and `shell` reference |
| [Examples](examples/) | Runnable scripts for every feature |

## Scrape responsibly

wintergrab makes polite crawling the default. Spiders obey robots.txt,
adapt their speed to each site, and back off when asked. Stealth features
exist so legitimate automation isn't misclassified. They don't make it OK
to ignore a site's terms, hammer servers, or collect personal data you
have no right to. Check the rules of each site you scrape.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium        # for the browser tests (skipped otherwise)
pytest                             # runs against a local test site; no internet needed
ruff check . && ruff format --check .
```

## License

[MIT](LICENSE)
