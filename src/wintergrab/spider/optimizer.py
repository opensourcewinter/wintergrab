"""Automatic crawl optimization: learn during a crawl which URL patterns are worth fetching.

::

    class Shop(Spider):
        optimize = True                  # or a file that keeps what it learns: "shop.optimizer.json"

Pages are grouped by URL pattern (``shop.example/p/{slug}``, see :func:`~wintergrab.urls.url_template`;
below a site's first path segment, more than five different words in one place count as one, as in
``shop.example/tag/{word}``).
For each pattern the optimizer counts the pages fetched, the items their callbacks yielded (and the
pipelines kept), and whether they led to pages that yielded items. It uses what it learns three ways:

1. **Order.** Requests of patterns whose pages yield items go first, then those of patterns whose
   pages lead to such pages (listings, categories), then the rest. Nothing is left out for that, but
   with a limit on items (``max_items``) fewer pages are fetched to reach it.
2. **Barren patterns are skipped.** A page is *settled* once the pages it led to (two levels down,
   pages of its own pattern aside) have been fetched. A pattern with 20 settled pages of which none
   yielded an item or led to a page that did, ever, is barren: its requests are skipped, apart from
   one in ten, fetched in case. A probe that finds something makes the pattern productive for good.
   A pattern that gave something once is never skipped, even when its later pages only lead to
   items found already (the pages it leads to may still lead further). Requests
   queued before their pattern was found barren are skipped when their turn comes. Nothing is
   skipped before the crawl has found an item, so crawls without items (a link checker) are never
   pruned.
3. **Parameters that change nothing are dropped.** When pages that differ in one query parameter
   only (``?ref=nav``, ``?ref=footer``, or none) have the same text and links, twice, and never
   otherwise, that parameter is dropped from the pattern's later URLs, and a URL whose page was
   already fetched with it is not fetched again. JavaScript app shells, whose HTML is the same
   whatever the URL, teach nothing.

Start requests, sitemaps and requests yielded with ``dont_filter`` are never skipped or rewritten.
"""

from __future__ import annotations

import itertools
import json
import os
import random
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from lxml import etree

from ..errors import ConfigurationError
from ..fetchers.strategy import needs_javascript
from ..request import Request
from ..urls import url_template

__all__ = ["CrawlOptimizer", "PatternValue"]

_META = "_optimizer"  # request.meta: [page id, ancestors' ids, the ancestors that wait for it, pattern]
_PROBE = "_optimizer_probe"  # request.meta: let through a barren pattern's skipping, to check on it
_CAP = 200.0  # pages of a pattern before its old counts are halved (recent pages weigh more)
_MAX_GROUPS = 20_000  # pages remembered to compare parameters (the oldest are forgotten)
_SIBLINGS = 5  # different words side by side (below the first path segment) before they count as one
_HREFS = etree.XPath("//a/@href")
_TEXT = etree.XPath("string()")


@dataclass
class PatternValue:
    """What the pages of one URL pattern gave.

    Attributes:
        pages: Pages fetched whose callback ran.
        items: Items their callbacks yielded (kept by the pipelines).
        settled: Pages whose links (two levels down, the pattern's own pages aside) were all followed.
        productive: Settled pages that yielded an item or led to a page that did.
        skipped: Requests not fetched because the pattern was barren.
        queued: Requests waiting to be fetched.
        updated: When the counts last changed (Unix time).
    """

    pages: float = 0.0
    items: float = 0.0
    settled: float = 0.0
    productive: float = 0.0
    skipped: float = 0.0
    queued: float = 0.0
    updated: float = 0.0

    @property
    def rate(self) -> float | None:
        """Items per page (``None`` before 3 pages)."""
        return self.items / self.pages if self.pages >= 3 else None

    def barren(self, min_pages: int) -> bool:
        """``min_pages`` settled pages at least, and none that ever gave anything: a pattern that did
        (listings whose later pages only link to items already found) is never skipped, because the
        pages it leads to may still lead further."""
        return self.settled >= min_pages and self.productive == 0

    def halve(self) -> None:
        for name in ("pages", "items", "settled"):
            setattr(self, name, getattr(self, name) / 2)
        if self.productive:
            self.productive = max(1.0, self.productive / 2)  # once productive, always


