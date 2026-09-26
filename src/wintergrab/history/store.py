"""A crawl history: what every run saw, how pages changed between runs, and when to look again.

::

    history = PageHistory("shop.history")
    run = history.start_run("shop")
    history.observe(run, response, items=items)       # for every page (spiders do it with history=...)
    history.finish_run(run, "finished")
    print(history.compare(name="shop").describe())    # this run against the one before
    history.freshness("https://shop.example/p/1")     # first/last seen, last changed, change rate, when to look again

The history is one SQLite file holding each run, a :class:`~wintergrab.history.PageSnapshot`
per page per run (and the HTML and items when asked), and per URL when it was
first and last seen, when it last changed and how often it changes.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
import time
import zlib
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ConfigurationError
from .snapshot import PageChange, PageSnapshot, compare_snapshots, snapshot_page

__all__ = ["ChangeReport", "Freshness", "PageHistory", "Run"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, started REAL NOT NULL,
    finished REAL, status TEXT, stats TEXT
);
CREATE INDEX IF NOT EXISTS runs_by_name ON runs (name, id);
CREATE TABLE IF NOT EXISTS pages (
    run INTEGER NOT NULL, url TEXT NOT NULL, status INTEGER, fetched REAL, snapshot TEXT NOT NULL,
    html BLOB, items BLOB, PRIMARY KEY (run, url)
);
CREATE INDEX IF NOT EXISTS pages_by_url ON pages (url, run);
CREATE TABLE IF NOT EXISTS skipped (run INTEGER NOT NULL, url TEXT NOT NULL, PRIMARY KEY (run, url));
CREATE TABLE IF NOT EXISTS urls (
    url TEXT PRIMARY KEY, first_seen REAL NOT NULL, last_seen REAL NOT NULL, last_changed REAL,
    observations INTEGER NOT NULL, changes INTEGER NOT NULL, content TEXT, status INTEGER, last_run INTEGER
);
"""
_GONE = frozenset({404, 410})
_DAY = 86_400.0


def _pages(count: int) -> str:
    return f"{count:,} page{'' if count == 1 else 's'}"


@dataclass
class Run:
    """One crawl recorded in the history."""

    id: int
    name: str
    started: float
    finished: float | None = None
    status: str | None = None
    stats: dict[str, Any] = field(default_factory=dict)
    pages: int = 0

    @property
    def complete(self) -> bool:
        """The crawl ran to the end: a page it did not see is gone, not merely not visited."""
        return self.status == "finished"

    def describe(self) -> str:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.started))
        return f"run {self.id} ({self.name}, {when}, {self.status or 'running'}): {self.pages:,} pages"


@dataclass
class Freshness:
    """How a URL changes, from its history.

    Attributes:
        first_seen, last_seen, last_changed: Seconds since the epoch.
        observations: How many times it was fetched; ``changes``: how many of those found new content.
        rate: Estimated changes per day (``None`` after a single fetch).
        recrawl_after: Seconds after ``last_seen`` when the page has probably changed (by default,
            when the chance of a change reaches one half).
        fresh: The probability that the page is still as last seen, now.
    """

    url: str
    first_seen: float
    last_seen: float
    last_changed: float | None
    observations: int
    changes: int
    rate: float | None
    recrawl_after: float
    fresh: float

    def due(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.last_seen + self.recrawl_after

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass
class ChangeReport:
    """What changed between two runs.

    Attributes:
        old, new: The runs compared (``old`` is ``None`` for a first run).
        added: Pages seen for the first time.
        removed: Pages gone: now 404 or 410, or not found by a crawl that ran to the end.
        missing: Pages the new run did not reach, when it stopped early (a limit, a pause).
        skipped: Pages not fetched because they were still probably fresh (``skip_fresh``).
        modified: Pages that changed, with what changed.
        unchanged: How many pages were fetched again and had not changed.
    """

    old: Run | None
    new: Run
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    modified: list[PageChange] = field(default_factory=list)
    unchanged: int = 0

    def kinds(self) -> dict[str, int]:
        """How many modified pages had each kind of change, most common first."""
        return dict(Counter(kind for change in self.modified for kind in change.kinds).most_common())

    def counts(self) -> dict[str, int]:
        return {
            "added": len(self.added),
            "removed": len(self.removed),
            "modified": len(self.modified),
            "unchanged": self.unchanged,
            "missing": len(self.missing),
            "skipped": len(self.skipped),
        }

    def summary(self) -> str:
        """The counts, one line each: ``+ 184 pages``, ``- 27 pages``, ``~ 913 pages modified (...)``."""
        lines = [f"+ {_pages(len(self.added))}", f"- {_pages(len(self.removed))}"]
        kinds = self.kinds()
        detail = f" ({', '.join(f'{kind} {count:,}' for kind, count in kinds.items())})" if kinds else ""
        lines.append(f"~ {_pages(len(self.modified))} modified{detail}")
        lines.append(f"= {_pages(self.unchanged)} unchanged")
        if self.missing:
            lines.append(f"? {_pages(len(self.missing))} not reached (the run stopped early)")
        if self.skipped:
            lines.append(f"  {_pages(len(self.skipped))} skipped as still fresh")
        return "\n".join(lines)

    def describe(self, limit: int = 10) -> str:
        against = f"run {self.old.id}" if self.old else "nothing (first run)"
        lines = [f"run {self.new.id} ({self.new.name}) against {against}", self.summary()]
        for title, urls in (("added", self.added), ("removed", self.removed)):
            if urls:
                lines.append(f"{title}:")
                lines.extend(f"  {url}" for url in urls[:limit])
                if len(urls) > limit:
                    lines.append(f"  ... and {len(urls) - limit:,} more")
        if self.modified:
            lines.append("modified:")
            lines.extend(f"  {change}" for change in self.modified[:limit])
            if len(self.modified) > limit:
                lines.append(f"  ... and {len(self.modified) - limit:,} more")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "old": self.old.id if self.old else None,
            "new": self.new.id,
            **self.counts(),
            "kinds": self.kinds(),
            "added_urls": list(self.added),
            "removed_urls": list(self.removed),
            "modified_pages": [change.to_dict() for change in self.modified],
        }


