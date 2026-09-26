"""PostgreSQL: items as the rows of a table (``output = "postgresql://crawler@db.example/shop?table=products"``).

Needs psycopg 3 (``pip install "wintergrab[postgres]"``). The table (``items`` unless the URL says
``?table=NAME``, or ``?table=schema.name``) is created on first use. Each new field becomes a
column typed from its first value: ``boolean``, ``bigint``, ``double precision``, ``text``, or
``jsonb`` for nested values. When a later value does not fit, the column widens to hold both: a
``bigint`` column that gets a decimal becomes ``double precision``; one that gets text becomes
``text``; one that gets an object becomes ``jsonb``. Nothing is ever cut to fit.

With ``unique_key`` (``--unique-key url``), rows are upserted on that field: crawling again updates
them. Without it, a fresh crawl empties its table first, as a file output is replaced; a resumed
crawl adds to it.

The exporter only writes to tables it created: it keeps their fields and columns in a
``_wintergrab_columns`` table beside them, and refuses a table of the same name that it did not
create. Keep the password out of the URL (``PGPASSWORD`` or ``~/.pgpass`` are read); where the URL is
shown or kept (logs, run records), its password is left out.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..errors import ConfigurationError, ExportError
from ..redact import redact_url
from ..spider.exporters import Exporter, _json_default, _size, dumps, to_dict
from .common import kind_of, require

__all__ = ["PostgresExporter", "read_postgres"]

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_COLUMN = re.compile(r"[^0-9a-zA-Z_]")
_META = "_wintergrab_columns"
_ROWID = "_wg_rowid"
_TYPES = {"bool": "boolean", "int": "bigint", "float": "double precision", "str": "text", "json": "jsonb"}
_OPTIONS = ("table",)  # the URL's own query parameters (the others are the connection's)


def _psycopg() -> Any:
    return require("psycopg", "postgres", "PostgreSQL output")


def parse_target(url: str) -> tuple[str, list[str]]:
    """The connection string (without wintergrab's options) and the table, as ``[schema, ]name``."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    options = {k: v for k, v in query if k in _OPTIONS}
    dsn = urlunsplit(parts._replace(query=urlencode([(k, v) for k, v in query if k not in _OPTIONS])))
    table = options.get("table") or "items"
    names = table.split(".")
    if len(names) > 2 or not all(_NAME.match(n) for n in names):
        raise ConfigurationError(f"table {table!r}: use letters, digits and _ (or schema.table)", key="output")
    return dsn, names


def _connect(url: str) -> tuple[Any, list[str]]:
    psycopg = _psycopg()
    dsn, names = parse_target(url)
    try:
        return psycopg.connect(dsn, autocommit=False), names
    except psycopg.Error as exc:
        raise ConfigurationError(f"cannot connect to {redact_url(url)}: {str(exc).strip()}", key="output") from None


def _column_name(key: str, used: set[str]) -> str:
    base = re.sub(r"_+", "_", _COLUMN.sub("_", key)).strip("_").lower()[:55] or "field"
    if base[0].isdigit():
        base = "f_" + base
    name, n = base, 2
    while name in used:
        name, n = f"{base}_{n}", n + 1
    return name


def _index_name(table: str, column: str) -> str:
    """The unique index of a table's key column (index names are shared by a schema's tables)."""
    digest = hashlib.sha1(f"{table}.{column}".encode()).hexdigest()[:12]
    return f"wg_unique_{digest}"


def _wider(column_type: str, kind: str) -> str | None:
    """The type a column must become to hold a value of ``kind`` (``None``: it holds it already)."""
    if kind == "null" or column_type in ("jsonb", "text"):
        return None  # jsonb holds any value; text too (objects as JSON text)
    if column_type == _TYPES[kind] or (column_type == "double precision" and kind == "int"):
        return None
    if column_type == "bigint" and kind == "float":
        return "double precision"
    return "jsonb" if kind == "json" else "text"


class PostgresExporter(Exporter):
    """Items as the rows of a PostgreSQL table (see the module docs)."""

    supports_unique_key = True

    def __init__(self, url: str, *, append: bool, unique_key: str | None = None) -> None:
        super().__init__(Path(redact_url(url)), append=append)
        self.url = url
        self.unique_key = unique_key
        self.bytes_written = 0  # the items as JSON
        self._psycopg = _psycopg()
        from psycopg import sql

        self._sql = sql
        self._conn, names = _connect(url)
        self.table_name = ".".join(names)
        self._table = sql.Identifier(*names)
        self._meta = sql.Identifier(*names[:-1], _META)
        self._rows: list[dict[str, Any]] = []
        try:
            self._open(append)
        except self._psycopg.Error as exc:
            self._conn.close()
            raise ExportError(f"{self.table_name}: {str(exc).strip()}") from None
        except Exception:
            self._conn.close()
            raise

    # -- the table ---------------------------------------------------------------------------- #
    def _execute(self, query: Any, params: Any = None) -> Any:
        return self._conn.execute(query, params)

    def _open(self, append: bool) -> None:
        sql = self._sql
        self._execute(sql.SQL(
            "CREATE TABLE IF NOT EXISTS {} (tbl text NOT NULL, key text NOT NULL, col text NOT NULL, "
            "type text NOT NULL, PRIMARY KEY (tbl, key))"
        ).format(self._meta))  # fmt: skip
        rows = self._execute(sql.SQL("SELECT key, col, type FROM {} WHERE tbl = %s").format(self._meta),
                             (self.table_name,)).fetchall()  # fmt: skip
        exists = self._execute("SELECT to_regclass(%s)", (self._table.as_string(self._conn),)).fetchone()[0]
        ours = any(key == "" for key, _, _ in rows)  # the table's own row: wintergrab created it
        if exists and not ours:
            raise ConfigurationError(
                f"{self.table_name} already exists and wintergrab did not create it; name another (?table=NAME)",
                key="output",
            )
        if not exists:
            self._execute(sql.SQL("DELETE FROM {} WHERE tbl = %s").format(self._meta), (self.table_name,))
            self._execute(sql.SQL("CREATE TABLE {} ({} bigserial PRIMARY KEY)").format(
                self._table, sql.Identifier(_ROWID)))  # fmt: skip
            self._execute(sql.SQL("INSERT INTO {} (tbl, key, col, type) VALUES (%s, '', %s, 'table')").format(
                self._meta), (self.table_name, _ROWID))  # fmt: skip
            rows = []
        elif not append and not self.unique_key:
            self._execute(sql.SQL("TRUNCATE {}").format(self._table))  # a fresh crawl replaces its rows
        self._load_columns(rows)
        if self.unique_key:
            self._unique_index()
        self._conn.commit()

    def _load_columns(self, rows: list[tuple[str, str, str]] | None = None) -> None:
        if rows is None:
            rows = self._execute(self._sql.SQL("SELECT key, col, type FROM {} WHERE tbl = %s").format(self._meta),
                                 (self.table_name,)).fetchall()  # fmt: skip
        self._columns: dict[str, str] = {key: col for key, col, _ in rows if key}
        self._types: dict[str, str] = {col: kind for key, col, kind in rows if key}
        self._used = set(self._types) | {_ROWID}

    def _rollback(self) -> None:
        """Undo the transaction, and forget the columns it added (they are gone with it)."""
        self._conn.rollback()
        self._load_columns()

    def _unique_index(self) -> None:
        """The unique index on ``unique_key``'s column (once the column exists); others go."""
        sql = self._sql
        column = self._columns.get(self.unique_key or "")
        wanted = _index_name(self.table_name, column) if column else None
        schema = self.table_name.rpartition(".")[0] or None
        for (name,) in self._execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = %s AND indexname LIKE 'wg\\_unique\\_%%'"
            " AND schemaname = COALESCE(%s, current_schema())",
            (self.table_name.rpartition(".")[2], schema),
        ).fetchall():
            if name != wanted:  # unique_key changed since the last run
                self._execute(sql.SQL("DROP INDEX {}").format(sql.Identifier(*([schema] if schema else []), name)))
        if column:
            try:
                self._execute(sql.SQL("CREATE UNIQUE INDEX IF NOT EXISTS {} ON {} ({})").format(
                    sql.Identifier(str(wanted)), self._table, sql.Identifier(column)))  # fmt: skip
            except self._psycopg.errors.UniqueViolation:
                raise ConfigurationError(
                    f"{self.table_name} already holds duplicate values of {self.unique_key!r}; it cannot be the"
                    " unique key",
                    key="unique_key",
                ) from None

    def _column(self, key: str, kind: str) -> str:
        """The column of field ``key``, created or widened to hold a value of ``kind``."""
        sql = self._sql
        column = self._columns.get(key)
        if column is None:
            column = _column_name(key, self._used)
            pg_type = _TYPES.get(kind, "text")
            self._execute(
                sql.SQL("ALTER TABLE {} ADD COLUMN {} " + pg_type).format(self._table, sql.Identifier(column))
            )
            self._execute(sql.SQL("INSERT INTO {} (tbl, key, col, type) VALUES (%s, %s, %s, %s)").format(self._meta),
                          (self.table_name, key, column, pg_type))  # fmt: skip
            self._columns[key] = column
            self._types[column] = pg_type
            self._used.add(column)
            if key == self.unique_key:
                self._unique_index()
            self._conn.commit()  # a column, at once: the rows waiting for flush() need it
            return column
        wider = _wider(self._types[column], kind)
        if wider is not None:
            using = "to_jsonb({c})" if wider == "jsonb" else "{c}::" + wider
            self._execute(sql.SQL("ALTER TABLE {} ALTER COLUMN {} TYPE " + wider + " USING " + using).format(
                self._table, sql.Identifier(column), c=sql.Identifier(column)))  # fmt: skip
            self._execute(sql.SQL("UPDATE {} SET type = %s WHERE tbl = %s AND key = %s").format(self._meta),
                          (wider, self.table_name, key))  # fmt: skip
            self._types[column] = wider
            self._conn.commit()
        return column

    # -- writing ------------------------------------------------------------------------------ #
    def write(self, item: Any) -> None:
        row = to_dict(item)
        if not isinstance(row, dict):
            row = {"value": row}
        text = dumps(row)
        self.bytes_written += _size(text) + 1  # type: ignore[operator]
        record = json.loads(text)  # JSON values, as every output gets them
        try:
            for key, value in record.items():
                self._column(str(key), kind_of(value))
        except self._psycopg.Error as exc:
            self._rollback()
            raise ExportError(f"{self.table_name}: {str(exc).strip()}") from None
        self._rows.append(record)
        self.count += 1
        self._maybe_flush()

    def _value(self, column: str, value: Any) -> Any:
        pg_type = self._types[column]
        if value is None:
            return None
        if pg_type == "jsonb":
            return self._psycopg.types.json.Jsonb(value)
        if pg_type == "text":
            return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=_json_default)
        if pg_type == "double precision":
            return float(value)
        return value

    def flush(self) -> None:
        if not self._rows:
            self._conn.commit()
            return
        sql = self._sql
        key_column = self._columns.get(self.unique_key or "") if self.unique_key else None
        groups: dict[tuple[str, ...], list[list[Any]]] = {}
        for record in self._rows:
            columns = tuple(self._columns[str(k)] for k in record)
            groups.setdefault(columns, []).append([self._value(self._columns[str(k)], v) for k, v in record.items()])
        try:
            with self._conn.cursor() as cursor:
                for columns, values in groups.items():
                    query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                        self._table,
                        sql.SQL(", ").join(map(sql.Identifier, columns)),
                        sql.SQL(", ").join(sql.Placeholder() * len(columns)),
                    )
                    if key_column is not None and key_column in columns:
                        others = [c for c in columns if c != key_column]
                        action = sql.SQL("DO UPDATE SET {}").format(sql.SQL(", ").join(
                            sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c)) for c in others
                        )) if others else sql.SQL("DO NOTHING")  # fmt: skip
                        query = sql.SQL("{} ON CONFLICT ({}) {}").format(query, sql.Identifier(key_column), action)
                    cursor.executemany(query, values)
            self._conn.commit()
        except self._psycopg.Error as exc:
            self._rollback()
            raise ExportError(f"{self.table_name}: {len(self._rows)} item(s) not written: {str(exc).strip()}") from None
        finally:
            self._rows.clear()

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self._conn.close()


