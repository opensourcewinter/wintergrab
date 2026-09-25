"""Everything a detail page says about its main item, as one flat, typed record.

:func:`extract_details` merges, most trusted first:

1. schema.org data the page declares (JSON-LD or microdata ``Product``, ``Book``,
   ``Recipe``, ``Event``, ``Article``... with offers, ratings, brand, identifiers);
2. the visible block around the page's ``<h1>`` (price, availability, rating, image);
3. label/value pairs: two-column tables, ``<dl>`` lists and ``Label: value`` list items;
4. breadcrumbs (``category`` and the ``breadcrumbs`` path);
5. the description, and meta tags as a last resort.

A value from a more trusted source is never overwritten. The result goes through
:func:`~wintergrab.parser.normalize.clean_record`, so prices are numbers with a
``currency``, stock is ``in_stock``/``stock``, and so on.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from lxml import etree

from .normalize import clean_record
from .structured import structured_data
from .text import normalize_space, own_text, tag_name, text_content

__all__ = ["extract_details"]

_MAIN_TYPES = (
    "Product", "Book", "IndividualProduct", "ProductModel", "ProductGroup", "Vehicle", "Car",
    "SoftwareApplication", "MobileApplication", "VideoGame", "Recipe", "Event", "JobPosting", "Course",
    "Movie", "TVSeries", "MusicAlbum", "Hotel", "Restaurant", "LocalBusiness", "Place", "Offer",
    "Article", "NewsArticle", "BlogPosting", "Review", "Person", "Organization",
)  # fmt: skip
_SKIP_PROPS = frozenset(
    {"@context", "@id", "@type", "mainEntityOfPage", "potentialAction", "review", "reviews", "hasVariant",
     "isPartOf", "breadcrumb", "subjectOf", "workExample", "sameAs", "url"}
)  # fmt: skip
_NAME_PROPS = frozenset({"brand", "author", "publisher", "manufacturer", "creator", "seller", "organizer", "performer"})
_OUTSIDE = frozenset({"header", "nav", "footer", "aside", "menu", "form", "script", "style", "noscript", "template"})
_OLD_PRICE_RE = re.compile(r"old|was|regular|compare|strike|before|original|rrp|msrp|list", re.I)
_PRICE_TEXT_RE = re.compile(
    r"(?:US\$|C\$|A\$|R\$|[$€£¥₹₩₽₺₪฿₫]|\b(?:USD|EUR|GBP|JPY|INR|CAD|AUD|CHF|CNY)\b)\s?\d|\d[\d,.]*\s?(?:[€£¥₹₽₺₪]|\bEUR\b|\bUSD\b)"
)
_STOCK_TEXT_RE = re.compile(r"in[\s-]*stock|out[\s-]*of[\s-]*stock|sold[\s-]*out|\b(?:un)?availab|pre-?order", re.I)
_STOCK_CLASS_RE = re.compile(r"availab|stock", re.I)
_RATING_CLASS_RE = re.compile(r"rating|(?:^|[\s_-])stars?(?:$|[\s_-])", re.I)
_DESCRIPTION_RE = re.compile(r"description|synopsis|summary|about|overview", re.I)
_LABEL_VALUE_RE = re.compile(r"^\s*([^:\uff1a]{1,40}?)\s*[:\uff1a]\s*(.{1,300}?)\s*$", re.S)
_MAX_PAIRS = 100


def extract_details(root: Any, base_url: str | None = None, *, clean: bool = True) -> dict[str, Any]:
    """One flat record describing the page's main item (a product, article, job, event...).

    Args:
        root: The page (Response, Selector, lxml element or HTML).
        base_url: Page URL, for making links absolute (defaults to the page's own URL).
        clean: Type the values (see :func:`~wintergrab.parser.normalize.clean_record`).
            ``False`` keeps the raw text.

    Example::

        wintergrab.get("https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html").extract_details()
        # {"title": "A Light in the Attic", "price": 51.77, "currency": "GBP", "availability": ...,
        #  "in_stock": True, "stock": 22, "rating": 3.0, "upc": "a897fe39b1053632", ...,
        #  "category": "Poetry", "breadcrumbs": "Home > Books > Poetry", "description": ..., "url": ...}
    """
    from .autoextract import _document_base, _resolve  # the same page-resolution rules

    doc, url = _resolve(root)
    top = doc.getroottree().getroot()
    base = _document_base(top, base_url or url)
    data = structured_data(top, base)
    record: dict[str, Any] = {}

    item = _main_item(data)
    if item is not None:
        _fill(record, _flatten_item(item, base))
    h1 = _main_heading(top)
    if h1 is not None:
        _fill(record, _visible_block(h1, base))
    _fill(record, _pairs(top))
    crumbs = _breadcrumbs(top, data, record.get("title"))
    if crumbs:
        _fill(record, {"category": crumbs[-1], "breadcrumbs": " > ".join(crumbs)})
    _fill(record, {"description": _description(top)})
    og, meta = data["opengraph"], data["meta"]
    _fill(
        record,
        {
            "title": _first(og.get("title")) or _site_less(meta.get("title")),
            "description": _first(og.get("description")) or meta.get("description"),
            "image": _first(og.get("image")),
        },
    )
    page_url = meta.get("canonical") or (base_url or url)
    if page_url:
        record["url"] = page_url
    record = {k: v for k, v in record.items() if v not in (None, "", [], {})}
    record = clean_record(record) if clean else record
    last = [k for k in ("description", "image", "url") if k in record]  # long values last
    return {**{k: v for k, v in record.items() if k not in last}, **{k: record[k] for k in last}}


# --------------------------------------------------------------------------- #
# merging helpers
# --------------------------------------------------------------------------- #


def _fill(record: dict[str, Any], values: Mapping[str, Any]) -> None:
    """Add values whose key is missing or empty; never overwrite a more trusted source."""
    for key, value in values.items():
        if value in (None, "", [], {}):
            continue
        if record.get(key) in (None, "", [], {}):
            record[key] = value


def _first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _snake(label: str) -> str:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", label)
    name = re.sub(r"[^0-9A-Za-z]+", "_", name).strip("_").lower()
    return name[:40].rstrip("_")


def _site_less(title: Any) -> str | None:
    """``"A Light in the Attic | Books to Scrape"`` -> ``"A Light in the Attic"``."""
    if not isinstance(title, str):
        return None
    parts = re.split(r"\s+[|\u2013\u2014-]\s+", title.strip())
    return parts[0] if parts and parts[0] else title.strip() or None


# --------------------------------------------------------------------------- #
# 1. schema.org
# --------------------------------------------------------------------------- #


def _types(item: Mapping[str, Any]) -> list[str]:
    raw = item.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    return [str(v).rsplit("/", 1)[-1] for v in values if v]


def _main_item(data: Mapping[str, Any]) -> dict[str, Any] | None:
    items = [i for i in [*data["json_ld"], *data["microdata"]] if isinstance(i, dict)]
    for wanted in _MAIN_TYPES:
        for item in items:
            if wanted in _types(item):
                return item
    return None


def _name(value: Any) -> str | None:
    if isinstance(value, list):
        names = [n for n in (_name(v) for v in value) if n]
        return ", ".join(names) or None
    if isinstance(value, Mapping):
        return _name(value.get("name") or value.get("legalName"))
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return normalize_space(str(value)) or None
    return None


def _url_of(value: Any, base: str | None) -> str | None:
    from .autoextract import _join

    value = _first(value)
    if isinstance(value, Mapping):
        value = value.get("url") or value.get("contentUrl") or value.get("@id")
    return _join(base, value.strip()) if isinstance(value, str) and value.strip() else None


def _availability_text(value: Any) -> str | None:
    """``"https://schema.org/InStock"`` -> ``"In stock"``."""
    if not isinstance(value, str) or not value.strip():
        return None
    word = value.strip().rsplit("/", 1)[-1]
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", word)
    return spaced[:1].upper() + spaced[1:].lower()


def _flatten_item(item: Mapping[str, Any], base: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    title = _name(item.get("name")) or _name(item.get("headline"))
    if title:
        out["title"] = title
    offers = item.get("offers")
    offer = _first(offers)
    if isinstance(offer, Mapping):
        price = offer.get("price", offer.get("lowPrice"))
        if price is None and isinstance(offer.get("priceSpecification"), Mapping):
            price = offer["priceSpecification"].get("price")
        out["price"] = price
        out["currency"] = offer.get("priceCurrency") or (
            offer["priceSpecification"].get("priceCurrency")
            if isinstance(offer.get("priceSpecification"), Mapping)
            else None
        )
        if offer.get("highPrice") is not None:
            out["price_max"] = offer.get("highPrice")
        out["availability"] = _availability_text(offer.get("availability"))
        out["seller"] = _name(offer.get("seller"))
    rating = item.get("aggregateRating")
    if isinstance(rating, Mapping):
        out["rating"] = rating.get("ratingValue")
        out["review_count"] = rating.get("reviewCount", rating.get("ratingCount"))
    out["image"] = _url_of(item.get("image"), base)
    for key, value in item.items():
        if key in _SKIP_PROPS or key in ("name", "headline", "offers", "aggregateRating", "image"):
            continue
        name = _snake(key)
        if not name or name in out:
            continue
        if key in _NAME_PROPS:
            out[name] = _name(value)
        elif key == "address" and isinstance(value, Mapping):
            parts = [_name(value.get(k)) for k in ("streetAddress", "addressLocality", "addressRegion",
                                                  "postalCode", "addressCountry")]  # fmt: skip
            out["address"] = ", ".join(p for p in parts if p) or None
        elif isinstance(value, bool):
            out[name] = value
        elif isinstance(value, (str, int, float)):
            out[name] = normalize_space(value) if isinstance(value, str) else value
        elif isinstance(value, list) and value and all(isinstance(v, (str, int, float)) for v in value):
            out[name] = ", ".join(normalize_space(str(v)) for v in value)
    return out


# --------------------------------------------------------------------------- #
# 2. the visible block around <h1>
# --------------------------------------------------------------------------- #


def _outside_content(el: etree._Element) -> bool:
    return any(tag_name(anc) in _OUTSIDE for anc in el.iterancestors())


def _main_heading(root: etree._Element) -> etree._Element | None:
    for h1 in root.iter("h1"):
        if text_content(h1) and not _outside_content(h1):
            return h1
    return None


def _has_price(el: etree._Element) -> bool:
    return bool(_PRICE_TEXT_RE.search(text_content(el)))


def _is_old_price(el: etree._Element) -> bool:
    for node in (el, *el.iterancestors()):
        if tag_name(node) in ("del", "s", "strike"):
            return True
        if _OLD_PRICE_RE.search(node.get("class") or "") and "price" in (node.get("class") or "").lower():
            return True
        if tag_name(node) in ("body", "html"):
            break
    return False


def _visible_block(h1: etree._Element, base: str | None) -> dict[str, Any]:
    from .autoextract import _join

    out: dict[str, Any] = {"title": text_content(h1)}
    scope = h1
    for anc in list(h1.iterancestors())[:4]:  # the smallest block around the title that shows a price
        scope = anc
        if _has_price(anc):
            break
    elements = [e for e in scope.iter() if isinstance(e.tag, str) and not _outside_content(e)]

    # A price element's own text first (so "<p class=price><s>$120</s> <b>$89</b></p>" gives $89,
    # never the container's "$120 $89"); then whole price-classed elements (split into spans).
    candidates = [(el, own_text(el)) for el in elements] + [
        (el, text_content(el)) for el in elements if "price" in (el.get("class") or "").lower()
    ]
    for el, text in candidates:
        if 0 < len(text) <= 40 and _PRICE_TEXT_RE.search(text) and not _is_old_price(el):
            cls = f"{el.get('class') or ''} {el.get('itemprop') or ''}".lower()
            if "price" in cls:
                out["price"] = text
                break
            out.setdefault("price", text)  # unlabelled: keep looking for a price-classed one
    for el in elements:
        text = text_content(el)
        if 0 < len(text) <= 80 and (
            _STOCK_CLASS_RE.search(el.get("class") or "") or (_STOCK_TEXT_RE.search(text) and len(el) <= 2)
        ):
            out["availability"] = text
            break
    for el in elements:
        cls = el.get("class") or ""
        if _RATING_CLASS_RE.search(cls):
            out["rating"] = el.get("aria-label") or el.get("title") or (cls if len(cls.split()) > 1 else "")
            out["rating"] = out["rating"] or text_content(el)
            break
    # the main image sits near the title block (a gallery column beside it, for instance)
    for anc in [scope, *list(scope.iterancestors())[:3]]:
        for img in anc.iter("img"):
            src = img.get("data-src") or img.get("src") or ""
            hint = f"{img.get('class') or ''} {img.get('alt') or ''} {src}".lower()
            if src and not src.startswith("data:") and not re.search(r"logo|icon|sprite|avatar|badge", hint):
                out["image"] = _join(base, src)
                break
        if "image" in out:
            break
    return out


# --------------------------------------------------------------------------- #
# 3. label/value pairs
# --------------------------------------------------------------------------- #


def _pairs(root: etree._Element) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def add(label: str, value: str) -> None:
        key, value = _snake(label), normalize_space(value)
        if key and value and key not in out and len(out) < _MAX_PAIRS:
            out[key] = value

    for table in root.iter("table"):
        if _outside_content(table):
            continue
        rows = [[c for c in tr if tag_name(c) in ("th", "td")] for tr in table.iter("tr")]
        two = [r for r in rows if len(r) == 2 and 0 < len(text_content(r[0])) <= 40]
        if len(two) >= 2 and len(two) >= 0.8 * len(rows):
            for label, value in two:
                add(text_content(label), text_content(value))
    for dl in root.iter("dl"):
        if _outside_content(dl):
            continue
        label = None
        for child in dl:
            if tag_name(child) == "dt":
                label = text_content(child)
            elif tag_name(child) == "dd" and label:
                add(label, text_content(child))
                label = None
    for lst in (*root.iter("ul"), *root.iter("ol")):
        if _outside_content(lst):
            continue
        matches = [_LABEL_VALUE_RE.match(text_content(li)) for li in lst if tag_name(li) == "li"]
        found = [m for m in matches if m]
        if len(found) >= 3 and len(found) >= 0.6 * len(matches):
            for m in found:
                add(m.group(1), m.group(2))
    return out


# --------------------------------------------------------------------------- #
# 4. breadcrumbs, 5. description
# --------------------------------------------------------------------------- #


def _breadcrumbs(root: etree._Element, data: Mapping[str, Any], title: Any) -> list[str]:
    crumbs: list[str] = []
    for item in [*data["json_ld"], *data["microdata"]]:
        if isinstance(item, Mapping) and "BreadcrumbList" in _types(item):
            elements = item.get("itemListElement")
            entries = elements if isinstance(elements, list) else [elements]
            ranked = sorted(
                (e for e in entries if isinstance(e, Mapping)),
                key=lambda e: float(e.get("position") or 0) if str(e.get("position") or "0").isdigit() else 0,
            )
            crumbs = [n for n in (_name(e.get("name") or e.get("item")) for e in ranked) if n]
            if crumbs:
                break
    if not crumbs:
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            marker = f"{el.get('class') or ''} {el.get('id') or ''} {el.get('aria-label') or ''}".lower()
            if "breadcrumb" in marker and tag_name(el) in ("ol", "ul", "nav", "div"):
                items = [text_content(li) for li in el.iter("li")] or [text_content(a) for a in el.iter("a")]
                crumbs = [c for c in (normalize_space(i).strip("/>\u203a\u00bb| ") for i in items) if c]
                if crumbs:
                    break
    if crumbs and isinstance(title, str) and normalize_space(crumbs[-1]) == normalize_space(title):
        crumbs = crumbs[:-1]
    return crumbs


def _description(root: etree._Element) -> str | None:
    for el in root.xpath("//*[@itemprop='description']"):
        text = text_content(el)
        if len(text) >= 20:
            return text
    candidates: Iterable[etree._Element] = (
        el
        for el in root.iter()
        if isinstance(el.tag, str)
        and not _outside_content(el)
        and (
            _DESCRIPTION_RE.search(f"{el.get('id') or ''} {el.get('class') or ''}")
            or (tag_name(el) in ("h2", "h3", "h4") and _DESCRIPTION_RE.search(text_content(el)))
        )
    )
    for el in candidates:
        text = text_content(el)
        if len(text) >= 60 and tag_name(el) not in ("h2", "h3", "h4"):
            return text
        # a heading ("Product Description"), or a block holding only one: the text follows it
        sibling = el.getnext()
        while sibling is not None and not isinstance(sibling.tag, str):
            sibling = sibling.getnext()
        if sibling is not None and len(text_content(sibling)) >= 20:
            return text_content(sibling)
    return None
