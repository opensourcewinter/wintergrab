"""Normalizers: turn values as written on web pages into clean, typed, comparable values.

Every parser takes an optional ``notes`` list and appends short codes when it
had to guess (``"ambiguous-separator"``, ``"ambiguous-currency"``,
``"ambiguous-day-month"``, ``"two-digit-year"``...). Callers use them to lower
the confidence of a value instead of treating a guess as a fact.

>>> from wintergrab.data.normalize import parse_money
>>> parse_money("₹29,999")
Money(amount=Decimal('29999'), currency='INR')
"""

from __future__ import annotations

from .contact import Phone, normalize_email, normalize_phone, normalize_url_value, parse_phone
from .dates import parse_date, parse_datetime, parse_duration
from .geo import (
    Address,
    coordinates_in_url,
    normalize_country,
    normalize_language,
    normalize_region,
    parse_address,
    parse_coordinates,
    place_meanings,
    postal_code,
    read_address,
)
from .money import Money, currency_minor_units, detect_currency, parse_money, parse_money_range
from .numbers import as_int_or_float, find_number, iter_numbers, parse_integer, parse_number, parse_percent
from .text import (
    AVAILABILITY,
    Rating,
    clean_text,
    fix_mojibake,
    has_replacement_characters,
    is_placeholder,
    mojibake_score,
    normalize_availability,
    parse_boolean,
    parse_rating,
)
from .units import UNITS, Quantity, convert, parse_dimensions, parse_quantity, unit_info

__all__ = [
    "AVAILABILITY",
    "UNITS",
    "Address",
    "Money",
    "Phone",
    "Quantity",
    "Rating",
    "as_int_or_float",
    "clean_text",
    "convert",
    "coordinates_in_url",
    "currency_minor_units",
    "detect_currency",
    "find_number",
    "fix_mojibake",
    "has_replacement_characters",
    "is_placeholder",
    "iter_numbers",
    "mojibake_score",
    "normalize_availability",
    "normalize_country",
    "normalize_email",
    "normalize_language",
    "normalize_phone",
    "normalize_region",
    "normalize_url_value",
    "parse_address",
    "parse_boolean",
    "parse_coordinates",
    "parse_date",
    "parse_datetime",
    "parse_dimensions",
    "parse_duration",
    "parse_integer",
    "parse_money",
    "parse_money_range",
    "parse_number",
    "parse_percent",
    "parse_phone",
    "parse_quantity",
    "parse_rating",
    "place_meanings",
    "postal_code",
    "read_address",
    "unit_info",
]
