"""Self-healing extractors and the review queue."""

from __future__ import annotations

import json

import pytest

from wintergrab.data import Schema
from wintergrab.errors import ConfigurationError
from wintergrab.extraction.healing import ExtractorVersions, HealingExtractor
from wintergrab.extraction.review import ReviewQueue, pack_html, unpack_html
from wintergrab.fetchers.response import Response

SELLERS = ["Acme", "Globex", "Initech", "Umbrella"]
SCHEMA = {
    "name": "product",
    "fields": {
        "name": {"type": "string", "selectors": ["h1.title"]},
        "price": {"type": "money", "selectors": [".price"]},
        "seller": {"type": "string", "selectors": [".seller-name"]},
        "url": "url",
    },
}


def ld(i: int) -> str:
    data = {"@type": "Product", "name": f"Phone {i}", "offers": {"price": f"{100 + i}.00", "priceCurrency": "USD"}}
    return f'<script type="application/ld+json">{json.dumps(data)}</script>'


def page(i: int, body: str, *, structured: bool = True) -> Response:
    html = f"<html><head>{ld(i) if structured else ''}</head><body><nav><a href='/'>Home</a></nav>{body}</body></html>"
    return Response(f"https://shop.example/p/{i}", headers={"content-type": "text/html"}, body=html.encode())


def old(i: int) -> Response:
    return page(i, f"<h1 class='title'>Phone {i}</h1><span class='price'>${100 + i}.00</span>"
                   f"<div class='seller'><span class='seller-name'>{SELLERS[i % 4]}</span></div>")  # fmt: skip


def new(i: int) -> Response:
    return page(i, f"<h1 class='product-title'>Phone {i}</h1><div class='cost'><b>${100 + i}.00</b></div>"
                   f"<div class='seller-box'><a class='vendor' href='/s/{i % 4}'>{SELLERS[i % 4]}</a></div>")  # fmt: skip


def test_review_queue(tmp_path) -> None:
    path = tmp_path / "reviews.jsonl"
    queue = ReviewQueue(path)
    item = queue.add("value", "price", url="https://a.example/p/1", html=b"<p>$799</p>",
                     candidates=[{"value": 799, "confidence": 0.87, "method": "json-ld"},
                                 {"value": 999, "confidence": 0.41, "method": "dom"}])  # fmt: skip
    other = queue.add("broken", "seller", details={"reason": "no candidate"})
    assert (item.id, other.id) == ("r1", "r2") and len(queue.pending()) == 2
    assert "A: 799  (87% via json-ld)" in item.describe() and "B: 999  (41% via dom)" in item.describe()
    queue.decide("r1", "accept", choice=1, note="the sale price")
    again = ReviewQueue(path)  # decisions are kept in the file
    assert again.get("r1").status == "accepted" and again.get("r1").chosen == 999
    assert again.get("r1").decision["note"] == "the sale price" and unpack_html(again.get("r1").html) == b"<p>$799</p>"
    again.decide("r2", "correct", value=".vendor")
    assert ReviewQueue(path).get("r2").chosen == ".vendor" and ReviewQueue(path).pending() == []
    for bad in (lambda: again.decide("r9", "accept"), lambda: again.decide("r1", "maybe"),
                lambda: again.decide("r1", "correct"), lambda: again.decide("r1", "accept", choice=5)):  # fmt: skip
        with pytest.raises(ConfigurationError):
            bad()
    assert pack_html(b"x" * 10_000_000) is None or len(pack_html(b"x" * 10_000_000) or "") <= 400_000


