"""Search results: collected from search APIs that permit it, and read for rankings, competitors and gaps.

::

    answer = search("budget laptop", provider="brave")    # BRAVE_SEARCH_API_KEY from the environment
    answer.results[0]      # SearchResult(query='budget laptop', position=1, url=..., domain=..., title=..., ...)
    answer.modules         # the page's other results and boxes: news, videos, local results, questions...
    answer.related         # related searches, where the provider gives them (SearXNG)
    answer.questions       # questions people ask, where it gives them (Brave)

    competitors(results, domain="shop.example")     # the domains ranking for the same queries, most visible first
    gaps(results, domain="shop.example")            # queries others rank for and the domain does not
    cluster_queries(results)                        # queries whose results share pages: one topic
    overlap(results_a, results_b)                   # how alike two lists of results are
    modules(results, domain="shop.example")         # the boxes the pages have, and whose results are in them
    ranking_changes(before, after, domain="shop.example")
    ranking_history(results, domain="shop.example") # its position in each collection, oldest first

Only search APIs are asked, with your own access: Brave's Search API (``BRAVE_SEARCH_API_KEY``), Google's
Programmable Search JSON API (``GOOGLE_API_KEY`` and ``GOOGLE_CSE_ID``), or a SearXNG instance of your own
(``SEARXNG_URL``, with its JSON format enabled). Search engines' own result pages are not fetched: their terms
forbid it. Each provider's terms and quotas are yours to keep: requests go one at a time, a second apart by
default, a 429 is waited out as the answer asks (once), and a refusal is reported, not asked another way.

An answer is read without a fixed schema: its results are the list whose records have a URL and a title
(``web.results[]``, ``items[]``, ``results[]``...), their snippet the first of ``description``, ``snippet``,
``content``... The page's other results and boxes (its modules) are read where the provider keeps them: Brave's
news, videos, local results, discussions, questions and infobox, SearXNG's infoboxes, answers, corrections and
suggestions (and its results' categories), Google's promotions and spelling correction. Where the provider gives
their order on the page (Brave's ``mixed``, one list's order), each result has its place on it (``rank``).

The analyses read any records with a query, a position and a URL, however collected (a rank tracker's export,
a crawl of a site's own search pages): ``read_records`` gives them. They read the web results of each query's
latest collection (the results of one search share their ``fetched`` time); :func:`ranking_history` reads them
all. A query asked with other parameters (``params={"country": "de"}``) is another search to them.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from ..errors import ConfigurationError, WintergrabError, describe
from ..fetchers.resources import registrable_domain
from .sources import json_collections

__all__ = [
    "PROVIDERS",
    "Module",
    "Provider",
    "SearchAnswer",
    "SearchResult",
    "Visibility",
    "cluster_queries",
    "competitors",
    "gaps",
    "modules",
    "overlap",
    "ranking_changes",
    "ranking_history",
    "read_results",
    "search",
    "visibility_score",
]


@dataclass(frozen=True)
class Provider:
    """How to ask a search API: its endpoint, the key, how its pages go, and where its answers hold what.

    Attributes:
        endpoint: The API's URL (``None``: from ``endpoint_variable``, for an instance of your own).
        key_variable: The environment variable holding the key (``None``: no key).
        key_header: The header the key goes in (else ``key_param``, a query parameter).
        variables: More query parameters from the environment (``{"cx": "GOOGLE_CSE_ID"}``).
        size_param, size: The parameter for results a page, and how many are asked for.
        page_param: The parameter saying which page; ``page_by``: ``"index"`` (0, 1, 2...), ``"number"``
            (1, 2, 3...) or ``"first"`` (the first result's number: 1, 11, 21...).
        results_path: Where its results are, when its answer is the one expected.
        type_field: A result's field saying what kind of result it is (SearXNG's ``category``: ``general`` for
            a web result, ``news``, ``videos``, ``map``...); without one, the results are web results.
        modules: Where its answer keeps the page's other results and boxes, by type (``{"news":
            "news.results[]"}``): a list (``[]``), or one value (``"query.altered"``).
        order_path: Where its answer gives the order of the results and boxes on the page (Brave's ``mixed``).
        limit: How far the page parameter and the page's size may reach together (Google's ``start`` + ``num``:
            100, beyond which it answers an error): the last page asks for fewer.
    """

    name: str
    endpoint: str | None = None
    endpoint_variable: str | None = None
    key_variable: str | None = None
    key_header: str | None = None
    key_param: str | None = None
    params: Mapping[str, str] = field(default_factory=dict)
    variables: Mapping[str, str] = field(default_factory=dict)
    query_param: str = "q"
    size_param: str | None = None
    size: int = 10
    page_param: str = "page"
    page_by: str = "number"
    results_path: str | None = None
    type_field: str | None = None
    modules: Mapping[str, str] = field(default_factory=dict)
    order_path: str | None = None
    limit: int | None = None

    def page_value(self, index: int) -> int:
        """The page parameter's value for page ``index`` (0 first)."""
        if self.page_by == "index":
            return index
        if self.page_by == "first":
            return 1 + index * self.size
        return index + 1


