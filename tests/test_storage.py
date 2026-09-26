"""Storage adapters: Parquet, Excel, DuckDB and database outputs, their readers, and the registries."""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from wintergrab.data.io import read_records, register_reader
from wintergrab.errors import ConfigurationError
from wintergrab.spider.exporters import Exporter, open_exporter, register_exporter

ITEMS: list[Any] = [
    {"url": "https://s.example/1", "price": 10, "tags": ["a", "b"], "ok": True, "code": 7},
    {"url": "https://s.example/2", "price": 12.5, "offer": {"amount": 9.99, "currency": "EUR"}, "ok": False, "code": "X7"},
    {"url": "https://s.example/3", "price": None, "note": "=HYPERLINK(\"http://evil.example\")", "big": 2**70},
    "a plain value",
]  # fmt: skip


def write(path: str | Path, items: list[Any], **options: Any) -> Exporter:
    exporter = open_exporter(path, **options)
    for item in items:
        exporter.write(item)
    exporter.close()
    return exporter


def test_parquet(tmp_path) -> None:
    pq = pytest.importorskip("pyarrow.parquet")
    path = tmp_path / "items.parquet"
    exporter = write(path, ITEMS)
    assert exporter.count == 4 and exporter.bytes_written > 100  # the items as JSON (max_output_bytes)
    types = {f.name: str(f.type) for f in pq.read_schema(path)}
    assert types["price"] == "double" and types["ok"] == "bool"  # 10 and 12.5: numbers
    assert types["code"] == "string" and types["big"] == "string"  # 7 and "X7"; past 64 bits: text
    rows = list(read_records(path))
    assert rows[0]["tags"] == ["a", "b"] and rows[1]["offer"] == {"amount": 9.99, "currency": "EUR"}  # as they were
    assert rows[0]["code"] == "7" and rows[2]["big"] == str(2**70) and rows[3]["value"] == "a plain value"
    assert not list(tmp_path.glob(".*spool*"))  # the spool is gone once the file is written

    # a crawl that stopped: its items wait in the spool, and the resumed crawl adds to them
    stopped = open_exporter(path, append=False)
    stopped.write(ITEMS[0])
    stopped.flush()  # (a checkpoint)
    stopped.spool.close()  # (then the process dies, and its files are closed)
    resumed = write(path, [ITEMS[1]], append=True)
    assert resumed.count == 1 and [r["url"] for r in read_records(path)] == [
        "https://s.example/1",
        "https://s.example/2",
    ]
    write(path, [ITEMS[2]], append=True)  # no spool left: the file's records are continued
    assert len(list(read_records(path))) == 3
    write(path, [])  # a fresh crawl with nothing: an empty file
    assert list(read_records(path)) == []


