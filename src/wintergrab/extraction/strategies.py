"""Extraction strategies: each proposes candidate values for a schema field, with where they came from.

Strategies run in the order of the preferred hierarchy (cheapest and most
reliable first)::

    structured data (JSON-LD, microdata)  ->  meta tags (OpenGraph, Twitter, <meta>)
    ->  your selectors  ->  embedded JSON (app state)  ->  labelled values ("Weight: 1.2 kg")
    ->  DOM heuristics (the h1, the element classed "price"...)  ->  text patterns
    ->  an extraction model, only for what is still missing (see :mod:`~wintergrab.extraction.model`)

A strategy never decides: it returns :class:`Candidate` s, and the
:class:`~wintergrab.extraction.Extractor` compares them (agreement between
independent sources raises confidence, disagreement lowers it).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlsplit

from ..data.normalize import iter_numbers, normalize_availability, normalize_phone, parse_rating
from ..parser import Selector
from ..parser.text import tag_name, text_content
from .page import PageContext, schema_types
from .schemaorg import FIELD_PATHS, META_KEYS, camel, candidate_names, field_key, read_path, target_types

if TYPE_CHECKING:
    from ..data.schema import Schema, SchemaField

__all__ = [
    "STRATEGIES",
    "Candidate",
    "DomHeuristics",
    "EmbeddedJson",
    "LabelledValues",
    "MetaTags",
    "Patterns",
    "RecordFields",
    "Selectors",
    "Strategy",
    "StructuredData",
    "field_kind",
    "register_strategy",
]

_MAX_CANDIDATES = 5  # distinct values one strategy may propose for a field


@dataclass
class Candidate:
    """A value one strategy found for one field.

    Attributes:
        raw: The value as found (text, a number, a list for multi-valued fields).
        method: Which strategy (``"json-ld"``, ``"dom"``...) - its reliability prior applies.
        source: Where exactly: ``"json-ld:Product.offers.price"``, ``"css:h1"``...
        factor: Evidence about this particular value (1 = none): below 1 when the
            strategy saw several different values, or had to guess.
        detail: Why the factor is what it is (for explanations).
    """

    raw: Any
    method: str
    source: str
    factor: float = 1.0
    detail: str = ""
    # filled in by the extractor
    value: Any = None
    notes: list[str] = field(default_factory=list)
    confidence: float = 0.0


def _uniqueness(count: int) -> float:
    """Several different values found by one strategy: each is less likely the right one."""
    return 1.0 if count <= 1 else max(0.3, 1 / math.sqrt(count))


# --------------------------------------------------------------------------- #
# what a field holds
# --------------------------------------------------------------------------- #
_KIND_NAMES = {
    "title": {"name", "title", "headline", "product_name", "product_title", "job_title", "item_name", "heading"},
    "description": {"description", "summary", "body", "content", "details", "overview", "abstract", "text"},
    "availability": {"availability", "stock", "in_stock", "stock_status", "inventory"},
    "review_count": {"review_count", "reviews", "reviews_count", "num_reviews", "rating_count", "ratings", "ratings_count"},
    "author": {"author", "byline", "writer", "authors", "posted_by"},
    "sku": {"sku", "mpn", "model", "model_number", "product_id", "item_number", "part_number", "article_number",
            "gtin", "ean", "upc", "isbn", "asin"},
    "brand": {"brand", "manufacturer", "make", "vendor"},
    "image": {"image", "images", "photo", "photos", "thumbnail", "picture", "image_url", "img"},
    "url": {"url", "link", "href", "product_url", "page_url", "permalink"},
    "category": {"category", "categories", "section", "department", "breadcrumb"},
}  # fmt: skip


def field_kind(f: SchemaField) -> str:
    """What a field holds, for the heuristic strategies (see :func:`_kind`)."""
    return _kind(f.name, f.type, tuple(f.aliases))


@lru_cache(maxsize=1024)
def _kind(name: str, kind: str, aliases: tuple[str, ...]) -> str:
    """What a field holds, for the heuristic strategies.

    One of ``title``, ``description``, ``price``, ``currency``, ``availability``,
    ``rating``, ``review_count``, ``image``, ``url``, ``date``, ``author``,
    ``email``, ``phone``, ``address``, ``sku``, ``brand``, ``category``,
    ``bedrooms``, ``bathrooms``, ``floor_area`` or ``other``.
    """
    keys = set(candidate_names(name, aliases))
    if kind in ("integer", "number") and keys & {"bedrooms", "beds", "bedroom"}:
        return "bedrooms"
    if kind in ("integer", "number") and keys & {"bathrooms", "baths", "bathroom"}:
        return "bathrooms"
    if kind in ("quantity", "number") and keys & {"floor_size", "floor_area", "living_area", "living_space"}:
        return "floor_area"
    if kind == "currency":
        return "currency"
    if kind == "money" or any(word in k for k in keys for word in ("price", "cost")):
        return "price"
    if kind == "availability" or keys & _KIND_NAMES["availability"]:
        return "availability"
    if kind == "rating" or (keys & {"rating", "stars", "score", "average_rating"} and kind in ("number", "rating")):
        return "rating"
    if keys & _KIND_NAMES["review_count"] and kind in ("integer", "number"):
        return "review_count"
    if kind in ("email", "phone", "address"):
        return kind
    if keys & _KIND_NAMES["image"]:
        return "image"
    if kind == "url" or keys & _KIND_NAMES["url"]:
        return "url"
    if kind in ("date", "datetime"):
        return "date"
    for name in ("title", "description", "author", "sku", "brand", "category"):
        if keys & _KIND_NAMES[name]:
            return name
    return "other"


def _element_value(match: Selector, kind: str) -> str | None:
    """The useful value of a matched element: an URL for links and images, else its text."""
    if not match.is_element:
        return match.get()
    if kind in ("image", "url"):
        for attr in ("content", "href", "src", "data-src", "data-original", "data-lazy-src"):
            value = match.attr(attr)
            if value:
                return match.urljoin(value.strip())
        if kind == "image":
            img = match.css("img")
            if img:
                return _element_value(img[0], kind)
        return None
    for attr in ("content", "datetime", "data-value"):
        value = match.attr(attr)
        if value and value.strip():
            return value.strip()
    return match.text or None


def _distinct(values: list[tuple[Any, str]], limit: int = _MAX_CANDIDATES) -> list[tuple[Any, str]]:
    """``(value, source)`` pairs without repeated values (first occurrence wins)."""
    seen: set[str] = set()
    out = []
    for value, source in values:
        key = repr(value).casefold()
        if key not in seen:
            seen.add(key)
            out.append((value, source))
            if len(out) >= limit:
                break
    return out


class Strategy:
    """Base class: ``candidates(page, field, schema)`` returns what this strategy finds for one field."""

    #: Name used in provenance (``method``) and to configure priors.
    method: ClassVar[str] = "strategy"
    #: Strategies that describe the whole page (structured data, meta tags) are skipped inside listing records.
    page_level: ClassVar[bool] = False

    def candidates(self, page: PageContext, f: SchemaField, schema: Schema) -> list[Candidate]:
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


# --------------------------------------------------------------------------- #
# structured data
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=256)
def _paths_for(name: str, aliases: tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    for key in candidate_names(name, aliases):
        for path in (*FIELD_PATHS.get(key, ()), camel(key), key):
            if path not in out:
                out.append(path)
    return tuple(out)


class StructuredData(Strategy):
    """JSON-LD and microdata (schema.org): what the site publishes for machines.

    Records come from the objects whose ``@type`` fits the schema's name
    (``product`` -> ``Product``...). A field is read from the paths schema.org
    uses for it (``price`` -> ``offers.price``...), from its own name in
    camelCase, or from explicit ``sources`` such as ``"jsonld:Product.offers.price"``.
    """

    method = "json-ld"
    page_level = True
    kinds = ("json-ld", "microdata")

    def __init__(self, node: tuple[str, dict[str, Any]] | None = None, kind: str | None = None) -> None:
        #: Restrict to one object (a record of a listing made of several JSON-LD objects).
        self.node = node
        self.node_kind = kind

    @staticmethod
    def _explicit(f: SchemaField, kind: str) -> list[tuple[str | None, str]]:
        """``(type or None, path)`` from ``sources`` like ``jsonld:Product.offers.price`` / ``microdata:offers.price``."""
        prefixes = ("jsonld:", "json-ld:") if kind == "json-ld" else ("microdata:",)
        out: list[tuple[str | None, str]] = []
        for source in f.sources:
            for prefix in prefixes:
                if source.startswith(prefix):
                    spec = source[len(prefix) :]
                    head, _, rest = spec.partition(".")
                    if head[:1].isupper() and rest:
                        out.append((head, rest))
                    else:
                        out.append((None, spec))
        return out

    def candidates(self, page: PageContext, f: SchemaField, schema: Schema) -> list[Candidate]:
        out: list[Candidate] = []
        wanted = target_types(schema.name)
        for kind in (self.node_kind,) if self.node_kind else self.kinds:
            nodes = [self.node] if self.node is not None else list(page.nodes(kind))
            if not nodes:
                continue
            matching = [(p, n) for p, n in nodes if wanted & set(schema_types(n))] if wanted else []
            pool, mismatch = (matching, 1.0) if matching else (nodes, 0.85 if wanted else 1.0)
            explicit = self._explicit(f, kind)
            paths: list[tuple[str | None, str]] = [
                *explicit,
                *((None, p) for p in _paths_for(f.name, tuple(f.aliases))),
            ]
            for type_name, path in paths:
                found: list[tuple[Any, str]] = []
                for index, (_node_path, node) in enumerate(pool):
                    types = schema_types(node)
                    if type_name is not None and type_name not in types:
                        continue
                    for value in read_path(node, path):
                        # The first matching object is the page's subject; others (related products...) count less.
                        found.append(
                            (value, f"{kind}:{types[0] if types else '?'}.{path}" + (f"#{index + 1}" if index else ""))
                        )
                    if found and index == 0 and not f.many:
                        break
                if not found:
                    continue
                if f.many:
                    values = [value for value, _ in found]
                    out.append(Candidate(values, kind, found[0][1], mismatch))
                else:
                    distinct = _distinct(found)
                    for value, source in distinct:
                        factor = mismatch * (1.0 if source == distinct[0][1] else 0.6) * _uniqueness(len(distinct))
                        detail = "not the schema's type" if mismatch < 1 else ""
                        if len(distinct) > 1:
                            detail = (detail + "; " if detail else "") + f"{len(distinct)} different values"
                        out.append(Candidate(value, kind, source, factor, detail))
                break  # the most specific path that has a value wins
        return out


class MetaTags(Strategy):
    """OpenGraph, Twitter card and ``<meta>`` values (``og:title``, ``product:price:amount``...)."""

    method = "opengraph"
    page_level = True

    def candidates(self, page: PageContext, f: SchemaField, schema: Schema) -> list[Candidate]:
        data = page.structured
        out: list[Candidate] = []
        seen: set[str] = set()
        for key in candidate_names(f.name, f.aliases):
            for source, meta_key in META_KEYS.get(key, ()):
                value = (data.get(source) or {}).get(meta_key)
                if isinstance(value, list):
                    value = value if f.many else (value[0] if value else None)
                if value in (None, "", []):
                    continue
                factor, detail = 1.0, ""
                if source == "meta" and meta_key == "title":
                    value, factor, detail = self._title(str(value), data), 0.9, "the <title>, site name removed"
                marker = repr(value).casefold()
                if marker in seen:
                    continue
                seen.add(marker)
                method = {"opengraph": "opengraph", "twitter": "twitter"}.get(source, "meta")
                out.append(Candidate(value, method, f"{method}:{meta_key}", factor, detail))
        return out

    @staticmethod
    def _title(title: str, data: dict[str, Any]) -> str:
        site = str((data.get("opengraph") or {}).get("site_name") or "").strip()
        parts = [p.strip() for p in re.split(r"\s+[|\-–—:·]\s+", title) if p.strip()]
        if site:
            parts = [p for p in parts if p.casefold() != site.casefold()] or parts
        return parts[0] if parts else title


# --------------------------------------------------------------------------- #
# selectors and embedded JSON
# --------------------------------------------------------------------------- #
class Selectors(Strategy):
    """The CSS/XPath selectors given in the schema (``selectors=[".price", "[itemprop=price]::attr(content)"]``)."""

    method = "selector"

    def candidates(self, page: PageContext, f: SchemaField, schema: Schema) -> list[Candidate]:
        kind = field_kind(f)
        for query in f.selectors:
            try:
                matches = page.root.select(query)
            except Exception:  # an invalid selector must not stop the other strategies
                continue
            values = [v for v in (_element_value(m, kind) for m in matches) if v not in (None, "")]
            if not values:
                continue
            source = f"selector:{query}"
            if f.many:
                return [Candidate(values, self.method, source)]
            distinct = _distinct([(v, source) for v in values])
            detail = f"matched {len(distinct)} different values" if len(distinct) > 1 else ""
            return [Candidate(distinct[0][0], self.method, source, _uniqueness(len(distinct)), detail)]
        return []


def _scalar_or_list(value: Any) -> bool:
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return bool(str(value).strip())
    return isinstance(value, list) and bool(value) and all(isinstance(v, (str, int, float)) for v in value)


def _walk_json(data: Any, keys: set[str], *, prefix: str = "", limit: int = 50_000) -> Iterator[tuple[str, Any]]:
    """``(path, value)`` for every scalar (or list of scalars) stored under one of ``keys``."""
    stack: list[tuple[str, Any]] = [(prefix, data)]
    visited = 0
    while stack and visited < limit:
        path, node = stack.pop()
        visited += 1
        if isinstance(node, dict):
            for key, value in node.items():
                child = f"{path}.{key}" if path else str(key)
                if isinstance(key, str) and field_key(key) in keys and _scalar_or_list(value):
                    yield child, value
                if isinstance(value, (dict, list)):
                    stack.append((child, value))
        elif isinstance(node, list):
            for i, value in enumerate(node[:200]):
                if isinstance(value, (dict, list)):
                    stack.append((f"{path}[{i}]", value))


class EmbeddedJson(Strategy):
    """Values in the JSON state that JavaScript apps embed in their HTML (``__NEXT_DATA__``...).

    A field matches a key of the same name (``review_count`` = ``reviewCount``).
    App state often holds many records (related items, a cart), so several
    different values lower each one's confidence.
    """

    method = "embedded-json"
    page_level = True

    def candidates(self, page: PageContext, f: SchemaField, schema: Schema) -> list[Candidate]:
        if not page.embedded:
            return []
        keys = set(candidate_names(f.name, f.aliases))
        found: list[tuple[Any, str]] = []
        for name, data in page.embedded.items():
            for path, value in _walk_json(data, keys, prefix=name):
                found.append((value, f"embedded-json:{path}"))
                if len(found) >= 50:
                    break
        distinct = _distinct(found)
        detail = f"{len(distinct)} different values in the app state" if len(distinct) > 1 else ""
        return [Candidate(v, self.method, s, _uniqueness(len(distinct)), detail) for v, s in distinct]


# --------------------------------------------------------------------------- #
# labelled values
# --------------------------------------------------------------------------- #
_LABEL_SPLIT = re.compile(r"^\s*([^:：]{1,40}?)\s*[:：]\s*(.+?)\s*$")


def _label_pairs(page: PageContext) -> list[tuple[str, str, str]]:
    """``(label key, value, source)`` from definition lists, two-cell table rows and ``Label: value`` lines."""
    root = page.root
    pairs: list[tuple[str, str, str]] = []
    for dt in root.css("dt"):
        dd = dt.next
        if dd is not None and dd.tag == "dd" and dt.text and dd.text:
            pairs.append((field_key(dt.text.rstrip(":： ")), dd.text, "dl"))
    for row in root.css("tr"):
        cells = [cell for cell in row.root if tag_name(cell) in ("th", "td")] if row.root is not None else []
        if len(cells) == 2:
            label, value = text_content(cells[0]), text_content(cells[1])
            if label and value:
                pairs.append((field_key(label.rstrip(":： ")), value, "table"))
    for line in page.lines:
        match = _LABEL_SPLIT.match(line)
        if match and match.group(2):
            pairs.append((field_key(match.group(1)), match.group(2), "text"))
    return pairs


class LabelledValues(Strategy):
    """Values next to a label named like the field: ``<dt>Weight</dt><dd>1.2 kg</dd>``, ``SKU: AB-12``, spec tables."""

    method = "label"

    def candidates(self, page: PageContext, f: SchemaField, schema: Schema) -> list[Candidate]:
        cache = page.__dict__.setdefault("_label_pairs", {})
        key = id(page.root)
        if key not in cache:
            cache[key] = _label_pairs(page)
        names = set(candidate_names(f.name, f.aliases))
        found = [(value, f"label:{where}:{label}") for label, value, where in cache[key] if label in names]
        distinct = _distinct(found)
        detail = f"{len(distinct)} different labelled values" if len(distinct) > 1 else ""
        return [Candidate(v, self.method, s, _uniqueness(len(distinct)), detail) for v, s in distinct]


# --------------------------------------------------------------------------- #
# DOM heuristics
# --------------------------------------------------------------------------- #
def _class_has(*words: str) -> str:
    """XPath predicate: the class, id or itemprop contains one of ``words`` (case-insensitive)."""
    upper, lower = "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
    tests = []
    for word in words:
        for attr in ("@class", "@id", "@itemprop", "@data-testid"):
            tests.append(f"contains(translate({attr},'{upper}','{lower}'),'{word}')")
    return " or ".join(tests)


# Parts of a page about other things than its subject: marked with these words, or these elements.
_ASIDE_WORDS = ("related", "recommend", "similar", "also-", "upsell", "cross-sell", "carousel", "sidebar", "footer",
                "header", "cart", "basket", "minicart")  # fmt: skip
_ASIDE_TAGS = ("footer", "nav", "aside")
_ASIDE_ROLES = (
    "descendant-or-self::*[@role='banner' or @role='contentinfo' or @role='navigation' or @role='complementary']"
)
_STRUCK = frozenset({"del", "s", "strike"})
_OLD_PRICE = re.compile(r"old|was|regular|compare|strike|original|list|before|rrp|msrp|crossed", re.I)
_DOM_WORDS = {
    "price": ("price", "amount"),
    "availability": ("stock", "availab", "inventory"),
    "rating": ("rating", "stars", "score"),
    "review_count": ("review", "rating-count", "ratings"),
    "description": ("description", "desc", "summary", "overview"),
    "author": ("author", "byline"),
    "sku": ("sku", "mpn", "model-number", "product-id", "part-number"),
    "brand": ("brand", "manufacturer", "vendor"),
    "date": ("date", "published", "posted"),
}
_MARKED = "descendant-or-self::*[@class or @id or @itemprop or @data-testid or @rel]"
_BREADCRUMB_LINKS = "descendant-or-self::*[" + _class_has("breadcrumb") + "]//a"


def _markers(page: PageContext) -> list[tuple[Selector, str]]:
    """``(element, its lower-cased class, id, itemprop, data-testid and rel)`` for the marked elements, in page order.

    Listed once per page (one XPath query without predicates on the values):
    word tests over this short list are far cheaper than ``contains()`` in XPath.
    """
    cache = page.__dict__.setdefault("_markers", {})
    key = id(page.root)
    index = cache.get(key)
    if index is None:
        index = cache[key] = [
            (
                el,
                " ".join(filter(None, (el.attr(a) for a in ("class", "id", "itemprop", "data-testid", "rel")))).lower(),
            )
            for el in page.root.xpath(_MARKED)
        ]
    return index  # type: ignore[no-any-return]


def _marked(page: PageContext, kind: str) -> list[Selector]:
    """Elements whose class, id, itemprop, data-testid or rel mentions one of ``kind``'s words, in page order."""
    words = _DOM_WORDS[kind]
    return [el for el, marker in _markers(page) if any(word in marker for word in words)]


