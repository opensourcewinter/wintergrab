"""The visual scraper builder: what it proposes, the page it shows, its server, and its page in a browser."""

from __future__ import annotations

import http.client
import json
import re
import threading
from pathlib import Path

import pytest

from wintergrab import Fetcher
from wintergrab.builder import BuilderSession, serve
from wintergrab.cli import main
from wintergrab.data import Schema
from wintergrab.errors import ConfigurationError
from wintergrab.fetchers.response import Response


def _page(site, path: str) -> Response:
    with Fetcher() as fetcher:
        return fetcher.get(site.url + path)


def _number(session: BuilderSession, test) -> int:
    return next(i for i, el in enumerate(session.elements) if test(el))


def test_the_page_is_shown_without_scripts(tmp_path) -> None:
    html = (
        "<html><head><meta http-equiv='refresh' content='0;url=https://elsewhere.example/'>"
        "<script>steal()</script><base href='https://elsewhere.example/'></head>"
        "<body onload='steal()'><h1 onclick='steal()'>Title</h1><a href='javascript:steal()'>x</a> tail"
        "<iframe src='https://ads.example/'></iframe><svg><script>steal()</script></svg>"
        "<img src='/i.png' onerror='steal()'></body></html>"
    )
    page = Response("https://shop.example/p/1", headers={"content-type": "text/html"}, body=html.encode())
    session = BuilderSession(page, tmp_path / "s.json")
    shown = session.view_html()
    assert "steal" not in shown and "<script" not in shown and "<iframe" not in shown and "refresh" not in shown
    assert shown.count("<base ") == 1 and '<base href="https://shop.example/p/1">' in shown
    assert "tail" in shown and "/i.png" in shown  # the text after what is removed stays, and so do images
    h1 = _number(session, lambda el: el.tag == "h1")
    assert f'<h1 data-wg="{h1}">Title</h1>' in shown  # numbered like the tree the builder reads
    assert session.element(h1).text == "Title" and session.element(h1).get("data-wg") is None
    with pytest.raises(ConfigurationError, match="no element"):
        session.element(10_000)


def test_what_it_proposes(site, tmp_path) -> None:
    listing = BuilderSession(_page(site, "/books/"), tmp_path / "books.json", name="book")
    price = _number(listing, lambda el: el.get("class") == "price_color")
    card = listing.card(price)
    assert card["container"] == "article.product_pod" and card["count"] == 4 and len(card["ids"]) == 4
    assert {f["name"] for f in card["suggested"]} >= {"title", "price", "url"}
    title_link = _number(listing, lambda el: el.tag == "a" and el.getparent().tag == "h3")
    title = listing.field(title_link, card["container"])  # in a card: a selector for every card
    assert title["name"] == "title" and title["read"] == "attribute title"  # the link's text is cut short
    [candidate] = title["candidates"]
    assert candidate["selector"] == "h3 a::attr(title)"
    assert candidate["values"][:2] == ["Book number 1", "Book number 2"]
    assert listing.field(price, card["container"])["type"] == "money"
    with pytest.raises(ConfigurationError, match="not inside a card"):
        listing.field(_number(listing, lambda el: el.tag == "nav"), card["container"])
    following = listing.next_page(_number(listing, lambda el: el.tag == "a" and el.text == "next"))
    assert following["selector"] == "li.next > a" and following["url"].endswith("/books/catalogue/page-2.html")
    assert following["detected"] == following["url"]
    with pytest.raises(ConfigurationError, match="not a link"):
        listing.next_page(price)

    detail = BuilderSession(_page(site, "/books/catalogue/book-3/index.html"), tmp_path / "book.json")
    heading = detail.field(_number(detail, lambda el: el.tag == "h1"))
    assert heading["name"] == "name" and heading["candidates"][0] == {
        "selector": "h1", "matches": 1, "values": ["Book number 3"]}  # fmt: skip
    rating = detail.field(_number(detail, lambda el: "star-rating" in (el.get("class") or "")))
    assert rating["type"] == "rating" and rating["candidates"][0]["selector"].endswith("::attr(class)")
    specs = detail.table(_number(detail, lambda el: el.tag == "td"))  # a record's properties, by their labels
    assert specs["kind"] == "properties"
    assert [(f["name"], f["selector"]) for f in specs["fields"]] == [
        ("upc", '//tr[th[normalize-space()="UPC"]]/td'),
        ("product_type", '//tr[th[normalize-space()="Product Type"]]/td'),
    ]
    with pytest.raises(ConfigurationError, match="not in a table"):
        detail.table(_number(detail, lambda el: el.tag == "h1"))


