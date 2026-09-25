"""Exception types raised by wintergrab.

Every error derives from :class:`WintergrabError` and carries:

* ``category`` - a stable, machine-readable class of failure (``"network"``,
  ``"timeout"``, ``"policy"``, ``"http"``, ``"validation"``...). Failure reports,
  events and stats group errors by it.
* ``context`` - a dict of structured details (URL, domain, field, selector,
  config key...) for logs, diagnostics and error reports.

The hierarchy::

    WintergrabError
    ├── ConfigurationError        (also a ValueError)
    ├── SchemaError               (also a ValueError)
    ├── ParserError
    │   └── SelectorSyntaxError   (also a ValueError)
    ├── FetchError                a page could not be fetched
    │   ├── NetworkError          connection, DNS, TLS problems
    │   │   └── ProxyError
    │   ├── FetchTimeout
    │   ├── PolicyError           refused by a crawl policy (never retried)
    │   │   ├── RobotsPolicyError
    │   │   └── NetworkPolicyError (SSRF protection)
    │   ├── BrowserFetchError     also a BrowserError
    │   └── CacheMiss             (offline cache mode)
    ├── HTTPStatusError (alias HTTPError)
    ├── BrowserError
    │   └── BrowserNotAvailable
    ├── ExtractionError
    ├── ValidationError           (also a ValueError)
    ├── StorageError
    │   ├── CheckpointError
    │   └── ExportError
    └── BudgetExceeded
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .fetchers.response import Response


def _rebuild(cls: type[BaseException], args: tuple[Any, ...], state: dict[str, Any]) -> BaseException:
    exc = cls.__new__(cls)
    exc.args = args
    exc.__dict__.update(state)
    return exc


class WintergrabError(Exception):
    """Base class for every error raised by wintergrab.

    Attributes:
        category: Machine-readable class of failure (see the module docs).
        context: Structured details about where the error happened.
    """

    category: str = "error"

    def __init__(self, *args: Any, context: Mapping[str, Any] | None = None) -> None:
        super().__init__(*args)
        self.context: dict[str, Any] = dict(context or {})

    def with_context(self, **context: Any) -> WintergrabError:
        """Add details (``None`` values are skipped) and return the error, for ``raise err.with_context(...)``."""
        self.context.update({k: v for k, v in context.items() if v is not None})
        return self

    def __reduce__(self) -> tuple[Any, ...]:
        # Subclasses take keyword-only arguments, so the default "cls(*args)" cannot rebuild them.
        return (_rebuild, (type(self), self.args, self.__dict__.copy()))


class ConfigurationError(WintergrabError, ValueError):
    """Invalid settings, project files or command line options.

    Attributes:
        key: Dotted path of the offending setting (e.g. ``"crawl.concurrency"``), if known.
    """

    category = "configuration"

    def __init__(self, message: str, *, key: str | None = None, context: Mapping[str, Any] | None = None) -> None:
        text = f"{key}: {message}" if key else message
        super().__init__(text, context=context)
        self.key = key
        if key:
            self.context.setdefault("key", key)


class SchemaError(WintergrabError, ValueError):
    """A data schema is malformed (unknown type, bad constraint...)."""

    category = "schema"


class ParserError(WintergrabError):
    """A document or expression could not be parsed."""

    category = "parser"


class SelectorSyntaxError(ParserError, ValueError):
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
            invalid TLS certificate, a policy refusal...).
        kind: Finer-grained reason: ``"dns"``, ``"connect"``, ``"tls"``,
            ``"timeout"``, ``"proxy"``, ``"redirects"``, ``"protocol"``,
            ``"invalid_url"``, ``"policy"``, ``"browser"``, ``"cache"`` or ``"unknown"``.
    """

    category = "network"
    default_kind = "unknown"

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
        kind: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{message} ({url})", context=context)
        self.url = url
        self.message = message
        self.cause = cause
        self.proxy = proxy
        self.is_proxy_error = is_proxy_error
        self.is_timeout = is_timeout
        self.retryable = retryable
        self.kind = kind or ("timeout" if is_timeout else "proxy" if is_proxy_error else self.default_kind)
        self.context.setdefault("url", url)


class NetworkError(FetchError):
    """The connection failed: DNS lookup, refused/reset connection, TLS handshake..."""

    default_kind = "connect"


