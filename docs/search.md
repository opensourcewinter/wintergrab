# Search results

`wintergrab search` asks a search API for the results of queries, and
reads collected results for what they say: which sites compete for them,
where your site is missing, which queries one page can answer, and what
moved since last time.

```bash
export BRAVE_SEARCH_API_KEY=...
wintergrab search "budget laptop" "cheap laptop" "gaming mouse" -o serp.jsonl
wintergrab search --report serp.jsonl --domain shop.example --before last-week.jsonl
```

## Where results come from

Only search APIs are asked, with your own access:

| Provider | `--provider` | Needs |
|---|---|---|
| Brave's Search API | `brave` (the default) | `BRAVE_SEARCH_API_KEY` |
| Google's Programmable Search JSON API | `google` | `GOOGLE_API_KEY` and `GOOGLE_CSE_ID` (your engine's id) |
| A SearXNG instance of your own | `searxng` | `SEARXNG_URL` (or `--endpoint`), with JSON enabled in its settings (`search: formats: [json]`) |

Search engines' own result pages are not fetched: their terms forbid it.
Each provider's terms and quotas are yours to keep. Requests go one at a
time, a second apart (`--delay`). A 429 is waited out once, as the answer
asks, and then the search stops with a note. A refused key (401, 403) is an
error that names the variable to check. A key never appears in a message:
Google's travels as a parameter, and is replaced by `***` in errors.

An answer is read without a fixed schema. Its results are the provider's
list (`web.results[]`, `items[]`, `results[]`), or, for another API, the
list whose records have a URL and a title. Their snippet is the first of
`description`, `snippet`, `content`... Positions run across pages: the
first result of page two follows the last of page one.

Each result is a record: `query`, `position`, `url`, `domain` (the
registrable domain: `www.shop.example` is `shop.example`), `title`,
`snippet`, `date` when given, `source` (the provider) and `fetched`. `-o`
writes them to any [output](storage.md): JSON Lines, CSV, a database...
Without `-o`, the results are printed, with related searches (SearXNG's
suggestions) and the questions people ask (Brave's FAQ) when given.

## What results say

`--report FILE` reads results: those collected here, or any records with a
query, a position and a URL (a rank tracker's export works, and `rank` and
`keyword` are read too).

```
10 results for 3 queries
shop.example: visibility 0.111 (1.0: first for every query)

Competitors (visibility: the sum of 1/position, over the queries):
  reviews.example                  0.667  in 3 of 3 queries, average #1.7
  laptops.example                  0.5    in 2 of 3 queries, average #1.5
  mice.example                     0.333  in 1 of 3 queries, average #1.0
  blog.example                     0.194  in 2 of 3 queries, average #3.5
  forum.example                    0.083  in 1 of 3 queries, average #4.0

Queries one page can answer (they share results):
  budget laptop | cheap laptop

Gaps: 2 queries where rivals rank and shop.example does not:
  cheap laptop                             reviews.example #1, laptops.example #2
  gaming mouse                             mice.example #1, reviews.example #2
```

(`wintergrab search --report serp.jsonl --domain shop.example` on the
results the tests' SearXNG stand-in gives, `tests/test_serp.py`.)

- **Visibility**: over the queries searched, the sum of 1 / a domain's best
  position for each, divided by the number of queries. 1.0 is first for
  every query. It is a weight on rankings, not an estimate of clicks.
- **Competitors**: the domains in the top `--depth` (10) results, the most
  visible first; `--domain` (yours) is left out.
- **Queries one page can answer**: queries whose top results share at least
  three pages. Each query joins the first group whose first query it shares
  them with, so groups do not chain unrelated queries.
- **Gaps**: queries where your domain is not in the top results and one of
  the three most visible competitors is.
- **Changes** (`--before FILE`): each query's best position for your domain
  in both collections: up, down, new or lost.

In Python:

```python
from wintergrab.data.io import read_records
from wintergrab.intel.serp import competitors, cluster_queries, gaps, overlap, ranking_changes, read_results, search

answer = search("budget laptop", provider="searxng", endpoint="https://searx.example", pages=2)
results = answer.results + read_results(read_records("serp.jsonl"))
competitors(results, domain="shop.example")
overlap(budget_laptop, cheap_laptop)        # {"jaccard": 0.6, "rbo": 0.578}: how alike two lists are
```

`overlap` gives the share of pages two lists have in common (Jaccard) and
their rank-biased overlap, where agreement near the top weighs most (from 0
for nothing alike to 1 for the same pages in the same order).

## Sites for a goal

A goal that names no site can ask the same search APIs which sites rank for
it. The sites are shown for you to pick one: nothing is crawled that you did
not choose.

```
$ wintergrab goal "budget laptop" --find-sites --provider searxng
Sites that rank for it (searxng, 4 results):
   1. laptops.example                  best #1  Best budget laptops
   2. reviews.example                  best #2  Cheap laptops, reviewed
   3. shop.example                     best #3  Laptops | Shop
   4. blog.example                     best #4  Budget buys
pick one: wintergrab goal 'budget laptop' --site laptops.example
```

## Limits

- Results are what the API gives: an API's ranking can differ from the
  search engine's page for the same query, place and language.
- Brave's and Google's APIs have quotas and prices; SearXNG's results are
  those of the engines your instance asks.
- News, images and other kinds of results are not read, only web results.
