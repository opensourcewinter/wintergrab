"""Typed schemas: loading, normalizing, validating, exporting and inferring."""

from __future__ import annotations

import json

import pytest

from wintergrab.data import (
    FIELD_TYPES,
    Rule,
    Schema,
    SchemaField,
    explain_inference,
    infer_schema,
    is_valid,
    load_schema,
    register_type,
    validate_record,
)
from wintergrab.errors import ExpressionError, SchemaError

PRODUCT = {
    "name": "product",
    "key": "url",
    "extra": "warn",
    "fields": {
        "name": {"type": "string", "required": True, "aliases": ["title"], "max_length": 50},
        "price": {"type": "money", "required": True, "minimum": 0},
        "currency": "currency",
        "tags": "string[]",
        "rating": {"type": "rating", "best": 5},
        "sku": {"type": "string", "pattern": r"^[A-Z]{3}-\d+$"},
        "size": {"type": "enum", "enum": ["S", "M", "L"]},
        "released": "date",
        "weight": {"type": "quantity", "unit": "kg"},
        "stock": {"type": "integer", "default": 0},
        "seller": {"type": "object", "fields": {"name": "string", "country": "country"}},
        "url": {"type": "url", "required": True},
    },
}
RAW = {
    "title": " Phone ",
    "price": "₹29,999",
    "tags": ["a", " b ", ""],
    "rating": "9/10",
    "sku": "ABC-1",
    "size": "m",
    "released": "5 March 2024",
    "weight": "450 g",
    "seller": {"name": " Acme ", "country": "Deutschland"},
    "url": "/p/1?utm_source=newsletter",
    "color": "red",
}


@pytest.fixture
def schema() -> Schema:
    return Schema.from_dict(PRODUCT)


def test_normalize_types_every_field(schema: Schema) -> None:
    record, results = schema.normalize(RAW, base_url="https://shop.example/")
    assert record == {
        "name": "Phone",  # found under its alias
        "price": 29999,
        "currency": "INR",  # split out of the price
        "tags": ["a", "b"],
        "rating": 4.5,  # 9/10 on the 5-point scale
        "sku": "ABC-1",
        "size": "M",  # the enum's own spelling
        "released": "2024-03-05",
        "weight": 0.45,  # 450 g in kg
        "stock": 0,  # the default
        "seller": {"name": "Acme", "country": "DE"},
        "url": "https://shop.example/p/1",
        "color": "red",  # extra fields are kept ("warn")
    }
    assert results["price"].notes == ["separator-from-currency"]
    assert results["price"].raw == "₹29,999"
    assert all(result.ok for result in results.values())
    issues = schema.validate(record, results)
    assert [(i.field, i.code, i.severity) for i in issues] == [("color", "unknown-field", "info")]
    assert is_valid(issues)


def test_unreadable_values_are_reported(schema: Schema) -> None:
    record, results = schema.normalize({"name": "x", "price": "N/A", "url": "https://a.example/"})
    assert record["price"] is None
    assert not results["price"].ok
    issues = schema.validate(record, results)
    assert [(i.field, i.code) for i in issues] == [("price", "invalid")]
    assert issues[0].value == "N/A"
    # without the results, only the missing value is visible
    assert [(i.field, i.code) for i in schema.validate(record)] == [("price", "missing")]


def test_validation_checks(schema: Schema) -> None:
    raw = {
        "name": "x" * 60,
        "price": "-5",
        "sku": "abc-1",
        "size": "XL",
        "released": "2099-01-01",
        "url": "https://a.example/x",
    }
    record, results = schema.normalize(raw)
    codes = {(i.field, i.code, i.severity) for i in schema.validate(record, results)}
    assert codes == {
        ("name", "length", "error"),
        ("price", "range", "error"),
        ("sku", "pattern", "error"),
        ("size", "enum", "error"),
        ("released", "suspicious", "warning"),
    }


def test_suspicious_prices_and_damaged_text() -> None:
    schema = Schema.from_dict({"fields": {"price": "money", "title": "string"}})
    record, _ = schema.normalize({"price": "0", "title": "CafÃ©"})
    issues = {(i.field, i.code, i.severity) for i in schema.validate(record)}
    assert issues == {("price", "suspicious", "warning"), ("title", "encoding", "warning")}


