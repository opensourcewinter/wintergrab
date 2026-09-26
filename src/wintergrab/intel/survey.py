"""A quick look at a site: its robots.txt, its sitemaps, and a sample of its pages, with a profile.

::

    survey = survey_site("https://shop.example", pages=30)
    survey.profile.describe()        # technologies, page types, templates, sections, APIs...
    survey.sitemap_entries           # the pages the sitemaps list (50,000 at most)
    survey.pages                     # the sampled pages, with keep_pages=True

``wintergrab inspect`` prints a survey's profile; the goal planner
(:mod:`wintergrab.goals`) starts from one. The sample is the start page, pages
spread across the sitemaps (the ones ``prefer`` likes first) and the pages they
link to, fetched politely: robots.txt is obeyed unless told otherwise.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ..errors import WintergrabError
from ..fetchers.response import Response
from ..request import Request
from ..sitemaps import SitemapEntry, parse_sitemap, robots_sitemaps
from ..spider import Spider
from ..utils import ensure_scheme, host_of
from .profile import SiteProfile, SiteProfiler

__all__ = ["SiteSurvey", "SitemapRead", "read_sitemaps", "survey_site"]

log = logging.getLogger("wintergrab.intel")


@dataclass
class SitemapRead:
    """What :func:`read_sitemaps` found."""

    roots: list[str] = field(default_factory=list)  # the sitemaps it started from
    sitemaps: int = 0  # sitemaps read (indexes included)
    indexes: int = 0
    entries: list[SitemapEntry] = field(default_factory=list)  # the pages listed
    truncated: bool = False  # it stopped at a limit


# Spider settings that are fetcher options too (a spider's default_headers are a fetcher's headers).
_FETCH_SETTINGS = {
    "network_policy": "network_policy", "proxies": "proxies", "cache": "cache", "impersonate": "impersonate",
    "verify": "verify", "default_headers": "headers", "credentials": "credentials",
}  # fmt: skip


def _fetch_options(spider_settings: Mapping[str, Any], timeout: float) -> dict[str, Any]:
    options: dict[str, Any] = {"timeout": timeout}
    for setting, option in _FETCH_SETTINGS.items():
        if spider_settings.get(setting) is not None:
            options[option] = spider_settings[setting]
    return options


def read_sitemaps(
    origin: str,
    robots_text: str | None = None,
    *,
    max_sitemaps: int = 10,
    max_entries: int = 50_000,
    **fetch_options: Any,
) -> SitemapRead:
    """The pages a site's sitemaps list: those named in robots.txt, or ``/sitemap.xml``, following
    sitemap indexes, up to ``max_sitemaps`` sitemaps and ``max_entries`` pages."""
    from ..fetchers import Fetcher

    queue = robots_sitemaps(robots_text, origin + "/robots.txt") if robots_text else []
    queue = queue or [origin + "/sitemap.xml"]
    out = SitemapRead(roots=list(queue))
    seen: set[str] = set()
    with Fetcher(**fetch_options) as fetcher:
        while queue:
            if len(seen) >= max_sitemaps or len(out.entries) >= max_entries:
                out.truncated = True
                break
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                response = fetcher.get(url)
            except WintergrabError:
                continue
            if not response.ok:
                continue
            try:
                _, found = parse_sitemap(response.body, response.url)
            except ValueError:
                continue
            if not found:
                continue
            out.sitemaps += 1
            children = [e.loc for e in found if e.kind == "sitemap"]
            if children:
                out.indexes += 1
                queue.extend(children)
            out.entries.extend(e for e in found if e.kind == "url")
    if len(out.entries) > max_entries:
        del out.entries[max_entries:]
        out.truncated = True
    return out


@dataclass
class SiteSurvey:
    """What :func:`survey_site` found (see the module docs)."""

    url: str
    profile: SiteProfile
    robots_text: str | None = None
    robots_found: bool = False
    sitemaps: SitemapRead = field(default_factory=SitemapRead)
    pages: list[Response] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def origin(self) -> str:
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def sitemap_entries(self) -> list[SitemapEntry]:
        return self.sitemaps.entries


class _SurveySpider(Spider):
    """Wanders a site's pages from the start URLs, keeping them if asked, recording API calls in a browser."""

    name = "survey"
    capture_api = False
    keep_pages = False
    url_rules = True  # skip media, archives and crawler traps
    #: ``prefer(url) -> score``: links worth more are followed first.
    prefer: Any = None

    def __init__(self, **settings: Any) -> None:
        super().__init__(**settings)
        self.kept: list[Response] = []

    def _request(self, url: str) -> Request:
        request = Request(url, dont_filter=False)
        if self.capture_api:
            request.options["capture"] = True
        if self.prefer is not None:
            request.priority = int(10 * float(self.prefer(url)))
        return request

    def start_requests(self) -> Any:
        for url in self.start_urls:
            yield self._request(str(url))

    def parse(self, response: Response) -> Any:
        if self.keep_pages and response.is_html:
            self.kept.append(response)
        if not response.is_html:
            return
        for link in response.links(same_domain=True):
            yield self._request(link)
        next_url = response.next_page()
        if next_url:
            yield self._request(next_url)


