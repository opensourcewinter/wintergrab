# Changelog

## Unreleased

### Foundation: errors, URLs, network safety

- **Error taxonomy.** Every error has a `category` and a `context` dict and
  pickles cleanly. Fetch failures are now specific `FetchError` subclasses:
  `NetworkError` (with `kind`: `dns`, `connect`, `tls`, `redirects`,
  `invalid_url`, `protocol`), `ProxyError`, `FetchTimeout`, `PolicyError`
  (`NetworkPolicyError`, `RobotsPolicyError`), `BrowserFetchError`. New
  `ConfigurationError`, `SchemaError`, `ParserError`, `ExtractionError`,
  `ValidationError`, `StorageError`/`ExportError`, `BudgetExceeded`; `HTTPError`
  is an alias of `HTTPStatusError`. Existing `except FetchError` clauses keep
  working. Invalid URLs and redirect loops are no longer retried.
- **Network policy (SSRF protection).** `network_policy="public"` on fetchers,
  browsers and spiders (`--public-only` on the CLI) refuses private, loopback,
  link-local/cloud-metadata, multicast and reserved addresses before
  connecting. Names are resolved and every address checked, every redirect hop
  is checked, and the address actually connected to is checked afterwards
  (DNS rebinding). `Response.ip` records the server address.
- **URL normalization and rules.** `Spider.url_normalizer` drops tracking
  parameters, session ids and fragments, resolves dot segments and normalizes
  escapes and query order, without ever changing which page a URL means.
  `Spider.url_rules` filters discovered links by pattern, domain, extension
  and crawler-trap guards. `url_template()` turns URLs into route patterns.
  `wintergrab crawl URL` now skips media/archive links and crawler traps.
- **Browser resource control.** `resource_filter=` blocks ads, analytics and
  trackers (built-in lists, hosts/Adblock list files, third-party blocking);
  `Response.blocked_resources` reports what was blocked.
- Spider settings can now hold callable values (a URL normalizer, a
  priority function); only methods are rejected as overrides.

### Extension points, budgets and observability

- **Downloader middleware** (`Spider.middlewares`): `process_request`,
  `process_response` and `process_exception` hooks (sync or async) can answer
  requests, replace them, drop them (`IgnoreRequest`) or recover from errors.
- **Item pipelines** (`Spider.pipelines`): objects with `process_item` (plus
  optional `open_spider`/`close_spider`) or plain functions; drop with `None`
  or `DropItem`. They run before de-duplication and output.
- **Budgets**: `max_requests`, `max_bytes`, `max_runtime` (across resumes),
  `max_browser_pages`, `max_errors`, `max_error_rate`, `max_memory`,
  `max_cpu_seconds`, `max_output_bytes`. An exhausted budget stops the crawl
  with status `"limit"` and `result.limit_reason` (resumable with a
  `crawl_dir`). `budget_soft_limit` keeps the rest of a budget for
  high-priority requests.
- **Crawl order**: `crawl_order="dfs"` for depth-first crawls (memory and
  disk frontiers, preserved across pause/resume); `priority_fn(request)`
  sets every queued request's priority.
- **Events**: `spider.events` publishes structured events (crawl
  started/finished, responses, items, retries, failures, blocks, back-offs,
  budget and policy refusals); `event_log=True` writes them as JSON lines.
- **Metrics**: `spider.metrics()` / `result.metrics` with rolling rates,
  latency percentiles, per-domain throttle state (mode, delay, target delay,
  allowed and measured rate), budget usage, CPU and memory;
  `to_prometheus()` for scraping.
- **Failure reports**: `result.failures` / `result.failure_report()` group
  failures by domain and kind, with evidence and confirmed vs likely causes.
  `wintergrab crawl` prints a short diagnosis.
- **Dead letters**: failed requests are kept in `crawl_dir/dead_letters.jsonl`;
  `retry_dead_letters=True` (`--retry-failed`) fetches only those.
- CLI: `--max-requests`, `--max-bytes`, `--max-runtime`, `--order`,
  `--events`, `--retry-failed`.
- Fix: exceptions raised by callbacks no longer show a misleading
  "RuntimeError: no running event loop" as their context in logs.
