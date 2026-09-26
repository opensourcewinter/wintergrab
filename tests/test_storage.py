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
    stopped.flush()  # (a checkpoint; then the process dies)
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
