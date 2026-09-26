"""Parquet: items as a typed, compressed table (``output = "items.parquet"``; needs pyarrow).

Each field is a column, typed from all its values: ``int64``, ``double`` (integers and decimals
mixed), ``bool`` or ``string``. Nested values (objects, lists) are JSON text, and the file says
which columns hold them (its ``wintergrab`` metadata), so :func:`read_parquet` gives them back as
they were. A field whose values mix kinds (numbers and text) is text.

While the crawl runs the items are spooled as JSON Lines beside the file (see
:class:`~wintergrab.storage.common.Spool`); the Parquet file is written when the crawl ends, in
row groups of ``ROW_GROUP`` items, without holding the items in memory.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..spider.exporters import Exporter, _size, dumps, to_dict
from .common import Spool, as_text, column_kinds, require

__all__ = ["ParquetExporter", "read_parquet", "write_parquet"]

ROW_GROUP = 50_000
_META_KEY = b"wintergrab"


def _pyarrow() -> tuple[Any, Any]:
    pa = require("pyarrow", "parquet", "Parquet output")
    pq = require("pyarrow.parquet", "parquet", "Parquet output")
    return pa, pq


class ParquetExporter(Exporter):
    """Items in a Parquet file (see the module docs)."""

    def __init__(self, path: Path, *, append: bool) -> None:
        _pyarrow()  # say what to install before the crawl starts
        super().__init__(path, append=append)
        self.spool = Spool(path, append=append, seed=lambda: read_parquet(path))

    def write(self, item: Any) -> None:
        row = to_dict(item)
        line = dumps(row if isinstance(row, dict) else {"value": row}) + "\n"
        self.spool.write(line)
        self.bytes_written += _size(line)  # type: ignore[operator]  # the items as JSON: the file is compressed
        self.count += 1
        self._maybe_flush()

    def flush(self) -> None:
        self.spool.flush()

    def close(self) -> None:
        self.spool.finish(lambda temporary: write_parquet(temporary, self.spool.records))


def write_parquet(path: Path, records: Any) -> None:
    """Write the records ``records()`` gives (called twice: once to type the columns) to ``path``."""
    pa, pq = _pyarrow()
    kinds = column_kinds(records())
    types = {"bool": pa.bool_(), "int": pa.int64(), "float": pa.float64()}
    schema = pa.schema([pa.field(name, types.get(kind, pa.string())) for name, kind in kinds.items()])
    nested = [name for name, kind in kinds.items() if kind == "json"]
    schema = schema.with_metadata({_META_KEY: json.dumps({"json_columns": nested}).encode()})

    def convert(kind: str, value: Any) -> Any:
        if value is None:
            return None
        if kind == "float":
            return float(value)
        if kind in ("str", "json", "null"):
            return as_text(value)
        return value

    with pq.ParquetWriter(str(path), schema, compression="zstd") as writer:
        batch: list[dict[str, Any]] = []

        def put() -> None:
            columns = {name: [convert(kind, row.get(name)) for row in batch] for name, kind in kinds.items()}
            writer.write_table(pa.Table.from_pydict(columns, schema=schema))
            batch.clear()

        for record in records():
            batch.append(record)
            if len(batch) >= ROW_GROUP:
                put()
        if batch or not kinds:
            put()


def read_parquet(path: str | Path) -> Iterator[dict[str, Any]]:
    """The records of a Parquet file, with the JSON columns wintergrab wrote decoded again."""
    _, pq = _pyarrow()
    source = pq.ParquetFile(str(path))
    meta = source.schema_arrow.metadata or {}
    nested = set(json.loads(meta[_META_KEY]).get("json_columns", [])) if _META_KEY in meta else set()
    for batch in source.iter_batches(batch_size=10_000):
        for row in batch.to_pylist():
            for name in nested:
                if isinstance(row.get(name), str):
                    row[name] = json.loads(row[name])
            yield row
