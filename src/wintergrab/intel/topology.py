"""How a site is organized: its sections as a tree, its navigation, and its odd pages.

::

    builder = TopologyBuilder()
    builder.add_urls(sitemap_urls)          # what the site lists
    for response in pages:
        builder.observe(response)           # what was visited (and the links found there)
    print(builder.topology().render())

::

    shop.example  (1,540 URLs, 30 visited)
    ├── Products  /products  (1,204, product)
    │   ├── Phones  /products/phones  (312, product)
    │   └── Laptops  /products/laptops  (208, product)
    ├── Blog  /blog  (290, article)
    └── About  /about  (1, company)

The tree is made of the paths of every URL known: listed in a sitemap,
visited, or linked from a visited page. Numbers and hex ids in paths are
generalized (``/p/{id}``), and sections take the names the site gives them
in its navigation menus and breadcrumbs. Page types come from the pages
when they were classified, from the URLs otherwise. A
:class:`~wintergrab.intel.profile.SiteProfiler` keeps one
(``profile.topology``), so spiders with ``profile = True`` and
``wintergrab inspect`` have it.

The topology also has the site's main navigation (the menu entries found on
most pages, with their submenus), its feeds and HTML sitemaps, the sections
with paginated listings, and its odd pages: dead ends (pages linking nowhere
on the site), duplicate routes (the same text at several URLs, or pages
whose canonical link names another URL) and, for a crawl that ran to the
end, orphans (listed in a sitemap, linked from none of the pages visited).
"""

from __future__ import annotations

import hashlib
import math
import re
import statistics
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit

from lxml import etree

from ..extraction.page import PageContext, schema_types
from ..fetchers.resources import registrable_domain
from ..parser.text import text_content
from .classify import classify_url

__all__ = ["Topology", "TopologyBuilder", "TopologyNode"]

