"""Countries, regions, languages, addresses and coordinates.

* :func:`normalize_country`: names, native names, alpha-2/alpha-3 codes and
  common aliases (``"USA"``, ``"Deutschland"``, ``"UK"``) -> ISO 3166-1 alpha-2.
* :func:`normalize_region`: ``"California"``/``"Calif."``-free spellings and codes
  -> ISO 3166-2 (``"US-CA"``) for the countries in :data:`~wintergrab.data.reference.REGIONS`.
* :func:`normalize_language`: ``"English"``, ``"en_us"``, ``"Deutsch"`` -> BCP 47 (``"en-US"``, ``"de"``).
* :func:`parse_address`: a best-effort split of a one-line address into
  street, city, region, postal code and country. Addresses vary enormously;
  the result says what it could and could not identify.
* :func:`parse_coordinates`: ``"48.8584, 2.2945"`` or DMS -> ``(lat, lon)``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any

from ..reference import COUNTRIES, LANGUAGE_ALIASES, LANGUAGES, REGION_ALIASES, REGIONS
from .numbers import _note

__all__ = [
    "Address",
    "normalize_country",
    "normalize_language",
    "normalize_region",
    "parse_address",
    "parse_coordinates",
    "postal_code",
]


def _fold(text: str) -> str:
    """Lower-case, accents removed, punctuation squeezed: ``"Côte d’Ivoire"`` -> ``"cote d'ivoire"``."""
    text = unicodedata.normalize("NFKD", text.strip().lower().replace("’", "'"))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text.replace("the ", "", 1) if text.startswith("the ") else text).strip(" .")


_COUNTRY_INDEX: dict[str, str] = {}
for _c in COUNTRIES.values():
    for _name in (_c.name, *_c.aliases):
        _COUNTRY_INDEX.setdefault(_fold(_name), _c.alpha2)
    _COUNTRY_INDEX.setdefault(_c.alpha3.lower(), _c.alpha2)
_ALPHA3 = {c.alpha3: c.alpha2 for c in COUNTRIES.values()}


def normalize_country(text: str | None, *, notes: list[str] | None = None) -> str | None:
    """ISO 3166-1 alpha-2 code of a country name or code (``None`` if unknown)."""
    if not text:
        return None
    raw = str(text).strip()
    if len(raw) == 2 and raw.isalpha():
        code = raw.upper()
        if code == "UK":
            return "GB"
        return code if code in COUNTRIES else None
    if len(raw) == 3 and raw.isalpha() and raw.upper() in _ALPHA3:
        return _ALPHA3[raw.upper()]
    folded = _fold(raw)
    if folded in _COUNTRY_INDEX:
        return _COUNTRY_INDEX[folded]
    # "Republic of Korea" style inversions and trailing qualifiers: try without parenthesised parts.
    simplified = _fold(re.sub(r"\(.*?\)", "", raw))
    if simplified in _COUNTRY_INDEX:
        _note(notes, "country-simplified")
        return _COUNTRY_INDEX[simplified]
    return None


def normalize_region(text: str | None, country: str | None, *, notes: list[str] | None = None) -> str | None:
    """ISO 3166-2 code (``"US-CA"``) of a region of ``country`` (``None`` if unknown or unsupported)."""
    if not text or not country:
        return None
    country = country.upper()
    table = REGIONS.get(country)
    if table is None:
        return None
    raw = str(text).strip()
    upper = raw.upper().removeprefix(f"{country}-")
    if upper in table:
        return f"{country}-{upper}"
    folded = _fold(raw)
    for code, name in table.items():
        if _fold(name) == folded:
            return f"{country}-{code}"
    alias = REGION_ALIASES.get(country, {}).get(folded)
    if alias:
        return f"{country}-{alias}"
    return None


_LANG_TAG = re.compile(r"^([A-Za-z]{2,3})(?:[-_]([A-Za-z]{4}))?(?:[-_]([A-Za-z]{2}|\d{3}))?$")
_NAME_TO_LANG = {_fold(name): code for code, name in LANGUAGES.items()}


def normalize_language(text: str | None, *, notes: list[str] | None = None) -> str | None:
    """A BCP 47 language tag: ``"en_us"`` -> ``"en-US"``, ``"English"`` -> ``"en"``, ``"zh-hans"`` -> ``"zh-Hans"``."""
    if not text:
        return None
    raw = str(text).strip().split(";")[0].split(",")[0].strip()  # "en-US,en;q=0.9" (Accept-Language)
    match = _LANG_TAG.match(raw)
    if match:
        language = match.group(1).lower()
        language = LANGUAGE_ALIASES.get(language, language)
        if language not in LANGUAGES and len(language) == 2:
            return None
        parts = [language]
        if match.group(2):
            parts.append(match.group(2).title())
        if match.group(3):
            parts.append(match.group(3).upper())
        return "-".join(parts)
    folded = _fold(raw)
    if folded in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[folded]
    if folded in _NAME_TO_LANG:
        return _NAME_TO_LANG[folded]
    return None


# Postal code shapes of common countries (the country's code must appear with them).
_POSTAL: dict[str, re.Pattern[str]] = {
    "US": re.compile(r"\b(\d{5})(?:-(\d{4}))?\b"),
    "CA": re.compile(r"\b([ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z])\s?(\d[ABCEGHJ-NPRSTV-Z]\d)\b", re.I),
    "GB": re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s?(\d[A-Z]{2})\b", re.I),
    "DE": re.compile(r"\b(\d{5})\b"),
    "FR": re.compile(r"\b(\d{5})\b"),
    "ES": re.compile(r"\b(\d{5})\b"),
    "IT": re.compile(r"\b(\d{5})\b"),
    "IN": re.compile(r"\b(\d{3})\s?(\d{3})\b"),
    "NL": re.compile(r"\b(\d{4})\s?([A-Z]{2})\b", re.I),
    "AU": re.compile(r"\b(\d{4})\b"),
    "JP": re.compile(r"〒?\s?\b(\d{3})-(\d{4})\b"),
    "BR": re.compile(r"\b(\d{5})-?(\d{3})\b"),
    "CH": re.compile(r"\b(\d{4})\b"),
    "AT": re.compile(r"\b(\d{4})\b"),
    "BE": re.compile(r"\b(\d{4})\b"),
    "SE": re.compile(r"\b(\d{3})\s?(\d{2})\b"),
    "PL": re.compile(r"\b(\d{2})-(\d{3})\b"),
    "PT": re.compile(r"\b(\d{4})-(\d{3})\b"),
    "IE": re.compile(r"\b([AC-FHKNPRTV-Y]\d{2}|D6W)\s?([0-9AC-FHKNPRTV-Y]{4})\b", re.I),
    "SG": re.compile(r"\b(\d{6})\b"),
    "MX": re.compile(r"\b(\d{5})\b"),
}
_POSTAL_JOIN = {"CA": " ", "GB": " ", "IN": "", "NL": " ", "JP": "-", "BR": "-", "SE": " ", "PL": "-", "PT": "-",
                "IE": " ", "US": "-"}  # fmt: skip


def postal_code(text: str | None, country: str | None) -> str | None:
    """The postal code of ``country``'s format found in ``text``, normalized (``"sw1a1aa"`` -> ``"SW1A 1AA"``)."""
    if not text or not country:
        return None
    pattern = _POSTAL.get(country.upper())
    if pattern is None:
        return None
    matches = list(pattern.finditer(str(text)))
    if not matches:
        return None
    match = matches[-1]  # postal codes come late in an address; house numbers come first
    groups = [g for g in match.groups() if g]
    return _POSTAL_JOIN.get(country.upper(), "").join(g.upper() for g in groups)


