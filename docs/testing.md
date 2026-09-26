# Extraction tests

Treat extractors like code. Keep a few pages with the values a schema must
read from them. After changing the schema, or wintergrab itself, check that
it still does, in CI, before the change ships.

```
$ wintergrab fixture https://books.example/books/catalogue/book-1/index.html \
      https://books.example/books/catalogue/book-2/index.html --schema book.schema.json --to tests/books
tests/books/0001-book-1.json: name="Book number 1", price=11.5 GBP, availability="InStock", rating=2, url="https://books.example/books/catalogue/book-1/index.html"
tests/books/0002-book-2.json: name="Book number 2", price=13 GBP, availability="InStock", rating=3, url="https://books.example/books/catalogue/book-2/index.html"
check these values: they are what wintergrab test will expect

$ wintergrab fixture https://books.example/books/catalogue/book-3/index.html --to tests/books --expect price="£14.50" --only
tests/books/0003-book-3.json: price="£14.50"

$ wintergrab test tests/books
0001-book-1  ok      5 field(s)
0002-book-2  ok      5 field(s)
0003-book-3  ok      1 field(s)
3 fixture(s): 3 passed, 0 failed (11 value(s) checked)
```

The schema's `name` selector is then changed from `h1` to `title`:

```
$ wintergrab test tests/books                # exit status 1
0001-book-1  FAILED  name: expected "Book number 1", got "Book number 1 | Books to Scrape"
0002-book-2  FAILED  name: expected "Book number 2", got "Book number 2 | Books to Scrape"
0003-book-3  ok      1 field(s)
3 fixture(s): 1 passed, 2 failed (2 of 11 value(s) differ)
```

(A run against the repository's test site, `tests/testsite.py`; its address
is shortened here.)

## Fixtures

A fixture is two files in the suite's directory. They are text, so expected
values are read, edited and reviewed like code:

```
tests/books/
  0001-book-1.html        the page, as it was fetched
  0001-book-1.json        its URL and the values expected
  suite.json              {"schema": "../../book.schema.json"}: the schema the suite was made with
```

```json
{
  "url": "https://books.example/books/catalogue/book-1/index.html",
  "expected": {
    "name": "Book number 1",
    "price": {"amount": 11.5, "currency": "GBP"},
    "availability": "InStock",
    "rating": 2,
    "url": "https://books.example/books/catalogue/book-1/index.html"
  },
  "captured": 1790398885.818,
  "note": ""
}
```

Each fixture checks only the fields in `expected`:

- a snapshot has the fields the schema read (not the empty ones);
- `--expect FIELD=VALUE` adds or overrides one (a JSON value, or text);
- `--only` keeps just those;
- `null` asserts that nothing is found.

Check a snapshot before keeping it: the test expects what the schema reads
*now*, right or wrong.

`wintergrab fixture` takes pages from:

- **URLs**, fetched once;
- **a recorded crawl** (`--from-run run-7`, see [runs](runs.md)): the pages
  it kept, `--match REGEX` to choose them, `--limit N` (20).

## Running them

`wintergrab test DIR` reads every fixture's page with the suite's schema
(`--schema FILE` for another) and compares each expected value:

- **Prices** compare by amount: `"£14.50"` expected and
  `{"amount": 14.5, "currency": "GBP"}` read are the same. A currency must
  match when both give one.
- **Numbers** compare as numbers.
- **Text** compares as it is, spacing aside: case matters.
- **Lists** compare item by item, in order.
- **A field the schema does not have** reads nothing.

Other options:

- `--only NAME` runs some fixtures; a prefix works (`--only 0001`).
- `--json` prints the report.
- `--update` accepts what was read where it differs. The `.json` files
  change: review the change with your version control.
- `-v` lists the fields checked.

`wintergrab test --heal DIR` tests a [self-healing extractor](healing.md)'s
active version against the fixtures that people confirmed in review
(`DIR/fixtures`).

The exit status is 1 when a value differs, a fixture's page could not be
read, or there is no fixture. In CI:

```yaml
- run: wintergrab test tests/books
```

## In code

```python
from wintergrab.extraction.fixtures import FixtureSuite

suite = FixtureSuite("tests/books")
suite.add(response, schema="book.schema.json")                 # a snapshot
suite.add(html, url="https://books.example/b/3", expect={"price": "£14.50"}, only=True)

def test_book_schema():                                        # with pytest
    report = suite.run()                                       # the suite's schema, or run(schema)
    assert report.ok, report.describe()
```

`report.results` has each fixture's checks (`field`, `expected`, `got`,
`ok`); `report.to_dict()` is the JSON form.
