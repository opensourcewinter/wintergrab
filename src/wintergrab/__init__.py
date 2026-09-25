"""wintergrab - friendly web scraping that scales from one page to big crawls.

Quick start::

    import wintergrab as wg

    page = wg.get("https://quotes.toscrape.com/")
    for quote in page.css(".quote"):
        print(quote.css(".text::text").get(), "-", quote.css(".author::text").get())

See https://github.com/opensourcewinter/wintergrab for the full docs.
"""

from __future__ import annotations

__version__ = "0.3.0"

# The parser must be imported before the adaptive package (they reference each other).
from .parser import Field, Selector, SelectorList, parse  # isort: skip
from .adaptive import MemoryStorage, SQLiteStorage
from .errors import (
    BrowserNotAvailable,
    CheckpointError,
    FetchError,
    HTTPStatusError,
    SelectorSyntaxError,
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
    Response,
    aget,
    apost,
    arender,
    get,
    post,
    render,
)
from .parser.autoextract import LearnedSchema, RecordGroup
from .proxy import ProxyRotator
from .request import Request
from .scrape import scrape
from .sitemaps import SitemapEntry, sitemap
from .spider import AutoThrottle, CrawlResult, SessionManager, Spider
from .utils import configure_logging

__all__ = [
    "AsyncBrowserFetcher",
    "AsyncFetcher",
    "AutoThrottle",
    "BrowserFetcher",
    "BrowserNotAvailable",
    "CacheMiss",
    "CapturedResponse",
    "CheckpointError",
    "CrawlResult",
    "FetchError",
    "Fetcher",
    "Field",
    "HTTPCache",
    "HTTPStatusError",
    "LearnedSchema",
    "MemoryStorage",
    "ProxyRotator",
    "RecordGroup",
    "Request",
    "Response",
    "SQLiteStorage",
    "Selector",
    "SelectorList",
    "SelectorSyntaxError",
    "SessionManager",
    "SitemapEntry",
    "Spider",
    "WintergrabError",
    "__version__",
    "aget",
    "apost",
    "arender",
    "configure_logging",
    "get",
    "parse",
    "post",
    "render",
    "scrape",
    "sitemap",
]
