# Crawler benchmarks

A reproducible throughput benchmark for wintergrab's `Spider` against Scrapy
and Crawlee (`ParselCrawler`), on a synthetic shop served from localhost by
a very fast server, plus tooling to profile wintergrab's crawl path.

| File | What it does |
|---|---|
| `fastserver.py` | Raw `asyncio.Protocol` HTTP/1.1 keep-alive server. Every response (headers + body) is pre-rendered in memory. Optional per-response latency, optional `SO_REUSEPORT` worker processes, `/__stats` and `/__reset` request counters. |
| `loadgen.py` | Multi-process raw-socket keep-alive load generator, used to prove the server is never the bottleneck. |
| `bench_wintergrab.py`, `bench_scrapy.py`, `bench_crawlee.py` | The same crawl implemented idiomatically for each tool. Each prints one `RESULT {json}` line. |
| `_common.py` | Shared selectors, CPU/RSS measurement and the item check. It has no dependencies, so it runs in any venv. |
| `run.py` | Runs the matrix (latency × concurrency × tool × repetitions). Each run is its own subprocess. Prints a markdown table and writes `results.json`. |
| `results.json`, `results_eventloop.json` | The measured runs behind the tables below. |
| `profile_wintergrab.py` | Runs the wintergrab crawl under cProfile, or reads `py-spy` stacks. Groups the time by layer: wintergrab, curl_cffi, lxml, asyncio, stdlib. |

## Running it

```bash
# wintergrab only (project venv)
.venv/bin/python benchmarks/run.py

# against Scrapy and Crawlee, installed in a separate venv
uv venv -p python3.11 /tmp/compvenv
VIRTUAL_ENV=/tmp/compvenv uv pip install scrapy "crawlee[parsel]" uvloop
.venv/bin/python benchmarks/run.py \
    --tools wintergrab,wintergrab-noimp,scrapy,scrapy-epoll,crawlee \
    --competitor-python /tmp/compvenv/bin/python --server-check

# smaller/faster, or a single scenario
.venv/bin/python benchmarks/run.py --pages 500 --items 2000 --latency 0 --concurrency 64 -r 5

# one tool by hand
python benchmarks/fastserver.py --pages 2000 --items 8000 --latency 0 --port 0   # prints http://127.0.0.1:PORT
python benchmarks/bench_wintergrab.py --url http://127.0.0.1:PORT --concurrency 64
python benchmarks/loadgen.py http://127.0.0.1:PORT --processes 3 --connections 256

# profiling
.venv/bin/python benchmarks/profile_wintergrab.py --concurrency 64 --latency 0 --out /tmp/wg.prof
py-spy record --native -r 250 -f raw -o /tmp/stacks.txt -- .venv/bin/python benchmarks/bench_wintergrab.py --url ... --concurrency 64
.venv/bin/python benchmarks/profile_wintergrab.py --collapsed /tmp/stacks.txt
```

`run.py` flags: `--pages`, `--items`, `--concurrency 16,64,256`, `--latency 0,20` (ms),
`-r/--repetitions`, `--tools` (`wintergrab`, `wintergrab-noimp`, `wintergrab-asyncio`,
`scrapy`, `scrapy-epoll`, `scrapy-uvloop`, `crawlee`, `crawlee-uvloop`),
`--python FAMILY=PATH` / `--competitor-python PATH`, `--server-workers`,
`--server-check`, `--no-jump-links`, `--timeout`, `--out`.

## Methodology

**The site.** There are 2,000 listing pages (`/page/<i>`, about 22 KB each) and 8,000 detail pages (`/item/<j>`, about 9.5 KB each).
A listing page has a `<title>`, inline CSS and JS, a header nav with 30 category links, 20 product cards
(name, price, rating, blurb, link to `/item/<j>`) and a footer with 24 off-site links.
Its pager links to the next 5 pages (`i+1..i+5`) and to 5 "jump" pages (`5i+1..5i+5`).
Each item is linked from 5 different listing pages, so duplicate filtering matters: a crawl sees about 54,000 links for 10,000 unique URLs.

The jump links keep the discovery graph about log5(2000) ≈ 5 levels deep.
With only next-5 links, page `i` is `i/5` hops from `/page/0`.
The critical path would then be 400 sequential round trips, which is 8 s at 20 ms latency.
The benchmark would measure queue order (Scrapy is LIFO, wintergrab FIFO) instead of throughput.
Use `--no-jump-links` to reproduce that deep graph.

