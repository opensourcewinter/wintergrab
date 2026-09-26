"""Goals: what the user wants, read from a sentence.

::

    goal = parse_goal("Find all laptops under $1000 on shop.example with name, price and rating")
    goal.entity      # "product"
    goal.fields      # ["name", "price", "currency", "rating", "url"]
    goal.filters     # [GoalFilter("price < 1000 and (currency is None or currency == 'USD')", ...)]
    goal.scope       # ["laptops"]
    goal.sites       # ["https://shop.example"]
    print(goal.describe())

The reading is rule-based: a vocabulary of entities (products, articles,
jobs, events, companies, places such as restaurants, people, reviews,
recipes), of fields and their synonyms ("stock status" is ``availability``,
"phone number" is ``telephone``), and of conditions ("under $1000", "rated 4
or more", "published in the last 30 days", "in stock"), which become
expressions of :mod:`wintergrab.data.expressions`. What it does not
understand it says (``goal.notes``), rather than guessing. A model can read
the sentence instead: ``parse_goal(text, parser=my_parser)``, where
``my_parser(text)`` returns the goal as a dict; its answer is checked the
same way (known entity, valid expressions).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..data.expressions import Expression
from ..data.normalize import parse_date, parse_money
from ..data.schema import Schema
from ..errors import ConfigurationError

__all__ = ["ENTITIES", "EntityKind", "Goal", "GoalFilter", "parse_goal"]


@dataclass(frozen=True)
class EntityKind:
    """A kind of record a goal can ask for.

    Attributes:
        name: ``"product"``, ``"article"``...
        words: Nouns that name it in a request.
        page_types: Page types (:func:`~wintergrab.intel.classify_page`) that hold one each.
        listing_types: Page types that list them.
        fields: Every field it knows, with its type (a :class:`~wintergrab.data.Schema` field spec).
        default_fields: The fields a goal gets when it names none.
        date_field: The field date conditions apply to.
    """

    name: str
    words: tuple[str, ...]
    page_types: tuple[str, ...]
    listing_types: tuple[str, ...]
    fields: Mapping[str, Any]
    default_fields: tuple[str, ...]
    date_field: str | None = None

    def schema(self, fields: list[str]) -> Schema:
        """An extraction schema for these fields (types from :attr:`fields`, strings otherwise)."""
        spec = {name: self.fields.get(name, "string") for name in fields}
        if "price" in spec and "currency" not in spec:
            spec["currency"] = "currency"
        return Schema.from_dict({"name": self.name, "fields": spec})


_PLACE_FIELDS = {
    "name": "string", "address": "address", "street": "string", "city": "string", "postal_code": "string",
    "country": "country", "telephone": "phone", "email": "email", "website": "url", "rating": {"type": "rating", "best": 5},
    "review_count": "integer", "opening_hours": "string", "price_range": "string", "cuisine": "string",
    "latitude": "number", "longitude": "number", "description": "text", "image": "url", "url": "url",
}  # fmt: skip

#: The kinds of records goals can ask for, by name.
ENTITIES: dict[str, EntityKind] = {
    kind.name: kind
    for kind in (
        EntityKind(
            "product",
            ("product", "products", "item", "items", "goods", "skus", "offers", "deals"),
            ("product",),
            ("category", "listing", "search"),
            {
                "name": "string", "price": "money", "list_price": "money", "currency": "currency",
                "availability": "availability", "rating": {"type": "rating", "best": 5}, "review_count": "integer",
                "brand": "string", "sku": "string", "gtin": "string", "category": "string", "image": "url",
                "description": "text", "url": "url",
            },
            ("name", "price", "currency", "availability", "url"),
        ),
        EntityKind(
            "article",
            ("article", "articles", "post", "posts", "blog post", "blog posts", "news", "story", "stories",
             "press release", "press releases", "headlines"),
            ("article", "news"),
            ("listing", "archive", "category"),
            {
                "title": "string", "author": "string", "published": "datetime", "modified": "datetime",
                "description": "text", "section": "string", "tags": "string[]", "image": "url", "body": "text",
                "url": "url",
            },
            ("title", "author", "published", "url"),
            "published",
        ),
        EntityKind(
            "job",
            ("job", "jobs", "vacancy", "vacancies", "job posting", "job postings", "job offer", "job offers",
             "job listings", "openings", "positions", "roles"),
            ("job",),
            ("listing", "search", "directory"),
            {
                "title": "string", "company": "string", "location": "string", "salary": "money", "currency": "currency",
                "employment_type": "string", "remote": "string", "date_posted": "date", "valid_through": "date",
                "description": "text", "url": "url",
            },
            ("title", "company", "location", "date_posted", "url"),
            "date_posted",
        ),
        EntityKind(
            "event",
            ("event", "events", "concert", "concerts", "gig", "gigs", "conference", "conferences", "meetup",
             "meetups", "festival", "festivals", "webinar", "webinars"),
            ("event",),
            ("listing", "search"),
            {
                "name": "string", "start_date": "datetime", "end_date": "datetime", "venue": "string", "city": "string",
                "address": "address", "price": "money", "currency": "currency", "organizer": "string",
                "performer": "string", "description": "text", "image": "url", "url": "url",
            },
            ("name", "start_date", "venue", "city", "url"),
            "start_date",
        ),
        EntityKind(
            "company",
            ("company", "companies", "business", "businesses", "organization", "organizations", "organisation",
             "organisations", "firm", "firms", "vendor", "vendors", "supplier", "suppliers", "manufacturer",
             "manufacturers", "agency", "agencies", "startup", "startups", "brand", "brands"),
            ("company", "profile"),
            ("directory", "listing", "search"),
            {
                "name": "string", "website": "url", "telephone": "phone", "email": "email", "address": "address",
                "city": "string", "country": "country", "description": "text", "founded": "date",
                "employees": "integer", "industry": "string", "url": "url",
            },
            ("name", "website", "telephone", "address", "url"),
        ),
        EntityKind(
            "place",
            ("place", "places", "restaurant", "restaurants", "cafe", "cafes", "café", "cafés", "bars", "hotel",
             "hotels", "stores", "shops", "locations", "venues", "clinic", "clinics", "dentist", "dentists", "gyms",
             "salons", "attractions", "dealers", "dealerships", "branches", "pharmacies", "museums"),
            ("company", "profile", "directory"),
            ("directory", "listing", "search"),
            _PLACE_FIELDS,
            ("name", "address", "telephone", "rating", "website", "url"),
        ),
        EntityKind(
            "person",
            ("person", "people", "profiles", "speakers", "team members", "staff", "experts", "members"),
            ("profile",),
            ("directory", "listing"),
            {
                "name": "string", "job_title": "string", "organization": "string", "email": "email",
                "telephone": "phone", "location": "string", "image": "url", "description": "text", "url": "url",
            },
            ("name", "job_title", "organization", "url"),
        ),
        EntityKind(
            "review",
            ("review", "reviews", "testimonial", "testimonials"),
            ("review",),
            ("listing",),
            {
                "author": "string", "rating": {"type": "rating", "best": 5}, "date": "date", "title": "string",
                "text": "text", "item": "string", "url": "url",
            },
            ("author", "rating", "date", "text", "url"),
            "date",
        ),
        EntityKind(
            "property",
            ("property", "properties", "real estate", "property listings", "apartment", "apartments", "houses",
             "houses for sale", "homes for sale", "homes for rent", "rental", "rentals", "condo", "condos"),
            ("property",),
            ("listing", "search", "category"),
            {
                "name": "string", "price": "money", "currency": "currency", "address": "address", "city": "string",
                "bedrooms": "integer", "bathrooms": "number", "rooms": "number", "floor_size": "quantity",
                "year_built": "integer", "latitude": "number", "longitude": "number", "description": "text",
                "image": "url", "url": "url",
            },
            ("name", "price", "address", "bedrooms", "url"),
        ),
        EntityKind(
            "documentation",
            ("documentation", "docs", "doc pages", "documentation pages", "reference pages", "manual pages",
             "api reference"),
            ("documentation",),
            ("documentation", "listing"),
            {
                "title": "string", "description": "text", "section": "string", "body": "text",
                "modified": "datetime", "url": "url",
            },
            ("title", "section", "modified", "url"),
            "modified",
        ),
        EntityKind(
            "recipe",
            ("recipe", "recipes"),
            ("article",),
            ("listing", "category", "archive"),
            {
                "name": "string", "ingredients": "string[]", "total_time": "duration", "servings": "string",
                "calories": "string", "rating": {"type": "rating", "best": 5}, "author": "string", "image": "url",
                "url": "url",
            },
            ("name", "ingredients", "total_time", "url"),
        ),
    )
}  # fmt: skip

# Phrases for fields, by entity where the same words mean different fields ("date" of an event is its start).
_FIELD_WORDS: dict[str, str] = {
    "name": "name", "names": "name", "product name": "name", "title": "title", "titles": "title", "headline": "title",
    "headlines": "title", "price": "price", "prices": "price", "cost": "price", "sale price": "price",
    "current price": "price", "list price": "list_price", "original price": "list_price", "regular price": "list_price",
    "rrp": "list_price", "msrp": "list_price", "currency": "currency", "stock": "availability",
    "stock status": "availability", "availability": "availability", "in stock": "availability",
    "rating": "rating", "ratings": "rating", "stars": "rating", "star rating": "rating", "score": "rating",
    "review count": "review_count", "reviews count": "review_count", "number of reviews": "review_count",
    "brand": "brand", "brands": "brand", "manufacturer": "brand", "maker": "brand", "sku": "sku", "skus": "sku",
    "model number": "sku", "part number": "sku", "gtin": "gtin", "ean": "gtin", "upc": "gtin", "barcode": "gtin",
    "isbn": "gtin", "image": "image", "images": "image", "photo": "image", "photos": "image", "picture": "image",
    "pictures": "image", "description": "description", "descriptions": "description", "summary": "description",
    "details": "description", "category": "category", "categories": "category", "url": "url", "urls": "url",
    "link": "url", "links": "url", "page url": "url", "website": "website", "websites": "website",
    "web site": "website", "homepage": "website", "site": "website", "phone": "telephone", "phones": "telephone",
    "phone number": "telephone", "phone numbers": "telephone", "telephone": "telephone", "tel": "telephone",
    "contact number": "telephone", "email": "email", "emails": "email", "e-mail": "email", "email address": "email",
    "email addresses": "email", "address": "address", "addresses": "address", "street address": "address",
    "street": "street", "city": "city", "cities": "city", "town": "city", "country": "country", "zip": "postal_code",
    "zip code": "postal_code", "postcode": "postal_code", "postal code": "postal_code", "opening hours": "opening_hours",
    "hours": "opening_hours", "opening times": "opening_hours", "price range": "price_range", "cuisine": "cuisine",
    "coordinates": "latitude", "author": "author", "authors": "author", "writer": "author", "byline": "author",
    "date": "date", "dates": "date", "publication date": "published", "published": "published",
    "publish date": "published", "date published": "published", "published date": "published",
    "updated": "modified", "last updated": "modified", "modified": "modified", "section": "section", "body": "body",
    "text": "text", "content": "body", "full text": "body", "article text": "body", "tags": "tags", "keywords": "tags",
    "company": "company", "companies": "company", "employer": "company", "hiring company": "company",
    "salary": "salary", "salaries": "salary", "pay": "salary", "compensation": "salary", "job type": "employment_type",
    "employment type": "employment_type", "contract type": "employment_type", "remote": "remote",
    "deadline": "valid_through", "closing date": "valid_through", "posted": "date_posted",
    "date posted": "date_posted", "posting date": "date_posted", "location": "location", "locations": "location",
    "start": "start_date", "start date": "start_date", "start time": "start_date", "end": "end_date",
    "end date": "end_date", "venue": "venue", "venues": "venue", "organizer": "organizer", "organiser": "organizer",
    "host": "organizer", "performer": "performer", "performers": "performer", "artist": "performer",
    "artists": "performer", "lineup": "performer", "job title": "job_title", "position": "job_title",
    "role": "job_title", "organization": "organization", "organisation": "organization", "ingredients": "ingredients",
    "cooking time": "total_time", "total time": "total_time", "time": "total_time", "servings": "servings",
    "yield": "servings", "portions": "servings", "calories": "calories", "founded": "founded", "employees": "employees",
    "industry": "industry", "reviews": "review_count", "review": "text",
}  # fmt: skip
# Fields that go by another name for some entities.
_FIELD_BY_ENTITY: dict[tuple[str, str], str] = {
    ("article", "name"): "title", ("job", "name"): "title", ("review", "name"): "title",
    ("product", "title"): "name", ("event", "title"): "name", ("place", "title"): "name", ("company", "title"): "name",
    ("person", "title"): "job_title", ("recipe", "title"): "name",
    ("article", "date"): "published", ("event", "date"): "start_date", ("job", "date"): "date_posted",
    ("place", "location"): "address", ("company", "location"): "address", ("event", "location"): "venue",
    ("person", "company"): "organization", ("review", "reviews"): "text", ("review", "review_count"): "text",
}  # fmt: skip

_CURRENCY_WORDS = {"dollar": "USD", "dollars": "USD", "usd": "USD", "euro": "EUR", "euros": "EUR", "eur": "EUR",
                   "pound": "GBP", "pounds": "GBP", "gbp": "GBP", "yen": "JPY", "rupees": "INR", "inr": "INR"}  # fmt: skip
_AMOUNT = r"(?:[$€£¥₹]\s?\d[\d,.]*\s?[km]?|\d[\d,.]*\s?[km]?\s?(?:[$€£¥₹]|usd|eur|gbp|jpy|inr|dollars?|euros?|pounds?|yen|rupees)?)"
_LESS = r"(?:under|below|less than|cheaper than|lower than|up to|at most|max(?:imum)?|no more than|<=?)"
_MORE = r"(?:over|above|more than|higher than|at least|min(?:imum)?|from|>=?)"
_PRICE_WORD = r"(?:(?:that )?(?:cost|costs|costing|priced|price[sd]?|for)\s+)?"
_URL = re.compile(r"https?://[^\s,;)\"']+", re.I)
_DOMAIN = re.compile(r"(?<![@\w.-])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,24})(/[^\s,;)\"']*)?(?![\w@])", re.I)
_UNITS = {"day": 1, "days": 1, "week": 7, "weeks": 7, "month": 30, "months": 30, "year": 365, "years": 365}
_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "ten": 10, "twelve": 12, "thirty": 30}  # fmt: skip
_STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "all",
        "every",
        "each",
        "any",
        "some",
        "of",
        "on",
        "in",
        "at",
        "from",
        "for",
        "to",
        "with",
        "and",
        "or",
        "their",
        "its",
        "his",
        "her",
        "my",
        "our",
        "your",
        "this",
        "that",
        "these",
        "those",
        "find",
        "get",
        "extract",
        "collect",
        "scrape",
        "list",
        "crawl",
        "fetch",
        "grab",
        "download",
        "give",
        "me",
        "us",
        "i",
        "we",
        "want",
        "need",
        "please",
        "public",
        "publicly",
        "accessible",
        "available",
        "site",
        "website",
        "page",
        "pages",
        "web",
        "data",
        "dataset",
        "clean",
        "new",
        "latest",
        "recent",
        "current",
        "them",
        "it",
        "is",
        "are",
        "be",
        "which",
        "who",
        "whose",
        "where",
        "what",
        "how",
        "many",
        "much",
        "also",
        "including",
        "include",
        "plus",
        "as",
        "well",
        "into",
        "by",
        "per",
    ]
)


@dataclass
class GoalFilter:
    """A condition records must meet: an expression (:mod:`wintergrab.data.expressions`) and what it came from."""

    expression: str
    text: str = ""
    field: str | None = None

    def __post_init__(self) -> None:
        try:
            Expression(self.expression)
        except Exception as exc:
            raise ConfigurationError(f"invalid condition {self.expression!r}: {exc}", key="filters") from exc


@dataclass
class Goal:
    """What to collect (see the module docs).

    Attributes:
        text: The request as written.
        entity: The kind of record (a key of :data:`ENTITIES`).
        fields: The fields to extract; ``url`` is always among them.
        sites: Where to look (URLs).
        filters: Conditions records must meet.
        scope: Words that narrow down where to look ("laptops": a section of the site).
        limit: At most this many records.
        monitor: Keep watching for changes (``"daily"``, ``"weekly"``, ``"hourly"`` or ``"yes"``).
        dedupe: Remove duplicate records.
        notes: What was assumed, or not understood.
    """

    text: str
    entity: str
    fields: list[str]
    sites: list[str] = field(default_factory=list)
    filters: list[GoalFilter] = field(default_factory=list)
    scope: list[str] = field(default_factory=list)
    limit: int | None = None
    monitor: str | None = None
    dedupe: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def kind(self) -> EntityKind:
        return ENTITIES[self.entity]

    def schema(self) -> Schema:
        """The extraction schema: the goal's fields, typed."""
        return self.kind.schema(self.fields)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, text: str = "") -> Goal:
        """A goal from its dict form (a saved plan, or a model's reading of a request), checked."""
        entity = str(data.get("entity") or "").lower()
        if entity not in ENTITIES:
            raise ConfigurationError(f"unknown entity {entity!r}; one of {', '.join(ENTITIES)}", key="entity")
        fields = [_field_key(str(f)) for f in data.get("fields") or ENTITIES[entity].default_fields]
        if "url" not in fields:
            fields.append("url")
        filters = []
        for item in data.get("filters") or ():
            if isinstance(item, str):
                filters.append(GoalFilter(item, item))
            else:
                filters.append(GoalFilter(str(item["expression"]), str(item.get("text") or ""), item.get("field")))
        limit = data.get("limit")
        return cls(
            text=str(data.get("text") or text),
            entity=entity,
            fields=list(dict.fromkeys(fields)),
            sites=[_site_url(s) for s in data.get("sites") or ()],
            filters=filters,
            scope=[str(s) for s in data.get("scope") or ()],
            limit=int(limit) if limit not in (None, "") else None,
            monitor=data.get("monitor") or None,
            dedupe=bool(data.get("dedupe", True)),
            notes=[str(n) for n in data.get("notes") or ()],
        )

    def describe(self) -> str:
        """The goal as understood, in a few lines."""
        lines = [f"{self.entity}s with {', '.join(self.fields)}"]
        if self.sites:
            lines.append("on " + ", ".join(self.sites))
        if self.scope:
            lines.append("in the section(s): " + ", ".join(self.scope))
        for f in self.filters:
            lines.append(f"where {f.expression}" + (f"   ({f.text})" if f.text and f.text != f.expression else ""))
        if self.limit:
            lines.append(f"at most {self.limit:,}")
        if self.dedupe:
            lines.append("duplicates removed")
        if self.monitor:
            lines.append(f"watched for changes ({self.monitor})")
        lines.extend(f"note: {note}" for note in self.notes)
        return "\n".join(lines)


