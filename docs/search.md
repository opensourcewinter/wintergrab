# Search results

`wintergrab search` asks a search API for the results of queries, with the
rest of their pages (news, videos, local results, questions people ask...),
and reads collected results for what they say: which sites compete for them,
where your site is missing, which queries one page can answer, what the
pages show besides web results, and how rankings moved.

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
error that names the variable to check. A page after the first that fails
stops the search with a note, and the pages before it are kept. A key never
appears in a message: Google's travels as a parameter, and is replaced by
`***` in errors.

## Where, and in what language

Rankings differ from one country and language to another. `--param
NAME=VALUE` (repeatable; `params=` in Python) passes the API's own
parameters:

| Provider | Where | Language | More |
|---|---|---|---|
| Brave | `country=de` | `search_lang=de`, `ui_lang=de-DE` | `freshness=pw` (the past week), `safesearch=off` |
| Google | `gl=de` | `hl=de`, `lr=lang_de` | `cr=countryDE`, `dateRestrict=w1` |
| SearXNG | | `language=de` | `categories=news`, `time_range=month`, `safesearch=0` |

```bash
wintergrab search "günstiger laptop" --param country=de --param search_lang=de -o serp-de.jsonl
```

Those wintergrab sets itself (the query, its pages, the key) are refused.
Each result keeps the parameters it was searched with (`params`), and the
analyses below tell apart a query searched with others: `budget laptop
[country=de]` and `budget laptop` are two searches.

An answer is read without a fixed schema. Its results are the provider's
list (`web.results[]`, `items[]`, `results[]`), or, for another API, the
list whose records have a URL and a title. Their snippet is the first of
`description`, `snippet`, `content`... Positions run across pages: the
first result of page two follows the last of page one.

Each result is a record: `query`, `type` (`web`), `position`, `rank` (its
place on the page, below), `url`, `domain` (the registrable domain:
`www.shop.example` is `shop.example`), `title`, `snippet`, `date` when
given, `source` (the provider) and `fetched`. `-o` writes them to any
[output](storage.md): JSON Lines, CSV, a database...

## The rest of the page

A page has more than its web results, and these are records too, with
their `type`, where the provider gives them:

| `type` | What | From |
|---|---|---|
| `news`, `video`, `discussion` | News, videos and forum threads, shown as results of their own | Brave; SearXNG's results of those categories |
| `location` | Local results, with `address`, `telephone`, `rating`, `reviews`, `latitude`, `longitude`, `price_range` | Brave; SearXNG's map results |
| `question` | A question people ask (its title), its answer (its snippet) and source | Brave's FAQ |
| `infobox` | A box about what the query names | Brave, SearXNG |
| `answer` | A direct answer | SearXNG |
| `promotion` | The engine's own promotion | Google |
| `related` | A related search (its title) | SearXNG's suggestions |
| `correction` | The query as the provider corrected it (and searched it) | Brave, Google, SearXNG |

Videos add `duration`, `creator`, `publisher` and `views`; discussions
`forum` and `answers`; news `publisher`, and `breaking` when it is. Results
of other SearXNG categories keep theirs as their type (`image`, `science`...).

