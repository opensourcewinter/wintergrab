"""The page as extraction strategies see it: parsed once, with lazily computed views."""

from __future__ import annotations

import time
from collections.abc import Iterator
from functools import cached_property
from typing import Any

from ..fetchers.response import Response
from ..parser import Selector
from ..parser.layout import Layout, element_path
from ..parser.text import text_content

__all__ = ["PageContext", "schema_types"]

_MAX_MODEL_TEXT = 24_000  # characters of page text handed to an extraction model


#: The kinds of structured data that hold typed objects, and their key in :func:`~wintergrab.parser.structured_data`.
STRUCTURED_KEYS = {"json-ld": "json_ld", "microdata": "microdata", "rdfa": "rdfa"}
STRUCTURED_KINDS = tuple(STRUCTURED_KEYS)


def schema_types(node: Any) -> list[str]:
    """The schema.org type names of a JSON-LD, microdata or RDFa node (``"https://schema.org/Product"`` ->
    ``"Product"``)."""
    if not isinstance(node, dict):
        return []
    declared = node.get("@type")
    values = declared if isinstance(declared, list) else [declared]
    out = []
    for value in values:
        if isinstance(value, str) and value.strip():
            out.append(value.strip().rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1])
    return out


class PageContext:
    """A page (a :class:`~wintergrab.Response`, a :class:`~wintergrab.Selector` or HTML) ready for extraction.

    Everything derived from the page (structured data, embedded JSON, visible
    text) is computed on first use and kept, so strategies can share it.
    """

    url: str | None
    fetched_at: float

    def __init__(
        self,
        source: Any,
        *,
        url: str | None = None,
        fetched_at: float | None = None,
        scope: Selector | None = None,
        parent: PageContext | None = None,
    ) -> None:
        self.response: Response | None = None
        if isinstance(source, PageContext):
            self.response = source.response
            self.selector: Selector = source.selector
            url = url or source.url
            fetched_at = fetched_at or source.fetched_at
        elif isinstance(source, Response):
            self.response = source
            self.selector = source.selector
            url = url or source.url
        elif isinstance(source, Selector):
            self.selector = source
            url = url or source.url
        elif isinstance(source, (str, bytes)):
            text = source.decode("utf-8", errors="replace") if isinstance(source, bytes) else source
            self.selector = Selector(text, url=url)
        else:
            raise TypeError(f"cannot extract from {type(source).__name__}; pass a Response, a Selector or HTML")
        self.url = url
        self.fetched_at = fetched_at if fetched_at is not None else time.time()
        #: For records inside a listing: the element holding one record (strategies look only inside it).
        self.scope = scope
        self.parent = parent

    def scoped(self, element: Selector) -> PageContext:
        """The same page, looking only at ``element`` (one record of a listing)."""
        return PageContext(self, scope=element, parent=self)

    @property
    def root(self) -> Selector:
        """Where selectors are evaluated: the record's element, or the whole page."""
        return self.scope if self.scope is not None else self.selector

    @cached_property
    def layout(self) -> Layout | None:
        """Where the page's text was drawn (a browser fetch with ``layout=True``): the record's part of it
        for a record of a listing; ``None`` when not recorded."""
        layout = self.response.layout if self.response is not None else None
        if layout is None or self.scope is None or self.scope.root is None:
            return layout
        return layout.within(element_path(self.scope.root))

    # -- structured data ---------------------------------------------------- #
    @cached_property
    def structured(self) -> dict[str, Any]:
        """JSON-LD, microdata, RDFa, OpenGraph, Twitter and meta tags (see
        :func:`~wintergrab.parser.structured_data`)."""
        if self.parent is not None:
            return self.parent.structured
        try:
            return self.selector.structured_data()
        except Exception:  # pragma: no cover - defensive: broken markup must not stop extraction
            return {"json_ld": [], "microdata": [], "rdfa": [], "opengraph": {}, "twitter": {}, "meta": {}}

    def nodes(self, kind: str) -> list[tuple[str, dict[str, Any]]]:
        """:meth:`iter_nodes` as a list, computed once per page."""
        cache = self.__dict__.setdefault("_nodes", {})
        if kind not in cache:
            cache[kind] = list(self.iter_nodes(kind))
        return list(cache[kind])

    def iter_nodes(self, kind: str) -> Iterator[tuple[str, dict[str, Any]]]:
        """``(path, node)`` for every typed object in the page's JSON-LD (``kind="json-ld"``), microdata or RDFa.

        Nested objects that carry their own ``@type`` (a ``WebPage``'s
        ``mainEntity``, an ``ItemList``'s items) are included with their path.
        """
        roots = self.structured.get(STRUCTURED_KEYS.get(kind, kind)) or []
        stack: list[tuple[str, Any]] = [(f"[{i}]", node) for i, node in reversed(list(enumerate(roots)))]
        while stack:
            path, node = stack.pop()
            if isinstance(node, dict):
                if schema_types(node):
                    yield path, node
                for key, value in reversed(list(node.items())):
                    if isinstance(value, (dict, list)) and key not in ("@context",):
                        stack.append((f"{path}.{key}", value))
            elif isinstance(node, list):
                for i, value in reversed(list(enumerate(node))):
                    if isinstance(value, (dict, list)):
                        stack.append((f"{path}[{i}]", value))

    @cached_property
    def embedded(self) -> dict[str, Any]:
        """JSON state embedded by JavaScript apps (``__NEXT_DATA__``, ``window.__STATE__``...)."""
        if self.parent is not None:
            return self.parent.embedded
        try:
            return self.selector.embedded_json()
        except Exception:  # pragma: no cover - defensive
            return {}

    # -- text ----------------------------------------------------------------- #
    @cached_property
    def text(self) -> str:
        """Visible text of the page (or of the record's element), whitespace-normalized."""
        root = self.root.root
        return text_content(root) if root is not None else ""

    @cached_property
    def lines(self) -> list[str]:
        """Visible text, one line per block element (for ``Label: value`` patterns)."""
        return [line.strip() for line in self.root.get_text().splitlines() if line.strip()]

    @cached_property
    def main_text(self) -> str:
        """The main content as Markdown, trimmed for an extraction model."""
        root = self.root
        text = root.markdown(main_content=self.scope is None)
        if len(text) < 200 and self.scope is None:  # the "main" element was a small part of the page
            text = root.markdown()
        return text[:_MAX_MODEL_TEXT]

    def __repr__(self) -> str:
        where = " (one record)" if self.scope is not None else ""
        return f"PageContext({self.url!r}{where})"
