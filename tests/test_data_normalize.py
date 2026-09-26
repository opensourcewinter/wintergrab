"""Normalizers: numbers, money, dates, units, contact details, places, text."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from wintergrab.data.normalize import (
    Money,
    Quantity,
    clean_text,
    convert,
    detect_currency,
    fix_mojibake,
    has_replacement_characters,
    is_placeholder,
    mojibake_score,
    normalize_availability,
    normalize_country,
    normalize_email,
    normalize_language,
    normalize_phone,
    normalize_region,
    normalize_url_value,
    parse_address,
    parse_boolean,
    parse_coordinates,
    parse_date,
    parse_datetime,
    parse_dimensions,
    parse_duration,
    parse_integer,
    parse_money,
    parse_money_range,
    parse_number,
    parse_percent,
    parse_phone,
    parse_quantity,
    parse_rating,
    postal_code,
    unit_info,
)
from wintergrab.errors import ConfigurationError

NOW = datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc)


def parsed(fn, value, **kwargs):
    notes: list[str] = []
    return fn(value, notes=notes, **kwargs), notes


# --------------------------------------------------------------------------- #
# numbers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1,234.56", "1234.56"),
        ("1.234,56", "1234.56"),
        ("1 234,56", "1234.56"),
        ("1'234.50", "1234.50"),
        ("12,5", "12.5"),
        ("$.99", "0.99"),
        ("-3.5", "-3.5"),
        ("−7", "-7"),  # a real minus sign
        ("(12)", "-12"),  # accounting negative
        ("2.3k", "2300"),
        ("1.5M", "1500000"),
        ("12 lakh", "1200000"),
        ("3 crore", "30000000"),
        ("٣٤٥", "345"),  # Arabic-Indic digits
        ("sizes 10 12", "10"),  # the first number, not "1012"
        ("5 m", "5"),  # metres, not millions
    ],
)
def test_parse_number(text: str, expected: str) -> None:
    assert parse_number(text) == Decimal(expected)


def test_parse_number_notes_ambiguity_and_accepts_the_site_separator() -> None:
    assert parsed(parse_number, "1,234") == (Decimal("1234"), ["ambiguous-separator"])
    assert parsed(parse_number, "1.234") == (Decimal("1.234"), ["ambiguous-separator"])
    assert parse_number("1.234", decimal=",") == Decimal("1234")
    assert parse_number("1,234", decimal=",") == Decimal("1.234")
    assert parsed(parse_number, "2.3k")[1] == ["suffix"]
    assert parse_number("2.3k", allow_suffix=False) == Decimal("2.3")


@pytest.mark.parametrize("value", [None, "", "abc", True, float("nan"), float("inf")])
def test_parse_number_without_a_number(value) -> None:
    assert parse_number(value) is None


def test_parse_number_passes_numbers_through() -> None:
    assert parse_number(3) == Decimal(3)
    assert parse_number(0.1) == Decimal("0.1")  # repr, not the binary expansion
    assert parse_number(Decimal("2.50")) == Decimal("2.50")


def test_parse_integer_and_percent() -> None:
    assert parse_integer("1,204 reviews") == 1204
    assert parse_integer("1.204") == 1204  # counts have no decimals: no ambiguity
    assert parsed(parse_integer, "12.6") == (13, ["rounded"])
    assert parse_integer("no") is None
    assert parse_percent("12.5 %") == Decimal("0.125")
    assert parse_percent("50") == Decimal("0.5")


# --------------------------------------------------------------------------- #
# money
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "amount", "currency"),
    [
        ("₹29,999", "29999", "INR"),
        ("Rs. 1,49,999", "149999", "INR"),  # Indian grouping
        ("$1,299.99", "1299.99", "USD"),
        ("US$5", "5", "USD"),
        ("1.299,99 €", "1299.99", "EUR"),
        ("€1.299", "1299", "EUR"),  # two minor units: three digits after the point are a group
        ("EUR 12,50", "12.50", "EUR"),
        ("12,50 EUR", "12.50", "EUR"),
        ("25,- €", "25", "EUR"),
        ("1.299,- EUR", "1299", "EUR"),
        ("CHF 1'299.50", "1299.50", "CHF"),
        ("£3.50", "3.50", "GBP"),
        ("¥1,500", "1500", "JPY"),
        ("10 JPY", "10", "JPY"),
        ("Price: 19,99 € (incl. 19% VAT)", "19.99", "EUR"),
        ("15% off, now $20", "20", "USD"),  # a percentage is not a price
        ("1.234 KWD", "1.234", "KWD"),  # three minor units: the reading stays
    ],
)
def test_parse_money(text: str, amount: str, currency: str) -> None:
    assert parse_money(text) == Money(Decimal(amount), currency)


def test_parse_money_notes_its_guesses() -> None:
    assert parsed(parse_money, "₹29,999")[1] == ["separator-from-currency"]
    assert "ambiguous-currency" in parsed(parse_money, "$5")[1]
    assert parsed(parse_money, "99 kr", country="SE") == (Money(Decimal(99), "SEK"), ["currency-from-country"])
    assert parsed(parse_money, "12,50", currency="EUR") == (Money(Decimal("12.50"), "EUR"), ["currency-from-default"])
    assert parsed(parse_money, "free") == (Money(Decimal(0), None), ["free"])


def test_parse_money_edge_cases() -> None:
    assert parse_money("") is None
    assert parse_money("$") is None
    assert parse_money(None) is None
    assert parse_money(12.5, currency="usd") == Money(Decimal("12.5"), "USD")
    assert parse_money("$5", currency="CAD") == Money(Decimal(5), "CAD")  # the given currency settles "$"
    assert str(Money(Decimal("5"), "USD")) == "5 USD"
    assert Money(Decimal("5.50"), "EUR").to_dict() == {"amount": 5.5, "currency": "EUR"}


def test_money_ranges_and_currency_detection() -> None:
    assert parse_money_range("$10 - $20") == (Money(Decimal(10), "USD"), Money(Decimal(20), "USD"))
    assert parse_money_range("€10–15") == (Money(Decimal(10), "EUR"), Money(Decimal(15), "EUR"))
    assert parse_money_range("$20") is None
    assert detect_currency("₹29,999") == "INR"
    assert detect_currency("5 USD") == "USD"
    assert detect_currency("EUR") == "EUR"
    assert detect_currency("nothing") is None
    assert detect_currency("nothing", default="gbp") == "GBP"


# --------------------------------------------------------------------------- #
# dates and durations
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2024-03-05", date(2024, 3, 5)),
        ("13/05/2024", date(2024, 5, 13)),  # 13 cannot be a month
        ("5 March 2024", date(2024, 3, 5)),
        ("March 5, 2024", date(2024, 3, 5)),
        ("Mar 5th 2024", date(2024, 3, 5)),
        ("5. März 2024", date(2024, 3, 5)),
        ("5 mars 2024", date(2024, 3, 5)),
        ("2024年3月5日", date(2024, 3, 5)),
        ("20240305", date(2024, 3, 5)),
        ("yesterday", date(2024, 6, 14)),
        ("3 days ago", date(2024, 6, 12)),
        ("2 weeks ago", date(2024, 6, 1)),
    ],
)
def test_parse_date(text: str, expected: date) -> None:
    assert parse_date(text, now=NOW) == expected


def test_parse_date_notes_and_failures() -> None:
    assert parsed(parse_date, "03/05/2024", now=NOW) == (date(2024, 3, 5), ["ambiguous-day-month"])
    assert parse_date("03/05/2024", dayfirst=True) == date(2024, 5, 3)
    assert parsed(parse_date, "05.03.24")[1] == ["two-digit-year"]
    assert parsed(parse_date, "Posted on 2024-03-05T10:00:00Z") == (date(2024, 3, 5), ["date-in-text"])
    assert parsed(parse_date, "yesterday", now=NOW)[1] == ["relative-date"]
    assert parse_date("31/02/2024") is None
    assert parse_date("nonsense") is None
    assert parse_date(datetime(2024, 1, 2, 3, 4)) == date(2024, 1, 2)


def test_parse_datetime() -> None:
    utc = timezone.utc
    assert parse_datetime("2024-03-05T10:30:00Z") == datetime(2024, 3, 5, 10, 30, tzinfo=utc)
    assert parse_datetime("Tue, 05 Mar 2024 10:30:00 GMT") == datetime(2024, 3, 5, 10, 30, tzinfo=utc)
    assert parse_datetime("2024-03-05T10:30:00+05:30").utcoffset() == timedelta(hours=5, minutes=30)
    assert parsed(parse_datetime, "5 March 2024 10:30 PM") == (datetime(2024, 3, 5, 22, 30), ["no-timezone"])
    assert parse_datetime("2024-03-05 10:30", tz=utc) == datetime(2024, 3, 5, 10, 30, tzinfo=utc)
    assert parsed(parse_datetime, "1709634600") == (datetime(2024, 3, 5, 10, 30, tzinfo=utc), ["timestamp"])


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("PT1H30M", 5400),
        ("1h 30m", 5400),
        ("90 minutes", 5400),
        ("1:30:00", 5400),
        ("2 days", 172800),
        ("P1DT2H", 93600),
    ],
)
def test_parse_duration(text: str, seconds: int) -> None:
    assert parse_duration(text) == timedelta(seconds=seconds)


def test_parse_duration_without_a_duration() -> None:
    assert parse_duration("abc") is None


# --------------------------------------------------------------------------- #
# quantities
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "value", "unit"),
    [
        ("1.5 kg", "1.5", "kg"),
        ("1,5 kg", "1.5", "kg"),
        ("450 g", "450", "g"),
        ("3.5 inch", "3.5", "in"),
        ("100 ml", "100", "ml"),
        ("30 °C", "30", "degC"),
        ("68°F", "68", "degF"),
        ("64 GB", "64", "GB"),
        ("2.4 GHz", "2.4", "GHz"),
        ("5000 mAh", "5000", "mAh"),
    ],
)
def test_parse_quantity(text: str, value: str, unit: str) -> None:
    assert parse_quantity(text) == Quantity(Decimal(value), unit)


def test_parse_quantity_words_that_look_like_units() -> None:
    assert parse_quantity("2 in stock") is None  # "in" followed by a word is not inches
    assert parse_quantity("Model 5 C") is None
    height = parse_quantity("5'11\"")
    assert height is not None and height.unit == "ft"
    assert convert(height, "cm").value.quantize(Decimal("0.01")) == Decimal("180.34")
    assert parse_quantity("5 ft 11 in") == height
    assert parsed(parse_quantity, "12", default_unit="kg") == (Quantity(Decimal(12), "kg"), ["unit-from-default"])


def test_convert_and_unit_info() -> None:
    assert convert(Quantity(Decimal(1), "lb"), "g").value == Decimal("453.59237")
    assert convert(Quantity(Decimal(68), "degF"), "C") == Quantity(Decimal(20), "degC")  # "C" is Celsius here
    assert convert(Quantity(Decimal(5000), "mAh"), "Ah").value == Decimal(5)
    assert Quantity(Decimal(1500), "g").canonical() == Quantity(Decimal("1.5"), "kg")
    assert unit_info("lbs") == ("lb", "mass")
    with pytest.raises(ValueError, match="cannot convert mass"):
        convert(Quantity(Decimal(1), "kg"), "m")
    with pytest.raises(ConfigurationError):
        unit_info("furlongs per fortnight")


def test_parse_dimensions() -> None:
    assert parse_dimensions("10 x 20 x 30 cm") == [Quantity(Decimal(n), "cm") for n in (10, 20, 30)]
    assert parse_dimensions("5 cm x 3 in") == [Quantity(Decimal(5), "cm"), Quantity(Decimal(3), "in")]


# --------------------------------------------------------------------------- #
# contact details
# --------------------------------------------------------------------------- #
def test_parse_phone_international_numbers() -> None:
    assert normalize_phone("+1 (415) 555-2671") == "+14155552671"
    assert normalize_phone("+44 20 7946 0958") == "+442079460958"
    assert normalize_phone("0049 30 123456") == "+4930123456"
    phone = parse_phone("+33 1 23 45 67 89")
    assert phone is not None and phone.country == "FR"


def test_parse_phone_national_numbers_need_a_country() -> None:
    assert normalize_phone("(415) 555-2671") is None
    assert parsed(normalize_phone, "(415) 555-2671", country="US") == ("+14155552671", ["country-from-default"])
    assert normalize_phone("020 7946 0958", country="GB") == "+442079460958"
    assert normalize_phone("098765 43210", country="IN") == "+919876543210"


def test_parse_phone_vanity_extensions_and_junk() -> None:
    assert parsed(normalize_phone, "1-800-FLOWERS", country="US") == (
        "+18003569377",
        ["vanity", "country-from-default"],
    )
    assert normalize_phone("call 555-1234 today", country="US") is None
    phone = parse_phone("(415) 555-2671 ext. 12", country="US")
    assert phone is not None and phone.extension == "12"
    assert normalize_phone("12") is None
    assert normalize_phone("+1 123 456 7890") is None  # NANP area codes cannot start with 1


def test_normalize_email_and_url() -> None:
    assert normalize_email(" John.Doe@Example.COM ") == "John.Doe@example.com"
    assert normalize_email("mailto:a@b.co") == "a@b.co"
    assert parsed(normalize_email, "a [at] b [dot] com") == ("a@b.com", ["deobfuscated"])
    assert normalize_email("x@localhost") is None
    base = "https://shop.example/c/"
    assert normalize_url_value("/p/1?utm_source=x#frag", base_url=base) == "https://shop.example/p/1"
    assert normalize_url_value("p/2", base_url=base) == "https://shop.example/c/p/2"
    assert normalize_url_value("//cdn.example/x.js", base_url=base) == "https://cdn.example/x.js"
    assert normalize_url_value("HTTP://Example.COM:80/a/../b") == "http://example.com/b"
    assert normalize_url_value("javascript:alert(1)", base_url=base) is None


# --------------------------------------------------------------------------- #
# places and languages
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("United States", "US"),
        ("USA", "US"),
        ("us", "US"),
        ("Deutschland", "DE"),
        ("UK", "GB"),
        ("Great Britain", "GB"),
        ("Côte d'Ivoire", "CI"),
        ("Ivory Coast", "CI"),
        ("Korea, Republic of", "KR"),
        ("Narnia", None),
    ],
)
def test_normalize_country(text: str, code: str | None) -> None:
    assert normalize_country(text) == code


def test_regions_languages_and_postal_codes() -> None:
    assert normalize_region("California", "US") == "US-CA"
    assert normalize_region("CA", "US") == "US-CA"
    assert normalize_region("Bayern", "DE") == "DE-BY"
    assert normalize_region("NSW", "AU") == "AU-NSW"
    assert normalize_region("Texas", None) is None
    assert normalize_language("English") == "en"
    assert normalize_language("pt_BR") == "pt-BR"
    assert normalize_language("zh-Hant-TW") == "zh-Hant-TW"
    assert normalize_language("klingon") is None
    assert postal_code("sw1a1aa", "GB") == "SW1A 1AA"
    assert postal_code("94105-1234", "US") == "94105-1234"
    assert postal_code("1234", "US") is None


def test_parse_address() -> None:
    address = parse_address("1600 Amphitheatre Parkway, Mountain View, CA 94043, USA")
    assert address is not None
    assert (address.street, address.city, address.region, address.postal_code, address.country) == (
        "1600 Amphitheatre Parkway",
        "Mountain View",
        "US-CA",
        "94043",
        "US",
    )
    berlin = parse_address("Unter den Linden 77, 10117 Berlin, Germany")
    assert berlin is not None and (berlin.city, berlin.region, berlin.postal_code) == ("Berlin", "DE-BE", "10117")
    london = parse_address("10 Downing Street, London SW1A 2AA, United Kingdom")
    assert london is not None and (london.city, london.postal_code) == ("London", "SW1A 2AA")
    assert parse_address("just text") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("37.7749, -122.4194", (37.7749, -122.4194)),
        ("37.7749° N, 122.4194° W", (37.7749, -122.4194)),
        ("N 37° 46' 29.64\", W 122° 25' 9.84\"", (37.7749, -122.4194)),
        ("37° 46' 29.64\" N 122° 25' 9.84\" W", (37.7749, -122.4194)),
        ("E 2° 17' N 48° 51'", (48.85, 2.2833333)),  # longitude first, by its hemisphere
        ("lat=48.8584&lon=2.2945", (48.8584, 2.2945)),
        ("@48.8584,2.2945,17z", (48.8584, 2.2945)),
        ("200, 300", None),
        ("37° 46' only", None),
    ],
)
def test_parse_coordinates(text: str, expected) -> None:
    assert parse_coordinates(text) == expected


# --------------------------------------------------------------------------- #
# text
# --------------------------------------------------------------------------- #
def test_clean_text() -> None:
    assert clean_text("  Hello\n\t world  ") == "Hello world"
    assert clean_text("line1\n\nline2", keep_newlines=True) == "line1\nline2"
    assert clean_text("Zero" + chr(0x200B) + "width") == "Zerowidth"
    assert clean_text("Caf&eacute; &amp; bar") == "Café & bar"
    assert clean_text("non" + chr(0xA0) + "breaking") == "non breaking"
    assert clean_text("e" + chr(0x301)) == chr(0xE9)  # NFC


def test_mojibake_repair_is_conservative() -> None:
    assert parsed(fix_mojibake, "CafÃ©") == ("Café", ["mojibake-repaired"])
    assert fix_mojibake("naÃ¯ve rÃ©sumÃ©") == "naïve résumé"
    assert fix_mojibake("â€œquotedâ€" + chr(0x9D)) == "“quoted”"  # undefined Windows-1252 byte
    assert fix_mojibake("Itâ€™s") == "It’s"
    assert fix_mojibake("CafÃ© 東京") == "Café 東京"  # the rest of the text is left alone
    for clean in ("Café", "plain ascii", "Ã", "Clean text ç"):
        assert fix_mojibake(clean) == clean
    assert mojibake_score("CafÃ©") > 0.1 > mojibake_score("Café")
    assert has_replacement_characters("bad " + chr(0xFFFD))


@pytest.mark.parametrize(
    ("value", "expected"),
    [("yes", True), ("No", False), ("TRUE", True), ("0", False), (1, True), ("✓", True), ("nein", False),
     ("maybe", None), (None, None), (2, None)],
)  # fmt: skip
def test_parse_boolean(value, expected) -> None:
    assert parse_boolean(value) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("In stock", "InStock"),
        ("Only 3 left in stock", "LimitedAvailability"),
        ("Out of stock", "OutOfStock"),
        ("Currently unavailable", "OutOfStock"),
        ("Sold out", "SoldOut"),
        ("Pre-order now", "PreOrder"),
        ("Available in 2-3 weeks", "BackOrder"),
        ("Ships in 2 days", "InStock"),
        ("Discontinued", "Discontinued"),
        ("https://schema.org/InStock", "InStock"),
        ("in_stock", "InStock"),
        ("Auf Lager", "InStock"),
        ("Nicht verfügbar", "OutOfStock"),
        ("Coming soon", None),
        (True, "InStock"),
    ],
)
def test_normalize_availability(text, expected) -> None:
    assert normalize_availability(text) == expected


@pytest.mark.parametrize(
    ("value", "normalized"),
    [
        ("4.5 out of 5", 4.5),
        ("4.5/5", 4.5),
        ("★★★★☆", 4),
        ("90%", 4.5),
        ("8/10", 4),
        ("4,5 von 5 Sternen", 4.5),
        (3, 3),
    ],
)
def test_parse_rating(value, normalized) -> None:
    rating = parse_rating(value)
    assert rating is not None and rating.normalized(5) == Decimal(str(normalized))


def test_parse_rating_edge_cases() -> None:
    assert parsed(parse_rating, "Four stars")[1] == ["scale-assumed"]
    assert parse_rating("6 out of 5") is None
    assert parse_rating("abc") is None
    rating = parse_rating("8", best=10)
    assert rating is not None and rating.normalized(5) == 4


def test_placeholders() -> None:
    for value in ("N/A", "n/a", " - ", "{{ price }}", "${price}", "TBD", "None", "undefined", "[object Object]"):
        assert is_placeholder(value), value
    for value in ("Nano", "Anna", "5", 5, None, "-5"):
        assert not is_placeholder(value), value