def _field_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "field"


def _site_url(text: str) -> str:
    text = text.strip().rstrip(".,;")  # sentence punctuation, not the path's slash
    return text if re.match(r"https?://", text, re.I) else "https://" + text


def _money(text: str) -> tuple[float, str | None] | None:
    raw = text.strip().lower().replace(" ", "")
    factor = 1
    match = re.search(r"(\d[\d,.]*)([km])(?![a-z])", raw)
    if match:
        factor = 1000 if match.group(2) == "k" else 1_000_000
    currency = next((code for word, code in _CURRENCY_WORDS.items() if re.search(rf"\b{word}\b", text.lower())), None)
    parsed = parse_money(re.sub(r"(\d)[km](?![a-z])", r"\1", raw), currency=currency)
    if parsed is None:
        return None
    return float(parsed.amount) * factor, parsed.currency or currency


def _number(text: str) -> float:
    return float(text.replace(",", ""))


# Several fields at once
_FIELD_GROUPS: dict[str, tuple[str, ...]] = {
    "contact details": ("telephone", "email", "address"), "contact info": ("telephone", "email", "address"),
    "contact information": ("telephone", "email", "address"), "contact data": ("telephone", "email", "address"),
    "contacts": ("telephone", "email", "address"), "location details": ("address", "city", "country"),
    "full address": ("address",), "geo coordinates": ("latitude", "longitude"), "coordinates": ("latitude", "longitude"),
    "price and currency": ("price", "currency"),
}  # fmt: skip
_LEADS = ("and their", "and its", "with their", "with its", "with the", "including the", "including", "extract",
          "extracting", "with", "get", "getting", "collect", "collecting", "fields", "columns", "capture",
          "capturing", "showing", "containing")  # fmt: skip
