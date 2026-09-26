"""Keeping credentials out of what wintergrab writes down: run records, events, logs, dashboards.

::

    >>> redact_url("http://user:hunter2@proxy.example:8080")
    'http://***@proxy.example:8080'
    >>> redact({"Authorization": "Bearer abc", "Accept": "text/html"})
    {'Authorization': '***', 'Accept': 'text/html'}
    >>> redact_argv(["crawl", "https://shop.example/", "--proxy", "http://u:p@proxy:8080", "-s", "api_token=abc"])
    ['crawl', 'https://shop.example/', '--proxy', 'http://***@proxy:8080', '-s', 'api_token=***']

What counts as a credential: the user information of a URL (``user:password@``), and the value of
anything named like one (``Authorization``, ``Cookie``, ``password``, ``secret``, ``token``, ``api_key``,
``credentials``...), in mappings and in ``NAME=VALUE`` or ``Name: value`` arguments. Query
parameters stay where a URL is kept (they are part of which page it is); in log lines,
:func:`redact_query` masks those named like credentials (``?key=***``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from itertools import pairwise
from typing import Any
from urllib.parse import urlsplit, urlunsplit

__all__ = ["REDACTED", "is_sensitive", "redact", "redact_argv", "redact_query", "redact_url"]

#: What a credential is replaced with.
REDACTED = "***"
_WORDS = frozenset(
    {"auth", "authorization", "cookie", "cookies", "password", "passwd", "passphrase", "pwd", "secret", "secrets",
     "token", "tokens", "credential", "credentials", "signature", "apikey", "sessionid"}
)  # fmt: skip
_PAIRS = frozenset({("api", "key"), ("access", "key"), ("private", "key"), ("secret", "key"), ("session", "id")})
_USERINFO = re.compile(r"(?<![\w.+-])([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s?#]+@")


def is_sensitive(name: Any) -> bool:
    """Whether a setting, header or field named ``name`` holds a credential (``X-Api-Key``,
    ``accessToken`` and ``session_id`` do; ``tokenizer`` does not)."""
    if not isinstance(name, str):
        return False
    words = re.findall(r"[a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower())
    return any(word in _WORDS for word in words) or any(pair in _PAIRS for pair in pairwise(words))


def redact_url(url: str) -> str:
    """``url`` without its user information (``http://***@proxy:8080``)."""
    if "@" not in url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return _USERINFO.sub(rf"\1{REDACTED}@", url)
    if parts.username is None and parts.password is None:
        return url
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit(parts._replace(netloc=f"{REDACTED}@{host}"))


#: Query parameters that hold a key, besides those named like a credential (``key=`` is Google's API key).
_QUERY_KEYS = frozenset({"key", "sig", "hmac", "jwt"})
_QUERY_PAIR = re.compile(r"([?&])([^=&#\s]+)=([^&#\s]*)")


def redact_query(text: str) -> str:
    """A URL (or a message holding URLs) for a log line: without user information, and with the values of
    query parameters named like credentials (``api_key``, ``access_token``, ``key``, ``sig``...) as ``***``.
    The URL itself, for requests, keeps them: which page a URL is may depend on them."""

    def mask(match: re.Match[str]) -> str:
        sep, name, value = match.groups()
        sensitive = is_sensitive(name) or name.lower() in _QUERY_KEYS
        return f"{sep}{name}={REDACTED}" if sensitive and value else match.group(0)

    without_users = _USERINFO.sub(rf"\1{REDACTED}@", text) if "@" in text else text
    return _QUERY_PAIR.sub(mask, without_users)


def redact(value: Any, name: Any = None) -> Any:
    """``value`` (a setting, a header mapping, JSON data...) with its credentials replaced, recursively.
    ``name``: what the value is called (``"password"`` makes the whole value one)."""
    if name is not None and is_sensitive(name) and value not in (None, "", {}, []) and not isinstance(value, bool):
        return REDACTED  # (a yes or no is no credential: share_browser_cookies = True)
    if isinstance(value, str):
        return _USERINFO.sub(rf"\1{REDACTED}@", value) if "@" in value else value
    if isinstance(value, Mapping):
        return {key: redact(item, key) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


_HEADER_FLAGS = frozenset({"-H", "--header"})
_PAIR_FLAGS = frozenset({"-s", "--set", "--cookie", "-b"})


def redact_argv(argv: Sequence[str]) -> list[str]:
    """A command line with its credentials replaced: URL user information, the values of headers
    and ``NAME=VALUE`` settings named like credentials (``-H "Authorization: ..."``, ``-s token=...``),
    and credentials inside JSON values (``-s 'default_headers={"Cookie": "..."}'``)."""
    out: list[str] = []
    previous = ""
    for arg in argv:
        flag, eq, inline = arg.partition("=") if arg.startswith("--") else (arg, "", "")
        if eq and flag in _HEADER_FLAGS | _PAIR_FLAGS:
            out.append(f"{flag}={_redact_value(flag, inline)}")  # --set=name=value
        elif previous in _HEADER_FLAGS | _PAIR_FLAGS:
            out.append(_redact_value(previous, arg))
        else:
            out.append(redact(arg))
        previous = arg
    return out


def _redact_value(flag: str, value: str) -> str:
    if flag in _HEADER_FLAGS:
        name, colon, rest = value.partition(":")
        return f"{name}: {REDACTED}" if colon and is_sensitive(name) else redact(value)
    name, eq, rest = value.partition("=")
    if not eq:
        return str(redact(value))
    if is_sensitive(name) or flag in ("--cookie", "-b"):
        return f"{name}={REDACTED}"
    try:
        data = json.loads(rest)
    except ValueError:
        return f"{name}={redact(rest)}"
    cleaned = redact(data)
    return f"{name}={rest}" if cleaned == data else f"{name}={json.dumps(cleaned, ensure_ascii=False)}"
