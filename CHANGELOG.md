# Changelog

## Unreleased

### Responsible access (breaking)

WINTERGRAB does not try to get past a block, a bot check, a rate limit or a
login it was not given. Features built for that are gone, and using one is
an error that says so, not a setting silently ignored
(docs/responsible-access.md, which replaces docs/anti-blocking.md):

- `Spider.fallback_session`, which fetched a blocked or rate-limited page
  again through another session (a browser), raises `ConfigurationError`.
  The page is reported (`blocked`, the failure report's likely cause) and
  its domain slowed down, as before.
- The browser's `stealth` mode, on by default, is gone: nothing patches
  `navigator.webdriver`, the user agent, plugins or WebGL, and no launch
  flag hides automation. `stealth=` is no longer an option.
- `referer="google"` / `"bing"`, which made requests look like clicks from
  search results, are gone: `referer` takes a URL (anything else raises
  `ConfigurationError`).
- A site's 403, 429 or block page no longer counts against the proxy it
  came through, and its retry goes through the same proxy, in fetchers and
  spiders alike: only a proxy's own failures (connection errors, 407, 502,
  504) bench it or move a retry to another. `PROXY_FAILURE_STATUSES` is
  `{407, 502, 504}`; `ProxyRotator.reuse()`.
- `examples/08_sessions_and_fallback.py` is `examples/08_sessions.py`: HTTP
  for most pages, a browser for the one that needs JavaScript.

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

### Goals (`wintergrab.goals`)

- `parse_goal("Find all laptops under $1000 on shop.example with name, price
  and rating")` reads a request in plain words: the kind of record
  (products, articles, jobs, events, companies, places such as restaurants,
  people, reviews, recipes), the fields and their synonyms ("stock status",
  "phone number", "contact details"), sites, sections ("all laptops", "the
  Phones category"), limits, watching for changes, and conditions (prices,
  ratings, dates such as "in the last 30 days" or "in 2025", stock, places)
  as data-layer expressions. What it does not understand it notes. A model
  can read requests instead (`parser=`), checked the same way.
- `plan_goal(goal)` surveys each site (`wintergrab.intel.survey_site()`:
  robots.txt, sitemaps, a sample of pages preferring those that look like
  the goal's) and learns which pages hold the records (classification, or a
  record with the goal's fields; card grids are listings), their URL
  patterns, the listings leading to them, whether they need a browser, and
  how well the fields come out. It chooses between the sitemaps' pages and
  following links (from the sections the goal names), and estimates pages,
  requests, browser pages, download, time, records, CPU and storage, with
  what each rests on. Plans are JSON: save, edit, run later.
- `plan.run(output)` collects the records with one spider: extraction,
  conditions and de-duplication as a data pipeline, adaptive fetching when
  some pages need JavaScript, the goal's limit, history when watching.
- `wintergrab goal "..."` shows how the request was understood and the plan,
  asks before big crawls (`--yes`), and writes the records; `--plan-only`,
  `--save-plan`, `--plan`, `--explain`, `--json`.
- `wintergrab inspect` now runs on `survey_site()`.
- Extraction: ratings written in class names (`class="star-rating Three"`,
  `stars-4-5`) are read.

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

### Self-healing extractors and the review queue (`wintergrab.extraction`)

- `HealingExtractor(directory, schema, review=...)` is an `Extractor` that
  keeps versions of its schema in a directory and watches the fields read
  with selectors. When a field's selectors match at most half as often as
  on the first pages (a baseline kept across runs), it looks for a
  replacement on the failing pages. It relocates the element most like the
  one they matched, and anchors on the value other strategies (JSON-LD,
  meta tags...) still find. It tests each candidate selector on those pages
  (coverage, agreement with other strategies, plausibility against past
  values, regression fixtures). A candidate scoring 0.9 or more whose values
  other strategies confirm becomes a new active version, on probation: it is
  rolled back when it does not hold on the next pages, even if later repairs
  were stacked on it. Other candidates, and fields with none, go to a person.
  There is one question per field at a time.
- `ReviewQueue(file)`: low-confidence values (with one or two competing
  values and the page), selector repairs and broken fields, with decisions
  (accept a candidate, reject, correct) kept with who made them. The
  extractor applies them: accepted repairs become versions by `human`, and
  confirmed values become fixtures that later repairs must reproduce.
- `ExtractorVersions`: versions with reason, author and status, `diff()`,
  `rollback()`, `revert()`, `activate()`, a repair log, fixtures and
  `check_fixtures()`.
- CLI: `get`/`crawl --extract SCHEMA --heal DIR [--review FILE]`, `get --why
  FIELD` (why a field is what it is on a page), `wintergrab heal DIR` (versions,
  health, `--log`, `--diff`, `--rollback`, `--activate`, `--import`,
  `--check`), and `wintergrab review FILE` (`--accept`, `--choice`,
  `--reject`, `--correct`, `--note`).

### Crawl optimization (`wintergrab.spider.optimizer`)

- `Spider.optimize = True` (or a file that keeps what was learned; `crawl
  --optimize [FILE]`) makes a crawl learn, per URL pattern, how many items
  its pages yield and whether they lead to pages that do. Past a site's
  first path segment, more than five different words in one place count as
  one pattern (`/tag/{word}`). With that:
  - requests of productive patterns go first;
  - patterns with 20 settled pages that never gave or led to an item are
    skipped. One request in ten is still fetched, and nothing is skipped
    before an item is found. A pattern that gave something once is never
    skipped;
  - query parameters shown to change nothing (pages alike with and without
    them, twice, never otherwise) are dropped from later URLs. Pages
    already fetched under them are not fetched again.
- `result.optimizer.describe()` reports what was learned. The stats count
  `optimizer/skipped`, `optimizer/duplicates` and `optimizer/rewritten`,
  and the progress line and progress logs show the items expected from
  the queue. With a `crawl_dir`, what was learned survives pauses.
- On the test site's `/shop/` (150 products, `?ref=` links, a tag cloud
  leading nowhere), crawls took:
  - 203 pages instead of 376, with all 150 items;
  - 172 pages on the next crawl, which loaded the saved file;
  - 56 pages instead of 92 with `max_items = 50`.

  Where there is nothing to save (the benchmark site), it costs about 5%
  speed: 985 pages/s instead of 1,033.
