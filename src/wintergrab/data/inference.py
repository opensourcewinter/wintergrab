"""Infer a :class:`~wintergrab.data.schema.Schema` from sample records.

For every field the candidate types are tried from the most specific to the
least (``boolean``, ``integer``, ``number``, ``url``, ``email``, ``date``,
``datetime``, ``money``, ``rating``, ``quantity``, ``availability``, ``phone``,
``country``, ``language``, ``enum``, ``text``, ``string``); the first one that
reads at least ``threshold`` (90%) of the field's non-empty values wins. Lists
become ``many`` fields, dicts nested ``object`` fields. A field present in
every sample is ``required``; money fields get their dominant currency,
quantity fields their dominant dimension's canonical unit.

The result is a starting point to review, not ground truth: :func:`infer_schema`
returns a schema whose ``description`` records how many samples it saw, and
:func:`explain_inference` says why each type was chosen.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import islice
from typing import Any

from .normalize import (
    is_placeholder,
    normalize_availability,
    normalize_country,
    normalize_email,
    normalize_language,
    parse_date,
    parse_datetime,
    parse_money,
    parse_phone,
    parse_quantity,
    parse_rating,
)
from .normalize.units import _CANONICAL
from .schema import Schema, SchemaField

__all__ = ["TypeGuess", "explain_inference", "infer_schema"]

_INT_TEXT = re.compile(r"^[+-]?\d{1,3}(?:[,. ]\d{3})*$|^[+-]?\d+$")
_NUMBER_TEXT = re.compile(r"^[+-]?(?:\d+|\d{1,3}(?:[,. ]\d{3})+)(?:[.,]\d+)?$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RATING_TEXT = re.compile(r"\d\s*(?:/|out of)\s*\d|[★☆⭐]|\bstars?\b", re.I)
_SAFE_FIELD = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass
class TypeGuess:
    """Why a field got its type."""

    field: str
    type: str
    share: float  # share of non-empty values the type could read
    samples: int  # non-empty values seen
    present: int  # records that had the field
    records: int


def _is_int(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    return isinstance(v, str) and bool(_INT_TEXT.match(v.strip()))


def _is_number(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    return isinstance(v, str) and bool(_NUMBER_TEXT.match(v.strip()))


def _is_url(v: Any) -> bool:
    return isinstance(v, str) and v.strip().lower().startswith(("http://", "https://", "//"))


def _is_date(v: Any) -> bool:
    if not isinstance(v, str) or len(v) > 40 or _is_number(v):
        return False
    parsed = parse_date(v)
    return parsed is not None and not re.search(r"\d{1,2}:\d{2}", v)


def _is_datetime(v: Any) -> bool:
    if not isinstance(v, str) or len(v) > 60 or _is_number(v):
        return False
    return parse_datetime(v) is not None and bool(re.search(r"\d{1,2}:\d{2}", v))


def _is_money(v: Any) -> bool:
    if not isinstance(v, str) or len(v) > 40:
        return False
    money = parse_money(v)
    return money is not None and money.currency is not None


def _is_rating(v: Any) -> bool:
    return isinstance(v, str) and len(v) <= 40 and bool(_RATING_TEXT.search(v)) and parse_rating(v) is not None


def _is_quantity(v: Any) -> bool:
    return isinstance(v, str) and len(v) <= 40 and parse_quantity(v) is not None and not _is_money(v)


def _is_phone(v: Any) -> bool:
    return isinstance(v, str) and v.strip().startswith(("+", "00", "tel:")) and parse_phone(v) is not None


def _is_email(v: Any) -> bool:
    return isinstance(v, str) and "@" in v and normalize_email(v) is not None


def _is_country(v: Any) -> bool:
    return isinstance(v, str) and len(v) <= 60 and len(v.strip()) > 2 and normalize_country(v) is not None


def _is_language(v: Any) -> bool:
    return isinstance(v, str) and len(v) <= 30 and normalize_language(v) is not None and not _is_country(v)


def _is_boolean(v: Any) -> bool:
    if isinstance(v, bool):
        return True
    return isinstance(v, str) and v.strip().lower() in {"true", "false", "yes", "no"}


def _is_availability(v: Any) -> bool:
    return isinstance(v, str) and len(v) <= 60 and normalize_availability(v) is not None


# (type, predicate) from most to least specific.
_CANDIDATES = (
    ("boolean", _is_boolean),
    ("integer", _is_int),
    ("number", _is_number),
    ("url", _is_url),
    ("email", _is_email),
    ("date", _is_date),
    ("datetime", _is_datetime),
    ("money", _is_money),
    ("rating", _is_rating),
    ("quantity", _is_quantity),
    ("availability", _is_availability),
    ("phone", _is_phone),
    ("country", _is_country),
    ("language", _is_language),
)


def _guess(values: list[Any], threshold: float) -> tuple[str, float]:
    if not values:
        return "any", 0.0
    if all(isinstance(v, Mapping) for v in values):
        return "object", 1.0
    scalars = [v for v in values if not isinstance(v, (Mapping, list))]
    if len(scalars) < len(values):
        return "any", len(scalars) / len(values)
    for name, predicate in _CANDIDATES:
        hits = sum(1 for v in scalars if predicate(v))
        share = hits / len(scalars)
        if share >= threshold:
            return name, share
    texts = [str(v) for v in scalars]
    distinct = set(texts)
    if len(texts) >= 10 and 2 <= len(distinct) <= min(20, len(texts) // 2) and all(len(t) <= 40 for t in distinct):
        return "enum", 1.0
    if sum(len(t) for t in texts) / len(texts) > 200 or any("\n" in t for t in texts):
        return "text", 1.0
    return "string", 1.0


def _field_name(name: Any) -> str:
    cleaned = _SAFE_FIELD.sub("_", str(name)).strip("_.-") or "field"
    return cleaned if not cleaned[0].isdigit() else "f_" + cleaned


def infer_schema(
    records: Iterable[Mapping[str, Any]],
    *,
    name: str = "inferred",
    sample: int = 1000,
    threshold: float = 0.9,
    guesses: list[TypeGuess] | None = None,
) -> Schema:
    """Guess a schema from up to ``sample`` records (see the module docs).

    Pass a list as ``guesses`` to receive a :class:`TypeGuess` per field.
    """
    rows = [r for r in islice(records, sample) if isinstance(r, Mapping)]
    order: list[str] = []
    values: dict[str, list[Any]] = {}
    present: Counter[str] = Counter()
    for row in rows:
        for key, value in row.items():
            if str(key).startswith("_"):
                continue  # metadata (provenance, issues...)
            if key not in values:
                values[key] = []
                order.append(key)
            present[key] += 1
            if value is None or value == "" or value == [] or value == {} or is_placeholder(value):
                continue  # "N/A", "-", "{{ price }}"... stand for a missing value
            values[key].append(value)
    fields = []
    for key in order:
        raw_values = values[key]
        many = bool(raw_values) and all(isinstance(v, list) for v in raw_values)
        flat = [item for v in raw_values for item in v] if many else raw_values
        flat = [v for v in flat if v not in (None, "")]
        kind, share = _guess(flat, threshold)
        options: dict[str, Any] = {"type": kind, "many": many}
        if present[key] == len(rows) and len(raw_values) == len(rows) and rows:
            options["required"] = True
        if kind == "object":
            nested = infer_schema(flat, name=str(key), sample=sample, threshold=threshold)
            options["fields"] = nested.fields
        elif kind == "enum":
            options["enum"] = sorted({str(v) for v in flat})
        elif kind == "money":
            currencies = Counter(m.currency for v in flat if (m := parse_money(str(v))) and m.currency)
            if currencies:
                options["currency"] = currencies.most_common(1)[0][0]
        elif kind == "quantity":
            dimensions = Counter(q.dimension for v in flat if (q := parse_quantity(str(v))))
            if dimensions:
                options["unit"] = _CANONICAL[dimensions.most_common(1)[0][0]]
        field_name = _field_name(key)
        if field_name != key:
            options["aliases"] = [str(key)]
        fields.append(SchemaField(name=field_name, **options))
        if guesses is not None:
            guesses.append(TypeGuess(field_name, kind, round(share, 4), len(flat), present[key], len(rows)))
    key_fields = [f.name for f in fields if f.name in ("url", "id", "sku", "gtin", "isbn") and f.required]
    return Schema(
        name=name,
        fields=fields,
        key=key_fields[:1],
        description=f"inferred from {len(rows)} record(s)",
    )


def explain_inference(records: Iterable[Mapping[str, Any]], **options: Any) -> list[TypeGuess]:
    """Why each field got its type (share of values the type could read, presence...)."""
    guesses: list[TypeGuess] = []
    infer_schema(records, guesses=guesses, **options)
    return guesses
