"""wintergrab benchmark: measured on this machine, each scenario in a process of its own; and wintergrab extract."""

from __future__ import annotations

import json
import sys
import urllib.request

import pytest

from wintergrab.bench import build_shop, describe, listing_page, product_page, run_benchmark
from wintergrab.cli import main


def test_the_shop() -> None:
    site = build_shop(pages=5, items=40)
    assert len(site) == 5 + 40 + 1  # and its robots.txt
    listing = listing_page(0, pages=5, items=40).decode()
    assert listing.count('class="item"') == 20 and 'href="/page/1"' in listing
    assert '"@type": "Product"' in product_page(0, 5).decode()  # every other one has JSON-LD
    assert '"@type": "Product"' not in product_page(1, 5).decode()
    assert site[b"/item/7"].startswith(b"HTTP/1.1 200 OK\r\n")


def test_every_scenario_is_measured() -> None:
    finished = []
    report = run_benchmark(
        scenarios=("startup", "crawl", "parse", "extract", "data", "dedupe"),
        pages=5,
        items=40,
        rounds=5,
        startup_runs=1,
        on_result=lambda name, result: finished.append(name),
    )
    assert finished == ["startup", "crawl", "parse", "extract", "data", "dedupe"]
    results = report["results"]
    assert not [name for name, result in results.items() if "error" in result]
    crawl = results["crawl"]
    assert (crawl["pages"], crawl["items"], crawl["bad_items"], crawl["errors"]) == (45, 40, 0, 0)  # the whole shop
    assert crawl["pages_per_s"] > 0 and crawl["latency_p90_ms"] >= crawl["latency_p50_ms"] >= 0
    assert results["extract"]["pages_per_s"] > 0 and results["extract"]["fields"] > 10
    assert results["dedupe"]["unique_urls"] * 5 == results["dedupe"]["urls"]  # five spellings of every page
    assert results["data"]["records_per_s"] > 0 and results["startup"]["import_ms"] > 0
    if sys.platform != "win32":
        assert crawl["peak_rss_mb"] > 10  # its own process: its own memory
    table = describe(report)
    assert "crawl    pages/s" in table and "shop: 5 listing + 40 product pages" in table
    with pytest.raises(ValueError, match="unknown scenario"):
        run_benchmark(scenarios=("warp",))


def test_the_benchmark_command(capsys, tmp_path) -> None:
    out = tmp_path / "bench.json"
    code = main(["benchmark", "--quick", "--scenario", "parse", "--scenario", "dedupe", "--json", "-o", str(out)])
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert set(printed["results"]) == {"parse", "dedupe"} and json.loads(out.read_text()) == printed
    assert printed["environment"]["shop"] == {"pages": 20, "items": 200, "latency_ms": 0.0}
    assert main(["benchmark", "--pages", "0"]) == 2
    assert "--pages must be at least 1" in capsys.readouterr().err


def test_the_shop_serves_over_http() -> None:
    import subprocess

    server = subprocess.Popen(
        [sys.executable, "-m", "wintergrab.bench", "serve", "--pages", "3", "--items", "10"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert server.stdout is not None
        url = server.stdout.readline().strip()
        with urllib.request.urlopen(url + "/item/3", timeout=10) as answer:
            assert answer.status == 200 and b"<h1>" in answer.read()
        with pytest.raises(urllib.error.HTTPError, match="404"):
            urllib.request.urlopen(url + "/nowhere", timeout=10)
    finally:
        server.terminate()
        server.wait(timeout=10)


@pytest.mark.browser
def test_the_browser_scenario() -> None:
    report = run_benchmark(scenarios=("browser",), pages=2, items=10, rounds=5)
    browser = report["results"]["browser"]
    assert "error" not in browser, browser
    assert browser["pages"] == 5 and browser["times_slower"] > 1 and browser["pages_per_s"] > 0


def test_the_extract_command(site, tmp_path, capsys) -> None:
    out = tmp_path / "books.jsonl"
    code = main(["-q", "extract", site.url + "/books/", "--schema", "product", "--follow", "article.product_pod h3",
                 "--max-pages", "6", "-o", str(out)])  # fmt: skip
    assert code == 0
    # the listing page is no product: its title and first card would make one that no page states
    assert "1 page(s) looked like lists of records" in capsys.readouterr().err
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert rows and all(row["name"].startswith("Book number") for row in rows)
    same = tmp_path / "crawled.jsonl"
    assert main(["-q", "crawl", site.url + "/books/", "--extract", "product", "--follow", "article.product_pod h3",
                 "--max-pages", "6", "-o", str(same)]) == 0  # fmt: skip
    lines = sorted(out.read_text(encoding="utf-8").splitlines())
    assert sorted(same.read_text(encoding="utf-8").splitlines()) == lines  # the same crawl
    everything = tmp_path / "everything.jsonl"
    assert main(["-q", "extract", site.url + "/books/", "--schema", "product", "--follow", "article.product_pod h3",
                 "--max-pages", "6", "-s", "skip_listings=false", "-o", str(everything)]) == 0  # fmt: skip
    names = [json.loads(line)["name"] for line in everything.read_text(encoding="utf-8").splitlines()]
    assert len(names) == len(lines) + 1  # read anyway when asked: the listing's own "record"
