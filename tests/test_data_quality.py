"""Dataset quality: metrics, anomalies, degradation against a baseline, crawl integration."""

from __future__ import annotations

import json
import random
import time

import pytest

from wintergrab import Spider
from wintergrab.data import QualityMonitor, QualityReport, Schema, ks_statistic
from wintergrab.data.quality import _Distinct

SCHEMA = Schema.from_dict(
    {
        "name": "products",
        "key": ["url"],
        "fields": {
            "name": {"type": "string", "required": True},
            "price": {"type": "number", "required": True, "minimum": 0},
            "url": {"type": "url", "required": True},
        },
    }
)


def records(n: int, *, seed: int = 0, price_share: float = 1.0, shift: float = 0.0, start: int = 0) -> list[dict]:
    rng = random.Random(seed)
    out = []
    for i in range(start, start + n):
        record = {"name": f"Product {i}", "url": f"https://shop.example/p/{i}"}
        if rng.random() < price_share:
            record["price"] = round(rng.gauss(20 + shift, 4), 2)
        out.append(record)
    return out


def monitor_of(rows: list[dict], **options) -> QualityMonitor:
    monitor = QualityMonitor(SCHEMA, save_to=None, **options)
    for row in rows:
        monitor.observe(row)
    return monitor


def test_metrics_of_a_clean_dataset() -> None:
    report = monitor_of(records(200)).report()
    assert report.records == 200
    assert report.metrics["completeness"] == 1.0
    assert report.metrics["validity"] == 1.0
    assert report.metrics["uniqueness"] == 1.0
    assert report.metrics["score"] >= 0.95
    assert report.issues == []
    price = report.fields["price"]
    assert price["dominant_type"] == "number" and price["numeric"]["count"] == 200
    assert 17 < price["numeric"]["median"] < 23


def test_validity_uses_the_schema() -> None:
    rows = records(100)
    for row in rows[:25]:
        row["price"] = -1
    report = monitor_of(rows).report()
    assert report.fields["price"]["validity"] == 0.75
    assert report.fields["price"]["errors"] == 25


def test_anomalies_within_one_dataset() -> None:
    rows = records(60, seed=1)
    for row in rows:
        row["brand"] = "Acme"  # the same value everywhere: a broken selector?
    rows[0]["name"] = "N/A"
    rows[1]["name"] = "{{ product.name }}"
    rows[2]["name"] = "CafÃ©"
    rows[3]["price"] = 99999.0
    rows.append(dict(rows[5]))  # a repeated key
    rows.append({})
    report = monitor_of(rows).report()
    codes = {(i.field, i.code) for i in report.issues}
    assert codes == {
        (None, "duplicate"),
        (None, "empty-record"),
        ("brand", "constant-field"),
        ("name", "placeholder"),
        ("name", "encoding"),
        ("price", "outlier"),
    }
    assert report.metrics["uniqueness"] < 1.0


def test_consistency_looks_at_formats_not_words() -> None:
    rows = [
        {"name": name, "date": date}
        for name, date in [
            ("A thing", "2024-01-05"),
            ("Thing", "05/01/2024"),
            ("Another longer thing", "2024-01-06"),
            ("X", "2024-01-07"),
        ]
    ]
    monitor = QualityMonitor(None, save_to=None)
    for row in rows:
        monitor.observe(row)
    fields = monitor.report().fields
    assert fields["name"]["consistency"] == 1.0  # plain words all have the same "shape"
    assert fields["date"]["consistency"] == 0.75  # one date is written differently


def test_freshness_and_confidence() -> None:
    now = time.time()
    rows = [
        {"name": "a", "_fetched_at": now - 60, "_confidence": 0.9},
        {"name": "b", "_fetched_at": now - 3 * 86400, "_confidence": 0.7},
        {"name": "c", "fetched_at": "2001-01-01T00:00:00Z"},
    ]
    monitor = QualityMonitor(None, save_to=None, max_age=86400)
    for row in rows:
        monitor.observe(row)
    metrics = monitor.report().metrics
    assert metrics["freshness"] == pytest.approx(1 / 3, abs=1e-3)
    assert metrics["confidence"] == pytest.approx(0.8)


