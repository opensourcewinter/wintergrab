"""Goals collected from the API a site's pages call: planned from the calls rendered pages make, run over HTTP
page by page, left for the pages when the API fails, and never asked another way when it refuses."""

from __future__ import annotations

import json
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from wintergrab import WinterGrab
from wintergrab.cli import main
from wintergrab.goals import GoalPlan, parse_goal, plan_goal
from wintergrab.goals.api import ApiSource, find_api, items_at, next_page, records_of
from wintergrab.runs import replay

TOTAL, PER_PAGE = 10, 4

SHELL = b"""<!doctype html><html><head><title>Laptops</title></head><body>
<div id="app">Loading...</div>
<script>
fetch('/api/laptops?page=1&per_page=4').then(r => r.json()).then(answer => {
  document.getElementById('app').innerHTML = answer.items.map(item =>
    `<div class="laptop"><a href="${item.url}">${item.title}</a> <span>$${item.price.amount}</span></div>`).join('');
});
</script></body></html>"""


def _laptop(n: int) -> dict:
    return {
        "id": n,
        "title": f"Laptop {n}",
        "price": {"amount": 500 + 50 * n, "currency": "USD"},
        "rating": {"average": round(3.5 + n / 10, 1), "count": 10 * n},
        "url": f"/p/{n}",
    }


def _product_page(n: int) -> bytes:
    laptop = _laptop(n)
    data = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": laptop["title"],
        "offers": {"@type": "Offer", "price": str(laptop["price"]["amount"]), "priceCurrency": "USD"},
        "aggregateRating": {"@type": "AggregateRating", "ratingValue": str(laptop["rating"]["average"])},
    }
    return f"""<!doctype html><html><head><title>{laptop["title"]}</title>
<script type="application/ld+json">{json.dumps(data)}</script></head><body>
<h1>{laptop["title"]}</h1><p class="price">${laptop["price"]["amount"]}</p>
<p>Rated {laptop["rating"]["average"]} out of 5. A light laptop with a long battery life, a bright screen and a
keyboard that is good to type on for hours, sold with a two-year warranty.</p>
<a href="/">All laptops</a></body></html>""".encode()


class Shop(BaseHTTPRequestHandler):
    """A shop whose catalog is built in the browser from its JSON API (four laptops a page), with a
    server-rendered page per laptop and a sitemap of them. ``mode`` changes how the API and robots.txt answer."""

    mode: dict[str, str] = {}
    hits: Counter[str] = Counter()

    def log_message(self, *args) -> None:
        pass

    def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query)
        Shop.hits["api" if path.startswith("/api/") else "product" if path.startswith("/p/") else path] += 1
        host = f"http://{self.headers['Host']}"
        if path == "/robots.txt":
            rules = "Disallow: /api/\n" if Shop.mode.get("robots") == "no-api" else "Disallow:\n"
            return self._send(f"User-agent: *\n{rules}Sitemap: {host}/sitemap.xml\n".encode(), "text/plain")
        if path == "/sitemap.xml":
            urls = "".join(f"<url><loc>{host}/p/{n}</loc></url>" for n in range(1, TOTAL + 1))
            body = f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
            return self._send(body.encode(), "application/xml")
        if path.startswith("/p/"):
            return self._send(_product_page(int(path[3:])), "text/html; charset=utf-8")
        if path == "/api/laptops":
            api = Shop.mode.get("api", "ok")
            if api in ("403", "404", "429"):
                return self._send(b'{"error": "no"}', "application/json", int(api))
            if api == "html":
                return self._send(b"<html><body><h1>We moved</h1></body></html>", "text/html")
            if api == "bot":
                challenge = (
                    b"<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"
                )
                return self._send(challenge, "text/html")
            page, per_page = int(query.get("page", ["1"])[0]), int(query.get("per_page", ["4"])[0])
            if api == "fails-on-2" and page == 2:
                return self._send(b'{"error": "oops"}', "application/json", 500)
            items = [_laptop(n) for n in range((page - 1) * per_page + 1, min(page * per_page, TOTAL) + 1)]
            pages = -(-TOTAL // per_page)
            answer = {"items": items, "page": page, "per_page": per_page, "total_pages": pages, "total": TOTAL}
            return self._send(json.dumps(answer).encode(), "application/json")
        if path == "/":
            return self._send(SHELL, "text/html; charset=utf-8")
        return self._send(b"not found", "text/plain", 404)


@pytest.fixture(scope="module")
def shop():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Shop)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def modes():
    Shop.mode = {}
    Shop.hits.clear()
    yield Shop.mode
    Shop.mode = {}


