"""Text cleanup, encoding damage, booleans, availability and ratings.

* :func:`clean_text`: entities unescaped, zero-width characters removed,
  whitespace squeezed, Unicode normalized.
* :func:`mojibake_score` / :func:`fix_mojibake`: UTF-8 text that was decoded as
  Windows-1252 or Latin-1 (``"CafÃ©"`` for ``"Café"``) - detected, and repaired
  only when the repair is lossless and removes the damage.
* :func:`is_placeholder`: ``"N/A"``, ``"TBD"``, ``"-"``, unrendered ``{{ templates }}``...
  standing in for a missing value.
* :func:`parse_boolean`, :func:`normalize_availability` (schema.org
  ``ItemAvailability`` names), :func:`parse_rating` (``"4.5 out of 5"``,
  ``"★★★★☆"``, ``"Four stars"``, ``"90%"``).
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from .numbers import _note, parse_number

__all__ = [
    "AVAILABILITY",
    "Rating",
    "clean_text",
    "fix_mojibake",
    "has_replacement_characters",
    "is_placeholder",
    "mojibake_score",
    "normalize_availability",
    "parse_boolean",
    "parse_rating",
]

PLACEHOLDER = re.compile(
    r"^\s*(?:n/?a|null|none|nil|undefined|nan|tbd|tba|todo|-+|\?+|0000-00-00|lorem ipsum.*|\[object object\]|"
    r"\{\{.*\}\}|\$\{.*\}|<%.*%>|%[a-z_]+%)\s*$",
    re.I | re.S,
)


def is_placeholder(value: object) -> bool:
    """Whether ``value`` is text standing in for a missing value (``"N/A"``, ``"-"``, ``"{{ price }}"``...)."""
    return isinstance(value, str) and PLACEHOLDER.match(value) is not None


_ZERO_WIDTH = re.compile("[" + "".join(map(chr, (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD))) + "]")
_WS = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(
    text: str | None, *, form: Literal["NFC", "NFD", "NFKC", "NFKD"] = "NFC", keep_newlines: bool = False
) -> str | None:
    """Readable text: HTML entities decoded, invisible characters dropped, whitespace squeezed.

    ``form`` is the Unicode normalization (``"NFC"``, or ``"NFKC"`` to also fold
    compatibility characters such as full-width letters and ligatures).
    """
    if text is None:
        return None
    value = html.unescape(str(text)) if "&" in str(text) else str(text)
    value = _ZERO_WIDTH.sub("", _CONTROL.sub("", value))
    value = unicodedata.normalize(form, value)
    if keep_newlines:
        lines = [_WS.sub(" ", line).strip() for line in value.splitlines()]
        return "\n".join(line for line in lines if line)
    return _WS.sub(" ", value).strip()


# UTF-8 lead bytes decoded as cp1252/latin-1 show up as these pairs: "Ã©", "â€™", "Â ", "Ã¼"...
_MOJIBAKE = re.compile(
    "[" + chr(0xC2) + chr(0xC3) + "][\u0080-\u00bf]"  # "Ã©", "Â " (a UTF-8 lead byte read as Latin-1)
    "|" + chr(0xE2) + chr(0x20AC) + "[\u0080-\u00bf\u2018-\u201e\u2122\u0153\u0161\u017e]"  # "â€™"
    "|" + chr(0xC3) + chr(0xA0) + "|" + chr(0xE2) + "\u0080"
)


def mojibake_score(text: str) -> float:
    """Share of characters that look like UTF-8 read as Windows-1252 / Latin-1 (0 = clean)."""
    if not text:
        return 0.0
    hits = len(_MOJIBAKE.findall(text))
    return min(1.0, hits * 2 / len(text))


def fix_mojibake(text: str, *, notes: list[str] | None = None) -> str:
    """Undo UTF-8-as-Windows-1252 damage (``"CafÃ©"`` -> ``"Café"``); anything else is returned unchanged.

    A repair is kept only if re-encoding is lossless and the result has less
    damage than the input, so clean text (including real ``"Ã"``) is never broken.
    """
    if not text or not _MOJIBAKE.search(text):
        return text
    changed = False

    def repair(match: re.Match[str]) -> str:
        nonlocal changed
        run = match.group(0)
        if not _MOJIBAKE.search(run):
            return run
        try:
            repaired = _as_windows_1252(run).decode("utf-8")
        except UnicodeDecodeError:
            return run
        if mojibake_score(repaired) >= mojibake_score(run):
            return run
        changed = True
        return repaired

    # Repair each stretch of Windows-1252 characters on its own, so real non-Latin text around it is untouched.
    result = _CP1252_RUN.sub(repair, text)
    if changed:
        _note(notes, "mojibake-repaired")
    return result


# The characters Windows-1252 can produce: Latin-1 plus its 0x80-0x9F extras.
_CP1252_EXTRAS = "".join(
    bytes([b]).decode("cp1252") for b in range(0x80, 0xA0) if b not in (0x81, 0x8D, 0x8F, 0x90, 0x9D)
)
_CP1252_RUN = re.compile("[\u0000-\u00ff" + re.escape(_CP1252_EXTRAS) + "]+")


def _as_windows_1252(text: str) -> bytes:
    """``text`` encoded as Windows-1252, passing its five undefined bytes (0x81, 0x8D...) through as Latin-1."""
    out = bytearray()
    for ch in text:
        try:
            out += ch.encode("cp1252")
        except UnicodeEncodeError:
            out.append(ord(ch))  # only reached for U+0081, U+008D, U+008F, U+0090, U+009D (the run excludes others)
    return bytes(out)


def has_replacement_characters(text: str | None) -> bool:
    """``True`` if ``text`` contains U+FFFD: bytes that could not be decoded were lost."""
    return bool(text) and "�" in str(text)


_TRUE = {"true", "yes", "y", "1", "on", "t", "ja", "oui", "si", "sí", "sim", "да", "✓", "✔", "x", "checked"}
_FALSE = {"false", "no", "n", "0", "off", "f", "nein", "non", "não", "nao", "нет", "✗", "✘", "-", "unchecked"}


def parse_boolean(value: object, *, notes: list[str] | None = None) -> bool | None:
    """``"yes"``/``"true"``/``"1"``/``"✓"``... -> ``True``; the opposites -> ``False``; else ``None``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value) if value in (0, 1) else None
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


