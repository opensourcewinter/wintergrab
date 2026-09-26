"""What the storage adapters share: optional imports, the spool, and typing columns from values."""

from __future__ import annotations

import importlib
import json
import os
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from ..errors import ConfigurationError, ExportError

__all__ = ["Spool", "as_text", "column_kinds", "kind_of", "require", "widen"]


def require(module: str, extra: str, what: str) -> Any:
    """Import ``module``, or say which extra installs it."""
    try:
        return importlib.import_module(module)
    except ImportError:
        raise ConfigurationError(f'{what} needs {module.split(".")[0]}: pip install "wintergrab[{extra}]"') from None


# -- column kinds ------------------------------------------------------------------------------- #
# "bool", "int", "float", "str", "json" (nested: objects and lists), "null" (nothing seen yet)
_INT64 = (-(2**63), 2**63 - 1)


def kind_of(value: Any) -> str:
    """The kind of a JSON value, as a column sees it."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int" if _INT64[0] <= value <= _INT64[1] else "str"  # beyond 64 bits: as text
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return "json"


def widen(a: str, b: str) -> str:
    """A kind holding the values of both: int and float make float; other mixes make text."""
    if a == b or b == "null":
        return a
    if a == "null":
        return b
    if {a, b} == {"int", "float"}:
        return "float"
    return "str"


def column_kinds(records: Iterable[dict[str, Any]]) -> dict[str, str]:
    """Each field's kind over ``records``, the fields in the order they first came."""
    kinds: dict[str, str] = {}
    for record in records:
        for key, value in record.items():
            kinds[key] = widen(kinds.get(key, "null"), kind_of(value))
    return kinds


def as_text(value: Any) -> str | None:
    """A value in a text column: strings as they are, anything else as JSON."""
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


# -- the spool ---------------------------------------------------------------------------------- #
class Spool:
    """Items kept as JSON Lines beside an output that is written whole at the end (Parquet, XLSX).

    While a crawl runs, its items are in ``.NAME.spool.jsonl`` next to the output: a crash loses
    nothing, and a resumed crawl (``append``) continues it. When the spool is gone but the output
    is there, ``seed`` gives the output's records to continue from.
    """

    def __init__(
        self, target: Path, *, append: bool, seed: Callable[[], Iterable[dict[str, Any]]] | None = None
    ) -> None:
        self.target = target
        self.path = target.with_name(f".{target.name}.spool.jsonl")
        resume_output = append and not self.path.exists() and target.exists() and seed is not None
        self._fh = open(self.path, "a" if append else "w", encoding="utf-8")  # noqa: SIM115 - closed in close()
        if resume_output:
            assert seed is not None
            for record in seed():
                self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def write(self, line: str) -> None:
        self._fh.write(line)

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def records(self) -> Iterator[dict[str, Any]]:
        """The items so far (the spool's complete lines)."""
        if not self._fh.closed:
            self.flush()
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                if line.endswith("\n"):  # a line a crash cut short is not an item
                    yield json.loads(line)

    def finish(self, write: Callable[[Path], None]) -> None:
        """``write(temporary path)`` the whole output, put it in place, and drop the spool."""
        self.close()
        temporary = self.target.with_name(f".{self.target.name}.{os.getpid()}.tmp")
        try:
            write(temporary)
            os.replace(temporary, self.target)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            raise ExportError(f"could not write {self.target} (the items are kept in {self.path}): {exc}") from exc
        self.path.unlink(missing_ok=True)