def test_tables_of_records(tmp_path) -> None:
    rows = "".join(f"<tr><td>Item {i}</td><td>${i}.00</td><td>{i * 3}</td></tr>" for i in range(1, 4))
    html = f"<table class='prices'><thead><tr><th>Name</th><th>Unit price</th><th>Stock</th></tr></thead>{rows}</table>"
    page = Response("https://shop.example/prices", headers={"content-type": "text/html"}, body=html.encode())
    session = BuilderSession(page, tmp_path / "prices.json")
    table = session.table(_number(session, lambda el: el.tag == "td"))
    assert table["kind"] == "records" and table["count"] == 3
    assert [(f["name"], f["type"], f["selector"]) for f in table["fields"]] == [
        ("name", "string", "td:nth-of-type(1)"),
        ("unit_price", "money", "td:nth-of-type(2)"),
        ("stock", "number", "td:nth-of-type(3)"),
    ]
    spec = {"name": "price", "container": table["container"],
            "fields": {f["name"]: {"type": f["type"], "selectors": [f["selector"]]} for f in table["fields"]}}  # fmt: skip
    result = session.test(spec)
    assert result["records"] == 3 and result["filled"] == {"name": 3, "unit_price": 3, "stock": 3}
    assert result["rows"][1]["unit_price"]["value"] == {"amount": 2, "currency": "USD"}


