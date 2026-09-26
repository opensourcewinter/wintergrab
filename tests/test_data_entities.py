"""Entity resolution: names, evidence, merging, review, provenance."""

from __future__ import annotations

import json
import time

import pytest

from wintergrab.cli import main
from wintergrab.data.entities import EntityResolver, normalize_name


def resolve(kind: str, *mentions, **options):
    resolver = EntityResolver(kind, **options)
    for mention in mentions:
        if isinstance(mention, str):
            resolver.add(mention)
        else:
            name, attributes = mention
            resolver.add(name, **attributes)
    return resolver.resolve()


def groups(result) -> list[list[str]]:
    return [[m.name for m in entity.mentions] for entity in result.entities]


# ------------------------------------------------------------------------------------------ names


@pytest.mark.parametrize(
    ("text", "kind", "key", "qualifier"),
    [
        ("Apple Computer, Inc.", "company", "apple computer", "inc"),
        ("The Coca-Cola Company", "company", "coca cola", "company"),
        ("Procter & Gamble Co.", "company", "procter and gamble", "co"),
        ("P&G", "company", "pg", None),
        ("I.B.M.", "company", "ibm", None),
        ("Société Générale S.A.", "company", "societe generale", "sa"),
        ("Maersk A/S", "company", "maersk", "as"),
        ("McDonald's Corporation", "company", "mcdonalds", "corporation"),
        ("Smith & Co.", "company", "smith", "co"),
        ("Smith, Dr. John A., Jr.", "person", "john a smith", "jr"),
        ("Beethoven, Ludwig van", "person", "ludwig van beethoven", None),
        ("Jack Ma", "person", "jack ma", None),  # "ma" is a surname here, not a degree
        ("Martin Luther King Jr.", "person", "martin luther king", "jr"),
        ("Apple iPhone 15 Pro (128 GB) - Black", "product", "apple iphone 15 pro 128gb black", None),
        ("Sony WH-1000XM4", "product", "sony wh1000xm4", None),
        ('Samsung 55" QLED', "product", "samsung 55in qled", None),
        ("St. Louis", "location", "saint louis", None),
        ("Springfield, IL", "location", "springfield", "il"),
        ("City of London", "location", "london", None),
    ],
)
def test_names_are_normalized_for_their_kind(text: str, kind: str, key: str, qualifier: str | None) -> None:
    name = normalize_name(text, kind)
    assert (name.key, name.qualifier) == (key, qualifier)


def test_name_details() -> None:
    assert normalize_name("Apple Computer, Inc.").core == ("apple",)  # "computer" is generic
    assert normalize_name("iPhone 15 Pro 128GB", "product").codes == {"15", "128gb"}
    with pytest.raises(ValueError, match="unknown entity kind"):
        normalize_name("x", "planet")


# ------------------------------------------------------------------------------------------ organizations


def test_the_apple_example() -> None:
    result = resolve(
        "company", "Apple Inc.", "APPLE INC", "Apple", "Apple Computer, Inc.", "Apple Computer", "Apple Records"
    )
    assert groups(result) == [
        ["Apple Inc.", "APPLE INC", "Apple"],
        ["Apple Computer, Inc.", "Apple Computer"],
        ["Apple Records"],
    ]
    apple = result.entities[0]
    assert (apple.id, apple.name, apple.confidence) == ("company:apple", "Apple Inc.", 0.982)
    # "Apple" and "Apple Computer" may be one company or two: reviewed, not merged
    [pair] = result.review
    assert {pair.a.name, pair.b.name} == {"Apple Inc.", "Apple Computer, Inc."}
    assert (pair.score, pair.decision) == (0.818, "review")
    assert pair.reasons == ["+3.5 same name apart from 'computer'"]


def test_evidence_decides() -> None:
    result = resolve(
        "company",
        ("Apple Inc.", {"website": "https://www.apple.com"}),
        ("Apple Computer, Inc.", {"website": "apple.com/about"}),
        "Apple",
        ("Apple Records", {"website": "applerecords.com"}),
    )
    assert groups(result) == [["Apple Inc.", "Apple Computer, Inc.", "Apple"], ["Apple Records"]]
    reasons = [reason for link in result.entities[0].links for reason in link.reasons]
    assert "+4.0 same website: apple.com" in reasons
    assert result.review == []


