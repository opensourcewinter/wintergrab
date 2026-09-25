"""wintergrab.scrape(): a listing URL to complete, typed records in one call."""

from __future__ import annotations

import csv
import json

import wintergrab as wg
from wintergrab.cli import main

QUIET = {"obey_robots_txt": False, "log_level": None}


def test_deep_scrape_of_every_listing_page(site) -> None:
    books = wg.scrape(site.url + "/books/", pages=None, deep=True, **QUIET)
    assert [b["title"] for b in books] == [f"Book number {i}" for i in range(1, 13)]  # page order
    first = books[0]
    assert first == {
        "title": "Book number 1",
        "price": 11.5,
        "currency": "GBP",
        "availability": "In stock (4 available)",
        "in_stock": True,
        "stock": 4,
        "rating": 2.0,
        "upc": "upc0001",
        "product_type": "Books",
        "price_excl_tax": 11.5,
        "price_incl_tax": 11.5,
        "tax": 0.0,
        "number_of_reviews": 1,
        "category": "Poetry",
        "breadcrumbs": "Home > Books > Poetry",
        "description": "Book number 1 is a wonderful read. It was written long ago and still holds up today.",
        "image": site.url + "/books/media/cache/1.jpg",  # the detail page's image wins
        "url": site.url + "/books/catalogue/book-1/index.html",
    }
    assert all(isinstance(b["stock"], int) and isinstance(b["price"], float) for b in books)


def test_shallow_scrape_reads_one_listing_page(site) -> None:
    books = wg.scrape(site.url + "/books/", **QUIET)
    assert len(books) == 4
    assert list(books[0]) == ["title", "price", "currency", "rating", "availability", "in_stock", "url", "image"]


def test_scrape_a_single_item_page(site) -> None:
    [book] = wg.scrape(site.url + "/books/catalogue/book-2/index.html", **QUIET)
    assert (book["title"], book["upc"], book["stock"]) == ("Book number 2", "upc0002", 5)


def test_scrape_stops_at_max_items(site) -> None:
    assert len(wg.scrape(site.url + "/books/", pages=None, deep=True, max_items=5, **QUIET)) == 5


def test_records_are_kept_when_their_own_page_fails(site) -> None:
    gadgets = wg.scrape(site.url + "/deadlinks/", deep=True, retries=0, **QUIET)
    assert [(g["title"], g["price"]) for g in gadgets] == [(f"Gadget {i}", i + 0.99) for i in range(1, 5)]


def test_scrape_writes_its_output_in_page_order(site, tmp_path) -> None:
    out = tmp_path / "books.csv"
    wg.scrape(site.url + "/books/", pages=None, deep=True, output=str(out), **QUIET)
    rows = list(csv.DictReader(out.open(encoding="utf-8-sig", newline="")))
    assert [r["title"] for r in rows] == [f"Book number {i}" for i in range(1, 13)]
    assert rows[0]["price"] == "11.5" and rows[0]["stock"] == "4"


def test_cli_get_auto_deep(site, capsys) -> None:
    assert main(["-q", "get", site.url + "/books/", "--deep"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(rows) == 4 and rows[2]["upc"] == "upc0003" and rows[2]["category"] == "Poetry"


def test_cli_get_auto_on_a_single_item_page(site, capsys) -> None:
    assert main(["-q", "get", site.url + "/books/catalogue/book-3/index.html", "--auto"]) == 0
    captured = capsys.readouterr()
    [row] = [json.loads(line) for line in captured.out.splitlines()]
    assert row["upc"] == "upc0003" and "extracted its main item" in captured.err


def test_cli_crawl_auto_deep_paginate(site, tmp_path) -> None:
    out = tmp_path / "books.jsonl"
    code = main(["-q", "crawl", site.url + "/books/", "--auto", "--deep", "--paginate", "-o", str(out),
                 "--set", "obey_robots_txt=false"])  # fmt: skip
    assert code == 0
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert sorted(r["upc"] for r in rows) == [f"upc{i:04d}" for i in range(1, 13)]