#: schema.org ItemAvailability names.
AVAILABILITY = (
    "InStock", "OutOfStock", "PreOrder", "BackOrder", "Discontinued", "LimitedAvailability", "InStoreOnly",
    "OnlineOnly", "SoldOut", "PreSale", "Reserved", "MadeToOrder",
)  # fmt: skip
_AVAILABILITY_WORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "Discontinued",
        re.compile(r"discontinued|no longer (?:available|sold|made)|nicht mehr (?:lieferbar|erhältlich)", re.I),
    ),
    ("SoldOut", re.compile(r"sold[ -]?out|ausverkauft|agotado|épuisé|esgotado", re.I)),
    (
        "OutOfStock",
        re.compile(
            r"out[ -]of[ -]stock|not (?:in stock|available)|unavailable|currently unavailable|no stock|"
            r"nicht (?:auf lager|verfügbar|vorrätig)|rupture de stock|indisponible|sin (?:stock|existencias)|"
            r"no disponible|esaurito|non disponibile|indisponível|niet (?:op voorraad|leverbaar)",
            re.I,
        ),
    ),
    ("PreOrder", re.compile(r"pre[ -]?order|vorbestell|précommande|precommande|reserva(?:r)? ya|preordina", re.I)),
    (
        "BackOrder",
        re.compile(
            r"back[ -]?order|nachbestellt|en réassort|"
            r"(?:available|ships?|dispatch(?:ed|es)?|delivery)\s+(?:with)?in\s+\d+(?:\s*[-–]\s*\d+)?\s*(?:weeks?|months?)",
            re.I,
        ),
    ),
    (
        "LimitedAvailability",
        re.compile(
            r"(?:only|just)\s+\d+\s+(?:left|remaining)|limited (?:stock|availability)|few left|low stock|"
            r"(?:nur noch|noch)\s+\d+|plus que \d+|últimas? unidades|quedan \d+",
            re.I,
        ),
    ),
    ("InStoreOnly", re.compile(r"in[ -]store only|nur (?:im|in der) (?:laden|filiale)", re.I)),
    ("OnlineOnly", re.compile(r"online only|nur online", re.I)),
    (
        "InStock",
        re.compile(
            r"in[ -]stock|available|ready to ship|ships? (?:today|now|in)|auf lager|lieferbar|verfügbar|vorrätig|"
            r"en stock|disponible|en existencia|disponibile|disponível|op voorraad|add to (?:cart|basket|bag)",
            re.I,
        ),
    ),
)


