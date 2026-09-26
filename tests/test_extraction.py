"""The extraction engine: strategies, confidence, provenance, listings, models and calibration."""

from __future__ import annotations

import asyncio
import json

import pytest

from wintergrab.cli import main
from wintergrab.data import Schema
from wintergrab.errors import ConfigurationError
from wintergrab.extraction import (
    DEFAULT_PRIORS,
    Extractor,
    ModelRequest,
    PageContext,
    field_kind,
    grounding,
    value_key,
)
from wintergrab.extraction.model import parse_answer

PRODUCT = {
    "name": "product",
    "key": ["url"],
    "fields": {
        "name": {"type": "string", "required": True},
        "brand": "string",
        "price": {"type": "money", "required": True},
        "list_price": "money",
        "currency": "currency",
        "availability": "availability",
        "rating": {"type": "rating", "best": 5},
        "review_count": "integer",
        "sku": "string",
        "weight": {"type": "quantity", "unit": "kg"},
        "image": "url",
        "description": "text",
        "url": "url",
    },
}

JSON_LD_PAGE = """<html lang="en"><head><title>Phone X | Acme Shop</title>
<meta property="og:title" content="Phone X"><meta property="og:site_name" content="Acme Shop">
<meta property="og:image" content="/img/px.jpg"><meta name="description" content="A great phone with a big screen">
<link rel="canonical" href="https://shop.example/p/phone-x">
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "Product", "name": "Phone X",
 "brand": {"@type": "Brand", "name": "Acme"}, "sku": "PX-1",
 "offers": {"@type": "Offer", "price": "299.99", "priceCurrency": "USD", "availability": "https://schema.org/InStock"},
 "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.5", "reviewCount": "120"}}</script>
</head><body><h1>Phone X</h1>
<div class="product-price"><span class="price">$299.99</span> <del class="price old-price">$349.99</del></div>
<div class="stock">In stock</div><div class="rating" aria-label="4.5 out of 5 stars"></div><a class="reviews">120 reviews</a>
<table><tr><th>Weight</th><td>450 g</td></tr><tr><th>Colour</th><td>Black</td></tr></table>
<div class="description">A great phone with a big screen and a long battery life.</div>
<button>Add to cart</button><div class="related"><span class="price">$19.99</span></div></body></html>"""

PLAIN_PAGE = """<html><body><h1>Garden Chair</h1><p class="price">€ 49,95</p>
<ul><li>SKU: GC-778</li><li>Weight: 3.2 kg</li><li>Brand: Hortus</li></ul>
<p>Only 3 left in stock</p><p>4.2/5 (37 ratings)</p><img class="product-image" src="/c.jpg"></body></html>"""


@pytest.fixture
def extractor() -> Extractor:
    return Extractor(PRODUCT)


def test_structured_data_wins_and_agreement_is_recorded(extractor: Extractor) -> None:
    record = extractor.extract(JSON_LD_PAGE, url="https://shop.example/p/phone-x?utm_source=ad")
    assert record.data == {
        "name": "Phone X",
        "brand": "Acme",
        "price": 299.99,
        "list_price": 349.99,
        "currency": "USD",
        "availability": "InStock",
        "rating": 4.5,
        "review_count": 120,
        "sku": "PX-1",
        "weight": 0.45,
        "image": "https://shop.example/img/px.jpg",
        "description": "A great phone with a big screen and a long battery life.",
        "url": "https://shop.example/p/phone-x",
    }
    price = record.fields["price"]
    assert (price.method, price.source) == ("json-ld", "json-ld:Product.offers.price")
    assert set(price.agreed) >= {"dom", "pattern"}
    assert price.confidence > 0.85 and price.validation == "ok"
    assert record.fields["list_price"].method == "dom"  # the struck-through price
    assert record.fields["weight"].method == "label"
    assert record.fields["name"].confidence > record.fields["url"].confidence
    assert record.fields["description"].agreed == ["dom"]  # the meta description is part of the full text
    assert record.valid and 0.8 < record.confidence < 1