def _api_plan(shop: str) -> GoalPlan:
    """A plan that collects from the shop's API, as the planner would make it after rendering its catalog."""
    source = {
        "method": "GET",
        "url": f"{shop}/api/laptops?page=1&per_page={PER_PAGE}",
        "path": "items[]",
        "fields": {
            "name": "title",
            "price": "price",
            "currency": "price.currency",
            "rating": "rating.average",
            "url": "url",
        },
        "pagination": {"kind": "page", "parameter": "page", "value": 1, "size": PER_PAGE, "next": 2, "pages": 3},
        "per_page": PER_PAGE,
        "total": TOTAL,
        "pages": 3,
        "seen_on": shop + "/",
    }
    goal = parse_goal("laptops with name, price and rating", sites=[shop])
    plan = GoalPlan.from_dict(
        {
            "goal": goal.to_dict(),
            "sites": [
                {"site": shop, "strategy": "sitemap", "sitemap_urls": [shop + "/sitemap.xml"],
                 "target": ["/p/*"], "page_types": ["product"], "api": source}
            ],
        }
    )  # fmt: skip
    return plan


# --------------------------------------------------------------------------- #
# planning: the calls the rendered pages make
# --------------------------------------------------------------------------- #
@pytest.mark.browser
def test_the_plan_takes_the_api_the_pages_call(shop, modes, tmp_path) -> None:
    goal = parse_goal("laptops under $800 with name, price and rating", sites=[shop])
    plan = plan_goal(goal, sample=6, log_level=None)  # the catalog needs JavaScript: it is rendered, its calls kept
    site = plan.sites[0]
    source = ApiSource.from_dict(site.api or {})
    assert (source.method, source.path, source.pages, source.total) == ("GET", "items[]", 3, 10)
    assert source.fields == {
        "name": "title", "price": "price", "currency": "price.currency", "rating": "rating.average", "url": "url",
    }  # fmt: skip
    assert source.seen_on == shop + "/"
    shown = plan.describe()
    assert f"{shop}  (api, else sitemap)" in shown
    assert "Ask the API the site's pages call as they render, over HTTP: GET " in shown
    assert "If it fails without refusing, fetch the 10 product pages the sitemaps list" in shown
    mapping = "name <- title, price, currency <- price.currency, rating <- rating.average, url"
    assert f"Read name, price, currency, rating, url from each record ({mapping})" in shown
    assert site.estimate.pages == 3 and site.estimate.exact and site.estimate.browser_pages == 0
    assert site.estimate.requests == 4  # three pages of the API, and robots.txt
    assert site.sample["api"]["passing"] == 4  # of the four laptops its first page held, four under $800
    explained = plan.explain()
    assert "API pages: 3, as the API says (4 records a page, 10 records)" in explained
    assert "ms per API page to read (measured on the page's answer)" in explained
    # run: the API's three pages over HTTP, no product page, no browser
    Shop.hits.clear()
    result = plan.run()
    assert sorted(r["name"] for r in result.records) == [f"Laptop {n}" for n in range(1, 6)]  # under $800
    first = next(r for r in result.records if r["name"] == "Laptop 1")
    assert first["price"] == 550 and first["currency"] == "USD" and first["rating"] == 3.6
    assert first["url"] == shop + "/p/1"  # as the page would link it
    assert (result.counts["api_pages"], result.counts["api_records"], result.counts["browser_pages"]) == (3, 10, 0)
    assert (Shop.hits["api"], Shop.hits["product"]) == (3, 0)
    assert "10 record(s) from 3 page(s) of the site's API\n3 page(s) fetched, 0 error(s)" in result.summary()
    # a saved plan keeps its API
    plan.save(tmp_path / "plan.json")
    again = GoalPlan.load(tmp_path / "plan.json")
    assert again.sites[0].api == site.api
    assert len(again.run().records) == 5


