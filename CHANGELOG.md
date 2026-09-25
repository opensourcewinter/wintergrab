# Changelog

## 0.3.0

Complete, typed records without writing selectors.

- **`wintergrab.scrape(url, pages=None, deep=True)`**: reads every listing
  page, finds the records on each, opens each record's own page and merges
  what it adds. It returns the records in page order. On books.toscrape.com
  that means 18 typed fields per book (UPC, stock count, category,
  description, tax, reviews...) instead of 5 raw text columns. Inside it's a
  normal spider: it obeys robots.txt, uses AutoThrottle and retries, and can
  cache. If a record's own page fails, the record keeps its listing values.
- **`Response.extract_details()`**: one flat record for a product, article,
  job or event page. It merges schema.org data (JSON-LD or microdata), the
  block around the `<h1>`, label/value tables and lists, breadcrumbs and the
  description. It never picks a struck-out "was" price or the logo image.
- **Typed values** (`wintergrab.parser.normalize`):
  - prices become numbers plus an ISO `currency` when the symbol is
    unambiguous, in many number formats;
  - ratings become numbers (from text, classes or stars);
  - availability adds `in_stock` and a `stock` count;
  - counts such as reviews become integers.

  Anything that doesn't parse is kept as it was.
- **CLI**:
  - `wintergrab get URL --deep` and `wintergrab crawl URL --auto --deep --paginate`.
  - `get --auto` on a single item's page now returns that item.
- **CSV files open correctly in Excel**: they start with a UTF-8 byte order
  mark, so `£` no longer shows as `Â£`. In Python, read them with
  `encoding="utf-8-sig"`.

Changed:
- `auto_extract()`, `RecordGroup.extract()` and `LearnedSchema.extract()`
  now return typed values by default (`price=51.77, currency="GBP"`, not
  `"£51.77"`). Pass `clean=False` for the previous raw text.
- `auto_extract()` puts readable fields first and `url`/`image` last.

Fixed:
- Availability was dropped when every record on a page said the same thing
  (e.g. "In stock"), because identical text was treated as boilerplate.
- Label/value table rows (`<tr><th>UPC</th><td>...</td></tr>`) are no longer
  mistaken for a list of records.
- `write_items` to CSV keeps every column any item has, not just the first
  item's.

## 0.2.0

First release on PyPI.

- **Zero-selector extraction**: `structured_data()` (JSON-LD, microdata,
  OpenGraph, Twitter, meta), `embedded_json()` (`__NEXT_DATA__`,
  `window.__STATE__`, `JSON.parse` payloads), `find_json()`, `tables()`,
  `next_page()`/`follow_next()`, `detect_records()`/`auto_extract()` and
  learn-by-example `learn()` → reusable `LearnedSchema`.
- **HTTP cache** with `revalidate`/`prefer`/`offline`/`refresh` modes for all
  fetchers and spiders (offline replay of whole crawls).
- **Disk frontier** (`frontier="disk"`): SQLite queue + scalable Bloom filter,
  flat memory, at-least-once crash recovery.
- **Browser**: `capture=` records XHR/fetch API responses; `export_cookies()` /
  `add_cookies()`; spiders share browser cookies with HTTP sessions.
- **Sitemaps**: `wg.sitemap()`, `Spider.sitemap_urls/rules/follow/since`.
- **Output**: SQLite exporter with upserts, `unique_key` de-duplication,
  buffered writers, orjson.
- **Speed**: uvloop when installed (`[speed]` extra), leaner crawl loop, and
  profile-guided hot-path shortcuts (URL joining and canonicalisation,
  cached request host/fingerprint, compiled XPath cache, byte-level block
  check). Each shortcut is tested to return exactly what the code it
  bypasses returns.
- Live terminal progress line; `wintergrab doctor`; many new CLI flags.
- Engine robustness: no lost requests on cancel/force-stop/fatal errors,
  crash-safe JSON output, correct Retry-After/429 pacing, signal handlers
  restored, Ctrl+C in Jupyter.
- **Block detection** recognises Fastly's "Client Challenge" (served with
  status 200) and DataDome, and a spider that gives up on a block page says
  so instead of reporting a bare "HTTP 200".
- **Browser**: after a bot check passes, the capture waits until the real
  page is fully parsed. Behind pypi.org's check, the browser used to return
  a half-loaded page.
- **Security**: a page on an allowed domain that redirects elsewhere
  (possibly to an internal address) is no longer passed to the callbacks
  (`offsite_redirects` stat).
- The CLI writes UTF-8 when its output is redirected (Windows pipes default
  to cp1252). Tested on Linux, macOS and Windows, Python 3.10-3.14.

## 0.1.0

Internal milestone, never published to PyPI.

- **Fetching**: `get`/`post`/`aget`/`apost` shortcuts; `Fetcher` and `AsyncFetcher`
  sessions with browser TLS/HTTP2 impersonation (curl_cffi), retries with
  exponential backoff and `Retry-After`, proxy rotation, charset detection.
- **Browser**: `render`/`arender`, `BrowserFetcher` and `AsyncBrowserFetcher`
  (Playwright) with stealth patches, resource blocking, `wait_for`, scrolling,
  page actions, screenshots, challenge-page waiting and per-proxy contexts.
- **Parsing**: `Selector` with CSS (`::text`, `::attr()`) and XPath, extraction
  schemas (`Field`), `find_by_text`, `find_by_regex`, `find_similar`, generated
  selectors, links, text and Markdown conversion.
- **Adaptive selectors**: element fingerprints stored in SQLite; broken
  selectors relocate their elements by similarity.
- **Spiders**: asyncio crawler with per-domain scheduling, AutoThrottle,
  multiple sessions, session fallback for blocked pages, proxy rotation,
  robots.txt, limits, JSON/JSONL/CSV output, streaming, and pause/resume with
  checkpoints.
- **CLI**: `wintergrab get`, `wintergrab crawl`, `wintergrab shell`.
