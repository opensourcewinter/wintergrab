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
  crawling, a live progress line, and output to JSON Lines, CSV, SQLite,
  Parquet, Excel, DuckDB, PostgreSQL, MySQL or MongoDB, with upserts on a
  key.
- **Fast.** In a [reproducible benchmark](https://github.com/opensourcewinter/wintergrab/blob/main/benchmarks/README.md) against a
  local test shop, a wintergrab spider crawled about 1,000 pages/s on one
  core. That is 1.8× Crawlee and 3.8× Scrapy at the same concurrency, with
  under half their memory. On real sites, the site and your politeness
  settings usually set the pace, not the crawler.
- **Browser superpowers.** Capture the JSON API calls a page makes while it
  renders, and see where each page's data is (`get --sources`: HTML, JSON-LD,
  embedded JSON, APIs with their pagination). Sign in once in the browser,
  then continue over fast HTTP with the same cookies.
- **Say what you want.** `wintergrab goal 'Find all laptops under $1000 on
  shop.example with name, price and rating'` reads the request, surveys the
  site (robots.txt, sitemaps, a sample of pages), shows a plan with what it
  will cost, and collects clean, typed records. Pages that need JavaScript go
  to a browser, the others stay on fast HTTP; when the pages call a JSON API
  that holds the records, it is read instead, page by page. No site in mind?
  `--find-sites` asks a search API (with your key) which sites rank for it.
- **Click to build.** `wintergrab build URL -o FILE` shows the page without
  its scripts. Click a field, a repeated card, a table or the next-page
  link, and get a schema you can read, edit and test on the page. It
  crawls with `wintergrab crawl URL --extract FILE`.
- **Generates scrapers, and tests them.** `wintergrab generate "..." -o DIR`
  learns selectors for the site from its record pages. It then lints them,
  turns the sample pages into tests and crawls more pages. It measures what
  they read against wintergrab's own extraction, and keeps the scraper only
  when every step passes. A model, when you name one, finds what the pages
  don't publish, once; the scraper reads it without the model after that.
- **Learns as it crawls.** With `--optimize`, a crawl learns which URL
  patterns give items. It fetches those first, skips the patterns whose
  pages lead nowhere, and stops downloading pages under parameters that
  change nothing. On the test site's shop, that is 203 pages instead of
  376, with every item found, and 172 on the next crawl.
- **Replays crawls.** `--record` keeps a crawl's pages. `wintergrab replay`
  crawls them again offline after you change a spider or a schema, and
  shows what changed in the data. The exit status makes it a regression
  test.
- **Survives redesigns.** With `--heal`, an extractor notices when its
  selectors stop matching and finds replacements. It tests them on the
  failing pages and applies them only when other evidence on the page
  agrees. Every change is a version you can roll back. When it isn't sure,
  a person decides (`wintergrab review`).
- **Runs on its own.** A `wintergrab.yaml` lists crawl and goal jobs with
  their schedules (`every 2 hours`, `daily at 06:00`, cron).
  `wintergrab schedule` runs them. Signed webhooks tell you when a job
  fails, when a record changed or when extraction broke.
  `wintergrab dashboard` shows each run's numbers, failures, domains and
  changes, live while it runs.
- **A small CLI.** `wintergrab get` and `wintergrab crawl` cover the common
  jobs with no code at all, including `--auto`, `--learn` and `--offline`.

## Install

```bash
pip install wintergrab                 # HTTP fetching, parsing, spiders, CLI
pip install "wintergrab[browser]"      # + headless browser support
pip install "wintergrab[speed]"        # + uvloop and orjson
pip install "wintergrab[parquet]"      # + Parquet output (also: [xlsx], [duckdb], [postgres]...)
pip install "wintergrab[pdf]"          # + reading PDFs (their text, tables and links)
playwright install chromium            # one-time browser download (browser extra only)
wintergrab doctor                      # check what is installed
```

Python 3.10+ on Linux, macOS and Windows. On a fresh Linux machine, use
`playwright install --with-deps chromium` to get the browser's system
libraries too. The development version installs straight from GitHub:
`pip install "wintergrab @ git+https://github.com/opensourcewinter/wintergrab"`.

## A quick tour

### Say what you want

