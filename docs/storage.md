# Storage: where items go

A spider's `output` (`-o` on the command line) says where items go. The
file extension or URL picks the format:

| Output | What | Needs |
|---|---|---|
| `items.jsonl` (`.ndjson`, `.jl`) | One JSON object per line: the best format for big, resumable crawls | |
| `items.json` | One JSON array | |
| `items.csv` | CSV, a column per key (an item with a new key widens the file), nested values as JSON | |
| `items.sqlite` (`.sqlite3`, `.db`) | Rows of an `items` table, one column per field, upserted on `unique_key` | |
| `items.parquet` (`.pq`) | A Parquet file, a typed column per field | `pip install "wintergrab[parquet]"` |
| `items.xlsx` | An Excel workbook, a column per field | `pip install "wintergrab[xlsx]"` |
| `items.duckdb` (`.ddb`) | The `items` table of a DuckDB database, a typed column per field, upserted on `unique_key` | `pip install "wintergrab[duckdb]"` |
| `postgresql://user@host/db?table=NAME` | Rows of a PostgreSQL table, upserted on `unique_key` | `pip install "wintergrab[postgres]"` |
| `mysql://user@host/db?table=NAME` (`mariadb://`) | Rows of a MySQL or MariaDB table, upserted on `unique_key` | `pip install "wintergrab[mysql]"` |
| `mongodb://user@host/db?collection=NAME` (`mongodb+srv://`) | Documents of a MongoDB collection, upserted on `unique_key` | `pip install "wintergrab[mongodb]"` |
| `s3://bucket/path/items.jsonl` | Any file output (by its extension) as an object of an S3 bucket, or of an S3-compatible store | `pip install "wintergrab[s3]"` |
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
values included (and `mysql://...`, `mongodb://...`, `s3://...`).

## SQLite

Each field is a column of an `items` table, and nested values are JSON
text. With `unique_key`, rows are upserted on it; without, a fresh crawl
replaces the table. wintergrab keeps the kinds of value each column has
held, so reading the file back gives lists, objects and true and false as
they were, where a column only ever held them; a column that mixed them
gives its values as kept (JSON text, 1 and 0). Integers beyond 64 bits,
which SQLite cannot hold, are text.

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

## DuckDB

