"""Templates: ready-made extraction schemas for common kinds of records.

::

    wintergrab get https://jobs.example/role/42 --extract job
    wintergrab crawl https://shop.example/ --extract product -o products.jsonl
    wintergrab templates product > product.schema.json      # to start your own from

    Extractor("event").extract(page)

======================  ===========================================================================
Template                Fields
======================  ===========================================================================
``product``             name, price, list_price, currency, availability, rating, review_count, brand,
                        sku, gtin, category, image, description, url
``article`` (news)      title, author, published, modified, description, section, tags, image, body, url
``job``                 title, company, location, salary, currency, employment_type, remote,
                        date_posted, valid_through, description, url
``event``               name, start_date, end_date, venue, city, address, price, currency, organizer,
                        performer, description, image, url
``company`` (business)  name, website, telephone, email, address, city, country, description,
                        founded, employees, industry, url
``place`` (restaurant)  name, address, street, city, postal_code, country, telephone, email, website,
                        rating, review_count, opening_hours, price_range, cuisine, ...
``person`` (profile)    name, job_title, organization, email, telephone, location, image, description, url
``review``              author, rating, date, title, text, item, url
``recipe``              name, ingredients, total_time, servings, calories, rating, author, image, url
``property``            name, price, currency, address, city, bedrooms, bathrooms, rooms, floor_size
(real estate)           (with its unit), year_built, latitude, longitude, description, image, url
``documentation``       title, description, section, body, modified, url
======================  ===========================================================================

A template's identifying field (the record's name or title; a review's text) is required: a page
without it gives no record. Every other field is filled when the page states it: its structured
data first, then meta tags, labels and layout. To keep only the pages of one kind in a crawl,
follow only their links (``--follow``), or use a goal, which plans that. The same kinds are the
records goals ask for (:mod:`wintergrab.goals`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..data.schema import Schema, load_schema

__all__ = ["ALIASES", "schema_named", "template", "template_names"]

#: Other names of templates.
ALIASES = {
    "products": "product", "item": "product", "articles": "article", "news": "article", "post": "article",
    "blog": "article", "jobs": "job", "job_posting": "job", "vacancy": "job", "events": "event",
    "companies": "company", "business": "company", "organization": "company", "organisation": "company",
    "places": "place", "restaurant": "place", "hotel": "place", "local_business": "place", "store": "place",
    "people": "person", "profile": "person", "reviews": "review", "recipes": "recipe", "properties": "property",
    "real_estate": "property", "realestate": "property", "apartment": "property", "listing_property": "property",
    "docs": "documentation", "doc": "documentation",
}  # fmt: skip
_REQUIRED = {"review": "text"}  # (a review may have no title; its text is what makes it one)


def _kinds() -> dict[str, Any]:
    from ..goals.goal import ENTITIES

    return ENTITIES


def template_names() -> list[str]:
    """The templates there are."""
    return list(_kinds())


def template(name: str) -> Schema:
    """The extraction schema of template ``name`` (see the module docs)."""
    key = name.strip().lower().replace("-", "_").replace(" ", "_")
    kinds = _kinds()
    kind = kinds.get(ALIASES.get(key, key))
    if kind is None:
        raise KeyError(f"no template {name!r} (templates: {', '.join(kinds)})")
    required = _REQUIRED.get(kind.name, next(iter(kind.fields)))
    fields: dict[str, Any] = {}
    for field_name, spec in kind.fields.items():
        if isinstance(spec, str):
            spec = {"type": spec[:-2], "many": True} if spec.endswith("[]") else {"type": spec}
        else:
            spec = dict(spec)
        if field_name == required:
            spec["required"] = True
        fields[field_name] = spec
    return Schema.from_dict({"name": kind.name, "fields": fields,
                             "description": f"wintergrab's {kind.name} template"})  # fmt: skip


def schema_named(value: str | Path) -> Schema:
    """A schema file, or, when no such file exists, the template of that name (``"product"``)."""
    if isinstance(value, str) and not Path(value).is_file():
        try:
            return template(value)
        except KeyError:
            pass
    return load_schema(value)