@dataclass
class Address:
    """Parts of an address; ``None`` for what could not be identified."""

    street: str | None = None
    city: str | None = None
    region: str | None = None  # ISO 3166-2 when recognised, else as written
    postal_code: str | None = None
    country: str | None = None  # ISO alpha-2
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_address(text: str | None, *, country: str | None = None, notes: list[str] | None = None) -> Address | None:
    """Split a one-line address such as ``"1600 Amphitheatre Pkwy, Mountain View, CA 94043, USA"``.

    Works on comma-separated addresses: the country is read from the last part
    (or ``country=``) and the postal code by that country's format. In the part
    holding the postal code, what remains is a region code (``"CA 94043"``) or
    the city (``"10117 Berlin"``, ``"London SW1A 2AA"``). Otherwise the part
    before it is the city, and the first part is the street. Anything not
    identified stays ``None`` (``"partial-address"`` is noted).
    """
    if not text or not str(text).strip():
        return None
    raw = " ".join(str(text).split())
    parts = [p.strip() for p in re.split(r"\s*[,\n;]\s*", raw) if p.strip()]
    found_country = normalize_country(parts[-1]) if parts else None
    if found_country and len(parts) > 1:
        parts = parts[:-1]
    elif found_country is None and country:
        found_country = country.upper()
        _note(notes, "country-from-default")
    address = Address(raw=raw, country=found_country)
    if len(parts) == 1 and found_country and normalize_country(parts[0]):
        parts = []
    code = postal_code(", ".join(parts[1:]) if len(parts) > 1 else "", found_country) if found_country else None
    address.postal_code = code
    postal_index = None
    if code:
        compact = code.replace(" ", "").replace("-", "")
        for i in range(len(parts) - 1, 0, -1):
            if compact in re.sub(r"[\s-]", "", parts[i]).upper():
                postal_index = i
                break
    street_index = 0 if len(parts) >= 2 else None
    if postal_index is not None:
        remainder = _without_postal(parts[postal_index], code or "")
        region = normalize_region(remainder, found_country) if remainder else None
        if region:
            address.region = region
            before = postal_index - 1
            if before > (street_index or 0):
                address.city = parts[before]
            else:
                address.city = remainder  # a city-state such as Berlin: city and region at once
        elif remainder:
            address.city = remainder
        else:
            # The postal code stands alone: walk back over a region to the city ("..., Springfield, Illinois, 62701").
            for i in range(postal_index - 1, 0, -1):
                if i == street_index:
                    break
                region = normalize_region(parts[i], found_country) if address.region is None else None
                if region:
                    address.region = region
                    continue
                address.city = parts[i]
                break
    else:
        tail = [i for i in range(1, len(parts)) if i != street_index]
        for i in reversed(tail):
            region = normalize_region(re.sub(r"\d", "", parts[i]).strip(), found_country)
            if region and address.region is None:
                address.region = region
                continue
            if address.city is None and not re.search(r"\d", parts[i]):
                address.city = parts[i]
    if street_index is not None and (postal_index is None or postal_index != street_index):
        address.street = parts[street_index]
    if address.city and address.region is None and found_country:
        city_region = normalize_region(address.city, found_country)
        if city_region and found_country == "DE":
            address.region = city_region  # Berlin, Hamburg, Bremen
    if all(v is None for v in (address.street, address.city, address.region, address.postal_code, address.country)):
        return None  # nothing looked like an address
    if None in (address.street, address.city, address.postal_code, address.country):
        _note(notes, "partial-address")
    return address


