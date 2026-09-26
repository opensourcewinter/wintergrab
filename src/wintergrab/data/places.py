"""Where records are: each record's place, read from its fields and normalized; distances; and records
filtered and grouped by place.

::

    from wintergrab.data.places import distance_km, group_records, place_of, places_of

    place_of({"location": "Austin, TX"})              # Place(country='US', region='US-TX', city='Austin')
    place_of({"address": "Unter den Linden 77, 10117 Berlin", "country": "Germany"}).postal_code  # '10117'
    place_of({"location": "Remote - US"})             # Place(country='US', remote=True)
    place_of({"location": "San Francisco, CA"})       # Place(city='San Francisco', unsure='CA'): California? Canada?
    place_of({"location": "San Francisco, CA"}, country="US").region    # 'US-CA'

    distance_km((52.52, 13.405), (48.1374, 11.5755))  # 504.2: Berlin to Munich

    for group in group_records(jobs, "country", stats=["salary"]):
        print(group.label, group.count, group.stats)

A record's place comes from the fields it has (:data:`PLACE_FIELDS`; ``fields=`` names others): an
address (a one-line address, or an address object: WINTERGRAB's, or schema.org's ``PostalAddress``), a
location as listings write it (``"Austin, TX"``, ``"Remote - US"``, ``"Hybrid: Amsterdam, NL"``), and
separate city, region, postal code and country fields, which are trusted over what an address says.
Countries become ISO 3166-1 codes (``"Deutschland"``, ``"Germany"``, ``"DEU"``: ``"DE"``); regions become
ISO 3166-2 codes where WINTERGRAB knows the country's regions (United States, Canada, Australia,
Germany, United Kingdom: ``"Calif."``... ``"California"``, ``"CA"``: ``"US-CA"``), and are kept as
written elsewhere; postal codes are put in their country's format.

Coordinates are read when a record states them (a ``coordinates`` or ``geo`` field, ``latitude`` and
``longitude``, GeoJSON points) or links to a map showing them (:func:`coordinates_in_url`); nothing is
looked up: no address is sent anywhere to be geocoded.

A two-letter code naming several places (``"CA"``: California or Canada; ``"IN"``: Indiana or India;
``"WA"``: Washington or Western Australia) is settled by the country the records are in, when given
(``country="US"``). :func:`places_of` settles it from the records themselves when most of those that
name a country name the same one; otherwise the place has no region or country from it, and says so
(``unsure``).
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .expressions import get_path
from .normalize.geo import (
    coordinates_in_url,
    normalize_country,
    normalize_region,
    parse_coordinates,
    place_meanings,
    postal_code,
    read_address,
)
from .normalize.text import clean_text
from .reference import COUNTRIES, REGIONS

__all__ = [
    "DEFAULT_PARTS",
    "PLACE_FIELDS",
    "PLACE_PARTS",
    "Group",
    "Place",
    "coordinates_of",
    "distance_km",
    "group_records",
    "in_box",
    "place_of",
    "places_of",
]

#: The fields a record's place is read from, by what they hold; the first one a record has is used.
PLACE_FIELDS: dict[str, tuple[str, ...]] = {
    "address": ("address", "full_address", "postal_address"),
    "location": ("location", "job_location", "place"),
    "street": ("street", "street_address", "streetAddress"),
    "city": ("city", "locality", "town", "addressLocality"),
    "region": ("region", "state", "province", "addressRegion"),
    "postal_code": ("postal_code", "postcode", "zip", "zip_code", "zipcode", "postalCode"),
    "country": ("country", "country_code", "addressCountry"),
    "coordinates": ("coordinates", "geo"),
    "latitude": ("latitude", "lat"),
    "longitude": ("longitude", "lon", "lng", "long"),
    "map": ("map", "map_url", "maps_url", "directions"),
    "remote": ("remote",),
}
#: What a place has, and what records can be grouped by.
PLACE_PARTS = ("country", "region", "city", "postal_code", "street", "coordinates", "remote")
#: The parts added to records unless others are asked for.
DEFAULT_PARTS = ("country", "region", "city", "postal_code", "coordinates")

_EARTH_RADIUS_KM = 6371.0088  # the mean radius (IUGG)
_ISO_REGION = re.compile(r"[A-Z]{2}-[A-Z0-9]{1,3}")
# Work arrangements written with a location: "Remote - US", "Hybrid: Amsterdam", "Berlin (remote)".
_MODE = r"(?:100%\s*)?(?:fully\s+)?(?:remote|hybrid|on-?site|in[- ]office|work from home|wfh|home[- ]?based|anywhere)"
_MODE_BEFORE = re.compile(rf"^\s*({_MODE})\b\s*(?:[-:\u2013\u2014|/,]|\bin\b|\()?\s*", re.I)
_MODE_AFTER = re.compile(
    rf"\s*(?:[(\[]\s*({_MODE})[^)\]]*[)\]]|[-,/|\u2013\u2014]\s*({_MODE})|\s+or\s+({_MODE}))\s*$", re.I
)
_REMOTE = re.compile(r"remote|work from home|wfh|home|anywhere", re.I)
# Not one place: areas and non-answers ("Remote, Europe" is remote, somewhere in Europe).
_NOWHERE = re.compile(
    r"^(?:worldwide|global|international|anywhere|nationwide|flexible|multiple locations|various( locations)?|"
    r"n/?a|tbd|tba|see description|europe|eu|eea|emea|apac|asia|asia[- ]pacific|africa|oceania|americas|"
    r"north america|south america|latin america|latam|middle east|mena|dach|nordics|benelux)$",
    re.I,
)


@dataclass
class Place:
    """Where a record is; ``None`` for what it does not say.

    Attributes:
        country: ISO 3166-1 alpha-2 (``"DE"``).
        region: ISO 3166-2 (``"US-CA"``) where the country's regions are known, else as written.
        city: As written (the locality: ``addressLocality``).
        postal_code: In the country's format when it is known (``"SW1A 1AA"``), else as written.
        street: As written.
        coordinates: ``(latitude, longitude)``, when the record states them or links to a map showing them.
        remote: The record says it is remote (a job: ``"Remote - US"``); its place, if any, is where.
        unsure: A part naming several places that nothing settled (``"CA"``: California or Canada).
        sources: The field each part was read from (``{"city": "location"}``).
    """

    country: str | None = None
    region: str | None = None
    city: str | None = None
    postal_code: str | None = None
    street: str | None = None
    coordinates: tuple[float, float] | None = None
    remote: bool = False
    unsure: str | None = None
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def known(self) -> bool:
        """Whether anything is known of where."""
        return any(v is not None for v in (self.country, self.region, self.city, self.postal_code, self.coordinates))

    def key(self, part: str) -> Any:
        """What records are grouped by for ``part``: a city with its region or country (two Portlands are two
        cities), the others as they are."""
        if part == "city":
            if self.city is None:
                return None
            return (_fold(self.city), self.region if self.region and "-" in self.region else self.country)
        if part == "region" and self.region and not _ISO_REGION.fullmatch(self.region):
            return _fold(self.region)  # as written: "Karnataka", "karnataka"
        return getattr(self, part)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {part: getattr(self, part) for part in PLACE_PARTS}
        out["coordinates"] = list(self.coordinates) if self.coordinates else None
        if self.unsure:
            out["unsure"] = self.unsure
        return out

    def __repr__(self) -> str:
        shown = [f"{k}={v!r}" for k, v in self.to_dict().items() if v not in (None, False)]
        return f"Place({', '.join(shown)})"


def place_of(
    record: Mapping[str, Any], *, country: str | None = None, fields: Mapping[str, str | Sequence[str]] | None = None
) -> Place:
    """The place of ``record`` (see the module docs). ``country`` is the country its addresses are in when
    they do not say (and settles codes naming several places); ``fields`` maps parts (``"city"``,
    ``"address"``...: :data:`PLACE_FIELDS`) to the record's own field names (dotted paths work)."""
    names = dict(PLACE_FIELDS)
    for part, given in (fields or {}).items():
        if part not in names:
            raise ValueError(f"unknown place part {part!r}; known: {', '.join(names)}")
        names[part] = (given,) if isinstance(given, str) else tuple(given)

    def read(part: str) -> tuple[Any, str | None]:
        for name in names[part]:
            value = get_path(record, name)
            if value not in (None, "", [], {}):
                return value, name
        return None, None

    place = Place()
    stated_country, country_field = read("country")
    if isinstance(stated_country, Mapping):
        stated_country = stated_country.get("name") or stated_country.get("identifier")
    explicit = normalize_country(str(stated_country)) if stated_country else None
    hint = explicit or (normalize_country(country) if country else None)

    remote, remote_field = read("remote")
    if remote is True or (isinstance(remote, str) and remote.strip().lower() in ("true", "yes", "remote", "1")):
        place.remote = True
        place.sources["remote"] = remote_field or "remote"

    address, address_field = read("address")
    if address is not None:
        _merge(place, _read_value(address, hint), address_field or "address")
    location, location_field = read("location")
    if location is not None:
        _merge(place, _read_value(location, hint), location_field or "location")

    # separate fields say more surely than an address does
    if explicit:
        place.country = explicit
        place.sources["country"] = country_field or "country"
    elif place.country is None and hint and place.unsure is None and place.known:
        place.country = hint
        place.sources["country"] = "country="
    for part in ("street", "city"):
        value, name = read(part)
        if isinstance(value, (str, int)) and clean_text(str(value)):
            setattr(place, part, clean_text(str(value)))
            place.sources[part] = name or part
    region, region_field = read("region")
    if isinstance(region, str) and region.strip():
        code = normalize_region(region, place.country or hint) if (place.country or hint) else _region_anywhere(region)
        place.region = code or clean_text(region)
        if code and place.country is None:
            place.country = code.split("-")[0]
            place.sources["country"] = region_field or "region"
        place.sources["region"] = region_field or "region"
    postal, postal_field = read("postal_code")
    if isinstance(postal, (str, int)) and str(postal).strip():
        place.postal_code = postal_code(str(postal), place.country) or clean_text(str(postal))
        place.sources["postal_code"] = postal_field or "postal_code"
    if place.region and place.country and "-" in place.region and not place.region.startswith(f"{place.country}-"):
        place.region = None  # a region of another country than the one stated

    place.coordinates, source = _coordinates_in(read)
    if place.coordinates:
        place.sources["coordinates"] = source or "coordinates"
    if place.unsure and place.country:
        place.unsure = None
    return place