# Words that end a list of fields ("... name and price from shop.example")
_BOUNDARY = frozenset(
    [
        "from",
        "on",
        "in",
        "at",
        "for",
        "where",
        "that",
        "which",
        "whose",
        "who",
        "under",
        "over",
        "below",
        "above",
        "between",
        "published",
        "posted",
        "rated",
        "since",
        "after",
        "before",
        "to",
        "into",
        "within",
        "across",
        "of",
        "sorted",
        "ordered",
        "by",
        "if",
        "when",
        "per",
        "as",
        "than",
        "only",
        "then",
        "every",
        "all",
        "each",
        "remove",
        "removing",
        "dedupe",
        "deduplicate",
        "without",
        "keep",
        "save",
        "write",
        "output",
        "export",
        "track",
        "tracking",
        "monitor",
        "watch",
        "near",
        "around",
        "priced",
        "cost",
        "costing",
        "costs",
        "is",
        "are",
        "was",
        "were",
        "be",
    ]
)
_QUANTIFIERS = frozenset(
    [
        "all",
        "every",
        "each",
        "any",
        "find",
        "extract",
        "get",
        "list",
        "collect",
        "scrape",
        "crawl",
        "track",
        "monitor",
        "gather",
        "the",
    ]
)


class _Reader:
    """Reads one request: each rule takes out of the text what it understands."""

    def __init__(self, text: str, now: datetime) -> None:
        self.text = " " + " ".join(text.split()) + " "
        self.now = now
        self.filters: list[GoalFilter] = []
        self.notes: list[str] = []

    def find(self, pattern: str) -> list[re.Match[str]]:
        return list(re.finditer(pattern, self.text, re.I))

    def remove(self, matches: list[re.Match[str]]) -> None:
        """Take matched text out, so that later rules do not read it again (last first: offsets stay valid)."""
        for match in sorted(matches, key=lambda m: m.start(), reverse=True):
            self.text = self.text[: match.start()] + " " + self.text[match.end() :]

    # -- sites --------------------------------------------------------------------------------- #
    def sites(self) -> list[str]:
        urls = self.find(_URL.pattern)
        found = [m.group(0).rstrip(".") for m in urls]
        self.remove(urls)
        domains = [
            m for m in self.find(_DOMAIN.pattern) if not re.fullmatch(r"(?:e\.g|i\.e|etc|vs)\.?", m.group(1), re.I)
        ]
        found += [m.group(1).lower() + (m.group(2) or "") for m in domains]
        self.remove(domains)
        return [_site_url(u) for u in dict.fromkeys(found)]

    # -- conditions ------------------------------------------------------------------------------ #
    def prices(self, entity: str) -> None:
        field_name = "salary" if entity == "job" else "price"
        taken = []
        for match in self.find(rf"\b{_PRICE_WORD}between\s+({_AMOUNT})\s+and\s+({_AMOUNT})"):
            low, high = _money(match.group(1)), _money(match.group(2))
            if low and high and _money_like(match.group(0)):
                self._price(field_name, ">=", low, match.group(0))
                self._price(field_name, "<=", (high[0], high[1] or low[1]), match.group(0))
                taken.append(match)
        self.remove(taken)
        taken = []
        for words, op in ((_LESS, "<"), (_MORE, ">")):
            for match in self.find(rf"\b{_PRICE_WORD}{words}\s+({_AMOUNT})"):
                amount = _money(match.group(1))
                if amount is None or not _money_like(match.group(0)):
                    continue  # "up to 100" alone is a limit, not a price
                phrase = match.group(0).lower()
                inclusive = any(w in phrase for w in ("up to", "at most", "max", "no more", "at least", "min", "="))
                self._price(field_name, op + ("=" if inclusive else ""), amount, match.group(0))
                taken.append(match)
            self.remove(taken)
            taken = []

    def _price(self, field_name: str, op: str, amount: tuple[float, str | None], text: str) -> None:
        value = int(amount[0]) if amount[0] == int(amount[0]) else round(amount[0], 2)
        expression = f"{field_name} {op} {value}"
        if amount[1]:
            expression = f"{expression} and (currency is None or currency == {amount[1]!r})"
        self.filters.append(GoalFilter(expression, text.strip(), field_name))

    def ratings(self) -> None:
        patterns = (
            r"\b(?:rated|with\s+(?:a\s+)?rating\s+(?:of\s+)?|rating\s+(?:of\s+)?|scored?)\s*(at least|above|over|>=?)?\s*(\d(?:\.\d)?)(?:\s*(?:stars?|/\s*5|out of 5))?(\s+or\s+(?:more|higher|better|above))?",
            r"\b(\d(?:\.\d)?)\s*\+?\s*(?:stars?|star\s+ratings?)(\s+or\s+(?:more|higher|better|above))?",
        )  # fmt: skip
        for pattern in patterns:
            found = self.find(pattern)
            for match in found:
                number = next(g for g in match.groups() if g and re.fullmatch(r"\d(?:\.\d)?", g))
                strict = any(g and g.strip().lower() in ("above", "over", ">") for g in match.groups())
                self.filters.append(
                    GoalFilter(f"rating {'>' if strict else '>='} {number}", match.group(0).strip(), "rating")
                )
            self.remove(found)

    def availability(self) -> None:
        found = self.find(
            r"\b(?:(?:that are|which are|currently|only)\s+)?(out of stock|sold out|in stock|available to buy|available now)\b"
        )
        for match in found:
            value = "OutOfStock" if match.group(1).lower() in ("out of stock", "sold out") else "InStock"
            self.filters.append(GoalFilter(f"availability == {value!r}", match.group(0).strip(), "availability"))
        self.remove(found)

    def dates(self, date_field: str | None) -> None:
        if date_field is None:
            return
        today = self.now.date()
        rules: list[tuple[str, Callable[[re.Match[str]], list[tuple[str, date]] | None]]] = [
            (r"\b(?:(?:published|posted|dated|made)\s+)?(?:in|from|during|within|over)\s+the\s+(?:last|past)\s+(\d+|a|an|one|two|three|four|five|six|seven|ten|twelve|thirty)\s+(days?|weeks?|months?|years?)\b",
             lambda m: [(">=", today - timedelta(days=_count(m.group(1)) * _UNITS[m.group(2).lower()]))]),
            (r"\b(?:(?:published|posted|dated|made)\s+)?(?:in\s+the\s+|over\s+the\s+)?(?:last|past)\s+(day|week|month|year)\b",
             lambda m: [(">=", today - timedelta(days=_UNITS[m.group(1).lower()]))]),
            (r"\b(?:(?:published|posted|dated|made)\s+)?(?:this|the current)\s+(week|month|year)\b",
             lambda m: [(">=", _period_start(today, m.group(1).lower()))]),
            (r"\b(?:(?:published|posted|dated|made)\s+)?today\b", lambda m: [(">=", today)]),
            (r"\b(?:(?:published|posted|dated|made)\s+)?(?:after|since)\s+(\d{4}-\d\d-\d\d|(?:\d{1,2}\s+)?[a-z]+\.?\s+(?:\d{1,2},?\s+)?\d{4}|\d{4})\b",
             lambda m: _dated(">=" if "since" in m.group(0).lower() else ">", m.group(1), self.now)),
            (r"\b(?:(?:published|posted|dated|made)\s+)?before\s+(\d{4}-\d\d-\d\d|(?:\d{1,2}\s+)?[a-z]+\.?\s+(?:\d{1,2},?\s+)?\d{4}|\d{4})\b",
             lambda m: _dated("<", m.group(1), self.now)),
            (r"\b(?:(?:published|posted|dated|made|from)\s+)?in\s+((?:19|20)\d\d)\b",
             lambda m: [(">=", date(int(m.group(1)), 1, 1)), ("<", date(int(m.group(1)) + 1, 1, 1))]),
        ]  # fmt: skip
        for pattern, rule in rules:
            found = self.find(pattern)
            for match in found:
                bounds = rule(match)
                if not bounds:
                    self.notes.append(f"could not read the date in {match.group(0).strip()!r}")
                    continue
                for op, day in bounds:
                    expression = f"date({date_field}) {op} {day.isoformat()!r}"
                    self.filters.append(GoalFilter(expression, match.group(0).strip(), date_field))
            self.remove(found)

    def location(self, entity: str) -> None:
        """ "in Berlin", "near Paris": a condition on the city (the location, for jobs)."""
        if entity not in ("place", "company", "event", "job", "person"):
            return
        target = "location" if entity in ("job", "person") else "city"
        found = []
        for match in re.finditer(
            r"\b(?:in|near|around|located in|based in)\s+((?:[A-Z][\w'.-]+)(?:\s+[A-Z][\w'.-]+){0,2})", self.text
        ):
            place = match.group(1)
            if place.split()[0].lower() in _MONTHS:
                continue
            self.filters.append(GoalFilter(f"icontains({target}, {place!r})", match.group(0).strip(), target))
            found.append(match)
        self.remove(found)

    def monitor(self) -> str | None:
        found = self.find(
            r"\b(?:keep\s+)?(?:track|monitor|watch)(?:ing|s)?\b(?:\s+(?:for\s+)?(?:the\s+)?changes?(?:\s+(?:in|to|of))?)?"
            r"|\bchanges?\s+(?:in|to|of)\b|\b(hourly|daily|weekly|every\s+(?:hour|day|week))\b"
        )
        if not found:
            return None
        self.remove(found)
        for match in found:
            word = (match.group(1) or "").lower()
            if word:
                return {"every hour": "hourly", "every day": "daily", "every week": "weekly"}.get(word, word)
        return "yes"

    def limit(self) -> int | None:
        words = "|".join(re.escape(w) for w in sorted(_ALL_WORDS, key=len, reverse=True))
        found = self.find(
            r"\b(?:first|top|up to|at most|max(?:imum)?(?: of)?|limit(?:ed)?(?: to| of)?|no more than)\s+(\d[\d,]*)\b"
        )
        found += self.find(rf"\b(\d[\d,]*)\s+(?:(?:[a-z-]+\s+){{0,2}})(?:{words})\b")
        self.remove([m for m in found if not any(m is n for n in found[: found.index(m)])])
        numbers = [int(m.group(1).replace(",", "")) for m in found]
        return min(numbers) if numbers else None