def test_xlsx(tmp_path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    path = tmp_path / "items.xlsx"
    items = [*ITEMS[:3], {"url": "https://s.example/4", "note": "bell\x07 " + "x" * 40_000}]
    write(path, items)
    sheet = openpyxl.load_workbook(path)["items"]
    header = [c.value for c in sheet[1]]
    note = sheet.cell(row=4, column=header.index("note") + 1)
    assert note.value.startswith("=HYPERLINK") and note.data_type == "s"  # text, never a formula
    rows = list(read_records(path))
    assert rows[0]["tags"] == ["a", "b"] and rows[1]["offer"] == {"amount": 9.99, "currency": "EUR"}
    assert rows[0]["price"] == 10 and rows[1]["price"] == 12.5 and rows[2]["big"] == str(2**70)
    assert rows[3]["note"].startswith("bell x") and len(rows[3]["note"]) == 32_767  # no control character; cut


def test_sqlite_reads_back(tmp_path) -> None:
    import sqlite3

    path = tmp_path / "items.sqlite"
    write(path, ITEMS, unique_key="url")  # (an integer past 64 bits once stopped it: OverflowError)
    rows = list(read_records(path))
    assert rows[0]["tags"] == ["a", "b"] and rows[1]["offer"] == {"amount": 9.99, "currency": "EUR"}  # as they were
    assert rows[0]["ok"] is True and rows[1]["ok"] is False and rows[2]["big"] == str(2**70)
    assert (rows[0]["code"], rows[1]["code"], rows[3]["value"]) == (7, "X7", "a plain value")  # kept as they are
    write(path, [{"url": "https://s.example/9", "ok": "maybe", "tags": "none"}], unique_key="url", append=True)
    rows = list(read_records(path))
    assert rows[-1]["ok"] == "maybe" and rows[0]["ok"] == 1 and rows[0]["tags"] == '["a", "b"]'  # mixed: as kept
    with sqlite3.connect(tmp_path / "other.db") as connection:
        connection.execute("CREATE TABLE products (name TEXT)")
    with pytest.raises(ConfigurationError, match="no 'items' table"):
        list(read_records(tmp_path / "other.db"))


def test_duckdb(tmp_path) -> None:
    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "items.duckdb"
    exporter = write(
        path, [*ITEMS, {"url": "https://s.example/5", "Price (USD)": 3, "Code": "c", "page": "x" * 17_000_000}]
    )
    assert exporter.count == 5 and exporter.bytes_written > 17_000_000  # the items as JSON (max_output_bytes)
    with duckdb.connect(str(path), read_only=True) as connection:
        types = dict(connection.execute("SELECT column_name, data_type FROM information_schema.columns"
                                        " WHERE table_name = 'items'").fetchall())  # fmt: skip
        assert (types["price"], types["ok"], types["tags"], types["code"], types["big"]) == (
            "DOUBLE", "BOOLEAN", "JSON", "VARCHAR", "VARCHAR")  # 10 and 12.5: numbers; 7 and "X7": text  # fmt: skip
        assert (types["price_usd"], types["code_2"]) == ("BIGINT", "VARCHAR")  # "Code" beside "code"
        assert connection.execute("SELECT tags->>'$[1]', offer->>'$.currency' FROM items ORDER BY _wg_rowid"
                                  " LIMIT 2").fetchall() == [("b", None), (None, "EUR")]  # (JSON, for SQL) # fmt: skip
    rows = list(read_records(path))
    assert rows[0]["tags"] == ["a", "b"] and rows[1]["offer"] == {"amount": 9.99, "currency": "EUR"}  # as they were
    assert (rows[0]["code"], rows[2]["big"], rows[3]["value"]) == ("7", str(2**70), "a plain value")
    assert (rows[4]["Price (USD)"], rows[4]["Code"], len(rows[4]["page"])) == (3, "c", 17_000_000)  # own names
    assert not list(tmp_path.glob(".*"))  # the spool is gone once the table is written

    # a crawl that stopped: its items wait in the spool, and the resumed crawl adds to them
    stopped = open_exporter(path, append=False)
    stopped.write(ITEMS[0])
    stopped.flush()  # (a checkpoint)
    stopped.spool.close()  # (then the process dies: the table there is still the last run's)
    assert len(list(read_records(path))) == 5
    resumed = write(path, [ITEMS[1]], append=True)
    assert resumed.count == 1 and [r["url"] for r in read_records(path)] == ["https://s.example/1",
                                                                              "https://s.example/2"]  # fmt: skip
    write(path, [ITEMS[2]], append=True)  # no spool left: the table's rows are continued
    assert len(list(read_records(path))) == 3
    write(path, [])  # a fresh crawl with nothing: an empty table
    assert list(read_records(path)) == []
    empty = tmp_path / "new.duckdb"
    empty.touch()  # (a file made for it, with nothing in it yet)
    write(empty, [{"a": 1}])
    assert list(read_records(empty)) == [{"a": 1}]


def test_duckdb_upserts_on_unique_key_and_leaves_the_rest_of_the_database(tmp_path) -> None:
    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "shop.duckdb"
    write(path, [{"url": "/a", "Name": "A", "name": "a", "price": 10, "stock": 3}, {"url": "/b", "price": 5}],
          unique_key="url")  # fmt: skip
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE notes AS SELECT 'mine' AS note")
        connection.execute("CREATE VIEW cheap AS SELECT url FROM items WHERE price BETWEEN 4 AND 8")
    write(path, [{"url": "/a", "price": 12.5, "stock": None}, {"url": "/c", "NAME": "C"}, {"price": 1}, {"price": 2}],
          unique_key="url")  # fmt: skip
    rows = list(read_records(path))
    assert rows == [
        {"url": "/a", "Name": "A", "name": "a", "price": 12.5},  # updated in its place: the fields the item has
        {"url": "/b", "price": 5.0},
        {"url": "/c", "NAME": "C"},
        {"price": 1.0},  # without the key: rows of their own
        {"price": 2.0},
    ]
    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute("SELECT * FROM notes").fetchall() == [("mine",)]  # the rest is left as it is
        assert connection.execute("SELECT * FROM cheap ORDER BY url").fetchall() == [("/b",)]
        columns = connection.execute("SELECT key, col FROM _wintergrab_columns ORDER BY position").fetchall()
    assert columns[:3] == [("url", "url"), ("Name", "name"), ("name", "name_2")] and ("NAME", "name_3") in columns
    write(path, [{"name": "x", "Name": "y"}])  # a fresh crawl replaces the table; the columns keep their names
    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute("SELECT name, name_2 FROM items").fetchall() == [("y", "x")]


def test_duckdb_refuses_what_it_did_not_create(tmp_path) -> None:
    duckdb = pytest.importorskip("duckdb")
    theirs = tmp_path / "theirs.duckdb"
    with duckdb.connect(str(theirs)) as connection:
        connection.execute("CREATE TABLE items AS SELECT 1 AS a, '{\"b\": 2}'::JSON AS b")
    with pytest.raises(ConfigurationError, match="already has an 'items' table that wintergrab did not create"):
        open_exporter(theirs)
    assert list(read_records(theirs)) == [{"a": 1, "b": {"b": 2}}]  # read as it is
    viewed = tmp_path / "viewed.duckdb"
    with duckdb.connect(str(viewed)) as connection:
        connection.execute("CREATE TABLE products AS SELECT 'p' AS name")
        connection.execute("CREATE VIEW items AS SELECT * FROM products")
    with pytest.raises(ConfigurationError, match="already has an 'items' view that wintergrab did not create"):
        open_exporter(viewed)
    other = tmp_path / "other.duckdb"
    with duckdb.connect(str(other)) as connection:
        connection.execute("CREATE TABLE products AS SELECT 'p' AS name")
    assert list(read_records(other)) == [{"name": "p"}]  # its only table
    with duckdb.connect(str(other)) as connection:
        connection.execute("CREATE TABLE sellers AS SELECT 's' AS name")
    with pytest.raises(ConfigurationError, match="no 'items' table to read \\(its tables: products, sellers\\)"):
        list(read_records(other))
    notes = tmp_path / "notes.duckdb"
    with duckdb.connect(str(notes)) as connection:
        connection.execute("CREATE TABLE notes AS SELECT 'mine' AS note")
    write(notes, [{"a": 1}], append=True)  # a database without an items table gets one, with the items alone
    assert list(read_records(notes)) == [{"a": 1}]  # (not the notes: what is there is not continued)
    with duckdb.connect(str(notes), read_only=True) as connection:
        assert connection.execute("SELECT * FROM notes").fetchall() == [("mine",)]
    (tmp_path / "text.duckdb").write_text("not a database\n" * 100)
    with pytest.raises(ConfigurationError, match=r"cannot write items to .*not a valid DuckDB database"):
        open_exporter(tmp_path / "text.duckdb")


def test_duckdb_held_by_another_program(tmp_path, monkeypatch) -> None:
    import subprocess

    pytest.importorskip("duckdb")
    from wintergrab.errors import ExportError
    from wintergrab.storage import duckdb as storage

    path = tmp_path / "items.duckdb"
    write(path, [{"n": 1}])

    def hold(seconds: float) -> subprocess.Popen[str]:
        """Another process with the database open for writing (DuckDB lets a file have one writer)."""
        code = f"import duckdb, sys, time\nc = duckdb.connect({str(path)!r})\nprint('open', flush=True)\ntime.sleep({seconds})"
        holder = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        assert holder.stdout is not None and holder.stdout.readline().strip() == "open"
        return holder

    holder = hold(0.5)  # (a reader's moment: waited for)
    write(path, [{"n": 2}], append=True)
    holder.wait(10)
    assert [r["n"] for r in read_records(path)] == [1, 2]
    monkeypatch.setattr(storage, "_LOCK_WAIT", 0.5)
    exporter = open_exporter(path, unique_key="n")  # (a fresh crawl, upserting)
    exporter.write({"n": 2, "seen": True})
    exporter.write({"n": 3})
    holder = hold(60)  # (held until the crawl ends, and after)
    try:
        with pytest.raises(
            ConfigurationError,
            match=r"(?s)cannot write items to .*: IO Error: .*(Could not set lock|File is already open)",
        ):
            open_exporter(path, append=True)  # (a crawl starting now is told at once)
        with pytest.raises(ExportError, match=r"could not write .*items are kept in .*\.items\.duckdb\.spool\.jsonl"):
            exporter.close()
    finally:
        holder.kill()
        holder.wait(10)
    assert [r["n"] for r in read_records(path)] == [1, 2]  # (the table before)
    open_exporter(path, append=True, unique_key="n").close()  # what docs/storage.md says to run then
    assert list(read_records(path)) == [{"n": 1}, {"n": 2, "seen": True}, {"n": 3}]
    assert not list(tmp_path.glob(".*"))


def test_csv_widens_for_keys_later_items_bring(tmp_path) -> None:
    path = tmp_path / "items.csv"
    exporter = write(path, ITEMS)  # (its columns were the first item's: the others' keys were left out)
    assert exporter.bytes_written == path.stat().st_size and not list(tmp_path.glob("*.widening"))
    rows = list(read_records(path))
    assert list(rows[0]) == ["url", "price", "tags", "ok", "code", "offer", "note", "big", "value"]
    assert rows[0]["offer"] == "" and rows[1]["offer"] == '{"amount": 9.99, "currency": "EUR"}'
    assert (rows[2]["note"], rows[2]["big"], rows[3]["value"]) == ('=HYPERLINK("http://evil.example")', str(2**70),
                                                                   "a plain value")  # fmt: skip
    # a resumed output, with a byte-order mark as a spreadsheet writes it: its columns continued, then widened
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    write(path, [{"url": "https://s.example/5", "price": 3}, {"url": "https://s.example/6", "rank": 1}], append=True)
    rows = list(read_records(path))
    first, *_, last = rows[0]  # (the first column's name without the mark)
    assert len(rows) == 6 and (first, last, len(rows[0])) == ("url", "rank", 10)
    assert (rows[4]["price"], rows[4]["rank"], rows[5]["rank"], rows[0]["rank"]) == ("3", "", "1", "")


def test_crawls_write_them_and_data_commands_read_them(site, tmp_path, capsys) -> None:
    pytest.importorskip("pyarrow")
    pytest.importorskip("openpyxl")
    pytest.importorskip("duckdb")
    from wintergrab.cli import main

    for name in ("books.parquet", "books.xlsx", "books.duckdb"):
        out = str(tmp_path / name)
        assert main(["-q", "crawl", site.url + "/books/", "--allow", "/books/", "--max-pages", "6", "--auto", "-o", out,
                     "--no-progress"]) == 0  # fmt: skip
        assert len(list(read_records(out))) >= 3
        capsys.readouterr()
        assert main(["data", "quality", out]) == 0 and "record(s), quality score" in capsys.readouterr().out


def test_missing_libraries_say_what_to_install(tmp_path, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "pyarrow", None)  # (import pyarrow fails)
    with pytest.raises(ConfigurationError, match=r'pip install "wintergrab\[parquet\]"'):
        open_exporter(tmp_path / "items.parquet")
    monkeypatch.setitem(sys.modules, "duckdb", None)
    with pytest.raises(ConfigurationError, match=r'DuckDB output needs duckdb: pip install "wintergrab\[duckdb\]"'):
        open_exporter(tmp_path / "items.duckdb")
    with pytest.raises(ValueError, match="no output for ftp://"):
        open_exporter("ftp://files.example/items")
    with pytest.raises(ValueError, match=r"Unsupported output format '\.txt'"):
        open_exporter(tmp_path / "items.txt")


class _Memory(Exporter):
    kept: dict[str, list[Any]] = {}

    def __init__(self, path: Any, *, append: bool) -> None:
        super().__init__(Path("memory"), append=append)
        self.name = str(path)
        self.kept[self.name] = []

    def write(self, item: Any) -> None:
        self.kept[self.name].append(item)
        self.count += 1

    def close(self) -> None:
        pass


def test_registering_outputs_and_readers(tmp_path) -> None:
    register_exporter("memory", _Memory)
    register_exporter(".mem", _Memory)
    write("memory://bucket", [{"a": 1}])
    write(tmp_path / "x.mem", [{"b": 2}])
    assert _Memory.kept["memory://bucket"] == [{"a": 1}] and _Memory.kept[str(tmp_path / "x.mem")] == [{"b": 2}]
    register_reader("memory", lambda source: iter(_Memory.kept[source]))
    assert list(read_records("memory://bucket")) == [{"a": 1}]
    with pytest.raises(ValueError, match="by extension"):
        register_exporter("not a key", _Memory)


# -- PostgreSQL: WINTERGRAB_TEST_POSTGRES=postgresql://user@host:port/db names a server to test against -- #
POSTGRES = os.environ.get("WINTERGRAB_TEST_POSTGRES")
needs_postgres = pytest.mark.skipif(not POSTGRES, reason="set WINTERGRAB_TEST_POSTGRES to a PostgreSQL URL")


@pytest.fixture
def table():
    psycopg = pytest.importorskip("psycopg")
    name = f"wg_test_{uuid.uuid4().hex[:8]}"
    yield f"{POSTGRES}?table={name}", name
    with psycopg.connect(str(POSTGRES), autocommit=True) as connection:
        connection.execute(f"DROP TABLE IF EXISTS {name}")
        connection.execute("DELETE FROM _wintergrab_columns WHERE tbl = %s", (name,))


def _columns(name: str) -> dict[str, str]:
    import psycopg

    with psycopg.connect(str(POSTGRES)) as connection:
        rows = connection.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = %s",
                                  (name,)).fetchall()  # fmt: skip
    return dict(rows)