class PageHistory:
    """The history of crawls in one SQLite file (see the module docs).

    Args:
        path: The history file (created if missing).
        keep_html: Also keep every page's HTML (compressed), not just its fingerprints.
        keep_items: Keep the items extracted from every page (compressed).
        target: For :meth:`freshness`: recrawl once the chance that a page changed reaches this.
        min_interval, max_interval: Bounds, in seconds, for the recrawl interval (1 hour, 30 days).
        first_interval: Recrawl interval, in seconds, of a page fetched only once (1 day).
    """

    def __init__(
        self,
        path: str | Path,
        *,
        keep_html: bool = False,
        keep_items: bool = True,
        target: float = 0.5,
        min_interval: float = 3_600,
        max_interval: float = 30 * _DAY,
        first_interval: float = _DAY,
    ) -> None:
        if not 0 < target < 1:
            raise ConfigurationError("target must be between 0 and 1")
        self.path = Path(path)
        self.keep_html = keep_html
        self.keep_items = keep_items
        self.target = target
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.first_interval = first_interval
        if self.path.parent != Path():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._db = sqlite3.connect(self.path)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
        except sqlite3.DatabaseError as exc:
            raise ConfigurationError(f"{self.path} is not a history file: {exc}") from exc
        self._pending = 0

    # -- recording ---------------------------------------------------------------------------- #
    def start_run(self, name: str) -> int:
        """Begin recording a crawl; returns its run id."""
        cursor = self._db.execute("INSERT INTO runs (name, started) VALUES (?, ?)", (name, time.time()))
        self._db.commit()
        return int(cursor.lastrowid or 0)

    def finish_run(self, run: int, status: str, stats: dict[str, Any] | None = None) -> None:
        self._db.execute(
            "UPDATE runs SET finished = ?, status = ?, stats = ? WHERE id = ?",
            (time.time(), status, json.dumps(stats or {}, default=str), run),
        )
        self._db.commit()
        self._pending = 0

    def observe(
        self,
        run: int,
        page: Any,
        *,
        items: Iterable[Any] | None = None,
        url: str | None = None,
        status: int | None = None,
        fetched_at: float | None = None,
    ) -> PageSnapshot:
        """Record a fetched page (a :class:`~wintergrab.Response`, a Selector or HTML) and the items
        extracted from it; returns its snapshot. ``fetched_at`` (seconds since the epoch) defaults
        to now."""
        rows = list(items) if items is not None else None
        snap = snapshot_page(page, url=url, status=status, items=rows, fetched_at=fetched_at)
        html = None
        if self.keep_html:
            body = getattr(page, "body", None)
            html = zlib.compress(body if isinstance(body, bytes) else str(getattr(page, "html", page)).encode(), 6)
        stored_items = None
        if self.keep_items and rows:
            data = [row.to_dict() if hasattr(row, "to_dict") else row for row in rows]
            stored_items = zlib.compress(json.dumps(data, default=str, ensure_ascii=False).encode("utf-8"), 6)
        self._store(run, snap, html, stored_items)
        return snap

    def observe_status(self, run: int, url: str, status: int, *, fetched_at: float | None = None) -> PageSnapshot:
        """Record a page that answered with an error status (a 404 means it is gone)."""
        snap = PageSnapshot(url=url, status=status, fetched_at=fetched_at if fetched_at is not None else time.time())
        self._store(run, snap, None, None)
        return snap

    def mark_skipped(self, run: int, url: str) -> None:
        """Record that a page was not fetched because it was probably still fresh."""
        self._db.execute("INSERT OR IGNORE INTO skipped (run, url) VALUES (?, ?)", (run, url))
        self._maybe_commit()

    def _store(self, run: int, snap: PageSnapshot, html: bytes | None, items: bytes | None) -> None:
        now, url = snap.fetched_at, snap.url
        content = snap.content if snap.status < 400 else f"status:{snap.status}"
        self._db.execute(
            "INSERT OR REPLACE INTO pages (run, url, status, fetched, snapshot, html, items) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run, url, snap.status, now, json.dumps(snap.to_dict()), html, items),
        )
        row = self._db.execute(
            "SELECT content, observations, changes, last_changed, last_run FROM urls WHERE url = ?", (url,)
        ).fetchone()
        if row is None:
            self._db.execute(
                "INSERT INTO urls (url, first_seen, last_seen, last_changed, observations, changes, content, status,"
                " last_run) VALUES (?, ?, ?, NULL, 1, 0, ?, ?, ?)",
                (url, now, now, content, snap.status, run),
            )
        else:
            previous, observations, changes, last_changed, last_run = row
            changed = previous != content
            again = last_run == run  # fetched twice in one run: one observation
            self._db.execute(
                "UPDATE urls SET last_seen = ?, last_changed = ?, observations = ?, changes = ?, content = ?,"
                " status = ?, last_run = ? WHERE url = ?",
                (
                    now,
                    now if changed else last_changed,
                    observations + (0 if again else 1),
                    changes + (1 if changed and not again else 0),
                    content,
                    snap.status,
                    run,
                    url,
                ),
            )
        self._maybe_commit()

    def _maybe_commit(self) -> None:
        self._pending += 1
        if self._pending >= 200:
            self._db.commit()
            self._pending = 0

    def commit(self) -> None:
        self._db.commit()
        self._pending = 0

    def close(self) -> None:
        self._db.commit()
        self._db.close()

    def __enter__(self) -> PageHistory:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- reading ------------------------------------------------------------------------------ #
    def runs(self, name: str | None = None) -> list[Run]:
        """Recorded runs, oldest first (only the runs of ``name`` if given)."""
        query = "SELECT r.id, r.name, r.started, r.finished, r.status, r.stats, COUNT(p.url) FROM runs r"
        query += " LEFT JOIN pages p ON p.run = r.id"
        args: tuple[Any, ...] = ()
        if name is not None:
            query += " WHERE r.name = ?"
            args = (name,)
        query += " GROUP BY r.id ORDER BY r.id"
        return [
            Run(row[0], row[1], row[2], row[3], row[4], json.loads(row[5]) if row[5] else {}, row[6])
            for row in self._db.execute(query, args)
        ]

    def run(self, run_id: int) -> Run:
        for run in self.runs():
            if run.id == run_id:
                return run
        raise ConfigurationError(f"no run {run_id} in {self.path}")

    def snapshots(self, url: str) -> list[tuple[int, PageSnapshot]]:
        """Every snapshot of ``url``: ``(run id, snapshot)``, oldest first."""
        rows = self._db.execute("SELECT run, snapshot FROM pages WHERE url = ? ORDER BY run", (url,))
        return [(run, PageSnapshot.from_dict(json.loads(data))) for run, data in rows]

    def page_html(self, run: int, url: str) -> str | None:
        """The HTML of a page as fetched in ``run``, if it was kept (``keep_html``)."""
        row = self._db.execute("SELECT html FROM pages WHERE run = ? AND url = ?", (run, url)).fetchone()
        return zlib.decompress(row[0]).decode("utf-8", errors="replace") if row and row[0] else None

    def page_items(self, run: int, url: str) -> list[Any]:
        """The items extracted from a page in ``run``, if they were kept."""
        row = self._db.execute("SELECT items FROM pages WHERE run = ? AND url = ?", (run, url)).fetchone()
        return json.loads(zlib.decompress(row[0])) if row and row[0] else []

    def _pages(self, runs: list[int]) -> dict[str, PageSnapshot]:
        """The latest snapshot of every URL seen in ``runs``."""
        if not runs:
            return {}
        marks = ",".join("?" * len(runs))
        # only "?" placeholders are formatted in: the run ids are passed as parameters
        rows = self._db.execute(f"SELECT url, snapshot FROM pages WHERE run IN ({marks}) ORDER BY run", runs)
        return {url: PageSnapshot.from_dict(json.loads(data)) for url, data in rows}

    def _urls(self, table: str, run: int) -> set[str]:
        """URLs of ``run`` in ``table`` ("pages" or "skipped": never user input)."""
        return {row[0] for row in self._db.execute(f"SELECT url FROM {table} WHERE run = ?", (run,))}

    def compare(self, old: int | None = None, new: int | None = None, *, name: str | None = None) -> ChangeReport:
        """What changed from run ``old`` to run ``new``.

        By default ``new`` is the latest run (of ``name``, if given) and ``old`` the one before it
        with the same name. Each page is compared with its latest snapshot as of ``old``, so pages
        skipped as fresh in between still compare with what was last seen.
        """
        runs = self.runs(name)
        if not runs:
            raise ConfigurationError(f"no runs{f' of {name!r}' if name else ''} in {self.path}")
        new_run = self.run(new) if new is not None else runs[-1]
        same = [r for r in self.runs(new_run.name) if r.id < new_run.id]
        old_run = self.run(old) if old is not None else (same[-1] if same else None)
        report = ChangeReport(old_run, new_run)
        now = self._pages([new_run.id])
        earlier_ids = [r.id for r in self.runs(new_run.name) if old_run is not None and r.id <= old_run.id]
        before = self._pages(earlier_ids)
        alive = (self._urls("pages", old_run.id) | self._urls("skipped", old_run.id)) if old_run else set()
        alive = {url for url in alive if url not in before or before[url].status not in _GONE}
        skipped = self._urls("skipped", new_run.id)
        for url, snap in now.items():
            previous = before.get(url)
            if previous is None or previous.status in _GONE:
                if snap.status < 400:
                    report.added.append(url)
            elif snap.status in _GONE:
                report.removed.append(url)
            else:
                change = compare_snapshots(previous, snap)
                if change is None:
                    report.unchanged += 1
                else:
                    report.modified.append(change)
        for url in sorted(alive - now.keys()):
            if url in skipped:
                report.skipped.append(url)
            elif new_run.complete:
                report.removed.append(url)
            else:
                report.missing.append(url)
        return report

    def freshness(self, url: str, *, now: float | None = None) -> Freshness | None:
        """How ``url`` changes and when to fetch it again, from its history (``None`` if never seen).

        The change rate uses Cho and Garcia-Molina's estimator for pages checked at intervals
        (a page can change several times between two fetches and look changed once), with half a
        change added so that a few unchanged fetches do not mean "never changes".
        """
        row = self._db.execute(
            "SELECT first_seen, last_seen, last_changed, observations, changes FROM urls WHERE url = ?", (url,)
        ).fetchone()
        if row is None:
            return None
        first_seen, last_seen, last_changed, observations, changes = row
        now = now if now is not None else time.time()
        intervals = observations - 1
        rate = None
        if intervals > 0 and last_seen > first_seen:
            mean_days = (last_seen - first_seen) / _DAY / intervals
            n, x = intervals + 1, changes + 0.5
            rate = -math.log((n - x + 0.5) / (n + 0.5)) / mean_days
        if rate is None:
            recrawl = self.first_interval
        else:
            recrawl = -math.log(1 - self.target) / rate * _DAY if rate > 0 else self.max_interval
        recrawl = min(self.max_interval, max(self.min_interval, recrawl))
        fresh = math.exp(-(rate or 0.0) * max(0.0, now - last_seen) / _DAY) if rate is not None else 1.0
        return Freshness(
            url, first_seen, last_seen, last_changed, observations, changes, rate, round(recrawl, 1), round(fresh, 4)
        )

    def change_frequency(self, run: int | None = None) -> float | None:
        """The median change rate (changes per day) of the pages of ``run`` (all pages when ``None``)
        that were fetched at least twice."""
        if run is None:
            urls: Iterable[str] = self.urls()
        else:
            urls = [row[0] for row in self._db.execute("SELECT url FROM pages WHERE run = ?", (run,))]
        rates = [info.rate for url in urls if (info := self.freshness(url)) is not None and info.rate is not None]
        return round(statistics.median(rates), 4) if rates else None

    def due(self, url: str, now: float | None = None) -> bool:
        """Whether ``url`` should be fetched again now (always, for a page never seen)."""
        info = self.freshness(url, now=now)
        return info is None or info.due(now)

    def urls(self) -> Iterator[str]:
        """Every URL in the history."""
        for (url,) in self._db.execute("SELECT url FROM urls ORDER BY url"):
            yield url

    def __repr__(self) -> str:
        return f"PageHistory({str(self.path)!r})"
