"""Where a page's data is: every source that holds it, and the records each holds.

A page often holds the same records several ways: cards in its HTML, JSON-LD written for search engines,
the state a JavaScript app embeds in the page (``__NEXT_DATA__``), and the API calls the page makes as it
renders. The source with typed fields and no layout in the way is usually the one to read.
:func:`data_sources` lists them::

    page = browser.get("https://shop.example/catalog", capture=True)   # capture: record the page's API calls
    print(data_sources(page).describe())

    https://shop.example/catalog
      HTML           6 records (article.card): title, url, price
      tables         3 rows (Size, Chest)
      JSON-LD        Organization, ItemList
      microdata      none
      meta           og:title, og:type, title, description, language
      embedded JSON  __NEXT_DATA__: 6 record(s) at props.pageProps.products[] (id, name, price, currency, url, rating +1)
      API calls      3 recorded as the page rendered
                     GET shop.example/api/products?limit&page: 3 record(s) at items[] (id, name, price, url)
                       pages by page: 1 of 2, 6 record(s) in all, next 2; called 2 times (page 1, 2)
                     POST shop.example/graphql, query Reviews: 2 record(s) at data.reviews.edges[].node (id, author, ...)
                       pages by cursor after: next 'r2'
                     POST shop.example/graphql, mutation TrackView: changes data (a mutation), no source to read
      richest        embedded JSON __NEXT_DATA__ props.pageProps.products[]: 6 records, 7 fields

Over HTTP no call is recorded: the endpoints the page's scripts name are listed instead, unrequested.

:func:`json_collections` finds the lists of records in any JSON document, :func:`pagination_of` says how an
API's pages go, and :func:`api_calls` reads the calls a browser recorded (``capture=True``), grouped by URL
pattern, with GraphQL operations named.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from typing import TYPE_CHECKING, Any, NamedTuple
from urllib.parse import parse_qsl, urljoin, urlsplit

from ..urls import url_template

if TYPE_CHECKING:
    from ..data.schema import Schema

__all__ = [
    "ApiCall",
    "Collection",
    "DataSources",
    "HtmlRecords",
    "Pagination",
    "Source",
    "api_calls",
    "data_sources",
    "json_collections",
    "pagination_of",
]

MAX_DEPTH = 12  # how deep json_collections looks
_TYPE_SAMPLE = 50  # records read to type a collection's fields
_REFERENCES = frozenset({"__ref", "$ref", "__id"})  # records that only point at others (an app's normalized state)


# --------------------------------------------------------------------------- #
# lists of records in JSON
# --------------------------------------------------------------------------- #
@dataclass
class Collection:
    """A list of records in a JSON document.

    Attributes:
        path: Where the records are: ``items[]``, ``data.products.edges[].node``, ``[]`` (the document is the
            list). ``[]`` is each item of a list; ``{}`` each value of a map of records keyed by id
            (``{Product}``: those whose ``__typename`` is ``Product``).
        count: How many records.
        fields: Their fields, the most common first.
        types: Each field's type, read from the records' values (``integer``, ``money``, ``url``, ``object``...;
            ``[]`` after a list's type).
        records: The records themselves.
    """

    path: str
    count: int
    fields: list[str]
    types: dict[str, str]
    records: list[Mapping[str, Any]] = field(repr=False, compare=False)

    def schema(self, name: str = "records") -> Schema:
        """A data schema for these records (:func:`~wintergrab.data.inference.infer_schema`): a start to review."""
        from ..data.inference import infer_schema

        return infer_schema(self.records, name=name)

    def describe(self, fields: int = 6) -> str:
        shown = ", ".join(self.fields[:fields]) + (
            f" +{len(self.fields) - fields}" if len(self.fields) > fields else ""
        )
        return f"{self.count} record(s) at {self.path} ({shown})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "count": self.count,
            "fields": self.fields,
            "types": self.types,
            "sample": _shallow(self.records[0]) if self.records else None,
        }


def json_collections(data: Any, *, min_records: int = 2) -> list[Collection]:
    """The lists of records in a JSON document, those holding the most values (records x fields) first.

    A list is one when at least ``min_records`` of its items, and four in five, are objects. GraphQL
    connections are read through (``edges[].node``). A map of objects keyed by ids (``{"Product:1": {...}}``,
    an app's normalized state) counts too, split by ``__typename`` when its objects have one. Lists inside
    records are collections of their own, all the records' together (``products[].variants[]``).
    """
    found: list[Collection] = []
    _walk([data], "", 0, max(1, min_records), found)
    found.sort(key=lambda c: (-c.count * len(c.fields), c.path))
    return found


def _walk(values: list[Any], path: str, depth: int, min_records: int, found: list[Collection]) -> None:
    """Look at ``values``, everything found at ``path`` (the values of one field across a list's records)."""
    if depth > MAX_DEPTH or not values:
        return
    items = [item for value in values if isinstance(value, list) for item in value]
    if items:
        records = [item for item in items if isinstance(item, Mapping)]
        if len(records) >= min_records and len(records) * 5 >= len(items) * 4:
            where = path + "[]"
            if all(isinstance(record.get("node"), Mapping) for record in records):  # a GraphQL connection's edges
                records = [record["node"] for record in records]
                where += ".node"
            _add(found, where, records, depth, min_records)
        else:
            _walk(items, path + "[]", depth + 1, min_records, found)
    maps = [value for value in values if isinstance(value, Mapping)]
    if not maps:
        return
    if len(maps) == 1:
        groups, rest = _keyed_records(maps[0], min_records)
        for label, group in groups:
            _add(found, f"{path}{{{label}}}", group, depth, min_records)
        if groups:  # the ids are keys, not fields; what else the map holds is read as fields
            if rest:
                _walk_fields([rest], path, depth, min_records, found)
            return
    _walk_fields(maps, path, depth, min_records, found)


def _walk_fields(
    records: list[Mapping[str, Any]], path: str, depth: int, min_records: int, found: list[Collection]
) -> None:
    for key in dict.fromkeys(key for record in records for key in record):
        values = [record[key] for record in records if key in record]
        _walk(values, f"{path}.{key}" if path else str(key), depth + 1, min_records, found)


def _add(found: list[Collection], path: str, records: list[Mapping[str, Any]], depth: int, min_records: int) -> None:
    collection = _collection(path, records)
    if not set(collection.fields) <= _REFERENCES:  # a list of pointers to records kept elsewhere
        found.append(collection)
    _walk_fields(records, path, depth + 1, min_records, found)


def _collection(path: str, records: list[Mapping[str, Any]]) -> Collection:
    counts = Counter(str(key) for record in records for key in record)
    fields = [key for key, _ in counts.most_common()]  # ties keep the order they were first seen in
    return Collection(path, len(records), fields, _types(records[:_TYPE_SAMPLE]), list(records))


def _types(records: list[Mapping[str, Any]]) -> dict[str, str]:
    from ..data.inference import infer_schema

    try:
        schema = infer_schema(records)
    except Exception:  # pragma: no cover - a value no type reader expected
        return {}
    return {
        (f.aliases[0] if f.aliases else f.name): f.type + ("[]" if f.many else "")
        for f in schema.fields
        if not str(f.aliases[0] if f.aliases else f.name).startswith("_")
    }


_ID_KEY = re.compile(r".*\d.*|.+:.+|[0-9a-f]{16,}", re.I)


def _keyed_records(mapping: Mapping[str, Any], min_records: int) -> tuple[list[tuple[str, list[Any]]], dict[str, Any]]:
    """A map of records keyed by id (``{"Product:1": {...}, "Product:2": {...}}``), by ``__typename``; and the
    rest of the map (``ROOT_QUERY``...), to read as fields."""
    objects = {key: value for key, value in mapping.items() if isinstance(value, Mapping) and value}
    keyed = {key: value for key, value in objects.items() if _ID_KEY.fullmatch(str(key)) or _has_id(value)}
    least = max(3, min_records)
    if len(keyed) < least or len(keyed) * 5 < len(mapping) * 4:
        return [], dict(mapping)  # keys that are field names: a record whose fields are objects ({"price": {...}})
    typed = all(isinstance(value.get("__typename"), str) for value in keyed.values())
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for value in keyed.values():
        groups.setdefault(value["__typename"] if typed else "", []).append(value)
    out = []
    for label, group in groups.items():
        if len(group) < least:
            continue
        keys = [set(record) for record in group]
        shared, every = set.intersection(*keys), set.union(*keys)
        if len(shared) >= 2 and len(shared) * 2 >= len(every):
            out.append((label, group))
    if not out:
        return [], dict(mapping)
    return out, {key: value for key, value in mapping.items() if key not in keyed}


def _has_id(record: Mapping[str, Any]) -> bool:
    return any(key in record for key in ("id", "_id", "uuid", "slug", "sku", "key"))


def _shallow(record: Mapping[str, Any], limit: int = 200) -> dict[str, Any]:
    """A record for a report: text cut at ``limit`` characters, lists and objects by their size."""
    out: dict[str, Any] = {}
    for key, value in record.items():
        if isinstance(value, str):
            out[str(key)] = value if len(value) <= limit else value[:limit] + "..."
        elif isinstance(value, list):
            out[str(key)] = f"[{len(value)} item(s)]"
        elif isinstance(value, Mapping):
            out[str(key)] = f"{{{len(value)} field(s)}}"
        else:
            out[str(key)] = value
    return out


# --------------------------------------------------------------------------- #
# pagination
# --------------------------------------------------------------------------- #
@dataclass
class Pagination:
    """How an API's pages go, as far as one call and its answer tell.

    Attributes:
        kind: ``"page"`` (a page number), ``"offset"`` (the first record's position), ``"cursor"`` (a token the
            answer gives for the next page) or ``"next"`` (the answer gives the next page's URL).
        parameter: The query parameter or variable that says which page (``page``, ``offset``, ``after``);
            ``None`` when this call does not send one (a first page asked for without it).
        value: This call's value of it.
        size: Records per page, when the call says (``limit=20``, ``first: 10``).
        next: The next page's parameter value (a page number, an offset, a cursor), when there is one to tell.
        next_url: The next page's URL, when the answer gives it.
        total: How many records in all, when the answer says.
        pages: How many pages, when the answer says (or its total and the page size do).
        more: Whether there are more pages, when the answer says (``hasNextPage``, ``has_more``).
    """

    kind: str
    parameter: str | None = None
    value: Any = None
    size: int | None = None
    next: Any = None
    next_url: str | None = None
    total: int | None = None
    pages: int | None = None
    more: bool | None = None

    def describe(self) -> str:
        text = f"pages by {self.kind}" + (
            f" {self.parameter}" if self.parameter and self.parameter != self.kind else ""
        )
        details = []
        if self.kind == "page" and self.value is not None:
            details.append(f"{self.value} of {self.pages}" if self.pages else f"page {self.value}")
        elif self.value is not None:
            details.append(f"this one {self.value!r}" if self.kind == "cursor" else f"at {self.value}")
        if self.total is not None:
            details.append(f"{self.total:,} record(s) in all")
        if self.next is not None:
            details.append(f"next {self.next!r}" if isinstance(self.next, str) else f"next {self.next}")
        elif self.next_url:
            details.append(f"next {self.next_url}")
        elif self.more is False or (self.pages and self.value == self.pages):
            details.append("the last page")
        return text + (": " + ", ".join(details) if details else "")

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in self.__dict__.items() if value is not None}


def _norm(name: Any) -> str:
    return re.sub(r"[-_\s]", "", str(name)).lower()


_PAGE_NAMES = frozenset({"page", "p", "pg", "paged", "pagenum", "pageno", "pagenumber", "pageindex", "currentpage"})
_OFFSET_NAMES = frozenset({"offset", "start", "skip", "from", "startindex", "startat", "firstresult"})
_CURSOR_NAMES = frozenset({
    "cursor", "after", "before", "continuation", "pagetoken", "nextpagetoken", "nextcursor", "startcursor",
    "startingafter", "endingbefore", "sinceid", "maxid", "scrollid", "searchafter", "marker",
})  # fmt: skip
_SIZE_NAMES = frozenset({
    "limit", "perpage", "pagesize", "size", "count", "rows", "first", "last", "num", "hitsperpage", "take", "top",
    "maxresults", "resultsperpage", "itemsperpage",
})  # fmt: skip
# keys of an answer
_NEXT_KEYS = frozenset({"next", "nexturl", "nextpageurl", "nextlink", "nextpage", "nexthref"})
_NEXT_CURSOR_KEYS = frozenset({
    "endcursor", "nextcursor", "cursor", "nextpagetoken", "continuation", "scrollid", "nextmarker", "nextkey",
    "after",
})  # fmt: skip
_MORE_KEYS = frozenset({"hasnextpage", "hasmore", "more", "hasnext", "moreresults", "morepages"})
_TOTAL_KEYS = frozenset({
    "total", "totalcount", "totalresults", "totalitems", "totalrecords", "totalhits", "nbhits", "numfound",
    "totalelements", "recordcount", "resultcount",
})  # fmt: skip
_PAGES_KEYS = frozenset({"totalpages", "pages", "pagecount", "lastpage", "nbpages", "numpages", "maxpage"})
_CURRENT_KEYS = frozenset({"page", "currentpage", "pagenumber", "pageindex"})
_SIZE_KEYS = frozenset({"perpage", "pagesize", "limit", "size", "hitsperpage"})


def pagination_of(
    url: str,
    answer: Any = None,
    *,
    request: Mapping[str, Any] | None = None,
    records: int | None = None,
) -> Pagination | None:
    """How the pages of the API ``url`` go, from its query parameters (or ``request``: a GraphQL call's
    variables, a JSON request body) and its ``answer``, or ``None`` when neither says.

    ``records`` is how many records this page holds (the next offset, when the page size is not given).
    """
    parameters: dict[str, tuple[str, Any]] = {}
    for name, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        parameters.setdefault(_norm(name), (name, value))
    for name, value in (request or {}).items():
        if value is None or isinstance(value, (str, int, float)):
            parameters.setdefault(_norm(name), (str(name), value))
    hints = _answer_hints(answer) if answer is not None else {}

    def find(names: frozenset[str]) -> tuple[str, Any] | None:
        return next((parameters[n] for n in parameters if n in names), None)

    size_param = find(_SIZE_NAMES)
    size = _int(size_param[1]) if size_param else _int(hints.get("size"))
    total, pages, more = _int(hints.get("total")), _int(hints.get("pages")), hints.get("more")
    if pages is None and total is not None and size:
        pages = math.ceil(total / size)
    next_url = hints.get("next_url")
    if isinstance(next_url, str):
        next_url = urljoin(url, next_url)
    page = find(_PAGE_NAMES)
    offset = find(_OFFSET_NAMES)
    cursor = find(_CURSOR_NAMES)
    number = _int(page[1]) if page is not None else None
    if page is not None and number is not None:
        last = (pages is not None and number >= pages) or more is False or (next_url is None and hints.get("no_next"))
        return Pagination("page", page[0], number, size, None if last else number + 1, next_url, total, pages, more)
    start = _int(offset[1]) if offset is not None else None
    if offset is not None and start is not None:
        step = size or records
        following = start + step if step else None
        if following is not None and ((total is not None and following >= total) or more is False):
            following = None
        return Pagination("offset", offset[0], start, size, following, next_url, total, pages, more)
    cursor_next = hints.get("cursor")
    if cursor is not None or (cursor_next is not None and more is not None):
        parameter, token = cursor if cursor is not None else (None, None)
        following_token = cursor_next if more is not False else None
        return Pagination("cursor", parameter, token or None, size, following_token, next_url, total, pages, more)
    if next_url is not None:
        return Pagination("next", None, None, size, None, next_url, total, pages, more)
    current = _int(hints.get("page"))
    if current is not None and (pages is not None or more is not None):
        last = (pages is not None and current >= pages) or more is False
        return Pagination("page", None, current, size, None if last else current + 1, None, total, pages, more)
    if more is False or (total is not None and records and 0 < total <= records):
        return Pagination("page", None, 1, size, None, None, total, 1, more)  # the one page: it holds them all
    return None


def _answer_hints(answer: Any, depth: int = 0, hints: dict[str, Any] | None = None) -> dict[str, Any]:
    """What an API's answer says of its pages, from keys in its objects (not inside its lists of records)."""
    hints = {} if hints is None else hints
    if depth > 4 or not isinstance(answer, Mapping):
        return hints
    keys = {_norm(key) for key in answer}
    for key, value in answer.items():
        name = _norm(key)
        if name in _NEXT_KEYS:
            if isinstance(value, Mapping) and isinstance(value.get("href"), str):  # HAL: _links.next.href
                value = value["href"]
            if isinstance(value, str) and value.startswith(("http://", "https://", "/", "?")):
                hints.setdefault("next_url", value)
            elif isinstance(value, int) and not isinstance(value, bool):
                hints.setdefault("page_next", value)
            elif value is None:
                hints.setdefault("no_next", True)
        elif name in _NEXT_CURSOR_KEYS and isinstance(value, str) and value:
            hints.setdefault("cursor", value)
        elif name in _MORE_KEYS and isinstance(value, bool):
            hints.setdefault("more", value)
        elif (name in _TOTAL_KEYS and _int(value) is not None) or (
            name == "count" and _int(value) is not None and keys & {"next", "previous"}
        ):
            hints.setdefault("total", _int(value))
        elif name in _PAGES_KEYS and _int(value) is not None:
            hints.setdefault("pages", _int(value))
        elif name in _CURRENT_KEYS and _int(value) is not None:
            hints.setdefault("page", _int(value))
        elif name in _SIZE_KEYS and _int(value) is not None:
            hints.setdefault("size", _int(value))
    for value in answer.values():
        if isinstance(value, Mapping):
            _answer_hints(value, depth + 1, hints)
    return hints


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


# --------------------------------------------------------------------------- #
# API calls a page made
# --------------------------------------------------------------------------- #
@dataclass
class ApiCall:
    """An API a page called as it rendered (recorded by a browser fetch with ``capture=True``).

    Calls that differ only in their values (``?page=1``, ``?page=2``) are one :class:`ApiCall`, read from the
    first of them.

    Attributes:
        method, url, status, content_type: The first call's.
        template: Its URL's pattern (``shop.example/api/products?limit&page``).
        calls: How many calls it was.
        graphql: The GraphQL operation (``"query Reviews"``), when it is one.
        collections: The lists of records in its answer, the largest first.
        pagination: How its pages go, when it can be told.
        seen: The values its calls gave the pagination parameter, in order (``[1, 2]``).
        request: The first call's JSON body (a GraphQL call's query and variables), when it has one. It is
            left out of :meth:`to_dict`.
        page: The page that made the first call, when known.
    """

    method: str
    url: str
    template: str
    status: int
    content_type: str
    calls: int = 1
    graphql: str | None = None
    collections: list[Collection] = field(default_factory=list)
    pagination: Pagination | None = None
    seen: list[Any] = field(default_factory=list)
    request: Any = field(default=None, repr=False)
    page: str | None = None

    @property
    def mutation(self) -> bool:
        """A GraphQL mutation: it changes data, and is no source to read from."""
        return bool(self.graphql and self.graphql.startswith("mutation"))

    def head(self) -> str:
        """The call and what it answers: ``GET shop.example/api/products?page: 3 record(s) at items[] (...)``."""
        what = f"{self.method} {self.template}" + (f", {self.graphql}" if self.graphql else "")
        if self.mutation:
            return f"{what}: changes data (a mutation), no source to read"
        if self.collections:
            return f"{what}: {self.collections[0].describe()}"
        return f"{what}: {self.content_type or 'no content type'} {self.status}, no list of records"

    def details(self) -> str:
        """How its pages go, and how many times the page called it (``""`` when there is nothing to say)."""
        parts = [self.pagination.describe()] if self.pagination is not None and not self.mutation else []
        if self.calls > 1:
            seen = f" ({self.pagination.parameter if self.pagination else ''} {', '.join(map(str, self.seen))})"
            parts.append(f"called {self.calls} times" + (seen if self.seen else ""))
        return "; ".join(parts)

    def describe(self) -> str:
        details = self.details()
        return self.head() + (f"; {details}" if details else "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "url": self.url,
            "template": self.template,
            "status": self.status,
            "content_type": self.content_type,
            "calls": self.calls,
            "graphql": self.graphql,
            "collections": [c.to_dict() for c in self.collections],
            "pagination": self.pagination.to_dict() if self.pagination else None,
            "seen": self.seen,
        }


_OPERATION = re.compile(r"^\s*(query|mutation|subscription)\b\s*([A-Za-z_]\w*)?", re.S)


def api_calls(captured: Iterable[Any], *, pages: Iterable[str | None] | None = None) -> list[ApiCall]:
    """The calls a browser recorded (``response.captured``), grouped by method, URL pattern and GraphQL
    operation, in the order they were first made. ``pages``: the page that made each call, in the same order
    (for calls gathered from several pages)."""
    groups: dict[tuple[str, str, str | None], list[Any]] = {}
    origins = iter(pages) if pages is not None else None
    for call in captured:
        request = _request_json(call)
        operation = _graphql_operation(call, request)
        origin = next(origins, None) if origins is not None else None
        groups.setdefault((call.method.upper(), url_template(call.url), operation), []).append((call, request, origin))
    out = []
    for (method, template, operation), calls in groups.items():
        first, request, origin = calls[0]
        variables = _variables(request, operation)
        answer = _answer_json(first)
        collections = json_collections(answer) if answer is not None else []
        pagination = pagination_of(
            first.url, answer, request=variables, records=collections[0].count if collections else None
        )
        seen: list[Any] = []
        if pagination is not None and pagination.parameter and len(calls) > 1:
            for call, call_request, _ in calls:
                value = _parameter(call.url, _variables(call_request, operation), pagination.parameter)
                seen.append(_int(value) if _int(value) is not None else value)
        elif pagination is None and len(calls) > 1:
            pagination, seen = _stepping([(c, r) for c, r, _ in calls], collections[0].count if collections else None)
        out.append(
            ApiCall(
                method=method,
                url=first.url,
                template=template,
                status=first.status,
                content_type=(first.headers.get("content-type") or "").split(";")[0].strip(),
                calls=len(calls),
                graphql=operation,
                collections=collections,
                pagination=pagination,
                seen=seen,
                request=request,
                page=origin,
            )
        )
    return out


def _request_json(call: Any) -> Any:
    body = getattr(call, "request_body", None)
    if not body:
        return None
    try:
        return json.loads(body)
    except (TypeError, ValueError):
        return None


def _answer_json(call: Any) -> Any:
    body = getattr(call, "body", b"") or b""
    ctype = (getattr(call, "headers", {}) or {}).get("content-type", "")
    if "json" not in ctype.lower() and body.lstrip()[:1] not in (b"{", b"["):
        return None
    try:
        return json.loads(body)
    except (TypeError, ValueError):
        return None


def _graphql_operation(call: Any, request: Any) -> str | None:
    """``"query Reviews"``, ``"mutation AddToCart"``..., when the call is a GraphQL one."""
    operations = request if isinstance(request, list) else [request]
    names = []
    for operation in operations:
        if isinstance(operation, Mapping) and ("query" in operation or "operationName" in operation):
            names.append(_operation_name(operation.get("query"), operation.get("operationName")))
    if not names:
        query = dict(parse_qsl(urlsplit(call.url).query))
        if ("query" in query and "{" in query["query"]) or "operationName" in query:
            names.append(_operation_name(query.get("query"), query.get("operationName")))
    return ", ".join(names) if names else None


def _operation_name(query: Any, name: Any) -> str:
    kind, found = "query", None
    match = _OPERATION.match(query) if isinstance(query, str) else None
    if match:
        kind, found = match.group(1), match.group(2)
    name = name if isinstance(name, str) and name else found
    return f"{kind} {name}" if name else kind


def _variables(request: Any, operation: str | None) -> Mapping[str, Any] | None:
    """What says which page a call asks for: a GraphQL call's variables, or a JSON request body."""
    if isinstance(request, list):
        request = request[0] if request else None
    if not isinstance(request, Mapping):
        return None
    if operation is not None:
        variables = request.get("variables")
        if isinstance(variables, str):
            try:
                variables = json.loads(variables)
            except ValueError:
                return None
        return variables if isinstance(variables, Mapping) else None
    return request


def _parameter(url: str, variables: Mapping[str, Any] | None, name: str) -> Any:
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if key == name:
            return value
    return (variables or {}).get(name)


def _stepping(calls: list[tuple[Any, Any]], records: int | None) -> tuple[Pagination | None, list[Any]]:
    """Calls whose only difference is a number stepping up: pages (by one) or offsets (by a page's size). Like
    every :class:`ApiCall`'s, the pagination is the first call's (its next page follows it)."""
    rows = [dict(parse_qsl(urlsplit(call.url).query)) for call, _ in calls]
    for name in rows[0]:
        numbers = [_int(row.get(name)) for row in rows]
        values = [n for n in numbers if n is not None]
        if len(values) < len(numbers) or len(set(values)) < len(values):
            continue
        steps = {b - a for a, b in pairwise(values)}
        if len(steps) != 1:
            continue
        step = steps.pop()
        if step == 1:
            return Pagination("page", name, values[0], next=values[0] + 1), values
        if step > 1 and (records is None or step >= records):
            return Pagination("offset", name, values[0], size=step, next=values[0] + step), values
    return None, []


# --------------------------------------------------------------------------- #
# a page's sources
# --------------------------------------------------------------------------- #
@dataclass
class HtmlRecords:
    """Records drawn in a page's HTML: repeated elements (cards, rows) with the same fields."""

    selector: str
    count: int
    fields: list[str]

    def describe(self) -> str:
        return f"{self.count} records ({self.selector}): {', '.join(self.fields) or 'no fields named'}"

    def to_dict(self) -> dict[str, Any]:
        return {"selector": self.selector, "count": self.count, "fields": self.fields}


class Source(NamedTuple):
    """One place holding records (see :meth:`DataSources.richest`)."""

    kind: str  # "html", "table", "json-ld", "embedded", "api", "json"
    where: str
    records: int
    fields: int


@dataclass
class DataSources:
    """Where a page's data is (see the module docs).

    Attributes:
        url: The page.
        html: Its repeated records in the HTML, the most convincing first.
        tables: Its ``<table>`` elements: rows and column names.
        json_ld: JSON-LD objects by ``@type``, and how many.
        microdata: Microdata items by type, and how many.
        rdfa: RDFa items by type, and how many.
        meta: The OpenGraph, Twitter and meta fields it declares.
        embedded: The JSON an app embeds, by name, with the records in each.
        api: The API calls it made as it rendered (a browser fetch with ``capture=True``).
        recorded: Whether its calls were recorded; if not, ``api`` is empty whatever the page calls.
        endpoints: The API endpoints its scripts name, ``(method, endpoint)``: named, not requested.
        document: For a JSON answer (an API itself): the records in it.
        pagination: For a JSON answer: how its pages go.
    """

    url: str
    html: list[HtmlRecords] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    json_ld: dict[str, int] = field(default_factory=dict)
    microdata: dict[str, int] = field(default_factory=dict)
    rdfa: dict[str, int] = field(default_factory=dict)
    meta: list[str] = field(default_factory=list)
    embedded: dict[str, list[Collection]] = field(default_factory=dict)
    api: list[ApiCall] = field(default_factory=list)
    recorded: bool = False
    endpoints: list[tuple[str | None, str]] = field(default_factory=list)
    document: list[Collection] | None = None
    pagination: Pagination | None = None
    _json_ld_collections: list[tuple[str, Collection]] = field(default_factory=list, repr=False)

    def sources(self) -> list[Source]:
        """Every place holding at least two records, those holding the most values (records x fields) first."""
        out = [Source("html", group.selector, group.count, len(group.fields)) for group in self.html]
        out += [
            Source("table", f"table {i}", table["rows"], len(table["columns"]))
            for i, table in enumerate(self.tables, 1)
            if table["rows"] >= 2
        ]
        out += [Source("json-ld", where, c.count, len(c.fields)) for where, c in self._json_ld_collections]
        out += [
            Source("embedded", f"{name} {c.path}", c.count, len(c.fields))
            for name, cs in self.embedded.items()
            for c in cs
        ]
        out += [
            Source("api", f"{call.method} {call.template} {c.path}", c.count, len(c.fields))
            for call in self.api
            if not call.mutation
            for c in call.collections
        ]
        out += [Source("json", c.path, c.count, len(c.fields)) for c in self.document or []]
        return sorted((s for s in out if s.records >= 2), key=lambda s: (-s.records * s.fields, -s.records))

    def richest(self) -> Source | None:
        """The place holding the most values (records x fields): often the one to read. ``None`` if no place holds
        a list of records."""
        found = self.sources()
        return found[0] if found else None

    def describe(self) -> str:
        lines = [self.url]
        if self.document is not None:
            lines.append(_line("JSON", "; ".join(c.describe() for c in self.document[:3]) or "no list of records"))
            if self.pagination is not None:
                lines.append(_line("", self.pagination.describe()))
        else:
            lines.append(_line("HTML", self.html[0].describe() if self.html else "no repeated records"))
            if len(self.html) > 1:
                lines += [_line("", group.describe()) for group in self.html[1:3]]
            tables = [f"{t['rows']} rows ({', '.join(t['columns'][:5])})" for t in self.tables]
            lines.append(_line("tables", "; ".join(tables[:3]) or "none"))
            lines.append(_line("JSON-LD", _counts(self.json_ld)))
            lines.append(_line("microdata", _counts(self.microdata)))
            if self.rdfa:
                lines.append(_line("RDFa", _counts(self.rdfa)))
            lines.append(_line("meta", _names(self.meta)))
            embedded = [
                f"{name}: " + (collections[0].describe() if collections else "no list of records")
                for name, collections in self.embedded.items()
            ]
            lines.append(_line("embedded JSON", embedded[0] if embedded else "none"))
            lines += [_line("", text) for text in embedded[1:4]]
            if self.recorded:
                lines.append(
                    _line(
                        "API calls",
                        f"{len(self.api)} recorded as the page rendered"
                        if self.api
                        else "none recorded as the page rendered",
                    )
                )
                for call in self.api[:8]:
                    lines.append(_line("", call.head()))
                    if call.details():
                        lines.append(_line("", "  " + call.details()))
            else:
                lines.append(_line("API calls", "not recorded (a browser fetch with capture=True records them)"))
                if self.endpoints:
                    named = [f"{method} {endpoint}" if method else endpoint for method, endpoint in self.endpoints[:6]]
                    lines.append(_line("", "named in its scripts: " + ", ".join(named)))
        richest = self.richest()
        if richest is not None:
            lines.append(
                _line(
                    "richest",
                    f"{_KIND_NAMES[richest.kind]} {richest.where}: {richest.records} records, {richest.fields} fields",
                )
            )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        richest = self.richest()
        out: dict[str, Any] = {"url": self.url}
        if self.document is not None:
            out["json"] = [c.to_dict() for c in self.document]
            out["pagination"] = self.pagination.to_dict() if self.pagination else None
        else:
            out.update(
                {
                    "html": [group.to_dict() for group in self.html],
                    "tables": self.tables,
                    "json_ld": self.json_ld,
                    "microdata": self.microdata,
                    "rdfa": self.rdfa,
                    "meta": self.meta,
                    "embedded": {name: [c.to_dict() for c in cs] for name, cs in self.embedded.items()},
                    "api": [call.to_dict() for call in self.api] if self.recorded else None,
                    "endpoints": [{"method": m, "url": e} for m, e in self.endpoints],
                }
            )
        out["richest"] = richest._asdict() if richest else None
        return out


_KIND_NAMES = {
    "html": "HTML",
    "table": "table",
    "json-ld": "JSON-LD",
    "embedded": "embedded JSON",
    "api": "API",
    "json": "JSON",
}


def _line(label: str, text: str) -> str:
    return f"  {label:<14} {text}"


def _counts(counts: Mapping[str, int]) -> str:
    return ", ".join(f"{name} x{n}" if n > 1 else name for name, n in counts.items()) or "none"


def _names(names: list[str], shown: int = 6) -> str:
    if not names:
        return "none"
    return ", ".join(names[:shown]) + (f" +{len(names) - shown}" if len(names) > shown else "")


def data_sources(response: Any, *, recorded: bool | None = None) -> DataSources:
    """Where ``response``'s data is (see the module docs).

    ``recorded`` says whether the page's API calls were recorded (a browser fetch with ``capture=True``); by
    default, whether any was.
    """
    url = str(response.url)
    ctype = (response.headers.get("content-type") or "").lower() if response.headers else ""
    if "json" in ctype or (not ctype and response.body.lstrip()[:1] in (b"{", b"[")):
        try:
            answer = response.json()
        except ValueError:
            answer = None
        if answer is not None:
            document = json_collections(answer)
            pagination = pagination_of(url, answer, records=document[0].count if document else None)
            return DataSources(url, document=document, pagination=pagination)
    from .profile import script_endpoints

    structured = response.structured_data()
    json_ld: Counter[str] = Counter()
    json_ld_collections: list[tuple[str, Collection]] = []
    by_type: dict[str, list[Mapping[str, Any]]] = {}
    for item in structured.get("json_ld") or []:
        kind = _type_name(item.get("@type"))
        json_ld[kind] += 1
        by_type.setdefault(kind, []).append(item)
        for collection in json_collections(item):
            json_ld_collections.append((f"{kind} {collection.path}", collection))
    for kind, items in by_type.items():
        if len(items) >= 2:
            json_ld_collections.append((kind, _collection("[]", items)))
    microdata = Counter(_type_name((item.get("@type") or "").split()) for item in structured.get("microdata") or [])
    rdfa = Counter(_type_name(item.get("@type")) for item in structured.get("rdfa") or [])  # (several: a list)
    meta = [f"og:{k}" for k in structured.get("opengraph") or {}]
    meta += [f"twitter:{k}" for k in structured.get("twitter") or {}]
    meta += [str(k) for k in structured.get("meta") or {}]
    html = []
    for group in response.detect_records()[:3]:
        html.append(HtmlRecords(group.container_selector, len(group.elements), list(group.fields)))
    tables = [
        {"rows": len(table.get("rows") or []), "columns": [str(c) for c in table.get("headers") or []]}
        for table in response.tables()
    ]
    embedded = {name: json_collections(value) for name, value in response.embedded_json().items()}
    captured = list(getattr(response, "captured", None) or [])
    calls = api_calls(captured)
    return DataSources(
        url,
        html=html,
        tables=tables,
        json_ld=dict(json_ld),
        microdata=dict(microdata),
        rdfa=dict(rdfa),
        meta=meta,
        embedded=embedded,
        api=calls,
        recorded=bool(captured) if recorded is None else recorded,
        endpoints=script_endpoints(response.selector, url),
        _json_ld_collections=json_ld_collections,
    )


def _type_name(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else None
    if not value:
        return "(no type)"
    text = str(value)
    return text.rsplit("/", 1)[-1] if text.startswith("http") else text
