"""WinterGrab: one entry point with shared settings, over goals, pages and sites."""

from __future__ import annotations

import asyncio
import json

import pytest

from wintergrab import NetworkPolicy, NetworkPolicyError, WinterGrab
from wintergrab.goals import GoalPlan
from wintergrab.intel import read_sitemaps


def test_from_a_goal_to_records(site, tmp_path) -> None:
    wg = WinterGrab(log_level=None)
    plan = wg.plan("Find all products under $20 with name and price", sites=[site.url], sample=12)
    assert isinstance(plan, GoalPlan) and plan.sites[0].strategy == "sitemap"
    result = wg.run(plan)
    assert result.counts["records"] == 5 and all(r["price"] < 20 for r in result.records)
    assert asyncio.run(wg.arun(plan)).counts["records"] == 5  # the same from async code
    out = tmp_path / "books.jsonl"
    wg.run("the first 2 books with title and price", str(out), sites=[site.url + "/books/"], sample=15)
    names = [json.loads(line)["name"] for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(names) == 2 and all(name.startswith("Book number") for name in names)  # books are products


def test_pages_and_sites(site) -> None:
    wg = WinterGrab(log_level=None)
    page = wg.get(site.url + "/product/1")
    record = wg.extract(page, "product")  # a template's name is a schema
    assert (record["name"], record["price"]) == ("Product 1", 6.25)
    assert len(wg.extract(site.url + "/books/", "product", all=True)) == 4  # a URL is fetched first
    sources = wg.sources(site.url + "/books/")
    assert sources.richest() == ("html", "article.product_pod", 4, 5)
    survey = wg.inspect(site.url, pages=5)
    assert survey.robots_found and survey.profile.pages == 5


def test_settings_hold_everywhere(site) -> None:
    guarded = WinterGrab(network_policy="public", log_level=None)  # loopback, like any private address, refused
    with pytest.raises(NetworkPolicyError):
        guarded.get(site.url)
    # planning too: the survey reads robots.txt and sitemaps through the policy, and says why it could not
    plan = guarded.plan("Find all products with name and price", sites=[site.url], sample=3)
    assert "robots.txt could not be read (Blocked by network policy: 127.0.0.1 is a loopback address)" in (
        plan.describe()
    )
    assert plan.sites[0].strategy == "follow"  # no sitemap was read
    # a site's robots.txt (or sitemap index) may name sitemaps anywhere: they are read through the policy too
    port = site.url.rsplit(":", 1)[1]
    robots = f"Sitemap: http://localhost:{port}/sitemap.xml"
    assert read_sitemaps(site.url, robots).sitemaps >= 1
    refused = NetworkPolicy(allow_loopback=True, denied_hosts=["localhost"])
    assert read_sitemaps(site.url, robots, network_policy=refused).sitemaps == 0
    lenient = guarded.configure(network_policy=None, concurrency=2)
    assert lenient.get(site.url).ok and lenient.settings == {"concurrency": 2} and guarded.network_policy == "public"
    assert repr(guarded) == "WinterGrab(network_policy='public')"


def test_a_model_reads_the_goal() -> None:
    class Model:
        name = "scripted"

        def complete(self, prompt: str, *, json_output: bool = False) -> str:
            return json.dumps({"entity": "job", "fields": ["title", "salary"], "sites": ["jobs.example"]})

    goal = WinterGrab(model=Model()).goal("well paid jobs on jobs.example")
    assert (goal.kind.name, goal.fields, goal.notes[-1]) == ("job", ["title", "salary", "url"], "read by scripted")
    assert WinterGrab().goal("products with name and price on shop.example").kind.name == "product"  # no model
    loaded = WinterGrab(model="ollama:llama3.1")
    assert type(loaded._model_object()).__name__ == "Ollama"  # "provider:name", loaded when first needed
