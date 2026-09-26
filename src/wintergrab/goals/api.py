"""Collecting a goal's records from the API a site's pages call.

Many sites build their pages with JavaScript from a JSON API: a page is an empty shell until the browser has
asked the API for its records. When the pages a plan samples are rendered (``plan_goal(browser=True)``, or
the planner rendering a few of those that need JavaScript), the calls they make are recorded
(:mod:`wintergrab.intel.sources`). If one answers with the goal's records, their fields named like the
goal's, and its pages can be followed, the plan collects the records from it: one request per page of the
API, over HTTP, instead of one per record page (in a browser, for pages that need one)::

    source = find_api(goal, rendered_pages)
    source.describe()      # 'GET shop.example/api/products?page&per_page: 4 records a page (pages by page: 1 of 3)'
    source.fields          # {'name': 'title', 'price': 'price', 'rating': 'rating.average', 'url': 'url'}

The API is asked the way the page asked it: the same URL, method and body, the page parameter changed (a page
number, an offset, a cursor from the last answer, or the next page's URL it gives). No header the page added
is sent again, credentials included. An API that refuses, or answers without records, is left for the plan's
pages, and its URLs obey robots.txt, the network policy and the crawl's throttling like any page's.

Only reading calls are used: GETs, and GraphQL queries (a POST that is not a GraphQL query, or a mutation, may
change data). A field is taken from the first key, among its names, whose values read as the field's type
(``price`` from ``price``, ``price.amount``, ``offers.price``...; ``url`` from ``url``, ``link``...).
"""

from __future__ import annotations

import copy
import json
import math
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..data.schema import NormalizeContext, Schema
from ..extraction.schemaorg import FIELD_PATHS, camel
from ..intel.sources import ApiCall, Collection, api_calls, pagination_of
from ..redact import is_sensitive

if TYPE_CHECKING:
    from .goal import Goal

__all__ = ["ApiSource", "find_api", "items_at", "map_fields", "next_page", "records_of", "total_of"]

#: How many API pages a run asks for at most (the crawl's own budgets apply too).
MAX_API_PAGES = 10_000
_SAMPLE = 20  # records looked at to map a field

#: Names JSON APIs commonly give a field, besides its own and schema.org's (``FIELD_PATHS``).
API_NAMES: dict[str, tuple[str, ...]] = {
    "name": ("name", "title", "product_name", "productName", "label", "display_name", "displayName"),
    "title": ("title", "name", "headline", "label"),
    "price": ("price", "price.amount", "price.value", "price.current", "prices.price", "amount", "cost",
              "current_price", "currentPrice", "sale_price", "salePrice", "price_amount", "final_price"),
    "currency": ("currency", "price.currency", "price.currency_code", "currency_code", "currencyCode",
                 "priceCurrency", "prices.currency"),
    "rating": ("rating", "rating.value", "rating.average", "rating.rate", "stars", "average_rating",
               "averageRating", "avg_rating", "rating_value", "ratingValue", "score"),
    "review_count": ("review_count", "reviews_count", "reviewCount", "reviewsCount", "num_reviews", "rating.count",
                     "reviews.count", "ratings_count"),
    "url": ("url", "link", "href", "permalink", "canonical_url", "canonicalUrl", "web_url", "webUrl", "path"),
    "image": ("image", "image_url", "imageUrl", "image.url", "img", "thumbnail", "thumbnail_url", "picture",
              "images"),
    "availability": ("availability", "stock_status", "stockStatus", "in_stock", "inStock", "stock", "available"),
    "brand": ("brand", "brand.name", "manufacturer", "vendor"),
    "sku": ("sku", "mpn", "product_id", "productId"),
    "description": ("description", "summary", "excerpt", "body", "text"),
    "date": ("date", "published_at", "publishedAt", "created_at", "createdAt", "date_published"),
    "published": ("published", "published_at", "publishedAt", "date", "created_at", "createdAt"),
    "author": ("author", "author.name", "byline", "writer"),
    "company": ("company", "company.name", "company_name", "companyName", "employer", "organization"),
    "location": ("location", "location.name", "city", "place"),
    "salary": ("salary", "salary.amount", "salary.min", "compensation", "pay"),
}  # fmt: skip
#: Reading calls: GraphQL queries may be POSTs; any other POST may change something.
_READING = ("GET", "POST")
#: Parameter names that hold a key in an API's URL or body, besides those named like a credential
#: (:func:`~wintergrab.redact.is_sensitive`).
_KEYS = frozenset({"key", "sig", "hmac", "jwt", "bearer"})


