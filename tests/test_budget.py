"""Crawl budgets: hard limits that stop a crawl, and the soft limit that focuses it."""

from __future__ import annotations

import pytest

from wintergrab import Spider
from wintergrab.errors import ConfigurationError
from wintergrab.spider.budget import BudgetMonitor


class Links(Spider):
    """Crawls /links (30 item links) one request at a time."""

    log_level = None
    obey_robots_txt = False
    autothrottle = False
    concurrency = 1

    def parse(self, response):
        yield {"url": response.url}
        yield from response.follow_all(allow=r"/item/")


def links(site, **settings):
    return Links(start_urls=[site.url + "/links?n=30"], **settings)


def test_max_requests_is_exact(fresh_site) -> None:
    result = links(fresh_site, max_requests=7).run()
    assert result.status == "limit" and result.limit_reason == "max_requests"
    assert result.stats["requests"] == 7
    assert sum(fresh_site.site.hits.values()) == 7


def test_max_requests_counts_retries(fresh_site) -> None:
    class Flaky(Links):
        retries = 5

    result = Flaky(start_urls=[fresh_site.url + "/flaky/x?fail=9"], max_requests=3).run()
    assert result.limit_reason == "max_requests" and fresh_site.site.hits["/flaky/x"] == 3


def test_max_bytes(fresh_site) -> None:
    result = links(fresh_site, max_bytes=2_000).run()
    assert result.limit_reason == "max_bytes"
    assert 2_000 <= result.stats["bytes"] < 2_000 + 1_000  # stops right after crossing it
    assert result.stats["pages"] < 31


def test_max_runtime_across_resumed_runs(fresh_site, tmp_path) -> None:
    class Slow(Links):
        def parse(self, response):
            yield {"url": response.url}

    start = [fresh_site.url + f"/item/{i}?delay=0.3" for i in range(20)]
    crawl_dir = str(tmp_path / "crawl")
    first = Slow(start_urls=start, max_runtime=1.0, crawl_dir=crawl_dir).run()
    assert first.status == "limit" and first.limit_reason == "max_runtime"
    done = first.stats["pages"]
    assert 2 <= done < 20
    # The budget counts the time of earlier runs: a small increase buys a little more.
    second = Slow(start_urls=start, max_runtime=100, crawl_dir=crawl_dir).run()
    assert second.status == "finished" and second.stats["pages"] == 20


def test_max_errors_and_error_rate(fresh_site) -> None:
    class Failing(Links):
        retries = 0

        def on_error(self, request, error):
            pass

    urls = [fresh_site.url + f"/status/500?n={i}" for i in range(10)]
    result = Failing(start_urls=urls, max_errors=3).run()
    assert result.limit_reason == "max_errors" and result.stats["failed"] == 3
    mixed = [fresh_site.url + f"/status/{500 if i % 2 else 200}?n={i}" for i in range(40)]
    rated = Failing(start_urls=mixed, max_error_rate=0.3, error_rate_min_pages=10).run()
    assert rated.limit_reason == "max_error_rate"
    assert rated.stats["pages"] < 40


def test_max_output_bytes(fresh_site, tmp_path) -> None:
    out = tmp_path / "items.jsonl"
    result = links(fresh_site, max_output_bytes=300, output=str(out)).run()
    assert result.limit_reason == "max_output_bytes"
    assert 300 <= out.stat().st_size < 3_000


def test_soft_limit_keeps_the_budget_for_important_pages(fresh_site) -> None:
    class Focused(Links):
        def parse(self, response):
            yield {"url": response.url}
            for request in response.follow_all(allow=r"/item/"):
                # every third item is important
                request.priority = 5 if request.url.endswith(("/0", "/3", "/6", "/9")) else 0
                yield request

    result = Focused(
        start_urls=[fresh_site.url + "/links?n=12"], max_requests=6, budget_soft_limit=0.5, budget_soft_priority=5
    ).run()
    fetched = {i["url"].rsplit("/", 1)[-1] for i in result.items}
    assert {"0", "3", "6", "9"} <= fetched  # the important pages made it into the budget
    assert result.stats["requests"] <= 6


def test_budget_usage_in_metrics(fresh_site) -> None:
    result = links(fresh_site, max_requests=5, max_runtime=60).run()
    budget = result.metrics["budget"]
    assert budget["max_requests"] == {"used": 5.0, "limit": 5.0, "fraction": 1.0}
    assert 0 < budget["max_runtime"]["fraction"] < 1


@pytest.mark.parametrize(
    ("setting", "value"),
    [("max_requests", 0), ("max_bytes", -1), ("max_error_rate", 1.5), ("max_runtime", "soon"), ("max_errors", True)],
)
def test_invalid_budgets_are_rejected(setting: str, value: object) -> None:
    with pytest.raises(ConfigurationError) as info:
        BudgetMonitor(Links(**{setting: value}))
    assert info.value.key == setting


def test_invalid_soft_limit() -> None:
    with pytest.raises(ConfigurationError):
        BudgetMonitor(Links(budget_soft_limit=1.5))
