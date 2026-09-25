from __future__ import annotations

import csv
import json
import subprocess
import sys
import textwrap

import pytest

from wintergrab import __version__
from wintergrab.cli import load_spider_class, main


def run(capsys, *args: str) -> tuple[int, str, str]:
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def test_version_and_help(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["--version"])
    assert __version__ in capsys.readouterr().out
    code, out, _ = run(capsys)
    assert code == 2 and "get" in out and "crawl" in out


def test_get_prints_markdown(site, capsys) -> None:
    code, out, err = run(capsys, "get", site.url + "/product/2")
    assert code == 0
    assert "# Product 2" in out
    assert "200 " in err


def test_get_css_selection(site, capsys) -> None:
    _, out, _ = run(capsys, "get", site.url + "/products/page/1", "--css", ".product .name a::text")
    assert out.splitlines() == [f"Product {i}" for i in range(1, 5)]
    _, out, _ = run(capsys, "get", site.url + "/products/page/1", "--xpath", "//span[@class='price']", "-f", "json")
    assert json.loads(out)[0]["matches"][0] == "$6.25"


def test_get_records_to_csv(site, tmp_path, capsys) -> None:
    out_file = tmp_path / "products.csv"
    code, _, err = run(
        capsys, "get", site.url + "/products/page/1", "--each", ".product",
        "--field", "name=.name a::text", "--field", "price=.price::text", "-o", str(out_file),
    )  # fmt: skip
    assert code == 0 and "wrote 4 record(s)" in err
    rows = list(csv.DictReader(out_file.open(encoding="utf-8", newline="")))
    assert rows[0] == {"name": "Product 1", "price": "$6.25"}
    assert b"\r\r\n" not in out_file.read_bytes()  # Windows: csv's CRLF plus text-mode newline translation


def test_get_records_as_jsonl_for_many_urls(site, capsys) -> None:
    _, out, _ = run(capsys, "get", site.url + "/product/1", site.url + "/product/2", "--field", "title=h1::text")
    rows = [json.loads(line) for line in out.splitlines()]
    assert [r["title"] for r in rows] == ["Product 1", "Product 2"]
    assert rows[0]["url"].endswith("/product/1")


def test_get_saves_html_and_reports_failures(site, tmp_path, capsys) -> None:
    page = tmp_path / "page.html"
    code, _, _ = run(capsys, "get", site.url + "/json", site.url + "/status/404", "-o", str(page), "--retries", "0")
    assert code == 1  # the 404 counts as a failure
    assert '"items"' in page.read_text(encoding="utf-8")


def test_get_sends_headers_and_cookies(site, capsys) -> None:
    _, out, _ = run(capsys, "get", site.url + "/headers", "-H", "X-Hello: there", "--cookie", "k=v", "-f", "html")
    echoed = json.loads(out)
    assert echoed["X-Hello"] == "there"
    assert "k=v" in echoed["Cookie"]


def test_crawl_url_mode(site, tmp_path, capsys) -> None:
    out_file = tmp_path / "items.jsonl"
    code, _, err = run(
        capsys, "-q", "crawl", site.url + "/products/page/1", "--follow", "a.next", "--follow", ".product .name",
        "--each", "h1", "--field", "name=::text", "-o", str(out_file),
    )  # fmt: skip
    assert code == 0 and "finished" in err
    rows = [json.loads(line) for line in out_file.read_text(encoding="utf-8").splitlines()]
    names = sorted(r["name"] for r in rows)
    assert len(names) == 20 and names[0] == "Product 1"


def test_crawl_url_mode_streams_to_stdout(site, capsys) -> None:
    _, out, _ = run(capsys, "-q", "crawl", site.url + "/links?n=3", "--allow", "/item/", "--max-pages", "10")
    rows = [json.loads(line) for line in out.splitlines()]
    assert {r["title"] for r in rows} >= {"links", "item 0", "item 1", "item 2"}