@dataclass
class ApiSource:
    """An API a site's pages call, holding the goal's records (see the module docs).

    Attributes:
        method: ``"GET"``, or ``"POST"`` for a GraphQL query.
        url: The call's URL, as the page made it.
        body: Its JSON body (a GraphQL query and its variables), when it has one.
        graphql: The GraphQL operation (``"query Products"``), when it is one.
        path: Where the records are in an answer (``items[]``, ``data.products.edges[].node``).
        fields: Goal field -> where a record holds it (``{"price": "price.amount"}``).
        pagination: How its pages go (``Pagination.to_dict()`` of the page's call).
        per_page: Records in the answer the page got.
        total: Records in all, when the API says.
        pages: Pages in all, when the API says (or its total and page size do).
        seen_on: The page that made the call.
        template: The call's URL pattern, for reports.
        bytes: The size of the page's answer.
        examples: The goal's records in that answer, as they would be collected (not saved with a plan).
        read_seconds: How long reading that answer took (parsing it, reading its records), measured when the
            API was found (not saved with a plan).
    """

    method: str
    url: str
    body: Any = None
    graphql: str | None = None
    path: str = "[]"
    fields: dict[str, str] = field(default_factory=dict)
    pagination: dict[str, Any] | None = None
    per_page: int = 0
    total: int | None = None
    pages: int | None = None
    seen_on: str = ""
    template: str = ""
    bytes: int = 0
    examples: list[dict[str, Any]] = field(default_factory=list, repr=False, compare=False)
    read_seconds: float = field(default=0.0, repr=False, compare=False)

    def describe(self) -> str:
        """``GET shop.example/api/products?page: 4 record(s) a page (pages by page: 3 in all)``."""
        head = f"{self.method} {self.template or self.url}" + (f", {self.graphql}" if self.graphql else "")
        size = f"{self.per_page} record(s) a page"
        if self.pagination:
            kind, parameter = self.pagination.get("kind"), self.pagination.get("parameter")
            size += f" (pages by {kind}{f' {parameter}' if parameter and parameter != kind else ''}"
            size += f": {self.pages} in all)" if self.pages else ")"
        return f"{head}: {size}"

    def mapping(self) -> str:
        """``name <- title, price <- price.amount...``: where each field is read from."""
        return ", ".join(f"{name} <- {path}" if name != path else name for name, path in self.fields.items())

    def to_dict(self) -> dict[str, Any]:
        """The source as a plan's JSON holds it (``SitePlan.api``), without its examples."""
        out = asdict(self)
        out.pop("examples")
        out.pop("read_seconds")
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ApiSource:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


# --------------------------------------------------------------------------- #
# finding the API
# --------------------------------------------------------------------------- #
def find_api(
    goal: Goal, pages: Iterable[Any], *, schema: Schema | None = None, notes: list[str] | None = None
) -> ApiSource | None:
    """The API, among the calls ``pages`` made as they rendered (their ``captured`` calls), that answers
    with ``goal``'s records, their name (or title) and as many of the goal's fields as it can; ``None`` if
    none does, or if its pages cannot be followed. ``schema``: what records are read with (the goal's).
    ``notes``: when none is found, receives why the calls that held the goal's records were not used."""
    schema = schema if schema is not None else goal.schema()
    calls: list[Any] = []
    origins: list[str | None] = []
    for page in pages:
        for call in getattr(page, "captured", None) or ():
            calls.append(call)
            origins.append(getattr(page, "url", None))
    if not calls:
        return None
    identity = next((f for f in goal.fields if f in ("name", "title")), goal.fields[0])
    wanted = [f for f in goal.fields if f != "currency"]
    bodies: dict[str, bytes] = {}
    for captured in calls:
        bodies.setdefault(captured.url, getattr(captured, "body", b"") or b"")
    best: tuple[tuple[int, int, int], ApiSource] | None = None
    passed_over: list[str] = []
    for call in api_calls(calls, pages=origins):
        if not (200 <= call.status < 300):
            continue
        base = call.page or call.url  # links in an answer lead where they would from the page that asked
        for collection in call.collections[:5]:
            fields = map_fields(schema, [f for f in goal.fields if f in schema], collection, base_url=base)
            if identity not in fields:
                continue
            said = f"the pages call {call.method} {call.template}" + (f" ({call.graphql})" if call.graphql else "")
            said += f", which holds {goal.kind.name} records"
            if not _reading(call):
                what = "a GraphQL mutation" if call.mutation else "a POST that is not a GraphQL query"
                passed_over.append(f"{said}; asking it may change something ({what}), so it is not used")
                break
            secret = _credential(call)
            if secret is not None:
                passed_over.append(f"{said}, with a credential ({secret}): it is not used")
                break
            source = _source(call, collection, fields)
            if source is None:
                passed_over.append(f"{said}, but how to ask for its next page cannot be told: it is not used")
                continue
            body = bodies.get(call.url, b"")
            source.bytes = len(body)
            started = time.perf_counter()  # read as the run reads each page: parsed, its records read
            answer = _json(body)
            items = items_at(answer, source.path) if answer is not None else collection.records
            source.examples = _records(source, items, schema, base_url=base)
            source.read_seconds = time.perf_counter() - started
            score = (sum(1 for f in wanted if f in fields), collection.count, -len(source.path))
            if best is None or score > best[0]:
                best = (score, source)
    if best is None and notes is not None:
        notes.extend(dict.fromkeys(passed_over))
    return best[1] if best is not None else None


