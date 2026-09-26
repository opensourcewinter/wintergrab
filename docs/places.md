# Places

`wintergrab data places` reads where each record is: its address, its
location as listings write it, its city, region, postal code and country
fields, and its coordinates. It normalizes them, keeps the records in the
places you ask for, and groups them by place:

```bash
wintergrab data places jobs.jsonl --by country --stats salary
```

```
11 record(s): 10 with a place (2 countries), 2 remote
not read: 'NL' (1 record(s)): each could name several places; --country COUNTRY reads them in that country
3 record(s) naming a code of several places were read in US, the country the other records name most (--country to choose)
country             records  share  salary (median)
United States (US)        7    64%      130,000 USD
Germany (DE)              2    18%       80,000 EUR
(none)                    2    18%       80,000 EUR
```

(Eleven job postings: "Austin, TX", "San Francisco, CA", "Seattle, WA",
"Remote - US", "Berlin, Germany", "Munich, Bavaria, Deutschland",
"Amsterdam, NL", "Remote"... The test suite's `tests/test_places.py`.)

## A record's place

A place is read from the fields a record has, whichever they are:

| Part | Fields |
|---|---|
| an address | `address` (one line, or an address object: WINTERGRAB's, schema.org's `PostalAddress`, or a schema.org `Place` with its `geo`) |
| a location | `location` (`"Austin, TX"`, `"Remote - US"`, `"Hybrid: Amsterdam, NL"`, `"Berlin (remote possible)"`) |
| city, region, postal code, country | `city`, `locality`; `region`, `state`, `province`; `postal_code`, `zip`, `postcode`; `country`, `country_code` (and their schema.org names) |
| coordinates | `coordinates`, `geo`; `latitude` and `longitude`; a `map` link |

Separate fields are trusted over what an address says. Other names are
given with `--field PART=NAME` (`--field city=town`,
`--field address=hq.address`).

What comes out:

- **country**: an ISO 3166-1 code, from names in several languages and
  codes (`"Germany"`, `"Deutschland"`, `"DEU"`: `DE`);
- **region**: an ISO 3166-2 code where WINTERGRAB knows the country's
  regions. Those are the United States, Canada, Australia, Germany and the
  United Kingdom (`"California"`, `"CA"`: `US-CA`; `"Bavaria"`: `DE-BY`).
  Elsewhere the region is kept as written (`"Karnataka"`);
- **city** as written, **postal code** in its country's format
  (`"sw1a1aa"`: `SW1A 1AA`), **street** as written;
- **remote**: the location says the work is remote (a place, if any, says
  where: `"Remote - US"` is remote, in the US);
- **coordinates**, when the record states them (below).

A location such as `"Remote, Europe"` or `"Multiple locations"` names no
one place, and none is made up.

```python
from wintergrab.data import place_of

place_of({"location": "Austin, TX"})
# Place(country='US', region='US-TX', city='Austin')
place_of({"address": "Unter den Linden 77, 10117 Berlin", "country": "Germany"})
# Place(country='DE', region='DE-BE', city='Berlin', postal_code='10117', street='Unter den Linden 77')
place_of({"location": "Remote - US"})
# Place(country='US', remote=True)
place_of({"location": "Austin, TX"}).sources
# {'country': 'location', 'region': 'location', 'city': 'location'}
```

Addresses are split by `parse_address` (in `wintergrab.data.normalize`),
which reads full addresses and the short forms listings use, from the end:

1. the country;
2. a region (a name, or a code of the United States, Canada or Australia);
3. the postal code in the country's format. A region's code beside it
   tells the country: `"Springfield, IL 62701"` is in the US;
4. the city, and the street before it.

## Codes naming several places

`"CA"` is California and Canada; `"IN"` is Indiana and India; `"WA"` is
Washington and Western Australia; `"NL"` is the Netherlands and Newfoundland
and Labrador; `"Georgia"` is a state and a country. Such a code is read:

- in the country you give (`--country US`, `place_of(record, country="US")`):
  the country the addresses are in when they do not say it;
- otherwise, in the country the other records name most, among those it
  could be in. At least three records must name that country, and at least
  80% of those naming any of them. Among records in the United States,
  `"San Francisco, CA"` is in California; among records in Canada, `"CA"` is
  Canada;
- otherwise not at all. The place keeps its city, says which code was not
  read (`unsure`), and the command counts them.

A place read this way says so: its country's source is `other records`.
`--country` settles it your way, whatever the other records say.

## Coordinates

Coordinates are read, never looked up. No address is sent anywhere to be
geocoded. They come from:

- the record: `coordinates`, `geo` (`"48.8584, 2.2945"`, degrees and minutes,
  `{"latitude": ..., "longitude": ...}`, a GeoJSON point), or `latitude` and
  `longitude`;
- a map link in the record (`map`): Google Maps (a place's point, a
  search, directions, a view, an embed), OpenStreetMap, Apple Maps, Bing
  Maps, HERE, Waze, `geo:` URIs (`coordinates_in_url`). A link to an address
  gives none.

When extracting, a page's point fills `latitude`, `longitude` and
`coordinates` fields:

- from its structured data (schema.org `geo`);
- from its `geo.position` and `ICBM` meta tags and OpenGraph's
  `place:location`;
- from its map links, map embeds and static map images;
- from map elements' `data-lat` and `data-lng` attributes.

`"0, 0"`, a map's default, is no place.

## Keeping records by place

```bash
wintergrab data places jobs.jsonl --in Germany --in US-OR -o kept.jsonl   # in Germany, or in Oregon
wintergrab data places shops.jsonl --near 52.52,13.405 --within 10 -o near.jsonl
wintergrab data places jobs.jsonl --remote -o remote.jsonl
```

- **`--in`** takes a country (`DE`, `Germany`), a region (`US-CA`,
  `California`) or a city (`Berlin`), and can be repeated. A name that could
  be several places (`Georgia`) is refused, with what it could be.
- **`--near LAT,LON --within KM`** keeps the records within that distance of
  a point and adds their `distance_km`. Records stating no coordinates are
  left out, and counted.

Kept records get their place added: `country`, `region`, `city`,
`postal_code` and `coordinates` (`--add` chooses; `--prefix place_` names
them `place_country`...). A value a record has is not replaced by nothing.

Distances are great-circle distances on a sphere of the Earth's mean
radius. That is within 0.5% of the distance on its ellipsoid: Berlin to
Munich is 504.3 km.

```python
from wintergrab.data import distance_km
from wintergrab.data.places import in_box

distance_km((52.52, 13.405), (48.1374, 11.5755))    # 504.3
in_box((52.52, 13.405), 47.3, 5.9, 55.1, 15.0)      # True: south, west, north, east
```

## Grouping by place

`--by` prints the records grouped by `country`, `region`, `city`,
`postal_code` or `remote`, or by any other field. Places are grouped as they
were normalized, so `"Germany"`, `"DE"` and `"Deutschland"` are one group.
Two cities of one name are two groups: `Portland (US-OR)` and
`Portland (US-ME)`. `--stats FIELD` sums up a numeric field in each group
(its median in the table). Amounts of money are summed up by currency.
`--json` prints every group with its count, share and statistics
(`count`, `min`, `max`, `mean`, `median`). With `--by`, the table is the
output; `-o` writes the records as well.

```python
from wintergrab.data import group_records

for group in group_records(jobs, "country", stats=["salary"]):
    print(group.label, group.count, group.stats)
# United States (US) 7 {'salary': {'count': 7, 'min': 110000, 'max': 170000, 'mean': 137857.14,
#                                  'median': 130000, 'currency': 'USD'}}
```

`group_records` reads the places once for all the records (`places_of`),
so codes naming several places are settled as above.

## In pipelines and expressions

`Locate` adds the place to each record as it passes:

```yaml
stages:
  - locate: {country: US}            # add: [country, region, city, postal_code, coordinates]
  - filter: "distance_km(coordinates, [30.2672, -97.7431]) <= 50"   # within 50 km of Austin
```

A pipeline runs record by record, so `Locate` settles codes naming several
places only by the `country` it is given. It counts the records whose place
is `unknown` or `unsure`. Expressions have `distance_km(a, b)`,
`in_box(point, south, west, north, east)` and `coordinates(value)`. A record
without coordinates is neither near nor in a box (see
[data](data.md#expressions)).

## What it does not do

- It looks nothing up. Without coordinates in the data, there is no distance.
- It knows the regions of five countries. Other regions are kept as written,
  and `"Karnataka"` and `"KA"` are two regions.
- It does not translate city names: `"Munich"` and `"München"` are two
  cities.
- Addresses vary enormously. What could not be identified stays empty,
  rather than guessed; `parse_address` notes `partial-address` and
  `ambiguous-place`.
