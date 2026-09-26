"""Runs: the registry, recording, and replay without the network."""

from __future__ import annotations

import json
from typing import Any

import pytest

from wintergrab import Spider
from wintergrab.errors import ConfigurationError
from wintergrab.fetchers.cache import HTTPCache
from wintergrab.fetchers.response import Response
from wintergrab.runs import RunRegistry, replay


class Books(Spider):
    """The test site's miniature books.toscrape.com: 12 books on 3 listing pages."""

    def parse(self, response: Response) -> Any:
        if "/catalogue/book-" in response.url:
            yield {"url": response.url, "title": response.css("h1::text").get(),
                   "price": response.css(".price_color::text").get()}  # fmt: skip
        for link in response.links(same_domain=True, allow=r"/books/"):
            yield response.follow(link)


class RenamedBooks(Books):
    """The same crawl after a change: the price now comes without its currency sign."""

    def parse(self, response: Response) -> Any:
        for output in super().parse(response):
            if isinstance(output, dict):
                output["price"] = output["price"].lstrip("£")
            yield output


def test_a_recorded_crawl_replays_without_the_network(fresh_site, tmp_path) -> None:
    workspace = tmp_path / "ws"
    spider = Books(start_urls=[fresh_site.url + "/books/"], record=True, run_registry=str(workspace),
                   log_level="WARNING", output=str(tmp_path / "books.jsonl"))  # fmt: skip
    result = spider.run()
    assert result.run_id == "run-1" and result.stats["items"] == 12
    run = RunRegistry(workspace).get("run-1")
    assert (run.status, run.recorded, run.name, run.output) == (
        "finished",
        True,
        "Books",
        str(tmp_path / "books.jsonl"),
    )
    assert run.recipe == {"spider": "test_runs:Books"} and run.settings["start_urls"] == [fresh_site.url + "/books/"]
    assert len(list(run.items())) == 12 and run.stats["pages"] == 16
    kinds = [e["event"] for e in run.events()]
    assert kinds[0] == "crawl_started" and kinds[-1] == "crawl_finished" and kinds.count("response") == 16
    assert all("latency" in e for e in run.events("response"))  # the timings
    assert len(HTTPCache(run.archive, mode="offline")) == 17  # every page, and robots.txt

    before = sum(fresh_site.site.hits.values())
    again = replay("run-1", Books, registry=workspace)
    assert again.same and again.missing == 0 and "the same 12 item(s) as recorded" in again.summary()
    assert sum(fresh_site.site.hits.values()) == before  # not one request
    assert again.output.parent == run.directory and len(again.output.read_text().splitlines()) == 12

    changed = replay("last", RenamedBooks, registry=workspace)
    assert not changed.same and changed.diff.counts == {"added": 0, "removed": 0, "changed": 12, "unchanged": 0}
    assert "price" in changed.summary() and sum(fresh_site.site.hits.values()) == before


