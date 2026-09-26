"""What a page looks like: its layout's tables and labelled values, and screenshots for models that read images."""

from __future__ import annotations

import functools
import http.server
import json
import threading
from pathlib import Path

import pytest

from wintergrab.data import Schema
from wintergrab.extraction import Extractor, ModelRequest, layout_pairs, layout_tables
from wintergrab.fetchers.response import Response
from wintergrab.models import ModelProvider
from wintergrab.parser.layout import Box, Layout, element_path

DATA = Path(__file__).parent / "data" / "visual"


def _page(name: str) -> Response:
    """The test page as Chromium drew it (its layout was recorded once: tests/data/visual/*.layout.json)."""
    html = (DATA / f"{name}.html").read_bytes()
    response = Response(f"https://visual.example/{name}", body=html, headers={"content-type": "text/html"})
    response.layout = Layout.from_dict(json.loads((DATA / f"{name}.layout.json").read_text(encoding="utf-8"))["layout"])
    return response


def test_tables_drawn_with_divs() -> None:
    tables = layout_tables(_page("dashboard").layout)
    assert len(tables) == 1  # the pricing grid; not the tiles, the specification or the chart's labels
    [plans] = tables
    assert plans.header == ["Plan", "Price", "Seats"]  # bold over regular
    assert plans.rows[0] == ["Starter", "$9", "1"]  # "$" and "9" are two spans of one cell
    assert plans.records()[-1] == {"Plan": "Enterprise", "Price": "$299", "Seats": "500"}
    assert plans.path == "body > div:nth-of-type(2) > div:nth-of-type(1)"


def test_labels_and_their_values() -> None:
    pairs = {(p.label, p.value, p.how) for p in layout_pairs(_page("dashboard").layout)}
    assert pairs == {
        ("Active users", "1,284", "above"),  # a tile: the large number over its label
        ("Revenue", "$48.2k", "below"),  # a tile: the label over its number
        ("Uptime", "99.9%", "above"),
        ("Weight", "1.2 kg", "beside"),  # a row of its own: the value right of its label
        ("Colour", "Graphite", "beside"),
    }  # and nothing between a table's cells, the side menu and the table, or the chart's labels


def test_the_extractor_reads_what_the_page_draws() -> None:
    schema = Schema.from_dict({"name": "stats", "fields": {
        "active_users": "integer", "revenue": "money", "uptime": "number", "weight": "quantity", "colour": "string",
    }})  # fmt: skip
    record = Extractor(schema).extract(_page("dashboard"))
    assert record.data == {
        "active_users": 1284, "revenue": {"amount": 48200, "currency": "USD"}, "uptime": 99.9,
        "weight": {"value": 1.2, "unit": "kg"}, "colour": "Graphite",
    }  # fmt: skip
    assert record.fields["active_users"].method == "visual"
    assert record.fields["active_users"].source == "visual:above:Active users"
    without = _page("dashboard")
    without.layout = None
    assert Extractor(schema).extract(without).data["active_users"] is None  # the HTML alone does not say
    # a listing: each card read in its own part of the layout
    cards = Schema.from_dict({"name": "laptop", "container": "div.card",
                              "fields": {"name": {"type": "string", "selectors": [".name"]}, "price": "money",
                                         "weight": "quantity"}})  # fmt: skip
    found = [r.data for r in Extractor(cards).extract_all(_page("cards"))]
    assert [(r["name"], r["price"]["amount"], r["weight"]["value"]) for r in found] == [
        ("Aero 13", 999, 1.1),
        ("Aero 15", 1299, 1.6),
        ("Bolt 14", 749, 1.4),
    ]


def test_paths_are_written_as_the_browser_writes_them() -> None:
    page = _page("cards")
    [second] = [card for i, card in enumerate(page.css("div.card")) if i == 1]
    path = element_path(second.root)
    assert path == "body > div > div:nth-of-type(2)"
    assert [b.text for b in page.layout.within(path)] == ["Aero 15", "Price", "$1,299", "Weight", "1.6 kg"]
    box = Box("x", 1, 2, 3, 4, weight=700)
    assert (box.right, box.bottom, box.bold) == (4, 6, True)
    assert Layout.from_dict(page.layout.to_dict()).boxes == page.layout.boxes


