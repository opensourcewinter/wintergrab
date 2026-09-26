# Spiders

A spider crawls many pages: it starts from some URLs, follows the links you
choose and collects items. wintergrab's spiders are asyncio-based and come
with scheduling, politeness, retries, sessions, proxies and pause/resume
built in.

```python
from wintergrab import Spider

class QuotesSpider(Spider):
    start_urls = ["https://quotes.toscrape.com/"]

    async def parse(self, response):
        for quote in response.css(".quote"):
            yield {"text": quote.css(".text::text").get(),
                   "author": quote.css(".author::text").get()}
            yield response.follow(quote.css("a[href*=author]")[0], callback=self.parse_author)
        for link in response.css("li.next a"):
            yield response.follow(link)

    def parse_author(self, response):
        return {"author": response.css(".author-title::text").get(),
                "born": response.css(".author-born-date::text").get()}

result = QuotesSpider(output="quotes.jsonl").run()
```

## Callbacks

A callback receives a `Response` and produces **items** (dicts,
dataclasses, pydantic models, anything else) and **`Request`s** to crawl
next. Any style works:

```python
def parse(self, response): return {"a": 1}                 # return an item
def parse(self, response): return [item1, request2]        # or a list
def parse(self, response): yield item                      # generator
async def parse(self, response): return item               # coroutine
async def parse(self, response):                           # async generator
    data = await some_api(response.url)
    yield data
```

The default callback is `parse`. Exceptions inside a callback are logged
(with a traceback) and counted in `stats["callback_errors"]`; the crawl
continues.

## Requests

```python
from wintergrab import Request

Request(
    "https://example.com/page",
    callback=self.parse_page,     # or the method name as a string, "parse_page"
    method="GET",
    headers={"X-Foo": "bar"},
    params={"page": 2},
    data={"form": "field"},       # or json={"key": "value"}
    cookies={"session": "..."},
    meta={"category": "mugs"},    # travels to response.meta
    cb_kwargs={"page_no": 2},     # extra keyword arguments for the callback
    priority=10,                  # higher runs first
    dont_filter=False,            # True: fetch even if the URL was seen before
    session="browser",            # which session to use (see below)
    proxy=None,                   # force a proxy for this request
    errback=self.on_failure,      # called with (request, error) if it finally fails
    options={"wait_for": ".x"},   # extra fetch options (timeout, browser waits...)
)
```

`response.follow(link, ...)` builds a request from a relative URL, an `<a>`
element or a `::attr(href)` value. It takes the same keyword arguments.
`response.follow_all(css, ...)` does it for every link that matches.

`start_requests()` yields the first requests (plain URL strings are fine).
The default makes one request per URL in `start_urls`.

## Output

```python
QuotesSpider(output="quotes.jsonl").run()   # streamed as items arrive: .jsonl, .json, .csv, .sqlite,
                                            # .parquet, .xlsx or postgresql://... (see storage.md)
result = QuotesSpider().run()
result.items          # items from this run (turn off with keep_items=False for huge crawls)
result.stats          # pages, items, retries, errors, status codes, bytes, elapsed...
result.status         # "finished", "limit", "paused" or "stopped"
result.save("out.csv")

async for item in QuotesSpider().stream():  # consume items live
    ...
```

`process_item(self, item)` (sync or async) runs for every item. Return the
item (changed or not) to keep it, or `None` to drop it. This is the place for
cleaning, validation or saving to a database.

## Sessions

A session is a fetcher with its own cookies and settings. By default a spider
has one: `"http"`, an `AsyncFetcher` built from the spider's settings. Add
your own in `configure_sessions`:

```python
from wintergrab import AsyncBrowserFetcher, AsyncFetcher

class ShopSpider(Spider):
    def configure_sessions(self, sessions):
        sessions.add("fast", AsyncFetcher(impersonate="chrome"), default=True)
        sessions.add("firefox", AsyncFetcher(impersonate="firefox"))
        sessions.add("browser", AsyncBrowserFetcher(headless=True, max_pages=4))
        sessions.add("account2", AsyncFetcher(cookies={"session": "..."}))

    def parse(self, response):
        yield response.follow("/js-page", session="browser", options={"wait_for": ".items"})
```

Sessions start lazily, so a browser session costs nothing until a request
uses it.

Shortcuts:

- `use_browser = True` makes a headless browser the default session.
- `fallback_session = "browser"` retries requests that look **blocked** (a
  challenge page, 403 + bot-wall markers, 429) through that session.
  With the default sessions, setting it to `"browser"` also registers the
  browser session for you.
- `adaptive_fetch = True` fetches over HTTP and uses the browser only for
  the pages that need it (below).

### HTTP first, a browser when needed

```python
class Shop(Spider):
    adaptive_fetch = "shop.fetch.json"   # or True: learn, but keep nothing after the crawl
    render_if_missing = [".price"]       # optional: what a usable page has
```

```bash
wintergrab crawl https://shop.example --auto-browser --fetch-stats shop.fetch.json
wintergrab get https://app.example/dashboard --auto-browser
```

Every page is fetched over HTTP first. When its HTML is not enough, the page
is fetched again in the browser, which waits for the page's own requests to
finish and records its API calls (`response.captured`). "Not enough" is
`needs_browser(response)`, which you can override; by default:

- a `render_if_missing` selector finds nothing, or
- the page is a JavaScript app shell: an app's mount point (`#root`,
  `#app`, `#__next`, `app-root`...) with next to no text (under 15
  characters: nothing, or "Loading...") on a page with under 1,000
  characters of text; or a page with under 200 characters of text and three
  scripts or more, or a `<noscript>` asking for JavaScript. Pages whose data
  is in the HTML anyway (`__NEXT_DATA__`, `window.__INITIAL_STATE__`,
  JSON-LD, 2 KB or more) are enough as they are.

What happens is counted per URL pattern (`shop.example/search?q`,
`shop.example/p/{int}`, see `url_template()`) and per host. Once 80% of a
pattern's pages or more needed the browser (3 pages at least), its pages go
to the browser directly; one in ten is still tried over HTTP in case the
site changed. A pattern not seen yet follows its host once nine of the
host's pages have been tried. With a file, what was learned is saved at the
end of the crawl and read at the start of the next one, so a later crawl
wastes no HTTP request on the pages that need a browser. At the end of the
crawl, `result.fetch_strategy.describe()` (also logged) says how each
pattern did:

```
shop.example/search?q: HTTP enough for 0% of 3 page(s); browser: 8 page(s), 100% with content
shop.example/p/{int}: HTTP enough for 100% of 120 page(s)
```

The page fetched again in the browser is the same page: it does not count
as a new page for `max_pages`, it is one more of its attempts for
`retries`, and `browser_needed` [events](observability.md) say which pages
went and why. The stats have `adaptive/rendered` (pages fetched again in
the browser) and `adaptive/browser_first` (pages sent to it directly). If no
browser can be started, the crawl carries on over HTTP with a warning.
Tune it with `adaptive_fetch = FetchStrategy("shop.fetch.json",
threshold=0.8, min_pages=3, probe_every=10, capture=True,
wait_until="networkidle")` (from `wintergrab.fetchers.strategy`).

This is about how pages are built, not about access: a page that looks
blocked (`is_blocked()`) is never sent to the browser because of it. It is
retried and slows the crawl down as usual.

## Speed control (AutoThrottle)

Every domain gets its own slot with an allowed concurrency and a delay
between requests. AutoThrottle adjusts both from what the site tells it:

- **Healthy responses**: the delay drifts towards `latency / concurrency`,
  and concurrency grows back by one every 10 successes, up to
  `concurrency_per_domain`.
- **Push-back** (429, 503, a detected block page): concurrency halves and the
  delay doubles (at least 1 s). A `Retry-After` header pauses the domain for
  that long.
- **Timeouts and connection errors**: the delay grows by 50%.
- A robots.txt `Crawl-delay` becomes a floor for the delay.

```python
class Gentle(Spider):
    concurrency = 32               # overall
    concurrency_per_domain = 2     # per site
    download_delay = 1.0           # never faster than one request per second per site
    max_delay = 30                 # cap for back-off

class Fixed(Spider):
    autothrottle = False           # constant speed (Retry-After is still honoured)
```

For full control, pass your own instance:
`throttle = AutoThrottle(target_concurrency=1.5, backoff_factor=3, increase_every=20)`.

## Retries and errors

Failed requests are retried up to `retries` times (default 3) with
exponential backoff. This covers network errors, timeouts, the statuses in
`retry_statuses`, and pages that `is_blocked()` flags. Retries go through
the next proxy in the rotation. Blocked requests also switch to
`fallback_session` if it's set.