@pytest.mark.browser
def test_robots_txt_keeps_the_plan_off_the_api(shop, modes) -> None:
    modes["robots"] = "no-api"
    plan = plan_goal(parse_goal("laptops with name and price", sites=[shop]), sample=6, log_level=None)
    site = plan.sites[0]
    assert site.api is None and site.strategy == "sitemap"
    assert any(w.startswith("robots.txt forbids the site's API (GET ") for w in site.warnings)


def test_asked_for_the_pages_the_plan_does_not_look(shop, modes) -> None:
    plan = plan_goal(parse_goal("laptops with name and price", sites=[shop]), sample=6, log_level=None, api=False)
    assert plan.sites[0].api is None and Shop.hits["api"] == 0  # nothing rendered, no API asked


# --------------------------------------------------------------------------- #
# running: the API, and what happens when it fails
# --------------------------------------------------------------------------- #
def test_a_run_reads_every_page_of_the_api(shop, modes) -> None:
    result = _api_plan(shop).run(log_level=None)
    assert sorted(r["name"] for r in result.records) == sorted(f"Laptop {n}" for n in range(1, TOTAL + 1))
    assert (result.counts["api_pages"], result.counts["records"], result.notes) == (3, 10, [])
    assert (Shop.hits["api"], Shop.hits["product"], Shop.hits["/sitemap.xml"]) == (3, 0, 0)
    elsewhere = _api_plan(shop)  # an API on a host of its own
    elsewhere.sites[0].api["url"] = elsewhere.sites[0].api["url"].replace("127.0.0.1", "localhost")
    Shop.hits.clear()
    assert len(elsewhere.run(log_level=None).records) == TOTAL and Shop.hits["api"] == 3
    limited = parse_goal("the first 5 laptops with name and price", sites=[shop])
    plan = _api_plan(shop)
    plan.goal = limited
    Shop.hits.clear()
    assert len(plan.run(log_level=None).records) == 5
    assert Shop.hits["api"] == 2  # a limit stops the API's pages too


@pytest.mark.parametrize(
    ("api", "reason"),
    [("404", "HTTP 404"), ("html", "its answer is not JSON")],
)
def test_an_api_that_fails_leaves_the_site_to_its_pages(shop, modes, api, reason) -> None:
    modes["api"] = api
    result = _api_plan(shop).run(log_level=None)
    assert len(result.records) == TOTAL and result.counts["record_pages"] == TOTAL
    assert Shop.hits["product"] == TOTAL and Shop.hits["api"] == 1
    assert len(result.notes) == 1 and result.notes[0].startswith(f"{shop}: its API could not be used (")
    assert reason in result.notes[0] and result.notes[0].endswith("its pages were read instead")
    assert "note: " + result.notes[0] in result.summary()


@pytest.mark.parametrize(
    ("api", "how"),
    [("403", "HTTP 403"), ("429", "HTTP 429"), ("bot", "a bot check")],
)
def test_an_api_that_refuses_is_not_asked_another_way(shop, modes, api, how) -> None:
    modes["api"] = api
    result = _api_plan(shop).run(log_level=None, retries=0)
    assert result.records == [] and result.notes == [f"{shop}: its API refused ({how}): it is not asked another way"]
    assert Shop.hits["product"] == 0 and Shop.hits["/sitemap.xml"] == 0  # the site's answer stands


def test_an_api_that_fails_on_a_later_page_says_what_was_read(shop, modes) -> None:
    modes["api"] = "fails-on-2"
    result = _api_plan(shop).run(log_level=None, retries=0)
    assert len(result.records) == PER_PAGE and Shop.hits["product"] == 0  # what it gave is kept
    assert result.notes[0] == f"{shop}: its API stopped at page 2 (HTTP 500)"
    assert result.notes[1] == f"{shop}: its API said it has {TOTAL} records; {PER_PAGE} were read"


