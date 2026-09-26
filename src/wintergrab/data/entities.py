"""Entity resolution: which names in your data are the same company, brand, product, person or place.

::

    >>> resolver = EntityResolver("company")
    >>> for name in ("Apple Inc.", "APPLE INC", "Apple", "Apple Computer, Inc.", "Apple Computer", "Apple Records"):
    ...     resolver.add(name, source="https://...")
    >>> result = resolver.resolve()
    >>> [(entity.name, len(entity.mentions)) for entity in result.entities]
    [('Apple Inc.', 3), ('Apple Computer, Inc.', 2), ('Apple Records', 1)]
    >>> result.review
    [Match('Apple Inc.' ~ 'Apple Computer, Inc.', score=0.818, 'review', ["+3.5 same name apart from 'computer'"])]

"Apple" and "Apple Computer" may be one company, or two: with nothing else
to go on they are listed for review, not merged. Evidence decides: give
them the same website and they merge.

How it works:

1. Names are normalized for their kind. For organizations, legal forms
   (``Inc.``, ``GmbH``, ``S.A.``...), punctuation, accents and ``&``/``and``
   are set aside; for people, titles, ``"Last, First"`` order, initials and
   ``Jr.``/``Sr.``; for products, units (``128 GB`` = ``128GB``) and model
   numbers; for places, ``St.``/``Mt.`` and ``"Springfield, IL"`` qualifiers.
2. Only plausible pairs are compared: names sharing a significant word, a
   name and its initials (``IBM``), or an identifier (website, phone,
   GTIN...). Identical mentions are compared once.
3. A pair's score is a probability from log-odds evidence: a prior against a
   match (-2) plus a weight per signal (the same name +6, the same name but
   for generic words such as "Computer" or "Group" +3.5, the same website +4,
   the same GTIN +8, different LEIs -8, different model numbers -6...).
4. Pairs scoring ``merge_threshold`` (0.95) or more are merged, strongest
   first, unless the two groups have conflicting identifiers (two GTINs, a
   place in two countries, "Jr." and "Sr."). Pairs between
   ``review_threshold`` (0.5) and the merge threshold, and merges blocked by
   a conflict, are listed in :attr:`Resolution.review`: never merged
   silently. So are merges that would join two mentions that compared as
   different entities, and a mention that matches two conflicting groups
   equally well.

Every entity keeps its mentions (name, source, attributes), the matches
that merged them with their scores and reasons, and a confidence: the
score of its weakest merge.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import combinations
from typing import Any
from urllib.parse import urlsplit

from ..fetchers.resources import registrable_domain
from .normalize import (
    is_placeholder,
    normalize_country,
    normalize_email,
    normalize_phone,
    normalize_region,
    parse_coordinates,
)
from .reference import COUNTRIES, REGION_ALIASES, REGIONS

__all__ = ["KINDS", "Entity", "EntityResolver", "Match", "Mention", "Name", "Resolution", "normalize_name"]

#: The kinds of entities :class:`EntityResolver` understands.
KINDS = ("company", "organization", "brand", "product", "person", "location")
_ORGANIZATIONS = frozenset({"company", "organization", "brand"})

_PRIOR = -2.0  # log-odds that two plausible-looking mentions are the same entity, before evidence

# --------------------------------------------------------------------------------------------- #
# names
# --------------------------------------------------------------------------------------------- #
# straight, curly, back and modifier apostrophes, and the acute accent used as one
_APOSTROPHES = re.compile("['`" + chr(0x2019) + chr(0xB4) + chr(0x2BC) + "]")
_AMPERSAND = re.compile(r"\s*[&+]\s*")
_DOTTED = re.compile(r"\b[^\W\d_](?:\.[^\W\d_])+\.?")  # "s.a.", "i.b.m" -> "sa", "ibm"
_NON_WORD = re.compile(r"[\W_]+")
_STOPWORDS = frozenset(
    {
        "the", "and", "of", "for", "a", "an", "in", "on", "at", "de", "del", "la", "le", "les", "der", "die",
        "das", "und", "et", "y"
    }
)  # fmt: skip

_LEGAL_FORMS = tuple(
    sorted(
        {
            tuple(form.split())
            for form in (
                "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "llc", "llp",
                "lp", "plc", "public limited company", "private limited", "pvt", "pvt ltd", "pty", "pty ltd",
                "pte", "pte ltd", "gmbh", "mbh", "ag", "kg", "kgaa", "gmbh and co kg", "and co kg", "co kg",
                "se", "sa", "sas", "sarl", "sl", "slu", "srl", "spa", "bv", "nv", "oy", "oyj", "ab", "publ",
                "as", "asa", "aps", "kk", "bhd", "sdn bhd", "tbk", "jsc", "pjsc", "ooo", "zao", "oao", "ltda",
                "sa de cv", "de cv", "sro", "doo", "kft", "zrt", "nyrt", "sp z oo", "gk", "lda", "uab",
            )
        },
        key=len,
        reverse=True,
    )
)  # fmt: skip
_LEGAL_BY_LAST: dict[str, list[tuple[str, ...]]] = {}
for _form in _LEGAL_FORMS:  # longest first
    _LEGAL_BY_LAST.setdefault(_form[-1], []).append(_form)

# Legal forms that are the same kind of company for our purposes ("Inc." = "Corp.", "Ltd" = "Limited").
_LEGAL_FAMILIES = {
    **dict.fromkeys(("inc", "incorporated", "corp", "corporation", "co", "company"), "corporation"),
    **dict.fromkeys(("ltd", "limited", "plc", "public limited company", "private limited", "pvt", "pvt ltd", "pty",
                     "pty ltd", "pte", "pte ltd", "bhd", "sdn bhd"), "limited"),
    **dict.fromkeys(("gmbh", "mbh"), "gmbh"),
    **dict.fromkeys(("kg", "kgaa", "gmbh and co kg", "and co kg", "co kg"), "kg"),
    **dict.fromkeys(("llp", "lp"), "partnership"),
}  # fmt: skip


def _legal_family(form: str) -> str:
    return _LEGAL_FAMILIES.get(form, form)


# Generic words that often come and go in an organization's name ("Apple Computer" / "Apple").
_DESCRIPTORS = frozenset(
    {
        "group", "groups", "holding", "holdings", "international", "intl", "global", "worldwide", "enterprises",
        "enterprise", "industries", "industry", "technologies", "technology", "tech", "systems", "solutions",
        "services", "computer", "computers", "software", "labs", "laboratories", "partners", "associates",
        "consulting", "brands", "products", "manufacturing", "trading", "ventures", "capital", "networks",
        "communications", "online", "digital", "official", "europe", "european", "america", "americas", "usa",
        "us", "uk", "asia", "pacific", "emea", "com", "net", "org", "io"
    }
)  # fmt: skip
_TITLES = frozenset(
    {
        "mr", "mrs", "ms", "miss", "mx", "dr", "doctor", "prof", "professor", "sir", "dame", "lord", "lady",
        "rev", "reverend", "fr", "hon"
    }
)  # fmt: skip
_GENERATIONS = {"jr": "jr", "junior": "jr", "sr": "sr", "senior": "sr", "ii": "ii", "iii": "iii", "iv": "iv"}
_POSTNOMINALS = frozenset(
    {
        "phd", "md", "mba", "esq", "dds", "cpa"
    }
)  # fmt: skip
_AFTER_COMMA = _POSTNOMINALS | frozenset({"jd", "llm", "msc", "bsc", "ma", "ba", "mphil", "dphil", "rn", "pe"})
_PLACE_WORDS = {"st": "saint", "ste": "sainte", "mt": "mount", "ft": "fort"}
# units written after numbers in product names, and how they are spelled once joined ("128 GB" -> "128gb")
_UNITS = {
    **{unit: unit for unit in ("gb", "tb", "mb", "kb", "mah", "wh", "w", "kw", "v", "mm", "cm", "m", "km", "kg",
                               "g", "mg", "lb", "oz", "l", "ml", "in", "hz", "khz", "mhz", "ghz", "mp", "k", "pc",
                               "pack")},
    "lbs": "lb", "inch": "in", "inches": "in", "pcs": "pc", "pk": "pack",
}  # fmt: skip
_NUMBER_UNIT = re.compile(r"(\d+(?:\.\d+)?)\s*(" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r")\b")


@dataclass(frozen=True)
class Name:
    """A name prepared for comparison (see :func:`normalize_name`).

    Attributes:
        text: The name as given.
        kind: One of :data:`KINDS`.
        key: The normalized name (``"apple computer"`` for "Apple Computer, Inc.").
        tokens: Its words.
        core: The distinctive words: for organizations, without generic words ("Group",
            "Computer", "International"...).
        qualifier: What was set aside: a legal form (``"inc"``), a generation (``"jr"``), a
            place's area (``"il"`` in "Springfield, IL").
        codes: Words with digits: model numbers, sizes (``{"15", "128gb"}``).
    """

    text: str
    kind: str
    key: str
    tokens: tuple[str, ...]
    core: tuple[str, ...]
    qualifier: str | None = None
    codes: frozenset[str] = frozenset()


def _fold(text: str) -> str:
    """Lower case, without accents, compatibility forms unified (``ﬁ`` -> ``fi``, ``ß`` -> ``ss``)."""
    text = unicodedata.normalize("NFKD", text).casefold()
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _words(text: str) -> list[str]:
    return [w for w in _NON_WORD.split(text) if w]


def _codes(tokens: Iterable[str]) -> frozenset[str]:
    return frozenset(t for t in tokens if any(ch.isdigit() for ch in t))


def _join_letters(words: list[str]) -> list[str]:
    """``["j", "p", "morgan"]`` -> ``["jp", "morgan"]``: spelled-out initials become one word."""
    out: list[str] = []
    run = ""
    for word in words:
        if len(word) == 1 and word.isalpha():
            run += word
            continue
        if run:
            out.append(run)
            run = ""
        out.append(word)
    if run:
        out.append(run)
    return out


def _organization_name(text: str, folded: str, kind: str) -> Name:
    folded = _AMPERSAND.sub(" and ", _APOSTROPHES.sub("", folded))
    folded = _DOTTED.sub(lambda m: m.group(0).replace(".", ""), folded)
    tokens = _join_letters(_words(folded))
    letters = [t for t in tokens if t not in _STOPWORDS]
    if len(letters) >= 2 and all(len(t) == 1 for t in letters):
        tokens = ["".join(letters)]  # "P&G" -> "pg"
    if len(tokens) > 1 and tokens[0] == "the":
        tokens = tokens[1:]
    forms: list[str] = []
    stripped = True
    while stripped and len(tokens) > 1:
        stripped = False
        for form in _LEGAL_BY_LAST.get(tokens[-1], ()):
            if len(tokens) > len(form) and tuple(tokens[-len(form) :]) == form:
                forms.insert(0, " ".join(form))
                tokens = tokens[: -len(form)]
                stripped = True
                break
        while stripped and len(tokens) > 1 and tokens[-1] == "and":  # "Smith & Co." -> "smith"
            tokens = tokens[:-1]
    core = tuple(t for t in tokens if t not in _DESCRIPTORS) or tuple(tokens)
    return Name(text, kind, " ".join(tokens), tuple(tokens), core, " ".join(forms) or None, _codes(tokens))


def _person_name(text: str, folded: str) -> Name:
    folded = _APOSTROPHES.sub("", folded)
    folded = re.sub(r"\([^)]*\)|\"[^\"]*\"|“[^”]*”", " ", folded)  # nicknames and notes
    generation: str | None = None
    head, *parts = [part.strip() for part in folded.split(",")]
    given: list[str] = []
    for part in parts:  # "Smith, John", "John Smith, Jr.", "Smith, John, PhD"
        words = _words(part)
        if words and all(w in _GENERATIONS or w in _AFTER_COMMA for w in words):
            generation = next((_GENERATIONS[w] for w in words if w in _GENERATIONS), generation)
        else:
            given.extend(words)
    words = [*given, *_words(head)] if given else _words(head)
    while len(words) > 1 and words[0] in _TITLES:
        words = words[1:]
    while len(words) > 1 and (words[-1] in _GENERATIONS or words[-1] in _POSTNOMINALS):
        if words[-1] in _GENERATIONS:
            generation = _GENERATIONS[words[-1]]
        words = words[:-1]
    tokens = tuple(words)
    return Name(text, "person", " ".join(tokens), tokens, tokens, generation)


def _location_name(text: str, folded: str) -> Name:
    head, _, area = _APOSTROPHES.sub("", folded).partition(",")
    words = _words(head)
    if words[:2] == ["city", "of"] and len(words) > 2:
        words = words[2:]
    if len(words) > 1 and words[0] == "the":
        words = words[1:]
    tokens = tuple(_PLACE_WORDS.get(w, w) for w in words)
    return Name(text, "location", " ".join(tokens), tokens, tokens, " ".join(_words(area)) or None, _codes(tokens))


def _product_name(text: str, folded: str) -> Name:
    folded = _APOSTROPHES.sub("", folded)
    folded = re.sub(r"(\d),(\d)", r"\1.\2", folded)  # decimal commas
    folded = re.sub(r"(\d)\s*[\"”″]", r"\1in", folded)  # 15" -> 15in
    folded = _NUMBER_UNIT.sub(lambda m: m.group(1) + _UNITS[m.group(2)], folded)  # 128 GB -> 128gb
    folded = re.sub(r"(?<=[^\W_])[-/](?=[^\W_])", "", folded)  # WH-1000XM4 -> wh1000xm4
    tokens = tuple(w.strip(".") for w in re.split(r"[^\w.]+|_", folded) if w.strip("."))
    core = tuple(t for t in tokens if t not in _STOPWORDS) or tokens
    return Name(text, "product", " ".join(tokens), tokens, core, None, _codes(tokens))


@lru_cache(maxsize=65536)
def normalize_name(text: str, kind: str = "company") -> Name:
    """``text`` prepared for comparison as a name of ``kind`` (one of :data:`KINDS`).

    >>> normalize_name("Apple Computer, Inc.").key, normalize_name("Apple Computer, Inc.").qualifier
    ('apple computer', 'inc')
    >>> normalize_name("Smith, Dr. John A., Jr.", "person").key
    'john a smith'
    """
    if kind not in KINDS:
        raise ValueError(f"unknown entity kind {kind!r}; expected one of {', '.join(KINDS)}")
    folded = _fold(str(text)).strip()
    if kind in _ORGANIZATIONS:
        return _organization_name(text, folded, kind)
    if kind == "person":
        return _person_name(text, folded)
    if kind == "location":
        return _location_name(text, folded)
    return _product_name(text, folded)


# --------------------------------------------------------------------------------------------- #
# attributes
# --------------------------------------------------------------------------------------------- #
_ATTRIBUTE_NAMES = {
    "website": "website", "url": "website", "domain": "website", "homepage": "website", "site": "website",
    "web": "website", "profile": "website", "email": "email", "mail": "email", "e_mail": "email",
    "phone": "phone", "telephone": "phone", "tel": "phone", "phone_number": "phone",
    "gtin": "gtin", "gtin8": "gtin", "gtin12": "gtin", "gtin13": "gtin", "gtin14": "gtin", "ean": "gtin",
    "upc": "gtin", "isbn": "gtin", "barcode": "gtin", "mpn": "mpn", "model": "mpn", "model_number": "mpn",
    "brand": "brand", "manufacturer": "brand", "country": "country", "region": "region", "state": "region",
    "province": "region", "city": "city", "locality": "city", "town": "city", "postal_code": "postal_code",
    "zip": "postal_code", "zip_code": "postal_code", "postcode": "postal_code", "coordinates": "coordinates",
    "geo": "coordinates", "latlon": "coordinates", "employer": "employer", "company": "employer",
    "organization": "employer", "affiliation": "employer", "lei": "lei", "vat": "vat", "vat_id": "vat",
    "duns": "duns", "ein": "ein", "tax_id": "tax_id", "company_number": "company_number",
    "registration_number": "company_number", "orcid": "orcid", "wikidata": "wikidata",
}  # fmt: skip
_IDENTIFIERS = frozenset({"lei", "vat", "duns", "ein", "tax_id", "company_number", "orcid", "wikidata"})
_FREE_MAIL = frozenset(
    {
        "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.fr", "hotmail.com", "hotmail.co.uk",
        "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com", "mac.com", "proton.me",
        "protonmail.com", "gmx.de", "gmx.net", "web.de", "mail.ru", "yandex.ru", "qq.com", "163.com", "126.com",
        "zoho.com"
    }
)  # fmt: skip

# attribute -> (weight when a value is shared, weight when both have values and none is shared)
_WEIGHTS: dict[str, dict[str, tuple[float, float]]] = {
    "organization": {
        "website": (4.0, -3.0), "phone": (4.0, -0.5), "lei": (8.0, -8.0), "vat": (8.0, -8.0), "duns": (8.0, -8.0),
        "ein": (8.0, -8.0), "tax_id": (8.0, -8.0), "company_number": (8.0, -8.0), "wikidata": (8.0, -8.0),
        "country": (0.5, -2.0), "country_name": (0.5, -0.5), "region": (0.5, -0.5), "region_name": (0.5, -0.3),
        "city": (0.5, -0.3), "postal_code": (1.5, -0.5),
    },
    "person": {
        "email": (6.0, -0.5), "website": (6.0, -0.5), "phone": (4.0, -0.5), "orcid": (8.0, -8.0),
        "wikidata": (8.0, -8.0), "employer": (2.0, 0.0), "city": (0.5, -0.5), "country": (0.5, -1.0),
        "country_name": (0.5, -0.3),
    },
    "product": {"gtin": (8.0, -8.0), "mpn": (4.0, -3.0), "brand": (1.0, -5.0), "wikidata": (8.0, -8.0)},
    "location": {"country": (0.5, -6.0), "country_name": (0.5, -1.0), "region": (1.5, -4.0), "region_name": (1.0, -1.0),
                 "area_name": (1.0, -1.0), "postal_code": (2.0, -1.0), "wikidata": (8.0, -8.0)},
}  # fmt: skip
_ORDER = {family: {key: i for i, key in enumerate(table)} for family, table in _WEIGHTS.items()}
# Values two members of one entity cannot differ in: a merge between groups that do is refused.
_EXCLUSIVE = {
    "organization": frozenset({"lei", "vat", "duns", "ein", "tax_id", "company_number", "wikidata", "legal_form"}),
    "person": frozenset({"orcid", "wikidata", "generation"}),
    "product": frozenset({"gtin", "brand", "wikidata"}),
    "location": frozenset({"country", "region", "wikidata"}),
}
# Attributes whose values make blocking keys (mentions sharing one are compared).
_BLOCKING = {
    "organization": frozenset(
        {"website", "phone", "lei", "vat", "duns", "ein", "tax_id", "company_number", "wikidata"}
    ),
    "person": frozenset({"email", "website", "phone", "orcid", "wikidata"}),
    "product": frozenset({"gtin", "mpn", "wikidata"}),
    "location": frozenset({"wikidata"}),
}  # fmt: skip


def _family(kind: str) -> str:
    return "organization" if kind in _ORGANIZATIONS else kind


def _values(value: Any) -> list[Any]:
    if isinstance(value, Mapping):  # schema.org style {"@type": "Brand", "name": "Acme"}
        value = value.get("name") or value.get("url") or value.get("@id")
    if value is None or (isinstance(value, str) and (not value.strip() or is_placeholder(value))):
        return []
    if isinstance(value, (list, tuple, set, frozenset)) and not _is_pair_of_numbers(value):
        return [v for item in value for v in _values(item)]
    return [value]


def _is_pair_of_numbers(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
    )


def _site(value: Any, *, full: bool = False) -> str | None:
    """``"https://www.apple.com/about"`` -> ``"apple.com"`` (``full``: ``"apple.com/about"``)."""
    text = str(value).strip().lower()
    if not text:
        return None
    parts = urlsplit(text if "//" in text else "//" + text)
    host = (parts.hostname or "").removeprefix("www.")
    if "." not in host:
        return None
    if full:
        return host + parts.path.rstrip("/")
    return registrable_domain(host)


def _gtin(value: Any) -> str | None:
    """A GTIN (EAN, UPC, ISBN) as 14 digits; an ISBN-10 becomes its ISBN-13."""
    raw = re.sub(r"[\s-]", "", str(value))
    if re.fullmatch(r"\d{9}[\dXx]", raw):  # ISBN-10 (its check digit may be X)
        body = "978" + raw[:9]
        check = (10 - sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(body)) % 10) % 10
        raw = body + str(check)
    digits = re.sub(r"\D", "", raw)
    if len(digits) not in (8, 12, 13, 14):
        return None
    return digits.zfill(14)


def _identifier(value: Any) -> str | None:
    text = re.sub(r"[\s./-]", "", str(value)).upper()
    return text or None


def _normalize_attributes(kind: str, attributes: Mapping[str, Any]) -> tuple[dict[str, frozenset[str]], Any]:
    """Comparable values of the attributes that matter for ``kind``, and the coordinates if any."""
    family = _family(kind)
    named: dict[str, list[Any]] = {}
    latitude = longitude = None
    for raw_key, value in attributes.items():
        key = re.sub(r"[\s-]+", "_", str(raw_key).strip().lower())
        if key in ("latitude", "lat"):
            latitude = value
        elif key in ("longitude", "lon", "lng"):
            longitude = value
        elif key in _ATTRIBUTE_NAMES:
            named.setdefault(_ATTRIBUTE_NAMES[key], []).extend(_values(value))
    country = next((c for c in map(normalize_country, map(str, named.get("country", []))) if c), None)
    out: dict[str, set[str]] = {}

    def put(key: str, value: str | None) -> None:
        if value:
            out.setdefault(key, set()).add(value)

    for key, values in named.items():
        for value in values:
            if key == "website":
                put("website", _site(value, full=family == "person"))
            elif key == "email":
                email = normalize_email(str(value))
                if email and family == "person":
                    put("email", email.lower())
                elif email and email.rsplit("@", 1)[1] not in _FREE_MAIL:
                    put("website", registrable_domain(email.rsplit("@", 1)[1]))
            elif key == "phone":
                put("phone", normalize_phone(str(value), country=country))
            elif key == "gtin":
                put("gtin", _gtin(value))
            elif key == "mpn":
                put("mpn", re.sub(r"[\W_]", "", _fold(str(value))) or None)
            elif key == "brand":
                put("brand", normalize_name(str(value), "brand").key or None)
            elif key == "employer":
                put("employer", normalize_name(str(value), "company").key or None)
            elif key == "country":
                code = normalize_country(str(value))
                put("country" if code else "country_name", code or " ".join(_words(_fold(str(value)))) or None)
            elif key == "region":
                code = normalize_region(str(value), country)
                put("region" if code else "region_name", code or " ".join(_words(_fold(str(value)))) or None)
            elif key == "city":
                put("city", " ".join(_words(_fold(str(value)))) or None)
            elif key == "postal_code":
                put("postal_code", re.sub(r"\s", "", str(value)).upper() or None)
            elif key in _IDENTIFIERS:
                put(key, _identifier(value))
    if "region" in out and "country" not in out:
        out["country"] = {code.split("-")[0] for code in out["region"]}
    coordinates = None
    for value in named.get("coordinates", []):
        if _is_pair_of_numbers(value):
            coordinates = (float(value[0]), float(value[1]))
        else:
            coordinates = parse_coordinates(str(value))
        if coordinates:
            break
    if coordinates is None and latitude is not None and longitude is not None:
        coordinates = parse_coordinates(f"{latitude}, {longitude}")
    return {k: frozenset(v) for k, v in out.items()}, coordinates


# --------------------------------------------------------------------------------------------- #
# mentions, matches, entities
# --------------------------------------------------------------------------------------------- #
@dataclass(eq=False)
class Mention:
    """One occurrence of a name in the data, with where it was seen.

    Attributes:
        name: The name as it appeared.
        kind: One of :data:`KINDS`.
        source: Where it was seen: a URL, a file and line, a record id.
        attributes: What else was known (``website``, ``phone``, ``country``, ``gtin``, ``brand``,
            ``email``, ``employer``, ``coordinates``...), as given. Known attribute names are
            compared (see ``docs/entities.md``); the others are kept for the merged record.
        id: Its position among the resolver's mentions.
    """

    name: str
    kind: str
    source: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    id: int = -1
    normalized: Name = field(init=False, repr=False)
    ids: dict[str, frozenset[str]] = field(init=False, repr=False)
    coordinates: tuple[float, float] | None = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.normalized = normalize_name(self.name, self.kind)
        self.ids, self.coordinates = _normalize_attributes(self.kind, self.attributes)
        name = self.normalized
        if self.kind == "company" and name.qualifier:  # legal entities: Acme GmbH is not Acme Inc.
            self.ids["legal_form"] = frozenset({_legal_family(name.qualifier)})
        if self.kind == "person" and name.qualifier:
            self.ids["generation"] = frozenset({name.qualifier})
        if self.kind == "location" and name.qualifier:
            self._place_area(name.qualifier)

    def _place_area(self, qualifier: str) -> None:
        """The area after a place's name ("Springfield, IL"): a region or country code when it is
        unambiguous, else the candidate codes ("IL": Illinois or Israel), else the text."""
        codes = set(_area_index().get(qualifier, ()))
        country = next(iter(self.ids.get("country", ())), None)
        if country:
            codes = {code for code in codes if code == country or code.startswith(country + "-")}
        if len(codes) == 1:
            code = codes.pop()
            if "-" in code:
                self.ids.setdefault("region", frozenset({code}))
                self.ids.setdefault("country", frozenset({code.split("-")[0]}))
            else:
                self.ids.setdefault("country", frozenset({code}))
        elif codes:
            self.ids["area"] = frozenset(codes)
        else:
            self.ids["area_name"] = frozenset({qualifier})

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name}
        if self.source is not None:
            out["source"] = self.source
        if self.attributes:
            out["attributes"] = dict(self.attributes)
        return out


@dataclass(eq=False)
class Match:
    """Two mentions compared: a score (0-1), a decision and the evidence behind them.

    Attributes:
        a, b: The mentions.
        score: The probability that they are one entity, from the evidence.
        decision: ``"merge"``, ``"review"`` or ``"distinct"`` (from the resolver's thresholds).
        evidence: ``(weight, reason)`` pairs; the score is ``logistic(-2 + sum of weights)``.
        note: Why a pair that scored high enough was not merged, or how many identical mentions
            a review item stands for.
    """

    a: Mention
    b: Mention
    score: float
    decision: str
    evidence: list[tuple[float, str]] = field(default_factory=list)
    note: str | None = None

    @property
    def reasons(self) -> list[str]:
        return [f"{weight:+.1f} {reason}" for weight, reason in self.evidence]

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "a": self.a.to_dict(),
            "b": self.b.to_dict(),
            "score": self.score,
            "decision": self.decision,
            "reasons": self.reasons,
        }
        if self.note:
            out["note"] = self.note
        return out

    def __repr__(self) -> str:
        note = f", note={self.note!r}" if self.note else ""
        return (
            f"Match({self.a.name!r} ~ {self.b.name!r}, score={self.score}, {self.decision!r}, {self.reasons!r}{note})"
        )


def _ranked(values: Iterable[Any]) -> list[Any]:
    """Distinct values, most common first (then first seen)."""
    counts: Counter[str] = Counter()
    first: dict[str, Any] = {}
    for value in values:
        key = repr(value)
        counts[key] += 1
        first.setdefault(key, value)
    order = {key: i for i, key in enumerate(first)}
    return [first[key] for key in sorted(counts, key=lambda k: (-counts[k], order[k]))]


@dataclass(eq=False)
class Entity:
    """One real-world thing and every mention of it.

    Attributes:
        id: A readable id (``"company:apple"``; ``"~2"`` and so on is appended when names collide).
        kind: One of :data:`KINDS`.
        name: The canonical name: the most frequent spelling. Ties go to full names for people, the
            name with a legal form for companies ("Apple Inc."), the one without for brands and
            organizations ("Nike"), then the shortest.
        mentions: Every mention, in the order they were added.
        confidence: How sure the mentions belong together: the score of the weakest match that merged
            them (1.0 for a single mention). No two of them compared as different entities: such a
            merge is refused and listed for review.
        links: The matches that merged the mentions, strongest first.
    """

    id: str
    kind: str
    name: str
    mentions: list[Mention]
    confidence: float
    links: list[Match] = field(default_factory=list)

    @property
    def aliases(self) -> list[str]:
        """Every spelling seen, most common first."""
        return _ranked(m.name.strip() for m in self.mentions)

    @property
    def sources(self) -> list[str]:
        return list(dict.fromkeys(m.source for m in self.mentions if m.source is not None))

    @property
    def attributes(self) -> dict[str, list[Any]]:
        """Every value seen for each attribute, most common first."""
        keys = dict.fromkeys(k for m in self.mentions for k in m.attributes)
        return {k: _ranked(v for m in self.mentions for v in _values(m.attributes.get(k))) for k in keys}

    def to_dict(self, *, mentions: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "aliases": self.aliases,
            "confidence": self.confidence,
            "count": len(self.mentions),
            "sources": self.sources,
            "attributes": self.attributes,
        }
        if mentions:
            out["mentions"] = [m.to_dict() for m in self.mentions]
            out["links"] = [
                {"a": link.a.name, "b": link.b.name, "score": link.score, "reasons": link.reasons}
                for link in self.links
            ]
        return out

    def explain(self) -> str:
        lines = [f"{self.id}: {self.name!r}, {len(self.mentions)} mentions, confidence {self.confidence:.2f}"]
        for alias, count in Counter(m.name.strip() for m in self.mentions).most_common():
            lines.append(f"  {count:>4} x {alias}")
        for link in self.links:
            lines.append(f"  {link.a.name!r} ~ {link.b.name!r}: {link.score:.3f} ({'; '.join(link.reasons)})")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"Entity({self.id!r}, {self.name!r}, mentions={len(self.mentions)}, confidence={self.confidence})"


@dataclass(eq=False)
class Resolution:
    """What :meth:`EntityResolver.resolve` found.

    Attributes:
        entities: Largest first.
        review: Pairs that may be one entity but were not merged, best first: scores between the
            review and merge thresholds, and merges refused because of conflicting identifiers.
        stats: Counts: mentions, distinct mentions, comparisons, skipped (over-large) blocks...
    """

    entities: list[Entity]
    review: list[Match]
    stats: dict[str, int]
    _by_mention: dict[int, Entity] = field(default_factory=dict, repr=False)

    def entity_of(self, mention: Mention | int) -> Entity:
        """The entity a mention (or mention id) belongs to."""
        return self._by_mention[mention if isinstance(mention, int) else mention.id]

    def find(self, name: str) -> list[Entity]:
        """Entities that were mentioned with exactly this name (ignoring case and surrounding spaces)."""
        wanted = name.strip().casefold()
        return [e for e in self.entities if any(m.name.strip().casefold() == wanted for m in e.mentions)]

    def records(self) -> list[dict[str, Any]]:
        """One merged record per entity: its id, name, aliases, confidence, sources and, for each
        attribute, the most common value."""
        out = []
        for entity in self.entities:
            record: dict[str, Any] = {"id": entity.id, "name": entity.name}
            for key, values in entity.attributes.items():
                if key not in record and values:
                    record[key] = values[0]
            record.update(
                aliases=entity.aliases, confidence=entity.confidence, count=len(entity.mentions), sources=entity.sources
            )
            out.append(record)
        return out

    def summary(self) -> str:
        merged = sum(1 for e in self.entities if len(e.mentions) > 1)
        return (
            f"{self.stats['mentions']:,} mentions -> {len(self.entities):,} entities "
            f"({merged:,} with several mentions); {len(self.review):,} pairs to review"
        )


# --------------------------------------------------------------------------------------------- #
# comparing names
# --------------------------------------------------------------------------------------------- #
def _within_edits(a: str, b: str, limit: int) -> bool:
    """Levenshtein distance of ``a`` and ``b`` is at most ``limit`` (small limits, short words)."""
    if abs(len(a) - len(b)) > limit:
        return False
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        if min(current) > limit:
            return False
        previous = current
    return previous[-1] <= limit


def _one_edit(a: str, b: str) -> bool:
    """At most one insertion, deletion or substitution apart."""
    if len(a) > len(b):
        a, b = b, a
    if len(b) - len(a) > 1:
        return False
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i + 1 :] == b[i + 1 :] if len(a) == len(b) else a[i:] == b[i + 1 :]


@lru_cache(maxsize=65536)
def _similar_words(a: str, b: str) -> bool:
    """Probably the same word with a typo: long enough, and one edit apart (two for long words)."""
    if min(len(a), len(b)) < 5 or any(ch.isdigit() for ch in a + b):
        return False  # numbers must match exactly: "iPhone 14" is not "iPhone 15"
    if max(len(a), len(b)) < 9:
        return _one_edit(a, b)
    return _within_edits(a, b, 2)


def _soft_jaccard(a: Collection[str], b: Collection[str]) -> float:
    """Jaccard similarity of two word sets where words one typo apart count 0.8."""
    left, right = set(a), set(b)
    if not left or not right:
        return 0.0
    shared = left & right
    matched, total = len(shared), float(len(shared))
    rest = right - shared
    for word in sorted(left - shared):
        other = next((w for w in sorted(rest) if _similar_words(word, w)), None)
        if other is not None:
            rest.discard(other)
            matched += 1
            total += 0.8
    return total / (len(left) + len(right) - matched)


def _significant(words: Iterable[str]) -> list[str]:
    return [w for w in words if w not in _STOPWORDS]


def _initials(name: Name) -> str:
    return "".join(w[0] for w in _significant(name.tokens))


def _is_initialism(short: Name, long: Name) -> bool:
    """``short`` ("IBM") is the initials of ``long`` ("International Business Machines")."""
    return (
        len(short.tokens) == 1
        and len(short.key) >= 2
        and short.key.isalpha()
        and len(_significant(long.tokens)) >= 2
        and _initials(long) == short.key
    )


def _organization_evidence(x: Name, y: Name) -> list[tuple[float, str]]:
    evidence: list[tuple[float, str]] = []
    if x.key == y.key:
        if " ".join(x.text.split()).casefold() == " ".join(y.text.split()).casefold():
            evidence.append((6.0, "same name"))
        elif x.qualifier != y.qualifier:
            evidence.append((6.0, "same name apart from the legal form"))
        else:
            evidence.append((6.0, "same name apart from spelling"))
        if x.qualifier and y.qualifier and _legal_family(x.qualifier) != _legal_family(y.qualifier):
            evidence.append((-1.5, f"different legal forms: {x.qualifier} / {y.qualifier}"))
    elif x.key.replace(" ", "") == y.key.replace(" ", ""):
        evidence.append((5.0, "same name apart from spacing"))
    else:
        options: list[tuple[float, str]] = []
        if x.core == y.core:
            extra = ", ".join(repr(w) for w in sorted(set(x.tokens) ^ set(y.tokens)))
            options.append((3.5, f"same name apart from {extra}"))
        if _is_initialism(x, y) or _is_initialism(y, x):
            short, long = (x, y) if len(x.tokens) == 1 else (y, x)
            options.append((3.0, f"{short.text!r} is the initials of {long.text!r}"))
        similarity = _soft_jaccard(_significant(x.tokens), _significant(y.tokens))
        if similarity == 1.0:  # the same words in another order: "Blue Star" is not "Star Blue"
            options.append((3.0, "same words in another order"))
        elif similarity >= 0.75:
            options.append((2.0 + 3.5 * (similarity - 0.75) / 0.25, f"similar names ({similarity:.2f})"))
        if options:
            evidence.append(max(options))
    if x.codes and y.codes and x.codes != y.codes:
        evidence.append((-4.0, f"different numbers: {' '.join(sorted(x.codes))} / {' '.join(sorted(y.codes))}"))
    return evidence


def _compatible_given(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """Given (and middle) names that can be the same person's: equal, or an initial and a name."""
    return all(u == v or ((len(u) == 1 or len(v) == 1) and u[0] == v[0]) for u, v in zip(a, b, strict=False))


def _person_evidence(x: Name, y: Name) -> list[tuple[float, str]]:
    a, b = x.tokens, y.tokens
    if not a or not b:
        return []
    evidence: list[tuple[float, str]] = []
    if a == b:
        evidence.append((3.0, "same name"))
    elif a[-1] != b[-1]:
        if a[:-1] == b[:-1] and _similar_words(a[-1], b[-1]):
            evidence.append((1.5, f"same name apart from a letter: {a[-1]} / {b[-1]}"))
        else:
            return []
    elif _compatible_given(a[:-1], b[:-1]):
        evidence.append((1.5, f"same family name, compatible given names: {' '.join(a[:-1])} / {' '.join(b[:-1])}"))
    else:
        evidence.append((-3.0, f"different given names: {' '.join(a[:-1])} / {' '.join(b[:-1])}"))
    if x.qualifier != y.qualifier:
        if x.qualifier and y.qualifier:
            evidence.append((-5.0, f"different generations: {x.qualifier} / {y.qualifier}"))
        else:
            evidence.append((-1.0, f"only one is {x.qualifier or y.qualifier}"))
    return evidence


@lru_cache(maxsize=1)
def _area_index() -> dict[str, frozenset[str]]:
    """Country and region names and codes -> ISO codes (``"il"`` -> ``{"IL", "US-IL"}``)."""
    index: dict[str, set[str]] = {}

    def add(name: str, code: str) -> None:
        index.setdefault(" ".join(_words(_fold(name))), set()).add(code)

    for country in COUNTRIES.values():
        for name in (country.alpha2, country.alpha3, country.name, *country.aliases):
            add(name, country.alpha2)
    for alpha2, table in REGIONS.items():
        for code, name in table.items():
            add(code, f"{alpha2}-{code}")
            add(name, f"{alpha2}-{code}")
    for alpha2, aliases in REGION_ALIASES.items():
        for alias, code in aliases.items():
            add(alias, f"{alpha2}-{code}")
    return {key: frozenset(codes) for key, codes in index.items()}


def _areas(mention: Mention) -> set[str]:
    """The most specific area codes known for a place: a region's country is left out."""
    codes = {code for key in ("area", "region", "country") for code in mention.ids.get(key, ())}
    regions = {code.split("-")[0] for code in mention.ids.get("region", ())}
    return {code for code in codes if code not in regions}


def _within(a: str, b: str) -> bool:
    """Area codes that can be the same place: equal, or a country and one of its regions."""
    return a == b or a.startswith(b + "-") or b.startswith(a + "-")


def _location_evidence(a: Mention, b: Mention) -> list[tuple[float, str]]:
    x, y = a.normalized, b.normalized
    evidence: list[tuple[float, str]] = []
    if x.key == y.key:
        evidence.append((4.0, "same place name"))
    else:
        similarity = _soft_jaccard(x.tokens, y.tokens)
        if similarity < 0.85:
            return []
        evidence.append((2.0, f"similar place names ({similarity:.2f})"))
    if "area" in a.ids or "area" in b.ids:  # an ambiguous area in a name: "Springfield, IL"
        left, right = _areas(a), _areas(b)
        if left and right:
            if left & right:
                evidence.append((1.0, f"same area: {sorted(left & right)[0]}"))
            elif not any(_within(p, q) for p in left for q in right):
                evidence.append(
                    (-3.0, f"different areas: {x.qualifier or sorted(left)[0]} / {y.qualifier or sorted(right)[0]}")
                )
    return evidence


def _product_evidence(a: Mention, b: Mention) -> list[tuple[float, str]]:
    brand_words = {w for m in (a, b) for brand in m.ids.get("brand", ()) for w in brand.split()}
    x = [w for w in a.normalized.core if w not in brand_words] or list(a.normalized.core)
    y = [w for w in b.normalized.core if w not in brand_words] or list(b.normalized.core)
    codes_x, codes_y = a.normalized.codes, b.normalized.codes
    evidence: list[tuple[float, str]] = []
    if sorted(x) == sorted(y):
        weight = 3.0 + min(3.0, 0.5 * len(set(x)))  # long, specific names say more than "Chair"
        evidence.append((weight, "same name" if x == y else "same words"))
    else:
        similarity = _soft_jaccard(x, y)
        if similarity >= 0.6:
            evidence.append((1.0 + 3.0 * (similarity - 0.6) / 0.4, f"similar names ({similarity:.2f})"))
        if codes_x and codes_x == codes_y:
            evidence.append((2.0, f"same model numbers: {' '.join(sorted(codes_x))}"))
    if codes_x and codes_y and codes_x != codes_y:
        shown = f"{' '.join(sorted(codes_x))} / {' '.join(sorted(codes_y))}"
        if codes_x < codes_y or codes_y < codes_x:
            evidence.append((-2.0, f"one name has more details: {shown}"))
        else:
            evidence.append((-6.0, f"different model numbers: {shown}"))
    return evidence


def _haversine_km(p: tuple[float, float], q: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*p, *q))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def _attribute_evidence(a: Mention, b: Mention) -> list[tuple[float, str]]:
    family = _family(a.kind)
    evidence: list[tuple[float, str]] = []
    weights = _WEIGHTS[family]
    common = a.ids.keys() & b.ids.keys() & weights.keys()
    for key in sorted(common, key=_ORDER[family].__getitem__) if common else ():
        same, different = weights[key]
        left, right = a.ids[key], b.ids[key]
        if not left or not right:
            continue
        label = key.replace("_", " ")
        shared = left & right
        if shared:
            evidence.append((same, f"same {label}: {sorted(shared)[0]}"))
        elif key == "website" and family == "organization" and _site_names(left) & _site_names(right):
            shown = f"{sorted(left)[0]} / {sorted(right)[0]}"
            evidence.append((2.0, f"same web name in another domain: {shown}"))
        elif different:
            evidence.append((different, f"different {label}: {sorted(left)[0]} / {sorted(right)[0]}"))
    if a.coordinates and b.coordinates and family in ("location", "organization"):
        km = _haversine_km(a.coordinates, b.coordinates)
        if family == "location":
            if km < 1:
                evidence.append((4.0, f"same place ({km:.1f} km apart)"))
            elif km < 10:
                evidence.append((2.0, f"close ({km:.1f} km apart)"))
            elif km > 50:
                evidence.append((-6.0, f"far apart ({km:,.0f} km)"))
        elif km < 0.2:
            evidence.append((2.0, f"same address ({km * 1000:.0f} m apart)"))
        elif km > 50:
            evidence.append((-1.0, f"far apart ({km:,.0f} km)"))
    return evidence


