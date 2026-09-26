"""Reading records: JSON Lines, JSON, CSV, and through :mod:`wintergrab.storage` Parquet, Excel and
PostgreSQL (writing uses the crawl exporters)."""

from __future__ import annotations

import csv
import importlib
import itertools
import json
import re
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO, Any

from ..errors import ConfigurationError

__all__ = ["READERS", "RECORD_SUFFIXES", "URL_READERS", "read_records", "register_reader"]

Reader = Callable[[str], Iterator[dict[str, Any]]]
#: Readers by file extension, beyond the built-in ones: a function ``(path) -> records``, or
#: ``"module:function"`` imported when first used. More with :func:`register_reader`.
READERS: dict[str, Reader | str] = {
    ".parquet": "wintergrab.storage.parquet:read_parquet",
    ".pq": "wintergrab.storage.parquet:read_parquet",
    ".xlsx": "wintergrab.storage.xlsx:read_xlsx",
}
#: Readers by URL scheme (``postgresql://...``).
URL_READERS: dict[str, Reader | str] = {
    "postgresql": "wintergrab.storage.postgres:read_postgres",
    "postgres": "wintergrab.storage.postgres:read_postgres",
    "mongodb": "wintergrab.storage.mongodb:read_mongodb",
    "mongodb+srv": "wintergrab.storage.mongodb:read_mongodb",
    "mysql": "wintergrab.storage.mysql:read_mysql",
    "mariadb": "wintergrab.storage.mysql:read_mysql",
}
RECORD_SUFFIXES = (".jsonl", ".ndjson", ".json", ".csv", *READERS)
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://")


def register_reader(key: str, reader: Reader | str) -> None:
    """Read more: ``".ext"`` for files with that extension, ``"scheme"`` for ``scheme://`` URLs."""
    name = key.lower()
    if name.startswith("."):
        READERS[name] = reader
    elif _SCHEME.match(name + "://"):
        URL_READERS[name] = reader
    else:
        raise ValueError(f"a reader is registered by extension ('.ext') or URL scheme ('scheme'), not {key!r}")


def _reader(entry: Reader | str) -> Reader:
    if isinstance(entry, str):
        module, _, name = entry.partition(":")
        return getattr(importlib.import_module(module), name)  # type: ignore[no-any-return]
    return entry


_WRAPPERS = ("items", "records", "data", "results", "rows")


def _json_lines(lines: Iterator[str], source: str) -> Iterator[dict[str, Any]]:
    for number, line in enumerate(lines, 1):
        text = line.strip()
        if not text:
            continue
        try:
            record = json.loads(text)
        except ValueError as exc:
            raise ConfigurationError(f"{source}, line {number}: not JSON ({exc})") from None
        if not isinstance(record, dict):
            raise ConfigurationError(f"{source}, line {number}: expected a JSON object, got {type(record).__name__}")
        yield record


def read_records(path: str | Path, *, limit: int | None = None) -> Iterator[dict[str, Any]]:
    """The records in a file, one at a time.

    ``.jsonl``/``.ndjson``: one JSON object per line. ``.json``: a list of
    objects (or an object holding one under ``items``, ``records``, ``data``...).
    ``.csv``: one record per row, values as strings. ``.parquet``, ``.xlsx``,
    ``postgresql://.../db?table=NAME``, ``mysql://.../db?table=NAME`` and
    ``mongodb://.../db?collection=NAME``: see :mod:`wintergrab.storage`.
    ``"-"``: JSON Lines on standard input.
    """
    records = _read(str(path))
    yield from records if limit is None else itertools.islice(records, limit)


def _read(source: str) -> Iterator[dict[str, Any]]:
    if source == "-":
        yield from _json_lines(iter(sys.stdin), "stdin")
        return
    scheme = _SCHEME.match(source)
    key = scheme.group(1).lower() if scheme else Path(source).suffix.lower()
    if key not in (URL_READERS if scheme else (*READERS, ".jsonl", ".ndjson", ".json", ".csv")):
        from ..plugins import load_plugins

        load_plugins()  # a plugin may add it
    if scheme:
        entry = URL_READERS.get(scheme.group(1).lower())
        if entry is None:
            raise ConfigurationError(f"no reader for {scheme.group(1)}:// URLs (known: {', '.join(URL_READERS)})")
        yield from _reader(entry)(source)
        return
    target = Path(source)
    suffix = target.suffix.lower()
    if suffix not in (*RECORD_SUFFIXES, *READERS):
        raise ConfigurationError(f"unsupported input {target.name!r}; use {', '.join(RECORD_SUFFIXES)}")
    if suffix in READERS:
        if not target.is_file():
            raise ConfigurationError(f"cannot read {target}: no such file")
        yield from _reader(READERS[suffix])(str(target))
        return
    try:
        handle: IO[str] = target.open(encoding="utf-8-sig", newline="" if suffix == ".csv" else None)
    except OSError as exc:
        raise ConfigurationError(f"cannot read {target}: {exc.strerror or exc}") from None
    with handle:
        if suffix in (".jsonl", ".ndjson"):
            yield from _json_lines(iter(handle), target.name)
        elif suffix == ".csv":
            try:
                for row in csv.DictReader(handle):
                    yield {k: v for k, v in row.items() if k is not None}
            except csv.Error as exc:
                raise ConfigurationError(f"{target.name}: {exc}") from None
        else:
            try:
                data = json.load(handle)
            except ValueError as exc:
                raise ConfigurationError(f"{target.name}: not JSON ({exc})") from None
            if isinstance(data, dict):
                wrapped = next((data[k] for k in _WRAPPERS if isinstance(data.get(k), list)), None)
                data = wrapped if wrapped is not None else [data]
            if not isinstance(data, list):
                raise ConfigurationError(f"{target.name}: expected a list of objects")
            for index, record in enumerate(data):
                if not isinstance(record, dict):
                    raise ConfigurationError(f"{target.name}[{index}]: expected an object, got {type(record).__name__}")
                yield record