class Reader:
    """A stand-in model that reads images: it says what it saw, and keeps what it was sent."""

    name = "reader"

    def __init__(self, answer: dict) -> None:
        self.answer, self.requests = answer, []

    def extract(self, request: ModelRequest) -> dict:
        self.requests.append(request)
        return self.answer


def test_a_screenshot_for_a_model_that_reads_images() -> None:
    schema = Schema.from_dict({"name": "chart", "fields": {"title": "string", "q3_sales": "integer"}})
    page = Response("https://charts.example/", body=b"<h1>Sales</h1><canvas></canvas>",
                    headers={"content-type": "text/html"})  # fmt: skip
    page.screenshot = b"\x89PNG\r\n\x1a\n-a-picture-"
    reader = Reader({"q3_sales": 4120})
    record = Extractor(schema, model=reader, vision=True).extract(page)
    [request] = reader.requests
    assert [image.data for image in request.images] == [page.screenshot] and "screenshot" in request.prompt()
    # drawn on a canvas, so in no text: kept, unsure, and said so
    assert record.data["q3_sales"] == 4120 and record.fields["q3_sales"].confidence == pytest.approx(0.36)
    assert "image-only" in record.fields["q3_sales"].notes
    blind = Reader({"q3_sales": 4120})
    record = Extractor(schema, model=blind).extract(page)  # no vision: no picture, and a value in no text is dropped
    assert blind.requests[0].images == [] and record.data["q3_sales"] is None


def test_providers_send_the_images() -> None:
    sent = []

    class Provider(ModelProvider):
        provider = "test"

        def key_required(self, base_url):
            return False

        def complete(self, prompt, *, system=None, images=(), json_output=False):
            sent.append(list(images))
            return '{"q3_sales": 4120}'

    page = Response("https://charts.example/", body=b"<h1>Sales</h1>", headers={"content-type": "text/html"})
    page.screenshot = b"\x89PNG-a-picture-"
    Extractor({"name": "chart", "fields": {"q3_sales": "integer"}}, model=Provider("m"), vision=True).extract(page)
    assert [image.data for image in sent[0]] == [page.screenshot]


@pytest.mark.browser
def test_a_browser_records_the_layout(tmp_path, capsys) -> None:
    from wintergrab.cli import main
    from wintergrab.fetchers.browser import BrowserFetcher

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(DATA))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/dashboard.html"
    try:
        with BrowserFetcher() as browser:
            response = browser.get(url, layout=True, screenshot=True)
        assert response.screenshot is not None and response.screenshot.startswith(b"\x89PNG")
        texts = [box.text for box in response.layout.boxes]
        assert "1,284" in texts and "Q1" in texts  # SVG labels too
        tile = response.layout.find("1,284")[0]
        assert tile.size == 32 and tile.weight == 700 and tile.width > 0
        [table] = layout_tables(response.layout)
        assert table.header == ["Plan", "Price", "Seats"] and len(table.rows) == 4
        assert main(["-q", "get", url, "--visual-tables"]) == 0
        printed = json.loads(capsys.readouterr().out)
        assert printed["visual_tables"][0]["records"][0] == {"Plan": "Starter", "Price": "$9", "Seats": "1"}
        schema = tmp_path / "stats.json"
        schema.write_text(json.dumps({"name": "stats", "fields": {"uptime": "number", "colour": "string"}}))
        assert main(["-q", "get", url, "--layout", "--extract", str(schema)]) == 0
        assert json.loads(capsys.readouterr().out) == {"uptime": 99.9, "colour": "Graphite", "_confidence": 0.75}
    finally:
        server.shutdown()
        server.server_close()


def test_vision_needs_a_model(capsys) -> None:
    from wintergrab.cli import main

    assert main(["get", "https://example.com/", "--vision"]) == 2
    assert "needs --extract and --model" in capsys.readouterr().err
