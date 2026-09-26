# Extraction: typed records with provenance and confidence

Describe the record you want with a [schema](data.md#schemas); the
`Extractor` finds each field on the page, tells you where every value came
from and how sure it is, and leaves out what it could only guess.

```python
from wintergrab.data import Schema
from wintergrab.extraction import Extractor

schema = Schema.from_dict({
    "name": "product",
    "fields": {
        "name": {"type": "string", "required": True},
        "brand": "string",
        "price": {"type": "money", "required": True},
        "currency": "currency",
        "availability": "availability",
        "rating": {"type": "rating", "best": 5},
        "review_count": "integer",
        "weight": {"type": "quantity", "unit": "kg"},
        "url": "url",
    },
})
extractor = Extractor(schema)
record = extractor.extract(response)       # a Response, a Selector, or HTML text
record.data
# {'name': 'Phone X', 'brand': 'Acme', 'price': 299.99, 'currency': 'USD', 'availability': 'InStock',
#  'rating': 4.5, 'review_count': 120, 'weight': 0.45, 'url': 'https://shop.example/p/phone-x'}
print(record.explain())
```

```
product@1 from https://shop.example/p/phone-x: confidence 0.92
  field         value                           method     conf  evidence
  name          Phone X                         json-ld    1.00  agrees: opengraph, dom
  brand         Acme                            json-ld    0.95
  price         299.99                          json-ld    0.91  agrees: dom, pattern; vs {'amount': 349.99, 'currency': 'USD'} (pattern, 0.14)
  currency      USD                             json-ld    0.95
  availability  InStock                         json-ld    0.99  agrees: pattern, dom
  rating        4.5                             json-ld    0.98  agrees: dom
  review_count  120                             json-ld    0.99  agrees: dom, pattern
  weight        0.45                            label      0.80
  url           https://shop.example/p/phone-x  dom        0.70
```

No selectors were written for this page. Where the site's markup is
unusual, add `selectors` to a field and they are tried with a high prior.

## The strategy hierarchy

Every strategy proposes candidate values for every field; cheaper and more
reliable ones come first:

| Method | Looks at |
|---|---|
| `json-ld`, `microdata` | schema.org data (`Product.offers.price`, `aggregateRating.ratingValue`...), picked by the schema's name (`product` -> `Product`) |
| `opengraph`, `twitter`, `meta` | `og:title`, `product:price:amount`, `<meta name=description>`, the `<title>` without the site name |
| `selector` | the field's own `selectors` (CSS or XPath, `::text`, `::attr()`) |
| `embedded-json` | state embedded by JavaScript apps (`__NEXT_DATA__`, `window.__STATE__`), by key name |
| `label` | values next to a label with the field's name: `<dt>Weight</dt><dd>1.2 kg</dd>`, spec tables, `SKU: AB-12` |
| `records` | the fields of a detected repeating record (listing pages) |
| `dom` | layout conventions: the `<h1>`, elements classed `price`/`stock`/`rating`, `<time datetime>`, `mailto:` and `tel:` links, the canonical link, an add-to-cart button |
| `pattern` | the value's shape in the visible text: prices with a currency sign, e-mails, phone numbers, `4.5 out of 5`, `(123 reviews)` |
| `model` | an [extraction model](#extraction-models), only for what is still missing |

Field names are matched loosely (`review_count` = `reviewCount` = `Review
count`) and a field's `aliases` count too. `sources=["jsonld:Product.offers.price"]`
names a structured-data path explicitly.

The heuristics avoid the usual traps: a struck-through price is the old
price (a field named `list_price`, `old_price` or `was_price` takes it, `price`
does not), and prices inside related-product blocks, carts, headers and
footers are ignored.

## Confidence

Confidence comes from evidence, and the evidence is kept:

1. **Method prior**: how often the method is right. The defaults
   (`DEFAULT_PRIORS`) encode the hierarchy: 0.95 for JSON-LD down to 0.6 for
   patterns and models. `Extractor.calibrate()` measures them on your pages
   instead (below).
2. **Candidate evidence**: a strategy that saw several different values (three
   prices on the page) or had to guess while reading a value (an ambiguous
   `1,234`) scores lower.
3. **Agreement**: independent methods that found the same value combine:
   `1 - (1 - p1)(1 - p2)...`. `$299.99` in the page and `299.99` in JSON-LD agree.
4. **Disagreement**: the best competing value lowers the winner:
   `p * (1 - 0.5 * p_runner_up)`. Competing values are kept as `alternatives`.
5. **Validation**: an invalid value (out of range, wrong pattern) halves
   confidence; a suspicious one takes 20% off.

Values below `min_confidence` (default 0.3) are left out of the record: their
field's `validation` is `"low-confidence"` and the candidates stay in its
`alternatives`. A guess is not data. Set `min_confidence=0` to keep everything.

### Calibration

Priors are assumptions until you measure them. Give pages with known values:

```python
measured = extractor.calibrate([(page1, {"name": "Phone X", "price": 299.99}), ...])
# {'json-ld': 0.99, 'dom': 0.83, 'pattern': 0.41, ...}   (methods with at least 5 candidates)
```

Each method's prior becomes the share of its candidates that were right.

## Provenance

Every field keeps where its value came from:

```python
fv = record.fields["price"]
fv.value, fv.raw, fv.method, fv.source      # 299.99, '299.99', 'json-ld', 'json-ld:Product.offers.price'
fv.confidence, fv.agreed, fv.alternatives   # 0.91, ['dom', 'pattern'], [...]
fv.notes, fv.validation                     # [], 'ok'
```

`record.to_dict()` (what spiders export) holds the values and `_confidence`;
`record.to_dict(provenance=True)`, or `Extractor(..., provenance=True)`, adds
`_provenance`: the page URL, the fetch time, the extractor (`product@1`,
the schema's name and version), each field's method, source, raw value,
confidence, agreement, alternatives, notes and validation, and the record's
validation issues. [Quality monitoring](data.md#quality) reads `_confidence`.

## Listing pages

```python
records = extractor.extract_all(response)                        # every product on a category page
records = extractor.extract_all(response, container=".product-card")
```

`extract_all` uses, in this order: several schema.org objects of the
schema's type (an `ItemList` of products), the elements matching
`container`, or the page's main repeating group, found automatically. Inside
a record only record-level strategies run.

## Extraction models

wintergrab calls no model by default and ships no API client in the core.
Plug in any function (or object with an `extract` method, optionally `async`)
that takes a `ModelRequest` and returns `{field: value}` or a JSON string:

```python
def ask_my_model(request):
    return call_your_llm(request.prompt())     # request.fields, .text (Markdown), .url, .known

extractor = Extractor(schema, model=ask_my_model)
```

The model is asked, in one request per page, only for the fields the other
strategies did not find with at least `model_threshold` (0.5) confidence. Its
answers are candidates like any other: read with the field's type, validated,
compared with what other strategies found, and **grounded**: a value that
appears on the page keeps its confidence, a number that appears in another
format loses 10%, and a value that appears nowhere keeps 30% of it and the
note `not-on-page`, which usually puts it below `min_confidence`. Models can
invent values; the page cannot.

A model that raises is logged and ignored. With an `async` model use
`await extractor.aextract(page)`.

## Command line

```bash
wintergrab get https://shop.example/p/1 --extract product.schema.json --explain
wintergrab get https://shop.example/phones --extract product.schema.json --all -o phones.csv
wintergrab crawl https://shop.example --extract product.schema.json --max-pages 500 -o products.jsonl
```

`--explain` prints the evidence table for each record, `--provenance` adds
`_provenance`, `--all`/`--container` extract listings. A crawl keeps the
records that have every required field and reports how many pages had none.

## Tests

Keep pages with the values a schema must read from them (`wintergrab
fixture`), and check after every change that it still does (`wintergrab
test`, exit status 1 when a value differs). See [testing](testing.md).

## When the site changes

`HealingExtractor` (`--heal DIR`) keeps versions of a schema and repairs the
fields' selectors when they stop matching. It tests each repair on the pages
where they failed and applies it only when other evidence confirms it.
Otherwise a person decides, through a review queue (`--review FILE`,
`wintergrab review`). See [healing](healing.md).

## Speed

`benchmarks/bench_pages.py` (one core of a 4-vCPU cloud VM, Python 3.11,
median of 5 runs of 200 pages, parsing included) extracts a 13-field product
record in:

| Page | Parse only | `extract` |
|---|---:|---:|
| product page with JSON-LD (11 KB) | 0.26 ms | 6.7 ms |
| product page without structured data (10 KB) | 0.26 ms | 6.8 ms |
| product page, 480 KB of text | 1.5 ms | 62 ms |

Most of the time goes to the heuristics that read the whole page (the text
patterns and the DOM conventions), so it grows with the page's size.