def _spread(urls: list[str], count: int) -> list[str]:
    """``count`` URLs spread evenly across ``urls``."""
    if count <= 0 or not urls:
        return []
    step = max(1, len(urls) // count)
    return urls[::step][:count]


def survey_site(
    url: str,
    *,
    pages: int = 30,
    sitemaps: bool = True,
    obey_robots: bool = True,
    browser: bool = False,
    timeout: float = 20.0,
    keep_pages: bool = False,
    prefer: Callable[[str], bool | float] | None = None,
    extra_urls: Iterable[str] = (),
    log_level: str | None = "WARNING",
    **spider_settings: Any,
) -> SiteSurvey:
    """Read ``url``'s site: robots.txt, sitemaps, and ``pages`` pages, into a :class:`SiteSurvey`.

    Args:
        pages: Pages to visit, the start page included.
        sitemaps: Read the sitemaps (a third of the sample comes from them).
        obey_robots: Obey robots.txt (it is read either way).
        browser: Render the pages, and record their API calls.
        keep_pages: Keep the sampled pages (``survey.pages``).
        prefer: How much a page is worth sampling (``prefer(url) -> score``, ``True``/``False`` too):
            the sitemaps' pages are sampled best first, and links are followed best first.
        extra_urls: Pages to visit besides the start page and the sitemap sample.
    """
    from ..fetchers import Fetcher

    start = ensure_scheme(url)
    parts = urlsplit(start)
    origin = f"{parts.scheme}://{parts.netloc}"
    profiler = SiteProfiler()
    robots_text: str | None = None
    found = False
    # robots.txt and the sitemaps are read as the pages are: through the same network policy, proxies and cache
    fetch = _fetch_options(spider_settings, timeout)
    try:
        with Fetcher(**fetch) as fetcher:
            robots = fetcher.get(origin + "/robots.txt")
        found = robots.status == 200
        robots_text = robots.text if found else None
        profiler.add_robots(robots_text, found=found)
    except WintergrabError as exc:
        log.warning("could not read robots.txt (%s)", exc)
        profiler.add_robots(None, found=False, error=getattr(exc, "message", None) or str(exc))
    read = SitemapRead()
    samples: list[str] = []
    if sitemaps:
        read = read_sitemaps(origin, robots_text, **fetch)
        if read.sitemaps:
            profiler.add_sitemaps(read.sitemaps, read.indexes, read.entries)
        host = host_of(origin)
        listed = [e.loc for e in read.entries if host_of(e.loc) == host]
        share = max(0, pages // 3)
        if prefer is not None:
            scores = {u: float(prefer(u)) for u in listed}
            for best in sorted({s for s in scores.values() if s > 0}, reverse=True):
                samples += _spread([u for u in listed if scores[u] == best], share - len(samples))
                if len(samples) >= share:
                    break
        taken = set(samples)
        samples += _spread([u for u in listed if u not in taken], share - len(samples))
    spider = _SurveySpider(
        start_urls=list(dict.fromkeys([start, *extra_urls, *samples])),
        allowed_domains=[host_of(start)],
        max_pages=pages,
        profile=profiler,
        obey_robots_txt=obey_robots,
        use_browser=browser,
        capture_api=browser,
        keep_pages=keep_pages,
        prefer=prefer,
        timeout=timeout,
        output=None,
        keep_items=False,
        progress=False,
        log_level=log_level,
        **spider_settings,
    )
    result = spider.run(resume=False)
    profile = result.profile
    if profile is None:
        raise WintergrabError(f"no profile could be built for {start}")
    return SiteSurvey(
        url=start,
        profile=profile,
        robots_text=robots_text,
        robots_found=found,
        sitemaps=read,
        pages=spider.kept,
        stats=dict(result.stats),
    )
