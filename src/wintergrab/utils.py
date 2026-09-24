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
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

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


def canonicalize_url(url: str, *, keep_fragment: bool = False) -> str:
    """Normalize a URL so trivially different spellings compare equal.

    Lower-cases scheme and host, drops default ports and the fragment, sorts
    query parameters and percent-encodes unsafe characters.
    """
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


def host_of(url: str) -> str:
    """Lower-case host name of a URL (``""`` if there is none)."""
    return (urlsplit(url).hostname or "").lower()


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