def _asides(page: PageContext) -> set[Any]:
    """The page's elements about other things (related products, the cart, the footer...), found once."""
    cache = page.__dict__.setdefault("_asides", {})
    key = id(page.root)
    if key not in cache:
        found = {el.root for el, marker in _markers(page) if any(word in marker for word in _ASIDE_WORDS)}
        root = page.root.root
        if root is not None:
            found.update(root.iter(*_ASIDE_TAGS))
            found.update(el.root for el in page.root.xpath(_ASIDE_ROLES))
            # the site's header, not an article's or a product's own <header>
            found.update(
                header
                for header in root.iter("header")
                if not any(tag_name(a) in ("main", "article") for a in header.iterancestors())
            )
        cache[key] = found
    return cache[key]  # type: ignore[no-any-return]


_CART_BUTTON = re.compile(
    r"add to (?:cart|bag|basket)|buy now|in den warenkorb|ajouter au panier|añadir al carrito", re.I
)
_SOLD_OUT_BUTTON = re.compile(r"sold out|out of stock|notify me|currently unavailable|ausverkauft|épuisé|agotado", re.I)
_REVIEW_COUNT = re.compile(
    r"(\d(?:\d{0,2}(?:[,.\s]\d{3})+|\d*))\s*(?:reviews?|ratings?|customer reviews|bewertungen|avis|opiniones|recensioni)",
    re.I,
)


