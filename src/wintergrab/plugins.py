"""Plugins: installed packages that add to wintergrab without changing it.

A plugin declares an entry point in the ``wintergrab.plugins`` group::

    # the plugin's pyproject.toml
    [project.entry-points."wintergrab.plugins"]
    kafka = "wintergrab_kafka:plugin"

and ``plugin`` (a function, or an object with a ``register`` method) is given a :class:`Registry`
that says what it adds::

    def plugin(registry):
        registry.exporter("kafka", "wintergrab_kafka.output:KafkaExporter")   # output = "kafka://..."
        registry.reader(".avro", "wintergrab_kafka.avro:read_avro")           # read_records("x.avro")
        registry.field_type("isbn", normalize_isbn, {"type": "string"})       # a schema field type
        registry.stage(Geocode)                                               # a pipeline stage
        registry.strategy(PriceWidget)                                        # an extraction strategy
        registry.model_provider("mistral", MistralModel)                      # --model mistral:NAME
        registry.command("kafka-tail", add_arguments, run, help="...")        # wintergrab kafka-tail

Plugins are loaded once, when wintergrab first needs what they may add: the command line, an
output or input, a pipeline, a schema type, an extractor, a model. A plugin that fails to load
is reported (``wintergrab plugins``) and skipped: it never stops wintergrab. ``WINTERGRAB_PLUGINS=0``
loads none. A plugin is code you installed, and runs as wintergrab does.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = ["COMMANDS", "PluginInfo", "Registry", "load_plugins", "plugins"]

log = logging.getLogger("wintergrab.plugins")

GROUP = "wintergrab.plugins"
#: Commands plugins added: name -> (help, add_arguments(parser), run(args) -> exit status).
COMMANDS: dict[str, tuple[str, Callable[[Any], None], Callable[[Any], int]]] = {}
_PLUGINS: list[PluginInfo] = []
_lock = threading.RLock()
_loaded = False


@dataclass
class PluginInfo:
    """One plugin: where it comes from, what it added, and what went wrong."""

    name: str
    target: str  # "module:object"
    package: str | None = None
    version: str | None = None
    added: list[str] = field(default_factory=list)
    error: str | None = None

    def describe(self) -> str:
        source = f"{self.package} {self.version}" if self.package else self.target
        if self.error:
            return f"{self.name} ({source}): could not load: {self.error}"
        return f"{self.name} ({source}): {', '.join(self.added) or 'nothing'}"


class Registry:
    """What a plugin is given to add what it brings (see the module docs)."""

    def __init__(self, info: PluginInfo) -> None:
        self.info = info

    def exporter(self, key: str, exporter: Any) -> None:
        """An output: ``".ext"`` or a URL ``"scheme"`` (see :func:`~wintergrab.spider.exporters.register_exporter`)."""
        from .spider.exporters import register_exporter

        register_exporter(key, exporter)
        self.info.added.append(f"output {key if key.startswith('.') else key + '://'}")

    def reader(self, key: str, reader: Any) -> None:
        """An input for :func:`~wintergrab.data.io.read_records`: ``".ext"`` or a URL ``"scheme"``."""
        from .data.io import register_reader

        register_reader(key, reader)
        self.info.added.append(f"input {key if key.startswith('.') else key + '://'}")

    def field_type(self, name: str, normalizer: Any, json_schema: Any = None) -> None:
        """A schema field type (see :func:`~wintergrab.data.schema.register_type`)."""
        from .data.schema import register_type

        register_type(name, normalizer, json_schema)
        self.info.added.append(f"field type {name}")

    def stage(self, stage: Any) -> None:
        """A data pipeline stage class, used by its ``kind`` in pipeline files."""
        from .data.pipeline import register_stage

        register_stage(stage)
        self.info.added.append(f"stage {stage.kind}")

    def strategy(self, strategy: Any, *, before: str | None = None) -> None:
        """An extraction strategy class, tried by extractors (after the built-in ones, or ``before`` one)."""
        from .extraction.strategies import register_strategy

        register_strategy(strategy, before=before)
        self.info.added.append(f"strategy {strategy.method}")

    def model_provider(self, name: str, provider: Any) -> None:
        """A model provider for ``--model NAME:MODEL`` (see :mod:`wintergrab.models`)."""
        from .models import register_provider

        register_provider(name, provider)
        self.info.added.append(f"model provider {name}")

    def command(
        self, name: str, add_arguments: Callable[[Any], None], run: Callable[[Any], int], *, help: str = ""
    ) -> None:
        """A command: ``wintergrab NAME ...`` (``add_arguments(parser)``, then ``run(args)`` gives the exit status)."""
        with _lock:
            COMMANDS[name] = (help or f"(from the plugin {self.info.name})", add_arguments, run)
        self.info.added.append(f"command {name}")


def load_plugins(*, force: bool = False) -> list[PluginInfo]:
    """Load the installed plugins, once (``force``: again). Returns what each added."""
    global _loaded
    with _lock:
        if _loaded and not force:
            return list(_PLUGINS)
        _loaded = True
        if force:
            _PLUGINS.clear()
        if os.environ.get("WINTERGRAB_PLUGINS", "1").strip().lower() in ("0", "false", "no", "off"):
            return []
        from importlib.metadata import entry_points

        for point in entry_points(group=GROUP):
            dist = getattr(point, "dist", None)
            info = PluginInfo(point.name, point.value, getattr(dist, "name", None), getattr(dist, "version", None))
            try:
                target = point.load()
                register = getattr(target, "register", target)
                register(Registry(info))
            except Exception as exc:  # a broken plugin never breaks wintergrab
                info.error = f"{type(exc).__name__}: {exc}"
                log.warning("the plugin %s could not load: %s", point.name, info.error)
            _PLUGINS.append(info)
        return list(_PLUGINS)


def plugins() -> list[PluginInfo]:
    """The plugins loaded so far."""
    return list(_PLUGINS)
