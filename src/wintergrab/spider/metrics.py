"""Live crawl metrics: rolling rates, latency percentiles, per-domain throttle state, process resources.

``spider.metrics()`` (while a crawl runs) and ``CrawlResult.metrics`` (after
it) return a snapshot dict; :func:`to_prometheus` renders one in the
Prometheus text format for scraping.
"""

from __future__ import annotations

import os
import re
import sys
import time
from collections import deque
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .throttle import AutoThrottle

__all__ = ["CrawlMetrics", "current_rss", "peak_rss", "process_usage", "to_prometheus"]


def current_rss() -> int | None:
    """Resident memory of this process in bytes (``None`` where it cannot be read cheaply)."""
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/self/statm", "rb") as fh:
                resident = int(fh.read().split()[1])
            return resident * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            return None
    if sys.platform == "win32":
        return _windows_rss()
    return peak_rss()  # macOS & co: the peak is the best the stdlib offers


def _windows_rss() -> int | None:  # pragma: no cover - Windows only
    try:
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        process = ctypes.windll.kernel32.GetCurrentProcess()  # type: ignore[attr-defined]
        if not ctypes.windll.psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):  # type: ignore[attr-defined]
            return None
        return int(counters.WorkingSetSize)
    except Exception:
        return None


def peak_rss() -> int | None:
    """Peak resident memory in bytes (``None`` on platforms without ``resource``)."""
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


def process_usage() -> dict[str, Any]:
    """CPU seconds used and memory of this process."""
    return {"cpu_seconds": round(time.process_time(), 3), "rss_bytes": current_rss(), "peak_rss_bytes": peak_rss()}


class CrawlMetrics:
    """Rolling measurements kept by the engine (cheap to update, computed on demand)."""

    def __init__(self, window: float = 30.0, latency_samples: int = 2048) -> None:
        self.window = window
        self.latencies: deque[float] = deque(maxlen=latency_samples)
        self._samples: deque[tuple[float, float, float, float, float]] = deque()  # t, pages, items, bytes, requests
        self._domains: dict[str, deque[tuple[float, int]]] = {}

    def observe_latency(self, seconds: float) -> None:
        self.latencies.append(seconds)

    def sample(self, stats: Mapping[str, Any], throttle: AutoThrottle | None, now: float | None = None) -> None:
        """Record counters for rate computations (the engine calls this about once a second)."""
        now = time.monotonic() if now is None else now
        self._samples.append(
            (
                now,
                float(stats.get("pages", 0)),
                float(stats.get("items", 0)),
                float(stats.get("bytes", 0)),
                float(stats.get("requests", 0)),
            )
        )
        while len(self._samples) > 2 and now - self._samples[0][0] > self.window:
            self._samples.popleft()
        if throttle is not None:
            for domain, slot in throttle.slots.items():
                history = self._domains.setdefault(domain, deque())
                history.append((now, slot.requests))
                while len(history) > 2 and now - history[0][0] > self.window:
                    history.popleft()

    def rates(self) -> dict[str, float]:
        """Pages, items, bytes and requests per second over the window."""
        if len(self._samples) < 2:
            return {
                "pages_per_second": 0.0,
                "items_per_second": 0.0,
                "bytes_per_second": 0.0,
                "requests_per_second": 0.0,
            }
        first, last = self._samples[0], self._samples[-1]
        span = last[0] - first[0]
        if span <= 0:
            span = 1.0
        return {
            "pages_per_second": round((last[1] - first[1]) / span, 3),
            "items_per_second": round((last[2] - first[2]) / span, 3),
            "bytes_per_second": round((last[3] - first[3]) / span, 1),
            "requests_per_second": round((last[4] - first[4]) / span, 3),
        }

    def domain_rate(self, domain: str) -> float:
        history = self._domains.get(domain)
        if not history or len(history) < 2:
            return 0.0
        span = history[-1][0] - history[0][0]
        return round((history[-1][1] - history[0][1]) / span, 3) if span > 0 else 0.0

    def latency(self) -> dict[str, float | None]:
        """Latency percentiles (seconds) over the most recent responses."""
        if not self.latencies:
            return {"p50": None, "p90": None, "p99": None, "mean": None}
        ordered = sorted(self.latencies)
        n = len(ordered)

        def pct(q: float) -> float:
            return round(ordered[min(n - 1, int(q * n))], 4)

        return {"p50": pct(0.5), "p90": pct(0.9), "p99": pct(0.99), "mean": round(sum(ordered) / n, 4)}

    def snapshot(
        self,
        stats: Mapping[str, Any],
        *,
        throttle: AutoThrottle | None = None,
        queued: int = 0,
        in_flight: int = 0,
        elapsed: float = 0.0,
        budget: Mapping[str, Any] | None = None,
        domains: int = 20,
    ) -> dict[str, Any]:
        """Everything worth watching about a running crawl, as one JSON-serialisable dict."""
        now = time.monotonic()
        per_domain = []
        if throttle is not None:
            busiest = sorted(throttle.slots.values(), key=lambda s: (-s.active, -s.requests))[:domains]
            for slot in busiest:
                state = throttle.state(slot.domain, now)
                state["current_rate"] = self.domain_rate(slot.domain)
                per_domain.append(state)
        requests = float(stats.get("requests", 0)) or 0.0
        failed = float(stats.get("failed", 0))
        pages = float(stats.get("pages", 0))
        return {
            "elapsed_seconds": round(elapsed, 3),
            "pages": int(pages),
            "requests": int(requests),
            "responses": int(stats.get("responses", 0)),
            "items": int(stats.get("items", 0)),
            "bytes": int(stats.get("bytes", 0)),
            "browser_pages": int(stats.get("browser_pages", 0)),
            "errors": int(stats.get("errors", 0)),
            "failed": int(failed),
            "retries": int(stats.get("retries", 0)),
            "blocked": int(stats.get("blocked", 0)),
            "success_rate": round(1 - failed / pages, 4) if pages else None,
            "queued": queued,
            "in_flight": in_flight,
            "active_domains": sum(1 for s in (throttle.slots.values() if throttle else ()) if s.active),
            "rates": self.rates(),
            "latency": self.latency(),
            "domains": per_domain,
            "budget": dict(budget or {}),
            "process": process_usage(),
        }


