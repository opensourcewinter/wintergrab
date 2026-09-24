"""Exception types raised by wintergrab."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .fetchers.response import Response


class WintergrabError(Exception):
    """Base class for every error raised by wintergrab."""


class SelectorSyntaxError(WintergrabError, ValueError):
    """A CSS selector or XPath expression could not be parsed."""


class FetchError(WintergrabError):
    """A page could not be fetched (network error, timeout, proxy failure...).

    Attributes:
        url: The URL that failed.
        cause: The underlying exception, if any.
        proxy: The proxy that was in use, if any.
        is_proxy_error: ``True`` when the failure looks like the proxy's fault.
        is_timeout: ``True`` when the request timed out.
        retryable: ``False`` for errors that retrying cannot fix (bad URL,
            invalid TLS certificate...).
    """

    def __init__(
        self,
        url: str,
        message: str,
        *,
        cause: BaseException | None = None,
        proxy: str | None = None,
        is_proxy_error: bool = False,
        is_timeout: bool = False,
        retryable: bool = True,
    ) -> None:
        super().__init__(f"{message} ({url})")
        self.url = url
        self.cause = cause
        self.proxy = proxy
        self.is_proxy_error = is_proxy_error
        self.is_timeout = is_timeout
        self.retryable = retryable


class HTTPStatusError(WintergrabError):
    """Raised by :meth:`Response.raise_for_status` for 4xx/5xx responses."""

    def __init__(self, response: Response, detail: str | None = None) -> None:
        status = f"{detail} (HTTP {response.status})" if detail else f"HTTP {response.status}"
        super().__init__(f"{status} for {response.url}")
        self.response = response
        self.status = response.status
        self.detail = detail


class BrowserNotAvailable(WintergrabError):
    """Playwright (or a browser binary for it) is not installed."""


class CheckpointError(WintergrabError):
    """A crawl could not be checkpointed or resumed."""


def describe(exc: BaseException | Any) -> str:
    """Short one-line description of an exception, for logs and stats."""
    name = type(exc).__name__
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    # curl appends a long "See https://curl.se/libcurl/..." hint; drop it.
    text = text.split(" See https://curl.se/")[0].removeprefix("Failed to perform, ")
    return f"{name}: {text}" if text else name
