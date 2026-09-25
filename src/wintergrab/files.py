"""Reading structured files (JSON, YAML, TOML) with useful errors.

Schemas, pipelines and project configuration all load through
:func:`read_structured`, so they accept the same formats and report problems
the same way.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import ConfigurationError, WintergrabError

__all__ = ["STRUCTURED_SUFFIXES", "read_structured", "yaml_module"]

STRUCTURED_SUFFIXES = (".json", ".yaml", ".yml", ".toml")


def yaml_module(error: type[WintergrabError] = ConfigurationError) -> Any:
    """The ``yaml`` module (PyYAML), or ``error`` explaining how to install it."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:
        raise error('YAML files need PyYAML: pip install "wintergrab[yaml]"') from exc
    return yaml


def _toml_loads(text: str, error: type[WintergrabError]) -> Any:
    try:
        import tomllib
    except ImportError:  # Python 3.10
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError as exc:
            raise error("TOML files on Python 3.10 need tomli: pip install tomli") from exc
    return tomllib.loads(text)


def read_structured(path: str | Path, *, error: type[WintergrabError] = ConfigurationError) -> Any:
    """Parse ``path`` by its suffix: ``.json``, ``.yaml``/``.yml`` or ``.toml`` (anything else: JSON).

    Raises:
        error: The file cannot be read or parsed (the message names the file).
    """
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise error(f"cannot read {source}: {exc.strerror or exc}") from exc
    suffix = source.suffix.lower()
    try:
        if suffix in (".yaml", ".yml"):
            return yaml_module(error).safe_load(text)
        if suffix == ".toml":
            return _toml_loads(text, error)
        return json.loads(text)
    except WintergrabError:
        raise
    except Exception as exc:  # each parser has its own error types
        raise error(f"cannot parse {source}: {exc}") from exc