#: The search APIs asked by name (``search(..., provider="brave")``).
PROVIDERS: dict[str, Provider] = {
    "brave": Provider(
        "brave", endpoint="https://api.search.brave.com/res/v1/web/search", key_variable="BRAVE_SEARCH_API_KEY",
        key_header="X-Subscription-Token", size_param="count", size=20, page_param="offset", page_by="index",
        results_path="web.results[]", order_path="mixed",
        modules={"news": "news.results[]", "video": "videos.results[]", "location": "locations.results[]",
                 "discussion": "discussions.results[]", "question": "faq.results[]", "infobox": "infobox.results[]",
                 "correction": "query.altered"},
    ),
    "google": Provider(
        "google", endpoint="https://www.googleapis.com/customsearch/v1", key_variable="GOOGLE_API_KEY",
        key_param="key", variables={"cx": "GOOGLE_CSE_ID"}, size_param="num", size=10, page_param="start",
        page_by="first", results_path="items[]", limit=100,
        modules={"promotion": "promotions[]", "correction": "spelling.correctedQuery"},
    ),
    "searxng": Provider(
        "searxng", endpoint_variable="SEARXNG_URL", params={"format": "json"}, page_param="pageno",
        page_by="number", results_path="results[]", type_field="category",
        modules={"infobox": "infoboxes[]", "answer": "answers[]", "correction": "corrections[]",
                 "related": "suggestions[]"},
    ),
}  # fmt: skip
_URL_NAMES = ("url", "link", "href")
_TITLE_NAMES = ("title", "name")
_SNIPPET_NAMES = ("description", "snippet", "content", "summary", "text", "body")
_DATE_NAMES = ("page_age", "publishedDate", "published", "date", "age")
_MAX_PAGES = 10
#: Lists in an answer that are not its results (questions, boxes, suggestions).
_NOT_RESULTS = ("faq", "infobox", "answers", "suggestions", "related", "corrections", "queries", "context")
#: The kinds of result as providers and rank trackers name them (Brave's types, SearXNG's categories...).
_TYPE_NAMES = {
    "general": "web", "search_result": "web", "organic": "web", "videos": "video", "video_result": "video",
    "locations": "location", "location_result": "location", "map": "location", "local": "location",
    "local_pack": "location", "discussions": "discussion", "faq": "question", "people_also_ask": "question",
    "images": "image", "files": "file", "related_searches": "related", "top_stories": "news",
}  # fmt: skip
#: What each kind of result adds, and where its record holds it (the first found).
_DETAILS: dict[str, dict[str, tuple[str, ...]]] = {
    "location": {
        "address": ("postal_address.displayAddress", "postal_address", "address"),
        "telephone": ("contact.telephone", "telephone", "phone"),
        "rating": ("rating.ratingValue",), "reviews": ("rating.reviewCount",),
        "latitude": ("coordinates.0", "latitude"), "longitude": ("coordinates.1", "longitude"),
        "price_range": ("price_range",),
    },
    "video": {
        "duration": ("video.duration", "duration", "length"), "creator": ("video.creator", "author"),
        "publisher": ("video.publisher",), "views": ("video.views",),
    },
    "discussion": {"forum": ("data.forum_name",), "answers": ("data.num_answers",)},
    "news": {"publisher": ("source",), "breaking": ("breaking",)},
}  # fmt: skip
_DETAIL_NAMES = tuple(dict.fromkeys(name for kind in _DETAILS.values() for name in kind))
_ADDRESS_PARTS = ("streetAddress", "road", "house_number", "addressLocality", "locality", "city", "addressRegion",
                  "postalCode", "postcode", "addressCountry", "country")  # fmt: skip