- `max_output_bytes` counts the bytes each exporter produces and is checked
  after every item, so it no longer depends on when buffers reach the disk.
- Measured: crawl throughput is unchanged (1,038 pages/s median before and
  after on the benchmark site, concurrency 64, 5 alternating runs each).

### Data layer (`wintergrab.data`)

- **Normalizers** for prices (currency symbols, ISO codes, Indian and Swiss
  grouping, `1.299,-`; separator ambiguity settled by the currency's minor
  units), numbers (`2.3k`, `12 lakh`, accounting negatives, non-Latin
  digits), dates (many formats and languages, relative dates, timestamps),
  durations, measurements with exact conversions, phone numbers (E.164),
  emails, URLs, countries, regions, postal codes, addresses, coordinates
  (decimal and DMS), languages, booleans, availability (schema.org names),
  ratings and text (entities, invisible characters, mojibake repair).
  Every guess is reported as a note (`ambiguous-separator`,
  `ambiguous-currency`, `ambiguous-day-month`...) instead of being hidden.
- **Typed schemas**: 24 field types, constraints, aliases, nested objects,
  lists; `normalize()` returns typed records plus per-field results;
  `validate()` returns issues (missing, invalid, range, pattern, enum,
  suspicious values, damaged text). Versioned JSON/YAML/TOML files, JSON
  Schema export, custom types, and `infer_schema()` from sample records.
- **Expression language** for filters, computed fields and rules
  (`"price > 0 and availability == 'InStock'"`): Python syntax without
  attribute access, imports or loops, SQL-like missing values, 46 functions.
- **Pipelines**: `Rename`, `Select`, `Exclude`, `Transform` (36 value
  operations), `Compute`, `Filter`, `Normalize`, `Validate` (drop, flag,
  keep or raise; rejects file), `Deduplicate`, `Lookup`, `ConvertCurrency`
  (rates you supply), `Enrich` (sync or async), `QualityCheck`. Pipelines
  are item pipelines for spiders and load from JSON/YAML/TOML files, which
  can only name Python code when loaded with `allow_imports=True`.
- **Duplicates**: by normalized key, by content, and near duplicates by
  MinHash/LSH (one-permutation hashing: about 15 times faster than classic
  MinHash in pure Python, same accuracy). SimHash with an exact
  pigeonhole index for long documents.
- **Quality monitoring**: completeness, validity, uniqueness, consistency,
  freshness and confidence; anomalies (constant fields, placeholders,
  outliers, damaged text, duplicates); comparison with the previous run's
  report (extraction collapse, fields disappearing or appearing, type drift,
  distribution shift by Kolmogorov-Smirnov test, volume drop), emitted as
  `quality_degraded` events.
- **CLI**: `wintergrab data infer | validate | run | quality`, and
  `wintergrab crawl --pipeline FILE`. `validate` and `quality --baseline`
  exit with status 1 on bad data, for use in CI.
- New optional extra `yaml` (PyYAML); `tomli` is installed on Python 3.10
  for TOML files.
- Measured (one core, Python 3.11, `benchmarks/bench_data.py`, 20,000
  synthetic product records): `Schema.normalize` of a 10-field record
  131 µs, `validate` 24 µs, a normalize-validate-filter-compute-dedupe
  pipeline about 4,200 records/s, near-duplicate checks 158 µs per record,
  expressions 1 µs.

### Extraction engine (`wintergrab.extraction`)

- `Extractor(schema).extract(page)` finds every field of a data schema
  without selectors: schema.org JSON-LD and microdata, OpenGraph/Twitter/meta
  tags, your selectors, embedded app state, labelled values ("Weight: 1.2 kg",
  spec tables), repeating-record fields, DOM conventions and text patterns,
  in that order of preference. Struck-through prices and prices in related
  products, carts, headers and footers are told apart.
- Each value keeps its provenance (method, exact source, raw value, agreeing
  methods, competing values, normalizer notes, validation) and a confidence
  computed from evidence: method priors (measurable with `calibrate()`),
  ambiguity, agreement between independent methods, disagreement and
  validation. Values below `min_confidence` are left out, not guessed.
