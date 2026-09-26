"""Numbers written for humans: ``"1,234.56"``, ``"1.234,56"``, ``"1 234"``, ``"12,5 %"``, ``"1.2k"``.

The hard part is the separators. ``"1,234"`` is one thousand two hundred and
thirty-four in English and one point two three four in German. The rules:

* Both ``,`` and ``.`` present: the last one is the decimal separator.
* One kind, repeated (``1,234,567``): grouping.
* One kind, once: followed by exactly three digits it is ambiguous. A comma
  is then read as grouping (``1,234`` -> 1234) and a point as a decimal
  (``1.234`` -> 1.234), unless ``decimal=`` says which character is the decimal
  separator. Every ambiguous case adds ``"ambiguous-separator"`` to ``notes``.
* Spaces, thin spaces and apostrophes (``1 234``, ``1'234``) are grouping.

Functions take an optional ``notes`` list and append short codes describing
guesses they had to make, so callers can lower their confidence.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation

__all__ = ["as_int_or_float", "find_number", "iter_numbers", "parse_integer", "parse_number", "parse_percent"]

# Hyphen-minus, minus sign, figure/en/em dashes, small and full-width hyphen-minus.
_MINUS = "-" + "".join(map(chr, (0x2212, 0x2012, 0x2013, 0x2014, 0xFE63, 0xFF0D)))
# Spaces (plain, no-break, thin, narrow no-break, figure) and apostrophes group digits, in threes: "1 234", "1'234".
_GROUP_CHARS = " " + "".join(map(chr, (0x00A0, 0x2009, 0x202F, 0x2007))) + "'" + chr(0x2019)
_NUMBER = re.compile(
    rf"(?P<neg>[{_MINUS}]\s*)?"
    r"(?P<lead>(?<!\d)[.,](?=\d))?"  # ".99", ",5"
    rf"(?P<num>\d{{1,3}}(?:[{_GROUP_CHARS}]\d{{3}})+(?:[.,]\d+)?|\d[\d,.]*\d|\d)"
    # "1.2k", "3M", "2bn" (attached: "5 m" is five metres) or a word ("2 million", "3 lakh")
    r"(?P<suffix>(?:[kKmMbB]|mn|bn)\b|\s*(?:thousand|million|billion|lakh|crore)\b)?"
)
_SUFFIXES = {
    "k": 1_000, "thousand": 1_000, "m": 1_000_000, "mn": 1_000_000, "million": 1_000_000,
    "b": 1_000_000_000, "bn": 1_000_000_000, "billion": 1_000_000_000, "lakh": 100_000, "crore": 10_000_000,
}  # fmt: skip
_EASTERN_DIGITS = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹०१२३४५६७८९০১২৩৪৫৬৭৮৯０１２３４５６７８９",
    "0123456789" * 5,
)


def _note(notes: list[str] | None, code: str) -> None:
    if notes is not None and code not in notes:
        notes.append(code)


def _digits(text: str, decimal: str | None, notes: list[str] | None) -> str | None:
    """``"1.234,56"`` -> ``"1234.56"`` (a string ``Decimal`` accepts), or ``None``."""
    body = "".join(ch for ch in text if ch not in _GROUP_CHARS)
    commas, points = body.count(","), body.count(".")
    if commas and points:
        dec = "," if body.rfind(",") > body.rfind(".") else "."
        group = "." if dec == "," else ","
        if body.count(dec) > 1:
            return None  # "1.234.56,7" is not a number
        return body.replace(group, "").replace(dec, ".")
    sep = "," if commas else "." if points else ""
    if not sep:
        return body
    count = commas or points
    if count > 1:
        parts = body.split(sep)
        if sep == decimal or any(len(p) != 3 for p in parts[1:]):
            return None  # "1,23,4" - grouping must come in threes (lakh grouping is handled below)
        return body.replace(sep, "")
    head, tail = body.split(sep)
    if decimal is not None:
        if sep == decimal:
            return f"{head}.{tail}"
        return head + tail if len(tail) == 3 else None
    if len(tail) != 3:
        return f"{head}.{tail}"  # "12,5" / "12.50": a decimal separator
    _note(notes, "ambiguous-separator")
    return head + tail if sep == "," else f"{head}.{tail}"


def _indian_grouping(text: str) -> str | None:
    """``"1,23,45,678"`` (lakh/crore grouping) -> ``"12345678"``."""
    if re.fullmatch(r"\d{1,2}(?:,\d\d)+,\d{3}(?:\.\d+)?", text):
        return text.replace(",", "")
    return None


def iter_numbers(
    text: str, *, decimal: str | None = None, allow_suffix: bool = True
) -> Iterator[tuple[Decimal, int, int, list[str]]]:
    """Every number in ``text``: ``(value, start, end, notes)`` in order of appearance."""
    if decimal not in (None, ",", "."):
        raise ValueError("decimal must be ',' or '.'")
    source = text.translate(_EASTERN_DIGITS)
    for match in _NUMBER.finditer(source):
        notes: list[str] = []
        raw = match.group("num").strip(_GROUP_CHARS)
        if match.group("lead"):
            if not raw.isdigit():
                continue
            digits: str | None = "0." + raw  # ".99" / ",5"
        else:
            digits = _indian_grouping(raw) if decimal != "," else None
            if digits is None:
                digits = _digits(raw, decimal, notes)
        if digits is None:
            continue
        try:
            value = Decimal(digits)
        except InvalidOperation:
            continue
        start, end = match.start(), match.end("num")
        negative = bool(match.group("neg")) or (
            start > 0 and source[start - 1] == "(" and source[match.end() :].lstrip().startswith(")")
        )
        suffix = (match.group("suffix") or "").strip().lower()
        if suffix and allow_suffix:
            value *= _SUFFIXES[suffix]
            end = match.end()
            notes.append("suffix")
        yield (-value if negative else value), start, end, notes


def find_number(
    text: str, *, decimal: str | None = None, notes: list[str] | None = None, allow_suffix: bool = True
) -> tuple[Decimal, int, int] | None:
    """The first number in ``text`` and where it is: ``(value, start, end)``, or ``None``."""
    for value, start, end, local in iter_numbers(text, decimal=decimal, allow_suffix=allow_suffix):
        for code in local:
            _note(notes, code)
        return value, start, end
    return None


def parse_number(
    text: str | float | int | Decimal | None,
    *,
    decimal: str | None = None,
    notes: list[str] | None = None,
    allow_suffix: bool = True,
) -> Decimal | None:
    """The first number in ``text`` as a :class:`~decimal.Decimal` (``None`` if there is none).

    Args:
        decimal: The decimal separator (``","`` or ``"."``) when you know it,
            e.g. from the site's language.
        notes: Receives ``"ambiguous-separator"`` / ``"suffix"`` codes for guesses.
        allow_suffix: Expand ``k``/``M``/``bn``/``lakh``/``crore`` multipliers.
    """
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, Decimal):
        return text
    if isinstance(text, int):
        return Decimal(text)
    if isinstance(text, float):
        return Decimal(repr(text)) if text == text and text not in (float("inf"), float("-inf")) else None
    found = find_number(str(text), decimal=decimal, notes=notes, allow_suffix=allow_suffix)
    return found[0] if found is not None else None


def parse_integer(
    text: str | float | int | Decimal | None, *, decimal: str | None = None, notes: list[str] | None = None
) -> int | None:
    """Like :func:`parse_number`, for counts (``"1,204 reviews"``, ``"2.3k"``). Rounds half-even.

    A count has no decimals, so ``"1.204"`` / ``"1,204"`` is read as 1204 (no ambiguity noted).
    """
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, (int, float, Decimal)):
        value = parse_number(text)
    else:
        local: list[str] = []
        found = find_number(str(text), decimal=decimal, notes=local)
        if found is None:
            return None
        value, start, end = found
        if "ambiguous-separator" in local:
            local.remove("ambiguous-separator")
            source = str(text).translate(_EASTERN_DIGITS)
            value = Decimal("".join(ch for ch in source[start:end] if ch.isdigit()))
            if source[start : start + 1] in _MINUS:
                value = -value
        for code in local:
            _note(notes, code)
    if value is None:
        return None
    if value != value.to_integral_value():
        _note(notes, "rounded")
    return int(value.to_integral_value())


def parse_percent(text: str | float | int | None, *, notes: list[str] | None = None) -> Decimal | None:
    """``"12.5 %"`` -> ``Decimal("0.125")``; a bare number counts as a percentage too."""
    value = parse_number(text, notes=notes, allow_suffix=False)
    return None if value is None else value / 100


def as_int_or_float(value: Decimal) -> int | float:
    """A JSON-friendly number: ``int`` when integral, else ``float``."""
    if value == value.to_integral_value() and abs(value) < 2**63:
        return int(value)
    return float(value)
