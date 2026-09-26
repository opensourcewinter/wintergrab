# Data: clean, typed, validated records

Scraped values arrive as page text: `"₹29,999"`, `"1.299,- €"`, `"5 March 2024"`,
`"450 g"`, `"Only 3 left!"`, `"N/A"`. The `wintergrab.data` package turns them
into clean, typed, comparable records, checks them, removes duplicates, and
tells you when a dataset's quality drops.

- [Normalizers](#normalizers): one function per kind of value.
- [Schemas](#schemas): declare the record you want; normalize and validate it.
- [Expressions](#expressions): a safe little language for filters and rules.
- [Pipelines](#pipelines): rename, transform, normalize, validate, filter,
  de-duplicate, look up and enrich, in Python or in a config file.
- [Duplicates](#duplicates): exact and near-duplicate records; the same
  company, brand, product, person or place under different names is
  [entity resolution](entities.md).
- [Quality](#quality): completeness, validity, drift and collapse detection.
- [Versions and differences](#versions-and-differences): what was added, removed
  and changed since the last run.
- [Command line](#command-line): `wintergrab data ...`.

Everything here runs locally with the standard library (YAML files need
`pip install "wintergrab[yaml]"`). Nothing calls a web service.

## Normalizers

```python
from wintergrab.data.normalize import (
    parse_money, parse_number, parse_date, parse_quantity, normalize_phone,
    normalize_availability, parse_rating, parse_address, normalize_country,
)

parse_money("₹29,999")                 # Money(amount=Decimal('29999'), currency='INR')
parse_money("1.299,- €")               # Money(amount=Decimal('1299'), currency='EUR')
parse_money("15% off, now $20")        # Money(amount=Decimal('20'), currency='USD')
parse_number("1.234,56")               # Decimal('1234.56')
parse_number("2.3k")                   # Decimal('2300.0')
parse_date("5. März 2024")             # datetime.date(2024, 3, 5)
parse_quantity("5'11\"").to("cm")      # Quantity(value=Decimal('180.34...'), unit='cm')
normalize_phone("(415) 555-2671", country="US")   # '+14155552671'
normalize_availability("Only 3 left!")            # 'LimitedAvailability'
parse_rating("4,5 von 5 Sternen").normalized(5)   # Decimal('4.5')
normalize_country("Deutschland")                  # 'DE'
parse_address("1600 Amphitheatre Parkway, Mountain View, CA 94043, USA").region   # 'US-CA'
```

| Kind | Functions |
|---|---|
| Numbers | `parse_number`, `parse_integer`, `parse_percent`, `find_number`, `iter_numbers` |
| Money | `parse_money`, `parse_money_range`, `detect_currency`, `currency_minor_units` |
| Dates and times | `parse_date`, `parse_datetime`, `parse_duration` (relative dates: "3 days ago") |
| Measurements | `parse_quantity`, `parse_dimensions`, `convert`, `unit_info` (mass, length, volume, area, data, power, energy, electricity, frequency, time, speed, temperature) |
| Contact | `parse_phone`/`normalize_phone` (E.164), `normalize_email`, `normalize_url_value` |
| Places | `normalize_country` (ISO 3166), `normalize_region` (ISO 3166-2), `postal_code`, `parse_address`, `parse_coordinates` |
| Languages | `normalize_language` (BCP 47) |
| Text | `clean_text`, `fix_mojibake`, `mojibake_score`, `has_replacement_characters`, `is_placeholder` |
| Other | `parse_boolean`, `normalize_availability` (schema.org names), `parse_rating` |

### Guesses are written down

A value like `"1,234"` is 1234 in the US and 1.234 in Germany. The
normalizers never pretend to know: pass a `notes` list and they add a code
for every guess they make.

```python
notes = []
parse_money("€1.299", notes=notes)   # 1299 EUR
notes                                # ['separator-from-currency']
```

| Note | Meaning |
|---|---|
| `ambiguous-separator` | `"1,234"`/`"1.234"`: read with the English convention |
| `separator-from-currency` | settled by the currency (euros have 2 decimals, so `"1.299"` is 1299) |
| `ambiguous-currency` | `$`, `¥`, `kr`... could be several currencies |
| `currency-from-country`, `currency-from-default` | taken from `country=` / `currency=` |
| `ambiguous-day-month` | `03/05/2024`: read month-first (pass `dayfirst=True` for 3 May) |
| `two-digit-year`, `relative-date`, `date-in-text`, `no-timezone`, `timestamp` | how a date was read |
| `unit-from-default`, `scale-assumed`, `suffix`, `rounded`, `vanity`, `deobfuscated`, `mojibake-repaired`, `free` | ... |

Knowing the site helps: `decimal=","` for a German shop, `country="SE"` for
`"99 kr"`, `dayfirst=True` for a British site.

## Schemas

A `Schema` declares the fields you want and their types. It is the contract
between extraction and everything downstream.

```python
from wintergrab.data import Schema

schema = Schema.from_dict({
    "name": "product",
    "key": ["url"],
    "fields": {
        "name": {"type": "string", "required": True, "aliases": ["title"]},
        "price": {"type": "money", "required": True, "minimum": 0},
        "currency": "currency",
        "rating": {"type": "rating", "best": 5},
        "weight": {"type": "quantity", "unit": "kg"},
        "tags": "string[]",
        "url": {"type": "url", "required": True},
    },
})

record, results = schema.normalize(
    {"title": " Phone ", "price": "₹29,999", "rating": "9/10", "weight": "450 g", "url": "/p/1"},
    base_url="https://shop.example/",
)
# {'name': 'Phone', 'price': 29999, 'currency': 'INR', 'rating': 4.5, 'weight': 0.45,
#  'tags': None, 'url': 'https://shop.example/p/1'}

schema.validate(record, results)   # [] - or a list of Issues
```

Normalizing a record:

- finds each field under its name or one of its `aliases`;
- converts the value with the field's type (a list for `"type[]"`/`many`);
- moves a price's currency into the schema's `currency` field (or keeps
  `{"amount", "currency"}` together when there is none);
- converts quantities to the field's `unit`;
- keeps, drops or flags fields the schema does not know (`extra`:
  `"keep"`, `"drop"`, `"warn"`).

`results[field]` tells how it went: the raw value, `ok` (was there a value
that could not be read?) and the normalizer's notes.

**Types**: `string`, `text`, `integer`, `number`, `boolean`, `date`,
`datetime`, `duration`, `url`, `email`, `phone`, `money`, `currency`,
`country`, `region`, `language`, `quantity`, `rating`, `availability`,
`address`, `coordinates`, `enum`, `object` (nested `fields`), `any`. Add your
own with `register_type(name, normalizer, json_schema)`.

**Field options**: `required`, `many`, `description`, `enum`, `minimum`,
`maximum`, `min_length`, `max_length`, `pattern`, `unit`, `currency`,
`country`, `country_field`, `currency_field`, `best`, `key`, `aliases`,
`default`, `selectors` and `sources` (extraction hints), `fields`.

**Schema options**: `name`, `version`, `description`, `key`, `extra`, and
two extraction hints for listing pages: `container` (the elements holding
one record each) and `next_page` (the link to the next page of records).
The [visual builder](builder.md) writes them.

Schemas are files too: `Schema.load("product.schema.json")` reads JSON, YAML
or TOML; `schema.save(path)` writes JSON or YAML in a versioned format
(`"$schema": "wintergrab/schema/v1"`). `schema.to_json_schema()` exports
standard JSON Schema (draft 2020-12) to validate the output elsewhere.

### Inferring a schema

Start from what you scraped:

```python
from wintergrab.data import infer_schema, explain_inference

schema = infer_schema(records, name="product")   # types, required fields, key, enums, units
explain_inference(records)                       # why each field got its type
```

A type is chosen when at least 90% of a field's values fit it; placeholder
values such as `"N/A"` count as missing.

### Validation

`schema.validate(record, results)` returns `Issue`s (`field`, `code`,
`message`, `severity`, `value`):

| Code | Severity | |
|---|---|---|
| `missing` | error | a required field has no value |
| `invalid` | error | there was a value, but it could not be read as the field's type |
| `range`, `length`, `pattern`, `enum`, `type` | error | constraint violations |
| `suspicious` | warning | a price of 0 or less, a date far in the future or before 1800 |
| `encoding` | warning | damaged text (mojibake, U+FFFD) |
| `unknown-field` | info | with `extra="warn"` |

Rules check what one field cannot:

```python
from wintergrab.data import Rule, validate_record

rules = [
    Rule("sale-below-list", "sale_price <= price", "the sale price is above the list price", field="sale_price"),
    Rule("known-brand", lambda r: r.get("brand") in BRANDS, severity="warning"),
]
issues = validate_record(record, schema, rules=rules)
```

An expression rule is skipped when a field it reads has no value
(`skip_missing=True`): whether a value must exist is the schema's
`required`, so `sale_price <= price` only judges records that have both.

## Expressions

Filters, computed fields and rules use a small expression language with
Python's syntax:

```
price > 0 and availability == "InStock"
round(1 - sale_price / price, 2)
coalesce(title, name, "untitled")
domain(url) in ("shop.example", "store.example")
matches(sku, "^[A-Z]{3}-[0-9]+$")
get("offers.0.price")        # nested values and names that are not identifiers
```

It is safe to take from a configuration file or a planner: no attribute
access, no imports, no comprehensions or lambdas, and only these functions:

| | |
|---|---|
| Text | `len`, `lower`, `upper`, `title`, `strip`, `clean`, `contains`, `icontains`, `startswith`, `endswith`, `matches`, `extract`, `replace`, `sub`, `split`, `join`, `words` |
| Values | `first`, `last`, `coalesce`, `min`, `max`, `sum`, `any`, `all`, `abs`, `round`, `int`, `float`, `str`, `bool`, `get` |
| Parsing | `number`, `integer`, `money`, `currency`, `date`, `datetime`, `boolean` |
| URLs | `host`, `domain`, `path`, `param` |
| Other | `now`, `today`, `hash` |

Missing data behaves like SQL's `NULL`: a missing field is `None`,
arithmetic with `None` (and division by zero) gives `None`, ordering
comparisons with `None` are false. Mixing types (`"12.99" > 10`) raises an
`ExpressionError`: the value was not normalized, and you should know.

```python
from wintergrab.data import Expression

deal = Expression("price < 20 and availability == 'InStock'")
deal({"price": 12.5, "availability": "InStock"})   # True
deal.names                                          # frozenset({'price', 'availability'})
```

## Pipelines

A `Pipeline` runs records through stages. Each stage returns the record,
possibly changed, or drops it, and counts what it did.

```python
from wintergrab.data import (Compute, Deduplicate, Filter, Normalize, Pipeline, Rename,
                             Select, Transform, Validate)

records = [
    {"title": " Phone X ", "cost": "₹29,999", "sale_price": "₹24,999", "url": "https://shop.example/p/1?utm_source=ad"},
    {"title": "Phone X", "cost": "₹29,999", "url": "https://shop.example/p/1"},
    {"title": "Case", "cost": "N/A", "url": "https://shop.example/p/2"},
    {"title": "Charger", "cost": "₹999", "url": "https://shop.example/p/3"},
]
pipeline = Pipeline([
    Rename({"cost": "price", "title": "name"}),
    Transform("name", ["clean"]),
    Normalize(schema),
    Validate(on_error="drop", rejects="rejects.jsonl"),
    Filter("price > 0"),
    Compute("discount", "round(1 - sale_price / price, 2)"),
    Deduplicate(key="url"),
    Select(["name", "price", "currency", "discount", "url"]),
])
clean = pipeline.run(records)     # or pipeline.stream(records) for large inputs
# [{'name': 'Phone X', 'price': 29999, 'currency': 'INR', 'discount': 0.17, 'url': 'https://shop.example/p/1'},
#  {'name': 'Charger', 'price': 999, 'currency': 'INR', 'discount': None, 'url': 'https://shop.example/p/3'}]
print(pipeline.describe())
```

(`schema` is the product schema above, with a `sale_price` money field.)

```
pipeline: 4 in -> 2 out
  stage      in  out  dropped  errors
  rename      4    4        0       0
  transform   4    4        0       0
  normalize   4    4        0       0  1 value(s) unreadable
  validate    4    3        1       0  price: invalid x1
  filter      3    3        0       0
  compute     3    3        0       0
  dedupe      3    2        1       0  key 1
  select      2    2        0       0
```

| Stage | Does |
|---|---|
| `Rename({"old": "new"})` | renames fields in place; several old names may map to one new name (the first with a value wins) |
| `Select([...])` | keeps these fields in this order (patterns like `"price_*"`); missing ones become `None` |
| `Exclude([...])` | removes fields (patterns allowed) |
| `Transform(field, ops)` | value operations, below |
| `Compute(field, expression)` | sets a field from an expression or a function of the record |
| `Filter(condition, keep=True)` | keeps (or with `keep=False` drops) the records a condition holds for |
| `Normalize(schema)` | types the record with a schema (`notes=True` keeps the guesses under `_notes`) |
| `Validate(schema=None, rules=...)` | `on_error`: `"drop"`, `"flag"` (issues under `_issues`), `"keep"`, `"raise"`; `on_warning`; `rejects` file. Without a schema it uses the one the record was normalized with, and then also reports unreadable values |
| `Deduplicate(key=..., near=False)` | see [Duplicates](#duplicates) |
| `Lookup(on, table, fields=...)` | copies columns from a reference table (dict, rows, `.csv`/`.json`/`.jsonl`/`.yaml`) matched on a field |
| `ConvertCurrency(fields, to=, rates=)` | converts amounts with exchange rates **you** supply (none are fetched) |
| `Enrich(fn)` | merges the fields a function returns; the function may be `async` (a web service, an AI provider adapter) |
| `QualityCheck(schema)` | measures quality as records pass (see [Quality](#quality)) |

Any object with `process_item` and any `record -> record | None` function
works as a stage too.

### Transform operations

`Transform("price", ["strip", {"regex": "[0-9.,]+"}, "number"])` applies
operations in order. Most apply to each item of a list; `first`, `last`,
`join`, `split`, `unique`, `compact`, `length`, `default` and `expr` work on
the whole value.

| | |
|---|---|
| Text | `strip`, `lower`, `upper`, `title`, `clean`, `fix_encoding`, `{regex: pattern}`, `{replace: [old, new]}`, `{sub: [pattern, replacement]}`, `{prefix: s}`, `{suffix: s}`, `{truncate: n}` |
| Parsing | `number` (`{number: ","}` for a decimal comma), `integer`, `percent`, `boolean`, `date`, `datetime`, `money` (`{money: EUR}`: the default currency), `currency`, `{unit: kg}`, `url`, `json` |
| Values | `{map: {from: to}}`, `{nullif: ["N/A", "-"]}`, `{default: value}`, `{round: n}`, `{multiply: x}`, `{expr: "value * 100"}` |
| Lists | `first`, `last`, `{join: ", "}`, `{split: ","}`, `unique`, `compact`, `length` |

A failing operation (a parser that found nothing, a function that raised)
sets the value to `None` and counts an error; `on_error="keep"` leaves the
field as it was, `"drop"` drops the record, `"raise"` raises. Add operations
with `register_operation(name, build)`.

### Pipelines in crawls

A pipeline is an item pipeline, so it plugs into any spider:

```python
class Shop(Spider):
    pipelines = [pipeline]
```

Dropped items are counted per pipeline (`items_dropped/Pipeline`) and each
emits an `item_dropped` event with the stage and reason
(`"filter: not price > 0"`). When the crawl ends the pipeline logs its table
and emits a `pipeline_report` event. Asynchronous stages (`Enrich` with an
async function) just work in a crawl.

### Pipelines as configuration

```yaml
# pipeline.yaml
name: shop
stages:
  - rename: {cost: price, title: name}
  - transform: {field: name, ops: [clean]}
  - normalize: {schema: product.schema.json, country: IN}
  - validate: {on_error: flag, rules: [{code: sale-below-list, check: "sale_price <= price"}]}
  - filter: "price > 0"
  - compute: {discount: "round(1 - sale_price / price, 2)"}
  - dedupe: {key: url}
  - lookup: {on: sku, table: catalog.csv, fields: [brand]}
  - select: [name, price, currency, discount, brand, url]
```

```python
pipeline = Pipeline.load("pipeline.yaml")      # JSON and TOML work too
pipeline.to_config()                           # back to the dict form; pipeline.save(path)
```

Paths in the file (schemas, tables, rejects files) are relative to the file.
Files can only refer to Python code (`enrich: {function: "pkg.module:fn"}`,
`function: "pkg.module:fn"`) when loaded with `allow_imports=True`: importing a
module runs its code, so allow it only for files you trust. Expressions are
always safe.

## Duplicates

`Deduplicator` (or the `Deduplicate` stage) catches, cheapest first:

1. **Same key**: `Deduplicator(key="url")` compares keys after
   normalization; URLs through the [URL normalizer](spiders.md#which-urls-get-crawled), so
   `?utm_source=...` does not make a new product.
2. **Same content**: without a key, records whose fields are equal after
   normalizing case, spacing and punctuation.
3. **Near duplicates** (`near=True`): records whose text shares at least
   `similarity` (default 0.8) of its word 3-grams with an earlier record.

Similarity is the Jaccard index of 3-word shingles. A single changed word
removes three shingles, so 0.8 catches texts with up to about one word in 30
changed and 0.7 about one in 20. Template texts can be similar and still
describe different things ("Blue shirt, size M" and "Red shirt, size M"), so
try `mark=True` first: duplicates are kept and marked `_duplicate_of` (the
index of the first record) and `_duplicate_kind`.

Under the hood (`wintergrab.data.similarity`): content hashes, MinHash
signatures with LSH (one-permutation hashing, about 0.1 ms per record) for
records, and SimHash with an exact pigeonhole index for long documents.

Records that describe the same thing with different names ("Apple Inc." in
one directory, "APPLE INC" in another) are not duplicates to these checks:
[entity resolution](entities.md) groups them, with evidence, and lists the
uncertain cases for review.

## Quality

`QualityMonitor` watches records stream by and reports:

| Metric | |
|---|---|
| completeness | share of records with a value, per field |
| validity | share of values without validation errors (with a schema) |
| uniqueness | distinct keys (or contents) / records |
| consistency | share of values with the field's usual type and format (`2024-03-05` vs `05/03/2024`) |
| freshness | share of records younger than `max_age` (from `_fetched_at`, `scraped_at`...) |
| confidence | mean `_confidence` of the records, when present |

It also flags anomalies: repeated keys, empty records, a field with the
same value everywhere (a broken selector?), placeholder values (`N/A`,
`{{ price }}`), damaged text and outliers.

Compared with a baseline report from an earlier run, it finds degradation:
a field's completeness collapsing (`extraction-collapse`, an error) or
dropping, validity dropping, fields disappearing or appearing, a field's
type changing, a numeric distribution shifting (two-sample
Kolmogorov-Smirnov test at the 0.1% level), fewer records, more duplicates.

In a crawl with a `crawl_dir`, the monitor saves `quality.json` and compares
the next run with it automatically:

```python
class Shop(Spider):
    crawl_dir = ".crawl/shop"
    pipelines = [Pipeline([Normalize(schema), QualityCheck(schema, baseline="auto")])]
```

Each problem is logged and emitted as a `quality_degraded` event (`field`,
`code`, `message`, `severity`), so alerts can hang off it. When fields came,
went or changed type, a `schema_changed` event says which: `{"added":
["title"], "removed": ["price"], "retyped": {"rating": ["number", "text"]}}`.

On the command line, `--quality FILE` does the same for `wintergrab crawl`
and `wintergrab goal`: each run is compared with the report in FILE, which
then holds this run's. Pages where `--extract` found no complete record (a
required field missing) are each an `extraction_failed` event (`url`,
`schema`, `missing`).

```python
from wintergrab.data import QualityMonitor

monitor = QualityMonitor(schema)
for record in records:
    monitor.observe(record)
report = monitor.report()
print(report.describe())
report.compare(previous_report)   # [Issue(...), ...]
report.save("quality.json")
```

## Versions and differences

`diff_records` compares two datasets record by record:

```python
from wintergrab.data import diff_records

diff = diff_records(yesterday, today, key="url")
print(diff.describe())
```

```
+1 added, -1 removed, ~3 changed, 0 unchanged
fields changed:
  price              2 records: 1 up, 1 down, median +7.8%
  availability       2 records: InStock -> OutOfStock 1, OutOfStock -> InStock 1
```

Records are matched by `key` (one field, several, or dotted paths), normalized
like [duplicate](#duplicates) keys: `?utm_source=` does not make a new page,
nor does case or punctuation make a new SKU. Compared values ignore extra
whitespace, `None` equals a missing field, URLs are compared normalized, and
fields starting with `_` (`_confidence`, `_provenance`) are skipped unless
`private=True`; `ignore=["scraped_at"]` skips others. Records without the key
(or all records, without a key) are matched by content: added or removed,
never changed.

`diff.added`, `diff.removed`, `diff.changed` (each change with its
`FieldChange`s: `old`, `new`, and for numbers and same-currency prices
`delta` and `ratio`) and `diff.unchanged` hold the details; `diff.fields()`
summarizes each field (up/down and median change for numbers, the most
common transitions for short values); `diff.rows()` flattens it all for a
file.

### Versions

`DatasetVersions` keeps a directory of versions of a dataset, the way you
would keep releases of any other artifact:

```python
from wintergrab.data import DatasetVersions

versions = DatasetVersions("prices", key="url")
versions.commit(records, message="daily run")   # v1, v2, v3... (nothing new is saved if nothing changed)
versions.latest.changes                         # {'added': 1, 'removed': 1, 'changed': 3, 'unchanged': 0}
versions.diff("v1", "latest")                   # a DatasetDiff
versions.load("previous")                       # the records of a version
```

Each version is a gzipped JSON Lines file (`v3.jsonl.gz`); `versions.json`
lists them with their time, record count, message, key, an order-independent
SHA-256 digest of the records and their differences from the version before.
Files are written through a temporary file and renamed, so an interrupted
save never leaves half a version.

## Command line

```bash
wintergrab data infer items.jsonl -o product.schema.json --explain
wintergrab data validate product.schema.json items.jsonl -o clean.jsonl --rejects rejects.jsonl
wintergrab data run pipeline.yaml items.jsonl -o clean.csv
wintergrab data quality items.jsonl --schema product.schema.json --save quality.json
wintergrab data quality items.jsonl --baseline quality.json
wintergrab data entities companies.jsonl --field name --attribute website -o entities.jsonl
wintergrab data commit prices/ today.jsonl --key url -m "daily run"
wintergrab data log prices/
wintergrab data diff prices/@previous prices/@latest -o changes.jsonl
wintergrab data diff yesterday.jsonl today.jsonl --key sku --exit-code

wintergrab crawl https://shop.example --field ... --pipeline pipeline.yaml -o items.jsonl
```

Inputs are JSON Lines, JSON or CSV (`-` reads JSON Lines from standard
input); outputs are chosen by extension like `crawl -o`. `validate` exits
with status 1 when a record is invalid, and `quality --baseline` when
quality collapsed, so both work as checks in CI.
