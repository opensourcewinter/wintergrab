"""Search results from search APIs (answered here in each provider's documented shape), and what they say."""

from __future__ import annotations

import json
import re
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from wintergrab.cli import main
from wintergrab.errors import ConfigurationError, WintergrabError
from wintergrab.intel.serp import (
    Module,
    SearchResult,
    cluster_queries,
    competitors,
    gaps,
    modules,
    overlap,
    ranking_changes,
    ranking_history,
    read_results,
    search,
    visibility_score,
)

#: What each query finds: (url, title, snippet), best first.
FOUND = {
    "budget laptop": [
        ("https://www.laptops.example/best-budget", "Best budget laptops", "Ten laptops under $500."),
        ("https://reviews.example/laptops/cheap", "Cheap laptops, reviewed", "We tested 30."),
        ("https://shop.example/c/laptops", "Laptops | Shop", "Laptops from $299."),
        ("https://blog.example/budget", "Budget buys", "Our picks."),
    ],
    "cheap laptop": [
        ("https://reviews.example/laptops/cheap", "Cheap laptops, reviewed", "We tested 30."),
        ("https://www.laptops.example/best-budget", "Best budget laptops", "Ten laptops under $500."),
        ("https://blog.example/budget", "Budget buys", "Our picks."),
        ("https://forum.example/t/1", "Which laptop?", "A thread."),
    ],
    "gaming mouse": [
        ("https://mice.example/gaming", "Gaming mice", "Light and fast."),
        ("https://reviews.example/mice", "Mice, reviewed", "We tested 12."),
    ],
}


def _page(query: str, first: int, size: int) -> list[tuple[str, str, str]]:
    return FOUND.get(query, [])[first : first + size]


#: The rest of Brave's first page for "budget laptop": its boxes, and their order on the page ("mixed").
BRAVE_BOXES = {
    "news": {"type": "news", "results": [
        {"title": "Laptop prices fall", "url": "https://news.example/laptops", "description": "Down 10%.",
         "age": "2 hours ago", "page_age": "2026-09-25T10:00:00", "source": "News Example", "breaking": True,
         "meta_url": {"hostname": "news.example"}},
        {"title": "Back to school", "url": "https://reviews.example/news/school", "description": "Deals.",
         "source": "Reviews Example", "breaking": False},
    ]},
    "videos": {"type": "videos", "results": [
        {"type": "video_result", "title": "Budget laptop review", "url": "https://video.example/watch?v=1",
         "description": "Five laptops.", "video": {"duration": "12:31", "creator": "Tech Reviews",
                                                   "publisher": "Video Example", "views": 12000}},
    ]},
    "locations": {"type": "locations", "results": [
        {"type": "location_result", "id": "loc-1", "title": "Laptop Shop", "url": "https://shop.example/stores/1",
         "coordinates": [48.8566, 2.3522], "contact": {"telephone": "+33 1 23 45 67 89"}, "price_range": "€€",
         "postal_address": {"type": "PostalAddress", "country": "FR", "postalCode": "75001",
                            "streetAddress": "1 Rue Example", "addressLocality": "Paris",
                            "displayAddress": "1 Rue Example, 75001 Paris"},
         "rating": {"ratingValue": 4.5, "bestRating": 5, "reviewCount": 120}},
    ]},
    "discussions": {"type": "search", "results": [
        {"type": "discussion", "title": "Which laptop?", "url": "https://forum.example/t/1", "description": "A thread.",
         "data": {"forum_name": "Laptops", "num_answers": 14, "score": "0.9", "question": "Which laptop?",
                  "top_comment": "Get one with 16 GB."}},
    ]},
    "infobox": {"type": "graph", "results": [
        {"type": "infobox", "title": "Laptop", "url": "https://encyclopedia.example/wiki/Laptop",
         "description": "A portable computer.", "long_desc": "A laptop is a small, portable computer."},
    ]},
    "mixed": {"type": "mixed", "top": [], "side": [{"type": "infobox", "index": 0, "all": False}], "main": [
        {"type": "web", "index": 0, "all": False}, {"type": "news", "all": True}, {"type": "web", "index": 1, "all": False},
        {"type": "locations", "all": True}, {"type": "videos", "all": True}, {"type": "discussions", "all": True},
        {"type": "faq", "all": True},
    ]},
}  # fmt: skip


