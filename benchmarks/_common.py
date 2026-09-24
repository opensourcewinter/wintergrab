"""Measurement helpers shared by the ``bench_*.py`` crawler scripts.

Kept dependency-free so every script can run from its own virtualenv.
"""

from __future__ import annotations

import argparse
import json
import platform
import resource
import sys
import time
from importlib import metadata
from typing import Any

# CSS selectors every crawler uses, so they all do the same work per page.
ITEM_LINKS = "div.card a.item-link::attr(href)"
PAGE_LINKS = "nav.pager a::attr(href)"
ITEM_NAME = "h1::text"
ITEM_PRICE = ".price::text"


def cpu_seconds() -> float:
    """User + system CPU of this process and its (waited-for) children."""
    total = 0.0
    for who in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN):
        usage = resource.getrusage(who)
        total += usage.ru_utime + usage.ru_stime
    return total


def peak_rss_mb() -> float:
    """Peak resident set size of this process (Linux reports KB)."""
    kb = max(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
    return kb / 1024 if sys.platform != "darwin" else kb / 1024 / 1024


def versions(*packages: str) -> dict[str, str]:
    out = {"python": platform.python_version()}
    for name in packages:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = "not installed"
    return out


class Meter:
    """Wall and CPU time of the crawl itself (interpreter start-up and imports excluded)."""

    def __init__(self) -> None:
        self.wall0 = time.perf_counter()
        self.cpu0 = cpu_seconds()

    def result(self, tool: str, *, listing_pages: int, items: int, bad_items: int, **extra: Any) -> dict[str, Any]:
        wall = time.perf_counter() - self.wall0
        cpu = cpu_seconds() - self.cpu0
        responses = listing_pages + items
        return {
            "tool": tool,
            "responses": responses,
            "listing_pages": listing_pages,
            "items": items,
            "bad_items": bad_items,
            "wall_s": round(wall, 4),
            "cpu_s": round(cpu, 4),
            "cpu_total_s": round(cpu_seconds(), 4),
            "peak_rss_mb": round(peak_rss_mb(), 1),
            "pages_per_s": round(responses / wall, 1) if wall else 0.0,
            **extra,
        }


def emit(result: dict[str, Any]) -> None:
    """Print the result as the last stdout line, prefixed so ``run.py`` can find it."""
    print("RESULT " + json.dumps(result, sort_keys=True), flush=True)


def base_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--url", required=True, help="base URL of benchmarks/fastserver.py")
    parser.add_argument("--concurrency", type=int, default=64, help="max requests in flight (global and per domain)")
    return parser


def check_item(item: dict[str, Any]) -> bool:
    """An item is good if it has a name, a price and its URL."""
    return bool(item.get("name")) and str(item.get("price") or "").startswith("$") and "/item/" in item.get("url", "")
