"""The crawl loop that drives a :class:`Spider`."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import pickle
import random
import signal
import time
from collections.abc import AsyncIterable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import (
    BrowserNotAvailable,
    CheckpointError,
    ConfigurationError,
    FetchError,
    FetchTimeout,
    HTTPStatusError,
    NetworkPolicyError,
    PolicyError,
    RobotsPolicyError,
    category_of,
    describe,
)
from ..events import JsonlEventSink
from ..fetchers.cache import CacheMiss, HTTPCache
from ..fetchers.http import PROXY_FAILURE_STATUSES, AsyncFetcher
from ..fetchers.response import Response
from ..fetchers.strategy import FetchStrategy
from ..proxy import ProxyRotator, proxy_label
from ..redact import redact_url
from ..request import Request
from ..runs import RunRecorder, RunRegistry
from ..urls import URLNormalizer, URLRules
from ..utils import configure_logging, domain_matches, ensure_scheme, host_of, maybe_await, parse_retry_after
from ..webhooks import Webhook
from .budget import BudgetMonitor, BudgetStatus
from .checkpoint import Checkpoint
from .deadletters import DeadLetterQueue
from .exporters import Exporter, open_exporter, to_dict
from .failures import FailureTracker
from .frontier import DiskScheduler
from .metrics import CrawlMetrics
from .middleware import DropItem, IgnoreRequest
from .optimizer import CrawlOptimizer
from .progress import ProgressDisplay
from .robots import RobotsPolicy
from .scheduler import Scheduler
from .sessions import SessionManager
from .spider import CrawlResult
from .throttle import AutoThrottle

if TYPE_CHECKING:
    from .spider import Spider

log = logging.getLogger("wintergrab.spider")

PUSHBACK_STATUSES = frozenset({429, 503})
_HANDLED: Any = object()  # a middleware dealt with the request itself (dropped or replaced it)
CRAWL_ORDERS = {"bfs": False, "fifo": False, "dfs": True, "lifo": True}  # name -> lifo
# Keyword arguments the engine passes to fetchers itself; Request.options may not override them.
RESERVED_OPTIONS = frozenset({"method", "url", "headers", "cookies", "data", "json", "proxy", "retries", "request"})


async def _deadline(coro: Any, seconds: float) -> Any:
    """Await ``coro`` with a hard time limit (cheap ``asyncio.timeout`` on 3.11+)."""
    timeout_cm = getattr(asyncio, "timeout", None)
    if timeout_cm is None:  # Python 3.10
        return await asyncio.wait_for(coro, seconds)
    async with timeout_cm(seconds):
        return await coro


class Stats(dict):  # type: ignore[type-arg]
    def inc(self, key: str, n: int | float = 1) -> None:
        self[key] = self.get(key, 0) + n


def _function_pipe(fn: Any) -> Any:
    """A plain ``item -> item`` function used as a pipeline."""

    def process_item(item: Any, spider: Spider) -> Any:
        return fn(item)

    return process_item


class Engine:
    """Runs one crawl. Created by :meth:`Spider.run` / :meth:`Spider.arun`."""

    def __init__(
        self,
        spider: Spider,
        *,
        install_signal_handlers: bool = False,
        item_queue: asyncio.Queue[Any] | None = None,
    ) -> None:
        self.spider = spider
        self.install_signal_handlers = install_signal_handlers
        self.item_queue = item_queue
        self.stats = Stats()
        self.items: list[Any] = []
        order = str(spider.crawl_order).lower()
        if order not in CRAWL_ORDERS:
            raise ConfigurationError(f"must be 'bfs' or 'dfs', not {spider.crawl_order!r}", key="crawl_order")
        self._lifo = CRAWL_ORDERS[order]
        self.scheduler: Scheduler | DiskScheduler = Scheduler(dedupe=spider.dedupe, lifo=self._lifo)
        self._persistent = False  # True with the disk frontier
        self.throttle: AutoThrottle = spider.throttle or AutoThrottle(
            enabled=spider.autothrottle,
            base_delay=spider.download_delay,
            max_delay=spider.max_delay,
            max_concurrency=spider.concurrency_per_domain,
        )
        self.proxies = ProxyRotator.coerce(spider.proxies)
        self.url_normalizer = URLNormalizer.coerce(spider.url_normalizer)
        self.url_rules = URLRules.coerce(spider.url_rules)
        self.adaptive = FetchStrategy.coerce(spider.adaptive_fetch)
        self.optimizer = CrawlOptimizer.coerce(spider.optimize, crawl_dir=spider.crawl_dir)
        registry = RunRegistry.coerce(spider.run_registry) or (RunRegistry() if spider.record else None)
        self.recorder = RunRecorder(registry, spider, record=bool(spider.record)) if registry is not None else None
        self._continued: set[int] = set()  # with the optimizer: attempts whose request goes on (a retry...)
        self.network_policy = spider.get_network_policy()
        self._policy_warned: set[str] = set()
        self.priority_fn = spider.priority_fn
        self.sessions = SessionManager()
        self.checkpoint = Checkpoint(spider.crawl_dir) if spider.crawl_dir else None
        self.exporter: Exporter | None = None
        self.robots: RobotsPolicy | None = None
        self._robots_fetcher: AsyncFetcher | None = None
        # observability
        self.events = spider.events
        self.metrics = CrawlMetrics()
        self.failures = FailureTracker()
        self.budget = BudgetMonitor(spider)
        self.dead_letters = self._dead_letter_queue()
        self._own_sinks: list[Any] = []  # event sinks and webhooks: closed at the end
        self._unsubscribe_sinks: list[Any] = []
        self._emit_response = False  # someone subscribed to high-volume events (re-checked periodically)
        self._emit_item = False
        self._min_priority: int | None = None  # set by budget_soft_limit
        self._output_budget = False  # max_output_bytes set, checked after every written item
        self._elapsed_final = False
        self._last_sample = 0.0
        self._last_metrics = 0.0  # when the run's metrics were last kept
        # extension points (bound methods of the hooks that exist, so unused ones cost nothing)
        self._mw_request: list[Any] = []
        self._mw_response: list[Any] = []
        self._mw_exception: list[Any] = []
        self._pipes: list[tuple[str, Any]] = []
        self._pipelines_open: list[Any] = []

        self._loop: asyncio.AbstractEventLoop | None = None
        self._wakeup: asyncio.Event | None = None
        self._inflight: dict[asyncio.Task[None], Request] = {}
        # Retries waiting to be queued: request, timer, and (disk frontier) the
        # original request whose acknowledgement waits until the retry is queued.
        self._delayed: dict[int, tuple[Request, asyncio.TimerHandle, Request | None]] = {}
        self._ack_deferred: set[int] = set()
        self._last_commit = 0.0
        self._stopping = False
        self._status = "finished"
        self._fatal: BaseException | None = None
        self._force = False
        self._sigints = 0
        self._crawl_delays_checked: set[str] = set()
        self._last_checkpoint = 0.0
        self._last_log = 0.0
        self._started = 0.0
        self._pending_stop: str | None = None
        self._state_ready = False
        self._limit_reached = False
        self._warned_options: set[str] = set()
        self._item_keys: set[bytes] = set()
        self._progress: ProgressDisplay | None = None
        self.history: Any = None  # a PageHistory while recording
        self._history_run: int | None = None
        self._own_history = False
        self.profiler: Any = None  # a SiteProfiler with ``profile``
        self._whole_crawl = False  # this run started the crawl (not a resume, not only dead letters)

    # ------------------------------------------------------------------ #
    # control (thread-safe entry points)
    # ------------------------------------------------------------------ #
    def request_stop(self, status: str = "stopped") -> None:
        if self._loop is None:
            self._pending_stop = status
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._begin_stop(status)
        else:
            self._loop.call_soon_threadsafe(self._begin_stop, status)

    def fail(self, error: BaseException) -> None:
        self._fatal = error
        self.request_stop("stopped")

    def force_stop(self) -> None:
        """Stop now: cancel in-flight requests (thread-safe). Pending work is still saved when pausing."""
        if self._loop is None:
            self._pending_stop = self._pending_stop or "stopped"
            return
        self._loop.call_soon_threadsafe(self._force_now)

    def _force_now(self) -> None:
        if not self._stopping:
            self._begin_stop("paused" if self.checkpoint is not None else "stopped")
        self._force = True
        self._wake()

    def _keeps_state(self) -> bool:
        """Whether unfinished requests will be saved for a later resume."""
        return self.checkpoint is not None and (self._status in ("paused", "limit") or self._fatal is not None)

    def _requeue(self, request: Request) -> None:
        if self.optimizer is not None:
            self._continued.add(id(request))
        self.scheduler.push(request.replace(dont_filter=True), force=True)

    def _begin_stop(self, status: str) -> None:
        if self._stopping:
            return
        if status == "paused" and self.checkpoint is None:
            log.warning("no crawl_dir set, so the crawl cannot be resumed; stopping instead")
            status = "stopped"
        self._stopping = True
        self._status = status
        verb = {"paused": "pausing", "limit": "limit reached; finishing", "stopped": "stopping"}.get(status, status)
        log.info("%s (%d request(s) in flight)...", verb, len(self._inflight))
        self._wake()

    def _wake(self) -> None:
        if self._wakeup is not None:
            self._wakeup.set()

    def _on_signal(self) -> None:
        self._sigints += 1
        if self._sigints == 1:
            status = "paused" if self.checkpoint is not None else "stopped"
            log.warning(
                "interrupt received: %s gracefully - press Ctrl+C again to force",
                "pausing" if status == "paused" else "stopping",
            )
            self._begin_stop(status)
        else:
            log.warning("forcing shutdown")
            # The main loop cancels in-flight tasks itself so it can re-queue
            # their requests before the state is saved.
            self._force_now()

    # ------------------------------------------------------------------ #
    # main
    # ------------------------------------------------------------------ #
    async def run(self, *, resume: bool = True) -> CrawlResult:
        spider = self.spider
        if spider.log_level:
            configure_logging(spider.log_level)
        self._loop = asyncio.get_running_loop()
        self._wakeup = asyncio.Event()
        self._started = time.monotonic()
        restore_signals = self._install_signals()
        try:
            state = self._load_state(resume)
            frontier_existed = self._open_frontier(resume)
            if self.recorder is not None:  # before the sessions: a recorded run has its own cache
                self.recorder.start(resumed=state is not None or frontier_existed)
            self._whole_crawl = state is None and not frontier_existed and not spider.retry_dead_letters
            # A frontier that survived a crash means earlier output is part of this crawl.
            self._setup_output(append=state is not None or frontier_existed)
            self.budget.start(append=state is not None or frontier_existed)
            self.budget.attach_output(self.exporter)
            self._output_budget = "max_output_bytes" in self.budget.limits and self.exporter is not None
            self._setup_events()
            for setting in spider.webhooks or ():
                hook = Webhook.coerce(setting)
                self._own_sinks.append(hook)  # closed (what waits, delivered) at the end
                self._unsubscribe_sinks.append(self.events.subscribe(hook, hook.kinds()))
            if self.recorder is not None and self.recorder.run is not None:
                sink = JsonlEventSink(self.recorder.run.directory / "events.jsonl")
                self._own_sinks.append(sink)
                self._unsubscribe_sinks.append(self.events.subscribe(sink, self.recorder.event_kinds()))
            self._bind_extensions()
            spider.configure_sessions(self.sessions)
            if not len(self.sessions):
                raise RuntimeError("configure_sessions() registered no sessions")
            if self.adaptive is not None and not {"http", "browser"} <= set(self.sessions):
                log.warning("adaptive_fetch needs an 'http' and a 'browser' session; fetching as configured")
                self.adaptive = None
            if spider.obey_robots_txt:
                self._robots_fetcher = AsyncFetcher(
                    impersonate=spider.impersonate,
                    timeout=15,
                    retries=1,
                    verify=spider.verify,
                    cache=spider.http_cache(),
                    network_policy=self.network_policy,
                )
                self.robots = RobotsPolicy(self._fetch_robots, spider.robots_user_agent)
            await maybe_await(spider.on_start())
            await self._open_pipelines()
            self._open_history(state)
            self._open_profiler()
            if state is not None:
                self._restore(state)
            elif spider.retry_dead_letters:
                self.stats["runs"] = 1  # only the dead letters: the rest of the crawl already succeeded
            else:
                if self.dead_letters is not None and not frontier_existed:
                    self.dead_letters.clear()  # a new crawl: the old failures are history
                await self._seed()
            self._requeue_dead_letters()
            # Only now may shutdown save/clear the checkpoint: a failure above
            # must leave an existing state file untouched.
            self._state_ready = True
            if isinstance(self.scheduler, DiskScheduler):
                # The frontier starts recording acks right away, so the state it
                # belongs to (stats, output mode) must exist from the start too.
                self._save_state([])
                self._last_checkpoint = time.monotonic()
            if self._pending_stop:
                self._begin_stop(self._pending_stop)
            log.info(
                "%s %s: %d request(s) queued",
                "resuming" if state is not None else "starting",
                spider.name,
                len(self.scheduler),
            )
            self.events.emit("crawl_started", spider=spider.name, resumed=state is not None, queued=len(self.scheduler))
            show = spider.progress if spider.progress is not None else (spider.log_level is not None)
            if show and ProgressDisplay.supported():
                self._progress = ProgressDisplay(self)
                self._progress.start()
            await self._loop_until_done()
        except BaseException as exc:
            if self._fatal is None and not isinstance(exc, asyncio.CancelledError):
                self._fatal = exc
            if isinstance(exc, asyncio.CancelledError):
                self._status = "paused" if self.checkpoint is not None else "stopped"
                await self._cancel_inflight()
            if not isinstance(exc, Exception):
                raise
        finally:
            restore_signals()
            result = await self._shutdown()
        if self._fatal is not None:
            raise self._fatal
        return result

    async def _loop_until_done(self) -> None:
        assert self._wakeup is not None
        while True:
            if not self._stopping:
                wait = self._dispatch()
            else:
                wait = None
                if self._force:
                    await self._cancel_inflight()
            if self._stopping and not self._inflight:
                break
            if not self._stopping and not self._inflight and not self._delayed:
                if not len(self.scheduler):
                    self._status = "finished"
                    break
                if self._limit_reached and not self.scheduler.retry_count:
                    self._status = "limit"
                    break
            self._periodic()
            timeout = 1.0 if wait is None else max(0.01, min(wait, 1.0))
            if not self._wakeup.is_set():
                # A timer instead of asyncio.wait_for: no extra task per loop turn.
                assert self._loop is not None
                timer = self._loop.call_later(timeout, self._wakeup.set)
                try:
                    await self._wakeup.wait()
                finally:
                    timer.cancel()
            self._wakeup.clear()

    def _dispatch(self) -> float | None:
        spider = self.spider
        now = time.monotonic()
        wait: float | None = None
        while len(self._inflight) < spider.concurrency:
            if self.budget.active and self._counter_budget_hit():
                break
            if spider.max_pages is not None and self.stats.get("pages", 0) >= spider.max_pages:
                # No new pages, but let retries of pages already started finish.
                if not self._limit_reached:
                    self._limit_reached = True
                    log.info("max_pages (%d) reached; finishing in-flight requests and their retries", spider.max_pages)
                if not self.scheduler.retry_count:
                    break
                request, wait = self.scheduler.pop_ready(self.throttle, now, retries_only=True)
            elif self._min_priority is not None:
                request, wait = self.scheduler.pop_ready(self.throttle, now, min_priority=self._min_priority)
            else:
                request, wait = self.scheduler.pop_ready(self.throttle, now)
            if request is None:
                break
            if self.optimizer is not None and self._drop_unneeded(request):
                continue
            slot = self.throttle.slot(request.host)
            self.throttle.on_start(slot, now)
            if request.retries == 0:
                self.stats.inc("pages")
            self.stats.inc("requests")
            task = asyncio.create_task(self._process(request, slot))
            self._inflight[task] = request
            task.add_done_callback(self._task_done)
        return wait

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._inflight.pop(task, None)
        if not task.cancelled() and task.exception() is not None:  # pragma: no cover - _process catches
            log.error("internal error: %s", describe(task.exception()))
        self._wake()

    async def _cancel_inflight(self) -> None:
        # Snapshot first: done-callbacks remove tasks from _inflight while we wait.
        cancelled = dict(self._inflight)
        for task in cancelled:
            task.cancel()
        for task, request in cancelled.items():
            try:
                await task
            except BaseException:
                pass
            if self._keeps_state():
                self._requeue(request)
            self._inflight.pop(task, None)

    # ------------------------------------------------------------------ #
    # setup / teardown
    # ------------------------------------------------------------------ #
    def _install_signals(self) -> Callable[[], None]:
        if not self.install_signal_handlers:
            return lambda: None
        loop = self._loop
        assert loop is not None
        installed: list[Any] = []
        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if sig is None:
                continue
            try:
                previous = signal.getsignal(sig)
                loop.add_signal_handler(sig, self._on_signal)
                installed.append(("loop", sig, previous))
            except (NotImplementedError, RuntimeError, ValueError):
                try:
                    previous = signal.signal(sig, lambda *_: loop.call_soon_threadsafe(self._on_signal))
                    installed.append(("signal", sig, previous))
                except (ValueError, OSError):  # pragma: no cover - not main thread
                    pass

        def restore() -> None:
            for entry in installed:
                try:
                    if entry[0] == "loop":
                        loop.remove_signal_handler(entry[1])
                    if entry[2] is not None:
                        signal.signal(entry[1], entry[2])
                except Exception:  # pragma: no cover
                    pass

        return restore

    def _load_state(self, resume: bool) -> dict[str, Any] | None:
        if self.checkpoint is None:
            return None
        if not resume:
            self.checkpoint.clear()
            return None
        state = self.checkpoint.load()
        if state is not None and state.get("spider") not in (None, self.spider.name):
            raise CheckpointError(
                f"{self.checkpoint.path} belongs to spider {state.get('spider')!r}, not {self.spider.name!r}"
            )
        if state is not None and state.get("frontier") == "disk" and self.spider.frontier != "disk":
            raise CheckpointError(
                f"{self.checkpoint.dir} was saved with frontier='disk'; resume it with frontier='disk' too"
            )
        return state

    def _open_frontier(self, resume: bool) -> bool:
        """Open the disk frontier if configured. Returns whether an existing one was reopened."""
        kind = self.spider.frontier
        if kind == "memory":
            return False
        if kind != "disk":
            raise ValueError(f"frontier must be 'memory' or 'disk', not {kind!r}")
        if self.checkpoint is None:
            raise ValueError("frontier='disk' needs a crawl_dir to keep the queue in")
        path = self.checkpoint.dir / "frontier.sqlite3"
        if not resume:
            for suffix in ("", "-wal", "-shm"):
                path.with_name(path.name + suffix).unlink(missing_ok=True)
        existed = path.exists()
        # The engine commits itself (after flushing output), so disable the
        # frontier's own commit clock: acks must never become durable before
        # the items produced for them.
        self.scheduler = DiskScheduler(
            path,
            self.spider,
            dedupe=self.spider.dedupe,
            commit_every=1 << 62,
            commit_interval=float("inf"),
            lifo=self._lifo,
        )
        self._persistent = True
        return existed

    def _setup_output(self, append: bool) -> None:
        if self.spider.output:
            self.exporter = open_exporter(self.spider.output, append=append, unique_key=self.spider.unique_key)

    def _open_history(self, state: dict[str, Any] | None) -> None:
        """Start recording into the history (or continue the run a resumed crawl began)."""
        setting = self.spider.history
        if not setting:
            if self.spider.skip_fresh:
                raise ConfigurationError("needs a history to know which pages are fresh", key="skip_fresh")
            return
        from ..history import PageHistory

        if isinstance(setting, PageHistory):
            self.history = setting
        else:
            self.history = PageHistory(setting, keep_html=self.spider.history_html)
            self._own_history = True
        run = (state or {}).get("history_run")
        if run is not None and any(r.id == run for r in self.history.runs(self.spider.name)):
            self._history_run = int(run)  # a resumed crawl continues its run
        else:
            self._history_run = self.history.start_run(self.spider.name)

    def _open_profiler(self) -> None:
        setting = self.spider.profile
        if not setting:
            return
        from ..intel.profile import SiteProfiler

        self.profiler = setting if isinstance(setting, SiteProfiler) else SiteProfiler()

    def _profile_page(self, response: Response, latency: float | None) -> None:
        try:
            self.profiler.observe(response, latency=latency)
        except Exception as exc:  # profiling must never break a crawl
            self.stats.inc("profile_errors")
            log.error("could not profile %s: %s", response.url, describe(exc))

    def _close_profiler(self, status: str) -> Any:
        if self.profiler is None:
            return None
        try:
            self.profiler.topology.start_urls(str(url) for url in self.spider.start_urls)
            if self.profiler.robots is None and self.robots is not None and self.spider.start_urls:
                origin = RobotsPolicy.origin(str(self.spider.start_urls[0]))
                if origin in self.robots.texts:
                    text = self.robots.texts[origin]
                    self.profiler.add_robots(text, found=text is not None)
            if self.history is not None and self._history_run is not None and self.profiler.change_frequency is None:
                self.profiler.change_frequency = self.history.change_frequency(self._history_run)
            # A crawl that ran to the end in this run followed every link it could, so a sitemap page none
            # points to is an orphan. (A resumed crawl's profile has only the pages of the last run.)
            profile = self.profiler.profile(complete=status == "finished" and self._whole_crawl)
            if isinstance(self.spider.profile, (str, Path)):
                Path(self.spider.profile).write_text(
                    json.dumps(profile.to_dict(), indent=2, ensure_ascii=False, default=str), encoding="utf-8"
                )
            return profile
        except Exception as exc:
            log.error("could not build the site profile: %s", describe(exc))
            return None

    # The history must never break a crawl: its errors are counted and logged.
    def _history_failed(self, what: str, exc: Exception) -> None:
        self.stats.inc("history_errors")
        log.error("history: could not %s: %s", what, describe(exc))

    def _record_page(self, response: Response, items: list[Any] | None) -> None:
        try:
            self.history.observe(self._history_run, response, items=items)
        except Exception as exc:
            self._history_failed(f"record {response.url}", exc)

    def _record_status(self, url: str, status: int) -> None:
        try:
            self.history.observe_status(self._history_run, url, status)
        except Exception as exc:
            self._history_failed(f"record {url}", exc)

    def _still_fresh(self, request: Request) -> bool:
        """With ``skip_fresh``: the page has probably not changed since it was last fetched."""
        try:
            if self.history.due(request.url):
                return False
            self.history.mark_skipped(self._history_run, request.url)
            return True
        except Exception as exc:
            self._history_failed(f"check {request.url}", exc)
            return False  # when in doubt, fetch

    def _restore(self, state: dict[str, Any]) -> None:
        self.stats.update(state.get("stats") or {})
        self.stats.inc("runs")
        self.scheduler.restore_seen(state.get("seen") or ())
        self._item_keys.update(state.get("item_keys") or ())
        self.throttle.restore(state.get("throttle") or {})
        pending = list(state.get("pending") or ())
        if self._lifo:
            pending.reverse()  # saved newest first: re-queue oldest first so the newest stays on top
        for data in pending:
            self.scheduler.push(Request.from_dict(data, self.spider), force=True)

    def _dead_letter_queue(self) -> DeadLetterQueue | None:
        setting = self.spider.dead_letters
        if setting is None or setting is False:
            return None
        if setting is True:
            return DeadLetterQueue(self.checkpoint.dir / "dead_letters.jsonl") if self.checkpoint else None
        return DeadLetterQueue(setting)

    def _requeue_dead_letters(self) -> None:
        """``retry_dead_letters``: queue the requests an earlier run gave up on, and start a fresh file."""
        if not self.spider.retry_dead_letters:
            return
        if self.dead_letters is None:
            raise ConfigurationError("needs dead letters (a crawl_dir, or dead_letters=PATH)", key="retry_dead_letters")
        requests = self.dead_letters.requests(self.spider)
        for request in requests:
            self._check_serializable(request)
            self.scheduler.push(request, force=True)
        self.dead_letters.clear()
        self.stats.inc("dead_letters_retried", len(requests))
        log.info("queued %d request(s) from the dead-letter queue again", len(requests))

    def _setup_events(self) -> None:
        setting = self.spider.event_log
        if setting is None or setting is False:
            return
        if setting is True:
            if self.checkpoint is None:
                raise ConfigurationError("event_log=True needs a crawl_dir (or pass a file path)", key="event_log")
            path: Any = self.checkpoint.dir / "events.jsonl"
        else:
            path = setting
        sink = JsonlEventSink(path)
        self._own_sinks.append(sink)
        self._unsubscribe_sinks = [self.events.subscribe(sink)]

    def _bind_extensions(self) -> None:
        """Collect middleware and pipeline hooks once (so crawling without them costs nothing)."""
        spider = self.spider
        middlewares = list(spider.middlewares or ())
        for mw in middlewares:
            if not any(hasattr(mw, h) for h in ("process_request", "process_response", "process_exception")):
                raise ConfigurationError(
                    f"{mw!r} has no process_request/process_response/process_exception method", key="middlewares"
                )
        self._mw_request = [mw.process_request for mw in middlewares if hasattr(mw, "process_request")]
        # Responses and errors travel back through the middlewares in reverse order.
        self._mw_response = [mw.process_response for mw in reversed(middlewares) if hasattr(mw, "process_response")]
        self._mw_exception = [mw.process_exception for mw in reversed(middlewares) if hasattr(mw, "process_exception")]
        pipes = []
        for pipe in spider.pipelines or ():
            hook = getattr(pipe, "process_item", None)
            if hook is None:
                if callable(pipe):  # a plain function item -> item
                    pipes.append((getattr(pipe, "__name__", type(pipe).__name__), _function_pipe(pipe)))
                    continue
                raise ConfigurationError(f"{pipe!r} has no process_item method", key="pipelines")
            pipes.append((type(pipe).__name__, hook))
        self._pipes = pipes
        self._refresh_subscriptions()

    def _refresh_subscriptions(self) -> None:
        self._emit_response = self.events.wants("response")
        self._emit_item = self.events.wants("item_scraped")

    async def _open_pipelines(self) -> None:
        for pipe in self.spider.pipelines or ():
            opener = getattr(pipe, "open_spider", None)
            if opener is not None:
                await maybe_await(opener(self.spider))
            self._pipelines_open.append(pipe)

    async def _close_pipelines(self) -> None:
        for pipe in reversed(self._pipelines_open):
            closer = getattr(pipe, "close_spider", None)
            if closer is None:
                continue
            try:
                await maybe_await(closer(self.spider))
            except Exception as exc:
                log.error("pipeline %s failed to close: %s", type(pipe).__name__, describe(exc))
        self._pipelines_open.clear()

    async def _seed(self) -> None:
        self.stats["runs"] = 1
        produced = self.spider.start_requests()
        if isinstance(produced, AsyncIterable):
            async for entry in produced:
                self._seed_one(entry)
        elif produced is not None:
            for entry in produced:
                self._seed_one(entry)

    def _seed_one(self, entry: Request | str) -> None:
        request = Request(ensure_scheme(entry)) if isinstance(entry, str) else entry
        if not isinstance(request, Request):
            raise TypeError(f"start_requests() must yield Request objects or URLs, got {entry!r}")
        if self.url_normalizer is not None:
            request.url = self.url_normalizer(request.url)
        request.meta.setdefault("depth", 0)
        if self.priority_fn is not None:
            request.priority = int(self.priority_fn(request))
        if self.optimizer is not None:
            self.optimizer.enqueued(request, None)
        self._check_serializable(request)
        if not self.scheduler.push(request) and self.optimizer is not None:
            self.optimizer.discard(request)

    async def _shutdown(self) -> CrawlResult:
        spider = self.spider
        if self._progress is not None:
            self._progress.stop()
            self._progress = None
        elapsed = time.monotonic() - self._started
        self.stats["elapsed_seconds"] = round(self.stats.get("elapsed_seconds", 0) + elapsed, 3)
        self._elapsed_final = True
        self.stats["duplicates_filtered"] = self.stats.get("duplicates_filtered", 0) + self.scheduler.duplicates
        self.scheduler.duplicates = 0
        status = self._status if self._fatal is None else "stopped"
        disk = self.scheduler if isinstance(self.scheduler, DiskScheduler) else None
        pending: list[Request] = [] if disk is not None else self._pending_requests()
        pending_count = (
            len(self.scheduler) + len(self._delayed) + len(self._inflight) if disk is not None else len(pending)
        )
        keep = status in ("paused", "limit") or self._fatal is not None
        if self.checkpoint is not None and self._state_ready:
            try:
                if keep and pending_count:
                    # Paused, stopped at a limit, or crashed: keep the queue so the
                    # crawl can continue (after raising the limit / fixing the bug).
                    if disk is not None:
                        self._flush_delayed_to_frontier()
                    self._save_state(pending)
                    hint = "raise the limit and run again to continue" if status == "limit" else "run again to resume"
                    log.info("saved %d pending request(s) to %s; %s", pending_count, self.checkpoint.dir, hint)
                else:
                    self.checkpoint.clear()
                    if status == "paused":
                        status = "finished"
                self.checkpoint.write_summary({"status": status, "stats": dict(self.stats)})
            except CheckpointError as exc:
                log.error("%s", exc)
                if self._fatal is None:
                    self._fatal = exc
        elif pending_count and status != "finished":
            log.info("%d pending request(s) discarded", pending_count)
        for entry in self._delayed.values():
            entry[1].cancel()
        self._delayed.clear()
        if disk is not None:
            keep_frontier = keep and pending_count and self._state_ready
            disk.close()
            if not keep_frontier and self._state_ready and self.checkpoint is not None:
                path = Path(disk.path)
                for suffix in ("", "-wal", "-shm"):
                    path.with_name(path.name + suffix).unlink(missing_ok=True)
        await self._close_pipelines()
        if self.exporter is not None:
            self.exporter.close()
        await self.sessions.close_all()
        if self._robots_fetcher is not None:
            await self._robots_fetcher.aclose()
        cache = spider.http_cache()
        if cache is not None:
            self.stats["cache"] = dict(cache.stats)
            if not isinstance(spider.cache, HTTPCache):  # we created it, so we close it
                cache.close()
                spider._http_cache = None
        self.stats["status"] = status
        if self.dead_letters is not None and self.dead_letters.added:
            self.stats["dead_letters"] = self.dead_letters.added
        profile = self._close_profiler(status)
        changes = self._close_history(status)
        strategy = self._close_adaptive()
        optimizer = self._close_optimizer()
        result = CrawlResult(
            items=self.items,
            stats=dict(self.stats),
            status=status,
            crawl_dir=spider.crawl_dir,
            failures=self.failures.diagnose(self.throttle),
            metrics=self.snapshot(),
            changes=changes,
            profile=profile,
            fetch_strategy=strategy,
            optimizer=optimizer,
        )
        if self.recorder is not None and self.recorder.run is not None:
            self.recorder.finish(result, status=status, error=self._fatal)
            result.run_id = self.recorder.run.id
        self._log_progress(final=True)
        try:
            await maybe_await(spider.on_close(result))
        except Exception as exc:
            log.error("on_close failed: %s", describe(exc))
        self.events.emit(
            "crawl_finished",
            spider=spider.name,
            status=status,
            limit_reason=self.stats.get("limit_reason"),
            stats=dict(self.stats),
        )
        await self.events.drain()
        for unsubscribe in self._unsubscribe_sinks:
            unsubscribe()
        for sink in self._own_sinks:
            if isinstance(sink, Webhook):
                await asyncio.to_thread(sink.close)  # the last deliveries, without holding up the loop
            else:
                sink.close()
        return result

    def _close_history(self, status: str) -> Any:
        """Finish the history run and report what changed since the previous one."""
        if self.history is None or self._history_run is None:
            return None
        report = None
        try:
            self.history.finish_run(self._history_run, status, dict(self.stats))
            if status != "paused":  # a paused run continues when the crawl resumes
                report = self.history.compare(new=self._history_run)
                self.stats["changes"] = report.counts()
                if report.old is None:
                    log.info("history: first run of %s recorded (%d pages)", self.spider.name, len(report.added))
                else:
                    log.info("changes since run %d:\n%s", report.old.id, report.summary())
                self.events.emit("changes_detected", run=self._history_run, kinds=report.kinds(), **report.counts())
                if report.old is not None:  # a first run is the baseline: nothing "changed"
                    self._emit_changes(report)
        except Exception as exc:
            log.error("could not finish the history run: %s", describe(exc))
        finally:
            if self._own_history:
                self.history.close()
        return report

    def _emit_changes(self, report: Any) -> None:
        """``site_changed`` when something did, and (for those who listen) one event per page."""
        events = self.events
        if report.added or report.removed or report.modified:
            events.emit("site_changed", run=self._history_run, previous=report.old.id, **report.counts())
        if events.wants("record_created"):
            for url in report.added:
                events.emit("record_created", url=url)
        if events.wants("record_updated"):
            for change in report.modified:
                events.emit("record_updated", **change.to_dict())
        if events.wants("record_deleted"):
            for url in report.removed:
                events.emit("record_deleted", url=url)

    def snapshot(self) -> dict[str, Any]:
        """Live metrics (see :mod:`wintergrab.spider.metrics`)."""
        elapsed = self._elapsed_total()
        return self.metrics.snapshot(
            self.stats,
            throttle=self.throttle,
            queued=len(self.scheduler),
            in_flight=len(self._inflight),
            elapsed=time.monotonic() - self._started if self._started else 0.0,
            budget={name: st.to_dict() for name, st in self.budget.usage(self.stats, elapsed).items()},
        )

    def _elapsed_total(self) -> float:
        """Crawl time including earlier (resumed) runs."""
        if self._elapsed_final:
            return float(self.stats.get("elapsed_seconds", 0))
        current = time.monotonic() - self._started if self._started else 0.0
        return float(self.stats.get("elapsed_seconds", 0)) + current

    def _pending_requests(self) -> list[Request]:
        pending = self.scheduler.pending()
        pending.extend(entry[0] for entry in self._delayed.values())
        pending.extend(req.replace(dont_filter=True) for req in self._inflight.values())
        return pending

    def _flush_delayed_to_frontier(self) -> None:
        """Queue waiting retries now (disk frontier) so they are part of the saved queue."""
        for request, timer, original in list(self._delayed.values()):
            timer.cancel()
            self.scheduler.push(request, force=True)
            if original is not None:
                self._ack_deferred.discard(id(original))
                self._ack(original)
        self._delayed.clear()

    def _ack(self, request: Request) -> None:
        if isinstance(self.scheduler, DiskScheduler):
            self.scheduler.ack(request)

    def _save_state(self, pending: list[Request]) -> None:
        assert self.checkpoint is not None
        if self.exporter is not None:
            self.exporter.flush()  # items on disk must match the saved queue
        state: dict[str, Any] = {
            "spider": self.spider.name,
            "stats": dict(self.stats),
            "throttle": self.throttle.snapshot(),
            "item_keys": self._item_keys,
        }
        if self._history_run is not None:
            state["history_run"] = self._history_run
        if isinstance(self.scheduler, DiskScheduler):
            # The queue and seen-filter live in the frontier database already.
            self.scheduler.commit()
            self._last_commit = time.monotonic()
            state["frontier"] = "disk"
        else:
            state["pending"] = [r.to_dict(self.spider) for r in pending]
            state["seen"] = self.scheduler.seen
        self.checkpoint.save(state)

    def _counter_budget_hit(self) -> bool:
        """Checked before each request starts: stop once a counter budget is used up."""
        exhausted = self.budget.check_counters(self.stats)
        if exhausted is None:
            return False
        self._budget_exhausted(exhausted)
        return True

    def _budget_exhausted(self, status: BudgetStatus) -> None:
        if self._stopping:
            return
        self.stats["limit_reason"] = status.name
        log.info("budget %s exhausted (%s of %s); finishing", status.name, status.used, status.limit)
        self.events.emit("budget_exhausted", budget=status.name, used=status.used, limit=status.limit)
        self._begin_stop("limit")

    def _periodic(self) -> None:
        now = time.monotonic()
        spider = self.spider
        if now - self._last_sample >= 1.0:
            self._last_sample = now
            self.metrics.sample(self.stats, self.throttle, now)
            self._refresh_subscriptions()
            if self.budget.active or self.budget.soft_limit is not None:
                elapsed = self._elapsed_total()
                exhausted = self.budget.check_resources(elapsed) or self.budget.check_counters(self.stats)
                if exhausted is not None:
                    self._budget_exhausted(exhausted)
                previous, self._min_priority = self._min_priority, self.budget.min_priority(self.stats, elapsed)
                if self._min_priority is not None and previous is None:
                    log.info(
                        "budget %.0f%% used: only starting requests with priority >= %d",
                        (self.budget.soft_limit or 0) * 100,
                        self._min_priority,
                    )
        if isinstance(self.scheduler, DiskScheduler) and now - self._last_commit >= 1.0:
            # Output first, then the queue: a crash may redo work, never lose it.
            if self.exporter is not None:
                self.exporter.flush()
            self.scheduler.commit()  # bounds what a crash can lose to about a second of work
            self._last_commit = now
        if self.checkpoint is not None and now - self._last_checkpoint >= spider.checkpoint_interval:
            if self._last_checkpoint:
                try:
                    self._save_state([] if self._persistent else self._pending_requests())
                except CheckpointError as exc:
                    log.error("%s", exc)
            self._last_checkpoint = now
        if now - self._last_log >= spider.log_interval:
            if self._last_log and self._progress is None:
                self._log_progress()
            self._last_log = now
        if self.recorder is not None and now - self._last_metrics >= 2.0:
            self._last_metrics = now
            self.recorder.metrics(self.snapshot())  # for the dashboard, while the crawl runs

    def _log_progress(self, final: bool = False) -> None:
        s = self.stats
        elapsed = s.get("elapsed_seconds", 0) if final else time.monotonic() - self._started
        rate = s.get("pages", 0) / elapsed * 60 if elapsed else 0.0
        expected = ""
        if self.optimizer is not None and not final:
            forecast = self.optimizer.forecast()
            expected = f", ~{forecast['items']:.0f} more items expected, {self.optimizer.skipped} skipped"
        log.info(
            "%s%d pages (%.0f/min), %d items, %d retries, %d errors, %d queued, %d in flight%s",
            "done: " if final else "",
            s.get("pages", 0),
            rate,
            s.get("items", 0),
            s.get("retries", 0),
            s.get("errors", 0),
            len(self.scheduler),
            len(self._inflight),
            expected,
        )

    # ------------------------------------------------------------------ #
    # one request
    # ------------------------------------------------------------------ #
    async def _fetch_robots(self, url: str) -> Response:
        assert self._robots_fetcher is not None
        proxy = self.proxies.next() if self.proxies is not None else None
        return await self._robots_fetcher.get(url, proxy=proxy)

    async def _process(self, request: Request, slot: Any) -> None:
        spider = self.spider
        domain = slot.domain
        try:
            if self.network_policy is not None:
                try:
                    await self.network_policy.check(
                        request.url, proxied=request.proxy is not None or self.proxies is not None
                    )
                except NetworkPolicyError as exc:
                    await self._refused(request, exc)
                    return
                except FetchError:
                    pass  # e.g. DNS trouble: the fetch reports (and retries) it
            if self.robots is not None and not await self._robots_allow(request, domain):
                return
            if spider.skip_fresh and request.depth > 0 and self._still_fresh(request):
                self.stats.inc("history_skipped")
                return
            response: Response | None = None
            if self._mw_request:
                outcome = await self._request_middleware(request)
                if outcome is _HANDLED:
                    return
                response = outcome
            fetcher: Any = None
            proxy: str | None = None
            rotated = False
            fetched = response is None  # False: a middleware supplied the response
            started = time.monotonic()
            if response is None:
                session = self._session_for(request)
                try:
                    fetcher = self.sessions.get(session)
                except LookupError as exc:
                    self._requeue(request)
                    self.fail(exc)
                    return
                proxy, rotated = self._pick_proxy(request)
                options = self._fetch_options(request)
                if request.meta.get("adaptive") == "browser" and session == "browser" and self.adaptive is not None:
                    options = {**self.adaptive.browser_options(), **options}  # for this attempt only
                timeout = options.pop("timeout", spider.timeout)
                started = time.monotonic()
                try:
                    response = await _deadline(
                        fetcher.request(
                            request.method,
                            request.url,
                            headers=request.headers,
                            cookies=request.cookies,
                            data=request.data,
                            json=request.json,
                            proxy=proxy,
                            timeout=timeout,
                            retries=0,
                            request=request,
                            **options,
                        ),
                        max(60.0, timeout * 4),
                    )
                except asyncio.CancelledError:
                    raise
                except BrowserNotAvailable as exc:
                    if self.adaptive is not None and request.meta.get("adaptive") in ("browser", "rendered"):
                        self._no_browser(request, exc)
                        return
                    self._requeue(request)
                    self.fail(exc)
                    return
                except NetworkPolicyError as exc:  # a redirect hop, or DNS rebinding
                    await self._refused(request, exc)
                    return
                except Exception as exc:
                    error = FetchTimeout(request.url, "Hard timeout") if isinstance(exc, asyncio.TimeoutError) else exc
                    if self._mw_exception:
                        outcome = await self._exception_middleware(request, error)
                        if outcome is _HANDLED:
                            return
                        response = outcome
                    if response is None:
                        await self._fetch_failed(request, error, domain, proxy, rotated)
                        return
                    fetched = False  # a middleware recovered with a response

            latency = time.monotonic() - started
            if self._mw_response:
                outcome = await self._response_middleware(request, response)
                if outcome is _HANDLED:
                    return
                response = outcome
            if not fetched:
                self.stats.inc("middleware_responses")
                slot.next_start = min(slot.next_start, time.monotonic())  # nothing reached the site
            elif response.cache_status == "hit":
                # Served from disk: no request reached the site, so don't make
                # the next one wait for this one's politeness delay.
                self.stats.inc("cache_hits")
                slot.next_start = min(slot.next_start, time.monotonic())
            elif response.cache_status == "revalidated":
                self.stats.inc("cache_revalidated")
            live = fetched and response.cache_status != "hit"
            self.stats.inc("responses")
            self.stats.inc(f"status/{response.status}")
            self.stats.inc("bytes", len(response.body))
            if live:
                self.metrics.observe_latency(latency)
                if response.source == "browser":
                    self.stats.inc("browser_pages")
            # adaptive fetching: a page whose HTML is not enough is fetched again in the browser
            render = self._adaptive_check(request, response) if self.adaptive is not None else None
            if self.profiler is not None and render is None:
                self._profile_page(response, latency if live else None)
            if self._emit_response:
                self.events.emit(
                    "response",
                    url=response.url,
                    status=response.status,
                    bytes=len(response.body),
                    latency=round(latency, 4),
                    source=response.source,
                    cache=response.cache_status,
                )

            if (
                spider.allowed_domains
                and response.url != request.url
                and domain_matches(request.host, spider.allowed_domains)
                and not domain_matches(host_of(response.url), spider.allowed_domains)
            ):
                # An allowed page redirected somewhere else (possibly an internal
                # address): never hand what it led to to the callbacks.
                self.stats.inc("offsite_redirects")
                log.info("dropped %s: it redirected off the allowed domains to %s", request.url, response.url)
                return

            blocked = False
            try:
                blocked = bool(spider.is_blocked(response))
            except Exception as exc:
                log.error("is_blocked() failed: %s", describe(exc))
            if blocked:
                self.stats.inc("blocked")
                self.events.emit("blocked", url=request.url, status=response.status, domain=domain)
            if (blocked or response.status in spider.retry_statuses) and response.cache_status is not None:
                self._uncache(fetcher, request)  # never replay a block page or an error from the cache
            retry_after = parse_retry_after(response.headers.get("retry-after"), cap=600)
            if blocked or response.status in PUSHBACK_STATUSES:
                self.throttle.on_pushback(domain, retry_after)
                self.stats.inc("backoffs")
                self.events.emit(
                    "throttle_backoff",
                    domain=domain,
                    delay=round(slot.delay, 3),
                    concurrency=slot.concurrency,
                    retry_after=retry_after,
                )
            elif live:
                self.throttle.on_success(domain, latency)
            if rotated:  # a block or a rate limit is the site's answer, not the proxy's failure
                bad = response.status in PROXY_FAILURE_STATUSES
                (self.proxies.report_failure if bad else self.proxies.report_success)(proxy)  # type: ignore[union-attr]
            if render is not None:
                self._render(request, render)
                return

            retryable = blocked or response.status in spider.retry_statuses
            if retryable:
                reason = "blocked" if blocked else f"HTTP {response.status}"
                same_proxy = proxy if rotated and response.status not in PROXY_FAILURE_STATUSES else None
                retried = self._retry(request, reason, retry_after=retry_after, proxy=same_proxy)
                self.failures.failure(domain, request.url, response=response, blocked=blocked, final=not retried)
                if retried:
                    return
            if response.source == "browser" and response.cookie_jar and spider.share_browser_cookies:
                self._share_cookies(response)
            ok_status = 200 <= response.status < 300 or response.status in spider.allowed_statuses
            if blocked and response.status not in spider.allowed_statuses:
                ok_status = False
            if not ok_status:
                self.stats.inc("http_errors")
                if self.history is not None:
                    self._record_status(response.url, response.status)
                if not retryable:  # retryable ones were recorded above
                    self.failures.failure(domain, request.url, response=response, final=True)
                detail = "looks like a bot-check page" if blocked else None
                await self._give_up(request, HTTPStatusError(response, detail), quiet=response.status == 404)
                return
            self.failures.success(domain)
            await self._run_callback(request, response)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - safety net
            log.exception("unexpected error processing %s: %s", request.url, exc)
            self.stats.inc("errors")
        finally:
            self.throttle.on_finish(slot)
            if self._persistent and id(request) not in self._ack_deferred:
                self._ack(request)  # children are queued by now: safe to forget the request
            if self.optimizer is not None:
                if id(request) in self._continued:
                    self._continued.discard(id(request))  # retried, rendered, requeued: not finished
                else:
                    self.optimizer.done(request)
            self._wake()

    async def _fetch_failed(
        self, request: Request, error: BaseException, domain: str, proxy: str | None, rotated: bool
    ) -> None:
        self.stats.inc("errors")
        if isinstance(error, FetchError):
            if rotated and error.retryable and self.proxies is not None:
                self.proxies.report_failure(proxy)
            if error.is_timeout:
                self.throttle.on_error(domain)
            self.stats.inc(f"error/{type(error.cause).__name__ if error.cause else 'Timeout'}")
            retried = error.retryable and self._retry(request, describe(error))
            self.failures.failure(domain, request.url, error=error, final=not retried)
            if not retried:
                # offline, a page not in the cache is information (a replay went further), not an alarm
                await self._give_up(request, error, quiet=isinstance(error, CacheMiss))
            return
        self.stats.inc(f"error/{type(error).__name__}")
        self.failures.failure(domain, request.url, error=error, final=True)
        await self._give_up(request, error)

    # ------------------------------------------------------------------ #
    # middleware
    # ------------------------------------------------------------------ #
    async def _request_middleware(self, request: Request) -> Any:
        """``None`` (download it), a :class:`Response` (skip the download) or ``_HANDLED``."""
        try:
            for hook in self._mw_request:
                result = await maybe_await(hook(request, self.spider))
                if result is None:
                    continue
                if isinstance(result, Request):
                    self._reroute(result, request)
                    return _HANDLED
                if isinstance(result, Response):
                    return result
                raise TypeError(
                    f"process_request must return None, a Response or a Request, not {type(result).__name__}"
                )
        except IgnoreRequest as exc:
            self._ignored(request, exc)
            return _HANDLED
        except Exception as exc:
            await self._middleware_failed(request, exc)
            return _HANDLED
        return None

    async def _response_middleware(self, request: Request, response: Response) -> Any:
        """The (possibly changed) :class:`Response`, or ``_HANDLED``."""
        try:
            for hook in self._mw_response:
                result = await maybe_await(hook(request, response, self.spider))
                if isinstance(result, Request):
                    self._reroute(result, request)
                    return _HANDLED
                if not isinstance(result, Response):
                    raise TypeError(
                        f"process_response must return a Response or a Request, not {type(result).__name__}"
                    )
                response = result
        except IgnoreRequest as exc:
            self._ignored(request, exc)
            return _HANDLED
        except Exception as exc:
            await self._middleware_failed(request, exc)
            return _HANDLED
        return response

    async def _exception_middleware(self, request: Request, error: BaseException) -> Any:
        """``None`` (default handling), a recovered :class:`Response`, or ``_HANDLED``."""
        try:
            for hook in self._mw_exception:
                result = await maybe_await(hook(request, error, self.spider))
                if result is None:
                    continue
                if isinstance(result, Request):
                    self._reroute(result, request)
                    return _HANDLED
                if isinstance(result, Response):
                    return result
                raise TypeError(
                    f"process_exception must return None, a Response or a Request, not {type(result).__name__}"
                )
        except IgnoreRequest as exc:
            self._ignored(request, exc)
            return _HANDLED
        except Exception as exc:
            await self._middleware_failed(request, exc)
            return _HANDLED
        return None

    def _reroute(self, new: Request, original: Request) -> None:
        """A middleware replaced ``original`` with ``new``: queue it (bypassing the duplicate filter)."""
        new.meta.setdefault("depth", original.depth)
        self._check_serializable(new)
        self.scheduler.push(new, force=True)
        self.stats.inc("rerouted")
        self._wake()

    def _ignored(self, request: Request, exc: IgnoreRequest) -> None:
        self.stats.inc("ignored")
        log.debug("ignored %s: %s", request.url, exc)

    async def _middleware_failed(self, request: Request, exc: Exception) -> None:
        self.stats.inc("middleware_errors")
        log.error("middleware failed on %s: %s", request.url, describe(exc))
        self.failures.failure(request.host, request.url, error=exc, final=True)
        await self._give_up(request, exc)

    @staticmethod
    def _uncache(fetcher: Any, request: Request) -> None:
        layer = getattr(fetcher, "_cache_layer", None)
        if layer is not None:
            layer.cache.delete(request, layer.namespace)

    def _share_cookies(self, response: Response) -> None:
        """Hand a browser session's cookies to every HTTP session."""
        shared = 0
        for name in self.sessions:
            fetcher = self.sessions.get(name)
            if isinstance(fetcher, AsyncFetcher):
                fetcher.add_cookies(response.cookie_jar, url=response.url)
                shared += 1
        if shared:
            self.stats.inc("cookie_handoffs")

    async def _robots_allow(self, request: Request, domain: str) -> bool:
        assert self.robots is not None
        if not await self.robots.allowed(request.url):
            self.stats.inc("robots_blocked")
            log.debug("robots.txt forbids %s", request.url)
            self.failures.failure(domain, request.url, error=RobotsPolicyError(request.url), final=True)
            return False
        origin = RobotsPolicy.origin(request.url)
        if origin not in self._crawl_delays_checked:
            self._crawl_delays_checked.add(origin)
            delay = await self.robots.crawl_delay(request.url)
            if delay:
                log.info("robots.txt asks for a %.1fs crawl delay on %s", delay, domain)
                self.throttle.set_min_delay(domain, delay)
        return True

    def _pick_proxy(self, request: Request) -> tuple[str | None, bool]:
        """The proxy for ``request``, and whether the rotator chose it. A retry of a page the site answered goes
        through the proxy it was asked through: a block or a rate limit is not a reason to ask from elsewhere."""
        if request.proxy:
            return request.proxy, False
        if self.proxies is None:
            return None, False
        pinned = request.meta.get("proxy_index")
        pool = self.proxies.proxies
        if isinstance(pinned, int) and 0 <= pinned < len(pool):
            return self.proxies.reuse(pool[pinned]), True
        return self.proxies.next(), True

    def _retry(
        self, request: Request, reason: str, *, retry_after: float | None = None, proxy: str | None = None
    ) -> bool:
        """Queue ``request`` again after a pause, the same way: the same session, and ``proxy`` when given (kept
        as its place in the pool, so no proxy credentials go into a crawl's saved state)."""
        spider = self.spider
        attempt = request.retries
        if attempt >= spider.retries:
            self.stats.inc("retries_exhausted")
            return False
        if self._stopping and not self._keeps_state():
            return False
        new = request.replace(dont_filter=True)
        new.meta["retry_times"] = attempt + 1
        new.meta.pop("proxy_index", None)
        if proxy is not None and self.proxies is not None and proxy in self.proxies.proxies:
            new.meta["proxy_index"] = self.proxies.proxies.index(proxy)
        delay = min(30.0, 0.5 * 2**attempt) * random.uniform(0.75, 1.25)
        if retry_after:
            delay = 0.0  # the throttle already paused the whole domain for Retry-After
        self.stats.inc("retries")
        log.debug("retry %d/%d for %s in %.1fs (%s)", attempt + 1, spider.retries, request.url, delay, reason)
        self.events.emit("request_retried", url=request.url, reason=reason, attempt=attempt + 1, delay=round(delay, 3))
        self._schedule_later(new, delay, original=request)
        return True

    def _schedule_later(self, request: Request, delay: float, original: Request | None = None) -> None:
        assert self._loop is not None
        if original is not None and self.optimizer is not None:
            self._continued.add(id(original))
        if delay <= 0:
            self.scheduler.push(request, force=True)
            self._wake()
            return
        key = id(request)
        if self._persistent and original is not None:
            # Keep the original leased until its retry is safely queued.
            self._ack_deferred.add(id(original))
        else:
            original = None

        def release() -> None:
            entry = self._delayed.pop(key, None)
            if entry is not None:
                self.scheduler.push(entry[0], force=True)
                if entry[2] is not None:
                    self._ack_deferred.discard(id(entry[2]))
                    self._ack(entry[2])
                self._wake()

        self._delayed[key] = (request, self._loop.call_later(delay, release), original)

    async def _refused(self, request: Request, error: NetworkPolicyError) -> None:
        """The network policy refused a request: count it, say so once per host, and give up on it."""
        self.stats.inc("policy_blocked")
        host = request.host
        if host not in self._policy_warned:
            self._policy_warned.add(host)
            log.warning("network policy refused %s: %s", host, error.reason)
        self.failures.failure(host, request.url, error=error, final=True)
        self.events.emit("policy_refused", url=request.url, reason=error.reason, policy=error.policy)
        await self._give_up(request, error, quiet=True)

    async def _give_up(self, request: Request, error: BaseException, quiet: bool = False) -> None:
        self.stats.inc("failed")
        spider = self.spider
        if self.dead_letters is not None and not isinstance(error, PolicyError):
            try:
                self.dead_letters.add(request, error, spider)
            except OSError as exc:
                log.error("could not record a dead letter in %s: %s", self.dead_letters.path, describe(exc))
        self.events.emit(
            "request_failed",
            url=request.url,
            error=describe(error),
            category=category_of(error),
            kind=getattr(error, "kind", None),
            status=getattr(error, "status", None),
        )
        errback = request.errback
        if isinstance(errback, str):
            errback = getattr(spider, errback)
        try:
            if errback is not None:
                result = errback(request, error)
                await self._consume(result, request)
            elif quiet:
                log.debug("giving up on %s: %s", request.url, error)
            else:
                await maybe_await(spider.on_error(request, error))
        except Exception as exc:
            log.exception("error handler failed for %s: %s", request.url, exc)

    # ------------------------------------------------------------------ #
    # callbacks and outputs
    # ------------------------------------------------------------------ #
    async def _run_callback(self, request: Request, response: Response) -> None:
        callback = request.callback or self.spider.parse
        if isinstance(callback, str):
            callback = getattr(self.spider, callback)
        items: list[Any] | None = [] if self.history is not None or self.optimizer is not None else None
        try:
            await self._consume(callback(response, **request.cb_kwargs), request, items)
            if self.history is not None:
                self._record_page(response, items)
        except (CheckpointError, BrowserNotAvailable) as exc:
            self._requeue(request)  # so the page is processed again after a resume
            self.fail(exc)
            return
        except Exception as exc:
            self.stats.inc("callback_errors")
            self.failures.failure(request.host, request.url, error=exc, final=True, stage="callback")
            log.exception("error in %s for %s: %s", getattr(callback, "__name__", callback), response.url, exc)
        if self.optimizer is not None:
            try:
                self.optimizer.page(request, response, len(items or ()))
            except Exception as exc:  # pragma: no cover - learning must never break a crawl
                log.error("optimizer: %s", describe(exc))

    async def _consume(self, result: Any, parent: Request, items: list[Any] | None = None) -> None:
        """Handle what a callback returned; ``items`` collects the items it produced."""
        if result is None:
            return
        if inspect.isasyncgen(result):
            async for output in result:
                await self._output(output, parent, items)
        elif inspect.isawaitable(result):
            await self._consume(await result, parent, items)
        elif isinstance(result, (Request, dict, str, bytes)):
            await self._output(result, parent, items)
        elif inspect.isgenerator(result) or isinstance(result, (list, tuple)):
            for output in result:
                await self._output(output, parent, items)
        else:
            await self._output(result, parent, items)

    async def _output(self, output: Any, parent: Request, items: list[Any] | None = None) -> None:
        if output is None:
            return
        if isinstance(output, Request):
            self._enqueue_child(output, parent)
        elif isinstance(output, (str, bytes)):
            log.warning(
                "callback yielded a %s (%r...); yield dicts for items or Requests to crawl",
                type(output).__name__,
                output[:40],
            )
        else:
            kept = await self._item(output)
            if kept is not None and items is not None:
                items.append(kept)

    def _enqueue_child(self, request: Request, parent: Request) -> None:
        spider = self.spider
        depth = parent.depth + 1
        request.meta["depth"] = depth
        if spider.max_depth is not None and depth > spider.max_depth:
            self.stats.inc("depth_filtered")
            return
        if self.url_normalizer is not None:
            request.url = self.url_normalizer(request.url)
        optimizer = self.optimizer
        rewritten = False
        if optimizer is not None and not request.dont_filter:
            url = optimizer.rewrite(request.url)
            rewritten, request.url = url != request.url, url
        if spider.allowed_domains and not domain_matches(request.host, spider.allowed_domains):
            self.stats.inc("offsite_filtered")
            return
        if self.url_rules is not None:
            reason = self.url_rules.check(request.url, sitemap=request.callback == "_parse_sitemap")
            if reason is not None:
                self.stats.inc("rules_filtered")
                self.stats.inc(f"rules_filtered/{reason}")
                return
        # the optimizer only looks at requests the duplicate filter would let through
        fresh = optimizer if optimizer is not None and not self._seen_before(request) else None
        if fresh is not None:
            if rewritten:
                self.stats.inc("optimizer/rewritten")
            if not request.dont_filter and fresh.duplicate(request.url):
                self.stats.inc("optimizer/duplicates")  # fetched already, with a parameter that changes nothing
                self.scheduler.restore_seen([request.fingerprint()])  # from now on a plain duplicate
                return
            if fresh.skip(request, parent):
                return  # its pattern gives nothing (counted by the optimizer: optimizer/skipped)
        if self.priority_fn is not None:
            request.priority = int(self.priority_fn(request))
        elif fresh is not None:
            request.priority += fresh.boost(request.url)
        if fresh is not None:
            fresh.enqueued(request, parent)
        self._check_serializable(request)
        if self.scheduler.push(request):
            self.stats.inc("enqueued")
            self._wake()
        elif fresh is not None:
            fresh.discard(request)

    def _check_serializable(self, request: Request) -> None:
        """Fail early (with a clear message) if a request could not be checkpointed."""
        if self.checkpoint is None:
            return
        data = request.to_dict(self.spider)  # CheckpointError for non-method callbacks
        try:
            pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as exc:
            raise CheckpointError(
                f"Request for {request.url} cannot be saved for pause/resume ({describe(exc)}). "
                "Keep meta, cb_kwargs and options picklable - e.g. pass a spider method name instead of a lambda."
            ) from exc

    # ------------------------------------------------------------------ #
    # adaptive fetching (see wintergrab.fetchers.strategy)
    # ------------------------------------------------------------------ #
    def _session_for(self, request: Request) -> str | None:
        """The request's own session; with adaptive fetching, HTTP or (for the URL patterns whose
        pages needed one) the browser."""
        adaptive = self.adaptive
        if request.session is not None or adaptive is None or not adaptive.available:
            return request.session
        if adaptive.browser_first(request.url):
            request.meta["adaptive"] = "browser"
            self.stats.inc("adaptive/browser_first")
            return "browser"
        request.meta.pop("adaptive", None)  # a retry of a browser-first page may be tried over HTTP
        return "http"

    def _adaptive_check(self, request: Request, response: Response) -> str | None:
        """Count how the page came out; return why it should be fetched again in the browser, if
        it came over HTTP without its content."""
        adaptive = self.adaptive
        assert adaptive is not None
        via_browser = response.source == "browser"
        if not adaptive.available or not (200 <= response.status < 300) or not response.is_html:
            return None
        if request.session is not None and not (via_browser and request.meta.get("adaptive") == "rendered"):
            return None  # a session the spider chose itself
        try:
            if self.spider.is_blocked(response):
                return None  # blocks are handled as blocks, never by switching to a browser
            reason = self.spider.needs_browser(response)
        except Exception as exc:
            log.error("needs_browser() failed for %s: %s", request.url, describe(exc))
            return None
        adaptive.record(request.url, "browser" if via_browser else "http", not reason)
        if via_browser:
            if reason:
                self.stats.inc("adaptive/browser_short")
                log.debug("%s still lacks content in the browser (%s)", request.url, reason)
            return None
        if not reason:
            return None
        return reason if isinstance(reason, str) else "needs_browser() said so"

    def _render(self, request: Request, reason: str) -> None:
        """Fetch the page again in the browser: another attempt at the same page (like a retry, it
        is not a new page for ``max_pages``, and it counts among the page's attempts)."""
        assert self.adaptive is not None
        new = request.replace(dont_filter=True, session="browser")
        new.meta["adaptive"] = "rendered"
        new.meta["retry_times"] = request.retries + 1
        for key, value in self.adaptive.browser_options().items():
            new.options.setdefault(key, value)
        self.stats.inc("adaptive/rendered")
        log.debug("%s needs a browser: %s", request.url, reason)
        self.events.emit("browser_needed", url=request.url, pattern=self.adaptive.pattern(request.url), reason=reason)
        self._schedule_later(new, 0.0, original=request)

    def _no_browser(self, request: Request, error: BaseException) -> None:
        """No browser here: adaptive fetching stops, and the page is fetched over HTTP after all."""
        assert self.adaptive is not None
        if self.adaptive.available:
            self.adaptive.available = False
            log.warning("adaptive_fetch: no browser available, so every page is fetched over HTTP (%s)", error)
        new = request.replace(dont_filter=True, session=None)
        new.meta["adaptive"] = "no-browser"
        new.meta["retry_times"] = request.retries + 1
        for key in self.adaptive.browser_options():
            new.options.pop(key, None)
        self._schedule_later(new, 0.0, original=request)

    def _close_adaptive(self) -> Any:
        adaptive = self.adaptive
        if adaptive is None:
            return None
        if adaptive.patterns:
            log.info("fetch strategy by URL pattern:\n%s", adaptive.describe())
        try:
            adaptive.save()
        except OSError as exc:
            log.error("could not save the fetch statistics to %s: %s", adaptive.path, exc)
        return adaptive

    def _drop_unneeded(self, request: Request) -> bool:
        """With the optimizer: whether a request just taken from the queue can be left out, because
        since it was queued its pattern was found barren, or its page was fetched under another
        address (with a parameter found to change nothing)."""
        optimizer = self.optimizer
        if optimizer is None or request.dont_filter or request.depth == 0:
            return False  # start requests are always fetched
        if not optimizer.skip(request):
            if not optimizer.duplicate(optimizer.rewrite(request.url)):
                return False
            self.stats.inc("optimizer/duplicates")
        optimizer.done(request)
        if self._persistent:
            self._ack(request)
        return True

    def _seen_before(self, request: Request) -> bool:
        """Whether the duplicate filter would drop ``request`` anyway."""
        scheduler = self.scheduler
        return bool(scheduler.dedupe) and not request.dont_filter and request.fingerprint() in scheduler.seen

    def _close_optimizer(self) -> Any:
        optimizer = self.optimizer
        if optimizer is None:
            return None
        if optimizer.skipped:
            self.stats["optimizer/skipped"] = optimizer.skipped
        if optimizer.patterns:
            log.info("what the optimizer learned:\n%s", optimizer.describe())
        try:
            optimizer.save()
        except OSError as exc:
            log.error("could not save what the optimizer learned to %s: %s", optimizer.path, exc)
        return optimizer

    def _fetch_options(self, request: Request) -> dict[str, Any]:
        options = dict(request.options)
        clashes = RESERVED_OPTIONS.intersection(options)
        for key in clashes:
            options.pop(key)
            if key not in self._warned_options:
                self._warned_options.add(key)
                log.warning("Request.options[%r] is ignored; use the Request's own %r field instead", key, key)
        return options

    async def _run_pipelines(self, item: Any) -> Any:
        """Pass an item through the pipelines; ``None`` if one dropped it."""
        for name, hook in self._pipes:
            try:
                item = await maybe_await(hook(item, self.spider))
            except DropItem as exc:
                self._item_dropped(name, str(exc) or "dropped")
                return None
            except Exception as exc:
                self.stats.inc("pipeline_errors")
                log.error("pipeline %s failed: %s", name, describe(exc))
                self._item_dropped(name, f"error: {describe(exc)}")
                return None
            if item is None:
                self._item_dropped(name, "dropped")
                return None
        return item

    def _item_dropped(self, pipeline: str, reason: str) -> None:
        self.stats.inc("items_dropped")
        self.stats.inc(f"items_dropped/{pipeline}")
        self.events.emit("item_dropped", pipeline=pipeline, reason=reason)

    def _is_duplicate_item(self, item: Any) -> bool:
        data = to_dict(item)
        if not isinstance(data, dict) or data.get(self.spider.unique_key) is None:
            return False
        digest = hashlib.blake2b(repr(data[self.spider.unique_key]).encode(), digest_size=16).digest()
        if digest in self._item_keys:
            return True
        self._item_keys.add(digest)
        return False

    async def _item(self, item: Any) -> Any:
        """Process, export and keep an item; returns it as exported (``None`` if it was dropped)."""
        spider = self.spider
        if spider.max_items is not None and self.stats.get("items", 0) >= spider.max_items:
            self.stats.inc("items_over_limit")
            return None
        processed = await maybe_await(spider.process_item(item))
        if processed is None:
            self.stats.inc("items_dropped")
            return None
        if self._pipes:
            processed = await self._run_pipelines(processed)
            if processed is None:
                return None
        if spider.unique_key and self._is_duplicate_item(processed):
            self.stats.inc("items_duplicate")
            return None
        self.stats.inc("items")
        if self.recorder is not None:
            self.recorder.item(processed)
        if self.exporter is not None:
            try:
                self.exporter.write(processed)
            except Exception as exc:
                self.stats.inc("export_errors")
                log.error("could not write item to %s: %s", redact_url(str(self.spider.output)), describe(exc))
            if self._output_budget:
                exhausted = self.budget.check_output()
                if exhausted is not None:
                    self._budget_exhausted(exhausted)
        if spider.keep_items:
            self.items.append(processed)
        if self._emit_item:
            self.events.emit("item_scraped", item=processed)
        if self.item_queue is not None:
            await self.item_queue.put(processed)
        if spider.max_items is not None and self.stats["items"] >= spider.max_items:
            self._begin_stop("limit")
        return processed


__all__ = ["Engine", "Stats", "proxy_label"]