class Api(BaseHTTPRequestHandler):
    """Brave's Search API, Google's Programmable Search JSON API and a SearXNG instance, as documented."""

    def log_message(self, *args) -> None:
        pass

    def _send(self, status: int, data: object) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        query = q.get("q", "")
        if parts.path == "/res/v1/web/search":  # Brave: the key in a header; offset counts pages
            if self.headers.get("X-Subscription-Token") != "brave-key":
                return self._send(401, {"type": "ErrorResponse", "error": {"code": "SUBSCRIPTION_TOKEN_INVALID"}})
            size, offset = int(q.get("count", 20)), int(q.get("offset", 0))
            if query == "rate limited" and offset:
                return self._send(429, {"type": "ErrorResponse", "error": {"code": "RATE_LIMITED"}})
            if query == "flaky" and offset:
                return self._send(400, {"type": "ErrorResponse", "error": {"code": "INVALID"}})
            size = 2  # (a small page, to see pages go)
            asked = {"rate limited": "budget laptop", "budget laptopp": "budget laptop", "flaky": "budget laptop"}.get(
                query, query
            )
            results = [{"title": t, "url": u, "description": d, "extra_snippets": [d], "page_age": "2026-09-01"}
                       for u, t, d in _page(asked, offset * size, size)]  # fmt: skip
            if q.get("country") == "de" and offset == 0:  # searched from Germany
                results.insert(0, {"title": "Laptops im Preisvergleich", "url": "https://preise.example.de/laptops",
                                   "description": "Günstige Laptops."})  # fmt: skip
            faq = {"type": "faq", "results": [{"question": "Is 8 GB enough?", "answer": "For most.", "title": "FAQ",
                                               "url": "https://faq.example/8gb"}]}  # fmt: skip
            answer = {"type": "search", "query": {"original": query}, "web": {"type": "search", "results": results},
                      "mixed": {"type": "mixed", "main": [{"type": "web", "index": i, "all": False}
                                                          for i in range(len(results))]}}  # fmt: skip
            if offset == 0:  # the boxes are on the first page
                answer.update(faq=faq, **(BRAVE_BOXES if asked == "budget laptop" else {}))
            if asked != query:  # a misspelling, searched as corrected
                answer["query"]["altered"] = asked
            return self._send(200, answer)
        if parts.path == "/customsearch/v1":  # Google: key and cx as parameters; start is the first result's number
            if q.get("key") != "google-key" or q.get("cx") != "engine-1":
                return self._send(403, {"error": {"code": 403, "message": "The request is missing a valid API key."}})
            start, num = int(q.get("start", 1)), int(q.get("num", 10))
            if start + num > 100:  # as documented: "setting the sum of start + num to a number greater than 100
                # will produce an error"
                return self._send(400, {"error": {"code": 400, "message": "Request contains an invalid argument."}})
            if query == "laptop":  # a query with more results than the API gives
                items = [
                    {"title": f"Laptops {n}", "link": f"https://site{n}.example/"} for n in range(start, start + num)
                ]
                return self._send(200, {"kind": "customsearch#search", "items": items})
            items = [{"kind": "customsearch#result", "title": t, "link": u, "snippet": d,
                      "displayLink": urlsplit(u).hostname} for u, t, d in _page(query, start - 1, num)]  # fmt: skip
            answer = {"kind": "customsearch#search", "items": items,
                      "searchInformation": {"totalResults": str(len(FOUND.get(query, [])))}}  # fmt: skip
            if query == "cheap laptop":  # the engine's promotion, and a spelling it suggests
                answer["promotions"] = [{"title": "Laptop sale", "link": "https://shop.example/sale",
                                         "displayLink": "shop.example", "bodyLines": [{"title": "Up to 30% off"}]}]  # fmt: skip
                answer["spelling"] = {"correctedQuery": "cheap laptops", "htmlCorrectedQuery": "cheap <b>laptops</b>"}
            return self._send(200, answer)
        if parts.path == "/search":  # SearXNG: JSON when its settings allow it
            if q.get("format") != "json":
                return self._send(403, {"error": "format not allowed"})
            pageno = int(q.get("pageno", 1))
            results = [{"url": u, "title": t, "content": d, "engine": "duckduckgo", "engines": ["duckduckgo"],
                        "score": 1.0, "category": "general", "publishedDate": None}
                       for u, t, d in _page(query, (pageno - 1) * 10, 10)]  # fmt: skip
            answer = {"query": query, "number_of_results": 0, "results": results, "answers": [], "corrections": [],
                      "infoboxes": [], "unresponsive_engines": [],
                      "suggestions": ["budget laptop 2026", "best cheap laptop"]}  # fmt: skip
            if query == "budget laptop" and pageno == 1:  # a news result among them, an infobox, an answer
                results.insert(1, {"url": "https://news.example/laptops", "title": "Laptop prices fall",
                                   "content": "Down 10%.", "engine": "bing news", "category": "news",
                                   "publishedDate": "2026-09-25T10:00:00"})  # fmt: skip
                answer["infoboxes"] = [{"infobox": "Laptop", "id": "https://encyclopedia.example/wiki/Laptop",
                                        "content": "A portable computer.", "engine": "wikipedia",
                                        "urls": [{"title": "Encyclopedia", "url": "https://encyclopedia.example/wiki/Laptop"}]}]  # fmt: skip
                answer["answers"] = [{"answer": "Budget laptops cost $300 to $500.", "url": "https://answers.example/a",
                                      "engine": "demo"}]  # fmt: skip
            elif query == "cheap laptop":
                answer["answers"] = ["Cheap laptops start at $199."]  # (older instances answer with text)
            elif query == "gaming mouse":
                answer["corrections"] = ["gaming mice"]
            return self._send(200, answer)
        return self._send(404, {})


