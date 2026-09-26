"""The dashboard: runs, their numbers and what lies under them, over HTTP."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from test_history import ShopSpider, product_page, shop  # noqa: F401 (the fixture)
from wintergrab.dashboard import Dashboard, serve, summarize_events
from wintergrab.data.quality import QualityMonitor
from wintergrab.project import Project
from wintergrab.runs import RunRegistry


@pytest.fixture
def dashboard_server(tmp_path):
    started: list[Any] = []

    def start(workspace: Path, project: Any = None) -> Any:
        server = serve(workspace, project=project, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        return server

    yield start
    for server in started:
        server.shutdown()
        server.server_close()


def fetch(server: Any, path: str, *, method: str = "GET", host: str | None = None) -> tuple[int, Any, str]:
    request = urllib.request.Request(server.url.rstrip("/") + path, method=method,
                                     data=b"x" if method == "POST" else None,
                                     headers={"Host": host} if host else {})  # fmt: skip
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            return answer.status, answer.headers, answer.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read().decode("utf-8")


def test_runs_and_what_lies_under_them(shop, tmp_path, dashboard_server) -> None:  # noqa: F811
    workspace = tmp_path / ".wintergrab"

    def crawl() -> None:
        ShopSpider(start_urls=[shop.url + "/"], history=str(tmp_path / "shop.history"), run_registry=str(workspace),
                   run_label="<script>alert('x')</script>", pipelines=[QualityMonitor(name="products")]).run()  # fmt: skip

    crawl()
    shop.pages["/p/1"] = (200, product_page("Product 1", 8.0))
    shop.pages["/p/3"] = (404, "gone")
    crawl()
    server = dashboard_server(workspace)

    status, headers, page = fetch(server, "/")
    assert status == 200 and 'href="/runs/run-2"' in page and 'href="/runs/run-1"' in page
    assert "<script>" not in page and "&lt;script&gt;" in page  # what crawls give is text, never markup
    assert "default-src 'none'" in headers["Content-Security-Policy"] and headers["X-Frame-Options"] == "DENY"

    status, _, page = fetch(server, "/runs/run-2")
    assert status == 200 and "<script>" not in page
    assert '<div class="label">Pages</div><div class="value">5</div>' in page
    assert "Since the last run: +0 new, −1 gone, ~1 modified" in page  # the history
    assert f'href="{shop.url}/p/1"' in page and "[10.0, 8.0]" in page  # what changed on the page
    assert "HTTP 404" in page  # the failure, with its cause
    assert "Quality of products (3 records)" in page

    status, _, page = fetch(server, "/runs/run-2/events?kind=record_updated")
    assert status == 200 and shop.url + "/p/1" in page

    runs = json.loads(fetch(server, "/api/runs")[2])
    assert [(r["id"], r["state"]) for r in runs] == [("run-2", "finished"), ("run-1", "finished")]
    data = json.loads(fetch(server, "/api/runs/run-2")[2])
    assert data["metrics"]["pages"] == 5 and data["events"]["counts"]["record_deleted"] == 1
    assert data["quality"][0]["dataset"] == "products" and data["quality"][0]["records"] == 3

    assert fetch(server, "/runs/run-9")[0] == 404 and fetch(server, "/nothing")[0] == 404
    assert fetch(server, "/", host="evil.example:8710")[0] == 403  # DNS rebinding: not for this host
    assert fetch(server, "/", method="POST")[0] == 405  # it changes nothing


def test_credentials_stay_hidden(tmp_path, dashboard_server) -> None:
    registry = RunRegistry(tmp_path / "ws")
    run = registry.create("old", settings={"default_headers": {"Authorization": "Bearer s3cr3t"}, "concurrency": 2},
                          recipe={"command": ["crawl", "https://a.example/", "--proxy", "http://me:pa55@proxy:8080"]})  # fmt: skip
    run.status = "finished"
    run.finished = run.started + 1
    registry.save(run)  # a run kept before runs left credentials out
    server = dashboard_server(tmp_path / "ws")
    for path in ("/runs/run-1", "/api/runs/run-1", "/api/runs"):
        text = fetch(server, path)[2]
        assert "s3cr3t" not in text and "pa55" not in text and "***" in text


def test_a_run_that_stopped_answering(tmp_path) -> None:
    registry = RunRegistry(tmp_path)
    run = registry.create("crashed")  # "running", and no one keeps its metrics
    dashboard = Dashboard(tmp_path)
    old = time.time() - 120
    os.utime(run.directory / "run.json", (old, old))
    run.started = old
    registry.save(run)
    assert dashboard.state(registry.get("run-1")) == "not responding"
    (run.directory / "metrics.json").write_text("{}", encoding="utf-8")  # a crawl that is alive keeps them
    assert dashboard.state(registry.get("run-1")) == "running"


def test_a_live_crawl(fresh_site, tmp_path, dashboard_server) -> None:
    from wintergrab import Spider

    class Slow(Spider):
        concurrency = 1
        log_level = None
        progress = False

        def parse(self, response: Any) -> Any:
            yield {"url": response.url}

    workspace = tmp_path / "ws"
    urls = [fresh_site.url + f"/slow?delay=0.4&n={i}" for i in range(12)]
    crawl = threading.Thread(target=Slow(start_urls=urls, run_registry=str(workspace)).run, daemon=True)
    crawl.start()
    server = dashboard_server(workspace)
    deadline = time.monotonic() + 10
    while not (workspace / "runs" / "run-1" / "metrics.json").exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    status, _, page = fetch(server, "/runs/run-1")
    assert status == 200 and "running" in page and 'http-equiv="refresh"' in page  # it follows the crawl
    assert '<div class="label">Queued</div>' in page
    live = json.loads(fetch(server, "/api/runs/run-1")[2])
    assert live["state"] == "running" and 0 < live["metrics"]["pages"] < 12
    crawl.join(20)
    assert json.loads(fetch(server, "/api/runs/run-1")[2])["metrics"]["pages"] == 12


def test_summarize_events(tmp_path) -> None:
    lines = [
        {"event": "response", "url": "https://s.example/1"},
        {"event": "request_failed", "url": "https://s.example/a", "category": "http", "kind": None, "status": 500,
         "error": "HTTP 500"},
        {"event": "request_failed", "url": "https://s.example/b", "category": "http", "kind": None, "status": 500,
         "error": "HTTP 500"},
        {"event": "request_failed", "url": "https://s.example/c", "category": "network", "kind": "dns", "status": None,
         "error": "no such host"},
        {"event": "blocked", "url": "https://s.example/x", "status": 403, "domain": "s.example"},
        {"event": "extraction_failed", "url": "https://s.example/p/1", "schema": "p", "missing": ["price"]},
        {"event": "extraction_failed", "url": "https://s.example/p/2", "schema": "p", "missing": ["price", "name"]},
        {"event": "schema_changed", "dataset": "p", "added": ["sku"], "removed": [], "retyped": {}},
    ]  # fmt: skip
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    summary = summarize_events(path)
    assert summary.counts["response"] == 1 and summary.counts["request_failed"] == 3
    assert [(g["category"], g["count"]) for g in summary.failures] == [("http", 2), ("network", 1)]
    assert summary.blocked == {"s.example": 1}
    assert summary.extraction == {"pages": 2, "fields": {"price": 2, "name": 1},
                                  "urls": ["https://s.example/p/1", "https://s.example/p/2"]}  # fmt: skip
    assert summary.schema == [{"dataset": "p", "added": ["sku"], "removed": [], "retyped": {}}]
    tail = summarize_events(path, max_bytes=200)  # a huge file: its last events
    assert tail.truncated and tail.counts["schema_changed"] == 1 and tail.counts["response"] == 0


def test_a_project_s_jobs(tmp_path, dashboard_server) -> None:
    path = tmp_path / "wintergrab.json"
    path.write_text(json.dumps({"jobs": {"books": {"crawl": "https://books.example/", "schedule": "daily at 06:00"},
                                         "later": {"crawl": "https://a.example/"}}}), encoding="utf-8")  # fmt: skip
    project = Project(path)
    jobs = Dashboard(project.workspace, project).jobs()
    assert [(j["job"], j["schedule"]) for j in jobs] == [("books", "daily at 06:00"), ("later", None)]
    assert jobs[0]["next"] is not None and jobs[1]["next"] is None
    page = fetch(dashboard_server(project.workspace, project), "/")[2]
    assert "<h2>Jobs</h2>" in page and "daily at 06:00" in page and "when asked" in page


def test_a_run_s_records_a_page_at_a_time(tmp_path, dashboard_server) -> None:
    workspace = tmp_path / "ws"
    registry = RunRegistry(workspace)
    output = tmp_path / "books.jsonl"  # (as given to the crawl, from the workspace's directory)
    output.write_text("".join(json.dumps({"title": f"Book {n}", "price": n}) + "\n" for n in range(1, 6)),
                      encoding="utf-8")  # fmt: skip
    for output_of in ("books.jsonl", "postgresql://***@db.example/shop?table=books", None, "gone.jsonl"):
        run = registry.create("books")
        run.status, run.finished, run.output = "finished", run.started + 1, output_of
        registry.save(run)
    server = dashboard_server(workspace)

    first = json.loads(fetch(server, "/api/runs/run-1/items?limit=2")[2])
    assert [r["title"] for r in first["items"]] == ["Book 1", "Book 2"] and first["next"] == 2
    last = json.loads(fetch(server, "/api/runs/run-1/items?offset=4&limit=2")[2])
    assert [r["title"] for r in last["items"]] == ["Book 5"] and last["next"] is None
    assert json.loads(fetch(server, "/api/runs/run-1/items")[2])["limit"] == 100  # (the default)
    assert 'href="/api/runs/run-1/items">records</a>' in fetch(server, "/runs/run-1")[2]
    with output.open("a", encoding="utf-8") as fh:
        fh.write('{"title": "Book 6", "pri')  # a crawl still writing it
    growing = json.loads(fetch(server, "/api/runs/run-1/items?offset=4")[2])
    assert [r["title"] for r in growing["items"]] == ["Book 5"] and "could not be read (yet)" in growing["note"]
    status, _, said = fetch(server, "/api/runs/run-2/items")
    assert status == 404 and "read its records there" in said  # a database: not read from here
    assert "kept no output" in fetch(server, "/api/runs/run-3/items")[2]
    assert "is not here any more" in fetch(server, "/api/runs/run-4/items")[2]
    assert fetch(server, "/api/runs/run-9/items")[0] == 404


def test_the_dashboard_command(tmp_path) -> None:
    import subprocess
    import sys

    process = subprocess.Popen(
        [sys.executable, "-m", "wintergrab", "dashboard", "--port", "0", "--workspace", str(tmp_path / "ws")],
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stderr is not None
        line = process.stderr.readline()  # "wintergrab dashboard: http://127.0.0.1:PORT/  (...; Ctrl+C to stop)"
        url = line.split()[2].rstrip("/")
        with urllib.request.urlopen(url + "/api/runs", timeout=10) as answer:
            assert json.loads(answer.read()) == [] and answer.headers["Server"] == "wintergrab-dashboard"
        with urllib.request.urlopen(urllib.request.Request(url + "/", method="HEAD"), timeout=10) as answer:
            assert answer.status == 200
    finally:
        process.terminate()
        process.wait(10)
        if process.stderr is not None:
            process.stderr.close()
