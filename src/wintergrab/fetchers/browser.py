"""Headless-browser fetchers (Playwright) for JavaScript-heavy or guarded sites."""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import platform
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from ..errors import BrowserNotAvailable, FetchError, describe
from ..proxy import ProxyRotator, proxy_for_playwright, proxy_label
from ..request import Request
from ..utils import ensure_scheme, maybe_await
from .blocking import has_challenge_markers
from .http import DEFAULT_RETRY_STATUSES, PROXY_FAILURE_STATUSES
from .response import Headers, Response

if TYPE_CHECKING:
    from ..adaptive.storage import AdaptiveStorage

log = logging.getLogger("wintergrab.browser")

T = TypeVar("T")

DEFAULT_BLOCKED_RESOURCES = ("image", "media", "font")

INSTALL_HINT = (
    "Browser fetching needs Playwright and a Chromium build:\n"
    "    pip install 'wintergrab[browser]'\n"
    "    playwright install chromium\n"
    "Or point WINTERGRAB_BROWSER_PATH at an existing Chrome/Chromium binary."
)

# Evasions for the most common automation tells. They make a headless browser
# look like a normal one to simple checks; they are not a guarantee.
STEALTH_SCRIPT = r"""
(() => {
  const define = (obj, prop, value) => {
    try { Object.defineProperty(obj, prop, { get: () => value, configurable: true }); } catch (e) {}
  };
  define(Navigator.prototype, 'webdriver', undefined);
  define(Navigator.prototype, 'languages', __LANGUAGES__);
  define(Navigator.prototype, 'hardwareConcurrency', 8);
  define(Navigator.prototype, 'deviceMemory', 8);
  if (navigator.plugins && navigator.plugins.length === 0) {
    const fake = [
      { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    ];
    define(Navigator.prototype, 'plugins', Object.assign(fake, { item: i => fake[i], namedItem: n => fake.find(p => p.name === n) }));
  }
  if (!window.chrome) {
    window.chrome = { runtime: {}, app: { isInstalled: false }, csi: () => ({}), loadTimes: () => ({}) };
  }
  if (navigator.permissions && navigator.permissions.query) {
    const original = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (params) =>
      params && params.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission, onchange: null })
        : original(params);
  }
  const patchWebGL = (proto) => {
    if (!proto) return;
    const getParameter = proto.getParameter;
    proto.getParameter = function (p) {
      if (p === 37445) return 'Intel Inc.';
      if (p === 37446) return 'Intel Iris OpenGL Engine';
      return getParameter.call(this, p);
    };
  };
  patchWebGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
  patchWebGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
})();
"""

STEALTH_ARGS = ["--disable-blink-features=AutomationControlled", "--no-default-browser-check", "--no-first-run"]


def _platform_token() -> str:
    system = platform.system()
    if system == "Windows":
        return "Windows NT 10.0; Win64; x64"
    if system == "Darwin":
        return "Macintosh; Intel Mac OS X 10_15_7"
    return "X11; Linux x86_64"


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


