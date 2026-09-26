# Self-healing extractors and the review queue

Sites change their markup, and selectors stop matching. A healing extractor
notices and looks for a replacement. It tests the replacement on the pages
where the selectors failed, and applies it only when the test convinces it
and other evidence on the pages confirms the values. Otherwise it asks a
person. Every change is a version you can look at, compare and roll back.

```bash
wintergrab crawl https://shop.example --extract product.schema.json \
    --heal extractors/shop-product --review shop.reviews.jsonl -o products.jsonl
wintergrab heal extractors/shop-product          # versions, health, repairs
wintergrab review shop.reviews.jsonl             # what needs a person
```

The schema is the one of [extraction](extraction.md): a healing extractor
watches the fields that have `selectors`. The schema is needed only the
first time; afterwards the directory holds it (`--heal DIR` alone works).

## What happens when a site changes

Here a shop is redesigned after 15 pages. The title moves from
`h1.title` to `h1.product-title`, the price from `.price` to
`div.cost > b`, and the seller from `span.seller-name` to `a.vendor`. The
pages still carry JSON-LD with the name and the price, but not the seller:

The log says:

```
WARNING wintergrab.extraction: self-healing: name repaired as version 2 (repair of name: h1.title -> .product-title (anchored))
WARNING wintergrab.extraction: self-healing: price repaired as version 3 (repair of price: .price -> div.cost > b (anchored))
```

```
$ wintergrab heal extractors/shop-product
extractors/shop-product: product@3, version 3 of 4
  v1 retired     initial initial
  v2 retired     auto    repair of name: h1.title -> .product-title (anchored)
  v3 active      auto    repair of price: .price -> div.cost > b (anchored)
  v4 candidate   auto    repair of seller: .seller-name -> .vendor (relocated)
  name: selectors matched 100% at first, 100% on the last 10 page(s) of the last run
  price: selectors matched 100% at first, 100% on the last 10 page(s) of the last run
  seller: selectors matched 100% at first, 0% on the last 10 page(s) of the last run
```

The name and the price were repaired on their own: new selectors read, on
the pages where the old ones failed, the values the JSON-LD still gives.
The seller's new element looks like the old one, but nothing else on the
pages says who the seller is. So the repair waits for a person:

```
$ wintergrab review shop.reviews.jsonl
r1  repair of seller: .seller-name -> .vendor (relocated)
    A: .vendor  (70% via relocated; e.g. 'Umbrella', 'Acme', 'Globex')
    B: a.vendor  (70% via relocated; e.g. 'Umbrella', 'Acme', 'Globex')
    C: div.seller-box > a  (70% via relocated; e.g. 'Umbrella', 'Acme', 'Globex')

1 item(s) to review: --accept ID [--choice B], --reject ID, --correct ID VALUE

$ wintergrab review shop.reviews.jsonl --accept r1 --choice B --note "the link, not its box"
r1: accepted ('a.vendor')
the extractor applies it the next time it runs
```

The next run with the same `--review FILE` applies the decision, or
`wintergrab heal DIR --review FILE` does:

```
$ wintergrab heal extractors/shop-product --review shop.reviews.jsonl
extractors/shop-product: product@5, version 5 of 5
  v1 retired     initial initial
  v2 retired     auto    repair of name: h1.title -> .product-title (anchored)
  v3 retired     auto    repair of price: .price -> div.cost > b (anchored)
  v4 accepted    auto    repair of seller: .seller-name -> .vendor (relocated)
  v5 active      human   repair of seller: -> a.vendor (accepted in review r1)
  ...
$ wintergrab heal extractors/shop-product --diff 3 5
~ seller: selectors ['.seller-name'] -> ['a.vendor', '.seller-name']
```

(Generated pages of the shape used in `tests/test_healing.py`.)

## Noticing a breakage

For each field with selectors, the extractor counts the pages where the
selectors found the field's value. They count when their value won, agreed
with the winner, or was one of the alternatives. The **baseline** is the
share on the first 10 pages (`min_pages`). It is kept in the directory for
later runs, for every set of selectors the field has had, so a later run
notices a change within 10 pages. A field is **broken** when, over the last
10 pages, its selectors matched at most half as often as at first
(`drop=0.5`), with at least 3 failing pages to test a replacement on.

