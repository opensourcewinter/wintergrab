"""MySQL and MariaDB: items as the rows of a table (``output = "mysql://crawler@db.example/shop?table=products"``).

Needs PyMySQL (``pip install "wintergrab[mysql]"``). The database is the URL's path, and the table is
``items`` unless the URL says ``?table=NAME``; ``mariadb://`` URLs work the same. The URL's other parameters
are the connection's: ``connect_timeout``, ``unix_socket``, ``ssl_ca``, ``ssl_cert``, ``ssl_key``,
``ssl_verify_cert``, ``ssl_verify_identity``. Any other is an error, not ignored.

The table is created on first use. Each new field becomes a column, typed from its first value: ``BOOLEAN``,
``BIGINT``, ``DOUBLE``, or ``LONGTEXT`` for text and for nested values (as JSON, given back as the objects
and lists they were). When a later value does not fit, the column widens to hold both: a ``BIGINT`` column
that gets a decimal becomes ``DOUBLE``; one that gets text or an object becomes ``LONGTEXT``. Nothing is cut
to fit.

With ``unique_key`` (``--unique-key url``), rows are upserted on that field: crawling again updates them.
The key is kept unique by a stored column beside it, ``_wg_key_<column>``: the SHA-256 of its value, with a
unique index (a long text column cannot have a unique index of its own). Without it, a fresh crawl empties
its table first, as a file output is replaced; a resumed crawl adds to it.

The exporter only writes to tables it created: it keeps their fields and columns in a
``_wintergrab_columns`` table beside them, and refuses a table of the same name that it did not create.
Keep the password out of the URL: ``MYSQL_PWD`` and the ``[client]`` section of ``~/.my.cnf`` are read, as
the ``mysql`` client reads them. Where the URL is shown or kept (logs, run records), its user and password
are left out.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from ..errors import ConfigurationError, ExportError
from ..redact import redact_url
from ..spider.exporters import Exporter, _json_default, _size, dumps, to_dict
from .common import column_name, kind_of, require

__all__ = ["MySQLExporter", "read_mysql"]

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_META = "_wintergrab_columns"
_ROWID = "_wg_rowid"
_KEY = "_wg_key_"  # + the unique key's column: the stored hash its unique index is on
#: Column types by kind: text and nested values (JSON) are both LONGTEXT; the metadata says which.
_TYPES = {"bool": "BOOLEAN", "int": "BIGINT", "float": "DOUBLE", "str": "LONGTEXT", "json": "LONGTEXT"}
_TEXT = ("str", "json")


def _yes(value: str) -> bool:
    if value.lower() in ("1", "true", "yes", "on"):
        return True
    if value.lower() in ("0", "false", "no", "off"):
        return False
    raise ValueError(value)


#: The URL's connection parameters, and how each is read (the others are refused).
_CONNECTION: dict[str, Any] = {
    "connect_timeout": int,
    "read_timeout": int,
    "write_timeout": int,
    "unix_socket": str,
    "ssl_ca": str,
    "ssl_cert": str,
    "ssl_key": str,
    "ssl_verify_cert": _yes,
    "ssl_verify_identity": _yes,
}
_OPTIONS = ("table",)  # the URL's own
_STATEMENT = 1_000_000  # bytes: rows go in statements of about this size


def _pymysql() -> Any:
    return require("pymysql", "mysql", "MySQL output")


def parse_target(url: str) -> tuple[dict[str, Any], str]:
    """``pymysql.connect`` arguments for ``url`` (the password from it, ``MYSQL_PWD`` or ``~/.my.cnf``) and
    the table."""
    parts = urlsplit(url)
    database = unquote(parts.path.lstrip("/"))
    if not database:
        raise ConfigurationError(f"{redact_url(url)}: name the database, as in mysql://host/DATABASE?table=NAME",
                                 key="output")  # fmt: skip
    if not _NAME.match(database):
        raise ConfigurationError(f"database {database!r}: use letters, digits and _", key="output")
    arguments: dict[str, Any] = {
        "host": parts.hostname or "localhost",
        "port": parts.port or 3306,
        "database": database,
        "charset": "utf8mb4",
        "autocommit": False,
        "connect_timeout": 10,
    }
    if parts.username:
        arguments["user"] = unquote(parts.username)
    if parts.password is not None:
        arguments["password"] = unquote(parts.password)
    elif os.environ.get("MYSQL_PWD"):
        arguments["password"] = os.environ["MYSQL_PWD"]
    options = Path("~/.my.cnf").expanduser()
    if options.is_file():
        arguments["read_default_file"] = str(options)  # [client]: what the URL does not say
    table = "items"
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        if name in _OPTIONS:
            table = value
        elif name in _CONNECTION:
            try:
                arguments[name] = _CONNECTION[name](value)
            except ValueError:
                raise ConfigurationError(f"{name}={value!r}: not a valid value", key="output") from None
        else:
            known = ", ".join(sorted([*_OPTIONS, *_CONNECTION]))
            raise ConfigurationError(f"unknown option {name!r} in the MySQL URL (known: {known})", key="output")
    if not _NAME.match(table) or table.startswith(("_wg_", "_wintergrab")):
        raise ConfigurationError(f"table {table!r}: use letters, digits and _", key="output")
    return arguments, table


def _connect(url: str) -> tuple[Any, str]:
    pymysql = _pymysql()
    arguments, table = parse_target(url)
    try:
        return pymysql.connect(**arguments), table
    except pymysql.MySQLError as exc:
        raise ConfigurationError(f"cannot connect to {redact_url(url)}: {_message(exc)}", key="output") from None


def _message(exc: BaseException) -> str:
    """The server's message (PyMySQL's errors are ``(code, message)``)."""
    args: tuple[Any, ...] = tuple(exc.args)
    return str(args[1]) if len(args) >= 2 else str(exc)


def _quote(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _wider(kind: str, incoming: str) -> str | None:
    """The kind a column must become to hold a value of ``incoming`` (``None``: it holds it already)."""
    if incoming == "null" or kind in _TEXT or kind == incoming or (kind == "float" and incoming == "int"):
        return None  # text holds anything (objects as JSON text); JSON too
    if kind == "int" and incoming == "float":
        return "float"
    return "json" if incoming == "json" else "str"


class MySQLExporter(Exporter):
    """Items as the rows of a MySQL or MariaDB table (see the module docs)."""

    supports_unique_key = True

    def __init__(self, url: str, *, append: bool, unique_key: str | None = None) -> None:
        super().__init__(Path(redact_url(url)), append=append)
        self.url = url
        self.unique_key = unique_key
        self.bytes_written = 0  # the items as JSON
        self._pymysql = _pymysql()
        self._conn, self.table_name = _connect(url)
        self._table = _quote(self.table_name)
        self._rows: list[dict[str, Any]] = []
        try:
            self._alias = self._upsert_alias()
            self._open(append)
        except self._pymysql.MySQLError as exc:
            self._conn.close()
            raise ExportError(f"{self.table_name}: {_message(exc)}") from None
        except Exception:
            self._conn.close()
            raise

    # -- the table ---------------------------------------------------------------------------- #
    def _execute(self, query: str, params: Any = None) -> Any:
        cursor = self._conn.cursor()
        cursor.execute(query, params)
        return cursor

    def _upsert_alias(self) -> bool:
        """Whether the server names the new row with an alias (MySQL 8.0.19 on) rather than ``VALUES()``
        (MariaDB, older MySQL)."""
        version = str(self._execute("SELECT VERSION()").fetchone()[0])
        if "mariadb" in version.lower():
            return False
        numbers = [int(n) for n in re.findall(r"\d+", version)[:3]]
        return numbers >= [8, 0, 19]

    def _open(self, append: bool) -> None:
        self._execute(
            f"CREATE TABLE IF NOT EXISTS {_quote(_META)} (tbl VARBINARY(256) NOT NULL, "
            "`key` VARBINARY(2048) NOT NULL, col VARCHAR(64) NOT NULL, type VARCHAR(16) NOT NULL, "
            "PRIMARY KEY (tbl, `key`)) CHARACTER SET utf8mb4"
        )
        rows = self._meta_rows()
        exists = self._execute(
            "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
            (self.table_name,),
        ).fetchone()[0]
        ours = any(key == "" for key, _, _ in rows)  # the table's own row: wintergrab created it
        if exists and not ours:
            raise ConfigurationError(
                f"{self.table_name} already exists and wintergrab did not create it; name another (?table=NAME)",
                key="output",
            )
        if not exists:
            self._execute(f"DELETE FROM {_quote(_META)} WHERE tbl = %s", (self.table_name.encode(),))
            self._execute(
                f"CREATE TABLE {self._table} ({_quote(_ROWID)} BIGINT AUTO_INCREMENT PRIMARY KEY) CHARACTER SET utf8mb4"
            )
            self._execute(f"INSERT INTO {_quote(_META)} (tbl, `key`, col, type) VALUES (%s, '', %s, 'table')",
                          (self.table_name.encode(), _ROWID))  # fmt: skip
            rows = []
        elif not append and not self.unique_key:
            self._execute(f"DELETE FROM {self._table}")  # a fresh crawl replaces its rows
        self._load_columns(rows)
        self._unique_index()
        self._conn.commit()

    def _meta_rows(self) -> list[tuple[str, str, str]]:
        rows = self._execute(f"SELECT `key`, col, type FROM {_quote(_META)} WHERE tbl = %s",
                             (self.table_name.encode(),)).fetchall()  # fmt: skip
        return [(bytes(key).decode(), col, kind) for key, col, kind in rows]

    def _load_columns(self, rows: list[tuple[str, str, str]] | None = None) -> None:
        rows = self._meta_rows() if rows is None else rows
        self._columns: dict[str, str] = {key: col for key, col, _ in rows if key}
        self._kinds: dict[str, str] = {col: kind for key, col, kind in rows if key}
        self._used = set(self._kinds) | {_ROWID}

    def _key_columns(self) -> list[str]:
        rows = self._execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s"
            " AND COLUMN_NAME LIKE %s",
            (self.table_name, _KEY.replace("_", "\\_") + "%"),
        ).fetchall()
        return [name for (name,) in rows]

    def _unique_index(self, *, drop_only: bool = False) -> None:
        """The stored hash of ``unique_key``'s column and its unique index (once the column exists); those of
        another key go."""
        column = self._columns.get(self.unique_key or "")
        wanted = f"{_KEY}{column}" if column and not drop_only else None
        present = self._key_columns()
        for name in present:
            if name != wanted:  # unique_key changed since the last run (or its column is about to widen)
                self._execute(f"ALTER TABLE {self._table} DROP COLUMN {_quote(name)}")
        if wanted is None or wanted in present:
            return
        try:
            self._execute(
                f"ALTER TABLE {self._table} ADD COLUMN {_quote(wanted)} BINARY(32) "
                f"AS (UNHEX(SHA2({_quote(str(column))}, 256))) STORED, ADD UNIQUE INDEX {_quote(wanted)} "
                f"({_quote(wanted)})"
            )
        except self._pymysql.err.IntegrityError:
            raise ConfigurationError(
                f"{self.table_name} already holds duplicate values of {self.unique_key!r}; it cannot be the unique key",
                key="unique_key",
            ) from None

    def _column(self, key: str, kind: str) -> str:
        """The column of field ``key``, created or widened to hold a value of ``kind``."""
        column = self._columns.get(key)
        if column is None:
            column = column_name(key, self._used)  # never _wg...: column_name drops leading underscores
            self._execute(f"ALTER TABLE {self._table} ADD COLUMN {_quote(column)} {_TYPES[kind]}")
            self._execute(f"INSERT INTO {_quote(_META)} (tbl, `key`, col, type) VALUES (%s, %s, %s, %s)",
                          (self.table_name.encode(), key.encode(), column, kind))  # fmt: skip
            self._columns[key] = column
            self._kinds[column] = kind
            self._used.add(column)
            if key == self.unique_key:
                self._unique_index()
            self._conn.commit()
            return column
        wider = _wider(self._kinds[column], kind)
        if wider is not None:
            keyed = key == self.unique_key
            if keyed:
                self._unique_index(drop_only=True)  # its hash follows the column's new text
            before = self._kinds[column]
            self._execute(f"ALTER TABLE {self._table} MODIFY COLUMN {_quote(column)} {_TYPES[wider]}")
            if before == "bool" and wider in _TEXT:  # 1 and 0 as the JSON true and false they were
                self._execute(
                    f"UPDATE {self._table} SET {_quote(column)} = CASE {_quote(column)} WHEN '1' THEN 'true' "
                    f"WHEN '0' THEN 'false' ELSE {_quote(column)} END WHERE {_quote(column)} IS NOT NULL"
                )
            self._execute(f"UPDATE {_quote(_META)} SET type = %s WHERE tbl = %s AND `key` = %s",
                          (wider, self.table_name.encode(), key.encode()))  # fmt: skip
            self._kinds[column] = wider
            if keyed:
                self._unique_index()
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
                kind = kind_of(value)
                if kind == "null" and key not in self._columns:
                    continue  # no column yet, and nothing to keep: its type waits for a value
                if len(key.encode()) > 2048:
                    raise ExportError(f"{self.table_name}: a field name longer than 2,048 bytes: {key[:40]}...")
                self._column(key, kind)
        except self._pymysql.MySQLError as exc:
            self._conn.rollback()
            self._load_columns()
            raise ExportError(f"{self.table_name}: {_message(exc)}") from None
        self._rows.append(record)
        self.count += 1
        self._maybe_flush()

    def _value(self, column: str, value: Any) -> Any:
        kind = self._kinds[column]
        if value is None:
            return None
        if kind == "json":
            return json.dumps(value, ensure_ascii=False, default=_json_default)
        if kind == "str":
            return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=_json_default)
        if kind == "float":
            return float(value)
        return value

    def flush(self) -> None:
        if not self._rows:
            self._conn.commit()
            return
        keyed = bool(self.unique_key and self._columns.get(self.unique_key))
        groups: dict[tuple[str, ...], list[list[Any]]] = {}
        for record in self._rows:
            fields = [k for k in record if k in self._columns]  # (a field only ever null has no column)
            columns = tuple(self._columns[k] for k in fields)
            groups.setdefault(columns, []).append([self._value(self._columns[k], record[k]) for k in fields])
        try:
            with self._conn.cursor() as cursor:
                for columns, values in groups.items():
                    head = f"INSERT INTO {self._table} ({', '.join(map(_quote, columns))}) VALUES "
                    tail = ""
                    if keyed and columns:
                        if self._alias:
                            updates = ", ".join(f"{_quote(c)} = _wg_new.{_quote(c)}" for c in columns)
                            tail = f" AS _wg_new ON DUPLICATE KEY UPDATE {updates}"
                        else:
                            tail = " ON DUPLICATE KEY UPDATE " + ", ".join(
                                f"{_quote(c)} = VALUES({_quote(c)})" for c in columns
                            )
                    placeholders = "(" + ", ".join(["%s"] * len(columns)) + ")"
                    chunk: list[str] = []
                    size = 0
                    for row in values:
                        text = cursor.mogrify(placeholders, row)  # escaped as execute() escapes
                        if chunk and size + len(text) > _STATEMENT:
                            cursor.execute(head + ", ".join(chunk) + tail)
                            chunk, size = [], 0
                        chunk.append(text)
                        size += len(text) + 2
                    cursor.execute(head + ", ".join(chunk) + tail)
            self._conn.commit()
        except self._pymysql.MySQLError as exc:
            self._conn.rollback()
            raise ExportError(f"{self.table_name}: {len(self._rows)} item(s) not written: {_message(exc)}") from None
        finally:
            self._rows.clear()

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self._conn.close()


def read_mysql(url: str) -> Iterator[dict[str, Any]]:
    """The rows of a table as records: a table wintergrab wrote gives its fields back by their names and
    types (nested values as the objects and lists they were)."""
    pymysql = _pymysql()
    connection, table = _connect(url)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(f"SELECT `key`, col, type FROM {_quote(_META)} WHERE tbl = %s", (table.encode(),))
            meta = [(bytes(key).decode(), col, kind) for key, col, kind in cursor.fetchall()]
        except pymysql.err.ProgrammingError:
            meta = []  # not a database wintergrab wrote to: the table's columns as they are
        ours = any(key == "" for key, _, _ in meta)
        names = {col: key for key, col, _ in meta if key}
        kinds = {col: kind for key, col, kind in meta if key}
        with connection.cursor(pymysql.cursors.SSDictCursor) as rows:
            rows.execute(f"SELECT * FROM {_quote(table)}" + (f" ORDER BY {_quote(_ROWID)}" if ours else ""))
            for row in rows:
                record: dict[str, Any] = {}
                for column, value in row.items():
                    if value is None or (ours and (column == _ROWID or column.startswith(_KEY))):
                        continue
                    kind = kinds.get(column)
                    if kind == "json":
                        value = json.loads(value)
                    elif kind == "bool":
                        value = bool(value)
                    record[names.get(column, column)] = value
                yield record
    except pymysql.MySQLError as exc:
        raise ConfigurationError(f"cannot read {redact_url(url)}: {_message(exc)}") from None
    finally:
        connection.close()
