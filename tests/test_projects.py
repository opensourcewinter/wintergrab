"""Projects, jobs, schedules and webhooks."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from test_history import ShopSpider, product_page, shop  # noqa: F401 (the fixture)
from wintergrab.errors import ConfigurationError
from wintergrab.project import Project, Scheduler
from wintergrab.schedules import Cron, Interval, Once, parse_schedule
from wintergrab.webhooks import SIGNATURE_HEADER, Webhook, verify


class Receiver(ThreadingHTTPServer):
    """A webhook endpoint that keeps what it gets (answering with ``statuses`` in turn, then 200)."""

    daemon_threads = True

    def __init__(self, statuses: list[int] | None = None) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.statuses = list(statuses or [])
        self.received: list[tuple[dict[str, str], bytes]] = []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/hook"

    def events(self) -> list[dict[str, Any]]:
        return [e for _, body in self.received for e in json.loads(body)["events"]]

    def kinds(self) -> list[str]:
        return [e["event"] for e in self.events()]


class _Handler(BaseHTTPRequestHandler):
    server: Receiver

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        status = self.server.statuses.pop(0) if self.server.statuses else 200
        if status == 200:
            self.server.received.append((dict(self.headers), body))
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def receiver():
    server = Receiver()
    yield server
    server.shutdown()
    server.server_close()


def test_schedules() -> None:
    saturday = datetime(2026, 9, 26, 9, 30)
    assert parse_schedule("every 2 hours").next(saturday) == saturday  # the first run: now
    assert parse_schedule("every 2 hours").next(saturday, last=datetime(2026, 9, 26, 8)) == datetime(2026, 9, 26, 10)
    assert parse_schedule("every 15m").next(saturday, last=datetime(2026, 9, 26, 9)) == saturday  # overdue: now
    assert parse_schedule("*/15 * * * *").next(saturday) == datetime(2026, 9, 26, 9, 45)
    assert parse_schedule("0 6 * * mon-fri").next(saturday) == datetime(2026, 9, 28, 6)
    assert parse_schedule("daily at 06:00").next(saturday) == datetime(2026, 9, 27, 6)
    assert parse_schedule("weekly on monday at 07:15").next(saturday) == datetime(2026, 9, 28, 7, 15)
    assert parse_schedule("0 12 * * 7").next(saturday) == datetime(2026, 9, 27, 12)  # 7 is Sunday too
    assert parse_schedule("0 0 29 2 *").next(saturday) == datetime(2028, 2, 29)
    assert parse_schedule("0 0 30 2 *").next(saturday) is None
    assert parse_schedule("0 9 1,15 * mon").next(saturday) == datetime(2026, 9, 28, 9)  # either day
    once = parse_schedule("once at 2026-10-01 06:00")
    assert isinstance(once, Once) and once.next(saturday) == datetime(2026, 10, 1, 6)
    assert once.next(saturday, last=datetime(2026, 10, 1, 6)) is None
    assert isinstance(parse_schedule(90), Interval) and parse_schedule("hourly").seconds == 3600
    berlin = parse_schedule("daily at 06:00", timezone="Europe/Berlin")
    utc = datetime(2026, 9, 26, 9, 30).astimezone()
    assert isinstance(berlin, Cron) and berlin.next(utc).tzinfo is not None
    for bad in ("sometimes", "every -2 hours", "61 * * * *", "daily at 25:00", "once at yesterday"):
        with pytest.raises(ConfigurationError):
            parse_schedule(bad)
    with pytest.raises(ConfigurationError, match="unknown time zone"):
        parse_schedule("hourly", timezone="Mars/Olympus")


def test_webhooks_deliver_signed_batches(receiver) -> None:
    from wintergrab.events import Event

    hook = Webhook(receiver.url, secret="s3cret", interval=0.2, headers={"Authorization": "Bearer x"})
    for i in range(3):
        hook(Event("request_failed", {"url": f"https://s.example/p/{i}"}, origin="shop"))
    hook(Event("response", {"url": "https://s.example/"}))  # one per page: only when asked by name
    hook(Event("record_updated", {"url": "https://s.example/p/9"}))
    hook.close()
    [(headers, body)] = receiver.received  # one delivery for the three
    assert [e["url"] for e in json.loads(body)["events"]] == [f"https://s.example/p/{i}" for i in range(3)]
    assert verify(body, headers[SIGNATURE_HEADER], "s3cret") and not verify(body, headers[SIGNATURE_HEADER], "other")
    assert headers["Authorization"] == "Bearer x" and hook.stats["delivered"] == 3

    receiver.received.clear()
    receiver.statuses = [503, 200]  # tried again
    retried = Webhook(receiver.url, interval=0)
    retried.post_event({"event": "job_failed", "job": "x"})
    retried.close()
    assert receiver.kinds() == ["job_failed"] and retried.stats["failed"] == 0
    receiver.statuses = [400]  # not tried again
    refused = Webhook(receiver.url, interval=0)
    refused.post_event({"event": "job_failed"})
    refused.close()
    assert refused.stats["failed"] == 1

    with pytest.raises(ConfigurationError, match="http"):
        Webhook("ftp://x.example/")
    with pytest.raises(ConfigurationError, match="no setting"):
        Webhook.coerce({"url": receiver.url, "secrets": "typo"})
    assert Webhook.coerce({"url": receiver.url, "events": "job_failed"}).kinds() == {"job_failed"}


def test_changes_reach_webhooks(shop, receiver, tmp_path) -> None:  # noqa: F811
    history = str(tmp_path / "shop.history")
    hooks = [{"url": receiver.url, "events": ["record_created", "record_updated", "record_deleted", "site_changed",
                                              "crawl_finished"], "interval": 0.1}]  # fmt: skip
    ShopSpider(start_urls=[shop.url + "/"], history=history, webhooks=hooks).run()
    assert receiver.kinds() == ["crawl_finished"]  # a first run is the baseline: nothing changed
    receiver.received.clear()
    shop.pages["/p/1"] = (200, product_page("Product 1", 8.0))
    shop.pages["/p/3"] = (404, "gone")
    shop.pages["/p/5"] = (200, product_page("Product 5", 50.0))
    shop.pages["/"] = (200, shop.pages["/"][1].replace("</body>", "<a href='/p/5'>P5</a></body>"))
    ShopSpider(start_urls=[shop.url + "/"], history=history, webhooks=hooks).run()
    events = {e["event"]: e for e in receiver.events()}
    assert sorted(events) == ["crawl_finished", "record_created", "record_deleted", "record_updated", "site_changed"]
    assert events["record_updated"]["url"] == shop.url + "/p/1" and events["record_updated"]["details"]["price"] == [
        10.0,
        8.0,
    ]
    assert events["record_created"]["url"] == shop.url + "/p/5" and events["record_deleted"]["url"] == shop.url + "/p/3"
    assert events["site_changed"]["added"] == 1 and events["crawl_finished"]["origin"] == "shop"


def test_extraction_and_quality_events(site, tmp_path) -> None:
    from wintergrab.cli import main

    def crawl(fields: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        schema = tmp_path / "book.schema.json"
        schema.write_text(json.dumps({"name": "book", "fields": fields}), encoding="utf-8")
        events = tmp_path / "events.jsonl"
        events.unlink(missing_ok=True)
        assert main(["-q", "crawl", site.url + "/books/", "--allow", "/books/catalogue/", "--extract", str(schema),
                     "--quality", str(tmp_path / "quality.json"), "--events", str(events),
                     "-o", str(tmp_path / "books.jsonl"), "--no-progress"]) == 0  # fmt: skip
        found: dict[str, list[dict[str, Any]]] = {}
        for line in events.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            found.setdefault(event["event"], []).append(event)
        return found

    name = {"type": "string", "selectors": ["h1"], "required": True}
    first = crawl({"name": name, "price": "money", "url": "url"})
    assert "schema_changed" not in first and "quality_degraded" not in first  # the first run is the baseline
    second = crawl({"name": name, "title": {"type": "string", "selectors": ["title"]}, "url": "url"})
    [changed] = second["schema_changed"]
    assert (changed["dataset"], changed["added"], changed["removed"]) == ("book", ["title"], ["price"])
    assert [e["code"] for e in second["quality_degraded"]] == ["field-disappeared"]
    third = crawl({"name": name, "isbn": {"type": "string", "selectors": [".isbn"], "required": True}})
    failed = third["extraction_failed"]  # every book page: no ISBN on them (the listing pages are no book's)
    assert len(failed) == 12 and failed[0]["missing"] == ["isbn"] and failed[0]["schema"] == "book"


def _project(tmp_path, site_url: str, hook_url: str, **extra: Any) -> Any:
    data = {
        "defaults": {"concurrency": 4},
        "webhooks": [
            {
                "url": hook_url,
                "events": ["job_started", "job_finished", "job_failed", "crawl_finished"],
                "secret": "${TEST_WEBHOOK_SECRET}",
                "interval": 0.1,
            }
        ],
        "jobs": {
            "books": {
                "crawl": site_url + "/books/",
                "allow": "/books/",
                "output": "data/books.jsonl",
                "schedule": "every 2 hours",
                "no_progress": True,
            },
            "rated": {
                "goal": f"books rated 4 stars or more on {site_url}/books/",
                "output": "data/rated.jsonl",
                "sample": 15,
            },
        },
        **extra,
    }
    path = tmp_path / "wintergrab.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_a_project(site, receiver, tmp_path, capsys, monkeypatch) -> None:
    from wintergrab.cli import main

    monkeypatch.setenv("TEST_WEBHOOK_SECRET", "s3cret")
    path = _project(tmp_path, site.url, receiver.url)
    project = Project(path)
    books = project.jobs["books"]
    assert books.command() == ["crawl", site.url + "/books/", "--concurrency", "4", "--allow", "/books/",
                               "--output", "data/books.jsonl", "--no-progress"]  # fmt: skip
    assert project.jobs["rated"].command()[:3] == ["goal", f"books rated 4 stars or more on {site.url}/books/", "--yes"]
    assert str(books.schedule) == "every 2 hours" and project.jobs["rated"].schedule is None
    assert main(["run", "--project", str(path), "--list"]) == 0
    assert "books            wintergrab crawl" in capsys.readouterr().out

    assert main(["run", "--project", str(path)]) == 0
    err = capsys.readouterr().err
    assert "books: finished in" in err and "rated: finished in" in err
    assert len((tmp_path / "data" / "books.jsonl").read_text().splitlines()) == 16
    assert len((tmp_path / "data" / "rated.jsonl").read_text().splitlines()) == 4
    from wintergrab.runs import RunRegistry

    labels = sorted(r.label for r in RunRegistry(tmp_path / ".wintergrab").runs())
    assert labels == ["books", "rated"]
    kinds = receiver.kinds()
    assert kinds.count("job_started") == 2 and kinds.count("job_finished") == 2 and kinds.count("crawl_finished") == 2
    finished = [e for e in receiver.events() if e["event"] == "job_finished"]
    assert {e["job"] for e in finished} == {"books", "rated"} and all(e["run"] for e in finished)
    assert all(verify(body, headers[SIGNATURE_HEADER], "s3cret") for headers, body in receiver.received)

    monkeypatch.delenv("TEST_WEBHOOK_SECRET")
    with pytest.raises(ConfigurationError, match="TEST_WEBHOOK_SECRET is not set"):
        Project(path).webhooks()


def test_project_errors(tmp_path) -> None:
    def project(data: dict[str, Any]) -> Project:
        path = tmp_path / "p.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return Project(path)

    with pytest.raises(ConfigurationError, match="no jobs"):
        project({"jobs": {}})
    with pytest.raises(ConfigurationError, match="has no option 'folow' \\(did you mean 'follow'\\?\\)"):
        project({"jobs": {"a": {"crawl": "https://s.example/", "folow": "a"}}})
    with pytest.raises(ConfigurationError, match="one of crawl"):
        project({"jobs": {"a": {"crawl": "https://s.example/", "goal": "products"}}})
    with pytest.raises(ConfigurationError, match="job 'a': cannot read the schedule"):
        project({"jobs": {"a": {"crawl": "https://s.example/", "schedule": "whenever"}}})
    with pytest.raises(ConfigurationError, match="unknown section"):
        project({"jobs": {"a": {"crawl": "https://s.example/"}}, "hooks": []})
    with pytest.raises(ConfigurationError, match="set by the project"):
        project({"jobs": {"a": {"crawl": "https://s.example/", "workspace": "x"}}})
    with pytest.raises(ConfigurationError, match="read in webhooks only"):  # never on a command line
        project({"jobs": {"a": {"crawl": "https://s.example/", "proxy": "http://u:${PROXY_PASSWORD}@p.example"}}})
    with pytest.raises(ConfigurationError, match="start_within is for schedules at set times"):
        project({"jobs": {"a": {"crawl": "https://s.example/", "schedule": "every 2 hours", "start_within": "1h"}}})
    assert project({"jobs": {"a": {"spider": "spiders/shop.py:Shop", "set": {"max_pages": 5}}}}).jobs["a"].command() == [
        "crawl", "spiders/shop.py:Shop", "--set", "max_pages=5"]  # fmt: skip


def test_the_scheduler(tmp_path) -> None:
    path = tmp_path / "wintergrab.json"
    path.write_text(json.dumps({"jobs": {
        "often": {"crawl": "https://s.example/", "schedule": "every 2 hours"},
        "morning": {"crawl": "https://s.example/m", "schedule": "daily at 06:00", "start_within": "1 hour"},
        "late": {"crawl": "https://s.example/l", "schedule": "daily at 23:00"},
        "manual": {"crawl": "https://s.example/x"},
    }}), encoding="utf-8")  # fmt: skip
    clock = [datetime(2026, 9, 26, 9, 30)]
    ran: list[str] = []

    def runner(command: list[str], *, cwd: Any, log_file: Any) -> int:
        ran.append(command[1])
        clock[0] += timedelta(minutes=5)  # a job takes a while
        return 0 if "/m" not in command[1] else 3

    def make() -> Scheduler:
        return Scheduler(Project(path), now=lambda: clock[0], sleep=lambda s: None, runner=runner, webhooks=[])

    scheduler = make()
    assert [(j.name, w) for j, w in scheduler.plan()] == [
        ("often", datetime(2026, 9, 26, 9, 30)), ("late", datetime(2026, 9, 26, 23)), ("morning", datetime(2026, 9, 27, 6))]  # fmt: skip
    assert [r.job.name for r in scheduler.run_due()] == ["often"] and ran == ["https://s.example/"]
    clock[0] = datetime(2026, 9, 27, 6, 0, 20)  # (wintergrab schedule --once, from cron)
    results = make().run_due()  # a new scheduler knows the last runs (schedule.json)
    assert [(r.job.name, r.status, r.exit_code) for r in results] == [("often", "finished", 0), ("morning", "failed", 3),
                                                                        ("late", "finished", 0)]  # fmt: skip
    state = json.loads((tmp_path / ".wintergrab" / "schedule.json").read_text())["jobs"]
    assert state["morning"]["last_status"] == "failed" and state["often"]["last_run"].startswith("2026-09-27T06:00")
    clock[0] = datetime(2026, 9, 29, 7, 0)  # nothing ran for two days
    due = {j.name: w for j, w in make().plan()}
    assert due["late"] == clock[0] and due["morning"] == datetime(2026, 9, 30, 6)  # made up for / too late
    loops = iter(range(3))
    scheduler = make()
    scheduler.loop(until=lambda: next(loops, None) is None)
    assert len(scheduler.results) == 2  # often and late, then nothing due


def test_init_and_schedule_commands(tmp_path, capsys) -> None:
    from wintergrab.cli import main

    assert main(["init", str(tmp_path)]) == 0 and (tmp_path / ".wintergrab").is_dir()
    assert main(["init", str(tmp_path)]) == 1  # not twice
    capsys.readouterr()
    assert main(["schedule", "--project", str(tmp_path / "wintergrab.yaml"), "--list"]) == 0
    assert "example          daily at 06:00" in capsys.readouterr().out