def test_comparison_finds_degradation() -> None:
    baseline = monitor_of(records(300)).report()
    # the same distribution again: nothing to report
    assert monitor_of(records(300, seed=9)).report().compare(baseline) == []
    collapsed = monitor_of(records(300, seed=2, price_share=0.35)).report().compare(baseline)
    assert [(i.field, i.code, i.severity) for i in collapsed] == [("price", "extraction-collapse", "error")]
    dropped = monitor_of(records(300, seed=3, price_share=0.7)).report().compare(baseline)
    assert [(i.field, i.code) for i in dropped] == [("price", "completeness-drop")]
    shifted = monitor_of(records(300, seed=4, shift=15)).report().compare(baseline)
    assert [(i.field, i.code) for i in shifted] == [("price", "distribution-shift")]


def test_comparison_finds_schema_drift_and_volume_changes() -> None:
    baseline = monitor_of(records(100)).report()
    rows = records(30, seed=5)
    for row in rows:
        row.pop("name")
        row["price"] = str(row["price"])
        row["title"] = "new field"
    rows += rows[:10]  # duplicates too
    monitor = QualityMonitor(None, key=["url"], save_to=None)
    for row in rows:
        monitor.observe(row)
    codes = {(i.field, i.code) for i in monitor.report().compare(baseline)}
    assert codes == {
        (None, "volume-drop"),
        ("name", "field-disappeared"),
        ("price", "type-drift"),
        ("title", "field-appeared"),
        (None, "duplicates-increase"),
    }


def test_reports_round_trip_and_describe(tmp_path) -> None:
    report = monitor_of(records(50)).report()
    path = report.save(tmp_path / "q" / "quality.json")
    again = QualityReport.load(path)
    assert again.to_dict() == json.loads(json.dumps(report.to_dict()))
    text = report.describe()
    assert text.startswith("products: 50 record(s), quality score")
    assert "price" in text and "completeness 100%" in text
    with pytest.raises(ValueError):
        QualityReport.from_dict({"$format": "something else"})


def test_distinct_counting_switches_to_an_estimate() -> None:
    counter = _Distinct(limit=1000)
    for i in range(50_000):
        counter.add(f"value-{i}")
        counter.add(f"value-{i}")  # repeats do not count
    assert abs(len(counter) - 50_000) / 50_000 < 0.03


def test_ks_statistic() -> None:
    rng = random.Random(0)
    a = [rng.gauss(0, 1) for _ in range(400)]
    b = [rng.gauss(0, 1) for _ in range(400)]
    c = [rng.gauss(2, 1) for _ in range(400)]
    assert ks_statistic(a, a) == 0.0
    assert ks_statistic(a, b) < 0.15 < 0.5 < ks_statistic(a, c)
    assert ks_statistic([], a) == 0.0


def test_quality_in_a_crawl_compares_runs(site, tmp_path) -> None:
    class Shop(Spider):
        log_level = None
        obey_robots_txt = False
        autothrottle = False
        start_urls = [site.url + f"/product/{i}" for i in range(1, 31)]
        crawl_dir = str(tmp_path / "crawl")
        broken = False

        def parse(self, response):
            price = response.css(".price::text").get()
            yield {
                "name": response.css("h1::text").get(),
                "price": None if self.broken else float(price.strip("$")),
                "url": response.url,
            }

    first = Shop(pipelines=[QualityMonitor(SCHEMA)]).run(resume=False)
    assert first.stats["items"] == 30
    saved = QualityReport.load(tmp_path / "crawl" / "quality.json")
    assert saved.records == 30 and saved.metrics["completeness"] == 1.0

    events = []
    second = Shop(pipelines=[QualityMonitor(SCHEMA, baseline="auto")], broken=True)
    second.events.subscribe(events.append, kinds=["quality_degraded"])
    second.run(resume=False)
    assert [(e["field"], e["code"], e["severity"]) for e in events] == [("price", "field-disappeared", "error")]