def _json(body: bytes) -> Any:
    try:
        return json.loads(body) if body else None
    except ValueError:
        return None


def _reading(call: ApiCall) -> bool:
    if call.method not in _READING or call.mutation:
        return False
    return call.method == "GET" or bool(call.graphql)  # a POST only as a GraphQL query


def _credential(call: ApiCall) -> str | None:
    """What names a credential the call carries in its URL or body (``api_key``), if it carries one: such an
    API is not used (its credential would be kept in the plan, and sent again)."""
    parts = urlsplit(call.url)
    if parts.username or parts.password:
        return "a user name and password"
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if value and (is_sensitive(key) or key.lower() in _KEYS):
            return key
    return _sensitive_key(call.request)


def _sensitive_key(data: Any, depth: int = 0) -> str | None:
    if depth > 6:
        return None
    if isinstance(data, Mapping):
        for key, value in data.items():
            named = is_sensitive(key) or str(key).lower() in _KEYS
            if named and value not in (None, "", [], {}) and not isinstance(value, bool):
                return str(key)
            found = _sensitive_key(value, depth + 1)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data[:20]:
            found = _sensitive_key(item, depth + 1)
            if found is not None:
                return found
    return None


def _source(call: ApiCall, collection: Collection, fields: dict[str, str]) -> ApiSource | None:
    """The call as a source, if all its records can be had: one answer holds them all, or its next page can
    be asked for."""
    pagination = call.pagination
    variables = _variables(call.request, call.graphql)
    total = pagination.total if pagination is not None else None
    pages = pagination.pages if pagination is not None else None
    complete = (
        pagination is not None
        and pagination.next is None
        and pagination.next_url is None
        and (
            pagination.more is False
            or (pages is not None and pagination.value == pages)
            or (total is not None and total <= collection.count)
        )
    )
    followable = pagination is not None and (
        pagination.next_url is not None
        or (pagination.next is not None and pagination.parameter is not None and (
            _in_query(call.url, pagination.parameter) or (variables is not None and pagination.parameter in variables)
        ))
    )  # fmt: skip
    if not (complete or followable):
        return None
    if pages is None and total is not None and collection.count:
        pages = math.ceil(total / collection.count)
    return ApiSource(
        method=call.method,
        url=call.url,
        body=call.request,
        graphql=call.graphql,
        path=collection.path,
        fields=fields,
        pagination=pagination.to_dict() if pagination is not None else None,
        per_page=collection.count,
        total=total,
        pages=1 if complete and pages is None else pages,
        seen_on=call.page or "",
        template=call.template,
    )


def map_fields(
    schema: Schema, fields: Iterable[str], collection: Collection, *, base_url: str | None = None
) -> dict[str, str]:
    """Goal field -> where ``collection``'s records hold it: the first of the field's names (its own, its
    aliases, :data:`API_NAMES`, schema.org's) whose values, in half the records or more, read as the field's
    type. A key already taken by another field is not taken again."""
    records = collection.records[:_SAMPLE]
    context = NormalizeContext(base_url=base_url)
    taken: set[str] = set()
    out: dict[str, str] = {}
    for name in fields:
        spec = schema[name]
        for path in _names(name, spec.aliases):
            if path in taken:
                continue
            values = [_raw(record, path) for record in records]
            present = [v for v in values if v not in (None, "", [], {})]
            if len(present) * 2 < len(records) or not present:
                continue
            readable = sum(1 for v in present if _reads_as(schema, name, v, context))
            if readable * 2 >= len(present):
                out[name] = path
                taken.add(path)
                break
    return out


def _names(name: str, aliases: Iterable[str]) -> list[str]:
    names = [name, camel(name), *aliases, *API_NAMES.get(name, ()), *FIELD_PATHS.get(name, ())]
    return list(dict.fromkeys(names))


def _reads_as(schema: Schema, name: str, value: Any, context: NormalizeContext) -> bool:
    if isinstance(value, Mapping) and schema[name].type != "money" and schema[name].type != "object":
        value = _unwrap(value)
        if value is None:
            return False
    try:
        result = schema.normalize_value(name, value, context=context)
    except Exception:
        return False
    return result.ok and result.value not in (None, "", [])


