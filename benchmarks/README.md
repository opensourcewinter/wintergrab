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
One server process served **≈23,000 req/s** (≈280 MB/s) at 0 ms latency, about 27× the fastest crawler. By hand, with 2 load processes and 64 connections, it served ≈40,000 req/s.
With 2 `SO_REUSEPORT` workers it served ≈52,000 req/s.
At 20 ms latency the server sustained ≈12,000 req/s over 255 connections, which is its theoretical limit of 255 / 0.02 s.
Every crawler result below is therefore limited by the crawler.

## Results

**Machine and software**
- 4 CPUs (`os.cpu_count()` = 4): Intel Xeon @ 2.10 GHz, a Firecracker VM, Linux 6.18. CPython 3.11.15 for everything.
- wintergrab 0.1.0 (commit `58dca25` plus uncommitted `src/` work in progress, `src_diff_sha1` `ce8c7f14584b` in `results.json`), with curl_cffi 0.16.3, lxml 6.1.3, cssselect 1.5.0 and uvloop 0.22.1.
- Scrapy 2.19.0 with Twisted 26.4.0, parsel 1.11.0 and lxml 6.1.3.
- Crawlee 1.10.2 with impit 0.14.1 and parsel 1.11.0.
- Run on 2026-09-24 with `run.py --tools wintergrab,wintergrab-noimp,scrapy,scrapy-epoll,crawlee --server-check`: 2,000 pages and 8,000 items, 3 repetitions, medians. Full data is in `results.json`.

**All 90 runs were correct.** Every tool fetched exactly 2,000 listing pages and 8,000 items, every item had a name and price, and the server counted exactly 10,000 requests with no 404s and no duplicates.

| Tool | Latency | Concurrency | Pages/s (median) | min-max | Wall s | CPU s | CPU ms/page | Peak RSS MB | Correct |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| wintergrab | 0 ms | 16 | **837** | 807-838 | 11.95 | 11.82 | 1.18 | 48 | 3/3 |
| wintergrab-noimp | 0 ms | 16 | **832** | 829-854 | 12.02 | 11.84 | 1.18 | 48 | 3/3 |
| crawlee | 0 ms | 16 | **557** | 557-567 | 17.94 | 21.45 | 2.15 | 111 | 3/3 |
| scrapy | 0 ms | 16 | **274** | 273-275 | 36.45 | 35.97 | 3.60 | 126 | 3/3 |
| scrapy-epoll | 0 ms | 16 | **227** | 226-228 | 44.08 | 43.47 | 4.35 | 107 | 3/3 |
| wintergrab-noimp | 0 ms | 64 | **880** | 856-911 | 11.36 | 11.19 | 1.12 | 55 | 3/3 |
| wintergrab | 0 ms | 64 | **866** | 855-871 | 11.55 | 11.37 | 1.14 | 56 | 3/3 |
| crawlee | 0 ms | 64 | **548** | 540-553 | 18.24 | 21.58 | 2.16 | 132 | 3/3 |
| scrapy | 0 ms | 64 | **268** | 264-269 | 37.27 | 36.69 | 3.67 | 133 | 3/3 |
| scrapy-epoll | 0 ms | 64 | **215** | 210-217 | 46.45 | 45.96 | 4.60 | 120 | 3/3 |
| wintergrab-noimp | 0 ms | 256 | **880** | 870-892 | 11.37 | 11.11 | 1.11 | 83 | 3/3 |
| wintergrab | 0 ms | 256 | **855** | 842-861 | 11.69 | 11.49 | 1.15 | 83 | 3/3 |
| crawlee | 0 ms | 256 | **488** | 473-491 | 20.51 | 24.47 | 2.45 | 184 | 3/3 |
| scrapy | 0 ms | 256 | **238** | 232-241 | 41.95 | 41.41 | 4.14 | 193 | 3/3 |
| scrapy-epoll | 0 ms | 256 | **188** | 185-189 | 53.28 | 52.61 | 5.26 | 207 | 3/3 |
| wintergrab-noimp | 20 ms | 16 | **413** | 411-416 | 24.21 | 11.89 | 1.19 | 49 | 3/3 |
| wintergrab | 20 ms | 16 | **411** | 407-413 | 24.36 | 12.09 | 1.21 | 49 | 3/3 |
| crawlee | 20 ms | 16 | **322** | 318-326 | 31.02 | 23.11 | 2.31 | 112 | 3/3 |
| scrapy | 20 ms | 16 | **239** | 235-255 | 41.81 | 35.04 | 3.50 | 120 | 3/3 |
| scrapy-epoll | 20 ms | 16 | **227** | 222-228 | 44.12 | 43.48 | 4.35 | 108 | 3/3 |
| wintergrab-noimp | 20 ms | 64 | **750** | 734-760 | 13.33 | 11.29 | 1.13 | 56 | 3/3 |
| wintergrab | 20 ms | 64 | **735** | 733-776 | 13.60 | 11.70 | 1.17 | 56 | 3/3 |
| crawlee | 20 ms | 64 | **473** | 467-483 | 21.13 | 22.73 | 2.27 | 136 | 3/3 |
| scrapy | 20 ms | 64 | **272** | 271-273 | 36.76 | 36.00 | 3.60 | 134 | 3/3 |
| scrapy-epoll | 20 ms | 64 | **215** | 214-218 | 46.48 | 45.62 | 4.56 | 111 | 3/3 |
| wintergrab-noimp | 20 ms | 256 | **884** | 867-886 | 11.31 | 11.07 | 1.11 | 84 | 3/3 |
| wintergrab | 20 ms | 256 | **854** | 838-864 | 11.71 | 11.48 | 1.15 | 84 | 3/3 |
| crawlee | 20 ms | 256 | **496** | 488-518 | 20.15 | 24.08 | 2.41 | 184 | 3/3 |
| scrapy | 20 ms | 256 | **235** | 234-243 | 42.54 | 41.91 | 4.19 | 188 | 3/3 |
| scrapy-epoll | 20 ms | 256 | **181** | 180-188 | 55.23 | 54.32 | 5.43 | 177 | 3/3 |

