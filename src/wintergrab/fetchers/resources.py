"""Which sub-resources a browser downloads: resource types, ad/analytics/tracker domains, third parties.

A rendered page pulls in dozens of scripts, images, fonts, ads and trackers
that a scraper never needs. Blocking them saves bandwidth and time, and makes
pages render more predictably. :class:`ResourceFilter` decides, request by
request; :class:`~wintergrab.AsyncBrowserFetcher` applies it.

The built-in lists (``"ads"``, ``"analytics"``, ``"trackers"``) cover
widely-used services; they are not exhaustive. Load complete community lists
with :meth:`ResourceFilter.load_list` (hosts files, plain domain lists and the
``||domain^`` rules of Adblock-style lists are understood).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..errors import ConfigurationError

__all__ = ["BUILTIN_LISTS", "DEFAULT_BLOCKED_RESOURCES", "ResourceFilter", "registrable_domain"]

#: Resource types a scraper rarely needs.
DEFAULT_BLOCKED_RESOURCES = ("image", "media", "font")
#: Every resource type Playwright reports.
RESOURCE_TYPES = frozenset(
    {"document", "stylesheet", "image", "media", "font", "script", "texttrack", "xhr", "fetch", "eventsource",
     "websocket", "manifest", "other", "ping", "prefetch", "cspviolationreport", "preflight", "signedexchange"}
)  # fmt: skip

_ADS = frozenset(
    {
        "doubleclick.net", "googlesyndication.com", "googleadservices.com", "adservice.google.com",
        "adnxs.com", "adsrvr.org", "criteo.com", "criteo.net", "taboola.com", "outbrain.com",
        "amazon-adsystem.com", "advertising.com", "rubiconproject.com", "pubmatic.com", "openx.net",
        "casalemedia.com", "indexww.com", "smartadserver.com", "adform.net", "yieldmo.com", "media.net",
        "moatads.com", "serving-sys.com", "2mdn.net", "adroll.com", "bidswitch.net", "sharethrough.com",
        "teads.tv", "3lift.com", "33across.com", "contextweb.com", "lijit.com", "sovrn.com", "gumgum.com",
        "spotxchange.com", "springserve.com", "zemanta.com", "revcontent.com", "mgid.com", "propellerads.com",
        "popads.net", "adcolony.com", "applovin.com", "inmobi.com", "yieldlab.net", "adition.com",
        "flashtalking.com", "innovid.com", "doubleverify.com", "adsafeprotected.com", "quantcast.com",
        "adsymptotic.com", "rlcdn.com", "bluekai.com", "exelator.com", "agkn.com", "mathtag.com",
        "turn.com", "tapad.com", "crwdcntrl.net", "eyeota.net", "lotame.com", "id5-sync.com",
    }
)  # fmt: skip
_ANALYTICS = frozenset(
    {
        "google-analytics.com", "googletagmanager.com", "analytics.google.com", "hotjar.com", "hotjar.io",
        "mixpanel.com", "segment.com", "segment.io", "amplitude.com", "heap.io", "heapanalytics.com",
        "fullstory.com", "mouseflow.com", "crazyegg.com", "clarity.ms", "nr-data.net", "quantserve.com",
        "scorecardresearch.com", "chartbeat.com", "chartbeat.net", "parsely.com", "parse.ly", "statcounter.com",
        "kissmetrics.com", "omtrdc.net", "demdex.net", "2o7.net", "everesttech.net", "mc.yandex.ru",
        "plausible.io", "cloudflareinsights.com", "contentsquare.net", "quantummetric.com", "smartlook.com",
        "luckyorange.com", "inspectlet.com", "lr-ingest.io", "mparticle.com", "matomo.cloud", "pendo.io",
        "kissmetrics.io", "woopra.com", "clicky.com", "getclicky.com",
    }
)  # fmt: skip
_TRACKERS = frozenset(
    {
        "connect.facebook.net", "bat.bing.com", "px.ads.linkedin.com", "snap.licdn.com", "analytics.twitter.com",
        "static.ads-twitter.com", "analytics.tiktok.com", "ct.pinterest.com", "tr.snapchat.com", "sc-static.net",
        "addthis.com", "sharethis.com", "fingerprintjs.com", "fpjs.io", "branch.io", "app-measurement.com",
        "adjust.com", "appsflyer.com", "kochava.com", "onesignal.com", "pushwoosh.com",
    }
)  # fmt: skip

#: Built-in domain lists: ``"ads"``, ``"analytics"`` and ``"trackers"``.
BUILTIN_LISTS: dict[str, frozenset[str]] = {"ads": _ADS, "analytics": _ANALYTICS, "trackers": _TRACKERS}

# Common multi-label public suffixes. Without the full Public Suffix List this is an
# approximation, good enough to tell "same site" from "third party" on real sites.
_MULTI_SUFFIXES = frozenset(
    {
        "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "com.au", "net.au", "org.au",
        "edu.au", "gov.au", "co.nz", "org.nz", "co.jp", "ne.jp", "or.jp", "ac.jp", "co.in", "net.in", "org.in",
        "gov.in", "ac.in", "com.br", "net.br", "org.br", "gov.br", "com.cn", "net.cn", "org.cn", "gov.cn",
        "com.mx", "org.mx", "gob.mx", "co.za", "org.za", "gov.za", "com.ar", "com.tr", "com.tw", "com.hk",
        "com.sg", "com.my", "co.kr", "or.kr", "co.id", "or.id", "co.il", "org.il", "com.ua", "co.th", "in.th",
        "com.vn", "com.ph", "com.pk", "com.ng", "com.eg", "com.sa", "com.co", "com.pe", "com.ve", "com.ec",
        "co.ke", "github.io", "herokuapp.com", "blogspot.com", "cloudfront.net", "appspot.com",
        "azurewebsites.net", "netlify.app", "vercel.app", "pages.dev", "workers.dev", "firebaseapp.com",
    }
)  # fmt: skip


def registrable_domain(host: str) -> str:
    """The "site" part of a host name (``shop.example.co.uk`` -> ``example.co.uk``).

    Approximate: common multi-label suffixes are known, the rest is treated as
    a one-label suffix. IP addresses are returned unchanged.
    """
    host = host.lower().rstrip(".")
    if not host or host.replace(".", "").isdigit() or ":" in host:
        return host
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in _MULTI_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _suffix_in(host: str, domains: frozenset[str] | set[str]) -> bool:
    """``host`` or one of its parent domains is in ``domains``."""
    while host:
        if host in domains:
            return True
        _, dot, host = host.partition(".")
        if not dot:
            return False
    return False


_HOSTS_LINE = re.compile(r"^\s*(?:0\.0\.0\.0|127\.0\.0\.1|::1?)\s+([^\s#]+)")
_ADBLOCK_LINE = re.compile(r"^\|\|([a-z0-9.-]+)\^(?:\$third-party)?\s*$", re.I)
_DOMAIN_LINE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+$", re.I)


class ResourceFilter:
    """Decides which browser requests to block.

    Args:
        block_types: Resource types to block (``"image"``, ``"media"``,
            ``"font"``, ``"stylesheet"``, ``"script"``...).
        lists: Built-in domain lists to block: any of ``"ads"``,
            ``"analytics"``, ``"trackers"``. Only third-party requests are
            checked against them, so crawling one of those sites still works.
        block_domains: Extra domains (and subdomains) to block.
        allow_domains: Domains never blocked (checked first).
        block_third_party: Block every request to another site than the page's
            (except ``allow_domains``). Pages often need their CDN; use with care.
        block_patterns: Regexes; matching URLs are blocked.
        allow_patterns: Regexes; matching URLs are never blocked.

    The page itself (the main document) is never blocked. ``stats`` counts
    blocked requests by reason.
    """

    def __init__(
        self,
        *,
        block_types: Iterable[str] = DEFAULT_BLOCKED_RESOURCES,
        lists: Iterable[str] = (),
        block_domains: Iterable[str] = (),
        allow_domains: Iterable[str] = (),
        block_third_party: bool = False,
        block_patterns: Iterable[str] = (),
        allow_patterns: Iterable[str] = (),
    ) -> None:
        self.block_types = frozenset(t.lower() for t in block_types)
        unknown = self.block_types - RESOURCE_TYPES
        if unknown:
            raise ConfigurationError(f"unknown resource type(s): {', '.join(sorted(unknown))}", key="block_types")
        self.lists = tuple(lists)
        self.list_domains: set[str] = set()
        for name in self.lists:
            if name not in BUILTIN_LISTS:
                raise ConfigurationError(
                    f"unknown list {name!r}; built-in lists: {', '.join(BUILTIN_LISTS)}", key="lists"
                )
            self.list_domains.update(BUILTIN_LISTS[name])
        self.block_domains = {d.lower().lstrip(".") for d in block_domains}
        self.allow_domains = {d.lower().lstrip(".") for d in allow_domains}
        self.block_third_party = block_third_party
        self.block_patterns = [re.compile(p) for p in block_patterns]
        self.allow_patterns = [re.compile(p) for p in allow_patterns]
        self.stats: Counter[str] = Counter()

    @classmethod
    def coerce(cls, value: Any, block_types: Iterable[str] | None = None) -> ResourceFilter | None:
        """``None``/``False`` -> none; a filter -> itself; a dict -> options; ``True`` -> ads+analytics+trackers."""
        if isinstance(value, ResourceFilter):
            return value
        types = tuple(block_types) if block_types is not None else DEFAULT_BLOCKED_RESOURCES
        if value is None or value is False:
            return cls(block_types=types) if types else None
        if value is True:
            return cls(block_types=types, lists=tuple(BUILTIN_LISTS))
        if isinstance(value, Mapping):
            options = dict(value)
            options.setdefault("block_types", types)
            try:
                return cls(**options)
            except TypeError as exc:
                raise ConfigurationError(str(exc), key="resource_filter") from exc
        raise ConfigurationError(f"expected True, a dict or a ResourceFilter, got {value!r}", key="resource_filter")

    def load_list(self, path: str | Path) -> int:
        """Add the domains of a blocklist file. Returns how many were added.

        Understands hosts files (``0.0.0.0 ads.example``), plain domain lists
        and ``||domain^`` rules; other Adblock rules (paths, cosmetic filters)
        are skipped.
        """
        added = 0
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "!", "[")):
                continue
            match = _HOSTS_LINE.match(line) or _ADBLOCK_LINE.match(line)
            domain = match.group(1) if match else (line if _DOMAIN_LINE.match(line) else None)
            if domain and domain not in ("localhost", "localhost.localdomain", "0.0.0.0"):
                domain = domain.lower()
                if domain not in self.block_domains:
                    self.block_domains.add(domain)
                    added += 1
        return added

    def reason(
        self, url: str, resource_type: str, page_url: str | None = None, *, main_document: bool = False
    ) -> str | None:
        """Why the request should be blocked, or ``None`` to let it through."""
        if main_document:
            return None
        if self.allow_patterns and any(rx.search(url) for rx in self.allow_patterns):
            return None
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            return None
        if self.allow_domains and _suffix_in(host, self.allow_domains):
            return None
        if resource_type in self.block_types:
            return f"type:{resource_type}"
        if self.block_domains and _suffix_in(host, self.block_domains):
            return "domain"
        if self.block_patterns and any(rx.search(url) for rx in self.block_patterns):
            return "pattern"
        if (self.list_domains or self.block_third_party) and page_url and host:
            page_host = (urlsplit(page_url).hostname or "").lower()
            third_party = bool(page_host) and registrable_domain(host) != registrable_domain(page_host)
            if third_party and self.list_domains and _suffix_in(host, self.list_domains):
                return "list"
            if third_party and self.block_third_party:
                return "third-party"
        return None

    def blocks(self, url: str, resource_type: str, page_url: str | None = None, *, main_document: bool = False) -> bool:
        reason = self.reason(url, resource_type, page_url, main_document=main_document)
        if reason is not None:
            self.stats[reason] += 1
        return reason is not None

    def __bool__(self) -> bool:
        return bool(
            self.block_types or self.list_domains or self.block_domains or self.block_third_party or self.block_patterns
        )

    def __repr__(self) -> str:
        return (
            f"ResourceFilter(types={sorted(self.block_types)}, lists={list(self.lists)}, "
            f"domains={len(self.block_domains)}, third_party={self.block_third_party})"
        )
