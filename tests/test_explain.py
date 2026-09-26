"""Why a field is what it is, or empty: what was seen, and causes that say how sure they are."""

from __future__ import annotations

import pytest

from wintergrab.extraction import Extractor, FieldDiagnosis
from wintergrab.fetchers.response import Response

PRODUCT = {"name": "product", "fields": {"name": {"type": "string", "required": True}, "price": "money",
                                         "currency": "currency", "rating": "rating"}}  # fmt: skip


def _page(body: str, *, url: str = "https://shop.example/p/1", status: int = 200) -> Response:
    return Response(url, status=status, headers={"content-type": "text/html; charset=utf-8"}, body=body.encode())


def test_a_value_found_says_where_it_came_from() -> None:
    ld = '<script type="application/ld+json">{"@type": "Product", "name": "Lamp", "offers": {"price": "12.99", "priceCurrency": "USD"}}</script>'
    diagnosis = Extractor(PRODUCT).why(
        "price", _page(f"<html><head>{ld}</head><body><h1>Lamp</h1><p class='price'>$12.99</p></body></html>")
    )
    assert isinstance(diagnosis, FieldDiagnosis) and diagnosis.status == "found" and diagnosis.value == 12.99
    assert any(line.startswith("JSON-LD: '12.99'") for line in diagnosis.seen)
    assert any(line.startswith("kept: 12.99 from json-ld") and "agreeing with" in line for line in diagnosis.seen)
    assert diagnosis.causes == [] and {c["method"] for c in diagnosis.candidates if c["kept"]} >= {"json-ld"}
    assert diagnosis.describe().startswith("price (money) on https://shop.example/p/1: 12.99")
    assert diagnosis.to_dict()["status"] == "found" and str(diagnosis) == diagnosis.describe()
    with pytest.raises(KeyError, match="no field 'colour'"):
        Extractor(PRODUCT).why("colour", "<p>x</p>")


def test_the_layout_changed_and_a_selector_that_reads_the_value_now() -> None:
    schema = {"name": "product", "fields": {"name": "string", "price": {"type": "money", "selectors": [".price"]}}}
    moved = _page("<html><body><h1>Lamp</h1><div class='buy'><span class='amount'>$12.99</span></div></body></html>")
    diagnosis = Extractor(schema).why("price", moved)
    assert "selector .price: 0 element(s) on this page" in diagnosis.seen
    assert diagnosis.status == "found"  # the page's layout still gives it
    [cause] = diagnosis.causes
    assert cause.startswith("likely: the page's layout changed: the field's selectors find nothing, but")
    assert "span.amount reads it on this page" in cause


def test_values_too_unsure_unreadable_or_invalid() -> None:
    page = _page("<html><body><h1>Lamp</h1><p class='price'>$12.99</p><p class='rating'>N/A</p></body></html>")
    unsure = Extractor(PRODUCT, min_confidence=0.95).why("price", page)
    assert unsure.status == "unsure" and unsure.value is None
    assert (
        unsure.causes[0].startswith("certain: 12.99 USD was found (dom/pattern)")
        and "under the extractor's 0.95" in unsure.causes[0]
    )
    schema = {"name": "product", "fields": {"name": "string", "rating": {"type": "rating", "selectors": [".rating"]}}}
    unreadable = Extractor(schema).why("rating", page)
    assert (
        unreadable.status == "unreadable"
        and unreadable.causes[0] == "certain: what was found ('N/A') cannot be read as rating"
    )
    bounded = {"name": "product", "fields": {"name": "string", "price": {"type": "money", "minimum": 100}}}
    invalid = Extractor(bounded).why("price", page)
    assert (
        invalid.status == "invalid"
        and invalid.causes[0] == "certain: 12.99 USD was found, and breaks a rule: 12.99 is below the minimum 100"
    )


def test_what_the_page_is_explains_an_empty_field() -> None:
    extractor = Extractor(PRODUCT)
    gone = extractor.why("price", _page("<html><body><h1>Not found</h1></body></html>", status=404))
    assert gone.status == "empty" and gone.causes == [
        "certain: the page answered HTTP 404: it is not the page with the value"
    ]
    check = "<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"
    blocked = extractor.why("price", _page(check, status=403))
    assert blocked.causes == ["likely: this is a bot-check or access page (HTTP 403), not the content"]
    shell = "<html><body><div id='root'></div>" + "<script src='/a.js'></script>" * 3 + "</body></html>"
    app = extractor.why("price", _page(shell))
    assert app.causes[0].startswith("likely: the content is drawn by JavaScript (its app mount point (#root) is empty)")
    cards = "".join(
        f"<div class='product-card'><h3><a href='/p/{i}'>Lamp {i}</a></h3><span class='price'>${i}.99</span></div>"
        for i in range(1, 9)
    )
    listing = extractor.why(
        "rating", _page(f"<html><body><h1>Lamps</h1>{cards}</body></html>", url="https://shop.example/category/lamps")
    )
    assert any(c.startswith("likely: the page lists products") and "(--all, a container)" in c for c in listing.causes)
    plain = extractor.why("rating", _page("<html><body><h1>Lamp</h1><p class='price'>$12.99</p></body></html>"))
    assert plain.status == "empty" and plain.causes == [
        "possibly: the page does not state its rating (no strategy found anything)"
    ]
    assert "no strategy found a candidate value" in plain.seen
