"""Generated scrapers: selectors learned from sample pages, and the steps that accept or reject them."""

from __future__ import annotations

import json
import re

import pytest

from wintergrab import Fetcher
from wintergrab.cli import main
from wintergrab.errors import ConfigurationError, SchemaError
from wintergrab.extraction import Extractor, ModelRequest, generate_schema
from wintergrab.extraction.fixtures import FixtureSuite
from wintergrab.goals import GoalPlan, generate_scraper, parse_goal


def _books(site, *numbers: int) -> list:
    with Fetcher() as fetcher:
        return [fetcher.get(f"{site.url}/books/catalogue/book-{i}/index.html") for i in numbers]


def test_selectors_learned_from_sample_pages(site) -> None:
    generated = generate_schema(_books(site, 1, 2, 3, 5), "product")
    fields = generated.fields
    assert fields["name"].selector == "h1" and fields["name"].found_by == "meta" and fields["name"].reproduced == 4
    assert fields["price"].selector == "p.price_color"
    assert fields["availability"].selector == "p.availability"  # not "p.instock": the class naming the field
    assert fields["rating"].selector == "p.star-rating::attr(class)"  # "star-rating Three" reads as 3
    assert fields["category"].selector == "ul.breadcrumb > li:nth-of-type(3) > a"
    assert fields["url"].status == "skipped" and fields["url"].note == "the page's own address"
    assert fields["currency"].status == "skipped" and fields["currency"].note == "read from price"
    assert fields["brand"].status == "not found"
    assert generated.schema["price"].selectors == ["p.price_color"] and not generated.base["price"].selectors
    assert generated.values[0]["category"] == "Poetry" and generated.methods[0]["price"] == "dom"
    assert "selectors learned for 5 of 14 fields, from 4 pages" in generated.describe()
    # a page it was not generated from, read without anything else: the selectors agree with the page's
    # own evidence, and the record is surer
    [other] = _books(site, 11)
    record = Extractor(generated.schema).extract(other)
    assert record.data["name"] == "Book number 11" and record.data["price"] == 26.5 and record.data["rating"] == 2
    assert record.fields["price"].method == "selector" and "dom" in record.fields["price"].agreed
    assert record.confidence > Extractor("product").extract(other).confidence


def test_a_model_finds_values_once_and_selectors_read_them_after(site) -> None:
    class Model:
        usage = {"input": 0, "output": 0, "requests": 0}

        def __call__(self, request: ModelRequest) -> dict:
            self.usage["requests"] += 1
            self.usage["input"] += 100
            upc = re.search(r"UPC\W+(upc\d+)", request.text)
            return {"sku": upc.group(1) if upc else None, "brand": "Invented Books Ltd"}

    model = Model()
    generated = generate_schema(_books(site, 1, 2, 4), "product", model=model)
    sku = generated.fields["sku"]
    assert sku.found_by == "model" and sku.selector == '//tr[th[normalize-space()="UPC"]]/td'
    assert generated.fields["brand"].status == "not found"  # an answer the pages do not hold is not learned from
    assert generated.expected(0)["sku"] == "upc0001" and generated.usage == {"input": 300, "output": 0, "requests": 3}
    assert "3 request(s), 300 token(s) while generating; the schema needs no model" in generated.describe()
    [other] = _books(site, 9)
    assert Extractor(generated.schema).extract(other).data["sku"] == "upc0009"  # no model any more


def test_a_selector_must_read_the_value_on_every_page() -> None:
    pages = [
        ("<h1>Alpha</h1><p class='price'>$10.00</p>", "https://shop.example/p/alpha"),
        ("<h1>Beta</h1><div class='amount'>$12.00</div>", "https://shop.example/p/beta"),
        ("<h1>Gamma</h1><span class='price-now'>$9.00</span>", "https://shop.example/p/gamma"),
    ]
    from wintergrab.extraction import PageContext

    generated = generate_schema([PageContext(html, url=url) for html, url in pages], "product")
    assert generated.fields["name"].selector == "h1"
    price = generated.fields["price"]
    assert price.status == "not learned" and price.pages == 3 and price.tried >= 3
    assert re.fullmatch(r"\d+ selector\(s\) tried; the closest read the value on 1 of 3 pages", price.note)
    assert not generated.schema["price"].selectors
    with pytest.raises(ConfigurationError, match="no sample pages"):
        generate_schema([], "product")