SPIDER_FILE = """
from wintergrab import Spider

class QuotesSpider(Spider):
    name = "quotes"

    def parse(self, response):
        for q in response.css(".quote"):
            yield {"author": q.css(".author::text").get()}
        nxt = response.css("li.next a")
        if nxt:
            yield response.follow(nxt[0])
"""


def test_crawl_spider_file_with_overrides(site, tmp_path, capsys) -> None:
    spider_file = tmp_path / "quotes_spider.py"
    spider_file.write_text(SPIDER_FILE)
    out_file = tmp_path / "quotes.json"
    code, _, err = run(
        capsys, "-q", "crawl", str(spider_file), "-o", str(out_file),
        "-s", f'start_urls=["{site.url}/quotes/"]', "-s", "concurrency=2", "--crawl-dir", str(tmp_path / "state"),
    )  # fmt: skip
    assert code == 0, err
    authors = [r["author"] for r in json.loads(out_file.read_text(encoding="utf-8"))]
    assert len(authors) == 6 and "Albert Einstein" in authors


def test_load_spider_class_errors(tmp_path) -> None:
    two = tmp_path / "two.py"
    two.write_text("from wintergrab import Spider\nclass A(Spider): pass\nclass B(Spider): pass\n")
    with pytest.raises(SystemExit, match="several spiders"):
        load_spider_class(str(two))
    assert load_spider_class(f"{two}:B").__name__ == "B"
    with pytest.raises(SystemExit, match="no such file"):
        load_spider_class(str(tmp_path / "missing.py"))


def test_unknown_override_is_reported(site, tmp_path, capsys) -> None:
    with pytest.raises(SystemExit, match="no setting"):
        main(["crawl", site.url, "-s", "bogus=1"])


def test_module_entry_point(site) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "wintergrab", "get", site.url + "/product/5", "--css", "h1::text"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "Product 5"


def test_shell_is_wired(monkeypatch, site, capsys) -> None:
    import code as code_module

    captured = {}
    monkeypatch.setattr(code_module, "interact", lambda **kw: captured.update(kw))
    monkeypatch.setitem(sys.modules, "IPython", None)  # force the stdlib shell
    assert main(["shell", site.url + "/product/1"]) == 0
    assert captured["local"]["page"].css("h1::text").get() == "Product 1"
    assert textwrap.dedent("page = ") in captured["banner"]


def test_output_survives_a_narrow_locale_encoding(monkeypatch) -> None:
    import io

    from wintergrab import cli

    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))  # a Windows pipe
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    cli._utf8_output()
    print("₹ 中文 £")  # UnicodeEncodeError in cp1252
    sys.stdout.flush()
    assert raw.getvalue().decode("utf-8").strip() == "₹ 中文 £"


def test_public_only_refuses_local_addresses(fresh_site, capsys) -> None:
    code, _, err = run(capsys, "get", fresh_site.url + "/product/1", "--public-only")
    assert code == 1 and "Blocked by network policy" in err and "loopback" in err
    code, out, err = run(capsys, "crawl", fresh_site.url + "/products/page/1", "--public-only", "--no-robots")
    assert out == "" and "0 items" in err
    assert sum(fresh_site.site.hits.values()) == 0


def test_crawl_normalize_urls_and_default_url_rules(fresh_site, capsys) -> None:
    code, out, _ = run(
        capsys, "crawl", fresh_site.url + "/tracking-links", "--normalize-urls", "--no-robots",
        "--allow", r"/item/|/img/|/a/b/|tracking-links",
    )  # fmt: skip
    assert code == 0
    urls = sorted(json.loads(line)["url"] for line in out.splitlines())
    assert urls == [fresh_site.url + f"/item/{i}" for i in range(3)] + [fresh_site.url + "/tracking-links"]
    assert fresh_site.site.hits["/img/photo.jpg"] == 0  # skipped by the default URL rules