class DomHeuristics(Strategy):
    """What pages usually look like: the ``<h1>`` is the title, an element classed "price" holds the price,
    ``<time datetime>`` a date, ``mailto:``/``tel:`` links contact details...

    Guesses, so their prior is lower than structured data's; they matter most
    when they agree with another source or when nothing else is available.
    """

    method = "dom"

    def candidates(self, page: PageContext, f: SchemaField, schema: Schema) -> list[Candidate]:
        kind = field_kind(f)
        finder = getattr(self, f"_{kind}", None)
        if finder is None:
            return []
        found: list[tuple[Any, str]] = finder(page, f)
        if not found:
            return []
        if f.many:
            return [Candidate([v for v, _ in found], self.method, found[0][1])]
        distinct = _distinct(found)
        detail = f"{len(distinct)} different candidates on the page" if len(distinct) > 1 else ""
        return [
            Candidate(v, self.method, s, _uniqueness(len(distinct)) * (1.0 if i == 0 else 0.8), detail)
            for i, (v, s) in enumerate(distinct)
        ]

    # each finder returns (value, source) pairs, best first
    @staticmethod
    def _texts(page: PageContext, kind: str, label: str, *, max_len: int = 200) -> list[tuple[Any, str]]:
        out = []
        for match in _marked(page, kind):
            text = match.text
            if text and len(text) <= max_len:
                out.append((text, f"dom:{label}"))
        return out

    def _title(self, page: PageContext, f: SchemaField) -> list[tuple[Any, str]]:
        if page.scope is None:
            return [(h.text, "dom:h1") for h in page.root.css("h1") if h.text]
        for query in ("h1, h2, h3, h4", "[class*=title], [class*=name]", "a[title]", "a"):
            for match in page.root.css(query):
                value = match.attr("title") if query == "a[title]" else match.text
                if value and len(value) <= 300:
                    return [(value, f"dom:{query.split(',')[0]}")]
        return []

    def _price(self, page: PageContext, f: SchemaField) -> list[tuple[Any, str]]:
        wants_old = bool(_OLD_PRICE.search(f.name))
        marked = _marked(page, "price")
        nodes = {match.root for match in marked if match.root is not None}
        # elements wrapping other price elements: look at those instead
        wrappers = {parent for node in nodes for parent in node.iterancestors() if parent in nodes}
        asides = _asides(page) if page.scope is None else set()
        out = []
        for match in marked:
            node = match.root
            if node is None or node in wrappers:
                continue
            ancestors = list(node.iterancestors())
            if asides and any(a in asides for a in ancestors):
                continue  # related products, the cart, the footer...
            text = match.attr("content") or match.text
            if not text or len(text) > 40 or not any(ch.isdigit() for ch in text):
                continue
            marker = " ".join(filter(None, [match.attr("class"), match.attr("id")]))
            struck = match.tag in _STRUCK or any(tag_name(a) in _STRUCK for a in ancestors)
            if (struck or bool(_OLD_PRICE.search(marker))) != wants_old:
                continue
            classes = (match.attr("class") or "").split()
            out.append((text, f"dom:{match.tag}.{classes[0]}" if classes else f"dom:{match.tag}"))
        return out

    def _availability(self, page: PageContext, f: SchemaField) -> list[tuple[Any, str]]:
        out = []
        for match in _marked(page, "availability"):
            text = match.attr("href") or match.attr("content") or match.text
            if text and normalize_availability(text):
                out.append((text, "dom:[class*=stock]"))
        for button in page.root.css("button, input[type=submit], a[class*=button], a[class*=btn]"):
            label = button.text or button.attr("value") or ""
            if _SOLD_OUT_BUTTON.search(label) or (button.attr("disabled") is not None and _CART_BUTTON.search(label)):
                out.append(("OutOfStock", "dom:button"))
            elif _CART_BUTTON.search(label):
                out.append(("InStock", "dom:add-to-cart button"))
        return out

    def _rating(self, page: PageContext, f: SchemaField) -> list[tuple[Any, str]]:
        out = []
        for match in _marked(page, "rating"):
            for text in (
                match.attr("aria-label"),
                match.attr("title"),
                match.attr("data-rating"),
                match.attr("content"),
                match.text,
            ):
                if text and len(text) <= 60 and parse_rating(text, best=f.best if f.best_given else None):
                    out.append((text, "dom:[class*=rating]"))
                    break
            else:  # the class may say it: <p class="star-rating Three">, <div class="stars stars-4-5">
                value = _class_rating(match.attr("class") or "")
                if value is not None:
                    out.append((value, "dom:class"))
        return out

    def _review_count(self, page: PageContext, f: SchemaField) -> list[tuple[Any, str]]:
        out = []
        for match in _marked(page, "review_count"):
            text = match.text
            if text and len(text) <= 60:
                found = _REVIEW_COUNT.search(text)
                if found:
                    out.append((found.group(1).strip(), "dom:[class*=review]"))
        return out

    def _image(self, page: PageContext, f: SchemaField) -> list[tuple[Any, str]]:
        out = []
        queries = (
            "[itemprop=image]",
            "img[class*=product], img[id*=main], img[class*=main]",
            "main img, article img",
            "img",
        )
        for query in queries if page.scope is None else ("img",):
            for match in page.root.css(query):
                value = _element_value(match, "image")
                if value and not value.startswith("data:"):
                    out.append((value, f"dom:{query.split(',')[0]}"))
            if out:
                break
        return out

    def _url(self, page: PageContext, f: SchemaField) -> list[tuple[Any, str]]:
        if page.scope is not None:
            for match in page.root.css("a[href]"):
                href = match.attr("href")
                if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                    return [(match.urljoin(href), "dom:a[href]")]
            return []
        canonical = page.root.css("link[rel=canonical]")
        if canonical and canonical[0].attr("href"):
            return [(canonical[0].urljoin(canonical[0].attr("href") or ""), "dom:link[rel=canonical]")]
        return [(page.url, "page url")] if page.url else []

    def _date(self, page: PageContext, f: SchemaField) -> list[tuple[Any, str]]:
        out = [
            (t.attr("datetime"), "dom:time[datetime]") for t in page.root.css("time[datetime]") if t.attr("datetime")
        ]
        if not out:
            out = self._texts(page, "date", "[class*=date]", max_len=60)
        return out

    def _author(self, page: PageContext, f: SchemaField) -> list[tuple[Any, str]]:
        out = []
        for value, source in self._texts(page, "author", "[class*=author]", max_len=100):
            cleaned = re.sub(r"^\s*(?:by|von|par|por|di)\s+", "", value, flags=re.I).strip()
            if cleaned:
                out.append((cleaned, source))
        return out

    def _email(self, page: PageContext, f: SchemaField) -> list[tuple[Any, str]]:
        return [
            ((m.attr("href") or "")[7:].split("?")[0], "dom:a[href^=mailto]")
            for m in page.root.css("a[href^='mailto:']")
            if m.attr("href")
        ]

    def _phone(self, page: PageContext, f: SchemaField) -> list[tuple[Any, str]]:
        links = page.root.css("a[href^='tel:']")
        return [((m.attr("href") or "")[4:], "dom:a[href^=tel]") for m in links if m.attr("href")]

    def _address(self, page: PageContext, f: SchemaField) -> list[tuple[Any, str]]:
        return [(a.text, "dom:address") for a in page.root.css("address") if a.text]

    def _description(self, page: PageContext, f: SchemaField) -> list[tuple[Any, str]]:
        found = self._texts(page, "description", "[class*=description]", max_len=20_000)
        return sorted(found, key=lambda pair: -len(pair[0]))[:1]

    def _sku(self, page: PageContext, f: SchemaField) -> list[tuple[Any, str]]:
        out = []
        for value, source in self._texts(page, "sku", "[class*=sku]", max_len=60):
            cleaned = re.sub(
                r"^\s*(?:sku|mpn|model|item|part|art(?:icle)?)(?:\s*(?:no\.?|number|#))?\s*[:#]?\s*",
                "",
                value,
                flags=re.I,
            )
            if cleaned:
                out.append((cleaned.strip(), source))
        return out

    def _brand(self, page: PageContext, f: SchemaField) -> list[tuple[Any, str]]:
        out = []
        for value, source in self._texts(page, "brand", "[class*=brand]", max_len=80):
            cleaned = re.sub(r"^\s*(?:brand|by|marke|marque|marca)\s*[:]?\s*", "", value, flags=re.I).strip()
            if cleaned:
                out.append((cleaned, source))
        return out

    def _category(self, page: PageContext, f: SchemaField) -> list[tuple[Any, str]]:
        links = [a for a in page.root.xpath(_BREADCRUMB_LINKS) if a.text]
        if len(links) >= 2 and _is_this_page(page, links[-1]):
            links.pop()  # the page itself, linked: its category is the crumb before it
        crumbs = [a.text for a in links]
        if len(crumbs) >= 2:  # the last link is the most specific category (the first is the home page)
            return [(crumbs if f.many else crumbs[-1], "dom:breadcrumb")]
        return []