def test_testing_and_saving(site, tmp_path) -> None:
    output = tmp_path / "out" / "books.schema.json"
    session = BuilderSession(_page(site, "/books/"), output, name="book")
    spec = {"name": "book", "container": "article.product_pod", "next_page": "li.next > a",
            "fields": {"title": {"type": "string", "selectors": ["h3 a::attr(title)"]},
                       "price": {"type": "money", "selectors": ["p.price_color"]}}}  # fmt: skip
    result = session.test(spec)
    assert result["records"] == 4 and result["filled"] == {"title": 4, "price": 4}
    assert result["next"].endswith("/books/catalogue/page-2.html")
    assert result["rows"][0]["price"]["method"] == "selector"
    saved = session.save(spec)
    assert saved["path"] == str(output) and saved["command"].startswith("wintergrab crawl")
    schema = Schema.load(output)
    assert schema.container == "article.product_pod" and schema.next_page == "li.next > a"
    with pytest.raises(ConfigurationError, match="not a valid schema"):
        session.save({"fields": {"x": {"type": "no-such-type"}}})
    assert Schema.load(output).names == ["title", "price"]  # left as it was
    # a file already there is carried on with
    again = BuilderSession(_page(site, "/books/"), output)
    assert again.state()["spec"]["container"] == "article.product_pod"
    (tmp_path / "junk.json").write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="not a schema to carry on with"):
        BuilderSession(_page(site, "/books/"), tmp_path / "junk.json")
    # the schema crawls the listing: its cards, and its next pages only
    records = tmp_path / "books.jsonl"
    assert main(["-q", "crawl", site.url + "/books/", "--extract", str(output), "-o", str(records)]) == 0
    rows = [json.loads(line) for line in records.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 12 and rows[-1]["title"] == "Book number 12"


@pytest.fixture
def builder(site, tmp_path):
    session = BuilderSession(_page(site, "/books/"), tmp_path / "books.schema.json", name="book")
    server = serve(session, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _request(server, method: str, path: str, body: object = None, **headers: str) -> tuple[int, dict, bytes]:
    host, port = server.server_address[:2]
    connection = http.client.HTTPConnection(host, port, timeout=10)
    data = json.dumps(body).encode() if body is not None else None
    connection.request(method, path, body=data, headers=headers)
    response = connection.getresponse()
    result = (response.status, dict(response.getheaders()), response.read())
    connection.close()
    return result


def test_the_server(builder) -> None:
    token = builder.token
    ok = {"Content-Type": "application/json", "X-Wintergrab-Token": token}
    status, headers, body = _request(builder, "GET", "/")
    assert status == 200 and f'content="{token}"' in body.decode()
    assert "script-src 'self'" in headers["Content-Security-Policy"] and headers["X-Frame-Options"] == "DENY"
    status, headers, body = _request(builder, "GET", "/page")
    assert status == 200 and b"<script" not in body
    policy = headers["Content-Security-Policy"]
    assert "sandbox allow-same-origin" in policy and "script-src" not in policy  # no script at all
    assert _request(builder, "GET", "/builder.js")[0] == 200 and _request(builder, "GET", "/builder.css")[0] == 200
    assert json.loads(_request(builder, "GET", "/api/state")[2])["spec"]["name"] == "book"
    assert _request(builder, "GET", "/nothing")[0] == 404
    session = builder.session
    price = _number(session, lambda el: el.get("class") == "price_color")
    status, _, body = _request(builder, "POST", "/api/card", {"id": price}, **ok)
    assert status == 200 and json.loads(body)["container"] == "article.product_pod"
    # what other pages, and other hosts, cannot do
    assert _request(builder, "POST", "/api/card", {"id": price}, **{"Content-Type": "application/json"})[0] == 403
    assert _request(builder, "POST", "/api/card", {"id": price}, **{**ok, "X-Wintergrab-Token": "guess"})[0] == 403
    assert _request(builder, "POST", "/api/card", {"id": price}, **{**ok, "Content-Type": "text/plain"})[0] == 415
    assert _request(builder, "POST", "/api/card", {"id": price}, **ok, Origin="https://evil.example")[0] == 403
    assert _request(builder, "GET", "/", Host="evil.example")[0] == 403  # a name pointed at this machine
    assert _request(builder, "PUT", "/api/save", {}, **ok)[0] == 405
    status, _, body = _request(builder, "POST", "/api/field", {"id": "x"}, **ok)
    assert status == 400 and "no element" in json.loads(body)["error"]
    assert _request(builder, "POST", "/api/nothing", {}, **ok)[0] == 404
    status, _, body = _request(builder, "POST", "/api/save", {"spec": {"name": "b", "fields": {"t": "string"}}}, **ok)
    assert status == 200 and Path(json.loads(body)["path"]) == session.output and session.output.exists()


@pytest.mark.browser
def test_the_builder_in_a_browser(builder) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from wintergrab.fetchers.browser import _discover_chromium

    with sync_api.sync_playwright() as playwright:
        browser = None
        for path in [None, *_discover_chromium()]:
            try:
                browser = playwright.chromium.launch(**({"executable_path": path} if path else {}))
                break
            except Exception:
                continue
        if browser is None:
            pytest.skip("no Chromium to run")
        page = browser.new_page(viewport={"width": 1300, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        expect = sync_api.expect
        page.goto(builder.url)
        expect(page.locator("#json")).to_have_value(re.compile('"name": "book"'))
        shown = page.frame_locator("#page")
        page.check('input[name="mode"][value="card"]')
        shown.locator("article.product_pod p.price_color").first.click()
        expect(page.locator("#container")).to_have_value("article.product_pod")
        shown.locator("article.product_pod h3 a").nth(1).click()  # a field, in a card: the link does not open
        expect(page.locator("#proposal")).to_be_visible()
        expect(page.locator("#p-name")).to_have_value("title")
        page.click("#p-add")
        shown.locator("article.product_pod p.price_color").nth(2).click()
        expect(page.locator("#p-type")).to_have_value("money")
        page.click("#p-add")
        page.check('input[name="mode"][value="next"]')
        shown.locator("li.next a").click()
        expect(page.locator("#next")).to_have_value("li.next > a")
        page.click("#test")
        expect(page.locator("#summary")).to_contain_text("4 record(s)")
        expect(page.locator("#records thead")).to_contain_text("price (4/4)")
        page.click("#save")
        expect(page.locator("#message")).to_contain_text("Saved to")
        assert page.frames[1].url.endswith("/page")  # the clicks opened nothing
        assert not errors
        browser.close()
    schema = Schema.load(builder.session.output)
    assert schema.container == "article.product_pod" and schema.next_page == "li.next > a"
    assert schema["title"].selectors == ["h3 a::attr(title)"] and schema["price"].type == "money"


def test_the_builder_on_a_phone(builder) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from wintergrab.fetchers.browser import _discover_chromium

    with sync_api.sync_playwright() as playwright:
        browser = None
        for path in [None, *_discover_chromium()]:
            try:
                browser = playwright.chromium.launch(**({"executable_path": path} if path else {}))
                break
            except Exception:
                continue
        if browser is None:
            pytest.skip("no Chromium to run")
        context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        page = context.new_page()
        expect = sync_api.expect
        page.goto(builder.url)
        shown = page.frame_locator("#page")
        page.locator('input[name="mode"][value="card"]').tap()
        shown.locator("article.product_pod p.price_color").first.tap()
        expect(page.locator("#container")).to_have_value("article.product_pod")
        shown.locator("article.product_pod h3 a").nth(1).tap()
        expect(page.locator("#proposal")).to_be_visible()
        view, proposal = page.locator(".view").bounding_box(), page.locator("#proposal").bounding_box()
        assert view is not None and proposal is not None
        assert view["width"] == 390 and 250 < view["height"] < 500  # the page across the screen, on top
        assert proposal["y"] >= view["y"] + view["height"]  # what was tapped is proposed beneath it, in sight
        assert page.evaluate("document.documentElement.scrollWidth") == 390  # nothing wider than the screen
        browser.close()


def test_the_address_for_other_devices() -> None:
    from wintergrab.cli import other_devices_url

    assert other_devices_url("127.0.0.1", 8711) is None  # this machine only: nothing to open elsewhere
    url = other_devices_url("0.0.0.0", 8711)  # this machine's address on its network, when it has one
    assert url is None or re.fullmatch(r"http://(?!127\.)\d+\.\d+\.\d+\.\d+:8711/", url)


def test_the_build_command(site, tmp_path, monkeypatch, capsys) -> None:
    from wintergrab import builder

    monkeypatch.setattr(builder._Server, "serve_forever", lambda self: (_ for _ in ()).throw(KeyboardInterrupt))
    output = tmp_path / "b.json"
    assert main(["build", site.url + "/books/", "-o", str(output), "--port", "0"]) == 0
    assert "wintergrab build: http://127.0.0.1:" in capsys.readouterr().err
    assert main(["build", site.url + "/status/404", "-o", str(output), "--port", "0"]) == 1
    assert "answered 404" in capsys.readouterr().err