@pytest.fixture(scope="module")
def api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_brave(api, monkeypatch) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    answer = search("budget laptop", endpoint=api + "/res/v1/web/search", pages=3, delay=0)
    assert [(r.position, r.domain) for r in answer.results] == [
        (1, "laptops.example"),
        (2, "reviews.example"),
        (3, "shop.example"),
        (4, "blog.example"),
    ]  # fmt: skip  (two pages of two, and an empty third: the end)
    first = answer.results[0]
    assert (first.title, first.snippet, first.date, first.source) == (
        "Best budget laptops",
        "Ten laptops under $500.",
        "2026-09-01",
        "brave",
    )
    assert answer.questions == [
        {"question": "Is 8 GB enough?", "answer": "For most.", "url": "https://faq.example/8gb"}
    ]
    # the page's boxes, placed where "mixed" puts them: the news in one place, the infobox beside the results
    assert [r.rank for r in answer.results] == [1, 3, 8, 9]  # (page two's continue)
    assert [(m.type, m.position, m.rank) for m in answer.modules] == [
        ("news", 1, 2), ("news", 2, 2), ("video", 1, 5), ("location", 1, 4), ("discussion", 1, 6),
        ("question", 1, 7), ("infobox", 1, None),
    ]  # fmt: skip
    news, school, video, shop, thread, _, infobox = answer.modules
    assert (news.domain, news.date, news.details) == ("news.example", "2026-09-25T10:00:00",
                                                      {"publisher": "News Example", "breaking": True})  # fmt: skip
    assert school.details == {"publisher": "Reviews Example"}  # (not breaking: not said)
    assert video.details == {
        "duration": "12:31",
        "creator": "Tech Reviews",
        "publisher": "Video Example",
        "views": 12000,
    }
    assert (shop.title, shop.domain, shop.details) == ("Laptop Shop", "shop.example", {
        "address": "1 Rue Example, 75001 Paris", "telephone": "+33 1 23 45 67 89", "rating": 4.5, "reviews": 120,
        "latitude": 48.8566, "longitude": 2.3522, "price_range": "€€",
    })  # fmt: skip
    assert (thread.domain, thread.details) == ("forum.example", {"forum": "Laptops", "answers": 14})
    assert (infobox.title, infobox.snippet) == ("Laptop", "A portable computer.")
    record = shop.to_dict()
    assert list(record)[:4] == ["query", "type", "position", "rank"] and record["reviews"] == 120
    assert read_results([record]) == [shop]  # read back as it was
    corrected = search("budget laptopp", endpoint=api + "/res/v1/web/search", delay=0)
    assert [(m.type, m.title) for m in corrected.modules][-1] == ("correction", "budget laptop")  # searched instead
    limited = search("rate limited", endpoint=api + "/res/v1/web/search", pages=3, delay=0)
    assert len(limited.results) == 2 and "rate limit (HTTP 429) stopped the search at page 2" in limited.notes[0]
    with pytest.raises(ConfigurationError, match=r"refused the request \(HTTP 401\): check the key in BRAVE_SEARCH"):
        search("budget laptop", endpoint=api + "/res/v1/web/search", key="wrong")
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY")
    with pytest.raises(ConfigurationError, match="set BRAVE_SEARCH_API_KEY to your API key"):
        search("budget laptop")


