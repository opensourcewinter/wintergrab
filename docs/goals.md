# Goals: say what data you want

Tell wintergrab what you are after, in plain words. It works out what kind
of records you mean, surveys the site, shows you a plan with what it will
cost, and collects the records.

```bash
wintergrab goal "Find all books rated 4 stars or more with title, price and rating" --site https://books.example/books/
```

```
Understood: products with name, price, currency, rating, url
            on https://books.example/books/
            in the section(s): books
            where rating >= 4   (rated 4 stars or more)
            duplicates removed
Surveying https://books.example/books/: robots.txt, sitemaps, 30 pages...

https://books.example/books/  (follow)
  1. robots.txt allows crawling; 3 sitemap(s) list 8 pages.
  2. Follow links from /books/ through /books/** and pagination to the product pages (/books/catalogue/*/index.html), over HTTP.
  3. Extract name, price, currency, rating, url: sampled product pages gave name 12/12, price 12/12, rating 12/12.
  4. Keep the records where (rating >= 4).
  5. Remove duplicates: records with the same URL (pages that name their canonical URL count once).
  Estimates:
    pages with records: at least 12, plus 3 listing pages
    requests: 16 (none in a browser)
    download: 12.1 KB; time: 0 s; CPU: 0 s
    records: about 4 (558 bytes as JSON Lines)
4 record(s)
fields found: name 100%, price 100%, currency 100%, rating 100%, url 100%
left out: 8 not meeting the conditions
15 page(s) fetched, 12 with a record, 0 error(s)
```