**The crawl (the same for every tool).** The crawl starts at `/page/0`.
On listing pages it follows `div.card a.item-link::attr(href)` to item pages and `nav.pager a::attr(href)` to other listing pages.
Links are joined against the page URL, deduplicated, and kept on the same domain.
On item pages it extracts the name (`h1::text`), the price (`.price::text`) and the URL, then counts the item in a no-op pipeline:
- wintergrab: `process_item`
- Scrapy: an `ITEM_PIPELINES` class
- Crawlee: a counter in the handler, standing in for `push_data`

**The settings.** Every tool gets the same concurrency N, both globally and per domain.
All of them run with robots.txt off, no delays or AutoThrottle, no HTTP cache, and logging at ERROR or off.
Cookies, retries, redirects and compression stay at each tool's defaults.
- **wintergrab:** `Spider(concurrency=N, concurrency_per_domain=N, autothrottle=False, obey_robots_txt=False, keep_items=False, log_level=None, impersonate="chrome")`. `wintergrab-noimp` uses `impersonate=None`. wintergrab runs on uvloop when it is installed (its default). `wintergrab-asyncio` turns that off.
- **Scrapy:**
  - `CONCURRENT_REQUESTS = CONCURRENT_REQUESTS_PER_DOMAIN = N` and `DOWNLOAD_DELAY = 0`
  - `AUTOTHROTTLE_ENABLED`, `ROBOTSTXT_OBEY`, `HTTPCACHE_ENABLED` and `TELNETCONSOLE_ENABLED` all `False`
  - `LOG_LEVEL = "ERROR"`
  - `scrapy` uses the default asyncio reactor, `scrapy-epoll` uses the EPoll reactor, and `scrapy-uvloop` sets `ASYNCIO_EVENT_LOOP = "uvloop.Loop"`
  - The start request is `dont_filter=False`, like in the other tools. With `start_urls`, `/page/0` would be fetched twice.
- **Crawlee:** `ParselCrawler` with `ConcurrencySettings(min = desired = max = N)`. It uses the default HTTP client (`ImpitHttpClient`) and `MemoryStorageClient`, so there is no request-queue or dataset I/O on disk. Links are followed with `enqueue_links(selector=...)`.

**Measurement.**
- Each run is a fresh subprocess.
- Wall and CPU time cover the crawl itself, from crawler construction to its end. Interpreter start-up and imports are excluded, but `run.py` also records the whole process wall time as `process_wall_s`.
- CPU is `getrusage(SELF) + getrusage(CHILDREN)`, user plus system. It includes every thread, which matters for Crawlee's Rust HTTP client.
- Peak RSS is `ru_maxrss`.
- **Pages/s** counts all HTTP responses (listing and item pages) per wall-clock second.
- Each cell is the **median of 3 repetitions**. The tool order rotates between repetitions.
- Before every run, `run.py` resets the server counters.

**Correctness check.** A run counts as correct only if all of the following hold:
- The tool parsed exactly 2,000 listing pages and 8,000 items.
- Every item has a name, a `$` price and its URL.
- The server counted exactly 10,000 requests and no 404s, so nothing was fetched twice and nothing was missed.

**Environment.** Crawler subprocesses run with all `*_proxy` variables removed.
In the sandbox this was measured in, `HTTPS_PROXY`/`NO_PROXY` were set.
With them, Scrapy's `HttpProxyMiddleware` calls `proxy_bypass()` on every request, which re-scans the whole environment.
That cost it about 1 ms per request and has nothing to do with Scrapy itself.

**Server capacity.** With `--server-check`, `run.py` load-tests each server before the crawls, using `loadgen.py` (3 processes, 255 keep-alive connections, same page/item mix).
One server process served **≈19,800 req/s** (≈240 MB/s) at 0 ms latency, about 19× the fastest crawler. By hand, with 2 load processes and 64 connections, it served ≈40,000 req/s.
With 2 `SO_REUSEPORT` workers it served ≈52,000 req/s.
At 20 ms latency the server sustained ≈11,800 req/s over 255 connections, close to its theoretical limit of 255 / 0.02 s.
Every crawler result below is therefore limited by the crawler.

