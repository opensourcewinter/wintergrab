# Runs, recording and replay

Keep a record of each crawl. With `--record`, a crawl also keeps the pages
it received, so it can be crawled again later from those pages, without the
network, and the two sets of items compared. Change a spider or a schema,
replay a recorded crawl, and see exactly what the change does to the data.

```
$ wintergrab crawl https://books.example/books/ --allow /books/ --extract book.schema.json --unique-key url --record -o books.jsonl
finished: 16 pages, 16 items, 0 errors in 0.3s -> books.jsonl
kept as run-1; replay it with: wintergrab replay run-1

$ wintergrab runs
run-1    2026-09-26 04:50  finished  quick            16 pages, 16 items, 0 errors, 0.3 s  [recorded]

$ wintergrab replay run-1
replayed run-1 (16 page(s) from the recording): the same 16 item(s) as recorded
```

The schema's `name` selector is then changed from `h1` to `title`. With the
site gone, the replay still works, and it says what changed (exit status 1):

```
$ wintergrab replay run-1
replayed run-1 (16 page(s) from the recording): +0 added, -0 removed, ~16 changed, 0 unchanged (recorded -> replayed)
+0 added, -0 removed, ~16 changed, 0 unchanged
fields changed:
  name      16 records: All products -> All products | Books to Scrape 4, Book number 1 -> Book number 1 | Books to Scrape 1, Book number 2 -> Book number 2 | Books to Scrape 1
```

(A run against the repository's test site, `tests/testsite.py`; its address
is shortened here.)

## What a run keeps

Runs live in a workspace, `.wintergrab` in the current directory by default
(`--workspace DIR`). Each run has a numbered directory, `runs/run-N`:

| File | What |
|---|---|
| `run.json` | When it started and finished, its status, the spider's settings, its stats and failure diagnoses, where its items went, and how to run it again (the command line, the spider class, or a goal's plan) |
| `events.jsonl` | Its [events](observability.md#events): starts, retries, failures, blocks, budgets, changes... The per-response ones (with their timings) are kept only when recording |
| `items.jsonl` | (recorded) The items it wrote, after the pipelines |
| `archive/` | (recorded) Every response it received: pages, redirects, errors, robots.txt, whatever their status or caching headers. The archive is an [HTTP cache](power-features.md) |

A crawl run with `--record` is kept. Once the workspace directory exists,
every `wintergrab crawl` and `wintergrab goal` run is kept too, without its
pages unless it records. `mkdir .wintergrab` turns that on.

```bash
wintergrab runs                        # the runs, newest first
wintergrab runs run-7                  # one run: its command, stats, failures and files
wintergrab runs last --json
wintergrab runs --remove run-7
```

## Replay

`wintergrab replay RUN` crawls again what the run recorded:

- The run's command line runs again, or its spider class, or its goal's
  plan: a goal replay does not survey the site again.
- The archive is the only source, in the cache's offline mode. Nothing
  reaches the network: no page, no DNS lookup, no robots.txt.
- The items the replay writes are compared with the recorded ones by
  `--key`. That is the spider's `unique_key`, else `url` when every item has
  one, else the whole item.
- The replay's items go to `-o FILE`, or `replay-<time>.jsonl` in the run's
  directory.
- The exit status is 0 when the items are the same, and 1 when they differ.
  In CI, that makes a recorded crawl a regression test for a spider.

The recording decides which pages there are. A run stopped by a page limit
(`max_pages`, `max_requests`, `max_bytes`, `max_runtime`) is replayed without
it: every recorded page is crawled again. The requests that go beyond the
recording are counted, and are not a difference. `max_items` stays, since it
shapes the output.

What a replay does not repeat, it says:

- A request for a page the recording does not have means the spider now
  goes elsewhere. The replay counts these, and they make it differ.
- A healing extractor (`--heal`) is replayed as its active version, and is
  not changed.
- Learned files (`--fetch-stats`, `--optimize FILE`) are not read or
  written. The replay learns afresh.

## In code

```python
from wintergrab.runs import RunRegistry, replay

class Shop(Spider):
    record = True                       # or run_registry=True to keep the record without the pages

result = Shop().run()
print(result.run_id)                     # run-7

for run in RunRegistry().runs(limit=10):
    print(run.describe())
run = RunRegistry().get("last")          # run.stats, run.settings, run.items(), run.events("request_failed")

again = replay("run-7", Shop)            # the same crawl from the archive
print(again.summary())                   # again.same, again.diff (a DatasetDiff), again.missing
```

A spider class that can be imported (not one defined in the `__main__`
script) is found again by its name. Otherwise, pass it to `replay`. The
settings the run was given (`start_urls`, limits...) are applied again, and
the class's own code is what is tested.

## Limits

- The archive holds what the run received: a crawl whose pages need a
  browser is replayed from the pages the browser gave. Timing-dependent
  behavior (retries after a timeout, throttling) is not replayed. The
  events keep the timings.
- Recording keeps every page. A long crawl's archive is as big as its
  pages, compressed. Recording uses its own cache (the run's archive), in
  place of a `cache` the spider set.
- A workspace is written by the processes that run in it. Run numbers stay
  distinct across processes. Delete old runs with `wintergrab runs
  --remove`.
