"""Search results: collected from search APIs that permit it, and read for rankings, competitors and gaps.

::

    answer = search("budget laptop", provider="brave")    # BRAVE_SEARCH_API_KEY from the environment
    answer.results[0]      # SearchResult(query='budget laptop', position=1, url=..., domain=..., title=..., ...)
    answer.related         # related searches, where the provider gives them (SearXNG)
    answer.questions       # questions people ask, where it gives them (Brave)

    competitors(results, domain="shop.example")     # the domains ranking for the same queries, most visible first
    gaps(results, domain="shop.example")            # queries others rank for and the domain does not
    cluster_queries(results)                        # queries whose results share pages: one topic
    overlap(results_a, results_b)                   # how alike two lists of results are
    ranking_changes(before, after, domain="shop.example")

Only search APIs are asked, with your own access: Brave's Search API (``BRAVE_SEARCH_API_KEY``), Google's
Programmable Search JSON API (``GOOGLE_API_KEY`` and ``GOOGLE_CSE_ID``), or a SearXNG instance of your own
(``SEARXNG_URL``, with its JSON format enabled). Search engines' own result pages are not fetched: their terms
forbid it. Each provider's terms and quotas are yours to keep: requests go one at a time, a second apart by
default, a 429 is waited out as the answer asks (once), and a refusal is reported, not asked another way.

An answer is read without a fixed schema: its results are the list whose records have a URL and a title
(``web.results[]``, ``items[]``, ``results[]``...), their snippet the first of ``description``, ``snippet``,
``content``... The analyses read any records with a query, a position and a URL, however collected (a rank
tracker's export, a crawl of a site's own search pages): ``read_records`` gives them.
"""

from __future__ import annotations

import os
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from ..errors import ConfigurationError, WintergrabError, describe
from ..fetchers.resources import registrable_domain
from .sources import json_collections

__all__ = [
    "PROVIDERS",
    "Provider",
    "SearchAnswer",
    "SearchResult",
    "Visibility",
    "cluster_queries",
    "competitors",
    "gaps",
    "overlap",
    "ranking_changes",
    "read_results",
    "search",
    "visibility_score",
]


