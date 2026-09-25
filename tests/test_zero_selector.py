"""End-to-end: zero-selector extraction through Response, spiders and the CLI."""

from __future__ import annotations

import json

import wintergrab as wg
from wintergrab.cli import main


def test_structured_and_embedded_data(site) -> None:
    page = wg.get(site.url + "/rich/1")
    data = page.structured_data()
    assert data["json_ld"][0]["name"] == "Rich product 1"
    assert data["json_ld"][0]["offers"]["price"] == "19.99"
    assert data["microdata"][0]["name"] == "Micro 1"
    assert data["opengraph"]["title"] == "Rich 1"
    assert data["meta"]["title"] == "Rich 1"
    state = page.embedded_json()
    assert state["__NEXT_DATA__"]["props"]["pageProps"]["product"]["price"] == 10
    assert state["__INITIAL_STATE__"]["cart"]["items"] == []
    assert sorted(map(str, page.find_json("price"))) == ["0", "10", "19.99"]


def test_find_json_on_json_responses(site) -> None:
    assert wg.get(site.url + "/json").find_json("ok") == [True]


def test_tables_and_next_page(site) -> None:
    page = wg.get(site.url + "/rich/1")
    table = page.tables()[0]
    assert table["headers"] == ["Spec", "Value"]
    assert table["rows"] == [{"Spec": "Weight", "Value": "1 kg"}, {"Spec": "Color", "Value": "Blue"}]
    assert page.next_page() == site.url + "/rich/2"
    assert wg.get(site.url + "/rich/3").next_page() is None
    assert wg.get(site.url + "/products/page/1").next_page() == site.url + "/products/page/2"


def test_follow_next_drives_pagination(site) -> None:
    class Paged(wg.Spider):
        log_level = None
        start_urls = [site.url + "/rich/1"]

        def parse(self, response):
            yield {"name": response.structured_data()["json_ld"][0]["name"]}
            yield response.follow_next()

    names = sorted(i["name"] for i in Paged().run().items)
    assert names == ["Rich product 1", "Rich product 2", "Rich product 3"]


def test_auto_extract_catalogue(site) -> None:
    books = wg.get(site.url + "/books/").auto_extract()
    assert len(books) == 4
    first = books[0]
    assert first["title"].startswith("Book number 1")
    assert first["url"] == site.url + "/books/catalogue/book-1/index.html"
    assert (first["price"], first["currency"], first["in_stock"]) == (11.5, "GBP", True)


def test_learn_on_one_page_extract_on_another(site) -> None:
    page1 = wg.get(site.url + "/books/")
    schema = page1.learn({"title": "Book number 2", "price": "£13.00"})
    rows = schema.extract(page1)
    assert [r["title"] for r in rows] == [f"Book number {i}" for i in range(1, 5)]
    page2 = wg.get(site.url + "/books/catalogue/page-2.html")
    assert [r["price"] for r in schema.extract(page2)][:2] == [17.5, 19.0]
    again = type(schema).from_dict(json.loads(json.dumps(schema.to_dict())))
    assert again.extract(page2) == schema.extract(page2)


def test_cli_structured_json_data_tables_next(site, capsys) -> None:
    assert main(["-q", "get", site.url + "/rich/2", "--structured", "--json-data", "--tables", "--next"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["structured"]["json_ld"][0]["name"] == "Rich product 2"
    assert "__NEXT_DATA__" in doc["embedded_json"]
    assert doc["tables"][0]["rows"][1]["Value"] == "Blue"
    assert doc["next_page"] == site.url + "/rich/3"


def test_cli_auto_and_learn(site, capsys, tmp_path) -> None:
    assert main(["-q", "get", site.url + "/books/", "--auto"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(rows) == 4 and rows[0]["price"] == 11.5 and rows[0]["currency"] == "GBP"

    schema_file = tmp_path / "books.json"
    code = main(
        ["-q", "get", site.url + "/books/", "--learn", "title=Book number 1", "--learn", "price=£11.50",
         "--save-schema", str(schema_file)]
    )  # fmt: skip
    assert code == 0 and schema_file.exists()
    capsys.readouterr()

    out = tmp_path / "all.jsonl"
    assert main(["-q", "crawl", site.url + "/books/", "--schema", str(schema_file), "--paginate", "-o", str(out)]) == 0
    titles = [json.loads(line)["title"] for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(titles) == 12 and "Book number 12" in titles


def test_cli_crawl_auto_paginate(site, tmp_path) -> None:
    out = tmp_path / "auto.jsonl"
    assert main(["-q", "crawl", site.url + "/books/", "--auto", "--paginate", "-o", str(out)]) == 0
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 12
