"""Extraction templates: ready-made schemas for common kinds of records, used by name."""

from __future__ import annotations

import json
from typing import Any

import pytest

from wintergrab.extraction import Extractor
from wintergrab.extraction.templates import schema_named, template, template_names
from wintergrab.intel import classify_page
from wintergrab.parser import Selector


def page(body: str, *, data: dict[str, Any] | None = None, title: str = "Page", url: str) -> Selector:
    script = f"<script type='application/ld+json'>{json.dumps(data)}</script>" if data else ""
    return Selector(f"<html><head><title>{title}</title>{script}</head><body>{body}</body></html>", url=url)


def values(name: str, sel: Selector) -> dict[str, Any]:
    record = Extractor(name).extract(sel)
    return {field: fv.value for field, fv in record.fields.items() if fv.value is not None}


def test_a_job_an_event_and_an_article() -> None:
    job = page("<h1>Data engineer</h1><p>Join us.</p>", url="https://jobs.example/jobs/42", data={
        "@context": "https://schema.org", "@type": "JobPosting", "title": "Data engineer",
        "hiringOrganization": {"@type": "Organization", "name": "Acme"}, "datePosted": "2026-09-01",
        "employmentType": "FULL_TIME", "jobLocation": {"@type": "Place", "address": {"addressLocality": "Berlin"}},
        "baseSalary": {"@type": "MonetaryAmount", "currency": "EUR", "value": {"value": 70000}}})  # fmt: skip
    got = values("job", job)
    assert got["title"] == "Data engineer" and got["company"] == "Acme" and got["location"] == "Berlin"
    assert got["date_posted"] == "2026-09-01" and (got["salary"], got["currency"]) == (70000, "EUR")

    event = page("<h1>Jazz night</h1>", url="https://events.example/events/jazz", data={
        "@context": "https://schema.org", "@type": "MusicEvent", "name": "Jazz night",
        "startDate": "2026-10-03T20:00:00+02:00", "location": {"@type": "Place", "name": "Blue Room",
        "address": {"addressLocality": "Lyon"}}, "performer": {"@type": "Person", "name": "Trio K"}})  # fmt: skip
    got = values("event", event)
    assert (got["name"], got["venue"], got["city"], got["performer"]) == ("Jazz night", "Blue Room", "Lyon", "Trio K")
    assert got["start_date"].startswith("2026-10-03T20:00")

    article = page("<h1>Rates rise</h1><p>By Ann Lee</p>", url="https://news.example/2026/09/rates", data={
        "@context": "https://schema.org", "@type": "NewsArticle", "headline": "Rates rise",
        "author": {"@type": "Person", "name": "Ann Lee"}, "datePublished": "2026-09-20T08:00:00Z",
        "keywords": ["economy", "rates"]})  # fmt: skip
    got = values("news", article)  # (an alias)
    assert got["title"] == "Rates rise" and got["author"] == "Ann Lee" and got["tags"] == ["economy", "rates"]


