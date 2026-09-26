"""DuckDB: items as a table of a DuckDB database (``output = "items.duckdb"``; needs duckdb).

DuckDB is a database for analysis kept in one file: the items become its ``items`` table, for SQL
(``duckdb items.duckdb -c "SELECT ..."``) and for anything that reads DuckDB. Each field is a column,
typed from all its values: ``BIGINT``, ``DOUBLE`` (integers and decimals mixed), ``BOOLEAN``,
``VARCHAR``, or ``JSON`` for nested values (objects, lists), which DuckDB queries as they are
(``tags->>'$[0]'``). A field whose values mix kinds (numbers and text) is text, and so are integers
beyond 64 bits. Columns are named after the fields, in lower case with letters, digits and _
(``Price (USD)`` is ``price_usd``; ``Name`` and ``name`` are ``name`` and ``name_2``), and keep their
names from one run to the next. The ``_wintergrab_columns`` table says which field each column holds,
so :func:`read_duckdb` gives the items back under their own names.

With ``unique_key`` (``--unique-key url``) rows are upserted on that field, as in the other
databases: crawling again updates them (the fields an item has replace the row's, the others keep
theirs) and adds the new ones. Without it, a fresh crawl replaces the table, and a resumed one adds
to it.

While the crawl runs, its items are spooled as JSON Lines beside the file (see
:class:`~wintergrab.storage.common.Spool`): a crash loses none. The table is written when the crawl
ends, in one transaction, without holding the items in memory: the database has the new table or
the one before, never half of one. The rest of the database (other tables, views) is left as it is,
and an ``items`` table that wintergrab did not create is refused.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from ..errors import ConfigurationError, ExportError
from ..spider.exporters import Exporter, _size, dumps, to_dict
from .common import Spool, column_name, kind_of, require, widen

__all__ = ["DuckDBExporter", "read_duckdb"]

TABLE = "items"
_META = "_wintergrab_columns"
_ROWID = "_wg_rowid"
_HAS = "_wg_has"  # (in the rows loaded when upserting: the columns each item has)
_TYPES = {"bool": "BOOLEAN", "int": "BIGINT", "float": "DOUBLE", "json": "JSON"}  # the others: VARCHAR
_LOCK_WAIT = 5.0  # seconds to wait for another program's hold on the file (a reader's) to end
_OBJECT_SIZE = 16 * 1024 * 1024  # the largest item DuckDB reads by default, in bytes of JSON


def _duckdb() -> Any:
    return require("duckdb", "duckdb", "DuckDB output")


def _q(name: str) -> str:
    """A quoted identifier."""
    return '"' + name.replace('"', '""') + '"'


def _connect(path: Path, *, read_only: bool) -> Any:
    """A connection to the database at ``path``; one that another program holds (DuckDB lets a file have one
    writer, or readers) is waited for a few seconds."""
    duckdb = _duckdb()
    deadline = time.monotonic() + _LOCK_WAIT
    while True:
        try:
            return duckdb.connect(str(path), read_only=read_only)
        except duckdb.IOException as exc:
            if not _held(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def _held(exc: BaseException) -> bool:
    """Whether DuckDB could not open a file because another program has it ("Could not set lock on file";
    on Windows, "File is already open in")."""
    message = str(exc).lower()
    return "could not set lock" in message or "already open" in message


def _objects(connection: Any) -> dict[str, str]:
    """The tables and views of the database, by lower-case name (DuckDB's names ignore case): their types."""
    rows = connection.execute(
        "SELECT table_name, table_type FROM information_schema.tables"
        " WHERE table_schema = 'main' AND table_catalog = current_database()"
    ).fetchall()
    return {name.lower(): kind for name, kind in rows}


def _check(path: Path, objects: dict[str, str]) -> None:
    if TABLE in objects and (_META not in objects or objects[TABLE] != "BASE TABLE"):
        what = "view" if objects[TABLE] == "VIEW" else "table"
        raise ConfigurationError(
            f"{path} already has an '{TABLE}' {what} that wintergrab did not create; use another output file",
            key="output",
        )


def _fields(path: Path) -> list[tuple[str, str, str]] | None:
    """The fields (key, column, kind) of the ``items`` table wintergrab wrote in the database at ``path``;
    ``None`` when there is no such table (or no database yet). A file that is not a DuckDB database, or an
    ``items`` table wintergrab did not create, is an error."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    duckdb = _duckdb()
    try:
        connection = _connect(path, read_only=True)
    except duckdb.Error as exc:
        raise ConfigurationError(f"cannot write items to {path}: {str(exc).strip()}", key="output") from None
    try:
        objects = _objects(connection)
        _check(path, objects)
        if TABLE not in objects:
            return None
        rows = connection.execute(f"SELECT key, col, kind FROM {_META} ORDER BY position").fetchall()
        return [(str(key), str(col), str(kind)) for key, col, kind in rows]
    finally:
        connection.close()


class DuckDBExporter(Exporter):
    """Items as the ``items`` table of a DuckDB database (see the module docs)."""

    supports_unique_key = True

    def __init__(self, path: Path, *, append: bool, unique_key: str | None = None) -> None:
        _duckdb()  # say what to install before the crawl starts
        super().__init__(path, append=append)
        self.unique_key = unique_key
        found = _fields(path)  # (and what is wrong with the file, before the crawl starts)
        #: The fields of the table there: theirs keep their columns' names.
        self._fields = found or []
        #: Whether the rows there stay (added to, or upserted into), with their columns.
        self._keep = append or unique_key is not None
        # (the rows there are those of the items table wintergrab wrote, never of another table)
        self.spool = Spool(path, append=append, seed=(lambda: read_duckdb(path)) if found is not None else None)
        if found is not None and unique_key is not None and not append:
            for record in read_duckdb(path):  # (the rows there, which the items update)
                self.spool.write(json.dumps(record, ensure_ascii=False) + "\n")

    def write(self, item: Any) -> None:
        row = to_dict(item)
        line = dumps(row if isinstance(row, dict) else {"value": row}) + "\n"
        self.spool.write(line)
        self.bytes_written += _size(line)  # type: ignore[operator]  # the items as JSON
        self.count += 1
        self._maybe_flush()

    def flush(self) -> None:
        self.spool.flush()

    def close(self) -> None:
        self.spool.close()
        try:
            _write(self.path, self.spool.records, self._fields, keep=self._keep, unique_key=self.unique_key)
        except Exception as exc:
            reason = str(exc).strip() or type(exc).__name__
            raise ExportError(
                f"could not write {self.path} (the items are kept in {self.spool.path}): {reason}"
            ) from exc
        self.spool.path.unlink(missing_ok=True)


def _write(
    path: Path,
    records: Callable[[], Iterable[dict[str, Any]]],
    fields: list[tuple[str, str, str]],
    *,
    keep: bool,
    unique_key: str | None,
) -> None:
    """Write ``records()`` as the ``items`` table of the database at ``path``, in one transaction.

    ``fields`` are the table's before: their columns keep their names, and with ``keep`` the table keeps
    its columns (and their kinds, which values widen). With ``unique_key``, the records of a key are one
    row: the first one's place, and each field's value in the last record that has it."""
    known = {key: column for key, column, _ in fields}
    names: dict[str, str] = {}  # the fields' columns, in the table's order
    kinds: dict[str, str] = {}
    if keep:
        names.update(known)
        kinds.update((key, kind) for key, _, kind in fields)
    used = set(known.values()) | {_ROWID, _HAS}
    place = {column: n for n, column in enumerate(names.values())}
    load = path.with_name(f".{path.name}.{os.getpid()}.load.jsonl")
    largest = 0
    try:
        with open(load, "w", encoding="utf-8") as fh:
            for number, record in enumerate(records(), 1):
                row: dict[str, Any] = {_ROWID: number}
                for key, value in record.items():
                    column = names.get(key)
                    if column is None:
                        column = names[key] = known.get(key) or column_name(key, used)
                        used.add(column)
                        place[column] = len(place)
                    kinds[key] = widen(kinds.get(key, "null"), kind_of(value))
                    row[column] = value
                if unique_key is not None:
                    row[_HAS] = [place[column] for column in row if column != _ROWID]
                line = dumps(row) + "\n"
                largest = max(largest, _size(line))
                fh.write(line)
        types = {_ROWID: "BIGINT"} | {names[key]: _TYPES.get(kinds[key], "VARCHAR") for key in names}
        if unique_key is not None:
            types[_HAS] = "INTEGER[]"
        if path.exists() and path.stat().st_size == 0:
            path.unlink()  # (an empty file: no database yet)
        connection = _connect(path, read_only=False)
        try:
            _check(path, _objects(connection))  # (in case one was made since the crawl started)
            connection.execute("BEGIN TRANSACTION")
            try:
                connection.execute(f"DROP TABLE IF EXISTS {TABLE}")
                connection.execute(
                    f"CREATE OR REPLACE TABLE {_META} (position INTEGER, key VARCHAR, col VARCHAR, kind VARCHAR)"
                )
                if names:
                    connection.executemany(
                        f"INSERT INTO {_META} VALUES (?, ?, ?, ?)",
                        [(n, key, column, kinds[key]) for n, (key, column) in enumerate(names.items())],
                    )
                source = "read_json(?, format = 'newline_delimited', columns = ?, maximum_object_size = ?)"
                query = _select(list(names.values()), source, names.get(unique_key) if unique_key else None)
                connection.execute(f"CREATE TABLE {TABLE} AS {query}", [str(load), types, max(largest, _OBJECT_SIZE)])
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()
    finally:
        load.unlink(missing_ok=True)


def _select(columns: list[str], source: str, key: str | None) -> str:
    """The rows of the table from those loaded: each as it is, or with ``key`` one row a value of it."""
    every = ", ".join(_q(c) for c in [_ROWID, *columns])
    if key is None:
        return f"SELECT {every} FROM {source} ORDER BY {_q(_ROWID)}"
    merged = ", ".join(
        _q(c)
        if c == key
        else f"arg_max_null({_q(c)}, {_q(_ROWID)}) FILTER (WHERE list_contains({_q(_HAS)}, {n})) AS {_q(c)}"
        for n, c in enumerate(columns)
    )
    return (
        f"WITH loaded AS (SELECT * FROM {source}) "
        f"SELECT min({_q(_ROWID)}) AS {_q(_ROWID)}, {merged} FROM loaded WHERE {_q(key)} IS NOT NULL GROUP BY {_q(key)} "
        f"UNION ALL SELECT {every} FROM loaded WHERE {_q(key)} IS NULL "  # (no key: a row of its own)
        f"ORDER BY {_q(_ROWID)}"
    )


def read_duckdb(path: str | Path) -> Iterator[dict[str, Any]]:
    """The records of a DuckDB database: its ``items`` table, or its only table. A table wintergrab wrote
    gives its fields back under their own names; ``JSON`` columns give their values decoded."""
    duckdb = _duckdb()
    target = Path(path)
    try:
        connection = _connect(target, read_only=True)
    except duckdb.Error as exc:
        raise ConfigurationError(f"cannot read {target}: {str(exc).strip()}") from None
    try:
        objects = _objects(connection)
        tables = [name for name in objects if name != _META]
        if TABLE in objects:
            table = TABLE
        elif len(tables) == 1:
            table = tables[0]
        else:
            found = f"its tables: {', '.join(sorted(tables))}" if tables else "it has none"
            raise ConfigurationError(f"{target}: no '{TABLE}' table to read ({found})")
        renames: dict[str, str] = {}
        if table == TABLE and _META in objects:
            renames = {
                str(col): str(key) for key, col in connection.execute(f"SELECT key, col FROM {_META}").fetchall()
            }
        described = connection.execute(
            "SELECT column_name, data_type FROM information_schema.columns"
            " WHERE table_schema = 'main' AND table_catalog = current_database() AND lower(table_name) = ?"
            " ORDER BY ordinal_position",
            [table],
        ).fetchall()
        encoded = {str(name) for name, kind in described if kind == "JSON"}
        order = f" ORDER BY {_q(_ROWID)}" if any(name == _ROWID for name, _ in described) else ""
        cursor = connection.execute(f"SELECT * FROM {_q(table)}{order}")
        columns = [d[0] for d in cursor.description]
        while rows := cursor.fetchmany(2_000):
            for row in rows:
                record: dict[str, Any] = {}
                for column, value in zip(columns, row, strict=True):
                    if value is None or column == _ROWID:
                        continue
                    if column in encoded and isinstance(value, str):
                        value = json.loads(value)
                    record[renames.get(column, column)] = value
                yield record
    except duckdb.Error as exc:
        raise ConfigurationError(f"cannot read {target}: {str(exc).strip()}") from None
    finally:
        connection.close()