@needs_postgres
def test_postgres(table) -> None:
    url, name = table
    write(url, ITEMS, unique_key="url")
    columns = _columns(name)
    assert columns["price"] == "double precision" and columns["ok"] == "boolean"  # 10, then 12.5: widened
    assert columns["code"] == "text" and columns["offer"] == "jsonb" and columns["tags"] == "jsonb"
    rows = list(read_records(url))
    assert len(rows) == 4 and rows[1]["offer"] == {"amount": 9.99, "currency": "EUR"} and rows[0]["code"] == "7"

    write(url, [{"url": "https://s.example/1", "price": 8.0, "Price (USD)": 8.5}], unique_key="url", append=True)
    rows = {r.get("url"): r for r in read_records(url)}
    assert len(rows) == 4 and rows["https://s.example/1"]["price"] == 8.0  # upserted, not added
    assert rows["https://s.example/1"]["Price (USD)"] == 8.5  # a field's own name, whatever its column's

    write(url, [{"url": "https://s.example/9", "extra": {"deep": [1, 2]}}])  # a fresh crawl, no unique key
    assert [r["url"] for r in read_records(url)] == ["https://s.example/9"]  # it replaced the rows
    write(url, [{"url": "https://s.example/10", "extra": "now text"}], append=True)  # an object column takes it
    assert [r["extra"] for r in read_records(url)] == [{"deep": [1, 2]}, "now text"]
    write(url, [{"url": "https://s.example/10"}], append=True)  # without a key, the earlier run's index is gone
    assert len(list(read_records(url))) == 3


