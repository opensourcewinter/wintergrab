"""What a page looked like when it was fetched: a handful of comparable fingerprints.

A :class:`PageSnapshot` keeps hashes, not the page (the HTML is kept only on
request), so a history of millions of pages stays small. Comparing two
snapshots of a URL (:func:`compare_snapshots`) says what changed: the text,
the title, the metadata, the structured data and its types, the price and
availability (from schema.org or OpenGraph data), the layout, the images,
the navigation, or the items extracted from the page.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, fields
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from ..data.normalize import normalize_availability, parse_money
from ..data.similarity import hamming, simhash, simhash_similarity
from ..extraction.page import STRUCTURED_KEYS, STRUCTURED_KINDS, PageContext, schema_types
from ..extraction.schemaorg import read_path
from ..parser.text import tag_name

__all__ = ["PageChange", "PageSnapshot", "compare_snapshots", "snapshot_page"]

_SKIP = frozenset({"script", "style", "noscript", "template", "svg", "iframe"})
_NAVIGATION = frozenset({"nav", "header", "footer"})
_VOLATILE_META = ("token", "nonce", "csrf", "request-id", "request_id", "build", "generated", "timestamp", "date")
_PRICE_PATHS = ("offers.price", "offers.lowPrice", "offers.priceSpecification.price", "price")
_CURRENCY_PATHS = ("offers.priceCurrency", "offers.priceSpecification.priceCurrency", "priceCurrency")
_AVAILABILITY_PATHS = ("offers.availability", "availability")
_LAYOUT_BITS = 8  # layout SimHashes this many bits apart (or more) count as a changed layout
_SIMILARITY_TEXT = 50_000  # characters of text the similarity estimate reads (the hash reads all of it)
_MAX_DEPTH = 14


def _digest(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.blake2b(text.encode("utf-8"), digest_size=12).hexdigest()


@dataclass
class PageSnapshot:
    """Fingerprints of one fetch of a page.

    Attributes:
        url: The page.
        status: Its HTTP status.
        fetched_at: When (seconds since the epoch).
        body: Hash of the exact body.
        text: Hash of the visible text (whitespace aside); ``text_simhash`` tells how much it changed.
        title, description: The page's title and meta description.
        meta: Hash of the other ``<meta>`` tags (tokens, nonces and timestamps left out).
        structured: Hash of the JSON-LD and microdata; ``types`` their schema.org types.
        price, currency, availability: From the page's schema.org or OpenGraph product data.
        layout: SimHash of the page's tag structure (no text): templates changes move it.
        images, image_count: Hash and number of the image URLs.
        navigation, nav_count: Hash and number of the links in ``<nav>``, ``<header>``, ``<footer>``.
        items, item_count: Hash and number of the items extracted from the page, when known.
        etag, last_modified: The validators the server sent.
    """

    url: str
    status: int = 200
    fetched_at: float = 0.0
    body: str = ""
    text: str = ""
    text_simhash: int = 0
    text_length: int = 0
    title: str | None = None
    description: str | None = None
    meta: str = ""
    structured: str | None = None
    types: list[str] = field(default_factory=list)
    price: float | None = None
    currency: str | None = None
    availability: str | None = None
    layout: int = 0
    images: str = ""
    image_count: int = 0
    navigation: str = ""
    nav_count: int = 0
    items: str | None = None
    item_count: int = 0
    etag: str | None = None
    last_modified: str | None = None

    @property
    def content(self) -> str:
        """Hash of what the page says (text, title, description, structured data, price, availability,
        items), leaving out layout and markup: what freshness tracking calls a change."""
        return _digest(
            [self.text, self.title, self.description, self.structured, self.price, self.availability, self.items]
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PageSnapshot:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class PageChange:
    """What changed on a page between two snapshots.

    Attributes:
        url: The page.
        kinds: What changed, among ``status``, ``text``, ``title``, ``description``, ``metadata``,
            ``structured-data``, ``schema``, ``price``, ``availability``, ``layout``, ``images``,
            ``navigation`` and ``items``.
        details: Before and after for the values (``{"price": [299.0, 279.0]}``), and how similar
            the text and the layout still are (``text_similarity``, ``layout_similarity``).
    """

    url: str
    kinds: list[str]
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"url": self.url, "kinds": list(self.kinds), "details": dict(self.details)}

    def __str__(self) -> str:
        parts = []
        for kind in self.kinds:
            value = self.details.get(kind)
            if isinstance(value, list) and len(value) == 2:
                parts.append(f"{kind} {value[0]} -> {value[1]}")
            else:
                parts.append(kind)
        return f"{self.url}: {', '.join(parts)}"


def _items_digest(items: Iterable[Any]) -> tuple[str | None, int]:
    rows = []
    for item in items:
        data = item.to_dict() if hasattr(item, "to_dict") else item
        if isinstance(data, Mapping):
            data = {k: v for k, v in data.items() if not str(k).startswith("_")}  # not metadata
        rows.append(_digest(data))
    if not rows:
        return None, 0
    return _digest(sorted(rows)), len(rows)


def _offer(ctx: PageContext) -> tuple[float | None, str | None, str | None]:
    """The price, currency and availability in the page's product data."""
    price = currency = availability = None
    nodes = [node for kind in STRUCTURED_KINDS for _, node in ctx.nodes(kind)]
    products = [n for n in nodes if {"Product", "ProductGroup", "IndividualProduct", "Offer"} & set(schema_types(n))]
    for node in products:
        for path in _PRICE_PATHS:
            for value in read_path(node, path):
                money = parse_money(str(value))
                if money is not None and price is None:
                    price, currency = float(money.amount), money.currency
        for path in _CURRENCY_PATHS:
            for value in read_path(node, path):
                if currency is None and isinstance(value, str) and len(value.strip()) == 3:
                    currency = value.strip().upper()
        for path in _AVAILABILITY_PATHS:
            for value in read_path(node, path):
                availability = availability or normalize_availability(str(value))
        if price is not None:
            break
    og = ctx.structured.get("opengraph") or {}
    if price is None:
        amount = og.get("product:price:amount") or og.get("og:price:amount")
        money = parse_money(str(amount)) if amount else None
        if money is not None:
            price, currency = float(money.amount), money.currency or og.get("product:price:currency")
    if availability is None and og.get("product:availability"):
        availability = normalize_availability(str(og["product:availability"]))
    return price, currency, availability


