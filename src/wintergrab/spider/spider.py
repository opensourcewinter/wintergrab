"""The :class:`Spider` base class - subclass it to crawl many pages."""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import re
import sys
import threading
from collections.abc import AsyncIterable, AsyncIterator, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..fetchers.blocking import looks_blocked
from ..fetchers.browser import AsyncBrowserFetcher
from ..fetchers.cache import HTTPCache
from ..fetchers.http import DEFAULT_RETRY_STATUSES, AsyncFetcher
from ..netpolicy import NetworkPolicy
from ..proxy import ProxyRotator
from ..request import Request
from ..sitemaps import parse_lastmod, parse_sitemap, robots_sitemaps
from .sessions import SessionManager

if TYPE_CHECKING:
    from ..errors import WintergrabError
    from ..fetchers.response import Response
    from .engine import Engine


@dataclass
class CrawlResult:
    """What :meth:`Spider.run` returns.

    Attributes:
        items: Items scraped during this run (if ``keep_items`` is on).
        stats: Counters: pages, items, retries, errors, status codes...
        status: ``"finished"`` (nothing left to do), ``"limit"`` (hit
            ``max_pages``/``max_items``), ``"paused"`` (state saved, run
            again to resume) or ``"stopped"``.
        crawl_dir: Where the resumable state lives, if configured.
    """

    items: list[Any] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    status: str = "finished"
    crawl_dir: str | None = None

    @property
    def paused(self) -> bool:
        return self.status == "paused"

    @property
    def finished(self) -> bool:
        return self.status in ("finished", "limit")

    def save(self, path: str | Path) -> Path:
        """Write :attr:`items` to ``.jsonl``, ``.json`` or ``.csv``."""
        from .exporters import write_items

        return write_items(path, self.items)

    def __repr__(self) -> str:
        return (
            f"<CrawlResult {self.status}: {self.stats.get('pages', 0)} pages, "
            f"{self.stats.get('items', 0)} items, {len(self.items)} kept>"
        )