@dataclass
class SearchResult:
    """One result of a search: its query, its position (1 first, across pages), and what it links to.

    A web result, or one of the page's other results and boxes (see :attr:`type`), whose position counts the
    results of its type.
    """

    query: str
    position: int
    url: str = ""
    title: str = ""
    snippet: str = ""
    domain: str = ""
    date: str | None = None
    source: str = ""
    fetched: str = ""
    #: ``"web"``; ``"news"``, ``"video"``, ``"location"`` (a local result), ``"discussion"`` (a forum thread),
    #: ``"question"`` (a question people ask: its answer the snippet), ``"infobox"``, ``"answer"`` (a direct
    #: answer), ``"promotion"``, ``"related"`` (a related search: its title), ``"correction"`` (the query as
    #: the provider corrected it); or a SearXNG category (``"image"``, ``"science"``...).
    type: str = "web"
    #: Its place among the page's results and boxes (1 first), where the provider gives their order: a box
    #: shown whole (a row of news) is one place.
    rank: int | None = None
    #: What its type adds, where given: a location's ``address``, ``telephone``, ``rating``, ``reviews``,
    #: ``latitude``, ``longitude``, ``price_range``; a video's ``duration``, ``creator``, ``publisher``,
    #: ``views``; a discussion's ``forum`` and ``answers``; a news result's ``publisher`` and ``breaking``.
    details: dict[str, Any] = field(default_factory=dict)
    #: The API's parameters its search was asked with besides the query (``{"country": "de"}``).
    params: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.domain and self.url:
            self.domain = registrable_domain(urlsplit(self.url).hostname or "")

    @property
    def searched(self) -> str:
        """What was searched: the query, with the parameters it was asked with (``budget laptop [country=de]``).
        The analyses tell searches apart by it."""
        return _searched(self.query, self.params)

    def to_dict(self) -> dict[str, Any]:
        """The result as a record: its fields, then its details (the empty ones left out)."""
        record = {
            "query": self.query, "type": self.type, "position": self.position, "rank": self.rank, "url": self.url,
            "domain": self.domain, "title": self.title, "snippet": self.snippet, "date": self.date,
        }  # fmt: skip
        record.update({k: v for k, v in self.details.items() if k not in record})
        record.update(params=dict(self.params) or None, source=self.source, fetched=self.fetched)
        return {k: v for k, v in record.items() if v is not None and v != ""}


@dataclass
class SearchAnswer:
    """What a search gave: its web results, the page's other results and boxes, and what else was said."""

    query: str
    provider: str
    #: Its web results, the first first.
    results: list[SearchResult] = field(default_factory=list)
    #: The page's other results and boxes where the provider gives them, each with its :attr:`~SearchResult.type`
    #: (news, videos, local results, questions people ask, related searches, the query as corrected...).
    modules: list[SearchResult] = field(default_factory=list)
    #: Related searches (SearXNG's suggestions).
    related: list[str] = field(default_factory=list)
    #: Questions people ask about it, with an answer when given (Brave's FAQ).
    questions: list[dict[str, str]] = field(default_factory=list)
    #: How many results the provider says it has, when it says.
    total: int | None = None
    #: Why fewer pages were read than asked for, when that happened.
    notes: list[str] = field(default_factory=list)
    #: The API's parameters it was asked with besides the query.
    params: dict[str, str] = field(default_factory=dict)

    @property
    def searched(self) -> str:
        """The query, with the parameters it was asked with (see :attr:`SearchResult.searched`)."""
        return _searched(self.query, self.params)

    def records(self) -> list[dict[str, Any]]:
        """Its results and modules as records (:meth:`SearchResult.to_dict`), the web results first."""
        return [r.to_dict() for r in [*self.results, *self.modules]]


