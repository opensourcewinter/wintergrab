"""Write scraped items to JSON Lines, JSON, CSV or SQLite as they arrive."""

from __future__ import annotations

import csv
import dataclasses
import json
import os
import re
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:  # optional speed-up: pip install "wintergrab[speed]"
    import orjson
except ImportError:  # pragma: no cover - depends on the environment
    orjson = None  # type: ignore[assignment]

FLUSH_EVERY = 64  # items
FLUSH_INTERVAL = 1.0  # seconds


def to_dict(item: Any) -> Any:
    """Turn dataclasses, pydantic models, attrs classes... into plain dicts."""
    if isinstance(item, Mapping):
        return dict(item)
    if dataclasses.is_dataclass(item) and not isinstance(item, type):
        return dataclasses.asdict(item)
    for method in ("model_dump", "dict", "_asdict", "to_dict"):
        fn = getattr(item, method, None)
        if callable(fn):
            try:
                return fn()
            except TypeError:
                continue
    if hasattr(item, "__dict__"):
        return {k: v for k, v in vars(item).items() if not k.startswith("_")}
    return item


def _json_default(value: Any) -> Any:
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return str(value)


def dumps(item: Any) -> str:
    """One item as compact JSON (uses orjson when installed)."""
    data = to_dict(item)
    if orjson is not None:
        try:
            return orjson.dumps(data, default=_json_default, option=orjson.OPT_NON_STR_KEYS).decode()
        except TypeError:
            pass  # e.g. integers beyond 64 bits: fall back to the stdlib
    return json.dumps(data, ensure_ascii=False, default=_json_default)