```python
from wintergrab import WinterGrab

wg = WinterGrab(network_policy="public")
plan = wg.plan("Find all laptops under $1000 on shop.example with name, price and rating")
print(plan.describe())                   # what it will fetch, how, and what it will cost
result = wg.run(plan, "laptops.jsonl")   # typed, validated, de-duplicated records
print(result.summary())
```

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
so you know to update the selector. See [docs/adaptive-selectors.md](https://github.com/opensourcewinter/wintergrab/blob/main/docs/adaptive-selectors.md).

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
    adaptive_fetch = True        # HTTP first; a browser for the pages that need JavaScript
```

Spiders also give you:

- **Sessions.** Route requests through different fetchers with
  `Request(url, session="browser")`. Cookies from a browser session (a
  sign-in) carry over to the HTTP sessions.
- **Proxies.** `proxies = [...]` (or a `ProxyRotator`). A proxy that keeps
  failing is benched for a while; a site's refusal is not held against it.
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
| [Getting started](https://github.com/opensourcewinter/wintergrab/blob/main/docs/getting-started.md) | Install, first scrape, first spider, in 10 minutes |
| [Fetching](https://github.com/opensourcewinter/wintergrab/blob/main/docs/fetching.md) | `get`/`Fetcher`/`AsyncFetcher`/`BrowserFetcher`, options, errors |
| [Parsing](https://github.com/opensourcewinter/wintergrab/blob/main/docs/parsing.md) | Selectors, extraction schemas, text search, Markdown |
| [Adaptive selectors](https://github.com/opensourcewinter/wintergrab/blob/main/docs/adaptive-selectors.md) | How relocation works and how to tune it |
| [Spiders](https://github.com/opensourcewinter/wintergrab/blob/main/docs/spiders.md) | Crawling, sessions, pause/resume, output, every setting |
| [Power features](https://github.com/opensourcewinter/wintergrab/blob/main/docs/power-features.md) | Zero-selector extraction, cache & offline replay, API capture, cookie handoff, sitemaps, disk frontier, SQLite |
| [Responsible access](https://github.com/opensourcewinter/wintergrab/blob/main/docs/responsible-access.md) | robots.txt, slowing down, blocked pages, honest browsers, logins, proxies, etiquette |
| [Storage](https://github.com/opensourcewinter/wintergrab/blob/main/docs/storage.md) | Where items go: JSON Lines, JSON, CSV, SQLite, Parquet, Excel, DuckDB, PostgreSQL and MySQL (typed columns, upserts), MongoDB, S3 objects, and your own formats |
| [Observability](https://github.com/opensourcewinter/wintergrab/blob/main/docs/observability.md) | Events, live metrics, Prometheus, failure reports, dead letters |
| [Data](https://github.com/opensourcewinter/wintergrab/blob/main/docs/data.md) | Normalizers, typed schemas, validation, pipelines, duplicates, quality monitoring |
| [Extraction](https://github.com/opensourcewinter/wintergrab/blob/main/docs/extraction.md) | Typed records from any page with a strategy hierarchy, provenance and confidence |
| [Entities](https://github.com/opensourcewinter/wintergrab/blob/main/docs/entities.md) | Which names are the same company, brand, product, person or place |
| [What pages look like](https://github.com/opensourcewinter/wintergrab/blob/main/docs/visual.md) | Tables and labelled values read from where a browser draws them (div grids, dashboard tiles), and screenshots for models that read images (`--layout`, `--visual-tables`, `--vision`) |
| [Benchmarks](https://github.com/opensourcewinter/wintergrab/blob/main/docs/benchmarks.md) | `wintergrab benchmark`: crawl throughput and latency, CPU, memory, parsing, extraction, validation, deduplication and browser overhead, measured on your machine |
| [Where a page's data is](https://github.com/opensourcewinter/wintergrab/blob/main/docs/sources.md) | Every source a page holds records in: HTML, tables, JSON-LD, embedded JSON and the API calls it makes, with the records each holds, GraphQL operations and pagination (`get --sources`) |
| [Places](https://github.com/opensourcewinter/wintergrab/blob/main/docs/places.md) | Where records are: addresses and listings' locations read into countries, regions, cities, postal codes and coordinates; kept by place or distance, grouped by place (`wintergrab data places`) |
| [Knowledge graphs](https://github.com/opensourcewinter/wintergrab/blob/main/docs/graph.md) | The things records name, resolved, and typed edges between them with their sources; JSON, GraphML, Neo4j CSV (`wintergrab data graph`) |
| [History](https://github.com/opensourcewinter/wintergrab/blob/main/docs/history.md) | What changed since the last crawl, and how often each page changes |
| [Intelligence](https://github.com/opensourcewinter/wintergrab/blob/main/docs/intelligence.md) | Page types, technologies, site profiles and topology (`wintergrab inspect`), with the evidence |
| [Goals](https://github.com/opensourcewinter/wintergrab/blob/main/docs/goals.md) | Say what data you want; wintergrab plans the crawl, shows its cost, and collects the records (`wintergrab goal`) |
| [Search results](https://github.com/opensourcewinter/wintergrab/blob/main/docs/search.md) | Results from search APIs you have access to (Brave, Google, your SearXNG), with the rest of their pages (news, local results, questions...), and what they say: competitors, gaps, queries one page can answer, rankings over time (`wintergrab search`) |
| [Visual builder](https://github.com/opensourcewinter/wintergrab/blob/main/docs/builder.md) | Click a page's fields, cards, tables and next-page link to build a schema; test it on the page, edit it, save it (`wintergrab build`) |
| [Generated scrapers](https://github.com/opensourcewinter/wintergrab/blob/main/docs/generate.md) | A scraper for a goal: selectors learned for the site, linted, tested, sample-crawled, validated and benchmarked before it is kept (`wintergrab generate`) |
| [Extraction tests](https://github.com/opensourcewinter/wintergrab/blob/main/docs/testing.md) | Pages with the values a schema must read from them; check every change in CI (`wintergrab fixture`, `wintergrab test`) |
| [Runs and replay](https://github.com/opensourcewinter/wintergrab/blob/main/docs/runs.md) | Keep each crawl's record and pages; replay it offline after a change and see what it does to the data (`wintergrab runs`, `wintergrab replay`) |
| [Projects](https://github.com/opensourcewinter/wintergrab/blob/main/docs/projects.md) | Jobs in one file, run on schedules (cron, `every 2 hours`), with signed webhooks for their events and for changed records (`wintergrab init`, `run`, `schedule`) |
| [Dashboard](https://github.com/opensourcewinter/wintergrab/blob/main/docs/dashboard.md) | A local page over the runs: numbers, failures, domains, extraction, changes, live while a crawl runs; JSON too (`wintergrab dashboard`) |
| [Healing](https://github.com/opensourcewinter/wintergrab/blob/main/docs/healing.md) | Extractors that repair their selectors when a site changes, with versions, rollback and a review queue (`wintergrab heal`, `wintergrab review`) |
| [Models](https://github.com/opensourcewinter/wintergrab/blob/main/docs/models.md) | Optional language models (OpenAI-compatible, Anthropic, Ollama) for the fields a page's own data does not give, checked against the page |
| [Plugins](https://github.com/opensourcewinter/wintergrab/blob/main/docs/plugins.md) | Packages that add outputs, inputs, field types, stages, strategies, model providers and commands |
| [CLI](https://github.com/opensourcewinter/wintergrab/blob/main/docs/cli.md) | `get`, `crawl`, `goal`, `generate`, `build`, `inspect`, `run`, `schedule`, `init`, `runs`, `replay`, `dashboard`, `fixture`, `test`, `heal`, `review`, `history`, `data`, `shell`, `doctor` and `plugins` reference |
| [Configuration](https://github.com/opensourcewinter/wintergrab/blob/main/docs/configuration.md) | Where each setting lives: spider settings, files, environment variables, secrets |
| [Architecture](https://github.com/opensourcewinter/wintergrab/blob/main/docs/architecture.md) | How it is built: the layers, a request's way through a crawl, where things are, extension points |
| [Upgrading from 0.2](https://github.com/opensourcewinter/wintergrab/blob/main/docs/migration.md) | What behaves differently, and what code may need a change |
| [API reference](https://github.com/opensourcewinter/wintergrab/blob/main/docs/api.md) | Every public name, by module, with its signature and what it does (written from the code) |
| [Examples](https://github.com/opensourcewinter/wintergrab/tree/main/examples/) | Runnable scripts for every feature |

## Scrape responsibly

wintergrab makes polite crawling the default. Spiders obey robots.txt,
adapt their speed to each site, and back off when asked. A blocked or
rate-limited page is reported, not fetched another way to get past the
refusal, and the browser does not hide that it is automated
([responsible access](https://github.com/opensourcewinter/wintergrab/blob/main/docs/responsible-access.md)).
None of this makes it OK to ignore a site's terms, hammer servers, or
collect personal data you have no right to. Check the rules of each site
you scrape.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium        # for the browser tests (skipped otherwise)
pytest                             # runs against a local test site; no internet needed
ruff check . && ruff format --check .
```

See [CONTRIBUTING.md](https://github.com/opensourcewinter/wintergrab/blob/main/CONTRIBUTING.md) for the live tests and the release
process, [SECURITY.md](https://github.com/opensourcewinter/wintergrab/blob/main/SECURITY.md) to report a vulnerability, and the
[code of conduct](https://github.com/opensourcewinter/wintergrab/blob/main/CODE_OF_CONDUCT.md).

## License

[MIT](https://github.com/opensourcewinter/wintergrab/blob/main/LICENSE)
