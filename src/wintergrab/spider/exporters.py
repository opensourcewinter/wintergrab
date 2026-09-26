"""Write scraped items as they arrive: JSON Lines, JSON, CSV, SQLite, and through
:mod:`wintergrab.storage` Parquet, Excel and PostgreSQL (or any format registered with
:func:`register_exporter`)."""

from __future__ import annotations

import csv
import dataclasses
import importlib
import json
import os
import re
import sqlite3
import sys
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


def _size(text: str) -> int:
    """UTF-8 size of ``text`` (cheap for ASCII, the usual case)."""
    return len(text) if text.isascii() else len(text.encode("utf-8"))


class _CountingWriter:
    """A file-like wrapper counting the UTF-8 bytes written through it (for the csv module)."""

    def __init__(self, fh: Any, exporter: Exporter) -> None:
        self._fh = fh
        self._exporter = exporter

    def write(self, text: str) -> int:
        assert self._exporter.bytes_written is not None
        self._exporter.bytes_written += _size(text)
        return int(self._fh.write(text))


class Exporter:
    """Writes items to an output. Subclasses take ``(path, *, append)``, and ``unique_key`` too when
    they set :attr:`supports_unique_key` (upserts); an output given as a URL gets the URL."""

    #: Rows are upserted on ``unique_key`` (the exporter's constructor takes it).
    supports_unique_key = False

    def __init__(self, path: Path, *, append: bool) -> None:
        self.path = path
        self.append = append
        self.count = 0
        #: Bytes this exporter produced in this run, buffered ones included (``None``: not measurable).
        self.bytes_written: int | None = 0
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
        line = dumps(item) + "\n"
        self._fh.write(line)
        self.bytes_written += _size(line)  # type: ignore[operator]
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
        chunk = ("\n" if self._first else ",\n") + dumps(item)
        self._fh.write(chunk)
        self.bytes_written += _size(chunk)  # type: ignore[operator]
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
        self._out = _CountingWriter(self._fh, self)
        self._writer: csv.DictWriter[str] | None = None
        if self._fields:
            self._writer = csv.DictWriter(self._out, fieldnames=self._fields, extrasaction="ignore")

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
            self._writer = csv.DictWriter(self._out, fieldnames=self._fields, extrasaction="ignore")
            self._writer.writeheader()
        self._writer.writerow(flat)
        self.count += 1
        self._maybe_flush()

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


_IDENT = re.compile(r"[^0-9a-zA-Z_]")


def _sanitize(name: str) -> str:
    cleaned = _IDENT.sub("_", str(name)).strip("_") or "field"
    return cleaned if not cleaned[0].isdigit() else "f_" + cleaned


def _q(name: str) -> str:
    """Quote an identifier (already restricted to [0-9A-Za-z_])."""
    return '"' + name + '"'


