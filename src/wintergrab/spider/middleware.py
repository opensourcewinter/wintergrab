"""Extension points around downloading (middleware) and around items (pipelines).

**Downloader middleware** sees every request before it is fetched and every
response or error after. A middleware is any object with one or more of::

    process_request(request, spider)            -> None | Response | Request
    process_response(request, response, spider) -> Response | Request
    process_exception(request, error, spider)   -> None | Response | Request

Each may be ``async``. ``process_request`` returning ``None`` lets the request
continue; a :class:`~wintergrab.Response` skips the download (a mock, a
local cache); a new :class:`~wintergrab.Request` replaces the original. Raise
:class:`IgnoreRequest` to drop it silently. ``process_response`` may change the
response, or return a request to fetch instead (e.g. after re-logging in).
``process_exception`` may recover with a response or a replacement request;
``None`` keeps the default error handling (retries, errbacks).

Requests pass through middlewares in list order; responses and exceptions in
reverse order, so the first middleware wraps all the others.

**Item pipelines** process every item after ``Spider.process_item``::

    open_spider(spider)            # optional, once before the crawl (may be async)
    process_item(item, spider)     # return the item (possibly changed); None or DropItem drops it
    close_spider(spider)           # optional, once after the crawl

The data layer (validation, normalization, de-duplication...) plugs in here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .spider import Spider

__all__ = ["DropItem", "IgnoreRequest", "ItemPipeline"]


class IgnoreRequest(Exception):
    """Raised by a middleware's ``process_request`` to drop a request without an error."""


class DropItem(Exception):
    """Raised by a pipeline to drop an item; the message says why (counted in the stats)."""


class ItemPipeline:
    """Optional base class for pipelines (every method is optional)."""

    def open_spider(self, spider: Spider) -> Any:
        """Called once before the crawl starts."""

    def process_item(self, item: Any, spider: Spider) -> Any:
        """Return the item (possibly changed), or ``None`` / raise :class:`DropItem` to drop it."""
        return item

    def close_spider(self, spider: Spider) -> Any:
        """Called once after the crawl ends."""
