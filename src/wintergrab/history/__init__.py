"""Crawl history: page snapshots, change detection between runs, and freshness.

* :func:`~wintergrab.history.snapshot_page` / :func:`~wintergrab.history.compare_snapshots`:
  fingerprints of a page, and what changed between two of them.
* :class:`~wintergrab.history.PageHistory`: every run's snapshots in one SQLite file,
  change reports between runs, and per-URL freshness (first seen, last seen, last changed,
  change rate, when to fetch again). Spiders record into it with ``history = "shop.history"``.
"""

from __future__ import annotations

from .snapshot import PageChange, PageSnapshot, compare_snapshots, snapshot_page
from .store import ChangeReport, Freshness, PageHistory, Run

__all__ = [
    "ChangeReport",
    "Freshness",
    "PageChange",
    "PageHistory",
    "PageSnapshot",
    "Run",
    "compare_snapshots",
    "snapshot_page",
]