- Goal runs use it by default (`optimize=False`, `wintergrab goal
  --no-optimize`), and fetch record pages before more listing pages: the
  first 40 of the shop's products took 50 pages instead of 81.

### Runs, recording and replay (`wintergrab.runs`)

- `Spider.run_registry = True` (or a workspace directory) keeps a record of
  each run in `.wintergrab/runs/run-N`: settings, status, stats, failure
  diagnoses, events, and how to run it again. `result.run_id` names it.
  `Spider.record = True` (`crawl --record`, `goal --record`) also keeps:
  - every response received, whatever its status (an HTTP cache, robots.txt
    included);
  - the items written;
  - each response's timing.
- `replay(run)` (`wintergrab replay RUN`) crawls a recorded run again from
  its archive, without the network (no page, DNS lookup or robots.txt
  request):
  - it uses the run's command line, spider class or goal plan, or a class
    given;
  - it compares the items with the recorded ones (`DatasetDiff`) and counts
    the requests beyond the recording;
  - the CLI exits with 1 when they differ.

  Page limits are lifted, so the recording decides which pages. Healing
  extractors and learned files are left as they are.
- `wintergrab runs` lists, shows and removes runs. Once `.wintergrab` exists,
  every CLI crawl and goal run is kept.
- `GoalPlan.from_dict()`.

### Extraction tests (`wintergrab.extraction.fixtures`)

- `FixtureSuite(directory)`: pages kept with the values a schema must read
  from them. Each fixture is two text files, `0001-name.json` (URL,
  expected values) and `0001-name.html`, so the values are reviewed like
  code. `add()` snapshots what a schema reads now, and takes values to
  expect on top (`None`: nothing must be found). `run()` checks every
  value as its type reads it: prices by amount (and currency), numbers as
  numbers, text exactly (spacing aside), lists in order. `update()`
  accepts what was read. A healing extractor's confirmed fixtures are read
  too.
- `wintergrab fixture URL... --to DIR --schema S [--expect F=V] [--only]`,
  or `--from-run RUN [--match REGEX]` for the pages a recorded crawl kept.
  `wintergrab test DIR [--schema S | --heal DIR] [--update] [--json]`
  exits with 1 when a value differs, for CI.
- `HTTPCache.entries()` lists what a cache holds.

### Projects, schedules and webhooks (`wintergrab.project`, `wintergrab.schedules`, `wintergrab.webhooks`)