def test_robots_txt_forbidding_the_api_leaves_the_site_to_its_pages(shop, modes) -> None:
    modes["robots"] = "no-api"  # since the plan was made
    result = _api_plan(shop).run(log_level=None)
    assert len(result.records) == TOTAL and Shop.hits["api"] == 0
    assert result.notes == [f"{shop}: its API could not be used (robots.txt forbids it): its pages were read instead"]


def test_a_recorded_run_replays_its_api(shop, modes, tmp_path) -> None:
    workspace = str(tmp_path / "runs")
    result = _api_plan(shop).run(str(tmp_path / "out.jsonl"), log_level=None, record=True, run_registry=workspace)
    assert result.counts["api_pages"] == 3 and result.crawl is not None
    modes["api"] = "403"  # the replay asks the recording, not the site
    again = replay(result.crawl.run_id, registry=workspace)
    assert again.same and Shop.hits["api"] == 3


def test_api_records_say_where_they_came_from(shop, modes) -> None:
    result = _api_plan(shop).run(log_level=None, provenance=True)
    first = next(r for r in result.records if r["name"] == "Laptop 1")
    where = first["_provenance"]
    assert where["url"] == f"{shop}/api/laptops?page=1&per_page={PER_PAGE}" and "fetched_at" in where
    assert where["extractor"] == "product@1" and where["api"]["page"] == 1 and where["api"]["records"] == "items[]"
    assert where["api"]["fields"]["price"] == "price" and where["api"]["method"] == "GET"
    last = next(r for r in result.records if r["name"] == f"Laptop {TOTAL}")
    assert last["_provenance"]["api"]["page"] == 3 and last["_provenance"]["url"].endswith("page=3&per_page=4")
    assert all("_provenance" not in r for r in _api_plan(shop).run(log_level=None).records)  # (only when asked)


def test_the_pages_when_asked(shop, modes, tmp_path) -> None:
    result = _api_plan(shop).run(log_level=None, use_api=False)
    assert len(result.records) == TOTAL and Shop.hits["api"] == 0 and result.notes == []
    wg = WinterGrab(log_level=None)
    assert len(wg.run(_api_plan(shop), api=False).records) == TOTAL and Shop.hits["api"] == 0
    assert wg.plan("laptops with name and price", sites=[shop], sample=6, api=False).sites[0].api is None
    path = tmp_path / "plan.json"
    _api_plan(shop).save(path)
    Shop.hits.clear()
    assert main(["-q", "goal", "--plan", str(path), "--no-api", "-o", str(tmp_path / "out.jsonl")]) == 0
    assert len((tmp_path / "out.jsonl").read_text().splitlines()) == TOTAL and Shop.hits["api"] == 0


# --------------------------------------------------------------------------- #
# finding the API among recorded calls
# --------------------------------------------------------------------------- #
def _call(url: str, answer: object, *, method: str = "GET", request: object = None) -> SimpleNamespace:
    return SimpleNamespace(
        url=url,
        method=method,
        status=200,
        headers={"content-type": "application/json"},
        body=json.dumps(answer).encode(),
        request_body=json.dumps(request) if request is not None else None,
    )