@needs_postgres
def test_postgres_refuses_what_it_did_not_create(table) -> None:
    import psycopg

    url, name = table
    with psycopg.connect(str(POSTGRES), autocommit=True) as connection:
        connection.execute(f"CREATE TABLE {name} (id int)")
    with pytest.raises(ConfigurationError, match="wintergrab did not create it"):
        open_exporter(url)
    assert list(read_records(url)) == []  # (it reads any table)


def test_postgres_errors_keep_passwords_out() -> None:
    pytest.importorskip("psycopg")
    with pytest.raises(ConfigurationError) as error:
        open_exporter("postgresql://crawler:hunter2@127.0.0.1:1/shop?connect_timeout=2")
    assert "hunter2" not in str(error.value) and "postgresql://***@127.0.0.1:1/shop" in str(error.value)
    with pytest.raises(ConfigurationError, match="letters, digits"):
        open_exporter("postgresql://crawler@127.0.0.1:1/shop?table=items;drop")


def test_output_urls_keep_their_password_out(site, tmp_path, capsys) -> None:
    from wintergrab import Spider
    from wintergrab.cli import main
    from wintergrab.runs import RunRegistry

    class Books(Spider):
        log_level = None
        progress = False

        def parse(self, response: Any) -> Any:
            yield {"url": response.url}

    register_exporter("memory", _Memory)
    workspace = tmp_path / "ws"
    Books(start_urls=[site.url + "/books/"], output="memory://crawler:hunter2@db.example/shop",
          run_registry=str(workspace), max_pages=2).run()  # fmt: skip
    run = RunRegistry(workspace).get("last")
    assert run.output == "memory://***@db.example/shop"
    assert "hunter2" not in (run.directory / "run.json").read_text(encoding="utf-8")
    assert _Memory.kept["memory://crawler:hunter2@db.example/shop"]  # (the exporter itself gets the URL)
    assert main(["-q", "crawl", site.url + "/books/", "--max-pages", "1", "-o", "memory://crawler:hunter2@db/x",
                 "--no-progress"]) == 0  # fmt: skip
    assert "hunter2" not in capsys.readouterr().err


