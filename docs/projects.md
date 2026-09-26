# Projects, schedules and webhooks

A project is one file that says what to crawl, when, and whom to tell.

```yaml
# wintergrab.yaml
defaults:                                  # options for every job
  concurrency: 4
webhooks:
  - url: https://hooks.example/wintergrab
    events: [job_finished, job_failed, crawl_finished]
    secret: ${WINTERGRAB_WEBHOOK_SECRET}   # from the environment
jobs:
  books:
    crawl: https://books.example/books/    # like: wintergrab crawl URL --allow ... --output ...
    allow: /books/
    output: data/books.jsonl
    history: data/books.history            # what changed since the last run
    schedule: every 2 hours
  rated:
    goal: books rated 4 stars or more on https://books.example/books/
    output: data/rated.jsonl
    schedule: daily at 06:00
```

```
$ wintergrab run --list
books            wintergrab crawl https://books.example/books/ --concurrency 4 --allow /books/ --output data/books.jsonl --history data/books.history  (every 2 hours)
rated            wintergrab goal books rated 4 stars or more on https://books.example/books/ --yes --output data/rated.jsonl  (daily at 06:00)

$ wintergrab run
== books: wintergrab crawl https://books.example/books/ --concurrency 4 --allow /books/ --output data/books.jsonl --history data/books.history
finished: 16 pages, 16 items, 0 errors in 0.3s -> data/books.jsonl
kept as run-1
books: finished in 0.6 s, 16 pages, 16 items (run-1)
== rated: wintergrab goal books rated 4 stars or more on https://books.example/books/ --yes --output data/rated.jsonl
...
rated: finished in 1.2 s, 15 pages, 4 items (run-2)

$ wintergrab runs
run-2    2026-09-26 05:14  finished  rated (goal)     15 pages, 4 items, 0 errors, 0.2 s
run-1    2026-09-26 05:14  finished  books (quick)    16 pages, 16 items, 0 errors, 0.3 s

$ wintergrab schedule --list
rated            daily at 06:00               next: 2026-09-26 06:00
books            every 2 hours                next: 2026-09-26 07:14
```

(A run against the repository's test site, `tests/testsite.py`; its address
is shortened here.)

`wintergrab init` writes a `wintergrab.yaml` to start from, and the
workspace, `.wintergrab`. The project file can also be TOML or JSON
(`wintergrab.toml`, `wintergrab.json`), or any file given with `--project
FILE`.

## Jobs

A job is a command line written as a mapping:

- **What it does**, one of:
  - `crawl: URL`: a crawl, like `wintergrab crawl URL`;
  - `goal: TEXT`: a [goal](goals.md), like `wintergrab goal TEXT --yes`;
  - `spider: FILE.py:Class`: your own [spider](spiders.md).
- **The command's options**, named as on the command line without the
  dashes (`max_pages` or `max-pages`):
  - `follow: [a, b]` gives `--follow a --follow b`;
  - `paginate: true` gives `--paginate`;
  - an unknown option is an error that suggests the closest one.
- **`set:`** holds spider settings (`--set NAME=VALUE`).
- **`schedule:`** says when it runs (below). Other keys:
  - `timezone:` for the schedule;
  - `enabled: false` keeps it off the schedule;
  - `start_within:` (see below);
  - `description:` for your own notes.

`defaults` are options every job has unless it says otherwise, where its
command has them (a goal has no `--concurrency`).

`${NAME}` in a webhook's value is the environment variable `NAME`: keep
secrets there, never in the file. A job's options take no variables. They
become its command line, which others on the machine can read. Keep secrets
a job needs in files it reads, such as `proxy_file`.

Each job runs in a process of its own, in the project's directory. Its run
is kept in the project's workspace (see [runs](runs.md)), labelled with the
job's name.

- `wintergrab run` runs every job now, one after the other, their output
  shown.
- `wintergrab run books rated` runs only those jobs.
- The exit status is 1 when a job failed.

## Schedules

