"""URL normalization, crawl rules and URL templates.

* :class:`URLNormalizer` rewrites URLs so that trivially different spellings of
  the same page become one URL: tracking parameters (``utm_*``, ``gclid``,
  ``fbclid``...) and session ids are dropped, dot segments resolved, percent
  escapes normalized, the query sorted and the fragment removed.
* :class:`URLRules` decides which discovered URLs a crawl should queue: allow
  and deny patterns, domains, file extensions and guards against crawler traps
  (endlessly repeating path segments, huge query strings).
* :func:`url_template` generalizes a URL into its route pattern
  (``/product/123`` -> ``/product/{int}``), which groups pages built from the
  same template for statistics, fetch-strategy history and crawl planning.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

from .errors import ConfigurationError

__all__ = [
    "IGNORED_EXTENSIONS",
    "TRACKING_PARAMS",
    "URLNormalizer",
    "URLRules",
    "normalize_url",
    "remove_dot_segments",
    "url_template",
]

#: Query parameters that only track where a click came from; they never change the page.
TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "gclid", "gclsrc", "dclid", "gbraid", "wbraid", "fbclid", "msclkid", "yclid", "igshid", "twclid",
        "ttclid", "li_fat_id", "mc_cid", "mc_eid", "_ga", "_gl", "_hsenc", "_hsmi", "__hsfp", "__hssc",
        "__hstc", "hsctatracking", "mkt_tok", "oly_anon_id", "oly_enc_id", "rb_clickid", "s_cid",
        "vero_conv", "vero_id", "wickedid", "epik", "srsltid", "_openstat", "cmpid", "icid",
    }
)  # fmt: skip
#: Prefixes of tracking parameter families (Google Analytics, Matomo/Piwik, HubSpot ads).
TRACKING_PREFIXES: tuple[str, ...] = ("utm_", "pk_", "mtm_", "hsa_")
#: Session-id parameters (query or ``;name=`` path parameters).
SESSION_PARAMS: frozenset[str] = frozenset({"jsessionid", "phpsessid", "aspsessionid", "sessionid", "sid"})

#: Extensions of files a crawl for web pages rarely wants (media, archives, binaries, office files).
IGNORED_EXTENSIONS: frozenset[str] = frozenset(
    {
        # images
        "mng", "pct", "bmp", "gif", "jpg", "jpeg", "png", "pst", "psp", "tif", "tiff", "ai", "drw", "dxf",
        "eps", "ps", "svg", "cdr", "ico", "webp", "avif", "heic",
        # audio / video
        "mp3", "wma", "ogg", "wav", "ra", "aac", "mid", "au", "aiff", "flac", "m4a", "3gp", "asf", "asx",
        "avi", "mov", "mp4", "mpg", "qt", "rm", "swf", "wmv", "m4v", "webm", "mkv",
        # office / documents
        "xls", "xlsx", "ppt", "pptx", "pps", "doc", "docx", "odt", "ods", "odg", "odp",
        # archives and binaries
        "css", "exe", "bin", "rss", "dmg", "iso", "apk", "zip", "rar", "gz", "tgz", "bz2", "7z", "tar", "jar",
        "msi", "deb", "rpm", "woff", "woff2", "ttf", "otf", "eot",
    }
)  # fmt: skip

_DEFAULT_PORTS = {"http": 80, "https": 443}
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PCT = re.compile(r"%([0-9A-Fa-f]{2})")
_INDEX_FILES = frozenset({"index.html", "index.htm", "index.php", "index.asp", "index.aspx", "default.asp",
                          "default.aspx", "default.htm", "default.html", "index.shtml", "index.jsp"})  # fmt: skip
_SESSION_PATH_PARAM = re.compile(r";(?:jsessionid|phpsessid|sid|sessionid)=[^/?#;]*", re.I)
# Characters left unescaped inside a query name or value ("&" and "=" never appear raw there).
_QUERY_SAFE = "!$'()*+,;:@/?"


def remove_dot_segments(path: str) -> str:
    """Resolve ``.`` and ``..`` segments (RFC 3986 section 5.2.4)."""
    if "." not in path:
        return path
    output: list[str] = []
    segments = path.split("/")
    for i, segment in enumerate(segments):
        last = i == len(segments) - 1
        if segment == ".":
            if last:
                output.append("")
        elif segment == "..":
            if len(output) > 1 or (output and output[0] != ""):
                output.pop()
            if last:
                output.append("")
        else:
            output.append(segment)
    result = "/".join(output)
    if path.startswith("/") and not result.startswith("/"):
        result = "/" + result
    return result


def _normalize_escapes(text: str, safe: str) -> str:
    """Upper-case percent escapes, decode escaped unreserved characters, escape unsafe characters."""

    def fix(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED else "%" + match.group(1).upper()

    if "%" in text:
        text = _PCT.sub(fix, text)
    return quote(text, safe=safe + "%")


def _is_tracking(name: str, extra: Collection[str]) -> bool:
    low = name.lower()
    return low in TRACKING_PARAMS or low.startswith(TRACKING_PREFIXES) or low in extra


class URLNormalizer:
    """Rewrites URLs to one canonical spelling. Call it: ``normalizer(url) -> url``.

    The defaults are safe for any site: they never change which page a URL
    points to. The options marked *site-dependent* are right for most sites but
    not all of them (some servers treat ``/Page`` and ``/page`` differently).

    Args:
        strip_tracking: Drop tracking parameters (see :data:`TRACKING_PARAMS`).
        strip_session_ids: Drop session ids (``;jsessionid=...``, ``PHPSESSID=...``).
        remove_fragment: Drop ``#fragment`` (``#!`` "hashbang" routes are kept when ``keep_hashbang``).
        keep_hashbang: Keep ``#!/route`` fragments, which old AJAX sites use to address pages.
        sort_query: Sort query parameters so their order doesn't matter.
        drop_params: Extra parameter names to drop (case-insensitive).
        keep_params: If given, drop every parameter *not* in this set.
        remove_empty_params: Drop parameters with an empty value (``?a=&b=1`` -> ``?b=1``).
        strip_www: *Site-dependent.* ``www.example.com`` -> ``example.com``.
        remove_trailing_slash: *Site-dependent.* ``/a/b/`` -> ``/a/b`` (the root keeps its slash).
        remove_index: *Site-dependent.* ``/a/index.html`` -> ``/a/``.
        lowercase_path: *Site-dependent.* Lower-case the path.
        force_https: *Site-dependent.* Rewrite ``http://`` to ``https://``.
    """

    def __init__(
        self,
        *,
        strip_tracking: bool = True,
        strip_session_ids: bool = True,
        remove_fragment: bool = True,
        keep_hashbang: bool = True,
        sort_query: bool = True,
        drop_params: Iterable[str] = (),
        keep_params: Iterable[str] | None = None,
        remove_empty_params: bool = False,
        strip_www: bool = False,
        remove_trailing_slash: bool = False,
        remove_index: bool = False,
        lowercase_path: bool = False,
        force_https: bool = False,
    ) -> None:
        self.strip_tracking = strip_tracking
        self.strip_session_ids = strip_session_ids
        self.remove_fragment = remove_fragment
        self.keep_hashbang = keep_hashbang
        self.sort_query = sort_query
        self.drop_params = frozenset(p.lower() for p in drop_params)
        self.keep_params = frozenset(p.lower() for p in keep_params) if keep_params is not None else None
        self.remove_empty_params = remove_empty_params
        self.strip_www = strip_www
        self.remove_trailing_slash = remove_trailing_slash
        self.remove_index = remove_index
        self.lowercase_path = lowercase_path
        self.force_https = force_https
        self._cache: Callable[[str], str] = lru_cache(maxsize=16384)(self._normalize)

    @classmethod
    def coerce(cls, value: Any) -> Callable[[str], str] | None:
        """``None``/``False`` -> no normalizer, ``True`` -> defaults, a dict -> options, a callable -> itself."""
        if value is None or value is False:
            return None
        if value is True:
            return cls()
        if isinstance(value, dict):
            try:
                return cls(**value)
            except TypeError as exc:
                raise ConfigurationError(str(exc), key="url_normalizer") from exc
        if callable(value):
            return value  # type: ignore[no-any-return]
        raise ConfigurationError(f"expected True, a dict or a callable, got {value!r}", key="url_normalizer")

    def __call__(self, url: str) -> str:
        return self._cache(url)

    def _normalize(self, url: str) -> str:
        try:
            parts = urlsplit(url.strip())
        except ValueError:
            return url
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            return url
        if self.force_https and scheme == "http":
            scheme = "https"
        try:
            host = (parts.hostname or "").rstrip(".")
            port = parts.port
        except ValueError:
            return url
        if not host:
            return url
        try:
            host = host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            host = host.lower()
        if self.strip_www and host.startswith("www.") and host.count(".") >= 2:
            host = host[4:]
        netloc = host if ":" not in host else f"[{host}]"
        if port and port != _DEFAULT_PORTS.get(scheme):
            netloc += f":{port}"
        if parts.username:
            userinfo = parts.username + (f":{parts.password}" if parts.password else "")
            netloc = f"{userinfo}@{netloc}"

        path = parts.path or "/"
        if self.strip_session_ids and ";" in path:
            path = _SESSION_PATH_PARAM.sub("", path)
        path = remove_dot_segments(_normalize_escapes(path, safe="/:@!$&'()*+,;="))
        if self.lowercase_path:
            path = path.lower()
        if self.remove_index:
            head, _, last = path.rpartition("/")
            if last.lower() in _INDEX_FILES:
                path = head + "/"
        if self.remove_trailing_slash and len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/") or "/"

        query = parts.query
        if query:
            # Work on the raw components: only escapes are normalized, so "+" vs "%20" and
            # bare "?flag" keys are never rewritten (servers may treat them differently).
            kept: list[tuple[str, str, str | None]] = []
            for part in query.split("&"):
                if not part:
                    continue
                raw_name, eq, raw_value = part.partition("=")
                name = unquote(raw_name.replace("+", " "))
                if not self._keep_param(name, unquote(raw_value.replace("+", " "))):
                    continue
                value = _normalize_escapes(raw_value, safe=_QUERY_SAFE) if eq else None
                kept.append((name, _normalize_escapes(raw_name, safe=_QUERY_SAFE), value))
            if self.sort_query:
                kept.sort(key=lambda p: (p[0], p[2] is not None, p[2] or ""))
            query = "&".join(n if v is None else f"{n}={v}" for _, n, v in kept)

        fragment = ""
        if parts.fragment and (not self.remove_fragment or (self.keep_hashbang and parts.fragment.startswith("!"))):
            fragment = parts.fragment
        return urlunsplit((scheme, netloc, path, query, fragment))

    def _keep_param(self, name: str, value: str) -> bool:
        low = name.lower()
        if self.keep_params is not None and low not in self.keep_params:
            return False
        if self.strip_tracking and _is_tracking(low, ()):
            return False
        if self.strip_session_ids and low in SESSION_PARAMS:
            return False
        if low in self.drop_params:
            return False
        return not (self.remove_empty_params and value == "")

    def __getstate__(self) -> dict[str, Any]:
        state = self.__dict__.copy()
        state.pop("_cache", None)  # an lru_cache of a bound method cannot be pickled
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        self._cache = lru_cache(maxsize=16384)(self._normalize)

    def __repr__(self) -> str:
        flags = [k for k in ("strip_www", "remove_trailing_slash", "remove_index", "lowercase_path", "force_https")
                 if getattr(self, k)]  # fmt: skip
        return f"URLNormalizer({', '.join(flags) or 'defaults'})"


_DEFAULT_NORMALIZER = URLNormalizer()


def normalize_url(url: str, **options: Any) -> str:
    """Normalize one URL (see :class:`URLNormalizer` for the options)."""
    return (URLNormalizer(**options) if options else _DEFAULT_NORMALIZER)(url)


def _extension(path: str) -> str:
    last = path.rsplit("/", 1)[-1]
    if "." not in last:
        return ""
    return unquote(last.rsplit(".", 1)[-1]).lower()


def _compile(patterns: str | Iterable[str], key: str) -> list[re.Pattern[str]]:
    if isinstance(patterns, str):
        patterns = [patterns]
    out = []
    for pattern in patterns:
        try:
            out.append(re.compile(pattern))
        except re.error as exc:
            raise ConfigurationError(f"invalid regular expression {pattern!r}: {exc}", key=key) from exc
    return out


def _domain_in(host: str, domains: Collection[str]) -> bool:
    for domain in domains:
        domain = domain.lower().lstrip(".")
        if host == domain or host.endswith("." + domain):
            return True
    return False


@dataclass
class URLRules:
    """Which URLs a crawl may queue.

    ``check(url)`` returns ``None`` when the URL passes, or the reason it was
    rejected (``"deny"``, ``"extension"``, ``"repeating-path"``...). Spiders use
    it for every link they discover (``Spider.url_rules``); start URLs are not
    filtered.

    Args:
        allow: Regexes; if any are given, a URL must match at least one.
        deny: Regexes a URL must not match.
        allowed_domains: If given, the URL's host must be one of these (subdomains included).
        denied_domains: Hosts (and subdomains) never queued.
        schemes: Allowed URL schemes.
        deny_extensions: File extensions never queued (default :data:`IGNORED_EXTENSIONS`;
            pass ``()`` to allow every extension).
        max_url_length: Longer URLs are rejected (they are usually generated junk).
        max_query_params: Reject URLs with more query parameters (faceted-search explosions).
        max_path_depth: Reject URLs with more path segments.
        max_segment_repeats: Reject URLs in which one path segment occurs more than this
            many times (``/a/b/a/b/a/b/...``: a relative-link crawler trap).
    """

    allow: Sequence[str] = ()
    deny: Sequence[str] = ()
    allowed_domains: Sequence[str] = ()
    denied_domains: Sequence[str] = ()
    schemes: Sequence[str] = ("http", "https")
    deny_extensions: Collection[str] = IGNORED_EXTENSIONS
    max_url_length: int | None = 2048
    max_query_params: int | None = 30
    max_path_depth: int | None = 32
    max_segment_repeats: int | None = 3
    _allow: list[re.Pattern[str]] = field(default_factory=list, init=False, repr=False)
    _deny: list[re.Pattern[str]] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        self._allow = _compile(self.allow, "url_rules.allow")
        self._deny = _compile(self.deny, "url_rules.deny")
        self.deny_extensions = frozenset(e.lower().lstrip(".") for e in self.deny_extensions)
        self.schemes = tuple(s.lower() for s in self.schemes)

    @classmethod
    def coerce(cls, value: Any) -> URLRules | None:
        if value is None or value is False:
            return None
        if isinstance(value, URLRules):
            return value
        if value is True:
            return cls()
        if isinstance(value, dict):
            try:
                return cls(**value)
            except TypeError as exc:
                raise ConfigurationError(str(exc), key="url_rules") from exc
        raise ConfigurationError(f"expected True, a dict or URLRules, got {value!r}", key="url_rules")

    def check(self, url: str) -> str | None:
        """``None`` if ``url`` may be queued, else a short reason."""
        if self.max_url_length is not None and len(url) > self.max_url_length:
            return "url-too-long"
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
        except ValueError:
            return "invalid-url"
        if parts.scheme.lower() not in self.schemes:
            return "scheme"
        if self.allowed_domains and not _domain_in(host, self.allowed_domains):
            return "domain"
        if self.denied_domains and _domain_in(host, self.denied_domains):
            return "denied-domain"
        path = parts.path
        if self.deny_extensions:
            ext = _extension(path)
            if ext and ext in self.deny_extensions:
                return "extension"
        segments = [s for s in path.split("/") if s]
        if self.max_path_depth is not None and len(segments) > self.max_path_depth:
            return "path-too-deep"
        if self.max_segment_repeats is not None and len(segments) > self.max_segment_repeats:
            counts: dict[str, int] = {}
            for segment in segments:
                counts[segment] = counts.get(segment, 0) + 1
                if counts[segment] > self.max_segment_repeats:
                    return "repeating-path"
        if (
            self.max_query_params is not None
            and parts.query.count("&") >= self.max_query_params  # cheap pre-check
            and len(parse_qsl(parts.query, keep_blank_values=True)) > self.max_query_params
        ):
            return "too-many-params"
        if self._allow and not any(rx.search(url) for rx in self._allow):
            return "not-allowed"
        if self._deny and any(rx.search(url) for rx in self._deny):
            return "deny"
        return None

    def allows(self, url: str) -> bool:
        return self.check(url) is None


# --------------------------------------------------------------------------- #
# URL templates
# --------------------------------------------------------------------------- #
_UUID = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}", re.I)
_HEX = re.compile(r"(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8,}", re.I)
_DATE = re.compile(r"(?:19|20)\d\d(?:[-_/.]?(?:0[1-9]|1[0-2])(?:[-_/.]?(?:0[1-9]|[12]\d|3[01]))?)?")
_INT = re.compile(r"\d+")
_WORD = re.compile(r"[a-z]+(?:[-_][a-z]+)?", re.I)
_TOKEN_SPLIT = re.compile(r"[-_.~+]+")


def _segment_kind(segment: str) -> str:
    """``segment`` itself if it looks like a fixed route word, else a placeholder."""
    text = unquote(segment)
    if _INT.fullmatch(text):
        return "{int}"
    if _UUID.fullmatch(text):
        return "{uuid}"
    if _DATE.fullmatch(text) and len(text) >= 6:
        return "{date}"
    if _HEX.fullmatch(text):
        return "{hex}"
    stem, dot, ext = text.rpartition(".")
    if dot and stem and ext.isalpha() and len(ext) <= 5:
        inner = _segment_kind(stem)
        return f"{inner}.{ext.lower()}" if inner.startswith("{") else text.lower()
    if _WORD.fullmatch(text) and len(text) <= 24:
        return text.lower()  # "products", "blog", "new-arrivals": a route
    tokens = [t for t in _TOKEN_SPLIT.split(text) if t]
    if len(tokens) >= 3 or any(ch.isdigit() for ch in text) or len(text) > 24:
        return "{slug}"  # "apple-iphone-15-pro", "p12345"
    return text.lower()


@lru_cache(maxsize=65536)
def url_template(url: str, *, include_host: bool = True, include_query: bool = True) -> str:
    """The route pattern of a URL.

    ``https://shop.example/product/123?page=2&utm_source=x`` ->
    ``shop.example/product/{int}?page``. Numbers, UUIDs, hex ids, dates and
    slugs (multi-word or digit-bearing path segments) become placeholders;
    short words stay literal. Query parameter *names* are kept (sorted, tracking
    parameters dropped); their values are not.
    """
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return url
    segments = [s for s in parts.path.split("/") if s]
    path = "/" + "/".join(_segment_kind(s) for s in segments)
    if parts.path.endswith("/") and segments:
        path += "/"
    out = (host + path) if include_host else path
    if include_query and parts.query:
        names = sorted(
            {k.lower() for k, _ in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k, ())}
        )
        if names:
            out += "?" + "&".join(names)
    return out
