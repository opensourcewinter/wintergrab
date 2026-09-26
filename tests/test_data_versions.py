"""Dataset differences and versions."""

from __future__ import annotations

import gzip
import json
import stat

import pytest

from wintergrab.cli import main
from wintergrab.data import DatasetVersions, diff_records
from wintergrab.errors import ConfigurationError

DAY1 = [
    {"url": "https://shop.example/p/1", "name": "Phone X", "price": {"amount": 299.0, "currency": "USD"},
     "availability": "InStock", "_confidence": 0.9},
    {"url": "https://shop.example/p/2", "name": "Case", "price": {"amount": 19.0, "currency": "USD"}, "availability": "InStock"},
    {"url": "https://shop.example/p/3", "name": "Cable", "price": {"amount": 9, "currency": "USD"}, "availability": "OutOfStock"},
    {"url": "https://shop.example/p/4", "name": "Charger", "price": {"amount": 25.0, "currency": "USD"}, "availability": "InStock"},
]  # fmt: skip
DAY2 = [
    {"url": "https://shop.example/p/1?utm_source=feed", "name": "Phone X", "price": {"amount": 279.0, "currency": "USD"},
     "availability": "InStock", "_confidence": 0.8},
    {"url": "https://shop.example/p/2", "name": "Case  ", "price": {"amount": 19, "currency": "USD"}, "availability": "OutOfStock"},
    {"url": "https://shop.example/p/3", "name": "Cable", "price": {"amount": 11.0, "currency": "USD"}, "availability": "InStock"},
    {"url": "https://shop.example/p/5", "name": "Screen", "price": {"amount": 49.0, "currency": "USD"}, "availability": "InStock"},
]  # fmt: skip


def test_record_level_differences() -> None:
    diff = diff_records(DAY1, DAY2, key="url")
    assert diff.counts == {"added": 1, "removed": 1, "changed": 3, "unchanged": 0}
    assert bool(diff)
    assert [r["name"] for r in diff.added] == ["Screen"] and [r["name"] for r in diff.removed] == ["Charger"]
    phone = next(c for c in diff.changed if c.key == "https://shop.example/p/1")
    # the tracking parameter does not make another page, "_confidence" is metadata, "Case  " is "Case"
    [price] = phone.fields
    assert (price.field, price.delta, price.ratio) == ("price", -20.0, -0.06689)
    fields = diff.fields()
    assert fields["price"] == {"records": 2, "down": 1, "up": 1, "median": 0.077666}
    assert fields["availability"]["transitions"] == {"InStock -> OutOfStock": 1, "OutOfStock -> InStock": 1}
    text = diff.describe()
    assert text.startswith("+1 added, -1 removed, ~3 changed, 0 unchanged\nfields changed:")
    assert "median -" in text or "median +" in text
    rows = list(diff.rows())
    assert rows[0] == {"change": "added", "record": DAY2[3]}
    assert {"change": "changed", "key": "https://shop.example/p/3", "field": "availability", "old": "OutOfStock",
            "new": "InStock"} in rows  # fmt: skip
    assert diff_records(DAY1, DAY1, key="url").counts == {"added": 0, "removed": 0, "changed": 0, "unchanged": 4}
    assert not diff_records(DAY1, list(reversed(DAY1)), key="url")


def test_options() -> None:
    private = diff_records(DAY1, DAY2, key="url", private=True)
    assert "_confidence" in private.fields()
    ignored = diff_records(DAY1, DAY2, key="url", ignore=["availability"])
    assert "availability" not in ignored.fields() and len(ignored.changed) == 2
    appeared = diff_records([{"id": 1}], [{"id": 1, "rating": 4.5}], key="id")
    assert appeared.fields() == {"rating": {"records": 1, "appeared": 1}}
    assert diff_records([{"id": 1, "a": None}], [{"id": 1}], key="id").unchanged == 1  # None = missing
    other_currency = diff_records(
        [{"id": 1, "price": {"amount": 10, "currency": "USD"}}],
        [{"id": 1, "price": {"amount": 10, "currency": "EUR"}}],
        "id",
    )
    assert other_currency.changed[0].fields[0].delta is None  # no arithmetic across currencies