def _is_this_page(page: PageContext, link: Selector) -> bool:
    """Whether a breadcrumb's link is the page itself: it says so (``aria-current``), leads to this
    page, or names what the page's heading names (a crumb may shorten it: "A Light in the...")."""
    if (link.attr("aria-current") or "").strip().lower() in ("page", "true", "location"):
        return True
    href = (link.attr("href") or "").strip()
    if href and not href.startswith("#") and page.url:  # "#" is a placeholder, not this page
        there, here = urlsplit(link.urljoin(href)), urlsplit(page.url)
        if (there.netloc, there.path.rstrip("/")) == (here.netloc, here.path.rstrip("/")):
            return True
    heading = page.root.css("h1")
    name = " ".join((heading[0].text or "").split()).casefold() if heading else ""
    text = " ".join((link.text or "").split()).casefold().rstrip(".…")
    return bool(name and text) and (text == name or (len(text) >= 8 and name.startswith(text)))


# --------------------------------------------------------------------------- #
# text patterns
# --------------------------------------------------------------------------- #
_SIGN_BEFORE = ("US$", "CA$", "AU$", "NZ$", "HK$", "S$", "$", "€", "£", "¥", "₹", "₩", "₽", "₺", "₪", "R$", "Rs.", "Rs",
                "CHF", "USD", "EUR", "GBP", "INR", "JPY")  # fmt: skip
