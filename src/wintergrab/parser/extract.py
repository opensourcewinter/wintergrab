"""Declarative extraction: describe the data you want as a dict of selectors."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .selector import Selector, SelectorList


class Field:
    """One field of an extraction schema.

    Args:
        *queries: One or more selectors (CSS by default; XPath if it starts
            with ``/``, ``./`` or ``(``; force either with a ``css:`` or
            ``xpath:`` prefix). They are tried in order until one matches -
            handy when a site uses different markup on different pages.
        many: Return every match as a list instead of the first one.
        default: Value used when nothing matches.
        regex: Keep only what this regex captures (first group if any).
        transform: Function applied to each extracted value (e.g. ``float``).
        attr: Read this attribute of the matched element instead of its text.
        html: Return the matched element's HTML instead of its text.
        adaptive: Use adaptive selection for this field (see ``Selector.css``).

    Example::

        Field(".price", "[itemprop=price]", regex=r"[\\d.]+", transform=float)
    """

    __slots__ = ("adaptive", "attr", "default", "html", "many", "queries", "regex", "transform")

    def __init__(
        self,
        *queries: str,
        many: bool = False,
        default: Any = None,
        regex: str | None = None,
        transform: Callable[[Any], Any] | None = None,
        attr: str | None = None,
        html: bool = False,
        adaptive: bool = False,
    ) -> None:
        if not queries:
            raise ValueError("Field() needs at least one selector")
        self.queries = queries
        self.many = many
        self.default = default
        self.regex = regex
        self.transform = transform
        self.attr = attr
        self.html = html
        self.adaptive = adaptive

    def __repr__(self) -> str:
        return f"Field({', '.join(map(repr, self.queries))}, many={self.many})"

    def extract(self, sel: Selector) -> Any:
        from .selector import _regex_all

        for query in self.queries:
            matches = sel.select(query, adaptive=True) if self.adaptive else sel.select(query)
            if not matches:
                continue
            values: list[Any] = []
            for match in matches if self.many else matches[:1]:
                value = _value_of(match, attr=self.attr, html=self.html)
                if value is None:
                    continue
                if self.regex:
                    found = _regex_all(self.regex, value)
                    if not found:
                        continue
                    value = found[0]
                if self.transform is not None:
                    value = self.transform(value)
                values.append(value)
            if values:
                return values if self.many else values[0]
        return [] if self.many and self.default is None else self.default


def _value_of(match: Selector, *, attr: str | None = None, html: bool = False) -> str | None:
    if not match.is_element:
        return match.get()
    if attr:
        return match.attr(attr)
    return match.html if html else match.text


def extract_schema(sel: Selector, schema: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, spec in schema.items():
        out[name] = _extract_one(sel, spec)
    return out


def _extract_one(sel: Selector, spec: Any) -> Any:
    if isinstance(spec, Field):
        return spec.extract(sel)
    if isinstance(spec, str):
        matches: SelectorList = sel.select(spec)
        return _value_of(matches[0]) if matches else None
    if isinstance(spec, list):
        if len(spec) != 1:
            raise ValueError("A list in a schema must hold exactly one selector (it means 'all matches')")
        inner = spec[0]
        if isinstance(inner, dict):
            raise ValueError("Use Selector.extract_all(container, schema) for lists of objects")
        return [v for v in (_value_of(m) for m in sel.select(inner)) if v is not None]
    if isinstance(spec, Mapping):
        return extract_schema(sel, spec)
    if callable(spec):
        return spec(sel)
    raise TypeError(f"Unsupported schema value: {spec!r}")