When a request finally fails:

1. its `errback(request, error)` runs if it has one;
2. otherwise `spider.on_error(request, error)` runs (the default logs it).

Non-2xx responses don't reach your callback unless their status is in
`allowed_statuses` (e.g. `{404}`). Override `is_blocked(self, response)` for
site-specific block detection.

## Middleware and pipelines

**Downloader middleware** wraps every download. A middleware is any object
with one or more of these methods (plain or `async`):

```python
class Auth:
    def process_request(self, request, spider):           # before the download
        request.headers = {**(request.headers or {}), "Authorization": "Bearer ..."}
        return None            # None: go on; a Response: skip the download; a Request: fetch that instead

    def process_response(self, request, response, spider):  # after it
        if response.status == 401:
            return Request(LOGIN_URL, callback="relogin")     # fetch something else instead
        return response        # possibly changed

    def process_exception(self, request, error, spider):     # when the download failed
        return None            # default handling (retries, errbacks); or recover with a Response/Request

class MySpider(Spider):
    middlewares = [Auth()]
```

Raise `IgnoreRequest` (from `wintergrab.spider.middleware`) to drop a request
silently. Requests pass the middlewares in list order, responses and
exceptions in reverse order. A middleware that raises fails that one request
(`stats["middleware_errors"]`), not the crawl.

**Item pipelines** process every item after `process_item()`:

```python
from wintergrab.spider.middleware import DropItem, ItemPipeline

class Prices(ItemPipeline):
    def open_spider(self, spider): ...          # optional, may be async
    def process_item(self, item, spider):       # may be async
        if not item.get("price"):
            raise DropItem("no price")          # or return None
        item["price"] = float(item["price"].strip("$"))
        return item
    def close_spider(self, spider): ...         # optional

class MySpider(Spider):
    pipelines = [Prices(), lambda item: {**item, "source": "shop"}]   # plain functions work too
```

Pipelines run before de-duplication (`unique_key`) and output. Dropped
items are counted in `stats["items_dropped/<Pipeline>"]`; a pipeline that
raises drops the item and counts `pipeline_errors`.