def _site_names(domains: Iterable[str]) -> set[str]:
    """``{"apple.co.uk"}`` -> ``{"apple"}``: the name part of registrable domains."""
    return {d.split(".", 1)[0] for d in domains}


def _evidence(a: Mention, b: Mention) -> list[tuple[float, str]]:
    x, y = a.normalized, b.normalized
    if a.kind in _ORGANIZATIONS:
        names = _organization_evidence(x, y)
    elif a.kind == "person":
        names = _person_evidence(x, y)
    elif a.kind == "location":
        names = _location_evidence(a, b)
    else:
        names = _product_evidence(a, b)
    return names + _attribute_evidence(a, b)


def _probability(evidence: list[tuple[float, str]]) -> float:
    logit = _PRIOR + sum(weight for weight, _ in evidence)
    return round(1 / (1 + math.exp(-max(-50.0, min(50.0, logit)))), 3)


# --------------------------------------------------------------------------------------------- #
# the resolver
# --------------------------------------------------------------------------------------------- #
def _blocking_keys(mention: Mention) -> set[str]:
    name = mention.normalized
    keys: set[str] = set()
    if mention.kind in _ORGANIZATIONS:
        words = [w for w in _significant(name.core) if len(w) > 1] or [w for w in name.tokens if len(w) > 1]
        keys.add(f"k:{name.key.replace(' ', '')}")
        keys.add(f"c:{''.join(name.core)}")  # names that differ by generic words ("Apple Computer")
        if len(name.tokens) >= 2 and len(_initials(name)) >= 2:
            keys.add(f"w:{_initials(name)}")  # so that "IBM" meets "International Business Machines"
    elif mention.kind == "person":
        words = []
        if name.tokens:
            keys.add(f"p:{name.tokens[-1]}:{name.tokens[0][0]}")
    elif mention.kind == "location":
        words = _significant(name.tokens)
    else:
        words = [w for w in name.core if len(w) > 1]
    keys.update(f"w:{w}" for w in words)
    # pairs of words: when single words are too common to use ("Blue Star" / "Blue Star Group")
    keys.update(f"w2:{p}|{q}" for p, q in combinations(sorted(set(words[:4])), 2))
    for key in _BLOCKING[_family(mention.kind)]:
        keys.update(f"i:{key}:{value}" for value in mention.ids.get(key, ()))
    return keys