def test_money_without_a_currency_field_stays_an_object() -> None:
    schema = Schema.from_dict({"fields": {"price": "money", "old_price": "money[]"}})
    record, _ = schema.normalize({"price": "€5", "old_price": ["€7", "€9"]})
    assert record == {
        "price": {"amount": 5, "currency": "EUR"},
        "old_price": [{"amount": 7, "currency": "EUR"}, {"amount": 9, "currency": "EUR"}],
    }


def test_currency_conflicts_are_noted() -> None:
    schema = Schema.from_dict({"fields": {"price": "money", "sale": "money", "currency": "currency"}})
    record, results = schema.normalize({"price": "€5", "sale": "$4"})
    assert record == {"price": 5, "sale": 4, "currency": "EUR"}
    assert "currency-conflict" in results["sale"].notes


def test_quantities_in_a_unit_that_does_not_fit() -> None:
    schema = Schema.from_dict({"fields": {"weight": {"type": "quantity", "unit": "kg"}, "size": "quantity"}})
    record, results = schema.normalize({"weight": "12 cm", "size": "12 cm"})
    assert record["weight"] is None and "unit-mismatch" in results["weight"].notes
    assert record["size"] == {"value": 12, "unit": "cm"}


def test_extra_fields_can_be_dropped() -> None:
    schema = Schema.from_dict({"extra": "drop", "fields": {"a": "integer"}})
    assert schema.normalize({"a": "1", "b": 2})[0] == {"a": 1}


def test_list_values_for_single_fields_take_the_first() -> None:
    schema = Schema.from_dict({"fields": {"a": "integer"}})
    record, results = schema.normalize({"a": ["", "7", "8"]})
    assert record == {"a": 7}
    assert results["a"].notes == ["first-of-many"]


def test_schema_file_round_trips(schema: Schema, tmp_path) -> None:
    again = Schema.from_dict(schema.to_dict())
    assert again.to_dict() == schema.to_dict()
    assert again.normalize(RAW, base_url="https://shop.example/") == schema.normalize(
        RAW, base_url="https://shop.example/"
    )
    for suffix in (".json", ".yaml"):
        path = schema.save(tmp_path / f"product{suffix}")
        assert load_schema(path).to_dict() == schema.to_dict()
    toml = tmp_path / "s.toml"
    toml.write_text('name = "t"\nkey = ["id"]\n[fields]\nid = "integer"\nprice = { type = "money", required = true }\n')
    loaded = Schema.load(toml)
    assert loaded.names == ["id", "price"] and loaded["price"].required and loaded.key == ["id"]


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"fields": {"a": "nope"}}, "unknown type 'nope'"),
        ({"fields": {"a": {"type": "enum"}}}, "needs an 'enum' list"),
        ({"fields": {"a": {"type": "string", "bogus": 1}}}, "unknown option"),
        ({"fields": {"a": "string"}, "key": ["b"]}, "key field"),
        ({"x": 1, "fields": {}}, "unknown schema option"),
        ({"$schema": "other", "fields": {}}, "unsupported schema format"),
        ({"fields": {"1a": "string"}}, "invalid field name"),
        ({"fields": {"a": {"type": "string", "pattern": "("}}}, "invalid pattern"),
        ({"fields": {"a": {"type": "quantity", "unit": "parsecs"}}}, "unknown unit"),
        ({"fields": {"a": {"type": "money", "currency": "XYZ"}}}, "unknown currency"),
        ({"fields": {"a": {"type": "object"}}}, "needs 'fields'"),
        ({"fields": {"a": {"type": "number", "minimum": "0"}}}, "must be a number"),
        ({"fields": [{"type": "string"}]}, "needs a 'name'"),
        ({"name": "no fields"}, "needs 'fields'"),
        ({"fields": {"a": "string"}, "container": ["article"]}, "container must be a selector"),
    ],
)
def test_invalid_schemas(spec, message: str) -> None:
    with pytest.raises(SchemaError, match=message):
        Schema.from_dict(spec)


