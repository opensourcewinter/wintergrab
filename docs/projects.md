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
- **`schedule:`** says when it runs (below), **`watch:`** runs it when a
  sitemap, feed or page changes, and **`after:`** runs it after another job
  ([triggers](#triggers-when-something-changes-after-another-job)). Other keys:
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
- `wintergrab dashboard` shows the jobs, when each runs next, and each run:
  see [the dashboard](dashboard.md).

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

## Triggers: when something changes, after another job

```yaml
jobs:
  listing:
    crawl: https://shop.example/
    watch: https://shop.example/sitemap.xml    # runs when it changes
    check: 10 minutes                           # how often it is checked (15 minutes by default)
  details:
    crawl: https://shop.example/products/
    after: listing                              # runs after each successful run of listing
  report:
    goal: laptops under $1000 on shop.example
    after: [details]
```

A **watched URL** can be a sitemap, an RSS or Atom feed, or any page:

- a sitemap is compared by its URLs and their `lastmod`;
- a feed by its items;
- a page by its visible text (scripts and styles aside), a JSON document by
  its data.

A job can watch a **dataset** too: a file of records (`.jsonl`, `.csv`,
`.json`, `.sqlite`, `.parquet`, `.xlsx`, from the project's directory when
relative) or a table's or an object's URL (`postgresql://`, `mysql://`,
`mongodb://`, `s3://`: [what `data` commands read](storage.md)). Its records
are compared by their contents, whatever their order ("3 new records, 1
gone": a record that changed is one gone and one new). The dataset is read
whole at each check, except a file whose size and modification time did
not change since.

```yaml
jobs:
  report:
    goal: laptops under $1000 on shop.example
    watch: postgresql://crawler:${SHOP_DB_PASSWORD}@db.internal/shop?table=products
    check: 1 hour
```

`${NAME}` in `watch:` is read from the environment when it is checked, in
the scheduler's process: its value is never shown, logged or kept, nor is a
password the URL holds itself (shown as `***`).

`wintergrab schedule` checks a URL every `check`, with a conditional
request when the site gave an `ETag` or `Last-Modified` (a `304` costs
nothing). It obeys robots.txt unless the job says `no_robots: true`. The job
runs when the URL changed, and the first time, when it has never run.
Checks that fail (the network, a 5xx, robots.txt) are logged and change
nothing. What the last check found is kept in `.wintergrab/watch/JOB.json`.

**`after:`** names one job or several. The job runs after each run of those
that succeeded, however it started, and the jobs after it follow. A failed
run stops the chain. A circle of jobs is an error. `wintergrab run` without
names starts the jobs that come after no other, and the chains run the
rest.

A job can have several triggers: `schedule: daily at 06:00` and `watch:`
together run it every morning and whenever its sitemap changes.
`job_started` and `job_finished` say which one ran it: `trigger` is
`schedule`, `watch`, `after`, `request` (below) or `manual`, and `reason`
says why (`"3 new URLs, 1 gone"`, `"after listing"`).

```
$ wintergrab schedule --list
listing          when https://shop.example/sitemap.xml changes (checked every 10 minutes) next check: now (due)
details          after listing
report           after details
```

## When asked over HTTP

Other programs can ask for a job: a script, a service's webhook when
something changed there, another project's webhook when one of its jobs
finished.

```bash
export WINTERGRAB_TRIGGER_TOKEN=...            # a long random secret, 16 characters at least
wintergrab schedule --listen 127.0.0.1:8765

curl -X POST -H "Authorization: Bearer $WINTERGRAB_TRIGGER_TOKEN" \
     -d '{"reason": "prices changed"}' http://127.0.0.1:8765/jobs/listing/run
{"job": "listing", "queued": true}
```

| Request | Answer |
|---|---|
| `POST /jobs/NAME/run` | `202`, `{"job": ..., "queued": true}`: it runs as soon as the job running now is done, then the jobs after it. `queued` is `false` when it was already waiting |
| `GET /jobs` | each job, what runs it, when next, and how its last run went |

Every request must carry the token, as a bearer token, or as the
HMAC-SHA256 signature of its body: in `X-Wintergrab-Signature`, as another
project's webhooks sign their deliveries when the token is their `secret`,
or in `X-Hub-Signature-256`, as GitHub's do. Anything else is answered
`401` before the job is looked up (a job that is not there: `404`; one with
`enabled: false`: `409`). The body's `reason`, or the events of a webhook's
delivery, is the run's `reason`; its `trigger` is `request`.

```yaml
# in another project: when one of its jobs finishes, ask for this one's details job
webhooks:
  - url: http://127.0.0.1:8765/jobs/details/run
    events: [job_finished]
    secret: ${WINTERGRAB_TRIGGER_TOKEN}
```

The scheduler listens on the loopback address unless given a host
(`--listen 0.0.0.0:8765`), and speaks plain HTTP: on any other network, put
a TLS proxy in front of it. With `--listen`, a project whose jobs have no
schedule is served all the same.

## Webhooks

```yaml
webhooks:
  - url: https://hooks.example/wintergrab
    events: [job_failed, record_updated, quality_degraded]   # default: all but the per-page ones
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
| Whatever runs the jobs (`run`, `schedule`) | `job_started`, `job_finished`, `job_failed` (with the run, its stats, its log, and its `trigger` and `reason`) |
| Each job's crawl | `crawl_started`, `crawl_finished`, `request_failed`, `blocked`, `budget_exhausted`... |
| A job with a `history`, from its second run | `site_changed` (the counts), and one `record_created`, `record_updated` (with what changed: `{"price": [10.0, 8.0]}`) or `record_deleted` per page |
| A job with `extract`, or a goal | `extraction_failed` per page with no complete record (asked for by name) |
| A job with `quality: FILE` (compared with the last run) | `quality_degraded` (price completeness 98% → 41%), `schema_changed` (fields came or went) |

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
  the next runs, instead of running beside itself; a job asked for waits
  for the one running.
- The scheduler is a process: keep it running (a service, `tmux`), or use
  `wintergrab schedule --once` from cron.
- A webhook's URL is yours: it is not held to the network policy of
  crawls. Deliveries that are never answered are dropped after three tries.