_SIGN_AFTER = r"(?:€|EUR|USD|GBP|kr|zł|Kč|Ft|lei|CHF|₹|₽)"
# "$ 12.99", "12,99 €". Every alternative starts with a literal character, so the regular expression
# engine skips to the characters a price can start with instead of trying every position of the text.
_MONEY_TEXT = re.compile(
    "|".join(
        [re.escape(sign) + r"\s?\d[\d.,'\s]{0,14}\d?" for sign in _SIGN_BEFORE]
        + [digit + r"[\d.,'\s]{0,14}\d?\s?" + _SIGN_AFTER for digit in "0123456789"]
    )
)
_EMAIL_TEXT = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_TEXT = re.compile(r"(?<![\w/])\+?\(?\d[\d\s().-]{6,18}\d(?![\w/])")
_SKU_TEXT = re.compile(
    r"\b(?:SKU|MPN|Item\s*(?:No\.?|#|number)|Model(?:\s*(?:No\.?|#|number))?|Part\s*(?:No\.?|#|number)|Art(?:ikel)?\.?\s*-?\s*Nr\.?)"
    r"\s*[:#]?\s*([A-Z0-9][A-Z0-9\-_/.]{2,30})",
    re.I,
)
# A rating written in class names: a number word ("star-rating Three"), or digits after "rating"/"stars"
# ("rating-4", "stars-4-5" for 4.5). Other digits in classes ("col-md-4") are layout, not ratings.
_CLASS_RATING_WORD = re.compile(r"(?<![\w-])(zero|one|two|three|four|five)(?![\w-])", re.I)
_CLASS_RATING_DIGITS = re.compile(r"(?<![\w-])(?:rating|stars?|rated)[-_]([0-5])(?:[-_]([05]))?(?![\w-])", re.I)