(A run against the repository's test site, `tests/testsite.py`, a miniature
of books.toscrape.com with twelve books; its address is shortened here, and
the crawl's log lines are left out.)

The records go to stdout as JSON Lines, or to a file with `-o books.jsonl`
(`.csv` and `.json` work too). Each has the fields asked for and its
`_confidence` (see [extraction](extraction.md)).

## What a request can say

| Part | Examples | Becomes |
|---|---|---|
| The kind of record | products, articles, posts, news, jobs, vacancies, events, concerts, companies, businesses, restaurants, hotels, shops, dentists, people, speakers, reviews, recipes | `goal.entity`: `product`, `article`, `job`, `event`, `company`, `place`, `person`, `review`, `recipe` |
| Fields | "with name, price and rating", "extract title, author and date", "and their contact details" | `goal.fields`, with synonyms: "stock status" is `availability`, "phone number" is `telephone`, "headline" is `title`, "contact details" is `telephone`, `email` and `address` |
| Sites | `shop.example`, `https://shop.example/c/phones`, or `--site URL` | `goal.sites`; a URL with a path is the part of the site to cover |
| Prices | "under $1000", "between €100 and €300", "that cost up to 50 dollars" | `price < 1000 and (currency is None or currency == 'USD')` |
| Ratings | "rated 4 stars or more", "with a rating above 4.5" | `rating >= 4` |
| Dates | "published after 2025-01-01", "in the last 30 days", "this month", "in 2025", "before March 2024" | `date(published) >= '2026-08-27'` (on the entity's date field: published, date posted, start date...) |
| Stock | "in stock", "out of stock" | `availability == 'InStock'` |
| Places | "in Berlin", "near Paris" (for places, companies, events, jobs, people) | `icontains(city, 'Berlin')` |
| A section | "all laptops", "in the Phones category" | `goal.scope`: sections of the site with that name |
| A limit | "the first 50", "top 200", "100 products" | `goal.limit` |
| Watching | "track changes", "monitor daily" | `goal.monitor`: the pages' history is recorded |

When no kind of record is named, the fields decide (a price means
products). When no field is named, each kind has defaults (products: name,
price, currency, availability, url). What the reading does not understand
it says in `goal.notes`, and `url` is always among the fields.

Conditions are expressions of the [data layer](data.md#expressions): a
record without the value (no rating) does not meet a condition on it, and
prices are compared in their own currency (converting needs exchange rates,
which wintergrab does not fetch: see `ConvertCurrency`).

The rules are in `wintergrab.goals.goal`. A [language model](models.md) can
read requests instead: `--model PROVIDER:NAME` on the command line, or any
function that returns the goal as a dict. The answer is checked like any
other: it must name a known kind of record, and its conditions must compile.
A site the request does not name is dropped, since a model may invent one.
When the answer cannot be used, the rules read the request, and the notes
say so.

```bash
wintergrab goal "espresso machines with a steam wand under 300 euros on coffee.example" --model ollama:NAME
```

```python
from wintergrab.goals import model_reader
from wintergrab.models import load_model

goal = parse_goal("well paid remote jobs on jobs.example", parser=model_reader(load_model("ollama:NAME")))
```

A model helps with what the rules read poorly: the part of a site
("espresso machines"), and conditions phrased in their own words ("with a
steam wand", "remote").

## The plan

`plan_goal(goal)` surveys each site: robots.txt, the sitemaps, and 30
pages. Those are the start page, pages spread across the sitemaps, and the
pages their links lead to. Pages in the part of the site the goal names come
first, then pages that look like the goal's records, then their listings. It
learns from the sample:

- which pages hold the records: classified as the kind's pages (product,
  article, job...), or, for pages that list nothing, giving a record with its
  name and half the other fields; weak classifications (under 25%) are left
  out, and a page with a grid of cards is a listing even when its URL says
  otherwise;
- their URL pattern (`/p/*`, `/books/catalogue/*/index.html`), the listing
  pages that lead to them, and whether they need a browser;
- how well the fields come out, and how many sampled records meet the
  conditions.

Then it picks a strategy:

- **sitemap**: the sitemaps list pages of the records' pattern: those are
  fetched, and their number is known;
- **follow**: otherwise, links are followed from the start page (or the
  sections the goal names) through listings and pagination to the record
  pages. Within a section, record pages lead no further, so "related
  products" links do not take the crawl out of it. The number of pages is
  then a lower bound.

The estimates (pages, requests, browser pages, download, time, records,
CPU, storage) rest on the sample: its latency, page sizes, extraction time
and the share of records meeting the conditions, robots.txt's crawl delay,
and the spider's concurrency. `--explain` (or `plan.explain()`) prints what
each rests on. Pages that need a browser are assumed to take 2 s each,
eight at a time: that one is not measured.

A plan is JSON: save it, read it, edit it (the patterns, the start URLs,
the conditions), and run it later:

```bash
wintergrab goal "articles from news.example published in 2025" --plan-only --save-plan news.plan.json
wintergrab goal --plan news.plan.json --yes -o articles.jsonl
```

Before a plan of more than 200 requests (`--confirm-over`) or 50 browser
pages runs, `wintergrab goal` asks; when it cannot ask (no terminal), it
needs `--yes`. robots.txt is obeyed, and a site whose robots.txt keeps
crawlers out is not crawled.

## Records from the site's API

Many sites build their pages in the browser from a JSON API: the HTML is an
empty shell until the page has asked the API for its records. The planner
looks for that API. It renders up to three of the sampled pages that need
JavaScript (every sampled page with `--browser`) and records the calls they
make, as [`get --sources`](sources.md) does. If a call answers with the
goal's records, with their name and at least as many of the goal's fields as
the pages give, and its next page can be asked for, the plan collects from
it: one request per page of the API, over HTTP, instead of one per record
page.

```
$ wintergrab goal 'laptops under $800 with name, price and rating' --site http://127.0.0.1:40263 --plan-only
...
http://127.0.0.1:40263  (api, else sitemap)
  1. robots.txt allows crawling; 1 sitemap(s) list 10 pages.
  2. Ask the API the site's pages call as they render, over HTTP: GET 127.0.0.1/api/laptops?page&per_page: 4 record(s) a page (pages by page: 3 in all). If it fails without refusing, fetch the 10 product pages the sitemaps list (/p/*), adaptive fetching: HTTP, and a browser for the pages that need JavaScript.
  3. Read name, price, currency, rating, url from each record (name <- title, price, currency <- price.currency, rating <- rating.average, url): the page's answer gave name 4/4, price 4/4, rating 4/4.
  4. Keep the records where (price < 800 and (currency is None or currency == 'USD')).
  5. Remove duplicates: records with the same URL (pages that name their canonical URL count once).
  Estimates:
    pages with records: 3
    requests: 4 (none in a browser)
    ...
```

(The test shop of `tests/test_goal_api.py`, on a local port: its catalog is
an app shell that asks `/api/laptops` for ten laptops, four a page, and each
laptop also has a page of its own, listed in the sitemap.) Its run:

```
5 record(s)
fields found: name 100%, price 100%, currency 100%, rating 100%, url 100%
left out: 5 not meeting the conditions
10 record(s) from 3 page(s) of the site's API
3 page(s) fetched, 0 error(s)
```

Reading the same laptops from their pages took 12 requests (robots.txt,
the sitemap and ten pages); pages that need a browser would each have taken
one.

How the API is asked:

- the way the page asked it: the same URL, method and body, with the page
  parameter changed (a page number, an offset, a cursor from the last
  answer, or the next page's URL the answer gives);
- only reading calls: GETs and GraphQL queries. A POST that is not a GraphQL
  query, or a GraphQL mutation, may change something, and is not used;
- no header the page added is sent again, and an API the page called with a
  key or a token in its URL or body (`api_key`, `accessToken`...) is not
  used: a plan never keeps a credential, nor sends one again;
- robots.txt, the network policy, the crawl's throttling and budgets apply as
  to any page, and the planner does not take an API robots.txt forbids.

Each field is read from the first of its names whose values read as the
field's type: `price` from `price`, `price.amount`, `amount`...; `rating`
from `rating`, `rating.average`, `stars`...; `url` from `url`, `link`,
`href`... (`API_NAMES` in `wintergrab.goals.api`, besides the field's
aliases and its schema.org names). Links in an answer are read as the page
that asked would read them. The mapping is in the plan's JSON (`"api"`), to
check or edit, and the plan's warnings say why an API the pages called was
not used.

When the API does not work out:

| What happens | What the run does |
|---|---|
| It is not found or fails, or its first answer is not JSON or holds no records | Reads the site's pages as planned, with a note saying why |
| robots.txt forbids it (it changed since the plan was made) | The same |
| It refuses: 401, 403, 429, 451, or a bot check | Stops, with a note. The site has answered, and it is not asked another way |
| It fails on a later page | Keeps what it read, and notes where it stopped, and how many records the API said it has |

`--no-api` reads the pages instead (`plan_goal(goal, api=False)`,
`plan.run(use_api=False)`, `wg.plan(..., api=False)`).

## In code

`WinterGrab` is one entry point for goals, pages and sites, with settings
shared by all of them: a network policy, a cache, a browser, a model to
read requests with, and any [spider setting](spiders.md#settings-reference).

```python
from wintergrab import WinterGrab

wg = WinterGrab(network_policy="public")         # every survey, run and fetch refuses internal addresses
plan = wg.plan("Find all laptops under $1000 on shop.example with name, price and rating")
print(plan.describe())                           # the steps, and what they will cost
result = wg.run(plan, "laptops.jsonl")           # or wg.run("Find all laptops ...") at once
result = await wg.arun(plan)                     # the same from async code

page = wg.get("https://shop.example/p/1")        # a Response (rendered, with browser=True)
wg.extract(page, "product")                      # a typed record, from a template or a schema
wg.sources("https://shop.example/catalog")       # where a page's data is
wg.inspect("https://shop.example")               # robots.txt, sitemaps and a site profile
wg.configure(browser=True).sources("https://shop.example/catalog")   # a copy, one setting changed
```

Its methods return what the rest of wintergrab uses (`GoalPlan`,
`GoalResult`, `Response`, `SiteSurvey`...), so every lower level stays in
reach. The same, a step at a time:

```python
from wintergrab.goals import parse_goal, plan_goal

goal = parse_goal("Find all laptops under $1000 on shop.example with name, price and rating")
plan = plan_goal(goal, sample=30)
print(plan.describe())
print(plan.explain())
plan.save("laptops.plan.json")

result = plan.run("laptops.jsonl")       # or plan.run() to keep the records in result.records
print(result.summary())
result.counts                            # records, filtered, duplicates, pages, browser_pages...
```

The run is one [spider](spiders.md) (`GoalSpider`) for all the plan's
sites:

- the extraction engine fills the fields;
- a [data pipeline](data.md) keeps the records that meet the conditions and
  drops those with a URL already seen;
- pages that need JavaScript go to a browser when the survey saw some
  ([adaptive fetching](spiders.md#http-first-a-browser-when-needed));
- record pages are fetched before more listing pages, so a limit ("the first
  40") is reached sooner. On the test site's `/shop/`, 40 products took 50
  pages instead of 81;
- the crawl stops at the goal's limit;
- the crawl [learns what to crawl](spiders.md#learning-what-to-crawl): URL
  patterns that give nothing are skipped, and query parameters that change
  nothing are dropped. For all 150 products of `/shop/`, that meant 197
  pages instead of 226. `optimize=False` (`--no-optimize`) fetches everything
  the plan leads to.

With a goal that watches for changes and an output file, the pages'
[history](history.md) is kept next to it.

A plan can name a schema of its own (`"schema": "schema.json"`, a file next
to the plan, or the schema itself): its records are then read with it rather
than with the goal's fields.

## A scraper of its own

`wintergrab generate "REQUEST" -o DIR` goes further than a plan. It learns
selectors for the goal's fields on the site, tests them on the sample pages
and on a sample crawl, compares them with the goal's own extraction, and
keeps the scraper only when it passes. See [generated scrapers](generate.md).

## Limits

- Requests are read by rules: an unusual phrasing may be misread. The plan
  shows how it was understood before anything big runs; the notes say what
  was assumed.
- The plan rests on a sample: a site whose record pages the sample did not
  reach gets a plan that classifies every page it visits (slower, and the
  count unknown). A larger `--sample` helps.
- The URL tree, the patterns and the sections come from the URLs: a site
  whose URLs say nothing of its structure is crawled by following links.
- An API is found only among the calls of the pages rendered: without
  `--browser`, three at most, among those that look like they need
  JavaScript. It needs Playwright. The API is asked as the page asked it
  first; an API that pages only through headers or through a body that is
  not GraphQL is not followed.