def places_of(
    records: Iterable[Mapping[str, Any]],
    *,
    country: str | None = None,
    fields: Mapping[str, str | Sequence[str]] | None = None,
    settle: float = 0.8,
) -> tuple[list[Place], str | None]:
    """The places of ``records``, and the country that settled the most codes naming several places, if any.

    Without ``country``, a code naming several places is read as the one in the country the other records
    name most, among those it could be in: ``"CA"`` among records in the United States is California,
    among records in Canada, Canada; ``"NL"`` among records in the Netherlands, the Netherlands (not
    Newfoundland and Labrador). That country must be named by at least three records, and by at least
    ``settle`` of the records naming any of the countries the code could be in."""
    rows = list(records)
    places = [place_of(r, country=country, fields=fields) for r in rows]
    if country:
        return places, None
    named = Counter(p.country for p in places if p.country)
    settled: Counter[str] = Counter()
    for i, place in enumerate(places):
        if not place.unsure:
            continue
        countries = {meaning.split("-")[0] for meaning in place_meanings(place.unsure)}
        counts = sorted(((named[c], c) for c in countries), reverse=True)
        total = sum(n for n, _ in counts)
        if not counts or counts[0][0] < 3 or counts[0][0] < settle * total:
            continue
        again = place_of(rows[i], country=counts[0][1], fields=fields)
        if again.country == counts[0][1] and not again.unsure:
            again.sources["country"] = "other records"  # the country they name most
            places[i] = again
            settled[again.country] += 1
    return places, settled.most_common(1)[0][0] if settled else None


