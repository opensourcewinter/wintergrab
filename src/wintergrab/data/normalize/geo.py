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
* :func:`coordinates_in_url`: the point a map link or map embed shows (Google Maps, OpenStreetMap,
  Apple Maps, Bing Maps, ``geo:`` URIs) -> ``(lat, lon)``.
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
    "coordinates_in_url",
    "normalize_country",
    "normalize_language",
    "normalize_region",
    "parse_address",
    "parse_coordinates",
    "place_meanings",
    "postal_code",
    "read_address",
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


# Countries whose regions are written as codes after a city ("Austin, TX", "Toronto, ON", "Sydney NSW 2000").
_ABBREVIATED = ("US", "CA", "AU")
# Regions that are one city: named alone, they are the city too.
_CITY_STATES = frozenset({"DE-BE", "DE-HH", "DE-HB"})
# A street: a house number, a street word before the name ("Rue de Rivoli") or after it ("Main St", "Friedrichstr.").
_STREET = re.compile(
    r"\d|^(?:rue|avenue|boulevard|chemin|quai|place|via|viale|piazza|corso|calle|avenida|paseo|plaza|carrer|rua|"
    r"ulica|ul\.)\s|\s(?:street|st|road|rd|avenue|ave|boulevard|blvd|lane|ln|drive|dr|way|place|pl|court|ct|square|"
    r"sq|highway|hwy|parkway|pkwy|terrace|crescent|close|laan|straat|gracht|plein|gatan|vej|vei)\b\.?|"
    r"(?:stra(?:ss|ß)e|str\.|weg|gasse|platz|allee|damm)$",
    re.I,
)


def parse_address(text: str | None, *, country: str | None = None, notes: list[str] | None = None) -> Address | None:
    """Split an address, or a place as listings write it, into its parts.

    ``"1600 Amphitheatre Pkwy, Mountain View, CA 94043, USA"``, ``"Unter den Linden 77, 10117 Berlin"``,
    ``"Austin, TX"``, ``"Munich, Bavaria, Germany"``, ``"Toronto, ON M5V 3L9"``. Parts are separated by commas
    and read from the end: the country, a region (a name, or a code of the United States, Canada or
    Australia), the postal code in the country's format (with a region's code beside it, it tells the
    country: ``"IL 62701"``), then the city, and the street before it. A code naming several places
    (``"CA"``: California or Canada; ``"WA"``: Washington or Western Australia) is settled by ``country=``
    (the country the addresses are in); without it, the address has no region or country from it
    (``"ambiguous-place"`` is noted). Anything not identified stays ``None`` (``"partial-address"``).
    """
    address, _ = read_address(text, country=country, notes=notes)
    return address


