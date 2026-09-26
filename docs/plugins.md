# Plugins

A plugin is a Python package that adds to wintergrab without changing it.
It can add outputs and inputs, schema field types, data pipeline stages,
extraction strategies, model providers and commands. Installing it is all
it takes:

```
$ pip install wintergrab-kafka
$ wintergrab plugins
kafka (wintergrab-kafka 0.3.0): output kafka://, input .avro, command kafka-tail
```

## Writing one

A plugin declares an entry point in the `wintergrab.plugins` group:

```toml
# pyproject.toml
[project]
name = "wintergrab-kafka"
dependencies = ["wintergrab"]

[project.entry-points."wintergrab.plugins"]
kafka = "wintergrab_kafka:plugin"
```

The entry point is a function, or an object with a `register` method. It is
given a registry and adds what it brings:

```python
# wintergrab_kafka/__init__.py
def plugin(registry):
    registry.exporter("kafka", "wintergrab_kafka.output:KafkaExporter")    # output = "kafka://..."
    registry.reader(".avro", "wintergrab_kafka.avro:read_avro")            # read_records("x.avro")
    registry.field_type("isbn", normalize_isbn, {"type": "string"})        # {"type": "isbn"} in schemas
    registry.stage(Geocode)                                                # {geocode: {...}} in pipelines
    registry.strategy(PriceWidget, before="pattern")                       # tried by every extractor
    registry.model_provider("mistral", MistralModel)                       # --model mistral:NAME
    registry.command("kafka-tail", add_arguments, run, help="follow a topic")   # wintergrab kafka-tail
```

A string `"module:name"` is imported only when first used, so optional
libraries cost nothing until then.

| Registry method | What it adds | The contract |
|---|---|---|
| `exporter(".ext" \| "scheme", cls)` | An [output](storage.md#your-own) | `Exporter` subclass: `(path, *, append)`, `write(item)`, `close()`; `supports_unique_key = True` to take `unique_key` |
| `reader(".ext" \| "scheme", fn)` | An input of `read_records` and the `data` commands | `fn(path_or_url) -> iterator of dicts` |
| `field_type(name, normalizer, json_schema)` | A schema field type | `normalizer(raw, field, context, notes) -> value \| None` |
| `stage(cls)` | A [pipeline](data.md#pipelines) stage | `Stage` subclass with a `kind`, `apply(record, ctx)`, and `from_config(options, loader)` for pipeline files |
| `strategy(cls, before=None)` | An [extraction](extraction.md) strategy | `Strategy` subclass with a `method` name and `candidates(page, field, schema) -> [Candidate]` |
| `model_provider(name, cls)` | A [model](models.md) provider | `ModelProvider` subclass with `complete(prompt, *, system, images, json_output) -> str` |
| `command(name, add_arguments, run, help=)` | A command | `add_arguments(argparse parser)`, then `run(args) -> exit status` |

A strategy's values get the confidence of its method's prior:
`Extractor(priors={"widget": 0.85})`, 0.5 when none is given. A plugin
cannot replace a built-in stage or command: names that are taken are
refused, or skipped with a warning.

## When plugins load, and when they fail

Plugins load once, when wintergrab first needs what they may add: on the
command line, or when a library call looks up an output, an input, a
pipeline stage, a field type, an extractor's strategies or a model. A
plugin that raises while loading is reported and skipped. It never stops
wintergrab, and `wintergrab plugins` shows the error and exits with 1.

`WINTERGRAB_PLUGINS=0` loads none. A plugin is code you installed. It runs
with wintergrab's permissions, like any library you import.

## Testing one

Install your package in development mode (`pip install -e .`) and check
that `wintergrab plugins` lists what it adds. wintergrab's own test of the
mechanism, `tests/test_plugins.py`, shows each extension point used end
to end.