Event-loop side experiment at 0 ms latency, concurrency 64 (`results_eventloop.json`). uvloop is worth 1-5% to every tool:

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
- **wintergrab needs about 1.15 ms CPU per page.** Crawlee needs 2.2 ms (1.9× more) and Scrapy 3.6 ms (3.1× more), so wintergrab gets 1.6× Crawlee's throughput and 3.2× Scrapy's. It also uses about half the memory of either.
- **`impersonate="chrome"` costs 0-3.5%** over plain curl on plain HTTP. On HTTPS the TLS fingerprint work is in libcurl and was not measured here.
- **For Scrapy, the asyncio reactor (its default) beat the EPoll reactor by 5-30% in every cell.** `scrapy` is therefore the configuration to compare against.
- **Scrapy's cost is not a regression in 2.19.** Scrapy 2.11.2 with Twisted 23.10 was not faster in a smaller spot check.
- **Where Scrapy's time goes:** most of it is Twisted Deferred machinery in the engine and middleware chains. Each response also runs through `parallel()` with `CONCURRENT_ITEMS=100`, which starts 100 cooperative iterators per response.
- **At 20 ms latency and concurrency 16, the ceiling is about 16 / 21 ms ≈ 760 req/s.**
  - A bare `AsyncFetcher` loop with 16 workers reaches 710 req/s.
  - The wintergrab spider reaches 411 at only 50% CPU. Per-request CPU work queues up on the single event loop between curl_cffi's socket callbacks, which stretches every fetch from about 21 ms to about 29 ms.
  - Cutting CPU helps directly. With parsing, the block check and the deadline wrapper disabled, the same crawl ran at 565 pages/s.
  - Two dispatcher changes were tried and made no difference (412 → 413): refilling the pipeline synchronously in `_task_done`, and freeing the download slot before the callback runs.
  - At concurrency 64 the gap mostly closes (735). At 256 the crawl is CPU-bound again (854).

## Profile of wintergrab (latency 0, concurrency 64)

These were measured two ways.
- **cProfile** (`profile_wintergrab.py`) runs the crawl 1.8× slower and inflates small Python calls.
- **py-spy** (`--native`, 250 Hz, 4,091 samples) gives an unbiased split.

In both, lxml's parse and XPath time is C code inside `parse_document` and `_xpath_raw`.

Self time by layer (py-spy; each sample is charged to the innermost identifiable frame):

| Layer | Share | Main contents |
|---|---:|---|
| lxml | 35% | HTML parse 18%, tree free 2%, XPath evaluation 14% |
| curl_cffi | 27% | **Python layer 16%**: `Response.__init__`, `Curl.setopt` ×13/request plus `impersonate()` re-applied because handles are `reset()` after every request, `getinfo` ×16 in `_parse_response`, `Headers`. libcurl C 11%. |
| wintergrab | 18% | `looks_blocked` 5.6%, `Response.follow`/`urljoin`, `Scheduler.push` → `Request.fingerprint`/`canonicalize_url`, `host_of`, engine bookkeeping |
| urllib.parse | 10% | called by wintergrab: `urljoin`, `urlsplit` (×3 per request through `host_of`), `quote`/`parse_qsl`/`urlencode` in `canonicalize_url` |
| asyncio/uvloop | 5% | |
| other stdlib | 5% | sha1, `isinstance`, imports |

Top wintergrab functions by cumulative time (cProfile, 20.3 s total):
- `engine._process` 18.1 s
- `_run_callback` → `_consume` 11.3 s (the spider callbacks: `Response.css` 4.4 s, `_enqueue_child` 3.6 s)
- `_deadline` → `AsyncFetcher.request` 5.0 s
- `Response.follow` 2.7 s, `Selector.__init__` / `parse_document` 2.3 s, `Scheduler.push` 2.3 s, `Request.fingerprint` 2.1 s, curl_cffi `_parse_response` 2.1 s, `Selector._xpath_raw` 1.9 s, `canonicalize_url` 1.8 s, `curl_cffi set_curl_options` 1.4 s, `host_of` 1.2 s (74k calls), `looks_blocked` 1.2 s (150k generator steps)