def read_postgres(url: str) -> Iterator[dict[str, Any]]:
    """The rows of a table as records: a table wintergrab wrote gives its fields back by their names."""
    psycopg = _psycopg()
    from psycopg import sql
    from psycopg.rows import dict_row

    connection, names = _connect(url)
    table = sql.Identifier(*names)
    meta = sql.Identifier(*names[:-1], _META)
    try:
        with connection:
            renames: dict[str, str] = {}
            try:
                rows = connection.execute(sql.SQL("SELECT key, col FROM {} WHERE tbl = %s AND key <> ''").format(meta),
                                          (".".join(names),)).fetchall()  # fmt: skip
                renames = {col: key for key, col in rows}
            except psycopg.errors.UndefinedTable:
                connection.rollback()  # not a table wintergrab wrote: its columns as they are
            with connection.cursor(name="wintergrab_read", row_factory=dict_row) as cursor:
                cursor.itersize = 2_000
                cursor.execute(sql.SQL("SELECT * FROM {}").format(table))
                for row in cursor:
                    yield {renames.get(k, k): v for k, v in row.items() if k != _ROWID and v is not None}
    except psycopg.Error as exc:
        raise ConfigurationError(f"cannot read {redact_url(url)}: {str(exc).strip()}") from None
    finally:
        connection.close()
