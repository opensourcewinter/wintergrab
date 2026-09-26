"""Record pipelines: every stage, configuration files, crawls and the ``wintergrab data`` commands."""

from __future__ import annotations

import asyncio
import json
import textwrap

import pytest

from wintergrab import Spider
from wintergrab.cli import main
from wintergrab.data import (
    OPERATIONS,
    Compute,
    ConvertCurrency,
    Deduplicate,
    Enrich,
    Exclude,
    Filter,
    Lookup,
    Normalize,
    Pipeline,
    QualityCheck,
    Rename,
    Rule,
    Schema,
    Select,
    Transform,
    Validate,
    register_operation,
)
from wintergrab.errors import ConfigurationError, ValidationError
from wintergrab.spider.middleware import DropItem

PRODUCT = {
    "name": "product",
    "key": ["url"],
    "fields": {
        "name": {"type": "string", "required": True},
        "price": {"type": "money", "required": True, "minimum": 0},
        "sale_price": "money",
        "currency": "currency",
        "url": {"type": "url", "required": True},
    },
}


@pytest.fixture
def schema() -> Schema:
    return Schema.from_dict(PRODUCT)


# --------------------------------------------------------------------------- #
# field stages
# --------------------------------------------------------------------------- #
def test_rename() -> None:
    stage = Rename({"cost": "price", "title": "name"})
    assert list(stage({"title": "A", "x": 1, "cost": 5})) == ["name", "x", "price"]  # positions kept
    assert stage({"x": 1}) == {"x": 1}
    several = Rename({"cost": "price", "amount": "price"})
    assert several({"cost": "", "amount": 7}) == {"price": 7}  # the first with a value wins
    keep = Rename({"cost": "price"})
    assert keep({"price": 9, "cost": None}) == {"price": 9}  # an empty value does not overwrite
    swap = Rename({"a": "b", "b": "a"})
    assert swap({"a": 1, "b": 2}) == {"b": 1, "a": 2}
    with pytest.raises(ConfigurationError):
        Rename({})


def test_select_and_exclude() -> None:
    record = {"name": "A", "price_eur": 5, "price_usd": 6, "html": "<p>", "_issues": []}
    assert Select(["name", "price_*", "missing"])(record) == {
        "name": "A",
        "price_eur": 5,
        "price_usd": 6,
        "missing": None,
        "_issues": [],
    }
    assert Select("name", fill_missing=False, keep_metadata=False)(record) == {"name": "A"}
    assert Exclude(["html", "_*"])(record) == {"name": "A", "price_eur": 5, "price_usd": 6}
    with pytest.raises(ConfigurationError):
        Select([])


def test_transform_operations() -> None:
    record = {
        "name": "  big   PHONE ",
        "price": "Now only $1,299.00!",
        "tags": [" a ", "", "b", "a"],
        "weight": "450 g",
        "stock": "Yes",
        "path": "/p/1",
        "url": "https://shop.example/c/",
        "code": "sku: ABC-12",
        "released": "5 March 2024",
    }
    pipeline = Pipeline(
        [
            Transform("name", ["clean", "title"]),
            Transform("price", ["money"]),
            Transform("tags", ["strip", "compact", "unique", {"join": "|"}]),
            Transform("weight", [{"unit": "kg"}]),
            Transform("stock", ["boolean"]),
            Transform("path", ["url"]),
            Transform("code", [{"regex": r"[A-Z]{3}-\d+"}, "lower", {"prefix": "#"}]),
            Transform("released", ["date"], target="released_on"),
        ]
    )
    assert pipeline(record) == {
        "name": "Big Phone",
        "price": 1299,
        "tags": "a|b",
        "weight": 0.45,
        "stock": True,
        "path": "https://shop.example/p/1",  # resolved against the record's own URL
        "url": "https://shop.example/c/",
        "code": "#abc-12",
        "released": "5 March 2024",
        "released_on": "2024-03-05",
    }