def search(
    query: str,
    *,
    provider: str | Provider = "brave",
    pages: int = 1,
    endpoint: str | None = None,
    key: str | None = None,
    params: Mapping[str, Any] | None = None,
    delay: float = 1.0,
    timeout: float = 20.0,
    network_policy: Any = None,
    fetcher: Any = None,
) -> SearchAnswer:
    """Ask a search API for ``query`` (see the module docs): ``pages`` pages of it, ``delay`` seconds apart. A
    page after the first that fails ends the search with a note, the pages before it kept.

    Args:
        provider: ``"brave"``, ``"google"``, ``"searxng"`` or a :class:`Provider`.
        endpoint: The API's URL, instead of the provider's (a SearXNG instance: ``https://searx.example``).
        key: The key, instead of the environment's.
        params: More of the API's own parameters: where and in what language to search (Brave's ``country`` and
            ``search_lang``, Google's ``gl`` and ``hl``, SearXNG's ``language``), what to search
            (SearXNG's ``categories``)... Those wintergrab sets (the query, its pages, the key) are refused.
            The results keep them, and the analyses tell searches with different ones apart
            (:attr:`SearchResult.searched`).
        network_policy: Where requests may go (see :class:`~wintergrab.NetworkPolicy`).
        fetcher: A :class:`~wintergrab.Fetcher` to ask with (by default one of its own).
    """
    spec = PROVIDERS.get(provider) if isinstance(provider, str) else provider
    if spec is None:
        raise ConfigurationError(f"no search provider {provider!r} (known: {', '.join(PROVIDERS)})")
    extra = {str(k): str(v) for k, v in (params or {}).items()}
    own_names = {spec.query_param, spec.page_param, spec.size_param, spec.key_param, *spec.variables, *spec.params}
    taken = sorted(set(extra) & own_names)
    if taken:
        raise ConfigurationError(f"{spec.name}: {', '.join(taken)}: set by wintergrab (the query, its pages, the key)")
    url = endpoint or spec.endpoint or (os.environ.get(spec.endpoint_variable) if spec.endpoint_variable else None)
    if not url:
        raise ConfigurationError(f"{spec.name}: say where it is: set {spec.endpoint_variable} (or give endpoint=)")
    if spec.name == "searxng" and not urlsplit(url).path.rstrip("/").endswith("/search"):
        url = url.rstrip("/") + "/search"
    if spec.key_variable:
        key = key or os.environ.get(spec.key_variable)
        if not key:
            raise ConfigurationError(f"{spec.name}: set {spec.key_variable} to your API key")
    asked: dict[str, Any] = {spec.query_param: query, **spec.params, **extra}
    for name, variable in spec.variables.items():
        value = os.environ.get(variable)
        if not value:
            raise ConfigurationError(f"{spec.name}: set {variable}")
        asked[name] = value
    if spec.size_param:
        asked[spec.size_param] = spec.size
    headers = {"Accept": "application/json"}
    if key and spec.key_header:
        headers[spec.key_header] = key
    elif key and spec.key_param:
        asked[spec.key_param] = key
    own = fetcher is None
    if own:
        from ..fetchers import Fetcher

        fetcher = Fetcher(timeout=timeout, retries=1, network_policy=network_policy)
    answer = SearchAnswer(query=query, provider=spec.name, params=extra)
    fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
    counts: Counter[str] = Counter()  # results so far, by type
    placed = 0  # places on the page given so far
    try:
        for index in range(max(1, min(pages, _MAX_PAGES))):
            if index:
                time.sleep(delay)
            asked[spec.page_param] = spec.page_value(index)
            if spec.limit is not None and spec.size_param:  # (Google's: start + num at most 100)
                room = spec.limit - spec.page_value(index)
                if room < 1:
                    answer.notes.append(f"{spec.name} gives no results past its first {spec.limit}")
                    break
                asked[spec.size_param] = min(spec.size, room)
            try:
                data = _ask(fetcher, url, asked, headers, spec, key)
            except _RateLimited:
                answer.notes.append(f"the provider's rate limit (HTTP 429) stopped the search at page {index + 1}")
                break
            except WintergrabError as exc:
                if not index:
                    raise
                answer.notes.append(f"page {index + 1} failed, and the search stopped there: {exc}")
                break
            listed, boxes, placed = _read_page(data, query, spec, fetched, counts, placed, extra, first=index == 0)
            answer.results.extend(r for r in listed if r.type == "web")
            answer.modules.extend([*(r for r in listed if r.type != "web"), *boxes])
            if index == 0:
                answer.total = _total(data)
            if not listed:
                break
    finally:
        if own:
            fetcher.close()
    answer.related = [m.title for m in answer.modules if m.type == "related"]
    answer.questions = [
        {k: v for k, v in (("question", m.title), ("answer", m.snippet), ("url", m.url)) if v}
        for m in answer.modules
        if m.type == "question"
    ]
    return answer


class _RateLimited(Exception):
    """The API's rate limit said no (HTTP 429), after the fetcher waited it out once."""


def _ask(
    fetcher: Any, url: str, params: dict[str, Any], headers: dict[str, str], spec: Provider, key: str | None
) -> Any:
    """One page of the API's answer, as JSON: a refusal, a failure or an answer that is not JSON raised, and
    no key in what is raised."""
    try:
        response = fetcher.get(url, params=params, headers=headers)
    except WintergrabError as exc:
        if key and (key in str(exc) or key in repr(getattr(exc, "context", ""))):
            # (a key in the URL: Google's) the error as text, the key left out
            raise WintergrabError(f"{spec.name}: {_without(describe(exc), key)}") from None
        raise
    if response.status in (401, 403):
        check = f"check the key in {spec.key_variable}" if spec.key_variable else "check the instance"
        if spec.name == "searxng":
            check += " answers JSON (its settings: search: formats: [json])"
        raise ConfigurationError(f"{spec.name} refused the request (HTTP {response.status}): {check}")
    if response.status == 429:
        raise _RateLimited
    if not 200 <= response.status < 300:
        raise WintergrabError(f"{spec.name} answered HTTP {response.status}")
    try:
        return response.json()
    except ValueError:
        raise WintergrabError(f"{spec.name} answered something that is not JSON") from None


def _without(text: str, key: str | None) -> str:
    return text.replace(key, "***") if key else text