def _money_like(text: str) -> bool:
    return bool(
        re.search(
            r"[$€£¥₹]|\b(?:usd|eur|gbp|jpy|inr|dollars?|euros?|pounds?|yen|rupees)\b|cost|price|priced|salary|pay",
            text,
            re.I,
        )
    )


def _count(word: str) -> int:
    return int(word) if word.isdigit() else _NUMBER_WORDS.get(word.lower(), 1)


def _period_start(today: date, unit: str) -> date:
    if unit == "week":
        return today - timedelta(days=today.weekday())
    if unit == "month":
        return today.replace(day=1)
    return today.replace(month=1, day=1)


def _dated(op: str, text: str, now: datetime) -> list[tuple[str, date]] | None:
    text = text.strip(" ,.")
    if re.fullmatch(r"\d{4}", text):  # "after 2024": from the next year on
        year = int(text)
        return [(">=", date(year + 1, 1, 1))] if op == ">" else [(op, date(year, 1, 1))]
    day = parse_date(text, now=now)
    return [(op, day)] if day is not None else None


_ALL_WORDS = frozenset(word for kind in ENTITIES.values() for word in kind.words)
_MONTHS = frozenset(
    [
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
        "jan",
        "feb",
        "mar",
        "apr",
        "jun",
        "jul",
        "aug",
        "sep",
        "sept",
        "oct",
        "nov",
        "dec",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ]
)