# -- MongoDB: WINTERGRAB_TEST_MONGODB=mongodb://host:port/db names a server to test against ------------- #
MONGODB = os.environ.get("WINTERGRAB_TEST_MONGODB")
needs_mongodb = pytest.mark.skipif(not MONGODB, reason="set WINTERGRAB_TEST_MONGODB to a MongoDB URL")


def _with(url: str, **params: str) -> str:
    return url + ("&" if "?" in url else "?") + "&".join(f"{k}={v}" for k, v in params.items())


@pytest.fixture
def collection():
    pymongo = pytest.importorskip("pymongo")
    name = f"wg_test_{uuid.uuid4().hex[:8]}"
    client = pymongo.MongoClient(str(MONGODB), serverSelectionTimeoutMS=5000)
    database = client.get_default_database()
    yield _with(str(MONGODB), collection=name), database[name]
    database.drop_collection(name)
    database["_wintergrab_collections"].delete_one({"_id": name})
    client.close()


@needs_mongodb
def test_mongodb(collection, site, capsys) -> None:
    url, documents = collection
    write(url, ITEMS, unique_key="url")
    rows = list(read_records(url))
    assert len(rows) == 4 and rows[0]["tags"] == ["a", "b"] and rows[1]["offer"] == {"amount": 9.99, "currency": "EUR"}
    assert (rows[0]["code"], rows[1]["code"]) == (7, "X7")  # documents: each value keeps its own type
    assert rows[2]["big"] == str(2**70) and rows[3] == {"value": "a plain value"}  # past 64 bits: text
    assert "price" not in rows[2]  # a null is no value (as a table's NULL reads)

    write(url, [{"url": "https://s.example/1", "price": 8.0}], unique_key="url", append=True)
    rows = list(read_records(url))
    assert len(rows) == 4 and rows[0] == {"url": "https://s.example/1", "price": 8.0}  # replaced, in its place
    assert [i["name"] for i in documents.list_indexes() if i["name"].startswith("wg_unique_")]

    write(url, [{"url": "https://s.example/9", "_id": "a1", "$ref": "x", "a.b": 1}])  # a fresh crawl, no key
    assert list(read_records(url)) == [{"url": "https://s.example/9", "_id": "a1", "$ref": "x", "a.b": 1}]
    stored = documents.find_one()
    assert stored["_id_"] == "a1" and stored["\uff04ref"] == "x"  # not MongoDB's _id; $ not an operator
    assert not [i for i in documents.list_indexes() if i["name"].startswith("wg_unique_")]  # no key, no index
    write(url, [{"$ref": "y", "url": "https://s.example/10", "x": {"$id": 1}}, {"url": None}, {"url": None}],
          unique_key="url", append=True)  # fmt: skip
    write(url, [{"$ref": "z", "url": "https://s.example/10", "x": {"$id": 2}}], unique_key="url", append=True)
    rows = list(read_records(url))
    assert len(rows) == 4 and rows[1] == {"$ref": "z", "url": "https://s.example/10", "x": {"$id": 2}}  # replaced
    assert [r.get("url") for r in rows] == ["https://s.example/9", "https://s.example/10", None, None]  # in order

    from wintergrab.cli import main

    assert main(["-q", "crawl", site.url + "/books/", "--allow", "/books/", "--max-pages", "4", "--auto", "-o",
                 url, "--unique-key", "url", "--no-progress"]) == 0  # fmt: skip
    capsys.readouterr()
    assert main(["data", "quality", url]) == 0 and "record(s), quality score" in capsys.readouterr().out


@needs_mongodb
def test_mongodb_refuses_what_it_did_not_create(collection) -> None:
    import datetime

    from bson import ObjectId

    url, documents = collection
    documents.insert_many([{"url": "https://s.example/1", "seen": datetime.datetime(2026, 9, 1, 12, 0)},
                           {"url": "https://s.example/1", "ref": ObjectId("65f0a1b2c3d4e5f6a7b8c9d0")}])  # fmt: skip
    with pytest.raises(ConfigurationError, match="wintergrab did not create it"):
        open_exporter(url)
    assert list(read_records(url)) == [  # (it reads any collection, its values as JSON has them)
        {"url": "https://s.example/1", "seen": "2026-09-01T12:00:00"},
        {"url": "https://s.example/1", "ref": "65f0a1b2c3d4e5f6a7b8c9d0"},
    ]
    documents.database["_wintergrab_collections"].insert_one({"_id": documents.name})  # (as if it had)
    with pytest.raises(ConfigurationError, match="already holds duplicate values of 'url'"):
        open_exporter(url, unique_key="url")