class Exporter:
    def __init__(self, path: Path, *, append: bool) -> None:
        self.path = path
        self.append = append
        self.count = 0
        self._unflushed = 0
        self._last_flush = time.monotonic()

    def write(self, item: Any) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def flush(self) -> None:
        """Push buffered items to disk (called on checkpoints and at the end)."""

    def _maybe_flush(self) -> None:
        self._unflushed += 1
        now = time.monotonic()
        if self._unflushed >= FLUSH_EVERY or now - self._last_flush >= FLUSH_INTERVAL:
            self.flush()
            self._unflushed = 0
            self._last_flush = now

    def close(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class JsonLinesExporter(Exporter):
    """One JSON object per line - the best format for big, resumable crawls."""

    def __init__(self, path: Path, *, append: bool) -> None:
        super().__init__(path, append=append)
        self._fh = open(path, "a" if append else "w", encoding="utf-8")  # noqa: SIM115 - closed in close()

    def write(self, item: Any) -> None:
        self._fh.write(dumps(item) + "\n")
        self.count += 1
        self._maybe_flush()

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


class JsonExporter(Exporter):
    """A single JSON array (reopened and extended when a crawl resumes)."""

    def __init__(self, path: Path, *, append: bool) -> None:
        super().__init__(path, append=append)
        self._first = True
        if append and path.exists() and path.stat().st_size > 0:
            body, count = self._reopen(path)
            self._first = count == 0
            self._fh = open(path, "w", encoding="utf-8")  # noqa: SIM115 - closed in close()
            self._fh.write(body)
        else:
            self._fh = open(path, "w", encoding="utf-8")  # noqa: SIM115 - closed in close()
            self._fh.write("[")
        self._fh.flush()

    @staticmethod
    def _reopen(path: Path) -> tuple[str, int]:
        """The existing array without its closing ``]``, and how many items it holds.

        Handles files left unterminated by a crash (no ``]``, or a half-written
        last item, which is dropped). Refuses anything that isn't recognisably
        a JSON array we wrote, rather than risk corrupting it.
        """
        content = path.read_text(encoding="utf-8").rstrip()
        candidates = []
        if content.endswith("]"):
            candidates.append(content[:-1].rstrip())
        candidates.append(content)  # crashed before "]" was written
        cut = content.rfind(",\n")
        if cut != -1:
            candidates.append(content[:cut])  # crashed in the middle of an item
        for body in candidates:
            try:
                data = json.loads(body + "\n]")
            except ValueError:
                continue
            if isinstance(data, list):
                return body, len(data)
        raise ValueError(f"{path} is not a JSON array wintergrab can extend; move it away or use .jsonl output")

    def write(self, item: Any) -> None:
        self._fh.write(("\n" if self._first else ",\n") + dumps(item))
        self._first = False
        self.count += 1
        self._maybe_flush()

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        self._fh.write("\n]\n")
        self._fh.close()


class CsvExporter(Exporter):
    """CSV with columns taken from the first item (nested values become JSON)."""

    def __init__(self, path: Path, *, append: bool) -> None:
        super().__init__(path, append=append)
        self._fields: list[str] | None = None
        resuming = append and path.exists() and path.stat().st_size > 0
        if resuming:
            with open(path, newline="", encoding="utf-8") as fh:
                header = next(csv.reader(fh), None)
            self._fields = header or None
        self._fh = open(path, "a" if resuming else "w", newline="", encoding="utf-8")  # noqa: SIM115 - closed in close()
        self._writer: csv.DictWriter[str] | None = None
        if self._fields:
            self._writer = csv.DictWriter(self._fh, fieldnames=self._fields, extrasaction="ignore")

    def write(self, item: Any) -> None:
        row = to_dict(item)
        if not isinstance(row, Mapping):
            row = {"value": row}
        flat = {
            k: (
                v
                if isinstance(v, (str, int, float, bool)) or v is None
                else json.dumps(v, default=_json_default, ensure_ascii=False)
            )
            for k, v in row.items()
        }
        if self._writer is None:
            self._fields = list(flat)
            self._writer = csv.DictWriter(self._fh, fieldnames=self._fields, extrasaction="ignore")
            self._writer.writeheader()
        self._writer.writerow(flat)
        self.count += 1
        self._maybe_flush()

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


_IDENT = re.compile(r"[^0-9a-zA-Z_]")


def _column(name: str) -> str:
    """A safe, quoted SQLite column name."""
    cleaned = _IDENT.sub("_", str(name)) or "_"
    return '"' + cleaned.replace('"', "") + '"'


class SqliteExporter(Exporter):
    """Items as rows of an ``items`` table; new keys become new columns.

    With ``unique_key`` the table gets a unique index on that column and
    items are *upserted*: re-running a crawl updates existing rows instead of
    duplicating them - handy for keeping a product catalogue current.
    Nested values are stored as JSON text.
    """

    table = "items"

    def __init__(self, path: Path, *, append: bool, unique_key: str | None = None) -> None:
        super().__init__(path, append=append)
        self.unique_key = unique_key
        self._conn = sqlite3.connect(str(path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        if not append and not unique_key:
            self._conn.execute(f"DROP TABLE IF EXISTS {self.table}")
        self._conn.execute(f"CREATE TABLE IF NOT EXISTS {self.table} (_rowid INTEGER PRIMARY KEY AUTOINCREMENT)")
        self._columns = {row[1] for row in self._conn.execute(f"PRAGMA table_info({self.table})")}
        if unique_key:
            self._ensure_columns([unique_key])
            self._conn.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS idx_items_unique ON {self.table} ({_column(unique_key)})"
            )
        self._conn.commit()

    def _ensure_columns(self, names: list[str]) -> None:
        for name in names:
            column = _column(name).strip('"')
            if column not in self._columns:
                self._conn.execute(f"ALTER TABLE {self.table} ADD COLUMN {_column(name)}")
                self._columns.add(column)

    def write(self, item: Any) -> None:
        row = to_dict(item)
        if not isinstance(row, Mapping):
            row = {"value": row}
        values = {
            _column(k): (
                v
                if isinstance(v, (str, int, float, bytes)) or v is None
                else (int(v) if isinstance(v, bool) else json.dumps(v, default=_json_default, ensure_ascii=False))
            )
            for k, v in row.items()
        }
        self._ensure_columns(list(row))
        columns = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        sql = f"INSERT INTO {self.table} ({columns}) VALUES ({marks})"
        if self.unique_key and _column(self.unique_key) in values:
            updates = ", ".join(f"{c} = excluded.{c}" for c in values if c != _column(self.unique_key))
            sql += f" ON CONFLICT({_column(self.unique_key)}) DO " + (f"UPDATE SET {updates}" if updates else "NOTHING")
        self._conn.execute(sql, list(values.values()))
        self.count += 1
        self._maybe_flush()

    def flush(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()


EXPORTERS: dict[str, type[Exporter]] = {
    ".jsonl": JsonLinesExporter,
    ".ndjson": JsonLinesExporter,
    ".jl": JsonLinesExporter,
    ".json": JsonExporter,
    ".csv": CsvExporter,
    ".sqlite": SqliteExporter,
    ".sqlite3": SqliteExporter,
    ".db": SqliteExporter,
}


def open_exporter(path: str | os.PathLike[str], *, append: bool = False, unique_key: str | None = None) -> Exporter:
    """Pick an exporter from the file extension (``.jsonl``, ``.json``, ``.csv``, ``.sqlite``/``.db``)."""
    target = Path(path)
    cls = EXPORTERS.get(target.suffix.lower())
    if cls is None:
        raise ValueError(f"Unsupported output format {target.suffix!r}; use .jsonl, .json, .csv or .sqlite")
    target.parent.mkdir(parents=True, exist_ok=True)
    if cls is SqliteExporter:
        return SqliteExporter(target, append=append, unique_key=unique_key)
    return cls(target, append=append)


def write_items(path: str | os.PathLike[str], items: list[Any]) -> Path:
    """Write a list of items in one go (format chosen by extension)."""
    exporter = open_exporter(path)
    try:
        for item in items:
            exporter.write(item)
    finally:
        exporter.close()
    return exporter.path
