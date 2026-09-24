"""Sitemaps, SQLite output, item de-duplication and the live progress line."""

from __future__ import annotations

import io
import json
import sqlite3

import wintergrab as wg
from wintergrab.sitemaps import parse_sitemap, robots_sitemaps
from wintergrab.spider.exporters import open_exporter
from wintergrab.spider.progress import ProgressDisplay


def test_parse_sitemap_formats() -> None:
    xml = b"<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'><url><loc>/a</loc><lastmod>2026-01-02</lastmod><priority>0.8</priority></url></urlset>"
    kind, entries = parse_sitemap(xml, "https://x.test/sitemap.xml")
    assert kind == "urlset" and entries[0].loc == "https://x.test/a" and entries[0].priority == 0.8
    assert entries[0].lastmod_datetime.year == 2026
    import gzip

    kind, entries = parse_sitemap(gzip.compress(b"https://x.test/1\nhttps://x.test/2\nnot a url\n"))
    assert kind == "text" and [e.loc for e in entries] == ["https://x.test/1", "https://x.test/2"]
    rss = b"<rss><channel><item><link>https://x.test/post</link><pubDate>Mon</pubDate></item></channel></rss>"
    assert parse_sitemap(rss)[1][0].loc == "https://x.test/post"
    atom = b"<feed xmlns='http://www.w3.org/2005/Atom'><entry><link href='https://x.test/e'/></entry></feed>"
    assert parse_sitemap(atom)[1][0].loc == "https://x.test/e"
    assert robots_sitemaps(
        "User-agent: *\nSitemap: https://x.test/s.xml\nsitemap: /t.xml", "https://x.test/robots.txt"
    ) == [
        "https://x.test/s.xml",
        "https://x.test/t.xml",
    ]


def test_sitemap_helper_follows_indexes(site) -> None:
    entries = wg.sitemap(site.url + "/robots.txt")
    locs = [e.loc for e in entries]
    assert locs[:5] == [site.url + f"/product/{i}" for i in range(1, 6)]
    assert site.url + "/item/2" in locs and len(locs) == 8
    recent = wg.sitemap(site.url + "/sitemap_index.xml", since="2026-04-01")
    assert [e.loc for e in recent] == [site.url + "/product/4", site.url + "/product/5"]


def test_sitemap_spider_with_rules(site) -> None:
    class FromSitemap(wg.Spider):
        log_level = None
        sitemap_urls = [site.url + "/robots.txt"]
        sitemap_rules = [(r"/product/", "parse_product")]  # items are skipped

        def parse_product(self, response):
            yield {"name": response.css("h1::text").get(), "lastmod": response.meta["sitemap_lastmod"]}

    result = FromSitemap().run()
    assert sorted(i["name"] for i in result.items) == [f"Product {i}" for i in range(1, 6)]
    assert {i["lastmod"] for i in result.items} >= {"2026-01-01"}

    incremental = FromSitemap(sitemap_since="2026-05-01", sitemap_follow=[r"products"]).run()
    assert [i["name"] for i in incremental.items] == ["Product 5"]


def test_sqlite_output_with_upsert(site, tmp_path) -> None:
    db = tmp_path / "items.db"

    class Catalog(wg.Spider):
        log_level = None
        start_urls = [site.url + "/products/page/1"]
        unique_key = "url"
        output = str(db)

        def parse(self, response):
            for card in response.css(".product"):
                yield {
                    "url": response.urljoin(card.css("a::attr(href)").get()),
                    "name": card.css(".name a::text").get(),
                    "price": float(card.css(".price::text").get().strip("$")),
                    "tags": ["mug", "sale"],
                }
                # the same item again: dropped by unique_key
                yield {"url": response.urljoin(card.css("a::attr(href)").get()), "name": "dup"}

    first = Catalog().run()
    assert first.stats["items"] == 4 and first.stats["items_duplicate"] == 4
    Catalog().run()  # a re-crawl updates rows in place instead of duplicating them
    rows = sqlite3.connect(db).execute("SELECT url, name, price, tags FROM items ORDER BY name").fetchall()
    assert len(rows) == 4
    assert rows[0][1:] == ("Product 1", 6.25, '["mug", "sale"]')


def test_sqlite_exporter_adds_columns(tmp_path) -> None:
    exporter = open_exporter(tmp_path / "x.sqlite")
    exporter.write({"a": 1})
    exporter.write({"a": 2, "b": {"nested": True}, "weird key!": "ok"})
    exporter.close()
    conn = sqlite3.connect(tmp_path / "x.sqlite")
    assert conn.execute('SELECT a, b, "weird_key_" FROM items ORDER BY a').fetchall() == [
        (1, None, None),
        (2, '{"nested": true}', "ok"),
    ]


def test_jsonl_exporter_is_buffered_but_complete(tmp_path) -> None:
    exporter = open_exporter(tmp_path / "x.jsonl")
    for i in range(500):
        exporter.write({"i": i, "text": "ü"})
    exporter.close()
    rows = [json.loads(line) for line in (tmp_path / "x.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 500 and rows[-1] == {"i": 499, "text": "ü"}


def test_progress_line_renders(site) -> None:
    from wintergrab.spider.engine import Engine

    class Tiny(wg.Spider):
        log_level = None
        start_urls = [site.url + "/product/1"]
        max_pages = 10

    engine = Engine(Tiny())
    engine.stats.update({"pages": 5, "items": 3, "cache_hits": 2, "retries": 1})
    engine._started -= 10
    stream = io.StringIO()
    display = ProgressDisplay(engine, stream=stream)
    display.draw()
    line = stream.getvalue()
    assert "5 pages" in line and "3 items" in line and "2 cached" in line and "1 retries" in line
    assert not ProgressDisplay.supported(io.StringIO())
