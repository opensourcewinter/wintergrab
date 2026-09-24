"""Named fetch sessions a spider can route requests through."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Iterator
from typing import Any

log = logging.getLogger("wintergrab.spider")


class SessionManager:
    """A registry of async fetchers, each with its own cookies and settings.

    Typical setup inside :meth:`Spider.configure_sessions`::

        def configure_sessions(self, sessions):
            sessions.add("fast", AsyncFetcher(impersonate="chrome"), default=True)
            sessions.add("browser", AsyncBrowserFetcher(headless=True))

    Then route individual requests with ``Request(url, session="browser")``
    or ``response.follow(link, session="browser")``. Sessions are started
    lazily, so an unused browser session costs nothing.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Any] = {}
        self.default_name: str | None = None

    def add(self, name: str, fetcher: Any, *, default: bool = False) -> Any:
        """Register an :class:`~wintergrab.AsyncFetcher` or :class:`~wintergrab.AsyncBrowserFetcher`."""
        request = getattr(fetcher, "request", None)
        if request is None or not inspect.iscoroutinefunction(request):
            raise TypeError(
                f"Session {name!r} must be an async fetcher (AsyncFetcher / AsyncBrowserFetcher), got {fetcher!r}"
            )
        if name in self._sessions and self._sessions[name] is not fetcher:
            log.debug("replacing session %r", name)
        self._sessions[name] = fetcher
        if default or self.default_name is None:
            self.default_name = name
        return fetcher

    def get(self, name: str | None = None) -> Any:
        key = name or self.default_name
        if key is None:
            raise LookupError("No sessions configured")
        try:
            return self._sessions[key]
        except KeyError:
            raise LookupError(f"Unknown session {key!r}; configured: {', '.join(self._sessions)}") from None

    def __contains__(self, name: object) -> bool:
        return name in self._sessions

    def __iter__(self) -> Iterator[str]:
        return iter(self._sessions)

    def __len__(self) -> int:
        return len(self._sessions)

    async def close_all(self) -> None:
        for name, fetcher in list(self._sessions.items()):
            closer = getattr(fetcher, "aclose", None) or getattr(fetcher, "close", None)
            if closer is None:
                continue
            try:
                result = closer()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # pragma: no cover - best effort
                log.debug("error closing session %r: %s", name, exc)
