"""Live checks against real public sites. Opt-in, and skipped in CI:

    WINTERGRAB_LIVE=1 pytest -m live

They run the examples against the sites they were written for
(quotes.toscrape.com and books.toscrape.com, which exist for scraping
practice) and fetch a few pages from pypi.org. Every test makes only a
handful of requests. Run them from a normal network before a release.
"""

from __future__ import annotations

import asyncio
import csv
import importlib.util
import json
from pathlib import Path

import pytest

import wintergrab as wg
from wintergrab.fetchers import looks_blocked

pytestmark = pytest.mark.live

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"live_example_{name}", EXAMPLES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_quickstart() -> None:
    quotes = load("01_quickstart").main()
    assert len(quotes) == 10
    assert all(q["text"] and q["author"] for q in quotes)


def test_extract_to_csv(tmp_path) -> None:
    out = tmp_path / "books.csv"
    books = load("02_extract_to_csv").main(out=str(out))
    assert len(books) == 20
    assert books[0]["title"] == "A Light in the Attic"
    assert isinstance(books[0]["price"], float)
    assert len(list(csv.DictReader(out.open(encoding="utf-8", newline="")))) == 20


def test_async_many_pages() -> None:
    titles = asyncio.run(load("04_async_many_pages").main())
    assert len(titles) >= 20 and all(titles)


def test_quotes_spider(tmp_path) -> None:
    out = tmp_path / "quotes.jsonl"
    result = load("05_quotes_spider").QuotesSpider(output=str(out), max_pages=4, log_level=None).run()
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert result.status == "finished" and result.stats.get("failed", 0) == 0
    assert any("text" in row for row in rows)


def test_zero_selector() -> None:
    result = load("09_zero_selector").main()
    assert len(result["records"]) == 20
    assert len(result["rows"]) == 40  # the learned schema on pages 1 and 2
    assert result["next"].endswith("page-2.html")


@pytest.mark.browser
def test_browser_rendering() -> None:
    texts = load("07_browser_rendering").main()
    assert len(texts) == 10 and all(texts)


def test_pypi_project_page() -> None:
    page = wg.get("https://pypi.org/project/lxml/")
    assert page.status == 200 and not looks_blocked(page)
    assert page.css("h1.project-header__name::text").get("").strip().startswith("lxml")
    assert page.structured_data()["opengraph"]


def test_pypi_feed_and_cache(tmp_path) -> None:
    assert len(wg.sitemap("https://pypi.org/rss/updates.xml", max_sitemaps=1)) > 0
    with wg.Fetcher(cache=str(tmp_path)) as http:
        first = http.get("https://pypi.org/project/cssselect/")
        second = http.get("https://pypi.org/project/cssselect/")
    assert first.cache_status == "stored"
    assert second.cache_status in ("hit", "revalidated")