class CrawlOptimizer:
    """Learns which URL patterns give items and acts on it (see the module docs).

    Args:
        path: A JSON file to load what was learned from, and to save it to (:meth:`save`).
        min_pages: Settled pages of a pattern before it can be found barren.
        probe_every: One request in this many of a barren pattern is still fetched.
        depth: How many levels down a page's links count towards it.
        prioritize, prune, parameters: Which of the three optimizations to make.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        min_pages: int = 20,
        probe_every: int = 10,
        depth: int = 2,
        prioritize: bool = True,
        prune: bool = True,
        parameters: bool = True,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self.min_pages = max(1, min_pages)
        self.probe_every = max(2, probe_every)
        self.depth = max(1, depth)
        self.prioritize = prioritize
        self.prune = prune
        self.parameters = parameters
        self.patterns: dict[str, PatternValue] = {}
        #: Per path pattern and query parameter: [pairs of pages alike, pairs that differed].
        self.evidence: dict[str, dict[str, list[int]]] = {}
        self._dropped: dict[str, set[str]] = {}
        #: Items found (this crawl, and the crawls it learned from).
        self.found = 0.0
        #: Different URLs this crawl did not fetch because their pattern was barren.
        self.skipped = 0
        # pages whose links are being followed: id -> [pattern, open count, productive, fetched]
        self._open: dict[int, list[Any]] = {}
        self._ids = itertools.count(random.getrandbits(40) << 20)  # unique across resumed crawls
        self._probes: dict[str, int] = {}
        self._skipped_urls: set[str] = set()
        self._groups: OrderedDict[tuple[Any, ...], dict[tuple[str | None, str | None], int]] = OrderedDict()
        self._siblings: dict[str, set[str]] = {}
        self._general: set[str] = set()  # places in paths whose words count as one
        self._queried: set[str] = set()  # path patterns seen with a query string (pages worth comparing)
        self._last = ("", "")  # the last URL whose pattern was asked for, and its pattern
        if self.path is not None and self.path.exists():
            self.load(self.path)

    @classmethod
    def coerce(cls, value: Any, *, crawl_dir: str | os.PathLike[str] | None = None) -> CrawlOptimizer | None:
        """A spider's ``optimize`` setting: ``True``, a file path or an optimizer. With ``True`` and a
        ``crawl_dir``, what was learned is kept there (``optimizer.json``) for resumed and later crawls."""
        if value is None or value is False:
            return None
        if isinstance(value, CrawlOptimizer):
            return value
        if value is True:
            return cls(Path(crawl_dir) / "optimizer.json" if crawl_dir else None)
        if isinstance(value, (str, os.PathLike)):
            return cls(value)
        raise ConfigurationError(
            f"optimize must be True, a file path or a CrawlOptimizer, not {value!r}", key="optimize"
        )

    def pattern(self, url: str) -> str:
        """``url``'s pattern: :func:`~wintergrab.urls.url_template`, with the words of a place in the path
        that has more than five (below the first segment) counted as one: ``shop.example/tag/{word}``."""
        if url == self._last[0]:
            return self._last[1]  # asked again for the same request (skip, boost, enqueued)
        template = url_template(url)
        head, mark, query = template.partition("?")
        segments = head.split("/")  # [host, first segment, ...]
        for i in range(2, len(segments)):
            word = segments[i]
            if not word or word.startswith("{"):
                continue
            prefix = "/".join(segments[:i])
            if prefix not in self._general:
                seen = self._siblings.setdefault(prefix, set())
                seen.add(word)
                if len(seen) <= _SIBLINGS:
                    continue
                self._general.add(prefix)  # from now on (and in later crawls) its words count as one
                del self._siblings[prefix]
            segments[i] = "{word}"
        pattern = "/".join(segments) + mark + query
        self._last = (url, pattern)
        return pattern

    @staticmethod
    def _key(request: Request) -> str | None:
        info = request.meta.get(_META)
        return info[3] if info and len(info) > 3 else None

    def _stats(self, key: str) -> PatternValue:
        stats = self.patterns.get(key)
        if stats is None:
            stats = self.patterns[key] = PatternValue()
        return stats

    # -- deciding ------------------------------------------------------------------------------ #
    def boost(self, url: str) -> int:
        """Priority to add to a request of ``url``: 20 when its pattern's pages hold items, 10 when
        they lead to pages that do, 0 otherwise (or not known yet)."""
        return self._boost(self.patterns.get(self.pattern(url))) if self.prioritize else 0

    @staticmethod
    def _boost(stats: PatternValue | None) -> int:
        if stats is None or stats.pages < 3:
            return 0
        if stats.items / stats.pages >= 0.2:
            return 20
        if stats.settled >= 3 and stats.productive / stats.settled >= 0.2:
            return 10
        return 0

    def skip(self, request: Request, parent: Request | None = None) -> bool:
        """Whether to leave ``request`` out: its pattern is barren (and it is not a probe). ``parent``:
        the request whose page led to it, when known (a probe's links to its own pattern are no probes:
        probing would spread through a tag cloud)."""
        if not self.prune or self.found < 1 or request.dont_filter or request.callback == "_parse_sitemap":
            return False
        if request.meta.get("depth", 1) == 0 or request.meta.get(_PROBE):
            return False  # a start request, or a probe let through when it was queued
        key = self._key(request) or self.pattern(request.url)
        stats = self.patterns.get(key)
        if stats is None or not stats.barren(self.min_pages):
            return False
        if request.url in self._skipped_urls:
            return True  # skipped already (linked again from another page): no new chance to be a probe
        probing = parent is not None and parent.meta.get(_PROBE) and self._key(parent) == key
        if not probing:
            self._probes[key] = self._probes.get(key, 0) + 1
            if self._probes[key] % self.probe_every == 0:
                request.meta[_PROBE] = True  # fetched in case the pattern's pages changed
                return False
        if len(self._skipped_urls) < 100_000:
            self._skipped_urls.add(request.url)
            stats.skipped += 1
            self.skipped += 1
        return True

    def rewrite(self, url: str) -> str:
        """``url`` without the query parameters found to change nothing on its pattern's pages."""
        if not self._dropped or "?" not in url:
            return url
        drop = self._dropped.get(url_template(url, include_query=False))
        if not drop:
            return url
        parts = urlsplit(url)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        kept = [(k, v) for k, v in pairs if k not in drop]
        if len(kept) == len(pairs):
            return url
        return urlunsplit(parts._replace(query=urlencode(kept)))

    def duplicate(self, url: str) -> bool:
        """Whether ``url``'s page was fetched already under another address: with a query parameter
        found to change nothing on its pattern's pages."""
        drop = self._dropped.get(url_template(url, include_query=False)) if self._dropped else None
        if not drop:
            return False
        parts = urlsplit(url)
        key = ((parts.hostname or "").lower(), parts.path, frozenset(parse_qsl(parts.query, keep_blank_values=True)))
        group = self._groups.get(key)
        return group is not None and any(name in drop for name, _ in group)

    # -- learning ------------------------------------------------------------------------------ #
    def enqueued(self, request: Request, parent: Request | None) -> None:
        """``request`` is about to be queued (``parent``: the request whose page led to it)."""
        key = self.pattern(request.url)
        self._stats(key).queued += 1
        if self.parameters and "?" in request.url:
            self._queried.add(url_template(request.url, include_query=False))
        own = next(self._ids)
        chain: list[int] = []
        info = parent.meta.get(_META) if parent is not None else None
        if info:
            chain = [info[0], *info[1]][: self.depth]
        waiting = []
        for ancestor in chain:
            entry = self._open.get(ancestor)
            if entry is not None and entry[0] != key:  # a page of the same pattern is a question of its own
                entry[1] += 1
                waiting.append(ancestor)
        self._open[own] = [key, 1, False, False]
        request.meta[_META] = [own, chain, waiting, key]

    def discard(self, request: Request) -> None:
        """``request`` was not queued after all (a duplicate): forget :meth:`enqueued`."""
        key = self._key(request)
        info = request.meta.pop(_META, None)
        stats = self.patterns.get(key or self.pattern(request.url))
        if stats is not None and stats.queued > 0:
            stats.queued -= 1
        if info:
            self._open.pop(info[0], None)
            for ancestor in info[2]:
                self._close(ancestor)

    def page(self, request: Request, response: Any, items: int) -> None:
        """``request``'s page was fetched and its callback yielded ``items`` (kept) items."""
        stats = self._stats(self._key(request) or self.pattern(request.url))
        if request.url in self._skipped_urls:  # skipped once, fetched later (a probe)
            self._skipped_urls.discard(request.url)
            stats.skipped = max(0.0, stats.skipped - 1)
            self.skipped -= 1
        stats.pages += 1
        stats.items += items
        stats.updated = time.time()
        self.found += items
        info = request.meta.get(_META)
        if info:
            own = self._open.get(info[0])
            if own is not None:
                own[3] = True
            if items:
                for page_id in (info[0], *info[1]):
                    entry = self._open.get(page_id)
                    if entry is not None:
                        entry[2] = True
        if stats.pages > _CAP:
            stats.halve()
        if self.parameters and self._queried and response is not None:
            self._learn(request.url, response)

    def done(self, request: Request) -> None:
        """``request`` is finished (its page processed, or given up on): once, whatever the attempts."""
        stats = self.patterns.get(self._key(request) or self.pattern(request.url))
        if stats is not None and stats.queued > 0:
            stats.queued -= 1
        info = request.meta.get(_META)
        if info:
            self._close(info[0])
            for ancestor in info[2]:
                self._close(ancestor)

    def _close(self, page_id: int) -> None:
        entry = self._open.get(page_id)
        if entry is None:
            return
        entry[1] -= 1
        if entry[1] > 0:
            return
        del self._open[page_id]
        if not entry[3]:
            return  # never fetched (an error, a refusal): says nothing about the pattern
        stats = self._stats(entry[0])
        stats.settled += 1
        if entry[2]:
            stats.productive += 1

    def _learn(self, url: str, response: Any) -> None:
        """Compare the page with pages that differ from it in one query parameter."""
        if not getattr(response, "is_html", False) or response.status != 200:
            return
        pattern = url_template(url, include_query=False)
        if pattern not in self._queried:
            return  # no URL of the pattern has a query string: nothing to compare
        parts = urlsplit(url)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        names = [k for k, _ in pairs]
        if len(pairs) > 8 or len(set(names)) != len(names):
            return  # many or repeated parameters: not worth comparing
        try:
            if needs_javascript(response):
                return  # an app shell: its HTML says nothing of what the URL shows
            root = response.selector.root
            fingerprint = hash((_TEXT(root), frozenset(str(h).strip() for h in _HREFS(root))))
        except Exception:
            return
        base = ((parts.hostname or "").lower(), parts.path)
        params = dict(pairs)
        self._pair(pattern, (*base, frozenset(params.items())), (None, None), fingerprint)  # as "without one more"
        for name, value in params.items():
            rest = frozenset((k, v) for k, v in params.items() if k != name)
            self._pair(pattern, (*base, rest), (name, value), fingerprint)

    def _pair(self, pattern: str, key: tuple[Any, ...], slot: tuple[str | None, str | None], fingerprint: int) -> None:
        group = self._groups.get(key)
        if group is None:
            group = self._groups[key] = {}
            if len(self._groups) > _MAX_GROUPS:
                self._groups.popitem(last=False)
        if slot in group:
            return
        for (name, _), other in group.items():
            if slot[0] is not None and name is not None and name != slot[0]:
                continue  # two different parameters: not a comparison of one
            param = slot[0] if slot[0] is not None else name
            if param is None:
                continue
            counts = self.evidence.setdefault(pattern, {}).setdefault(param, [0, 0])
            counts[0 if other == fingerprint else 1] += 1
            if counts[1] == 0 and counts[0] >= 2:
                self._dropped.setdefault(pattern, set()).add(param)
            else:
                self._dropped.get(pattern, set()).discard(param)
        group[slot] = fingerprint

    # -- reporting ----------------------------------------------------------------------------- #
    def forecast(self) -> dict[str, float]:
        """What the queue should still give: requests queued, and items expected from them (at each
        pattern's rate so far; the pages they lead to are not counted)."""
        queued = expected = 0.0
        for stats in self.patterns.values():
            queued += stats.queued
            if stats.rate is not None:
                expected += stats.queued * stats.rate
        return {"queued": queued, "items": round(expected, 1)}

    def describe(self, limit: int = 12) -> str:
        """The patterns seen most: what their pages gave, and what the optimizer does with them."""
        rows = sorted(self.patterns.items(), key=lambda kv: -(kv[1].pages + kv[1].skipped))
        lines = []
        for key, stats in rows[:limit]:
            if not stats.pages and not stats.skipped:
                continue
            facts = [f"{stats.pages:g} page(s)"]
            if stats.items:
                facts.append(f"{stats.items:g} item(s)")
            if stats.barren(self.min_pages):
                facts.append(f"barren (none of {stats.settled:g} settled pages led to an item)")
            elif stats.settled and stats.productive:
                facts.append(f"{stats.productive:g} of {stats.settled:g} settled page(s) gave or led to items")
            if stats.skipped:
                facts.append(f"{stats.skipped:g} skipped")
            boost = self._boost(stats) if self.prioritize else 0
            if boost:
                facts.append(f"priority +{boost}")
            lines.append(f"{key}: " + ", ".join(facts))
        if len(rows) > limit:
            lines.append(f"... {len(rows) - limit} more pattern(s)")
        for pattern, params in sorted(self._dropped.items()):
            for param in sorted(params):
                same = self.evidence[pattern][param][0]
                lines.append(f"parameter dropped: {param} on {pattern} ({same} pair(s) of pages alike, none different)")
        return "\n".join(lines) or "nothing learned yet"

    # -- keeping ------------------------------------------------------------------------------- #
    def to_dict(self) -> dict[str, Any]:
        patterns = {k: {n: v for n, v in asdict(s).items() if n != "queued"} for k, s in sorted(self.patterns.items())}
        return {"version": 1, "patterns": patterns, "parameters": self.evidence, "words": sorted(self._general)}

    def load(self, path: str | os.PathLike[str]) -> None:
        """Add what a :meth:`save` file says (a missing or unreadable file is an error)."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigurationError(f"cannot read the optimizer's file {path}: {exc}", key="optimize") from exc
        for key, row in (data.get("patterns") or {}).items():
            known = {k: float(v) for k, v in row.items() if k in PatternValue.__dataclass_fields__ and k != "queued"}
            self.patterns[key] = PatternValue(**known)
        self.found = sum(stats.items for stats in self.patterns.values())
        self._general.update(str(prefix) for prefix in data.get("words") or ())
        for pattern, params in (data.get("parameters") or {}).items():
            for param, counts in params.items():
                same, differ = int(counts[0]), int(counts[1])
                self.evidence.setdefault(pattern, {})[param] = [same, differ]
                if differ == 0 and same >= 2:
                    self._dropped.setdefault(pattern, set()).add(param)

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
        return f"CrawlOptimizer({len(self.patterns)} pattern(s), path={str(self.path) if self.path else None!r})"
