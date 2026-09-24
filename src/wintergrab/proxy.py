"""Proxy rotation with health tracking."""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

log = logging.getLogger("wintergrab.proxy")


def normalize_proxy(proxy: str) -> str:
    """Accept the common proxy spellings and return a URL.

    ``host:port`` -> ``http://host:port``
    ``host:port:user:pass`` -> ``http://user:pass@host:port``
    ``socks5://...``/``http://...`` URLs are returned unchanged.
    """
    proxy = proxy.strip()
    if "://" in proxy:
        return proxy
    parts = proxy.split(":")
    if len(parts) == 4:
        host, port, user, password = parts
        return f"http://{user}:{password}@{host}:{port}"
    return f"http://{proxy}"


def proxy_label(proxy: str | None) -> str:
    """Proxy URL with the password hidden, for logs."""
    if not proxy:
        return "direct"
    parts = urlsplit(proxy)
    if parts.password:
        return proxy.replace(f":{parts.password}@", ":***@", 1)
    return proxy


def proxy_for_playwright(proxy: str) -> dict[str, str]:
    """Convert a proxy URL to Playwright's ``{"server", "username", "password"}``."""
    parts = urlsplit(normalize_proxy(proxy))
    server = f"{parts.scheme}://{parts.hostname}"
    if parts.port:
        server += f":{parts.port}"
    out = {"server": server}
    if parts.username:
        out["username"] = unquote(parts.username)
    if parts.password:
        out["password"] = unquote(parts.password)
    return out


@dataclass
class ProxyState:
    url: str
    uses: int = 0
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    banned_until: float = 0.0

    @property
    def available(self) -> bool:
        return self.banned_until <= time.monotonic()


class ProxyRotator:
    """Hands out proxies in turn and benches the ones that keep failing.

    Args:
        proxies: Proxy URLs (``http://user:pass@host:port``, ``socks5://...``,
            or ``host:port``).
        strategy: ``"round_robin"`` (default), ``"random"`` or
            ``"least_used"``.
        max_failures: Consecutive failures before a proxy is benched.
        cooldown: Seconds a benched proxy sits out (doubles on repeat offences,
            up to 1 hour).

    Example::

        rotator = ProxyRotator(["http://p1:8000", "http://p2:8000"])
        page = wg.get(url, proxies=rotator)
    """

    def __init__(
        self,
        proxies: Iterable[str],
        *,
        strategy: str = "round_robin",
        max_failures: int = 3,
        cooldown: float = 120.0,
    ) -> None:
        urls = [normalize_proxy(p) for p in proxies if p and p.strip() and not p.strip().startswith("#")]
        if not urls:
            raise ValueError("ProxyRotator needs at least one proxy")
        if strategy not in ("round_robin", "random", "least_used"):
            raise ValueError("strategy must be 'round_robin', 'random' or 'least_used'")
        self._states = {url: ProxyState(url) for url in dict.fromkeys(urls)}
        self._order = list(self._states)
        self._index = 0
        self._lock = threading.Lock()
        self._bans: dict[str, int] = {}
        self.strategy = strategy
        self.max_failures = max_failures
        self.cooldown = cooldown

    @classmethod
    def from_file(cls, path: str | Path, **kwargs: object) -> ProxyRotator:
        """One proxy per line; blank lines and ``#`` comments are ignored."""
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        return cls(lines, **kwargs)  # type: ignore[arg-type]

    @classmethod
    def coerce(cls, value: ProxyRotator | Sequence[str] | str | None) -> ProxyRotator | None:
        """Turn a list/str/rotator/``None`` into a rotator (or ``None``)."""
        if value is None or isinstance(value, ProxyRotator):
            return value
        if isinstance(value, str):
            value = [value]
        return cls(list(value)) if value else None

    def __len__(self) -> int:
        return len(self._order)

    @property
    def proxies(self) -> list[str]:
        return list(self._order)

    def next(self) -> str:
        """The proxy to use for the next request."""
        with self._lock:
            available = [u for u in self._order if self._states[u].available]
            if not available:
                # Everyone is benched: use the one that comes back soonest.
                url = min(self._order, key=lambda u: self._states[u].banned_until)
                log.warning("all proxies are cooling down; using %s anyway", proxy_label(url))
            elif self.strategy == "random":
                url = random.choice(available)
            elif self.strategy == "least_used":
                url = min(available, key=lambda u: self._states[u].uses)
            else:
                for _ in range(len(self._order)):
                    candidate = self._order[self._index % len(self._order)]
                    self._index += 1
                    if self._states[candidate].available:
                        url = candidate
                        break
                else:  # pragma: no cover - guarded by `available`
                    url = available[0]
            self._states[url].uses += 1
            return url

    def report_success(self, proxy: str | None) -> None:
        if proxy is None:
            return
        with self._lock:
            state = self._states.get(proxy)
            if state:
                state.successes += 1
                state.consecutive_failures = 0
                self._bans.pop(proxy, None)

    def report_failure(self, proxy: str | None) -> None:
        if proxy is None:
            return
        with self._lock:
            state = self._states.get(proxy)
            if not state:
                return
            state.failures += 1
            state.consecutive_failures += 1
            if state.consecutive_failures >= self.max_failures:
                strikes = self._bans.get(proxy, 0)
                self._bans[proxy] = strikes + 1
                duration = min(self.cooldown * (2**strikes), 3600.0)
                state.banned_until = time.monotonic() + duration
                state.consecutive_failures = 0
                log.warning("proxy %s benched for %.0fs after repeated failures", proxy_label(proxy), duration)

    def stats(self) -> list[dict[str, object]]:
        """Per-proxy counters (passwords hidden)."""
        with self._lock:
            return [
                {
                    "proxy": proxy_label(s.url),
                    "uses": s.uses,
                    "successes": s.successes,
                    "failures": s.failures,
                    "available": s.available,
                }
                for s in self._states.values()
            ]

    def __repr__(self) -> str:
        return f"ProxyRotator({len(self)} proxies, strategy={self.strategy!r})"