- `extract_all()` for listing pages (JSON-LD item lists, a container
  selector, or automatic record detection).
- Optional extraction models through a plain function interface, asked only
  for missing fields; their answers are grounded against the page text.
- CLI: `get --extract SCHEMA [--explain] [--provenance] [--all]`,
  `crawl URL --extract SCHEMA`.
- Measured with `benchmarks/bench_pages.py` (one core, parsing included):
  6.7 ms for a 13-field record from an 11 KB product page with JSON-LD,
  6.8 ms without structured data, 62 ms from a page with 480 KB of text.

### Entity resolution (`wintergrab.data.entities`)

- `EntityResolver(kind)` groups mentions of companies, organizations, brands,
  products, people and places: "Apple Inc.", "APPLE INC" and "Apple" are one
  company; "Apple Computer" is listed for review unless evidence (the same
  website) supports the merge. Names are normalized for their kind (legal
  forms, initials, "Last, First", generations, units and model numbers,
  "St."/"Mt.", areas such as "Springfield, IL").
- Pairs are scored from log-odds evidence (names and identifiers: website,
  e-mail domain, phone, GTIN, MPN, LEI, VAT, Wikidata id, coordinates...),
  merged above 0.95 and listed for review above 0.5. Merges are refused
  between groups with conflicting identifiers, across mentions that compared
  as different entities, and for mentions as close to two such groups.
- Every entity keeps its mentions with sources and attributes, the matches
  that merged them with their reasons, and a confidence; `records()` gives
  one merged record per entity.
- CLI: `wintergrab data entities FILE --field NAME [--kind ...] [--attribute
  ...] [-o] [--review-output] [--annotate]`.
- Measured: about 3,800 mentions per second on 20,000 generated company
  names (`benchmarks/bench_data.py`).

### Crawl history and change detection (`wintergrab.history`)

- `Spider.history = "shop.history"` (`crawl --history FILE`) records a
  snapshot of every page (fingerprints of its text, title, description,
  meta tags, structured data and schema.org types, price and availability,
  layout, images, navigation and items; the HTML with `history_html`) in one
  SQLite file, about 1.3 KB per page, and ends the crawl with what changed
  since the previous run: `result.changes` (`+ added`, `- removed`, `~
  modified` by kind, `= unchanged`, and pages not reached by an early stop),
  `stats["changes"]` and a `changes_detected` event. Tokens, nonces and
  cache-busting queries do not count as changes; resumed crawls continue
  their run.