def _without_postal(part: str, code: str) -> str:
    """``part`` without the postal code in it (however it was spaced)."""
    pattern = r"[\s-]?".join(re.escape(ch) for ch in code.replace(" ", "").replace("-", ""))
    return re.sub(pattern, "", part, count=1, flags=re.I).strip(" -,")


_DECIMAL_COORDS = re.compile(
    r"(-?\d{1,3}(?:\.\d+)?)\s*°?\s*([NS])?\s*[,;/ ]\s*(-?\d{1,3}(?:\.\d+)?)\s*°?\s*([EW])?(?![\d.])", re.I
)
# One angle: an optional hemisphere, degrees with a degree sign, optional minutes and seconds, optional hemisphere.
_ANGLE = re.compile(
    r"(?<![A-Za-z])([NSEW])?\s*(\d{1,3}(?:\.\d+)?)\s*[°º]\s*(?:(\d{1,2}(?:\.\d+)?)\s*['′’]\s*)?"
    r"(?:(\d{1,2}(?:\.\d+)?)\s*(?:[\"″”]|''|′′)\s*)?([NSEW])?(?![A-Za-z])",
    re.I,
)
_KEYED = re.compile(
    r"\blat(?:itude)?\s*[=:]\s*(-?\d{1,2}(?:\.\d+)?)\b.*?\b(?:lon|lng|long|longitude)\s*[=:]\s*(-?\d{1,3}(?:\.\d+)?)",
    re.I | re.S,
)


def _angles(text: str) -> tuple[float, float] | None:
    """Two degree-sign angles (decimal degrees or DMS) with their hemispheres, as ``(lat, lon)``."""
    found: list[tuple[float, str]] = []
    pos = 0
    while len(found) < 2:
        match = _ANGLE.search(text, pos)
        if match is None:
            return None
        minutes, seconds = float(match.group(3) or 0), float(match.group(4) or 0)
        if minutes >= 60 or seconds >= 60:
            return None
        value = float(match.group(2)) + minutes / 60 + seconds / 3600
        prefix, suffix = (match.group(1) or "").upper(), (match.group(5) or "").upper()
        hemisphere = prefix or suffix
        # "N 37° 46', W 122° 25'": a letter after an angle that had one before belongs to the next angle.
        pos = match.start(5) if prefix and suffix else match.end()
        found.append((-value if hemisphere in ("S", "W") else value, hemisphere))
    (first, h1), (second, h2) = found
    if h1 in ("E", "W") and h2 in ("N", "S"):
        return second, first
    return first, second


def parse_coordinates(text: str | None) -> tuple[float, float] | None:
    """``(latitude, longitude)`` from ``"48.8584, 2.2945"``, ``"48.8584° N, 2.2945° E"``,
    ``"N 48° 51' 30\" E 2° 17' 40\""``, ``"lat=48.8584&lon=2.2945"``...; ``None`` if invalid.
    """
    if not text:
        return None
    text = str(text)
    angles = _angles(text) if ("°" in text or "º" in text) else None
    if angles is not None:
        lat, lon = angles
    elif re.search(r"[°º]\s*\d{1,2}(?:\.\d+)?\s*['′’]", text):
        return None  # degrees and minutes we could not pair up: do not guess
    else:
        keyed = _KEYED.search(text)
        if keyed is not None:
            lat, lon = float(keyed.group(1)), float(keyed.group(2))
        else:
            match = _DECIMAL_COORDS.search(text)
            if match is None:
                return None
            lat, lon = float(match.group(1)), float(match.group(3))
            if (match.group(2) or "").upper() == "S":
                lat = -abs(lat)
            if (match.group(4) or "").upper() == "W":
                lon = -abs(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return round(lat, 7), round(lon, 7)
