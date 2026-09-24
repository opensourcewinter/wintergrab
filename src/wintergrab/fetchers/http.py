"""Fast HTTP fetchers built on curl_cffi (real browser TLS/HTTP2 fingerprints)."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from curl_cffi import requests as curl_requests
from curl_cffi.requests import exceptions as curl_exc

from ..errors import FetchError, describe
from ..proxy import ProxyRotator, proxy_label
from ..request import Request
from ..utils import ensure_scheme, resolve_verify
from .cache import CacheLayer, HTTPCache
from .response import Headers, Response

if TYPE_CHECKING:
    from ..adaptive.storage import AdaptiveStorage

log = logging.getLogger("wintergrab.fetch")

DEFAULT_RETRY_STATUSES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524})
# Statuses that count against the proxy that returned them: blocked/throttled
# IPs (403, 429), proxy auth failures (407) and gateway errors (502, 504).
PROXY_FAILURE_STATUSES: frozenset[int] = frozenset({403, 407, 429, 502, 504})

REFERERS = {"google": "https://www.google.com/", "bing": "https://www.bing.com/"}

_HTTP_VERSIONS = {1: "HTTP/1.0", 2: "HTTP/1.1", 3: "HTTP/2", 30: "HTTP/3"}

# Per-request options that only make sense for browser fetchers.
BROWSER_ONLY_OPTIONS = frozenset({"wait_for", "wait", "wait_until", "scroll", "page_action", "screenshot"})


class _HTTPBase:
    """Settings and helpers shared by :class:`Fetcher` and :class:`AsyncFetcher`."""

    def __init__(
        self,
        *,
        impersonate: str | None = "chrome",
        headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
        proxy: str | None = None,
        proxies: ProxyRotator | Sequence[str] | None = None,
        timeout: float = 30.0,
        retries: int = 2,
        backoff: float = 0.5,
        max_backoff: float = 30.0,
        retry_statuses: Iterable[int] = DEFAULT_RETRY_STATUSES,
        follow_redirects: bool = True,
        max_redirects: int = 10,
        verify: bool | str = True,
        http_version: str | None = None,
        referer: str | None = None,
        raise_for_status: bool = False,
        adaptive_storage: AdaptiveStorage | None = None,
        cache: HTTPCache | str | bool | None = None,
        cache_mode: str | None = None,
        cache_ttl: float | None = None,
    ) -> None:
        """
        Args:
            impersonate: Browser whose TLS/HTTP2 fingerprint and default headers
                to mimic: ``"chrome"`` (default), ``"firefox"``, ``"safari"``,
                ``"edge"``, a specific version like ``"chrome131"``, or ``None``
                for plain curl.
            headers: Headers sent with every request.
            cookies: Cookies sent with every request.
            proxy: One proxy for every request.
            proxies: Several proxies to rotate through (list or
                :class:`~wintergrab.ProxyRotator`).
            timeout: Seconds before a request is abandoned.
            retries: Extra attempts after network errors / retryable statuses.
            backoff: Base delay (seconds) for exponential backoff between retries.
            max_backoff: Upper bound for a single backoff delay.
            retry_statuses: HTTP statuses worth retrying.
            follow_redirects: Follow 3xx redirects.
            max_redirects: Redirect limit.
            verify: Verify TLS certificates (or a CA bundle path).
            http_version: Force ``"1.1"``, ``"2"`` or ``"3"``.
            referer: ``Referer`` sent with requests - a URL, or ``"google"``
                / ``"bing"`` to look like a click from search results.
            raise_for_status: Raise :class:`~wintergrab.errors.HTTPStatusError` on 4xx/5xx.
            adaptive_storage: Where adaptive selectors on fetched pages keep
                their data (defaults to a SQLite file in your cache dir).
            cache: Cache responses on disk: ``True`` (``./.wintergrab-cache``),
                a path, or an :class:`~wintergrab.HTTPCache`.
            cache_mode: ``"revalidate"`` (default), ``"prefer"``, ``"offline"``
                or ``"refresh"`` - see :class:`~wintergrab.HTTPCache`.
            cache_ttl: Seconds a cached response counts as fresh.
        """
        if proxy and proxies:
            raise ValueError("Pass either proxy= or proxies=, not both")
        self.impersonate = impersonate
        self.headers = dict(headers or {})
        self.cookies = dict(cookies or {})
        self.proxy = proxy
        self.proxies = ProxyRotator.coerce(proxies)
        self.timeout = timeout
        self.retries = max(0, retries)
        self.backoff = backoff
        self.max_backoff = max_backoff
        self.retry_statuses = frozenset(retry_statuses)
        self.follow_redirects = follow_redirects
        self.max_redirects = max_redirects
        self.verify = resolve_verify(verify)
        self.http_version = {"1.1": "v1", "1": "v1", "2": "v2", "3": "v3"}.get(str(http_version))
        self.referer = REFERERS.get(referer, referer) if referer else None
        self.raise_for_status = raise_for_status
        self.adaptive_storage = adaptive_storage
        self.cache = HTTPCache.coerce(cache, mode=cache_mode, ttl=cache_ttl)
        self._cache_layer = CacheLayer(self.cache) if self.cache is not None else None

    # -- helpers ---------------------------------------------------------- #
    def _session_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "headers": self.headers,
            "cookies": self.cookies,
            "verify": self.verify,
            "timeout": self.timeout,
            "max_redirects": self.max_redirects,
        }
        if self.impersonate:
            kwargs["impersonate"] = self.impersonate
        if self.http_version:
            kwargs["http_version"] = self.http_version
        return kwargs

    def _pick_proxy(self, explicit: str | None) -> tuple[str | None, bool]:
        """Returns ``(proxy, from_rotator)``."""
        if explicit:
            return explicit, False
        if self.proxy:
            return self.proxy, False
        if self.proxies is not None:
            return self.proxies.next(), True
        return None, False

    def _request_kwargs(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None,
        cookies: Mapping[str, str] | None,
        data: Any,
        json: Any,
        timeout: float | None,
        allow_redirects: bool | None,
        proxy: str | None,
        extra: Mapping[str, Any],
    ) -> dict[str, Any]:
        merged = dict(headers or {})
        if self.referer and not any(k.lower() == "referer" for k in {**self.headers, **merged}):
            merged["Referer"] = self.referer
        kwargs: dict[str, Any] = {
            "method": method,
            "url": url,
            "headers": merged or None,
            "cookies": dict(cookies) if cookies else None,
            "timeout": timeout if timeout is not None else self.timeout,
            "allow_redirects": self.follow_redirects if allow_redirects is None else allow_redirects,
        }
        if data is not None:
            kwargs["data"] = data
        if json is not None:
            kwargs["json"] = json
        if proxy:
            kwargs["proxy"] = proxy
        ignored = BROWSER_ONLY_OPTIONS.intersection(extra)
        if ignored:
            log.debug("ignoring browser-only options for an HTTP request: %s", ", ".join(sorted(ignored)))
        kwargs.update({k: v for k, v in extra.items() if k not in BROWSER_ONLY_OPTIONS})
        return kwargs

    def _to_response(self, raw: Any, request: Request, elapsed: float) -> Response:
        headers = Headers((k, v or "") for k, v in raw.headers.multi_items())
        try:
            cookies = {name: value for name, value in raw.cookies.items()}
        except Exception:  # pragma: no cover - defensive, cookie jars vary
            cookies = {}
        return Response(
            raw.url or request.url,
            status=raw.status_code,
            headers=headers,
            body=raw.content or b"",
            request=request,
            reason=raw.reason or "",
            cookies=cookies,
            elapsed=elapsed,
            history=[h.url for h in (raw.history or [])],
            http_version=_HTTP_VERSIONS.get(int(raw.http_version or 0)),
            source="http",
            adaptive_storage=self.adaptive_storage,
        )

    def _wrap_error(self, exc: BaseException, url: str, proxy: str | None) -> FetchError:
        is_timeout = isinstance(exc, (curl_exc.Timeout, asyncio.TimeoutError, TimeoutError))
        is_proxy = isinstance(exc, curl_exc.ProxyError) or (
            proxy is not None and "proxy" in str(exc).lower() and not is_timeout
        )
        # Bad URLs, bad arguments and invalid certificates will not fix themselves.
        retryable = not isinstance(exc, (curl_exc.CertificateVerifyError, TypeError, ValueError))
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

    def _cache_before(
        self, req: Request, headers: Mapping[str, str] | None
    ) -> tuple[Response | None, Any, Mapping[str, str] | None]:
        """Serve from cache, or add conditional headers for revalidation."""
        if self._cache_layer is None:
            return None, None, headers
        cached, stale, conditional = self._cache_layer.before(req, self.adaptive_storage)
        if conditional:
            headers = {**conditional, **(headers or {})}
        return cached, stale, headers

    def _finish(self, req: Request, response: Response, stale: Any = None) -> Response:
        if self._cache_layer is not None and response.cache_status is None:
            response = self._cache_layer.after(req, response, stale, self.adaptive_storage)
        if self.raise_for_status:
            response.raise_for_status()
        return response

    @staticmethod
    def _cookie_records(
        cookies: Mapping[str, str] | Iterable[Mapping[str, Any]], url: str | None, domain: str | None
    ) -> list[dict[str, Any]]:
        host = domain or (urlsplit(url).hostname if url else None) or ""
        if isinstance(cookies, Mapping):
            return [{"name": k, "value": v, "domain": host, "path": "/"} for k, v in cookies.items()]
        return [
            {
                "name": c["name"],
                "value": c.get("value", ""),
                "domain": (c.get("domain") or host).lstrip("."),
                "path": c.get("path") or "/",
                "secure": bool(c.get("secure", False)),
            }
            for c in cookies
        ]

    def _report(self, proxy: str | None, from_rotator: bool, ok: bool) -> None:
        if from_rotator and self.proxies is not None:
            (self.proxies.report_success if ok else self.proxies.report_failure)(proxy)

    def _retry_delay(self, attempt: int, response: Response | None = None) -> float:
        from ..utils import parse_retry_after

        delay = min(self.max_backoff, self.backoff * (2**attempt)) * (0.5 + random.random())
        if response is not None:
            retry_after = parse_retry_after(response.headers.get("retry-after"))
            if retry_after is not None:
                delay = max(delay, min(retry_after, self.max_backoff))
        return delay


class Fetcher(_HTTPBase):
    """Synchronous HTTP client that looks like a real browser.

    Keeps cookies between requests (it is a session). Use it as a context
    manager or call :meth:`close` when done::

        with Fetcher(impersonate="chrome", retries=3) as fetcher:
            page = fetcher.get("https://quotes.toscrape.com/")
            print(page.css(".quote .text::text").getall())
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._session = curl_requests.Session(**self._session_kwargs())

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
        data: Any = None,
        json: Any = None,
        proxy: str | None = None,
        timeout: float | None = None,
        retries: int | None = None,
        allow_redirects: bool | None = None,
        **extra: Any,
    ) -> Response:
        """Send a request and return a :class:`Response` (retrying if needed).

        Extra keyword arguments are passed to ``curl_cffi`` as-is.
        Raises :class:`~wintergrab.errors.FetchError` if every attempt fails
        with a network error.
        """
        req = Request(
            ensure_scheme(url), method=method, params=params, headers=dict(headers or {}), data=data, json=json
        )
        cached, stale, headers = self._cache_before(req, headers)
        if cached is not None:
            return self._finish(req, cached)
        attempts = 1 + (self.retries if retries is None else max(0, retries))
        for attempt in range(attempts):
            chosen, rotated = self._pick_proxy(proxy)
            kwargs = self._request_kwargs(
                req.method,
                req.url,
                headers=headers,
                cookies=cookies,
                data=data,
                json=json,
                timeout=timeout,
                allow_redirects=allow_redirects,
                proxy=chosen,
                extra=extra,
            )
            start = time.monotonic()
            try:
                raw = self._session.request(**kwargs)
            except Exception as exc:  # curl_cffi raises many exception types
                err = self._wrap_error(exc, req.url, chosen)
                if err.retryable:  # bad URLs/arguments are not the proxy's fault
                    self._report(chosen, rotated, ok=False)
                if attempt + 1 < attempts and err.retryable:
                    delay = self._retry_delay(attempt)
                    log.info("retrying %s in %.1fs (%s)", req.url, delay, describe(exc))
                    time.sleep(delay)
                    continue
                raise err from exc
            response = self._to_response(raw, req, time.monotonic() - start)
            self._report(chosen, rotated, ok=response.status not in PROXY_FAILURE_STATUSES)
            if response.status in self.retry_statuses and attempt + 1 < attempts:
                delay = self._retry_delay(attempt, response)
                log.info("retrying %s in %.1fs (HTTP %s)", req.url, delay, response.status)
                time.sleep(delay)
                continue
            return self._finish(req, response, stale)
        raise AssertionError("unreachable")  # pragma: no cover

    def get(self, url: str, **kwargs: Any) -> Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> Response:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> Response:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Response:
        return self.request("DELETE", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> Response:
        return self.request("HEAD", url, **kwargs)

    @property
    def session_cookies(self) -> dict[str, str]:
        """Cookies currently stored in the session."""
        return {c.name: c.value or "" for c in self._session.cookies.jar}

    def add_cookies(
        self,
        cookies: Mapping[str, str] | Iterable[Mapping[str, Any]],
        *,
        url: str | None = None,
        domain: str | None = None,
    ) -> None:
        """Load cookies into the session, scoped to a domain.

        Accepts ``{name: value}`` (scoped to ``url``'s host or ``domain``) or
        cookie dicts as returned by :meth:`BrowserFetcher.export_cookies` - the way to
        log in with a real browser and continue with fast HTTP requests.
        """
        for c in self._cookie_records(cookies, url, domain):
            self._session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"], secure=c["secure"])

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class AsyncFetcher(_HTTPBase):
    """Asynchronous version of :class:`Fetcher` for fetching many pages at once.

    ::

        async with AsyncFetcher() as fetcher:
            pages = await fetcher.get_many(urls, concurrency=10)

    Args:
        max_connections: Maximum simultaneous connections for this client.
        **kwargs: Same options as :class:`Fetcher`.
    """

    def __init__(self, *, max_connections: int = 64, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.max_connections = max_connections
        self._session: curl_requests.AsyncSession | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Cookies to (re)apply to new sessions, keyed by (domain, path, name).
        self._pending_cookies: dict[tuple[str, str, str], dict[str, Any]] = {}

    def _get_session(self) -> curl_requests.AsyncSession:
        loop = asyncio.get_running_loop()
        if self._session is None or self._loop is not loop:
            # AsyncSessions are bound to the loop they were created on. Close a
            # session from another loop if that loop can still run it.
            old, old_loop = self._session, self._loop
            if old is not None and old_loop is not None and old_loop.is_running() and not old_loop.is_closed():
                asyncio.run_coroutine_threadsafe(old.close(), old_loop)
            self._session = curl_requests.AsyncSession(max_clients=self.max_connections, **self._session_kwargs())
            self._loop = loop
            for c in self._pending_cookies.values():
                self._session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"], secure=c["secure"])
        return self._session

    def add_cookies(
        self,
        cookies: Mapping[str, str] | Iterable[Mapping[str, Any]],
        *,
        url: str | None = None,
        domain: str | None = None,
    ) -> None:
        """Load cookies into the session (see :meth:`Fetcher.add_cookies`)."""
        records = self._cookie_records(cookies, url, domain)
        for c in records:  # also applied if the session is recreated
            self._pending_cookies[(c["domain"], c["path"], c["name"])] = c
        if self._session is not None:
            for c in records:
                self._session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"], secure=c["secure"])

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        cookies: Mapping[str, str] | None = None,
        data: Any = None,
        json: Any = None,
        proxy: str | None = None,
        timeout: float | None = None,
        retries: int | None = None,
        allow_redirects: bool | None = None,
        request: Request | None = None,
        **extra: Any,
    ) -> Response:
        """Async version of :meth:`Fetcher.request`."""
        req = request or Request(
            ensure_scheme(url), method=method, params=params, headers=dict(headers or {}), data=data, json=json
        )
        cached, stale, headers = self._cache_before(req, headers)
        if cached is not None:
            return self._finish(req, cached)
        session = self._get_session()
        attempts = 1 + (self.retries if retries is None else max(0, retries))
        for attempt in range(attempts):
            chosen, rotated = self._pick_proxy(proxy)
            kwargs = self._request_kwargs(
                req.method,
                req.url,
                headers=headers,
                cookies=cookies,
                data=data,
                json=json,
                timeout=timeout,
                allow_redirects=allow_redirects,
                proxy=chosen,
                extra=extra,
            )
            start = time.monotonic()
            try:
                raw = await session.request(**kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                err = self._wrap_error(exc, req.url, chosen)
                if err.retryable:  # bad URLs/arguments are not the proxy's fault
                    self._report(chosen, rotated, ok=False)
                if attempt + 1 < attempts and err.retryable:
                    delay = self._retry_delay(attempt)
                    log.info("retrying %s in %.1fs (%s)", req.url, delay, describe(exc))
                    await asyncio.sleep(delay)
                    continue
                raise err from exc
            response = self._to_response(raw, req, time.monotonic() - start)
            self._report(chosen, rotated, ok=response.status not in PROXY_FAILURE_STATUSES)
            if response.status in self.retry_statuses and attempt + 1 < attempts:
                delay = self._retry_delay(attempt, response)
                log.info("retrying %s in %.1fs (HTTP %s)", req.url, delay, response.status)
                await asyncio.sleep(delay)
                continue
            return self._finish(req, response, stale)
        raise AssertionError("unreachable")  # pragma: no cover

    async def get(self, url: str, **kwargs: Any) -> Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Response:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> Response:
        return await self.request("PUT", url, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> Response:
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> Response:
        return await self.request("DELETE", url, **kwargs)

    async def head(self, url: str, **kwargs: Any) -> Response:
        return await self.request("HEAD", url, **kwargs)

    async def get_many(
        self,
        urls: Iterable[str],
        *,
        concurrency: int = 10,
        return_exceptions: bool = True,
        **kwargs: Any,
    ) -> list[Response | FetchError]:
        """Fetch many URLs concurrently; results come back in input order.

        With ``return_exceptions=True`` (default) a failed URL yields its
        :class:`~wintergrab.errors.FetchError` instead of aborting the batch.
        """
        sem = asyncio.Semaphore(max(1, concurrency))

        async def one(u: str) -> Response | FetchError:
            async with sem:
                try:
                    return await self.get(u, **kwargs)
                except FetchError as exc:
                    if return_exceptions:
                        return exc
                    raise

        return list(await asyncio.gather(*(one(u) for u in urls)))

    async def iter_many(
        self, urls: Iterable[str], *, concurrency: int = 10, **kwargs: Any
    ) -> AsyncIterator[Response | FetchError]:
        """Like :meth:`get_many` but yields each result as soon as it is ready."""
        sem = asyncio.Semaphore(max(1, concurrency))

        async def one(u: str) -> Response | FetchError:
            async with sem:
                try:
                    return await self.get(u, **kwargs)
                except FetchError as exc:
                    return exc

        tasks = [asyncio.ensure_future(one(u)) for u in urls]
        try:
            for fut in asyncio.as_completed(tasks):
                yield await fut
        finally:
            for t in tasks:
                t.cancel()

    async def aclose(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            finally:
                self._session = None

    close = aclose

    async def __aenter__(self) -> AsyncFetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