def read_address(
    text: str | None, *, country: str | None = None, notes: list[str] | None = None
) -> tuple[Address | None, str | None]:
    """:func:`parse_address`, and the part that named several places when one did (``"CA"``)."""
    if not text or not str(text).strip():
        return None, None
    raw = " ".join(str(text).split())
    parts = [p.strip() for p in re.split(r"\s*[,\n;]\s*", raw) if p.strip(" .")]
    hint = normalize_country(country) if country else None
    address = Address(raw=raw)
    unsure: str | None = None
    single = len(parts) == 1

    # 1. The last part: a country, or a region (which tells the country), or a code naming several places.
    if parts:
        named, several = _place_named(parts[-1], None, hint)
        if named and "-" not in named:
            address.country = named
            parts.pop()
        if parts:
            region, several_regions = _place_named(parts[-1], address.country, hint)
            if region and "-" in region:
                address.region, address.country = region, region.split("-")[0]
                text_of_region = parts.pop()
                if region in _CITY_STATES and not any(_part_holds_postal(p, address.country) for p in parts):
                    address.city = text_of_region  # "Berlin", "Hamburg"
            elif several or several_regions:
                unsure = parts.pop()
                _note(notes, "ambiguous-place")

    # 2. The postal code, in the format of the country (known, or given), or beside a region's code ("IL 62701").
    postal_index, remainder = None, ""
    known = [address.country] if address.country else ([hint] if hint else [])
    candidates = known if address.country else known + [c for c in _ABBREVIATED if c not in known]
    for i in range(len(parts) - 1, -1, -1):
        if i == 0 and len(parts) > 1:
            break  # house numbers come first
        for code in candidates:
            found = _postal_in(parts[i], code, certain=code in known)
            if found is None:
                continue
            address.postal_code, region_code, remainder = found
            if address.country is None:
                address.country = code
            if region_code and address.region is None:
                address.region = f"{code}-{region_code}"
            postal_index = i
            break
        if postal_index is not None:
            break
    if address.country is None and hint and unsure is None:
        address.country = hint
        _note(notes, "country-from-default")

    head = parts[:postal_index] if postal_index is not None else list(parts)
    if remainder:  # "10117 Berlin", "London SW1A 2AA", "California 94043"
        region = normalize_region(remainder, address.country) if address.country else None
        if region and address.region is None:
            address.region = region
        if region is None or region in _CITY_STATES:
            address.city = remainder
    elif postal_index is None and address.country is None:
        # a postal code of a country not known, as written: alone, or before or after the city ("10117 Berlin")
        for i in range(len(head) - 1, 0 if len(head) > 1 else -1, -1):
            alone = re.fullmatch(r"\d{3,7}|\d{2,5}[\s-]\d{3,4}", head[i])
            beside = re.fullmatch(r"(\d{4,6})\s+(\D{2,})|(\D{2,}?)\s+(\d{4,6})", head[i])
            if alone and len(head) > 1:
                address.postal_code = head.pop(i)
            elif beside:
                address.postal_code = beside.group(1) or beside.group(4)
                address.city = (beside.group(2) or beside.group(3)).strip(" -")
                postal_index = i
                head = head[:i]
            else:
                continue
            break

    # 3. A region before the postal code or the country ("Munich, Bavaria, Germany").
    if address.region is None and len(head) > 1 and not _STREET.search(head[-1]):
        region, several = _place_named(head[-1], address.country, hint)
        if region and "-" in region:
            address.region = region
            address.country = address.country or region.split("-")[0]
            text_of_region = head.pop()
            if region in _CITY_STATES and address.city is None:
                address.city = text_of_region
        elif several and address.country is None:
            unsure = head.pop()
            _note(notes, "ambiguous-place")

    # 4. The city: the last part left that is not a street; before it, the street.
    if address.city is None and head:
        places = [i for i, part in enumerate(head) if not _STREET.search(part) or (i > 0 and postal_index is not None)]
        tail = [i for i in places if all(j in places for j in range(i, len(head)))]  # the places at the end
        if address.region is None and len(tail) >= 2 and not single:
            address.region = head[tail[-1]]  # "Bengaluru, Karnataka, India": a region not known, as written
            head = head[: tail[-1]]
            tail = tail[:-1]
        if tail and not (single and address.country is None and address.region is None and unsure is None):
            address.city = head[tail[-1]]
            head = head[: tail[-1]]
    elif address.city is not None and postal_index is not None:
        head = parts[:postal_index]
    if head and (postal_index is not None or _STREET.search(head[0])):
        address.street = ", ".join(head)

    if all(v is None for v in (address.street, address.city, address.region, address.postal_code, address.country)):
        return None, unsure  # nothing looked like an address
    if None in (address.street, address.city, address.postal_code, address.country):
        _note(notes, "partial-address")
    return address, unsure


def place_meanings(text: str, *, hint: str | None = None) -> list[str]:
    """Every country (``"DE"``) and region (``"US-CA"``) ``text`` can name: ``["CA", "US-CA"]`` for ``"CA"``,
    ``["GE", "US-GA"]`` for ``"Georgia"``, ``["DE-BY"]`` for ``"Bavaria"``. Region codes are those of the
    United States, Canada and Australia (and of ``hint``, a country), whose regions are written as codes."""
    token = str(text).strip(" .")
    if not token or any(ch.isdigit() for ch in token):
        return []
    meanings: list[str] = []
    named = normalize_country(token)
    if named:
        meanings.append(named)
    short = len(token) <= 3 and token.isalpha() and token.isupper()
    for code, table in REGIONS.items():
        if short:
            if (code in _ABBREVIATED or code == hint) and token in table:
                meanings.append(f"{code}-{token}")
        elif len(token) > 3:
            region = normalize_region(token, code)
            if region:
                meanings.append(region)
    return meanings


def _place_named(part: str, country: str | None, hint: str | None) -> tuple[str | None, bool]:
    """``(place, several)``: the country (``"DE"``) or region (``"US-CA"``) ``part`` names, and whether it
    could name several (``"CA"``: California or Canada), unless ``hint`` (a country) settles it. Only
    ``country``'s regions when it is known."""
    if country:
        token = part.strip(" .")
        return (normalize_region(token, country) if token and not any(c.isdigit() for c in token) else None), False
    meanings = place_meanings(part, hint=hint)
    if len(meanings) == 1:
        return meanings[0], False
    if hint:
        for meaning in meanings:
            if meaning.startswith(f"{hint}-"):
                return meaning, False
        if hint in meanings:
            return hint, False
    return None, len(meanings) > 1


