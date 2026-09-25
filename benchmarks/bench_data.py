"""Throughput of the data layer on synthetic scraped records (no network).

    .venv/bin/python benchmarks/bench_data.py [--records 20000] [--repeat 5]

Every measurement runs ``--repeat`` times over the same records and reports
the median rate. The records look like a product crawl's raw output: prices
in several currencies and formats, dates, weights, ratings, availability
text, URLs with tracking parameters, and a few duplicates and bad values.
"""

from __future__ import annotations

import argparse
import platform
import random
import statistics
import time
from collections.abc import Callable
from typing import Any

from wintergrab import __version__
from wintergrab.data import (
    Compute,
    Deduplicate,
    Deduplicator,
    Expression,
    Filter,
    Normalize,
    Pipeline,
    QualityMonitor,
    Schema,
    Validate,
)
from wintergrab.data.normalize import parse_date, parse_money

SCHEMA = Schema.from_dict(
    {
        "name": "product",
        "key": ["url"],
        "fields": {
            "name": {"type": "string", "required": True},
            "price": {"type": "money", "required": True, "minimum": 0},
            "sale_price": "money",
            "currency": "currency",
            "released": "date",
            "weight": {"type": "quantity", "unit": "kg"},
            "rating": {"type": "rating", "best": 5},
            "availability": "availability",
            "description": "text",
            "url": {"type": "url", "required": True},
        },
    }
)
PRICES = ["₹{:,}", "${:,}.99", "{:,},00 €", "£{}.50", "EUR {}", "{} kr"]
DATES = ["2024-03-{:02d}", "{} March 2024", "03/{:02d}/2024", "{} days ago"]
WORDS = [
    "phone",
    "case",
    "cable",
    "charger",
    "screen",
    "battery",
    "fast",
    "slim",
    "black",
    "white",
    "pro",
    "max",
    "mini",
    "ultra",
]


def make_records(n: int, seed: int = 0) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        amount = rng.randint(5, 99_999)
        out.append(
            {
                "name": " ".join(rng.choice(WORDS) for _ in range(3)).title(),
                "price": "N/A" if i % 97 == 0 else rng.choice(PRICES).format(amount),
                "sale_price": rng.choice(PRICES).format(max(1, amount - 5)) if i % 3 == 0 else None,
                "released": rng.choice(DATES).format(rng.randint(1, 28)),
                "weight": f"{rng.randint(50, 2000)} g",
                "rating": f"{rng.randint(1, 9)}.{rng.randint(0, 9)} out of 10",
                "availability": rng.choice(["In stock", "Only 3 left", "Out of stock", "Pre-order"]),
                "description": " ".join(rng.choice(WORDS) for _ in range(40)),
                "url": f"https://shop.example/p/{i if i % 50 else i - 1}?utm_source=feed",
            }
        )
    return out


def rate(fn: Callable[[], int], repeat: int) -> float:
    """Median items per second over ``repeat`` runs (``fn`` returns how many items it processed)."""
    rates = []
    for _ in range(repeat):
        start = time.perf_counter()
        count = fn()
        rates.append(count / (time.perf_counter() - start))
    return statistics.median(rates)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--records", type=int, default=20_000)
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()
    records = make_records(args.records)
    normalized = [SCHEMA.normalize(r)[0] for r in records]
    prices = [r["price"] for r in records]
    dates = [r["released"] for r in records]
    expression = Expression("price > 100 and availability == 'InStock' and currency in ('USD', 'EUR')")

    def normalize() -> int:
        for record in records:
            SCHEMA.normalize(record)
        return len(records)

    def validate() -> int:
        for record in normalized:
            SCHEMA.validate(record)
        return len(normalized)

    def pipeline() -> int:
        stages = Pipeline(
            [
                Normalize(SCHEMA),
                Validate(),
                Filter("price > 0"),
                Compute("discount", "round(1 - sale_price / price, 2)"),
                Deduplicate(key="url"),
            ]
        )
        stages.run(records)
        return len(records)

    def dedupe_near() -> int:
        Deduplicator(key="url", near=True, text_fields=["description"]).run(normalized)
        return len(normalized)

    def quality() -> int:
        monitor = QualityMonitor(SCHEMA, save_to=None)
        for record in normalized:
            monitor.observe(record)
        monitor.report()
        return len(normalized)

    def money() -> int:
        for text in prices:
            parse_money(text)
        return len(prices)

    def dates_() -> int:
        for text in dates:
            parse_date(text)
        return len(dates)

    def expressions() -> int:
        for record in normalized:
            expression(record)
        return len(normalized)

    rows = [
        ("parse_money", money, "values"),
        ("parse_date", dates_, "values"),
        ("expression (3 comparisons)", expressions, "records"),
        ("Schema.normalize (10 fields)", normalize, "records"),
        ("Schema.validate (10 fields)", validate, "records"),
        ("pipeline: normalize, validate, filter, compute, dedupe", pipeline, "records"),
        ("Deduplicator near=True (40-word texts)", dedupe_near, "records"),
        ("QualityMonitor.observe + report", quality, "records"),
    ]
    print(f"wintergrab {__version__}, Python {platform.python_version()}, {platform.machine()}, "
          f"{args.records:,} records, median of {args.repeat} runs\n")  # fmt: skip
    print("| Operation | Rate | Time per item |")
    print("|---|---:|---:|")
    for label, fn, unit in rows:
        per_second = rate(fn, args.repeat)
        print(f"| {label} | {per_second:,.0f} {unit}/s | {1e6 / per_second:,.1f} µs |")


if __name__ == "__main__":
    main()