A result's `position` counts the results of its type: the second news
result is news #2. Its `rank` is its place among all the page's results and
boxes, where the provider gives their order: Brave says where each box goes
(its `mixed` order), and a single list (Google's, SearXNG's) is in page
order. A box shown whole, like a row of news, is one place; a box beside the
results (Brave's infobox) has none.

Without `-o`, a search prints its results, then a line for each kind of box
in its order on the page:

```
budget laptop  (4 results)
    1. laptops.example              Best budget laptops
    2. reviews.example              Cheap laptops, reviewed
    3. shop.example                 Laptops | Shop
    4. blog.example                 Budget buys
  news (place #2): Laptop prices fall (news.example); Back to school (reviews.example)
  location (place #4): Laptop Shop (shop.example)
  video (place #5): Budget laptop review (video.example)
  discussion (place #6): Which laptop? (forum.example)
  asked: Is 8 GB enough?
  infobox: Laptop (encyclopedia.example)
```

(Two pages of the tests' Brave stand-in, `tests/test_serp.py`, which answers
in the shape Brave documents.)

## What results say

`--report FILE` reads results: those collected here, or any records with a
query, a position and a URL (a rank tracker's export works, and `rank` and
`keyword` are read too; a record without a `type` is a web result).

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

Besides web results, the pages have:
  related      in 3 of 3 queries
  answer       in 2 of 3 queries: answers.example (1)
  correction   in 1 of 3 queries
  infobox      in 1 of 3 queries: encyclopedia.example (1)
  news         in 1 of 3 queries, place #2 on the page: news.example (1)

Gaps: 2 queries where rivals rank and shop.example does not:
  cheap laptop                             reviews.example #1, laptops.example #2
  gaming mouse                             mice.example #1, reviews.example #2
```

(`wintergrab search --report serp.jsonl --domain shop.example` on the
results the tests' SearXNG stand-in gives.)

- **Visibility**: over the queries searched, the sum of 1 / a domain's best
  position for each, divided by the number of queries. 1.0 is first for
  every query. It is a weight on rankings, not an estimate of clicks.
- **Competitors**: the domains in the top `--depth` (10) web results, the
  most visible first; `--domain` (yours) is left out.
- **Queries one page can answer**: queries whose top results share at least
  three pages. Each query joins the first group whose first query it shares
  them with, so groups do not chain unrelated queries.
- **Besides web results**: each kind of box, for how many queries' pages,
  its place on the page on average where the provider says, the domains in
  it most often (with the number of queries), and how often `--domain` is.
- **Gaps**: queries where your domain is not in the top results and one of
  the three most visible competitors is.
- **Changes** (`--before FILE`): each query's best position for your domain
  in both collections: up, down, new or lost.

## Rankings over time

The results of one search share their `fetched` time: they are one
collection. Search again later into the same output with `--append` (or
into a database), and the report reads each query's latest collection for
all of the above, and shows your domain's history:

```bash
wintergrab search "budget laptop" "cheap laptop" "gaming mouse" -o serp.jsonl --append    # each week
wintergrab search --report serp.jsonl --domain shop.example
```

```
29 results for 3 queries, searched up to 3 times (2026-09-12 to 2026-09-26): what follows reads each query's latest
...
History of shop.example, oldest first (the last 8 searches; -: not in the results):
  budget laptop                            - 3 3                    best #3
  cheap laptop                             - 4 -                    best #4, lost
```

The history lists the queries your domain ranked for in any of them (in
the top 100), and the last move: up, down, new or lost. `--before FILE`
compares with the results of another file instead.

## In Python

```python
from wintergrab.data.io import read_records
from wintergrab.intel.serp import (competitors, cluster_queries, gaps, modules, overlap, ranking_changes,
                                   ranking_history, read_results, search)

answer = search("budget laptop", provider="searxng", endpoint="https://searx.example", pages=2)
answer.results         # the web results
answer.modules         # the rest of the page: news, local results, questions, related searches...
results = read_results(read_records("serp.jsonl")) + answer.results + answer.modules
competitors(results, domain="shop.example")
modules(results, domain="shop.example")     # [Module(type="news", queries=1, average_rank=2.0, ...), ...]
ranking_history(results, "shop.example")    # [{"query": ..., "positions": [{"fetched": ..., "position": 3}, ...]}]
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
- The rest of the page is what the API gives of it: Google's Programmable
  Search API has no news or local results, and SearXNG's are those of the
  engines your instance asks.
- Brave's and Google's APIs have quotas and prices; SearXNG's results are
  those of the engines your instance asks.
- Google's API gives 100 results at most for a query (its `start` plus
  `num` may not pass 100, the documented limit): ten pages give 99, the
  last asking for nine.
- Two searches of one query in the same second are one collection: keep
  `--delay` at a second or more when a query comes twice.