class ProxyError(NetworkError):
    """The proxy refused or failed the request."""

    default_kind = "proxy"

    def __init__(self, url: str, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("is_proxy_error", True)
        super().__init__(url, message, **kwargs)


class FetchTimeout(FetchError):
    """The request did not complete in time."""

    category = "timeout"
    default_kind = "timeout"

    def __init__(self, url: str, message: str = "Timed out", **kwargs: Any) -> None:
        kwargs["is_timeout"] = True
        super().__init__(url, message, **kwargs)


class PolicyError(FetchError):
    """A crawl policy refused the request. Never retried.

    Attributes:
        policy: Which policy refused it (``"robots"``, ``"network"``...).
        reason: Why, in a few words.
    """

    category = "policy"
    default_kind = "policy"

    def __init__(self, url: str, message: str, *, policy: str = "policy", reason: str = "", **kwargs: Any) -> None:
        kwargs["retryable"] = False
        super().__init__(url, message, **kwargs)
        self.policy = policy
        self.reason = reason or message
        self.context.setdefault("policy", policy)


class RobotsPolicyError(PolicyError):
    """robots.txt disallows the URL for the configured user agent."""

    def __init__(self, url: str, message: str = "Disallowed by robots.txt", **kwargs: Any) -> None:
        kwargs.setdefault("policy", "robots")
        super().__init__(url, message, **kwargs)


class NetworkPolicyError(PolicyError):
    """The destination is not allowed by the :class:`~wintergrab.netpolicy.NetworkPolicy`
    (for example a private, loopback or cloud-metadata address: SSRF protection).

    Attributes:
        address: The offending IP address, when the refusal was about one.
    """

    def __init__(self, url: str, message: str, *, address: str | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("policy", "network")
        super().__init__(url, message, **kwargs)
        self.address = address
        if address:
            self.context.setdefault("address", address)


class CacheMiss(FetchError):
    """Raised in ``"offline"`` cache mode when a request is not in the cache."""

    category = "cache"
    default_kind = "cache"

    def __init__(self, url: str) -> None:
        super().__init__(url, "Not in the HTTP cache (offline mode)", retryable=False)


class HTTPStatusError(WintergrabError):
    """Raised by :meth:`Response.raise_for_status` for 4xx/5xx responses."""

    category = "http"

    def __init__(self, response: Response, detail: str | None = None) -> None:
        status = f"{detail} (HTTP {response.status})" if detail else f"HTTP {response.status}"
        super().__init__(f"{status} for {response.url}", context={"url": response.url, "status": response.status})
        self.response = response
        self.status = response.status
        self.detail = detail


#: The spelling used across the docs; the same class as :class:`HTTPStatusError`.
HTTPError = HTTPStatusError


class BrowserError(WintergrabError):
    """Something went wrong with the headless browser."""

    category = "browser"


class BrowserNotAvailable(BrowserError):
    """Playwright (or a browser binary for it) is not installed."""


class BrowserFetchError(FetchError, BrowserError):
    """A page failed to load or render in the browser (crash, navigation error...)."""

    category = "browser"
    default_kind = "browser"


class ExtractionError(WintergrabError):
    """Data could not be extracted (a required field is missing, an extractor crashed...)."""

    category = "extraction"


class ValidationError(WintergrabError, ValueError):
    """A record or dataset failed validation.

    Attributes:
        issues: The validation issues (see :mod:`wintergrab.data.validate`).
    """

    category = "validation"

    def __init__(self, message: str, *, issues: list[Any] | None = None, context: Mapping[str, Any] | None = None):
        super().__init__(message, context=context)
        self.issues = list(issues or [])


class StorageError(WintergrabError):
    """Reading or writing stored data failed."""

    category = "storage"


class CheckpointError(StorageError):
    """A crawl could not be checkpointed or resumed."""


class ExportError(StorageError):
    """Items could not be written to an output."""


class BudgetExceeded(WintergrabError):
    """A crawl budget (requests, bytes, runtime...) ran out.

    Attributes:
        budget: Name of the exhausted budget, e.g. ``"max_bytes"``.
    """

    category = "budget"

    def __init__(self, budget: str, message: str | None = None) -> None:
        super().__init__(message or f"crawl budget {budget} exhausted", context={"budget": budget})
        self.budget = budget


def category_of(exc: BaseException) -> str:
    """The failure category of any exception (``"internal"`` for non-wintergrab ones)."""
    if isinstance(exc, WintergrabError):
        return exc.category
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "internal"


def describe(exc: BaseException | Any) -> str:
    """Short one-line description of an exception, for logs and stats."""
    name = type(exc).__name__
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    # curl appends a long "See https://curl.se/libcurl/..." hint; drop it.
    text = text.split(" See https://curl.se/")[0].removeprefix("Failed to perform, ")
    return f"{name}: {text}" if text else name
