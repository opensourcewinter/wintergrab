from __future__ import annotations

import time

import pytest

import wintergrab as wg
from wintergrab import MemoryStorage, SQLiteStorage
from wintergrab.adaptive import fingerprint, similarity
from wintergrab.parser.selector import parse_document

URL = "https://shop.example/catalog"


def old_layout(n: int = 12) -> str:
    cards = "".join(
        f'<div class="product" data-id="{i}"><h2 class="name">Mug {i}</h2>'
        f'<span class="price">${i}.50</span><a href="/p/{i}">View</a></div>'
        for i in range(n)
    )
    return f'<html><body><h1 id="title">Catalog</h1><div id="products" class="grid">{cards}</div></body></html>'


def new_layout(start: int = 5, n: int = 12) -> str:
    """A redesign: renamed classes, extra wrappers, moved title, new items."""
    cards = "".join(
        f'<div class="product-card" data-id="{i}"><h2 class="product-name">Cup {i}</h2>'
        f'<div class="meta"><span class="price-tag">€{i * 2}.99</span></div><a href="/p/{i}">Details</a></div>'
        for i in range(start, start + n)
    )
    return (
        '<html><body><header><h1 class="page-title">Catalog</h1></header>'
        f'<main><div class="catalog-grid">{cards}</div></main></body></html>'
    )


def test_selectors_survive_a_redesign() -> None:
    store = MemoryStorage()
    before = wg.Selector(old_layout(), url=URL, adaptive_storage=store)
    assert len(before.css("#products .product", adaptive=True)) == 12
    assert before.css("h1#title::text", adaptive=True).get() == "Catalog"
    assert before.css(".product .price::text", adaptive=True).get() == "$0.50"

    after = wg.Selector(new_layout(), url=URL, adaptive_storage=store)
    assert after.css("#products .product") == []  # the plain selector is broken...
    cards = after.css("#products .product", adaptive=True)  # ...but adaptive finds them
    assert len(cards) == 12
    assert all(c.attr("class") == "product-card" for c in cards)
    assert after.css("h1#title::text", adaptive=True).get() == "Catalog"
    prices = after.css(".product .price::text", adaptive=True).getall()
    assert prices[:2] == ["€10.99", "€12.99"]


def test_relocation_updates_the_saved_fingerprint() -> None:
    store = MemoryStorage()
    wg.Selector(old_layout(), url=URL, adaptive_storage=store).css("h1#title", adaptive=True)
    wg.Selector(new_layout(), url=URL, adaptive_storage=store).css("h1#title", adaptive=True)
    saved = store.load("shop.example", "h1#title")
    assert saved and saved["elements"][0]["attrs"] == {"class": "page-title"}


def test_unrelated_page_is_not_matched() -> None:
    store = MemoryStorage()
    wg.Selector(old_layout(), url=URL, adaptive_storage=store).css("h1#title", adaptive=True)
    other = wg.Selector(
        "<html><body><div><p>Hello</p><span>x</span></div></body></html>", url=URL, adaptive_storage=store
    )
    assert other.css("h1#title", adaptive=True) == []


def test_auto_save_only_and_identifier() -> None:
    store = MemoryStorage()
    wg.Selector(old_layout(), url=URL, adaptive_storage=store).css(".product", auto_save=True, identifier="cards")
    assert store.load("shop.example", "cards")["count"] == 12
    after = wg.Selector(new_layout(), url=URL, adaptive_storage=store)
    assert after.css(".product", auto_save=True, identifier="cards") == []  # auto_save never relocates
    assert len(after.css(".totally-new-selector", adaptive=True, identifier="cards")) == 12


def test_adaptive_xpath_with_text_tail() -> None:
    store = MemoryStorage()
    wg.Selector(old_layout(), url=URL, adaptive_storage=store).xpath("//div[@class='product']/h2/text()", adaptive=True)
    after = wg.Selector(new_layout(), url=URL, adaptive_storage=store)
    names = after.xpath("//div[@class='product']/h2/text()", adaptive=True).getall()
    assert names[:2] == ["Cup 5", "Cup 6"]


def test_domain_scoping() -> None:
    store = MemoryStorage()
    wg.Selector(old_layout(), url="https://www.a.example/", adaptive_storage=store).css("h1#title", adaptive=True)
    assert store.load("a.example", "h1#title") is not None  # www. is ignored
    other_site = wg.Selector(new_layout(), url="https://b.example/", adaptive_storage=store)
    assert other_site.css("h1#title", adaptive=True) == []


def test_sqlite_storage_roundtrip(tmp_path) -> None:
    db = SQLiteStorage(tmp_path / "fp.sqlite3")
    db.save("x.example", "sel", {"count": 1, "elements": []})
    assert db.load("x.example", "sel") == {"count": 1, "elements": []}
    assert db.identifiers() == [("x.example", "sel")]
    db.delete("x.example", "sel")
    assert db.load("x.example", "sel") is None
    db.close()


def test_default_storage_is_used_when_none_given(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WINTERGRAB_ADAPTIVE_DB", str(tmp_path / "default.sqlite3"))
    wg.Selector(old_layout(), url=URL).css("h1#title", adaptive=True)
    assert (tmp_path / "default.sqlite3").exists()
    assert wg.Selector(new_layout(), url=URL).css("h1#title", adaptive=True).text == "Catalog"


def test_similarity_scores() -> None:
    root = parse_document(old_layout())
    a, b = root.xpath("//div[@class='product']")[:2]
    title = root.xpath("//h1")[0]
    assert similarity(fingerprint(a), fingerprint(a)) == pytest.approx(1.0)
    assert similarity(fingerprint(a), fingerprint(b)) > 0.8
    assert similarity(fingerprint(a), fingerprint(title)) < 0.5


def test_relocation_is_fast_on_big_pages() -> None:
    store = MemoryStorage()
    rows = "".join(
        f'<tr class="row"><td class="c1">Item {i}</td><td><a href="/i/{i}">x</a></td></tr>' for i in range(2000)
    )
    html = f"<html><body><table>{rows}</table></body></html>"
    wg.Selector(html, url=URL, adaptive_storage=store).css("tr.row", adaptive=True)
    changed = wg.Selector(html.replace('class="row"', 'class="line"'), url=URL, adaptive_storage=store)
    started = time.perf_counter()
    assert len(changed.css("tr.row", adaptive=True)) == 2000
    assert time.perf_counter() - started < 5