def test_record_replay_and_runs_on_the_command_line(site, tmp_path, capsys) -> None:
    from wintergrab.cli import main

    workspace = str(tmp_path / "ws")
    schema = tmp_path / "book.schema.json"
    schema.write_text(json.dumps({"name": "book", "fields": {"name": {"type": "string", "selectors": ["h1"]},
                                  "price": "money", "url": "url"}}), encoding="utf-8")  # fmt: skip
    crawl = ["crawl", site.url + "/books/", "--allow", "/books/",
             "--extract", str(schema), "--unique-key", "url", "--no-progress", "-o", str(tmp_path / "out.jsonl")]  # fmt: skip
    assert main([*crawl, "--record", "--workspace", workspace]) == 0
    assert "kept as run-1; replay it with: wintergrab replay run-1" in capsys.readouterr().err
    assert main(["runs", "--workspace", workspace]) == 0
    listed = capsys.readouterr().out
    assert listed.startswith("run-1 ") and "[recorded]" in listed
    assert main(["runs", "run-1", "--workspace", workspace]) == 0
    assert "command: wintergrab crawl " + site.url + "/books/" in capsys.readouterr().out
    assert main(["replay", "run-1", "--workspace", workspace]) == 0
    written = len((tmp_path / "out.jsonl").read_text(encoding="utf-8").splitlines())
    assert written >= 12 and f"the same {written} item(s) as recorded" in capsys.readouterr().out

    # the schema changes: the replay says what it does to the data (exit status 1)
    schema.write_text(json.dumps({"name": "book", "fields": {"name": {"type": "string", "selectors": ["title"]},
                                  "price": "money", "url": "url"}}), encoding="utf-8")  # fmt: skip
    assert main(["replay", "last", "--workspace", workspace, "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["same"] is False and report["changed"] >= 12  # every name

    # without --record, a workspace keeps the run's record only: nothing to replay
    assert main([*crawl, "--workspace", workspace]) == 0
    assert "kept as run-2" in capsys.readouterr().err
    assert main(["replay", "run-2", "--workspace", workspace]) == 1
    assert "run-2 was not recorded" in capsys.readouterr().err
    assert main(["runs", "--remove", "run-2", "--workspace", workspace]) == 0
    assert [r.id for r in RunRegistry(workspace).runs()] == ["run-1"]


class TokenBooks(Books):
    api_token: str = "abc123"  # a credential the spider uses


def test_credentials_are_not_kept(site, tmp_path, capsys) -> None:
    from wintergrab.cli import main

    workspace = tmp_path / "ws"
    spider = TokenBooks(start_urls=[site.url + "/books/"], record=True, run_registry=str(workspace), log_level=None,
                        default_headers={"Authorization": "Bearer s3cr3t", "Accept-Language": "en"},
                        api_token="t0k3n", max_pages=4)  # fmt: skip
    spider.run()
    run = RunRegistry(workspace).get("last")
    text = (run.directory / "run.json").read_text(encoding="utf-8")
    assert "s3cr3t" not in text and "t0k3n" not in text
    assert run.settings["default_headers"] == {"Authorization": "***", "Accept-Language": "en"}
    assert run.settings["api_token"] == "***"
    assert replay(run, TokenBooks, registry=workspace).same  # the spider's own values stand in

    crawl = ["crawl", site.url + "/books/", "--allow", "/books/", "--max-pages", "3", "--no-progress", "-o",
             str(tmp_path / "out.jsonl"), "--record", "--workspace", str(workspace),
             "-s", 'default_headers={"Cookie": "sid=c00k1e"}']  # fmt: skip
    assert main(crawl) == 0
    run = RunRegistry(workspace).get("last")
    assert "c00k1e" not in (run.directory / "run.json").read_text(encoding="utf-8")
    assert 'default_headers={"Cookie": "***"}' in run.recipe["command"]
    capsys.readouterr()
    assert main(["replay", "last", "--workspace", str(workspace)]) == 0  # without the options left out


def test_redact_query() -> None:
    from wintergrab.redact import redact_query

    url = "https://www.googleapis.com/customsearch/v1?key=AIza123&cx=e1&q=laptop&access_token=t0k&page=2"
    assert (
        redact_query(url) == "https://www.googleapis.com/customsearch/v1?key=***&cx=e1&q=laptop&access_token=***&page=2"
    )
    assert (
        redact_query("failed: https://u:pw@h.example/?api_key=s3 (timeout)")
        == "failed: https://***@h.example/?api_key=*** (timeout)"
    )
    assert redact_query("https://shop.example/?sort=price&page=2") == "https://shop.example/?sort=price&page=2"


def test_the_fetchers_log_no_key(caplog) -> None:
    import logging

    from wintergrab import Fetcher
    from wintergrab.errors import FetchError

    with caplog.at_level(logging.INFO, logger="wintergrab"), Fetcher(retries=1, timeout=1) as fetcher:
        with pytest.raises(FetchError):
            fetcher.get("http://127.0.0.1:1/search?q=x&key=s3cr3t")  # refused: retried once, logged
    assert "retrying http://127.0.0.1:1/search?q=x&key=***" in caplog.text and "s3cr3t" not in caplog.text


def test_redact_argv() -> None:
    from wintergrab.redact import redact_argv

    argv = ["crawl", "https://a.example/?q=1", "--proxy", "http://u:pw@proxy.example:8080", "-H",
            "Authorization: Bearer z", "-H", "Accept: text/html", "--set=tokenizer=word", "--cookie", "sid=1"]  # fmt: skip
    assert redact_argv(argv) == ["crawl", "https://a.example/?q=1", "--proxy", "http://***@proxy.example:8080", "-H",
                                 "Authorization: ***", "-H", "Accept: text/html", "--set=tokenizer=word", "--cookie",
                                 "sid=***"]  # fmt: skip


def test_a_run_stopped_at_a_limit(fresh_site, tmp_path) -> None:
    workspace = tmp_path / "ws"
    result = Books(start_urls=[fresh_site.url + "/books/"], record=True, run_registry=str(workspace),
                   log_level="WARNING", max_pages=6, concurrency=4).run()  # fmt: skip
    assert result.status == "limit"
    again = replay(result.run_id, Books, registry=workspace)  # the recording says which pages
    assert again.same and again.missing > 0 and "went beyond the recording" in again.summary()


def test_goal_runs_replay_from_their_plan(fresh_site, tmp_path) -> None:
    from wintergrab.goals import parse_goal, plan_goal

    plan = plan_goal(parse_goal(f"books rated 4 stars or more on {fresh_site.url}/books/"), sample=15)
    result = plan.run(log_level="WARNING", progress=False, record=True, run_registry=str(tmp_path / "ws"))
    run = RunRegistry(tmp_path / "ws").get(result.crawl.run_id)
    assert run.name == "goal" and "goal_plan" in run.recipe and len(list(run.items())) == 4
    before = sum(fresh_site.site.hits.values())
    again = replay(run, registry=tmp_path / "ws")
    assert again.same and sum(fresh_site.site.hits.values()) == before


def test_the_registry(tmp_path) -> None:
    registry = RunRegistry(tmp_path / "ws")
    assert registry.runs() == []
    with pytest.raises(ConfigurationError, match="no run in"):
        registry.get("last")
    first = registry.create("shop", settings={"concurrency": 4}, recipe={"spider": "shop:Shop"})
    second = registry.create("shop")
    assert (first.id, second.id) == ("run-1", "run-2") and registry.get("last").id == "run-2"
    assert registry.get("1").settings == {"concurrency": 4} and registry.get(2).id == "run-2"
    assert "running" in registry.get("run-1").describe()
    with pytest.raises(ConfigurationError, match="no run 'run-9'"):
        registry.get("run-9")
    with pytest.raises(ConfigurationError, match="was not recorded"):
        replay("run-1", registry=registry)
    with pytest.raises(ConfigurationError, match="run_registry must be"):
        RunRegistry.coerce(3)


def test_an_empty_cache_is_a_cache(tmp_path) -> None:
    cache = HTTPCache(tmp_path / "c")  # no entry yet, so len() == 0
    assert Spider(cache=cache).http_cache() is cache


@pytest.mark.browser
def test_pages_a_browser_gave_replay_too(fresh_site, tmp_path) -> None:
    class Spa(Spider):
        use_browser = True

        def parse(self, response: Response) -> Any:
            yield {"url": response.url, "rows": response.css(".row::text").getall()}

    urls = [fresh_site.url + f"/spa?n={i}" for i in (1, 2)]
    result = Spa(start_urls=urls, record=True, run_registry=str(tmp_path / "ws"), log_level="WARNING").run()
    assert result.stats["browser_pages"] == 2 and len(list(RunRegistry(tmp_path / "ws").get("last").items())) == 2
    before = sum(fresh_site.site.hits.values())
    assert replay(result.run_id, Spa, registry=tmp_path / "ws").same
    assert sum(fresh_site.site.hits.values()) == before


def test_load_spider_by_module_or_file(tmp_path) -> None:
    from wintergrab.runs import load_spider

    assert load_spider("wintergrab.spider:Spider") is Spider
    (tmp_path / "shop.py").write_text("from wintergrab import Spider\n\nclass Shop(Spider):\n    name = 'shop'\n")
    assert load_spider(f"{tmp_path / 'shop.py'}:Shop").name == "shop"
    with pytest.raises(ConfigurationError, match=r"'module:Class' or 'file\.py:Class'"):
        load_spider("no-class-here")