# ---------------------------------------------------------------------------------------------- #
# coordinates and distances
# ---------------------------------------------------------------------------------------------- #
def coordinates_of(value: Any) -> tuple[float, float] | None:
    """``(latitude, longitude)`` of a value: a pair, ``"48.8584, 2.2945"`` (or degrees, minutes and
    seconds), ``{"latitude": ..., "longitude": ...}`` (schema.org ``GeoCoordinates``), a GeoJSON point
    (``{"type": "Point", "coordinates": [lon, lat]}``), a map link, or a :class:`Place`; ``None`` otherwise."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Place):
        return value.coordinates
    if isinstance(value, Mapping):
        if str(value.get("type", "")).lower() == "point" and isinstance(value.get("coordinates"), (list, tuple)):
            pair = value["coordinates"]
            return _pair(pair[1], pair[0]) if len(pair) >= 2 else None  # GeoJSON: longitude first
        lat = _first(value, ("latitude", "lat"))
        lon = _first(value, ("longitude", "lon", "lng", "long"))
        if lat is not None and lon is not None:
            return _pair(lat, lon)
        for key in ("geo", "coordinates", "position"):
            if key in value:
                return coordinates_of(value[key])
        return None
    if isinstance(value, (list, tuple)):
        return _pair(value[0], value[1]) if len(value) == 2 else None
    text = str(value).strip()
    found = coordinates_in_url(text) if re.match(r"(?:https?:|geo:)", text, re.I) else parse_coordinates(text)
    return _pair(*found) if found else None


def distance_km(a: Any, b: Any) -> float | None:
    """The great-circle distance between two points, in kilometres (to 0.1 km); ``None`` when either has no
    coordinates. Points are anything :func:`coordinates_of` reads. The Earth is taken as a sphere of its
    mean radius, which is within 0.5% of the distance on its ellipsoid."""
    first, second = coordinates_of(a), coordinates_of(b)
    if first is None or second is None:
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, (*first, *second))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return round(2 * _EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h))), 1)


def in_box(point: Any, south: float, west: float, north: float, east: float) -> bool | None:
    """Whether a point is inside a box of latitudes and longitudes (a box across the 180th meridian has
    ``west > east``); ``None`` when the point has no coordinates."""
    coordinates = coordinates_of(point)
    if coordinates is None:
        return None
    lat, lon = coordinates
    if not south <= lat <= north:
        return False
    return west <= lon <= east if west <= east else (lon >= west or lon <= east)


# ---------------------------------------------------------------------------------------------- #
# grouping
# ---------------------------------------------------------------------------------------------- #
@dataclass
class Group:
    """Records sharing a value (:func:`group_records`).

    Attributes:
        key: The value (``"DE"``; for a city, ``("portland", "US-OR")``); ``None`` for records without one.
        label: The value to show (``"Germany (DE)"``, ``"Portland (US-OR)"``, ``"(none)"``).
        count: How many records.
        share: Their share of the records (0 to 1).
        stats: For each field asked for, its numbers' ``count``, ``min``, ``max``, ``mean`` and ``median``
            (money: of the amounts, by currency: ``"salary (EUR)"``).
    """

    key: Any
    label: str
    count: int
    share: float
    stats: dict[str, dict[str, float | int]] = field(default_factory=dict)
    indices: list[int] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict[str, Any]:
        key = list(self.key) if isinstance(self.key, tuple) else self.key
        return {"key": key, "label": self.label, "count": self.count, "share": self.share, "stats": self.stats}


def group_records(
    records: Iterable[Mapping[str, Any]],
    by: str | Callable[[Mapping[str, Any]], Any],
    *,
    stats: Sequence[str] = (),
    country: str | None = None,
    fields: Mapping[str, str | Sequence[str]] | None = None,
    places: Sequence[Place] | None = None,
) -> list[Group]:
    """Group ``records`` by a place part (``"country"``, ``"region"``, ``"city"``, ``"postal_code"``,
    ``"remote"``: read with :func:`places_of`, so ``"Germany"``, ``"DE"`` and ``"Deutschland"`` are one
    group), by another field (a dotted path), or by a function of the record. Largest groups first; the
    records with no value are one group (key ``None``). ``stats`` names numeric fields to sum up in each
    group. ``places`` gives places already read (one per record)."""
    rows = list(records)
    if places is not None and len(places) != len(rows):
        raise ValueError(f"{len(places)} place(s) for {len(rows)} record(s): give one place per record")
    if isinstance(by, str) and by in PLACE_PARTS:
        if places is None:
            places, _ = places_of(rows, country=country, fields=fields)
        keys = [place.key(by) for place in places]
    elif isinstance(by, str):
        keys = [_hashable(get_path(r, by)) for r in rows]
    elif callable(by):
        keys = [_hashable(by(r)) for r in rows]
    else:
        raise TypeError(f"group by a field name or a function, not {type(by).__name__}")
    members: dict[Any, list[int]] = {}
    for i, key in enumerate(keys):
        members.setdefault(key, []).append(i)
    total = len(rows) or 1
    groups = []
    for key, indices in members.items():
        spelled = [_spelling(by, rows[i], places[i] if places is not None else None) for i in indices]
        label = _label(by, key, Counter(s for s in spelled if s).most_common(1)[0][0] if any(spelled) else None)
        groups.append(
            Group(key, label, len(indices), round(len(indices) / total, 4), _stats(rows, indices, stats), indices)
        )
    groups.sort(key=lambda g: (g.key is None, -g.count, g.label.casefold()))
    return groups


def _stats(rows: list[Mapping[str, Any]], indices: list[int], names: Sequence[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name in names:
        by_currency: dict[str | None, list[float]] = {}
        for i in indices:
            value = get_path(rows[i], name)
            for item in value if isinstance(value, list) else [value]:
                amount, currency = _number(item)
                if amount is not None:
                    by_currency.setdefault(currency, []).append(amount)
        for currency, values in sorted(by_currency.items(), key=lambda kv: (-len(kv[1]), str(kv[0]))):
            label = name if currency is None or len(by_currency) == 1 else f"{name} ({currency})"
            summary: dict[str, Any] = {
                "count": len(values),
                "min": _round(min(values)),
                "max": _round(max(values)),
                "mean": _round(statistics.fmean(values)),
                "median": _round(statistics.median(values)),
            }
            if currency is not None:
                summary["currency"] = currency
            out[label] = summary
    return out


def _number(value: Any) -> tuple[float | None, str | None]:
    """A number, or a money amount and its currency; ``(None, None)`` for anything else."""
    if isinstance(value, bool) or value is None:
        return None, None
    if isinstance(value, (int, float)):
        return (float(value), None) if math.isfinite(value) else (None, None)
    if isinstance(value, Mapping):
        amount = value.get("amount", value.get("value"))
        number, _ = _number(amount)
        currency = value.get("currency")
        return number, str(currency) if currency else None
    amount = getattr(value, "amount", None)
    if amount is not None:
        return float(amount), getattr(value, "currency", None)
    return None, None


def _round(value: float) -> float | int:
    rounded = round(value, 2)
    return int(rounded) if rounded == int(rounded) else rounded


def _label(by: Any, key: Any, spelling: str | None) -> str:
    if key is None:
        return "(none)"
    if by == "country":
        name = COUNTRIES[key].name if key in COUNTRIES else None
        return f"{name} ({key})" if name else str(key)
    if by == "region":
        country, _, code = str(key).partition("-")
        name = REGIONS.get(country, {}).get(code) if code else None
        return f"{name} ({key})" if name else (spelling or str(key))
    if by == "city":
        city, where = key
        return f"{spelling or city} ({where})" if where else (spelling or city)
    if by == "remote":
        return "remote" if key else "not remote"
    if by == "coordinates":
        return f"{key[0]}, {key[1]}"
    return spelling or str(key)


def _spelling(by: Any, record: Mapping[str, Any], place: Place | None) -> str | None:
    if by in ("city", "region") and place is not None:
        return getattr(place, by)
    if isinstance(by, str) and by not in PLACE_PARTS:
        value = get_path(record, by)
        return value if isinstance(value, str) else None
    return None


def _hashable(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_hashable(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    if isinstance(value, str):
        return _fold(value) or None  # "Acme", "ACME": one group, shown as spelled most
    return value


# ---------------------------------------------------------------------------------------------- #
# reading
# ---------------------------------------------------------------------------------------------- #
def _read_value(value: Any, hint: str | None) -> Place:
    """The place a field's value says: a one-line address or location, or an address object."""
    place = Place()
    if isinstance(value, Mapping):
        return _read_mapping(value, hint)
    if isinstance(value, list):
        for item in value:  # several locations: the first one that says where
            found = _read_value(item, hint)
            if found.known or found.remote:
                return found
        return place
    text = clean_text(str(value)) or ""
    text, remote = _work_mode(text)
    place.remote = remote
    if not text or _NOWHERE.match(text):
        return place
    address, unsure = read_address(text, country=hint)
    if address is not None:
        place.country, place.region, place.city = address.country, address.region, address.city
        place.postal_code, place.street = address.postal_code, address.street
    elif "," not in text and not any(ch.isdigit() for ch in text) and len(text) <= 60:
        place.city = text  # a location of one part: a city ("Springfield")
    place.unsure = unsure if place.country is None else None
    return place