- A project file (`wintergrab.yaml`, `.toml` or `.json`) holds jobs: crawl,
  goal and spider command lines written as mappings (`crawl: URL`,
  `allow: /books/`, `output: ...`), with `defaults` for all of them. An
  unknown option is an error that names the closest one. `wintergrab init`
  writes one to start from.
- `wintergrab run [JOB...]` runs jobs now, each in a process of its own,
  and keeps its run in the workspace, labelled with the job's name.
  `wintergrab schedule` runs them on their schedules until stopped, with
  each job's output in `.wintergrab/logs/`. `--list` shows when each job
  runs next, and `--once` runs what is due, for cron or CI.
- Schedules: cron expressions (lists, ranges, steps, names), `every 2
  hours`, `daily at 06:00`, `weekly on monday at 06:00`, `once at
  2026-10-01 06:00`, in the machine's time or a `timezone`. A time missed
  while no scheduler was running is made up for once, as soon as one runs,
  unless the job would start later than its `start_within`. On Windows,
  which has no time zone database of its own, `tzdata` is installed with
  wintergrab.
- Webhooks (`Spider.webhooks`, or a project's `webhooks`) post events as
  JSON, batched (a second, at most 100 per delivery), signed with
  HMAC-SHA256 when given a `secret` (`X-Wintergrab-Signature`;
  `wintergrab.webhooks.verify()`), and retried after 1 and 4 seconds.
  Deliveries have their own thread and never slow a crawl down. `${NAME}`
  in a project's webhook takes the environment variable, so secrets stay
  out of the file (and off command lines: a job's options take none).
- New events. With a `history`, from its second run: `site_changed` (the
  counts), and one `record_created`, `record_updated` (what changed) or
  `record_deleted` per page. From a project's jobs: `job_started`,
  `job_finished`, `job_failed` (the run, its stats, its log).
- `extraction_failed` (a page where `--extract` or a goal found no complete
  record: `url`, `schema`, `missing`) and `schema_changed` (a quality
  monitor found fields that came, went or changed type since the last run:
  `added`, `removed`, `retyped`) were listed in `EVENT_KINDS` but never
  emitted; now they are. `pipeline_report` was missing from the list.
- `crawl --quality FILE` and `goal --quality FILE` measure the records'
  quality and compare it with the last run's report, kept in FILE:
  `quality_degraded` (price completeness 98% → 41%) and `schema_changed`
  without writing a data pipeline.

### Extraction templates (`wintergrab.extraction.templates`)

- Ready-made schemas for common records, used by name wherever a schema
  file is (`--extract product`, `Extractor("job")`, `data validate event
  items.jsonl`): product, article, job, event, company, place, person,
  review, recipe, and two new kinds, `property` (real estate) and
  `documentation`. `wintergrab templates [NAME]` lists them or prints one
  to start from.
- Real estate: a `property` page type (schema.org `RealEstateListing`,
  `Apartment`, `House`..., property URLs, and rooms and floor area in the
  text), paths for bedrooms, bathrooms, rooms, floor size and year built,
  and text patterns for "3 bedrooms", "2.5 baths" and "2,100 sq ft" (floor
  sizes keep their unit). Goals understand "apartments under $2000 on
  ...".

### Plugins (`wintergrab.plugins`)

- Installed packages with a `wintergrab.plugins` entry point add to
  wintergrab without changing it:
  - outputs and inputs (`registry.exporter`, `registry.reader`);
  - schema field types and data pipeline stages;
  - extraction strategies, tried by every extractor;
  - model providers, and commands (`wintergrab NAME`).
  `"module:name"` targets are imported only when used.
- Plugins load once, when first needed. A plugin that fails is reported
  and skipped, and never stops wintergrab. `wintergrab plugins` lists what
  each added, and `WINTERGRAB_PLUGINS=0` turns them off.
- `register_stage()` and `register_strategy()` join `register_exporter()`,
  `register_reader()`, `register_type()` and `register_operation()`.
  `STRATEGIES` is now a list.

### Language models (`wintergrab.models`)

- Adapters for OpenAI-compatible chat completions APIs (OpenAI, and
  self-hosted servers such as vLLM, llama.cpp or LM Studio), the Anthropic
  Messages API, and a local Ollama. They use only the standard library,
  keep keys in the environment, retry on 429, 5xx and network errors, take
  images, and count tokens.
- `get`/`crawl --extract SCHEMA --model PROVIDER:NAME [--model-url URL]`
  asks a model for the fields a page's own data does not give, in one
  request per page. Its answers are checked against the page like any
  value: a value the page does not contain is kept aside, never taken. A
  crawl logs the requests and tokens it used.
- `goal "..." --model PROVIDER:NAME` (`parse_goal(text,
  parser=model_reader(model))`) has a model read the request. The part of
  the site, and conditions phrased in their own words, are what the rules
  read poorly. The reading is checked: a known kind, conditions that
  compile, and no site the request does not name. When the model cannot
  read it, the rules do.
- None is needed or called unless named.

### Content intelligence (`wintergrab.intel.content`)

- `analyze_text(text)` gives a text's language, keywords, words, sentences,
  characters and reading time, with no model:
  - **scripts** name their language where they are one language's (Greek,
    Hebrew, Thai, Korean...), and letters where a script is shared: Persian
    and Urdu letters in Arabic script, hiragana for Japanese, ten Chinese
    characters without it for Chinese;
  - **common words** name 25 other languages (21 in Latin script, Russian,
    Ukrainian, Bulgarian, Hindi, Marathi), each word counting by how few
    languages share it;
  - a text too short or too mixed to tell gets no language, not a guess.
    On the 59 test paragraphs (`tests/data/languages.json`), every
    language is named, none wrongly.
  - a text written without spaces between words (Chinese, Japanese, Thai)
    gets no word count, reading time or keywords rather than wrong ones.
- `classify_text(text, model, categories=)` asks a model for a topic, a
  category, a sentiment and the entities named, and checks the answer. A
  category must be one of yours, a sentiment one of four, and an entity
  must be in the text.
- Pipeline stages `analyze` and `classify`, and `wintergrab data analyze
  INPUT --field FIELD [--model PROVIDER:NAME]`, which sums up the
  languages, keywords and sentiments.

### What pages look like (`wintergrab.parser.layout`, `wintergrab.extraction.visual`)

- A browser fetch with `layout=True` (`get --layout`) records where each
  piece of visible text is drawn: its box, font size and weight, and its
  element's path, SVG labels included (`response.layout`, at most 5,000
  per page). `screenshot=True` keeps the full-page PNG in
  `response.screenshot`.
