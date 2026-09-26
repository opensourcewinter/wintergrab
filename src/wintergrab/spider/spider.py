"""The :class:`Spider` base class - subclass it to crawl many pages."""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import re
import sys
import threading
import types
from collections.abc import AsyncIterable, AsyncIterator, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import ConfigurationError
from ..events import EventBus
from ..fetchers.blocking import looks_blocked
from ..fetchers.browser import AsyncBrowserFetcher
from ..fetchers.cache import HTTPCache
from ..fetchers.http import DEFAULT_RETRY_STATUSES, AsyncFetcher
from ..fetchers.strategy import needs_javascript
from ..netpolicy import NetworkPolicy
from ..proxy import ProxyRotator
from ..request import Request
from ..sitemaps import parse_lastmod, parse_sitemap, robots_sitemaps
from .sessions import SessionManager

if TYPE_CHECKING:
    from ..errors import WintergrabError
    from ..fetchers.response import Response
    from .engine import Engine
    from .failures import FailureDiagnosis


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
    #: What went wrong, grouped and explained (see :mod:`wintergrab.spider.failures`).
    failures: list[FailureDiagnosis] = field(default_factory=list)
    #: Final metrics snapshot: rates, latency percentiles, per-domain throttle state, budgets.
    metrics: dict[str, Any] = field(default_factory=dict)
    #: With ``history``: what changed since the previous run (a :class:`~wintergrab.history.ChangeReport`).
    changes: Any = None
    #: With ``profile``: the site's :class:`~wintergrab.intel.SiteProfile`.
    profile: Any = None
    #: With ``adaptive_fetch``: the :class:`~wintergrab.fetchers.strategy.FetchStrategy`, with what it
    #: learned about each URL pattern.
    fetch_strategy: Any = None
    #: With ``optimize``: the :class:`~wintergrab.spider.optimizer.CrawlOptimizer`, with what it learned
    #: about each URL pattern (``describe()``).
    optimizer: Any = None
    #: With ``run_registry`` or ``record``: the run's id in the registry (``"run-7"``; see :mod:`wintergrab.runs`).
    run_id: str | None = None

    @property
    def paused(self) -> bool:
        return self.status == "paused"

    @property
    def limit_reason(self) -> str | None:
        """Which limit or budget stopped the crawl (``"max_pages"``, ``"max_bytes"``...), if any."""
        return self.stats.get("limit_reason")

    def failure_report(self, limit: int = 10) -> str:
        """The failure diagnoses as readable text."""
        if not self.failures:
            return "no failures"
        parts = [d.describe() for d in self.failures[:limit]]
        if len(self.failures) > limit:
            parts.append(f"... and {len(self.failures) - limit} more kinds of failure")
        return "\n\n".join(parts)

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