def test_healing_repairs_what_it_can_and_asks_about_the_rest(tmp_path) -> None:
    folder, reviews = tmp_path / "product", tmp_path / "reviews.jsonl"
    healer = HealingExtractor(folder, SCHEMA, review=reviews)
    for i in range(15):
        assert healer.extract(old(i)).data["seller"] == SELLERS[i % 4]
    for i in range(15, 40):
        record = healer.extract(new(i))
    # name and price: anchored on the values JSON-LD still gives, and applied
    name = record.fields["name"]
    assert name.value == "Phone 39" and "selector" in (name.method, *name.agreed)
    assert record.data["price"] == {"amount": 139, "currency": "USD"}
    versions = healer.versions
    assert [v.reason for v in versions.versions[1:3]] == [
        "repair of name: h1.title -> .product-title (anchored)",
        "repair of price: .price -> div.cost > b (anchored)",
    ]
    assert versions.schema()["price"].selectors == ["div.cost > b", ".price"]  # the old one kept behind
    # seller: only resemblance to go on (70%), so a person decides
    assert record.data["seller"] is None
    candidate = versions.versions[3]
    assert candidate.status == "candidate" and candidate.reason.endswith(".vendor (relocated)")
    item = ReviewQueue(reviews).pending()[0]
    assert item.kind == "repair" and [c["selector"] for c in item.candidates][:2] == [".vendor", "a.vendor"]
    assert item.candidates[0]["examples"][:2] == ["Umbrella", "Acme"]
    why = healer.why("seller", new(41))
    assert "selector .seller-name: 0 element(s) on this page" in why and "70% alike" in why
    assert f"waiting for review: {item.id} (repair)" in why
    events = [(e["event"], e.get("outcome")) for e in versions.history()]
    assert ("repair", "applied") in events and ("repair", "queued") in events and ("confirmed", None) in events
    healer.close()

    ReviewQueue(reviews).decide(item.id, "accept", choice=1)  # a.vendor
    healer = HealingExtractor(folder, review=reviews)  # applies the decision
    assert healer.versions.active.by == "human" and healer.versions.get(4).status == "accepted"
    assert healer.extract(new(50)).data["seller"] == "Initech"
    assert healer.versions.diff(3, 5) == ["~ seller: selectors ['.seller-name'] -> ['a.vendor', '.seller-name']"]
    for i in range(60, 75):
        healer.extract(new(i))
    healer.close()

    # a later run knows how things were: a new breakage is noticed at once
    def newer(i: int) -> Response:
        return page(i, f"<h1 class='product-title'>Phone {i}</h1><div class='cost'><b>${100 + i}.00</b></div>"
                       f"<div class='seller-box'><a class='merchant' href='/s/{i % 4}'>{SELLERS[i % 4]}</a></div>")  # fmt: skip

    later = HealingExtractor(folder, review=reviews)
    for i in range(80, 90):
        record = later.extract(newer(i))
    # the element looks just like the old one, but nothing else on the pages confirms what it reads: a person decides
    question = ReviewQueue(reviews).pending()[0]
    assert record.data["seller"] is None and question.details["selector"] == ".merchant"
    assert question.candidates[0]["confidence"] >= 0.9 and later.versions.active_number == 5


def test_a_repair_that_does_not_hold_is_rolled_back(tmp_path) -> None:
    schema = {"name": "product", "fields": {"price": {"type": "money", "selectors": [".price"]}}}
    healer = HealingExtractor(tmp_path / "x", schema, review=tmp_path / "r.jsonl")
    for i in range(15):
        healer.extract(page(i, f"<span class='price'>${100 + i}.00</span>"))
    for i in range(15, 22):  # a first redesign: repaired
        healer.extract(page(i, f"<div class='cost'><b>${100 + i}.00</b></div>"))
    assert healer.versions.active_number == 2
    healer.close()  # the run ends before the repair could be judged: the next one judges it
    healer = HealingExtractor(tmp_path / "x", review=tmp_path / "r.jsonl")
    for i in range(22, 40):  # ... which did not last
        healer.extract(page(i, f"<p><em class='amount'>${100 + i}.00</em></p>", structured=False))
    events = [e["event"] for e in healer.versions.history()]
    assert "rolled back" in events and healer.versions.get(2).status == "rolled back"
    rolled = [i for i in ReviewQueue(tmp_path / "r.jsonl") if "rolled back" in i.details.get("reason", "")]
    assert rolled and rolled[0].details["selector"] == "div.cost > b"


def test_resemblance_alone_does_not_repair(tmp_path) -> None:
    schema = {"name": "product", "fields": {"seller": {"type": "string", "selectors": [".seller-name"]}}}
    healer = HealingExtractor(tmp_path / "x", schema, review=tmp_path / "r.jsonl")
    for i in range(15):
        healer.extract(page(i, f"<div><span class='seller-name'>{SELLERS[i % 4]}</span></div>", structured=False))
    for i in range(15, 40):  # the seller is gone; a badge with the same text on every page looks alike
        healer.extract(page(i, "<div><span class='badge'>Sale</span></div>", structured=False))
    assert healer.versions.active_number == 1 and len(healer.versions.versions) == 1
    flagged = [e for e in healer.versions.history() if e.get("outcome") == "flagged"]
    assert flagged and "scored" in flagged[0]["reason"]
    assert ReviewQueue(tmp_path / "r.jsonl").pending()[0].kind == "broken"