- `layout_tables(layout)` (`get --visual-tables`) reads the tables a page
  draws, whatever its HTML: an element's text in rows with the same columns
  (pieces of a cell joined; a bold or word-only first row a header).
  `layout_pairs(layout)` reads labels and their values: beside each other
  in a row of their own, stacked in a tile (the larger text is the value),
  or in a two-column table of labels.
- The extractor's `visual` method (prior 0.75) fills a field from the value
  of a label named like it when a page has a layout; in a listing, each
  record reads its own part. On the test dashboard, five fields empty from
  the HTML are all read.
- `Extractor(model=..., vision=True)` (`get --vision`) sends the model the
  page's screenshot. A value in no text is kept with a low confidence (0.36
  by default) and the note `image-only`, instead of being dropped as
  `not-on-page`. `ModelRequest.images`; `Image` moved to
  `wintergrab.extraction.model` (still importable from
  `wintergrab.models`). docs/visual.md.

### Where a page's data is (`wintergrab.intel.sources`)

- `get URL --sources` (`data_sources(response)`) lists every source a page
  holds records in: repeated HTML elements, tables, JSON-LD and microdata
  by type, meta fields, the JSON an app embeds, and with `--browser` the
  API calls the page made as it rendered, with the lists of records each
  holds (their paths, counts, fields and types), and the richest of them.
  Over HTTP, the endpoints the page's scripts name are listed, unrequested.
  An API's answer is a source too. `-f json` for a document per page.
- `json_collections(data)` finds the lists of records in any JSON: GraphQL
  connections read through (`edges[].node`), records of records together
  (`products[].variants[]`), maps of records keyed by id by `__typename`
  (`__APOLLO_STATE__{Product}`); lists of pointers left out.
  `Collection.schema()` starts a data schema from them.