def _class_rating(classes: str) -> str | None:
    word = _CLASS_RATING_WORD.search(classes)
    if word:
        return word.group(1).lower()
    digits = _CLASS_RATING_DIGITS.search(classes)
    if digits:
        return digits.group(1) + (f".{digits.group(2)}" if digits.group(2) else "")
    return None


# "3 bedrooms", "2 bd", "1.5 baths", "2,100 sq ft", "96 m²" (for fields named bedrooms, bathrooms, floor_size...)
_BEDROOMS = re.compile(r"\b(\d{1,2})\s*-?\s*(?:bed(?:room)?s?|bd|br)\b", re.I)
_BATHROOMS = re.compile(r"\b(\d{1,2}(?:[.,]5)?)\s*-?\s*(?:bath(?:room)?s?|ba)\b", re.I)
_FLOOR_AREA = re.compile(
    r"\b\d[\d,.]*\s*(?:sq\.?\s?ft|sqft|square\s+(?:feet|foot|metres|meters)|m²|m2|sqm)(?!\w)", re.I
)
_RATING_TEXT = re.compile(r"\b\d(?:[.,]\d{1,2})?\s*(?:/|out of|von|sur|de|su)\s*(?:5|10|100)\b", re.I)
_AVAILABILITY_TEXT = re.compile(
    r"\b(?:in stock|out of stock|sold out|pre-?order|back-?order|only \d+ left(?: in stock)?|currently unavailable|"
    r"auf lager|ausverkauft|en stock|rupture de stock|agotado|disponible)\b",
    re.I,
)


