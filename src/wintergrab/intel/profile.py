"""A website's profile: what it runs on, how it is organized, and how it treats a crawler.

::

    profiler = SiteProfiler()
    for response in responses:          # pages you fetched
        profiler.observe(response)
    profile = profiler.profile()
    print(profile.describe())

Spiders build one with ``profile = True`` (``result.profile``), and
``wintergrab inspect URL`` samples a site, reads its robots.txt and sitemaps
and prints one. The profile holds the site's technologies, languages and
regions, page types, template clusters (pages with the same layout, and
their URL pattern), structured data, links, the API endpoints its pages call,
latency and error rate, and what makes it easy or hard to crawl.

Endpoints come from the pages themselves: calls in inline scripts
(``fetch("/api/...")``, axios, jQuery), API-looking paths (``/api/``,
``/graphql``, ``/wp-json/``), JSON alternate links, what a browser recorded
(``capture=``) and the conventions of detected platforms (a WordPress site
has ``/wp-json/``). Conventional ones are marked as such: they are not
requested.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from ..data.normalize import normalize_language
from ..data.similarity import hamming
from ..extraction.page import PageContext
from ..fetchers.resources import registrable_domain
from ..history.snapshot import snapshot_page
from .classify import classify_page
from .tech import detect_technologies

__all__ = ["Endpoint", "SiteProfile", "SiteProfiler", "TemplateCluster"]

_CALL = re.compile(
    r"""(?:\bfetch|\baxios(?:\.(?P<verb>get|post|put|patch|delete|request))?|\$\.(?P<jquery>ajax|get|post|getJSON)"""
    r"""|\.open)\s*\(\s*(?:["'](?P<method>GET|POST|PUT|PATCH|DELETE)["']\s*,\s*)?"""
    r"""(?P<q>["'`])(?P<url>[^"'`\s]{2,300}?)(?P=q)""",
    re.I,
)
_VERBS = {"get": "GET", "getjson": "GET", "post": "POST", "put": "PUT", "patch": "PATCH", "delete": "DELETE"}
_API_PATH = re.compile(
    r"""(?P<q>["'`])(?P<url>(?:https?://[^"'`\s/]+)?/(?:api|graphql|gql|wp-json|rest|ajax|_api|jsonapi|v[1-9])"""
    r"""(?:[/?][^"'`\s]{0,300})?)(?P=q)"""
)
_ASSET = re.compile(
    r"\.(?:js|mjs|css|png|jpe?g|gif|svg|webp|avif|ico|woff2?|ttf|eot|map|mp4|webm|mp3|pdf)(?:$|\?)", re.I
)
# Path segments that are ids, not names: /product/123, /order/9f8e7d6c-... (endpoints are shown per pattern)
_ID_SEGMENT = re.compile(r"\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{24,}", re.I)
_DATA_URL_ATTRS = ("data-url", "data-endpoint", "data-api", "data-src-api", "data-action-url")
_GENERIC_TLDS = frozenset({"co", "io", "ai", "tv", "me", "fm", "am", "ly", "gg", "to", "cc", "ws", "nu", "sh", "app"})
_PLATFORM_ENDPOINTS: dict[str, list[tuple[str, str]]] = {
    "WordPress": [("/wp-json/", "WordPress REST API")],
    "WooCommerce": [("/wp-json/wc/store/v1/products", "WooCommerce Store API")],
    "Shopify": [("/products.json", "Shopify products feed"), ("/collections.json", "Shopify collections")],
    "Drupal": [("/jsonapi", "Drupal JSON:API (when enabled)")],
    "Magento": [("/rest/V1/", "Magento REST API (mostly authenticated)")],
    "Ghost": [("/ghost/api/content/", "Ghost Content API (needs a key)")],
}
# Layouts this many bits apart (or fewer) are one template; more tolerant for pages whose URLs have
# the same shape (small pages' layouts move more with one element: the last page has no "next" link).
_TEMPLATE_BITS = 6
_TEMPLATE_BITS_SAME_URLS = 16


@dataclass
class Endpoint:
    """An API endpoint seen in (or conventional for) the site's pages.

    Attributes:
        url: The endpoint; query values are left out (``/api/products?page=``).
        method: When known (``GET``, ``POST``).
        source: ``"script"`` (a call or an API path in an inline script or a ``data-`` attribute),
            ``"link"`` (a JSON alternate link), ``"captured"`` (recorded by a browser) or
            ``"platform"`` (the detected platform's convention, not requested).
        pages: How many pages mention it.
        note: What it is, for platform endpoints.
        status, content_type: For captured endpoints.
    """

    url: str
    method: str | None
    source: str
    pages: int = 1
    note: str | None = None
    status: int | None = None
    content_type: str | None = None


@dataclass
class TemplateCluster:
    """Pages that share a layout (their tag structure), with the URL pattern they follow."""

    pattern: str
    pages: int
    page_type: str | None
    examples: list[str]
    layout: int = 0


@dataclass
class SiteProfile:
    """What :class:`SiteProfiler` learned about a site (see the module docs)."""

    domains: list[str]
    pages: int
    statuses: dict[int, int]
    error_rate: float
    average_latency: float | None
    bytes: int
    technologies: list[dict[str, Any]]
    languages: dict[str, int]
    regions: list[str]
    page_types: dict[str, int]
    templates: list[TemplateCluster]
    structured_data: dict[str, int]
    internal_links: int
    external_links: int
    external_domains: dict[str, int]
    endpoints: list[Endpoint]
    crawlability: dict[str, Any]
    sitemaps: dict[str, Any] | None = None
    change_frequency: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def describe(self, limit: int = 8) -> str:
        """The profile as a short report."""
        ok = sum(n for s, n in self.statuses.items() if 200 <= s < 400)
        errors = ", ".join(f"{s} x{n}" for s, n in sorted(self.statuses.items()) if s >= 400)
        latency = f", {self.average_latency:.2f} s per response" if self.average_latency is not None else ""
        lines = [
            f"{', '.join(self.domains) or 'site'}: {self.pages:,} pages ({ok:,} ok"
            + (f", errors: {errors}" if errors else "")
            + f"){latency}"
        ]
        if self.technologies:
            shown = []
            for tech in self.technologies[:limit]:
                version = f" {tech['version']}" if tech.get("version") else ""
                shown.append(f"{tech['name']}{version} ({tech['category']})")
            lines.append("technologies: " + ", ".join(shown))
        if self.languages or self.regions:
            languages = ", ".join(f"{code} ({n})" for code, n in self.languages.items()) or "unknown"
            regions = f"; regions: {', '.join(self.regions)}" if self.regions else ""
            lines.append(f"languages: {languages}{regions}")
        if self.page_types:
            lines.append("page types: " + ", ".join(f"{t} {n}" for t, n in self.page_types.items()))
        if self.templates:
            lines.append("templates:")
            width = max(len(t.pattern) for t in self.templates[:limit])
            for template in self.templates[:limit]:
                kind = f"  {template.page_type}" if template.page_type else ""
                pages = f"{template.pages:>5,} {'page' if template.pages == 1 else 'pages':<5}"
                lines.append(f"  {template.pattern.ljust(width)}  {pages}{kind}")
        if self.structured_data:
            lines.append(
                "structured data: " + ", ".join(f"{t} {n}" for t, n in list(self.structured_data.items())[:limit])
            )
        domains = ", ".join(f"{d} {n}" for d, n in list(self.external_domains.items())[:5])
        lines.append(
            f"links: {self.internal_links:,} internal, {self.external_links:,} external"
            + (f" ({domains})" if domains else "")
        )
        if self.endpoints:
            lines.append("APIs:")
            for endpoint in self.endpoints[:limit]:
                method = f"{endpoint.method} " if endpoint.method else ""
                detail = endpoint.note or (f"{endpoint.pages} page(s)" if endpoint.source != "captured" else "")
                status = f", {endpoint.status}" if endpoint.status else ""
                lines.append(f"  {method}{endpoint.url}  ({endpoint.source}{status}{', ' if detail else ''}{detail})")
        if self.sitemaps:
            s = self.sitemaps
            lastmod = f", {s['lastmod_share']:.0%} with lastmod" if s.get("pages") else ""
            lines.append(f"sitemaps: {s['sitemaps']} ({s['indexes']} index), {s['pages']:,} pages listed{lastmod}")
        lines.append("crawlability: " + "; ".join(_crawlability_lines(self.crawlability)))
        if self.change_frequency is not None:
            lines.append(f"change frequency: {self.change_frequency:.2f} change(s) per page per day (median)")
        return "\n".join(lines)


def _crawlability_lines(info: dict[str, Any]) -> list[str]:
    out = []
    robots = info.get("robots")
    if robots is None:
        out.append("robots.txt not read")
    elif not robots.get("found"):
        out.append("no robots.txt")
    else:
        verdict = "disallows crawling" if robots.get("disallow_all") else "allows crawling"
        delay = f", crawl-delay {robots['crawl_delay']:g}" if robots.get("crawl_delay") else ""
        out.append(f"robots.txt {verdict}{delay}")
    for key, label in (
        ("noindex_pages", "noindex"),
        ("js_required_pages", "need JavaScript"),
        ("blocked_pages", "blocked (403/429)"),
    ):
        if info.get(key):
            out.append(f"{info[key]} page(s) {label}")
    if info.get("bot_protection"):
        out.append("bot protection: " + ", ".join(info["bot_protection"]))
    if info.get("canonical_pages"):
        out.append(f"{info['canonical_pages']} page(s) with a canonical link")
    return out


def _url_pattern(urls: list[str]) -> str:
    """``/product/12``, ``/product/34`` -> ``/product/{id}``; different lengths keep the common start."""
    paths = [[s for s in urlsplit(u).path.split("/") if s] for u in urls]
    if not paths:
        return "/"
    lengths = Counter(len(p) for p in paths)
    size = lengths.most_common(1)[0][0]
    same = [p for p in paths if len(p) == size]
    parts = []
    for segments in zip(*same, strict=False):
        values = set(segments)
        if len(values) == 1:
            parts.append(segments[0])
        elif all(v.isdigit() for v in values):
            parts.append("{id}")
        else:
            parts.append("{slug}")
    pattern = "/" + "/".join(parts)
    return pattern + ("/..." if len(lengths) > 1 else "")


def _url_shape(url: str) -> tuple[str, ...]:
    """``/product/12`` and ``/product/34`` have one shape: the first segment, then numbers or words."""
    segments = [s for s in urlsplit(url).path.split("/") if s]
    if not segments:
        return ("",)
    return (segments[0], *("{id}" if s.isdigit() else "*" for s in segments[1:]))


def _clean_endpoint(base: str, raw: str) -> str | None:
    raw = raw.strip()
    if not raw or raw.startswith(("data:", "javascript:", "#", "mailto:", "tel:")) or "${" in raw or "{{" in raw:
        return None
    url = urljoin(base, raw)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or _ASSET.search(parts.path):
        return None
    query = "&".join(sorted({f"{pair.split('=', 1)[0]}=" for pair in parts.query.split("&") if pair}))
    path = "/".join("{id}" if _ID_SEGMENT.fullmatch(segment) else segment for segment in parts.path.split("/"))
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


class SiteProfiler:
    """Builds a :class:`SiteProfile` from the pages of a site (see the module docs).

    Args:
        detailed: How many HTML pages get the full analysis (page type, technologies, layout,
            structured data); every page counts for statuses, latency, links and endpoints.
    """

    def __init__(self, *, detailed: int = 500) -> None:
        self.detailed = detailed
        self._pages = 0
        self._analyzed = 0
        self._statuses: Counter[int] = Counter()
        self._latencies: list[float] = []
        self._bytes = 0
        self._domains: Counter[str] = Counter()
        self._tech: dict[str, dict[str, Any]] = {}
        self._languages: Counter[str] = Counter()
        self._regions: Counter[str] = Counter()
        self._types: Counter[str] = Counter()
        self._structured: Counter[str] = Counter()
        self._layouts: list[tuple[int, str, str]] = []  # (layout, url, page type)
        self._internal: set[str] = set()
        self._external: set[str] = set()
        self._external_domains: Counter[str] = Counter()
        self._endpoints: dict[tuple[str | None, str], Endpoint] = {}
        self._crawl: Counter[str] = Counter()
        self._protection: set[str] = set()
        self.robots: dict[str, Any] | None = None
        self.sitemaps: dict[str, Any] | None = None
        self.change_frequency: float | None = None

    # -- observing ------------------------------------------------------------------------------ #
    def observe(self, response: Any, *, latency: float | None = None) -> None:
        """Take a fetched page (a :class:`~wintergrab.Response`) into account."""
        self._pages += 1
        status = int(getattr(response, "status", 200) or 200)
        self._statuses[status] += 1
        elapsed = latency if latency is not None else getattr(response, "elapsed", None)
        if elapsed:
            self._latencies.append(float(elapsed))
        self._bytes += len(getattr(response, "body", b"") or b"")
        url = getattr(response, "url", "") or ""
        host = urlsplit(url).hostname or ""
        if host:
            self._domains[registrable_domain(host)] += 1
        if status in (403, 429):
            self._crawl["blocked_pages"] += 1
        for captured in getattr(response, "captured", None) or ():
            self._endpoint(
                captured.url,
                getattr(captured, "method", None),
                "captured",
                status=captured.status,
                content_type=(captured.headers or {}).get("content-type", "").split(";")[0] or None,
            )
        if not (200 <= status < 300) or not getattr(response, "is_html", False):
            return
        ctx = PageContext(response)
        self._links_and_endpoints(ctx, url)
        self._crawlability(ctx, response)
        if self._analyzed < self.detailed:
            self._analyzed += 1
            self._analyze(ctx, response, url)

    def _analyze(self, ctx: PageContext, response: Any, url: str) -> None:
        page_type = classify_page(ctx)
        self._types[page_type.type] += 1
        for tech in detect_technologies(ctx):
            seen = self._tech.setdefault(
                tech.name,
                {"name": tech.name, "category": tech.category, "version": None, "confidence": 0.0, "pages": 0},
            )
            seen["pages"] += 1
            seen["confidence"] = max(seen["confidence"], tech.confidence)
            seen["version"] = seen["version"] or tech.version
            if tech.category == "security":
                self._protection.add(tech.name)
        snap = snapshot_page(ctx)
        self._layouts.append((snap.layout, url, page_type.type))
        for name in snap.types:
            self._structured[name] += 1
        if ctx.structured.get("opengraph"):
            self._structured["OpenGraph"] += 1
        root = ctx.selector.root
        lang = (root.get("lang") if root is not None else None) or response.headers.get("content-language")
        code = normalize_language(str(lang).split(",")[0].strip()) if lang else None
        if code:
            self._languages[code.split("-")[0]] += 1
            if "-" in code and len(code.split("-")[-1]) == 2:
                self._regions[code.split("-")[-1].upper()] += 1
        for element in ctx.selector.css("link[rel=alternate][hreflang]"):
            tag = (element.attr("hreflang") or "").replace("_", "-")
            if "-" in tag and len(tag.split("-")[-1]) == 2:
                self._regions[tag.split("-")[-1].upper()] += 1
        locale = str((ctx.structured.get("opengraph") or {}).get("locale") or "").replace("_", "-")
        if "-" in locale and len(locale.split("-")[-1]) == 2:
            self._regions[locale.split("-")[-1].upper()] += 1

    def _links_and_endpoints(self, ctx: PageContext, url: str) -> None:
        site = registrable_domain(urlsplit(url).hostname or "")
        for element in ctx.selector.css("a[href]"):
            href = (element.attr("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            link = urljoin(url, href).split("#")[0]
            host = urlsplit(link).hostname or ""
            if not host:
                continue
            if registrable_domain(host) == site:
                self._internal.add(link)
            elif link not in self._external:
                self._external.add(link)
                self._external_domains[registrable_domain(host)] += 1
        found: set[tuple[str | None, str]] = set()
        for script in ctx.selector.css("script:not([src])"):
            code = script.root.text if script.root is not None else None
            if not code or len(code) > 2_000_000:
                continue
            for match in _CALL.finditer(code):
                endpoint = _clean_endpoint(url, match.group("url"))
                if endpoint:
                    # the method, when the call says it: axios.post(...), $.getJSON(...), xhr.open("POST", ...)
                    verb = (match.group("method") or match.group("verb") or match.group("jquery") or "").lower()
                    found.add((_VERBS.get(verb), endpoint))
            for match in _API_PATH.finditer(code):
                endpoint = _clean_endpoint(url, match.group("url"))
                if endpoint:
                    found.add((None, endpoint))
        for attr in _DATA_URL_ATTRS:
            for element in ctx.selector.css(f"[{attr}]"):
                endpoint = _clean_endpoint(url, element.attr(attr) or "")
                if endpoint:
                    found.add((None, endpoint))
        for element in ctx.selector.css("link[rel=alternate][type*=json], link[rel='https://api.w.org/']"):
            endpoint = _clean_endpoint(url, element.attr("href") or "")
            if endpoint:
                self._endpoint(endpoint, "GET", "link")
        for method, endpoint in found:
            self._endpoint(endpoint, method, "script")

    def _endpoint(self, url: str, method: str | None, source: str, **extra: Any) -> None:
        if source == "captured":
            cleaned = _clean_endpoint(url, url) or url
            url = cleaned
        key = (method, url)
        existing = self._endpoints.get(key)
        if existing is None:
            self._endpoints[key] = Endpoint(url, method, source, **extra)
        else:
            existing.pages += 1

    def _crawlability(self, ctx: PageContext, response: Any) -> None:
        robots_meta = " ".join(
            el.attr("content") or "" for el in ctx.selector.css("meta[name=robots], meta[name=googlebot]")
        )
        header = response.headers.get("x-robots-tag", "") if hasattr(response, "headers") else ""
        if "noindex" in (robots_meta + " " + header).lower():
            self._crawl["noindex_pages"] += 1
        if ctx.selector.css("link[rel=canonical]"):
            self._crawl["canonical_pages"] += 1
        text = ctx.text
        scripts = len(ctx.selector.css("script"))
        shell = ctx.selector.css("#root, #app, #__next, #__nuxt, [data-reactroot], app-root")
        if len(text) < 200 and (scripts >= 3 or shell):
            self._crawl["js_required_pages"] += 1

    def add_robots(self, text: str | None, *, found: bool = True) -> None:
        """What the site's robots.txt says for every crawler (``*``)."""
        if not found or text is None:
            self.robots = {"found": False}
            return
        disallow_all = False
        delay: float | None = None
        sitemaps = 0
        applies = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            name, _, value = line.partition(":")
            name, value = name.strip().lower(), value.strip()
            if name == "user-agent":
                applies = value == "*"
            elif name == "sitemap" and value:
                sitemaps += 1
            elif applies and name == "disallow" and value == "/":
                disallow_all = True
            elif applies and name == "crawl-delay":
                try:
                    delay = float(value)
                except ValueError:
                    pass
        self.robots = {"found": True, "disallow_all": disallow_all, "crawl_delay": delay, "sitemaps": sitemaps}

    def add_sitemaps(self, sitemaps: int, indexes: int, entries: Iterable[Any]) -> None:
        """How many sitemaps (and indexes) the site has, and the pages they list."""
        rows = list(entries)
        dated = sum(1 for e in rows if getattr(e, "lastmod", None))
        self.sitemaps = {
            "sitemaps": sitemaps,
            "indexes": indexes,
            "pages": len(rows),
            "lastmod_share": round(dated / len(rows), 3) if rows else 0.0,
        }

    # -- the profile ---------------------------------------------------------------------------- #
    def _templates(self) -> list[TemplateCluster]:
        clusters: list[tuple[int, tuple[str, ...], list[str], Counter[str]]] = []
        for layout, url, page_type in self._layouts:
            shape = _url_shape(url)
            for representative, cluster_shape, urls, types in clusters:
                limit = _TEMPLATE_BITS_SAME_URLS if shape == cluster_shape else _TEMPLATE_BITS
                if hamming(representative, layout) <= limit:
                    urls.append(url)
                    types[page_type] += 1
                    break
            else:
                clusters.append((layout, shape, [url], Counter({page_type: 1})))
        out = []
        for layout, _, urls, types in clusters:
            kind = types.most_common(1)[0][0]
            out.append(
                TemplateCluster(_url_pattern(urls), len(urls), None if kind == "unknown" else kind, urls[:3], layout)
            )
        return sorted(out, key=lambda t: -t.pages)

    def profile(self) -> SiteProfile:
        errors = sum(n for s, n in self._statuses.items() if s >= 400)
        # the same URL found with its method (axios.post("/api/cart")) and without it (the path alone)
        with_method = {url for method, url in self._endpoints if method is not None}
        endpoints = [e for (method, url), e in self._endpoints.items() if method is not None or url not in with_method]
        origins = {urlsplit(t).scheme + "://" + (urlsplit(t).netloc or "") for _, t, _ in self._layouts}
        for name in self._tech:
            for path, note in _PLATFORM_ENDPOINTS.get(name, ()):
                for origin in sorted(origins)[:1]:
                    url = origin + path
                    if ("GET", url) not in self._endpoints and (None, url) not in self._endpoints:
                        endpoints.append(Endpoint(url, "GET", "platform", 0, note))
        order = {"captured": 0, "link": 1, "script": 2, "platform": 3}
        endpoints.sort(key=lambda e: (order[e.source], -e.pages, e.url))
        regions = [code for code, _ in self._regions.most_common()]
        for domain in self._domains:
            tld = domain.rsplit(".", 1)[-1]
            if len(tld) == 2 and tld not in _GENERIC_TLDS and tld.upper() not in regions:
                regions.append(tld.upper())
        crawl: dict[str, Any] = dict(self._crawl)
        crawl["robots"] = self.robots
        crawl["bot_protection"] = sorted(self._protection)
        return SiteProfile(
            domains=[d for d, _ in self._domains.most_common()],
            pages=self._pages,
            statuses=dict(sorted(self._statuses.items())),
            error_rate=round(errors / self._pages, 4) if self._pages else 0.0,
            average_latency=round(statistics.fmean(self._latencies), 4) if self._latencies else None,
            bytes=self._bytes,
            technologies=sorted(self._tech.values(), key=lambda t: (-t["pages"], -t["confidence"], t["name"])),
            languages=dict(self._languages.most_common()),
            regions=regions,
            page_types=dict(self._types.most_common()),
            templates=self._templates(),
            structured_data=dict(self._structured.most_common()),
            internal_links=len(self._internal),
            external_links=len(self._external),
            external_domains=dict(self._external_domains.most_common(10)),
            endpoints=endpoints,
            crawlability=crawl,
            sitemaps=self.sitemaps,
            change_frequency=self.change_frequency,
        )