def test_reviewed_values_become_fixtures(tmp_path) -> None:
    schema = {"name": "product", "fields": {"price": {"type": "money", "selectors": [".price"]}}}
    healer = HealingExtractor(tmp_path / "x", schema, review=tmp_path / "r.jsonl", review_below=0.99)
    disputed = page(1, "<span class='price'>$99.00</span>")  # the JSON-LD says 101
    healer.extract(disputed)
    queue = ReviewQueue(tmp_path / "r.jsonl")
    item = queue.pending("value")[0]
    assert [(c["value"]["amount"], c["method"]) for c in item.candidates] == [(99, "selector"), (101, "json-ld")]
    queue.decide(item.id, "correct", value="99.00")
    healer = HealingExtractor(tmp_path / "x", review=tmp_path / "r.jsonl")
    fixtures = healer.versions.fixtures()
    assert len(fixtures) == 1 and fixtures[0].expected == {"price": "99.00"} and b"$99.00" in fixtures[0].html
    assert healer.versions.check_fixtures() == []  # the active version reproduces it
    assert healer.apply_reviews() == 0  # decisions are applied once


def test_versions_by_hand(tmp_path) -> None:
    with pytest.raises(ConfigurationError, match="holds no extractor"):
        ExtractorVersions(tmp_path / "none")
    versions = ExtractorVersions(tmp_path / "v", SCHEMA)
    with pytest.raises(ConfigurationError, match="nothing to roll back"):
        versions.rollback(reason="test")
    schema = versions.schema()
    second = versions.add(schema, reason="by hand", by="human")
    assert versions.active_number == second.number == 2 and versions.rollback(reason="test").number == 1
    versions.activate(2, reason="again")
    assert versions.active_number == 2


def test_heal_and_review_commands(tmp_path, capsys) -> None:
    from wintergrab.cli import main

    folder = tmp_path / "product"
    healer = HealingExtractor(folder, SCHEMA, review=tmp_path / "r.jsonl")
    for i in range(15):
        healer.extract(old(i))
    for i in range(15, 40):
        healer.extract(new(i))
    healer.close()
    assert main(["heal", str(folder)]) == 0
    out = capsys.readouterr().out
    assert "v3 active      auto    repair of price" in out and "seller: selectors matched 100% at first" in out
    assert main(["heal", str(folder), "--log"]) == 0 and "repair: field=name" in capsys.readouterr().out
    assert main(["heal", str(folder), "--diff", "1", "2"]) == 0
    assert "~ name: selectors ['h1.title'] -> ['.product-title', 'h1.title']" in capsys.readouterr().out
    assert main(["review", str(tmp_path / "r.jsonl")]) == 0 and "A: .vendor" in capsys.readouterr().out
    assert main(["review", str(tmp_path / "r.jsonl"), "--accept", "r1", "--choice", "BB"]) == 1
    assert "--choice is a candidate's letter (A, B...) or number" in capsys.readouterr().err
    assert main(["review", str(tmp_path / "r.jsonl"), "--accept", "r1", "--choice", "2"]) == 0  # B
    assert "r1: accepted ('a.vendor')" in capsys.readouterr().out
    assert main(["heal", str(folder), "--review", str(tmp_path / "r.jsonl")]) == 0
    assert "human   repair of seller: -> a.vendor" in capsys.readouterr().out
    assert main(["heal", str(folder), "--rollback"]) == 0 and "is active again" in capsys.readouterr().out
    assert main(["heal", str(folder), "--check"]) == 0


def test_why_a_field_is_empty(site, tmp_path, capsys) -> None:
    from wintergrab.cli import main

    schema = tmp_path / "product.schema.json"
    schema.write_text(json.dumps(SCHEMA), encoding="utf-8")
    args = ["get", site.url + "/product/3", "--extract", str(schema), "--heal", str(tmp_path / "g"), "--why", "seller"]
    assert main(args) == 0
    out, err = capsys.readouterr()
    assert json.loads(out)["seller"] is None
    assert (
        "seller (string) in product@1, version 1" in err and "selector .seller-name: 0 element(s) on this page" in err
    )
    assert main(["get", site.url + "/product/3", "--extract", str(schema), "--why", "seller"]) == 0  # any extractor
    err = capsys.readouterr().err
    assert "seller (string) on " in err and "possibly: the page's layout changed" in err
    assert main(["get", site.url + "/product/3", "--extract", str(schema), "--why", "colour"]) == 2
    assert "the schema has no field 'colour'" in capsys.readouterr().err