def _entity(text: str, fields: list[str]) -> tuple[str | None, str | None]:
    """The entity a request names (and the word it used): the one after "all", "every", "find"...
    best, plural nouns next, the first in the text last."""
    best: tuple[int, int, str, str] | None = None
    for kind in ENTITIES.values():
        for word in kind.words:
            for match in re.finditer(rf"\b{re.escape(word)}\b", text, re.I):
                before = text[max(0, match.start() - 40) : match.start()].lower().split()[-3:]
                score = (3 if any(w in _QUANTIFIERS for w in before) else 0) + (1 if word.endswith("s") else 0)
                rank = (-score, match.start())
                if best is None or rank < (best[0], best[1]):
                    best = (rank[0], rank[1], kind.name, word)
    if best is not None:
        return best[2], best[3]
    # no entity named: what the fields suggest
    by_field = {"price": "product", "availability": "product", "published": "article", "author": "article",
                "salary": "job", "company": "job", "start_date": "event", "venue": "event", "telephone": "place",
                "address": "place", "opening_hours": "place", "ingredients": "recipe"}  # fmt: skip
    for name in fields:
        if name in by_field:
            return by_field[name], None
    return None, None


def _field_names(phrase: str) -> tuple[str, ...] | None:
    """The field(s) a phrase names ("stock status" -> availability), dropping leading words it does not
    need ("public business contact details" -> contact details); ``None`` if none."""
    words = phrase.lower().split()
    while words:
        text = " ".join(words)
        for candidate in (text, text[:-1] if text.endswith("s") else text):
            if candidate in _FIELD_GROUPS:
                return _FIELD_GROUPS[candidate]
            if candidate in _FIELD_WORDS:
                return (_FIELD_WORDS[candidate],)
        words = words[1:]
    return None