# --------------------------------------------------------------------------- #
# Prometheus text format
# --------------------------------------------------------------------------- #
_METRIC_NAME = re.compile(r"[^a-zA-Z0-9_]")


def _label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def to_prometheus(
    snapshot: Mapping[str, Any], stats: Mapping[str, Any] | None = None, *, prefix: str = "wintergrab_"
) -> str:
    """Render a metrics snapshot (and optionally the raw stats counters) in the Prometheus text format."""
    lines: list[str] = []

    def gauge(name: str, value: Any, labels: Mapping[str, Any] | None = None, kind: str = "gauge") -> None:
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        metric = prefix + _METRIC_NAME.sub("_", name)
        if not any(line.startswith(f"# TYPE {metric} ") for line in lines):
            lines.append(f"# TYPE {metric} {kind}")
        label_text = ""
        if labels:
            label_text = "{" + ",".join(f'{k}="{_label(v)}"' for k, v in labels.items()) + "}"
        lines.append(f"{metric}{label_text} {value}")

    for key in ("pages", "requests", "responses", "items", "bytes", "browser_pages", "errors", "failed", "retries",
                "blocked"):  # fmt: skip
        gauge(f"{key}_total", snapshot.get(key), kind="counter")
    for key in ("queued", "in_flight", "active_domains", "elapsed_seconds", "success_rate"):
        gauge(key, snapshot.get(key))
    for key, value in (snapshot.get("rates") or {}).items():
        gauge(key, value)
    for quantile, value in (snapshot.get("latency") or {}).items():
        if quantile.startswith("p"):
            gauge("latency_seconds", value, {"quantile": f"0.{quantile[1:]}"})
    for domain in snapshot.get("domains") or ():
        labels = {"domain": domain.get("domain")}
        for key in ("active", "concurrency", "delay", "target_delay", "avg_latency", "allowed_rate", "current_rate",
                    "backoffs", "requests"):  # fmt: skip
            gauge(f"domain_{key}", domain.get(key), labels)
    for key, value in (snapshot.get("process") or {}).items():
        gauge(f"process_{key}", value)
    for key, value in (snapshot.get("budget") or {}).items():
        if isinstance(value, Mapping):
            gauge("budget_used_ratio", value.get("fraction"), {"budget": key})
    for key, value in (stats or {}).items():
        if "/" in key and isinstance(value, (int, float)) and not isinstance(value, bool):
            family, _, label = key.partition("/")
            gauge(f"{family}_by_label_total", value, {"label": label}, kind="counter")
    return "\n".join(lines) + "\n"