def _absolute(base: str | None, href: str, *, query: bool = True) -> str:
    url = urljoin(base or "", href.strip())
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query if query else "", ""))


def snapshot_page(
    page: Any,
    *,
    url: str | None = None,
    status: int | None = None,
    items: Iterable[Any] | None = None,
    fetched_at: float | None = None,
) -> PageSnapshot:
    """Fingerprints of a page (a :class:`~wintergrab.Response`, a Selector or HTML text).

    ``items`` are the records extracted from it, if you want their changes tracked too.
    """
    ctx = page if isinstance(page, PageContext) else PageContext(page, url=url)
    response = ctx.response
    root = ctx.selector.root
    meta = ctx.structured.get("meta") or {}
    text = " ".join(ctx.text.split())
    body = response.body if response is not None else (ctx.selector.html or "").encode("utf-8")
    snap = PageSnapshot(
        url=url or ctx.url or "",
        status=status if status is not None else (response.status if response is not None else 200),
        fetched_at=fetched_at if fetched_at is not None else time.time(),
        body=hashlib.blake2b(body, digest_size=12).hexdigest(),
        text=_digest(text),
        text_simhash=simhash(text[:_SIMILARITY_TEXT]) if text else 0,
        text_length=len(text),
        title=(str(meta.get("title")).strip() or None) if meta.get("title") else None,
        description=(str(meta.get("description")).strip() or None) if meta.get("description") else None,
    )
    if response is not None:
        snap.etag = response.headers.get("etag")
        snap.last_modified = response.headers.get("last-modified")
    structured = [ctx.structured.get(key) or [] for key in STRUCTURED_KEYS.values()]
    if any(structured):
        snap.structured = _digest(structured[:2] if not structured[2] else structured)  # (as before, without RDFa)
        snap.types = sorted({t for kind in STRUCTURED_KINDS for _, n in ctx.nodes(kind) for t in schema_types(n)})
    snap.price, snap.currency, snap.availability = _offer(ctx)
    if items is not None:
        snap.items, snap.item_count = _items_digest(items)
    if root is None:
        return snap
    metas, images, navigation, paths = [], set(), set(), set()
    stack: list[tuple[Any, str, bool, int]] = [(root, "", False, 0)]
    while stack:
        element, path, in_nav, depth = stack.pop()
        if not isinstance(element.tag, str):
            continue
        name = tag_name(element)
        if name in _SKIP:
            continue
        here = f"{path}/{name}"
        if depth <= _MAX_DEPTH:
            paths.add(here)
        in_nav = in_nav or name in _NAVIGATION or element.get("role") == "navigation"
        if name == "meta":
            key = (element.get("name") or element.get("property") or element.get("itemprop") or "").lower()
            if key and not any(word in key for word in _VOLATILE_META):
                metas.append((key, element.get("content") or ""))
        elif name == "img" and element.get("src") and not element.get("src", "").startswith("data:"):
            images.add(_absolute(ctx.url, element.get("src"), query=False))
        elif name == "a" and in_nav and element.get("href") and not element.get("href", "").startswith("#"):
            navigation.add(_absolute(ctx.url, element.get("href")))
        stack.extend((child, here, in_nav, depth + 1) for child in element)
    snap.meta = _digest(sorted(metas))
    snap.layout = simhash(paths) if paths else 0
    snap.images, snap.image_count = _digest(sorted(images)), len(images)
    snap.navigation, snap.nav_count = _digest(sorted(navigation)), len(navigation)
    return snap