@dataclass(frozen=True)
class Provider:
    """How to ask a search API: its endpoint, the key, and how its pages go.

    Attributes:
        endpoint: The API's URL (``None``: from ``endpoint_variable``, for an instance of your own).
        key_variable: The environment variable holding the key (``None``: no key).
        key_header: The header the key goes in (else ``key_param``, a query parameter).
        variables: More query parameters from the environment (``{"cx": "GOOGLE_CSE_ID"}``).
        size_param, size: The parameter for results a page, and how many are asked for.
        page_param: The parameter saying which page; ``page_by``: ``"index"`` (0, 1, 2...), ``"number"``
            (1, 2, 3...) or ``"first"`` (the first result's number: 1, 11, 21...).
        results_path: Where its results are, when its answer is the one expected.
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
        results_path="web.results[]",
    ),
    "google": Provider(
        "google", endpoint="https://www.googleapis.com/customsearch/v1", key_variable="GOOGLE_API_KEY",
        key_param="key", variables={"cx": "GOOGLE_CSE_ID"}, size_param="num", size=10, page_param="start",
        page_by="first", results_path="items[]",
    ),
    "searxng": Provider(
        "searxng", endpoint_variable="SEARXNG_URL", params={"format": "json"}, page_param="pageno",
        page_by="number", results_path="results[]",
    ),
}  # fmt: skip
_URL_NAMES = ("url", "link", "href")
_TITLE_NAMES = ("title", "name")
_SNIPPET_NAMES = ("description", "snippet", "content", "summary", "text", "body")
_DATE_NAMES = ("page_age", "publishedDate", "published", "date", "age")
_MAX_PAGES = 10
#: Lists in an answer that are not its results (questions, boxes, suggestions).
_NOT_RESULTS = ("faq", "infobox", "answers", "suggestions", "related", "corrections", "queries", "context")


@dataclass
class SearchResult:
    """One result of a search: its query, its position (1 first, across pages), and what it links to."""

    query: str
    position: int
    url: str
    title: str = ""
    snippet: str = ""
    domain: str = ""
    date: str | None = None
    source: str = ""
    fetched: str = ""

    def __post_init__(self) -> None:
        if not self.domain and self.url:
            self.domain = registrable_domain(urlsplit(self.url).hostname or "")

    def to_dict(self) -> dict[str, Any]:
        """The result as a record (its empty fields left out)."""
        return {k: v for k, v in asdict(self).items() if v not in (None, "")}


@dataclass
class SearchAnswer:
    """What a search gave: its results, and what else the provider said."""

    query: str
    provider: str
    results: list[SearchResult] = field(default_factory=list)
    #: Related searches (SearXNG's suggestions).
    related: list[str] = field(default_factory=list)
    #: Questions people ask about it, with an answer when given (Brave's FAQ).
    questions: list[dict[str, str]] = field(default_factory=list)
    #: How many results the provider says it has, when it says.
    total: int | None = None
    #: Why fewer pages were read than asked for, when that happened.
    notes: list[str] = field(default_factory=list)


def search(
    query: str,
    *,
    provider: str | Provider = "brave",
    pages: int = 1,
    endpoint: str | None = None,
    key: str | None = None,
    delay: float = 1.0,
    timeout: float = 20.0,
    network_policy: Any = None,
    fetcher: Any = None,
) -> SearchAnswer:
    """Ask a search API for ``query`` (see the module docs): ``pages`` pages of it, ``delay`` seconds apart.

    Args:
        provider: ``"brave"``, ``"google"``, ``"searxng"`` or a :class:`Provider`.
        endpoint: The API's URL, instead of the provider's (a SearXNG instance: ``https://searx.example``).
        key: The key, instead of the environment's.
        network_policy: Where requests may go (see :class:`~wintergrab.NetworkPolicy`).
        fetcher: A :class:`~wintergrab.Fetcher` to ask with (by default one of its own).
    """
    spec = PROVIDERS.get(provider) if isinstance(provider, str) else provider
    if spec is None:
        raise ConfigurationError(f"no search provider {provider!r} (known: {', '.join(PROVIDERS)})")
    url = endpoint or spec.endpoint or (os.environ.get(spec.endpoint_variable) if spec.endpoint_variable else None)
    if not url:
        raise ConfigurationError(f"{spec.name}: say where it is: set {spec.endpoint_variable} (or give endpoint=)")
    if spec.name == "searxng" and not urlsplit(url).path.rstrip("/").endswith("/search"):
        url = url.rstrip("/") + "/search"
    if spec.key_variable:
        key = key or os.environ.get(spec.key_variable)
        if not key:
            raise ConfigurationError(f"{spec.name}: set {spec.key_variable} to your API key")
    params: dict[str, Any] = {spec.query_param: query, **spec.params}
    for name, variable in spec.variables.items():
        value = os.environ.get(variable)
        if not value:
            raise ConfigurationError(f"{spec.name}: set {variable}")
        params[name] = value
    if spec.size_param:
        params[spec.size_param] = spec.size
    headers = {"Accept": "application/json"}
    if key and spec.key_header:
        headers[spec.key_header] = key
    elif key and spec.key_param:
        params[spec.key_param] = key
    own = fetcher is None
    if own:
        from ..fetchers import Fetcher

        fetcher = Fetcher(timeout=timeout, retries=1, network_policy=network_policy)
    answer = SearchAnswer(query=query, provider=spec.name)
    fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        for index in range(max(1, min(pages, _MAX_PAGES))):
            if index:
                time.sleep(delay)
            params[spec.page_param] = spec.page_value(index)
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
                answer.notes.append(f"the provider's rate limit (HTTP 429) stopped the search at page {index + 1}")
                break
            if not 200 <= response.status < 300:
                raise WintergrabError(f"{spec.name} answered HTTP {response.status}")
            try:
                data = response.json()
            except ValueError:
                raise WintergrabError(f"{spec.name} answered something that is not JSON") from None
            page = _results(data, query, spec, len(answer.results), fetched)
            if index == 0:
                answer.related, answer.questions, answer.total = _related(data), _questions(data), _total(data)
            if not page:
                break
            answer.results.extend(page)
    finally:
        if own:
            fetcher.close()
    return answer


def _without(text: str, key: str | None) -> str:
    return text.replace(key, "***") if key else text


def _first(record: Mapping[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        value = record.get(name)
        if value not in (None, "", [], {}):
            return value
    return None


def _known_list(data: Any, path: str | None) -> list[Any] | None:
    """The list at a simple path (``web.results[]``) of an answer, when the answer has it (empty or not)."""
    if not path or not path.endswith("[]"):
        return None
    node = data
    for key in path[:-2].split(".") if path[:-2] else []:
        node = node.get(key) if isinstance(node, Mapping) else None
    return node if isinstance(node, list) else None


def _results(data: Any, query: str, spec: Provider, offset: int, fetched: str) -> list[SearchResult]:
    """The results in one of the provider's answers: the list at its known path when the answer has it (an
    empty one is the end), else the list whose records have URLs and titles (not questions, boxes...)."""
    known = _known_list(data, spec.results_path)
    if known is not None:
        candidates = [[r for r in known if isinstance(r, Mapping)]]
    else:
        candidates = [
            list(c.records) for c in json_collections(data, min_records=1) if not c.path.startswith(_NOT_RESULTS)
        ]
    for listed in candidates:
        records = [r for r in listed if isinstance(_first(r, _URL_NAMES), str)]
        linked = [r for r in records if str(_first(r, _URL_NAMES)).startswith(("http://", "https://"))]
        if not linked or not any(_first(r, _TITLE_NAMES) for r in linked):
            continue
        out: list[SearchResult] = []
        for record in linked:
            snippet = _first(record, _SNIPPET_NAMES)
            date = _first(record, _DATE_NAMES)
            out.append(
                SearchResult(
                    query=query,
                    position=offset + len(out) + 1,
                    url=str(_first(record, _URL_NAMES)),
                    title=_text(_first(record, _TITLE_NAMES)),
                    snippet=_text(snippet),
                    date=str(date) if date is not None else None,
                    source=spec.name,
                    fetched=fetched,
                )
            )
        return out
    return []


def _text(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _related(data: Any) -> list[str]:
    suggestions = data.get("suggestions") if isinstance(data, Mapping) else None
    return [str(s) for s in suggestions if isinstance(s, str)] if isinstance(suggestions, list) else []


def _questions(data: Any) -> list[dict[str, str]]:
    faq = data.get("faq") if isinstance(data, Mapping) else None
    items = faq.get("results") if isinstance(faq, Mapping) else None
    out = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, Mapping) and item.get("question"):
            out.append({k: _text(item.get(k)) for k in ("question", "answer", "url") if item.get(k)})
    return out


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
    :class:`SearchResult` objects; the others are left out."""
    out = []
    for record in records:
        try:
            position = int(record.get("position") or record.get("rank") or 0)
        except (TypeError, ValueError):
            continue
        url = record.get("url") or record.get("link")
        query = record.get("query") or record.get("keyword")
        if position < 1 or not isinstance(url, str) or not query:
            continue
        out.append(
            SearchResult(
                query=str(query), position=position, url=url, title=_text(record.get("title")),
                snippet=_text(record.get("snippet")), domain=str(record.get("domain") or ""),
                date=record.get("date"), source=str(record.get("source") or ""), fetched=str(record.get("fetched") or ""),
            )
        )  # fmt: skip
    return out