def test_a_scraper_generated_tested_and_accepted(site, tmp_path) -> None:
    seen = []
    result = generate_scraper("books with title, price, availability and rating", tmp_path / "books",
                              sites=[site.url + "/books/"], sample=15, on_stage=seen.append)  # fmt: skip
    assert result.accepted, result.describe()
    assert [s.name for s in seen] == ["plan", "generate", "lint", "test", "sample", "validate", "benchmark"]
    assert result.stage("test").summary.startswith("5/5 pages give the expected values")
    sample = result.stage("sample").details
    assert sample["unseen"] == 7 and sample["record_pages"] == 12  # the 12 books, 5 of them the samples
    validate = result.stage("validate")
    assert validate.details["agreed"] == validate.details["compared"] > 0
    benchmark = result.stage("benchmark").details
    assert benchmark["generated"]["confidence"] > benchmark["goal"]["confidence"]
    assert benchmark["generated"]["fields_per_page"] == benchmark["goal"]["fields_per_page"] == 6
    directory = tmp_path / "books"
    for name in ("plan.json", "schema.json", "sample.jsonl", "quality.json", "report.json"):
        assert (directory / name).exists(), name
    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    assert report["accepted"] is True and len(report["stages"]) == 7
    # the plan names its schema, beside it; its records are read with it
    plan = GoalPlan.load(directory / "plan.json")
    assert plan.schema == "schema.json" and plan.extraction_schema()["name"].selectors == ["h1"]
    records = plan.run(max_pages=4, log_level=None).records
    assert records and all(r["_confidence"] > 0.9 for r in records)
    # the samples are extraction tests of the scraper
    assert FixtureSuite(directory / "fixtures").run().ok
    # again in the same directory: its own files replaced, the tests too
    again = generate_scraper("books with title and price", directory, sites=[site.url + "/books/"], sample=15)
    assert again.accepted and len(FixtureSuite(directory / "fixtures").fixtures()) == 5


def test_a_model_while_generating_and_none_after(site, tmp_path) -> None:
    request = "books with title, price and sku"
    without = generate_scraper(request, tmp_path / "without", sites=[site.url + "/books/"], sample=15)
    assert without.reasons == ["validate: the goal asks for sku: found on no page"]

    def model(request: ModelRequest) -> dict:  # reads the spec table, as a model would
        upc = re.search(r"UPC\W+(upc\d+)", request.text)
        return {"sku": upc.group(1) if upc else None}

    result = generate_scraper(request, tmp_path / "with", sites=[site.url + "/books/"], sample=15, model=model)
    assert result.accepted, result.describe()
    assert result.generated.fields["sku"].selector == '//tr[th[normalize-space()="UPC"]]/td'
    benchmark = result.stage("benchmark").details  # the scraper finds a field the goal's extraction does not
    assert benchmark["generated"]["fields_per_page"] == benchmark["goal"]["fields_per_page"] + 1
    rows = [json.loads(line) for line in (tmp_path / "with" / "sample.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows and all(re.fullmatch(r"upc\d{4}", row["sku"]) for row in rows)  # read by the selector, no model


def test_a_scraper_that_does_not_pass_is_rejected(site, tmp_path) -> None:
    result = generate_scraper("books with title, price and brand", tmp_path / "brand",
                              sites=[site.url + "/books/"], sample=15)  # fmt: skip
    assert not result.accepted
    assert result.reasons == ["validate: the goal asks for brand: found on no page"]
    assert "rejected: validate: the goal asks for brand" in result.describe()
    assert json.loads((tmp_path / "brand" / "report.json").read_text(encoding="utf-8"))["accepted"] is False
    with pytest.raises(ConfigurationError, match="one site"):
        generate_scraper(parse_goal("books", sites=["a.example", "b.example"]), tmp_path / "two")
    with pytest.raises(ConfigurationError, match="which site"):
        generate_scraper("books with title", tmp_path / "none")
    mine = tmp_path / "mine"
    (mine / "fixtures").mkdir(parents=True)
    (mine / "fixtures" / "0001-a.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="holds files of its own"):  # never someone else's files
        generate_scraper("books with title", mine, sites=[site.url + "/books/"])
    assert (mine / "fixtures" / "0001-a.json").exists()


def test_generate_command(site, tmp_path, capsys) -> None:
    request = ["generate", "books with title and price", "--site", site.url + "/books/", "--sample", "15"]
    assert main([*request, "-o", str(tmp_path / "ok")]) == 0
    printed = capsys.readouterr().err
    assert "accepted:" in printed and f"wintergrab test {tmp_path / 'ok' / 'fixtures'}" in printed
    assert "validate   ok" in printed
    rejected = [request[0], "books with title, price and brand", *request[2:], "-o", str(tmp_path / "no"), "--json"]
    assert main(rejected) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["accepted"] is False and report["reasons"][0].startswith("validate:")


def test_plans_with_a_schema_of_their_own(tmp_path) -> None:
    goal = parse_goal("books with title and price", sites=["https://books.example"])
    plan = GoalPlan(goal=goal, sites=[], schema="book.schema.json")
    (tmp_path / "book.schema.json").write_text(
        json.dumps({"name": "book", "fields": {"name": {"type": "string", "selectors": ["h1.title"]}}}),
        encoding="utf-8",
    )
    plan.save(tmp_path / "plan.json")
    loaded = GoalPlan.load(tmp_path / "plan.json")
    assert loaded.schema == "book.schema.json" and loaded.extraction_schema()["name"].selectors == ["h1.title"]
    embedded = loaded.to_dict(embed_schema=True)["schema"]
    assert embedded["fields"]["name"]["selectors"] == ["h1.title"]  # a run's recipe needs no file
    assert GoalPlan.from_dict(loaded.to_dict(embed_schema=True)).extraction_schema()["name"].selectors == ["h1.title"]
    assert GoalPlan(goal=goal, sites=[]).extraction_schema().names == goal.schema().names
    (tmp_path / "book.schema.json").unlink()
    with pytest.raises(SchemaError, match=r"book\.schema\.json"):
        GoalPlan.load(tmp_path / "plan.json").extraction_schema()