def test_one_question_per_field(tmp_path) -> None:
    folder, reviews = tmp_path / "x", tmp_path / "r.jsonl"
    schema = {"name": "product", "fields": {"seller": {"type": "string", "selectors": [".seller-name"]}}}

    def seller(i: int, markup: str) -> Response:
        return page(i, markup.format(SELLERS[i % 4]), structured=False)

    healer = HealingExtractor(folder, schema, review=reviews)
    for i in range(15):
        healer.extract(seller(i, "<div class='seller'><span class='seller-name'>{}</span></div>"))
    for i in range(15, 30):  # only resemblance to go on: a person is asked
        healer.extract(seller(i, "<div class='seller-box'><a class='vendor'>{}</a></div>"))
    healer.close()
    assert [(i.id, i.kind, i.details["selector"]) for i in ReviewQueue(reviews)] == [("r1", "repair", ".vendor")]
    healer = HealingExtractor(folder, review=reviews)  # the next run finds the same: nothing new to ask
    for i in range(30, 45):
        healer.extract(seller(i, "<div class='seller-box'><a class='vendor'>{}</a></div>"))
    healer.close()
    assert len(ReviewQueue(reviews)) == 1 and len(healer.versions.versions) == 2
    assert [e["outcome"] for e in healer.versions.history() if e["event"] == "repair"] == ["queued", "waiting"]
    healer = HealingExtractor(folder, review=reviews)  # the site changed again: the new question replaces it
    for i in range(45, 60):
        healer.extract(seller(i, "<div class='seller-box'><a class='merchant'>{}</a></div>"))
    queue = ReviewQueue(reviews)
    assert queue.get("r1").status == "rejected" and queue.get("r1").decision["by"] == "auto"
    assert [(i.id, i.details["selector"]) for i in queue.pending()] == [("r2", ".merchant")]
    assert [v.status for v in healer.versions.versions] == ["active", "superseded", "candidate"]