# Links in menus (nav a[href], header a[href], [role=navigation] a[href]) and in breadcrumbs
_NAV_LINKS = etree.XPath(".//a[@href][ancestor::nav or ancestor::header or ancestor::*[@role='navigation']]")
_CRUMB_LINKS = etree.XPath(
    ".//a[@href][ancestor::*[contains(@class, 'breadcrumb') or contains(@id, 'breadcrumb')"
    " or contains(@aria-label, 'readcrumb')]]"
)
# Pages about one thing (as their content says): their "next" links ("Next post") are not pagination
_SINGLE_ITEM = frozenset({"product", "article", "news", "job", "event", "profile", "login", "contact", "error"})
# Path segments that are ids rather than names: /p/123, /order/9f8e7d6c...
_ID = re.compile(r"\d+|[0-9a-f]{8,}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
_PAGE_SUFFIX = re.compile(r"/(?:page|p)/\{id\}$")
_GENERIC_LABELS = frozenset(
    {"home", "menu", "more", "read more", "view all", "see all", "shop now", "learn more", "next", "previous", "back"}
)
_MAX_LABEL = 40
_MAX_CHILDREN = 50  # sections kept under each section (the biggest); the rest are only counted
_MIN_TEXT = 200  # pages with less text (app shells, redirects) are not compared for duplicate content
_LISTED, _LINKED, _VISITED = 1, 2, 4


@dataclass
class TopologyNode:
    """A section of the site: a URL path and the URLs under it.

    Attributes:
        path: The path, with ids generalized (``/p/{id}``).
        label: The name the site gives the section (its menus, its breadcrumbs), or one made from the path.
        urls: URLs known at or under the path.
        page_type: Their most common page type, when at least a quarter of them have it.
        types: How many of them have each page type (the five most common).
        children: The sections under it, biggest first (50 at most).
        other_sections, other_urls: The sections left out of ``children`` (the smallest), and
            their URLs.
    """

    path: str
    label: str
    urls: int
    page_type: str | None = None
    types: dict[str, int] = field(default_factory=dict)
    children: list[TopologyNode] = field(default_factory=list)
    other_sections: int = 0
    other_urls: int = 0

    def walk(self) -> Iterator[TopologyNode]:
        """This node and every node under it, depth first."""
        yield self
        for child in self.children:
            yield from child.walk()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Topology:
    """A site's organization (see the module docs).

    Attributes:
        site: The main host (``www.`` left out).
        root: Its tree of sections.
        hosts: Trees for the site's other hosts (``blog.shop.example``), biggest first.
        navigation: The main menu: ``{"label", "url", "children"}`` entries, in page order.
        feeds: RSS/Atom feeds the pages point to.
        html_sitemaps: Pages that list links to much of the site.
        paginated: Sections with a "next page" link, and how many of their pages had one.
        dead_ends: Pages that link nowhere on the site (the first 100).
        duplicates: Routes to the same page: ``{"reason": "same text" | "canonical link", "urls",
            "canonical"}`` (the first 100 groups).
        orphans: Pages listed in a sitemap that no visited page links to (the first 100), when
            the crawl visited the whole site; ``None`` otherwise.
        counts: ``urls``, ``listed``, ``visited``, ``linked``, ``dead_ends``, ``duplicates`` and
            ``orphans`` in full, and ``truncated`` when more URLs were seen than remembered.
    """

    site: str
    root: TopologyNode
    hosts: list[TopologyNode] = field(default_factory=list)
    navigation: list[dict[str, Any]] = field(default_factory=list)
    feeds: list[str] = field(default_factory=list)
    html_sitemaps: list[str] = field(default_factory=list)
    paginated: dict[str, int] = field(default_factory=dict)
    dead_ends: list[str] = field(default_factory=list)
    duplicates: list[dict[str, Any]] = field(default_factory=list)
    orphans: list[str] | None = None
    counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def find(self, text: str) -> list[TopologyNode]:
        """Sections whose name or path mentions ``text`` (any case), biggest first."""
        needle = text.lower().strip()
        found = [
            node
            for tree in (self.root, *self.hosts)
            for node in tree.walk()
            if node is not tree and (needle in node.label.lower() or needle in node.path.lower().rsplit("/", 1)[-1])
        ]
        return sorted(found, key=lambda n: (-n.urls, n.path))

    def render(self, depth: int = 3, width: int = 8) -> str:
        """The tree as text, ``depth`` levels deep, ``width`` sections per level."""
        visited = self.counts.get("visited", 0)
        lines = [f"{self.site or 'site'}  ({self.root.urls:,} URLs, {visited:,} visited)"]
        _render(self.root, "", 1, depth, width, lines)
        for host in self.hosts[:width]:
            lines.append(f"{host.label}  ({host.urls:,} URLs)")
            _render(host, "", 1, depth, width, lines)
        return "\n".join(lines)

    def summary(self, limit: int = 8) -> list[str]:
        """The navigation and the odd pages, one line each (the tree aside)."""
        lines = []
        if self.navigation:
            parts = []
            for entry in self.navigation[:limit]:
                kids = [child["label"] for child in entry["children"]]
                more = ", ..." if len(kids) > 5 else ""
                parts.append(entry["label"] + (f" ({', '.join(kids[:5])}{more})" if kids else ""))
            lines.append("navigation: " + ", ".join(parts) + (", ..." if len(self.navigation) > limit else ""))
        if self.feeds:
            lines.append("feeds: " + ", ".join(self.feeds[:limit]))
        if self.html_sitemaps:
            lines.append("HTML sitemaps: " + ", ".join(self.html_sitemaps[:limit]))
        if self.paginated:
            lines.append(
                "paginated listings: " + ", ".join(f"{p} ({n})" for p, n in list(self.paginated.items())[:limit])
            )
        if self.counts.get("dead_ends"):
            shown = ", ".join(self.dead_ends[:3]) + (", ..." if self.counts["dead_ends"] > 3 else "")
            lines.append(f"dead ends: {_pages(self.counts['dead_ends'])} linking nowhere on the site ({shown})")
        if self.duplicates:
            first = self.duplicates[0]
            lines.append(
                f"duplicate routes: {self.counts.get('duplicates', len(self.duplicates)):,} group(s), such as "
                f"{' = '.join(first['urls'][:3])} ({first['reason']})"
            )
        if self.orphans is not None:
            total = self.counts.get("orphans", len(self.orphans))
            example = f" ({', '.join(self.orphans[:3])}{', ...' if total > 3 else ''})" if total else ""
            lines.append(f"orphans: {_pages(total)} listed in sitemaps and linked from no page visited{example}")
        return lines


def _render(node: TopologyNode, prefix: str, level: int, depth: int, width: int, lines: list[str]) -> None:
    shown, rest = node.children[:width], node.children[width:]
    more = len(rest) + node.other_sections
    for i, child in enumerate(shown):
        last = i == len(shown) - 1 and not more
        facts = f"{child.urls:,}" + (f", {child.page_type}" if child.page_type else "")
        lines.append(f"{prefix}{'└── ' if last else '├── '}{child.label}  {child.path}  ({facts})")
        if level < depth:
            _render(child, prefix + ("    " if last else "│   "), level + 1, depth, width, lines)
    if more:
        urls = sum(c.urls for c in rest) + node.other_urls
        lines.append(f"{prefix}└── ... {more:,} more section(s) ({urls:,} URLs)")


def _pages(n: int) -> str:
    return f"{n:,} page" if n == 1 else f"{n:,} pages"


def _split(url: str) -> tuple[str, str, str] | None:
    """(host without ``www.``, path without a trailing slash, query) of an http(s) URL."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    return host.removeprefix("www."), parts.path.rstrip("/") or "/", parts.query


def _key(parts: tuple[str, str, str]) -> str:
    host, path, query = parts
    return f"{host}{path}?{query}" if query else host + path


def _general(path: str) -> str:
    """``/p/123/reviews`` -> ``/p/{id}/reviews``."""
    return "/".join("{id}" if _ID.fullmatch(segment) else segment for segment in path.split("/"))


def _prefixes(path: str) -> list[str]:
    """``/a/b/c`` -> ``["/", "/a", "/a/b", "/a/b/c"]``."""
    parts = [p for p in path.split("/") if p]
    return ["/"] + ["/" + "/".join(parts[: i + 1]) for i in range(len(parts))]


def _pretty(segment: str) -> str:
    """A label made from a path segment: ``summer-sale.html`` -> ``Summer sale``."""
    text = re.sub(r"\.(?:html?|php|aspx?|jsp)$", "", segment, flags=re.I)
    text = re.sub(r"[-_+]+", " ", text).strip()
    return (text[:1].upper() + text[1:]) if text else segment


def _clean_label(text: str | None) -> str:
    label = " ".join((text or "").split())
    if len(label) > _MAX_LABEL or label.lower() in _GENERIC_LABELS or not any(c.isalpha() for c in label):
        return ""
    return label


def _item_label(item: Any) -> str:
    """The name of a menu item (an ``<li>``): its own text, or its first child's that is not a submenu."""
    if (item.text or "").strip():
        return _clean_label(item.text)
    for child in item:
        if isinstance(child.tag, str) and child.tag not in ("ul", "ol"):
            label = _clean_label(text_content(child))
            if label:
                return label
    return ""


class _Paths:
    """URL counts, page types and names per path (of one host), with each path's sub-paths."""

    def __init__(self, counts: dict[str, int], types: dict[str, Counter[str]], labels: dict[str, Counter[str]]) -> None:
        self.counts = counts
        self.types = types
        self.labels = labels
        self.kids: dict[str, list[str]] = {}
        for path in counts:
            if path != "/":
                self.kids.setdefault(path.rsplit("/", 1)[0] or "/", []).append(path)
        self.leading: set[str] = set()  # paths with a named path under them
        for path in labels:
            while path != "/":
                path = path.rsplit("/", 1)[0] or "/"
                if path in self.leading:
                    break
                self.leading.add(path)

    def merged(self, parent: str, members: list[str]) -> _Paths:
        """The paths under ``parent`` listed in ``members``, with everything under them, as one:
        ``parent/{slug}``."""
        base = ("" if parent == "/" else parent) + "/{slug}"
        counts: Counter[str] = Counter()
        types: dict[str, Counter[str]] = {}
        for member in members:
            stack = [member]
            while stack:
                path = stack.pop()
                target = base + path[len(member) :]
                counts[target] += self.counts[path]
                if path in self.types:
                    types.setdefault(target, Counter()).update(self.types[path])
                stack.extend(self.kids.get(path, ()))
        return _Paths(dict(counts), types, {})


class TopologyBuilder:
    """Collects what a site's topology is made of, page by page (see the module docs).

    Args:
        max_urls: Distinct URLs remembered. Past that, new URLs are no longer counted
            (``counts["truncated"]`` says so): about 200 bytes each.
    """

    def __init__(self, *, max_urls: int = 200_000) -> None:
        self.max_urls = max_urls
        # key (host + path + query) -> [first URL seen, page type, sources (_LISTED | _LINKED | _VISITED)]
        self._urls: dict[str, list[Any]] = {}
        self._truncated = False
        self._tree: Counter[tuple[str, str]] = Counter()  # URLs at or under each (host, general path)
        self._types: dict[tuple[str, str], Counter[str]] = {}
        self._labels: dict[tuple[str, str], Counter[str]] = {}
        self._start: set[str] = set()
        self._visited = 0
        self._menu: Counter[tuple[str, str, str]] = Counter()  # (label, url, parent label) -> pages
        self._dead_ends: list[str] = []
        self._dead_end_count = 0
        self._feeds: Counter[str] = Counter()
        self._html_sitemaps: list[str] = []
        self._paginated: Counter[tuple[str, str]] = Counter()
        self._texts: dict[bytes, dict[str, str]] = {}  # text digest -> {key: URL}
        self._canonical: dict[str, dict[str, str]] = {}  # canonical URL -> {key: URL}
        self._parts: dict[str, tuple[str, str, str] | None] = {}  # _split() of recent links (menus repeat)

    # -- URLs --------------------------------------------------------------------------------- #
    def _add(self, url: str, source: int, page_type: str | None = None, *, sure: bool = False) -> str | None:
        """Count a URL once (its key is returned; ``None`` if it is not an http(s) URL). A page type
        read from the page (``sure``) replaces one guessed from the URL."""
        if url in self._parts:
            parts = self._parts[url]
        else:
            if len(self._parts) >= 20_000:
                self._parts.clear()
            parts = self._parts[url] = _split(url)
        if parts is None:
            return None
        host, path, _ = parts
        key = _key(parts)
        page_type = None if page_type == "unknown" else page_type
        seen = self._urls.get(key)
        if seen is not None:
            seen[2] |= source
            if sure and page_type and page_type != seen[1]:
                self._retype(host, path, seen[1], page_type)
                seen[1] = page_type
            return key
        if len(self._urls) >= self.max_urls:
            self._truncated = True
            return key
        if page_type is None:
            guess = classify_url(url).type
            page_type = None if guess == "unknown" else guess
        self._urls[key] = [url.split("#")[0], page_type, source]
        for prefix in _prefixes(_general(path)):
            self._tree[host, prefix] += 1
            if page_type:
                types = self._types.get((host, prefix))
                if types is None:
                    types = self._types[host, prefix] = Counter()
                types[page_type] += 1
        return key

    def _retype(self, host: str, path: str, old: str | None, new: str) -> None:
        for prefix in _prefixes(_general(path)):
            types = self._types.setdefault((host, prefix), Counter())
            if old:
                types[old] -= 1
                if types[old] <= 0:
                    del types[old]
            types[new] += 1

    def _label(self, url: str, text: str | None) -> None:
        label = _clean_label(text)
        parts = _split(url) if label else None
        if parts is not None:
            self._labels.setdefault((parts[0], _general(parts[1])), Counter())[label] += 1

    def add_urls(self, urls: Iterable[str]) -> None:
        """URLs the site lists (sitemaps, feeds): they are counted, and can be orphans."""
        for url in urls:
            self._add(url, _LISTED)

    def start_urls(self, urls: Iterable[str]) -> None:
        """Where the crawl started: those were reached without a link, so they are not orphans."""
        for url in urls:
            parts = _split(str(url))
            if parts is not None:
                self._start.add(_key(parts))

    # -- pages -------------------------------------------------------------------------------- #
    def observe(self, page: Any, *, page_type: str | None = None, links: Iterable[str] | None = None) -> None:
        """A visited HTML page (a :class:`~wintergrab.Response` or a :class:`PageContext`).

        Args:
            page_type: Its type, when it was classified (see :func:`~wintergrab.intel.classify_page`);
                otherwise it is guessed from the URL.
            links: Its links to the site, when already collected.
        """
        if not isinstance(page, PageContext) and not getattr(page, "is_html", True):
            return
        ctx = page if isinstance(page, PageContext) else PageContext(page)
        url = ctx.url or ""
        key = self._add(url, _VISITED, page_type, sure=True)
        if key is None:
            return
        self._visited += 1
        host = urlsplit(url).hostname or ""
        site = registrable_domain(host)
        if links is None:
            links = [link for link in ctx.selector.links() if registrable_domain(urlsplit(link).hostname or "") == site]
        found = 0
        for link in links:
            target = self._add(link, 0)
            if target is not None and target != key:  # a link to the page itself leads nowhere new
                found += 1
                entry = self._urls.get(target)
                if entry is not None:
                    entry[2] |= _LINKED
        if not found:
            self._dead_end_count += 1
            if len(self._dead_ends) < 100:
                self._dead_ends.append(url)
        root = ctx.selector.root
        if root is None:
            return
        self._read_menu(ctx, root, site)
        for element in _CRUMB_LINKS(root):
            self._label(ctx.selector.urljoin(element.get("href") or ""), text_content(element))
        if any("BreadcrumbList" in (script.text or "") for script in root.iter("script")):
            self._read_breadcrumb_list(ctx)
        canonical = None
        for element in root.iter("link"):
            rel = (element.get("rel") or "").lower().split()
            href = (element.get("href") or "").strip()
            if href and canonical is None and "canonical" in rel:
                canonical = ctx.selector.urljoin(href).split("#")[0]
            elif href and "alternate" in rel and any(k in (element.get("type") or "").lower() for k in ("rss", "atom")):
                self._feeds[ctx.selector.urljoin(href)] += 1
        if found >= 30 and len(self._html_sitemaps) < 20:
            title = next((text_content(t) for t in root.iter("title")), "").lower()
            path = urlsplit(url).path.lower()
            if "sitemap" in path or "site-map" in path or "sitemap" in title or "site map" in title:
                self._html_sitemaps.append(url)
        if page_type not in _SINGLE_ITEM and ctx.selector.next_page():
            parts = _split(url)
            if parts is not None:
                self._paginated[parts[0], _PAGE_SUFFIX.sub("", _general(parts[1])) or "/"] += 1
        text = ctx.text
        if len(text) >= _MIN_TEXT and len(self._texts) < self.max_urls:
            digest = hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=12).digest()
            self._texts.setdefault(digest, {})[key] = url
        if canonical is not None:
            parts = _split(canonical)
            if parts is not None and _key(parts) != key and len(self._canonical) < self.max_urls:
                self._canonical.setdefault(canonical, {})[key] = url

    def _read_menu(self, ctx: PageContext, root: Any, site: str) -> None:
        entries: dict[tuple[str, str, str], None] = {}  # in page order
        for element in _NAV_LINKS(root)[:300]:
            href = (element.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            url = ctx.selector.urljoin(href).split("#")[0]
            if registrable_domain(urlsplit(url).hostname or "") != site:
                continue
            label = _clean_label(text_content(element) or element.get("aria-label") or element.get("title"))
            if not label:
                continue
            self._label(url, label)
            items = list(element.iterancestors("li"))
            entries[label, url, _item_label(items[1]) if len(items) > 1 else ""] = None
        self._menu.update(list(entries))

    def _read_breadcrumb_list(self, ctx: PageContext) -> None:
        """Section names from schema.org ``BreadcrumbList`` data."""
        for _, node in ctx.nodes("json-ld"):
            if "BreadcrumbList" not in schema_types(node):
                continue
            for entry in node.get("itemListElement") or []:
                if isinstance(entry, dict):
                    item = entry.get("item")
                    target = (item.get("@id") or item.get("url")) if isinstance(item, dict) else item
                    name = entry.get("name") or (item.get("name") if isinstance(item, dict) else None)
                    if isinstance(target, str) and isinstance(name, str):
                        self._label(ctx.selector.urljoin(target), name)

    # -- the topology --------------------------------------------------------------------------- #
    def _node(self, view: _Paths, path: str, label: str | None = None) -> TopologyNode:
        labels = view.labels.get(path)
        segment = path.rsplit("/", 1)[-1]
        name = label or (labels.most_common(1)[0][0] if labels else _pretty(segment) if segment else "Home")
        named: list[tuple[_Paths, str]] = []
        unnamed: list[str] = []
        for child in view.kids.get(path, ()):
            if child in view.labels or child in view.leading:  # named by the site, or leading to one that is
                named.append((view, child))
            else:
                unnamed.append(child)
        if len(unnamed) > _MAX_CHILDREN:
            # Many unnamed paths side by side are items (/p/blue-shirt, /p/red-hat...), not sections:
            # they become one cluster, /p/{slug}, whose sub-paths add up (/p/{slug}/reviews).
            # A path much bigger than its siblings is a section all the same (/blog among /post-1, /post-2...).
            big = max(10, 5 * statistics.median_low(view.counts[c] for c in unnamed))
            keep = [c for c in unnamed if view.counts[c] >= big]
            items = [c for c in unnamed if view.counts[c] < big]
            cluster = view.merged(path, items)
            named.extend((view, c) for c in keep)
            named.extend((cluster, c) for c in cluster.kids.get(path, ()))
        else:
            named.extend((view, c) for c in unnamed if view.counts[c] >= 2)  # sections, not single pages
        named.sort(key=lambda pair: (-pair[0].counts[pair[1]], pair[1]))
        kids = [self._node(v, child) for v, child in named[:_MAX_CHILDREN]]
        other = named[_MAX_CHILDREN:]
        urls = view.counts.get(path, 0)  # 0 for the root of a site with no URL known yet
        types = view.types.get(path) or Counter()
        top = dict(types.most_common(5))
        main = next(iter(top), None)
        # the section's type when a good share of its URLs have it (a lone /about does not make 500 posts "company")
        page_type = main if main and top[main] * 4 >= urls else None
        return TopologyNode(
            path,
            name,
            urls,
            page_type,
            top,
            kids,
            len(other),
            sum(v.counts[c] for v, c in other),
        )

    def _host_tree(self, host: str, label: str | None = None) -> TopologyNode:
        view = _Paths(
            {path: n for (h, path), n in self._tree.items() if h == host},
            {path: types for (h, path), types in self._types.items() if h == host},
            {path: labels for (h, path), labels in self._labels.items() if h == host and (h, path) in self._tree},
        )
        return self._node(view, "/", label)

    def _navigation(self) -> list[dict[str, Any]]:
        if not self._visited:
            return []
        threshold = 1 if self._visited == 1 else max(2, math.ceil(self._visited / 2))
        best: dict[tuple[str, str], tuple[int, str]] = {}  # (label, url) -> (pages, parent); first seen first
        for (label, url, parent), count in self._menu.items():
            if count < threshold:
                continue
            current = best.get((label, url))
            if current is None or count > current[0] or (count == current[0] and parent and not current[1]):
                best[label, url] = (count, parent)
        entries: list[dict[str, Any]] = []
        named: dict[str, dict[str, Any]] = {}
        for (label, url), (_, parent) in best.items():
            entry: dict[str, Any] = {"label": label, "url": url, "children": []}
            if parent and parent != label:
                holder = named.get(parent)
                if holder is None:  # a menu heading that is not a link ("Products" opening a submenu)
                    holder = named[parent] = {"label": parent, "url": None, "children": []}
                    entries.append(holder)
                holder["children"].append(entry)
                named.setdefault(label, entry)
            elif label in named and named[label]["url"] is None:
                named[label]["url"] = url  # the heading turned out to be a link too
            else:
                named.setdefault(label, entry)
                entries.append(entry)
        return entries[:30]

    def _duplicates(self) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = []
        seen: set[frozenset[str]] = set()
        for urls in self._texts.values():
            if len(urls) > 1:
                seen.add(frozenset(urls))
                groups.append({"reason": "same text", "urls": sorted(urls.values())[:10], "canonical": None})
        for canonical, urls in self._canonical.items():
            parts = _split(canonical)
            target = _key(parts) if parts else ""
            if frozenset({*urls, target}) in seen or frozenset(urls) in seen:
                continue
            groups.append({"reason": "canonical link", "urls": sorted(urls.values())[:10], "canonical": canonical})
        return groups

    def topology(self, *, complete: bool = False) -> Topology:
        """The topology so far. ``complete``: the crawl visited the whole site, so a page listed in a
        sitemap that no visited page links to is an orphan."""
        hosts: Counter[str] = Counter()
        for (host, path), count in self._tree.items():
            if path == "/":
                hosts[host] += count
        main = hosts.most_common(1)[0][0] if hosts else ""
        site = registrable_domain(main)
        others = [h for h, _ in hosts.most_common() if h != main and registrable_domain(h) == site]
        root = self._host_tree(main)
        sources: Counter[str] = Counter()
        for _, _, flags in self._urls.values():
            for bit, name in ((_LISTED, "listed"), (_LINKED, "linked"), (_VISITED, "visited")):
                if flags & bit:
                    sources[name] += 1
        duplicates = self._duplicates()
        counts = {
            "urls": len(self._urls),
            "listed": sources["listed"],
            "visited": sources["visited"],
            "linked": sources["linked"],
            "dead_ends": self._dead_end_count,
            "duplicates": len(duplicates),
        }
        orphans = None
        if complete and sources["listed"]:
            found = sorted(
                url
                for key, (url, _, flags) in self._urls.items()
                if flags & _LISTED and not flags & _LINKED and key not in self._start
            )
            orphans = found[:100]
            counts["orphans"] = len(found)
        if self._truncated:
            counts["truncated"] = 1
        paginated = {
            path if host == main else f"{host}{path}": n for (host, path), n in self._paginated.most_common(20)
        }
        return Topology(
            site=main,
            root=root,
            hosts=[self._host_tree(h, h) for h in others[:20]],
            navigation=self._navigation(),
            feeds=[feed for feed, _ in self._feeds.most_common(10)],
            html_sitemaps=list(self._html_sitemaps),
            paginated=paginated,
            dead_ends=list(self._dead_ends),
            duplicates=duplicates[:100],
            orphans=orphans,
            counts=counts,
        )

    def __repr__(self) -> str:
        return f"TopologyBuilder({len(self._urls):,} URLs, {self._visited:,} visited)"
