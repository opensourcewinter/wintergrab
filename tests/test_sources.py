"""Where a page's data is: the records in its HTML, JSON-LD, embedded JSON and API calls; pagination; GraphQL."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

import wintergrab as wg
from wintergrab.cli import main
from wintergrab.intel.sources import api_calls, data_sources, json_collections, pagination_of

PAGE = (Path(__file__).parent / "data" / "sources" / "catalog.html").read_bytes()


def _product(n: int) -> dict:
    return {"id": n, "name": f"Parka {n}", "price": {"amount": 100 + n, "currency": "EUR"}, "url": f"/p/{n}"}


class Shop(BaseHTTPRequestHandler):
    """The catalog, its products API (three a page) and a GraphQL endpoint."""

    def log_message(self, *args) -> None:
        pass

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path == "/late":  # a page whose call answers well after its load event
            html = b"<html><body><p id='x'>...</p><script>fetch('/api/products?page=1&delay=1')</script></body></html>"
            return self._send(html, "text/html")
        if parts.path == "/api/products":
            time.sleep(float(parse_qs(parts.query).get("delay", ["0"])[0]))
            page = int(parse_qs(parts.query).get("page", ["1"])[0])
            items = [_product(n) for n in range(page * 3 - 2, page * 3 + 1)]
            answer = {"items": items, "page": page, "total_pages": 2, "total": 6}
            return self._send(json.dumps(answer).encode(), "application/json")
        return self._send(PAGE, "text/html; charset=utf-8")

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if request.get("operationName") == "Reviews":
            edges = [{"node": {"id": f"r{n}", "author": f"A{n}", "stars": 5 - n, "text": f"Warm {n}"}, "cursor": f"r{n}"}
                     for n in (1, 2)]  # fmt: skip
            answer = {"data": {"reviews": {"edges": edges, "pageInfo": {"hasNextPage": True, "endCursor": "r2"}}}}
        else:
            answer = {"data": {"trackView": {"ok": True}}}
        self._send(json.dumps(answer).encode(), "application/json")


@pytest.fixture(scope="module")
def shop():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Shop)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_lists_of_records_in_json() -> None:
    answer = {
        "data": {
            "products": {
                "edges": [
                    {"node": {"id": n, "name": f"P{n}", "images": [{"src": f"/i/{n}a.jpg"}] * 2}} for n in (1, 2, 3)
                ],
                "pageInfo": {"hasNextPage": False},
            }
        },
        "tags": ["a", "b", "c"],  # no records
    }
    found = json_collections(answer)
    assert [(c.path, c.count, c.fields) for c in found] == [
        ("data.products.edges[].node", 3, ["id", "name", "images"]),  # a GraphQL connection, read through
        ("data.products.edges[].node.images[]", 6, ["src"]),  # every record's images together
    ]
    assert found[0].types == {"id": "integer", "name": "string", "images": "object[]"}
    assert found[1].types == {"src": "string"}  # a path holds "0a", but is no quantity
    assert found[0].to_dict()["sample"] == {"id": 1, "name": "P1", "images": "[2 item(s)]"}
    assert found[0].schema("products").fields[0].name == "id"
    # an app's normalized state: records keyed by id, by type; lists of pointers to them are no records
    state = {"ROOT_QUERY": {"featured": [{"__ref": "Product:1"}, {"__ref": "Product:2"}]}}
    state |= {f"Product:{n}": {"__typename": "Product", "id": n, "name": f"P{n}"} for n in range(4)}
    assert [(c.path, c.count) for c in json_collections({"__APOLLO_STATE__": state})] == [
        ("__APOLLO_STATE__{Product}", 4)
    ]
    # a record whose fields are objects is not a map of records
    money = {n: {"amount": 1, "currency": "EUR"} for n in ("price", "shipping", "tax")}
    assert json_collections(money) == []
    assert json_collections([{"a": 1}, {"a": 2}])[0].path == "[]"  # the document is the list
    assert json_collections([{"a": 1}], min_records=2) == []


@pytest.mark.parametrize(
    ("url", "answer", "request_", "expected"),
    [
        ("https://a.example/items?page=2&limit=10", {"items": [], "total": 45}, None,
         {"kind": "page", "parameter": "page", "value": 2, "size": 10, "next": 3, "total": 45, "pages": 5}),
        ("https://a.example/items?page=5&limit=10", {"total": 45}, None,
         {"kind": "page", "parameter": "page", "value": 5, "size": 10, "total": 45, "pages": 5}),  # the last
        ("https://a.example/items?offset=40&limit=20", {"total": 55}, None,
         {"kind": "offset", "parameter": "offset", "value": 40, "size": 20, "total": 55, "pages": 3}),
        ("https://a.example/items?start=0", {"rows": []}, None,
         {"kind": "offset", "parameter": "start", "value": 0, "next": 25}),  # a page is the records it holds
        ("https://a.example/graphql", {"data": {"x": {"pageInfo": {"hasNextPage": True, "endCursor": "c9"}}}},
         {"first": 10, "after": None}, {"kind": "cursor", "parameter": "after", "size": 10, "next": "c9", "more": True}),
        ("https://a.example/graphql", {"data": {"x": {"pageInfo": {"hasNextPage": False, "endCursor": "c9"}}}},
         {"first": 10, "after": "c8"}, {"kind": "cursor", "parameter": "after", "value": "c8", "size": 10, "more": False}),
        ("https://a.example/api/items/?page=2", {"count": 97, "next": "https://a.example/api/items/?page=3",
                                                 "previous": None, "results": []}, None,
         {"kind": "page", "parameter": "page", "value": 2, "next": 3, "next_url": "https://a.example/api/items/?page=3",
          "total": 97}),  # Django REST framework: count is the total beside next and previous
        ("https://a.example/orders", {"_links": {"next": {"href": "/orders?cursor=abc"}}}, None,
         {"kind": "next", "next_url": "https://a.example/orders?cursor=abc"}),  # HAL
        ("https://a.example/items", {"items": [{"a": 1}]}, None, None),
        ("https://a.example/items", {"items": [], "total": 25}, None,
         {"kind": "page", "value": 1, "total": 25, "pages": 1}),  # the one page: it holds all 25
        ("https://a.example/items", {"items": [], "has_more": False}, None,
         {"kind": "page", "value": 1, "pages": 1, "more": False}),
        ("https://a.example/items", {"items": [], "total": 26}, None, None),  # more, but how to ask is not said
    ],
)  # fmt: skip
def test_how_an_apis_pages_go(url, answer, request_, expected) -> None:
    found = pagination_of(url, answer, request=request_, records=25)
    assert (found.to_dict() if found else None) == expected


def _call(url: str, answer: object, *, method: str = "GET", request: object = None) -> SimpleNamespace:
    return SimpleNamespace(
        url=url,
        method=method,
        status=200,
        headers={"content-type": "application/json"},
        body=json.dumps(answer).encode(),
        request_body=json.dumps(request) if request is not None else None,
    )


def test_calls_a_page_made() -> None:
    rows = {"rows": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]}
    calls = api_calls([
        _call("https://s.example/api/list?start=0&size=2", rows),
        _call("https://s.example/graphql", {"data": {"cart": {"ok": True}}}, method="POST",
              request={"operationName": "AddToCart", "query": "mutation AddToCart($id: ID!) { add(id: $id) { ok } }"}),
        _call("https://s.example/api/list?start=2&size=2", rows),
        _call("https://s.example/api/list?start=4&size=2", rows),
        _call("https://s.example/graphql?operationName=Menu&extensions=%7B%7D", {"data": {"menu": []}}),
        _call("https://s.example/graphql", [{"data": {}}, {"data": {}}], method="POST",
              request=[{"query": "{ a }"}, {"operationName": "B", "query": "query B { b }"}]),
    ])  # fmt: skip
    assert [(c.method, c.template, c.calls, c.graphql) for c in calls] == [
        ("GET", "s.example/api/list?size&start", 3, None),  # three pages of one list: one API
        ("POST", "s.example/graphql", 1, "mutation AddToCart"),
        ("GET", "s.example/graphql?extensions&operationname", 1, "query Menu"),  # a persisted query, by GET
        ("POST", "s.example/graphql", 1, "query, query B"),  # a batch of two
    ]
    listing = calls[0]
    assert (listing.pagination.kind, listing.pagination.parameter, listing.seen) == ("offset", "start", [0, 2, 4])
    assert listing.describe() == (
        "GET s.example/api/list?size&start: 2 record(s) at rows[] (id, name); pages by offset start: at 0, next 2; "
        "called 3 times (start 0, 2, 4)"
    )
    assert calls[1].mutation and not calls[0].mutation


def test_the_sources_of_a_page_over_http(shop) -> None:
    sources = data_sources(wg.get(shop + "/catalog"))
    assert [(g.count, g.fields) for g in sources.html][:1] == [(6, ["title", "url", "price"])]
    assert sources.tables == [{"rows": 3, "columns": ["Size", "Chest"]}]
    assert sources.json_ld == {"Organization": 1, "ItemList": 1}
    assert sources.meta == ["og:title", "og:type", "title", "description", "language"]
    [products] = sources.embedded["__NEXT_DATA__"]
    assert (products.path, products.count) == ("props.pageProps.products[]", 6)
    assert products.types["price"] == "number" and products.types["url"] == "url"
    # over HTTP, no call is recorded: the endpoints its scripts name are listed, not requested
    assert not sources.recorded and sources.api == []
    assert [endpoint.split("/", 3)[3] for _, endpoint in sources.endpoints] == ["api/products?limit=&page=", "graphql"]
    assert sources.richest() == ("embedded", "__NEXT_DATA__ props.pageProps.products[]", 6, 7)
    text = sources.describe()
    assert "embedded JSON  __NEXT_DATA__: 6 record(s) at props.pageProps.products[]" in text
    assert "API calls      not recorded (a browser fetch with capture=True records them)" in text
    assert json.loads(json.dumps(sources.to_dict()))["richest"]["records"] == 6


def test_an_api_answer_is_a_source_itself(shop, capsys) -> None:
    assert main(["-q", "get", shop + "/api/products?page=2&limit=3", "--sources"]) == 0
    out = capsys.readouterr().out
    assert "JSON           3 record(s) at items[] (id, name, price, url)" in out
    assert "pages by page: 2 of 2, 6 record(s) in all, the last page" in out
    assert main(["-q", "get", shop + "/api/products?page=1&limit=3", "--sources", "-f", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["pagination"] == {"kind": "page", "parameter": "page", "value": 1, "size": 3, "next": 2, "total": 6,
                                    "pages": 2}  # fmt: skip


@pytest.mark.browser
def test_the_calls_a_page_makes_as_it_renders(shop, capsys) -> None:
    from wintergrab.fetchers.browser import BrowserFetcher

    with BrowserFetcher() as browser:
        page = browser.get(shop + "/catalog", capture=True, wait_until="networkidle")
        # a call still on its way at the load event is waited for, not missed
        late = browser.get(shop + "/late", capture=True)
        assert [c.url.rsplit("/", 1)[1] for c in late.captured] == ["products?page=1&delay=1"]
    sources = data_sources(page)
    assert sources.recorded
    calls = {call.graphql or call.template.split("/", 1)[1]: call for call in sources.api}
    products = calls["api/products?limit&page"]
    assert (products.calls, products.seen, products.collections[0].count) == (2, [1, 2], 3)
    assert products.pagination.to_dict() == {"kind": "page", "parameter": "page", "value": 1, "size": 3, "next": 2,
                                             "total": 6, "pages": 2}  # fmt: skip
    reviews = calls["query Reviews"]
    assert (reviews.collections[0].path, reviews.pagination.next) == ("data.reviews.edges[].node", "r2")
    assert calls["mutation TrackView"].mutation  # a call that changes data is no source
    assert sources.richest().kind == "embedded"
    assert main(["-q", "get", shop + "/catalog", "--browser", "--sources"]) == 0
    out = capsys.readouterr().out
    assert "API calls      3 recorded as the page rendered" in out
    assert "POST 127.0.0.1/graphql, query Reviews: 2 record(s) at data.reviews.edges[].node" in out
    assert "called 2 times (page 1, 2)" in out
    # a site's profile says what its APIs answer
    assert main(["-q", "inspect", shop + "/catalog", "--browser", "--pages", "1", "--no-robots", "--no-sitemaps"]) == 0
    out = capsys.readouterr().out
    assert "api/products?limit=&page=  (captured, 200; records: 3 at items[]; pages by page)" in out
    assert "graphql  (captured, 200; query Reviews, mutation TrackView; records: 2 at data.reviews.edges[].node" in out
