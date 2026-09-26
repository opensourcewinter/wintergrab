# Architecture

This page is for people who want to change wintergrab, extend it, or know
where something happens. Users start with [getting started](getting-started.md).

## What it is built around

- **HTTP first.** Pages are fetched over HTTP, with a browser-like TLS
  fingerprint. A browser renders only the pages that need one. A page that
  is blocked or behind a bot check is never a reason to switch to a browser.
- **Evidence before guesses.** Extraction reads what a page publishes for
  machines first (JSON-LD, microdata, meta tags), then your selectors, then
  labels, layout and text patterns. Every value keeps where it came from
  and how sure wintergrab is. A language model, when you name one, only
  fills gaps, and its answers are checked against the page.
- **Nothing silent.** Crawls emit events, keep metrics, explain their
  failures, and can keep a record of every run.
- **Stop and continue.** Crawls can be paused, crash, or hit a budget, and
  continue where they were: the queue, the output and the history are
  written as they go.
- **Yours to run and to extend.** No service is needed, and no
  vendor-specific API. Everything important is an extension point, and
  [plugins](plugins.md) add to it without changing it.

## The layers

```
  command line (cli.py) · Python API (wintergrab.*) · dashboard (dashboard.py)
                                    │
  goals: read a request → survey the site → plan → run        projects: jobs, schedules, triggers,
  (goals/)                                                     webhooks (project.py, schedules.py,
                                    │                          watch.py, webhooks.py)
                     the crawl: spider engine (spider/)
     queue and frontier · per-domain throttle · robots.txt · budgets · middleware · optimizer ·
     checkpoints · dead letters · events and metrics · failure diagnosis · run records (runs.py)
             │                                                    │
  fetching (fetchers/)                                 extraction (extraction/, parser/)
  HTTP (curl_cffi) · browser (Playwright) ·            selectors · structured data · strategies ·
  HTTP cache · adaptive HTTP-or-browser ·              templates · models (models.py) · healing ·
  network policy (netpolicy.py) · proxies              review queue · extraction tests
                                                                  │
                                                       data (data/)
                                                       schemas · normalizers · validation · pipelines ·
                                                       expressions · quality · duplicates · entities ·
                                                       dataset versions
                                                                  │
                                         outputs (spider/exporters.py, storage/) · history (history/) ·
                                         events to webhooks · intelligence (intel/)
```

## A request's way through a crawl

1. **Queued.** A `Request` enters the scheduler (`spider/scheduler.py`, or
   the disk frontier in `spider/frontier.py`). Duplicates are recognised by
   a fingerprint of method, canonical URL and body. The URL rules and the
   normalizer (`urls.py`) run first.
