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


def test_templates_and_evidence() -> None:
    record = load("11_templates_and_evidence").main()
    assert record["name"] == "A Light in the Attic" and record["currency"] == "GBP" and record["price"] > 0


def test_goal() -> None:
    records = load("12_goal").main(limit=5)
    assert 1 <= len(records) <= 5 and all(r.get("name") for r in records)


def test_record_and_replay(tmp_path) -> None:
    result = load("13_record_and_replay").main(workspace=str(tmp_path / "ws"))
    assert not result.same and result.diff.counts["changed"] == 20  # the first page's 20 prices


def test_generate_scraper(tmp_path) -> None:
    result = load("14_generate_scraper").main(directory=str(tmp_path / "books"))
    assert result.accepted, result.describe()


def test_async_many_pages() -> None:
    titles = asyncio.run(load("04_async_many_pages").main())
    assert len(titles) >= 20 and all(titles)


def test_quotes_spider(tmp_path) -> None:
    out = tmp_path / "quotes.jsonl"
    result = load("05_quotes_spider").QuotesSpider(output=str(out), max_pages=4, log_level=None).run()
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert result.status == "limit"  # stopped at max_pages, as asked
    assert result.stats.get("failed", 0) == 0
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
    assert page.status == 200
    title = page.css("title::text").get("")
    if looks_blocked(page):
        # pypi.org's CDN sends some networks a JavaScript bot check instead of
        # the page. Then it must really be that check, not a misdetected page.
        assert "challenge" in title.lower(), f"flagged as blocked but not a challenge page: {title!r}"
        pytest.skip("pypi.org served its bot check to this network (detected correctly)")
    assert page.css("h1.project-header__name::text").get("").strip().startswith("lxml")
    assert page.structured_data()["opengraph"]


def test_pypi_feed_and_cache(tmp_path) -> None:
    assert len(wg.sitemap("https://pypi.org/rss/updates.xml", max_sitemaps=1)) > 0
    with wg.Fetcher(cache=str(tmp_path)) as http:
        first = http.get("https://pypi.org/project/cssselect/")
        second = http.get("https://pypi.org/project/cssselect/")
    assert first.cache_status == "stored"
    assert second.cache_status in ("hit", "revalidated")


@pytest.mark.browser
def test_browser_gets_the_whole_pypi_page() -> None:
    # pypi.org may put a JavaScript bot check in front of the page. Either way
    # the browser must end up with the complete real page (about 1.2 MB).
    page = wg.render("https://pypi.org/project/lxml/")
    assert not looks_blocked(page)
    assert page.css("h1.project-header__name::text").get("").strip().startswith("lxml")
    assert len(page.body) > 100_000
