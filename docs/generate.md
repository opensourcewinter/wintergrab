# Generated scrapers

`wintergrab generate` makes a scraper for one site from a goal in plain
words, then tests it. It keeps the scraper only when every test passes:

```console
$ wintergrab generate "books with title, price, availability and rating" --site books.example -o scrapers/books
plan       ok    8 of 15 pages sampled hold a product (/books/catalogue/*/index.html); generating from 5
generate   ok    selectors for 4 of 6 fields: name h1, price p.price_color, availability p.availability, rating p.star-rating::attr(class)
lint       ok    4 selector(s) compile, 2 URL pattern(s) valid
test       ok    5/5 pages give the expected values (30 values; fixtures/)
sample     ok    15 pages crawled, 12 with a product, 7 not among the samples; 12 records -> sample.jsonl
validate   ok    7 page(s): found name 100%, price 100%, currency 100%, availability 100%, rating 100%, url 100%; the same values as the goal's own extraction: 35/35; quality score 0.9833
benchmark  ok    1.1 ms/page (goal's extraction 1.0); 6.0 fields/page (6.0); confidence 0.94 (0.83)
accepted: scrapers/books/plan.json
  run it:  wintergrab goal --plan scrapers/books/plan.json --yes -o RECORDS.jsonl
  test it: wintergrab test scrapers/books/fixtures
```