Fields whose selectors matched on fewer than half of the first pages are not
watched: they were never reliable, and their misses say nothing. A repair is
not tried again for 40 pages after an attempt.

## Finding a replacement

Two ways, on up to 6 of the pages where the selectors failed (the extractor
keeps the last 12):

1. **Relocation.** The extractor remembers a few elements the selectors
   matched: their tag, attributes, text, position and neighbours, the
   fingerprints of [adaptive selectors](adaptive-selectors.md). On a
   failing page it finds the element most like them (50% alike at least).
2. **Anchoring.** Other strategies (JSON-LD, meta tags, the DOM
   conventions...) may still find the value on those pages. The element
   whose text holds that value is a candidate. This is stronger evidence
   than resemblance.

For each element it writes selectors that should hold on other pages built
from the same template. It uses stable attributes (`itemprop`,
`data-testid`, `name`...), classes, the id, and the parent's class
(`div.cost > b`). It skips names that look generated (long numbers, hashes,
`css-`/`sc-`/`jsx-` prefixes). It keeps only selectors whose first match on
the page is that element.

## Testing it before anything changes

Each candidate (20 at most) reads the failing pages on its own:

| Measure | |
|---|---|
| coverage | the share of the pages where it reads a valid value (typed and validated like the field) |
| agreement | the share of those where another strategy found the same value (1 when none found one) |
| plausibility | whether the values look like the field's past values: one constant where values used to differ scores 0.3; numbers must be within a fifth and five times the past range, text of a similar length |
| fixtures | 0 if it reads a page people confirmed differently (see below) |
| similarity | how much its element resembles the old one (relocation), or 0.95 (anchoring) |

The score is their product, and it decides what happens:

| Score | What happens |
|---|---|
| 0.9 or more (`auto_apply`), and other strategies confirm its values on 2 pages or more | a new version is made active, by `auto`, with the new selector in front of the old ones |
| 0.5 or more otherwise | a `candidate` version, and a `repair` item in the review queue |
| below 0.5, or no candidate | a `broken` item in the review queue |

Resemblance alone never repairs a field: an element that looks like the old
one may hold something else.

There is one question per field at a time. The same question is not asked
twice. A newer one (a different selector, or a candidate where there was
none) replaces the older one, which is closed as rejected by `auto`, and
its candidate version is marked `superseded`.

## Probation and rollback

An automatic repair is on probation for the next 10 pages. The new
selectors must match at least half as often as they did on the failing
pages. If they don't, the repair is rolled back and a person is asked (the
review item says why):

- if the repair is the active version, the version it was made from is
  active again;
- if other repairs came after it, a new version restores that field's
  selectors and keeps the other repairs.

The repair's version is marked `rolled back`. A repair the run had no time
to judge is judged by the next run.

## The review queue

`ReviewQueue` is a JSON Lines file: items, then decisions, with who decided
and when. Items are of three kinds:

| Kind | When | Candidates |
|---|---|---|
| `value` | a value found with less than `review_below` (0.5) confidence, with one or two other values found (more, as on a listing, is not a question for a person); at most 20 per field and run | the values, their confidence and methods; the page is kept (compressed, 400 KB at most) |
| `repair` | a candidate selector that was not applied, or a repair rolled back | the selectors, their scores and example values |
| `broken` | selectors that stopped matching, with no convincing candidate | the best tries, if any |

Decide with `wintergrab review FILE --accept ID [--choice B]`, `--reject
ID`, or `--correct ID VALUE` (the right value, or the right selector for a
repair), with an optional `--note`. In code, use `queue.decide(id,
"accept" | "reject" | "correct", choice=, value=, note=)`.

The extractor acts on decisions when it starts:

- an accepted or corrected **repair** becomes a new active version, by
  `human`, on top of the active version (other repairs made since are
  kept); a rejected one marks its candidate version `rejected`;
- an accepted or corrected **value** becomes a **fixture**: the page and
  the confirmed value, kept in `fixtures/`. A later candidate that reads
  a fixture page differently scores 0. `wintergrab heal DIR --check` runs
  the fixtures against the active version (exit code 1 when one differs).

A decision is applied once. The log says which.