2. **Picked.** The engine (`spider/engine.py`) takes the highest-priority
   request whose domain has a free slot. A budget, a page limit or the
   [optimizer](spiders.md#learning-what-to-crawl) (`spider/optimizer.py`)
   may drop it here.
3. **Throttled.** The domain's slot (`spider/throttle.py`) spaces requests
   and limits their concurrency. It slows down on errors, 429s and
   `Retry-After`, and speeds up while responses stay fast.
4. **Checked.** Middleware (`spider/middleware.py`) may answer, change or
   drop the request. robots.txt (`spider/robots.py`) is obeyed unless turned
   off, and the network policy refuses private addresses when asked to.
5. **Fetched.** A session (`spider/sessions.py`) fetches it over HTTP
   (`fetchers/http.py`) or in a browser (`fetchers/browser.py`). The
   adaptive strategy (`fetchers/strategy.py`) learns, per URL pattern, which
   pages need a browser. The HTTP cache (`fetchers/cache.py`) answers first
   when there is one; a recorded run's archive is such a cache.
6. **Judged.** Blocks and bot checks are recognised (`fetchers/blocking.py`).
   Failures are retried with backoff or given up. Each one is classified
   (`errors.py`) and grouped into diagnoses with likely causes
   (`spider/failures.py`). URLs given up on go to the dead letter queue.
7. **Parsed.** The spider's callback gets a `Response` with selectors
   (`parser/`), and yields items and more requests.
8. **Processed.** Items go through item pipelines and data pipelines
   (`data/pipeline.py`: normalize, validate, filter, enrich, quality). They
   are de-duplicated by `unique_key` and written by an exporter
   (`spider/exporters.py`, `storage/`).
9. **Recorded.** Events (`events.py`) go to subscribers: logs, event files,
   webhooks, a run's record. Metrics (`spider/metrics.py`) are sampled
   every second. With a history (`history/`), each page is kept as a
   snapshot, so the next run can tell what changed.

## Where things are

| Package or module | What it does | Look at |
|---|---|---|
| `fetchers/` | HTTP and browser fetching, the HTTP cache, blocking detection, resource filtering, the adaptive strategy | `Fetcher`, `AsyncFetcher`, `BrowserFetcher`, `HTTPCache`, `FetchStrategy` |
| `parser/` | Selectors (CSS, XPath, text), structured data, embedded JSON, pagination, records, learned schemas | `Selector`, `structured_data()`, `auto_extract()` |
| `adaptive/` | Selectors that find their element again after a page changes | `SQLiteStorage` |
| `spider/` | The crawl: `Spider`, the engine, queues, throttle, robots, budgets, middleware, sessions, checkpoints, metrics, optimizer, exporters | `Spider`, `Engine`, `CrawlResult` |
| `extraction/` | Typed records from pages: strategy hierarchy, confidence and provenance, templates, extraction models, selectors learned for a site, self-healing versions, the review queue, extraction tests | `Extractor`, `generate_schema()`, `HealingExtractor`, `FixtureSuite`, `template()` |
| `data/` | Schemas, normalizers (dates, money, units, phones...), validation, the expression language, pipelines, quality, duplicates, entity resolution, dataset versions, record readers | `Schema`, `Pipeline`, `QualityMonitor`, `read_records()` |
| `intel/` | Page types, technologies, site surveys, profiles and topology | `classify_page()`, `survey_site()`, `SiteProfiler`, `TopologyBuilder` |
| `goals/` | Requests in plain words: reading them, planning a crawl with estimates, running it, generating and testing a scraper for it | `parse_goal()`, `plan_goal()`, `GoalPlan.run()`, `generate_scraper()` |
| `history/` | Page snapshots across runs: what changed, and how often each page changes | `PageHistory` |
| `storage/` | Parquet, Excel and PostgreSQL outputs and inputs | `ParquetExporter`, `PostgresExporter` |
| `runs.py` | The run registry, recording and replay | `RunRegistry`, `replay()` |
| `project.py`, `schedules.py`, `watch.py`, `webhooks.py` | Projects: jobs, schedules, change triggers, webhooks | `Project`, `Scheduler`, `Webhook` |
| `dashboard.py` | The local web view of runs | `serve()` |
| `models.py` | Language model adapters | `load_model()` |
| `plugins.py` | Plugin discovery and the registry plugins use | `load_plugins()` |
| `redact.py`, `netpolicy.py` | Keeping credentials out of records; refusing private addresses | `redact()`, `NetworkPolicy` |
| `cli.py` | Every command | `build_parser()` |

## Extension points

| To add | Use |
|---|---|
| Behavior around requests and responses | Downloader middleware (`Spider.middlewares`) |
| Processing of items | Item pipelines, or data pipeline stages (`register_stage`) |
| A way of finding values | An extraction strategy (`register_strategy`) |
| A value type | A schema field type (`register_type`), a transform operation (`register_operation`) |
| An output or an input | `register_exporter`, `register_reader` |
| A language model | `register_provider`, or any function as `Extractor(model=...)` |
| A command | A plugin's `registry.command(...)` |
| Reactions to what happens | Event subscribers (`spider.events.subscribe`), webhooks |

All of these can come from an installed package through a
[plugin](plugins.md).

## State on disk

| Where | What | Written by |
|---|---|---|
| `crawl_dir` | A crawl's checkpoint (queue, seen URLs, state), its disk frontier, its quality report, what its optimizer learned | the engine, as it goes |
| `.wintergrab/` (the workspace) | `runs/run-N` (record, events, metrics, items and archive when recording), `logs/`, `schedule.json`, `watch/` | runs, `wintergrab run` and `schedule` |
| a history file (`--history`) | Page snapshots, runs, change rates (SQLite) | the engine, with `history=` |
| a healing directory (`--heal`) | Extractor versions, baselines, fixtures, the repair log | `HealingExtractor` |
| an HTTP cache | Responses, keyed by method, canonical URL and body (SQLite) | fetchers, with `cache=` |
| `~/.cache/wintergrab/` | Adaptive selector fingerprints | `adaptive/` |

Files that others may read while they are written (run records, metrics,
schedules) are replaced atomically. Nothing is kept in a hidden service.

## How it is tested

The tests need no internet. `tests/testsite.py` serves a small replica of
every kind of page the tests need: shops, books, listings, sitemaps, blocked
pages, slow pages, pages that need JavaScript. Browser tests run when
Playwright and Chromium are there. The PostgreSQL output is tested against a
real server when `WINTERGRAB_TEST_POSTGRES` names one, as CI does. Live
tests against real sites are opt-in (`WINTERGRAB_LIVE=1`). Benchmarks are
reproducible (`benchmarks/`), and performance claims in the docs come from
them.