Ready-made pipelines for cleaning data (typed schemas, validation, filters,
computed fields, near-duplicate removal, quality monitoring) are in
[the data layer](data.md#pipelines-in-crawls); `wintergrab crawl --pipeline
FILE` attaches one from a configuration file.

## Budgets

Stop a crawl before it uses too much. When a budget runs out the crawl
finishes in-flight requests and stops with status `"limit"`;
`result.limit_reason` names the budget. With a `crawl_dir` the queue is
kept: raise the budget and run again to continue.

| Setting | Measures |
|---|---|
| `max_pages`, `max_items` | pages started, items kept |
| `max_requests` | requests sent, retries included |
| `max_bytes` | response bytes downloaded |
| `max_runtime` | seconds of crawling, across resumed runs |
| `max_browser_pages` | pages rendered in a browser |
| `max_errors` | URLs given up on |
| `max_error_rate` | failed / started pages, after `error_rate_min_pages` (50) pages |
| `max_memory`, `max_cpu_seconds` | resident memory (bytes), CPU seconds of this run |
| `max_output_bytes` | bytes written to `output` in this run, checked after every item (SQLite output: the file's growth, checked once a second; Parquet, Excel and PostgreSQL: the items as JSON) |

To degrade gracefully instead, set `budget_soft_limit = 0.9`: once any budget
is 90% used only requests with `priority >= budget_soft_priority` (default 1)
are started, so what is left goes to the pages that matter most.

## Crawl order and priorities

Higher `priority` runs first. Among equal priorities the queue is
breadth-first (`crawl_order = "bfs"`, oldest first) or depth-first
(`crawl_order = "dfs"`, newest first). `priority_fn(request) -> int` sets the
priority of every queued request:

```python
def product_pages_first(request):
    return 10 if "/product/" in request.url else 0

class Shop(Spider):
    priority_fn = product_pages_first
```

## Learning what to crawl

```python
class Shop(Spider):
    optimize = True                   # or "shop.optimizer.json": keep what was learned for the next crawls
```

With `optimize`, a crawl learns from its own pages which URL patterns are
worth fetching (`shop.example/p/{int}`, `shop.example/tag/{word}`). Past a
site's first path segment, more than five different words in one place
count as one pattern. For each pattern it counts the pages fetched, the
items their callbacks yielded (and the pipelines kept), and whether they
led to pages that did. Then it acts in three ways:

- **Promising pages first.** Requests of patterns whose pages yield items
  get priority +20. Those of patterns whose pages lead to such pages
  (listings, categories) get +10.
- **Barren patterns are skipped.** A page is *settled* once the pages it led
  to (two levels down, its own pattern's pages aside) have been fetched. A
  pattern with 20 settled pages, none of which ever gave an item or led to
  one, is skipped. One request in ten is still fetched, in case, and one
  that finds something makes the pattern productive for good. A pattern
  that gave something once is never skipped, even when its later pages
  only lead to items found already. Nothing is skipped before the crawl
  has found an item. Requests queued before their pattern was judged are
  skipped when their turn comes.
- **Parameters that change nothing are dropped.** Sometimes pages that
  differ in one query parameter only (`?ref=nav`, `?ref=footer`, or none)
  have the same text and links. When that happens twice and never
  otherwise, the parameter is dropped from the pattern's later URLs. A page
  already fetched with it is not fetched again. JavaScript app shells, the
  same HTML whatever the URL, teach nothing.

Start requests, sitemaps and `dont_filter` requests are never skipped or
rewritten. The crawl's results report what happened:

- `result.optimizer.describe()` says what was learned about each pattern;
- the stats count `optimizer/skipped` (different URLs),
  `optimizer/duplicates` and `optimizer/rewritten`;
- the progress line shows the items expected from the queue: each pattern's
  queued requests times its items per page.

With `crawl_dir` and `optimize = True`, what was learned is kept in
`crawl_dir/optimizer.json`, for the resumed crawl and the next ones.

The test site's `/shop/` section (`tests/testsite.py`) has 150 products in
five categories. Their links carry a `?ref=` that changes nothing, and a
cloud of 60 tag pages leads only to itself. Crawling it gave:

| Crawl of `/shop/` | Pages fetched | Items |
|---|---:|---:|
| without `optimize` | 376 | 150 |
| with `optimize` | 203 | 150 |
| again, with the file the first crawl saved | 172 | 150 |
| again, `max_items = 50` | 56 | 50 |
| without `optimize`, `max_items = 50` | 92 | 50 |

Where there is nothing to save, it costs some speed. The benchmark site
(`benchmarks/`) has 10,000 pages that all lead to items and no query
strings. There the crawl ran at 985 pages/s instead of 1,033 (−5%), and
every page was still fetched.

It is off by default. Skipping a pattern means missing the records that
only its pages lead to, if its first 20 pages led to none. Patterns come
from URLs: a site whose URLs say nothing of their pages
(`/index.php?id=...` for everything) gives one pattern and nothing to
learn. And a crawl whose items come from every page has nothing to skip.

## Dead letters, events and metrics

Requests given up on are kept in `crawl_dir/dead_letters.jsonl` and can be
retried alone (`retry_dead_letters = True`, `--retry-failed`). Every crawl
emits structured events (`spider.events`), exposes live metrics
(`spider.metrics()`, `result.metrics`) and explains its failures
(`result.failures`, `result.failure_report()`). See
[observability.md](observability.md).

## Pause and resume

Set `crawl_dir` and the crawl becomes resumable:

```python
BooksSpider(crawl_dir=".crawl/books", output="books.jsonl").run()
```

- **Ctrl+C** (or SIGTERM) pauses: the spider stops scheduling, lets in-flight
  requests finish, then saves the queue, the seen-URL set, stats and throttle
  state to `crawl_dir/state.pickle`. A second Ctrl+C forces an immediate stop;
  unfinished requests are saved so they run again next time.
- `spider.pause()` does the same from code (from any thread).
- Running again **resumes**: pending requests continue, seen URLs stay
  filtered, stats accumulate and output files are appended to.
  `run(resume=False)` starts over.
- The state is also checkpointed every `checkpoint_interval` seconds (60) so
  a crash loses little. If the crawl dies with an error, the queue is saved
  as well. Fix the bug and run again.
- Reaching `max_pages` or `max_items` also keeps the unvisited queue, so you
  can raise the limit and run again to continue (limits count across runs).
- When the crawl finishes, the state file is removed and a `summary.json` is
  left behind.

Delivery is "at least once": after a crash (not a clean pause), pages fetched
since the last checkpoint are fetched again. Callbacks must be spider
methods (or their names) so they can be saved. wintergrab checks this early
and tells you if one isn't.

`spider.stop()` ends the crawl gracefully without keeping the queue.

## Change detection

`history = "shop.history"` records every page's fingerprints and items, and
the crawl ends with what changed since the previous run (`result.changes`:
pages added, removed and modified, with price and availability changes).
`skip_fresh = True` then skips the pages that have probably not changed,
judging by how often they changed before. See [history.md](history.md).

## Proxies

```python
class ViaProxies(Spider):
    proxies = ["http://user:pass@p1:8000", "http://p2:8000", "socks5://p3:1080"]
    # or: proxies = ProxyRotator.from_file("proxies.txt", strategy="random", cooldown=300)
```

Requests rotate through the proxies. A proxy that fails several times in a
row is benched for a cooldown, which doubles for repeat offenders. Failures
include connection errors, 403/407/429/502/504 and block pages. Use
`Request(proxy=...)` to pin one request to a proxy. See
[anti-blocking.md](anti-blocking.md#proxies).

## Which URLs get crawled

Three settings decide what a crawl queues and where it may connect:

```python
class Shop(Spider):
    allowed_domains = ["shop.example"]
    url_normalizer = True          # one spelling per page
    url_rules = {"deny": [r"/cart", r"\?sort="], "max_query_params": 5}
    network_policy = "public"      # never reach private or cloud-metadata addresses
```

- `url_normalizer = True` rewrites every queued URL with the default
  `URLNormalizer`: tracking parameters (`utm_*`, `gclid`, `fbclid`...) and
  session ids are dropped, `..` segments resolved, escapes normalized, the
  query sorted and the fragment removed (`#!` routes are kept). Four
  spellings of a page become one request. Site-dependent options
  (`strip_www`, `remove_trailing_slash`, `remove_index`, `lowercase_path`,
  `force_https`) are off by default; pass a dict to turn them on, or any
  `url -> url` function.
- `url_rules` filters discovered links (start URLs are never filtered):
  `allow`/`deny` regexes, `allowed_domains`/`denied_domains`, file extensions
  (images, media, archives and office files by default) and crawler-trap
  guards: overlong URLs, too many query parameters, too-deep paths and
  repeating path segments (`/a/b/a/b/a/b/...`). Each rejection is counted in
  `stats["rules_filtered/<reason>"]`.
- `network_policy` (see [fetching.md](fetching.md#network-policy-ssrf-protection))
  applies to every request, redirect hop and robots.txt fetch. Refused
  requests are counted in `stats["policy_blocked"]` and logged once per host.

`wintergrab.url_template(url)` turns a URL into its route pattern
(`/product/123` -> `/product/{int}`), handy for grouping pages by template.

## robots.txt

`obey_robots_txt = True` (the default) fetches each site's robots.txt once
and skips disallowed URLs (counted in `stats["robots_blocked"]`). A
`Crawl-delay` is honoured. Rules are matched for `robots_user_agent`
(default `"*"`).

## Running spiders

```python
result = MySpider().run()                  # blocking; works in scripts and Jupyter
result = await MySpider().arun()           # inside async code
MySpider(concurrency=4, max_pages=100).run()   # override any setting per instance
```

```bash
wintergrab crawl my_spider.py -o items.jsonl --crawl-dir .crawl/mine -s max_pages=100
```

## Settings reference

| Setting | Default | Meaning |
|---|---|---|
| `name` | class name | Used in logs and checkpoints. |
| `start_urls` | `()` | Where to start. |
| `allowed_domains` | `()` | Only follow links to these domains (and subdomains). A page that redirects off them is dropped (`offsite_redirects` stat). Empty = anywhere. |
| `concurrency` | `16` | Max requests in flight overall. |
| `concurrency_per_domain` | `4` | Max requests in flight per domain. |
| `download_delay` | `0.0` | Minimum delay between requests to one domain (jittered ±50%). |
| `autothrottle` | `True` | Adapt speed per domain (see above). |
| `max_delay` | `60` | Upper bound for back-off delays. |
| `throttle` | `None` | A custom `AutoThrottle` instance. |
| `max_pages` / `max_items` / `max_depth` | `None` | Stop after this many pages / items; don't follow deeper than this. With `max_pages`, retries of pages already started still finish. |
| `max_requests`, `max_bytes`, `max_runtime`, `max_browser_pages`, `max_errors`, `max_error_rate`, `max_memory`, `max_cpu_seconds`, `max_output_bytes` | `None` | [Budgets](#budgets). |
| `budget_soft_limit` / `budget_soft_priority` | `None` / `1` | Past this fraction of a budget, only start requests with at least this priority. |
| `crawl_order` | `"bfs"` | `"bfs"` or `"dfs"` among equal priorities. |
| `priority_fn` | `None` | `request -> int` priority for every queued request. |
| `run_registry` / `record` | `None` / `False` | Keep a record of each run in a workspace (`True`: `.wintergrab`), and with `record` its pages and items too, to [replay](runs.md) it without the network. `result.run_id` names it. |
| `optimize` | `False` | [Learn which URL patterns give items](#learning-what-to-crawl): fetch them first, skip those that give nothing, drop query parameters that change nothing. `True`, or a file that keeps what was learned. |
| `middlewares` / `pipelines` | `()` | [Downloader middleware and item pipelines](#middleware-and-pipelines). |
| `dead_letters` / `retry_dead_letters` | `True` / `False` | Record failed requests in `crawl_dir/dead_letters.jsonl`; queue them again. |
| `event_log` | `None` | Write events as JSON lines (`True` = `crawl_dir/events.jsonl`). |
| `impersonate` | `"chrome"` | Browser fingerprint for the default HTTP session. |
| `default_headers` | `{}` | Headers for the default HTTP session. |
| `timeout` | `30` | Seconds per request. |
| `verify` | `True` | TLS verification (or CA bundle path). |
| `retries` | `3` | Retries per request. |
| `retry_statuses` | 408, 425, 429, 5xx… | Statuses that are retried. |
| `allowed_statuses` | `()` | Non-2xx statuses passed to callbacks anyway. |
| `proxies` | `None` | Proxy list or `ProxyRotator`. |
| `use_browser` | `False` | Headless browser as the default session. |
| `adaptive_fetch` | `False` | [HTTP first, a browser for the pages that need one](#http-first-a-browser-when-needed), learned per URL pattern: `True`, a file to keep what was learned, or a `FetchStrategy`. |
| `render_if_missing` | `()` | With `adaptive_fetch`: CSS selectors a usable page has. |
| `fallback_session` | `None` | Session for retrying blocked requests. |
| `obey_robots_txt` | `True` | Respect robots.txt. |
| `robots_user_agent` | `"*"` | User agent used to match robots.txt rules. |
| `dedupe` | `True` | Filter already-seen URLs. |
| `url_normalizer` | `None` | Rewrite queued URLs to one canonical spelling (`True`, a dict of options, or a function). |
| `url_rules` | `None` | Filter discovered links: patterns, domains, extensions, crawler traps (`True`, a dict, or `URLRules`). |
| `network_policy` | `None` | Where requests may go: `"public"`, `"private"`, a dict, or a `NetworkPolicy`. `None` = anywhere. |
| `resource_filter` | `None` | Browser sessions: also block ads, analytics and trackers. |
| `output` | `None` | Where items go: `.jsonl`, `.json`, `.csv`, `.sqlite`, `.parquet`, `.xlsx`, `postgresql://...` ([storage](storage.md)). |
| `crawl_dir` | `None` | Enables pause/resume. |
| `checkpoint_interval` | `60` | Seconds between automatic checkpoints. |
| `keep_items` | `True` | Keep items in `result.items`. |
| `history` | `None` | A history file (or `PageHistory`): record page fingerprints and items, report changes since the last run in `result.changes`. |
| `history_html` | `False` | Keep each page's HTML in the history too. |
| `skip_fresh` | `False` | With `history`: don't fetch pages that have probably not changed (start URLs always are). |
| `profile` | `False` | Build a [site profile](intelligence.md#site-profiles) and [topology](intelligence.md#site-topology) while crawling (`result.profile`): `True`, a path to save it as JSON, or a `SiteProfiler`. A crawl that finishes reports orphan pages. |
| `log_level` | `"INFO"` | Level for the `wintergrab` logger (`None` = leave logging alone). |
| `log_interval` | `30` | Seconds between progress lines. |

Hooks to override: `start_requests`, `parse`, `configure_sessions`,
`process_item`, `is_blocked`, `on_error`, `on_start`, `on_close`.
