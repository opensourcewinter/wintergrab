"""Headless-browser fetchers (Playwright) for JavaScript-heavy or guarded sites."""

from __future__ import annotations

import asyncio
import atexit
import fnmatch
import glob
import json as _json
import logging
import os
import sys
import threading
import time
import weakref
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import urljoin

from ..errors import (
    BrowserFetchError,
    BrowserNotAvailable,
    FetchError,
    FetchTimeout,
    NetworkError,
    NetworkPolicyError,
    ProxyError,
    describe,
)
from ..netpolicy import NetworkPolicy
from ..parser.layout import LAYOUT_SCRIPT, MAX_BOXES, Layout
from ..proxy import ProxyRotator, proxy_for_playwright, proxy_label
from ..request import Request
from ..utils import ensure_scheme, maybe_await
from .actions import Action, parse_actions, run_actions
from .blocking import has_challenge_markers
from .cache import CacheLayer, HTTPCache
from .http import DEFAULT_RETRY_STATUSES, PROXY_FAILURE_STATUSES
from .resources import DEFAULT_BLOCKED_RESOURCES, ResourceFilter
from .response import Headers, Response

if TYPE_CHECKING:
    from ..adaptive.storage import AdaptiveStorage

log = logging.getLogger("wintergrab.browser")

T = TypeVar("T")

MAX_IDLE_CONTEXTS = 16
# Chromium net errors that mean "could not connect" (worth retrying).
_CONNECTION_ERRORS = (
    "ERR_CONNECTION_REFUSED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_ADDRESS_UNREACHABLE",
    "ERR_INTERNET_DISCONNECTED",
    "ERR_NETWORK_CHANGED",
    "ERR_EMPTY_RESPONSE",
)  # browser contexts (one per proxy) kept open when idle

INSTALL_HINT = (
    "Browser fetching needs Playwright and a Chromium build:\n"
    "    pip install 'wintergrab[browser]'\n"
    "    playwright install chromium\n"
    "Or point WINTERGRAB_BROWSER_PATH at an existing Chrome/Chromium binary."
)


def _discover_chromium() -> list[str]:
    """Chromium/Chrome binaries Playwright (or the system) may already have."""
    found: list[str] = []
    env = os.environ.get("WINTERGRAB_BROWSER_PATH")
    if env:
        found.append(env)
    roots = [
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH"),
        str(Path.home() / ".cache" / "ms-playwright"),
        str(Path.home() / "Library" / "Caches" / "ms-playwright"),
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright") if os.environ.get("LOCALAPPDATA") else None,
    ]
    patterns = [
        "chromium-*/chrome-linux*/chrome",
        "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
        "chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chromium-*/chrome-win*/chrome.exe",
        "chromium_headless_shell-*/chrome-*/chrome-headless-shell",
        "chromium_headless_shell-*/chrome-*/headless_shell",
    ]
    for root in filter(None, roots):
        for pattern in patterns:
            found.extend(sorted(glob.glob(os.path.join(root, pattern)), reverse=True))
    for path in (
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ):
        found.append(path)
    return [p for p in dict.fromkeys(found) if p and os.path.isfile(p) and os.access(p, os.X_OK)]


@dataclass
class CapturedResponse:
    """An XHR/fetch response recorded while a page rendered (see ``capture=``)."""

    url: str
    method: str
    status: int
    headers: dict[str, str]
    body: bytes
    resource_type: str = "fetch"
    request_body: str | None = None
    order: int = 0

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return _json.loads(self.body)

    def __repr__(self) -> str:
        return f"<CapturedResponse {self.method} {self.status} {self.url}>"


def _capture_matcher(capture: bool | str | Callable[[str], bool] | None) -> Callable[[str, str], bool] | None:
    """Turn the ``capture=`` option into ``matcher(url, content_type) -> bool``."""
    if not capture:
        return None
    if capture is True:
        return lambda url, ctype: "json" in ctype.lower()
    if isinstance(capture, str):
        pattern = capture
        if any(ch in pattern for ch in "*?["):
            return lambda url, ctype: fnmatch.fnmatch(url, pattern)
        return lambda url, ctype: pattern in url
    func = capture
    return lambda url, ctype: bool(func(url))


_CONSOLE_KEPT = 200  # console messages kept per page


class _PageError:
    """An uncaught error in the page's scripts, kept with its console messages."""

    type = "pageerror"

    def __init__(self, text: str) -> None:
        self.text = text