def test_where_a_listing_page_holds_its_records() -> None:
    from wintergrab.extraction import Extractor

    spec = {"name": "item", "container": "li.item", "next_page": "a.next", "fields": {"name": {"type": "string",
            "selectors": ["b"]}, "price": "money"}}  # fmt: skip
    schema = Schema.from_dict(spec)
    assert schema.container == "li.item" and schema.next_page == "a.next"
    assert Schema.from_dict(schema.to_dict()).to_dict() == schema.to_dict()
    assert "container" not in Schema.from_dict({"fields": {"a": "string"}, "container": ""}).to_dict()
    page = "<ul><li class=item><b>A</b> $1</li><li class=item><b>B</b> $2</li></ul><ul><li>not one</li></ul>"
    records = Extractor(schema).extract_all(page)  # the schema's container, unless another is given
    assert [(r.data["name"], r.data["price"]) for r in records] == [
        ("A", {"amount": 1, "currency": "USD"}),
        ("B", {"amount": 2, "currency": "USD"}),
    ]


def test_schema_files_that_cannot_be_read(tmp_path) -> None:
    with pytest.raises(SchemaError, match="cannot read"):
        load_schema(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{nope")
    with pytest.raises(SchemaError, match="cannot parse"):
        load_schema(broken)


def test_fields_as_a_list_and_type_aliases() -> None:
    schema = Schema(name="s", fields=[SchemaField("n", "int"), SchemaField("p", "price"), SchemaField("u", "link")])
    assert [f.type for f in schema] == ["integer", "money", "url"]
    assert "n" in schema and len(schema) == 3 and schema.required == []
    listed = Schema.from_dict({"fields": [{"name": "a", "type": "integer", "required": True}]})
    assert listed.required == ["a"]


def test_key_of() -> None:
    schema = Schema.from_dict({"key": ["sku", "region"], "fields": {"sku": "string", "region": "string"}})
    assert schema.key_of({"sku": "A", "region": "EU"}) == ("A", "EU")
    assert schema.key_of({"sku": "A"}) is None


def test_register_type() -> None:
    def isbn(raw, field, context, notes):
        digits = "".join(ch for ch in str(raw) if ch.isdigit() or ch in "Xx")
        return digits.upper() if len(digits) in (10, 13) else None

    register_type("isbn", isbn, {"type": "string", "pattern": "^[0-9X]{10,13}$"})
    try:
        schema = Schema.from_dict({"fields": {"isbn": "isbn"}})
        assert schema.normalize({"isbn": "978-3-16-148410-0"})[0] == {"isbn": "9783161484100"}
        assert schema.to_json_schema()["properties"]["isbn"]["pattern"] == "^[0-9X]{10,13}$"
        with pytest.raises(SchemaError):
            register_type("bad name!", isbn)
    finally:
        FIELD_TYPES.pop("isbn", None)


def test_json_schema_export_validates_normalized_records(schema: Schema) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    exported = schema.to_json_schema()
    assert exported["required"] == ["name", "price", "url"]
    assert exported["properties"]["tags"] == {"type": ["array", "null"], "items": {"type": "string"}}
    assert exported["properties"]["size"]["enum"] == ["S", "M", "L", None]
    record, _ = schema.normalize(RAW, base_url="https://shop.example/")
    jsonschema.validate(record, exported)
    sparse, _ = schema.normalize({"name": "n", "price": "$1", "url": "https://a.example/"})
    jsonschema.validate(sparse, exported)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**record, "price": "29,999"}, exported)


# --------------------------------------------------------------------------- #
# rules
# --------------------------------------------------------------------------- #
def test_expression_rules() -> None:
    rule = Rule("sale-below-list", "sale_price <= price", field="sale_price")
    assert rule.apply({"price": 10, "sale_price": 8}) is None
    issue = rule.apply({"price": 10, "sale_price": 12})
    assert issue is not None and (issue.code, issue.field, issue.value) == ("sale-below-list", "sale_price", 12)
    assert issue.message == "expected sale_price <= price"
    # a field the rule reads is missing: the rule does not apply (required fields catch absence)
    assert rule.apply({"price": 10}) is None
    strict = Rule("has-price", "price is not None or on_request", skip_missing=False)
    assert strict.apply({"on_request": True}) is None
    assert strict.apply({}) is not None


def test_function_rules_and_messages() -> None:
    rule = Rule("even", lambda r: r["n"] % 2 == 0 or f"{r['n']} is odd", severity="warning")
    issue = rule.apply({"n": 3})
    assert issue is not None and issue.message == "3 is odd" and issue.severity == "warning"
    broken = Rule("broken", lambda r: r["missing"])
    issue = broken.apply({})
    assert issue is not None and issue.severity == "warning" and "KeyError" in issue.message


