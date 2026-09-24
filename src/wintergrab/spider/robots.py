"""robots.txt support for spiders."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from ..errors import describe
from ..fetchers.response import Response

log = logging.getLogger("wintergrab.robots")


class RobotsPolicy:
    """Fetches, caches and applies each site's robots.txt.

    A missing robots.txt (4xx) allows everything. If it cannot be fetched
    (network error, 5xx) the site is treated as allowed and a warning is logged.
    """

    def __init__(self, fetch: Callable[[str], Awaitable[Response]], user_agent: str = "*") -> None:
        self._fetch = fetch
        self.user_agent = user_agent
        self._parsers: dict[str, RobotFileParser | None] = {}
        self._pending: dict[str, asyncio.Future[RobotFileParser | None]] = {}

    @staticmethod
    def origin(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}"

    async def _parser(self, url: str) -> RobotFileParser | None:
        origin = self.origin(url)
        if origin in self._parsers:
            return self._parsers[origin]
        pending = self._pending.get(origin)
        if pending is not None:
            return await asyncio.shield(pending)
        future: asyncio.Future[RobotFileParser | None] = asyncio.get_running_loop().create_future()
        self._pending[origin] = future
        parser: RobotFileParser | None = None
        try:
            response = await self._fetch(origin + "/robots.txt")
            if response.status == 200:
                parser = RobotFileParser(origin + "/robots.txt")
                parser.parse(response.text.splitlines())
                parser.modified()
            elif response.status >= 500:
                log.warning("robots.txt for %s returned %s; treating the site as allowed", origin, response.status)
        except Exception as exc:
            log.warning("could not fetch robots.txt for %s (%s); treating the site as allowed", origin, describe(exc))
        self._parsers[origin] = parser
        self._pending.pop(origin, None)
        future.set_result(parser)
        return parser

    async def allowed(self, url: str) -> bool:
        parser = await self._parser(url)
        return True if parser is None else parser.can_fetch(self.user_agent, url)

    async def crawl_delay(self, url: str) -> float | None:
        parser = await self._parser(url)
        if parser is None:
            return None
        delay = parser.crawl_delay(self.user_agent)
        return float(delay) if delay is not None else None
