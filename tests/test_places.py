"""Where records are: addresses and locations read into places, coordinates, distances, filters and groups."""

from __future__ import annotations

import json

import pytest

import wintergrab as wg
from wintergrab.cli import main
from wintergrab.data import Filter, Pipeline, compile_expression
from wintergrab.data.normalize import coordinates_in_url, parse_address, read_address
from wintergrab.data.pipeline import Locate
from wintergrab.data.places import coordinates_of, distance_km, group_records, in_box, place_of, places_of
from wintergrab.errors import ConfigurationError
from wintergrab.extraction import Extractor
from wintergrab.extraction.templates import template

ADDRESSES = [
    # text, country given, (street, city, region, postal code, country), the code naming several places
    ("1600 Amphitheatre Parkway, Mountain View, CA 94043, USA", None,
     ("1600 Amphitheatre Parkway", "Mountain View", "US-CA", "94043", "US"), None),
    ("Unter den Linden 77, 10117 Berlin, Germany", None, ("Unter den Linden 77", "Berlin", "DE-BE", "10117", "DE"), None),
    ("Flat 3, 12 High St, London SW1A 2AA, UK", None, ("Flat 3, 12 High St", "London", None, "SW1A 2AA", "GB"), None),
    ("Rue de Rivoli, 75001 Paris, France", None, ("Rue de Rivoli", "Paris", None, "75001", "FR"), None),
    ("Friedrichstraße 43, 10117 Berlin", None, ("Friedrichstraße 43", "Berlin", None, "10117", None), None),
    ("Rue du Parc 4, Lyon, 69003", None, ("Rue du Parc 4", "Lyon", None, "69003", None), None),
    # places as listings write them
    ("Austin, TX, USA", None, (None, "Austin", "US-TX", None, "US"), None),
    ("New York, NY", None, (None, "New York", "US-NY", None, "US"), None),
    ("Toronto, ON M5V 3L9", None, (None, "Toronto", "CA-ON", "M5V 3L9", "CA"), None),
    ("Sydney NSW 2000", None, (None, "Sydney", "AU-NSW", "2000", "AU"), None),
    ("Springfield, IL 62701", None, (None, "Springfield", "US-IL", "62701", "US"), None),  # a ZIP: the US
    ("Munich, Bavaria, Germany", None, (None, "Munich", "DE-BY", None, "DE"), None),
    ("Bengaluru, Karnataka, India", None, (None, "Bengaluru", "Karnataka", None, "IN"), None),  # as written
    ("Berlin", None, (None, "Berlin", "DE-BE", None, "DE"), None),  # a city that is a region
    ("St. Louis, MO", "US", (None, "St. Louis", "US-MO", None, "US"), None),
    # codes naming several places: settled by the country given, or left unread
    ("San Francisco, CA", None, (None, "San Francisco", None, None, None), "CA"),  # California or Canada
    ("San Francisco, CA", "US", (None, "San Francisco", "US-CA", None, "US"), None),
    ("Perth, WA", None, (None, "Perth", None, None, None), "WA"),  # Washington or Western Australia
    ("Perth, WA", "AU", (None, "Perth", "AU-WA", None, "AU"), None),
    ("Kolkata, IN", "IN", (None, "Kolkata", None, None, "IN"), None),
    ("Tbilisi, Georgia", None, (None, "Tbilisi", None, None, None), "Georgia"),  # the country or the state
    ("Atlanta, Georgia, USA", None, (None, "Atlanta", "US-GA", None, "US"), None),
]  # fmt: skip


@pytest.mark.parametrize(("text", "country", "parts", "several"), ADDRESSES)
def test_addresses_and_places_as_listings_write_them(text, country, parts, several) -> None:
    address, unsure = read_address(text, country=country)
    assert address is not None
    assert (address.street, address.city, address.region, address.postal_code, address.country) == parts
    assert unsure == several