def test_provenance_and_to_dict(extractor: Extractor) -> None:
    record = extractor.extract(JSON_LD_PAGE, url="https://shop.example/p/phone-x")
    plain = record.to_dict()
    assert "_provenance" not in plain and plain["_confidence"] == record.confidence
    full = record.to_dict(provenance=True)["_provenance"]
    assert full["url"] == "https://shop.example/p/phone-x"
    assert full["extractor"] == "product@1"
    assert full["fetched_at"].endswith("+00:00")
    assert full["fields"]["price"]["method"] == "json-ld"
    assert full["fields"]["price"]["validation"] == "ok"
    assert "sku" in full["fields"]
    assert Extractor(PRODUCT, provenance=True).extract(JSON_LD_PAGE).to_dict()["_provenance"]
    assert record["price"] == 299.99 and record.get("nope", 1) == 1


def test_microdata_and_opengraph() -> None:
    microdata = """<div itemscope itemtype="https://schema.org/Product"><h1 itemprop="name">Phone Y</h1>
    <div itemprop="offers" itemscope itemtype="https://schema.org/Offer"><span itemprop="price" content="199.00">$199</span>
    <meta itemprop="priceCurrency" content="USD"></div></div>"""
    record = Extractor(PRODUCT).extract(microdata)
    assert record.data["price"] == 199 and record.fields["price"].method == "microdata"
    opengraph = """<html><head><meta property="og:title" content="Kettle">
    <meta property="product:price:amount" content="25.00"><meta property="product:price:currency" content="GBP"></head></html>"""
    record = Extractor(PRODUCT).extract(opengraph)
    assert (record.data["name"], record.data["price"], record.data["currency"]) == ("Kettle", 25, "GBP")
    assert record.fields["price"].method == "opengraph"


def test_pages_without_structured_data(extractor: Extractor) -> None:
    record = extractor.extract(PLAIN_PAGE, url="https://garden.example/chair")
    assert record.data["name"] == "Garden Chair"
    assert (record.data["price"], record.data["currency"]) == (49.95, "EUR")
    assert record.fields["currency"].method == "derived:price"
    assert record.data["sku"] == "GC-778" and record.fields["sku"].method == "label"
    assert record.data["brand"] == "Hortus" and record.data["weight"] == 3.2
    assert record.data["availability"] == "LimitedAvailability"
    assert record.data["rating"] == 4.2 and record.data["review_count"] == 37
    assert record.data["image"] == "https://garden.example/c.jpg"
    assert record.fields["description"].validation == "absent"


def test_dom_price_heuristics_and_long_texts() -> None:
    schema = {"name": "product", "fields": {"price": "money", "list_price": "money", "review_count": "integer"}}
    html = f"""<header><span class="price">$1.00</span></header>
    <div class="product"><div class="price-box"><span class="price">$249.00</span>
    <s><span class="price">$299.00</span></s></div><p>Rated 4.8/5 from 2 301 reviews</p></div>
    <main><article><header><h1>Chair</h1></header></article></main><div role="banner"><b class="price">$2</b></div>
    <div class="related-products"><span class="price">$19.99</span></div><footer><span class="price">$5</span></footer>
    <p>{"lorem ipsum " * 20000}</p>"""
    record = Extractor(schema).extract(html)
    # the wrapper (.price-box) and prices in the header, related products and footer are not the price;
    # a price inside <s> is the old price
    assert record.data["price"] == {"amount": 249, "currency": "USD"}
    assert record.fields["price"].source == "dom:span.price"
    assert record.data["list_price"] == {"amount": 299, "currency": "USD"}
    assert record.data["review_count"] == 2301
    # a product's own <header> is content
    own = '<article class="product"><header><h1>Chair</h1><span class="price">$10</span></header></article>'
    assert Extractor(schema).extract(own).data["price"] == {"amount": 10, "currency": "USD"}