def test_initials_need_support() -> None:
    alone = resolve("company", "IBM", "International Business Machines Corporation")
    assert len(alone.entities) == 2
    assert alone.review[0].reasons == ["+3.0 'IBM' is the initials of 'International Business Machines Corporation'"]
    backed = resolve(
        "company",
        ("IBM", {"website": "ibm.com"}),
        ("International Business Machines Corporation", {"email": "press@ibm.com"}),  # company e-mail domain
    )
    assert groups(backed) == [["IBM", "International Business Machines Corporation"]]


def test_legal_entities_and_organizations() -> None:
    names = ["Delta Stone", "Delta Stone Inc.", "Delta Stone Corp", "Delta Stone GmbH", "Delta Stone Ltd"]
    # a company is a legal entity: Inc./Corp. are one kind of company, GmbH and Ltd others
    company = resolve("company", *names)
    assert ["Delta Stone Inc.", "Delta Stone Corp"] in groups(company)
    assert ["Delta Stone"] in groups(company)  # as close to several legal entities: merged with none
    notes = [m.note for m in company.review if m.note]
    assert any(note.startswith("ambiguous: as close to") for note in notes)
    # an organization (or a brand) spans legal entities
    organization = resolve("organization", *names)
    assert len(organization.entities) == 1
    assert organization.entities[0].name == "Delta Stone"


def test_conflicting_identifiers_are_never_merged() -> None:
    result = resolve(
        "company",
        ("Acme Corp", {"lei": "5493001KJTIIGC8Y1R12"}),
        ("ACME Corporation", {"lei": "969500T3MBS4SQAMHJ45"}),
    )
    assert len(result.entities) == 2
    assert result.review == []  # -8 for different LEIs: clearly two companies
    result = resolve(
        "company",
        ("Acme Corp", {"lei": "5493001KJTIIGC8Y1R12"}),
        ("Acme Corp", {"lei": "5493001KJTIIGC8Y1R12", "website": "acme.com"}),
    )
    assert len(result.entities) == 1


def test_no_chains_through_different_entities() -> None:
    # "Delta Stone" matches both; the two are different organizations: no merge joins them
    result = resolve(
        "organization",
        "Delta Stone",
        ("Delta Stone Inc.", {"website": "deltastone.com", "country": "US"}),
        ("Delta Stone GmbH", {"website": "delta-stein.de", "country": "DE"}),
    )
    assert groups(result) == [["Delta Stone", "Delta Stone Inc."], ["Delta Stone GmbH"]]
    [pair] = result.review
    assert pair.note == "not merged: 'Delta Stone GmbH' and 'Delta Stone Inc.' look like different entities (0.076)"


def test_word_order_and_typos() -> None:
    assert resolve("company", "Blue Star", "Star Blue").review[0].reasons == ["+3.0 same words in another order"]
    typo = resolve("company", "Acme Widgets Ltd", "Acme Widgetz Ltd")
    assert typo.review[0].reasons == ["+4.1 similar names (0.90)"]


# ------------------------------------------------------------------------------------------ people, products, places


def test_people() -> None:
    result = resolve(
        "person",
        "John Smith",
        "John Smith",
        ("Smith, John", {"email": "john@acme.com"}),
        ("J. Smith", {"email": "John@Acme.com"}),
        "John Smith Jr.",
        ("Dr. Jane Smith", {"employer": "Acme Inc."}),
        ("Jane Smith", {"employer": "ACME"}),
    )
    names = groups(result)
    assert ["Smith, John", "J. Smith"] in names  # the same e-mail address
    assert ["Dr. Jane Smith", "Jane Smith"] in names  # the same name and employer
    assert ["John Smith"] in names and ["John Smith Jr."] in names
    john = next(e for e in result.entities if len(e.mentions) == 2 and e.mentions[0].name == "Smith, John")
    assert john.name == "Smith, John"  # a full name rather than initials
    # two "John Smith" with nothing else in common: one review item, not a merge
    same = [m for m in result.review if m.note == "2 mentions with the same name and details"]
    assert len(same) == 1 and same[0].score == 0.731
    jr = resolve("person", ("John Smith Jr.", {"email": "a@x.org"}), ("John Smith Sr.", {"email": "a@x.org"}))
    assert len(jr.entities) == 2  # generations never merge