(That is the test suite's miniature of books.toscrape.com.) The exit status
is 0 when the scraper is accepted, 1 when it is rejected.

## What it makes

A scraper is a [goal](goals.md)'s plan (where the records are, how to reach
them) and a [schema](data.md#schemas) whose fields have **selectors learned
for this site**:

```json
"price": {"type": "money", "selectors": ["p.price_color"]},
"rating": {"type": "rating", "best": 5, "selectors": ["p.star-rating::attr(class)"]},
"sku": {"type": "string", "selectors": ["//tr[th[normalize-space()=\"UPC\"]]/td"]}
```

It runs through the normal [extractor](extraction.md). The selectors are one
more source of evidence, weighed with the page's structured data, meta tags
and layout. Every value is normalized and validated as before, and a
selector that stops matching leaves the other sources to find the value.
What it adds:

- **Confidence.** A value that a selector written for the site reads, and
  that the page's layout confirms, is surer than a guess from layout alone.
  On the books, the records' mean confidence went from 0.83 to 0.94.
- **No model at run time.** With `--model`, a [language model](models.md)
  finds, on the sample pages, the values the pages do not publish for
  machines (the UPC in a spec table, say). A selector learned from its
  answers then reads them on every other page, without the model.
- **Tests.** The sample pages are kept as [extraction tests](testing.md), so
  a change to the site, the schema or wintergrab shows up as a failing test.

The directory holds:

| File | What |
|---|---|
| `plan.json` | The plan, naming `schema.json`. Run it with `wintergrab goal --plan`. |
| `schema.json` | The schema with its selectors, to read, edit, or use elsewhere (`--extract schema.json`). |
| `fixtures/` | The sample pages and the values expected from them. Run them with `wintergrab test`. |
| `sample.jsonl` | The sample crawl's records. |
| `quality.json` | Their [quality report](data.md#quality): a baseline for `--quality`. |
| `report.json` | Every step: what it did, what it measured, what went wrong. |

## The steps

1. **plan**: survey the site and plan the crawl, as `wintergrab goal` does.
   The plan says which pages hold records.
2. **generate**: read those pages (`--train`, 5) with the goal's fields, and
   learn a selector for each field from the values found:
   - Only values found with confidence are learned from. A model's answer
     counts only when the page holds it.
   - Selectors are proposed from the elements that hold the values, the
     most stable kinds first:
     - an attribute meant for machines (`itemprop`, `data-testid`...);
     - a class or an id;
     - a label beside the element (a table's header cell, a definition
       list's term);
     - the tag;
     - its place under an ancestor with a class;
     - its place on the page.
   - An element in the page's content is preferred to one in a menu or a
     breadcrumb.
   - Each selector is tried on every sample page, through the same code the
     extractor reads selectors with. It is kept only when it reads the
     value found on **all** of them.
3. **lint**: the schema reads back as written, its selectors compile, and
   the plan's URL patterns are valid. A selector that depends on an
   element's position is pointed out.
4. **test**: the sample pages become extraction tests expecting the values
   found on them. The generated schema must read them all, without a model.
5. **sample crawl**: the plan runs with the generated schema as a real
   crawl, obeying robots.txt and rate limits, for record pages beyond the
   samples (`--test`, 10).
6. **validate**: the [quality system](data.md#quality) measures what it
   read on the pages it was not generated from:
   - every required field must be found on `--min-completeness` (0.9) of
     them;
   - every field the goal asks for must be found on some page;
   - values must be valid;
   - where the goal's own extraction finds a value, the scraper must find
     the same, for `--min-agreement` (0.9) of the values.
7. **benchmark**: the scraper and the goal's own extraction on the same
   pages: time per page, fields found per page, confidence. It must find no
   fewer fields.
8. **accept or reject**, with the reasons.

A rejected scraper's files are kept, with `report.json` saying why:

```console
$ wintergrab generate "books with title, price and brand" --site books.example -o scrapers/brand
...
validate   FAIL  7 page(s): found name 100%, price 100%, currency 100%, brand 0%, url 100%; ...
                the goal asks for brand: found on no page
rejected: validate: the goal asks for brand: found on no page
```

## Options

| Option | Default | What |
|---|---|---|
| `-o DIR` | (required) | Where to write the scraper: a new or empty directory, or an earlier generation's (its files are replaced). |
| `--site URL` | | The site, when the request does not name it. |
| `--model PROVIDER:NAME`, `--model-url URL` | | A model to read the request and find values while generating ([models](models.md)). |
| `--sample N` | 30 | Pages to survey the site with. |
| `--train N` | 5 | Record pages to generate from, spread across those found. |
| `--test N` | 10 | Record pages beyond those to crawl and test on. |
| `--min-completeness SHARE` | 0.9 | For required fields. |
| `--min-agreement SHARE` | 0.9 | With the goal's own extraction. |
| `--browser`, `--timeout SEC` | | As for `wintergrab goal`. |
| `--json` | | Print the report as JSON. |

## In code

```python
from wintergrab.goals import GoalPlan, generate_scraper

result = generate_scraper("books with title, price and rating", "scrapers/books", sites=["books.example"])
print(result.describe())
result.accepted, result.reasons
result.stage("benchmark").details      # {"generated": {"ms_per_page", "fields_per_page", "confidence"}, "goal": {...}}
result.generated.fields["price"]       # LearnedField(status="learned", selector="p.price_color", reproduced=5, ...)

GoalPlan.load("scrapers/books/plan.json").run("books.jsonl")
```

`generate_schema()` is the generation step alone, for pages you already
have:

```python
from wintergrab.extraction import Extractor, generate_schema

generated = generate_schema(pages, "product", model=model)   # Responses, HTML...; a schema or a template
print(generated.describe())
generated.schema.save("shop.schema.json")
Extractor(generated.schema).extract(other_page)
```

## Limits

- **One site per scraper.** Selectors are learned for one site's pages.
  Generate one scraper per site.
- **Values are only as right as what found them.** A selector learns to
  read what the extractor (or the model) found on the sample pages. When
  that is wrong the same way on every page, so is the selector. The report
  lists the values learned from, per page (`report.json`, `generated`), and
  the tests in `fixtures/` say what is expected. Look at them, and correct
  them as you would any extraction test (`wintergrab test --update`).
- **Samples cannot show everything.** A selector checked on five pages in
  stock is not checked on an out-of-stock page. The sample crawl tests
  pages beyond the samples, but only as many as `--test`. More pages give
  more confidence and take more requests.
- **Not learned:** lists, nested objects, the page's own address, and values
  derived from another field (a currency read from a price). The
  extractor's other sources still find them.
- **The plan is the goal's.** When the planner picks the wrong pages, the
  sample crawl finds no record page and the scraper is rejected. The
  message names the patterns the plan looked for. Look at the plan
  (`wintergrab goal ... --plan-only`), then try again with a larger
  `--sample` or a more precise `--site`.
