"""Watching a URL or a dataset for changes: a sitemap, a feed, a page, or records (a project job's ``watch:``).

::

    >>> first = check("https://shop.example/sitemap.xml")
    >>> first.summary
    '142 URLs (first look)'
    >>> later = check("https://shop.example/sitemap.xml", previous=first.state)
    >>> later.changed, later.summary
    (True, '3 new URLs, 1 gone')

What is compared:

* a sitemap: its URLs and their ``lastmod`` (a sitemap index: its sitemaps and theirs);
* an RSS or Atom feed: its items' links;
* anything else: the page's visible text (scripts and styles aside), or a JSON document's data;
* a dataset (``data/products.jsonl``, a ``.csv``, ``.sqlite`` or ``.parquet`` file,
  ``postgresql://.../db?table=NAME``... anything :func:`~wintergrab.data.io.read_records` reads): its records,
  by their contents, whatever their order. A record that changed is one gone and one new.

Each check of a URL is one request, conditional when the last answer had an ``ETag`` or ``Last-Modified``
(a ``304 Not Modified`` means no change), and allowed by the site's robots.txt (unless told not
to look). A dataset is read whole, unless it is a file not written since the last check. A check that
fails (network, HTTP 4xx/5xx, robots.txt, a dataset that cannot be read) changes nothing: it says why.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from .errors import FetchError, WintergrabError, describe
from .redact import redact_query

__all__ = ["WatchCheck", "check"]

log = logging.getLogger("wintergrab.watch")

_KEPT_KEYS = 20_000  # entries remembered to say which are new or gone (more: counted only)


@dataclass
class WatchCheck:
    """What a check found.

    Attributes:
        url: What was checked.
        changed: It changed since the previous check.
        first: There was no previous check to compare with.
        kind: ``"sitemap"``, ``"feed"``, ``"page"`` or ``"data"``.
        summary: What changed, for people: ``"3 new URLs, 1 gone"``, ``"not modified (304)"``...
        status: The HTTP status (``None`` when no answer came).
        error: Why the check failed, if it did (then ``changed`` is false and ``state`` is the previous one).
        state: What to keep for the next check.
    """

    url: str
    changed: bool = False
    first: bool = False
    kind: str = "page"
    summary: str = ""
    status: int | None = None
    error: str | None = None
    state: dict[str, Any] = field(default_factory=dict)


def _digest(parts: list[str]) -> str:
    h = hashlib.sha1()
    for part in parts:
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\0")
    return h.hexdigest()


def _key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def _robots_allow(url: str, user_agent: str, timeout: float) -> bool:
    from . import get

    parts = urlsplit(url)
    robots = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        answer = get(robots, retries=1, timeout=timeout)
    except FetchError as exc:
        log.warning("could not fetch %s (%s); treating the site as allowed", robots, describe(exc))
        return True
    if answer.status != 200:
        if answer.status >= 500:
            log.warning("%s returned %s; treating the site as allowed", robots, answer.status)
        return True
    parser = RobotFileParser(robots)
    parser.parse(answer.text.splitlines())
    return parser.can_fetch(user_agent, url)


def _entries(body: bytes, url: str) -> tuple[str, list[str], list[str]] | None:
    """A sitemap's or a feed's entries: ``(kind, keys, parts to fingerprint)``, or ``None``."""
    from .sitemaps import parse_sitemap

    try:
        kind, entries = parse_sitemap(body, url)
    except Exception:
        return None
    if kind not in ("urlset", "sitemapindex", "feed") or (not entries and kind == "feed"):
        return None
    keys = [entry.loc for entry in entries]
    parts = sorted(f"{entry.loc}\t{entry.lastmod or ''}" for entry in entries)
    return ("feed" if kind == "feed" else "sitemap"), keys, parts


def _text_of(body: bytes, content_type: str) -> tuple[str, str]:
    """``(kind, what to compare)`` for a page or a document."""
    if "json" in content_type:
        try:
            return "data", json.dumps(json.loads(body), sort_keys=True, ensure_ascii=False)
        except ValueError:
            pass
    try:
        from lxml import html as lxml_html

        root = lxml_html.fromstring(body)
        for element in root.xpath("//script | //style | //noscript | //template"):
            element.drop_tree()
        return "page", " ".join(root.text_content().split())
    except Exception:
        return "page", body.decode("utf-8", "replace")


def check(
    url: str,
    previous: dict[str, Any] | None = None,
    *,
    obey_robots: bool = True,
    user_agent: str = "*",
    timeout: float = 30.0,
) -> WatchCheck:
    """Check ``url`` against ``previous`` (the last check's :attr:`WatchCheck.state`; see the module docs).
    Anything but an http(s) URL is a dataset."""
    from . import get

    previous = previous or {}
    if not url.startswith(("http://", "https://")):
        return _check_dataset(url, previous)
    result = WatchCheck(url=url, first=not previous.get("fingerprint"), kind=previous.get("kind", "page"))
    result.state = dict(previous)
    if obey_robots and not _robots_allow(url, user_agent, timeout):
        result.error = "robots.txt does not allow it"
        return result
    headers = {}
    if previous.get("etag"):
        headers["If-None-Match"] = previous["etag"]
    if previous.get("last_modified"):
        headers["If-Modified-Since"] = previous["last_modified"]
    try:
        answer = get(url, retries=1, timeout=timeout, headers=headers)
    except FetchError as exc:
        result.error = describe(exc)
        return result
    result.status = answer.status
    checked = time.time()
    if answer.status == 304:
        if not previous.get("fingerprint"):
            result.error = "304 Not Modified, with nothing to compare"
            return result
        result.summary = "not modified (304)"
        result.state["checked"] = checked
        return result
    if answer.status >= 400:
        result.error = f"HTTP {answer.status}"
        return result

    entries = _entries(answer.body, answer.url)
    keys: list[str] = []
    if entries is not None:
        result.kind, keys, parts = entries
        fingerprint = _digest(parts)
    else:
        result.kind, text = _text_of(answer.body, answer.headers.get("content-type", ""))
        fingerprint = _digest([text])
    state: dict[str, Any] = {
        "kind": result.kind,
        "fingerprint": fingerprint,
        "checked": checked,
        "etag": answer.headers.get("etag"),
        "last_modified": answer.headers.get("last-modified"),
        "count": len(keys),
    }
    hashed = [_key(k) for k in keys]
    if len(hashed) <= _KEPT_KEYS:
        state["keys"] = hashed
    result.state = state
    noun = "item" if result.kind == "feed" else "URL"
    if result.first:
        result.summary = (
            f"{len(keys):,} {noun}{'s' if len(keys) != 1 else ''} (first look)"
            if entries is not None
            else f"a {result.kind} (first look)"
        )
        return result
    result.changed = fingerprint != previous.get("fingerprint")
    if not result.changed:
        result.summary = "no change"
    elif entries is not None and "keys" in previous and "keys" in state:
        before, now = set(previous["keys"]), set(state["keys"])
        new, gone = len(now - before), len(before - now)
        facts = [f"{new:,} new {noun}{'s' if new != 1 else ''}"] if new else []
        facts += [f"{gone:,} gone"] if gone else []
        result.summary = ", ".join(facts) or f"{noun}s updated (lastmod)"
    elif entries is not None:
        result.summary = f"{previous.get('count', 0):,} -> {len(keys):,} {noun}s"
    else:
        result.summary = f"the {result.kind} changed"
    return result


def _check_dataset(target: str, previous: dict[str, Any]) -> WatchCheck:
    """A dataset's check: its records compared by their contents (see the module docs)."""
    from .data.io import read_records

    result = WatchCheck(url=redact_query(target), first=not previous.get("fingerprint"), kind="data")
    result.state = dict(previous)
    stamp = None
    if "://" not in target:  # a file: not read again when it was not written since
        try:
            info = Path(target).stat()
        except OSError as exc:
            result.error = f"cannot read {target}: {exc.strerror or exc}"
            return result
        stamp = [info.st_mtime_ns, info.st_size]
        if previous.get("fingerprint") and previous.get("stamp") == stamp:
            result.summary = "no change (not written since)"
            result.state["checked"] = time.time()
            return result
    try:
        keys = [_key(json.dumps(r, sort_keys=True, ensure_ascii=False, default=str)) for r in read_records(target)]
    except (WintergrabError, OSError) as exc:
        result.error = redact_query(describe(exc))
        return result
    state: dict[str, Any] = {
        "kind": "data",
        "fingerprint": _digest(sorted(keys)),
        "checked": time.time(),
        "count": len(keys),
        "stamp": stamp,
    }
    if len(keys) <= _KEPT_KEYS:
        state["keys"] = sorted(set(keys))
    result.state = state
    if result.first:
        result.summary = f"{len(keys):,} record{'s' if len(keys) != 1 else ''} (first look)"
        return result
    result.changed = state["fingerprint"] != previous.get("fingerprint")
    if not result.changed:
        result.summary = "no change"
    elif "keys" in previous and "keys" in state:
        before, now = set(previous["keys"]), set(state["keys"])
        new, gone = len(now - before), len(before - now)
        facts = [f"{new:,} new record{'s' if new != 1 else ''}"] if new else []
        facts += [f"{gone:,} gone"] if gone else []
        result.summary = ", ".join(facts) or "records repeated or no longer repeated"
    else:
        result.summary = f"{previous.get('count', 0):,} -> {len(keys):,} records"
    return result