## Why is this field empty?

`wintergrab get URL --heal DIR --why FIELD` prints the record, then on
stderr what the page shows and the likely causes, as for
[any extractor](extraction.md#why-is-this-field-empty). It adds the healing
extractor's own story: the element most like the one the selectors used to
match, the field's health, and the repairs made or waiting
(`extractor.why(field, page)` in code):

```
seller (string) in product@3, version 3 on https://shop.example/p/41: empty
  seen:
    selector .seller-name: 0 element(s) on this page
    no strategy found a candidate value
  why:
    possibly: the page's layout changed (the field's selectors find nothing), or the page does not state its seller
  healing:
    the element most like the one the selectors used to match: <a class='vendor'> 'Globex' (70% alike)
    selectors matched on 100% of the first pages, 0% lately
    repair (queued): .vendor
    waiting for review: r1 (repair)
```

## In code

```python
from wintergrab.extraction import HealingExtractor, ReviewQueue

with HealingExtractor("extractors/shop-product", schema="product.schema.json",
                      review="shop.reviews.jsonl") as extractor:
    for response in responses:
        record = extractor.extract(response)       # like Extractor.extract
    print(extractor.status())
    print(extractor.why("seller", response))

queue = ReviewQueue("shop.reviews.jsonl")
for item in queue.pending():
    print(item.describe())
queue.decide("r1", "accept", choice=1)

versions = extractor.versions                        # ExtractorVersions
versions.diff(3, 5); versions.history(); versions.rollback(reason="...")
versions.activate(2, reason="..."); versions.check_fixtures()
```

`HealingExtractor(directory, schema=None, *, review=None, auto_apply=0.9,
min_pages=10, drop=0.5, keep=12, review_below=0.5, max_value_reviews=20,
**extractor_options)` takes the options of `Extractor` too. `extract()`
never fails because of healing: a problem while watching a page is logged
and the record returned. `extractor.repair(field)` tries a repair at once
and returns a `RepairResult` with every candidate and its test.

In a [spider](spiders.md), use it where you would use an `Extractor`, and
close it at the end (`close()` keeps what it learned). `wintergrab crawl
--heal` does that, and so does `wintergrab goal --heal DIR`, which also
keeps the first complete record of each site as a fixture
([the whole loop](goals.md#the-whole-loop)). On the command line, `--heal
DIR` without `--review` puts the questions in `DIR/review.jsonl`.

## Cost

Watching adds about 0.3 ms per page: 8.02 ms instead of 7.68 ms for an
11 KB product page with three of thirteen fields read with selectors
(`benchmarks/bench_pages.py`, one core of a 4-vCPU cloud VM). A repair
attempt tests at most 20 selectors on at most 12 kept pages, once per field
every 40 pages. The kept pages are held in memory.

## The directory

| File | |
|---|---|
| `v1.json`, `v2.json`... | the schema of each version |
| `versions.json` | which version is active; each version's reason, author (`initial`, `auto`, `human`), status (`active`, `retired`, `candidate`, `accepted`, `rejected`, `superseded`, `rolled back`), parent, and the test its repair passed |
| `repairs.jsonl` | every attempt (`applied`, `queued`, `waiting`, `flagged`), confirmation, rollback, fixture and decision applied |
| `state.json` | baselines, remembered elements, the last run's health, repairs on probation |
| `fixtures/` | pages with confirmed values: those a person chose in the review queue, and (`by`: `goal`) the first complete record of each site a goal run read |
| `review.jsonl` | the questions for a person, and the decisions, unless `--review FILE` puts them elsewhere |

`wintergrab heal DIR --log` prints the log, `--rollback` goes back to the
version the active one came from, `--activate N` makes a version active,
`--import SCHEMA` adds a version from a schema file (by `human`), and
`--note` says why.

## Limits

- Only fields read with selectors are watched and repaired. Fields that the
  other strategies find need no selectors to repair.
- Detail pages are watched (`extract`), not listings (`extract_all`).
- The pages a repair is tested on are those of the current run, kept in
  memory. A breakage is repaired in the run that sees it.
- A repair changes selectors, not types or conditions. A person can
  always say no, and every change can be rolled back.
- A directory and a review file are written by one process at a time.
