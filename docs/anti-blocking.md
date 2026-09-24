# Dealing with tough sites

Many sites defend against bots. Some defences are simple (blocking obvious
HTTP clients), others are elaborate (JavaScript challenges, IP reputation,
rate limits). Here's what wintergrab does about each, and what you can
tune. Start at the top and only go further down when you need to.

## 1. Look like a browser, not a script

Libraries like `requests` are easy to spot. Their TLS handshake and HTTP/2
settings look nothing like a browser's, even with a fake `User-Agent`.
wintergrab's HTTP fetchers use curl_cffi to copy a real browser's TLS
(JA3/JA4) and HTTP/2 fingerprints, along with its default headers and
header order:

```python
wg.get(url)                           # Chrome (the default)
wg.get(url, impersonate="firefox")    # or "safari", "edge", "chrome131", "safari18_0"...
wg.get(url, referer="google")         # arrive "from" a search results page
```

Keep the rest of the request consistent. If you override `User-Agent`, pick
one that matches the impersonated browser.

## 2. Slow down and back off

Most blocks are really rate limits. Spiders handle this for you with
[AutoThrottle](spiders.md#speed-control-autothrottle):

- 429/503 responses and block pages halve the per-domain concurrency and
  double the delay;
- `Retry-After` is honoured (the domain is paused for that long);
- speed recovers gradually once responses are healthy again;
- robots.txt `Crawl-delay` is respected.

For one-off fetchers, `retries`, `backoff` and `retry_statuses` do the same
for single requests.

If you still get throttled, lower `concurrency_per_domain` (1-2 is very
polite) or set `download_delay`.

## 3. Render JavaScript with a real browser

Some pages only produce content after JavaScript runs. Others serve a
JavaScript challenge first. Use the browser fetcher:

```python
page = wg.render(url, wait_for=".results")
```

With `stealth=True` (the default) the browser hides the usual automation
tells:

- `navigator.webdriver` is removed;
- the user agent no longer says `HeadlessChrome`;
- plugins, languages and the WebGL vendor look like a desktop browser's;
- Chrome's `--enable-automation` switch and the `AutomationControlled`
  blink feature are disabled.

It also waits for "checking your browser…" interstitials that clear
themselves (`wait_for_challenge=True`, up to `challenge_timeout` seconds).
wintergrab does **not** solve CAPTCHAs.

In a spider you don't have to choose up front. Use HTTP for speed and let
blocked pages escalate:

```python
class Shop(Spider):
    fallback_session = "browser"     # retry blocked pages in a headless browser
```

Tips:

- `headless=False` shows the window, which helps with debugging and with
  sites that detect headless mode.
- `channel="chrome"` drives your installed Google Chrome instead of the
  bundled Chromium.
- `user_data_dir="profile/"` keeps cookies and logins between runs.
- `block_resources` (images, media and fonts by default) makes pages load
  faster. Pass `()` if a site breaks without them.

## 4. Rotate IPs with proxies

When the problem is your IP address (datacenter ranges, too many requests
from one address), use proxies:

```python
from wintergrab import ProxyRotator

rotator = ProxyRotator(
    ["http://user:pass@p1.example:8000", "p2.example:8000", "socks5://p3.example:1080",
     "p4.example:8000:user:pass"],        # host:port:user:pass is understood too
    strategy="round_robin",               # or "random", "least_used"
    max_failures=3,                       # consecutive failures before benching
    cooldown=120,                         # seconds on the bench (doubles for repeat offenders)
)
rotator = ProxyRotator.from_file("proxies.txt")

wg.get(url, proxies=rotator)                         # one-off
wg.Fetcher(proxies=rotator)                          # session
wg.BrowserFetcher(proxies=rotator)                   # browser: one context per proxy
class MySpider(Spider): proxies = rotator            # spider
rotator.stats()                                      # uses / successes / failures per proxy
```

A proxy counts as failing on connection errors, 403/407/429/502/504 and
detected block pages. Retries automatically move to the next proxy.

## 5. Sessions and identities

Some sites tie rate limits to cookies or accounts. Spiders can spread work
across several sessions, each with its own cookies (and optionally its own
browser fingerprint):

```python
def configure_sessions(self, sessions):
    for i, cookie in enumerate(ACCOUNT_COOKIES):
        sessions.add(f"acct{i}", AsyncFetcher(cookies={"session": cookie}), default=(i == 0))
```

Then spread requests with `Request(url, session=f"acct{n % len(ACCOUNT_COOKIES)}")`.

## 6. Detecting blocks

`Spider.is_blocked(response)` decides whether a response is a block page.
The default (`wintergrab.fetchers.looks_blocked`) flags:

- every 429;
- 403/503 responses that are empty or contain common challenge markers
  (Cloudflare, DDoS-Guard, PerimeterX, Incapsula, DataDome, Fastly…);
- small 200 pages that are clearly challenge pages, such as Fastly's
  "Client Challenge", which is served with status 200.

Override it when a site signals blocks differently, e.g. a 200 page that
says "unusual traffic":

```python
def is_blocked(self, response):
    return super().is_blocked(response) or "unusual traffic" in response.text
```

The marker scan costs about 5% of a crawl's CPU on fast local sites. On a
site you know never serves challenge pages, you can check only the status:

```python
def is_blocked(self, response):
    return response.status == 429
```

## Etiquette

These tools help legitimate automation get through filters that can't tell it
apart from abuse. Keep it legitimate:

- Respect robots.txt (the default in spiders) and the site's terms.
- Keep request rates modest. AutoThrottle helps, but a small
  `concurrency_per_domain` helps more.
- Identify yourself with a contact header on large crawls where appropriate:
  `default_headers = {"From": "you@example.com"}`.
- Don't collect personal data you have no right to process.