[DuckDB](https://duckdb.org) is a database for analysis kept in one file.
The items become its `items` table, ready for SQL:

```bash
pip install "wintergrab[duckdb]"
wintergrab crawl https://shop.example/ --auto -o shop.duckdb --unique-key url
duckdb shop.duckdb -c "SELECT currency, count(*), avg(price) FROM items GROUP BY currency"
```

Each field is a column, typed from all of its values: `BIGINT`, `DOUBLE`
(integers and decimals mixed), `BOOLEAN`, `VARCHAR`, or `JSON` for nested
values, which SQL reads as they are (`tags->>'$[0]'`,
`offer->>'$.seller'`). A field whose values mix kinds (numbers and text)
is text, and so are integers beyond 64 bits. Columns are named after the
fields in lower case, with letters, digits and `_`: `Price (USD)` is
`price_usd`, and `Name` beside `name` is `name_2`, since DuckDB's names
ignore case. The names hold from one run to the next, and
`_wintergrab_columns` says which field each column holds, so reading the
file back gives the fields their own names.

- **Upserts**: with `unique_key`, crawling again updates rows in their
  place: the fields an item has replace the row's, the others keep theirs
  (as in the other databases). Items without the field are rows of their
  own.
- **Fresh crawls**: without `unique_key`, a fresh crawl replaces the table,
  and a resumed one adds to it.
- **The rest of the database** (other tables, views on `items`) is left as
  it is. An `items` table that wintergrab did not create is refused.

The table is written when the crawl ends, in one transaction (below):
the database has the new table or the one before, never half of one.
`read_records("shop.duckdb")` reads it back, and reads any DuckDB database
with an `items` table or a single table.

## Parquet, Excel and DuckDB files are written at the end

Parquet, Excel and DuckDB files are written whole. While the crawl runs,
its items wait in `.NAME.spool.jsonl` beside the file, and the file is
written when the crawl ends. If the crawl stops (Ctrl+C, a crash), the
spool keeps its items, and the resumed crawl continues it. `max_output_bytes`
counts the items as JSON, since the file itself is compressed.

If the file cannot be written when the crawl ends (a full disk, a DuckDB
file another program holds), the crawl says so and the spool keeps the
items. Once the cause is gone, this writes them (with the crawl's
`unique_key`, if it had one):

```python
from wintergrab.spider.exporters import open_exporter

open_exporter("shop.duckdb", append=True, unique_key="url").close()
```

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

## MySQL and MariaDB

```bash
pip install "wintergrab[mysql]"
export MYSQL_PWD=...         # or the [client] section of ~/.my.cnf: keep the password out of the URL
wintergrab crawl https://shop.example/ --auto -o "mysql://crawler@db.example/shop?table=products" --unique-key url
```

The database is the URL's path, and the table is `items` unless the URL
says `?table=NAME`. `mariadb://` URLs work the same. The URL's other
parameters are the connection's: `connect_timeout`, `unix_socket`,
`ssl_ca`, `ssl_cert`, `ssl_key`, `ssl_verify_cert`, `ssl_verify_identity`.
Any other is an error, not ignored.

Tables work as PostgreSQL's do: created on first use, a column per field,
upserts on `unique_key`, a fresh crawl without one emptying its table, and
only tables wintergrab created written to. Columns are typed from the
field's first value (a field that is null until then waits for one):
`BOOLEAN`, `BIGINT`, `DOUBLE`, or `LONGTEXT` for text and for nested
values, kept as JSON and given back as the objects and lists they were. A
column widens as PostgreSQL's does, and a boolean column that gets text
keeps its values as `true` and `false`.

A long text column cannot have a unique index in MySQL, so the key's
column has a stored companion, `_wg_key_<column>`: the SHA-256 of its
value, uniquely indexed. Rows without a value for the key are added,
however many. Tested against MySQL 8.4 and MariaDB 10.11 and 11.

## MongoDB

```bash
pip install "wintergrab[mongodb]"
export WINTERGRAB_MONGODB_PASSWORD=...   # read when the URL names a user without a password
wintergrab crawl https://shop.example/ --auto -o "mongodb://crawler@db.example/shop?collection=products" --unique-key url
```

The database is the URL's path, and the collection is `items` unless the
URL says `?collection=NAME`. `mongodb+srv://` URLs work too. The URL's
other parameters (`authSource`, `tls`, `replicaSet`...) are the
connection's, checked by pymongo: a misspelt one is an error.

Each item is a document, its values as JSON has them: nothing is typed or
flattened, and each value keeps its own type (7 and `"X7"` in one field
stay a number and a text). Integers beyond 64 bits, which MongoDB cannot
hold, are text. A record's own `_id` field is kept as `_id_`, since `_id`
is MongoDB's, and a field name beginning with `$`, which MongoDB reads as an
operator or a reference, begins with a full-width dollar sign (U+FF04)
instead. Both are read back as they were.

- **Upserts**: with `unique_key`, each document replaces the one with the
  same key, whole: fields the new record does not have are gone, unlike a
  table's upsert, which updates the columns given. A unique index keeps the
  key unique; documents without a value for it stay out of the index.
- **Fresh crawls**: without `unique_key`, a fresh crawl empties its
  collection first. A resumed crawl adds to it.
- **Only its own**: wintergrab names the collections it created in a
  `_wintergrab_collections` collection beside them, and refuses others of
  the same name.

Reading a collection gives its documents in the order they were first
written, without MongoDB's `_id`. A collection wintergrab did not write is
read too: dates become ISO text, and object ids text.

## S3 and S3-compatible object storage

```bash
pip install "wintergrab[s3]"
export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...   # or AWS_PROFILE, ~/.aws/credentials, a machine's role
wintergrab crawl https://shop.example/ --auto -o s3://my-bucket/crawls/products.parquet
wintergrab crawl https://shop.example/ --auto -o "s3://crawls/products.jsonl?endpoint_url=https://minio.example:9000"
```

The object's extension picks the format, as a file's does: `.jsonl`,
`.json`, `.csv`, `.parquet`, `.xlsx`, `.sqlite`. While the crawl runs, the
items are written to a local file under `.wintergrab/uploads/`
(`WINTERGRAB_UPLOADS` to put it elsewhere). The file is uploaded when the
crawl ends, so the object appears whole. If the crawl stops, its items stay
there, and the resumed crawl continues them and uploads them. A resumed
crawl whose local file is gone continues the object itself.

Credentials come from the environment, as the AWS tools read them, and never
from the URL: a URL with a user or password is refused. S3-compatible
stores (MinIO, Cloudflare R2, Backblaze B2...) take `?endpoint_url=...` (or
`AWS_ENDPOINT_URL`); `?region=` and `?profile=` are read too, and any other
parameter is an error. A missing bucket or missing credentials are said
when the crawl starts, not when it ends. `read_records("s3://...")` reads an
object back by its extension.

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

- Google Cloud Storage and Azure Blob Storage have no built-in adapter
  yet (their S3-compatible endpoints aside). Write a file and copy it, or
  register your own.
- An object in S3 appears when the crawl ends, as a Parquet file does.
- Parquet, Excel and DuckDB files appear when the crawl ends: follow a
  long crawl in its spool, or write JSON Lines and convert them afterwards.
- DuckDB lets a file be open in one program that writes to it, or in any
  number that only read it. A crawl whose DuckDB file another program has
  open for writing (the `duckdb` shell and `duckdb.connect()` open files
  for writing unless told otherwise) says so when it starts. One that ends
  while another program has the file open waits 5 seconds for it, then
  keeps its items in the spool (above).
- PostgreSQL, MySQL and MariaDB widen columns with `ALTER TABLE`. On a big
  table that rewrites it, once per widening.
- Where the URL is shown or kept (the summary, logs, a run's record), its
  user and password are left out: `mysql://***@db.example/shop`. The
  environment is the place for passwords.