The engine's own scheduling is cheap: `engine.py` + `scheduler.py` + `throttle.py` together take about 5% self time.

## Optimization suggestions (not implemented)

The suggestions are ranked by expected gain on this workload. The baseline is about 1.14 ms CPU per page, and every tool here is CPU-bound, so a CPU cut is a throughput gain. Estimates come from the profiles above and from micro-benchmarks on the benchmark's own pages.

1. **Stop paying for curl_cffi's `requests` layer on the crawl path.**
   - *Where:* `fetchers/http.py` (`AsyncFetcher.request`, `_to_response`).
   - *What:* drive `curl_cffi.AsyncCurl` with a pool of `Curl` handles directly:
     - Apply impersonation and the static options once per handle, and don't `reset()` it after every request.
     - Per request, set only the URL, method and extra headers.
     - Collect the body and headers with `WRITEFUNCTION`/`HEADERFUNCTION` into buffers.
     - Read 2-3 `getinfo` values instead of 16.
     - Keep cookies in libcurl through a `CURLSH` share.
   - *Expected:* **-10 to -14% CPU/page**, most of the 16% Python-layer share. High effort, because redirects, cookies and proxies must keep working.
2. **Make link handling cheap: about 15 µs → 1.5 µs per link, 54k links per crawl.**
   - *Where:*
     - `parser/selector.py:Selector.urljoin`
     - `fetchers/response.py:Response.follow/urljoin`
     - `request.py:Request.fingerprint`
     - `utils.py:canonicalize_url/host_of`
     - `engine.py:_enqueue_child/_dispatch`
     - `scheduler.py:push`
   - *What:*
     - (a) Fast-path `/path` and `http(s)://` hrefs against a per-document cached `scheme://netloc` instead of `urllib.parse.urljoin`.
     - (b) Compute the host once per `Request` and reuse it. Today there are 3 `urlsplit` calls per request.
     - (c) Give `canonicalize_url` an already-canonical fast path (no query, fragment, userinfo, port, upper-case or unsafe characters) and add `lru_cache(65536)`, since 80% of links are duplicates.
     - (d) Cache `Response.content_type`/`is_html`. They are recomputed on every `.css()` and `.urljoin()`: 74k times.
   - *Expected:* **-6 to -9% CPU/page**, low effort.
3. **Make the block-page check cheap.**
   - *Where:* `fetchers/blocking.py:looks_blocked/has_challenge_markers`.
   - *Today:* it runs on every 2xx HTML page under 30 KB, lowercases up to 60 KB and makes 14 substring scans: 125 µs per 22 KB page, **5-7% CPU**.
   - *What:* search the raw bytes with `bytes.find` for the case-stable tokens (`cf-chl-`, `challenge-platform`, `_incapsula_resource`, `px-captcha`, `ddos-guard`, `cf-browser-verification`). Lowercase only the `<title>` and the first ~2 KB for the phrase markers. A single case-insensitive regex is *slower* (2.2 ms), so avoid that approach.
   - *Expected:* **-5%**, low effort.
4. **Make XPath cheaper.**
   - *Where:* `parser/selector.py:_xpath_raw` and `parser/css.py`.
   - *What:*
     - Cache compiled `etree.XPath` objects, because `root.xpath(str)` recompiles every call. This took `h1::text` from 7.3 to 3.7 µs and the pager query from 16.6 to 10.5 µs.
     - Emit a `contains(@class,'x')` pre-filter before the `concat(' ', normalize-space(@class), ' ')` test in the class translation. This took `.price::text` from 96 to 69 µs.
   - *Expected:* **-2 to -4%**.
5. **Trim engine bookkeeping per response.**
   - *Where:* `spider/engine.py:_process/_fetch_options`, `Stats.inc`, `throttle.py:on_success`.
   - *What:*
     - Skip `_fetch_options`' dict copy and set intersection when `request.options` is empty.
     - Keep `latencies` in a `deque(maxlen=20)` instead of rebuilding the list on every response.
     - Pre-build the `status/NNN` stat keys.
     - Call `parse_retry_after` only when the header is present.
   - *Expected:* **-1 to -2%**.

Not worth pursuing:
- Parsing from bytes instead of `text.encode()`: the encode is 12 µs of a 350 µs parse.
- Tuning the dispatcher: both variants tried above made no difference.
- GC: no samples in the garbage collector.

The remaining big item is the lxml parse itself (about 20%). Reducing it would need a different parser backend.

Items 1-4 together should bring wintergrab to roughly 0.8-0.9 ms per page, or about 1,100-1,250 pages/s on this machine. Because CPU queueing on the event loop is what limits the low-concurrency latency case, the 20 ms / concurrency 16 cell should improve too.