def compare_snapshots(old: PageSnapshot, new: PageSnapshot) -> PageChange | None:
    """What changed from ``old`` to ``new`` (two snapshots of one URL), or ``None``."""
    kinds: list[str] = []
    details: dict[str, Any] = {}

    def changed(kind: str, before: Any, after: Any, *, show: bool = False) -> None:
        if before != after:
            kinds.append(kind)
            if show:
                details[kind] = [before, after]

    changed("status", old.status, new.status, show=True)
    if old.text != new.text:
        kinds.append("text")
        details["text_similarity"] = round(simhash_similarity(old.text_simhash, new.text_simhash), 3)
    changed("title", old.title, new.title, show=True)
    changed("description", old.description, new.description)
    changed("metadata", old.meta, new.meta)
    changed("structured-data", old.structured, new.structured)
    if old.types != new.types:
        kinds.append("schema")
        details["schema"] = {
            "added": sorted(set(new.types) - set(old.types)),
            "removed": sorted(set(old.types) - set(new.types)),
        }
    if (old.price, old.currency) != (new.price, new.currency):
        kinds.append("price")
        details["price"] = [old.price, new.price]
        if old.currency != new.currency:
            details["currency"] = [old.currency, new.currency]
    changed("availability", old.availability, new.availability, show=True)
    if hamming(old.layout, new.layout) >= _LAYOUT_BITS:
        kinds.append("layout")
        details["layout_similarity"] = round(simhash_similarity(old.layout, new.layout), 3)
    changed("images", old.images, new.images)
    if "images" in kinds:
        details["image_count"] = [old.image_count, new.image_count]
    changed("navigation", old.navigation, new.navigation)
    if old.items is not None and new.items is not None:
        changed("items", old.items, new.items)
        if "items" in kinds:
            details["item_count"] = [old.item_count, new.item_count]
    return PageChange(new.url, kinds, details) if kinds else None