def test_ratings_written_in_class_names() -> None:
    schema = {"name": "product", "fields": {"name": "string", "rating": {"type": "rating", "best": 5}}}
    books = '<h1>A Light in the Attic</h1><p class="star-rating Three"></p>'
    record = Extractor(schema).extract(books)
    assert record.data["rating"] == 3 and record.fields["rating"].source == "dom:class"
    assert Extractor(schema).extract('<h1>X</h1><div class="stars stars-4-5"></div>').data["rating"] == 4.5
    # digits in layout classes are not ratings
    assert Extractor(schema).extract('<h1>X</h1><div class="col-md-4 rating"></div>').data.get("rating") is None


def test_selectors_and_embedded_json() -> None:
    schema = {"name": "item", "fields": {
        "title": {"type": "string", "selectors": [".t::text"]},
        "stock": {"type": "integer", "selectors": [".s"]},
        "review_count": "integer",
    }}  # fmt: skip
    html = """<p class="t">Hello</p><p class="s">5 left</p><p class="s">7 left</p>
    <script>window.__STATE__ = {"product": {"reviewCount": 12, "related": []}}</script>"""
    record = Extractor(schema).extract(html)
    assert record.data == {"title": "Hello", "stock": 5, "review_count": 12}
    assert record.fields["title"].method == "selector" and record.fields["title"].confidence > 0.9
    assert record.fields["stock"].confidence < record.fields["title"].confidence  # two different matches
    assert record.fields["review_count"].method == "embedded-json"
    assert record.fields["review_count"].source == "embedded-json:__STATE__.product.reviewCount"


def test_conflicts_lower_confidence_and_are_kept() -> None:
    html = """<script type="application/ld+json">{"@type": "Product", "name": "A", "offers": {"price": "10.00", "priceCurrency": "EUR"}}</script>
    <h1>A</h1><span class="price">€12.00</span>"""
    record = Extractor(PRODUCT).extract(html)
    price = record.fields["price"]
    assert record.data["price"] == 10 and price.method == "json-ld"
    assert price.alternatives[0]["value"] == {"amount": 12, "currency": "EUR"}
    assert price.alternatives[0]["methods"] == ["dom", "pattern"]
    assert price.confidence < 0.8


def test_validation_and_minimum_confidence() -> None:
    schema = {"name": "product", "fields": {"name": "string", "price": {"type": "money", "maximum": 100}}}
    record = Extractor(schema).extract("""<script type="application/ld+json">{"@type": "Product", "name": "B",
        "offers": {"price": "500"}}</script>""")
    assert record.fields["price"].validation == "error:range"
    assert record.fields["price"].confidence < 0.5 and not record.valid
    noisy = "<p>Deals: $1 $2 $3 $4 $5 $6</p>"
    record = Extractor({"name": "x", "fields": {"price": "money"}}).extract(noisy)
    assert record.data["price"] is None and record.fields["price"].validation == "low-confidence"
    assert len(record.fields["price"].alternatives) == 3
    kept = Extractor({"name": "x", "fields": {"price": "money"}}, min_confidence=0).extract(noisy)
    assert kept.data["price"] == {"amount": 1, "currency": "USD"}  # no currency field: kept together


def test_listing_pages() -> None:
    cards = "".join(
        f"<div class='card'><h3><a href='/p/{i}'>Item {i}</a></h3><span class='price'>${i}.99</span>"
        f"<img src='/i/{i}.jpg'></div>"
        for i in range(1, 6)
    )
    page = f"<html><body><h1>Shop</h1><div class='grid'>{cards}</div><footer class='price'>$0</footer></body></html>"
    extractor = Extractor(
        {
            "name": "product",
            "fields": {"name": "string", "price": "money", "currency": "currency", "url": "url", "image": "url"},
        }
    )
    records = extractor.extract_all(page, url="https://s.example/c")
    assert [r.data["name"] for r in records] == [f"Item {i}" for i in range(1, 6)]
    assert records[0].data == {"name": "Item 1", "price": 1.99, "currency": "USD", "url": "https://s.example/p/1",
                               "image": "https://s.example/i/1.jpg"}  # fmt: skip
    assert records[0].fields["name"].method == "records"
    by_container = extractor.extract_all(page, container="div.card", url="https://s.example/c")
    assert [r.data["price"] for r in by_container] == [1.99, 2.99, 3.99, 4.99, 5.99]
    assert extractor.extract_all("<p>nothing repeats here</p>") == []