def test_mongodb_urls() -> None:
    pytest.importorskip("pymongo")
    with pytest.raises(ConfigurationError) as error:
        open_exporter("mongodb://crawler:hunter2@127.0.0.1:1/shop?serverSelectionTimeoutMS=500")
    assert "hunter2" not in str(error.value) and "mongodb://***@127.0.0.1:1/shop" in str(error.value)
    with pytest.raises(ConfigurationError, match="Unknown option: colection"):
        open_exporter("mongodb://127.0.0.1:1/shop?colection=items")  # a misspelt option is not ignored
    with pytest.raises(ConfigurationError, match="name the database"):
        open_exporter("mongodb://127.0.0.1:1/?collection=items")
    with pytest.raises(ConfigurationError, match="letters, digits"):
        open_exporter("mongodb://127.0.0.1:1/shop?collection=system.users")
    for key in ("$where", "offer.url"):
        with pytest.raises(ConfigurationError, match="a top-level field's name"):
            open_exporter("mongodb://127.0.0.1:1/shop", unique_key=key)


def test_mongodb_password_from_the_environment(monkeypatch) -> None:
    pymongo = pytest.importorskip("pymongo")
    from wintergrab.storage import mongodb

    seen: dict[str, Any] = {}

    class Client:  # (no server: what the client is given is what matters)
        def __init__(self, url: str, **options: Any) -> None:
            seen.update(options, url=url)

        def __getitem__(self, name: str) -> Any:
            return type("Database", (), {"command": lambda self, *args: {"ok": 1}})()

    monkeypatch.setattr(pymongo, "MongoClient", Client)
    monkeypatch.setenv("WINTERGRAB_MONGODB_PASSWORD", "from-the-environment")
    mongodb._connect("mongodb://crawler@db.example/shop?collection=products")
    assert seen["password"] == "from-the-environment" and "from-the-environment" not in seen["url"]
    assert seen["serverSelectionTimeoutMS"] == 10_000
    seen.clear()
    mongodb._connect("mongodb://crawler:given@db.example/shop?serverSelectionTimeoutMS=500")
    assert "password" not in seen and "serverSelectionTimeoutMS" not in seen  # the URL's own stand


# -- MySQL and MariaDB: WINTERGRAB_TEST_MYSQL=mysql://user:password@host:port/db names a server to test against #
MYSQL = os.environ.get("WINTERGRAB_TEST_MYSQL")
needs_mysql = pytest.mark.skipif(not MYSQL, reason="set WINTERGRAB_TEST_MYSQL to a MySQL or MariaDB URL")


def _mysql() -> Any:
    import pymysql

    from wintergrab.storage.mysql import parse_target

    arguments, _ = parse_target(str(MYSQL))
    return pymysql.connect(**{**arguments, "autocommit": True})


@pytest.fixture
def mysql_table():
    pytest.importorskip("pymysql")
    name = f"wg_test_{uuid.uuid4().hex[:8]}"
    yield _with(str(MYSQL), table=name), name
    with _mysql() as connection, connection.cursor() as cursor:
        cursor.execute(f"DROP TABLE IF EXISTS {name}")
        cursor.execute("DELETE FROM _wintergrab_columns WHERE tbl = %s", (name.encode(),))


def _mysql_columns(name: str) -> dict[str, str]:
    with _mysql() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = "
                       "DATABASE() AND TABLE_NAME = %s", (name,))  # fmt: skip
        return {column: kind.lower() for column, kind in cursor.fetchall()}