class AsyncBrowserFetcher:
    """Fetch pages with a real (headless) Chromium via Playwright.

    Use it for pages that build their content with JavaScript, or need clicks
    or scrolling. One browser is shared by all requests; each request gets its
    own tab. The browser is Playwright's Chromium as it is: nothing hides that
    it is automated (see docs/responsible-access.md).

    Args:
        headless: Run without a window.
        executable_path: Chrome/Chromium binary to use (also read from
            ``$WINTERGRAB_BROWSER_PATH``).
        channel: Playwright browser channel, e.g. ``"chrome"`` to drive an
            installed Google Chrome.
        proxy: One proxy for the whole browser.
        proxies: Proxies to rotate (a browser context is kept per proxy).
        user_agent: Override the user agent.
        locale: Browser locale (also sets ``Accept-Language``).
        timezone_id: e.g. ``"Europe/Berlin"``.
        viewport: Window size, ``(width, height)``.
        extra_headers: Headers added to every request.
        cookies: Cookies to preload, as ``{name: value}`` (sent to every site
            you open) or Playwright cookie dicts.
        block_resources: Resource types not to download (``"image"``,
            ``"media"``, ``"font"``, ``"stylesheet"``...). Saves bandwidth.
        resource_filter: Also block ads, analytics and trackers: ``True`` for
            the built-in lists, a dict of :class:`~wintergrab.fetchers.resources.ResourceFilter`
            options, or a ``ResourceFilter``. ``response.blocked_resources``
            counts what was blocked, by reason.
        network_policy: Refuse requests to forbidden destinations (SSRF
            protection), e.g. ``"public"``. Every request the page makes is
            checked; redirect hops of the page itself are checked after the
            fact (Playwright cannot intercept them), and a page reached
            through a forbidden hop is discarded.
        timeout: Navigation timeout in seconds.
        wait_until: ``"load"``, ``"domcontentloaded"``, ``"networkidle"`` or ``"commit"``.
        max_pages: How many tabs may be open at once.
        retries: Extra attempts after errors / retryable statuses.
        wait_for_challenge: If a "checking your browser" interstitial shows up,
            wait (up to ``challenge_timeout`` seconds) for it to clear itself.
        user_data_dir: Keep a persistent browser profile (logins survive
            restarts). Incompatible with ``proxies``.
        launch_args: Extra Chromium command-line flags.
        adaptive_storage: Storage for adaptive selectors on fetched pages.
        cache: Cache rendered pages (``True``, a path or an
            :class:`~wintergrab.HTTPCache`); handy with ``cache_mode="prefer"``
            to render each page only once while developing.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        executable_path: str | None = None,
        channel: str | None = None,
        proxy: str | None = None,
        proxies: ProxyRotator | Sequence[str] | None = None,
        user_agent: str | None = None,
        locale: str = "en-US",
        timezone_id: str | None = None,
        viewport: tuple[int, int] = (1366, 768),
        extra_headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | Sequence[Mapping[str, Any]] | None = None,
        block_resources: Iterable[str] = DEFAULT_BLOCKED_RESOURCES,
        timeout: float = 30.0,
        wait_until: str = "load",
        max_pages: int = 4,
        retries: int = 1,
        retry_statuses: Iterable[int] = DEFAULT_RETRY_STATUSES,
        wait_for_challenge: bool = True,
        challenge_timeout: float = 20.0,
        user_data_dir: str | None = None,
        launch_args: Sequence[str] | None = None,
        adaptive_storage: AdaptiveStorage | None = None,
        cache: HTTPCache | str | bool | None = None,
        cache_mode: str | None = None,
        cache_ttl: float | None = None,
        resource_filter: ResourceFilter | Mapping[str, Any] | bool | None = None,
        network_policy: NetworkPolicy | str | bool | None = None,
    ) -> None:
        if proxy and proxies:
            raise ValueError("Pass either proxy= or proxies=, not both")
        if user_data_dir and proxies:
            raise ValueError("user_data_dir cannot be combined with rotating proxies")
        self.headless = headless
        self.executable_path = executable_path
        self.channel = channel
        self.proxy = proxy
        self.proxies = ProxyRotator.coerce(proxies)
        self.user_agent = user_agent
        self.locale = locale
        self.timezone_id = timezone_id
        self.viewport = {"width": viewport[0], "height": viewport[1]}
        self.extra_headers = dict(extra_headers or {})
        self.cookies = cookies
        self.block_resources = frozenset(block_resources or ())
        self.resource_filter = ResourceFilter.coerce(resource_filter, block_types=self.block_resources)
        self.network_policy = NetworkPolicy.coerce(network_policy)
        self.timeout = timeout
        self.wait_until = wait_until
        self.max_pages = max(1, max_pages)
        self.retries = max(0, retries)
        self.retry_statuses = frozenset(retry_statuses)
        self.wait_for_challenge = wait_for_challenge
        self.challenge_timeout = challenge_timeout
        self.user_data_dir = user_data_dir
        self.launch_args = list(launch_args or [])
        self.adaptive_storage = adaptive_storage
        self.cache = HTTPCache.coerce(cache, mode=cache_mode, ttl=cache_ttl)
        self._cache_layer = CacheLayer(self.cache, "browser:", "browser") if self.cache is not None else None

        self._pw: Any = None
        self._browser: Any = None
        self._contexts: OrderedDict[str, Any] = OrderedDict()
        self._context_users: dict[str, int] = {}
        self._context_lock: asyncio.Lock | None = None
        self._persistent: Any = None
        self._lock: asyncio.Lock | None = None
        self._sem: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Launch the browser (done automatically on the first request)."""
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._lock, self._sem, self._loop = asyncio.Lock(), asyncio.Semaphore(self.max_pages), loop
            self._context_lock = asyncio.Lock()
        assert self._lock is not None
        async with self._lock:
            if self._browser is not None or self._persistent is not None:
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                raise BrowserNotAvailable(INSTALL_HINT) from None
            self._pw = await async_playwright().start()
            try:
                await self._launch()
            except BaseException:
                await self._pw.stop()
                self._pw = None
                raise

    def _launch_options(self) -> dict[str, Any]:
        args = list(self.launch_args)
        opts: dict[str, Any] = {"headless": self.headless, "args": args}
        if self.proxy:
            opts["proxy"] = proxy_for_playwright(self.proxy)
        return opts

    async def _launch(self) -> None:
        chromium = self._pw.chromium
        base = self._launch_options()
        attempts: list[dict[str, Any]] = []
        if self.executable_path:
            attempts.append({"executable_path": self.executable_path})
        elif self.channel:
            attempts.append({"channel": self.channel})
        else:
            env_path = os.environ.get("WINTERGRAB_BROWSER_PATH")
            if env_path:
                attempts.append({"executable_path": env_path})
            attempts.append({})
            attempts.extend({"executable_path": p} for p in _discover_chromium())
        errors: list[str] = []
        for extra in attempts:
            opts = {**base, **extra}
            try:
                if self.user_data_dir:
                    self._persistent = await chromium.launch_persistent_context(
                        self.user_data_dir, **opts, **self._context_options(None)
                    )
                    await self._prepare_context(self._persistent)
                else:
                    self._browser = await chromium.launch(**opts)
                log.debug("launched browser with %s", extra or "playwright defaults")
                return
            except Exception as exc:
                errors.append(f"{extra or 'default'}: {describe(exc)}")
        raise BrowserNotAvailable(INSTALL_HINT + "\n\nLaunch attempts:\n  " + "\n  ".join(errors))

    def _user_agent(self) -> str | None:
        return self.user_agent or None

    def _context_options(self, proxy: str | None) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "locale": self.locale,
            "viewport": self.viewport,
            "extra_http_headers": {
                "Accept-Language": f"{self.locale},{self.locale.split('-')[0]};q=0.9",
                **self.extra_headers,
            },
        }
        ua = self._user_agent()
        if ua:
            opts["user_agent"] = ua
        if self.timezone_id:
            opts["timezone_id"] = self.timezone_id
        if proxy:
            opts["proxy"] = proxy_for_playwright(proxy)
        return opts

    async def _prepare_context(self, context: Any) -> None:
        if self.cookies and not isinstance(self.cookies, Mapping):
            await context.add_cookies([dict(c) for c in self.cookies])

    async def _acquire_context(self, proxy: str | None) -> tuple[str | None, Any]:
        """The browser context for ``proxy`` (created once, even under concurrency)."""
        if self._persistent is not None:
            return None, self._persistent
        assert self._context_lock is not None
        key = proxy or ""
        async with self._context_lock:
            ctx = self._contexts.get(key)
            if ctx is None:
                ctx = await self._browser.new_context(**self._context_options(proxy))
                await self._prepare_context(ctx)
                self._contexts[key] = ctx
            else:
                self._contexts.move_to_end(key)
            self._context_users[key] = self._context_users.get(key, 0) + 1
            # Keep the number of open profiles bounded, closing only idle ones.
            excess = len(self._contexts) - MAX_IDLE_CONTEXTS
            for old_key in list(self._contexts):
                if excess <= 0:
                    break
                if self._context_users.get(old_key, 0) == 0:
                    old = self._contexts.pop(old_key)
                    self._context_users.pop(old_key, None)
                    excess -= 1
                    try:
                        await old.close()
                    except Exception:
                        pass
        return key, ctx

    def _release_context(self, key: str | None) -> None:
        if key is not None and key in self._context_users:
            self._context_users[key] = max(0, self._context_users[key] - 1)

    async def export_cookies(self, url: str | None = None, *, proxy: str | None = None) -> list[dict[str, Any]]:
        """Cookies of the browser session (optionally only those sent to ``url``).

        Feed them to :meth:`Fetcher.add_cookies` to continue a browser session
        (a login, a solved consent wall...) with fast HTTP requests.
        """
        await self.start()
        if self._persistent is not None:
            context = self._persistent
        else:
            context = self._contexts.get(proxy or "")
            if context is None:
                return []
        return [dict(c) for c in await (context.cookies(url) if url else context.cookies())]

    async def aclose(self) -> None:
        """Close every tab, context and the browser itself."""
        for ctx in list(self._contexts.values()):
            try:
                await ctx.close()
            except Exception:
                pass
        self._contexts.clear()
        self._context_users.clear()
        for obj in (self._persistent, self._browser):
            if obj is not None:
                try:
                    await obj.close()
                except Exception:
                    pass
        self._persistent = self._browser = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None

    close = aclose

    async def __aenter__(self) -> AsyncBrowserFetcher:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ #
    # fetching
    # ------------------------------------------------------------------ #
    async def get(self, url: str, **kwargs: Any) -> Response:
        return await self.request("GET", url, **kwargs)

    async def request(
        self,
        method: str,
        url: str,
        *,
        proxy: str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        retries: int | None = None,
        wait_for: str | None = None,
        wait: float = 0.0,
        wait_until: str | None = None,
        scroll: bool | int = False,
        page_action: Callable[[Any], Any] | None = None,
        screenshot: str | Path | bool | None = None,
        capture: bool | str | Callable[[str], bool] | None = None,
        layout: bool = False,
        actions: Any = None,
        downloads: str | Path | None = None,
        request: Request | None = None,
        **_ignored: Any,
    ) -> Response:
        """Open ``url`` in a new tab and return the rendered page.

        Args:
            wait_for: CSS selector to wait for before capturing the page.
            wait: Extra seconds to wait after loading.
            wait_until: Override the fetcher's ``wait_until``.
            scroll: Scroll to the bottom (repeatedly, for infinite scroll).
                ``True`` scrolls up to 10 times; an int sets the limit.
            page_action: ``async def action(page)`` that receives Playwright's
                async ``Page`` to click, type, etc. before capture.
            screenshot: Take a full-page PNG screenshot: ``True`` keeps it in ``response.screenshot``
                (for a model that reads images); a path also saves it there.
            layout: Record where the page's text was drawn in ``response.layout`` (a
                :class:`~wintergrab.parser.layout.Layout`), for reading tables and labelled values
                from what the page looks like (:mod:`wintergrab.extraction.visual`).
            actions: What to do on the page before it is read: steps such as ``"click .more until-gone"``,
                ``{"fill": {"#q": "parka"}}``, ``"tabs .tabs a"`` (see :mod:`wintergrab.fetchers.actions`).
                What each did is in ``response.actions``; ``response.snapshots`` and
                ``response.downloads`` hold what they kept.
            downloads: The directory ``download`` steps save files in (a temporary one otherwise).
            capture: Record the page's own API calls (XHR/fetch) in
                ``response.captured``: ``True`` for JSON responses, a URL glob or
                substring (``"*/api/*"``, ``"graphql"``), or a function of the URL.
                Often the cleanest way to scrape a JavaScript site: take the data
                the page itself downloads.
        """
        if method.upper() != "GET":
            raise ValueError("Browser fetchers only support GET requests")
        url = ensure_scheme(url)
        req = request or Request(url)
        layer = self._cache_layer
        offline = layer is not None and layer.cache.mode == "offline"
        # Captures, screenshots and layouts need a live page, so they bypass the cache -
        # except offline, where the network is never used.
        steps = parse_actions(actions) if actions else []
        use_cache = layer is not None and (offline or (not capture and not screenshot and not layout and not steps))
        if use_cache:
            assert layer is not None
            cached, _, _ = layer.before(req, self.adaptive_storage)
            if cached is not None:
                if capture or screenshot or layout or steps:
                    log.warning("offline: %s served from cache without captures, screenshot, layout or actions", url)
                return cached
        if self.network_policy is not None:
            await self.network_policy.check(url, proxied=self._proxied(proxy))
        await self.start()
        assert self._sem is not None
        attempts = 1 + (self.retries if retries is None else max(0, retries))
        last_error: FetchError | None = None
        chosen, rotated, switch = proxy, False, True
        for attempt in range(attempts):
            if switch:  # the first attempt, or the last one's proxy failed: pick one
                chosen = proxy or (self.proxies.next() if self.proxies is not None else None)
                rotated = proxy is None and self.proxies is not None
            elif rotated and self.proxies is not None and chosen is not None:
                self.proxies.reuse(chosen)
            try:
                async with self._sem:
                    response = await self._fetch_once(
                        req,
                        chosen,
                        headers,
                        timeout,
                        wait_for,
                        wait,
                        wait_until,
                        scroll,
                        page_action,
                        screenshot,
                        _capture_matcher(capture),
                        layout,
                        steps,
                        downloads,
                    )
            except (asyncio.CancelledError, NetworkPolicyError):
                raise
            except Exception as exc:
                last_error = exc if isinstance(exc, FetchError) else self._wrap(exc, url, chosen)
                if rotated and self.proxies is not None:
                    self.proxies.report_failure(chosen)
                if attempt + 1 < attempts and last_error.retryable:
                    switch = True
                    await asyncio.sleep(min(10.0, 1.0 * 2**attempt))
                    continue
                raise last_error from exc
            if rotated and self.proxies is not None:
                if response.status in PROXY_FAILURE_STATUSES:
                    self.proxies.report_failure(chosen)
                else:
                    self.proxies.report_success(chosen)
            if response.status in self.retry_statuses and attempt + 1 < attempts:
                # The site's own answer (429, 503...) is asked for again the same way, through the same proxy.
                switch = response.status in PROXY_FAILURE_STATUSES
                await asyncio.sleep(min(10.0, 1.0 * 2**attempt))
                continue
            if use_cache and self._cache_layer is not None:
                response = self._cache_layer.after(req, response, None, self.adaptive_storage)
            return response
        assert last_error is not None  # pragma: no cover
        raise last_error  # pragma: no cover

    def _proxied(self, proxy: str | None) -> bool:
        return bool(proxy or self.proxy or self.proxies)

    def _wrap(self, exc: BaseException, url: str, proxy: str | None) -> FetchError:
        """Turn a Playwright/Chromium error into the matching :class:`FetchError` subclass."""
        text = str(exc)
        message = f"{describe(exc)}{f' via {proxy_label(proxy)}' if proxy else ''}"
        common: dict[str, Any] = {"cause": exc, "proxy": proxy}
        if "Timeout" in type(exc).__name__ or "timeout" in text.lower():
            return FetchTimeout(url, message, **common)
        if "ERR_PROXY" in text or "ERR_TUNNEL" in text:
            return ProxyError(url, message, **common)
        if "ERR_NAME_NOT_RESOLVED" in text:
            return NetworkError(url, message, kind="dns", retryable=False, **common)
        if "ERR_CERT" in text:
            return NetworkError(url, message, kind="tls", retryable=False, **common)
        if "ERR_SSL" in text:
            return NetworkError(url, message, kind="tls", **common)
        if "ERR_TOO_MANY_REDIRECTS" in text:
            return NetworkError(url, message, kind="redirects", retryable=False, **common)
        if "invalid url" in text.lower() or "ERR_INVALID_URL" in text:
            return NetworkError(url, message, kind="invalid_url", retryable=False, **common)
        if any(code in text for code in _CONNECTION_ERRORS):
            return NetworkError(url, message, kind="connect", **common)
        return BrowserFetchError(url, message, **common)

    def _router(
        self, page: Any, blocked: Counter[str], proxied: bool, refused: list[str] | None = None
    ) -> Callable[[Any], Awaitable[None]]:
        """A route handler applying the resource filter and the network policy to every request of ``page``
        (the addresses the policy refuses go in ``refused``, the last 20)."""
        resource_filter, policy = self.resource_filter, self.network_policy

        async def handle(route: Any) -> None:
            request = route.request
            url = request.url
            try:
                main = request.is_navigation_request() and request.frame == page.main_frame
            except Exception:  # pragma: no cover - frame detached
                main = False
            reason: str | None = None
            if policy is not None and url.startswith(("http:", "https:")):
                try:
                    await policy.check(url, proxied=proxied)
                except NetworkPolicyError:
                    reason = "policy"
                    if refused is not None:
                        refused[:] = [*refused[-19:], url]
                except FetchError:
                    pass  # e.g. a name that does not resolve: let the browser report it
            if reason is None and resource_filter is not None:
                reason = resource_filter.reason(url, request.resource_type, page.url, main_document=main)
                if reason is not None:
                    resource_filter.stats[reason] += 1
            try:
                if reason is None:
                    await route.continue_()
                else:
                    blocked[reason] += 1
                    await route.abort("blockedbyclient")
            except Exception:  # pragma: no cover - the page is closing
                pass

        return handle

    async def _check_navigation(self, hops: list[str], main: Any, proxied: bool) -> str | None:
        """Check the page's redirect hops (Playwright's router only sees the first) and the address
        the page came from. Raises :class:`NetworkPolicyError`; returns the server IP if known."""
        policy = self.network_policy
        for hop in dict.fromkeys(hops):
            if hop.startswith(("http:", "https:")):
                await policy.check(hop, proxied=proxied)  # type: ignore[union-attr]
        address = None
        if main is not None:
            try:
                server = await main.server_addr()
            except Exception:  # pragma: no cover - not available for every response
                server = None
            address = (server or {}).get("ipAddress") or None
        if policy is not None and hops:
            policy.check_connected(hops[-1], address, proxied=proxied)
        return address

    async def _fetch_once(
        self,
        req: Request,
        proxy: str | None,
        headers: Mapping[str, str] | None,
        timeout: float | None,
        wait_for: str | None,
        wait: float,
        wait_until: str | None,
        scroll: bool | int,
        page_action: Callable[[Any], Any] | None,
        screenshot: str | Path | bool | None,
        capture: Callable[[str, str], bool] | None = None,
        layout: bool = False,
        steps: Sequence[Action] = (),
        downloads: str | Path | None = None,
    ) -> Response:
        timeout_ms = (timeout or self.timeout) * 1000
        context_key, context = await self._acquire_context(proxy)
        try:
            page = await context.new_page()
        except BaseException:
            self._release_context(context_key)
            raise
        started = time.monotonic()
        blocked: Counter[str] = Counter()
        refused: list[str] = []
        proxied = self._proxied(proxy)
        try:
            if self.resource_filter or self.network_policy is not None:
                await page.route("**/*", self._router(page, blocked, proxied, refused))
            if headers:
                await page.set_extra_http_headers(dict(headers))
            if isinstance(self.cookies, Mapping) and self.cookies:
                await context.add_cookies([{"name": k, "value": v, "url": req.url} for k, v in self.cookies.items()])
            last_nav: list[Any] = []
            captured: list[CapturedResponse] = []
            grabbing: set[asyncio.Future[None]] = set()

            async def grab(resp: Any, order: int) -> None:
                try:
                    body = await resp.body()
                except Exception:  # redirects and aborted requests have no body
                    body = b""
                request = resp.request
                try:
                    post_data = request.post_data
                except Exception:  # binary bodies are not valid text
                    buffer = request.post_data_buffer
                    post_data = buffer.decode("latin-1") if buffer else None
                captured.append(
                    CapturedResponse(
                        url=resp.url,
                        method=request.method,
                        status=resp.status,
                        headers=dict(resp.headers),
                        body=body,
                        resource_type=request.resource_type,
                        request_body=post_data,
                        order=order,
                    )
                )

            def on_response(resp: Any) -> None:
                try:
                    if resp.request.is_navigation_request() and resp.frame == page.main_frame:
                        last_nav.append(resp)
                    elif (
                        capture is not None
                        and resp.request.resource_type in ("xhr", "fetch")
                        and capture(resp.url, resp.headers.get("content-type", ""))
                    ):
                        grabbing.add(asyncio.ensure_future(grab(resp, len(grabbing))))
                except Exception:  # pragma: no cover - page may be closing
                    pass

            page.on("response", on_response)
            console: list[dict[str, str]] = []

            def on_console(message: Any) -> None:
                if len(console) < _CONSOLE_KEPT:
                    try:
                        console.append({"type": message.type, "text": message.text[:2000]})
                    except Exception:  # pragma: no cover - page may be closing
                        pass

            page.on("console", on_console)
            page.on("pageerror", lambda error: on_console(_PageError(str(error))))
            try:
                nav = await page.goto(req.url, wait_until=wait_until or self.wait_until, timeout=timeout_ms)
            except Exception as exc:
                if "Download is starting" not in str(exc):
                    raise
                # a file the browser downloads rather than shows (an attachment, a PDF with no viewer):
                # the file is the answer, as over HTTP
                return await self._download(context, req, timeout_ms, proxied, started)
            if self.wait_for_challenge:
                await self._wait_out_challenge(page)
            if wait_for:
                await page.wait_for_selector(wait_for, state="attached", timeout=timeout_ms)
            if scroll:
                await self._scroll(page, 10 if scroll is True else int(scroll))
            if page_action is not None:
                await maybe_await(page_action(page))
            done = None
            if steps:
                refused.clear()
                try:
                    done = await run_actions(page, steps, timeout=timeout or self.timeout, downloads=downloads)
                except BrowserFetchError as exc:
                    if refused and page.url.startswith("chrome-error:"):  # a step led where the policy refuses
                        raise NetworkPolicyError(
                            req.url,
                            f"browser action {exc.context.get('action')!r} led to {refused[-1]}, which the"
                            " network policy refuses",
                            context={"action": exc.context.get("action"), "refused": refused[-1]},
                        ) from None
                    raise
            if wait:
                await page.wait_for_timeout(wait * 1000)
            if grabbing:
                await asyncio.wait(grabbing, timeout=10)
            main = last_nav[-1] if last_nav else nav
            address = None
            if self.network_policy is not None:
                address = await self._check_navigation([r.url for r in last_nav] + [page.url], main, proxied)
            status = main.status if main is not None else 200
            raw_headers: dict[str, str] = await main.all_headers() if main is not None else {}
            ctype = raw_headers.get("content-type", "")
            if main is not None and ctype and "html" not in ctype and "xml" not in ctype:
                body = await main.body()
                encoding = None
                if ctype.split(";")[0].strip().lower() == "application/pdf" and not body.lstrip().startswith(b"%PDF-"):
                    # The browser shows a PDF in its viewer, and hands back the viewer's page: ask for the file
                    # itself, with the browser's cookies (and no redirect: the address was checked already).
                    direct = await context.request.get(main.url, max_redirects=0, timeout=timeout_ms)
                    if direct.ok:
                        body = await direct.body()
            else:
                body = (await page.content()).encode("utf-8")
                encoding = "utf-8"
                if ctype:
                    raw_headers["content-type"] = ctype.split(";")[0] + "; charset=utf-8"
            png = None
            if screenshot:
                png = await page.screenshot(full_page=True)
                if not isinstance(screenshot, bool):
                    Path(screenshot).parent.mkdir(parents=True, exist_ok=True)
                    Path(screenshot).write_bytes(png)
            drawn = None
            if layout and encoding is not None:  # an HTML page, not a PDF or an image
                drawn = Layout.from_dict(await page.evaluate(LAYOUT_SCRIPT, MAX_BOXES))
            jar = await context.cookies(page.url)
            cookies = {c["name"]: c["value"] for c in jar}
            history = [r.url for r in last_nav[:-1]]
            response = Response(
                page.url,
                status=status,
                headers=Headers(raw_headers),
                body=body,
                request=req,
                reason=(main.status_text if main is not None else "") or "",
                encoding=encoding,
                cookies=cookies,
                elapsed=time.monotonic() - started,
                history=history,
                source="browser",
                adaptive_storage=self.adaptive_storage,
            )
            response.captured = sorted(captured, key=lambda c: c.order)
            response.screenshot = png
            response.layout = drawn
            response.console = console
            if done is not None:
                response.actions, response.snapshots, response.downloads = done.log, done.snapshots, done.downloads
            response.cookie_jar = [dict(c) for c in jar]
            response.blocked_resources = dict(blocked)
            response.ip = address
            return response
        finally:
            try:
                await page.close()
            except Exception:
                pass
            self._release_context(context_key)

    async def _download(self, context: Any, req: Request, timeout_ms: float, proxied: bool, started: float) -> Response:
        """The file at ``req.url``, asked for directly with the browser's cookies, following redirects itself so
        that each one is checked like the page's own (the network policy, 10 at most)."""
        url, history = req.url, []
        for _ in range(10):
            if self.network_policy is not None:
                await self.network_policy.check(url, proxied=proxied)
            answer = await context.request.get(url, max_redirects=0, timeout=timeout_ms)
            location = answer.headers.get("location")
            if 300 <= answer.status < 400 and location:
                history.append(url)
                url = urljoin(url, location)
                continue
            jar = await context.cookies(url)
            response = Response(
                url,
                status=answer.status,
                headers=Headers(dict(answer.headers)),
                body=await answer.body(),
                request=req,
                reason=answer.status_text or "",
                cookies={c["name"]: c["value"] for c in jar},
                elapsed=time.monotonic() - started,
                history=history,
                source="browser",
                adaptive_storage=self.adaptive_storage,
            )
            response.cookie_jar = [dict(c) for c in jar]
            return response
        raise NetworkError(req.url, "too many redirects", kind="redirects", retryable=False)

    async def _wait_out_challenge(self, page: Any) -> None:
        async def challenged(seen: bool) -> bool:
            """True while a challenge shows; once one was ``seen``, also while its replacement arrives."""
            try:
                ready, snippet = await page.evaluate(
                    "() => [document.readyState, document.title + ' ' +"
                    " (document.documentElement ? document.documentElement.outerHTML.slice(0, 20000) : '')]"
                )
            except Exception:
                return seen  # navigating away from the check: look again
            # A check that passes may reload the page or write the real one into
            # the document (document.write keeps readyState at "loading" until
            # it is closed); either way, wait until the new page is fully parsed.
            return (seen and ready == "loading") or has_challenge_markers(snippet)

        if not await challenged(seen=False):
            return
        log.info("challenge page detected on %s; waiting for it to clear", page.url)
        deadline = time.monotonic() + self.challenge_timeout
        while time.monotonic() < deadline:
            await page.wait_for_timeout(500)
            if not await challenged(seen=True):
                try:
                    await page.wait_for_load_state("load", timeout=10_000)
                except Exception:
                    pass
                return
        log.warning("challenge on %s did not clear within %.0fs", page.url, self.challenge_timeout)

    @staticmethod
    async def _scroll(page: Any, times: int) -> None:
        last = -1
        for _ in range(max(1, times)):
            height = await page.evaluate("() => document.body ? document.body.scrollHeight : 0")
            if height == last:
                break
            last = height
            await page.evaluate("() => window.scrollTo(0, document.body ? document.body.scrollHeight : 0)")
            await page.wait_for_timeout(600)

    async def get_many(
        self, urls: Iterable[str], *, concurrency: int | None = None, return_exceptions: bool = True, **kwargs: Any
    ) -> list[Response | FetchError]:
        """Render many pages concurrently (bounded by ``max_pages``)."""
        sem = asyncio.Semaphore(concurrency or self.max_pages)

        async def one(u: str) -> Response | FetchError:
            async with sem:
                try:
                    return await self.get(u, **kwargs)
                except FetchError as exc:
                    if return_exceptions:
                        return exc
                    raise

        return list(await asyncio.gather(*(one(u) for u in urls)))


