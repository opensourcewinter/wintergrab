"""Search results from search APIs (answered here in each provider's documented shape), and what they say."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from wintergrab.cli import main
from wintergrab.errors import ConfigurationError, WintergrabError
from wintergrab.intel.serp import (
    SearchResult,
    cluster_queries,
    competitors,
    gaps,
    overlap,
    ranking_changes,
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
            size = 2  # (a small page, to see pages go)
            results = [{"title": t, "url": u, "description": d, "extra_snippets": [d], "page_age": "2026-09-01"}
                       for u, t, d in _page(query.replace("rate limited", "budget laptop"), offset * size, size)]  # fmt: skip
            faq = {"type": "faq", "results": [{"question": "Is 8 GB enough?", "answer": "For most.", "title": "FAQ",
                                               "url": "https://faq.example/8gb"}]}  # fmt: skip
            return self._send(200, {"type": "search", "query": {"original": query}, "faq": faq,
                                    "web": {"type": "search", "results": results}})  # fmt: skip
        if parts.path == "/customsearch/v1":  # Google: key and cx as parameters; start is the first result's number
            if q.get("key") != "google-key" or q.get("cx") != "engine-1":
                return self._send(403, {"error": {"code": 403, "message": "The request is missing a valid API key."}})
            start, num = int(q.get("start", 1)), int(q.get("num", 10))
            items = [{"kind": "customsearch#result", "title": t, "link": u, "snippet": d,
                      "displayLink": urlsplit(u).hostname} for u, t, d in _page(query, start - 1, num)]  # fmt: skip
            return self._send(200, {"kind": "customsearch#search", "items": items,
                                    "searchInformation": {"totalResults": str(len(FOUND.get(query, [])))}})  # fmt: skip
        if parts.path == "/search":  # SearXNG: JSON when its settings allow it
            if q.get("format") != "json":
                return self._send(403, {"error": "format not allowed"})
            pageno = int(q.get("pageno", 1))
            results = [{"url": u, "title": t, "content": d, "engine": "duckduckgo", "engines": ["duckduckgo"],
                        "score": 1.0, "category": "general", "publishedDate": None}
                       for u, t, d in _page(query, (pageno - 1) * 10, 10)]  # fmt: skip
            return self._send(200, {"query": query, "number_of_results": 0, "results": results, "answers": [],
                                    "corrections": [], "infoboxes": [], "unresponsive_engines": [],
                                    "suggestions": ["budget laptop 2026", "best cheap laptop"]})  # fmt: skip
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
    limited = search("rate limited", endpoint=api + "/res/v1/web/search", pages=3, delay=0)
    assert len(limited.results) == 2 and "rate limit (HTTP 429) stopped the search at page 2" in limited.notes[0]
    with pytest.raises(ConfigurationError, match=r"refused the request \(HTTP 401\): check the key in BRAVE_SEARCH"):
        search("budget laptop", endpoint=api + "/res/v1/web/search", key="wrong")
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY")
    with pytest.raises(ConfigurationError, match="set BRAVE_SEARCH_API_KEY to your API key"):
        search("budget laptop")


def test_google(api, monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    monkeypatch.setenv("GOOGLE_CSE_ID", "engine-1")
    answer = search("cheap laptop", provider="google", endpoint=api + "/customsearch/v1")
    assert [r.url for r in answer.results][:2] == [
        "https://reviews.example/laptops/cheap", "https://www.laptops.example/best-budget",
    ]  # fmt: skip
    assert answer.total == 4 and answer.results[0].snippet == "We tested 30."
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
    assert answer.related == ["budget laptop 2026", "best cheap laptop"]
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


def test_the_search_command(api, monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("SEARXNG_URL", api)
    out = tmp_path / "serp.jsonl"
    assert main(["-q", "search", "budget laptop", "cheap laptop", "gaming mouse", "--provider", "searxng",
                 "--delay", "0", "-o", str(out)]) == 0  # fmt: skip
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 10 and rows[0]["query"] == "budget laptop" and rows[0]["source"] == "searxng"
    capsys.readouterr()
    assert main(["search", "budget laptop", "--provider", "searxng", "--delay", "0"]) == 0
    shown = capsys.readouterr().out
    assert (
        "budget laptop  (4 results)" in shown and "3. shop.example" in shown and "related: budget laptop 2026" in shown
    )
    before = tmp_path / "before.jsonl"
    before.write_text("".join(json.dumps({**r, "position": r["position"] + 1}) + "\n" for r in rows), encoding="utf-8")
    assert main(["search", "--report", str(out), "--domain", "shop.example", "--before", str(before)]) == 0
    report = capsys.readouterr().out
    assert "10 results for 3 queries" in report and "shop.example: visibility 0.111" in report
    assert "reviews.example" in report and "budget laptop | cheap laptop" in report
    assert "Gaps: 2 queries" in report and "budget laptop" in report.split("Changes since")[1]
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
