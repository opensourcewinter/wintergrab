"""Plugins: an installed package's entry point adds outputs, inputs, types, stages, strategies,
model providers and commands."""

from __future__ import annotations

import importlib
import sys
import textwrap
from typing import Any

import pytest

from wintergrab import plugins as plugin_module
from wintergrab.data import io as data_io
from wintergrab.data import pipeline as data_pipeline
from wintergrab.data import schema as data_schema
from wintergrab.extraction import strategies as extraction_strategies
from wintergrab.spider import exporters

PLUGIN = """
from pathlib import Path

from wintergrab.data.pipeline import Stage
from wintergrab.extraction.strategies import Candidate, Strategy
from wintergrab.models import ModelProvider
from wintergrab.spider.exporters import Exporter


class Lines(Exporter):
    def __init__(self, path, *, append):
        super().__init__(path, append=append)
        self.file = open(path, "a" if append else "w", encoding="utf-8")

    def write(self, item):
        self.file.write(f"{item['name']}|{item['price']}\\n")
        self.count += 1

    def close(self):
        self.file.close()


def read_lines(path):
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        name, price = line.split("|")
        yield {"name": name, "price": float(price)}


def isbn(raw, field, context, notes):
    digits = "".join(c for c in str(raw) if c.isdigit())
    return digits if len(digits) in (10, 13) else None


class Upper(Stage):
    kind = "upper"

    def apply(self, record, ctx):
        return {k: v.upper() if isinstance(v, str) else v for k, v in record.items()}

    @classmethod
    def from_config(cls, options, loader):
        return cls()


class Widget(Strategy):
    method = "widget"

    def candidates(self, page, field, schema):
        value = (page.root.css(f"[data-widget='{field.name}']::text").get() or "").strip()
        return [Candidate(value, self.method, f"widget:{field.name}")] if value else []


class Echo(ModelProvider):
    provider = "echo"

    def key_required(self, base_url):
        return False

    def complete(self, prompt, *, system=None, images=(), json_output=False):
        return '{"echo": true}'


def hello_arguments(parser):
    parser.add_argument("--name", default="world")


def hello(args):
    print(f"hello, {args.name}")
    return 0


def plugin(registry):
    registry.exporter(".lines", Lines)
    registry.reader(".lines", read_lines)
    registry.field_type("isbn", isbn, {"type": "string"})
    registry.stage(Upper)
    registry.strategy(Widget, before="pattern")
    registry.model_provider("echo", Echo)
    registry.command("hello", hello_arguments, hello, help="say hello")
"""


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A package with two entry points in the plugins group: one that works, one that does not."""
    (tmp_path / "wg_test_plugin.py").write_text(textwrap.dedent(PLUGIN), encoding="utf-8")
    info = tmp_path / "wg_test_plugin-1.2.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: wg-test-plugin\nVersion: 1.2\n", encoding="utf-8")
    (info / "entry_points.txt").write_text(
        "[wintergrab.plugins]\ngood = wg_test_plugin:plugin\nbroken = wg_test_plugin:no_such_thing\n", encoding="utf-8"
    )
    saved: list[tuple[Any, Any]] = [
        (exporters.EXPORTERS, dict(exporters.EXPORTERS)),
        (data_io.READERS, dict(data_io.READERS)),
        (data_schema.FIELD_TYPES, dict(data_schema.FIELD_TYPES)),
        (data_pipeline.STAGES, dict(data_pipeline.STAGES)),
        (plugin_module.COMMANDS, dict(plugin_module.COMMANDS)),
    ]
    from wintergrab import models

    saved.append((models.PROVIDERS, dict(models.PROVIDERS)))
    strategies = list(extraction_strategies.STRATEGIES)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    yield plugin_module.load_plugins(force=True)
    for registry, before in saved:
        registry.clear()
        registry.update(before)
    extraction_strategies.STRATEGIES[:] = strategies
    sys.modules.pop("wg_test_plugin", None)
    monkeypatch.undo()
    plugin_module.load_plugins(force=True)  # (none, again)


def test_a_plugin_adds_what_it_says(installed, tmp_path, capsys) -> None:
    found = {info.name: info for info in installed}
    good, broken = found["good"], found["broken"]
    assert (good.package, good.version, good.error) == ("wg-test-plugin", "1.2", None)
    assert good.added == ["output .lines", "input .lines", "field type isbn", "stage upper", "strategy widget",
                          "model provider echo", "command hello"]  # fmt: skip
    assert broken.error is not None and broken.error.startswith("AttributeError")  # reported, and skipped

    path = tmp_path / "books.lines"
    exporters.write_items(path, [{"name": "Dune", "price": 9.5}])
    assert list(data_io.read_records(path)) == [{"name": "Dune", "price": 9.5}]

    schema = data_schema.Schema.from_dict({"name": "book", "fields": {"isbn": "isbn", "title": "string"}})
    assert schema.fields[0].type == "isbn"
    pipeline = data_pipeline.Pipeline.from_config({"stages": [{"upper": {}}]})
    assert pipeline.process({"title": "dune"}) == {"title": "DUNE"}

    from wintergrab.extraction import Extractor
    from wintergrab.parser import Selector

    record = Extractor(schema).extract(Selector("<html><body><span data-widget='isbn'>978-0-441-17271-9</span>"
                                                "<h1>Dune</h1></body></html>", url="https://b.example/dune"))  # fmt: skip
    assert record.fields["isbn"].value == "9780441172719" and record.fields["isbn"].method == "widget"

    from wintergrab.models import load_model

    assert load_model("echo:any").complete("hi") == '{"echo": true}'

    from wintergrab.cli import main

    assert main(["hello", "--name", "plugins"]) == 0 and capsys.readouterr().out == "hello, plugins\n"
    assert main(["plugins"]) == 1  # one did not load
    out = capsys.readouterr().out
    assert (
        "good (wg-test-plugin 1.2): output .lines, input .lines" in out
        and "broken (wg-test-plugin 1.2): could not" in out
    )


def test_plugins_can_be_turned_off(installed, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WINTERGRAB_PLUGINS", "0")
    assert plugin_module.load_plugins(force=True) == []
    from wintergrab.cli import main

    assert main(["plugins"]) == 0 and "turned off" in capsys.readouterr().out


def test_registries_refuse_what_is_not_theirs() -> None:
    from wintergrab.errors import ConfigurationError

    with pytest.raises(ConfigurationError, match="Stage subclass"):
        data_pipeline.register_stage(dict)  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match="already a stage 'filter'"):

        class Other(data_pipeline.Stage):
            kind = "filter"

        data_pipeline.register_stage(Other)
    with pytest.raises(TypeError, match="Strategy subclass"):
        extraction_strategies.register_strategy(dict)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="no strategy 'nope'"):
        extraction_strategies.register_strategy(type("S", (extraction_strategies.Strategy,), {"method": "s"}),
                                                before="nope")  # fmt: skip