def _unwrap(value: Mapping[str, Any]) -> Any:
    """An object as a plain value: its name, value, text or URL (``{"name": "Acme"}`` -> ``"Acme"``)."""
    for key in ("name", "value", "text", "url", "href", "label", "title"):
        inner = value.get(key)
        if isinstance(inner, (str, int, float)) and not isinstance(inner, bool):
            return inner
    return None


def _raw(record: Any, path: str) -> Any:
    """The value at dotted ``path`` in ``record`` (the first item of a list along the way)."""
    node = record
    for part in path.split("."):
        if isinstance(node, list):
            node = node[0] if node else None
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


# --------------------------------------------------------------------------- #
# reading an answer
# --------------------------------------------------------------------------- #
_TOKEN = re.compile(r"\{[^}]*\}|\[\]|[^.\[\]{}]+")


def items_at(data: Any, path: str) -> list[Mapping[str, Any]]:
    """The records at ``path`` in an answer (a :class:`~wintergrab.intel.sources.Collection` path:
    ``items[]``, ``data.products.edges[].node``, ``[]``, ``__APOLLO_STATE__{Product}``)."""
    current: list[Any] = [data]
    for token in _TOKEN.findall(path):
        found: list[Any] = []
        for node in current:
            if token == "[]":
                if isinstance(node, list):
                    found.extend(node)
            elif token.startswith("{"):
                label = token[1:-1]
                if isinstance(node, Mapping):
                    found.extend(
                        v
                        for v in node.values()
                        if isinstance(v, Mapping) and (not label or v.get("__typename") == label)
                    )
            elif isinstance(node, Mapping) and token in node:
                found.append(node[token])
        current = found
    return [item for item in current if isinstance(item, Mapping)]


def records_of(source: ApiSource, answer: Any, schema: Schema, *, base_url: str | None = None) -> list[dict[str, Any]]:
    """The goal's records in one of the API's answers: each record's mapped fields, read as their types
    (money split into amount and currency, URLs made absolute against ``base_url``: by default the page that
    made the call)."""
    return _records(source, items_at(answer, source.path), schema, base_url=base_url or source.seen_on or source.url)


def _records(
    source: ApiSource, items: Iterable[Mapping[str, Any]], schema: Schema, *, base_url: str
) -> list[dict[str, Any]]:
    out = []
    for item in items:
        raw: dict[str, Any] = {}
        for name, path in source.fields.items():
            value = _raw(item, path)
            if isinstance(value, Mapping) and schema[name].type not in ("money", "object"):
                value = _unwrap(value)
            if value not in (None, "", [], {}):
                raw[name] = value
        if not raw:
            continue
        record, _ = schema.normalize(raw, base_url=base_url)
        out.append({k: v for k, v in record.items() if k in raw or k in schema})
    return out


# --------------------------------------------------------------------------- #
# the next page
# --------------------------------------------------------------------------- #
def next_page(source: ApiSource, url: str, body: Any, answer: Any, records: int) -> tuple[str, Any] | None:
    """The next page's URL and body after the answer to ``url``/``body`` (``records`` records in it), or
    ``None`` when it was the last."""
    if records == 0:
        return None
    variables = _variables(body, source.graphql)
    pagination = pagination_of(url, answer, request=variables, records=records)
    if pagination is None:
        return None
    if pagination.next is not None and pagination.parameter:
        if _in_query(url, pagination.parameter):
            return _with_query(url, pagination.parameter, pagination.next), body
        if variables is not None and pagination.parameter in variables:
            return url, _with_variable(body, source.graphql, pagination.parameter, pagination.next)
    if pagination.next_url:
        return pagination.next_url, body
    return None


def total_of(source: ApiSource, url: str, body: Any, answer: Any, records: int) -> int | None:
    """How many records the API says it has, in its answer to ``url``/``body``, when it says."""
    pagination = pagination_of(url, answer, request=_variables(body, source.graphql), records=records)
    return pagination.total if pagination is not None else None


def _variables(body: Any, graphql: str | None) -> Mapping[str, Any] | None:
    if isinstance(body, list):
        body = body[0] if body else None
    if not isinstance(body, Mapping):
        return None
    if graphql is not None:
        variables = body.get("variables")
        return variables if isinstance(variables, Mapping) else None
    return body


def _with_variable(body: Any, graphql: str | None, name: str, value: Any) -> Any:
    new = copy.deepcopy(body)
    target = new[0] if isinstance(new, list) else new
    if graphql is not None:
        target.setdefault("variables", {})[name] = value
    else:
        target[name] = value
    return new


def _in_query(url: str, name: str) -> bool:
    return any(key == name for key, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True))


def _with_query(url: str, name: str, value: Any) -> str:
    parts = urlsplit(url)
    pairs = [(k, str(value) if k == name else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), parts.fragment))