def normalize_availability(value: object, *, notes: list[str] | None = None) -> str | None:
    """A schema.org ``ItemAvailability`` name (``"InStock"``, ``"OutOfStock"``...) or ``None``.

    Accepts schema.org URLs (``"https://schema.org/InStock"``), the names
    themselves in any case, and shop wording in several languages (``"Only 3
    left"`` -> ``"LimitedAvailability"``, ``"Ausverkauft"`` -> ``"SoldOut"``).
    Negations are checked before the positive wording.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "InStock" if value else "OutOfStock"
    text = str(value).strip()
    if not text:
        return None
    tail = text.rstrip("/").rsplit("/", 1)[-1] if "schema.org" in text.lower() else text
    compact = re.sub(r"[\s_-]", "", tail).lower()
    for name in AVAILABILITY:
        if compact == name.lower():
            return name
    for name, pattern in _AVAILABILITY_WORDS:
        if pattern.search(text):
            if name == "InStock" and len(text) > 80:
                _note(notes, "availability-from-long-text")
            return name
    return None


@dataclass(frozen=True)
class Rating:
    """A rating and its scale: ``Rating(4.5, 5)``."""

    value: Decimal
    best: Decimal = Decimal(5)

    def normalized(self, scale: int = 5) -> Decimal:
        """The rating on a ``0..scale`` scale."""
        return self.value / self.best * scale if self.best else self.value


_WORD_NUMBERS: dict[str, int | Decimal] = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                 "nine": 9, "ten": 10, "half": Decimal("0.5")}  # fmt: skip
_OUT_OF = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:/|out of|of|von|sur|de|su)\s*(\d+(?:[.,]\d+)?)", re.I)
_FULL_STARS = "★⭐✭✮✯"
_EMPTY_STARS = "☆✩"


def parse_rating(value: object, *, best: float | None = None, notes: list[str] | None = None) -> Rating | None:
    """``"4.5 out of 5"``, ``"4.5/5"``, ``"★★★★☆"``, ``"Four stars"``, ``"90%"``, ``"star-rating Three"`` -> :class:`Rating`.

    Without an explicit scale, ``best`` (default 5) is assumed and
    ``"scale-assumed"`` noted; values above it are rejected.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(str(value))
        scale = Decimal(str(best)) if best else Decimal(5)
        return Rating(number, scale) if 0 <= number <= scale else None
    text = str(value).strip()
    if not text:
        return None
    out_of = _OUT_OF.search(text)
    if out_of:
        number = Decimal(out_of.group(1).replace(",", "."))
        scale = Decimal(out_of.group(2).replace(",", "."))
        return Rating(number, scale) if scale > 0 and 0 <= number <= scale else None
    stars = sum(text.count(ch) for ch in _FULL_STARS)
    empty = sum(text.count(ch) for ch in _EMPTY_STARS)
    if stars or empty:
        halves = text.count("½") + text.count("⯪")
        return Rating(Decimal(stars) + Decimal("0.5") * halves, Decimal(stars + empty + halves) or Decimal(5))
    percent = re.search(r"(\d+(?:[.,]\d+)?)\s*%", text)
    if percent:
        number = Decimal(percent.group(1).replace(",", "."))
        return Rating(number, Decimal(100)) if 0 <= number <= 100 else None
    scale = Decimal(str(best)) if best else Decimal(5)
    words = re.findall(r"[a-z]+", text.lower())
    for word in words:
        if word in _WORD_NUMBERS and word != "half":
            number = Decimal(_WORD_NUMBERS[word])
            if "half" in words:
                number += Decimal("0.5")
            if not best:
                _note(notes, "scale-assumed")
            return Rating(number, scale) if number <= scale else None
    parsed = parse_number(text, allow_suffix=False)
    if parsed is None:
        return None
    number = parsed
    if not best:
        _note(notes, "scale-assumed")
    if number > scale:
        if scale == 5 and number <= 10 and not best:
            _note(notes, "scale-10-guessed")
            return Rating(number, Decimal(10))
        return None
    return Rating(number, scale) if number >= 0 else None