def _first(record: Mapping[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        value = record.get(name)
        if value not in (None, "", [], {}):
            return value
    return None


def _value(record: Any, path: str) -> Any:
    """The value at a dotted path of a record (``rating.ratingValue``, ``coordinates.0``), or ``None``."""
    node = record
    for part in path.split("."):
        if isinstance(node, Mapping):
            node = node.get(part)
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


def _known_list(data: Any, path: str | None) -> list[Any] | None:
    """The list at a simple path (``web.results[]``) of an answer, when the answer has it (empty or not)."""
    if not path or not path.endswith("[]"):
        return None
    node = _value(data, path[:-2]) if path[:-2] else data
    return node if isinstance(node, list) else None


def _type(name: str) -> str:
    kind = name.strip().lower()
    return _TYPE_NAMES.get(kind, kind) or "web"


def _text(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _main_list(data: Any, spec: Provider) -> list[Mapping[str, Any]]:
    """The records of an answer's list of results: the list at its known path when the answer has it (an empty
    one is the end), else the list whose records have URLs and titles (not questions, boxes...)."""
    known = _known_list(data, spec.results_path)
    if known is not None:
        candidates = [[r for r in known if isinstance(r, Mapping)]]
    else:
        elsewhere = _NOT_RESULTS + tuple(path.split(".")[0].removesuffix("[]") for path in spec.modules.values())
        candidates = [
            list(c.records) for c in json_collections(data, min_records=1) if not c.path.startswith(elsewhere)
        ]
    for listed in candidates:
        records = [r for r in listed if isinstance(_first(r, _URL_NAMES), str)]
        linked = [r for r in records if str(_first(r, _URL_NAMES)).startswith(("http://", "https://"))]
        if linked and any(_first(r, _TITLE_NAMES) for r in linked):
            return linked
    return []


def _read_page(
    data: Any,
    query: str,
    spec: Provider,
    fetched: str,
    counts: Counter[str],
    placed: int,
    params: dict[str, str],
    *,
    first: bool,
) -> tuple[list[SearchResult], list[SearchResult], int]:
    """One answer's list of results (web results, and SearXNG's of other categories) and, on the first page,
    its boxes; numbered on from ``counts`` (the results so far, by type) and ``placed`` (the places given)."""

    def result(kind: str, record: Any) -> SearchResult | None:
        if isinstance(record, str):  # a related search, a correction, an answer: its text
            url, title, snippet, date, details = "", _text(record), "", None, {}
        elif isinstance(record, Mapping):
            link = _first(record, _URL_NAMES)
            if not (isinstance(link, str) and link.startswith(("http://", "https://"))):
                ident = record.get("id")  # (SearXNG's infobox: its id is its URL)
                link = ident if isinstance(ident, str) and ident.startswith(("http://", "https://")) else ""
            titles = ("question",) if kind == "question" else ("answer", "title") if kind == "answer" else None
            snippets = ("answer",) if kind == "question" else () if kind == "answer" else None
            url, title = link, _text(_first(record, titles or (*_TITLE_NAMES, "infobox")))
            snippet = _text(_first(record, snippets if snippets is not None else (*_SNIPPET_NAMES, "long_desc")))
            found = _first(record, _DATE_NAMES)
            date, details = (str(found) if found is not None else None), _details(record, kind)
        else:
            return None
        if not url and not title:
            return None
        counts[kind] += 1
        return SearchResult(
            query=query, position=counts[kind], url=url, title=title, snippet=snippet, date=date, source=spec.name,
            fetched=fetched, type=kind, details=details, params=dict(params),
        )  # fmt: skip

    listed = []
    for record in _main_list(data, spec):
        kind = _type(str(record.get(spec.type_field) or "general")) if spec.type_field else "web"
        found = result(kind, record)
        if found is not None:
            listed.append(found)
    boxes = []
    for kind, path in spec.modules.items() if first else ():
        items = _known_list(data, path) if path.endswith("[]") else [_value(data, path)]
        for item in items or ():
            found = result(kind, item)
            if found is not None:
                boxes.append(found)
    order = _value(data, spec.order_path) if spec.order_path else None
    if isinstance(order, Mapping):
        placed = _place(order, [*listed, *boxes], placed)
    else:
        for found in listed:
            placed += 1
            found.rank = placed
    return listed, boxes, placed


def _details(record: Mapping[str, Any], kind: str) -> dict[str, Any]:
    """What a result of this kind adds (a location's address, a video's duration...), where its record says."""
    out: dict[str, Any] = {}
    for name, paths in _DETAILS.get(kind, {}).items():
        for path in paths:
            value = _value(record, path)
            if name == "address" and isinstance(value, Mapping):
                value = ", ".join(str(value[k]).strip() for k in _ADDRESS_PARTS if isinstance(value.get(k), (str, int)))
            if name == "breaking":
                value = True if value is True else None  # (said when it is)
            elif isinstance(value, bool) or not isinstance(value, (str, int, float)):
                continue
            if value not in (None, ""):
                out[name] = _text(value) if isinstance(value, str) else value
                break
    return out


def _place(order: Mapping[str, Any], results: list[SearchResult], placed: int) -> int:
    """Give results their places on the page as the answer orders them (Brave's ``mixed``: ``top``, then
    ``main``, each a list of ``{"type", "index", "all"}``, the index into that type's results; ``side``, beside
    them, has none). A box shown whole (``all``) is one place."""
    by_type: dict[str, list[SearchResult]] = defaultdict(list)
    for result in results:
        by_type[result.type].append(result)
    refs = [ref for part in ("top", "main") if isinstance(order.get(part), list) for ref in order[part]]
    for ref in refs:
        if not isinstance(ref, Mapping) or not ref.get("type"):
            continue
        items = by_type.get(_type(str(ref["type"])), [])
        index = ref.get("index")
        if not ref.get("all"):
            items = [items[index]] if isinstance(index, int) and 0 <= index < len(items) else []
        chosen = [r for r in items if r.rank is None]
        if chosen:
            placed += 1
            for result in chosen:
                result.rank = placed
    return placed


def _total(data: Any) -> int | None:
    if not isinstance(data, Mapping):
        return None
    info = data.get("searchInformation")
    value = info.get("totalResults") if isinstance(info, Mapping) else data.get("number_of_results")
    try:
        return int(value) if value not in (None, "") and int(value) > 0 else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# reading collected results
# --------------------------------------------------------------------------- #
def read_results(records: Iterable[Mapping[str, Any]]) -> list[SearchResult]:
    """Records with a query, a position and a URL (a file of collected results, a rank tracker's export) as
    :class:`SearchResult` objects, with their ``type`` (a web result when not said) and details; a box's may
    have a title instead of a URL (a related search). The others are left out."""
    out = []
    for record in records:
        try:
            position = int(record.get("position") or record.get("rank") or 0)
        except (TypeError, ValueError):
            continue
        try:  # (a rank tracker's rank is the position; beside a position, it is the place on the page)
            rank = int(record["rank"]) if record.get("position") and record.get("rank") not in (None, "") else None
        except (TypeError, ValueError):
            rank = None
        kind = _type(str(record.get("type") or "web"))
        url = record.get("url") or record.get("link")
        url = url if isinstance(url, str) else ""
        query = record.get("query") or record.get("keyword")
        title = _text(record.get("title"))
        if position < 1 or not query or not (url if kind == "web" else url or title):
            continue
        params = record.get("params")
        if isinstance(params, str) and params.startswith("{"):  # (as CSV keeps it)
            try:
                params = json.loads(params)
            except ValueError:
                params = None
        out.append(
            SearchResult(
                query=str(query), position=position, url=url, title=title, snippet=_text(record.get("snippet")),
                domain=str(record.get("domain") or ""), date=record.get("date"), source=str(record.get("source") or ""),
                fetched=str(record.get("fetched") or ""), type=kind, rank=rank,
                details={k: record[k] for k in _DETAIL_NAMES if record.get(k) not in (None, "")},
                params={str(k): str(v) for k, v in params.items()} if isinstance(params, Mapping) else {},
            )
        )  # fmt: skip
    return out


def _searched(query: str, params: Mapping[str, str]) -> str:
    return f"{query} [{', '.join(f'{k}={v}' for k, v in sorted(params.items()))}]" if params else query


def _when(fetched: str) -> tuple[int, float, str]:
    """A collection's time as a key to sort by: as a moment where it reads as one (one moment however written),
    the undated first."""
    try:
        moment = datetime.fromisoformat(fetched.replace("Z", "+00:00"))
    except ValueError:
        return (0, 0.0, fetched)
    return (1, (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp(), "")


def _latest(results: Iterable[SearchResult], *, web: bool = True) -> list[SearchResult]:
    """The results of each query's latest collection (its latest ``fetched`` time), web results only unless
    ``web`` is false."""
    listed = [r for r in results if r.type == "web" or not web]
    times = {fetched: _when(fetched) for fetched in {r.fetched for r in listed}}
    newest: dict[str, tuple[int, float, str]] = {}
    for result in listed:
        if result.searched not in newest or times[result.fetched] > newest[result.searched]:
            newest[result.searched] = times[result.fetched]
    return [r for r in listed if times[r.fetched] == newest[r.searched]]


def _by_query(results: Iterable[SearchResult], depth: int) -> dict[str, list[SearchResult]]:
    grouped: dict[str, list[SearchResult]] = defaultdict(list)
    for result in _latest(results):
        if result.position <= depth:
            grouped[result.searched].append(result)
    for listed in grouped.values():
        listed.sort(key=lambda r: r.position)
    return dict(grouped)


def _site(domain: str) -> str:
    """A domain as given (``www.shop.example``, ``https://shop.example/``) as a registrable domain."""
    host = urlsplit(domain).hostname if "//" in domain else domain.split("/")[0]
    return registrable_domain(host or domain)


# --------------------------------------------------------------------------- #
# the analyses
# --------------------------------------------------------------------------- #
@dataclass
class Visibility:
    """A domain's place in a set of searches."""

    domain: str
    #: Queries it ranks for (within the depth looked at), of those searched.
    queries: int
    #: Its best position for each, averaged.
    average_position: float
    #: The sum over the queries of 1 / its best position, divided by the queries searched: 1.0 is first for all.
    visibility: float
    #: Its results in all.
    results: int


def competitors(
    results: Iterable[SearchResult], *, domain: str | None = None, depth: int = 10, top: int = 20
) -> list[Visibility]:
    """The domains ranking in the top ``depth`` of the searches, the most visible first (a result's weight is
    1 / its position: a ranking's weight, not a click estimate); ``domain`` (yours) is left out."""
    grouped = _by_query(results, depth)
    own = _site(domain) if domain else None
    best: dict[str, dict[str, int]] = defaultdict(dict)
    counts: Counter[str] = Counter()
    for query, listed in grouped.items():
        for result in listed:
            counts[result.domain] += 1
            if result.domain not in best or query not in best[result.domain]:
                best[result.domain][query] = result.position
    searched = max(1, len(grouped))
    out = [
        Visibility(
            domain=name,
            queries=len(positions),
            average_position=round(sum(positions.values()) / len(positions), 1),
            visibility=round(sum(1 / p for p in positions.values()) / searched, 3),
            results=counts[name],
        )
        for name, positions in best.items()
        if name and name != own
    ]
    out.sort(key=lambda v: (-v.visibility, v.average_position, v.domain))
    return out[:top]


def gaps(
    results: Iterable[SearchResult],
    domain: str,
    *,
    rivals: Sequence[str] | None = None,
    depth: int = 10,
) -> list[dict[str, Any]]:
    """The queries where ``domain`` is not in the top ``depth`` and a rival is (``rivals``, by default the three
    most visible competitors): each with the rivals' best positions, the best-placed rival's first."""
    listed = list(results)
    own = _site(domain)
    others = (
        [_site(r) for r in rivals] if rivals else [v.domain for v in competitors(listed, domain=own, depth=depth)[:3]]
    )
    out: list[dict[str, Any]] = []
    for query, ranked in _by_query(listed, depth).items():
        if any(r.domain == own for r in ranked):
            continue
        present: dict[str, int] = {}
        for result in ranked:
            if result.domain in others and result.domain not in present:
                present[result.domain] = result.position
        if present:
            out.append({"query": query, "rivals": dict(sorted(present.items(), key=lambda kv: kv[1]))})
    out.sort(key=lambda g: (min(dict(g["rivals"]).values()), str(g["query"])))
    return out


def overlap(
    a: Iterable[SearchResult], b: Iterable[SearchResult], *, depth: int = 10, p: float = 0.9
) -> dict[str, float]:
    """How alike two lists of results are, in their top ``depth``: ``jaccard``, the share of pages in common,
    and ``rbo``, rank-biased overlap (the agreement at each depth, the top weighing most: ``p`` is how much each
    deeper position weighs against the one above), from 0 (nothing alike) to 1 (the same pages, same order)."""
    first = [r.url for r in sorted(_latest(a), key=lambda r: r.position) if r.position <= depth]
    second = [r.url for r in sorted(_latest(b), key=lambda r: r.position) if r.position <= depth]
    union = set(first) | set(second)
    jaccard = len(set(first) & set(second)) / len(union) if union else 0.0
    deepest = max(len(first), len(second))
    if not deepest:
        return {"jaccard": 0.0, "rbo": 0.0}
    agreement, seen_a, seen_b = 0.0, set(), set()
    for d in range(1, deepest + 1):
        if d <= len(first):
            seen_a.add(first[d - 1])
        if d <= len(second):
            seen_b.add(second[d - 1])
        agreement += p ** (d - 1) * len(seen_a & seen_b) / d
    rbo = (1 - p) * agreement / (1 - p**deepest)  # normalized: identical lists of this depth give 1
    return {"jaccard": round(jaccard, 3), "rbo": round(min(1.0, rbo), 3)}


def cluster_queries(results: Iterable[SearchResult], *, depth: int = 10, shared: int = 3) -> list[list[str]]:
    """Queries in groups that share at least ``shared`` of their top ``depth`` pages (one topic, one page can
    answer them). Each query joins the first group whose first query it shares that many pages with (so groups
    do not chain unrelated queries); the queries with the most results lead."""
    grouped = _by_query(results, depth)
    pages = {query: {r.url for r in listed} for query, listed in grouped.items()}
    order = sorted(pages, key=lambda q: (-len(pages[q]), q))
    clusters: list[list[str]] = []
    for query in order:
        for cluster in clusters:
            if len(pages[cluster[0]] & pages[query]) >= shared:
                cluster.append(query)
                break
        else:
            clusters.append([query])
    return clusters


@dataclass
class Module:
    """A kind of result or box the pages of a set of searches have besides their web results."""

    type: str
    #: Queries whose results have it.
    queries: int
    #: Its results in all.
    results: int
    #: Its place on the page (1 first), averaged over those queries, where the provider says.
    average_rank: float | None = None
    #: The domains of its results, those in it for the most queries first, with how many.
    domains: dict[str, int] = field(default_factory=dict)
    #: Queries for which ``domain`` (yours) is in it, when one was given.
    own: int | None = None


def modules(results: Iterable[SearchResult], *, domain: str | None = None, top: int = 5) -> list[Module]:
    """The kinds of results and boxes the pages have besides web results (news, videos, local results,
    questions, related searches...), in each query's latest results: for how many queries, where on the page,
    and the ``top`` domains in it; with ``domain``, for how many queries it is in it. The most common first."""
    own = _site(domain) if domain else None
    grouped: dict[str, list[SearchResult]] = defaultdict(list)
    for result in _latest(results, web=False):
        if result.type != "web":
            grouped[result.type].append(result)
    out = []
    for kind, listed in grouped.items():
        ranks: dict[str, int] = {}
        for result in listed:
            if result.rank is not None:
                ranks[result.searched] = min(ranks.get(result.searched, result.rank), result.rank)
        domains = Counter(name for name, _ in {(r.domain, r.searched) for r in listed if r.domain})
        out.append(
            Module(
                type=kind,
                queries=len({r.searched for r in listed}),
                results=len(listed),
                average_rank=round(sum(ranks.values()) / len(ranks), 1) if ranks else None,
                domains=dict(sorted(domains.items(), key=lambda kv: (-kv[1], kv[0]))[:top]),
                own=len({r.searched for r in listed if r.domain == own}) if own else None,
            )
        )
    out.sort(key=lambda m: (-m.queries, m.type))
    return out


def _change(was: int | None, now: int | None) -> str:
    if was is None:
        return "new"
    if now is None:
        return "lost"
    return "up" if now < was else "down" if now > was else "same"


def ranking_changes(
    before: Iterable[SearchResult], after: Iterable[SearchResult], domain: str, *, depth: int = 100
) -> list[dict[str, Any]]:
    """Each query's best position for ``domain`` in two collections of results (each query's latest in each):
    ``"up"``, ``"down"``, ``"same"``, ``"new"`` (ranked now, not before) or ``"lost"`` (the reverse); the
    largest moves first."""
    own = _site(domain)

    def best(results: Iterable[SearchResult]) -> tuple[dict[str, int], set[str]]:
        found: dict[str, int] = {}
        queries = set()
        for result in _latest(results):
            queries.add(result.searched)
            if result.domain == own and result.position <= depth:
                found[result.searched] = min(found.get(result.searched, result.position), result.position)
        return found, queries

    old, old_queries = best(before)
    new, new_queries = best(after)
    out: list[dict[str, Any]] = []
    for query in sorted(old_queries & new_queries):
        was, now = old.get(query), new.get(query)
        if was is None and now is None:
            continue
        moved = (was or depth + 1) - (now or depth + 1)
        out.append({"query": query, "before": was, "after": now, "change": _change(was, now), "moved": moved})
    out.sort(key=lambda c: (-abs(int(c["moved"])), str(c["query"])))
    return out


def ranking_history(results: Iterable[SearchResult], domain: str, *, depth: int = 100) -> list[dict[str, Any]]:
    """``domain``'s best position in each collection of each query's results (the results of one search share
    their ``fetched`` time), oldest first, for the queries it ranked for in any: ``{"query", "positions":
    [{"fetched", "position"}...], "best", "latest", "change"}``, a position ``None`` where it was not in the top
    ``depth``, and ``change`` the last move as :func:`ranking_changes` says it (``None`` with one collection,
    or when it ranked in neither of the last two). By query."""
    own = _site(domain)
    seen: dict[str, dict[str, int | None]] = defaultdict(dict)
    for result in results:
        if result.type != "web":
            continue
        found = seen[result.searched]
        if result.domain == own and result.position <= depth:
            known = found.get(result.fetched)
            found[result.fetched] = result.position if known is None else min(known, result.position)
        else:
            found.setdefault(result.fetched, None)
    out = []
    for query in sorted(seen):
        found = seen[query]
        ranked = [p for p in found.values() if p is not None]
        if not ranked:
            continue
        times = sorted(found, key=_when)
        was, now = (found[times[-2]], found[times[-1]]) if len(times) > 1 else (None, None)
        out.append(
            {
                "query": query,
                "positions": [{"fetched": t, "position": found[t]} for t in times],
                "best": min(ranked),
                "latest": found[times[-1]],
                "change": _change(was, now) if was is not None or now is not None else None,
            }
        )
    return out


def visibility_score(results: Iterable[SearchResult], domain: str, *, depth: int = 10) -> float:
    """``domain``'s visibility (see :class:`Visibility`) over the searches in ``results``."""
    grouped = _by_query(results, depth)
    own = _site(domain)
    total = sum(1 / min(r.position for r in listed if r.domain == own) for listed in grouped.values()
                if any(r.domain == own for r in listed))  # fmt: skip
    return round(total / max(1, len(grouped)), 3) if grouped else 0.0
