"""Crawling: the Spider base class and everything that powers it."""

from .exporters import open_exporter, write_items
from .failures import FailureDiagnosis
from .middleware import DropItem, IgnoreRequest, ItemPipeline
from .sessions import SessionManager
from .spider import CrawlResult, Spider
from .throttle import AutoThrottle

__all__ = [
    "AutoThrottle",
    "CrawlResult",
    "DropItem",
    "FailureDiagnosis",
    "IgnoreRequest",
    "ItemPipeline",
    "SessionManager",
    "Spider",
    "open_exporter",
    "write_items",
]