def test_products() -> None:
    result = resolve(
        "product",
        ("Apple iPhone 15 Pro 128GB Black", {"brand": "Apple"}),
        ("iPhone 15 Pro (128 GB) - Black", {"brand": "Apple Inc."}),
        ("iPhone 14 Pro 128GB Black", {"brand": "Apple"}),
        ("iPhone 15 Pro 256GB Black", {"brand": "Apple"}),
        ("Galaxy S24 Ultra", {"gtin": "8806095299983"}),
        ("Samsung Galaxy S24 Ultra 5G 256GB", {"ean": "08806095299983"}),
        ("Pixel 8", {"brand": "Google", "gtin": "0840244705158"}),
        ("Pixel 8", {"brand": "Google", "gtin": "0840244705165"}),
    )
    names = groups(result)
    assert ["Apple iPhone 15 Pro 128GB Black", "iPhone 15 Pro (128 GB) - Black"] in names
    assert ["Galaxy S24 Ultra", "Samsung Galaxy S24 Ultra 5G 256GB"] in names  # the same GTIN
    assert ["iPhone 14 Pro 128GB Black"] in names and ["iPhone 15 Pro 256GB Black"] in names
    assert names.count(["Pixel 8"]) == 2  # different GTINs: different items
    books = resolve("product", ("The Book", {"isbn": "0-8044-2957-X"}), ("The Book", {"isbn": "978-0-8044-2957-3"}))
    assert len(books.entities) == 1  # an ISBN-10 is its ISBN-13
    phone = resolve("product", "iPhone 14", "iPhone 15")
    assert len(phone.entities) == 2 and phone.review == []


def test_places() -> None:
    result = resolve(
        "location",
        "Springfield, IL",
        ("Springfield, Illinois", {"country": "US"}),
        "Springfield, MA",
        ("Paris", {"country": "FR", "coordinates": "48.8566, 2.3522"}),
        ("Paris", {"country": "US", "region": "TX", "coordinates": (33.66, -95.55)}),
        "Paris, France",
    )
    names = groups(result)
    assert ["Springfield, IL", "Springfield, Illinois"] in names  # IL: Illinois (or Israel), and Illinois
    assert ["Springfield, MA"] in names
    assert names.count(["Paris"]) == 2
    paris = [m for m in result.review if {m.a.name, m.b.name} == {"Paris", "Paris, France"}]
    assert len(paris) == 1 and "+0.5 same country: FR" in paris[0].reasons
    near = resolve(
        "location", ("Big Ben", {"coordinates": (51.5007, -0.1246)}), ("Big Ben", {"lat": 51.5008, "lon": -0.1245})
    )
    assert len(near.entities) == 1


# ------------------------------------------------------------------------------------------ API


def test_compare_explains() -> None:
    resolver = EntityResolver("company")
    match = resolver.compare("Apple Inc.", "Apple GmbH")
    assert match.reasons == ["+6.0 same name apart from the legal form", "-1.5 different legal forms: inc / gmbh"]
    assert (match.score, match.decision) == (0.924, "review")
    data = match.to_dict()
    assert data["a"] == {"name": "Apple Inc."} and data["decision"] == "review"
    assert "Apple Inc." in repr(match)


def test_records_provenance_and_lookup() -> None:
    resolver = EntityResolver("company")
    added = resolver.add_records(
        [
            {
                "company": {"name": "Acme Corp"},
                "url": "https://a.example/1",
                "site": "acme.com",
                "phone": "+1 415 555 2671",
            },
            {"company": {"name": "ACME Corporation"}, "url": "https://b.example/9", "site": "https://www.acme.com"},
            {"company": {"name": "N/A"}, "url": "https://b.example/10"},
            {"url": "https://b.example/11"},
        ],
        "company.name",
        source_field="url",
        attributes={"website": "site", "phone": "phone"},
    )
    assert added[2] is None and added[3] is None  # placeholders and missing names are skipped
    result = resolver.resolve()
    [entity] = result.entities
    assert result.entity_of(added[0]) is entity and result.entity_of(added[1].id) is entity
    assert result.find("acme corp ") == [entity] and result.find("Acme") == []
    assert entity.sources == ["https://a.example/1", "https://b.example/9"]
    assert entity.attributes == {"website": ["acme.com", "https://www.acme.com"], "phone": ["+1 415 555 2671"]}
    [record] = result.records()
    assert record["id"] == "company:acme" and record["name"] == "Acme Corp"
    assert record["website"] == "acme.com" and record["count"] == 2
    data = entity.to_dict()
    assert data["mentions"][0] == {
        "name": "Acme Corp",
        "source": "https://a.example/1",
        "attributes": {"website": "acme.com", "phone": "+1 415 555 2671"},
    }
    assert data["links"] and "same website: acme.com" in data["links"][0]["reasons"][1]
    assert "2 mentions" in entity.explain()
    assert result.summary() == "2 mentions -> 1 entities (1 with several mentions); 0 pairs to review"
    assert result.stats["comparisons"] == 1