class Patterns(Strategy):
    """Regular expressions over the visible text, for values with a recognisable shape.

    Prices with a currency sign, e-mail addresses, phone numbers, ``SKU: ...``,
    ``4.5 out of 5``, ``(123 reviews)``, stock phrases. On a whole page such
    patterns find many things, so several different matches lower confidence
    sharply; inside one listing record they are much more telling.
    """

    method = "pattern"

    def candidates(self, page: PageContext, f: SchemaField, schema: Schema) -> list[Candidate]:
        kind = field_kind(f)
        text = page.text
        if not text:
            return []
        found: list[str] = []
        label = kind
        if kind == "price":
            found = [m.group(0).strip() for m in _MONEY_TEXT.finditer(text)]
        elif kind == "email":
            found = _EMAIL_TEXT.findall(text) if "@" in text else []
        elif kind == "phone":
            country = f.country or None
            found = [m.group(0) for m in _PHONE_TEXT.finditer(text) if normalize_phone(m.group(0), country=country)]
        elif kind == "sku":
            found = [m.group(1) for m in _SKU_TEXT.finditer(text)]
        elif kind == "rating":
            found = [m.group(0) for m in _RATING_TEXT.finditer(text)]
        elif kind == "review_count":
            found = [m.group(1).strip() for m in _REVIEW_COUNT.finditer(text)]
        elif kind == "availability":
            found = [m.group(0) for m in _AVAILABILITY_TEXT.finditer(text)]
        elif kind == "currency" and page.scope is not None:
            found = [m.group(0) for m in _MONEY_TEXT.finditer(text)]
            label = "price"
        elif kind in ("bedrooms", "bathrooms"):
            found = [
                m.group(1).replace(",", ".") for m in (_BEDROOMS if kind == "bedrooms" else _BATHROOMS).finditer(text)
            ]
        elif kind == "floor_area":
            found = [m.group(0) for m in _FLOOR_AREA.finditer(text)]
        if not found:
            return []
        if f.many:
            return [Candidate(list(dict.fromkeys(found))[:50], self.method, f"pattern:{label}")]
        distinct = _distinct([(v, f"pattern:{label}") for v in found])
        detail = f"{len(distinct)} different matches in the text" if len(distinct) > 1 else ""
        # A page mentions many prices (related products, shipping...): only a lone match says much.
        spread = _uniqueness(len(distinct)) ** (2 if page.scope is None else 1)
        return [
            Candidate(v, self.method, s, spread * (1.0 if i == 0 else 0.8), detail) for i, (v, s) in enumerate(distinct)
        ]