## Results

**Machine and software**
- 4 CPUs (`os.cpu_count()` = 4): Intel Xeon @ 2.10 GHz, a Firecracker VM, Linux 6.18. CPython 3.11.15 for everything.
- wintergrab 0.2.0 at commit `97d46d1` (clean tree), with curl_cffi 0.16.3, lxml 6.1.3, cssselect 1.5.0 and uvloop 0.22.1.
- Scrapy 2.19.0 with Twisted 26.4.0, parsel 1.11.0 and lxml 6.1.3.
- Crawlee 1.10.2 with impit 0.14.1 and parsel 1.11.0.
- Run on 2026-09-24 with `run.py --tools wintergrab,wintergrab-noimp,scrapy,scrapy-epoll,crawlee --server-check`: 2,000 pages and 8,000 items, 3 repetitions, medians. Full data is in `results.json`.

**All 90 runs were correct.** Every tool fetched exactly 2,000 listing pages and 8,000 items, every item had a name and price, and the server counted exactly 10,000 requests with no 404s and no duplicates.

| Tool | Latency | Concurrency | Pages/s (median) | min-max | Wall s | CPU s | CPU ms/page | Peak RSS MB | Correct |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| wintergrab-noimp | 0 ms | 16 | **1015** | 1014-1044 | 9.85 | 9.74 | 0.97 | 52 | 3/3 |
| wintergrab | 0 ms | 16 | **974** | 968-1001 | 10.27 | 10.11 | 1.01 | 52 | 3/3 |
| crawlee | 0 ms | 16 | **569** | 568-578 | 17.57 | 21.15 | 2.12 | 106 | 3/3 |
| scrapy | 0 ms | 16 | **286** | 282-287 | 34.91 | 34.57 | 3.46 | 124 | 3/3 |
| scrapy-epoll | 0 ms | 16 | **238** | 236-238 | 42.09 | 41.67 | 4.17 | 105 | 3/3 |
| wintergrab-noimp | 0 ms | 64 | **1099** | 1074-1116 | 9.10 | 9.01 | 0.90 | 57 | 3/3 |
| wintergrab | 0 ms | 64 | **1038** | 1036-1044 | 9.63 | 9.46 | 0.95 | 58 | 3/3 |
| crawlee | 0 ms | 64 | **572** | 570-576 | 17.47 | 21.15 | 2.12 | 130 | 3/3 |
| scrapy | 0 ms | 64 | **274** | 273-281 | 36.56 | 36.09 | 3.61 | 128 | 3/3 |
| scrapy-epoll | 0 ms | 64 | **219** | 217-220 | 45.74 | 45.26 | 4.53 | 117 | 3/3 |
| wintergrab-noimp | 0 ms | 256 | **1051** | 1031-1065 | 9.51 | 9.24 | 0.92 | 84 | 3/3 |
| wintergrab | 0 ms | 256 | **1008** | 996-1031 | 9.91 | 9.73 | 0.97 | 86 | 3/3 |
| crawlee | 0 ms | 256 | **517** | 505-531 | 19.34 | 23.15 | 2.31 | 186 | 3/3 |
| scrapy | 0 ms | 256 | **249** | 246-251 | 40.23 | 39.75 | 3.98 | 191 | 3/3 |
| scrapy-epoll | 0 ms | 256 | **196** | 192-201 | 50.99 | 50.47 | 5.05 | 198 | 3/3 |
| wintergrab-noimp | 20 ms | 16 | **454** | 453-459 | 22.01 | 9.77 | 0.98 | 51 | 3/3 |
| wintergrab | 20 ms | 16 | **447** | 446-452 | 22.37 | 9.97 | 1.00 | 52 | 3/3 |
| crawlee | 20 ms | 16 | **322** | 320-324 | 31.11 | 23.39 | 2.34 | 109 | 3/3 |
| scrapy | 20 ms | 16 | **237** | 233-240 | 42.26 | 35.20 | 3.52 | 121 | 3/3 |
| scrapy-epoll | 20 ms | 16 | **229** | 226-232 | 43.71 | 43.16 | 4.32 | 109 | 3/3 |
| wintergrab | 20 ms | 64 | **879** | 868-886 | 11.38 | 9.65 | 0.96 | 58 | 3/3 |
| wintergrab-noimp | 20 ms | 64 | **877** | 861-893 | 11.40 | 9.34 | 0.93 | 58 | 3/3 |
| crawlee | 20 ms | 64 | **472** | 469-477 | 21.18 | 23.16 | 2.32 | 131 | 3/3 |
| scrapy | 20 ms | 64 | **277** | 276-277 | 36.16 | 35.34 | 3.53 | 130 | 3/3 |
| scrapy-epoll | 20 ms | 64 | **222** | 220-224 | 45.07 | 44.39 | 4.44 | 110 | 3/3 |
| wintergrab-noimp | 20 ms | 256 | **1044** | 1008-1065 | 9.58 | 9.38 | 0.94 | 85 | 3/3 |
| wintergrab | 20 ms | 256 | **999** | 997-1006 | 10.01 | 9.78 | 0.98 | 86 | 3/3 |
| crawlee | 20 ms | 256 | **509** | 497-509 | 19.65 | 23.68 | 2.37 | 209 | 3/3 |
| scrapy | 20 ms | 256 | **243** | 242-247 | 41.16 | 40.68 | 4.07 | 186 | 3/3 |
| scrapy-epoll | 20 ms | 256 | **190** | 187-193 | 52.53 | 52.02 | 5.20 | 182 | 3/3 |