@needs_mysql
def test_mysql(mysql_table, site, capsys) -> None:
    url, name = mysql_table
    write(url, ITEMS, unique_key="url")
    columns = _mysql_columns(name)
    assert columns["price"] == "double" and columns["ok"] == "tinyint"  # 10, then 12.5: widened; BOOLEAN
    assert columns["code"] == "longtext" and columns["offer"] == "longtext" and columns["tags"] == "longtext"
    assert columns["_wg_key_url"] == "binary"  # the key's hash, uniquely indexed
    rows = list(read_records(url))
    assert len(rows) == 4 and rows[1]["offer"] == {"amount": 9.99, "currency": "EUR"} and rows[0]["tags"] == ["a", "b"]
    assert rows[0]["code"] == "7" and rows[2]["big"] == str(2**70) and rows[0]["ok"] is True  # as PostgreSQL has them

    write(url, [{"url": "https://s.example/1", "price": 8.0, "Price (USD)": 8.5}], unique_key="url", append=True)
    rows = {r.get("url"): r for r in read_records(url)}
    assert len(rows) == 4 and rows["https://s.example/1"]["price"] == 8.0  # upserted, not added
    assert rows["https://s.example/1"]["Price (USD)"] == 8.5 and rows["https://s.example/1"]["tags"] == ["a", "b"]

    write(url, [{"url": "https://s.example/9", "extra": {"deep": [1, 2]}, "flag": True, "none": None}])  # fresh
    assert [r["url"] for r in read_records(url)] == ["https://s.example/9"]  # it replaced the rows
    assert "_wg_key_url" not in _mysql_columns(name) and "none" not in _mysql_columns(name)  # no key; no value yet
    write(url, [{"url": "https://s.example/10", "extra": "now text", "flag": "maybe", "none": 3}], append=True)
    rows = list(read_records(url))
    assert [r["extra"] for r in rows] == [{"deep": [1, 2]}, "now text"]  # an object column takes text
    assert [r["flag"] for r in rows] == ["true", "maybe"]  # a boolean column that gets text: true is "true"
    assert rows[1]["none"] == 3 and _mysql_columns(name)["none"] == "bigint"  # typed from its first value

    write(url, [{"url": "https://s.example/10", "extra": "again"}, {"url": None}, {"url": None}], unique_key="url",
          append=True)  # fmt: skip
    assert len(list(read_records(url))) == 4 and list(read_records(url))[1]["extra"] == "again"

    from wintergrab.cli import main

    assert main(["-q", "crawl", site.url + "/books/", "--allow", "/books/", "--max-pages", "4", "--auto", "-o",
                 url, "--unique-key", "url", "--no-progress"]) == 0  # fmt: skip
    capsys.readouterr()
    assert main(["data", "quality", url]) == 0 and "record(s), quality score" in capsys.readouterr().out


@needs_mysql
def test_mysql_a_key_whose_column_widens(mysql_table) -> None:
    url, name = mysql_table
    write(url, [{"sku": 1, "n": 1}, {"sku": 2, "n": 2}], unique_key="sku")
    assert _mysql_columns(name)["sku"] == "bigint"
    write(url, [{"sku": "A-3", "n": 3}, {"sku": 2, "n": 22}], unique_key="sku", append=True)  # text: widened
    assert _mysql_columns(name)["sku"] == "longtext" and _mysql_columns(name)["_wg_key_sku"] == "binary"
    assert [(r["sku"], r["n"]) for r in read_records(url)] == [("1", 1), ("2", 22), ("A-3", 3)]  # still upserted


@needs_mysql
def test_mysql_refuses_what_it_did_not_create(mysql_table) -> None:
    url, name = mysql_table
    with _mysql() as connection, connection.cursor() as cursor:
        cursor.execute(f"CREATE TABLE {name} (id INT, url VARCHAR(100))")
        cursor.execute(f"INSERT INTO {name} VALUES (1, 'https://s.example/1'), (2, 'https://s.example/1')")
    with pytest.raises(ConfigurationError, match="wintergrab did not create it"):
        open_exporter(url)
    assert list(read_records(url)) == [{"id": 1, "url": "https://s.example/1"}, {"id": 2, "url": "https://s.example/1"}]
    write(url.replace(name, name + "_x"), [{"url": "a"}, {"url": "a"}])  # a key written twice, without the key
    try:
        with pytest.raises(ConfigurationError, match="already holds duplicate values of 'url'"):
            open_exporter(url.replace(name, name + "_x"), unique_key="url")
    finally:
        with _mysql() as connection, connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS {name}_x")
            cursor.execute("DELETE FROM _wintergrab_columns WHERE tbl = %s", ((name + "_x").encode(),))


def test_mysql_urls(monkeypatch, tmp_path) -> None:
    pytest.importorskip("pymysql")
    from wintergrab.storage.mysql import parse_target

    with pytest.raises(ConfigurationError) as error:
        open_exporter("mysql://crawler:hunter2@127.0.0.1:1/shop?connect_timeout=2")
    assert "hunter2" not in str(error.value) and "mysql://***@127.0.0.1:1/shop" in str(error.value)
    with pytest.raises(ConfigurationError, match="unknown option 'sslmode'"):
        open_exporter("mysql://127.0.0.1:1/shop?sslmode=require")  # a PostgreSQL option: not ignored
    with pytest.raises(ConfigurationError, match="name the database"):
        open_exporter("mariadb://127.0.0.1:1/?table=items")
    with pytest.raises(ConfigurationError, match="letters, digits"):
        open_exporter("mysql://127.0.0.1:1/shop?table=items;drop")
    monkeypatch.setenv("MYSQL_PWD", "from-the-environment")
    arguments, table = parse_target("mysql://crawler@db.example/shop?ssl_ca=/etc/ca.pem&ssl_verify_cert=true")
    assert (arguments["password"], arguments["ssl_ca"], arguments["ssl_verify_cert"], table) == (
        "from-the-environment",
        "/etc/ca.pem",
        True,
        "items",
    )
    with pytest.raises(ConfigurationError, match="not a valid value"):
        parse_target("mysql://db.example/shop?ssl_verify_cert=perhaps")
    monkeypatch.delenv("MYSQL_PWD")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # (the home on Windows)
    (tmp_path / ".my.cnf").write_text("[client]\npassword = from-the-file\n", encoding="utf-8")
    arguments, _ = parse_target("mysql://crawler@db.example/shop")
    assert "password" not in arguments and arguments["read_default_file"] == str(tmp_path / ".my.cnf")