def test_what_is_no_address() -> None:
    assert parse_address("Remote") is None and parse_address("just text") is None and parse_address("") is None
    notes: list[str] = []
    parse_address("San Francisco, CA", notes=notes)
    assert "ambiguous-place" in notes


def test_coordinates_in_map_links() -> None:
    assert coordinates_in_url("https://www.google.com/maps/@52.5200066,13.404954,14z") == (52.5200066, 13.404954)
    place = "https://www.google.com/maps/place/X/@52.51,13.37,17z/data=!3m1!4b1!8m2!3d52.5162746!4d13.3777041"
    assert coordinates_in_url(place) == (52.5162746, 13.3777041)  # the place's own point, not the view's
    embed = "https://www.google.com/maps/embed?pb=!1m18!1m12!1m3!1d2427.9!2d13.3777041!3d52.5162746!2m3"
    assert coordinates_in_url(embed) == (52.5162746, 13.3777041)  # embeds give the longitude first
    assert coordinates_in_url("https://www.openstreetmap.org/?mlat=52.5163&mlon=13.3777#map=17/52.5/13.3") == (
        52.5163,
        13.3777,
    )
    assert coordinates_in_url("https://www.bing.com/maps?cp=52.5163~13.3777&lvl=16") == (52.5163, 13.3777)
    assert coordinates_in_url("geo:52.5163,13.3777?z=17") == (52.5163, 13.3777)
    for other in (
        "https://maps.apple.com/place?coordinate=52.5163,13.3777&name=Tor",
        "https://wego.here.com/?map=52.5163,13.3777,15,normal",
        "https://share.here.com/l/52.5163,13.3777,Tor",
        "https://www.waze.com/ul?ll=52.5163%2C13.3777&navigate=yes",
        "https://maps.googleapis.com/maps/api/staticmap?center=52.5163,13.3777&zoom=15&size=600x300",
    ):
        assert coordinates_in_url(other) == (52.5163, 13.3777), other  # fmt: skip
    assert coordinates_in_url("https://maps.google.com/?q=Unter+den+Linden+77,+Berlin") is None  # an address
    assert coordinates_in_url("https://example.com/?q=52.52,13.405") is None  # not a map
    assert coordinates_in_url("https://www.google.com/maps/@200.0,13.4,17z") is None


def test_the_place_of_a_record() -> None:
    austin = place_of({"location": "Austin, TX"})
    assert (austin.city, austin.region, austin.country) == ("Austin", "US-TX", "US")
    assert austin.sources == {"country": "location", "region": "location", "city": "location"}
    office = place_of({"address": "Unter den Linden 77, 10117 Berlin", "country": "Germany"})
    assert (office.street, office.postal_code, office.country, office.sources["country"]) == (
        "Unter den Linden 77",
        "10117",
        "DE",
        "country",
    )
    # work arrangements written with the location
    assert place_of({"location": "Remote - US"}).to_dict()["remote"] is True
    assert (place_of({"location": "Remote - US"}).country, place_of({"location": "Remote, Europe"}).known) == (
        "US",
        False,
    )
    assert place_of({"location": "Hybrid: Munich, Germany"}).city == "Munich"
    assert place_of({"location": "Berlin (remote possible)"}).remote
    # address objects: wintergrab's, schema.org's, and a schema.org Place with its point
    postal = {"@type": "PostalAddress", "streetAddress": "1 Main St", "addressLocality": "Springfield",
              "addressRegion": "IL", "postalCode": "62701", "addressCountry": "US"}  # fmt: skip
    assert place_of({"address": postal}).to_dict() == {
        "country": "US", "region": "US-IL", "city": "Springfield", "postal_code": "62701", "street": "1 Main St",
        "coordinates": None, "remote": False,
    }  # fmt: skip
    venue = {"@type": "Place", "address": {"addressLocality": "Munich", "addressCountry": "DE"},
             "geo": {"latitude": 48.137, "longitude": 11.575}}  # fmt: skip
    assert place_of({"location": venue}).coordinates == (48.137, 11.575)
    # separate fields are trusted over an address; codes of one region name its country
    assert place_of({"address": "Berlin, Germany", "city": "Potsdam"}).city == "Potsdam"
    assert (place_of({"city": "Toronto", "region": "ON"}).region, place_of({"state": "Bavaria"}).country) == (
        "CA-ON",
        "DE",
    )
    # a record's own field names
    assert place_of({"hq": {"town": "Lyon"}}, fields={"city": "hq.town"}).city == "Lyon"
    with pytest.raises(ValueError, match="unknown place part"):
        place_of({}, fields={"planet": "x"})