**Compared with the previous run** (commit `58dca25` plus uncommitted crawl-loop work, same machine, earlier the same day; called "before" below), wintergrab went from 866 to 1,038 pages/s at 0 ms / concurrency 64 (+20%) and from 735 to 879 at 20 ms / concurrency 64 (+20%). CPU per page dropped from 1.14 to 0.95 ms. Scrapy and Crawlee, which did not change, came within 6% of their earlier numbers (most cells slightly higher), so the machine was not slower during the first run. The [optimizations](#optimizations) section lists what changed.

Event-loop side experiment at 0 ms latency, concurrency 64 (`results_eventloop.json`). This was measured on commit `56df668`, before the hot-path work, so wintergrab's absolute numbers are lower than in the table above. What it shows is the relative effect: uvloop is worth 1-5% to every tool.

| Tool | Pages/s | CPU ms/page |
|---|---:|---:|
| wintergrab (uvloop, its default) | 864 | 1.13 |
| wintergrab-asyncio | 856 | 1.16 |
| crawlee-uvloop | 587 | 2.02 |
| crawlee | 558 | 2.15 |
| scrapy-uvloop | 282 | 3.49 |
| scrapy (asyncio reactor) | 271 | 3.65 |

**Reading the numbers**
- **Every tool is CPU-bound on one core at 0 ms latency.** CPU s ≈ wall s. Crawlee's CPU exceeds wall because impit runs its own Rust threads.
- **wintergrab needs about 0.95 ms CPU per page.** Crawlee needs 2.1 ms (2.2× more) and Scrapy 3.6 ms (3.8× more). At concurrency 64, wintergrab gets 1.8× Crawlee's throughput and 3.8× Scrapy's, with less than half the memory of either.
- **`impersonate="chrome"` costs 0-6%** over plain curl on plain HTTP. Its share grew as the rest of the crawl got cheaper. On HTTPS the TLS fingerprint work is in libcurl and was not measured here.
- **For Scrapy, the asyncio reactor (its default) beat the EPoll reactor by 3-28% in every cell.** `scrapy` is therefore the configuration to compare against.
- **Scrapy's cost is not a regression in 2.19.** Scrapy 2.11.2 with Twisted 23.10 was not faster in a smaller spot check.
- **Where Scrapy's time goes:** most of it is Twisted Deferred machinery in the engine and middleware chains. Each response also runs through `parallel()` with `CONCURRENT_ITEMS=100`, which starts 100 cooperative iterators per response.
- **At 20 ms latency and concurrency 16, the ceiling is about 16 / 21 ms ≈ 760 req/s.**
  - A bare `AsyncFetcher` loop with 16 workers reached 710 req/s (measured before).
  - The wintergrab spider reaches 447 at only 45% CPU. Per-request CPU work queues up on the single event loop between curl_cffi's socket callbacks, which stretches every fetch well beyond 21 ms.
  - Cutting CPU helps directly. The 17% CPU cut in 0.2 took this cell from 411 to 447. Before, disabling parsing, the block check and the deadline wrapper gave 565 pages/s.
  - Two dispatcher changes were tried before and made no difference (412 → 413): refilling the pipeline synchronously in `_task_done`, and freeing the download slot before the callback runs.
  - At concurrency 64 the gap mostly closes (879). At 256 the crawl is CPU-bound again (999).

## Profile of wintergrab (latency 0, concurrency 64)

Measured with py-spy (`--native`, 250 Hz, 3,914 samples) on commit `97d46d1`. Each sample is charged to the innermost identifiable frame. The "Before" column is the same measurement on the previous run's build (4,091 samples), which used 1.14 ms CPU per page; this build uses 0.95 ms. The shares are of a smaller total, so a layer whose absolute cost did not change (lxml parsing, curl_cffi) takes a larger share now.

`profile_wintergrab.py` also runs the crawl under cProfile, but that runs 1.8× slower and inflates small Python calls. Use it to find call counts, not shares.

| Layer | Before | Now | Main contents now |
|---|---:|---:|---|
| lxml | 35% | 36% | HTML parse 15%, XPath evaluation 7% (14% before), tree free 2% |
| curl_cffi | 27% | 29% | Python layer ≈14%: `Response.__init__`, `Curl.setopt` ×13 per request plus `impersonate()` re-applied because handles are `reset()` after every request, `getinfo` ×16 in `_parse_response`. libcurl and cffi ≈14%. |
| wintergrab | 18% | 21% | block-page check 7%, link handling (`follow`, `_enqueue_child`, fingerprint, host) ≈5%, engine bookkeeping |
| urllib.parse | 10% | 0.4% | only links the fast paths don't cover |
| asyncio/uvloop | 5% | 5% | |
| other stdlib | 5% | 8% | `re` (the fast paths), sha1, `isinstance`, imports at start-up |

The engine's own scheduling is cheap: `engine.py`, `scheduler.py` and `throttle.py` together take about 5% self time.

## Optimizations

Done for 0.2, guided by the "before" profile. Each shortcut is tested to return exactly what the code it bypasses returns (`tests/test_fast_paths.py`). The URL shortcuts were also differential-fuzzed against `urllib.parse` over more than two million generated URLs with no mismatch.

| Change | Where | Effect |
|---|---|---|
| Absolute and root-relative hrefs skip `urljoin()` when it would return them unchanged or just prefixed with the origin. | `utils.fast_urljoin` | 4.3 → 0.5 µs per link. Relative links cost 0.35 µs more. |
| One regex recognises already-canonical URLs, including non-default ports. The full normalisation is cached. | `utils.canonicalize_url` | 5.0 → 0.8 µs |
| Hosts are parsed by one regex. `Request.host` and `Request.fingerprint()` are computed once per request. | `utils.host_of`, `request.py` | +4% pages/s on its own |
| Compiled XPath objects are cached per thread. A substring prefilter runs before cssselect's `normalize-space(@class)` test. | `parser/selector.py`, `parser/css.py` | XPath share 14% → 7% |
| The block-page check searches the raw bytes instead of decoding the page. | `fetchers/blocking.py` | small |

Tried and rejected:
- **Large LRU caches (65,536 entries) on the URL functions.** They gave a little more throughput but pushed the disk frontier's flat-memory test over its 16 MB budget. The regex fast paths get the win and hold no memory.
- **One regex for the block-page markers.** An alternation was 5× slower than the 14 substring scans, and a case-insensitive one was 35× slower. The scans already run at memory speed (≈6 GB/s).
- **Scanning only the `<title>` and the first 2 KB for the phrase markers.** It is faster, but it would miss challenge pages whose text sits further down. Detection wins here. A spider that only crawls sites known never to serve challenge pages can override `is_blocked` (see [anti-blocking](../docs/anti-blocking.md#6-detecting-blocks)).

Remaining ideas, ranked by expected gain:
1. **Stop paying for curl_cffi's `requests` layer on the crawl path** (`fetchers/http.py`). Drive `curl_cffi.AsyncCurl` with a pool of `Curl` handles directly:
   - Apply impersonation and the static options once per handle, and don't `reset()` it after every request.
   - Per request, set only the URL, method and extra headers.
   - Collect the body and headers through callbacks, and read 2-3 `getinfo` values instead of 16.
   - Keep cookies in libcurl through a `CURLSH` share.

   Expected: **-10 to -14% CPU per page**. High effort, because redirects, cookies and proxies must keep working.
2. **Trim engine bookkeeping per response** (`spider/engine.py`, `Stats.inc`, `throttle.py`):
   - Skip the options copy when `request.options` is empty.
   - Keep `latencies` in a `deque`.
   - Pre-build the `status/NNN` stat keys.

   Expected: **-1 to -2%**.
3. **The lxml parse itself** (about 15%). Reducing it would need a different parser backend.

Not worth pursuing:
- Parsing from bytes instead of `text.encode()`: the encode is 12 µs of a 350 µs parse.
- Tuning the dispatcher: both variants tried made no difference.
- GC: no samples in the garbage collector.

## Data layer

`bench_data.py` measures the data layer on synthetic product records (no
network): normalizers, schema normalization and validation, expressions, a
five-stage pipeline, near-duplicate detection, quality monitoring and entity
resolution.

```bash
.venv/bin/python benchmarks/bench_data.py --records 20000 --repeat 5
```

One core of a 4-vCPU cloud VM, Python 3.11.15, median of 5 runs:

| Operation | Rate | Time per item |
|---|---:|---:|
| parse_money | 94,924 values/s | 10.5 µs |
| parse_date | 166,308 values/s | 6.0 µs |
| expression (3 comparisons) | 1,053,321 records/s | 0.9 µs |
| Schema.normalize (10 fields) | 7,879 records/s | 126.9 µs |
| Schema.validate (10 fields) | 41,247 records/s | 24.2 µs |
| pipeline: normalize, validate, filter, compute, dedupe | 4,169 records/s | 239.9 µs |
| Deduplicator near=True (40-word texts) | 6,347 records/s | 157.6 µs |
| QualityMonitor.observe + report | 6,970 records/s | 143.5 µs |
| EntityResolver: add + resolve (company names) | 3,769 mentions/s | 265.4 µs |

The entity resolution row resolves 20,000 generated company names (two words
from 60, ten legal forms and generic suffixes, some in capitals, 30% with a
website): 15,558 distinct mentions and about 300,000 comparisons, with the
name caches cleared before each run.

URL normalization is the largest single cost inside `Schema.normalize`
(about a fifth of it for the benchmark's all-distinct URLs).

## Page analysis

`bench_pages.py` measures extraction, page classification, technology
detection and history snapshots on synthetic pages (no network). Every
measurement builds a fresh `Response`, so HTML parsing is included, as in a
crawl; "parse only" is that baseline.

```bash
.venv/bin/python benchmarks/bench_pages.py --repeat 5 --rounds 200
```

One core of a 4-vCPU cloud VM, Python 3.11.15, median of 5 runs of 200
pages (10 for the large page):

| Page | parse only | Extractor.extract (13 fields) | classify_page | detect_technologies | snapshot_page |
|---|---:|---:|---:|---:|---:|
| product page, JSON-LD (11 KB) | 0.26 ms | 6.72 ms | 3.03 ms | 0.86 ms | 2.44 ms |
| product page, no structured data (10 KB) | 0.26 ms | 6.83 ms | 3.07 ms | 0.86 ms | 2.36 ms |
| category page, 60 cards (10 KB) | 0.29 ms | 6.04 ms | 2.61 ms | 0.74 ms | 3.37 ms |
| large product page (482 KB) | 1.48 ms | 61.79 ms | 19.68 ms | 11.69 ms | 16.29 ms |

`classify_url`: 11.5 µs per URL.

A `HealingExtractor` watching three of the thirteen fields (read with
selectors) takes 8.02 ms per product page with JSON-LD, against 7.68 ms for
`Extractor.extract` with the same schema. The extra 0.3 ms covers counting
the selectors' matches, remembering the elements they match, and saving
its state every 50 pages.

The large page is mostly text. Scanning long texts is where regular
expressions without a literal start cost the most: the money pattern was
restructured so every alternative starts with a literal character (36 ms to
2 ms over 480 KB of text without prices), and technology fingerprints only
scan a page when it contains their literal parts (134 ms to 12 ms on this page, with the
same detections).