def test_where_and_in_what_language(api, monkeypatch) -> None:
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    brave = api + "/res/v1/web/search"
    german = search("budget laptop", endpoint=brave, params={"country": "de", "search_lang": "de"}, delay=0)
    first = german.results[0]
    assert (first.domain, first.params) == ("example.de", {"country": "de", "search_lang": "de"})
    assert first.searched == german.searched == "budget laptop [country=de, search_lang=de]"
    assert first.to_dict()["params"] == {"country": "de", "search_lang": "de"}
    plain = search("budget laptop", endpoint=brave, delay=0)
    # one query searched from two places: two searches to the analyses, not one
    assert visibility_score([*plain.results, *german.results], "example.de") == 0.5
    as_csv = {**first.to_dict(), "params": '{"country": "de", "search_lang": "de"}'}  # (a CSV keeps it as JSON)
    assert read_results([as_csv])[0].searched == first.searched
    with pytest.raises(ConfigurationError, match="offset: set by wintergrab"):
        search("budget laptop", endpoint=brave, params={"offset": 3})
    # a page after the first that fails: the pages before it kept, and said why the search stopped
    flaky = search("flaky", endpoint=brave, pages=3, delay=0)
    assert len(flaky.results) == 2 and flaky.notes == [
        "page 2 failed, and the search stopped there: brave answered HTTP 400"
    ]