class _LoopThread:
    """An event loop running in a background thread, for sync wrappers."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="wintergrab-browser", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except BaseException:
            future.cancel()
            raise

    def stop(self) -> None:
        if self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        self._loop.close()


_OPEN_BROWSERS: weakref.WeakSet[BrowserFetcher] = weakref.WeakSet()


@atexit.register
def _close_open_browsers() -> None:
    """Close browsers left open at exit (their loop thread is still alive here)."""
    for browser in list(_OPEN_BROWSERS):
        try:
            browser.close(timeout=10)
        except Exception:  # pragma: no cover - best effort
            pass


class BrowserFetcher:
    """Synchronous wrapper around :class:`AsyncBrowserFetcher`.

    Works in plain scripts *and* inside Jupyter (the browser runs on its own
    background event loop). Accepts the same options::

        with BrowserFetcher(headless=True) as browser:
            page = browser.get("https://example.com", wait_for=".results")

    ``page_action`` callbacks must be ``async def`` functions, since they
    receive Playwright's async ``Page``.
    """

    def __init__(self, **kwargs: Any) -> None:
        self._async = AsyncBrowserFetcher(**kwargs)
        self._runner: _LoopThread | None = None

    def _run(self, coro_factory: Callable[[], Awaitable[T]]) -> T:
        if self._runner is None:
            self._runner = _LoopThread()
            _OPEN_BROWSERS.add(self)

        async def wrapper() -> T:
            return await coro_factory()

        return self._runner.run(wrapper())

    def start(self) -> None:
        self._run(self._async.start)

    def get(self, url: str, **kwargs: Any) -> Response:
        """Render ``url``. See :meth:`AsyncBrowserFetcher.request` for options."""
        return self._run(lambda: self._async.get(url, **kwargs))

    def get_many(self, urls: Iterable[str], **kwargs: Any) -> list[Response | FetchError]:
        urls = list(urls)
        return self._run(lambda: self._async.get_many(urls, **kwargs))

    def export_cookies(self, url: str | None = None, *, proxy: str | None = None) -> list[dict[str, Any]]:
        """Cookies of the browser session - see :meth:`AsyncBrowserFetcher.export_cookies`."""
        return self._run(lambda: self._async.export_cookies(url, proxy=proxy))

    def close(self, timeout: float | None = 30) -> None:
        """Close the browser and its background thread."""
        runner, self._runner = self._runner, None
        _OPEN_BROWSERS.discard(self)
        if runner is not None:
            try:
                runner.run(self._async.aclose(), timeout=timeout)
            except Exception as exc:
                log.debug("error while closing the browser: %s", describe(exc))
            finally:
                runner.stop()

    def __enter__(self) -> BrowserFetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - best effort cleanup
        # During interpreter shutdown the loop thread is frozen; waiting on it
        # would hang forever (the atexit hook has already closed us anyway).
        if getattr(self, "_runner", None) is None or sys.is_finalizing():
            return
        try:
            self.close(timeout=10)
        except Exception:
            pass