| Schedule | When |
|---|---|
| `every 30 minutes`, `every 2 hours`, `every day`, `every 15m`, `hourly`, `daily`, `weekly` | From the last run; the first run right away |
| `daily at 06:00`, `at 18:30` | Every day at that time |
| `weekly on monday at 06:00`, `every friday at 17:00` | Every week |
| `0 */2 * * *`, `30 6 * * mon-fri`, `0 3 1 * *` | A cron expression: minute, hour, day, month, weekday (lists, ranges, steps, names) |
| `once at 2026-10-01 06:00` | One time |

Times are the machine's, or a `timezone:` (`Europe/Berlin`) for the job or
the whole project.

`wintergrab schedule` runs the jobs as they fall due, one after the other,
until stopped. Each job's output goes to `.wintergrab/logs/JOB-TIME.log`,
and when each job last ran is kept in `.wintergrab/schedule.json`.

- A job whose time comes while another runs, runs after it.
- A time that went by while no scheduler was running (the machine was off)
  is made up for as soon as one runs: once, not once per missed time. An
  interval job that is overdue runs at once.
- A job that must run close to its time (a site's quiet hours) says how
  late it may start: `start_within: 1 hour` skips a time it would start
  later than that, and waits for the next one.

`--list` shows when each job runs next. `--once` runs what is due and stops.
Run it from cron or CI instead of keeping a scheduler running: each call
runs what fell due since the one before.

## Webhooks

```yaml
webhooks:
  - url: https://hooks.example/wintergrab
    events: [job_failed, record_updated, quality_degraded]   # default: all but per-page ones
    secret: ${WINTERGRAB_WEBHOOK_SECRET}
    headers: {Authorization: "Bearer ${HOOK_TOKEN}"}
```

Each delivery is an HTTP `POST` of JSON, `{"events": [...]}`. Events are
gathered for a second, at most 100 per delivery. Every event has `event`,
`time`, `origin` and its own fields:

```json
{"events": [{"event": "job_finished", "time": "2026-09-26T05:14:52.660+00:00", "origin": "projdoc",
             "job": "books", "status": "finished", "exit_code": 0, "seconds": 0.6, "run": "run-1",
             "log": null, "stats": {"pages": 16, "items": 16}}]}
```

With a `secret`, the body is signed. `X-Wintergrab-Signature: sha256=<hex>`
is its HMAC-SHA256. Check it before trusting a delivery:

```python
from wintergrab.webhooks import verify

if not verify(request.body, request.headers["X-Wintergrab-Signature"], secret):
    return 403
```

A delivery that fails (no answer, a 5xx or a 429) is tried again after 1
and 4 seconds, then given up (logged). Webhooks never slow a crawl down:
deliveries have their own thread.

What there is to tell ([all event kinds](observability.md#events)):

| From | Events |
|---|---|
| Whatever runs the jobs (`run`, `schedule`) | `job_started`, `job_finished`, `job_failed` (with the run, its stats, its log) |
| Each job's crawl | `crawl_started`, `crawl_finished`, `request_failed`, `blocked`, `budget_exhausted`... |
| A job with a `history`, from its second run | `site_changed` (the counts), and one `record_created`, `record_updated` (with what changed: `{"price": [10.0, 8.0]}`) or `record_deleted` per page |
| A data pipeline's quality monitor | `quality_degraded` |

Outside projects, any spider can post its events:

```python
class Shop(Spider):
    webhooks = [{"url": "https://hooks.example/wintergrab", "events": ["crawl_finished"]}]
```

## In code

```python
from wintergrab.project import Project, Scheduler

project = Project("wintergrab.yaml")
scheduler = Scheduler(project)
scheduler.run(project.jobs["books"])      # now: a JobResult (status, exit_code, run)
scheduler.plan()                          # [(job, next time)...]
scheduler.loop()                          # until stopped
```

## Limits

- Jobs run one at a time. A job that runs longer than its interval delays
  the next runs, instead of running beside itself.
- The scheduler is a process: keep it running (a service, `tmux`), or use
  `wintergrab schedule --once` from cron.
- A webhook's URL is yours: it is not held to the network policy of
  crawls. Deliveries that are never answered are dropped after three tries.