#: Settings that were removed, and why (setting one is an error, not silently ignored).
_REMOVED_SETTINGS = {
    "fallback_session": "a blocked or rate-limited page is not fetched again another way to get past the refusal; "
    "the domain slows down, and the page is reported (docs/responsible-access.md). For pages that need "
    "JavaScript, set adaptive_fetch = True",
}


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

    # -- limits and budgets ------------------------------------------------ #
    max_pages: int | None = None
    max_items: int | None = None
    max_depth: int | None = None
    #: Budgets (see :mod:`wintergrab.spider.budget`): the crawl stops with status
    #: ``"limit"`` (resumable with a ``crawl_dir``) when one runs out.
    max_requests: int | None = None
    max_bytes: int | None = None
    #: Seconds of crawling, counted across resumed runs.
    max_runtime: float | None = None
    max_browser_pages: int | None = None
    #: URLs given up on.
    max_errors: int | None = None
    #: Stop when more than this fraction of pages failed (after ``error_rate_min_pages`` pages).
    max_error_rate: float | None = None
    error_rate_min_pages: int = 50
    #: Resident memory (bytes), CPU seconds, and bytes written to ``output`` in this run.
    max_memory: int | None = None
    max_cpu_seconds: float | None = None
    max_output_bytes: int | None = None
    #: Degrade gracefully: once any budget is this much used (e.g. ``0.9``), only
    #: start requests with a priority of at least ``budget_soft_priority``.
    budget_soft_limit: float | None = None
    budget_soft_priority: int = 1

    # -- queue order -------------------------------------------------------- #
    #: ``"bfs"`` (breadth-first: oldest first among equal priorities) or ``"dfs"`` (depth-first).
    crawl_order: str = "bfs"
    #: ``priority_fn(request) -> int``: the priority of every queued request (e.g.
    #: favour product pages). Higher runs first. Retries keep their priority.
    priority_fn: Callable[[Request], int] | None = None

    # -- extensions ---------------------------------------------------------- #
    #: Downloader middlewares (see :mod:`wintergrab.spider.middleware`).
    middlewares: Sequence[Any] = ()
    #: Item pipelines run after :meth:`process_item`: objects with ``process_item(item, spider)``
    #: or plain ``item -> item`` functions. Return ``None`` (or raise ``DropItem``) to drop.
    pipelines: Sequence[Any] = ()

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
    #: Fetch pages over HTTP first, and again in a browser when their HTML is not enough
    #: (:meth:`needs_browser`), learning per URL pattern which pages need one: ``True``, a file
    #: that keeps what was learned across crawls, or a :class:`~wintergrab.fetchers.strategy.FetchStrategy`.
    adaptive_fetch: Any = False
    #: With ``adaptive_fetch``: CSS selectors a usable page has; a page fetched over HTTP where
    #: one of them finds nothing goes to the browser.
    render_if_missing: Sequence[str] = ()
    #: Learn during the crawl which URL patterns give items: fetch those first, skip the patterns
    #: that give nothing, drop query parameters that change nothing (see
    #: :mod:`wintergrab.spider.optimizer`): ``True``, a file that keeps what was learned across
    #: crawls, or a :class:`~wintergrab.spider.optimizer.CrawlOptimizer`.
    optimize: Any = False
    #: Keep a record of each run in a workspace (see :mod:`wintergrab.runs`): ``True`` for ``.wintergrab``
    #: in the current directory, or a directory. The record has the run's settings, status, stats,
    #: failures and events.
    run_registry: Any = None
    #: Also keep every response and item of the run, to replay it without the network
    #: (:func:`wintergrab.runs.replay`); implies ``run_registry``. The recording is the run's own cache.
    record: bool = False
    #: How to run this crawl again, kept with its record (the command line and goal runs set it).
    run_recipe: dict[str, Any] | None = None
    #: A label kept with the run's record (a project sets its job's name).
    run_label: str | None = None
    #: Where to post the crawl's events as they happen (see :mod:`wintergrab.webhooks`): URLs,
    #: ``{"url", "events", "secret"...}`` mappings, or :class:`~wintergrab.webhooks.Webhook` objects.
    webhooks: Sequence[Any] = ()
    #: Copy cookies from browser responses into the HTTP sessions, so a session
    #: established in the browser (a login, a consent dialog) carries on over
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
    #: Record requests given up on in ``crawl_dir/dead_letters.jsonl`` (``True``), in a
    #: file of your choice (a path) or not at all (``False``).
    dead_letters: bool | str | None = True
    #: Queue the dead letters of earlier runs again (and start a fresh file).
    retry_dead_letters: bool = False
    #: Write structured events as JSON lines: ``True`` = ``crawl_dir/events.jsonl``, or a path.
    event_log: bool | str | None = None
    #: Keep items in memory for ``CrawlResult.items``. Turn off for huge crawls.
    keep_items: bool = True
    #: Record every page's fingerprints and items in this history file (a path, or a
    #: :class:`~wintergrab.history.PageHistory`): the crawl ends with what changed since the
    #: last run (``CrawlResult.changes``), and each URL's change rate is tracked.
    history: Any = None
    #: Also keep every page's HTML in the history (compressed).
    history_html: bool = False
    #: With ``history``: don't fetch pages that have probably not changed since they were last
    #: seen, judging by how often they changed before. Start URLs are always fetched.
    skip_fresh: bool = False
    #: Profile the site while crawling (``result.profile``): technologies, page types, templates,
    #: API endpoints, crawlability... ``True``, a path to also save it as JSON, or a
    #: :class:`~wintergrab.intel.SiteProfiler`.
    profile: Any = False
    #: Log level for the ``wintergrab`` logger (``None`` leaves logging alone).
    log_level: str | None = "INFO"
    #: Seconds between progress log lines.
    log_interval: float = 30.0
    #: Live status line on the terminal (``None`` = automatic: on for interactive terminals).
    progress: bool | None = None
    #: Run on uvloop (a faster event loop) when it is installed: ``pip install "wintergrab[speed]"``.
    use_uvloop: bool = True

    def __init__(self, **overrides: Any) -> None:
        for key, why in _REMOVED_SETTINGS.items():
            if key in overrides or getattr(type(self), key, None) is not None:
                raise ConfigurationError(f"{type(self).__name__}.{key} was removed: {why}", key=key)
        for key, value in overrides.items():
            if key.startswith("_") or not _is_setting(type(self), key):
                raise TypeError(f"{type(self).__name__} has no setting {key!r}")
            setattr(self, key, value)
        if not self.name:
            self.name = type(self).__name__
        for name, arity in (("priority_fn", 1), ("url_normalizer", 1)):
            unbound = _unbound_function(self, name, arity)
            if unbound is not None:
                setattr(self, name, unbound)
        self.logger = logging.getLogger(f"wintergrab.spider.{self.name}")
        #: Structured events of this spider's crawls (see :mod:`wintergrab.events`).
        self.events = EventBus(origin=self.name)
        self._engine: Engine | None = None
        self._pending_command: str | None = None
        self._http_cache: HTTPCache | None = None
        self._network_policy: NetworkPolicy | bool | None = False  # False = not resolved yet

    def http_cache(self) -> HTTPCache | None:
        """The spider's shared :class:`HTTPCache` (``None`` unless :attr:`cache` is set)."""
        if self._http_cache is None and self.cache is not None and self.cache is not False:  # an empty cache is falsy
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
        the spider's settings) and, if :attr:`use_browser` or :attr:`adaptive_fetch`
        asks for it, ``"browser"``. Override to add your own.
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
        if self.use_browser or self.adaptive_fetch:
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

    def needs_browser(self, response: Response) -> str | bool | None:
        """With :attr:`adaptive_fetch`: whether a page fetched over HTTP needs a browser to show
        its content. Return why (or ``True``), or a false value.

        By default: a :attr:`render_if_missing` selector finds nothing, or the page looks like a
        JavaScript app shell (:func:`~wintergrab.fetchers.strategy.needs_javascript`). Override
        for site-specific checks. Block pages are :meth:`is_blocked`'s business, never this one's.
        """
        for css in self.render_if_missing:
            if not response.css(css):
                return f"nothing matches {css!r}"
        return needs_javascript(response)

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
            in_loop = True
        except RuntimeError:
            in_loop = False
        if not in_loop:
            # Outside the except block: callback tracebacks must not carry this RuntimeError as context.
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

    def metrics(self) -> dict[str, Any]:
        """Live metrics of the running crawl: rates, latency, per-domain throttle state, budgets (empty when idle)."""
        return self._engine.snapshot() if self._engine is not None else {}

    def fatal(self, error: WintergrabError | Exception) -> None:
        """Abort the crawl with an error (raised from :meth:`run`)."""
        if self._engine is not None:
            self._engine.fail(error)
        else:
            raise error


def _unbound_function(spider: Spider, name: str, arity: int) -> Callable[..., Any] | None:
    """A plain function stored as a class attribute setting (``priority_fn = by_depth``).

    Python turns it into a bound method, so it would receive the spider as an extra
    first argument. If its signature takes exactly the setting's natural arguments,
    return the plain function so it is called as written.
    """
    if name in vars(spider):
        return None
    for klass in type(spider).__mro__:
        raw = vars(klass).get(name)
        if raw is None:
            continue
        if isinstance(raw, types.FunctionType):
            try:
                params = [
                    p
                    for p in inspect.signature(raw).parameters.values()
                    if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty
                ]
            except (TypeError, ValueError):
                return None
            return raw if len(params) == arity else None
        return None
    return None


def _is_setting(cls: type, key: str) -> bool:
    """Whether ``key`` is a setting (overridable per instance) rather than a method.

    A name is a setting when some class of the hierarchy declares it as a plain
    value. That keeps callable settings (``priority_fn = by_depth``) overridable
    while methods such as ``parse`` are not.
    """
    for klass in cls.__mro__:
        if key in vars(klass):
            value = vars(klass)[key]
            if not (inspect.isroutine(value) or isinstance(value, (property, classmethod, staticmethod))):
                return True
    return False
