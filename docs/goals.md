# Goals: say what data you want

Tell wintergrab what you are after, in plain words. It works out what kind
of records you mean, surveys the site, shows you a plan with what it will
cost, and collects the records.

```bash
wintergrab goal "Find all books rated 4 stars or more with title, price and rating" --site https://books.example/
```

```
Understood: products with name, price, currency, rating, url
            on https://books.example/
            in the section(s): books
            where rating >= 4   (rated 4 stars or more)
            duplicates removed
Surveying https://books.example/: robots.txt, sitemaps, 30 pages...

https://books.example/  (follow)
  1. robots.txt allows crawling; 3 sitemap(s) list 8 pages.
  2. Follow links from /books/ through /books/** and pagination to the product pages (/books/catalogue/*/index.html), over HTTP.
  3. Extract name, price, currency, rating, url: sampled product pages gave name 4/4, price 4/4, rating 4/4.
  4. Keep the records where (rating >= 4).
  5. Remove duplicates: records with the same URL (pages that name their canonical URL count once).
  Estimates:
    pages with records: at least 8, plus 2 listing pages
    requests: 11 (none in a browser)
    download: 8.1 KB; time: 0 s; CPU: 0 s
    records: about 4 (556 bytes as JSON Lines)
4 record(s)
fields found: name 100%, price 100%, currency 100%, rating 100%, url 100%
left out: 8 not meeting the conditions
15 page(s) fetched, 12 with a record, 0 error(s)
```

(A run against the repository's test site, `tests/testsite.py`, a miniature
of books.toscrape.com with twelve books; its address is shortened here.)

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

The rules are in `wintergrab.goals.goal`. A model can read requests
instead, through any function that returns the goal as a dict; the answer
is checked like any other (a known kind of record, conditions that compile):

```python
goal = parse_goal("well paid remote jobs", parser=my_model_reader)
```

## The plan

`plan_goal(goal)` surveys each site (robots.txt, the sitemaps, and 30 pages:
the start page and pages spread across the sitemaps, the ones that look like
the goal's first) and learns from the sample:

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

## In code

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

The run is one [spider](spiders.md) (`GoalSpider`) for all the plan's sites:
the extraction engine fills the fields, a [data pipeline](data.md) keeps the
records that meet the conditions and drops those with a URL already seen,
pages that need JavaScript go to a browser when the survey saw some
([adaptive fetching](spiders.md#http-first-a-browser-when-needed)), and the
crawl stops at the goal's limit. With a goal that watches for changes and an
output file, the pages' [history](history.md) is kept next to it.

## Limits

- Requests are read by rules: an unusual phrasing may be misread. The plan
  shows how it was understood before anything big runs; the notes say what
  was assumed.
- The plan rests on a sample: a site whose record pages the sample did not
  reach gets a plan that classifies every page it visits (slower, and the
  count unknown). A larger `--sample` helps.
- The URL tree, the patterns and the sections come from the URLs: a site
  whose URLs say nothing of its structure is crawled by following links.
