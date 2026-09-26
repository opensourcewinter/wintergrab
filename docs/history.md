# History: what changed since the last crawl

Give a spider a history file and every run is recorded: each page's
fingerprints, and the items it produced. A run ends by saying what changed
since the previous one, and each page's history tells how often it changes,
so later runs can skip the pages that probably haven't.

```python
class Shop(Spider):
    start_urls = ["https://shop.example/"]
    history = "shop.history"
    ...

result = Shop().run()
print(result.changes.summary())
```

```
+ 1 page
- 1 page
~ 3 pages modified (text 2, structured-data 2, price 1, items 1, availability 1)
= 1 page unchanged
```

```bash
wintergrab crawl https://shop.example --history shop.history -o items.jsonl
wintergrab history shop.history
```

## What is recorded

A `PageSnapshot` keeps hashes rather than the page, about 1.3 KB per page:

| Fingerprint | Changes when |
|---|---|
| `text` | The visible text changed (spacing aside). `text_similarity` tells by how much. |
| `title`, `description` | The title or the meta description changed (before and after are kept). |
| `meta` | Other `<meta>` tags changed. Tokens, nonces and timestamps are left out. |
| `structured`, `types` | The JSON-LD or microdata changed, or its schema.org types did (`schema`). |
| `price`, `currency`, `availability` | The product's price or availability changed, read from schema.org or OpenGraph data, with no extraction schema needed. |
| `layout` | The tag structure changed: a SimHash of the page's tag paths moved by 8 bits or more (`layout_similarity`). |
| `images` | The image URLs changed. Cache-busting queries (`?v=2`) are ignored. |
| `navigation` | The links in `<nav>`, `<header>` or `<footer>` changed. |
| `items` | The items the page produced changed. Fields starting with `_` are ignored. |
| `status` | The HTTP status changed. |

The exact body hash, `ETag` and `Last-Modified` are kept too. A body that
changes without any of the above changing (a new CSRF token, a new nonce)
does not count as a change. With `history_html = True` (`--history-html`)
the HTML is kept as well, compressed.

`snapshot_page(response)` and `compare_snapshots(old, new)` work without a
spider, and so does the store:

```python
from wintergrab.history import PageHistory

with PageHistory("shop.history") as history:
    run = history.start_run("shop")
    history.observe(run, response, items=[record])      # or observe_status(run, url, 404)
    history.finish_run(run, "finished")
    report = history.compare(name="shop")
```

## Change reports

`result.changes` (and `history.compare(old, new)`, which defaults to the
last run and the one before it) is a `ChangeReport`:

- `added`: pages seen for the first time;
- `removed`: pages now answering 404 or 410, or not found by a crawl that
  ran to the end;
- `missing`: pages the run did not reach because it stopped early (a
  `max_pages` limit, a stop). They are not reported as removed;
- `skipped`: pages not fetched because they were still fresh (below);
- `modified`: a `PageChange` per changed page, with `kinds` (`text`,
  `price`, `availability`, `layout`...) and `details`, such as
  `{"price": [299.0, 279.0]}`;
- `unchanged`: how many pages were fetched again with no change.

Each page is compared with its latest snapshot as of the previous run, so a
page skipped in between still compares with what was last seen. The counts
also go to `result.stats["changes"]` and to a `changes_detected`
[event](observability.md). A paused crawl keeps recording into the same run
when it resumes.

## Freshness

For every URL the history keeps `first_seen`, `last_seen`, `last_changed`,
the number of fetches and how many of them found new content.
`history.freshness(url)` turns those into:

- `rate`: estimated changes per day. It uses Cho and Garcia-Molina's
  estimator for pages checked at intervals: a page can change several times
  between two fetches and look changed only once. Half a change is added,
  so that a few unchanged fetches do not mean "never changes";
- `recrawl_after`: seconds after the last fetch when the chance of a change
  reaches one half (`PageHistory(target=0.5)`), kept between 1 hour and 30
  days. A page fetched once is due again after a day;
- `fresh`: the probability that the page is still as last seen, now.

With `skip_fresh = True` (`--skip-fresh`) a spider does not fetch pages
that are not due yet (`history_skipped` in the stats). Start URLs are always
fetched, so a site's hubs keep being read and new pages keep being found.
This works alongside the [HTTP cache](power-features.md#http-cache-and-offline-replay), which turns
re-fetches of unchanged pages into cheap `304 Not Modified` answers.

## Command line

```bash
wintergrab history shop.history                      # the runs, and what the last one changed
wintergrab history shop.history --compare 3 7        # two runs
wintergrab history shop.history --url https://shop.example/p/1   # one page over time, and its freshness
wintergrab history shop.history --due                # pages that have probably changed by now
wintergrab crawl https://shop.example --history shop.history --skip-fresh
```

For a page fetched twice a day apart, whose price dropped in between:

```
run 1    2026-09-25 00:07  status 200; price 10 USD; InStock; first seen
run 2    2026-09-26 00:07  status 200; price 8 USD; InStock; changed: text, structured-data, price, items
2 fetch(es), 1 with changes; 0.92 change(s) per day; look again after 18.2 h; still unchanged now with probability 96%
```

`--json` prints any of these as JSON.

## Speed and size

Recording a page takes about 2.9 ms on one core for an 11 KB product page,
parsing aside: fingerprints 2.4 ms (`snapshot_page` in
`benchmarks/bench_pages.py`) plus the database write. A page with 480 KB of
text takes 16 ms. The history file grows by about 1.3 KB per page per run
without HTML.