def _read_mapping(value: Mapping[str, Any], hint: str | None) -> Place:
    """An address object (WINTERGRAB's: street, city, region, postal_code, country; schema.org's
    PostalAddress), or a schema.org Place (its ``address`` and ``geo``)."""
    if "address" in value and not any(k in value for k in ("city", "addressLocality", "street", "streetAddress")):
        inner = _read_value(value["address"], hint)
        inner.coordinates = inner.coordinates or coordinates_of(value.get("geo"))
        return inner
    place = Place()
    country = value.get("country", value.get("addressCountry"))
    if isinstance(country, Mapping):
        country = country.get("name") or country.get("identifier")
    place.country = normalize_country(str(country)) if country else None
    where = place.country or hint
    region = value.get("region", value.get("addressRegion"))
    if isinstance(region, str) and region.strip():
        code = normalize_region(region, where) if where else _region_anywhere(region)
        place.region = code or clean_text(region)
        if code and place.country is None:
            place.country = code.split("-")[0]
    for part, keys in (("city", ("city", "addressLocality")), ("street", ("street", "streetAddress"))):
        text = next((value[k] for k in keys if isinstance(value.get(k), str) and value[k].strip()), None)
        setattr(place, part, clean_text(text) if text else None)
    postal = value.get("postal_code", value.get("postalCode"))
    if postal not in (None, ""):
        place.postal_code = postal_code(str(postal), place.country or hint) or clean_text(str(postal))
    raw = value.get("raw")
    if not place.known and isinstance(raw, str) and raw.strip():
        return _read_value(raw, hint)
    place.coordinates = coordinates_of(value.get("geo")) if "geo" in value else None
    return place


