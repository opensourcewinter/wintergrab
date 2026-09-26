"""Prices and currencies: ``"₹29,999"`` -> ``Money(29999, "INR")``.

A currency is taken from an ISO code or a currency sign *next to the number*
(``"USD 12"``, ``"12 €"``, ``"R$ 9,90"``): upper-case words elsewhere in the
text (``"ALL PRODUCTS"``, ``"TOP DEALS"`` - both ISO codes) are ignored.

Some signs are shared: ``$`` (US, Canada, Australia, Mexico...), ``¥`` (Japan,
China), ``kr`` (Nordic countries), ``Rs`` (India, Pakistan, Sri Lanka...).
With ``country=`` they resolve to that country's currency; otherwise to the
most common reading, and ``"ambiguous-currency"`` is noted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ..reference import COUNTRY_CURRENCY, CURRENCIES, CURRENCY_SYMBOLS
from .numbers import _note, as_int_or_float, find_number, iter_numbers, parse_number

__all__ = ["Money", "currency_minor_units", "detect_currency", "parse_money", "parse_money_range"]

# Currencies written with a plain "$" (dollars and several pesos).
_DOLLAR_SIGN = frozenset(
    {"USD", "CAD", "AUD", "NZD", "HKD", "SGD", "TWD", "JMD", "TTD", "XCD", "BSD", "BBD", "BZD", "BMD", "KYD", "FJD",
     "LRD", "NAD", "SBD", "SRD", "GYD", "MXN", "ARS", "CLP", "COP", "CUP", "DOP", "UYU"}
)  # fmt: skip
# Currencies that share an ambiguous sign, by sign.
_SHARED = {
    "$": _DOLLAR_SIGN,
    "¥": frozenset({"JPY", "CNY"}),
    "KR": frozenset({"SEK", "NOK", "DKK", "ISK"}),
    "KR.": frozenset({"SEK", "NOK", "DKK", "ISK"}),
    "RS": frozenset({"INR", "PKR", "LKR", "NPR", "MUR", "SCR"}),
    "RS.": frozenset({"INR", "PKR", "LKR", "NPR", "MUR", "SCR"}),
    "₨": frozenset({"PKR", "INR", "LKR", "NPR", "MUR", "SCR"}),
    "﷼": frozenset({"SAR", "IRR", "YER", "OMR", "QAR"}),
    "FR.": frozenset({"CHF", "XOF", "XAF", "XPF"}),
    "R": frozenset({"ZAR"}),
}
# Signs made of letters need word boundaries ("R" must not match the "R" of "Rated").
_WORDY = {sign for sign in CURRENCY_SYMBOLS if sign[:1].isalpha()}
# Signs grouped by length, longest first, so a lookup is one dict probe per length.
_SIGNS_BY_LENGTH = [
    (n, {sign: sign for sign in CURRENCY_SYMBOLS if len(sign) == n})
    for n in sorted({len(sign) for sign in CURRENCY_SYMBOLS}, reverse=True)
]
_SPACES = " \t" + chr(0xA0)
_ISO = re.compile(r"(?<![A-Za-z])([A-Z]{3})(?![A-Za-z])")
_FREE = re.compile(r"^\s*(?:free|gratis|kostenlos|gratuit|gratuito|mufti|free of charge)\b", re.I)


@dataclass(frozen=True)
class Money:
    """An amount of money. ``currency`` is an ISO 4217 code, or ``None`` if unknown."""

    amount: Decimal
    currency: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"amount": as_int_or_float(self.amount), "currency": self.currency}

    def __str__(self) -> str:
        return f"{self.amount} {self.currency}" if self.currency else str(self.amount)


_DASH_CENTS = re.compile(r"[,.][-\u2013\u2014]{1,2}")


def currency_minor_units(code: str) -> int | None:
    """Decimal places of a currency (``JPY`` -> 0, ``KWD`` -> 3), ``None`` if unknown."""
    entry = CURRENCIES.get(code.upper())
    return entry[1] if entry else None


def _resolve(sign: str, country: str | None, notes: list[str] | None) -> str:
    code, ambiguous = CURRENCY_SYMBOLS[sign]
    if not ambiguous:
        return code
    if country:
        local = COUNTRY_CURRENCY.get(country.upper())
        if local and local in _SHARED.get(sign, frozenset()):
            _note(notes, "currency-from-country")
            return local
    _note(notes, "ambiguous-currency")
    return code


def _sign_at(text: str, pos: int, before: bool) -> str | None:
    """A currency sign ending at ``pos`` (``before``) or starting at ``pos``, else ``None``."""
    for n, signs in _SIGNS_BY_LENGTH:
        piece = text[pos - n : pos] if before else text[pos : pos + n]
        if len(piece) != n:
            continue
        sign = signs.get(piece.upper())
        if sign is None:
            continue
        if sign in _WORDY:
            outer = text[pos - n - 1 : pos - n] if before else text[pos + n : pos + n + 1]
            if outer.isalpha():  # part of a longer word
                continue
        return sign
    return None


def _currency_near(text: str, start: int, end: int, country: str | None, notes: list[str] | None) -> str | None:
    """The currency written right before or after the number at ``text[start:end]``."""
    before = text[:start].rstrip()
    after_start = end
    while after_start < len(text) and text[after_start] in _SPACES:
        after_start += 1
    for candidate in (_iso_before(before), _iso_after(text, after_start)):
        if candidate:
            return candidate
    sign = _sign_at(before, len(before), before=True)
    if sign is None:
        # "€1" and "1€", but also "EUR 1,-": look after the number too.
        sign = _sign_at(text, after_start, before=False)
    return _resolve(sign, country, notes) if sign else None


def _iso_before(before: str) -> str | None:
    match = re.search(r"([A-Za-z]{3})\s*$", before)
    if match and match.group(1).upper() in CURRENCIES and (match.group(1).isupper() or match.group(1).islower()):
        head = before[: match.start()]
        if not head or not head[-1].isalpha():
            return match.group(1).upper()
    return None


def _iso_after(text: str, pos: int) -> str | None:
    match = re.match(r"([A-Za-z]{3})(?![A-Za-z])", text[pos:])
    if match and match.group(1).upper() in CURRENCIES and (match.group(1).isupper() or match.group(1).islower()):
        return match.group(1).upper()
    return None


def detect_currency(
    text: str, *, country: str | None = None, default: str | None = None, notes: list[str] | None = None
) -> str | None:
    """The ISO code of the currency ``text`` mentions (next to its first number), else ``default``."""
    found = find_number(text)
    code = None
    if found is not None:
        code = _currency_near(text, found[1], found[2], country, notes)
    else:
        stripped = text.strip()
        if stripped.upper() in CURRENCIES:
            code = stripped.upper()
        elif stripped.upper() in CURRENCY_SYMBOLS:
            code = _resolve(stripped.upper(), country, notes)
    if code is None and default:
        _note(notes, "currency-from-default")
        return default.upper()
    return code


def parse_money(
    text: str | float | int | Decimal | None,
    *,
    currency: str | None = None,
    country: str | None = None,
    decimal: str | None = None,
    notes: list[str] | None = None,
) -> Money | None:
    """Parse a price. ``None`` if there is no amount.

    Args:
        currency: Currency to use when the text names none (and to read an
            ambiguous sign if it is one of that sign's currencies).
        country: ISO 3166 alpha-2 country, to read shared signs (``$``, ``¥``, ``kr``).
        decimal: The decimal separator, when known.
        notes: Receives codes for guesses: ``"ambiguous-currency"``,
            ``"currency-from-country"``, ``"currency-from-default"``,
            ``"ambiguous-separator"``, ``"free"``.
    """
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, (int, float, Decimal)):
        amount = parse_number(text)
        return Money(amount, currency.upper() if currency else None) if amount is not None else None
    source = str(text)
    if _FREE.match(source):
        _note(notes, "free")
        return Money(Decimal(0), currency.upper() if currency else None)
    if currency and country is None:
        # A given currency also settles which country's reading an ambiguous sign has.
        country = next((c for c, cur in COUNTRY_CURRENCY.items() if cur == currency.upper()), None)
    chosen: tuple[Decimal, int, int, list[str], str | None] | None = None
    for amount, start, end, local in iter_numbers(source, decimal=decimal):
        if source[end : end + 1] == "%" or source[end : end + 9].lstrip().lower().startswith(("%", "percent")):
            continue  # "20% off" is not a price
        dash = _DASH_CENTS.match(source, end)  # "1.299,-" is 1299 with no cents (German/Scandinavian style)
        if dash and decimal is None and "ambiguous-separator" in local:
            amount = parse_number(source[start:end], decimal=dash.group(0)[0]) or amount
            local.remove("ambiguous-separator")
        code = _currency_near(source, start, dash.end() if dash else end, country, local)
        if chosen is None or (code is not None and chosen[4] is None):
            chosen = (amount, start, end, local, code)
        if code is not None:
            break  # the first amount with a currency next to it
    if chosen is None:
        return None
    amount, start, end, local, code = chosen
    if code is None and currency:
        code = currency.upper()
        _note(local, "currency-from-default")
    if code is not None and decimal is None and "ambiguous-separator" in local:
        amount = _fix_for_minor_units(source[start:end], amount, code, local)
    if notes is not None:
        notes.extend(n for n in local if n not in notes)
    return Money(amount, code)


def _fix_for_minor_units(raw: str, amount: Decimal, code: str, notes: list[str]) -> Decimal:
    """Settle ``"1.234"`` / ``"1,234"`` with what the currency allows.

    * No minor units (JPY, KRW...): there are no decimals, so the separator groups. Certain.
    * Two (most currencies): prices show 0 or 2 decimals, so three digits are a
      group - almost always (per-litre fuel prices have 3 decimals), so it is
      noted as ``"separator-from-currency"``.
    * Three (KWD, BHD, JOD...): three decimals are normal; the ambiguity stays.
    """
    minor = currency_minor_units(code)
    grouped = Decimal(raw.replace(".", "").replace(",", "").replace(" ", ""))
    if minor == 0:
        notes.remove("ambiguous-separator")
        return grouped
    if minor == 2:
        notes.remove("ambiguous-separator")
        _note(notes, "separator-from-currency")
        return grouped
    return amount


def parse_money_range(
    text: str, *, currency: str | None = None, country: str | None = None, notes: list[str] | None = None
) -> tuple[Money, Money] | None:
    """``"$10 - $20"`` / ``"10-20 EUR"`` -> ``(Money(10, "USD"), Money(20, "USD"))``; ``None`` if not a range."""
    parts = re.split(r"\s*(?:[-–—]|to|bis|à|a)\s*(?=[^\d\s]{0,4}\s*\d)", text, maxsplit=1)
    if len(parts) != 2:
        return None
    low = parse_money(parts[0], currency=currency, country=country, notes=notes)
    high = parse_money(parts[1], currency=currency, country=country, notes=notes)
    if low is None or high is None:
        return None
    code = low.currency or high.currency
    return Money(low.amount, code), Money(high.amount, code)