def _by_query(results: Iterable[SearchResult], depth: int) -> dict[str, list[SearchResult]]:
    grouped: dict[str, list[SearchResult]] = defaultdict(list)
    for result in results:
        if result.position <= depth:
            grouped[result.query].append(result)
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
    first = [r.url for r in sorted(a, key=lambda r: r.position) if r.position <= depth]
    second = [r.url for r in sorted(b, key=lambda r: r.position) if r.position <= depth]
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


def ranking_changes(
    before: Iterable[SearchResult], after: Iterable[SearchResult], domain: str, *, depth: int = 100
) -> list[dict[str, Any]]:
    """Each query's best position for ``domain`` in two collections of results: ``"up"``, ``"down"``,
    ``"same"``, ``"new"`` (ranked now, not before) or ``"lost"`` (the reverse); the largest moves first."""
    own = _site(domain)

    def best(results: Iterable[SearchResult]) -> tuple[dict[str, int], set[str]]:
        found: dict[str, int] = {}
        queries = set()
        for result in results:
            queries.add(result.query)
            if result.domain == own and result.position <= depth:
                found[result.query] = min(found.get(result.query, result.position), result.position)
        return found, queries

    old, old_queries = best(before)
    new, new_queries = best(after)
    out: list[dict[str, Any]] = []
    for query in sorted(old_queries & new_queries):
        was, now = old.get(query), new.get(query)
        if was is None and now is None:
            continue
        if was is None:
            change = "new"
        elif now is None:
            change = "lost"
        else:
            change = "up" if now < was else "down" if now > was else "same"
        moved = (was or depth + 1) - (now or depth + 1)
        out.append({"query": query, "before": was, "after": now, "change": change, "moved": moved})
    out.sort(key=lambda c: (-abs(int(c["moved"])), str(c["query"])))
    return out


def visibility_score(results: Iterable[SearchResult], domain: str, *, depth: int = 10) -> float:
    """``domain``'s visibility (see :class:`Visibility`) over the searches in ``results``."""
    grouped = _by_query(results, depth)
    own = _site(domain)
    total = sum(1 / min(r.position for r in listed if r.domain == own) for listed in grouped.values()
                if any(r.domain == own for r in listed))  # fmt: skip
    return round(total / max(1, len(grouped)), 3) if grouped else 0.0
