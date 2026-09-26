"""Extraction tests: fixtures, suites, and the fixture/test commands."""

from __future__ import annotations

import json

import pytest

from wintergrab.data import Schema
from wintergrab.errors import ConfigurationError, SchemaError
from wintergrab.extraction.fixtures import FixtureSuite, matches

SCHEMA = {"name": "product", "fields": {"name": {"type": "string", "selectors": ["h1"]}, "price": "money",
          "tags": {"type": "string", "many": True, "selectors": [".tag"]}, "url": "url"}}  # fmt: skip
PAGE = ("<html><head><title>Phone X | Shop</title></head><body><h1>Phone  X</h1><p class='price'>$799.00</p>"
        "<a class='tag'>new</a><a class='tag'>5g</a></body></html>")  # fmt: skip


def test_values_compare_as_their_type_reads_them() -> None:
    schema = Schema.from_dict(SCHEMA)
    price = {"amount": 799, "currency": "USD"}
    assert matches(schema, "price", "$799.00", price) and matches(schema, "price", "799", price)
    assert matches(schema, "price", {"amount": 799.0, "currency": "USD"}, price)
    assert not matches(schema, "price", "€799.00", price)  # another currency
    assert not matches(schema, "price", "$800", price)
    assert matches(schema, "name", "Phone X", "Phone  X") and not matches(schema, "name", "phone x", "Phone X")
    assert matches(schema, "tags", ["new", "5g"], ["new", "5g"]) and not matches(
        schema, "tags", ["5g", "new"], ["new", "5g"]
    )
    assert matches(schema, "name", None, None) and not matches(schema, "name", None, "x")
    assert not matches(schema, "name", "Phone X", None)


def test_a_suite(tmp_path) -> None:
    schema_file = tmp_path / "product.schema.json"
    schema_file.write_text(json.dumps(SCHEMA), encoding="utf-8")
    suite = FixtureSuite(tmp_path / "fixtures")
    first = suite.add(PAGE, url="https://shop.example/p/phone-x/index.html", schema=str(schema_file))
    assert first.name == "0001-phone-x" and first.path.with_suffix(".html").read_text() == PAGE
    assert first.expected == {"name": "Phone X", "price": {"amount": 799, "currency": "USD"}, "tags": ["new", "5g"],
                              "url": "https://shop.example/p/phone-x/index.html"}  # fmt: skip
    assert suite.schema_path == schema_file.resolve()  # remembered: suite.json
    second = suite.add(PAGE, url="https://shop.example/p/2", expect={"price": "$799", "rating": None}, only=True)
    assert second.name == "0002-2" and second.expected == {"price": "$799", "rating": None}
    report = suite.run()  # the suite's schema
    assert (
        report.ok
        and report.passed == 2
        and "2 fixture(s): 2 passed, 0 failed (6 value(s) checked)" in report.describe()
    )

    # a change: the name is read from the <title> now
    changed = {**SCHEMA, "fields": {**SCHEMA["fields"], "name": {"type": "string", "selectors": ["title"]}}}
    schema_file.write_text(json.dumps(changed), encoding="utf-8")
    report = suite.run()
    assert not report.ok and report.failed == 1
    assert '0001-phone-x  FAILED  name: expected "Phone X", got "Phone X | Shop"' in report.describe()
    assert report.to_dict()["fixtures"][0]["failed"] == [
        {"field": "name", "expected": "Phone X", "got": "Phone X | Shop"}
    ]
    assert suite.run(names=["0002"]).ok  # only some
    assert suite.update(report) == 1 and suite.run().ok  # accepted (the .json changed: review it)
    assert json.loads(first.path.read_text())["expected"]["name"] == "Phone X | Shop"

    # a broken schema raises (a page the extractor chokes on is an ERROR line in the report)
    schema_file.write_text(json.dumps({"name": "p", "fields": {"x": {"type": "no-such-type"}}}), encoding="utf-8")
    with pytest.raises(SchemaError, match="unknown type"):  # a broken schema is the caller's to report
        suite.run()
    with pytest.raises(ConfigurationError, match="nothing to expect"):
        FixtureSuite(tmp_path / "empty").add("<p></p>", url="https://x.example/")


def test_a_healing_extractors_fixtures_are_read(tmp_path) -> None:
    from wintergrab.extraction.healing import ExtractorVersions

    versions = ExtractorVersions(tmp_path / "ext", SCHEMA)
    versions.add_fixture("https://shop.example/p/1", PAGE, {"price": "799.00"})
    fixtures = FixtureSuite(versions.directory / "fixtures").fixtures()
    assert [(f.name, f.expected) for f in fixtures] == [("0001", {"price": "799.00"})]
    assert FixtureSuite(versions.directory / "fixtures").run(versions.schema()).ok


def test_fixture_and_test_commands(site, tmp_path, capsys) -> None:
    from wintergrab.cli import main

    schema = tmp_path / "book.schema.json"
    schema.write_text(json.dumps({"name": "book", "fields": {"name": {"type": "string", "selectors": ["h1"]},
                                  "price": "money", "url": "url"}}), encoding="utf-8")  # fmt: skip
    suite = str(tmp_path / "books")
    books = [site.url + f"/books/catalogue/book-{i}/index.html" for i in (1, 2)]
    assert main(["fixture", *books, "--schema", str(schema), "--to", suite]) == 0
    out = capsys.readouterr()
    assert '0001-book-1.json: name="Book number 1", price=11.5 GBP' in out.out and "check these values" in out.err
    assert main(["test", suite]) == 0 and "2 fixture(s): 2 passed" in capsys.readouterr().out
    assert main(["fixture", site.url + "/books/catalogue/book-3/index.html", "--to", suite,
                 "--expect", "price=99", "--only"]) == 0  # fmt: skip
    capsys.readouterr()
    assert main(["test", suite, "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["failed"] == 1 and report["fixtures"][2]["failed"][0]["got"] == {"amount": 14.5, "currency": "GBP"}
    assert main(["test", suite, "--only", "0001"]) == 0
    capsys.readouterr()

    # pages a recorded crawl kept
    workspace = str(tmp_path / "ws")
    assert main(["crawl", site.url + "/books/", "--allow", "/books/", "--record", "--workspace", workspace,
                 "--no-progress", "-o", str(tmp_path / "all.jsonl")]) == 0  # fmt: skip
    capsys.readouterr()
    more = str(tmp_path / "more")
    assert main(["fixture", "--from-run", "last", "--workspace", workspace, "--match", "book-1[0-2]/",
                 "--schema", str(schema), "--to", more]) == 0  # fmt: skip
    assert {f.name[5:] for f in FixtureSuite(more).fixtures()} == {"book-10", "book-11", "book-12"}  # in crawl order
    capsys.readouterr()
    assert main(["test", more]) == 0

    assert main(["test", str(tmp_path / "nothing")]) == 1 and "no fixture in" in capsys.readouterr().err
    assert main(["fixture", "--to", suite]) == 2


def test_testing_a_healing_extractor(tmp_path, capsys) -> None:
    from wintergrab.cli import main
    from wintergrab.extraction.healing import ExtractorVersions

    versions = ExtractorVersions(tmp_path / "ext", SCHEMA)
    versions.add_fixture("https://shop.example/p/1", PAGE, {"price": "799.00", "name": "Phone X"})
    assert main(["test", "--heal", str(tmp_path / "ext")]) == 0
    assert "1 fixture(s): 1 passed" in capsys.readouterr().out