def _fields(text: str) -> tuple[list[tuple[str, tuple[str, ...] | None]], str]:
    """The fields a request lists ("with name, price and rating"): ``(phrase, fields or None)`` pairs,
    and the text without the lists."""
    lead = "|".join(re.escape(w) for w in sorted(_LEADS, key=len, reverse=True))
    out: list[tuple[str, tuple[str, ...] | None]] = []
    spans = []
    for start in re.finditer(rf"\b(?:{lead})\b\s+", text, re.I):
        pos = start.end()
        phrases: list[str] = []
        current: list[str] = []
        tokens = list(re.finditer(r"[a-z][\w'-]*|,|&|/|\.|;|:", text[pos:], re.I))
        for i, token in enumerate(tokens):
            word = token.group(0).lower()
            if word in (",", "&", "/", "and"):
                if current:
                    phrases.append(" ".join(current))
                    current = []
                continue
            if word == "of" and current and i + 1 < len(tokens):
                following = tokens[i + 1].group(0).lower()
                if following not in _BOUNDARY and following not in _STOP and following not in _ALL_WORDS:
                    current.append(word)  # "number of bedrooms", not "price of every product"
                    continue
            if word in (".", ";", ":") or word in _BOUNDARY or (word in _LEADS and not current):
                break
            if word in ("the", "their", "its", "a", "an"):
                continue
            current.append(word)
            end = pos + token.end()
            if len(current) > 5:
                break
        if current:
            phrases.append(" ".join(current))
        kept = []
        for phrase in phrases:
            names = _field_names(phrase)
            words = phrase.split()
            if names is None and (phrase in _ALL_WORDS or all(w in _STOP or w in _ALL_WORDS for w in words)):
                continue
            kept.append((phrase, names))
        if kept and any(names for _, names in kept):  # a list with at least one known field
            out.extend(kept)
            spans.append((start.start(), end))
    rest = text
    for begin, finish in sorted(spans, reverse=True):
        rest = rest[:begin] + " " + rest[finish:]
    return out, rest


