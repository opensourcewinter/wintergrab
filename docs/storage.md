# Storage: where items go

A spider's `output` (`-o` on the command line) says where items go. The
file extension or URL picks the format:

| Output | What | Needs |
|---|---|---|
| `items.jsonl` (`.ndjson`, `.jl`) | One JSON object per line: the best format for big, resumable crawls | |
| `items.json` | One JSON array | |
| `items.csv` | CSV, the columns from the first item, nested values as JSON | |
| `items.sqlite` (`.sqlite3`, `.db`) | Rows of an `items` table, one column per field, upserted on `unique_key` | |
| `items.parquet` (`.pq`) | A Parquet file, a typed column per field | `pip install "wintergrab[parquet]"` |
| `items.xlsx` | An Excel workbook, a column per field | `pip install "wintergrab[xlsx]"` |
| `postgresql://user@host/db?table=NAME` | Rows of a PostgreSQL table, upserted on `unique_key` | `pip install "wintergrab[postgres]"` |
| `-` | JSON Lines on standard output | |

```bash
wintergrab crawl https://books.example/ --follow ".product a" --auto -o books.parquet
wintergrab crawl https://books.example/ --auto -o "postgresql://crawler@db.example/shop?table=books" --unique-key url
```

```python
class Books(Spider):
    output = "books.xlsx"
```

No database or library is needed for anything else: without pyarrow, only
a `.parquet` output fails, and it says what to install before the crawl
starts.

The same outputs are inputs. `wintergrab data validate book.schema.json
books.parquet`, `data quality books.xlsx`, and
`read_records("postgresql://.../shop?table=books")` read them back, nested
values included.

## Parquet

Each field is a column, typed from all of its values:

- `int64`, or `double` when integers and decimals mix;
- `bool` for true/false;
- `string` for text. A field whose values mix kinds (numbers and text) is
  text, and so are integers beyond 64 bits.

Nested values (objects, lists) are JSON text. The file records which
columns hold them, so reading it back gives the objects and lists they
were. The file is compressed with zstd, in row groups of 50,000 items.

## Excel

The first row names the fields, and each item is a row. Numbers and
true/false stay numbers and booleans. Nested values are JSON text, given
back as they were when read. The workbook also stays safe to open:

- Text is always text: a value such as `=HYPERLINK(...)` from a crawled page
  is never a formula.
- Control characters a workbook cannot hold are left out.
- Text longer than a cell holds (32,767 characters) is cut.
- Integers beyond 2^53 are text, since Excel would round them.
- Past 1,048,575 items, the rows continue on a new sheet.

## Parquet and Excel files are written at the end

Parquet and Excel files are written whole. While the crawl runs, its items
wait in `.NAME.spool.jsonl` beside the file, and the file is written when
the crawl ends. If the crawl stops (Ctrl+C, a crash), the spool keeps its
items, and the resumed crawl continues it. `max_output_bytes` counts the
items as JSON, since the file itself is compressed.

## PostgreSQL

```bash
pip install "wintergrab[postgres]"
export PGPASSWORD=...        # or ~/.pgpass: keep the password out of the URL
wintergrab crawl https://shop.example/ --auto -o "postgresql://crawler@db.example/shop?table=products" --unique-key url
```

The table is created on first use: `items`, unless the URL says
`?table=NAME` or `?table=schema.name`. The URL's other parameters
(`sslmode=require`...) are the connection's.

Each new field becomes a column, typed from its first value: `boolean`,
`bigint`, `double precision`, `text`, or `jsonb` for nested values. When a
later value does not fit, the column widens to hold both:

- a `bigint` column that gets a decimal becomes `double precision`;
- a number or boolean column that gets text becomes `text`;
- one that gets an object becomes `jsonb`.

Nothing is cut to fit. Field names that are not plain identifiers get a
column name of their own (`Price (USD)` is `price_usd`), and reading the
table gives the field names back.

- **Upserts**: with `unique_key` (`--unique-key url`), rows are upserted on
  that field, so crawling again updates them.
- **Fresh crawls**: without `unique_key`, a fresh crawl empties its table
  first, as a file output is replaced. A resumed crawl adds to it.
- **Batches**: rows are written in batches of 64 or once a second, and on
  checkpoints.

wintergrab only writes to tables it created. It keeps their fields and
columns in a `_wintergrab_columns` table beside them. A table of the same
name that it did not create is refused, so pick another with
`?table=NAME`. Where the URL is shown or kept (the summary, logs, a run's
record), its user and password are left out: `postgresql://***@db.example/shop`.

## Your own

An output format is an `Exporter` registered by extension or URL scheme.
Readers are registered the same way:

```python
from wintergrab.spider.exporters import Exporter, register_exporter
from wintergrab.data.io import register_reader

class Lines(Exporter):
    def __init__(self, path, *, append):
        super().__init__(path, append=append)
        self.file = open(path, "a" if append else "w")
    def write(self, item):
        self.file.write(repr(item) + "\n")
        self.count += 1
    def close(self):
        self.file.close()

register_exporter(".txt", Lines)                          # output = "items.txt"
register_exporter("kafka", "my_package.outputs:Kafka")    # kafka://..., imported when first used
register_reader(".txt", my_reader)                        # (path) -> records
```

An exporter that upserts sets `supports_unique_key = True`, and its
constructor then takes `unique_key` too. A URL exporter gets the URL.

## Limits

- Object storage (S3 and the like), MySQL and MongoDB have no built-in
  adapter yet. Write a file and copy it, or register your own.
- Parquet and Excel files appear when the crawl ends: follow a long crawl
  in its spool, or write JSON Lines and convert them afterwards.
- PostgreSQL widens columns with `ALTER TABLE`. On a big table that
  rewrites it, once per widening.
