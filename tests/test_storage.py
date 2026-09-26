"""Storage adapters: Parquet, Excel and PostgreSQL outputs, their readers, and the registries."""

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


def test_crawls_write_them_and_data_commands_read_them(site, tmp_path, capsys) -> None:
    pytest.importorskip("pyarrow")
    pytest.importorskip("openpyxl")
    from wintergrab.cli import main

    for name in ("books.parquet", "books.xlsx"):
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
    (tmp_path / ".my.cnf").write_text("[client]\npassword = from-the-file\n", encoding="utf-8")
    arguments, _ = parse_target("mysql://crawler@db.example/shop")
    assert "password" not in arguments and arguments["read_default_file"] == str(tmp_path / ".my.cnf")