- `pagination_of(url, answer)` says how an API's pages go: by page, offset,
  cursor or next URL, from parameters, GraphQL variables and the answer's
  keys (totals, `hasNextPage`, `endCursor`, `links.next`, `_links.next`,
  Django REST framework's `count`); the next page's value and whether this
  one is the last. `api_calls(captured)` groups a page's calls by URL
  pattern and GraphQL operation (batched and persisted queries included;
  mutations are never sources), and reads calls with a number stepping up
  as pages or offsets.
- `wintergrab inspect --browser` says what each API answered: GraphQL
  operations, its largest list of records, and how its pages go.
- A capturing browser fetch gives the page up to 10 seconds more for its
  network to go quiet: calls still on their way at the `load` event were
  missed. (`inspect --browser` missed them at random.)
- Type inference no longer reads a path or file name as a quantity
  (`/img/3m-tape.png` is no length). docs/sources.md.

### Browser actions (`wintergrab.fetchers.actions`)

- `browser.get(url, actions=[...])` does steps on the page before it is
  read, written as data: `"click .more until-gone"`, `"expand .faq
  summary"`, `"dismiss #cookies button"`, `"fill #q => parka"`, `"select
  #sort => price"`, `"press Enter"`, `"wait .results"`, `"scroll 5"`,
  `"tabs .tabs a"`, `"snapshot"`, `"screenshot F"`, `"pdf F"`, `"download
  a.csv"`. `response.actions` says what each did; `response.snapshots` keeps
  the HTML after each tab, and `response.downloads` the files (their names
  only, 200 MB at most). No step runs a script. What a `fill` step types is
  left out of `response.actions`, logs and errors (`fill #password => ***`).
- A step that cannot be done stops the page with a `BrowserFetchError`
  naming it, not retried; `dismiss` and `optional` steps are passed over.
  Steps are held to the network policy: one that leads where it refuses
  raises `NetworkPolicyError` naming the step and the address, and one that
  leads to a page that does not load stops the page.
- The same steps in a spider (`Request(..., options={"actions": [...]})`)
  and on the command line: `get --do STEP` (repeatable), `--actions FILE`
  (JSON or YAML), `--downloads DIR`. `get` prints what each step did, and
  after the page's Markdown, each tab's.
- `response.console`: a browser page's console messages and uncaught script
  errors.

### `wintergrab benchmark` and `wintergrab extract`

- `wintergrab benchmark` (`wintergrab.bench`) measures WINTERGRAB on this
  machine, against a synthetic shop served from 127.0.0.1 by a process of
  its own. Each scenario runs in a fresh process:
  - start-up (`import wintergrab`, `wintergrab --version`);
  - a crawl: pages/s, items/s, latency p50/p90, CPU, peak memory;
  - parsing, extraction with the product template, and normalizing and
    validating records;
  - canonical URLs with an empty cache, and near-duplicate fingerprints;
  - `--browser`: rendering against HTTP.

  `--latency MS` delays the shop's responses as a network would; `--json`
  and `-o` keep the report. docs/benchmarks.md, with the numbers of a
  4-vCPU VM.
- `wintergrab extract URL --schema SCHEMA|TEMPLATE`: `crawl URL --extract`
  under its own name, with every crawl option (the crawl's options now
  live in one place for both).

### PDFs (`wintergrab.parser.pdf`)

- With `pypdf` (`pip install "wintergrab[pdf]"`), a response holding a PDF
  (by its type, or its first bytes) is read: `response.pdf` (pages,
  metadata, link annotations), `response.layout` (its text where it is
  drawn, pages one under the other), and as its page, simple HTML. Headings
  come from font sizes, lines are paragraphs, tables are tables (read from
  the layout as a page's), and links are links a crawl follows. `get`
  prints a PDF as Markdown, `--visual-tables` reads its tables, and
  `--extract` its fields.
- A browser fetch of a PDF asks for the file itself (the browser hands back
  its viewer's page otherwise), with the browser's cookies and no redirect.
- A damaged or locked PDF, or one read without `pypdf`, is an empty page
  with a warning, not its bytes read as text. `wintergrab doctor` says
  whether PDFs can be read.

### Places (`wintergrab.data.places`)

- `place_of(record)` reads where a record is from the fields it has: an
  address (one line, WINTERGRAB's address object, schema.org's
  `PostalAddress` or `Place`), a location as listings write it (`"Austin,
  TX"`, `"Remote - US"`, `"Hybrid: Amsterdam, NL"`), city, region, postal
  code and country fields (trusted over the address), and coordinates.
  Countries become ISO 3166-1 codes; regions ISO 3166-2 codes where the
  country's regions are known, else as written; postal codes their
  country's format. Each part says which field it came from.
- A code naming several places (`"CA"`: California or Canada; `"WA"`,
  `"IN"`, `"NL"`, `"Georgia"`...) is read in the country given, or in the
  one the other records name most (`places_of`, at least three records
  and 80% of those naming any of its countries), or not at all, and the
  place says which code it could not read.
- Coordinates are read, never looked up: coordinates fields, GeoJSON
  points, `latitude`/`longitude`, and map links (`coordinates_in_url`:
  Google Maps, OpenStreetMap, Apple Maps, Bing Maps, HERE, Waze, `geo:`).
  Extraction fills `latitude`, `longitude` and `coordinates` fields from a
  page's `geo.position`/`ICBM` meta tags, OpenGraph's `place:location`, its
  map links, embeds and static maps, and `data-lat`/`data-lng` attributes.
- `distance_km` (great-circle, within 0.5% of the ellipsoid's), `in_box`
  (boxes across the 180th meridian too), and `group_records(records, by,
  stats=)`: by country, region, city (two Portlands are two cities),
  postal code, remote, or any field; numeric statistics per group, money by
  currency.
- The pipeline stage `locate` (`Locate`), the expression functions
  `distance_km`, `in_box` and `coordinates`, and `wintergrab data places
  INPUT [--country C] [--in PLACE] [--near LAT,LON --within KM] [--remote]
  [--by PART --stats FIELD] [-o OUT]`. docs/places.md.

### Knowledge graphs (`wintergrab.data.graph`)

- `wintergrab data graph [KIND=]INPUT... -o GRAPH` (`KnowledgeGraph`)
  makes a graph of records and the things they name:
  - each record is a node of its kind;
  - the fields naming other things (a product's brand and category, a
    job's company and location, an article's author and tags, an event's
    venue and organizer...) are nodes too, linked by typed edges
    (`manufactured_by`, `offered_by`, `written_by`, `located_in`...).
    Each template has its relations, and `--relation
    FIELD=RELATION:KIND` adds more.
- Names are resolved by entity resolution, and what is unsure is listed
  for review, not merged. Categories are matched by name. Products,
  companies and people are resolved with their identifiers, so the same
  product on two sites is one node. Several files of several kinds make
  one graph.
- Nodes keep their spellings, attributes, sources and resolution
  confidence. Edges keep the pages that state them and the extraction's
  confidence in the field behind them (`_provenance`, else `_confidence`).
  Records extracted with `--provenance` name their own kind.
- Written as JSON, GraphML, or `nodes.csv` and `edges.csv` for Neo4j's
  import.

### Why a field is empty (`Extractor.why`)

- `extractor.why(field, page)`, and `wintergrab get URL --extract SCHEMA
  --why FIELD` (which needed `--heal` before), say why a field is what it
  is on a page, or empty. They list what was seen: what the field's
  selectors match, what each strategy found and how sure it was, and the
  values kept and not kept. Then they give causes, each marked certain,
  likely or possibly:
  - a value too unsure to keep (under `min_confidence`), unreadable as the
    field's type, or breaking a rule;
  - an HTTP error, or a bot-check page;
  - content drawn by JavaScript, or a page that lists records rather than
    holding one;
  - a layout that changed: the selectors find nothing while another
    strategy finds the value, with a selector that reads it on this page.
- A healing extractor's `why` builds on it and adds its repairs and
  health.

### The visual builder (`wintergrab.builder`)

- `wintergrab build URL -o FILE` fetches a page and serves a builder on
  this machine. The page is shown without its scripts. Click:
  - **a field**: selectors that find it (attributes meant for machines,
    classes, a label, the tag, positions), each with what it matches and
    reads, and a guessed name and type;
  - **a repeated card**: the schema's `container`. Fields clicked in a
    card get selectors that work in every card, and the fields found in
    the cards are offered;
  - **a table**: a table of records, a field per column, or a table of one
    record's properties, a field per row found by its label;
  - **the next page**: the schema's `next_page`.
- The specification is a schema, editable in place and as JSON. **Test**
  reads the page with it through the extractor; **Save** writes it to
  `FILE` (and nowhere else), which the builder starts from when it exists.
- The page is shown with its scripts, frames, plugins, event handlers,
  `javascript:` links and refresh tags removed. Its frame is sandboxed
  without scripts, under a policy that allows none. Changes need the
  builder page's token, as JSON from the same origin. The builder listens
  on 127.0.0.1 and refuses other host names.
- On a phone (a window narrower than 760px), the page is on top, where it
  stays while the specification scrolls beneath it, and taps pick. With
  `--host 0.0.0.0`, `build` and `dashboard` print the address other devices
  on the network open.
- Schemas can say where a listing's records are: `container` (the elements
  holding one each) and `next_page`. `Extractor.extract_all` uses the
  schema's container. `get --extract` reads every card, and `crawl
  --extract` follows `next_page` rather than every link.

### Generated scrapers (`wintergrab.goals.generate_scraper`, `wintergrab.extraction.generate_schema`)

- `wintergrab generate "REQUEST" -o DIR` makes a scraper for one site from
  a goal, and keeps it only when every step passes:
  - **plan**: the goal's survey and plan;
  - **generate**: selectors for the fields, learned from record pages;
  - **lint**: the schema reads back, the selectors compile, the URL
    patterns are valid;
  - **test**: the sample pages as extraction tests, read without a model;
  - **sample crawl**: a real crawl of more record pages;
  - **validate**: required fields found, values valid, the same values as
    the goal's own extraction;
  - **benchmark**: time, fields and confidence against the goal's own
    extraction;
  - **accept or reject**, with the reasons (exit status 0 or 1).
  `DIR` holds `plan.json` (run with `goal --plan`), `schema.json`,
  `fixtures/` (run with `wintergrab test`), `sample.jsonl`, `quality.json`
  and `report.json`.
- `generate_schema(pages, schema, model=None)` learns a selector for each
  field from the values found on sample pages. It proposes selectors from
  the elements holding the values: stable attributes, classes, a table's
  header cell, the tag, positions. A selector is kept only when it reads
  the same value on every page. With a model, the values it finds (and the
  page holds) are read by selectors after that, without it. On the test
  site's books, it gives `h1`, `p.price_color`, `p.availability`,
  `p.star-rating::attr(class)` and a UPC read by its table header. Records
  read with them are surer (0.94 against 0.83).
- A goal plan can name a schema of its own (`GoalPlan.schema`, a file next
  to the plan or a schema), which its crawl reads records with.
  `run_plan(keep_pages=True)` keeps the record pages.

### Jobs that run when something changes (`wintergrab.watch`)

- A project job's `watch: URL` runs it when a sitemap (its URLs and their
  `lastmod`), a feed (its items) or a page (its visible text) changes,
  checked every `check:` (15 minutes by default). Checks are conditional
  requests (`ETag`, `Last-Modified`) and obey robots.txt. A check that
  fails changes nothing, and it says so.
- `after: JOB` runs a job after each successful run of another. The jobs
  after it follow, a failed run stops the chain, and circles are refused.
  `wintergrab run` starts the jobs that come after none.
- `job_started`/`job_finished` carry `trigger` (`schedule`, `watch`,
  `after`, `manual`) and `reason` (`"3 new URLs, 1 gone"`).
- `wintergrab.watch.check(url, previous)` does a check on its own: what
  changed, in words and as counts.

### The dashboard (`wintergrab.dashboard`)

- `wintergrab dashboard` serves a page on this machine with the workspace's
  runs and the project's jobs. Each run shows its numbers (pages, success,
  failed, blocked, pages/sec, latency, records, browser pages, data
  quality). Under them come its failures with their causes, each domain's
  throttling, extraction problems, the quality against the last run,
  fields that came or went, what changed since the last run, its events
  and its settings. A running crawl's page follows it live; a run whose
  process died shows as *not responding*. JSON at `/api/runs`,
  `/api/runs/RUN` and `/api/jobs`.
- It is read-only and listens on 127.0.0.1. It refuses requests addressed
  to other host names (DNS rebinding), shows crawled content as escaped
  text under a Content Security Policy that allows no script, and hides
  credentials.
- Runs keep their metrics (`metrics.json`: every two seconds while they
  run, then the final ones) and the quality reports of their records.

### Storage adapters (`wintergrab.storage`)

- More outputs, chosen by extension or URL, each behind its own extra:
  - `.parquet` (`wintergrab[parquet]`, pyarrow): a typed column per field,
    zstd-compressed, nested values as JSON that reads back as it was;
  - `.xlsx` (`wintergrab[xlsx]`, openpyxl): text is never a formula
    (`=HYPERLINK(...)` from a page stays text), control characters are
    left out, and long crawls continue on new sheets;
  - `postgresql://user@host/db?table=NAME` (`wintergrab[postgres]`,
    psycopg 3): typed columns that widen when a value does not fit, `jsonb`
    for nested values, upserts on `unique_key`. It only writes tables it
    created, and keeps passwords out of what it shows.
- Parquet and Excel files are written when the crawl ends. Until then the
  items are spooled beside them, so a stopped crawl loses nothing and a
  resumed one continues.
- `read_records` (and every `wintergrab data` command) reads Parquet, Excel
  and PostgreSQL tables too.
- `register_exporter(".ext" | "scheme", ...)` and `register_reader(...)` add
  formats. A class or `"module:Class"` works: optional libraries are
  imported only when used.
- A CI job tests the PostgreSQL output against a PostgreSQL 16 server.

### Documentation and contributing

- New guides: the architecture (layers, a request's way through a crawl,
  where things are, extension points, state on disk), configuration (where
  each setting lives, environment variables, secrets), and upgrading from
  0.2. SECURITY.md covers the new trust boundaries and credentials.
- A code of conduct, issue forms for bugs and features, and a pull request
  template.
- `docs/api.md`: every public name by module, with its signature and
  summary, written from the code by `scripts/api_reference.py`. A test
  keeps it up to date.
- New examples: a record from a template with its evidence, a goal, a
  recorded crawl replayed after a change, a project file, and a plugin
  package. Each is tested offline, and live before releases.

### Fixes

- A browser fetch of a URL the browser downloads rather than shows (an
  attachment, or a PDF where the browser has no viewer, as in Playwright's
  headless shell) failed with "Download is starting". The file is now the
  answer, as over HTTP: asked for with the browser's cookies, each redirect
  checked against the network policy.
- A run's record (`run.json`) kept credentials as they were given: the
  password in a proxy URL, `Authorization` and `Cookie` headers, settings
  such as `api_token`, and the same in a crawl's command line. They are now
  left out (`***`; see `wintergrab.redact`), and so are they in a project
  job's `job_started` event and log. A replay does without them: the pages
  come from the recording, and the spider's own values stand in.
- A spider given an empty `HTTPCache` object (`cache=HTTPCache(...)` with
  nothing in it yet) used no cache at all: the cache's length made it
  falsy.
- Offline, a page missing from the cache is logged quietly instead of as an
  error for each page.
- A goal on part of a site (`on shop.example/shop/`) could plan to extract
  record pages found in the sitemap outside that part, and collect nothing.
  The survey now samples the pages of that part first, then pages that look
  like the goal's records, then their listings. It does so in the sitemap
  sample and in the links it follows (`survey_site(prefer=)` takes a score).
- `wintergrab goal` printed how it understood the request twice.
- Spiders with URL rules (`wintergrab crawl URL --sitemap ...`) skipped the
  `.xml.gz` sitemaps of a sitemap index as archive downloads. Sitemap URLs
  are no longer held to the extension and `allow` rules, which are about
  pages.
- `next_page()` resolves URLs only for candidate links and looks for
  numbered pagination only around links whose text is a number: 0.25 ms
  instead of 1.1 ms on an 11 KB page without pagination, with the same
  answers (checked against the previous version on 120,000 generated
  pages).
- `parse_address` misread places as listings write them: `"San Francisco,
  CA"` was in Canada with no city, `"New York, NY"` had the city `NY`,
  `"Austin, TX, USA"` the street `Austin`, and `"10117 Berlin, Germany"` lost
  its city and postal code. It now reads a region's code or name, a postal
  code in a part of its own or beside the city, and a street only where
  one is written (a house number, a street word), and it notes a code
  naming several places rather than choosing one.
- A crawl extracting one record per page (`crawl --extract`, `extract`)
  made one from listing pages too: the page's title and its first card's
  price, a record no page states. Without `--all`, a page that looks like a
  list of records (a category, search results, a pager over three priced
  cards or more) now gives none, and the crawl says how many it left out
  (`-s skip_listings=false` reads them anyway). `get --extract` on such a
  page points to `--all`. Page classification counts a pager over priced
  cards as a category page.
- A product's category read from a breadcrumb was the next-to-last link
  even when the page itself was not linked: "Books" rather than "Poetry"
  in Home > Books > Poetry > A Light in the Attic. The last link is left
  out only when it is the page itself (marked `aria-current`, linking
  here, or naming the page's heading).

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
