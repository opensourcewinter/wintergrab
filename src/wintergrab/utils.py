"""Small helpers shared across the package."""

from __future__ import annotations

import inspect
import logging
import os
import re
import sys
import time
from collections.abc import Iterable, Mapping
from email.utils import parsedate_to_datetime
from functools import lru_cache
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def ensure_scheme(url: str) -> str:
    """Add ``https://`` to URLs typed without a scheme (``example.com/page``)."""
    url = url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        return "https://" + url.lstrip("/")
    return url


def add_params(url: str, params: Mapping[str, Any] | list[tuple[str, Any]] | None) -> str:
    """Append query parameters to a URL."""
    if not params:
        return url
    items = list(params.items()) if isinstance(params, Mapping) else list(params)
    flat: list[tuple[str, str]] = []
    for key, value in items:
        if isinstance(value, (list, tuple)):
            flat.extend((key, str(v)) for v in value)
        elif value is not None:
            flat.append((key, str(value)))
    parts = urlsplit(url)
    query = parts.query + ("&" if parts.query and flat else "") + urlencode(flat)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


# Already-canonical URLs (lower-case host, no default port, query or fragment, safe
# path) are by far the most common; recognising them is much cheaper than rebuilding them.
_CANONICAL = re.compile(r"(https?)://[a-z0-9.-]+(?::([1-9][0-9]{0,4}))?(?:/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*)?")
_DEFAULT_PORT_TEXT = {"http": "80", "https": "443"}


def canonicalize_url(url: str, *, keep_fragment: bool = False) -> str:
    """Normalize a URL so trivially different spellings compare equal.

    Lower-cases scheme and host, drops default ports and the fragment, sorts
    query parameters and percent-encodes unsafe characters.
    """
    match = _CANONICAL.fullmatch(url)
    if match is not None:
        port = match.group(2)
        if port is None or (port != _DEFAULT_PORT_TEXT[match.group(1)] and int(port) <= 65535):
            return url if url.count("/") > 2 else url + "/"
    return _canonicalize(url, keep_fragment)


@lru_cache(maxsize=8192)
def _canonicalize(url: str, keep_fragment: bool = False) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    netloc = host
    if parts.port and parts.port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"
    if parts.username:
        auth = parts.username + (f":{parts.password}" if parts.password else "")
        netloc = f"{auth}@{netloc}"
    path = quote(parts.path or "/", safe="%/:@!$&'()*+,;=-._~")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    fragment = parts.fragment if keep_fragment else ""
    return urlunsplit((scheme, netloc, path, query, fragment))


# http(s) URLs with a plain host (no user info, IPv6 literal or stripped characters).
_PLAIN_HOST = re.compile(r"https?://([A-Za-z0-9.-]+)(?::[0-9]*)?(?=[/?#]|$)")


def host_of(url: str) -> str:
    """Lower-case host name of a URL (``""`` if there is none)."""
    match = _PLAIN_HOST.match(url)
    if match is not None:
        return match.group(1).lower()
    return (urlsplit(url).hostname or "").lower()


# Links that urljoin() returns unchanged (absolute http(s)) or simply appends to the
# base's origin (root-relative). Anything urljoin() would rewrite is left to it: dot
# segments, characters urlsplit() strips, and empty params, queries or fragments.
_PLAIN_HREF = re.compile(r"(?:https?://[^/?#\t\n\r\\;]|/(?!/))[^\t\n\r\\;]*(?<![?#])")


@lru_cache(maxsize=4096)
def _origin(base: str) -> str | None:
    parts = urlsplit(base)
    if parts.scheme in ("http", "https") and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return None


def fast_urljoin(base: str, href: str) -> str:
    """``urllib.parse.urljoin`` with fast paths for the two most common link shapes.

    Absolute http(s) links come back unchanged and root-relative links without
    dot segments are appended to the base's origin - exactly what ``urljoin``
    returns for them, at a fraction of the cost. Everything else goes to ``urljoin``.
    """
    if "/." not in href and "?#" not in href and _PLAIN_HREF.fullmatch(href) is not None:
        if href[0] == "h":
            return href
        origin = _origin(base)
        if origin is not None:
            return origin + href
    return urljoin(base, href)


def domain_matches(host: str, domains: Iterable[str]) -> bool:
    """``True`` if ``host`` equals or is a subdomain of one of ``domains``."""
    host = host.lower()
    for domain in domains:
        domain = domain.lower().lstrip(".")
        if host == domain or host.endswith("." + domain):
            return True
    return False


def parse_retry_after(value: str | None, *, cap: float = 3600.0) -> float | None:
    """Seconds to wait from a ``Retry-After`` header (seconds or HTTP date)."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        seconds = when.timestamp() - time.time()
    return max(0.0, min(seconds, cap))


def resolve_verify(verify: bool | str) -> bool | str:
    """Honour the usual CA-bundle environment variables when ``verify=True``."""
    if verify is True:
        for name in ("WINTERGRAB_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE"):
            path = os.environ.get(name)
            if path and os.path.isfile(path):
                return path
    return verify


async def maybe_await(value: Any) -> Any:
    """Await ``value`` if it is awaitable, otherwise return it unchanged."""
    if inspect.isawaitable(value):
        return await value
    return value


def configure_logging(level: int | str = "INFO", *, fmt: str | None = None) -> None:
    """Send wintergrab's log messages to stderr (idempotent)."""
    logger = logging.getLogger("wintergrab")
    if isinstance(level, str):
        level = logging.getLevelName(level.upper())
    logger.setLevel(level)
    if not any(getattr(h, "_wintergrab", False) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(fmt or "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
        handler._wintergrab = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
        logger.propagate = False


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
