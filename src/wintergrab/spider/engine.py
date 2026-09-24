"""The crawl loop that drives a :class:`Spider`."""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import signal
import time
from collections.abc import AsyncIterable, Callable
from typing import TYPE_CHECKING, Any

from ..errors import BrowserNotAvailable, CheckpointError, FetchError, HTTPStatusError, describe
from ..fetchers.http import PROXY_FAILURE_STATUSES, AsyncFetcher
from ..proxy import ProxyRotator, proxy_label
from ..request import Request
from ..utils import configure_logging, domain_matches, ensure_scheme, host_of, maybe_await, parse_retry_after
from .checkpoint import Checkpoint
from .exporters import Exporter, open_exporter
from .robots import RobotsPolicy
from .scheduler import Scheduler
from .sessions import SessionManager
from .spider import CrawlResult
from .throttle import AutoThrottle

if TYPE_CHECKING:
    from ..fetchers.response import Response
    from .spider import Spider

log = logging.getLogger("wintergrab.spider")

PUSHBACK_STATUSES = frozenset({429, 503})


class Stats(dict):  # type: ignore[type-arg]
    def inc(self, key: str, n: int | float = 1) -> None:
        self[key] = self.get(key, 0) + n


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
        self.scheduler = Scheduler(dedupe=spider.dedupe)
        self.throttle: AutoThrottle = spider.throttle or AutoThrottle(
            enabled=spider.autothrottle,
            base_delay=spider.download_delay,
            max_delay=spider.max_delay,
            max_concurrency=spider.concurrency_per_domain,
        )
        self.proxies = ProxyRotator.coerce(spider.proxies)
        self.sessions = SessionManager()
        self.checkpoint = Checkpoint(spider.crawl_dir) if spider.crawl_dir else None
        self.exporter: Exporter | None = None
        self.robots: RobotsPolicy | None = None
        self._robots_fetcher: AsyncFetcher | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._wakeup: asyncio.Event | None = None
        self._inflight: dict[asyncio.Task[None], Request] = {}
        self._delayed: dict[int, tuple[Request, asyncio.TimerHandle]] = {}
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
            self._force = True
            self._wake()

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
            self._state_ready = True
            self._setup_output(append=state is not None)
            spider.configure_sessions(self.sessions)
            if not len(self.sessions):
                raise RuntimeError("configure_sessions() registered no sessions")
            if spider.obey_robots_txt:
                self._robots_fetcher = AsyncFetcher(
                    impersonate=spider.impersonate, timeout=15, retries=1, verify=spider.verify
                )
                self.robots = RobotsPolicy(self._fetch_robots, spider.robots_user_agent)
            await maybe_await(spider.on_start())
            if state is not None:
                self._restore(state)
            else:
                await self._seed()
            if self._pending_stop:
                self._begin_stop(self._pending_stop)
            log.info(
                "%s %s: %d request(s) queued",
                "resuming" if state is not None else "starting",
                spider.name,
                len(self.scheduler),
            )
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
        spider = self.spider
        while True:
            if not self._stopping:
                wait = self._dispatch()
            else:
                wait = None
                if self._force:
                    await self._cancel_inflight()
            if self._stopping and not self._inflight:
                break
            if not self._stopping and not self._inflight and not self._delayed and not len(self.scheduler):
                self._status = "finished"
                break
            self._periodic()
            timeout = 1.0 if wait is None else max(0.01, min(wait, 1.0))
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            self._wakeup.clear()
            if spider.max_pages is not None and self.stats.get("pages", 0) >= spider.max_pages and not self._stopping:
                self._begin_stop("limit")

    def _dispatch(self) -> float | None:
        spider = self.spider
        now = time.monotonic()
        wait: float | None = None
        while len(self._inflight) < spider.concurrency:
            if spider.max_pages is not None and self.stats.get("pages", 0) >= spider.max_pages:
                break
            request, wait = self.scheduler.pop_ready(self.throttle, now)
            if request is None:
                break
            slot = self.throttle.slot(host_of(request.url))
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
        tasks = list(self._inflight)
        for task in tasks:
            task.cancel()
        for task in tasks:
            request = self._inflight.get(task)
            try:
                await task
            except BaseException:
                pass
            if request is not None and self._status == "paused":
                self.scheduler.push(request.replace(dont_filter=True), force=True)
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
                loop.add_signal_handler(sig, self._on_signal)
                installed.append(("loop", sig))
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
                    else:
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
        return state

    def _setup_output(self, append: bool) -> None:
        if self.spider.output:
            self.exporter = open_exporter(self.spider.output, append=append)

    def _restore(self, state: dict[str, Any]) -> None:
        self.stats.update(state.get("stats") or {})
        self.stats.inc("runs")
        self.scheduler.restore_seen(state.get("seen") or ())
        self.throttle.restore(state.get("throttle") or {})
        for data in state.get("pending") or ():
            self.scheduler.push(Request.from_dict(data, self.spider), force=True)

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
        request.meta.setdefault("depth", 0)
        self._check_serializable(request)
        self.scheduler.push(request)

    async def _shutdown(self) -> CrawlResult:
        spider = self.spider
        elapsed = time.monotonic() - self._started
        self.stats["elapsed_seconds"] = round(self.stats.get("elapsed_seconds", 0) + elapsed, 3)
        self.stats["duplicates_filtered"] = self.stats.get("duplicates_filtered", 0) + self.scheduler.duplicates
        self.scheduler.duplicates = 0
        status = self._status if self._fatal is None else "stopped"
        pending = self._pending_requests()
        if self.checkpoint is not None and self._state_ready:
            try:
                if (status == "paused" or self._fatal is not None) and pending:
                    # Paused - or crashed: keep the queue so a fixed spider can resume.
                    self._save_state(pending)
                    log.info(
                        "saved %d pending request(s) to %s; run again to resume", len(pending), self.checkpoint.dir
                    )
                else:
                    self.checkpoint.clear()
                    if status == "paused":
                        status = "finished"
                self.checkpoint.write_summary({"status": status, "stats": dict(self.stats)})
            except CheckpointError as exc:
                log.error("%s", exc)
                if self._fatal is None:
                    self._fatal = exc
        elif pending and status != "finished":
            log.info("%d pending request(s) discarded", len(pending))
        for timer in self._delayed.values():
            timer[1].cancel()
        self._delayed.clear()
        if self.exporter is not None:
            self.exporter.close()
        await self.sessions.close_all()
        if self._robots_fetcher is not None:
            await self._robots_fetcher.aclose()
        self.stats["status"] = status
        result = CrawlResult(items=self.items, stats=dict(self.stats), status=status, crawl_dir=spider.crawl_dir)
        self._log_progress(final=True)
        try:
            await maybe_await(spider.on_close(result))
        except Exception as exc:
            log.error("on_close failed: %s", describe(exc))
        return result

    def _pending_requests(self) -> list[Request]:
        pending = self.scheduler.pending()
        pending.extend(req for req, _ in self._delayed.values())
        pending.extend(req.replace(dont_filter=True) for req in self._inflight.values())
        return pending

    def _save_state(self, pending: list[Request]) -> None:
        assert self.checkpoint is not None
        self.checkpoint.save(
            {
                "spider": self.spider.name,
                "pending": [r.to_dict(self.spider) for r in pending],
                "seen": self.scheduler.seen,
                "stats": dict(self.stats),
                "throttle": self.throttle.snapshot(),
            }
        )

    def _periodic(self) -> None:
        now = time.monotonic()
        spider = self.spider
        if self.checkpoint is not None and now - self._last_checkpoint >= spider.checkpoint_interval:
            if self._last_checkpoint:
                try:
                    self._save_state(self._pending_requests())
                except CheckpointError as exc:
                    log.error("%s", exc)
            self._last_checkpoint = now
        if now - self._last_log >= spider.log_interval:
            if self._last_log:
                self._log_progress()
            self._last_log = now

    def _log_progress(self, final: bool = False) -> None:
        s = self.stats
        elapsed = s.get("elapsed_seconds", 0) if final else time.monotonic() - self._started
        rate = s.get("pages", 0) / elapsed * 60 if elapsed else 0.0
        log.info(
            "%s%d pages (%.0f/min), %d items, %d retries, %d errors, %d queued, %d in flight",
            "done: " if final else "",
            s.get("pages", 0),
            rate,
            s.get("items", 0),
            s.get("retries", 0),
            s.get("errors", 0),
            len(self.scheduler),
            len(self._inflight),
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
            if self.robots is not None and not await self._robots_allow(request, domain):
                return
            session_name = request.session
            try:
                fetcher = self.sessions.get(session_name)
            except LookupError as exc:
                self.fail(exc)
                return
            proxy = request.proxy or (self.proxies.next() if self.proxies is not None else None)
            rotated = request.proxy is None and self.proxies is not None
            options = dict(request.options)
            timeout = options.pop("timeout", spider.timeout)
            started = time.monotonic()
            try:
                response: Response = await asyncio.wait_for(
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
                    timeout=max(60.0, timeout * 4),
                )
            except asyncio.CancelledError:
                raise
            except BrowserNotAvailable as exc:
                self.fail(exc)
                return
            except (FetchError, asyncio.TimeoutError) as exc:
                if isinstance(exc, asyncio.TimeoutError):
                    exc = FetchError(request.url, "Hard timeout", is_timeout=True)
                if rotated:
                    self.proxies.report_failure(proxy)  # type: ignore[union-attr]
                if exc.is_timeout:
                    self.throttle.on_error(domain)
                self.stats.inc("errors")
                self.stats.inc(f"error/{type(exc.cause).__name__ if exc.cause else 'Timeout'}")
                if exc.retryable and self._retry(request, describe(exc)):
                    return
                await self._give_up(request, exc)
                return
            except Exception as exc:
                self.stats.inc("errors")
                self.stats.inc(f"error/{type(exc).__name__}")
                await self._give_up(request, exc)
                return

            latency = time.monotonic() - started
            self.stats.inc("responses")
            self.stats.inc(f"status/{response.status}")
            self.stats.inc("bytes", len(response.body))

            blocked = False
            try:
                blocked = bool(spider.is_blocked(response))
            except Exception as exc:
                log.error("is_blocked() failed: %s", describe(exc))
            if blocked:
                self.stats.inc("blocked")
            if blocked or response.status in PUSHBACK_STATUSES:
                self.throttle.on_pushback(domain, parse_retry_after(response.headers.get("retry-after"), cap=600))
                self.stats.inc("backoffs")
            else:
                self.throttle.on_success(domain, latency)
            if rotated:
                bad = blocked or response.status in PROXY_FAILURE_STATUSES
                (self.proxies.report_failure if bad else self.proxies.report_success)(proxy)  # type: ignore[union-attr]

            if blocked or response.status in spider.retry_statuses:
                reason = "blocked" if blocked else f"HTTP {response.status}"
                if self._retry(request, reason, blocked=blocked, response=response):
                    return
            ok_status = 200 <= response.status < 300 or response.status in spider.allowed_statuses
            if blocked and response.status not in spider.allowed_statuses:
                ok_status = False
            if not ok_status:
                self.stats.inc("http_errors")
                await self._give_up(request, HTTPStatusError(response), quiet=response.status == 404)
                return
            await self._run_callback(request, response)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - safety net
            log.exception("unexpected error processing %s: %s", request.url, exc)
            self.stats.inc("errors")
        finally:
            self.throttle.on_finish(slot)
            self._wake()

    async def _robots_allow(self, request: Request, domain: str) -> bool:
        assert self.robots is not None
        if not await self.robots.allowed(request.url):
            self.stats.inc("robots_blocked")
            log.debug("robots.txt forbids %s", request.url)
            return False
        origin = RobotsPolicy.origin(request.url)
        if origin not in self._crawl_delays_checked:
            self._crawl_delays_checked.add(origin)
            delay = await self.robots.crawl_delay(request.url)
            if delay:
                log.info("robots.txt asks for a %.1fs crawl delay on %s", delay, domain)
                self.throttle.set_min_delay(domain, delay)
        return True

    def _retry(self, request: Request, reason: str, *, blocked: bool = False, response: Response | None = None) -> bool:
        spider = self.spider
        attempt = request.retries
        if attempt >= spider.retries:
            self.stats.inc("retries_exhausted")
            return False
        if self._stopping and self._status != "paused":
            return False
        new = request.replace(dont_filter=True)
        new.meta["retry_times"] = attempt + 1
        if blocked and spider.fallback_session and request.session != spider.fallback_session:
            log.info("%s looks blocked; retrying with session %r", request.url, spider.fallback_session)
            new.session = spider.fallback_session
        delay = min(30.0, 0.5 * 2**attempt) * random.uniform(0.75, 1.25)
        if response is not None and response.status in PUSHBACK_STATUSES:
            delay = 0.0  # the throttle already paused the domain
        self.stats.inc("retries")
        log.debug("retry %d/%d for %s in %.1fs (%s)", attempt + 1, spider.retries, request.url, delay, reason)
        self._schedule_later(new, delay)
        return True

    def _schedule_later(self, request: Request, delay: float) -> None:
        assert self._loop is not None
        if delay <= 0:
            self.scheduler.push(request, force=True)
            self._wake()
            return
        key = id(request)

        def release() -> None:
            entry = self._delayed.pop(key, None)
            if entry is not None:
                self.scheduler.push(entry[0], force=True)
                self._wake()

        self._delayed[key] = (request, self._loop.call_later(delay, release))

    async def _give_up(self, request: Request, error: BaseException, quiet: bool = False) -> None:
        self.stats.inc("failed")
        spider = self.spider
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
        try:
            await self._consume(callback(response, **request.cb_kwargs), request)
        except (CheckpointError, BrowserNotAvailable) as exc:
            self.fail(exc)
        except Exception as exc:
            self.stats.inc("callback_errors")
            log.exception("error in %s for %s: %s", getattr(callback, "__name__", callback), response.url, exc)

    async def _consume(self, result: Any, parent: Request) -> None:
        if result is None:
            return
        if inspect.isasyncgen(result):
            async for output in result:
                await self._output(output, parent)
        elif inspect.isawaitable(result):
            await self._consume(await result, parent)
        elif isinstance(result, (Request, dict, str, bytes)):
            await self._output(result, parent)
        elif inspect.isgenerator(result) or isinstance(result, (list, tuple)):
            for output in result:
                await self._output(output, parent)
        else:
            await self._output(result, parent)

    async def _output(self, output: Any, parent: Request) -> None:
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
            await self._item(output)

    def _enqueue_child(self, request: Request, parent: Request) -> None:
        spider = self.spider
        depth = parent.depth + 1
        request.meta["depth"] = depth
        if spider.max_depth is not None and depth > spider.max_depth:
            self.stats.inc("depth_filtered")
            return
        if spider.allowed_domains and not domain_matches(host_of(request.url), spider.allowed_domains):
            self.stats.inc("offsite_filtered")
            return
        self._check_serializable(request)
        if self.scheduler.push(request):
            self.stats.inc("enqueued")
            self._wake()

    def _check_serializable(self, request: Request) -> None:
        if self.checkpoint is not None:
            request.to_dict(self.spider)  # raises CheckpointError early, with a helpful message

    async def _item(self, item: Any) -> None:
        spider = self.spider
        if spider.max_items is not None and self.stats.get("items", 0) >= spider.max_items:
            self.stats.inc("items_over_limit")
            return
        processed = await maybe_await(spider.process_item(item))
        if processed is None:
            self.stats.inc("items_dropped")
            return
        self.stats.inc("items")
        if self.exporter is not None:
            self.exporter.write(processed)
        if spider.keep_items:
            self.items.append(processed)
        if self.item_queue is not None:
            await self.item_queue.put(processed)
        if spider.max_items is not None and self.stats["items"] >= spider.max_items:
            self._begin_stop("limit")


__all__ = ["Engine", "Stats", "proxy_label"]