- Freshness per URL: first and last seen, last changed, and a change rate
  (Cho and Garcia-Molina's estimator) that sets when to fetch again;
  `skip_fresh = True` (`--skip-fresh`) does not fetch pages that are not due.
- `wintergrab history FILE` lists runs and changes, `--compare OLD NEW`,
  `--url URL` (a page over time), `--due`, `--json`.
- Measured: 2.4 ms of fingerprints per 11 KB page (`snapshot_page` in
  `benchmarks/bench_pages.py`), about 2.9 ms per page with the database write.

### Dataset versions and differences (`wintergrab.data.versions`)

- `diff_records(old, new, key=...)`: added, removed, changed (with field-level
  old and new values, and deltas for numbers and same-currency prices) and
  unchanged records; keys normalized like duplicate keys, URLs compared
  normalized, metadata fields (`_...`) skipped; per-field summaries (up/down
  and median change, common transitions such as InStock -> OutOfStock).
- `DatasetVersions(directory)`: v1, v2, v3... as gzipped JSON Lines with a
  manifest of record counts, order-independent digests and the differences
  from the previous version; identical data is not saved twice.
- CLI: `wintergrab data commit DIR INPUT`, `data log DIR`, `data diff OLD NEW`
  (files or `DIR@VERSION`, `--exit-code` for CI).

### Page and site intelligence (`wintergrab.intel`)

- `classify_page(page)`: product, category, listing, article, news, job,
  event, company, profile, review, directory, documentation, homepage, login,
  search, archive, contact or error, from schema.org types, `og:type`, the
  status, the URL, the layout and the wording, with the evidence and a
  confidence from the winning score and its margin. Refined types (news,
  search results...) build on their parent's evidence. Add rules with
  `PageClassifier.add_rule`.
- `classify_url(url)`: the same from the URL alone, for crawl priorities.
- `detect_technologies(page)`: 124 fingerprints (CMS, e-commerce, JavaScript
  frameworks, analytics, payments, consent, CDNs, hosts, servers, languages)
  over headers, cookie names, meta tags, asset URLs, HTML markers and the
  URL, with versions, implied technologies and evidence-based confidence.
  Add fingerprints with `TechDetector(extra=[TechRule(...)])`.
- Site profiles: `SiteProfiler` (and `Spider.profile`, `wintergrab inspect
  URL`) sums up a site: technologies, languages and regions, page types,
  template clusters with URL patterns, structured data, links, API
  endpoints (script calls, API paths, JSON links, browser captures,
  platform conventions), sitemaps, crawlability (robots.txt, noindex,
  JavaScript-only pages, bot protection), latency, errors and, with a
  history, change frequency. `inspect` reads robots.txt and the sitemaps
  and visits a sample of pages spread across them.
- Site topology: `TopologyBuilder` (and `profile.topology`) arranges every
  URL known (listed in a sitemap, visited, or linked) into a tree of
  sections, named after the site's menus and breadcrumbs (links and
  schema.org `BreadcrumbList`), with ids generalized (`/blog/{id}`) and
  items clustered (`/p/{slug}`, `/p/{slug}/reviews`), URL counts and page
  types. It also has the main navigation with its submenus, feeds, HTML
  sitemaps, paginated listings, dead ends, duplicate routes (same text, or a
  canonical link to another URL) and, for a crawl that ran to the end,
  orphans (listed in a sitemap, linked from no page visited). `inspect`
  prints it (`--depth`, `--show`); `wintergrab crawl ... --profile FILE`
  saves the profile and topology of a crawl.
- A profiler reads the sitemaps and feeds a crawl fetches.
- Paths ending in `/page/N` count as listing pages, and `/products/page/N`
  is no longer taken for a product.
- Measured: 3.0 ms to classify and 0.9 ms to profile an 11 KB product page;
  20 ms and 12 ms for a page with 480 KB of text; `classify_url` 11 µs;
  6.9 ms per page for a site profile's full analysis, 2.3 ms after the
  first 500 pages (topology included).

### Adaptive fetching (`wintergrab.fetchers.strategy`)

- `Spider.adaptive_fetch = True` (or a file) fetches pages over HTTP first
  and again in a browser when their HTML is not enough:
  `Spider.needs_browser(response)`, by default a `render_if_missing`
  selector that finds nothing or a JavaScript app shell
  (`needs_javascript()`: an empty mount point such as `#root` or
  `#__next`, or little text with several scripts or a `<noscript>` asking
  for JavaScript; not when the data is embedded in the HTML). Rendered
  pages wait for the network to be idle and record their API calls.
- Outcomes are counted per URL pattern and host; a pattern whose pages
  needed a browser 80% of the time (3 pages at least) goes to the browser
  directly, with one page in ten still tried over HTTP. The counts can be
  kept in a file for the next crawls (`result.fetch_strategy`). The
  browser attempt is the same page (not a new one for `max_pages`), a
  `browser_needed` event says why, and a crawl without a browser carries on
  over HTTP. Blocked pages are never sent to the browser for being blocked.
- CLI: `crawl --auto-browser [--fetch-stats FILE] [--render-if-missing SEL]`
  and `get --auto-browser`.

### Fixes

- Spiders with URL rules (`wintergrab crawl URL --sitemap ...`) skipped the
  `.xml.gz` sitemaps of a sitemap index as archive downloads. Sitemap URLs
  are no longer held to the extension and `allow` rules, which are about
  pages.
- `next_page()` resolves URLs only for candidate links and looks for
  numbered pagination only around links whose text is a number: 0.25 ms
  instead of 1.1 ms on an 11 KB page without pagination, with the same
  answers (checked against the previous version on 120,000 generated
  pages).

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
