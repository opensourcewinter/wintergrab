"""Credentials: headers and cookies that only their own site's requests carry, and secrets kept off command lines."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from wintergrab import AsyncBrowserFetcher, AsyncFetcher, Credentials, Fetcher, Spider
from wintergrab.errors import ConfigurationError
from wintergrab.project import Project


class _Handler(BaseHTTPRequestHandler):
    """Pages under several names of one server: each name is a site of its own (``127.0.0.1``, ``localhost``,
    ``wg.localhost`` and the hosts below it, which curl and Chromium take for this machine)."""

    server: _Sites

    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        self.server.seen.append((host, self.path, self.headers.get("X-Token"), self.headers.get("Cookie")))
        port = self.server.server_address[1]
        if self.path == "/page":  # a page of its own site, with a link and an image on another site
            other = "127.0.0.1" if host != "127.0.0.1" else "localhost"
            body = (f"<html><body><h1>members</h1><a href='/own'>own</a><a href='http://{other}:{port}/other'>x</a>"
                    f"<img src='http://{other}:{port}/pixel'><img src='/image'></body></html>")  # fmt: skip
            return self._send(200, body)
        if self.path.startswith("/to/"):  # /to/HOST/PATH: a redirect there
            _, _, target, rest = self.path.split("/", 3)
            self.send_response(302)
            self.send_header("Location", f"http://{target}:{port}/{rest}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        return self._send(200, "<html><body><p>ok</p></body></html>")

    def _send(self, status: int, body: str) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _Sites(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.seen: list[tuple[str, str, str | None, str | None]] = []

    def url(self, host: str, path: str = "/") -> str:
        return f"http://{host}:{self.server_address[1]}{path}"

    def got(self, host: str, path: str) -> tuple[str | None, str | None]:
        """What the last request for ``path`` on ``host`` carried: (X-Token, Cookie)."""
        found = [(token, cookie) for h, p, token, cookie in self.seen if (h, p) == (host, path)]
        assert found, f"no request for {path} on {host} (requests: {self.seen})"
        return found[-1]


@pytest.fixture
def sites():
    server = _Sites()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def test_their_site_s_requests_carry_them_and_no_other_does(sites) -> None:
    club = Credentials("https://wg.localhost/login", headers={"X-Token": "s3cret"}, cookies={"session": "abc"})
    assert (
        club.site == "wg.localhost" and club.covers("http://www.wg.localhost/") and not club.covers("http://localhost/")
    )
    assert "s3cret" not in repr(club) and "abc" not in repr(club)  # (logs, errors: the names only)
    urls = {
        "own": sites.url("wg.localhost", "/own"),
        "below": sites.url("a.wg.localhost", "/below"),
        "other": sites.url("127.0.0.1", "/other"),
        "away": sites.url("wg.localhost", "/to/127.0.0.1/landed"),  # a redirect to another site
        "in": sites.url("127.0.0.1", "/to/wg.localhost/arrived"),  # and one to the site
    }
    with Fetcher(impersonate=None, retries=0, credentials=club) as fetcher:
        for url in urls.values():
            fetcher.get(url)
        fetcher.get(sites.url("wg.localhost", "/mine"), headers={"x-token": "its own"})  # a request's own wins
    expected = {
        ("wg.localhost", "/own"): ("s3cret", "session=abc"),
        ("a.wg.localhost", "/below"): ("s3cret", "session=abc"),
        ("127.0.0.1", "/other"): (None, None),
        ("127.0.0.1", "/landed"): (None, None),
        ("wg.localhost", "/arrived"): ("s3cret", "session=abc"),
        ("wg.localhost", "/mine"): ("its own", "session=abc"),
    }
    assert {key: sites.got(*key) for key in expected} == expected

    sites.seen.clear()

    async def fetch_all() -> None:
        async with AsyncFetcher(impersonate=None, retries=0, credentials=[club]) as fetcher:
            for url in urls.values():
                await fetcher.get(url)

    asyncio.run(fetch_all())
    del expected[("wg.localhost", "/mine")]
    assert {key: sites.got(*key) for key in expected} == expected
    with pytest.raises(ConfigurationError, match="credentials are for a site"):
        Credentials("")
    with pytest.raises(ConfigurationError, match=r"credentials are Credentials\(site, headers=..., cookies=...\)"):
        Fetcher(credentials=[{"site": "wg.localhost"}])  # type: ignore[list-item]


class Members(Spider):
    name = "members"
    allowed_domains = ["localhost", "127.0.0.1"]
    obey_robots_txt = False
    impersonate = None

    def parse(self, response):
        yield {"url": response.url}
        yield from response.follow_all(css="a::attr(href)")


def test_a_crawl_takes_them_to_its_site_only(sites) -> None:
    club = Credentials("localhost", headers={"X-Token": "s3cret"}, cookies={"session": "abc"})
    result = Members(start_urls=[sites.url("localhost", "/page")], credentials=[club], log_level="WARNING").run()
    assert result.stats["status"] == "finished" and len(result.items) == 3
    assert sites.got("localhost", "/page") == ("s3cret", "session=abc")
    assert sites.got("localhost", "/own") == ("s3cret", "session=abc")
    assert sites.got("127.0.0.1", "/other") == (None, None)  # a link the crawl followed to another site


def test_the_command_line_holds_the_names_of_secrets(sites, monkeypatch, capsys) -> None:
    from wintergrab.cli import main

    monkeypatch.setenv("WG_TEST_TOKEN", "s3cret")
    monkeypatch.setenv("WG_TEST_SESSION", "abc")
    argv = ["-q", "crawl", sites.url("localhost", "/page"), "--any-domain", "--follow", "a", "--no-robots",
            "--max-pages", "5", "-H", "X-Token: ${WG_TEST_TOKEN}", "--cookie", "session=${WG_TEST_SESSION}",
            "--no-progress"]  # fmt: skip
    assert main(argv) == 0
    assert sites.got("localhost", "/page") == ("s3cret", "session=abc")
    assert sites.got("127.0.0.1", "/other") == (None, None)
    capsys.readouterr()

    sites.seen.clear()
    assert main(["-q", "get", sites.url("127.0.0.1", "/to/localhost/arrived"), "-H", "X-Token: ${WG_TEST_TOKEN}"]) == 0
    assert sites.got("127.0.0.1", "/to/localhost/arrived") == ("s3cret", None)  # the URL's site: 127.0.0.1
    assert sites.got("localhost", "/arrived") == (None, None)  # not where it redirects
    monkeypatch.delenv("WG_TEST_TOKEN")
    assert main(["-q", "get", sites.url("127.0.0.1", "/x"), "-H", "X-Token: ${WG_TEST_TOKEN}"]) != 0
    assert "the environment variable WG_TEST_TOKEN is not set" in capsys.readouterr().err


@pytest.mark.browser
def test_in_a_browser(sites) -> None:
    # (Playwright fetches a page with headers itself, and it cannot find wg.localhost: localhost is the site here)
    club = Credentials("localhost", headers={"X-Token": "s3cret"}, cookies={"session": "abc"})

    async def fetch(path: str) -> Any:
        async with AsyncBrowserFetcher(credentials=[club], block_resources=(), wait_until="networkidle") as browser:
            return await browser.get(sites.url("localhost", path))

    page = asyncio.run(fetch("/page"))
    assert page.status == 200 and page.css("h1::text").get() == "members"
    assert sites.got("localhost", "/page") == ("s3cret", "session=abc")
    assert sites.got("localhost", "/image") == ("s3cret", "session=abc")  # what the page loads from its site
    landed = asyncio.run(fetch("/to/127.0.0.1/landed"))  # a redirect off the site
    assert landed.url == sites.url("127.0.0.1", "/landed") and sites.got("127.0.0.1", "/landed") == (None, None)
    # Nothing that went to the other site carried them: the image the page loads from it (which Chromium may
    # refuse to load at all, the page having no address it knows: Private Network Access) and the redirect.
    assert [(path, token, cookie) for host, path, token, cookie in sites.seen if host == "127.0.0.1"
            and (token or cookie)] == []  # fmt: skip


def _project(tmp_path, sites: _Sites) -> Project:
    quiet = {"header": "X-Token: ${WG_CLUB_TOKEN}", "max_pages": 1, "no_robots": True, "no_progress": True}
    data = {
        "credentials": {
            "club": ["WG_CLUB_TOKEN"],
            "shop_db": {"WG_PGPASSWORD": "${WG_SHOP_DB_PASSWORD}"},  # (the variable a job gets, from another)
        },
        "jobs": {
            "members": {"crawl": sites.url("localhost", "/page"), **quiet, "credentials": ["club"]},
            "prices": {"crawl": sites.url("localhost", "/prices"), **quiet, "credentials": "shop_db"},
        },
    }
    path = tmp_path / "wintergrab.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return Project(path)


def test_a_project_s_jobs_get_the_credentials_they_are_given_and_no_others(sites, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WG_CLUB_TOKEN", "s3cret")
    monkeypatch.setenv("WG_SHOP_DB_PASSWORD", "db-pass")
    monkeypatch.setenv("WINTERGRAB_TRIGGER_TOKEN", "t" * 20)
    monkeypatch.setenv("WG_HARMLESS", "yes")
    project = _project(tmp_path, sites)
    members, prices = project.jobs["members"], project.jobs["prices"]
    assert "X-Token: ${WG_CLUB_TOKEN}" in members.command()  # the job's command line: the name only
    given = project.environment(members)
    assert given["WG_CLUB_TOKEN"] == "s3cret" and given["WG_HARMLESS"] == "yes"
    assert not {"WG_SHOP_DB_PASSWORD", "WG_PGPASSWORD", "WINTERGRAB_TRIGGER_TOKEN"} & set(given)
    other = project.environment(prices)
    assert other["WG_PGPASSWORD"] == "db-pass" and not {"WG_CLUB_TOKEN", "WG_SHOP_DB_PASSWORD"} & set(other)

    result = project.run_job(members, log_file=tmp_path / "members.log")
    assert result.ok, (tmp_path / "members.log").read_text()
    assert sites.got("localhost", "/page") == ("s3cret", None)
    result = project.run_job(prices, log_file=tmp_path / "prices.log")  # not given the club's token: it cannot use it
    assert (
        not result.ok and "the environment variable WG_CLUB_TOKEN is not set" in (tmp_path / "prices.log").read_text()
    )
    assert not any(path == "/prices" for _, path, _, _ in sites.seen)

    monkeypatch.delenv("WG_SHOP_DB_PASSWORD")
    failed = project.run_job(prices, log_file=tmp_path / "missing.log")  # (its credentials cannot be read: not run)
    assert (failed.status, failed.exit_code) == ("failed", 2)
    assert (
        "credentials 'shop_db', WG_PGPASSWORD: the environment variable WG_SHOP_DB_PASSWORD is not set"
        in (tmp_path / "missing.log").read_text()
    )


def test_a_command_line_reads_the_variable_when_it_runs(monkeypatch) -> None:
    from wintergrab.cli import spider_from_command

    monkeypatch.setenv("WG_TEST_TOKEN", "first")
    command = ["crawl", "https://www.club.example/", "-H", "X-Token: ${WG_TEST_TOKEN}"]
    _, settings = spider_from_command(command)
    (club,) = settings["credentials"]
    assert (club.site, dict(club.headers)) == ("club.example", {"X-Token": "first"})
    monkeypatch.setenv("WG_TEST_TOKEN", "rotated")
    assert dict(spider_from_command(command)[1]["credentials"][0].headers) == {"X-Token": "rotated"}
    assert sys.modules["wintergrab.credentials"].site_of("http://127.0.0.1:8000/") == "127.0.0.1"


def test_add_cookies_takes_a_mapping(sites) -> None:
    # (it raised KeyError: 'secure', whatever it was given as {name: value})
    with Fetcher(impersonate=None, retries=0) as fetcher:
        fetcher.add_cookies({"theme": "dark"}, url=sites.url("localhost"))
        fetcher.get(sites.url("localhost", "/mine"))
        fetcher.get(sites.url("127.0.0.1", "/theirs"))
    assert sites.got("localhost", "/mine") == (None, "theme=dark") and sites.got("127.0.0.1", "/theirs") == (None, None)

    async def fetch() -> None:
        async with AsyncFetcher(impersonate=None, retries=0) as fetcher:
            fetcher.add_cookies({"theme": "light"}, domain="localhost")
            await fetcher.get(sites.url("localhost", "/async"))

    asyncio.run(fetch())
    assert sites.got("localhost", "/async") == (None, "theme=light")


def test_a_run_s_record_leaves_them_out_and_a_replay_needs_none(monkeypatch) -> None:
    from wintergrab.cli import spider_from_command
    from wintergrab.redact import redact_argv
    from wintergrab.runs import _replayable_argv

    argv = ["crawl", "https://club.example/", "-H", "Authorization: Bearer ${CLUB_TOKEN}", "--cookie",
            "session=abc", "--proxy", "http://crawler:hunter2@proxy.example:8080", "--max-pages", "5"]  # fmt: skip
    kept = redact_argv(argv)
    assert kept[3:8] == ["Authorization: ***", "--cookie", "session=***", "--proxy", "http://***@proxy.example:8080"]
    replayed = _replayable_argv(kept)  # (a replay reads the recorded pages: it sends no request)
    assert replayed == ["crawl", "https://club.example/", "--max-pages", "5"]
    monkeypatch.delenv("CLUB_TOKEN", raising=False)
    assert spider_from_command(replayed)[1].get("credentials") is None  # (it needs no variable)
