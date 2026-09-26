"""wintergrab - friendly web scraping that scales from one page to big crawls.

Quick start::

    import wintergrab as wg

    page = wg.get("https://quotes.toscrape.com/")
    for quote in page.css(".quote"):
        print(quote.css(".text::text").get(), "-", quote.css(".author::text").get())

See https://github.com/opensourcewinter/wintergrab for the full docs.
"""

from __future__ import annotations

__version__ = "0.2.0"

# The parser must be imported before the adaptive package (they reference each other).
from .parser import Field, Selector, SelectorList, parse  # isort: skip
from .adaptive import MemoryStorage, SQLiteStorage
from .errors import (
    BrowserError,
    BrowserFetchError,
    BrowserNotAvailable,
    BudgetExceeded,
    CheckpointError,
    ConfigurationError,
    ExportError,
    ExpressionError,
    ExtractionError,
    FetchError,
    FetchTimeout,
    HTTPError,
    HTTPStatusError,
    ModelError,
    NetworkError,
    NetworkPolicyError,
    ParserError,
    PolicyError,
    ProxyError,
    RobotsPolicyError,
    SchemaError,
    SelectorSyntaxError,
    StorageError,
    ValidationError,
    WintergrabError,
)
from .fetchers import (
    AsyncBrowserFetcher,
    AsyncFetcher,
    BrowserFetcher,
    CacheMiss,
    CapturedResponse,
    Fetcher,
    HTTPCache,
    ResourceFilter,
    Response,
    aget,
    apost,
    arender,
    get,
    post,
    render,
)
from .netpolicy import NetworkPolicy
from .parser.autoextract import LearnedSchema, RecordGroup
from .proxy import ProxyRotator
from .request import Request
from .sitemaps import SitemapEntry, sitemap
from .spider import AutoThrottle, CrawlResult, DropItem, IgnoreRequest, ItemPipeline, SessionManager, Spider
from .urls import URLNormalizer, URLRules, normalize_url, url_template
from .utils import configure_logging

__all__ = [
    "AsyncBrowserFetcher",
    "AsyncFetcher",
    "AutoThrottle",
    "BrowserError",
    "BrowserFetchError",
    "BrowserFetcher",
    "BrowserNotAvailable",
    "BudgetExceeded",
    "CacheMiss",
    "CapturedResponse",
    "CheckpointError",
    "ConfigurationError",
    "CrawlResult",
    "DropItem",
    "ExportError",
    "ExpressionError",
    "ExtractionError",
    "FetchError",
    "FetchTimeout",
    "Fetcher",
    "Field",
    "HTTPCache",
    "HTTPError",
    "HTTPStatusError",
    "IgnoreRequest",
    "ItemPipeline",
    "LearnedSchema",
    "MemoryStorage",
    "ModelError",
    "NetworkError",
    "NetworkPolicy",
    "NetworkPolicyError",
    "ParserError",
    "PolicyError",
    "ProxyError",
    "ProxyRotator",
    "RecordGroup",
    "Request",
    "ResourceFilter",
    "Response",
    "RobotsPolicyError",
    "SQLiteStorage",
    "SchemaError",
    "Selector",
    "SelectorList",
    "SelectorSyntaxError",
    "SessionManager",
    "SitemapEntry",
    "Spider",
    "StorageError",
    "URLNormalizer",
    "URLRules",
    "ValidationError",
    "WintergrabError",
    "__version__",
    "aget",
    "apost",
    "arender",
    "configure_logging",
    "get",
    "normalize_url",
    "parse",
    "post",
    "render",
    "sitemap",
    "url_template",
]