def test_coordinates_are_read_not_looked_up() -> None:
    assert place_of({"latitude": "52.52", "longitude": 13.405}).coordinates == (52.52, 13.405)
    assert place_of({"geo": {"type": "Point", "coordinates": [13.405, 52.52]}}).coordinates == (52.52, 13.405)
    shop = {"address": "Pariser Platz, 10117 Berlin", "map": "https://www.openstreetmap.org/?mlat=52.5163&mlon=13.3777"}
    assert place_of(shop).coordinates == (52.5163, 13.3777) and place_of(shop).sources["coordinates"] == "map"
    assert place_of({"address": "Pariser Platz, 10117 Berlin"}).coordinates is None  # no address is geocoded
    assert place_of({"geo": "0, 0"}).coordinates is None  # a map's default, not a place
    assert coordinates_of("48.8584° N, 2.2945° E") == (48.8584, 2.2945)


def test_distances_and_boxes() -> None:
    assert distance_km((52.52, 13.405), (48.1374, 11.5755)) == 504.3  # Berlin to Munich
    assert distance_km("51.5072, -0.1276", {"latitude": 48.8566, "longitude": 2.3522}) == 343.5  # London to Paris
    assert distance_km((52.52, 13.405), None) is None
    assert in_box((52.52, 13.405), 47.3, 5.9, 55.1, 15.0) is True  # Germany's box
    assert in_box((64.84, -147.72), 51.2, 172.4, 71.4, -129.9) is True  # Alaska's box crosses the 180th meridian
    assert in_box((48.86, 2.35), 47.3, 5.9, 55.1, 15.0) is False and in_box(None, 0, 0, 1, 1) is None


JOBS = [
    {"title": "Engineer", "location": "Austin, TX", "salary": {"amount": 120000, "currency": "USD"}},
    {"title": "Designer", "location": "New York, NY", "salary": {"amount": 150000, "currency": "USD"}},
    {"title": "Analyst", "location": "San Francisco, CA", "salary": {"amount": 170000, "currency": "USD"}},
    {"title": "SRE", "location": "Seattle, WA", "salary": {"amount": 160000, "currency": "USD"}},
    {"title": "PM", "location": "Portland, OR", "salary": {"amount": 130000, "currency": "USD"}},
    {"title": "Writer", "location": "Remote - US", "salary": {"amount": 125000, "currency": "USD"}},
    {"title": "Dev", "location": "Portland, ME", "salary": {"amount": 110000, "currency": "USD"}},
    {"title": "Dev", "location": "Amsterdam, NL", "salary": {"amount": 80000, "currency": "EUR"}},
    {"title": "Dev", "location": "Berlin, Germany", "salary": {"amount": 75000, "currency": "EUR"}},
    {"title": "QA", "location": "Munich, Bavaria, Deutschland", "salary": {"amount": 85000, "currency": "EUR"}},
    {"title": "Support", "location": "Remote"},
]


