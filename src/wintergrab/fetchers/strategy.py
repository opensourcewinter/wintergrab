"""Adaptive fetching: plain HTTP first, a browser for the pages whose content needs JavaScript.

::

    class Shop(Spider):
        adaptive_fetch = True               # or a file that keeps what it learns: "shop.fetch.json"
        render_if_missing = [".price"]      # optional: what a usable page has

Pages are fetched over HTTP first. When the HTML is not enough
(:meth:`~wintergrab.Spider.needs_browser`: a ``render_if_missing`` selector finds
nothing, or the page is an app shell that builds its content with JavaScript), the
page is fetched again in a browser, which waits for the page's own requests to
finish and records its API calls (``response.captured``). Outcomes are counted per
URL pattern (``shop.example/p/{slug}``, see :func:`~wintergrab.urls.url_template`)
and per host: once the pages of a pattern needed a browser 80% of the time (3 pages
at least), its pages go to the browser directly, and one in ten is still tried over
HTTP in case the site changed.

This is about how pages are built, not about access: bot checks and refusals
(:meth:`~wintergrab.Spider.is_blocked`) are handled as before (the crawl slows down,
retries, then gives up), never by switching to a browser on its own.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from lxml import etree

from ..errors import ConfigurationError
from ..urls import url_template

__all__ = ["FetchStrategy", "PatternStats", "needs_javascript"]

_INVISIBLE = frozenset({"script", "style", "noscript", "template"})
# The elements JavaScript apps mount into (found from the page's ids, which is quick)
_MOUNT_IDS = frozenset({"root", "app", "__next", "__nuxt", "svelte", "main-app"})
_IDS = etree.XPath("//@id | //@data-reactroot")
_APP_ROOTS = etree.XPath("//app-root")
_NOSCRIPT = re.compile(
    r"(?:enable|requires?|turn on|need)\s+javascript|javascript\s+(?:is\s+)?(?:required|disabled)", re.I
)
# Server-rendered state: the data is in the HTML even when the text is not (two patterns, so that
# each can be searched for by its first characters)
_STATE = (
    re.compile(rb"""<script[^>]+(?:id=["']?__NU?XT_DATA__|type=["']?application/(?:ld\+)?json)[^>]*>""", re.I),
    re.compile(rb"window\.__(?:INITIAL_STATE|PRELOADED_STATE|APOLLO_STATE|NUXT)__\s*="),
)
_MIN_TEXT = 200  # visible characters of a page with content, at least
_MIN_STATE = 2_000  # bytes of embedded JSON that make the page's data
_EMPTY_MOUNT = 15  # an app mount point with less text than this is empty (nothing, or "Loading...")
_SHELL_TEXT = 1_000  # ... and matters when the page has less text than this in all


def _mounts(root: Any) -> list[Any]:
    found = [value.getparent() for value in _IDS(root) if value.attrname != "id" or str(value) in _MOUNT_IDS]
    found.extend(_APP_ROOTS(root))
    return found


def _chars(text: str, need: int) -> int:
    """Non-space characters in ``text``, counting no further than ``need`` (long texts are read in parts)."""
    count = 0
    step = max(need, 256)
    for start in range(0, len(text), step):
        count += len("".join(text[start : start + step].split()))
        if count >= need:
            break
    return count


def _visible(element: Any, enough: int) -> int:
    """Visible characters in ``element`` (spaces aside), counting no further than ``enough``."""
    total = 0
    hidden = 0  # depth inside <script>, <style>...
    for event, el in etree.iterwalk(element, events=("start", "end", "comment")):
        if event == "comment":
            text = el.tail if not hidden else None
        elif el.tag in _INVISIBLE:
            hidden += 1 if event == "start" else -1
            text = el.tail if event == "end" and not hidden and el is not element else None
        elif event == "start":
            text = el.text if not hidden else None
        else:
            text = el.tail if not hidden and el is not element else None
        if text:
            total += _chars(text, enough - total)
            if total >= enough:
                break
    return total


def _embedded_state(raw: bytes) -> int:
    """Bytes of the largest server-rendered JSON state in the page."""
    largest = 0
    for pattern in _STATE:
        for match in pattern.finditer(raw):
            end = raw.find(b"</script>", match.end())
            largest = max(largest, (end if end >= 0 else len(raw)) - match.end())
    return largest


def needs_javascript(response: Any) -> str | None:
    """Why an HTML page looks like it needs JavaScript to show its content, or ``None``.

    It does when a JavaScript app's mount point (``#root``, ``#app``, ``#__next``,
    ``app-root``...) is empty (under 15 characters of text: nothing, or "Loading...") on a
    page with under 1,000 characters of text, or when the page has under 200 characters of
    visible text and three scripts or more, or a ``<noscript>`` asking for JavaScript;
    unless its data is in the HTML anyway (``__NEXT_DATA__``, JSON-LD,
    ``window.__INITIAL_STATE__``..., 2 KB or more). Bot-check pages are not the
    business of this function: see :func:`~wintergrab.fetchers.blocking.looks_blocked`.
    """
    if not getattr(response, "is_html", False):
        return None
    root = response.selector.root
    if root is None:
        return None
    body = root.find("body")
    page = body if body is not None else root
    for mount in _mounts(root):
        if _visible(mount, _EMPTY_MOUNT) >= _EMPTY_MOUNT:
            continue
        # an empty mount point on a page with little text (not a widget on an article)
        if _visible(page, _SHELL_TEXT) >= _SHELL_TEXT or _embedded_state(response.body) >= _MIN_STATE:
            return None
        name = f"#{mount.get('id')}" if mount.get("id") else f"<{mount.tag}>"
        return f"its app mount point ({name}) is empty"
    if _visible(page, _MIN_TEXT) >= _MIN_TEXT or _embedded_state(response.body) >= _MIN_STATE:
        return None
    noscript = " ".join(" ".join(el.itertext()) for el in root.iter("noscript"))
    if _NOSCRIPT.search(noscript):
        return "little text, and a <noscript> asking for JavaScript"
    scripts = sum(1 for _ in root.iter("script"))
    if scripts >= 3:
        return f"little text, and {scripts} scripts"
    return None


@dataclass
class PatternStats:
    """What happened to the pages of one URL pattern (or host).

    Attributes:
        http_ok: Pages whose HTML was enough.
        http_short: Pages whose HTML was not, and went to the browser.
        browser_ok, browser_short: Pages the browser fetched, with and without their content.
        updated: When the counts last changed (Unix time).
    """

    http_ok: float = 0.0
    http_short: float = 0.0
    browser_ok: float = 0.0
    browser_short: float = 0.0
    updated: float = 0.0

    @property
    def http_pages(self) -> float:
        return self.http_ok + self.http_short

    @property
    def browser_share(self) -> float | None:
        """Share of the pages tried over HTTP that needed a browser (``None`` before any)."""
        return self.http_short / self.http_pages if self.http_pages else None

    def add(self, field: str, *, cap: float = 200.0) -> None:
        setattr(self, field, getattr(self, field) + 1)
        self.updated = time.time()
        if self.http_ok + self.http_short + self.browser_ok + self.browser_short > cap:
            # keep recent behaviour in charge: halve the old counts
            for name in ("http_ok", "http_short", "browser_ok", "browser_short"):
                setattr(self, name, getattr(self, name) / 2)

    def describe(self) -> str:
        parts = []
        if self.http_pages:
            parts.append(f"HTTP enough for {self.http_ok / self.http_pages:.0%} of {self.http_pages:g} page(s)")
        rendered = self.browser_ok + self.browser_short
        if rendered:
            parts.append(f"browser: {rendered:g} page(s), {self.browser_ok / rendered:.0%} with content")
        return "; ".join(parts) or "no pages yet"


class FetchStrategy:
    """Chooses between HTTP and a browser per URL pattern, and learns from each page (see the
    module docs).

    Args:
        path: A JSON file to load what was learned from, and to save it to (:meth:`save`).
        threshold: Share of a pattern's pages that needed a browser from which its pages go to
            the browser directly.
        min_pages: Pages of a pattern tried over HTTP before deciding that way. A pattern not
            seen yet follows its host once three times as many of the host's pages were tried.
        probe_every: One page in this many of a browser-first pattern is still tried over HTTP.
        capture: Record the API calls of rendered pages (``response.captured``): ``True`` for
            JSON responses, or a URL glob (see :class:`~wintergrab.AsyncBrowserFetcher`).
        wait_until: What the browser waits for before reading a page.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        threshold: float = 0.8,
        min_pages: int = 3,
        probe_every: int = 10,
        capture: bool | str = True,
        wait_until: str = "networkidle",
    ) -> None:
        if not 0 < threshold <= 1:
            raise ConfigurationError("threshold must be in (0, 1]", key="threshold")
        self.path = Path(path) if path is not None else None
        self.threshold = threshold
        self.min_pages = max(1, min_pages)
        self.probe_every = max(2, probe_every)
        self.capture = capture
        self.wait_until = wait_until
        self.patterns: dict[str, PatternStats] = {}
        self.hosts: dict[str, PatternStats] = {}
        self._decisions: dict[str, int] = {}
        #: False once the browser turned out to be unavailable: everything stays on HTTP
        self.available = True
        if self.path is not None and self.path.exists():
            self.load(self.path)

    @classmethod
    def coerce(cls, value: Any) -> FetchStrategy | None:
        """A spider's ``adaptive_fetch`` setting: ``True``, a file path or a strategy."""
        if value is None or value is False:
            return None
        if isinstance(value, FetchStrategy):
            return value
        if value is True:
            return cls()
        if isinstance(value, (str, os.PathLike)):
            return cls(value)
        raise ConfigurationError(
            f"adaptive_fetch must be True, a file path or a FetchStrategy, not {value!r}", key="adaptive_fetch"
        )

    # -- deciding ------------------------------------------------------------------------------ #
    @staticmethod
    def pattern(url: str) -> str:
        return url_template(url)

    def _stats(self, url: str) -> PatternStats | None:
        """The pattern's counts when it has enough pages tried over HTTP, else its host's when that
        has three times as many (one section of JavaScript pages does not make a whole site)."""
        stats = self.patterns.get(self.pattern(url))
        if stats is not None and stats.http_pages >= self.min_pages:
            return stats
        host = self.hosts.get(urlsplit(url).hostname or "")
        if host is not None and host.http_pages >= 3 * self.min_pages:
            return host
        return None

    def browser_first(self, url: str) -> bool:
        """Whether to fetch ``url`` in the browser right away (its pattern, or failing that its
        host, needed one for most pages), apart from the occasional HTTP probe."""
        if not self.available:
            return False
        stats = self._stats(url)
        share = stats.browser_share if stats is not None else None
        if share is None or share < self.threshold:
            return False
        key = self.pattern(url)
        self._decisions[key] = self._decisions.get(key, 0) + 1
        return self._decisions[key] % self.probe_every != 0  # every tenth page: try HTTP again

    def browser_options(self) -> dict[str, Any]:
        """Fetch options for a page sent to the browser."""
        options: dict[str, Any] = {"wait_until": self.wait_until}
        if self.capture:
            options["capture"] = self.capture
        return options

    # -- learning ------------------------------------------------------------------------------ #
    def record(self, url: str, via: str, ok: bool) -> None:
        """A page of ``url``'s pattern was fetched ``via`` ``"http"`` or ``"browser"``; ``ok``: its
        content was there."""
        field = f"{via}_{'ok' if ok else 'short'}"
        if field not in ("http_ok", "http_short", "browser_ok", "browser_short"):
            raise ValueError(f"via must be 'http' or 'browser', not {via!r}")
        self.patterns.setdefault(self.pattern(url), PatternStats()).add(field)
        self.hosts.setdefault(urlsplit(url).hostname or "", PatternStats()).add(field)

    def describe(self, limit: int = 10) -> str:
        """The patterns seen most, and how their pages were best fetched."""
        rows = sorted(
            self.patterns.items(), key=lambda kv: -(kv[1].http_pages + kv[1].browser_ok + kv[1].browser_short)
        )
        lines = [f"{pattern}: {stats.describe()}" for pattern, stats in rows[:limit]]
        if len(rows) > limit:
            lines.append(f"... {len(rows) - limit} more pattern(s)")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "patterns": {k: asdict(v) for k, v in sorted(self.patterns.items())},
            "hosts": {k: asdict(v) for k, v in sorted(self.hosts.items())},
        }

    def load(self, path: str | os.PathLike[str]) -> None:
        """Add what a :meth:`save` file says (a missing or unreadable file is an error)."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigurationError(f"cannot read fetch statistics from {path}: {exc}", key="adaptive_fetch") from exc
        for name, target in (("patterns", self.patterns), ("hosts", self.hosts)):
            for key, row in (data.get(name) or {}).items():
                known = {k: float(v) for k, v in row.items() if k in PatternStats.__dataclass_fields__}
                target[key] = PatternStats(**known)

    def save(self, path: str | os.PathLike[str] | None = None) -> None:
        """Write what was learned to ``path`` (default: the file it was loaded from), atomically."""
        target = Path(path) if path is not None else self.path
        if target is None:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")
        os.replace(tmp, target)

    def __repr__(self) -> str:
        return f"FetchStrategy({len(self.patterns)} pattern(s), path={str(self.path) if self.path else None!r})"