def _merge(place: Place, found: Place, source: str) -> None:
    """Fill what ``place`` does not know from ``found`` (read from the field ``source``)."""
    for part in ("country", "region", "city", "postal_code", "street", "coordinates"):
        if getattr(place, part) is None and getattr(found, part) is not None:
            setattr(place, part, getattr(found, part))
            place.sources[part] = source
    if found.remote and not place.remote:
        place.remote = True
        place.sources["remote"] = source
    if place.country is None and found.unsure and place.unsure is None:
        place.unsure = found.unsure


def _coordinates_in(read: Callable[[str], tuple[Any, str | None]]) -> tuple[tuple[float, float] | None, str | None]:
    value, name = read("coordinates")
    if value is not None:
        found = coordinates_of(value)
        if found:
            return found, name
    lat, lat_field = read("latitude")
    lon, _ = read("longitude")
    if lat is not None and lon is not None:
        found = _pair(lat, lon)
        if found:
            return found, lat_field
    for part in ("address", "location"):
        value, name = read(part)
        if isinstance(value, Mapping):
            found = coordinates_of(value.get("geo")) if "geo" in value else None
            if found:
                return found, name
    link, link_field = read("map")
    if isinstance(link, str):
        found = coordinates_in_url(link)
        if found:
            return found, link_field
    return None, None