def test_codes_are_settled_by_the_other_records() -> None:
    places, settled = places_of(JOBS)
    by_title = {job["location"]: place for job, place in zip(JOBS, places, strict=True)}
    assert settled == "US"  # most records naming a country name the United States
    assert by_title["San Francisco, CA"].region == "US-CA"  # so "CA" is California, not Canada
    assert by_title["Seattle, WA"].region == "US-WA" and by_title["Portland, ME"].region == "US-ME"
    assert by_title["San Francisco, CA"].sources["country"] == "other records"
    assert by_title["Amsterdam, NL"].unsure == "NL"  # the Netherlands or Newfoundland: no record says which
    assert places_of(JOBS, country="US")[1] is None  # a country given settles them itself


def test_grouping_by_place() -> None:
    groups = group_records(JOBS, "country", stats=["salary"])
    assert [(g.label, g.count) for g in groups] == [("United States (US)", 7), ("Germany (DE)", 2), ("(none)", 2)]
    assert groups[0].stats["salary"] == {
        "count": 7, "min": 110000, "max": 170000, "mean": 137857.14, "median": 130000, "currency": "USD",
    }  # fmt: skip
    assert groups[0].share == round(7 / 11, 4)
    cities = {g.label: g.count for g in group_records(JOBS, "city")}
    assert cities["Portland (US-OR)"] == 1 and cities["Portland (US-ME)"] == 1  # two cities, not one
    regions = [g.label for g in group_records(JOBS, "region")]
    assert "California (US-CA)" in regions and "Bavaria (DE-BY)" not in regions and "Bayern (DE-BY)" in regions
    remote = {g.label: g.count for g in group_records(JOBS, "remote")}
    assert remote == {"not remote": 9, "remote": 2}
    # other fields: one group whatever the case, shown as it is written most
    brands = group_records([{"brand": "Acme"}, {"brand": "ACME"}, {"brand": "Acme"}, {"brand": None}], "brand")
    assert [(g.label, g.count) for g in brands] == [("Acme", 3), ("(none)", 1)]
    mixed = group_records([{"price": 10, "k": 1}, {"price": {"amount": 20, "currency": "EUR"}, "k": 1}], "k",
                          stats=["price"])  # fmt: skip
    assert set(mixed[0].stats) == {"price", "price (EUR)"}  # amounts in different currencies are not summed up


def test_the_locate_stage_and_filters() -> None:
    records = [
        {"name": "A", "address": "Unter den Linden 77, 10117 Berlin, Germany", "geo": {"latitude": 52.517, "longitude": 13.389}},
        {"name": "B", "location": "Munich, Bavaria, Germany", "latitude": 48.1374, "longitude": 11.5755},
        {"name": "C", "location": "Austin, TX"},
        {"name": "D", "country": "Narnia"},
    ]  # fmt: skip
    near = Pipeline([Locate(), Filter("distance_km(coordinates, [52.52, 13.405]) <= 25")]).run(records)
    assert [r["name"] for r in near] == ["A"] and near[0]["coordinates"] == [52.517, 13.389]
    assert near[0]["region"] == "DE-BE" and near[0]["postal_code"] == "10117"
    stage = Locate(add=["country", "city"], prefix="place_", country="US")
    located = Pipeline([stage]).run([dict(r) for r in records[2:]])
    assert located[0] == {"name": "C", "location": "Austin, TX", "place_country": "US", "place_city": "Austin"}
    assert located[1]["country"] == "Narnia" and located[1]["place_country"] is None  # not made up
    assert stage.stats["unknown"] == 1
    kept = Pipeline([Locate(add=["country"])]).run([{"country": "Narnia"}])
    assert kept[0]["country"] == "Narnia"  # a value is not replaced by nothing
    config = Pipeline.from_config({"stages": [{"locate": {"country": "US", "add": ["country", "region"]}}]})
    assert config.to_config()["stages"] == [{"locate": {"add": ["country", "region"], "country": "US"}}]
    with pytest.raises(ConfigurationError, match="unknown part"):
        Locate(add=["planet"])
    with pytest.raises(ConfigurationError, match="not a country"):
        Locate(country="Narnia")
    assert compile_expression("in_box(coordinates, 47.3, 5.9, 55.1, 15.0)")({"coordinates": [52.52, 13.405]})
    assert compile_expression("coordinates(map)")({"map": "geo:52.5,13.4"}) == [52.5, 13.4]