def test_transform_more_operations() -> None:
    def run(ops, value):
        return Transform("v", ops)({"v": value})["v"]

    assert run([{"map": {"yes": True, "no": False}}], " YES ") is True
    assert run([{"map": {"a": 1}}], "zzz") == "zzz"  # unmapped values stay
    assert run([{"nullif": ["N/A", "-"]}, {"default": 0}], "n/a") == 0
    assert run([{"replace": ["-", " "]}], "a-b") == "a b"
    assert run([{"sub": [r"\s+", "_"]}], "a  b") == "a_b"
    assert run([{"split": ","}, "first"], "x, y") == "x"
    assert run([{"split": ","}, "last"], "x, y") == "y"
    assert run(["length"], [1, 2, 3]) == 3
    assert run([{"truncate": 3}, {"suffix": "..."}], "abcdef") == "abc..."
    assert run(["number", {"multiply": 0.01}], "1250") == 12.5
    assert run(["number", {"round": 1}], "3.14159") == 3.1
    assert run(["percent"], "12.5%") == 0.125
    assert run(["integer"], "1,204 reviews") == 1204
    assert run(["json"], '{"a": 1}') == {"a": 1}
    assert run([{"expr": "value * 2 + coalesce(bonus, 0)"}], 5) == 10  # the record's other fields are there too
    assert run([{"currency": "USD"}], "5") == "USD"
    assert run(["datetime"], "2024-03-05T10:30:00Z") == "2024-03-05T10:30:00+00:00"
    assert run(["fix_encoding"], "CafÃ©") == "Café"
    assert run([str.upper], "abc") == "ABC"  # a function of the value
    assert run(["number"], [" 1 ", "x", "2"]) == [1, 2]  # lists: item by item; failures dropped


def test_transform_missing_values_and_patterns() -> None:
    assert Transform("currency", [{"default": "EUR"}])({}) == {"currency": "EUR"}
    assert Transform("x", ["lower"])({}) == {}  # nothing written for a field the record lacks
    assert Transform("*_price", ["number"])({"list_price": "5", "sale_price": "4", "name": "n"}) == {
        "list_price": 5,
        "sale_price": 4,
        "name": "n",
    }


