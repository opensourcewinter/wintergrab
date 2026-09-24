"""Run the example scripts against local replicas of the sandbox sites."""

from __future__ import annotations

import asyncio
import csv
import importlib.util
import json
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"example_{name}", EXAMPLES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_01_quickstart(site, capsys) -> None:
    quotes = load("01_quickstart").main(site.url + "/quotes/")
    assert len(quotes) == 2
    assert quotes[0]["author"] == "Albert Einstein"
    assert quotes[0]["tags"] == ["change", "thinking"]
    assert "next page:" in capsys.readouterr().out


def test_02_extract_to_csv(site, tmp_path) -> None:
    out = tmp_path / "books.csv"
    books = load("02_extract_to_csv").main(site.url + "/books/", out=str(out))
    assert books[0] == {
        "title": "Book number 1",
        "price": 11.5,
        "rating": "Two",
        "in_stock": True,
        "url": site.url + "/books/catalogue/book-1/index.html",
    }
    assert len(list(csv.DictReader(out.open()))) == 4


def test_03_adaptive_selectors() -> None:
    result = load("03_adaptive_selectors").main()
    assert result == {
        "names": ["Green mug", "Blue mug", "Tea pot"],
        "prices": ["$11", "$12", "$30"],
        "title": "Summer sale",
    }


def test_04_async_many_pages(site) -> None:
    urls = [f"{site.url}/books/catalogue/page-{n}.html" for n in (1, 2, 3)]
    titles = asyncio.run(load("04_async_many_pages").main(urls))
    assert titles == [f"Book number {i}" for i in range(1, 13)]


def test_05_quotes_spider(site, tmp_path) -> None:
    spider_cls = load("05_quotes_spider").QuotesSpider
    out = tmp_path / "quotes.jsonl"
    result = spider_cls(
        start_urls=[site.url + "/quotes/"], allowed_domains=["127.0.0.1"], output=str(out), log_level=None
    ).run()
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    quotes = [r for r in rows if "text" in r]
    authors = [r for r in rows if "born" in r]
    assert len(quotes) == 6 and len(authors) == 4
    assert result.stats["duplicates_filtered"] >= 2  # Einstein appears three times


def test_06_books_resumable(site, tmp_path) -> None:
    spider_cls = load("06_books_resumable").BooksSpider
    result = spider_cls(
        start_urls=[site.url + "/books/"],
        allowed_domains=["127.0.0.1"],
        crawl_dir=str(tmp_path / "state"),
        output=str(tmp_path / "books.jsonl"),
        log_level=None,
    ).run()
    assert result.status == "finished"
    books = {b["upc"]: b for b in result.items}
    assert len(books) == 12
    assert books["upc0003"] == {
        "title": "Book number 3",
        "price": 14.5,
        "stock": 6,
        "rating": 4,
        "upc": "upc0003",
        "category": "Poetry",
        "url": site.url + "/books/catalogue/book-3/index.html",
    }


@pytest.mark.browser
def test_07_browser_rendering(site) -> None:
    texts = load("07_browser_rendering").main(site.url + "/js", selector=".item")
    assert texts == ["Item 1", "Item 2", "Item 3"]


@pytest.mark.browser
def test_08_sessions_and_fallback(site) -> None:
    spider_cls = load("08_sessions_and_fallback").MixedSpider
    result = spider_cls(
        start_urls=[site.url + "/quotes/"], js_url=site.url + "/quotes/page/2/", log_level=None, max_pages=5
    ).run()
    via = {item["via"] for item in result.items}
    assert via == {"fast", "browser"}