class SqliteExporter(Exporter):
    """Items as rows of an ``items`` table; new keys become new columns.

    With ``unique_key`` the table gets a unique index on that column and
    items are *upserted*: re-running a crawl updates existing rows instead of
    duplicating them - handy for keeping a product catalogue current.
    Nested values are stored as JSON text. Item keys are mapped to safe,
    case-insensitively unique column names; the mapping is kept in the
    database (``_wintergrab_columns``) so later runs reuse it. The exporter
    only ever touches an ``items`` table it created itself.
    """

    table = "items"
    meta_table = "_wintergrab_columns"
    rowid = "_wg_rowid"
    supports_unique_key = True

    def __init__(self, path: Path, *, append: bool, unique_key: str | None = None) -> None:
        super().__init__(path, append=append)
        self.bytes_written = None  # pages are allocated in blocks: the file size is measured instead
        self.unique_key = unique_key
        self._conn = sqlite3.connect(str(path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        tables = {row[0] for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if self.table in tables and self.meta_table not in tables:
            self._conn.close()
            raise ValueError(
                f"{path} already has an '{self.table}' table that wintergrab did not create; use another output file"
            )
        if self.table in tables and not append and not unique_key:
            self._conn.execute(f"DROP TABLE {self.table}")  # a fresh crawl replaces its own previous output
            self._conn.execute(f"DELETE FROM {self.meta_table}")
        self._conn.execute(f"CREATE TABLE IF NOT EXISTS {self.meta_table} (key TEXT PRIMARY KEY, col TEXT NOT NULL)")
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self.table} ({_q(self.rowid)} INTEGER PRIMARY KEY AUTOINCREMENT)"
        )
        self._columns: dict[str, str] = dict(self._conn.execute(f"SELECT key, col FROM {self.meta_table}").fetchall())
        self._used = {c.lower() for c in self._columns.values()} | {self.rowid.lower()}
        self._used |= {row[1].lower() for row in self._conn.execute(f"PRAGMA table_info({self.table})")}
        if unique_key:
            column = self._column_for(unique_key)
            for (name,) in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND name LIKE 'wg_unique_%'",
                (self.table,),
            ).fetchall():
                if name != f"wg_unique_{column}":
                    self._conn.execute(f"DROP INDEX {_q(name)}")  # unique_key changed since the last run
            try:
                self._conn.execute(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {_q('wg_unique_' + column)} ON {self.table} ({_q(column)})"
                )
            except sqlite3.IntegrityError:
                self._conn.close()
                raise ValueError(
                    f"{path} already holds duplicate values of {unique_key!r}; it cannot become the unique key"
                ) from None
        self._conn.commit()

    def _column_for(self, key: str) -> str:
        """The column storing item key ``key`` (created on first use)."""
        column = self._columns.get(key)
        if column is not None:
            return column
        base = _sanitize(key)
        column, n = base, 2
        while column.lower() in self._used:
            column, n = f"{base}_{n}", n + 1
        self._conn.execute(f"ALTER TABLE {self.table} ADD COLUMN {_q(column)}")
        self._conn.execute(f"INSERT INTO {self.meta_table} (key, col) VALUES (?, ?)", (key, column))
        self._columns[key] = column
        self._used.add(column.lower())
        return column

    @staticmethod
    def _value(v: Any) -> Any:
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, (str, int, float, bytes)) or v is None:
            return v
        return json.dumps(v, default=_json_default, ensure_ascii=False)

    def write(self, item: Any) -> None:
        row = to_dict(item)
        if not isinstance(row, Mapping):
            row = {"value": row}
        values = {self._column_for(str(k)): self._value(v) for k, v in row.items()}
        columns = ", ".join(_q(c) for c in values)
        marks = ", ".join("?" for _ in values)
        sql = f"INSERT INTO {self.table} ({columns}) VALUES ({marks})"
        key_column = self._columns.get(self.unique_key) if self.unique_key else None
        if key_column is not None and key_column in values:
            updates = ", ".join(f"{_q(c)} = excluded.{_q(c)}" for c in values if c != key_column)
            sql += f" ON CONFLICT({_q(key_column)}) DO " + (f"UPDATE SET {updates}" if updates else "NOTHING")
        self._conn.execute(sql, list(values.values()))
        self.count += 1
        self._maybe_flush()

    def flush(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()


class StdoutExporter(Exporter):
    """JSON Lines on standard output (``output="-"``) - for piping crawls into other tools."""

    def __init__(self, path: Path, *, append: bool) -> None:
        super().__init__(path, append=append)

    def write(self, item: Any) -> None:
        line = dumps(item) + "\n"
        sys.stdout.write(line)
        self.bytes_written += _size(line)  # type: ignore[operator]
        self.count += 1
        self._maybe_flush()

    def flush(self) -> None:
        sys.stdout.flush()

    def close(self) -> None:
        sys.stdout.flush()


#: Outputs by file extension: an :class:`Exporter` class, or ``"module:Class"`` imported when first
#: used (for formats whose library is optional). More with :func:`register_exporter`.
EXPORTERS: dict[str, type[Exporter] | str] = {
    ".jsonl": JsonLinesExporter,
    ".ndjson": JsonLinesExporter,
    ".jl": JsonLinesExporter,
    ".json": JsonExporter,
    ".csv": CsvExporter,
    ".sqlite": SqliteExporter,
    ".sqlite3": SqliteExporter,
    ".db": SqliteExporter,
    ".parquet": "wintergrab.storage.parquet:ParquetExporter",
    ".pq": "wintergrab.storage.parquet:ParquetExporter",
    ".xlsx": "wintergrab.storage.xlsx:XlsxExporter",
}
#: Outputs by URL scheme (``postgresql://...``).
URL_EXPORTERS: dict[str, type[Exporter] | str] = {
    "postgresql": "wintergrab.storage.postgres:PostgresExporter",
    "postgres": "wintergrab.storage.postgres:PostgresExporter",
}
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://")


def register_exporter(key: str, exporter: type[Exporter] | str) -> None:
    """Add an output: ``".ext"`` for files with that extension, ``"scheme"`` for ``scheme://`` URLs.

    ``exporter`` is an :class:`Exporter` subclass, or ``"module:Class"`` imported when first used.
    """
    name = key.lower()
    if name.startswith("."):
        EXPORTERS[name] = exporter
    elif _SCHEME.match(name + "://"):
        URL_EXPORTERS[name] = exporter
    else:
        raise ValueError(f"an output is registered by extension ('.ext') or URL scheme ('scheme'), not {key!r}")


def output_scheme(output: str | os.PathLike[str]) -> str | None:
    """The URL scheme of an output (``"postgresql"``), or ``None`` for a file."""
    match = _SCHEME.match(os.fspath(output))
    return match.group(1).lower() if match else None


def _resolve(entry: type[Exporter] | str) -> type[Exporter]:
    if isinstance(entry, str):
        module, _, name = entry.partition(":")
        return getattr(importlib.import_module(module), name)  # type: ignore[no-any-return]
    return entry


def open_exporter(path: str | os.PathLike[str], *, append: bool = False, unique_key: str | None = None) -> Exporter:
    """Pick an exporter by the output's extension (``.jsonl``, ``.json``, ``.csv``, ``.sqlite``/``.db``,
    ``.parquet``, ``.xlsx``) or URL scheme (``postgresql://``).

    ``"-"`` writes JSON Lines to standard output.
    """
    text = os.fspath(path)
    if text == "-":
        return StdoutExporter(Path("-"), append=append)
    scheme = output_scheme(text)
    if (scheme or Path(text).suffix.lower()) not in (URL_EXPORTERS if scheme else EXPORTERS):
        from ..plugins import load_plugins

        load_plugins()  # a plugin may add it
    if scheme is not None:
        entry = URL_EXPORTERS.get(scheme)
        if entry is None:
            known = ", ".join(f"{s}://" for s in sorted(URL_EXPORTERS))
            raise ValueError(f"no output for {scheme}:// URLs (known: {known})")
        cls = _resolve(entry)
        return cls(text, append=append, **({"unique_key": unique_key} if cls.supports_unique_key else {}))  # type: ignore[arg-type]
    target = Path(text)
    found = EXPORTERS.get(target.suffix.lower())
    if found is None:
        known = ", ".join(sorted(EXPORTERS))
        raise ValueError(f"Unsupported output format {target.suffix!r}; use one of {known}, or postgresql://...")
    cls = _resolve(found)
    target.parent.mkdir(parents=True, exist_ok=True)
    return cls(target, append=append, **({"unique_key": unique_key} if cls.supports_unique_key else {}))


def write_items(path: str | os.PathLike[str], items: list[Any]) -> Path:
    """Write a list of items in one go (format chosen by extension)."""
    exporter = open_exporter(path)
    try:
        for item in items:
            exporter.write(item)
    finally:
        exporter.close()
    return exporter.path