def test_keys_duplicates_and_records_without_keys() -> None:
    old = [{"sku": "A-1", "n": 1}, {"sku": "a 1", "n": 2}, {"name": "loose"}, {"name": "gone"}]
    new = [{"sku": "a-1", "n": 2}, {"name": "loose"}, {"name": "new"}]
    diff = diff_records(old, new, key="sku")
    # "A-1", "a 1" and "a-1" are one key once case and punctuation are set aside; the last record wins
    assert diff.stats == {"old": 4, "new": 3, "duplicate_keys": 1, "unkeyed": 4}
    assert diff.counts == {"added": 1, "removed": 1, "changed": 0, "unchanged": 2}
    assert diff.added == [{"name": "new"}] and diff.removed == [{"name": "gone"}]
    nested = diff_records([{"id": {"shop": "a", "n": 1}, "x": 1}], [{"id": {"shop": "a", "n": 1}, "x": 2}], key="id.n")
    assert len(nested.changed) == 1
    no_key = diff_records([{"a": 1}, {"a": 2}], [{"a": 2}, {"a": 3}])
    assert no_key.counts == {"added": 1, "removed": 1, "changed": 0, "unchanged": 1}


def test_versions(tmp_path) -> None:
    versions = DatasetVersions(tmp_path / "prices", key="url")
    assert versions.latest is None
    with pytest.raises(ConfigurationError, match="no versions yet"):
        versions.get("latest")
    v1 = versions.commit(DAY1, message="day 1")
    assert (v1.name, v1.records, v1.changes, v1.message) == ("v1", 4, None, "day 1")
    assert versions.commit(list(reversed(DAY1))) is v1  # the same records: nothing new to save
    v2 = versions.commit(DAY2, message="day 2")
    assert v2.changes == {"added": 1, "removed": 1, "changed": 3, "unchanged": 0}
    forced = versions.commit(DAY2, force=True)
    assert forced.number == 3 and forced.changes == {"added": 0, "removed": 0, "changed": 0, "unchanged": 4}
    # reopened from disk: the key and every version are remembered
    reopened = DatasetVersions(tmp_path / "prices")
    assert reopened.key == ["url"] and [v.name for v in reopened.versions] == ["v1", "v2", "v3"]
    assert reopened.load("v1") == DAY1 and reopened.get("previous").number == 2 and reopened.get(3).number == 3
    assert reopened.diff("v1", "v2").counts == v2.changes
    with pytest.raises(ConfigurationError, match="known: v1, v2, v3"):
        reopened.get("v9")
    stored = tmp_path / "prices" / "v2.jsonl.gz"
    assert [json.loads(line) for line in gzip.decompress(stored.read_bytes()).splitlines()] == DAY2
    assert stat.S_IMODE(stored.stat().st_mode) & 0o044  # readable by others, as usual files are
    assert "v2" in v2.describe() and "+1 -1 ~3 =0" in v2.describe()
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "versions.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="not a versions file"):
        DatasetVersions(tmp_path / "broken")


def test_cli_versions_and_diff(tmp_path, capsys) -> None:
    day1, day2 = tmp_path / "day1.jsonl", tmp_path / "day2.jsonl"
    day1.write_text("\n".join(json.dumps(r) for r in DAY1), encoding="utf-8")
    day2.write_text("\n".join(json.dumps(r) for r in DAY2), encoding="utf-8")
    repo = tmp_path / "prices"
    assert main(["data", "commit", str(repo), str(day1)]) == 2  # which field is the key?
    assert "--key" in capsys.readouterr().err
    assert main(["data", "commit", str(repo), str(day1), "--key", "url", "-m", "day 1"]) == 0
    assert "saved v1 (4 records)" in capsys.readouterr().out
    assert main(["data", "commit", str(repo), str(day1)]) == 0
    assert "no changes since v1" in capsys.readouterr().out
    assert main(["data", "commit", str(repo), str(day2), "-m", "day 2"]) == 0
    assert "saved v2 (4 records): +1 added, -1 removed, ~3 changed, 0 unchanged" in capsys.readouterr().out
    assert main(["data", "log", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "2 version(s), key url" in out and out.index("v2") < out.index("v1")

    changes = tmp_path / "changes.jsonl"
    code = main(["data", "diff", f"{repo}@previous", f"{repo}@latest", "-o", str(changes), "--exit-code"])
    assert code == 1  # the datasets differ
    out = capsys.readouterr().out
    assert "@v1 -> " in out and "@v2, records matched by url" in out and "~3 changed" in out
    rows = [json.loads(line) for line in changes.read_text(encoding="utf-8").splitlines()]
    assert [r["change"] for r in rows].count("changed") == 4
    assert main(["data", "diff", str(day1), str(day1), "--exit-code"]) == 0  # no key given: url is used
    assert "matched by url" in capsys.readouterr().out
    assert main(["data", "diff", str(day1), str(day2), "--json", "--key", "name"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["key"] == ["name"] and summary["added"] == 1
    assert main(["data", "diff", f"{repo}@v9", str(repo)]) == 2
    assert main(["data", "log", str(tmp_path)]) == 2
