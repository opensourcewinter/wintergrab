"""Turn scraped text into typed values: prices, currencies, ratings, stock and counts.

``"£51.77"`` becomes ``51.77`` with currency ``"GBP"``, ``"star-rating Three"``
becomes ``3.0``, ``"In stock (22 available)"`` becomes ``in_stock=True, stock=22``.
:func:`clean_record` applies the right parser to each field of a record by its
name, the way :func:`~wintergrab.parser.autoextract.auto_extract` does.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, NamedTuple

from .text import normalize_space

__all__ = [
    "Availability",
    "Price",
    "clean_record",
    "parse_availability",
    "parse_count",
    "parse_number",
    "parse_price",
    "parse_rating",
]


class Price(NamedTuple):
    amount: float
    #: ISO 4217 code when the text makes it unambiguous (``£`` -> ``GBP``), otherwise the
    #: symbol as written (``$``), or ``None`` when there is none.
    currency: str | None


class Availability(NamedTuple):
    in_stock: bool | None
    stock: int | None


# --------------------------------------------------------------------------- #
# numbers
# --------------------------------------------------------------------------- #

# Spaces and apostrophes only as thousands groups ("1 234,56"), so "£5 12 items" stays 5.
_NUMBER_RE = re.compile(r"\d{1,3}(?:[\u00a0\u202f ']\d{3})+(?:[.,]\d+)?(?!\d)|\d(?:[\d,.]*\d)?")
_EURO_STYLE = frozenset({"EUR", "PLN", "CZK", "DKK", "NOK", "SEK", "BRL", "TRY", "RUB", "HUF", "IDR", "VND", "ARS"})


def _to_float(raw: str, *, euro_style: bool = False) -> float | None:
    s = re.sub(r"[\s\u00a0\u202f']", "", raw)
    if not s:
        return None
    commas, dots = s.count(","), s.count(".")
    if commas and dots:  # the later separator is the decimal one: 1,234.56 / 1.234,56
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif commas:
        tail = s.rpartition(",")[2]
        s = s.replace(",", "") if commas > 1 or len(tail) == 3 else s.replace(",", ".")  # 1,234 / 12,50
    elif dots > 1:
        s = s.replace(".", "")  # 1.234.567
    elif dots and euro_style and len(s.rpartition(".")[2]) == 3:
        s = s.replace(".", "")  # 1.234 € means one thousand two hundred ...
    try:
        return float(s)
    except ValueError:
        return None


def parse_number(text: Any) -> float | None:
    """The first number in ``text`` (``"1,234.5 sold"`` -> ``1234.5``), or ``None``."""
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return float(text)
    if not isinstance(text, str):
        return None
    match = _NUMBER_RE.search(text)
    return _to_float(match.group()) if match else None


_SCALE = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def parse_count(text: Any) -> int | None:
    """A whole count: ``"1,234 reviews"`` -> ``1234``, ``"2.5k"`` -> ``2500``, ``"No reviews"`` -> ``0``."""
    if isinstance(text, bool):
        return None
    if isinstance(text, int):
        return text
    if isinstance(text, float):
        return int(text)
    if not isinstance(text, str):
        return None
    match = re.search(r"(\d(?:[\d,.]*\d)?)\s*([kKmMbB])?\b", text)
    if match is None:
        return 0 if re.search(r"\b(?:no|none|zero)\b", text, re.I) else None
    number = match.group(1)
    scale = _SCALE.get((match.group(2) or "").lower(), 1)
    if scale == 1:
        digits = number.replace(",", "").replace(".", "") if re.fullmatch(r"\d{1,3}(?:[,.]\d{3})+", number) else number
        value = _to_float(digits)
    else:
        value = _to_float(number)
    return round(value * scale) if value is not None else None


# --------------------------------------------------------------------------- #
# prices
# --------------------------------------------------------------------------- #

# Symbols and prefixes, longest first so "US$" wins over "$".
_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("US$", "USD"), ("CA$", "CAD"), ("AU$", "AUD"), ("NZ$", "NZD"), ("HK$", "HKD"), ("MX$", "MXN"),
    ("CN¥", "CNY"), ("C$", "CAD"), ("A$", "AUD"), ("S$", "SGD"), ("R$", "BRL"), ("zł", "PLN"), ("Kč", "CZK"),
    ("€", "EUR"), ("£", "GBP"), ("₹", "INR"), ("₩", "KRW"), ("₽", "RUB"), ("₺", "TRY"), ("₪", "ILS"),
    ("฿", "THB"), ("₫", "VND"), ("₱", "PHP"), ("₦", "NGN"), ("₴", "UAH"), ("৳", "BDT"),
    ("$", "$"), ("¥", "¥"), ("Rs.", "Rs"), ("Rs", "Rs"), ("kr", "kr"),
)  # fmt: skip
_ISO_CODES = (
    "USD", "EUR", "GBP", "JPY", "CNY", "INR", "CAD", "AUD", "NZD", "CHF", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF",
    "BRL", "MXN", "ZAR", "SGD", "HKD", "KRW", "RUB", "TRY", "ILS", "THB", "VND", "PHP", "IDR", "MYR", "AED", "SAR",
    "NGN", "UAH", "ARS", "CLP", "COP", "PEN", "EGP", "PKR", "BDT", "LKR", "KES", "TWD", "RON", "BGN", "ISK",
)  # fmt: skip
_ISO_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(_ISO_CODES) + r")(?![A-Za-z])")
_FREE_RE = re.compile(r"^\s*(?:free|gratis|kostenlos|gratuit)\b", re.I)


def _currency_of(text: str) -> str | None:
    iso = _ISO_RE.search(text)
    if iso:
        return iso.group(1)
    for symbol, code in _SYMBOLS:
        if symbol in text:
            if symbol in ("kr", "Rs") and not re.search(rf"(?<![A-Za-z]){re.escape(symbol)}(?![A-Za-z])", text):
                continue  # "kr" inside a word is not a currency
            return code
    return None


def parse_price(text: Any, *, currency: str | None = None) -> Price | None:
    """``"£51.77"`` -> ``Price(51.77, "GBP")``; ``"1.234,56 €"`` -> ``Price(1234.56, "EUR")``.

    ``currency`` is used when the text has none (e.g. a page-level ``priceCurrency``).
    Ranges and "from" prices give their first amount. ``"Free"`` is ``0.0``.
    Returns ``None`` when there is no number.
    """
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return Price(float(text), currency)
    if not isinstance(text, str) or not text.strip():
        return None
    code = _currency_of(text) or currency
    if _FREE_RE.search(text):
        return Price(0.0, code)
    match = _NUMBER_RE.search(text)
    if match is None:
        return None
    amount = _to_float(match.group(), euro_style=code in _EURO_STYLE)
    if amount is None:
        return None
    if re.match(r"\s*[,.]-", text[match.end() :]):  # "12,-"
        amount = float(int(amount))
    return Price(amount, code)


# --------------------------------------------------------------------------- #
# ratings
# --------------------------------------------------------------------------- #

_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
          "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}  # fmt: skip
_OUT_OF_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:out\s+of|/|of)\s*\d+(?:[.,]\d+)?", re.I)
_STARS_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:stars?|★)", re.I)
_CLASS_RE = re.compile(r"(?:rating|stars?)[-_](\d)(?:[-_]?(\d))?(?!\d)", re.I)  # rating-4, stars-45, stars-4-5
_BARE_RE = re.compile(r"\s*(\d+(?:[.,]\d+)?)\s*")


def parse_rating(text: Any) -> float | None:
    """``"star-rating Three"`` -> ``3.0``; ``"4.5 out of 5"``, ``"4,5/5"``, ``"4.5 stars"``,
    ``"rating-45"`` (-> 4.5), ``"★★★☆☆"`` and ``"4.5"`` work too."""
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return float(text)
    if not isinstance(text, str) or not text.strip():
        return None
    for pattern in (_OUT_OF_RE, _STARS_RE):
        match = pattern.search(text)
        if match:
            return float(match.group(1).replace(",", "."))
    if "★" in text or "☆" in text:
        return float(text.count("★"))
    match = _CLASS_RE.search(text)
    if match:
        return float(f"{match.group(1)}.{match.group(2) or 0}")
    tokens = re.findall(r"[a-z]+", text.lower())
    if len(tokens) <= 4:  # class-like text ("star-rating Three"), not a sentence ("no one rated it")
        for token in tokens:
            if token in _WORDS:
                return float(_WORDS[token])
    match = _BARE_RE.fullmatch(text)
    if match:
        return float(match.group(1).replace(",", "."))
    return None


# --------------------------------------------------------------------------- #
# availability
# --------------------------------------------------------------------------- #

_OUT_RE = re.compile(
    r"out[\s-]*of[\s-]*stock|sold[\s-]*out|\bunavailable|not\s+(?:currently\s+)?available|no\s+longer\s+available|"
    r"discontinued|back[\s-]*order|pre[\s-]*order|ausverkauft|nicht\s+(?:auf\s+lager|verf[üu]gbar)|agotado|"
    r"[ée]puis[ée]|rupture\s+de\s+stock|esaurito|niet\s+op\s+voorraad",
    re.I,
)
_IN_RE = re.compile(
    r"in[\s-]*stock|\bavailable|add\s+to\s+(?:cart|basket|bag)|ships?\s+(?:today|in|within)|limited\s*availability|"
    r"online\s*only|in\s*store\s*only|auf\s+lager|\blieferbar|en\s+stock|disponible|disponibile|op\s+voorraad",
    re.I,
)
_STOCK_COUNT_RE = re.compile(
    r"(\d[\d,.]*)\s*(?:available|in\s*stock|left|remaining|units?|items?|pcs|pieces|auf\s+lager)|"
    r"only\s+(\d[\d,.]*)\s+left|stock\s*[:=]?\s*(\d[\d,.]*)",
    re.I,
)


def parse_availability(text: Any) -> Availability | None:
    """``"In stock (22 available)"`` -> ``Availability(True, 22)``; ``"Sold out"`` -> ``(False, None)``.

    Also reads schema.org values such as ``"https://schema.org/InStock"``.
    Returns ``None`` when the text says nothing about stock.
    """
    if isinstance(text, bool):
        return Availability(text, None)
    if not isinstance(text, str) or not text.strip():
        return None
    match = _STOCK_COUNT_RE.search(text)
    count = parse_count(next(g for g in match.groups() if g)) if match else None
    if _OUT_RE.search(text) or count == 0:  # "0 available" says "available" too
        return Availability(False, count)
    if _IN_RE.search(text) or count is not None:
        return Availability(True, count)
    return None


# --------------------------------------------------------------------------- #
# records
# --------------------------------------------------------------------------- #

_PRICE_FIELD = re.compile(r"(?:^|_)(?:price|prices|cost|amount|fee|msrp|rrp|tax|total|subtotal|shipping)(?:_|$)")
_RATING_FIELD = re.compile(r"(?:^|_)(?:rating|ratings?_value|stars|score|rating_value)$")
_AVAILABILITY_FIELD = re.compile(r"(?:^|_)(?:availability|available|stock|in_stock|stock_status|inventory)$")
_COUNT_FIELD = re.compile(
    r"(?:^|_)(?:reviews?|review_count|reviews_count|ratings_count|rating_count|number_of_\w+|num_\w+|count|"
    r"quantity|qty|sold|views|likes|votes|comments|answers|followers)$"
)
_SHORT_NUMBER_RE = re.compile(r"\D{0,24}\d[\d,.]*\s*[kKmMbB]?\b\D{0,24}")


def clean_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Type a record's values by field name.

    * price-like fields (``price``, ``price_incl_tax``, ``tax``...) become numbers, and the
      record gets one ``currency`` field;
    * ``rating``/``stars`` become numbers;
    * ``availability``/``stock`` keep their text and add ``in_stock`` (bool) and ``stock`` (int);
    * counts (``reviews``, ``number_of_reviews``, ``quantity``...) become ints;
    * other text has its whitespace normalized.

    Values that don't parse are kept as they were, so no data is lost.
    """
    out: dict[str, Any] = {}
    currency: str | None = record.get("currency") if isinstance(record.get("currency"), str) else None
    for key, value in record.items():
        if isinstance(value, str):
            value = normalize_space(value)
        name = key.lower()
        if not isinstance(value, str) or name in ("currency", "url", "image", "images", "link", "href", "id"):
            out[key] = value
            continue
        if _COUNT_FIELD.search(name) and _SHORT_NUMBER_RE.fullmatch(value):  # before prices: "total_reviews"
            count = parse_count(value)
            if count is not None:
                out[key] = count
                continue
        if _PRICE_FIELD.search(name) and len(value) <= 60:
            price = parse_price(value)
            if price is not None:
                out[key] = price.amount
                if price.currency is not None:
                    if currency is None:
                        currency = price.currency
                        out.setdefault("currency", currency)
                    elif price.currency != currency:
                        out[f"{key}_currency"] = price.currency
                continue
        if _RATING_FIELD.search(name) and len(value) <= 80:
            rating = parse_rating(value)
            if rating is not None:
                out[key] = rating
                continue
        if _AVAILABILITY_FIELD.search(name):
            availability = parse_availability(value)
            if availability is not None:
                out[key] = availability.in_stock if name == "in_stock" else value
                if availability.in_stock is not None and "in_stock" not in record:
                    out["in_stock"] = availability.in_stock
                if availability.stock is not None and name != "stock" and "stock" not in record:
                    out["stock"] = availability.stock
                if name == "stock" and availability.stock is not None:
                    out[key] = availability.stock
                continue
            if re.fullmatch(r"\d[\d,.]*", value):  # a stock column holding a count: "In stock: 120"
                out[key] = parse_count(value)
                continue
        out[key] = value
    return out
