# Observability: events, metrics and failure reports

A big crawl should be understandable while it runs and explainable after it
ends. wintergrab gives you three views:

- **Events**: a stream of small structured records (a request failed, a
  domain backed off, a budget ran out...).
- **Metrics**: live numbers (rates, latency percentiles, per-domain throttle
  state, budget usage, CPU and memory), also in Prometheus format.
- **Failure reports**: raw errors grouped and explained, with *confirmed*
  causes kept apart from *likely* ones.

## Events

Every spider has an event bus, `spider.events`. Subscribe any function
(plain or `async`) to all events or to some kinds:

```python
from wintergrab.events import JsonlEventSink

spider = BooksSpider()
spider.events.subscribe(print, "request_failed")          # one kind
spider.events.subscribe(JsonlEventSink("events.jsonl"))   # everything, as JSON lines
spider.run()
```

Or let the spider write them: `event_log = True` writes
`crawl_dir/events.jsonl`, `event_log = "path.jsonl"` any file
(`wintergrab crawl ... --events FILE`).

| Event | Data |
|---|---|
| `crawl_started` | `spider`, `resumed`, `queued` |
| `crawl_finished` | `spider`, `status`, `limit_reason`, `stats` |
| `response` | `url`, `status`, `bytes`, `latency`, `source`, `cache` |
| `item_scraped` | `item` |
| `item_dropped` | `pipeline`, `reason` |
| `request_retried` | `url`, `reason`, `attempt`, `delay` |
| `request_failed` | `url`, `error`, `category`, `kind`, `status` |
| `blocked` | `url`, `status`, `domain` |
| `browser_needed` | `url`, `pattern`, `reason` (with [`adaptive_fetch`](spiders.md#http-first-a-browser-when-needed): a page fetched again in the browser) |
| `throttle_backoff` | `domain`, `delay`, `concurrency`, `retry_after` |
| `policy_refused` | `url`, `reason`, `policy` |
| `budget_exhausted` | `budget`, `used`, `limit` |
| `pipeline_report` | `pipeline`, `stages` (per-stage counts, when a [data pipeline](data.md#pipelines) closes) |
| `quality_degraded` | `dataset`, `field`, `code`, `message`, `severity` ([quality monitoring](data.md#quality)) |
| `changes_detected` | `run`, `added`, `removed`, `modified`, `unchanged`, `missing`, `skipped`, `kinds` (at the end of a crawl with a [history](history.md)) |

`response` and `item_scraped` fire for every page and item, so they are
only built when someone subscribed to them: an unobserved crawl pays
nothing. A handler that raises is logged and ignored; watching a crawl never
breaks it. Each event carries `time` (UTC) and `origin` (the spider name).

## Metrics

`spider.metrics()` (while running, from any callback or thread) and
`result.metrics` (afterwards) return a snapshot:

```python
{
  "pages": 1204, "requests": 1251, "responses": 1240, "items": 980, "bytes": 25_104_331,
  "errors": 11, "failed": 4, "retries": 47, "blocked": 2, "success_rate": 0.9967,
  "queued": 311, "in_flight": 16, "active_domains": 3,
  "rates": {"pages_per_second": 38.5, "items_per_second": 31.2, "bytes_per_second": 802113.0, ...},
  "latency": {"p50": 0.212, "p90": 0.48, "p99": 1.3, "mean": 0.26},
  "domains": [
    {"domain": "shop.example", "mode": "backing off", "active": 1, "concurrency": 1, "max_concurrency": 4,
     "delay": 8.0, "target_delay": 0.05, "avg_latency": 0.2, "allowed_rate": 0.125, "current_rate": 0.12,
     "paused_for": 0.0, "backoffs": 3, "requests": 402},
    ...
  ],
  "budget": {"max_requests": {"used": 1251, "limit": 5000, "fraction": 0.25}},
  "process": {"cpu_seconds": 41.2, "rss_bytes": 187_000_000, "peak_rss_bytes": 190_000_000},
}
```

Per domain, `mode` is `normal`, `backing off` (a push-back in the last 30
seconds), `recovering` (still slower than its target after a push-back) or
`paused` (honouring a `Retry-After`). `delay` is the current spacing between
requests and `target_delay` where healthy responses pull it
(`latency / target concurrency`); `allowed_rate` is the request rate the
current settings allow and `current_rate` the measured one.

Rates are measured over the last 30 seconds; latency percentiles over the
last 2,048 responses (cache hits excluded).

### Prometheus

```python
from wintergrab.spider.metrics import to_prometheus

text = to_prometheus(spider.metrics(), spider.stats)   # serve it on /metrics
```

Counters become `wintergrab_pages_total` and friends, per-domain gauges carry
a `domain` label, and stats like `status/404` become
`wintergrab_status_by_label_total{label="404"}`.

## Failure reports

`result.failures` groups every failed attempt by domain and kind of failure
and explains it; `result.failure_report()` prints it:

```text
HTTP 429 on shop.example
  affected URLs: 14,921 (15,002 failed attempts, 38 given up)
  previous success: 18 min before the first failure
  cause (confirmed): the server is rate limiting (HTTP 429 Too Many Requests)
  evidence: Retry-After: 30; Server: cloudflare
  crawler state: backing off (delay 8.0s, concurrency 1/4)

HTTP 403 on shop.example
  affected URLs: 212 (212 failed attempts, 212 given up)
  previous success: 3 min before the first failure
  likely cause: server-side access policy or rate limiting
  crawler state: backing off (delay 8.0s, concurrency 1/4)
```

A **confirmed** cause is conclusive from the evidence: a 429 is the server
saying "too many requests"; a name that doesn't resolve doesn't exist; a 404
is a missing page; robots.txt disallowed the URL; the spider's own callback
raised an exception. A **likely** cause is a hypothesis: a 403 can be a bot
wall, an IP ban or a real permission problem, so it is reported as likely,
with the evidence that points one way or the other (challenge-page markers
in the body, a `Server: cloudflare` header, `Retry-After`). Reports never
state a hypothesis as fact.

Each `FailureDiagnosis` has `domain`, `signature`, `category`, `attempts`,
`failed_urls`, `affected_urls`, `sample_urls`, `first_seen`, `last_seen`,
`last_success`, `confirmed_cause`, `likely_cause`, `evidence` and
`crawler_state`. `wintergrab crawl` prints the top three at the end (all of
them with `-v`).

## Dead letters

Requests that finally fail are recorded in `crawl_dir/dead_letters.jsonl`
(one readable JSON line each: URL, error, category, status, attempts, plus the
full request). Fix whatever was wrong and retry just those:

```python
MySpider(crawl_dir=".crawl/mine", retry_dead_letters=True).run()
```

```bash
wintergrab crawl my_spider.py --crawl-dir .crawl/mine --retry-failed
```

A paused crawl resumes its queue plus the dead letters; a finished crawl
fetches only the dead letters. Starting a crawl over begins a fresh file.
`dead_letters = "path.jsonl"` puts the file elsewhere, `False` turns it off.
