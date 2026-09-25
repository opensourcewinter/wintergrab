"""Typed values from scraped text: prices, currencies, ratings, stock and counts."""

from __future__ import annotations

import pytest

from wintergrab.parser.normalize import (
    Availability,
    Price,
    clean_record,
    parse_availability,
    parse_count,
    parse_price,
    parse_rating,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("£51.77", Price(51.77, "GBP")),
        ("Â£51.77", Price(51.77, "GBP")),  # mojibake still finds the pound sign
        ("$1,299.99", Price(1299.99, "$")),  # "$" is ambiguous: kept as written
        ("US$ 12", Price(12.0, "USD")),
        ("CA$1,000", Price(1000.0, "CAD")),
        ("1.234,56 €", Price(1234.56, "EUR")),
        ("€ 1.234", Price(1234.0, "EUR")),  # European thousands
        ("12,50 EUR", Price(12.5, "EUR")),
        ("1 234,56 zł", Price(1234.56, "PLN")),
        ("CHF 1'234.50", Price(1234.5, "CHF")),
        ("₹ 1,49,999", Price(149999.0, "INR")),  # Indian grouping
        ("¥ 3,000", Price(3000.0, "¥")),  # JPY or CNY: kept as written
        ("USD 99", Price(99.0, "USD")),
        ("12,-", Price(12.0, None)),
        ("from £10 to £20", Price(10.0, "GBP")),
        ("£5 12 items", Price(5.0, "GBP")),
        ("Free", Price(0.0, None)),
        ("49.99", Price(49.99, None)),
        (19.5, Price(19.5, None)),
    ],
)
def test_parse_price(text, expected) -> None:
    assert parse_price(text) == expected


@pytest.mark.parametrize("text", ["", "Call for price", None, "Europe", True])
def test_parse_price_without_a_price(text) -> None:
    assert parse_price(text) is None


def test_parse_price_page_currency_fallback() -> None:
    assert parse_price("19.99", currency="EUR") == Price(19.99, "EUR")
    assert parse_price("£19.99", currency="EUR") == Price(19.99, "GBP")  # the text wins


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("star-rating Three", 3.0),
        ("star-rating One", 1.0),
        ("4.5 out of 5", 4.5),
        ("Rated 4.5 out of 5 stars", 4.5),
        ("4,5/5", 4.5),
        ("4.8 stars", 4.8),
        ("rating-4", 4.0),
        ("stars-45", 4.5),
        ("stars-4-5", 4.5),
        ("★★★☆☆", 3.0),
        ("4.2", 4.2),
        (5, 5.0),
    ],
)
def test_parse_rating(text, expected) -> None:
    assert parse_rating(text) == expected


@pytest.mark.parametrize("text", ["No one has rated this product yet", "(123 reviews)", "", None, "score 87 points"])
def test_parse_rating_without_a_rating(text) -> None:
    assert parse_rating(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("In stock", Availability(True, None)),
        ("In stock (22 available)", Availability(True, 22)),
        ("Only 3 left in stock", Availability(True, 3)),
        ("Out of stock", Availability(False, None)),
        ("Sold out", Availability(False, None)),
        ("Currently unavailable.", Availability(False, None)),
        ("Not available", Availability(False, None)),
        ("https://schema.org/InStock", Availability(True, None)),
        ("http://schema.org/OutOfStock", Availability(False, None)),
        ("PreOrder", Availability(False, None)),
        ("Auf Lager", Availability(True, None)),
        ("Ausverkauft", Availability(False, None)),
        ("0 available", Availability(False, 0)),
        (True, Availability(True, None)),
    ],
)
def test_parse_availability(text, expected) -> None:
    assert parse_availability(text) == expected


def test_parse_availability_unknown() -> None:
    assert parse_availability("Ships to Europe") is None
    assert parse_availability("") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1,234 reviews", 1234), ("2.5k", 2500), ("No reviews", 0), ("0", 0), ("(12)", 12), ("3 books", 3), (7, 7)],
)
def test_parse_count(text, expected) -> None:
    assert parse_count(text) == expected


def test_clean_record_types_fields_by_name() -> None:
    record = {
        "title": "  A Light in\n the Attic ",
        "url": "https://books.example/1",
        "price": "£51.77",
        "price_incl_tax": "£51.77",
        "tax": "£0.00",
        "rating": "star-rating Three",
        "availability": "In stock (22 available)",
        "number_of_reviews": "0",
        "total_reviews": "1,204 reviews",
        "upc": "a897fe39b1053632",
    }
    assert clean_record(record) == {
        "title": "A Light in the Attic",
        "url": "https://books.example/1",
        "price": 51.77,
        "currency": "GBP",
        "price_incl_tax": 51.77,
        "tax": 0.0,
        "rating": 3.0,
        "availability": "In stock (22 available)",
        "in_stock": True,
        "stock": 22,
        "number_of_reviews": 0,
        "total_reviews": 1204,
        "upc": "a897fe39b1053632",
    }


def test_clean_record_keeps_what_it_cannot_parse() -> None:
    record = {"price": "Call us", "rating": "Not rated yet", "availability": "Ask in store", "reviews": "Great book!"}
    assert clean_record(record) == record


def test_clean_record_currency_conflicts_and_existing_fields() -> None:
    assert clean_record({"price": "£5", "shipping": "€2"}) == {
        "price": 5.0, "currency": "GBP", "shipping": 2.0, "shipping_currency": "EUR",
    }  # fmt: skip
    assert clean_record({"price": "5", "currency": "EUR"}) == {"price": 5.0, "currency": "EUR"}
    assert clean_record({"in_stock": "In stock", "stock": "7 left"}) == {"in_stock": True, "stock": 7}
