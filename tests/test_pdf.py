"""PDFs: their text where it is drawn, their tables and links, read like a page's."""

from __future__ import annotations

import functools
import http.server
import logging
import threading

import pytest

pytest.importorskip("pypdf")

from pdfwriter import make_pdf
from wintergrab.cli import main
from wintergrab.extraction import Extractor, layout_tables
from wintergrab.fetchers.response import Response
from wintergrab.parser import pdf as pdf_module
from wintergrab.parser.pdf import read_pdf

PRICES = [
    (72, 72, 20, "Price list 2026", "bold"),
    (72, 110, 11, "Invoice number: INV-0042"),
    (72, 126, 11, "Due date: 2026-10-31"),
    (72, 170, 11, "Product", "bold"), (260, 170, 11, "Unit", "bold"), (380, 170, 11, "Price", "bold"),
    (72, 186, 11, "Oak table"), (260, 186, 11, "each"), (380, 186, 11, "EUR 450.00"),
    (72, 202, 11, "Pine chair"), (260, 202, 11, "each"), (380, 202, 11, "EUR 89.50"),
    (72, 218, 11, "Walnut shelf"), (260, 218, 11, "per metre"), (380, 218, 11, "EUR 120.00"),
    (72, 270, 11, "Questions? Write to sales@oak.example or see our site."),
    ("link", 72, 262, 300, 12, "https://oak.example/contact"),
]  # fmt: skip


def _response(data: bytes, content_type: str = "application/pdf") -> Response:
    return Response("https://oak.example/prices.pdf", body=data, headers={"content-type": content_type})


def test_the_text_where_it_is_drawn() -> None:
    document = read_pdf(make_pdf([PRICES], title="Oak furniture: prices"))
    assert (document.title, document.page_count, document.truncated) == ("Oak furniture: prices", 1, False)
    [page] = document.pages
    assert (page.width, page.height) == (612, 792)
    title = page.boxes[0]
    assert (title.text, title.x, title.size, title.weight) == ("Price list 2026", 72, 20, 700)
    assert title.y == pytest.approx(72 - 0.8 * 20)  # from the top, as a web page's
    [oak] = [b for b in page.boxes if b.text == "Oak table"]
    cells = [b for b in page.boxes if b.y == oak.y]
    assert [c.x for c in cells] == [72, 260, 380] and len({c.y for c in cells}) == 1  # one row, three columns
    # lines close together make a block; the table is a block of its own
    assert cells[0].path == "body > section > div:nth-of-type(3) > p:nth-of-type(2) > span:nth-of-type(1)"
    assert page.links == [("https://oak.example/contact", 72, 262, 300, 12)]
    assert "Oak table each EUR 450.00" in document.text


def test_a_pdf_response_reads_as_a_page() -> None:
    response = _response(make_pdf([PRICES], title="Oak furniture: prices"))
    assert response.is_pdf and response.pdf.title == "Oak furniture: prices"
    assert response.markdown().splitlines()[:5] == [
        "# Price list 2026", "", "Invoice number: INV-0042", "", "Due date: 2026-10-31",
    ]  # fmt: skip
    assert "| Oak table | each | EUR 450.00 |" in response.markdown()
    assert response.css("a::attr(href)").getall() == ["https://oak.example/contact"]  # links a crawl follows
    [table] = layout_tables(response.layout)
    assert table.records()[1] == {"Product": "Pine chair", "Unit": "each", "Price": "EUR 89.50"}
    invoice = {"name": "invoice", "fields": {"invoice_number": "string", "due_date": "date", "email": "email",
                                              "title": "string"}}  # fmt: skip
    record = Extractor(invoice).extract(response)
    assert record.data == {
        "invoice_number": "INV-0042", "due_date": "2026-10-31", "email": "sales@oak.example",
        "title": "Oak furniture: prices",
    }  # fmt: skip
    # the body says what it is, whatever the server calls it
    assert _response(make_pdf([PRICES]), "application/octet-stream").is_pdf
    assert not _response(b"<html></html>", "text/html").is_pdf


def test_pages_one_under_the_other() -> None:
    second = [(72, 72, 11, "Page two says hello")]
    response = _response(make_pdf([PRICES, second]))
    layout = response.layout
    [hello] = layout.find("Page two says hello")
    assert hello.y == pytest.approx(792 + 24 + 72 - 0.8 * 11)  # under the first page, and a gap
    assert hello.path.startswith("body > section:nth-of-type(2) >")
    assert [s.attrib["data-page"] for s in response.css("section")] == ["1", "2"]
    assert read_pdf(make_pdf([PRICES, second]), max_pages=1).truncated


def test_what_cannot_be_read(monkeypatch, caplog) -> None:
    broken = _response(b"%PDF-1.4\nnot really a pdf\n")
    with caplog.at_level(logging.WARNING):
        assert broken.css("body *") == [] and broken.layout is None and broken.markdown().strip() == ""
    assert sum("not a PDF that can be read" in r.getMessage() for r in caplog.records) == 1  # said once
    with pytest.raises(ValueError, match="not a PDF"):
        broken.pdf  # noqa: B018

    # without pypdf: nothing read, and why
    def missing(data: bytes, **_: object) -> None:
        raise ImportError("reading PDFs needs pypdf: pip install 'wintergrab[pdf]'")

    monkeypatch.setattr(pdf_module, "read_pdf", missing)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert _response(make_pdf([PRICES])).markdown().strip() == ""
    assert "wintergrab[pdf]" in caplog.text


def test_the_doctor_says_whether_pdfs_can_be_read(capsys) -> None:
    main(["doctor"])
    assert "pypdf" in capsys.readouterr().out


@pytest.mark.browser
def test_a_browser_hands_back_the_pdf_itself(tmp_path) -> None:
    from wintergrab.fetchers.browser import BrowserFetcher

    (tmp_path / "prices.pdf").write_bytes(make_pdf([PRICES]))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with BrowserFetcher() as browser:
            response = browser.get(f"http://127.0.0.1:{server.server_address[1]}/prices.pdf")
        # the browser shows a PDF in its viewer; the file is asked for itself
        assert response.body.startswith(b"%PDF-") and response.is_pdf
        assert layout_tables(response.layout)[0].header == ["Product", "Unit", "Price"]
    finally:
        server.shutdown()
        server.server_close()
