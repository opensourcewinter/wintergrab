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
QuotesSpider(output="quotes.jsonl").run()   # .jsonl, .json or .csv, streamed as items arrive
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
| `max_output_bytes` | bytes written to `output` in this run, checked after every item (SQLite output: the file's growth, checked once a second) |

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
| `fallback_session` | `None` | Session for retrying blocked requests. |
| `obey_robots_txt` | `True` | Respect robots.txt. |
| `robots_user_agent` | `"*"` | User agent used to match robots.txt rules. |
| `dedupe` | `True` | Filter already-seen URLs. |
| `url_normalizer` | `None` | Rewrite queued URLs to one canonical spelling (`True`, a dict of options, or a function). |
| `url_rules` | `None` | Filter discovered links: patterns, domains, extensions, crawler traps (`True`, a dict, or `URLRules`). |
| `network_policy` | `None` | Where requests may go: `"public"`, `"private"`, a dict, or a `NetworkPolicy`. `None` = anywhere. |
| `resource_filter` | `None` | Browser sessions: also block ads, analytics and trackers. |
| `output` | `None` | Stream items to `.jsonl` / `.json` / `.csv`. |
| `crawl_dir` | `None` | Enables pause/resume. |
| `checkpoint_interval` | `60` | Seconds between automatic checkpoints. |
| `keep_items` | `True` | Keep items in `result.items`. |
| `log_level` | `"INFO"` | Level for the `wintergrab` logger (`None` = leave logging alone). |
| `log_interval` | `30` | Seconds between progress lines. |

Hooks to override: `start_requests`, `parse`, `configure_sessions`,
`process_item`, `is_blocked`, `on_error`, `on_start`, `on_close`.
