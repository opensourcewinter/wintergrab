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
| `max_response_bytes` | `134217728` (128 MiB) | The largest body read, decompressed. A larger one, whether it says its size or not (a compressed "bomb" too), is abandoned as it arrives with a `FetchError` (`kind="too_large"`) that is not retried. `None`: no limit. |
| `retries` | `2` | Extra attempts after network errors or a retryable status. |
| `backoff`, `max_backoff` | `0.5`, `30` | Exponential backoff between retries (with jitter). `Retry-After` is honoured. |
| `retry_statuses` | 408, 425, 429, 500, 502, 503, 504, 520-524 | Statuses worth retrying. |
| `follow_redirects`, `max_redirects` | `True`, `10` | Redirect handling. |
| `verify` | `True` | TLS verification, or a CA bundle path. `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE` / `SSL_CERT_FILE` are honoured. |
| `http_version` | auto | Force `"1.1"`, `"2"` or `"3"`. |
| `referer` | – | The URL of the page that links to the ones fetched (sent as `Referer`). |
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
    actions=["click .more until-gone"],  # steps done on the page (below)
    page_action=load_more,  # your own automation (must be async)
    screenshot="shot.png",  # full-page screenshot (True: in page.screenshot only)
    layout=True,            # where the page draws its text (see visual.md)
)
```

### Browser actions

Many pages show their data only after something is done on them: a "Load
more" button, a cookie dialog in the way, accordions, a search form, tabs
that replace each other's content. `actions` lists the steps as data. They
are done in order once the page has loaded, and the page is read after the
last one:

```python
page = browser.get("https://shop.example/", actions=[
    "dismiss #accept",                  # a dialog in the way, if there is one
    "click .load-more until-gone",      # "Load more" until there is no more
    "expand summary",                   # every accordion open
    {"fill": {"#q": "parka"}},          # a form
    "press #q => Enter",
    "wait #results",
    "tabs .tabs a",                     # each tab's content, kept
    "download a.export",                # the file the page offers
], downloads="files")
page.actions     # [{'step': 'dismiss #accept', 'ok': True, 'detail': 'clicked'},
                 #  {'step': 'click .load-more until-gone', 'ok': True, 'detail': 'clicked 3 time(s); it is gone'},
                 #  {'step': 'expand summary', 'ok': True, 'detail': 'clicked 2 element(s)'}, ...]
page.snapshots   # [{'after': '.tabs a #1: Specifications', 'html': '...'}, {'after': '.tabs a #2: Reviews', ...}]
page.downloads   # [{'url': '.../report.csv', 'path': 'files/report.csv', 'name': 'report.csv', 'bytes': 47}]
page.console     # [{'type': 'log', 'text': 'shop ready'}, {'type': 'warning', 'text': 'an old API'}]
```

(The page is `tests/data/actions/shop.html` from the test suite.)

A step is a string, `"VERB [SELECTOR] [ARGUMENT]"`, or a mapping with one
verb:

| Step | What it does |
|---|---|
| `click S` | Click the first element matching `S`. `click S x5` clicks up to 5 times while it is there; `click S until-gone` until it is gone (50 times at most). |
| `expand S` | Click every visible element matching `S` once: accordions, "read more" toggles. |
| `dismiss S` | Click `S` if it is there (a cookie dialog, a modal); nothing if not. |
| `hover S` | Move the pointer over `S` (menus that open on hover). |
| `fill S => V` | Type `V` into the field `S`. `{"fill": {S: V, ...}}` fills several. |
| `select S => V` | Choose the option `V` (its value or its label) of the list `S`. |
| `check S`, `uncheck S` | Tick or clear a box. |
| `press K`, `press S => K` | Press a key (`Enter`, `Escape`, `ArrowDown`...), in the field `S` if given. |
| `wait S`, `wait 1.5` | Wait for `S` to show, or that many seconds (120 at most). |
| `scroll N` | Scroll to the bottom, up to `N` times, while the page grows (lazy loading). |
| `tabs S` | Click each element matching `S` in turn, keeping the page's HTML after each in `snapshots`. |
| `snapshot` | Keep the page's HTML as it is now in `snapshots`. |
| `screenshot F`, `pdf F` | Save a full-page PNG, or the page as a PDF (headless Chromium), to the file `F`. |
| `download S` | Click `S` and keep the file it downloads, in the directory `downloads=` names (a temporary one otherwise). |

A mapping can also say `"repeat": N`, `"until_gone": true` or
`"optional": true`: `{"click": ".next", "repeat": 3}`. After a step that
clicks or presses a key, the page gets up to a second for the requests it
started to finish. Each step waits `timeout` seconds at most.

- A step that cannot be done (nothing to click, a wait that runs out) stops
  the page with a `BrowserFetchError` naming the step. It is not retried:
  trying again would not make it possible. `dismiss` steps, and steps marked
  `optional`, are noted in `page.actions` and passed over instead.
- Steps are held to the [network policy](#network-policy-ssrf-protection): a
  click or a download that leads where the policy refuses raises
  `NetworkPolicyError`, naming the step and the address. A step that leads
  to a page that does not load stops the page too, optional or not.
- A download keeps only the file's name, with no directories from the site.
  A file over 200 MB is deleted, and the step fails.
- What a `fill` step types is left out of `page.actions`, logs and errors
  (`fill #password => ***`): a login's password stays out of what is kept.
- No step runs a script: steps do what a person could do on the page. For
  anything else, `page_action` takes your own async function.

The same steps work in a spider, per request:
`Request(url, session="browser", options={"actions": [...]})` (see
[spiders](spiders.md#sessions)). On the command line, `wintergrab get URL
--do STEP` (repeatable) or `--actions steps.yaml` (a JSON or YAML list), and
`--downloads DIR`; it prints what each step did, and after the page's
Markdown, each kept snapshot's:

```bash
wintergrab get https://shop.example/ --do "click .load-more until-gone" --do "tabs .tabs a"
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
| `actions`, `snapshots`, `downloads` | What [browser actions](#browser-actions) did, the HTML they kept, the files they downloaded. |
| `console` | A browser page's console messages and uncaught script errors: `{"type", "text"}` (200 at most). |
| `screenshot`, `layout`, `pdf` | A browser fetch's PNG, where its text is drawn ([visual](visual.md)); a PDF's pages, when the body is one. |

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
| `BrowserFetchError` | The page failed in the browser (crash, navigation error), or a [browser action](#browser-actions) could not be done. |
| `CacheMiss` | Offline cache mode and the page isn't cached. |
| `HTTPStatusError` (alias `HTTPError`) | Raised by `raise_for_status()` (or with `raise_for_status=True`). A 404 or 500 is **not** an exception by default. |
| `BrowserNotAvailable` | Playwright or a Chromium binary is missing. The message tells you what to install. |

Other categories: `ConfigurationError`, `SchemaError`, `ParserError` (e.g.
`SelectorSyntaxError`), `ExtractionError`, `ValidationError`,
`StorageError` (`CheckpointError`, `ExportError`) and `BudgetExceeded`.
Errors pickle cleanly, so they survive being sent between processes.