def test_transform_failures() -> None:
    record = {"price": "call us"}
    null = Transform("price", ["number"])
    assert null(record) == {"price": None} and null.stats["errors"] == 1
    assert Transform("price", ["number"], on_error="keep")(record) == {"price": "call us"}
    drop = Transform("price", ["number"], on_error="drop")
    ctx_record = drop(record)
    assert ctx_record is None and drop.stats["dropped"] == 1
    with pytest.raises(ValidationError, match="number failed"):
        Transform("price", ["number"], on_error="raise")(record)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("x", ["nope"]), "unknown operation"),
        (("x", ["unit"]), "needs an argument"),
        (("x", [{"lower": 1}]), "takes no argument"),
        (("x", [{"unit": "parsecs"}]), "unknown unit"),
        (("x", [{"regex": "("}]), "invalid regular expression"),
        (("x", [{"regex": {"pattern": "a", "group": 2}}]), "no group 2"),
        (("x", [{"truncate": -1}]), "number of characters"),
        (("x", [{"number": ";"}]), "decimal separator"),
        (("x", [{"a": 1, "b": 2}]), "an operation is"),
        (("x", []), "no operations"),
    ],
)
def test_transform_rejects_bad_operations(args, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        Transform(*args)
    with pytest.raises(ConfigurationError, match="target"):
        Transform(["a", "b"], ["lower"], target="c")


def test_register_operation() -> None:
    register_operation("reverse", lambda arg: lambda value, record: value[::-1])
    try:
        assert Transform("s", ["reverse"])({"s": "abc"}) == {"s": "cba"}
    finally:
        OPERATIONS.pop("reverse")


def test_compute() -> None:
    stage = Compute({"discount": "round(1 - sale_price / price, 2)", "label": "discount > 0.1"})
    assert stage({"price": 10, "sale_price": 8}) == {"price": 10, "sale_price": 8, "discount": 0.2, "label": True}
    assert stage({"price": 10})["discount"] is None  # missing data propagates
    assert Compute("n", lambda r: len(r))({"a": 1}) == {"a": 1, "n": 1}
    failing = Compute("x", "'a' - 1")
    assert failing({"x": 5}) == {"x": None} and failing.stats["errors"] == 1
    assert Compute("x", "'a' - 1", on_error="keep")({"x": 5}) == {"x": 5}
    assert Compute("x", "'a' - 1", on_error="drop")({"x": 5}) is None
    with pytest.raises(ConfigurationError, match="invalid syntax"):
        Compute("x", "1 +")
    with pytest.raises(ConfigurationError):
        Compute("x")


def test_filter() -> None:
    cheap = Filter("price < 10")
    assert cheap({"price": 5}) == {"price": 5}
    assert cheap({"price": 50}) is None
    assert cheap({}) is None  # a missing price is not below 10
    assert Filter("price < 10", keep=False)({"price": 50}) == {"price": 50}
    assert Filter(lambda r: r.get("ok"))({"ok": 1}) == {"ok": 1}
    broken = Filter("price < 10")
    assert broken({"price": "5"}) is None and broken.stats["errors"] == 1
    assert Filter("price < 10", on_error="keep")({"price": "5"}) == {"price": "5"}
    with pytest.raises(Exception, match="cannot compare"):
        Filter("price < 10", on_error="raise")({"price": "5"})


# --------------------------------------------------------------------------- #
# schema stages
# --------------------------------------------------------------------------- #
def test_normalize_then_validate(schema: Schema) -> None:
    pipeline = Pipeline([Normalize(schema, notes=True), Validate()])  # Validate uses the record's schema
    ok = pipeline({"name": " A ", "price": "₹29,999", "url": "https://a.example/"})
    assert ok == {
        "name": "A",
        "price": 29999,
        "sale_price": None,
        "currency": "INR",
        "url": "https://a.example/",
        "_notes": {"price": ["separator-from-currency"]},
    }
    assert pipeline({"name": "B", "price": "N/A", "url": "https://a.example/b"}) is None  # unreadable: invalid
    validate = pipeline["validate"]
    assert isinstance(validate, Validate)
    assert validate.summary() == [("price", "invalid", "error", 1)]
    assert pipeline["normalize"].stats["unreadable"] == 1


def test_validate_policies(schema: Schema, tmp_path) -> None:
    bad = {"name": "A", "price": -1, "url": "https://a.example/"}
    flagged = Validate(schema, on_error="flag")(bad)
    assert flagged is not None and [i["code"] for i in flagged["_issues"]] == ["range"]
    kept = Validate(schema, on_error="keep")
    assert kept(bad) == bad and kept.stats["invalid"] == 1
    with pytest.raises(ValidationError) as info:
        Validate(schema, on_error="raise")(bad)
    assert info.value.issues[0].code == "range"
    rejects = tmp_path / "rejects.jsonl"
    dropping = Validate(schema, rejects=rejects)
    assert dropping(bad) is None
    dropping.close()
    line = json.loads(rejects.read_text(encoding="utf-8"))
    assert line["record"] == bad and line["issues"][0]["code"] == "range"


def test_validate_warnings_and_rules(schema: Schema) -> None:
    free = {"name": "A", "price": 0, "url": "https://a.example/"}
    loose = Schema.from_dict({"fields": {"price": "money"}})
    assert Validate(loose)(free) == free  # a warning alone keeps the record
    assert Validate(loose, on_warning="drop")(free) is None
    assert Validate(loose, on_warning="flag")(free)["_issues"][0]["code"] == "suspicious"
    rules = Validate(rules=[Rule("sale", "sale_price <= price"), {"code": "named", "check": "len(name) > 1"}])
    assert rules({"name": "AB", "price": 5, "sale_price": 4}) is not None
    assert rules({"name": "AB", "price": 5, "sale_price": 6}) is None
    assert rules({"name": "A"}) is None


def test_deduplicate_stage() -> None:
    stage = Deduplicate(key="url")
    records = [
        {"url": "https://a.example/1?utm_source=x"},
        {"url": "https://a.example/1"},
        {"url": "https://a.example/2"},
    ]
    assert len(Pipeline([stage]).run(records)) == 2
    assert stage.stats["duplicates/key"] == 1 and stage.details() == "key 1"
    marked = Pipeline([Deduplicate(key="url", mark=True)]).run(records)
    assert marked[1]["_duplicate_of"] == 0


def test_lookup(tmp_path) -> None:
    table = tmp_path / "brands.csv"
    table.write_text("sku,brand,category\nABC-1,Acme,phones\n42,Globex,tools\n", encoding="utf-8")
    lookup = Lookup("sku", table, fields=["brand"])
    assert lookup({"sku": " abc-1 "}) == {"sku": " abc-1 ", "brand": "Acme"}
    assert lookup({"sku": 42}) == {"sku": 42, "brand": "Globex"}  # "42" in the file matches the number 42
    assert lookup({"sku": "zzz"}) == {"sku": "zzz"}
    assert lookup.details() == "2 matched, 1 unmatched"
    assert Lookup("sku", table, required=True)({"sku": "zzz"}) is None
    prefixed = Lookup("sku", {"ABC-1": {"brand": "Acme"}}, prefix="ref_")
    assert prefixed({"sku": "ABC-1", "brand": "mine"}) == {"sku": "ABC-1", "brand": "mine", "ref_brand": "Acme"}
    assert Lookup("sku", {"ABC-1": {"brand": "Acme"}})({"sku": "ABC-1", "brand": "mine"})["brand"] == "mine"
    assert (
        Lookup("sku", {"ABC-1": {"brand": "Acme"}}, overwrite=True)({"sku": "ABC-1", "brand": "x"})["brand"] == "Acme"
    )
    rows = tmp_path / "rows.jsonl"
    rows.write_text('{"id": 1, "name": "one"}\n{"id": 2, "name": "two"}\n', encoding="utf-8")
    assert Lookup("ref", rows, key="id")({"ref": "2"}) == {"ref": "2", "name": "two"}
    assert Lookup("sku", {"A": "Acme"}, fields=["brand"])({"sku": "a"}) == {"sku": "a", "brand": "Acme"}
    with pytest.raises(ConfigurationError, match="plain values"):
        Lookup("sku", {"A": "Acme"})


def test_convert_currency() -> None:
    stage = ConvertCurrency(["price", "was"], to="EUR", rates={"USD": "0.92", "JPY": 0.0061})
    assert stage({"price": 10, "was": 12.5, "currency": "USD"}) == {"price": 9.2, "was": 11.5, "currency": "EUR"}
    assert stage({"price": 1000, "currency": "JPY"}) == {"price": 6.1, "currency": "EUR"}
    assert stage({"price": 5, "currency": "EUR"}) == {"price": 5, "currency": "EUR"}  # already in euros
    assert stage({"price": {"amount": 10, "currency": "USD"}}) == {"price": {"amount": 9.2, "currency": "EUR"}}
    unknown = {"price": 10, "currency": "GBP"}
    assert stage(unknown) == unknown and stage.stats["errors"] == 1
    assert ConvertCurrency("price", to="EUR", rates={}, on_error="null")(unknown) == {"price": None, "currency": "GBP"}
    assert ConvertCurrency("price", to="EUR", rates={}, on_error="drop")(unknown) is None
    with pytest.raises(ConfigurationError):
        ConvertCurrency("price", to="EUR", rates={"XXX": 1})
    with pytest.raises(ConfigurationError):
        ConvertCurrency("price", to="EUR", rates={"USD": -1})


def test_enrich_sync_and_async() -> None:
    stage = Enrich(lambda r: {"brand": r["name"].split()[0]})
    assert stage({"name": "Acme Phone"}) == {"name": "Acme Phone", "brand": "Acme"}
    fill_only = Enrich(lambda r: {"brand": "X"}, overwrite=False)
    assert fill_only({"brand": "mine"}) == {"brand": "mine"}
    failing = Enrich(lambda r: 1 / 0)
    assert failing({"a": 1}) == {"a": 1} and failing.stats["errors"] == 1
    assert Enrich(lambda r: 1 / 0, on_error="drop")({"a": 1}) is None

    async def lookup(record):
        await asyncio.sleep(0)
        return {"stock": 3}

    pipeline = Pipeline([Enrich(lookup), Filter("stock > 0")])
    assert pipeline.is_async
    with pytest.raises(ConfigurationError, match="arun"):
        pipeline.run([{"a": 1}])
    assert asyncio.run(pipeline.arun([{"a": 1}])) == [{"a": 1, "stock": 3}]


def test_quality_check_stage(schema: Schema) -> None:
    stage = QualityCheck(schema, dataset="shop", save_to=None)
    Pipeline([stage]).run([{"name": "A", "price": 1, "url": "https://a.example/"}] * 3)
    report = stage.report()
    assert report.name == "shop" and report.records == 3


def test_item_pipelines_and_functions_as_stages() -> None:
    class Tagger:
        def process_item(self, item, spider):
            if item.get("skip"):
                raise DropItem("skipped on purpose")
            return {**item, "tagged": True}

    pipeline = Pipeline([Tagger(), lambda r: None if r.get("drop") else r])
    assert pipeline.run([{"a": 1}, {"skip": True}, {"drop": True}]) == [{"a": 1, "tagged": True}]
    assert [s.name for s in pipeline] == ["Tagger", "<lambda>"]
    assert pipeline["Tagger"].stats["dropped"] == 1


# --------------------------------------------------------------------------- #
# the pipeline
# --------------------------------------------------------------------------- #
def test_pipeline_report_describe_and_names(schema: Schema) -> None:
    pipeline = Pipeline(
        [Normalize(schema), Validate(), Transform("name", ["upper"]), Transform("name", ["strip"]), Filter("price > 1")]
    )
    records = [
        {"name": "a", "price": "$5", "url": "https://a.example/1"},
        {"name": "b", "price": "N/A", "url": "https://a.example/2"},
        {"name": "c", "price": "$1", "url": "https://a.example/3"},
    ]
    assert [r["name"] for r in pipeline.run(records)] == ["A"]
    assert [s.name for s in pipeline] == ["normalize", "validate", "transform", "transform#2", "filter"]
    report = pipeline.report()
    assert report[1] == {
        "stage": "validate",
        "kind": "validate",
        "in": 3,
        "out": 2,
        "dropped": 1,
        "errors": 0,
        "invalid": 1,
        "valid": 2,
    }
    text = pipeline.describe()
    assert text.splitlines()[0] == "pipeline: 3 in -> 1 out"
    assert "price: invalid x1" in text
    with pytest.raises(ConfigurationError, match="two stages"):
        Pipeline([Filter("a", name="same"), Filter("b", name="same")])
    with pytest.raises(KeyError):
        pipeline["nope"]
    with pytest.raises(ConfigurationError):
        Pipeline([42])


def test_stream_and_records_of_other_types() -> None:
    from dataclasses import dataclass

    @dataclass
    class Item:
        name: str

    pipeline = Pipeline([Transform("name", ["upper"])])
    assert list(pipeline.stream([Item("a"), {"name": "b"}])) == [{"name": "A"}, {"name": "B"}]
    with pytest.raises(TypeError):
        pipeline.run(["not a record"])
    assert pipeline.process_item("passed through") == "passed through"


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
CONFIG = {
    "name": "shop",
    "stages": [
        {"rename": {"cost": "price", "title": "name"}},
        {"transform": {"field": "name", "ops": ["clean", {"truncate": 40}], "on_error": "keep"}},
        {"normalize": {"schema": "product.schema.json", "country": "IN", "notes": True}},
        {"validate": {"on_error": "flag", "rules": [{"code": "sale", "check": "sale_price <= price"}]}},
        {"filter": "price > 0"},
        {"filter": {"condition": "name == 'hidden'", "keep": False, "name": "not-hidden"}},
        {"compute": {"domain": "domain(url)"}},
        {"dedupe": {"key": ["url"], "near": True, "similarity": 0.9}},
        {"lookup": {"on": "name", "table": "brands.json", "fields": ["brand"]}},
        {"convert_currency": {"fields": ["price"], "to": "EUR", "rates": {"INR": "0.011"}}},
        {"quality": {"dataset": "shop", "save_to": None}},
        {"exclude": ["_notes"]},
        {"select": ["name", "price", "currency", "brand", "domain", "url", "_issues"]},
    ],
}


@pytest.fixture
def project(tmp_path):
    (tmp_path / "product.schema.json").write_text(json.dumps(PRODUCT), encoding="utf-8")
    (tmp_path / "brands.json").write_text(json.dumps({"Phone X": {"brand": "Acme"}}), encoding="utf-8")
    return tmp_path


def test_from_config_runs(project) -> None:
    pipeline = Pipeline.from_config(CONFIG, base_dir=project)
    out = pipeline.run(
        [
            {"title": " Phone X ", "cost": "₹29,999", "url": "https://shop.example/p/1"},
            {"title": "Phone X", "cost": "₹29,999", "url": "https://shop.example/p/1"},  # duplicate
            {"title": "hidden", "cost": "₹5", "url": "https://shop.example/p/2"},
            {"title": "Case", "cost": "₹0", "url": "https://shop.example/p/3"},  # filtered: price 0
        ]
    )
    assert out == [
        {
            "name": "Phone X",
            "price": 329.99,
            "currency": "EUR",
            "brand": "Acme",
            "domain": "shop.example",
            "url": "https://shop.example/p/1",
            "_issues": None,
        }
    ]
    assert pipeline.name == "shop"
    assert pipeline["normalize"].schema is not None


def test_config_round_trips(project) -> None:
    pipeline = Pipeline.from_config(CONFIG, base_dir=project)
    config = pipeline.to_config()
    assert config["$schema"] == "wintergrab/pipeline/v1"
    again = Pipeline.from_config(config, base_dir=project)
    assert again.to_config() == config
    assert [s.name for s in again] == [s.name for s in pipeline]
    saved = pipeline.save(project / "pipeline.yaml")
    assert Pipeline.load(saved).to_config() == config


@pytest.mark.parametrize("suffix", [".yaml", ".json", ".toml"])
def test_load_pipeline_files(project, suffix: str) -> None:
    text = {
        ".yaml": """
            stages:
              - rename: {cost: price}
              - normalize: product.schema.json
              - validate: {rejects: out/rejects.jsonl}
            """,
        ".json": json.dumps(
            {
                "stages": [
                    {"rename": {"cost": "price"}},
                    {"normalize": "product.schema.json"},
                    {"validate": {"rejects": "out/rejects.jsonl"}},
                ]
            }
        ),
        ".toml": """
            [[stages]]
            rename = { cost = "price" }
            [[stages]]
            normalize = "product.schema.json"
            [[stages]]
            validate = { rejects = "out/rejects.jsonl" }
            """,
    }[suffix]
    path = project / f"pipeline{suffix}"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    pipeline = Pipeline.load(path)
    out = pipeline.run(
        [{"name": "A", "cost": "$5", "url": "https://a.example/"}, {"name": "B", "url": "https://a.example/b"}]
    )
    assert [r["price"] for r in out] == [5]
    rejected = (project / "out" / "rejects.jsonl").read_text(encoding="utf-8").splitlines()  # relative to the file
    assert len(rejected) == 1 and json.loads(rejected[0])["record"]["name"] == "B"


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"stages": [{"nope": {}}]}, "unknown stage 'nope'"),
        ({"stages": [{"filter": "x >"}]}, "stages[0]"),
        ({"stages": [{"transform": {"field": "a", "ops": ["lower"], "bogus": 1}}]}, "unknown option(s) bogus"),
        ({"stages": [{"transform": {"ops": ["lower"]}}]}, "either 'field' or 'fields'"),
        ({"stages": [{"select": "a", "filter": "b"}]}, "one-entry mapping"),
        ({"stages": "rename"}, "a list of stages"),
        ({"stages": [], "extra": 1}, "unknown option(s) extra"),
        ({"$schema": "v0", "stages": []}, "unsupported format"),
        ({"stages": [{"normalize": {"schema": "missing.json"}}]}, "cannot read"),
        ({"stages": [{"enrich": {"function": "os:getcwd"}}]}, "allow_imports=True"),
        ({"stages": [{"lookup": {"on": "a"}}]}, "missing option(s) table"),
        ({"stages": [{"compute": {"a": 1}}]}, "expected {field: expression}"),
    ],
)
def test_config_errors_say_where(config, message: str, tmp_path) -> None:
    with pytest.raises(ConfigurationError) as info:
        Pipeline.from_config(config, base_dir=tmp_path)
    assert message in str(info.value)


