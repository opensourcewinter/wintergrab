"""Sitemaps: the fastest way to discover every URL a site wants you to find.

Handles XML sitemaps and sitemap indexes (gzipped or not), plain-text
sitemaps, RSS/Atom feeds and ``Sitemap:`` lines in robots.txt.
"""

from __future__ import annotations

import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin

from lxml import etree

_MAX_UNCOMPRESSED = 200 * 1024 * 1024  # refuse gzip bombs


@dataclass
class SitemapEntry:
    """One ``<url>`` (``kind="url"``) or nested ``<sitemap>`` (``kind="sitemap"``)."""

    loc: str
    kind: str = "url"
    lastmod: str | None = None
    changefreq: str | None = None
    priority: float | None = None

    @property
    def lastmod_datetime(self) -> datetime | None:
        """``lastmod`` parsed as an aware datetime (``None`` if absent/invalid)."""
        return parse_lastmod(self.lastmod)


def parse_lastmod(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def decompress(body: bytes) -> bytes:
    """Gunzip ``body`` if it is gzip data (sitemaps are often served as .xml.gz)."""
    if body[:2] != b"\x1f\x8b":
        return body
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    data = decoder.decompress(body, _MAX_UNCOMPRESSED)
    if decoder.unconsumed_tail:
        raise ValueError("sitemap is too large once decompressed")
    return data


def _local(tag: object) -> str:
    return etree.QName(tag).localname.lower() if isinstance(tag, str) else ""


def _child_text(el: etree._Element, name: str) -> str | None:
    for child in el:
        if _local(child.tag) == name:
            text = (child.text or "").strip()
            return text or None
    return None


def parse_sitemap(body: bytes, base_url: str | None = None) -> tuple[str, list[SitemapEntry]]:
    """Parse a sitemap document.

    Returns ``(kind, entries)`` where ``kind`` is ``"urlset"``,
    ``"sitemapindex"``, ``"feed"`` or ``"text"``.
    """
    data = decompress(body).lstrip()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    if not data.startswith(b"<"):
        lines = data.decode("utf-8", errors="replace").splitlines()
        urls = [line.strip() for line in lines if line.strip().startswith(("http://", "https://"))]
        return "text", [SitemapEntry(u) for u in urls]
    parser = etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False, no_network=True)
    root = etree.fromstring(data, parser=parser)
    if root is None:
        return "urlset", []
    kind = _local(root.tag)
    entries: list[SitemapEntry] = []
    join = (lambda u: urljoin(base_url, u)) if base_url else (lambda u: u)
    if kind in ("urlset", "sitemapindex"):
        wanted = "url" if kind == "urlset" else "sitemap"
        for el in root:
            if _local(el.tag) != wanted:
                continue
            loc = _child_text(el, "loc")
            if not loc:
                continue
            priority = _child_text(el, "priority")
            try:
                prio = float(priority) if priority else None
            except ValueError:
                prio = None
            entries.append(
                SitemapEntry(
                    loc=join(loc),
                    kind="url" if kind == "urlset" else "sitemap",
                    lastmod=_child_text(el, "lastmod"),
                    changefreq=_child_text(el, "changefreq"),
                    priority=prio,
                )
            )
        return kind, entries
    if kind in ("rss", "feed", "rdf"):
        for el in root.iter():
            name = _local(el.tag)
            if name in ("item", "entry"):
                link = _child_text(el, "link")
                if not link:
                    for child in el:
                        if _local(child.tag) == "link" and child.get("href"):
                            link = child.get("href")
                            break
                if link:
                    date = _child_text(el, "updated") or _child_text(el, "pubdate") or _child_text(el, "date")
                    entries.append(SitemapEntry(join(link.strip()), lastmod=date))
        return "feed", entries
    return kind or "urlset", entries


def robots_sitemaps(robots_txt: str, base_url: str | None = None) -> list[str]:
    """URLs listed on ``Sitemap:`` lines of a robots.txt file."""
    out: list[str] = []
    for line in robots_txt.splitlines():
        name, _, value = line.partition(":")
        if name.strip().lower() == "sitemap" and value.strip():
            url = value.strip()
            out.append(urljoin(base_url, url) if base_url else url)
    return list(dict.fromkeys(out))


def sitemap(
    url: str,
    *,
    follow: bool = True,
    max_sitemaps: int = 200,
    since: str | datetime | None = None,
    **fetch_options: object,
) -> list[SitemapEntry]:
    """Every page URL listed in a sitemap (following sitemap indexes).

    ``url`` may be a sitemap, a sitemap index, a feed, or a ``robots.txt``.
    ``since`` keeps only entries modified at or after that date.
    """
    from .fetchers import Fetcher

    cutoff = parse_lastmod(since) if isinstance(since, str) else since
    if cutoff is not None and cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    queue = [url]
    seen: set[str] = set()
    pages: list[SitemapEntry] = []
    with Fetcher(**fetch_options) as fetcher:  # type: ignore[arg-type]
        while queue and len(seen) < max_sitemaps:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            response = fetcher.get(current)
            if not response.ok:
                continue
            if current.rstrip("/").endswith("robots.txt"):
                queue.extend(robots_sitemaps(response.text, current))
                continue
            _, entries = parse_sitemap(response.body, response.url)
            for entry in entries:
                if entry.kind == "sitemap":
                    if follow:
                        queue.append(entry.loc)
                elif cutoff is None or (entry.lastmod_datetime is not None and entry.lastmod_datetime >= cutoff):
                    pages.append(entry)
    return pages


def filter_entries(entries: Iterable[SitemapEntry], since: datetime | None) -> list[SitemapEntry]:
    if since is None:
        return list(entries)
    return [e for e in entries if e.lastmod_datetime is not None and e.lastmod_datetime >= since]


__all__ = [
    "SitemapEntry",
    "decompress",
    "filter_entries",
    "parse_lastmod",
    "parse_sitemap",
    "robots_sitemaps",
    "sitemap",
]