def test_listing_from_json_ld() -> None:
    items = [{"@type": "Product", "name": f"P{i}", "url": f"https://s.example/p{i}",
              "offers": {"@type": "Offer", "price": f"{i + 1}0.00", "priceCurrency": "EUR"}} for i in range(3)]  # fmt: skip
    ld = json.dumps({"@type": "ItemList", "itemListElement": [{"@type": "ListItem", "item": item} for item in items]})
    records = Extractor(PRODUCT).extract_all(f"<script type='application/ld+json'>{ld}</script>")
    assert [(r.data["name"], r.data["price"], r.data["currency"]) for r in records] == [
        ("P0", 10, "EUR"),
        ("P1", 20, "EUR"),
        ("P2", 30, "EUR"),
    ]


# --------------------------------------------------------------------------- #
# extraction models
# --------------------------------------------------------------------------- #
LAMP = "<html><body><h1>Lamp</h1><p>Our customers rate it 4.8 of 5. Now 12,99 €.</p></body></html>"
LAMP_SCHEMA = {"name": "product", "fields": {"name": {"type": "string", "required": True}, "price": "money", "currency": "currency",
                                             "rating": "rating", "color": "string"}}  # fmt: skip


def test_the_model_fills_gaps_and_is_grounded() -> None:
    asked: list[ModelRequest] = []

    def model(request: ModelRequest):
        asked.append(request)
        return '```json\n{"rating": "4.8", "color": "Midnight blue", "price": "12.99", "name": "Invented"}\n```'

    record = Extractor(LAMP_SCHEMA, model=model, min_confidence=0.1).extract(LAMP, url="https://s.example/lamp")
    [request] = asked
    assert [f.name for f in request.fields] == ["rating", "color"]  # name and price were found without it
    assert request.known["name"] == "Lamp" and "Lamp" in request.text
    assert "Answer with one JSON object" in request.prompt()
    assert record.data["name"] == "Lamp"  # the model's "Invented" name was not asked for
    assert record.data["rating"] == 4.8 and record.fields["rating"].method == "model"
    color = record.fields["color"]
    assert color.value == "Midnight blue" and "not-on-page" in color.notes and color.confidence < 0.2
    assert Extractor(LAMP_SCHEMA, model=model).extract(LAMP).data["color"] is None  # below the default 0.3


def test_model_failures_and_async_models() -> None:
    def broken(request):
        raise RuntimeError("model offline")

    assert Extractor(LAMP_SCHEMA, model=broken).extract(LAMP).data["name"] == "Lamp"

    async def model(request):
        await asyncio.sleep(0)
        return {"rating": "4.8"}

    extractor = Extractor(LAMP_SCHEMA, model=model)
    with pytest.raises(ConfigurationError, match="aextract"):
        extractor.extract(LAMP)
    assert asyncio.run(extractor.aextract(LAMP)).data["rating"] == 4.8

    class Named:
        name = "local-llm"

        def extract(self, request):
            return {"color": "Red"}

    record = Extractor(LAMP_SCHEMA, model=Named(), min_confidence=0).extract(LAMP)
    assert record.fields["color"].source == "model:local-llm" and record.model == "local-llm"


def test_grounding_and_answer_parsing() -> None:
    text = "Price: ₹29,999. Rated 4.5 by 1,204 people. Colour: Midnight Blue."
    assert grounding("midnight blue", text) == "exact"
    assert grounding("29999", text) == "number"
    assert grounding(["4.5", "1204"], text) == "number"
    assert grounding("Crimson", text) == "none"
    assert parse_answer('Sure! {"a": 1} Hope that helps.') == {"a": 1}
    assert parse_answer("no json here") == {}
    assert parse_answer({"b": 2}) == {"b": 2}


