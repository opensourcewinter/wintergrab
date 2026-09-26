# Responsible access

WINTERGRAB reads sites the way their owners allow. It follows robots.txt,
slows down when a site pushes back, and does not try to get past a block, a
bot check, a rate limit or a login it was not given. This page says what it
does when a site says no, and what you can tune.

## robots.txt

Spiders obey robots.txt by default (`obey_robots_txt = True`), matched for
`robots_user_agent`, and wait the `Crawl-delay` it asks for. A page it
forbids is not fetched: the request ends with a `RobotsPolicyError`, and
the failure report counts it.

## Slowing down

A site that answers 429 or 503, or with a block page, is asking for fewer
requests. Spiders slow down for the whole domain
([AutoThrottle](spiders.md#speed-control-autothrottle)):

- the domain's concurrency halves and its delay doubles;
- a `Retry-After` header pauses the domain for that long;
- speed comes back gradually once the answers are healthy again.

The one-off fetchers (`wg.get`, `Fetcher`) wait between attempts too
(`retries`, `backoff`), and honour `Retry-After`.

If a site keeps pushing back, lower `concurrency_per_domain` (1 or 2 is
gentle) or set `download_delay`.

## Blocked pages

`Spider.is_blocked(response)` decides whether an answer is a refusal. The
default (`wintergrab.fetchers.looks_blocked`) flags:

- every 429;
- 403 and 503 answers that are empty or carry a bot check's markers
  (Cloudflare, DDoS-Guard, PerimeterX, Incapsula, DataDome, Fastly...);
- small 200 pages that are plainly bot checks, such as Fastly's "Client
  Challenge".

A blocked page slows its domain down (above). It is asked for again after
the pause, at most `retries` times, the same way: the same session, through
the same proxy. Then it is given up on and reported: the `blocked` count, a
`blocked` event, and the failure report, which gives a likely cause rather
than a certain one:

```
HTTP 403 on shop.example
  affected URLs: 1 (2 failed attempts, 1 given up)
  previous success: none on this domain
  likely cause: bot protection or a web application firewall (challenge page served)
  evidence: challenge-page markers in the body
  crawler state: backing off (delay 2.0s, concurrency 1/4)
```

A blocked page is never fetched again in a browser, or through another
proxy, because it was blocked.
[Adaptive fetching](spiders.md#http-first-a-browser-when-needed) uses a
browser for pages whose HTML lacks their content, never for a blocked one.

Override `is_blocked` when a site says no differently:

```python
def is_blocked(self, response):
    return super().is_blocked(response) or "unusual traffic" in response.text
```

The marker scan costs about 5% of a crawl's CPU on fast local sites. On a
site you know never serves bot checks, check the status only:

```python
def is_blocked(self, response):
    return response.status == 429
```

## Browsers

Some pages build their content with JavaScript: `wg.render(url)`, a
spider's `use_browser = True`, or
[adaptive fetching](spiders.md#http-first-a-browser-when-needed). The
browser is Playwright's Chromium as it is. Nothing hides that it is
automated: `navigator.webdriver` is true, and nothing on the page is
patched to pass for a person.

A "checking your browser" page that clears itself is waited for
(`wait_for_challenge=True`, up to `challenge_timeout` seconds): the site's
own check decides. If it does not let the browser through, the page is
blocked like any other. WINTERGRAB does not solve CAPTCHAs.

## Access you were given

Logins and keys that are yours to use work as they should:

- cookies and headers: `cookies={...}`, `headers={"Authorization": ...}`;
  `--cookie` and `-H` on the command line. In a project file, secrets come
  from environment variables (`${NAME}`), and run records leave them out;
- a browser profile that keeps a login: `user_data_dir="profile/"`;
- signing in with the site's own form, with
  [browser actions](fetching.md#browser-actions) or your own
  `page_action`. What a `fill` step types is left out of `response.actions`,
  logs and errors (`fill #password => ***`). Spiders carry the browser's
  cookies over to their HTTP sessions (`share_browser_cookies = True`), so
  the rest of the crawl goes at HTTP speed:

```python
import os

class Members(Spider):
    def configure_sessions(self, sessions):
        super().configure_sessions(sessions)
        sessions.add("browser", AsyncBrowserFetcher())

    def start_requests(self):
        sign_in = [{"fill": {"#user": "ada", "#password": os.environ["CLUB_PASSWORD"]}}, "click #sign-in", "wait .account"]
        yield Request("https://club.example/login", session="browser", options={"actions": sign_in})

    def parse(self, response):
        ...  # the pages it follows go over HTTP, signed in
```

## Proxies

`proxies=[...]` or a `ProxyRotator` sends requests through proxies you may
use: a company's gateway, a network in another region.

```python
rotator = ProxyRotator(["http://user:pass@p1.example:8000", "p2.example:8000", "socks5://p3.example:1080"],
                       strategy="round_robin")   # or "random", "least_used"
wg.get(url, proxies=rotator)
class MySpider(Spider): proxies = rotator
rotator.stats()      # uses, successes and failures per proxy (passwords hidden)
```

A proxy is benched for a while after repeated failures of its own: a
connection that fails, 407 (the proxy refused us), 502 and 504 (it could
not reach the site). A site's 403, 429 or block page is the site's answer,
not the proxy's failure: it is not held against the proxy, the domain slows
down instead, and the page is asked for again through the same proxy.

## Browser-compatible requests

The HTTP fetchers send a browser's TLS and HTTP/2 settings and its default
headers (curl_cffi's `impersonate="chrome"`, or `"firefox"`, `"safari"`,
`"edge"`...), as many servers expect of their clients. `impersonate=None`
sends plain curl requests.

## Saying who you are

On a large crawl, tell the site who is crawling and how to reach you:

```python
class Catalog(Spider):
    default_headers = {"From": "crawler@yourcompany.example"}
```

## Etiquette

- Respect the site's terms, not only robots.txt.
- Keep request rates modest. AutoThrottle helps; a small
  `concurrency_per_domain` helps more.
- Don't collect personal data you have no right to process.

## What changed

Earlier versions had features built to get past a site's refusal. They are
gone, and using one is an error that says so, not a setting silently
ignored:

| Before | Now |
|---|---|
| `fallback_session = "browser"` fetched a blocked page again in a browser. | Setting it raises `ConfigurationError`. The page is reported, and its domain slowed down. For pages that need JavaScript, `adaptive_fetch = True`. |
| `stealth=True`, the default, patched the browser to hide that it is automated. | The browser is not patched, and `stealth` is no longer an option. |
| `referer="google"` or `"bing"` made requests look like clicks from search results. | `referer` takes the URL of the page that links to the one fetched. |
| A 403, 429 or block page benched the proxy it came through, and the next attempt went through another. | Only a proxy's own failures bench it. The retry goes through the same proxy. |
