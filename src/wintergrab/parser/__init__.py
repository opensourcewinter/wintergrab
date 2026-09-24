"""HTML/XML parsing: CSS & XPath selection, extraction schemas, text & Markdown."""

from .css import css_to_xpath
from .extract import Field
from .selector import Selector, SelectorList, parse_document

__all__ = ["Field", "Selector", "SelectorList", "css_to_xpath", "parse", "parse_document"]


def parse(markup: str | bytes, url: str | None = None, *, type: str = "html", **kwargs) -> Selector:  # type: ignore[no-untyped-def]
    """Parse HTML (or XML with ``type="xml"``) into a :class:`Selector`."""
    return Selector(markup, url=url, type=type, **kwargs)