def _postal_in(part: str, country: str, *, certain: bool) -> tuple[str, str | None, str] | None:
    """``(postal code, region code beside it, the rest of the part)`` of ``country``'s format in ``part``.
    When the country is not ``certain``, a code of one of its regions must be beside the postal code."""
    pattern = _POSTAL.get(country)
    if pattern is None:
        return None
    matches = list(pattern.finditer(part))
    if not matches:
        return None
    match = matches[-1]  # postal codes come late; house numbers first
    rest = f"{part[: match.start()]} {part[match.end() :]}".split()
    table = REGIONS.get(country, {})
    region_code = rest[-1].upper().strip(".") if rest and rest[-1].strip(".").upper() in table else None
    if region_code is None and not certain:
        return None
    if region_code is not None:
        rest = rest[:-1]
    groups = [g for g in match.groups() if g]
    return _POSTAL_JOIN.get(country, "").join(g.upper() for g in groups), region_code, " ".join(rest).strip(" -,")


def _part_holds_postal(part: str, country: str | None) -> bool:
    pattern = _POSTAL.get(country or "")
    return bool(pattern and pattern.search(part))


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


_NUMBER = r"(-?\d{1,3}(?:\.\d+)?)"
_POINT_PARAMETERS = "q|query|ll|sll|center|destination|daddr|cp|near|coordinate|map"
# The point a map link or embed is about, each with the order of its numbers: "lat,lon" or "lon,lat".
_MAP_POINTS = (
    ("lat,lon", re.compile(rf"!3d{_NUMBER}!4d{_NUMBER}")),  # Google Maps: a place's own point
    ("lon,lat", re.compile(rf"!2d{_NUMBER}!3d{_NUMBER}")),  # Google Maps embeds
    # a point in the query: Google Maps (q, query, center, destination...), Apple Maps (ll, coordinate), Bing
    # Maps (cp), HERE (map), Waze (ll)
    ("lat,lon", re.compile(rf"[?&;](?:{_POINT_PARAMETERS})={_NUMBER}(?:,|%2C|~)\s*(?:%20)?{_NUMBER}", re.I)),
    ("lat,lon", re.compile(rf"here\.com/l/{_NUMBER},{_NUMBER}", re.I)),  # HERE share links
    ("lat,lon", re.compile(rf"/@{_NUMBER},{_NUMBER}")),  # Google Maps: the view's centre
    ("lat,lon", re.compile(rf"[?&;]mlat={_NUMBER}&(?:amp;)?mlon={_NUMBER}", re.I)),  # OpenStreetMap: its marker
    ("lat,lon", re.compile(rf"#map=\d{{1,2}}/{_NUMBER}/{_NUMBER}")),  # OpenStreetMap: the view
    ("lat,lon", re.compile(rf"^geo:{_NUMBER},{_NUMBER}", re.I)),  # geo: URIs (RFC 5870)
)
_MAP_HOSTS = re.compile(
    r"^(?:geo:|https?://(?:[\w-]+\.)*(?:google\.[a-z.]+/maps|maps\.google\.[a-z.]+|googleapis\.com/maps|goo\.gl/maps|"
    r"openstreetmap\.org|osm\.org|maps\.apple\.com|bing\.com/maps|here\.com|waze\.com))",
    re.I,
)


def coordinates_in_url(url: str | None) -> tuple[float, float] | None:
    """``(latitude, longitude)`` of the point a map link or embed shows: Google Maps (a place's point, a query,
    a view's centre, embeds), OpenStreetMap (a marker, a view), Apple Maps, Bing Maps, HERE, Waze, and
    ``geo:`` URIs; ``None`` for other links, and for links naming no point (an address to look up)."""
    if not url or not _MAP_HOSTS.match(str(url).strip()):
        return None
    text = str(url).strip()
    for order, pattern in _MAP_POINTS:
        match = pattern.search(text)
        if match is None:
            continue
        first, second = float(match.group(1)), float(match.group(2))
        lat, lon = (first, second) if order == "lat,lon" else (second, first)
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return round(lat, 7), round(lon, 7)
    return None