def test_thresholds_and_kinds_are_checked() -> None:
    with pytest.raises(ValueError, match="unknown entity kind"):
        EntityResolver("planet")
    with pytest.raises(ValueError, match="thresholds"):
        EntityResolver("company", merge_threshold=0.5, review_threshold=0.9)
    lenient = resolve("company", "Apple", "Apple Computer", merge_threshold=0.8)
    assert len(lenient.entities) == 1  # merge at 0.8: name evidence alone is enough


def test_scale_and_common_words() -> None:
    resolver = EntityResolver("company", max_block=20)
    words = ["alpha", "beta", "gamma", "delta", "omega", "nova", "terra", "blue", "red", "green"]
    for i in range(10_000):
        first, second = words[i % 10], words[(i // 10) % 10]
        resolver.add(f"{first.title()} {second.title()} {['Inc.', 'Ltd', 'Group'][i % 3]}", source=f"row {i}")
    for _ in range(2_000):
        resolver.add("Apple Inc.")  # identical mentions are compared once
    start = time.perf_counter()
    result = resolver.resolve()
    assert time.perf_counter() - start < 10
    # every word is in more names than max_block; pairs of words still find the variants
    assert result.stats["skipped_blocks"] > 0
    assert any({m.a.name, m.b.name} == {"Alpha Beta Inc.", "Alpha Beta Group"} for m in result.review), (
        "names differing by a generic word are compared"
    )
    apple = result.find("Apple Inc.")
    assert len(apple) == 1 and len(apple[0].mentions) == 2_000


# ------------------------------------------------------------------------------------------ command line


def test_cli_entities(tmp_path, capsys) -> None:
    source = tmp_path / "companies.jsonl"
    rows = [
        {"name": "Apple Inc.", "website": "https://www.apple.com", "url": "https://dir-a.example/1"},
        {"name": "APPLE INC", "url": "https://dir-b.example/7"},
        {"name": "Apple Computer, Inc.", "website": "apple.com", "url": "https://dir-a.example/2"},
        {"name": "IBM", "url": "https://dir-c.example/5"},
        {"name": "International Business Machines", "url": "https://dir-a.example/4"},
    ]
    source.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    out, review, annotated = tmp_path / "entities.jsonl", tmp_path / "review.jsonl", tmp_path / "annotated.jsonl"
    code = main(["data", "entities", str(source), "--field", "name", "--attribute", "website", "-o", str(out),
                 "--review-output", str(review), "--annotate", str(annotated)])  # fmt: skip
    assert code == 0
    err = capsys.readouterr().err
    assert "5 mentions -> 3 entities" in err and "to review:" in err
    entities = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert entities[0]["id"] == "company:apple" and entities[0]["count"] == 3
    assert entities[0]["sources"][0] == "https://dir-a.example/1"  # the url field is the source
    [pair] = [json.loads(line) for line in review.read_text(encoding="utf-8").splitlines()]
    assert {pair["a"]["name"], pair["b"]["name"]} == {"IBM", "International Business Machines"}
    records = [json.loads(line) for line in annotated.read_text(encoding="utf-8").splitlines()]
    assert records[1]["name_entity"] == "company:apple" and records[1]["name_canonical"] == "Apple Inc."
    assert (
        main(["data", "entities", str(source), "--field", "name", "--merge", "0.4"]) == 2
    )  # below the review threshold
