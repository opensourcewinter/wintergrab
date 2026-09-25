"""Fetchers: HTTP (curl_cffi) and headless browser (Playwright), sync and async."""

from __future__ import annotations

from typing import Any

from .blocking import has_challenge_markers, looks_blocked
from .browser import AsyncBrowserFetcher, BrowserFetcher, CapturedResponse
from .cache import CacheMiss, HTTPCache
from .http import DEFAULT_RETRY_STATUSES, AsyncFetcher, Fetcher
from .resources import ResourceFilter
from .response import Headers, Response

__all__ = [
    "DEFAULT_RETRY_STATUSES",
    "AsyncBrowserFetcher",
    "AsyncFetcher",
    "BrowserFetcher",
    "CacheMiss",
    "CapturedResponse",
    "Fetcher",
    "HTTPCache",
    "Headers",
    "ResourceFilter",
    "Response",
    "aget",
    "apost",
    "arender",
    "get",
    "has_challenge_markers",
    "looks_blocked",
    "post",
    "render",
]

# Options that configure the client rather than a single request.
_CLIENT_OPTIONS = {
    "impersonate",
    "proxies",
    "retries",
    "backoff",
    "max_backoff",
    "retry_statuses",
    "follow_redirects",
    "max_redirects",
    "verify",
    "http_version",
    "referer",
    "raise_for_status",
    "adaptive_storage",
    "cache",
    "cache_mode",
    "cache_ttl",
    "network_policy",
}
_BROWSER_CLIENT_OPTIONS = {
    "headless",
    "stealth",
    "executable_path",
    "channel",
    "proxy",
    "proxies",
    "user_agent",
    "locale",
    "timezone_id",
    "viewport",
    "extra_headers",
    "cookies",
    "block_resources",
    "wait_until",
    "max_pages",
    "retries",
    "retry_statuses",
    "wait_for_challenge",
    "challenge_timeout",
    "user_data_dir",
    "launch_args",
    "adaptive_storage",
    "timeout",
    "cache",
    "cache_mode",
    "cache_ttl",
    "resource_filter",
    "network_policy",
}


def _split(kwargs: dict[str, Any], client_keys: set[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    client = {k: v for k, v in kwargs.items() if k in client_keys}
    per_request = {k: v for k, v in kwargs.items() if k not in client_keys}
    return client, per_request


def get(url: str, **kwargs: Any) -> Response:
    """Fetch one page over HTTP, looking like Chrome. The quickest way in::

        page = wintergrab.get("https://quotes.toscrape.com/")
        print(page.css(".quote .text::text").getall())

    Accepts every :class:`Fetcher` option (``impersonate``, ``retries``,
    ``proxies``...) and every request option (``params``, ``headers``,
    ``cookies``, ``proxy``, ``timeout``...).
    """
    client, per_request = _split(kwargs, _CLIENT_OPTIONS)
    with Fetcher(**client) as fetcher:
        return fetcher.get(url, **per_request)


def post(url: str, **kwargs: Any) -> Response:
    """POST with ``data=`` (form) or ``json=``. Same options as :func:`get`."""
    client, per_request = _split(kwargs, _CLIENT_OPTIONS)
    with Fetcher(**client) as fetcher:
        return fetcher.post(url, **per_request)


async def aget(url: str, **kwargs: Any) -> Response:
    """Async :func:`get`."""
    client, per_request = _split(kwargs, _CLIENT_OPTIONS)
    async with AsyncFetcher(**client) as fetcher:
        return await fetcher.get(url, **per_request)


async def apost(url: str, **kwargs: Any) -> Response:
    """Async :func:`post`."""
    client, per_request = _split(kwargs, _CLIENT_OPTIONS)
    async with AsyncFetcher(**client) as fetcher:
        return await fetcher.post(url, **per_request)


def render(url: str, **kwargs: Any) -> Response:
    """Load a page in a headless browser and return the rendered HTML::

        page = wintergrab.render("https://example.com", wait_for=".results")

    Accepts :class:`BrowserFetcher` options (``headless``, ``stealth``,
    ``proxy``, ``block_resources``...) and per-page options (``wait_for``,
    ``wait``, ``scroll``, ``page_action``, ``screenshot``).
    """
    client, per_request = _split(kwargs, _BROWSER_CLIENT_OPTIONS)
    with BrowserFetcher(**client) as browser:
        return browser.get(url, **per_request)


async def arender(url: str, **kwargs: Any) -> Response:
    """Async :func:`render`."""
    client, per_request = _split(kwargs, _BROWSER_CLIENT_OPTIONS)
    async with AsyncBrowserFetcher(**client) as browser:
        return await browser.get(url, **per_request)