def _signature(mention: Mention) -> tuple[Any, ...]:
    """Mentions with equal signatures compare identically to everything: they are compared once."""
    name = mention.normalized
    ids = tuple(sorted((k, tuple(sorted(v))) for k, v in mention.ids.items()))
    return (name.key, name.qualifier, ids, mention.coordinates)


def _slug(text: str) -> str:
    return re.sub(r"[^\w]+", "-", text).strip("-") or "unnamed"


class EntityResolver:
    """Groups mentions of names into entities, with evidence (see the module documentation).

    Args:
        kind: What the names are: one of :data:`KINDS`.
        merge_threshold: Pairs scoring at least this are merged (unless their groups conflict).
        review_threshold: Pairs scoring at least this, but less than ``merge_threshold``, are
            listed for review.
        max_block: Words shared by more than this many distinct mentions are too common to
            suggest a match ("the", a brand in every product name); they are not used to find pairs.
    """

    def __init__(
        self,
        kind: str = "company",
        *,
        merge_threshold: float = 0.95,
        review_threshold: float = 0.5,
        max_block: int = 300,
    ) -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown entity kind {kind!r}; expected one of {', '.join(KINDS)}")
        if not 0 < review_threshold <= merge_threshold <= 1:
            raise ValueError("thresholds must satisfy 0 < review_threshold <= merge_threshold <= 1")
        self.kind = kind
        self.merge_threshold = merge_threshold
        self.review_threshold = review_threshold
        self.max_block = max_block
        self.mentions: list[Mention] = []
        self._group_of: dict[int, int] = {}  # per resolve(): identical mentions that did not merge
        self._self_scores: dict[int, Match] = {}  # per resolve(): why a unit of identical mentions merged

    def add(self, name: Any, *, source: str | None = None, **attributes: Any) -> Mention | None:
        """Add a mention of ``name``, seen at ``source``, with what else is known about it.

        Returns the :class:`Mention`, or ``None`` for an empty name or a placeholder ("N/A").
        """
        if name is None or is_placeholder(name) or not str(name).strip():
            return None
        mention = Mention(str(name).strip(), self.kind, source, dict(attributes), id=len(self.mentions))
        if not mention.normalized.tokens:
            return None
        self.mentions.append(mention)
        return mention

    def add_records(
        self,
        records: Iterable[Mapping[str, Any]],
        name_field: str,
        *,
        source_field: str | None = None,
        attributes: Mapping[str, str] | Iterable[str] = (),
    ) -> list[Mention | None]:
        """Add one mention per record: the name in ``name_field`` (a dotted path works), the source in
        ``source_field``, and ``attributes`` (``{"website": "brand_url"}``, or field names that are
        attribute names). Returns the mentions in record order (``None`` where a record had no name).
        """
        from .expressions import get_path

        fields = dict(attributes) if isinstance(attributes, Mapping) else {name: name for name in attributes}
        added: list[Mention | None] = []
        for record in records:
            values = {attr: get_path(record, path) for attr, path in fields.items()}
            source = get_path(record, source_field) if source_field else None
            added.append(
                self.add(
                    get_path(record, name_field),
                    source=None if source is None else str(source),
                    **{k: v for k, v in values.items() if v is not None},
                )
            )
        return added

    def compare(self, a: Mention | str, b: Mention | str) -> Match:
        """Compare two mentions (or names) and explain the score."""
        left = a if isinstance(a, Mention) else Mention(str(a), self.kind)
        right = b if isinstance(b, Mention) else Mention(str(b), self.kind)
        evidence = _evidence(left, right)
        score = _probability(evidence)
        return Match(left, right, score, self._decision(score), evidence)

    def _decision(self, score: float) -> str:
        if score >= self.merge_threshold:
            return "merge"
        return "review" if score >= self.review_threshold else "distinct"

    def resolve(self) -> Resolution:
        """Group the mentions into entities; list the uncertain pairs for review."""
        units, review = self._units()
        matches, scores, skipped = self._candidates(units)
        neighbors: dict[int, list[tuple[int, float]]] = {}
        for (i, j), score in scores.items():
            neighbors.setdefault(i, []).append((j, score))
            neighbors.setdefault(j, []).append((i, score))
        parent = list(range(len(units)))
        members = {i: [i] for i in range(len(units))}
        exclusive = [self._exclusive(unit) for unit in units]
        links: dict[int, list[Match]] = {}

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def worst(left: int, right: int) -> tuple[float, int, int] | None:
            """The lowest score between members of two groups that were compared."""
            small, other = (left, right) if len(members[left]) <= len(members[right]) else (right, left)
            found = [(score, u, v) for u in members[small] for v, score in neighbors.get(u, ()) if find(v) == other]
            return min(found) if found else None

        unit_of = {m.id: i for i, unit in enumerate(units) for m in unit}
        blocked = self._ambiguous(matches, units, unit_of, exclusive)
        for match in sorted(matches, key=lambda m: (-m.score, m.a.id, m.b.id)):
            if match.score < self.merge_threshold:
                break
            if match.note:
                continue  # ambiguous
            left, right = find(unit_of[match.a.id]), find(unit_of[match.b.id])
            if left == right:
                continue
            note = _conflict(exclusive[left], exclusive[right])
            lowest = worst(left, right)
            if note is None and lowest is not None and lowest[0] < self.review_threshold:
                _, u, v = lowest  # merging would join two mentions that look like different entities
                note = f"{units[u][0].name!r} and {units[v][0].name!r} look like different entities ({lowest[0]})"
            if note:
                match.decision = "review"
                match.note = f"not merged: {note}"
                blocked.append(match)
                continue
            parent[right] = left
            members[left].extend(members.pop(right))
            for key, values in exclusive[right].items():
                exclusive[left].setdefault(key, set()).update(values)
            links.setdefault(left, []).extend([*links.pop(right, []), match])
        sizes = Counter(find(i) for i in range(len(units)))

        def representative(unit: int) -> tuple[str, int]:
            # identical mentions that did not merge (each its own entity) are reviewed once
            root = find(unit)
            group = self._group_of.get(units[unit][0].id)
            return ("group", group) if group is not None and sizes[root] == 1 else ("entity", root)

        best: dict[tuple[tuple[str, int], ...], Match] = {}
        for match in [*blocked, *(m for m in matches if self.review_threshold <= m.score < self.merge_threshold)]:
            left, right = unit_of[match.a.id], unit_of[match.b.id]
            if find(left) != find(right):
                pair = tuple(sorted((representative(left), representative(right))))
                if pair not in best or match.score > best[pair].score:
                    best[pair] = match
        review.extend(best.values())
        review.sort(key=lambda m: (-m.score, m.a.id, m.b.id))
        entities = self._entities(units, find, links)
        by_mention = {m.id: entity for entity in entities for m in entity.mentions}
        stats = {
            "mentions": len(self.mentions),
            "distinct": len({_signature(m) for m in self.mentions}),
            "comparisons": len(scores),
            "skipped_blocks": skipped,
        }
        return Resolution(entities, review, stats, by_mention)

    # -- steps ------------------------------------------------------------------------------- #
    def _units(self) -> tuple[list[list[Mention]], list[Match]]:
        """Mentions grouped by signature: a group of identical mentions is one unit if it would
        merge with itself; otherwise each is its own unit, and the group becomes one review item."""
        groups: dict[tuple[Any, ...], list[Mention]] = {}
        for mention in self.mentions:
            groups.setdefault(_signature(mention), []).append(mention)
        units: list[list[Mention]] = []
        review: list[Match] = []
        self._group_of, self._self_scores = {}, {}
        for number, group in enumerate(groups.values()):
            if len(group) == 1:
                units.append(group)
                continue
            match = self.compare(group[0], group[1])
            if match.score >= self.merge_threshold:
                units.append(group)
                self._self_scores[len(units) - 1] = match
                continue
            match.note = f"{len(group)} mentions with the same name and details"
            match.decision = "review" if match.score >= self.review_threshold else "distinct"
            if match.decision == "review":
                review.append(match)
            for mention in group:
                self._group_of[mention.id] = number
                units.append([mention])
        return units, review

    def _candidates(self, units: list[list[Mention]]) -> tuple[list[Match], dict[tuple[int, int], float], int]:
        """Matches worth keeping (at least the review threshold), the score of every pair of units
        compared, and how many blocking keys were too common to use."""
        blocks: dict[str, list[int]] = {}
        for i, unit in enumerate(units):
            for key in _blocking_keys(unit[0]):
                blocks.setdefault(key, []).append(i)
        pairs: set[tuple[int, int]] = set()
        skipped = 0
        for key, members in blocks.items():
            if len(members) < 2:
                continue
            if len(members) > self.max_block * (10 if key.startswith("i:") else 1):
                skipped += 1  # too common to suggest a match (identifiers get more room)
                continue
            for n, i in enumerate(members):
                for j in members[n + 1 :]:
                    pairs.add((i, j))
        matches = []
        scores: dict[tuple[int, int], float] = {}
        for i, j in sorted(pairs):
            a, b = units[i][0], units[j][0]
            group = self._group_of.get(a.id)
            if group is not None and group == self._group_of.get(b.id):
                continue  # identical mentions that do not merge: already one review item
            match = self.compare(a, b)
            scores[(i, j)] = match.score
            if match.score >= self.review_threshold:
                matches.append(match)
        return matches, scores, skipped

    def _ambiguous(
        self,
        matches: list[Match],
        units: list[list[Mention]],
        unit_of: dict[int, int],
        exclusive: list[dict[str, set[str]]],
    ) -> list[Match]:
        """Merge-level matches of a mention with two groups that cannot be one entity, about equally good
        ("Acme" with "Acme Corp" and "ACME Corporation", which have different LEIs): reviewed, not merged."""
        partners: dict[int, list[tuple[int, Match]]] = {}
        for match in matches:
            if match.score >= self.merge_threshold:
                a, b = unit_of[match.a.id], unit_of[match.b.id]
                partners.setdefault(a, []).append((b, match))
                partners.setdefault(b, []).append((a, match))
        flagged: list[Match] = []
        for found in partners.values():
            for (v, first), (w, second) in combinations(found, 2):
                if abs(first.score - second.score) > 0.05 or not _conflict(exclusive[v], exclusive[w]):
                    continue
                for match, other in ((first, w), (second, v)):
                    if not match.note:
                        match.note = f"ambiguous: as close to {units[other][0].name!r}, which is another entity"
                        match.decision = "review"
                        flagged.append(match)
        return flagged

    def _exclusive(self, unit: list[Mention]) -> dict[str, set[str]]:
        keys = _EXCLUSIVE[_family(self.kind)]
        out: dict[str, set[str]] = {}
        for mention in unit:
            for key in keys & mention.ids.keys():
                out.setdefault(key, set()).update(mention.ids[key])
        return out

    def _entities(
        self, units: list[list[Mention]], find: Callable[[int], int], links: dict[int, list[Match]]
    ) -> list[Entity]:
        clusters: dict[int, list[int]] = {}
        for i in range(len(units)):
            clusters.setdefault(find(i), []).append(i)
        entities = []
        for root, members in clusters.items():
            mentions = sorted((m for i in members for m in units[i]), key=lambda m: m.id)
            own = [self._self_scores[i] for i in members if i in self._self_scores]
            merged = sorted(links.get(root, []), key=lambda m: (-m.score, m.a.id))
            scores = [m.score for m in [*own, *merged]]
            entities.append(
                Entity("", self.kind, self._canonical(mentions), mentions, min(scores, default=1.0), [*merged, *own])
            )
        entities.sort(key=lambda e: (-len(e.mentions), e.mentions[0].id))
        used: Counter[str] = Counter()
        for entity in entities:
            base = f"{self.kind}:{_slug(normalize_name(entity.name, self.kind).key or entity.name.lower())}"
            used[base] += 1
            entity.id = base if used[base] == 1 else f"{base}~{used[base]}"
        return entities

    def _canonical(self, mentions: list[Mention]) -> str:
        counts = Counter(m.name.strip() for m in mentions)
        first = {name: i for i, name in reversed(list(enumerate(m.name.strip() for m in mentions)))}

        def rank(name: str) -> tuple[Any, ...]:
            normalized = normalize_name(name, self.kind)
            # a company is a legal entity: its formal name ("Apple Inc."); a brand or an organization
            # spans legal entities: its plain name ("Nike")
            has_form = bool(normalized.qualifier) and self.kind in _ORGANIZATIONS
            form_rank = not has_form if self.kind == "company" else has_form
            # people: full names before initials, "John Smith" before "Smith, John"
            initials = sum(len(t) == 1 for t in normalized.tokens) if self.kind == "person" else 0
            inverted = self.kind == "person" and "," in name
            return (-counts[name], initials, inverted, form_rank, name.isupper(), len(name), first[name])

        return min(counts, key=rank)


def _conflict(left: dict[str, set[str]], right: dict[str, set[str]]) -> str | None:
    for key in sorted(left.keys() & right.keys()):
        if left[key] and right[key] and not left[key] & right[key]:
            shown = " / ".join(sorted(left[key])[:1] + sorted(right[key])[:1])
            return f"different {key.replace('_', ' ')} ({shown})"
    return None
