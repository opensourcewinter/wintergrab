"""Crawl budgets: stop (or focus) a crawl before it uses too much of something.

A budget is a spider setting (``max_requests``, ``max_bytes``, ``max_runtime``...).
When one runs out the crawl stops gracefully with status ``"limit"`` and
``stats["limit_reason"]`` naming it; with a ``crawl_dir`` the unvisited queue is
kept, so raising the budget and running again continues the crawl.

``budget_soft_limit`` degrades gracefully instead of stopping abruptly: once any
budget is that far used (``0.9`` = 90%), only requests with a priority of at
least ``budget_soft_priority`` are started, so what is left goes to the pages
that matter most.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..errors import ConfigurationError
from .metrics import current_rss

if TYPE_CHECKING:
    from .spider import Spider

__all__ = ["BUDGETS", "BudgetMonitor", "BudgetStatus"]

#: Spider settings that are budgets, and what they measure.
BUDGETS: dict[str, str] = {
    "max_pages": "pages started (retries not counted)",
    "max_items": "items kept",
    "max_requests": "requests sent, retries included",
    "max_bytes": "response bytes downloaded",
    "max_runtime": "seconds of crawling, across resumed runs",
    "max_browser_pages": "pages rendered in a browser",
    "max_errors": "URLs given up on",
    "max_error_rate": "URLs given up on / pages started (after error_rate_min_pages pages)",
    "max_memory": "bytes of resident memory of this process",
    "max_cpu_seconds": "CPU seconds used by this process in this run",
    "max_output_bytes": "bytes written to the output file",
}
# Budgets checked after every response (counters); the rest are checked once a second.
_COUNTER_BUDGETS = ("max_requests", "max_bytes", "max_browser_pages", "max_errors", "max_error_rate")
_STAT_OF = {
    "max_requests": "requests",
    "max_bytes": "bytes",
    "max_browser_pages": "browser_pages",
    "max_errors": "failed",
}


@dataclass
class BudgetStatus:
    name: str
    used: float
    limit: float

    @property
    def fraction(self) -> float:
        return self.used / self.limit if self.limit else 1.0

    def to_dict(self) -> dict[str, Any]:
        return {"used": self.used, "limit": self.limit, "fraction": round(self.fraction, 4)}


class BudgetMonitor:
    """Tracks a spider's budgets (the engine asks it after responses and once a second)."""

    def __init__(self, spider: Spider) -> None:
        self.limits: dict[str, float] = {}
        for name in BUDGETS:
            if name in ("max_pages", "max_items"):
                continue  # the engine enforces these itself (with their own semantics)
            value = getattr(spider, name, None)
            if value is None:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise ConfigurationError(f"must be a positive number, got {value!r}", key=name)
            if name == "max_error_rate" and value > 1:
                raise ConfigurationError(f"is a fraction between 0 and 1, got {value!r}", key=name)
            self.limits[name] = float(value)
        self.min_pages = int(getattr(spider, "error_rate_min_pages", 50))
        soft = getattr(spider, "budget_soft_limit", None)
        if soft is not None and not 0 < soft < 1:
            raise ConfigurationError(f"is a fraction between 0 and 1, got {soft!r}", key="budget_soft_limit")
        self.soft_limit: float | None = soft
        self.soft_priority = int(getattr(spider, "budget_soft_priority", 1))
        # max_pages/max_items count towards the soft limit too.
        self.page_limit = spider.max_pages
        self.item_limit = spider.max_items
        self.output_path = spider.output if spider.output and spider.output != "-" else None
        self.active = bool(self.limits)
        self._cpu_start = time.process_time()
        self._output_start: int | None = None
        self._exporter: Any = None

    def attach_output(self, exporter: Any) -> None:
        """Measure ``max_output_bytes`` with the exporter's own count (exact, and buffered bytes included)."""
        self._exporter = exporter

    def check_output(self) -> BudgetStatus | None:
        """``max_output_bytes`` after an item was written, when the exporter counts its bytes."""
        limit = self.limits.get("max_output_bytes")
        written = getattr(self._exporter, "bytes_written", None)
        if limit is None or written is None:
            return None
        return BudgetStatus("max_output_bytes", written, limit) if written >= limit else None

    def _output_bytes(self) -> int:
        written = getattr(self._exporter, "bytes_written", None)
        if written is not None:
            return int(written)
        try:  # SQLite output: the file on disk (updated when the exporter commits)
            return max(0, os.path.getsize(self.output_path or "") - (self._output_start or 0))
        except OSError:
            return 0

    def start(self, append: bool) -> None:
        """Remember what the output file held before this run (only this run's writes count)."""
        if "max_output_bytes" in self.limits and self.output_path:
            try:
                self._output_start = os.path.getsize(self.output_path) if append else 0
            except OSError:
                self._output_start = 0

    # -- checks ------------------------------------------------------------ #
    def check_counters(self, stats: dict[str, Any]) -> BudgetStatus | None:
        """The first exhausted counter budget, or ``None``."""
        for name in _COUNTER_BUDGETS:
            limit = self.limits.get(name)
            if limit is None:
                continue
            if name == "max_error_rate":
                pages = float(stats.get("pages", 0))
                if pages < self.min_pages:
                    continue
                used = float(stats.get("failed", 0)) / pages
                if used > limit:
                    return BudgetStatus(name, round(used, 4), limit)
                continue
            used = float(stats.get(_STAT_OF[name], 0))
            if used >= limit:
                return BudgetStatus(name, used, limit)
        return None

    def check_resources(self, elapsed_total: float) -> BudgetStatus | None:
        """The first exhausted time/memory/CPU/output budget, or ``None``."""
        for status in self._resource_usage(elapsed_total):
            if status.used >= status.limit:
                return status
        return None

    def _resource_usage(self, elapsed_total: float) -> list[BudgetStatus]:
        out = []
        limits = self.limits
        if "max_runtime" in limits:
            out.append(BudgetStatus("max_runtime", round(elapsed_total, 3), limits["max_runtime"]))
        if "max_memory" in limits:
            rss = current_rss()
            if rss is not None:
                out.append(BudgetStatus("max_memory", rss, limits["max_memory"]))
        if "max_cpu_seconds" in limits:
            out.append(
                BudgetStatus(
                    "max_cpu_seconds", round(time.process_time() - self._cpu_start, 3), limits["max_cpu_seconds"]
                )
            )
        if "max_output_bytes" in limits and self.output_path:
            out.append(BudgetStatus("max_output_bytes", self._output_bytes(), limits["max_output_bytes"]))
        return out

    def usage(self, stats: dict[str, Any], elapsed_total: float) -> dict[str, BudgetStatus]:
        """Every configured budget with how much of it is used."""
        out: dict[str, BudgetStatus] = {}
        for name in _COUNTER_BUDGETS:
            limit = self.limits.get(name)
            if limit is None:
                continue
            if name == "max_error_rate":
                pages = float(stats.get("pages", 0))
                used = float(stats.get("failed", 0)) / pages if pages else 0.0
                out[name] = BudgetStatus(name, round(used, 4), limit)
            else:
                out[name] = BudgetStatus(name, float(stats.get(_STAT_OF[name], 0)), limit)
        for status in self._resource_usage(elapsed_total):
            out[status.name] = status
        if self.page_limit:
            out["max_pages"] = BudgetStatus("max_pages", float(stats.get("pages", 0)), float(self.page_limit))
        if self.item_limit:
            out["max_items"] = BudgetStatus("max_items", float(stats.get("items", 0)), float(self.item_limit))
        return out

    def min_priority(self, stats: dict[str, Any], elapsed_total: float) -> int | None:
        """With ``budget_soft_limit``: the lowest priority still worth starting, or ``None`` (no restriction)."""
        if self.soft_limit is None:
            return None
        usage = self.usage(stats, elapsed_total)
        if any(s.fraction >= self.soft_limit for n, s in usage.items() if n != "max_error_rate"):
            return self.soft_priority
        return None