class Spider:
    """Base class for crawlers. Override :meth:`parse` (and friends).

    A minimal spider::

        class QuotesSpider(Spider):
            start_urls = ["https://quotes.toscrape.com/"]

            async def parse(self, response):
                for quote in response.css(".quote"):
                    yield {"text": quote.css(".text::text").get(),
                           "author": quote.css(".author::text").get()}
                next_page = response.css("li.next a")
                if next_page:
                    yield response.follow(next_page[0])

        result = QuotesSpider(output="quotes.jsonl").run()

    Callbacks may be regular functions, generators, async functions or
    async generators, and may yield/return dicts (or dataclasses, pydantic
    models...) as items and :class:`Request` objects to crawl next.

    Every class attribute below can be overridden per instance:
    ``QuotesSpider(concurrency=4, max_pages=100)``.
    """

    #: Name used in logs and the default checkpoint folder.
    name: str | None = None
    #: URLs to start from (see also :meth:`start_requests`).
    start_urls: Sequence[str] = ()
    #: Only follow links on these domains (subdomains included). Empty = no limit.
    allowed_domains: Sequence[str] = ()

    # -- discovery -------------------------------------------------------- #
    #: Sitemaps, sitemap indexes, feeds or robots.txt URLs to take pages from.
    sitemap_urls: Sequence[str] = ()
    #: ``(regex, callback)`` pairs routing sitemap URLs to callbacks (first match
    #: wins; URLs matching no rule are skipped). Empty = every URL -> ``parse``.
    sitemap_rules: Sequence[tuple[str, str | Callable[..., Any]]] = ()
    #: Only follow nested sitemaps whose URL matches one of these regexes.
    sitemap_follow: Sequence[str] = ()
    #: Only crawl sitemap entries modified at/after this date (incremental crawls).
    sitemap_since: str | datetime | None = None
    #: Rewrite URLs before queueing them. ``True`` drops tracking parameters
    #: (``utm_*``, ``gclid``...), session ids and fragments, resolves ``..`` and
    #: sorts the query (see :class:`~wintergrab.urls.URLNormalizer`); or pass a
    #: normalizer, a dict of its options, or any ``url -> url`` callable.
    url_normalizer: Any = None
    #: Which discovered links get queued: a :class:`~wintergrab.urls.URLRules`,
    #: a dict of its options, or ``True`` for the defaults (skip images, media and
    #: archives; guard against crawler traps). Start URLs are never filtered.
    url_rules: Any = None

    # -- speed ------------------------------------------------------------ #
    #: Maximum requests in flight overall.
    concurrency: int = 16
    #: Maximum requests in flight per domain.
    concurrency_per_domain: int = 4
    #: Minimum seconds between requests to the same domain.
    download_delay: float = 0.0
    #: Adapt speed to the site: back off on 429/503/blocks, speed up when healthy.
    autothrottle: bool = True
    #: Upper bound for the per-domain delay when backing off.
    max_delay: float = 60.0
    #: Pass an :class:`AutoThrottle` instance for full control (overrides the above).
    throttle: Any = None

    # -- limits ----------------------------------------------------------- #
    max_pages: int | None = None
    max_items: int | None = None
    max_depth: int | None = None

    # -- fetching --------------------------------------------------------- #
    #: Browser to impersonate for HTTP requests (``None`` = plain curl).
    impersonate: str | None = "chrome"
    #: Headers added to every HTTP request.
    default_headers: Mapping[str, str] = {}
    timeout: float = 30.0
    verify: bool | str = True
    #: Attempts after a failure / retryable status / block.
    retries: int = 3
    retry_statuses: Collection[int] = DEFAULT_RETRY_STATUSES
    #: Non-2xx statuses that should still reach your callbacks (e.g. ``{404}``).
    allowed_statuses: Collection[int] = ()
    #: Proxy URLs (or a :class:`ProxyRotator`) to rotate through.
    proxies: Sequence[str] | ProxyRotator | None = None
    #: Make the default session a headless browser instead of plain HTTP.
    use_browser: bool = False
    #: Session to retry *blocked* requests with (e.g. ``"browser"``).
    fallback_session: str | None = None
    #: Copy cookies from browser responses into the HTTP sessions, so a session
    #: established in the browser (consent wall, login, JS check) carries on over
    #: fast HTTP for the rest of the crawl.
    share_browser_cookies: bool = True
    #: Respect robots.txt rules and Crawl-delay.
    obey_robots_txt: bool = True
    robots_user_agent: str = "*"
    #: Where requests may go. ``"public"`` refuses private, loopback and cloud-metadata
    #: addresses (SSRF protection; also checked on every redirect hop); pass a
    #: :class:`~wintergrab.netpolicy.NetworkPolicy` for finer control. ``None`` = anywhere.
    network_policy: Any = None
    #: Browser sessions: block ads, analytics and trackers too (``True``, a dict of
    #: :class:`~wintergrab.fetchers.resources.ResourceFilter` options, or a filter).
    resource_filter: Any = None
    #: Drop requests for URLs already seen.
    dedupe: bool = True
    #: ``"memory"`` (fastest) or ``"disk"``: an SQLite queue + Bloom filter that keeps
    #: memory flat for crawls of millions of URLs and survives crashes (needs ``crawl_dir``).
    frontier: str = "memory"

    # -- caching ---------------------------------------------------------- #
    #: Cache responses on disk (``True``, a path, or an :class:`HTTPCache`).
    cache: bool | str | HTTPCache | None = None
    #: ``"revalidate"`` (only re-download what changed), ``"prefer"`` (fetch each
    #: page once), ``"offline"`` (replay from cache only) or ``"refresh"``.
    cache_mode: str = "revalidate"
    #: Seconds a cached response counts as fresh (overrides HTTP headers).
    cache_ttl: float | None = None

    # -- output & state --------------------------------------------------- #
    #: Stream items to this file (``.jsonl``, ``.json``, ``.csv`` or ``.sqlite``/``.db``).
    output: str | None = None
    #: Item field that identifies an item (e.g. ``"url"``): duplicates are dropped,
    #: and SQLite output upserts on it so re-crawls update rows in place.
    unique_key: str | None = None
    #: Directory for pause/resume state. Setting it makes the crawl resumable.
    crawl_dir: str | None = None
    #: Seconds between automatic checkpoints (crash safety).
    checkpoint_interval: float = 60.0
    #: Keep items in memory for ``CrawlResult.items``. Turn off for huge crawls.
    keep_items: bool = True
    #: Log level for the ``wintergrab`` logger (``None`` leaves logging alone).
    log_level: str | None = "INFO"
    #: Seconds between progress log lines.
    log_interval: float = 30.0
    #: Live status line on the terminal (``None`` = automatic: on for interactive terminals).
    progress: bool | None = None
    #: Run on uvloop (a faster event loop) when it is installed: ``pip install "wintergrab[speed]"``.
    use_uvloop: bool = True

    def __init__(self, **overrides: Any) -> None:
        for key, value in overrides.items():
            # Methods are not settings; callable *values* (a URL normalizer, a priority function) are fine.
            if key.startswith("_") or not hasattr(type(self), key) or inspect.isroutine(getattr(type(self), key)):
                raise TypeError(f"{type(self).__name__} has no setting {key!r}")
            setattr(self, key, value)
        if not self.name:
            self.name = type(self).__name__
        self.logger = logging.getLogger(f"wintergrab.spider.{self.name}")
        self._engine: Engine | None = None
        self._pending_command: str | None = None
        self._http_cache: HTTPCache | None = None
        self._network_policy: NetworkPolicy | bool | None = False  # False = not resolved yet

    def http_cache(self) -> HTTPCache | None:
        """The spider's shared :class:`HTTPCache` (``None`` unless :attr:`cache` is set)."""
        if self._http_cache is None and self.cache:
            self._http_cache = HTTPCache.coerce(self.cache, mode=self.cache_mode, ttl=self.cache_ttl)
        return self._http_cache

    def get_network_policy(self) -> NetworkPolicy | None:
        """The spider's shared :class:`~wintergrab.netpolicy.NetworkPolicy` (``None`` = no restriction)."""
        if self._network_policy is False:
            self._network_policy = NetworkPolicy.coerce(self.network_policy)
        return self._network_policy  # type: ignore[return-value]

    # ------------------------------------------------------------------ #
    # hooks to override
    # ------------------------------------------------------------------ #
    def start_requests(self) -> Iterable[Request | str] | AsyncIterable[Request | str]:  # type: ignore[return]
        """Initial requests: one per URL in :attr:`start_urls` and :attr:`sitemap_urls`."""
        for url in self.sitemap_urls:
            yield Request(url, callback="_parse_sitemap", priority=100)
        for url in self.start_urls:
            yield Request(url, dont_filter=False)

    def _parse_sitemap(self, response: Response) -> Iterable[Request]:
        """Turn a sitemap (index, feed or robots.txt) into requests."""
        if response.url.split("?")[0].rstrip("/").endswith("robots.txt"):
            for url in robots_sitemaps(response.text, response.url):
                yield Request(url, callback="_parse_sitemap", priority=100)
            return
        try:
            _, entries = parse_sitemap(response.body, response.url)
        except ValueError as exc:
            self.logger.warning("could not read sitemap %s: %s", response.url, exc)
            return
        since = parse_lastmod(self.sitemap_since) if isinstance(self.sitemap_since, str) else self.sitemap_since
        follow = [re.compile(p) for p in self.sitemap_follow]
        for entry in entries:
            if entry.kind == "sitemap":
                if not follow or any(rx.search(entry.loc) for rx in follow):
                    yield Request(entry.loc, callback="_parse_sitemap", priority=100)
                continue
            if since is not None:
                modified = entry.lastmod_datetime
                cutoff = since if since.tzinfo else since.replace(tzinfo=timezone.utc)  # naive = UTC, like wg.sitemap()
                if modified is None or modified < cutoff:
                    continue
            callback = self._sitemap_callback(entry.loc)
            if callback is not None:
                yield Request(entry.loc, callback=callback, meta={"sitemap_lastmod": entry.lastmod})

    def _sitemap_callback(self, url: str) -> str | Callable[..., Any] | None:
        if not self.sitemap_rules:
            return "parse"
        for pattern, callback in self.sitemap_rules:
            if re.search(pattern, url):
                return callback
        return None

    def parse(self, response: Response) -> Any:
        """Default callback. Yield items and/or :class:`Request` objects."""
        raise NotImplementedError(f"{type(self).__name__}.parse() is not implemented")

    def configure_sessions(self, sessions: SessionManager) -> None:
        """Register the fetch sessions this spider uses.

        The default registers ``"http"`` (an :class:`AsyncFetcher` built from
        the spider's settings) and, if :attr:`use_browser` or
        :attr:`fallback_session` asks for it, ``"browser"``. Override to add
        your own, e.g. several logged-in accounts.
        """
        sessions.add(
            "http",
            AsyncFetcher(
                impersonate=self.impersonate,
                headers=self.default_headers,
                timeout=self.timeout,
                verify=self.verify,
                retries=0,
                max_connections=max(16, self.concurrency * 2),
                cache=self.http_cache(),
                network_policy=self.get_network_policy(),
            ),
            default=not self.use_browser,
        )
        if self.use_browser or self.fallback_session == "browser":
            sessions.add(
                "browser",
                AsyncBrowserFetcher(
                    timeout=self.timeout,
                    retries=0,
                    max_pages=max(1, min(self.concurrency, 8)),
                    cache=self.http_cache(),
                    resource_filter=self.resource_filter,
                    network_policy=self.get_network_policy(),
                ),
                default=self.use_browser,
            )

    def process_item(self, item: Any) -> Any:
        """Clean/validate each item. Return it (possibly changed) or ``None`` to drop it.

        May be ``async``.
        """
        return item

    def is_blocked(self, response: Response) -> bool:
        """Decide whether a response is a block/challenge page (retried, and
        slows the domain down). Override for site-specific checks."""
        return looks_blocked(response)

    def on_error(self, request: Request, error: BaseException) -> Any:
        """Called when a request finally fails (after retries) and has no errback."""
        self.logger.error("giving up on %s: %s", request.url, error)

    def on_start(self) -> Any:
        """Called once before crawling starts (may be async)."""

    def on_close(self, result: CrawlResult) -> Any:
        """Called once after crawling ends (may be async)."""

    # ------------------------------------------------------------------ #
    # running
    # ------------------------------------------------------------------ #
    async def arun(self, *, resume: bool = True) -> CrawlResult:
        """Run the crawl inside an existing event loop."""
        from .engine import Engine

        engine = Engine(self, install_signal_handlers=threading.current_thread() is threading.main_thread())
        self._engine = engine
        if self._pending_command:
            engine.request_stop(self._pending_command)
            self._pending_command = None
        try:
            return await engine.run(resume=resume)
        finally:
            self._engine = None

    def run(self, *, resume: bool = True) -> CrawlResult:
        """Run the crawl and block until it finishes, pauses or stops.

        With :attr:`crawl_dir` set, pressing Ctrl+C pauses the crawl and saves
        its state; running again resumes where it left off (pass
        ``resume=False`` to start over).
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self._run_coroutine(self.arun(resume=resume))
        # Already inside an event loop (e.g. Jupyter): run in a helper thread.
        # Signal handlers only work in the main thread, so turn Ctrl+C
        # (KeyboardInterrupt here) into pause / force-stop requests ourselves.
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            future = pool.submit(self._run_coroutine, self.arun(resume=resume))
            interrupts = 0
            while True:
                try:
                    # Wait in short slices: on Windows a wait without a timeout
                    # isn't interrupted by Ctrl+C until the crawl has ended.
                    if concurrent.futures.wait([future], timeout=0.25).done:
                        return future.result()
                except KeyboardInterrupt:
                    interrupts += 1
                    if interrupts == 1:
                        self.pause()  # stops instead when there is no crawl_dir
                    elif self._engine is not None:
                        self._engine.force_stop()

    def _run_coroutine(self, coro: Any) -> Any:
        if self.use_uvloop and sys.platform != "win32":
            try:
                import uvloop
            except ImportError:
                pass
            else:
                return uvloop.run(coro)
        return asyncio.run(coro)

    async def stream(self, *, resume: bool = True) -> AsyncIterator[Any]:
        """Run the crawl and yield items as they are scraped::

        async for item in MySpider().stream():
            print(item)
        """
        from .engine import Engine

        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=256)
        sentinel = object()
        engine = Engine(self, install_signal_handlers=False, item_queue=queue)
        self._engine = engine
        if self._pending_command:
            engine.request_stop(self._pending_command)
            self._pending_command = None
        consumer_active = True

        async def runner() -> None:
            try:
                await engine.run(resume=resume)
            finally:
                if consumer_active:
                    await queue.put(sentinel)

        task = asyncio.create_task(runner())
        try:
            while True:
                item = await queue.get()
                if item is sentinel:
                    break
                yield item
            await task  # surface errors
        finally:
            consumer_active = False
            if not task.done():
                # The consumer stopped early: stop feeding the queue, unblock any
                # pending put, and let the crawl shut down cleanly.
                engine.item_queue = None
                engine.request_stop("stopped")
                deadline = asyncio.get_running_loop().time() + 60
                while not task.done():
                    while not queue.empty():
                        queue.get_nowait()
                    if asyncio.get_running_loop().time() > deadline:
                        task.cancel()
                    await asyncio.wait({task}, timeout=0.1)
            if task.done() and not task.cancelled():
                task.exception()  # mark as retrieved; errors after an early exit are not raised
            self._engine = None

    def pause(self) -> None:
        """Stop gracefully and save state so the crawl can resume (thread-safe)."""
        self._command("paused")

    def stop(self) -> None:
        """Stop gracefully without keeping the queue (thread-safe)."""
        self._command("stopped")

    def _command(self, status: str) -> None:
        if self._engine is not None:
            self._engine.request_stop(status)
        else:
            self._pending_command = status

    @property
    def stats(self) -> dict[str, Any]:
        """Live stats of the running crawl (empty when not running)."""
        return dict(self._engine.stats) if self._engine is not None else {}

    def fatal(self, error: WintergrabError | Exception) -> None:
        """Abort the crawl with an error (raised from :meth:`run`)."""
        if self._engine is not None:
            self._engine.fail(error)
        else:
            raise error