def test_a_property_listing() -> None:
    listing = page(
        "<h1>Bright 3-room flat by the park</h1><p>€ 420,000</p><ul><li>3 bedrooms</li><li>2 bathrooms</li>"
        "<li>96 m²</li></ul><p>Rue du Parc 4, 69003 Lyon</p>",
        url="https://homes.example/for-sale/lyon-3-room-flat-8812",
        data={"@context": "https://schema.org", "@type": "RealEstateListing", "name": "Bright 3-room flat by the park",
              "offers": {"@type": "Offer", "price": 420000, "priceCurrency": "EUR"},
              "about": {"@type": "Apartment", "numberOfBedrooms": 3, "numberOfBathroomsTotal": 2,
                        "floorSize": {"@type": "QuantitativeValue", "value": 96, "unitCode": "MTK"},
                        "address": {"@type": "PostalAddress", "streetAddress": "Rue du Parc 4",
                                    "addressLocality": "Lyon", "postalCode": "69003"}}},
    )  # fmt: skip
    got = values("real-estate", listing)
    assert got["name"] == "Bright 3-room flat by the park" and (got["price"], got["currency"]) == (420000, "EUR")
    assert (got["bedrooms"], got["bathrooms"], got["floor_size"]) == (3, 2, {"value": 96, "unit": "m2"})
    assert got["address"]["city"] == "Lyon" and got["address"]["street"] == "Rue du Parc 4"
    assert classify_page(listing).type == "property"

    plain = page("<h1>Garden house</h1><p>Price: $515,000</p><p>4 bedrooms, 2.5 baths, 2,100 sq ft</p>",
                 title="Garden house", url="https://homes.example/property/garden-house")  # fmt: skip
    assert classify_page(plain).type == "property"  # its address and its wording say so
    got = values("property", plain)  # no structured data: what the text says
    assert got["name"] == "Garden house" and (got["price"], got["currency"]) == (515000, "USD")
    assert (got["bedrooms"], got["bathrooms"], got["floor_size"]) == (4, 2.5, {"value": 2100, "unit": "ft2"})


def test_a_documentation_page_and_a_recipe() -> None:
    docs = page(
        "<nav>Guides</nav><article><h1>Configuring retries</h1><p>Retries are tried again after a delay.</p>"
        "<pre><code>retries = 3</code></pre><pre><code>retry_statuses = [503]</code></pre>"
        "<pre><code>backoff = 2</code></pre></article>",
        url="https://docs.example/docs/retries",
        data={"@context": "https://schema.org", "@type": "TechArticle", "headline": "Configuring retries",
              "articleSection": "Guides", "dateModified": "2026-08-30T10:00:00Z"},
    )  # fmt: skip
    got = values("docs", docs)
    assert got["title"] == "Configuring retries" and got["section"] == "Guides"
    assert got["modified"].startswith("2026-08-30")
    assert classify_page(docs).type == "documentation"

    recipe = page("<h1>Lentil soup</h1>", url="https://food.example/recipes/lentil-soup", data={
        "@context": "https://schema.org", "@type": "Recipe", "name": "Lentil soup", "totalTime": "PT45M",
        "recipeIngredient": ["200 g lentils", "1 onion"], "recipeYield": "4 servings"})  # fmt: skip
    got = values("recipe", recipe)
    assert got["name"] == "Lentil soup" and got["ingredients"] == ["200 g lentils", "1 onion"]
    assert got["total_time"] == 2700


def test_templates_are_schemas_with_a_required_identity() -> None:
    assert template_names()[:3] == ["product", "article", "job"] and "property" in template_names()
    for name in template_names():
        schema = template(name)
        required = [f.name for f in schema.fields if f.required]
        assert len(required) == 1 and schema.name == name
    assert [f.name for f in template("review").fields if f.required] == ["text"]
    assert template("Real Estate").name == "property" and template("restaurants".rstrip("s")).name == "place"
    with pytest.raises(KeyError, match="no template 'spaceship'"):
        template("spaceship")
    empty = Selector("<html><body><p>Nothing here</p></body></html>", url="https://s.example/x")
    assert Extractor("event").extract(empty).fields["name"].validation == "missing"  # no record


def test_names_work_where_schema_files_do(tmp_path, site, capsys, monkeypatch) -> None:
    from wintergrab.cli import main

    monkeypatch.chdir(tmp_path)
    assert schema_named("product").name == "product"
    (tmp_path / "product").write_text(json.dumps({"name": "mine", "fields": {"title": "string"}}), encoding="utf-8")
    assert schema_named("product").name == "mine"  # a file of that name comes first
    (tmp_path / "product").unlink()

    url = site.url + "/books/catalogue/book-2/index.html"
    assert main(["get", url, "--extract", "product"]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["name"] == "Book number 2" and record["price"] > 0 and record["currency"] == "GBP"
    assert main(["templates", "job"]) == 0 and json.loads(capsys.readouterr().out)["name"] == "job"
    assert main(["templates", "nope"]) == 1
