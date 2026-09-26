"""Goals: requests in plain words, plans with estimates, and runs that collect the records."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from wintergrab.errors import ConfigurationError
from wintergrab.fetchers.response import Response
from wintergrab.goals import GoalPlan, parse_goal, path_pattern, plan_goal
from wintergrab.goals.plan import _pattern_regex
from wintergrab.goals.run import _url_regex
from wintergrab.intel.profile import SiteProfiler
from wintergrab.intel.survey import SitemapRead, SiteSurvey
from wintergrab.sitemaps import SitemapEntry

NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)


def read(text: str, **options):
    return parse_goal(text, now=NOW, **options)


@pytest.mark.parametrize(
    ("text", "entity", "fields", "filters"),
    [
        # the examples of the project's own brief
        ("Crawl these websites and extract product names, prices, ratings and stock status.", "product",
         ["name", "price", "currency", "rating", "availability", "url"], []),
        ("Find all publicly accessible products from these sites, normalize their prices, remove duplicates "
         "and give me a clean dataset.", "product", ["name", "price", "currency", "availability", "url"], []),
        ("Find all public restaurant listings in this site and extract name, address, phone, rating and website",
         "place", ["name", "address", "telephone", "rating", "website", "url"], []),
        ("Extract every product and its price.", "product", ["name", "price", "currency", "url"], []),
        ("Find all articles published after 2025-01-01.", "article", ["title", "author", "published", "url"],
         ["date(published) > '2025-01-01'"]),
        ("Find all companies and their public business contact details.", "company",
         ["name", "telephone", "email", "address", "url"], []),
        # conditions
        ("Find all in stock products between €100 and €300", "product",
         ["name", "price", "currency", "availability", "url"],
         ["price >= 100 and (currency is None or currency == 'EUR')",
          "price <= 300 and (currency is None or currency == 'EUR')", "availability == 'InStock'"]),
        ("Show me all products rated 4 stars or more", "product",
         ["name", "price", "currency", "availability", "url", "rating"], ["rating >= 4"]),
        ("jobs posted in the last 30 days with title, company, salary and location", "job",
         ["title", "company", "salary", "currency", "location", "date_posted", "url"],
         ["date(date_posted) >= '2026-08-27'"]),
        ("events in Berlin this month with name, date and venue", "event",
         ["name", "start_date", "venue", "city", "url"],
         ["date(start_date) >= '2026-09-01'", "icontains(city, 'Berlin')"]),
        ("news articles published in 2025 with headline, author and date", "article",
         ["title", "author", "published", "url"],
         ["date(published) >= '2025-01-01'", "date(published) < '2026-01-01'"]),
        ("Find all products that cost up to 50 dollars", "product",
         ["name", "price", "currency", "availability", "url"],
         ["price <= 50 and (currency is None or currency == 'USD')"]),
        ("Collect all recipes with ingredients and cooking time", "recipe",
         ["name", "ingredients", "total_time", "url"], []),
    ],
)  # fmt: skip
def test_requests_become_goals(text: str, entity: str, fields: list[str], filters: list[str]) -> None:
    goal = read(text)
    assert goal.entity == entity
    assert goal.fields == fields
    assert [f.expression for f in goal.filters] == filters


def test_what_else_a_request_says() -> None:
    goal = read("Find all laptops under $1000 on shop.example with name, price and rating")
    assert goal.sites == ["https://shop.example"] and goal.scope == ["laptops"]
    assert goal.entity == "product"  # from the fields: no kind of record named
    assert goal.filters[0].text == "under $1000"
    goal = read("Get the first 50 jobs from https://jobs.example.com/it/ with title and company")
    assert goal.limit == 50 and goal.sites == ["https://jobs.example.com/it/"]
    assert read("Monitor daily the prices of all products on shop.example").monitor == "daily"
    assert read("Track changes in product availability").monitor == "yes"
    assert read("products on a.example", sites=["b.example"]).sites == ["https://a.example", "https://b.example"]
    unknown = read("Find all dentists with name, phone number and number of chairs")
    assert unknown.entity == "place" and "number_of_chairs" in unknown.fields
    assert any("'number of chairs' is not a field" in note for note in unknown.notes)
    assert read("Find all products on shop.example").notes == [
        "no fields named: name, price, currency, availability, url"
    ]
    assert read("everything on shop.example").notes[0].startswith("no kind of record named")
    described = read("Find all laptops under $1000 on shop.example with name and price").describe()
    assert "products with name, price, currency, url" in described and "where price < 1000" in described
    with pytest.raises(ConfigurationError):
        read("   ")


def test_a_model_can_read_the_request() -> None:
    def model(text: str) -> dict:
        return {
            "entity": "job",
            "fields": ["title", "salary"],
            "filters": ["salary > 50000"],
            "sites": ["jobs.example"],
        }

    goal = parse_goal("well paid jobs", parser=model, sites=["more.example"])
    assert goal.entity == "job" and goal.fields == ["title", "salary", "url"]
    assert goal.sites == ["https://jobs.example", "https://more.example"]
    assert goal.filters[0].expression == "salary > 50000"
    with pytest.raises(ConfigurationError, match="unknown entity"):
        parse_goal("x", parser=lambda text: {"entity": "spaceship"})
    with pytest.raises(ConfigurationError, match="invalid condition"):
        parse_goal("x", parser=lambda text: {"entity": "job", "filters": ["import os"]})


def test_path_patterns() -> None:
    assert path_pattern(["https://a.example/p/phone-x"]) == "/p/*"
    assert path_pattern(["https://a.example/books/b-1/index.html"]) == "/books/*/index.html"
    assert path_pattern(["https://a.example/p/1", "https://a.example/p/2", "https://a.example/q/3/x"]) == "/p/*"
    assert path_pattern(["https://a.example/c/phones/1", "https://a.example/c/laptops/2"]) == "/c/*/*"
    assert _pattern_regex("/p/*").match("/p/phone-x/") and not _pattern_regex("/p/*").match("/p/a/b")
    assert _pattern_regex("/books/**").match("/books/catalogue/page-2.html") and _pattern_regex("/books/**").match(
        "/books"
    )
    assert not _pattern_regex("/books/**").match("/booksellers")
    import re

    assert re.match(_url_regex("/p/*"), "https://a.example/p/1?ref=x")
    assert not re.match(_url_regex("/p/*"), "https://a.example/p/1/reviews")
    assert re.match(_url_regex("/books/**"), "https://a.example/books/catalogue/b-1/index.html")


def _page(url: str, body: str, head: str = "") -> Response:
    html = f"<html><head><title>t</title>{head}</head><body>{body}</body></html>"
    return Response(url, headers={"content-type": "text/html"}, body=html.encode(), elapsed=0.2)


def _survey(robots: str | None = None) -> SiteSurvey:
    """A made-up survey of a shop: three product pages and a listing, a sitemap of 40 products."""
    site = "https://shop.example"
    ld = '<script type="application/ld+json">{"@type": "Product", "name": "Phone %d", "offers": {"price": "%d", "priceCurrency": "USD"}}</script>'
    pages = [
        _page(f"{site}/p/phone-{i}", f"<h1>Phone {i}</h1><span class='price'>${100 * i}</span>", ld % (i, 100 * i))
        for i in range(1, 4)
    ]
    cards = "".join(
        f"<div class='card'><a href='/p/phone-{i}'>Phone {i}</a><span class='price'>${100 * i}</span></div>"
        for i in range(1, 9)
    )
    pages.append(_page(f"{site}/c/phones", f"<h1>Phones</h1>{cards}"))
    profiler = SiteProfiler()
    for page in pages:
        profiler.observe(page)
    profiler.add_robots(robots, found=robots is not None)
    entries = [SitemapEntry(f"{site}/p/phone-{i}") for i in range(1, 41)] + [SitemapEntry(f"{site}/about")]
    profiler.add_sitemaps(1, 0, entries)
    return SiteSurvey(
        url=site + "/",
        profile=profiler.profile(),
        robots_text=robots,
        robots_found=robots is not None,
        sitemaps=SitemapRead(roots=[f"{site}/sitemap.xml"], sitemaps=1, entries=entries),
        pages=pages,
        stats={"pages": len(pages)},
    )  # fmt: skip


def test_a_plan_from_a_survey() -> None:
    goal = read("Find all products under $250 on shop.example with name and price")
    plan = plan_goal(goal, surveys={"https://shop.example": _survey("User-agent: *\nCrawl-delay: 2\n")})
    site = plan.sites[0]
    assert (
        site.strategy == "sitemap"
        and site.target == ["/p/*"]
        and site.sitemap_urls == ["https://shop.example/sitemap.xml"]
    )
    assert site.sample["record_pages"] == 3 and site.sample["listing_pages"] == 1  # the card grid is a listing
    assert site.sample["fields"] == {"name": 3, "price": 3} and site.sample["passing"] == 2
    estimate = site.estimate
    assert estimate.pages == 40 and estimate.exact and estimate.requests == 42  # + robots.txt and the sitemap
    assert estimate.records == round(40 * 2 / 3)
    assert estimate.seconds == pytest.approx(42 * 2)  # the crawl delay sets the pace
    assert any("crawl delay of 2 s" in basis for basis in estimate.basis)
    text = plan.describe()
    assert "Fetch the 40 product pages the sitemaps list (/p/*), over HTTP." in text
    assert "robots.txt allows crawling (crawl delay 2 s); 1 sitemap(s) list 41 pages." in text
    assert "requests: 42 (none in a browser)" in text

    closed = plan_goal(goal, surveys={"https://shop.example": _survey("User-agent: *\nDisallow: /\n")})
    assert not closed.sites[0].allowed and "stay out of the whole site" in closed.sites[0].warnings[0]
    assert closed.run(log_level=None).counts["records"] == 0  # nothing is fetched

    with pytest.raises(ConfigurationError, match="names no site"):
        plan_goal(read("products with name and price"))


def test_plans_run_and_replay(site, tmp_path) -> None:
    goal = read("Find all products under $20 with name and price", sites=[site.url])
    plan = plan_goal(goal, sample=12)
    shop = plan.sites[0]
    assert shop.strategy == "sitemap" and "/product/*" in shop.target
    result = plan.run(log_level=None)
    assert result.counts["records"] == 5 and all(r["price"] < 20 for r in result.records)
    assert result.counts["filtered"] == 3  # the pages without a price do not meet the condition
    assert "5 record(s)" in result.summary() and "fields found: name 100%, price 100%" in result.summary()

    books = read("Find all books rated 4 stars or more with title, price and rating", sites=[site.url + "/books/"])
    plan = plan_goal(books, sample=15)
    catalogue = plan.sites[0]
    assert catalogue.strategy == "follow" and catalogue.sections == ["/books"]
    assert catalogue.target == ["/books/catalogue/*/index.html"] and "/books/**" in catalogue.follow
    saved = tmp_path / "books.plan.json"
    plan.save(saved)
    again = GoalPlan.load(saved)
    assert again.goal.filters[0].expression == "rating >= 4" and again.sites[0].target == catalogue.target
    out = tmp_path / "books.jsonl"
    result = again.run(str(out), log_level=None)
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == result.counts["records"] == 4
    assert all(r["rating"] >= 4 and "/books/catalogue/" in r["url"] for r in rows)
    assert result.found["rating"] == 4
    limited = plan_goal(read("the first 2 books with title and price", sites=[site.url + "/books/"]), sample=15)
    assert limited.run(log_level=None).counts["records"] == 2


def test_the_whole_loop_in_one_run(site, tmp_path, capsys) -> None:
    """provenance=True, heal=DIR: records say where each value came from, the plan's schema is read by a
    self-healing extractor kept in DIR (a fixture per site, questions in DIR/review.jsonl), and a redesign is
    repaired the next time the plan runs."""
    from wintergrab.cli import main
    from wintergrab.extraction.healing import ExtractorVersions
    from wintergrab.extraction.review import ReviewQueue

    schema = tmp_path / "product.schema.json"
    fields = {"name": {"type": "string", "selectors": ["h1"]}, "price": {"type": "money", "selectors": ["p.price"]}}
    schema.write_text(json.dumps({"name": "product", "fields": {**fields, "url": "url"}}), encoding="utf-8")
    heal = tmp_path / "ext"
    products = plan_goal(read("products with name and price", sites=[site.url + "/products/page/1"]), sample=15)
    products.schema = str(schema)
    out = tmp_path / "products.jsonl"
    result = products.run(str(out), log_level=None, provenance=True, heal=str(heal))
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert result.counts["records"] == len(rows) == 20
    where = rows[0]["_provenance"]
    assert where["url"] == rows[0]["url"] and where["extractor"] == "product@1" and "fetched_at" in where
    assert where["fields"]["price"]["method"] == "selector"  # (the evidence per field, as the extractor keeps it)
    versions = ExtractorVersions(heal)
    assert versions.active_number == 1 and versions.load_state()["fields"]["price"]["baseline"] == 1.0
    fixtures = versions.fixtures()  # the site's first complete record, as later versions must read it
    assert len(fixtures) == 1 and fixtures[0].by == "goal" and "/product/" in fixtures[0].url
    assert fixtures[0].expected["name"].startswith("Product") and fixtures[0].expected["price"]["currency"] == "USD"
    assert versions.check_fixtures() == []
    assert (result.extractor, result.extractor_version, result.review) == (str(heal), 1, str(heal / "review.jsonl"))
    assert result.counts["fixtures"] == 1 and result.counts["repairs"] == 0 and result.counts["questions"] == 0
    assert f"self-healing extractor {heal}: version 1, 1 regression fixture(s) kept" in result.summary()

    # "the redesign": the books section, whose prices are p.price_color; the plan runs again, on the same extractor
    books = plan_goal(read("books with title and price", sites=[site.url + "/books/"]), sample=15)
    books.schema = str(schema)
    books.save(tmp_path / "books.plan.json")
    again = books.run(str(tmp_path / "books.jsonl"), log_level=None, provenance=True, heal=str(heal))
    assert again.counts["records"] > 10 and again.counts["repairs"] == 1 and again.counts["fixtures"] == 0
    assert ExtractorVersions(heal).active.reason == "repair of price: p.price -> .price_color (anchored)"
    assert ", 1 repair(s) this run" in again.summary() and "question(s)" not in again.summary()
    assert ReviewQueue(heal / "review.jsonl").pending() == []  # (nothing it could not decide)
    # the command: the same, and the summary says what the extractor did
    assert main(["goal", "--plan", str(tmp_path / "books.plan.json"), "--yes", "-o", str(tmp_path / "b.jsonl"),
                 "--provenance", "--heal", str(heal)]) == 0  # fmt: skip
    err = capsys.readouterr().err
    assert f"self-healing extractor {heal}: version 2" in err
    assert main(["goal", "--plan", str(tmp_path / "books.plan.json"), "--yes", "--review", "r.jsonl"]) == 2
    assert "--review needs --heal" in capsys.readouterr().err
    with pytest.raises(ConfigurationError, match="review needs heal"):
        books.run(log_level=None, review=str(tmp_path / "r.jsonl"))


def test_goal_command(site, tmp_path, capsys) -> None:
    from wintergrab.cli import main

    request = [
        "goal",
        "books rated 4 stars or more with title and price",
        "--site",
        site.url + "/books/",
        "--sample",
        "15",
    ]
    assert main([*request, "--json"]) == 0
    printed = capsys.readouterr()
    assert json.loads(printed.out)["goal"]["entity"] == "product" and "Understood: products" in printed.err
    out = tmp_path / "books.jsonl"
    assert main([*request, "--confirm-over", "1", "-o", str(out)]) == 0  # big for the threshold: not run
    assert "add --yes to run it" in capsys.readouterr().err and not out.exists()
    assert (
        main(
            [
                *request,
                "--confirm-over",
                "1",
                "--yes",
                "-o",
                str(out),
                "--explain",
                "--save-plan",
                str(tmp_path / "p.json"),
            ]
        )
        == 0
    )
    printed = capsys.readouterr()
    assert "What the estimates rest on:" in printed.err and "4 record(s)" in printed.err
    assert len(out.read_text(encoding="utf-8").splitlines()) == 4
    assert main(["-q", "goal", "--plan", str(tmp_path / "p.json"), "--yes"]) == 0  # records on stdout
    assert len(capsys.readouterr().out.splitlines()) == 4
    assert main(["goal", "products with name and price"]) == 2  # no site
    assert "which site?" in capsys.readouterr().err
