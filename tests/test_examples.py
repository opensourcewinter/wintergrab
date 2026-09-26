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
    assert len(list(csv.DictReader(out.open(encoding="utf-8", newline="")))) == 4


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
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
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


def test_09_zero_selector(site) -> None:
    result = load("09_zero_selector").main(site.url + "/books/", "Book number 1", "£11.50")
    assert len(result["records"]) == 4 and result["records"][0]["title"] == "Book number 1"
    assert [r["title"] for r in result["rows"]] == [f"Book number {i}" for i in range(1, 9)]
    assert result["next"] == site.url + "/books/catalogue/page-2.html"


def test_10_big_crawl(site, tmp_path) -> None:
    import sqlite3

    spider_cls = load("10_big_crawl").ShopCrawl
    kwargs = {
        "sitemap_urls": [],
        "start_urls": [site.url + "/books/"],
        "sitemap_rules": [],
        "crawl_dir": str(tmp_path / "crawl"),
        "cache": str(tmp_path / "cache"),
        "output": str(tmp_path / "shop.db"),
        "log_level": None,
    }
    live = spider_cls(**kwargs).run()
    assert live.status == "finished" and live.stats["items"] == 12
    replay = spider_cls(cache_mode="offline", **kwargs).run()
    assert replay.stats["items"] == 12 and replay.stats["cache_hits"] >= 15
    rows = sqlite3.connect(tmp_path / "shop.db").execute("SELECT url, title, stock, from_cache FROM items").fetchall()
    assert len(rows) == 12  # upserted, not duplicated
    assert all(row[3] == 1 for row in rows)  # the replay's rows replaced the live ones


def test_11_templates_and_evidence(site, capsys) -> None:
    record = load("11_templates_and_evidence").main(site.url + "/books/catalogue/book-1/index.html")
    assert record["name"] == "Book number 1" and record["price"] == 11.5 and record["currency"] == "GBP"
    assert "confidence" in capsys.readouterr().out


def test_12_goal(site, capsys) -> None:
    records = load("12_goal").main(site.url + "/books/", limit=5)
    assert 1 <= len(records) <= 5 and all(r.get("name") for r in records)
    assert "Understood:" in capsys.readouterr().out


def test_13_record_and_replay(fresh_site, tmp_path) -> None:
    before = sum(fresh_site.site.hits.values())
    result = load("13_record_and_replay").main(fresh_site.url + "/books/", workspace=str(tmp_path / "ws"))
    assert not result.same and result.diff.counts["changed"] == 4  # the prices became numbers
    assert sum(fresh_site.site.hits.values()) - before == 2  # robots.txt and the page, once: the replay is offline


def test_14_generate_scraper(site, tmp_path, capsys) -> None:
    result = load("14_generate_scraper").main(site.url + "/books/", directory=str(tmp_path / "books"))
    assert result.accepted and result.generated.fields["price"].selector == "p.price_color"
    printed = capsys.readouterr().out
    assert "accepted:" in printed and "record(s); the first:" in printed


def test_the_example_project() -> None:
    from wintergrab.project import Project

    project = Project(EXAMPLES / "project" / "wintergrab.yaml")
    assert list(project.jobs) == ["quotes", "books", "rated"] and project.jobs["rated"].after == ("books",)
    books = project.jobs["books"].command()
    assert books[:2] == ["crawl", "https://books.toscrape.com/"] and "--extract" in books and "product" in books
    assert project.jobs["quotes"].command()[-2:] == ["--output", "data/quotes.jsonl"]


def test_the_example_plugin(tmp_path, capsys) -> None:
    from wintergrab import plugins
    from wintergrab.cli import main as cli
    from wintergrab.spider import exporters

    spec = importlib.util.spec_from_file_location(
        "wintergrab_lines", EXAMPLES / "plugin" / "wintergrab_lines" / "__init__.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    info = plugins.PluginInfo("lines", "wintergrab_lines:plugin")
    try:
        module.plugin(plugins.Registry(info))
        assert info.added == ["output .lines", "command count-lines"]
        exporters.write_items(tmp_path / "quotes.lines", [{"text": "a", "author": "b"}, {"text": "c", "author": "d"}])
        assert (tmp_path / "quotes.lines").read_text(encoding="utf-8").splitlines()[0] == "text=a\tauthor=b"
        assert cli(["count-lines", str(tmp_path / "quotes.lines")]) == 0 and capsys.readouterr().out == "2\n"
    finally:
        exporters.EXPORTERS.pop(".lines", None)
        plugins.COMMANDS.pop("count-lines", None)
