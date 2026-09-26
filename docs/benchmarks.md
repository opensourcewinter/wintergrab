# Benchmarks

`wintergrab benchmark` measures WINTERGRAB on your machine:

```bash
wintergrab benchmark --browser
```

```
wintergrab 0.2.0 on Linux x86_64, CPython 3.11.15, 4 CPUs; shop: 100 listing + 1000 product pages

startup  import wintergrab     168 ms (Python alone: 12 ms)
startup  wintergrab --version  190 ms
crawl    pages/s               1,473.2 (1,100 pages, 1,000 items, 0 errors, concurrency 32)
crawl    items/s               1,339.3
crawl    latency p50 / p90     9.5 / 13.3 ms
crawl    CPU / peak memory     0.745 s / 46.5 MB (3.7 MB downloaded)
parse    pages/s               2,244.1 (7.3 KB pages, 20 cards each)
extract  pages/s               339.4 (the product template's 14 fields)
data     records/s             9,146.3 (normalized and validated, 7 fields)
dedupe   URLs/s                49,018.9 (made canonical: 10,000 URLs, 2,000 pages)
dedupe   pages/s               1,522.1 (near-duplicate fingerprints)
browser  pages/s               10.2 (265.8x slower than HTTP from this machine: 2,698.8 pages/s, one page at a time)
```

(A 4-vCPU cloud VM; the table is what the command printed there. Your
numbers will differ: compare runs of one machine.)

## What it measures

Nothing leaves the machine. A synthetic shop is served from 127.0.0.1 by a
server in a process of its own. It has 20 product cards to a listing page,
links on to the next pages, and product pages with a price, a stock line,
specifications and, on every other one, JSON-LD. Each scenario runs in a
fresh Python process, so its CPU time and peak memory are its own.

| Scenario | What runs | Reported |
|---|---|---|
| `startup` | `python -c "import wintergrab"` and `wintergrab --version`, five times each | medians, and Python's own start-up |
| `crawl` | a `Spider` crawling the whole shop from its first page, reading each product's name and price with CSS | pages/s, items/s, request latency (p50, p90), CPU seconds, peak memory, bytes, errors |
| `parse` | parsing a listing page and reading its 20 cards | pages/s |
| `extract` | the `product` template on product pages: structured data, then the heuristics | pages/s |
| `data` | a pipeline normalizing and validating product records (prices in three formats, ratings, dates, weights) | records/s |
| `dedupe` | making 10,000 URLs canonical (each page in five spellings: host case, port, parameter order, tracking parameters, fragment), with an empty cache; SimHash fingerprints of pages | URLs/s, pages/s |
| `outputs` | 5,000 product records (numbers, booleans, a list, an object) written to each output a crawl can write, through the same code a crawl uses, and read back: JSON Lines, CSV, JSON, SQLite, and Parquet, Excel and DuckDB when installed. `--store URL` adds a database table or an S3 object | records/s written and read, the file's size |
| `browser` | `--browser`: 5 to 30 product pages rendered in Chromium one after another, and the same pages over HTTP | pages/s, and how many times slower |

Rates are the median of three runs of the scenario's work. The crawl runs
once, with AutoThrottle off: it measures what the crawler can do, not what
a site should get.

## Outputs

```bash
wintergrab benchmark --scenario outputs --store "postgresql://crawler@127.0.0.1/shop?table=bench" \
    --store "mongodb://127.0.0.1/shop?collection=bench" --store "mysql://crawler@127.0.0.1/shop?table=bench"
```

```
outputs  .jsonl       654,546.8 records/s written, 496,004.4 read (1.02 MB): 5,000 records
outputs  .csv         132,517.7 records/s written, 336,888.8 read (0.75 MB): 5,000 records
outputs  .json        617,266.3 records/s written, 420,496.1 read (1.02 MB): 5,000 records
outputs  .sqlite      49,657.3 records/s written, 181,340.6 read (0.75 MB): 5,000 records
outputs  .parquet     55,593.3 records/s written, 127,802.9 read (0.14 MB): 5,000 records
outputs  .xlsx        9,599.6 records/s written, 8,916.8 read (0.29 MB): 5,000 records
outputs  postgresql   17,279.6 records/s written, 111,410.9 read: 5,000 records
outputs  mongodb      25,557.3 records/s written, 121,128.3 read: 5,000 records
outputs  mysql        20,954.2 records/s written, 64,856.5 read: 5,000 records
```

(The same 4-vCPU machine, its URLs shortened here: PostgreSQL 16, MongoDB 7
and MariaDB 10.11 on the machine itself, so no network time is counted.)
Every output writes far more records a second than a crawl finds, Excel
included. A table or collection named with `--store` is written as a fresh
crawl writes it: its rows are replaced, so name one kept for it. S3 objects
can be measured the same way; the upload is timed with the rest.

## Options

```bash
wintergrab benchmark --scenario crawl --pages 1000 --items 10000 --concurrency 64   # a bigger crawl
wintergrab benchmark --latency 50                  # each response 50 ms late, as over a network
wintergrab benchmark --quick                       # a small shop and few rounds: a few seconds
wintergrab benchmark --json -o bench.json          # the report as JSON, to keep or compare
```

The exit status is 1 when a scenario failed (the report says why). From
Python, `run_benchmark()` returns the report as a dictionary:

```python
from wintergrab.bench import describe, run_benchmark

report = run_benchmark(scenarios=["crawl"], pages=200, items=2000, concurrency=64)
report["results"]["crawl"]["pages_per_s"]
print(describe(report))
```

## Latency changes the picture

On 127.0.0.1 a response takes a fraction of a millisecond, so the crawl is
bound by CPU (0.745 CPU seconds for 1,100 pages above), and a browser looks
very slow. With 50 ms per response, as over a network:

```
wintergrab 0.2.0 on Linux x86_64, CPython 3.11.15, 4 CPUs; shop: 100 listing + 1000 product pages, 50.0 ms latency

crawl    pages/s            450.4 (1,100 pages, 1,000 items, 0 errors, concurrency 32)
crawl    items/s            409.4
crawl    latency p50 / p90  56.6 / 64.2 ms
crawl    CPU / peak memory  0.931 s / 46.3 MB (3.7 MB downloaded)
browser  pages/s            6.4 (3.0x slower than HTTP from this machine: 19.5 pages/s, one page at a time)
```

Latency now sets the pace. 32 requests in flight at 56.6 ms allow at most
565 pages/s, and the crawl reaches 80% of that. A browser costs 3 times as
much as HTTP, not 266 times: this is why [adaptive
fetching](spiders.md#http-first-a-browser-when-needed) tries HTTP first. On real sites,
the site and your politeness settings (AutoThrottle, delays, robots.txt)
set the pace.

## Against other crawlers

`benchmarks/` in the repository compares WINTERGRAB's spider with Scrapy and
Crawlee on the same shop, served by a server fast enough never to be the
bottleneck, at several latencies and concurrencies, and explains its method:
see [benchmarks/README.md](../benchmarks/README.md). `benchmarks/bench_pages.py`
and `benchmarks/bench_data.py` time extraction, classification and the data
layer in more detail.