# --------------------------------------------------------------------------- #
# repeating records
# --------------------------------------------------------------------------- #
_RECORD_FIELD_KINDS = {"title": "title", "name": "title", "url": "url", "link": "url", "image": "image", "price": "price",
                       "rating": "rating", "reviews": "review_count", "date": "date", "author": "author",
                       "description": "description", "text": "description"}  # fmt: skip


class RecordFields(Strategy):
    """For a record of a listing found by :func:`~wintergrab.parser.autoextract.detect_records`: its inferred fields.

    The detector names what it finds (``title``, ``url``, ``image``, ``price``,
    ``rating``...); a schema field takes the one with its name or its kind.
    """

    method = "records"

    def __init__(self, fields: dict[str, str]) -> None:
        self.fields = fields  # detected name -> selector relative to the record

    def candidates(self, page: PageContext, f: SchemaField, schema: Schema) -> list[Candidate]:
        if page.scope is None:
            return []
        names = set(candidate_names(f.name, f.aliases))
        kind = field_kind(f)
        for detected, query in self.fields.items():
            key = field_key(detected)
            if key in names or _RECORD_FIELD_KINDS.get(key.split("_")[0]) == kind:
                try:
                    matches = page.root.select(query)
                except Exception:
                    continue
                values = [v for v in (_element_value(m, kind) for m in matches) if v not in (None, "")]
                if values:
                    return [Candidate(values if f.many else values[0], self.method, f"records:{detected}={query}")]
        return []


#: The default strategies, in hierarchy order.
#: The strategies extractors use by default, in priority order (plugins add theirs with
#: :func:`register_strategy`).
STRATEGIES: list[type[Strategy]] = [
    StructuredData,
    MetaTags,
    Selectors,
    EmbeddedJson,
    LabelledValues,
    DomHeuristics,
    Patterns,
]


def register_strategy(strategy: type[Strategy], *, before: str | None = None) -> None:
    """Add a strategy extractors use by default: last, or before the one whose ``method`` is ``before``.

    A strategy's :meth:`Strategy.candidates` gives candidate values for a field; its ``method``
    names it in provenance, and its confidence comes from ``Extractor(priors={method: ...})``
    (0.5 when not given).
    """
    if not (isinstance(strategy, type) and issubclass(strategy, Strategy)):
        raise TypeError(f"a strategy is a Strategy subclass, not {strategy!r}")
    if strategy in STRATEGIES:
        return
    methods = [s.method for s in STRATEGIES]
    if before is not None and before not in methods:
        raise ValueError(f"no strategy {before!r} (strategies: {', '.join(methods)})")
    STRATEGIES.insert(methods.index(before) if before is not None else len(STRATEGIES), strategy)


def numbers_in(text: str) -> set[str]:
    """Every number in ``text``, as normalized decimal strings (for grounding model output)."""
    return {str(value.normalize()) for value, *_ in iter_numbers(text)}