def test_google_gives_100_results_at_most(api, monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    monkeypatch.setenv("GOOGLE_CSE_ID", "engine-1")
    answer = search("laptop", provider="google", endpoint=api + "/customsearch/v1", pages=10, delay=0)
    assert len(answer.results) == 99 and not answer.notes  # the tenth page asks for 9: start 91 + num 9 = 100
    assert answer.results[-1].url == "https://site99.example/"


def test_google(api, monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    monkeypatch.setenv("GOOGLE_CSE_ID", "engine-1")
    answer = search("cheap laptop", provider="google", endpoint=api + "/customsearch/v1")
    assert [r.url for r in answer.results][:2] == [
        "https://reviews.example/laptops/cheap", "https://www.laptops.example/best-budget",
    ]  # fmt: skip
    assert answer.total == 4 and answer.results[0].snippet == "We tested 30."
    assert [r.rank for r in answer.results] == [1, 2, 3, 4]  # (one list: its order)
    assert [(m.type, m.title, m.domain) for m in answer.modules] == [
        ("promotion", "Laptop sale", "shop.example"),
        ("correction", "cheap laptops", ""),
    ]
    with pytest.raises(WintergrabError) as error:  # a failure never shows the key (Google's is a parameter)
        search("cheap laptop", provider="google", endpoint="http://127.0.0.1:1/customsearch/v1", timeout=2)
    assert "google-key" not in str(error.value)
    monkeypatch.delenv("GOOGLE_CSE_ID")
    with pytest.raises(ConfigurationError, match="set GOOGLE_CSE_ID"):
        search("cheap laptop", provider="google", endpoint=api + "/customsearch/v1")


def test_searxng(api, monkeypatch) -> None:
    monkeypatch.setenv("SEARXNG_URL", api)  # the instance; /search is added
    answer = search("budget laptop", provider="searxng")
    assert len(answer.results) == 4 and answer.results[2].domain == "shop.example"
    assert [(r.position, r.rank) for r in answer.results] == [(1, 1), (2, 3), (3, 4), (4, 5)]  # a news result 2nd
    assert [(m.type, m.position, m.rank, m.title) for m in answer.modules] == [
        ("news", 1, 2, "Laptop prices fall"),  # (its category: news)
        ("infobox", 1, None, "Laptop"),
        ("answer", 1, None, "Budget laptops cost $300 to $500."),
        ("related", 1, None, "budget laptop 2026"),
        ("related", 2, None, "best cheap laptop"),
    ]
    assert answer.modules[1].url == "https://encyclopedia.example/wiki/Laptop"  # (an infobox's id is its URL)
    assert answer.related == ["budget laptop 2026", "best cheap laptop"]
    assert search("cheap laptop", provider="searxng").modules[0].title == "Cheap laptops start at $199."
    assert search("gaming mouse", provider="searxng").modules[0].to_dict()["type"] == "correction"
    monkeypatch.delenv("SEARXNG_URL")
    with pytest.raises(ConfigurationError, match="set SEARXNG_URL"):
        search("budget laptop", provider="searxng")
    with pytest.raises(ConfigurationError, match="no search provider 'bing'"):
        search("budget laptop", provider="bing")


def _results() -> list[SearchResult]:
    return [SearchResult(query=q, position=n, url=u, title=t) for q, found in FOUND.items()
            for n, (u, t, _) in enumerate(found, 1)]  # fmt: skip


def test_what_results_say() -> None:
    results = _results()
    ranked = competitors(results, domain="shop.example")
    assert [v.domain for v in ranked[:3]] == ["reviews.example", "laptops.example", "mice.example"]
    assert "shop.example" not in [v.domain for v in ranked]  # (yours)
    reviews = ranked[0]
    assert (reviews.queries, reviews.average_position, reviews.visibility) == (
        3,
        1.7,
        round((1 / 2 + 1 + 1 / 2) / 3, 3),
    )
    assert visibility_score(results, "https://shop.example/") == round((1 / 3) / 3, 3)
    missing = gaps(results, "shop.example")
    assert [g["query"] for g in missing] == ["cheap laptop", "gaming mouse"]
    assert missing[0]["rivals"] == {"reviews.example": 1, "laptops.example": 2}
    clusters = cluster_queries(results, shared=3)
    assert clusters == [["budget laptop", "cheap laptop"], ["gaming mouse"]]  # three pages in common
    budget = [r for r in results if r.query == "budget laptop"]
    assert overlap(budget, budget) == {"jaccard": 1.0, "rbo": 1.0}
    assert overlap(budget, [r for r in results if r.query == "gaming mouse"]) == {"jaccard": 0.0, "rbo": 0.0}
    alike = overlap(budget, [r for r in results if r.query == "cheap laptop"])
    assert alike["jaccard"] == 0.6 and 0.3 < alike["rbo"] < 1.0  # 3 of 5 pages; the top two swapped
    later = [SearchResult(query=r.query, position=r.position, url=r.url) for r in results if r.domain != "blog.example"]
    later.append(SearchResult(query="cheap laptop", position=2, url="https://blog.example/budget"))
    changes = {c["query"]: c for c in ranking_changes(results, later, "blog.example")}
    assert (changes["budget laptop"]["change"], changes["budget laptop"]["after"]) == ("lost", None)
    assert (changes["cheap laptop"]["change"], changes["cheap laptop"]["before"], changes["cheap laptop"]["after"]) == (
        "up",
        3,
        2,
    )
    records = [r.to_dict() for r in results]
    assert read_results(records) == results and read_results([{"query": "x", "position": "zero"}]) == []


def test_history_and_boxes() -> None:
    def searched(when: str, found: dict[str, list[str]]) -> list[SearchResult]:
        return [SearchResult(query=q, position=n, url=u, fetched=when) for q, urls in found.items()
                for n, u in enumerate(urls, 1)]  # fmt: skip

    laptops, reviews, shop = "https://www.laptops.example/b", "https://reviews.example/c", "https://shop.example/c/l"
    mice, forum = "https://mice.example/gaming", "https://forum.example/t/1"
    first, second, third = "2026-09-01T09:00:00+00:00", "2026-09-08T09:00:00+00:00", "2026-09-15T09:00:00Z"
    week1 = searched(first, {"budget laptop": [laptops, reviews, shop], "cheap laptop": [reviews, laptops, forum],
                             "gaming mouse": [mice, "https://reviews.example/mice"]})  # fmt: skip
    week2 = searched(second, {"budget laptop": [shop, laptops, reviews], "cheap laptop": [reviews, shop]})
    week3 = searched(third, {"budget laptop": [laptops, shop], "gaming mouse": [mice, "https://reviews.example/mice"]})
    boxes = [
        SearchResult(query="budget laptop", position=1, url="https://shop.example/stores/1", title="Shop",
                     type="location", rank=1, fetched=first),  # (an older search's)
        SearchResult(query="budget laptop", position=1, url="https://news.example/l", title="Prices fall",
                     type="news", rank=2, fetched=third),
        SearchResult(query="budget laptop", position=1, title="cheap laptops", type="related", fetched=third),
        SearchResult(query="gaming mouse", position=1, url="https://shop.example/stores/2", title="Shop",
                     type="location", rank=1, details={"rating": 4.5}, fetched=third),
    ]  # fmt: skip
    results = [*week1, *week2, *week3, *boxes]
    # what they say now: each query's latest web results (budget laptop's and gaming mouse's of week 3...)
    ranked = competitors(results, domain="shop.example")
    assert [v.domain for v in ranked] == ["reviews.example", "laptops.example", "mice.example"]
    assert visibility_score(results, "shop.example") == round((1 / 2 + 1 / 2) / 3, 3)
    assert modules(results, domain="shop.example") == [
        Module(type="location", queries=1, results=1, average_rank=1.0, domains={"shop.example": 1}, own=1),
        Module(type="news", queries=1, results=1, average_rank=2.0, domains={"news.example": 1}, own=0),
        Module(type="related", queries=1, results=1, average_rank=None, domains={}, own=0),
    ]
    history = ranking_history(results, "shop.example")
    assert [
        (h["query"], [p["position"] for p in h["positions"]], h["best"], h["latest"], h["change"]) for h in history
    ] == [
        ("budget laptop", [3, 1, 2], 1, 2, "down"),
        ("cheap laptop", [None, 2], 2, 2, "new"),
    ]  # fmt: skip  (gaming mouse: never ranked)
    assert [p["fetched"] for p in history[0]["positions"]] == [first, second, third]
    changes = {c["query"]: c["change"] for c in ranking_changes(week1, [*week2, *week3], "shop.example")}
    assert changes == {"budget laptop": "up", "cheap laptop": "new"}  # (week 3's budget laptop against week 1's)
    assert read_results(r.to_dict() for r in results) == results
    one_moment = [SearchResult(query="q", position=1, url="https://a.example/", fetched="2026-09-15T09:00:00Z"),
                  SearchResult(query="q", position=2, url="https://b.example/", fetched="2026-09-15T09:00:00+00:00")]  # fmt: skip
    assert [v.domain for v in competitors(one_moment)] == ["a.example", "b.example"]  # one search, however written
    kept = read_results([{"query": "q", "position": 2, "rank": "n/a", "url": "https://a.example/"}])
    assert [(r.position, r.rank) for r in kept] == [(2, None)]  # (a rank that is not a number: no rank)


def test_the_search_command(api, monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("SEARXNG_URL", api)
    out = tmp_path / "serp.jsonl"
    assert main(["-q", "search", "budget laptop", "cheap laptop", "gaming mouse", "--provider", "searxng",
                 "--delay", "0", "-o", str(out)]) == 0  # fmt: skip
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len([r for r in rows if r["type"] == "web"]) == 10 and rows[0]["query"] == "budget laptop"
    assert rows[0]["source"] == "searxng" and rows[0]["rank"] == 1
    others = Counter(r["type"] for r in rows if r["type"] != "web")
    assert others == {"related": 6, "answer": 2, "news": 1, "infobox": 1, "correction": 1}
    capsys.readouterr()
    assert main(["search", "budget laptop", "--provider", "searxng", "--delay", "0"]) == 0
    shown = capsys.readouterr().out
    assert (
        "budget laptop  (4 results)" in shown and "3. shop.example" in shown and "related: budget laptop 2026" in shown
    )
    assert "news (place #2): Laptop prices fall (news.example)" in shown and "infobox: Laptop (encyclopedia." in shown
    before = tmp_path / "before.jsonl"
    before.write_text("".join(json.dumps({**r, "position": r["position"] + 1}) + "\n" for r in rows), encoding="utf-8")
    assert main(["search", "--report", str(out), "--domain", "shop.example", "--before", str(before)]) == 0
    report = capsys.readouterr().out
    assert "10 results for 3 queries\n" in report and "shop.example: visibility 0.111" in report
    assert "reviews.example" in report and "budget laptop | cheap laptop" in report
    assert "related      in 3 of 3 queries\n" in report
    assert "news         in 1 of 3 queries, place #2 on the page: news.example (1)\n" in report
    assert "Gaps: 2 queries" in report and "budget laptop" in report.split("Changes since")[1]
    assert "History" not in report  # (one search of each)

    # an earlier search (shop.example not in it), and this one added: the history
    history = tmp_path / "history.jsonl"
    history.write_text("".join(json.dumps({**r, "fetched": "2026-09-01T09:00:00+00:00"}) + "\n" for r in rows
                               if r.get("domain") != "shop.example"), encoding="utf-8")  # fmt: skip
    assert main(["-q", "search", "budget laptop", "--provider", "searxng", "--delay", "0", "-o", str(history),
                 "--append"]) == 0  # fmt: skip
    assert main(["search", "--report", str(history), "--domain", "shop.example"]) == 0
    report = capsys.readouterr().out
    assert "searched up to 2 times (2026-09-01 to " in report and "reads each query's latest" in report
    assert re.search(r"\n  budget laptop +- 3 +best #3, new\n", report)
    assert main(["search", "--report", str(out), "--pages", "2"]) == 2
    assert "--pages: for searching" in capsys.readouterr().err
    assert main(["search", "--report", str(out), "--before", str(before)]) == 2
    assert "say which with --domain" in capsys.readouterr().err
    assert main(["search", "budget laptop", "--domain", "shop.example", "--depth", "5"]) == 2
    assert "--domain, --depth: for --report" in capsys.readouterr().err
    assert main(["search", "budget laptop", "--append"]) == 2 and "with -o FILE" in capsys.readouterr().err
    assert main(["search", "budget laptop", "--param", "country"]) == 2 and "say NAME=VALUE" in capsys.readouterr().err
    assert main(["search", "--report", str(out), "--param", "country=de"]) == 2
    assert "--param: for searching" in capsys.readouterr().err
    assert main(["search", "budget laptop", "--provider", "searxng", "--param", "language=de", "--delay", "0"]) == 0
    assert "budget laptop [language=de]  (4 results)" in capsys.readouterr().out
    assert main(["search"]) == 2


def test_from_the_entry_point(api, monkeypatch) -> None:
    from wintergrab import NetworkPolicyError, WinterGrab

    monkeypatch.setenv("SEARXNG_URL", api)
    assert len(WinterGrab().search("budget laptop", provider="searxng").results) == 4
    with pytest.raises(NetworkPolicyError):  # its settings hold: a local instance is refused under "public"
        WinterGrab(network_policy="public").search("budget laptop", provider="searxng")


def test_a_goal_finds_sites(api, monkeypatch, capsys) -> None:
    monkeypatch.setenv("SEARXNG_URL", api)
    code = main(["goal", "budget laptop", "--find-sites", "--provider", "searxng"])
    assert code == 2  # candidates shown: the user picks
    out, err = capsys.readouterr()
    assert "Sites that rank for it (searxng, 4 results):" in out and "1. laptops.example" in out
    assert "pick one: wintergrab goal 'budget laptop' --site laptops.example" in err
    assert main(["goal", "budget laptop"]) == 2 and "--find-sites asks a search API" in capsys.readouterr().err