# --------------------------------------------------------------------------- #
# calibration, helpers, explanations
# --------------------------------------------------------------------------- #
def test_calibration_measures_method_precision() -> None:
    schema = {"name": "product", "fields": {"name": "string", "price": "money"}}
    pages = [
        (f"""<script type="application/ld+json">{{"@type": "Product", "name": "P{i}", "offers": {{"price": "{i}.00"}}}}</script>
        <h1>Sale!</h1><span class="price">${i + 100}.00</span>""", {"name": f"P{i}", "price": i})
        for i in range(1, 7)
    ]  # fmt: skip
    extractor = Extractor(schema)
    measured = extractor.calibrate(pages)
    assert measured["json-ld"] == 0.99  # always right (capped)
    assert measured["dom"] == 0.05  # always wrong here (floored)
    assert extractor.priors["dom"] == 0.05 and DEFAULT_PRIORS["dom"] == 0.7


def test_explain_and_candidates(extractor: Extractor) -> None:
    text = extractor.explain(JSON_LD_PAGE, url="https://shop.example/p/phone-x")
    assert text.splitlines()[0].startswith("product@1 from https://shop.example/p/phone-x: confidence")
    assert any(line.split()[:3] == ["price", "299.99", "json-ld"] for line in text.splitlines()[2:])
    found = extractor.candidates(JSON_LD_PAGE)
    assert {c.method for c in found["price"]} >= {"json-ld", "dom", "pattern"}


def test_value_keys_and_field_kinds() -> None:
    from decimal import Decimal

    from wintergrab.data.normalize import Money, Quantity

    assert value_key(Money(Decimal("299.990"), "USD")) == value_key(Money(Decimal("299.99"), None)) == value_key(299.99)
    assert value_key(Quantity(Decimal(450), "g")) == value_key(Quantity(Decimal("0.45"), "kg"))
    assert value_key(" Hello, World ") == value_key("hello world")
    schema = Schema.from_dict(PRODUCT)
    kinds = {f.name: field_kind(f) for f in schema}
    assert kinds == {
        "name": "title", "brand": "brand", "price": "price", "list_price": "price", "currency": "currency",
        "availability": "availability", "rating": "rating", "review_count": "review_count", "sku": "sku",
        "weight": "other", "image": "image", "description": "description", "url": "url",
    }  # fmt: skip


def test_page_context_accepts_pages_and_rejects_others() -> None:
    import wintergrab as wg

    page = wg.parse("<p>x</p>", url="https://a.example/")
    assert PageContext(page).url == "https://a.example/"
    assert PageContext(b"<p>x</p>").text == "x"
    with pytest.raises(TypeError):
        PageContext(42)
    with pytest.raises(ConfigurationError):
        Extractor(42)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# command line and crawls
# --------------------------------------------------------------------------- #
def test_cli_get_extract(site, tmp_path, capsys) -> None:
    schema = tmp_path / "product.json"
    schema.write_text(
        json.dumps(
            {
                "name": "product",
                "fields": {"name": {"type": "string", "required": True}, "price": "money", "currency": "currency"},
            }
        )
    )
    assert main(["-q", "get", site.url + "/product/1", "--extract", str(schema), "--explain", "--provenance"]) == 0
    captured = capsys.readouterr()
    row = json.loads(captured.out)
    assert (row["name"], row["price"], row["currency"]) == ("Product 1", 6.25, "USD")
    name = row["_provenance"]["fields"]["name"]
    assert (name["method"], name["agreed"]) == ("meta", ["dom"])  # the <title> and the <h1> agree
    assert "product@1 from" in captured.err


def test_cli_crawl_extract(site, tmp_path, capsys) -> None:
    schema = tmp_path / "product.json"
    schema.write_text(
        json.dumps(
            {
                "name": "product",
                "fields": {"name": {"type": "string", "required": True}, "price": {"type": "money", "required": True}},
            }
        )
    )
    out = tmp_path / "items.jsonl"
    code = main(["-q", "crawl", site.url + "/product/1", "--extract", str(schema), "--max-depth", "0", "--no-robots",
                 "--no-autothrottle", "-o", str(out)])  # fmt: skip
    assert code == 0
    [item] = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert item["name"] == "Product 1" and item["price"] == {"amount": 6.25, "currency": "USD"}
    assert 0 < item["_confidence"] <= 1