def test_the_places_command(tmp_path, capsys) -> None:
    source = tmp_path / "jobs.jsonl"
    source.write_text("\n".join(json.dumps(job) for job in JOBS), encoding="utf-8")
    assert main(["data", "places", str(source), "--by", "country", "--stats", "salary"]) == 0
    out, err = capsys.readouterr()
    assert out.splitlines()[0].split() == ["country", "records", "share", "salary", "(median)"]
    assert out.splitlines()[1].split()[:4] == ["United", "States", "(US)", "7"]
    assert "11 record(s): 10 with a place (2 countries), 2 remote" in err
    assert "'NL' (1 record(s))" in err and "were read in US" in err
    # kept: in a country, a region, a city
    kept = tmp_path / "kept.jsonl"
    assert main(["-q", "data", "places", str(source), "--in", "Germany", "--in", "US-OR", "-o", str(kept)]) == 0
    rows = [json.loads(line) for line in kept.read_text(encoding="utf-8").splitlines()]
    assert [r["city"] for r in rows] == ["Portland", "Berlin", "Munich"]
    assert set(rows[0]) >= {"country", "region", "city", "postal_code", "coordinates"}
    assert main(["data", "places", str(source), "--in", "Georgia"]) == 2
    assert "could be GE or US-GA" in capsys.readouterr().err
    # near a point: records stating none are left out, and counted
    shops = tmp_path / "shops.jsonl"
    shops.write_text("\n".join(json.dumps(r) for r in [
        {"name": "Mitte", "latitude": 52.5200, "longitude": 13.4050},
        {"name": "Potsdam", "geo": "52.3906, 13.0645"},
        {"name": "Munich", "coordinates": [48.1374, 11.5755]},
        {"name": "Nowhere"},
    ]), encoding="utf-8")  # fmt: skip
    near = tmp_path / "near.jsonl"
    assert main(["data", "places", str(shops), "--near", "52.52,13.405", "--within", "30", "-o", str(near)]) == 0
    rows = [json.loads(line) for line in near.read_text(encoding="utf-8").splitlines()]
    assert [(r["name"], r["distance_km"]) for r in rows] == [("Mitte", 0.0), ("Potsdam", 27.2)]
    assert "1 record(s) left out by --near: they state no coordinates" in capsys.readouterr().err
    assert main(["data", "places", str(shops), "--near", "52.52,13.405"]) == 2  # --within too


def test_pages_state_their_point() -> None:
    meta = wg.parse(
        '<html><head><meta name="geo.position" content="52.5163;13.3777"><meta name="geo.region" content="DE-BE">'
        "</head><body><h1>Cafe Linden</h1><address>Unter den Linden 77, 10117 Berlin</address></body></html>",
        url="https://cafe.example/",
    )
    record = Extractor(template("place")).extract(meta)
    assert (record["latitude"], record["longitude"]) == (52.5163, 13.3777)
    assert record.fields["latitude"].source == "meta:geo.latitude"
    linked = wg.parse(
        '<h1>Cafe Linden</h1><a href="https://www.google.com/maps/dir/?api=1&destination=52.5163,13.3777">Directions</a>',
        url="https://cafe.example/2",
    )
    record = Extractor(template("place")).extract(linked)
    assert (record["latitude"], record["longitude"]) == (52.5163, 13.3777)
    assert record.fields["longitude"].source == "dom:a[href] map"
    marked = wg.parse(
        '<h1>Cafe</h1><div class="map" data-lat="48.1374" data-lng="11.5755"></div>', url="https://c.example/"
    )
    assert Extractor(template("place")).extract(marked)["latitude"] == 48.1374