def _work_mode(text: str) -> tuple[str, bool]:
    """``text`` without the work arrangement written with it, and whether that says remote."""
    remote = False
    for _ in range(2):
        before = _MODE_BEFORE.match(text)
        if before:
            remote = remote or bool(_REMOTE.search(before.group(1)))
            text = text[before.end() :].strip()
            text = text[:-1].strip() if text.endswith(")") and "(" not in text else text
            continue
        after = _MODE_AFTER.search(text)
        if after:
            mode = next(g for g in after.groups() if g)
            remote = remote or bool(_REMOTE.search(mode))
            text = text[: after.start()].strip()
            continue
        break
    return text.strip(" -,:|/"), remote


def _region_anywhere(text: str) -> str | None:
    """The region ``text`` names when it names one region and no country (``"Bavaria"``: ``"DE-BY"``;
    ``"ON"``: ``"CA-ON"``; not ``"CA"``, California or Canada)."""
    meanings = place_meanings(text)
    return meanings[0] if len(meanings) == 1 and "-" in meanings[0] else None


def _first(value: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    return next((value[k] for k in keys if value.get(k) not in (None, "")), None)


def _pair(lat: Any, lon: Any) -> tuple[float, float] | None:
    try:
        latitude, longitude = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180) or (latitude == 0 and longitude == 0):
        return None  # "0, 0" is a map's default, not a place
    return round(latitude, 7), round(longitude, 7)


def _fold(text: str) -> str:
    return " ".join(text.casefold().split())