def test_crawl_heals(site, tmp_path) -> None:
    from wintergrab.cli import main

    schema = tmp_path / "product.schema.json"
    fields = {"name": {"type": "string", "selectors": ["h1"]}, "price": {"type": "money", "selectors": ["p.price"]}}
    schema.write_text(json.dumps({"name": "product", "fields": {**fields, "url": "url"}}), encoding="utf-8")
    common = ["--extract", str(schema), "--heal", str(tmp_path / "ext"), "--review", str(tmp_path / "r.jsonl"),
              "--max-pages", "40", "--no-progress"]  # fmt: skip
    shop = [site.url + "/products/page/1", "--follow", "a[href^='/product/']", "--paginate", "--allow", "/product"]
    assert main(["crawl", *shop, *common, "-o", str(tmp_path / "a.jsonl")]) == 0
    baseline = ExtractorVersions(tmp_path / "ext").load_state()["fields"]["price"]["baseline"]
    assert 0.5 <= baseline < 1  # the 5 listing pages have no p.price
    # "the redesign": the books section, whose prices are p.price_color
    books = [site.url + "/books/", "--follow", "a", "--paginate", "--allow", "/books/"]
    assert main(["crawl", *books, *common, "-o", str(tmp_path / "b.jsonl")]) == 0
    versions = ExtractorVersions(tmp_path / "ext")
    assert versions.active.reason == "repair of price: p.price -> .price_color (anchored)"
    assert ReviewQueue(tmp_path / "r.jsonl").pending() == []  # listing pages' prices are not questions
    rows = [json.loads(line) for line in (tmp_path / "b.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(r["price"]["currency"] == "GBP" for r in rows if "/catalogue/book-" in r["url"])


def test_the_questions_go_in_the_extractors_directory_unless_told_otherwise(site, tmp_path, capsys) -> None:
    from argparse import Namespace

    from wintergrab.cli import _extractor, main

    schema = tmp_path / "product.schema.json"
    schema.write_text(json.dumps({"name": "product", "fields": {"name": {"type": "string", "selectors": ["h1"]}}}))
    args = Namespace(extract=str(schema), heal=str(tmp_path / "ext"), review=None, provenance=False, model=None)
    assert _extractor(args).review.path == tmp_path / "ext" / "review.jsonl"
    args.review = str(tmp_path / "elsewhere.jsonl")
    assert _extractor(args).review.path == tmp_path / "elsewhere.jsonl"
    args.heal = None
    with pytest.raises(SystemExit, match="--review needs --heal"):
        _extractor(args)
    with pytest.raises(SystemExit, match="--review needs --heal"):
        main(["crawl", site.url + "/product/1", "--extract", str(schema), "--review", str(tmp_path / "r.jsonl")])
    assert main(["get", site.url + "/product/1", "--extract", str(schema), "--heal", str(tmp_path / "g")]) == 0
    capsys.readouterr()
    assert not (tmp_path / "g" / "review.jsonl").exists()  # (no question: no file)
    assert main(["heal", str(tmp_path / "g")]) == 0  # (reads the same queue)


def test_fixtures_hold_typed_values_as_json_does(tmp_path) -> None:
    from wintergrab.data.normalize import Money

    schema = {"name": "product", "fields": {"price": {"type": "money", "selectors": ["p.price"]}}}
    versions = ExtractorVersions(tmp_path / "ext", schema)
    kept = versions.add_fixture("https://s.example/1", "<p class='price'>$9.50</p>", {"price": Money(9.5, "USD")})
    assert kept.expected == {"price": "9.5 USD"} and versions.fixtures()[0].expected == kept.expected
    assert versions.check_fixtures() == []  # (read back as the field's type: the same price)


def test_rolling_back_one_repair_keeps_the_others(tmp_path) -> None:
    schema = {"name": "product", "fields": {"name": {"type": "string", "selectors": ["h1.title"]},
                                            "price": {"type": "money", "selectors": [".price"]}}}  # fmt: skip
    healer = HealingExtractor(tmp_path / "x", schema, review=tmp_path / "r.jsonl")
    for i in range(15):
        healer.extract(old(i))
    for i in range(15, 22):
        healer.extract(new(i))
    assert [v.reason.split(":")[0] for v in healer.versions.versions[1:]] == ["repair of name", "repair of price"]
    for i in range(22, 34):  # the name moves again (and the JSON-LD is gone): its repair does not hold
        healer.extract(page(i, f"<h2 class='heading'>Phone {i}</h2><div class='cost'><b>${100 + i}.00</b></div>",
                            structured=False))  # fmt: skip
    versions = healer.versions
    assert versions.get(2).status == "rolled back" and versions.active.reason.startswith("rollback of version 2 (name)")
    assert versions.schema()["name"].selectors == ["h1.title"]
    assert versions.schema()["price"].selectors == ["div.cost > b", ".price"]  # the price's repair held
    judged = [(e["event"], e.get("version") or e.get("from")) for e in versions.history()]
    assert ("rolled back", 2) in judged and ("confirmed", 3) in judged
    assert healer._health["name"].baseline == 1.0  # still watched, with what was known of those selectors
    item = ReviewQueue(tmp_path / "r.jsonl").pending()[0]
    assert item.details["selector"] == ".product-title" and item.details["reason"].startswith("rolled back: name")


def test_anchoring_reads_values_as_the_field_does(tmp_path) -> None:
    from wintergrab.data import Schema
    from wintergrab.extraction.healing import _holding, _read, _root

    schema = Schema.from_dict({"name": "p", "fields": {"price": "money", "weight": "quantity", "name": "string"}})
    root = _root(b"<div><span class='a'>$1,139.00</span><b class='b'>139.00 USD</b><i>Phone 139</i>"
                 b"<em class='w'>1.5 kg</em><p>None</p><h1> Phone  X </h1></div>")  # fmt: skip

    def holding(name: str, value: object) -> list[str]:
        return [el.get("class") or el.tag for el in _holding(root, value, lambda t: _read(schema, name, t))]

    assert holding("price", {"amount": 139, "currency": "USD"}) == ["b", "i"]  # not $1,139.00
    assert holding("price", {"amount": 139, "currency": "EUR"}) == ["i"]  # the same number in dollars is not it
    assert holding("weight", {"value": 1.5, "unit": "kg"}) == ["w"]  # and never an element saying "None"
    assert holding("name", "Phone X") == ["h1"]


def test_decisions_about_what_is_gone(tmp_path, caplog) -> None:
    folder, reviews = tmp_path / "x", tmp_path / "r.jsonl"
    healer = HealingExtractor(folder, SCHEMA, review=reviews)
    for i in range(15):
        healer.extract(old(i))
    for i in range(15, 40):
        healer.extract(new(i))
    healer.close()
    healer.versions.add(Schema.from_dict({"name": "product", "fields": {"url": "url"}}), reason="by hand", by="human")
    ReviewQueue(reviews).decide("r1", "accept")  # about the seller, which the active version no longer has
    healer = HealingExtractor(folder, review=reviews)
    assert "does not have" in caplog.text and healer.versions.active.reason == "by hand"
    assert "seller: selectors" not in healer.status() and healer.extract(new(50)).data == {
        "url": "https://shop.example/p/50"
    }
