# Power features

These features take wintergrab beyond "fetch and parse". They cover
scraping without writing selectors, crawling at scale, and speed.

- [Zero-selector extraction](#zero-selector-extraction): structured data,
  embedded app state, tables, automatic records, learning by example,
  pagination
- [HTTP cache and offline replay](#http-cache-and-offline-replay)
- [Capturing a page's API calls](#capturing-a-pages-api-calls)
- [Handing a browser session to fast HTTP](#handing-a-browser-session-to-fast-http)
- [Sitemaps](#sitemaps)
- [Huge crawls: the disk frontier](#huge-crawls-the-disk-frontier)
- [Output: SQLite with upserts, de-duplication](#output-sqlite-with-upserts-de-duplication)
- [Live progress](#live-progress)
- [Speed extras](#speed-extras)

## Zero-selector extraction

Selectors are the part of a scraper that breaks. A lot of the time you don't
need them.

### Structured data

Most product pages, articles, recipes, events and job posts embed
machine-readable data for search engines:

```python
page = wg.get("https://shop.example/product/42")
data = page.structured_data()
data["json_ld"]        # [{"@type": "Product", "name": ..., "offers": {"price": ...}}]
data["microdata"]      # itemscope/itemprop trees as dicts
data["opengraph"]      # {"title": ..., "image": [...], "product:price:amount": ...}
data["twitter"], data["meta"]   # twitter cards; title, description, canonical, language, feeds...
```

### Embedded app state (scrape SPAs without a browser)

React/Next/Vue/Nuxt sites often ship their whole data model inside the HTML:

```python
state = page.embedded_json()
state["__NEXT_DATA__"]["props"]["pageProps"]      # Next.js
state["__INITIAL_STATE__"]                         # window.__INITIAL_STATE__ = {...}
page.find_json("price")                            # every "price" value, at any depth
```

It handles `<script type="application/json">` blocks, `window.X = {...}`
assignments and `JSON.parse("...")` payloads.

### Tables

```python
for table in page.tables():
    table["headers"]      # ["Rank", "Name", "Score"]
    table["rows"]         # [{"Rank": "1", "Name": "...", "Score": "..."}, ...]
```

`colspan`/`rowspan` are expanded, stacked header rows are merged, and nested
tables are kept separate.

### Automatic records

```python
page.auto_extract()
# [{"title": "A Light in the Attic", "url": "https://...", "image": "https://...",
#   "price": "£51.77", "rating": "Three", "availability": "In stock"}, ...]
```

wintergrab finds the page's main repeating structure (a product grid, search
results, table rows) and ignores menus, footers and tag clouds. It then names
the fields it finds. `page.detect_records()` returns the candidate groups
with their generated CSS selectors if you want to take it from there.

### Learning by example

Point at values you can see on the page. wintergrab works out the selectors:

```python
schema = page.learn({"title": "A Light in the Attic", "price": "£51.77"})
schema.container, schema.fields     # "article.product_pod", {"title": "h3 a::attr(title)", "price": "p.price_color::text"}
schema.extract(page)                # every record on this page
schema.extract(wg.get(next_page))   # ...and on any page with the same template
json.dumps(schema.to_dict())        # save it; LearnedSchema.from_dict() to load
```

Give a list of examples from different records for better generalisation. On
a detail page (no repeating records) the schema extracts a single record.

From the command line:

```bash
wintergrab get https://books.toscrape.com --learn "title=A Light in the Attic" --learn "price=£51.77" \
    --save-schema books.json -o page1.csv
wintergrab crawl https://books.toscrape.com --schema books.json --paginate -o all-books.jsonl
```

### Pagination

```python
page.next_page()                       # absolute URL of the next page, or None
yield response.follow_next()           # in a spider callback: the whole pagination loop
```

It detects `rel="next"`, "Next"/"›"/"»"/"Older posts"/"Load more" links,
`aria-label`s, `next` classes, and numbered pagination (the link after the
current page).

```bash
wintergrab crawl https://books.toscrape.com --paginate --auto -o books.jsonl
```

## HTTP cache and offline replay

```python
wg.get(url, cache=True)                                   # ./.wintergrab-cache/http.sqlite3
wg.Fetcher(cache=".cache", cache_mode="prefer")
class MySpider(Spider):
    cache = ".cache/my-spider"
    cache_mode = "revalidate"
```

| Mode | Behaviour | Use it for |
|---|---|---|
| `revalidate` (default) | Serves fresh responses from disk (`max-age`, `Expires`, or your `cache_ttl`). Stale ones are revalidated with `If-None-Match`/`If-Modified-Since`, so unchanged pages cost a tiny `304`. | Recurring crawls that should only re-download what changed |
| `prefer` | Anything cached is used, whatever its age. | Developing a scraper: fetch once, then iterate on parsing |
| `offline` | Never touches the network. A miss raises `CacheMiss`. | Deterministic replays: tests, debugging, demos |
| `refresh` | Always downloads, and overwrites the cache. | Rebuilding the cache |

Responses tell you where they came from: `response.cache_status` is
`"hit"`, `"revalidated"`, `"stored"` or `None`, and `response.from_cache` is
a shortcut. In spiders, cache hits skip the politeness delay (no request
reached the site). The stats show `cache_hits` and `cache_revalidated`.
Browser fetchers cache rendered pages too (in their own namespace). Use
`cache_mode="prefer"` to render each page only once.

```bash
wintergrab crawl my_spider.py --cache-dir .cache --cache-mode prefer     # record
wintergrab crawl my_spider.py --cache-dir .cache --offline               # replay, zero requests
```

## Capturing a page's API calls

Many JavaScript sites load their data from JSON APIs. Take the data the page
itself downloads:

```python
page = wg.render("https://spa.example/products", capture=True)   # every JSON XHR/fetch
page = browser.get(url, capture="*/api/v2/*")                     # or a URL glob / substring / function
for call in page.captured:
    call.url, call.method, call.status, call.json()
page.captured_json("products")                                    # parsed bodies, filtered by URL
```

```bash
wintergrab get https://spa.example --capture-filter "*graphql*"
```

## Handing a browser session to fast HTTP

Log in, accept a consent wall or clear a JavaScript check once in a real
browser, then carry on over fast HTTP:

```python
with wg.BrowserFetcher(headless=False) as browser:
    browser.get("https://site.example/login", page_action=log_in)
    cookies = browser.export_cookies()

with wg.Fetcher() as http:
    http.add_cookies(cookies)
    http.get("https://site.example/account")     # logged in, at HTTP speed
```

Spiders do this automatically (`share_browser_cookies = True`). When a
request goes through a browser session, for example to sign in with the
site's own form ([browser actions](fetching.md#browser-actions)), the
cookies it ends up with are copied into every HTTP session. The next
requests go over HTTP instead of paying for the browser again.

## Sitemaps

```python
wg.sitemap("https://site.example/robots.txt")              # every URL from every listed sitemap
wg.sitemap("https://site.example/sitemap.xml", since="2026-01-01")

class Shop(Spider):
    sitemap_urls = ["https://shop.example/robots.txt"]
    sitemap_rules = [(r"/product/", "parse_product"), (r"/category/", "parse_category")]
    sitemap_follow = [r"products"]          # only these nested sitemaps
    sitemap_since = "2026-06-01"            # incremental: only pages changed since
```

It handles sitemap indexes, `.xml.gz`, plain-text sitemaps, RSS/Atom feeds
and robots.txt `Sitemap:` lines. The entry's `lastmod` is available as
`response.meta["sitemap_lastmod"]`.

```bash
wintergrab crawl https://shop.example --sitemap https://shop.example/robots.txt --auto -o shop.jsonl
```

## Huge crawls: the disk frontier

The default queue lives in memory. That's fast, and fine for hundreds of
thousands of URLs. For tens of millions, switch to the SQLite-backed
frontier:

```python
class Big(Spider):
    frontier = "disk"          # needs crawl_dir
    crawl_dir = ".crawl/big"
```

- Memory stays flat. Only a small buffer per domain is in RAM, and
  seen-URL filtering uses a scalable Bloom filter at about 2-3 bytes per URL
  instead of about 70.
- Every request is on disk the moment it is queued, so a crash loses
  nothing. Requests that were in flight are delivered again on resume
  (at-least-once).
- Resuming is instant: there's no giant pickle to load.

## Output: SQLite with upserts, de-duplication

```python
class Catalog(Spider):
    output = "catalog.db"      # .sqlite / .sqlite3 / .db -> an "items" table; new fields become columns
    unique_key = "url"         # duplicates dropped; re-crawls update rows in place
```

JSON Lines, JSON and CSV outputs are buffered (flushed every 64 items or
every second, and at every checkpoint), so writing is cheap even at
thousands of items per second.

## Live progress

On an interactive terminal, spiders show a live status line:

```
⠹ 12,480 pages · 213.4/s · 11,902 items · 3,311 queued · 64 active · 214 cached · 2m04s (~1m12s left)
```

Log messages print above it. Set `progress = False` (or `--no-progress`)
to turn it off. It's never shown when output is redirected or in CI.

## Speed extras

```bash
pip install "wintergrab[speed]"      # uvloop + orjson
```

Spiders run on uvloop automatically when it is installed
(`use_uvloop = True`), and exporters serialise with orjson. The core
engine is tuned as well: no per-iteration task creation in the crawl loop,
cheap deadlines, buffered output, and fast paths for link joining, URL
canonicalisation, host parsing and XPath. See
[benchmarks](../benchmarks/README.md) for measured numbers against other
frameworks: about 0.95 ms of CPU per page, against 2.1 ms for Crawlee and
3.6 ms for Scrapy.