# -- S3: against moto's S3 server, run here ------------------------------------------------------------- #
@pytest.fixture
def s3(monkeypatch, tmp_path):
    pytest.importorskip("s3fs")
    server_module = pytest.importorskip("moto.server")
    server = server_module.ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    for name in ("AWS_PROFILE", "AWS_ENDPOINT_URL", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("WINTERGRAB_UPLOADS", str(tmp_path / "uploads"))
    endpoint = f"http://{host}:{port}"
    import urllib.request

    import s3fs

    urllib.request.urlopen(urllib.request.Request(f"{endpoint}/moto-api/reset", method="POST"))  # (state is shared)
    fs = s3fs.S3FileSystem(client_kwargs={"endpoint_url": endpoint})
    fs.mkdir("wg-bucket")
    yield endpoint, fs
    server.stop()


def test_s3(s3, site, tmp_path, capsys) -> None:
    endpoint, fs = s3
    url = f"s3://wg-bucket/crawls/items.jsonl?endpoint_url={endpoint}"
    exporter = write(url, ITEMS[:3])
    assert fs.exists("wg-bucket/crawls/items.jsonl") and exporter.count == 3 and exporter.bytes_written > 100
    rows = list(read_records(url))
    assert len(rows) == 3 and rows[1]["offer"] == {"amount": 9.99, "currency": "EUR"}  # as JSON Lines hold them
    assert not list((tmp_path / "uploads").rglob("*.jsonl"))  # uploaded: nothing left behind

    write(url, [{"url": "https://s.example/5"}], append=True)  # a resumed crawl, its local file gone: continued
    assert [r["url"] for r in read_records(url)][-2:] == ["https://s.example/3", "https://s.example/5"]
    stopped = open_exporter(url, append=False)  # a crawl that stops before it ends...
    stopped.write({"url": "https://s.example/6"})
    stopped.flush()
    resumed = write(url, [{"url": "https://s.example/7"}], append=True)  # ...continued by the next
    assert [r["url"] for r in read_records(url)] == ["https://s.example/6", "https://s.example/7"]
    assert resumed.count == 1

    if __import__("importlib").util.find_spec("pyarrow"):
        parquet = f"s3://wg-bucket/crawls/items.parquet?endpoint_url={endpoint}"
        write(parquet, ITEMS[:3])
        assert [r.get("price") for r in read_records(parquet)] == [10.0, 12.5, None]  # a typed column
    measured = open_exporter(f"s3://wg-bucket/measured.sqlite?endpoint_url={endpoint}")
    for n in range(50):
        measured.write({"url": f"https://s.example/{n}", "text": "x" * 200})
    measured.flush()
    assert measured.bytes_written > 10_000  # SQLite counts no bytes: its file does (max_output_bytes)
    measured.close()
    installed = __import__("importlib").util.find_spec
    formats = [".json", ".sqlite", *([".xlsx"] if installed("openpyxl") else []),
               *([".duckdb"] if installed("duckdb") else [])]  # fmt: skip
    for suffix in formats:  # every file output, as an object
        target = f"s3://wg-bucket/crawls/items{suffix}?endpoint_url={endpoint}"
        write(target, ITEMS[:2], unique_key="url")
        write(target, [{"url": "https://s.example/2", "price": 13}], unique_key="url", append=True)
        rows = list(read_records(target))
        assert rows[0]["url"] == "https://s.example/1" and rows[-1]["url"] == "https://s.example/2", suffix
        assert len(rows) == (2 if suffix in (".sqlite", ".duckdb") else 3), suffix  # (upserted on the key)
        assert rows[-1]["price"] == 13, suffix

    from wintergrab.cli import main

    books = f"s3://wg-bucket/books.csv?endpoint_url={endpoint}"
    assert main(["-q", "crawl", site.url + "/books/", "--allow", "/books/", "--max-pages", "3", "--auto", "-o",
                 books, "--no-progress"]) == 0  # fmt: skip
    capsys.readouterr()
    assert main(["data", "quality", books]) == 0 and "record(s), quality score" in capsys.readouterr().out


def test_s3_urls(s3, monkeypatch, tmp_path) -> None:
    endpoint, _ = s3
    with pytest.raises(ConfigurationError, match="holds no credentials"):
        open_exporter(f"s3://AKIA:secret@wg-bucket/items.jsonl?endpoint_url={endpoint}")
    with pytest.raises(ConfigurationError, match="unknown option 'acl'"):
        open_exporter(f"s3://wg-bucket/items.jsonl?endpoint_url={endpoint}&acl=public-read")
    with pytest.raises(ConfigurationError, match="extension picks the format"):
        open_exporter(f"s3://wg-bucket/items.txt?endpoint_url={endpoint}")
    with pytest.raises(ConfigurationError, match="name the bucket and the object"):
        open_exporter(f"s3://wg-bucket/?endpoint_url={endpoint}")
    with pytest.raises(ConfigurationError, match="there is no bucket 'nothing-here'"):
        open_exporter(f"s3://nothing-here/items.jsonl?endpoint_url={endpoint}")
    with pytest.raises(ConfigurationError, match="no such object"):
        list(read_records(f"s3://wg-bucket/missing.jsonl?endpoint_url={endpoint}"))
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "none"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "none"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    with pytest.raises(ConfigurationError, match="no AWS credentials found"):
        open_exporter(f"s3://wg-bucket/other.jsonl?endpoint_url={endpoint}&region=eu-west-1")
