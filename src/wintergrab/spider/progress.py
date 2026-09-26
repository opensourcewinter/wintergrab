"""A live one-line status display for crawls running in a terminal."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import time
from collections import deque
from typing import IO, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


class ProgressDisplay:
    """Redraws ``⠹ 1,204 pages · 38.5/s · 980 items · 311 queued · 16 active · 2m04s`` in place.

    Log records from the ``wintergrab`` logger are printed above the line
    instead of breaking it.
    """

    def __init__(self, engine: Engine, stream: IO[str] | None = None, interval: float = 0.25) -> None:
        self.engine = engine
        self.stream = stream or sys.stderr
        self.interval = interval
        self._samples: deque[tuple[float, int]] = deque(maxlen=64)
        self._frame = 0
        self._visible = False
        self._task: asyncio.Task[None] | None = None
        self._patched: list[tuple[logging.Handler, Any]] = []

    @staticmethod
    def supported(stream: IO[str] | None = None) -> bool:
        """Only on an interactive terminal (not in CI logs or redirected output)."""
        stream = stream or sys.stderr
        try:
            tty = stream.isatty()
        except (AttributeError, ValueError):
            return False
        return tty and os.environ.get("TERM") != "dumb" and not os.environ.get("CI")

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        for handler in logging.getLogger("wintergrab").handlers:
            if isinstance(handler, logging.StreamHandler) and handler.stream is self.stream:
                original = handler.emit

                def emit(record: logging.LogRecord, _original: Any = original) -> None:
                    self.clear()
                    _original(record)
                    self.draw()

                handler.emit = emit  # type: ignore[method-assign]
                self._patched.append((handler, original))
        self._task = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        while True:
            self.draw()
            await asyncio.sleep(self.interval)

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self.clear()
        for handler, original in self._patched:
            handler.emit = original  # type: ignore[method-assign]
        self._patched.clear()

    # ------------------------------------------------------------------ #
    def rate(self) -> float:
        """Pages per second over roughly the last ten seconds."""
        now = time.monotonic()
        pages = int(self.engine.stats.get("pages", 0))
        self._samples.append((now, pages))
        while len(self._samples) > 2 and now - self._samples[0][0] > 10:
            self._samples.popleft()
        then, before = self._samples[0]
        return (pages - before) / (now - then) if now - then >= 0.5 else 0.0

    def line(self) -> str:
        engine = self.engine
        stats = engine.stats
        spider = engine.spider
        pages = int(stats.get("pages", 0))
        rate = self.rate()
        self._frame = (self._frame + 1) % len(_FRAMES)
        parts = [
            f"{_FRAMES[self._frame]} {pages:,} pages",
            f"{rate:,.1f}/s",
            f"{int(stats.get('items', 0)):,} items",
            f"{len(engine.scheduler):,} queued",
            f"{len(engine._inflight)} active",
        ]
        for key, label in (
            ("cache_hits", "cached"),
            ("retries", "retries"),
            ("errors", "errors"),
            ("backoffs", "backoffs"),
        ):
            if stats.get(key):
                parts.append(f"{int(stats[key]):,} {label}")
        optimizer = engine.optimizer
        if optimizer is not None:
            expected = optimizer.forecast()["items"]
            if expected >= 1:
                parts.append(f"~{expected:,.0f} more items expected")
            if optimizer.skipped:
                parts.append(f"{optimizer.skipped:,} skipped")
        elapsed = time.monotonic() - engine._started
        if spider.max_pages and rate > 0:
            remaining = max(0, spider.max_pages - pages) / rate
            parts.append(f"{_duration(elapsed)} (~{_duration(remaining)} left)")
        else:
            parts.append(_duration(elapsed))
        if engine._stopping:
            parts.append(engine._status + "...")
        text = " · ".join(parts)
        width = shutil.get_terminal_size((100, 20)).columns - 1
        return text if len(text) <= width else text[: max(10, width - 1)] + "…"

    def draw(self) -> None:
        try:
            self.stream.write("\r\033[K" + self.line())
            self.stream.flush()
            self._visible = True
        except (OSError, ValueError):  # pragma: no cover - closed stream
            pass

    def clear(self) -> None:
        if self._visible:
            try:
                self.stream.write("\r\033[K")
                self.stream.flush()
            except (OSError, ValueError):  # pragma: no cover
                pass
            self._visible = False
