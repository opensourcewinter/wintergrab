"""Reading record files: JSON Lines, JSON and CSV (writing uses the crawl exporters)."""

from __future__ import annotations

import csv
import itertools
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

from ..errors import ConfigurationError

__all__ = ["RECORD_SUFFIXES", "read_records"]

RECORD_SUFFIXES = (".jsonl", ".ndjson", ".json", ".csv")
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
    ``.csv``: one record per row, values as strings. ``"-"``: JSON Lines on
    standard input.
    """
    records = _read(str(path))
    yield from records if limit is None else itertools.islice(records, limit)


def _read(source: str) -> Iterator[dict[str, Any]]:
    if source == "-":
        yield from _json_lines(iter(sys.stdin), "stdin")
        return
    target = Path(source)
    suffix = target.suffix.lower()
    if suffix not in RECORD_SUFFIXES:
        raise ConfigurationError(f"unsupported input {target.name!r}; use {', '.join(RECORD_SUFFIXES)}")
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
