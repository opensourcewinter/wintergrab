"""Phone numbers, e-mail addresses and URLs.

Phone numbers become E.164 (``+14155552671``). Numbers written with a country
code (``+44 20 7946 0958``, ``0044 ...``) are checked against the E.164 limits;
national numbers (``(415) 555-2671``, ``020 7946 0958``) need ``country=``.
Length checks use the usual national number lengths of each country, so
validation is approximate (``"length-unverified"`` is noted for countries
without known lengths): exact validation needs per-range numbering data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

from ...urls import URLNormalizer, normalize_url
from ..reference import COUNTRIES, PHONE_RULES, calling_codes
from .numbers import _note

__all__ = ["Phone", "normalize_email", "normalize_phone", "normalize_url_value", "parse_phone"]

_CODES = calling_codes()
_EXTENSION = re.compile(r"\s*(?:ext\.?|extension|x|#|poste|durchwahl|dw)\s*(\d{1,6})\s*$", re.I)
_PHONE_CHARS = re.compile(r"[^\d+]")
_LETTERS = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "22233344455566677778889999")


@dataclass(frozen=True)
class Phone:
    """A phone number in E.164 form plus what is known about it."""

    e164: str  # "+14155552671"
    country: str | None  # alpha-2 of the calling code (None when the code is shared and unresolved)
    extension: str | None = None

    @property
    def calling_code(self) -> str:
        for size in (1, 2, 3):
            if self.e164[1 : 1 + size] in _CODES:
                return self.e164[1 : 1 + size]
        return ""

    @property
    def national_number(self) -> str:
        return self.e164[1 + len(self.calling_code) :]

    def __str__(self) -> str:
        return self.e164 + (f" ext. {self.extension}" if self.extension else "")


def _split_code(digits: str) -> tuple[str, str] | None:
    """``"4420..."`` -> ``("44", "20...")`` (calling codes are prefix-free)."""
    for size in (1, 2, 3):
        if digits[:size] in _CODES:
            return digits[:size], digits[size:]
    return None


def _country_for(code: str, national: str, hint: str | None) -> str | None:
    countries = _CODES.get(code, [])
    if hint and hint.upper() in countries:
        return hint.upper()
    if len(countries) == 1:
        return countries[0]
    if code == "1":
        return "US" if hint is None else None  # the North American Numbering Plan spans ~25 countries
    if code == "7":
        return "KZ" if national[:1] in ("6", "7") else "RU"
    if code == "44":
        return "GB"
    return countries[0] if countries else None


def _check_length(country: str | None, national: str, notes: list[str] | None) -> bool:
    rule = PHONE_RULES.get(country or "")
    if rule is None:
        _note(notes, "length-unverified")
        return 4 <= len(national) <= 14
    return rule.min_length <= len(national) <= rule.max_length


_VANITY = re.compile(r"[+\d\s().\-/]*\d[+\d\s().\-/]*[A-Z]{2,}[A-Z\d\s().\-/]*")


def parse_phone(text: str | None, *, country: str | None = None, notes: list[str] | None = None) -> Phone | None:
    """Parse a phone number; ``None`` if it cannot be a valid number.

    Args:
        country: ISO alpha-2 country for numbers written without a country code.
        notes: Receives ``"country-from-default"``, ``"length-unverified"``, ``"vanity"`` codes.
    """
    if not text:
        return None
    raw = str(text).strip()
    if raw.lower().startswith("tel:"):
        raw = raw[4:]
    extension = None
    ext = _EXTENSION.search(raw)
    if ext:
        extension = ext.group(1)
        raw = raw[: ext.start()]
    if _VANITY.fullmatch(raw):  # 1-800-FLOWERS (capitals among the digits, no other words)
        raw = raw.translate(_LETTERS)
        _note(notes, "vanity")
    compact = _PHONE_CHARS.sub("", raw)
    if compact.count("+") > 1 or ("+" in compact and not compact.startswith("+")):
        return None
    digits = compact.lstrip("+")
    if not digits.isdigit():
        return None
    international = compact.startswith("+")
    if not international and digits.startswith("00"):
        digits, international = digits[2:], True
    if not international and digits.startswith("011") and (country or "US").upper() in ("US", "CA"):
        digits, international = digits[3:], True  # NANP international prefix
    if international:
        split = _split_code(digits)
        if split is None:
            return None
        code, national = split
        found = _country_for(code, national, country)
    else:
        if not country:
            return None
        found = country.upper()
        rule = PHONE_RULES.get(found)
        info = COUNTRIES.get(found)
        if info is None:
            return None
        code = rule.calling_code if rule else info.calling_code
        national = digits
        trunk = rule.trunk_prefix if rule else "0"
        if trunk and national.startswith(trunk) and len(national) > (rule.max_length if rule else 8):
            national = national[len(trunk) :]
        elif trunk == "0" and national.startswith("0"):
            national = national[1:]
        _note(notes, "country-from-default")
    if code == "1" and not (len(national) == 10 and national[0] in "23456789" and national[3] in "23456789"):
        return None  # NANP: NXX-NXX-XXXX
    if not _check_length(found if code != "1" else "US", national, notes):
        return None
    if len(code) + len(national) > 15:
        return None
    return Phone(f"+{code}{national}", found, extension)


def normalize_phone(text: str | None, *, country: str | None = None, notes: list[str] | None = None) -> str | None:
    """E.164 string (``"+14155552671"``), or ``None`` if the number is not valid."""
    phone = parse_phone(text, country=country, notes=notes)
    return phone.e164 if phone else None


_OBFUSCATED_AT = re.compile(r"\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|\s+at\s+|＠)\s*", re.I)
_OBFUSCATED_DOT = re.compile(r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|\s+dot\s+)\s*", re.I)
_EMAIL = re.compile(
    r"^(?P<local>[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*)@"
    r"(?P<domain>(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63})$"
)


def normalize_email(text: str | None, *, notes: list[str] | None = None) -> str | None:
    """``"Mailto:John.Doe@Example.COM"`` -> ``"John.Doe@example.com"``; ``None`` if it is not an address.

    The domain is lower-cased (it is case-insensitive); the local part is kept
    as written (it may be case-sensitive). Obfuscations such as
    ``john [at] example [dot] com`` are undone (noted as ``"deobfuscated"``).
    """
    if not text:
        return None
    value = str(text).strip().strip("<>").strip()
    if value.lower().startswith("mailto:"):
        value = value[7:].split("?", 1)[0]
    if "@" not in value and _OBFUSCATED_AT.search(value):
        value = _OBFUSCATED_DOT.sub(".", _OBFUSCATED_AT.sub("@", value, count=1))
        _note(notes, "deobfuscated")
    value = value.strip().rstrip(".")
    match = _EMAIL.match(value)
    if match is None or len(value) > 254 or len(match.group("local")) > 64:
        return None
    domain = match.group("domain").lower()
    if ".." in domain or domain.split(".")[-1].isdigit():
        return None
    return f"{match.group('local')}@{domain}"


_KEEP_TRACKING = URLNormalizer(strip_tracking=False)


def normalize_url_value(text: str | None, *, base_url: str | None = None, strip_tracking: bool = True) -> str | None:
    """An absolute, normalized http(s) URL (relative ones resolved against ``base_url``), else ``None``."""
    if not text:
        return None
    value = str(text).strip()
    if value.startswith("//"):
        value = "https:" + value
    if base_url and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value):
        value = urljoin(base_url, value)
    if not value.lower().startswith(("http://", "https://")):
        return None
    try:
        return normalize_url(value) if strip_tracking else _KEEP_TRACKING(value)
    except ValueError:
        return None