def test_imports_need_permission(tmp_path, monkeypatch) -> None:
    (tmp_path / "shop_helpers.py").write_text(
        "def brand(record):\n    return {'brand': record['name'].split()[0]}\n\n"
        "def only_phones(record):\n    return record if 'Phone' in record['name'] else None\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    config = {"stages": [{"enrich": {"function": "shop_helpers:brand"}}, {"function": "shop_helpers:only_phones"}]}
    with pytest.raises(ConfigurationError, match="allow_imports"):
        Pipeline.from_config(config)
    pipeline = Pipeline.from_config(config, allow_imports=True)
    assert pipeline.run([{"name": "Acme Phone"}, {"name": "Acme Case"}]) == [{"name": "Acme Phone", "brand": "Acme"}]
    assert pipeline.to_config()["stages"] == config["stages"]
    with pytest.raises(ConfigurationError, match="cannot import"):
        Pipeline.from_config({"stages": [{"function": "shop_helpers:nope"}]}, allow_imports=True)


def test_python_functions_cannot_be_written_to_files() -> None:
    with pytest.raises(ConfigurationError, match="cannot be written"):
        Pipeline([Compute("x", lambda r: 1)]).to_config()


# --------------------------------------------------------------------------- #
# crawls and the command line
# --------------------------------------------------------------------------- #
def test_pipeline_in_a_crawl(site, schema: Schema) -> None:
    events = []

    async def stock(record):
        await asyncio.sleep(0)
        return {"in_stock": record["price"] < 8}

    class Shop(Spider):
        log_level = None
        obey_robots_txt = False
        autothrottle = False
        start_urls = [site.url + f"/product/{i}" for i in range(1, 8)]
        pipelines = [Pipeline([Normalize(schema), Validate(), Filter("price < 9"), Enrich(stock)], name="clean")]

        def parse(self, response):
            yield {
                "name": response.css("h1::text").get(),
                "price": response.css(".price::text").get(),
                "url": response.url,
            }

    spider = Shop()
    spider.events.subscribe(events.append, kinds=["item_dropped", "pipeline_report"])
    result = spider.run()
    prices = sorted(item["price"] for item in result.items)
    assert prices == [6.25, 7.5, 8.75]
    assert all(item["currency"] == "USD" and item["in_stock"] == (item["price"] < 8) for item in result.items)
    assert result.stats["items_dropped/Pipeline"] == 4
    reasons = {e["reason"] for e in events if e.kind == "item_dropped"}
    assert reasons == {"filter: not price < 9"}
    report = next(e for e in events if e.kind == "pipeline_report")
    assert report["pipeline"] == "clean" and report["stages"][2]["dropped"] == 4


def test_cli_data_commands(tmp_path, capsys) -> None:
    items = tmp_path / "items.jsonl"
    items.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"name": "A", "price": "$5", "url": "https://a.example/1"},
                {"name": "B", "price": "N/A", "url": "https://a.example/2"},
                {"name": "C", "price": "$7", "url": "https://a.example/3"},
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "product.schema.json").write_text(json.dumps(PRODUCT), encoding="utf-8")

    assert main(["data", "infer", str(items), "-o", str(tmp_path / "inferred.json"), "--explain"]) == 0
    inferred = json.loads((tmp_path / "inferred.json").read_text(encoding="utf-8"))
    assert inferred["fields"]["price"]["type"] == "money"
    capsys.readouterr()

    code = main(["data", "validate", str(tmp_path / "product.schema.json"), str(items), "-o", str(tmp_path / "ok.jsonl"),
                 "--rejects", str(tmp_path / "bad.jsonl")])  # fmt: skip
    assert code == 1  # an invalid record
    err = capsys.readouterr().err
    assert "3 record(s): 2 valid, 1 invalid" in err and "price: invalid" in err
    assert len((tmp_path / "ok.jsonl").read_text(encoding="utf-8").splitlines()) == 2

    pipeline = tmp_path / "pipeline.yaml"
    pipeline.write_text("stages:\n  - normalize: product.schema.json\n  - filter: 'price > 6'\n", encoding="utf-8")
    assert main(["data", "run", str(pipeline), str(items), "-o", str(tmp_path / "out.csv")]) == 0
    assert (tmp_path / "out.csv").read_text(encoding="utf-8").splitlines()[1].startswith("C,7,")
    assert "pipeline: 3 in -> 1 out" in capsys.readouterr().err

    assert (
        main(
            [
                "data",
                "quality",
                str(items),
                "--schema",
                str(tmp_path / "product.schema.json"),
                "--save",
                str(tmp_path / "q.json"),
            ]
        )
        == 0
    )
    assert "items: 3 record(s)" in capsys.readouterr().out
    assert main(["data", "quality", str(items), "--baseline", str(tmp_path / "q.json"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["comparison"] == []
    assert main(["data"]) == 2


def test_cli_crawl_with_a_pipeline(site, tmp_path) -> None:
    (tmp_path / "product.schema.json").write_text(json.dumps(PRODUCT), encoding="utf-8")
    pipeline = tmp_path / "pipeline.yaml"
    pipeline.write_text(
        "stages:\n  - rename: {title: name}\n  - normalize: product.schema.json\n  - filter: 'price < 8'\n",
        encoding="utf-8",
    )
    out = tmp_path / "items.jsonl"
    code = main(["-q", "crawl", site.url + "/product/1", "--field", "title=h1::text", "--field", "price=.price::text",
                 "--max-depth", "0", "--no-robots", "--no-autothrottle", "--pipeline", str(pipeline), "-o", str(out)])  # fmt: skip
    assert code == 0
    [item] = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert item["name"] == "Product 1" and item["price"] == 6.25 and item["currency"] == "USD"


def test_model_stages_round_trip_through_their_config() -> None:
    from wintergrab.data.pipeline import Analyze, Classify

    pipeline = Pipeline([Analyze("body"), Classify("body", model="ollama:llama3", categories=["a", "b"])])
    config = pipeline.to_config()
    assert Pipeline.from_config(config).to_config() == config  # (what a pipeline file holds, read back as it was)
    assert config["stages"][1]["classify"]["categories"] == ["a", "b"]


def test_a_schema_inferred_from_records() -> None:
    from wintergrab.data.schema import Schema

    schema = Schema.infer([{"price": 10.5, "name": "A"}, {"price": 12, "name": "B"}], name="products")
    assert schema.name == "products" and {f.name for f in schema.fields} >= {"price", "name"}


# --------------------------------------------------------------------------- #
# where a value came from
# --------------------------------------------------------------------------- #
def test_a_record_with_provenance_says_what_the_pipeline_did_to_it() -> None:
    evidence = {"cost": {"method": "selector", "source": "selector:.price", "confidence": 0.9}}
    record = {
        "cost": "$1,299",
        "title": "Laptop X ",
        "note": "old",
        "_provenance": {"url": "https://s.example/1", "fields": evidence},
    }
    pipeline = Pipeline(
        [
            Rename({"cost": "price", "title": "name"}, name="names"),
            Transform("price", ["strip", {"regex": "[0-9.,]+"}, "number"]),
            Transform("name", ["strip"]),
            Exclude(["note"]),
        ]
    )
    out = pipeline(record)
    assert (out["price"], out["name"]) == (1299, "Laptop X") and "note" not in out
    fields = out["_provenance"]["fields"]
    assert "cost" not in fields and fields["price"]["method"] == "selector"  # the evidence follows the new name
    assert fields["price"]["transforms"] == [
        {"stage": "names", "kind": "rename", "from": "cost"},
        {"stage": "transform", "kind": "transform", "before": "$1,299"},
    ]
    assert fields["name"]["transforms"] == [
        {"stage": "names", "kind": "rename", "from": "title"},
        {"stage": "transform#2", "kind": "transform", "before": "Laptop X "},  # (a second stage of a kind is numbered)
    ]
    assert fields["note"]["transforms"] == [{"stage": "exclude", "kind": "exclude", "dropped": True}]
    assert pipeline({"cost": "$5", "title": "Y", "note": "n"}) == {"price": 5, "name": "Y"}  # no provenance: as before

    from wintergrab.data import describe_provenance

    assert describe_provenance(out, ["price"]).splitlines() == [
        "from: https://s.example/1",
        "price: 1299",
        "  read by selector from selector:.price, confidence 0.90",
        "  rename (names): was named cost",
        "  transform: was '$1,299'",
    ]
    assert describe_provenance({"price": 5}).startswith("no provenance")


def test_data_trace_says_where_a_value_came_from(site, tmp_path, capsys) -> None:
    schema = tmp_path / "product.json"
    fields = {"name": {"type": "string", "required": True}, "price": "money", "currency": "currency"}
    schema.write_text(json.dumps({"name": "product", "fields": fields}), encoding="utf-8")
    out, workspace = tmp_path / "items.jsonl", tmp_path / "ws"
    assert main(["-q", "crawl", site.url + "/product/1", "--extract", str(schema), "--provenance", "--max-pages",
                 "1", "--workspace", str(workspace), "-o", str(out), "--no-progress"]) == 0  # fmt: skip
    row = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert row["_provenance"]["run"] == "run-1" and row["_provenance"]["output"] == str(out)  # (the crawl's stamp)
    capsys.readouterr()
    assert main(["data", "trace", str(out), "price", "--where", f"url={site.url}/product/1"]) == 0
    told = capsys.readouterr().out.splitlines()
    fetched = row["_provenance"]["fetched_at"]
    assert told[0] == f"from: {site.url}/product/1, fetched {fetched}, extractor product@1, run run-1, output {out}"
    assert told[1] == "price: 6.25" and told[2].startswith("  read by ") and "confidence 0." in told[2]
    assert main(["data", "trace", str(out), "--where", "url=nope"]) == 1
    assert "no record matches" in capsys.readouterr().err
    plain = tmp_path / "plain.jsonl"
    plain.write_text('{"url": "u", "price": 5}\n', encoding="utf-8")
    assert main(["data", "trace", str(plain)]) == 1  # (nothing to trace: collected without provenance)
    assert capsys.readouterr().out.startswith("no provenance")
    assert main(["data", "trace", str(out), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["_provenance"]["run"] == "run-1"