def _scope(text: str) -> list[str]:
    """Words that narrow the request down to part of a site: "laptops", "the Phones category"."""
    out = []
    for match in re.finditer(
        r"\b(?:in|from|under)\s+the\s+([\w&' -]+?)\s+(?:category|section|department|collection|aisle)\b", text, re.I
    ):
        out.append(match.group(1).strip().lower())
    for match in re.finditer(
        r"\b(?:all|every|each|any)\s+(?:the\s+|of\s+the\s+)?((?:[a-z][\w-]*\s+){0,2}[a-z][\w-]*)", text, re.I
    ):
        words = match.group(1).lower().split()
        if any(w in _ALL_WORDS for w in words):
            continue
        kept = [w for w in words if w not in _STOP and w not in _BOUNDARY]
        # the noun phrase up to its first plural noun: "laptops", "gaming laptops"
        phrase: list[str] = []
        for word in kept:
            phrase.append(word)
            if word.endswith("s") and len(word) > 3:
                break
        if phrase and phrase[-1].endswith("s") and not _field_names(" ".join(phrase)):
            out.append(" ".join(phrase))
    return list(dict.fromkeys(out))


def parse_goal(
    text: str,
    *,
    sites: list[str] | None = None,
    parser: Callable[[str], Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> Goal:
    """A :class:`Goal` from a request in plain words (see the module docs).

    Args:
        sites: Sites to add to those the request names.
        parser: Reads the request instead of the built-in rules: ``parser(text)`` returns the goal as a
            dict (``entity``, ``fields``, ``filters`` as expressions, ``sites``, ``scope``, ``limit``,
            ``monitor``), which is checked like any other.
        now: Reference time for relative dates ("in the last 30 days").
    """
    if not text or not text.strip():
        raise ConfigurationError("the goal is empty: say what to collect, e.g. 'products with name and price'")
    extra_sites = [_site_url(s) for s in sites or ()]
    if parser is not None:
        goal = Goal.from_dict(parser(text), text=text)
        goal.sites = list(dict.fromkeys([*goal.sites, *extra_sites]))
        return goal
    reader = _Reader(text, now or datetime.now(timezone.utc))
    found_sites = reader.sites()
    listed, rest = _fields(reader.text)
    guessed = [name for _, names in listed for name in names or ()]
    entity, entity_word = _entity(rest, guessed)
    notes = []
    if entity is None:
        entity = "product"
        notes.append("no kind of record named (products, articles, jobs, events...): assuming products")
    kind = ENTITIES[entity]
    monitor = reader.monitor()
    reader.prices(entity)
    reader.ratings()
    if entity == "product":
        reader.availability()
    reader.dates(kind.date_field)
    reader.location(entity)
    limit = reader.limit()
    listed, rest = _fields(reader.text)
    fields: list[str] = []
    for phrase, names in listed:
        if names is None:
            name = _field_key(phrase)
            notes.append(f"{phrase!r} is not a field wintergrab knows for {entity}s: it will look for a label like it")
            fields.append(name)
            continue
        for name in names:
            name = _FIELD_BY_ENTITY.get((entity, name), name)
            if name not in kind.fields:
                notes.append(f"{name!r} is not a usual field of {entity}s: it will look for it all the same")
            fields.append(name)
    if not fields:
        fields = list(kind.default_fields)
        notes.append(f"no fields named: {', '.join(fields)}")
    identity = kind.default_fields[0]  # what names a record: its name, or title
    if identity not in fields and not any(f in fields for f in ("name", "title")):
        fields.insert(0, identity)
    for condition in reader.filters:
        if condition.field and condition.field not in fields:
            fields.append(condition.field)  # a condition needs its field
    for money_field in ("price", "salary"):
        if money_field in fields and "currency" not in fields:
            fields.insert(fields.index(money_field) + 1, "currency")
    if "url" not in fields:
        fields.append("url")
    scope = [s for s in _scope(rest) if s != (entity_word or "").lower()]
    dedupe = not re.search(r"\b(?:keep|with|including)\s+(?:the\s+)?duplicates\b", text, re.I)
    if re.search(r"\bconvert\w*\b.*\b(?:usd|eur|gbp|dollars|euros|pounds)\b", text, re.I):
        notes.append("prices are kept in their own currency: converting needs exchange rates (ConvertCurrency)")
    return Goal(
        text=text,
        entity=entity,
        fields=list(dict.fromkeys(fields)),
        sites=list(dict.fromkeys([*found_sites, *extra_sites])),
        filters=reader.filters,
        scope=scope,
        limit=limit,
        monitor=monitor,
        dedupe=dedupe,
        notes=notes + reader.notes,
    )
