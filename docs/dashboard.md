# The dashboard

`wintergrab dashboard` shows the [runs](runs.md) of a workspace in a
browser. For each crawl it shows what it did, and for a running crawl, what
it is doing now.

```
$ wintergrab dashboard
wintergrab dashboard: http://127.0.0.1:8710/  (.wintergrab; Ctrl+C to stop)
```

The first page lists the runs: when each started, its label and spider,
status, pages, items, requests given up, success rate and duration. When
the current directory holds a [project](projects.md), its jobs come first,
with their schedules, when each runs next, and how its last run went.

A run's page starts with its numbers:

```
RUN run-1
slow · finished · 2026-09-26 05:47 · 4.5 s

Pages 18        Success 88.9%    Failed 11.1%    Blocked 0.0%    Pages/sec 4.0
18 requests                      2 given up      0 responses     on average

Latency 472 ms  Records 16       Browser pages 0
p50 543 · p90 546 ms
```

(A crawl of the repository's test site with two broken pages.) Under the
numbers come:

- **Failures**: the failure diagnoses with their likely causes, and the
  failed requests grouped by category, status and error, with example URLs.
  Blocked responses are counted per domain.
- **Domains**: for each domain, how it was throttled: its mode (normal,
  backing off, recovering, paused), requests, concurrency, delay, latency
  and push-backs.
- **Extraction**: pages that gave no complete record, with the fields that
  were missing and example URLs. Also the [quality](data.md#quality) of the
  records, what degraded since the last run, and fields that came, went or
  changed type.
- **Changes**: with a [history](history.md), what changed since the last
  run: new, gone and modified pages, and what changed on each (`price
  [10.0, 8.0]`).
- **Events**: how many of each kind. Each kind opens its last 500 events.
- **Settings**: the ones that differ from a spider's defaults. The others
  are folded away, and credentials are never shown.

A running crawl's page reloads itself every three seconds. It reads the
metrics the crawl keeps every two seconds (`metrics.json`) and shows how
many requests wait in the queue. A run that says it is running but has kept
no metrics for 30 seconds is shown as *not responding*: its process has
probably stopped without finishing its record.

## Options

```bash
wintergrab dashboard [--workspace DIR] [--project FILE] [--port 8710] [--host 127.0.0.1] [--open]
```

- The workspace is the project's (`workspace:` in `wintergrab.yaml`), or
  `.wintergrab`.
- `--open` opens the page in a browser.

## JSON

The same data is available as JSON, for scripts and other tools:

| URL | What |
|---|---|
| `/api/runs` | The runs, newest first (`?limit=N`, 200 by default) |
| `/api/runs/RUN` | One run: its record, `state`, `metrics`, a summary of its `events`, and the size of its output |
| `/api/jobs` | The project's jobs: schedule, next time, last status and run |

## Safety

The dashboard is for the machine it runs on:

- It listens on 127.0.0.1. It answers only requests addressed to
  `127.0.0.1` or `localhost`, so a web page elsewhere cannot read it
  through DNS rebinding.
- It changes nothing. It only reads the workspace, and other methods than
  `GET` are refused.
- What crawls collected is shown as text, never as markup. Pages carry a
  Content Security Policy that allows no script at all, and links to
  crawled pages send no referrer.
- Credentials are left out of what it shows: see
  [runs](runs.md#what-a-run-keeps).

`--host 0.0.0.0` makes it reachable from other machines. It then shows what
your crawls collected to anyone who can reach it, with no password, and it
says so when it starts.

## In code

```python
from wintergrab.dashboard import Dashboard, serve

server = serve(".wintergrab", port=8710)   # a ThreadingHTTPServer
server.serve_forever()

Dashboard(".wintergrab").run_data(run)     # what /api/runs/RUN gives
```