def test_rule_file_form() -> None:
    rule = Rule.from_dict({"code": "positive", "check": "price > 0", "severity": "warning", "field": "price"})
    assert Rule.from_dict(rule.to_dict()) == rule
    with pytest.raises(SchemaError, match="unknown option"):
        Rule.from_dict({"code": "x", "check": "1", "oops": 1})
    with pytest.raises(SchemaError, match="needs a 'code'"):
        Rule.from_dict({"check": "1"})
    with pytest.raises(SchemaError, match="severity"):
        Rule("x", "1", severity="fatal")
    with pytest.raises(ExpressionError):
        Rule("x", "price >")
    with pytest.raises(SchemaError, match="cannot be written"):
        Rule("x", lambda r: True).to_dict()


def test_validate_record_with_rules_only() -> None:
    rules = [Rule("positive", "price > 0", field="price")]
    assert validate_record({"price": 5}, None, rules=rules) == []
    assert [i.code for i in validate_record({"price": -5}, None, rules=rules)] == ["positive"]


def test_nested_validation_paths() -> None:
    schema = Schema.from_dict(
        {
            "fields": {
                "offers": {"type": "object", "many": True, "fields": {"price": {"type": "number", "required": True}}}
            }
        }
    )
    record, _ = schema.normalize({"offers": [{"price": "5"}, {"price": ""}]})
    assert [(i.field, i.code) for i in schema.validate(record)] == [("offers[1].price", "missing")]


# --------------------------------------------------------------------------- #
# inference
# --------------------------------------------------------------------------- #
def _sample(n: int = 30) -> list[dict]:
    return [
        {
            "url": f"https://s.example/p/{i}",
            "price": f"${i}.99",
            "Rating": f"{i % 5}.5 out of 5",
            "in stock": "yes" if i % 2 else "no",
            "date": f"2024-01-{i % 28 + 1:02d}",
            "brand": ["Acme", "Globex", "Initech"][i % 3],
            "seller": {"name": "A", "since": "2020-01-01"},
            "notes": "N/A" if i % 10 == 0 else f"note {i}",
            "_meta": 1,
        }
        for i in range(n)
    ]


def test_infer_schema() -> None:
    schema = infer_schema(_sample(), name="shop")
    types = {f.name: f.type for f in schema}
    assert types == {
        "url": "url",
        "price": "money",
        "Rating": "rating",
        "in_stock": "boolean",
        "date": "date",
        "brand": "enum",
        "seller": "object",
        "notes": "string",
    }
    assert schema.key == ["url"]
    assert schema["price"].currency == "USD"
    assert schema["in_stock"].aliases == ["in stock"]
    assert schema["brand"].enum == ["Acme", "Globex", "Initech"]
    assert schema["seller"].schema is not None and schema["seller"].schema["since"].type == "date"
    assert [f.name for f in schema if not f.required] == ["notes"]  # its "N/A" values count as missing
    # and the inferred schema reads the records it came from
    record, results = schema.normalize(_sample(1)[0])
    assert record["in_stock"] is False
    assert record["price"] == {"amount": 0.99, "currency": "USD"}  # no currency field: kept together
    assert all(r.ok for r in results.values())


def test_infer_schema_details() -> None:
    records = [{"n": str(i), "maybe": "x" if i % 2 else None} for i in range(10)]
    schema = infer_schema(records)
    assert schema["n"].type == "integer" and schema["n"].required
    assert not schema["maybe"].required
    guesses = explain_inference(records)
    assert [(g.field, g.type, g.share, g.present) for g in guesses] == [
        ("n", "integer", 1.0, 10),
        ("maybe", "string", 1.0, 10),
    ]
    one_value = infer_schema([{"k": "same"} for _ in range(20)])
    assert one_value["k"].type == "string"  # a constant is not an enum
    assert infer_schema([]).fields == []
    assert json.loads(json.dumps(infer_schema(_sample()).to_dict()))["fields"]["price"]["type"] == "money"
    # a path or a file name holds a number and a letter, but is no quantity: "/img/3m-tape.png" is no 3 m
    paths = infer_schema([{"img": v, "size": s} for v, s in (("/i/0a.jpg", "12.5cm"), ("/img/3m-tape.png", "1.2 kg"))])
    assert (paths["img"].type, paths["size"].type) == ("string", "quantity")
