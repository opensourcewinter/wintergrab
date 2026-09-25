# Fetching pages

wintergrab has two kinds of fetchers, each in a sync and an async flavour:

| | Sync | Async | Use for |
|---|---|---|---|
| HTTP | `Fetcher` / `wg.get()` | `AsyncFetcher` / `wg.aget()` | Most pages. Fast, looks like a real browser. |
| Browser | `BrowserFetcher` / `wg.render()` | `AsyncBrowserFetcher` / `wg.arender()` | JavaScript pages, clicking/scrolling, stubborn bot checks. |

All of them return the same [`Response`](#the-response-object).

## One-off requests

```python
import wintergrab as wg

page = wg.get("https://example.com")
page = wg.get("example.com/search", params={"q": "mugs"})         # https:// is added
page = wg.post("https://httpbin.org/post", data={"a": 1})         # form body
page = wg.post("https://httpbin.org/post", json={"a": 1})         # JSON body
page = wg.get(url, headers={"Accept-Language": "de"}, cookies={"session": "..."})
page = wg.get(url, proxy="http://user:pass@proxy:8000", timeout=10)
```

`wg.get` accepts every `Fetcher` option (below) plus per-request options. It
opens and closes a session each time. For several pages, use a `Fetcher`.

## `Fetcher`: a reusable HTTP session

```python
with wg.Fetcher(impersonate="chrome", retries=3) as fetcher:
    login = fetcher.post("https://example.com/login", data={"user": "me", "pw": "..."})
    page = fetcher.get("https://example.com/account")     # cookies are kept
```

| Option | Default | What it does |
|---|---|---|
| `impersonate` | `"chrome"` | Browser whose TLS/HTTP2 fingerprint and default headers to copy: `"chrome"`, `"firefox"`, `"safari"`, `"edge"`, a version such as `"chrome131"`, or `None` for plain curl. |
| `headers`, `cookies` | – | Sent with every request. |
| `proxy` | – | One proxy for every request. |
| `proxies` | – | A list of proxies or a `ProxyRotator` to rotate through. |
| `timeout` | `30` | Seconds before giving up on a request. |
| `retries` | `2` | Extra attempts after network errors or a retryable status. |
| `backoff`, `max_backoff` | `0.5`, `30` | Exponential backoff between retries (with jitter). `Retry-After` is honoured. |
| `retry_statuses` | 408, 425, 429, 500, 502, 503, 504, 520-524 | Statuses worth retrying. |
| `follow_redirects`, `max_redirects` | `True`, `10` | Redirect handling. |
| `verify` | `True` | TLS verification, or a CA bundle path. `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE` / `SSL_CERT_FILE` are honoured. |
| `http_version` | auto | Force `"1.1"`, `"2"` or `"3"`. |
| `referer` | – | A URL, or `"google"` / `"bing"` to look like a click from search results. |
| `raise_for_status` | `False` | Raise `HTTPStatusError` for 4xx/5xx instead of returning them. |
| `adaptive_storage` | SQLite in cache dir | Where [adaptive selectors](adaptive-selectors.md) store fingerprints. |

Per-request keyword arguments: `params`, `headers`, `cookies`, `data`, `json`,
`proxy`, `timeout`, `retries`, `allow_redirects`. Anything else is passed to
curl_cffi unchanged.

## `AsyncFetcher`: many pages concurrently

```python
import asyncio
import wintergrab as wg

async def main():
    async with wg.AsyncFetcher(max_connections=64) as fetcher:
        page = await fetcher.get("https://example.com")

        # Keep input order. Failures come back as FetchError objects, not exceptions.
        pages = await fetcher.get_many(urls, concurrency=10)

        # Or handle each page as soon as it arrives:
        async for page in fetcher.iter_many(urls, concurrency=10):
            ...

asyncio.run(main())
```

For crawls where you discover new URLs as you go, use a
[Spider](spiders.md). It adds scheduling, politeness, retries and resume on
top of this.

## Browser fetching

Install once: `pip install "wintergrab[browser]" && playwright install chromium`.

```python
page = wg.render("https://quotes.toscrape.com/js/", wait_for=".quote")

with wg.BrowserFetcher(headless=True) as browser:
    page = browser.get(url, wait_for="#results", scroll=True)
    page = browser.get(url, wait=2.5, screenshot="page.png")
```

The browser is started on the first request and shared by all requests. Each
request opens a new tab. `BrowserFetcher` also works inside Jupyter, because it
runs its event loop in a background thread.

### Browser options

| Option | Default | What it does |
|---|---|---|
| `headless` | `True` | Hide the window. `False` is handy for debugging. |
| `stealth` | `True` | Hide common automation tells: `navigator.webdriver`, the `HeadlessChrome` user agent, empty plugin list, WebGL vendor, … |
| `block_resources` | image, media, font | Resource types not to download. Faster and lighter. Pass `()` to load everything. |
| `resource_filter` | – | Also block ads, analytics and trackers: `True` for the built-in lists, or a dict of `ResourceFilter` options (`lists`, `block_domains`, `allow_domains`, `block_third_party`, `block_patterns`...). `ResourceFilter.load_list(path)` reads hosts files and `\|\|domain^` blocklists. `response.blocked_resources` counts what was blocked. |
| `network_policy` | – | Refuse requests to forbidden destinations, e.g. `"public"` (see [below](#network-policy-ssrf-protection)). |
| `wait_until` | `"load"` | `"domcontentloaded"`, `"load"`, `"networkidle"` or `"commit"`. |
| `timeout` | `30` | Navigation timeout in seconds. |
| `max_pages` | `4` | Tabs open at once. |
| `proxy` / `proxies` | – | One proxy, or several to rotate (a browser context is kept per proxy). |
| `user_agent`, `locale`, `timezone_id`, `viewport` | sensible | Browser identity. |
| `extra_headers`, `cookies` | – | Sent with every page. |
| `wait_for_challenge` | `True` | If a "checking your browser" page appears, wait up to `challenge_timeout` (20 s) for it to clear by itself. |
| `user_data_dir` | – | A persistent profile, so logins survive restarts. |
| `executable_path` / `channel` | auto | Which Chrome/Chromium to drive (e.g. `channel="chrome"`). `$WINTERGRAB_BROWSER_PATH` works too. |

### Per-page options

```python
async def load_more(page):             # receives Playwright's async Page
    await page.click("text=Load more")
    await page.wait_for_timeout(500)

page = browser.get(
    url,
    wait_for=".item",       # CSS selector to wait for
    wait=1.0,               # extra seconds after loading
    scroll=True,            # scroll to the bottom (repeatedly, for infinite scroll)
    page_action=load_more,  # your own automation (must be async)
    screenshot="shot.png",  # full-page screenshot
)
```

## The `Response` object

| Attribute / method | |
|---|---|
| `status`, `ok`, `reason` | Status code; `ok` is `True` for < 400. |
| `url`, `history` | Final URL and the redirects that led there. |
| `headers` | Case-insensitive; `headers.get_list("set-cookie")` for repeats. |
| `cookies` | Cookies set by the response. |
| `body` / `content`, `text`, `encoding`, `json()` | Raw bytes and decoded text (charset from headers, BOM or `<meta>`). |
| `css()`, `xpath()`, `select()`, `extract()`, `extract_all()` | Query the page (see [parsing.md](parsing.md)). |
| `find_by_text()`, `find_by_regex()`, `links()`, `title` | Search helpers. |
| `get_text()`, `markdown(main_content=False)` | Readable text / Markdown of the page. |
| `urljoin(href)`, `follow(link)`, `follow_all(css)` | URL helpers; `follow` builds spider `Request`s. |
| `raise_for_status()`, `save(path)` | Errors on 4xx/5xx; write the body to a file. |
| `request`, `meta`, `elapsed`, `source` | The request that made it, spider metadata, seconds taken, `"http"`/`"browser"`. |
| `ip` | Address of the server that answered (`None` from the cache). |
| `blocked_resources` | Browser sub-requests blocked while rendering, by reason (`"type:image"`, `"list"`, `"policy"`...). |

## Network policy (SSRF protection)

A crawler follows links that strangers wrote. A page can link or redirect to
`http://169.254.169.254/latest/meta-data/` (a cloud machine's credentials),
`http://localhost:6379` or `http://10.0.0.5/admin`. When you crawl sites you
don't control from a machine that can reach internal services, turn on a
network policy:

```python
page = wg.get(url, network_policy="public")              # public internet only
Fetcher(network_policy=NetworkPolicy(allowed_networks=["10.1.2.0/24"]))
BrowserFetcher(network_policy="public")
```

With `"public"`, requests to private (RFC 1918, carrier-grade NAT, unique
local IPv6), loopback, link-local (cloud metadata), multicast and reserved
addresses raise `NetworkPolicyError`, before any connection is made. Host
names are resolved and every address they resolve to must be allowed, so
tricks like `http://2130706433/` or a public name pointing at `127.0.0.1` do
not get through. Every redirect hop is checked, and so is the address curl
actually connected to (which defeats DNS rebinding).

`NetworkPolicy` options: `allow_private`, `allow_loopback`,
`allow_link_local`, `allow_reserved`, `allowed_networks`, `denied_networks`,
`allowed_hosts`, `denied_hosts`, `allowed_ports`, `schemes`. `"private"` is a
shortcut for "private networks and loopback too".

Limits: through a proxy, the proxy resolves and connects, so only what can be
resolved locally is checked. In a browser every request the page makes is
checked, but redirect hops of the page itself can only be checked after the
fact (the page is then discarded), and Chromium's own DNS lookups cannot be
pinned. For strong isolation, also restrict the machine's outbound network.

## Errors

Every error is a `wintergrab.WintergrabError` with a `category` (a stable
name such as `"network"`, `"timeout"`, `"policy"`, `"http"`) and a `context`
dict (URL, domain, field...). Fetch failures are `FetchError`s with `.url`,
`.cause`, `.proxy`, `.kind`, `.is_timeout`, `.is_proxy_error` and
`.retryable`; the subclasses say what went wrong:

| Error | When |
|---|---|
| `NetworkError` | Connection refused/reset, DNS (`kind="dns"`), TLS (`kind="tls"`), too many redirects, invalid URL. |
| `ProxyError` | The proxy failed (a `NetworkError`). |
| `FetchTimeout` | The request timed out. |
| `NetworkPolicyError`, `RobotsPolicyError` | A policy refused the request (`PolicyError`s: never retried). |
| `BrowserFetchError` | The page failed in the browser (crash, navigation error). |
| `CacheMiss` | Offline cache mode and the page isn't cached. |
| `HTTPStatusError` (alias `HTTPError`) | Raised by `raise_for_status()` (or with `raise_for_status=True`). A 404 or 500 is **not** an exception by default. |
| `BrowserNotAvailable` | Playwright or a Chromium binary is missing. The message tells you what to install. |

Other categories: `ConfigurationError`, `SchemaError`, `ParserError` (e.g.
`SelectorSyntaxError`), `ExtractionError`, `ValidationError`,
`StorageError` (`CheckpointError`, `ExportError`) and `BudgetExceeded`.
Errors pickle cleanly, so they survive being sent between processes.
