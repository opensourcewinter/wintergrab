# Changelog

## 0.2.0 — unreleased

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

## 0.1.0

First release.

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