class AsyncBrowserFetcher:
    """Fetch pages with a real (headless) Chromium via Playwright.

    Use it for pages that build their content with JavaScript, need clicks or
    scrolling, or reject plain HTTP clients. One browser is shared by all
    requests; each request gets its own tab.

    Args:
        headless: Run without a window.
        stealth: Hide common automation tells (``navigator.webdriver``, the
            ``HeadlessChrome`` user agent, missing plugins...).
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
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        stealth: bool = True,
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
    ) -> None:
        if proxy and proxies:
            raise ValueError("Pass either proxy= or proxies=, not both")
        if user_data_dir and proxies:
            raise ValueError("user_data_dir cannot be combined with rotating proxies")
        self.headless = headless
        self.stealth = stealth
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

        self._pw: Any = None
        self._browser: Any = None
        self._contexts: OrderedDict[str, Any] = OrderedDict()
        self._persistent: Any = None
        self._lock: asyncio.Lock | None = None
        self._sem: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ua: str | None = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Launch the browser (done automatically on the first request)."""
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._lock, self._sem, self._loop = asyncio.Lock(), asyncio.Semaphore(self.max_pages), loop
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
        if self.stealth:
            args.extend(a for a in STEALTH_ARGS if a not in args)
            opts["ignore_default_args"] = ["--enable-automation"]
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
            if self.stealth and self.headless:
                attempts.append({"channel": "chromium"})  # "new" headless: a full Chrome, no HeadlessChrome tells
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
        if self.user_agent:
            return self.user_agent
        if not self.stealth:
            return None
        if self._ua is None and self._browser is not None:
            major = str(self._browser.version).split(".")[0]
            self._ua = (
                f"Mozilla/5.0 ({_platform_token()}) AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{major}.0.0.0 Safari/537.36"
            )
        return self._ua

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
        if self.stealth:
            lang = self.locale.split("-")[0]
            languages = f"['{self.locale}', '{lang}']" if lang != self.locale else f"['{self.locale}']"
            await context.add_init_script(STEALTH_SCRIPT.replace("__LANGUAGES__", languages))
        if self.block_resources:
            blocked = self.block_resources

            async def router(route: Any) -> None:
                if route.request.resource_type in blocked:
                    await route.abort()
                else:
                    await route.continue_()

            await context.route("**/*", router)
        if self.cookies and not isinstance(self.cookies, Mapping):
            await context.add_cookies([dict(c) for c in self.cookies])

    async def _context(self, proxy: str | None) -> Any:
        if self._persistent is not None:
            return self._persistent
        key = proxy or ""
        ctx = self._contexts.get(key)
        if ctx is not None:
            self._contexts.move_to_end(key)
            return ctx
        ctx = await self._browser.new_context(**self._context_options(proxy))
        await self._prepare_context(ctx)
        self._contexts[key] = ctx
        while len(self._contexts) > 16:  # keep the number of open profiles bounded
            _, old = self._contexts.popitem(last=False)
            await old.close()
        return ctx

    async def aclose(self) -> None:
        """Close every tab, context and the browser itself."""
        for ctx in list(self._contexts.values()):
            try:
                await ctx.close()
            except Exception:
                pass
        self._contexts.clear()
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
        screenshot: str | Path | None = None,
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
            screenshot: Save a full-page PNG screenshot here.
        """
        if method.upper() != "GET":
            raise ValueError("Browser fetchers only support GET requests")
        url = ensure_scheme(url)
        req = request or Request(url)
        await self.start()
        assert self._sem is not None
        attempts = 1 + (self.retries if retries is None else max(0, retries))
        last_error: FetchError | None = None
        for attempt in range(attempts):
            chosen = proxy or (self.proxies.next() if self.proxies is not None else None)
            rotated = proxy is None and self.proxies is not None
            try:
                async with self._sem:
                    response = await self._fetch_once(
                        req, chosen, headers, timeout, wait_for, wait, wait_until, scroll, page_action, screenshot
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc if isinstance(exc, FetchError) else self._wrap(exc, url, chosen)
                if rotated and self.proxies is not None:
                    self.proxies.report_failure(chosen)
                if attempt + 1 < attempts and last_error.retryable:
                    await asyncio.sleep(min(10.0, 1.0 * 2**attempt))
                    continue
                raise last_error from exc
            if rotated and self.proxies is not None:
                if response.status in PROXY_FAILURE_STATUSES:
                    self.proxies.report_failure(chosen)
                else:
                    self.proxies.report_success(chosen)
            if response.status in self.retry_statuses and attempt + 1 < attempts:
                await asyncio.sleep(min(10.0, 1.0 * 2**attempt))
                continue
            return response
        assert last_error is not None  # pragma: no cover
        raise last_error  # pragma: no cover

    def _wrap(self, exc: BaseException, url: str, proxy: str | None) -> FetchError:
        text = str(exc)
        is_timeout = "Timeout" in type(exc).__name__ or "timeout" in text.lower()
        is_proxy = "ERR_PROXY" in text or "ERR_TUNNEL" in text
        retryable = "ERR_NAME_NOT_RESOLVED" not in text and "invalid url" not in text.lower()
        where = f" via {proxy_label(proxy)}" if proxy else ""
        return FetchError(
            url,
            f"{describe(exc)}{where}",
            cause=exc,
            proxy=proxy,
            is_proxy_error=is_proxy,
            is_timeout=is_timeout,
            retryable=retryable,
        )

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
        screenshot: str | Path | None,
    ) -> Response:
        timeout_ms = (timeout or self.timeout) * 1000
        context = await self._context(proxy)
        page = await context.new_page()
        started = time.monotonic()
        try:
            if headers:
                await page.set_extra_http_headers(dict(headers))
            if isinstance(self.cookies, Mapping) and self.cookies:
                await context.add_cookies([{"name": k, "value": v, "url": req.url} for k, v in self.cookies.items()])
            last_nav: list[Any] = []

            def on_response(resp: Any) -> None:
                try:
                    if resp.request.is_navigation_request() and resp.frame == page.main_frame:
                        last_nav.append(resp)
                except Exception:  # pragma: no cover - page may be closing
                    pass

            page.on("response", on_response)
            nav = await page.goto(req.url, wait_until=wait_until or self.wait_until, timeout=timeout_ms)
            if self.wait_for_challenge:
                await self._wait_out_challenge(page)
            if wait_for:
                await page.wait_for_selector(wait_for, state="attached", timeout=timeout_ms)
            if scroll:
                await self._scroll(page, 10 if scroll is True else int(scroll))
            if page_action is not None:
                await maybe_await(page_action(page))
            if wait:
                await page.wait_for_timeout(wait * 1000)
            main = last_nav[-1] if last_nav else nav
            status = main.status if main is not None else 200
            raw_headers: dict[str, str] = await main.all_headers() if main is not None else {}
            ctype = raw_headers.get("content-type", "")
            if main is not None and ctype and "html" not in ctype and "xml" not in ctype:
                body = await main.body()
                encoding = None
            else:
                body = (await page.content()).encode("utf-8")
                encoding = "utf-8"
                if ctype:
                    raw_headers["content-type"] = ctype.split(";")[0] + "; charset=utf-8"
            if screenshot:
                Path(screenshot).parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(screenshot), full_page=True)
            cookies = {c["name"]: c["value"] for c in await context.cookies(page.url)}
            history = [r.url for r in last_nav[:-1]]
            return Response(
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
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def _wait_out_challenge(self, page: Any) -> None:
        async def challenged() -> bool:
            try:
                snippet = await page.evaluate(
                    "() => document.title + ' ' + (document.documentElement ? document.documentElement.outerHTML.slice(0, 20000) : '')"
                )
            except Exception:
                return False  # navigating; check again
            return has_challenge_markers(snippet)

        if not await challenged():
            return
        log.info("challenge page detected on %s; waiting for it to clear", page.url)
        deadline = time.monotonic() + self.challenge_timeout
        while time.monotonic() < deadline:
            await page.wait_for_timeout(500)
            if not await challenged():
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

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def stop(self) -> None:
        if self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        self._loop.close()


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

    def close(self) -> None:
        if self._runner is not None:
            try:
                self._runner.run(self._async.aclose())
            finally:
                self._runner.stop()
                self._runner = None

    def __enter__(self) -> BrowserFetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - best effort cleanup
        try:
            if self._runner is not None:
                self.close()
        except Exception:
            pass