def _page(url: str, *calls: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(url=url, captured=list(calls))


def _answer(page: int) -> dict:
    items = [_laptop(n) for n in range((page - 1) * PER_PAGE + 1, min(page * PER_PAGE, TOTAL) + 1)]
    return {"items": items, "page": page, "total_pages": 3}


def test_the_api_that_holds_the_goals_records() -> None:
    goal = parse_goal("laptops with name, price and rating", sites=["https://shop.example"])
    tracking = _call("https://shop.example/api/track?page=1", {"ok": True})
    laptops = _call("https://shop.example/api/laptops?page=1&per_page=4", _answer(1))
    source = find_api(goal, [_page("https://shop.example/catalog", tracking, laptops)])
    assert source is not None and source.url.endswith("/api/laptops?page=1&per_page=4")
    assert source.describe() == (
        "GET shop.example/api/laptops?page&per_page: 4 record(s) a page (pages by page: 3 in all)"
    )
    assert source.mapping() == "name <- title, price, currency <- price.currency, rating <- rating.average, url"
    assert source.examples[0] == {
        "name": "Laptop 1", "price": 550.0, "currency": "USD", "rating": 3.6, "url": "https://shop.example/p/1",
    }  # fmt: skip
    assert "examples" not in source.to_dict() and ApiSource.from_dict(source.to_dict()) == source
    schema = goal.schema()
    records = records_of(source, _answer(3), schema)
    assert [r["name"] for r in records] == ["Laptop 9", "Laptop 10"]
    assert next_page(source, source.url, None, _answer(1), 4) == (
        "https://shop.example/api/laptops?page=2&per_page=4",
        None,
    )
    assert next_page(source, "https://shop.example/api/laptops?page=3&per_page=4", None, _answer(3), 2) is None


def test_a_graphql_query_is_followed_by_its_cursor() -> None:
    goal = parse_goal("laptops with name and price", sites=["https://shop.example"])
    query = (
        "query Laptops($first: Int, $after: String) { laptops(first: $first, after: $after) "
        "{ edges { node { title price { amount currency } } } pageInfo { hasNextPage endCursor } } }"
    )
    request = {"operationName": "Laptops", "query": query, "variables": {"first": 2, "after": None}}
    edges = [{"node": {"title": f"Laptop {n}", "price": {"amount": 600 + n, "currency": "EUR"}}} for n in (1, 2)]
    answer = {"data": {"laptops": {"edges": edges, "pageInfo": {"hasNextPage": True, "endCursor": "c2"}}}}
    call = _call("https://shop.example/graphql", answer, method="POST", request=request)
    source = find_api(goal, [_page("https://shop.example/", call)])
    assert source is not None and (source.method, source.graphql) == ("POST", "query Laptops")
    assert source.path == "data.laptops.edges[].node"
    assert source.fields == {"name": "title", "price": "price", "currency": "price.currency"}
    url, body = next_page(source, source.url, source.body, answer, 2) or ("", None)
    assert url == "https://shop.example/graphql" and body["variables"] == {"first": 2, "after": "c2"}
    assert request["variables"]["after"] is None  # the page's own body is left as it was
    assert items_at({"data": {"laptops": {"edges": [{"node": {"a": 1}}]}}}, source.path) == [{"a": 1}]


def test_apis_that_are_not_used_and_why() -> None:
    goal = parse_goal("laptops with name and price", sites=["https://shop.example"])
    page = "https://shop.example/"

    def why(*calls: SimpleNamespace) -> list[str]:
        notes: list[str] = []
        assert find_api(goal, [_page(page, *calls)], notes=notes) is None
        return notes

    keyed = _call("https://shop.example/api/laptops?page=1&api_key=s3cret", _answer(1))
    assert why(keyed) == [
        "the pages call GET shop.example/api/laptops?api_key&page, which holds product records, with a credential "
        "(api_key): it is not used"
    ]
    token = _call("https://shop.example/graphql", {"data": {"laptops": _answer(1)["items"]}}, method="POST",
                  request={"query": "query Laptops { laptops { title } }", "variables": {"accessToken": "t0k"}})  # fmt: skip
    assert why(token)[0].endswith("with a credential (accessToken): it is not used")
    search = _call("https://shop.example/api/search", _answer(1), method="POST", request={"q": "laptop"})
    assert why(search) == [
        "the pages call POST shop.example/api/search, which holds product records; asking it may change something "
        "(a POST that is not a GraphQL query), so it is not used"
    ]
    endless = _call("https://shop.example/api/laptops", {"items": _answer(1)["items"]})  # no page to ask for next
    assert why(endless)[0].endswith("but how to ask for its next page cannot be told: it is not used")
    whole = _call("https://shop.example/api/laptops", {"items": _answer(1)["items"], "total": 4})
    assert find_api(goal, [_page(page, whole)]).pages == 1  # one answer holds them all
    nameless = _call("https://shop.example/api/prices?page=1", {"items": [{"sku": n, "price": 5} for n in range(4)]})
    assert why(nameless) == []  # no record names: not the goal's records
