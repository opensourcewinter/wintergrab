"""Credentials: logins that go to their own site only, and secrets that stay off command lines.

::

    export CLUB_TOKEN=...
    wintergrab crawl https://club.example/ -H 'Authorization: Bearer ${CLUB_TOKEN}' --cookie 'session=${CLUB_SESSION}'

    class Members(Spider):
        credentials = [Credentials("club.example", headers={"Authorization": f"Bearer {token}"})]

    AsyncFetcher(credentials=[Credentials("api.example", headers={"X-Api-Key": key})])

A :class:`Credentials` holds the headers and cookies of one site: its host and the hosts below it
(``club.example`` covers ``www.club.example`` and ``api.club.example``). The requests to the site carry
them, and no other request does: not a link a crawl follows to another site, not a redirect to one
(every hop is looked at), not what a browser page loads from elsewhere (a CDN, an analytics script). A
header or cookie that a request sets itself wins over the credentials'. In a browser, the cookies are the
browser's for the site's domain, and the headers go on the requests to the site, sent from Playwright: a
redirect is followed without them, so that they cannot go along to another site (Chromium would take them),
and a page fetched that way has an address Chromium does not know, so that it refuses the page's requests to
other sites' private addresses (Private Network Access).

On the command line, ``-H`` and ``--cookie`` are the credentials of the site of the URLs given (their
registrable domain: ``https://www.club.example/`` is ``club.example``). ``${NAME}`` in their values, and in
``--proxy``, is the environment variable ``NAME``, read by the process that uses it: the command line,
which anyone on the machine can read, holds the variable's name and never the secret. Quote it so that
the shell leaves it alone (``'... ${NAME}'``). A project's jobs take ``${NAME}`` in the same options, and
are given the variables of the credentials they name (:mod:`wintergrab.project`).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .errors import ConfigurationError

__all__ = ["VARIABLE", "Credentials", "expand", "for_url", "site_of"]

#: ``${NAME}``: the environment variable ``NAME``.
VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand(text: str, where: str) -> str:
    """``text`` with each ``${NAME}`` the environment variable ``NAME`` (an error, naming it, when it is not set)."""

    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        found = os.environ.get(name)
        if found is None:
            raise ConfigurationError(f"{where}: the environment variable {name} is not set")
        return found

    return VARIABLE.sub(value, text)


def _host(value: str) -> str:
    """The host of a site given as a host name (``club.example``) or a URL (``https://club.example/login``)."""
    text = value.strip().lower()
    try:
        host = urlsplit(text if "://" in text else f"//{text}").hostname or ""
    except ValueError:
        return ""
    return host.rstrip(".")


def site_of(url: str) -> str:
    """The site credentials given with ``url`` are for: its registrable domain (``https://www.club.example/`` is
    ``club.example``); an IP address or a one-label host (``localhost``) as it is."""
    from .fetchers.resources import registrable_domain

    return registrable_domain(_host(url))


@dataclass(frozen=True)
class Credentials:
    """Headers and cookies for the requests to one site, and to no other (see the module docs).

    Args:
        site: The site: a host (``club.example``), which covers the hosts below it too, or a URL of it.
        headers: Headers its requests carry (``{"Authorization": "Bearer ..."}``).
        cookies: Cookies its requests carry (``{"session": "..."}``).

    Its ``repr`` names the headers and cookies, never their values.
    """

    site: str
    headers: Mapping[str, str] = field(default_factory=dict)
    cookies: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        host = _host(str(self.site))
        if not host:
            raise ConfigurationError(f"credentials are for a site (a host like club.example), not {self.site!r}")
        object.__setattr__(self, "site", host)
        object.__setattr__(self, "headers", {str(k): str(v) for k, v in dict(self.headers).items()})
        object.__setattr__(self, "cookies", {str(k): str(v) for k, v in dict(self.cookies).items()})

    def covers(self, url: str) -> bool:
        """Whether a request to ``url`` (or to a host) is to the site: its host, or a host below it."""
        host = _host(url)
        return host == self.site or host.endswith("." + self.site)

    def __repr__(self) -> str:
        return f"Credentials({self.site!r}, headers={sorted(self.headers)}, cookies={sorted(self.cookies)})"


def coerce(value: Any) -> tuple[Credentials, ...]:
    """Credentials given as one, several, or none (``None``)."""
    if value is None:
        return ()
    items = [value] if isinstance(value, Credentials) else list(value)
    wrong = [item for item in items if not isinstance(item, Credentials)]
    if wrong:
        raise ConfigurationError(f"credentials are Credentials(site, headers=..., cookies=...), not {wrong[0]!r}")
    return tuple(items)


def for_url(credentials: Iterable[Credentials], url: str) -> dict[str, str]:
    """The headers of the credentials covering ``url`` (where two say the same, the more specific site's)."""
    headers: dict[str, str] = {}
    for item in sorted((c for c in credentials if c.covers(url)), key=lambda c: len(c.site)):
        headers = merge_headers(headers, item.headers)
    return headers


def merge_headers(base: Mapping[str, str], over: Mapping[str, str]) -> dict[str, str]:
    """``base`` with ``over``'s headers in the place of its own of the same names (whatever their case)."""
    names = {name.lower() for name in over}
    return {**{k: v for k, v in base.items() if k.lower() not in names}, **over}
