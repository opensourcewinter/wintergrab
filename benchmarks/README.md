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
One server process served **≈23,000 req/s** (≈280 MB/s) at 0 ms latency, about 27× the fastest crawler.
With 2 `SO_REUSEPORT` workers it served ≈52,000 req/s.
At 20 ms latency the server sustained ≈12,000 req/s over 255 connections, which is its theoretical limit of 255 / 0.02 s.
Every crawler result below is therefore limited by the crawler.

RESULTS_PLACEHOLDER
