"""Dataset quality: measure it while a crawl runs, and notice when it degrades.

:class:`QualityMonitor` watches records stream by (it is an item pipeline that
never drops anything) and produces a :class:`QualityReport`:

* **completeness**: share of records with a non-empty value, per field;
* **validity**: share of present values without validation errors (needs a schema);
* **uniqueness**: distinct record keys (or record contents) / records;
* **consistency**: share of values with the field's dominant type and shape
  (``"2024-03-05"`` and ``"05/03/2024"`` have different shapes);
* **freshness**: age of the records, from a timestamp field;
* **confidence**: mean extraction confidence (``_confidence``), when present.

It also flags anomalies inside one dataset (a field with one value in every
record, placeholder values such as ``"N/A"`` or unrendered ``{{ templates }}``,
outliers by median absolute deviation, damaged text) and, compared with a
baseline report from an earlier run, degradation: completeness collapsing
(price present in 98% of records, now 41%), fields disappearing or appearing,
a field's type changing, and numeric distributions shifting (a two-sample
Kolmogorov-Smirnov test at the 0.1% level on reservoir samples).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import re
import statistics
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .issues import Issue
from .normalize import is_placeholder, parse_datetime
from .schema import Schema, encoding_issue
from .similarity import content_hash

if TYPE_CHECKING:
    from ..spider.spider import Spider

__all__ = ["FieldQuality", "QualityMonitor", "QualityReport", "ks_statistic"]

log = logging.getLogger("wintergrab.quality")

REPORT_FORMAT = "wintergrab/quality/v1"
_RESERVOIR = 512
_SHAPE_RUNS = re.compile(r"(9+|a+)")
_TIME_FIELDS = ("_fetched_at", "fetched_at", "scraped_at", "crawled_at", "date_modified", "updated_at")


def _shape(value: str) -> str:
    """``"2024-03-05"`` -> ``"9-9-9"``; ``"$12.99"`` -> ``"$9.9"`` (digit and letter runs collapsed).

    Only formatted values have a meaningful shape: plain words are all ``"text"``
    and URLs all ``"url"`` (a URL's path and query vary by nature).
    """
    value = value.strip()
    if value.startswith(("http://", "https://", "//")):
        return "url"
    if not any(ch.isdigit() for ch in value):
        return "text"
    mapped = "".join("9" if ch.isdigit() else "a" if ch.isalpha() else ch for ch in value[:40])
    return _SHAPE_RUNS.sub(lambda m: m.group(1)[0], mapped)


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


class _Distinct:
    """Exact distinct counting up to ``limit`` values, then a HyperLogLog estimate (precision 14, ~0.8% error)."""

    P = 14
    M = 1 << P

    def __init__(self, limit: int = 100_000) -> None:
        self.limit = limit
        self.exact: set[int] | None = set()
        self.registers: bytearray | None = None

    def add(self, value: Any) -> bool:
        """Add; ``True`` if it was new (only reliable while exact)."""
        h = int.from_bytes(hashlib.blake2b(repr(value).encode(), digest_size=8).digest(), "little")
        if self.exact is not None:
            if h in self.exact:
                return False
            self.exact.add(h)
            if len(self.exact) > self.limit:
                self.registers = bytearray(self.M)
                for item in self.exact:
                    self._hll(item)
                self.exact = None
            return True
        self._hll(h)
        return True

    def _hll(self, h: int) -> None:
        assert self.registers is not None
        index = h & (self.M - 1)
        rest = h >> self.P
        rank = (64 - self.P) - rest.bit_length() + 1 if rest else 64 - self.P + 1
        if rank > self.registers[index]:
            self.registers[index] = rank

    def __len__(self) -> int:
        if self.exact is not None:
            return len(self.exact)
        assert self.registers is not None
        alpha = 0.7213 / (1 + 1.079 / self.M)
        estimate = alpha * self.M * self.M / sum(2.0**-r for r in self.registers)
        zeros = self.registers.count(0)
        if estimate <= 2.5 * self.M and zeros:
            estimate = self.M * math.log(self.M / zeros)
        return int(estimate)


@dataclass
class FieldQuality:
    """What was observed about one field."""

    name: str
    present: int = 0  # non-empty values
    empty: int = 0  # records where the field was missing or empty
    errors: int = 0  # values with validation errors
    warnings: int = 0
    placeholders: int = 0
    damaged_text: int = 0
    kinds: Counter[str] = field(default_factory=Counter)
    shapes: Counter[str] = field(default_factory=Counter)
    values: Counter[str] = field(default_factory=Counter)  # the most frequent values (bounded)
    distinct: int = 0
    numeric: list[float] = field(default_factory=list)  # reservoir sample
    numeric_seen: int = 0
    examples: list[Any] = field(default_factory=list)

    def completeness(self, records: int) -> float:
        return self.present / records if records else 0.0

    def validity(self) -> float | None:
        return 1 - self.errors / self.present if self.present else None

    def consistency(self) -> float | None:
        if not self.present:
            return None
        top_kind = self.kinds.most_common(1)[0][1] / self.present
        if self.kinds.most_common(1)[0][0] != "string" or not self.shapes:
            return round(top_kind, 4)
        shapes = sum(self.shapes.values())
        top_shape = self.shapes.most_common(1)[0][1] / shapes if shapes else 1.0
        # Free text has many shapes by nature; only formatted values (short strings) should agree.
        return round(top_kind * (top_shape if len(self.shapes) <= 20 else 1.0), 4)

    def to_dict(self, records: int) -> dict[str, Any]:
        numeric = sorted(self.numeric)
        stats: dict[str, Any] | None = None
        if numeric:
            stats = {
                "count": self.numeric_seen,
                "min": numeric[0],
                "max": numeric[-1],
                "median": statistics.median(numeric),
                "mean": round(statistics.fmean(numeric), 6),
                "sample": [round(v, 6) for v in numeric],
            }
        return {
            "present": self.present,
            "empty": self.empty,
            "completeness": round(self.completeness(records), 4),
            "validity": None if self.validity() is None else round(self.validity() or 0.0, 4),
            "consistency": self.consistency(),
            "errors": self.errors,
            "warnings": self.warnings,
            "placeholders": self.placeholders,
            "damaged_text": self.damaged_text,
            "distinct": self.distinct,
            "dominant_type": self.kinds.most_common(1)[0][0] if self.kinds else None,
            "top_values": [[v, c] for v, c in self.values.most_common(5)],
            "numeric": stats,
            "examples": self.examples[:3],
        }


def ks_statistic(a: list[float], b: list[float]) -> float:
    """Two-sample Kolmogorov-Smirnov statistic: the largest gap between the two empirical CDFs."""
    if not a or not b:
        return 0.0
    a, b = sorted(a), sorted(b)
    i = j = 0
    gap = 0.0
    while i < len(a) and j < len(b):
        x = min(a[i], b[j])
        while i < len(a) and a[i] <= x:
            i += 1
        while j < len(b) and b[j] <= x:
            j += 1
        gap = max(gap, abs(i / len(a) - j / len(b)))
    return gap


def _ks_critical(n: int, m: int, alpha: float = 0.001) -> float:
    return math.sqrt(-math.log(alpha / 2) / 2) * math.sqrt((n + m) / (n * m))


@dataclass
class QualityReport:
    """The quality of a dataset at one point in time (JSON-serialisable via :meth:`to_dict`)."""

    name: str
    records: int
    created: float
    fields: dict[str, dict[str, Any]]
    metrics: dict[str, float | None]
    issues: list[Issue]
    key: list[str] = field(default_factory=list)

    @property
    def score(self) -> float | None:
        return self.metrics.get("score")

    def to_dict(self) -> dict[str, Any]:
        return {
            "$format": REPORT_FORMAT,
            "name": self.name,
            "records": self.records,
            "created": self.created,
            "key": self.key,
            "metrics": self.metrics,
            "fields": self.fields,
            "issues": [i.to_dict() for i in self.issues],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> QualityReport:
        if data.get("$format") != REPORT_FORMAT:
            raise ValueError(f"not a wintergrab quality report ({data.get('$format')!r})")
        return cls(
            name=str(data.get("name", "dataset")),
            records=int(data.get("records", 0)),
            created=float(data.get("created", 0)),
            fields=dict(data.get("fields") or {}),
            metrics=dict(data.get("metrics") or {}),
            issues=[Issue(**i) for i in data.get("issues") or ()],
            key=list(data.get("key") or []),
        )

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> QualityReport:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def describe(self, *, fields: int = 30) -> str:
        """A readable summary: overall metrics, a line per field, and the anomalies."""

        def pct(value: Any) -> str:
            return "   -" if value is None else f"{float(value):4.0%}"

        score = self.metrics.get("score")
        lines = [f"{self.name}: {self.records:,} record(s), quality score {'-' if score is None else f'{score:.3f}'}"]
        names = ("completeness", "validity", "uniqueness", "consistency", "freshness", "confidence")
        shown = [f"{name} {pct(self.metrics[name]).strip()}" for name in names if self.metrics.get(name) is not None]
        if shown:
            lines.append("  " + "  ".join(shown))
        if self.fields:
            width = max(12, *(len(name) for name in list(self.fields)[:fields]))
            lines.append(f"  {'field'.ljust(width)}  present  valid  consistent  type")
            for name, data in list(self.fields.items())[:fields]:
                lines.append(
                    f"  {name.ljust(width)}  {pct(data.get('completeness')):>7}  {pct(data.get('validity')):>5}"
                    f"  {pct(data.get('consistency')):>10}  {data.get('dominant_type') or '-'}"
                )
            if len(self.fields) > fields:
                lines.append(f"  ... and {len(self.fields) - fields} more field(s)")
        if self.issues:
            lines.append("anomalies:")
            lines.extend(f"  {issue}" for issue in self.issues)
        return "\n".join(lines)

    # -- comparison ---------------------------------------------------------- #
    def compare(
        self,
        baseline: QualityReport,
        *,
        drop: float = 0.2,
        collapse: float = 0.4,
        volume_drop: float = 0.5,
        min_records: int = 20,
    ) -> list[Issue]:
        """Degradation relative to ``baseline`` (an earlier run's report).

        Args:
            drop: A completeness/validity fall of at least this much (absolute) is reported.
            collapse: A completeness fall of at least this much is an ``extraction-collapse`` error.
            volume_drop: Fewer than this share of the baseline's records is a ``volume-drop``.
            min_records: Below this many records on either side, only structural checks run.
        """
        issues: list[Issue] = []
        enough = self.records >= min_records and baseline.records >= min_records
        if baseline.records and self.records < baseline.records * volume_drop:
            issues.append(
                Issue(None, "volume-drop", f"{self.records} records, baseline had {baseline.records}", "warning")
            )
        for name, before in baseline.fields.items():
            now = self.fields.get(name)
            before_c = float(before.get("completeness") or 0)
            if now is None or (float(now.get("completeness") or 0) == 0 and before_c >= 0.5):
                if before_c >= 0.5:
                    issues.append(
                        Issue(
                            name, "field-disappeared", f"was present in {before_c:.0%} of records, now never", "error"
                        )
                    )
                continue
            if not enough:
                continue
            now_c = float(now.get("completeness") or 0)
            if before_c - now_c >= collapse:
                issues.append(
                    Issue(name, "extraction-collapse", f"completeness {before_c:.0%} -> {now_c:.0%}", "error")
                )
            elif before_c - now_c >= drop:
                issues.append(
                    Issue(name, "completeness-drop", f"completeness {before_c:.0%} -> {now_c:.0%}", "warning")
                )
            before_v, now_v = before.get("validity"), now.get("validity")
            if before_v is not None and now_v is not None and before_v - now_v >= drop:
                issues.append(Issue(name, "validity-drop", f"validity {before_v:.0%} -> {now_v:.0%}", "warning"))
            if (
                before.get("dominant_type")
                and now.get("dominant_type")
                and before["dominant_type"] != now["dominant_type"]
            ):
                issues.append(
                    Issue(
                        name,
                        "type-drift",
                        f"values changed from {before['dominant_type']} to {now['dominant_type']}",
                        "warning",
                    )
                )
            issues.extend(_distribution_shift(name, before.get("numeric"), now.get("numeric")))
        for name, now in self.fields.items():
            if name not in baseline.fields and float(now.get("completeness") or 0) >= 0.5:
                issues.append(Issue(name, "field-appeared", "a new field (schema drift)", "info"))
        before_u, now_u = baseline.metrics.get("uniqueness"), self.metrics.get("uniqueness")
        if enough and before_u is not None and now_u is not None and before_u - now_u >= drop / 2:
            issues.append(Issue(None, "duplicates-increase", f"uniqueness {before_u:.0%} -> {now_u:.0%}", "warning"))
        return issues


def _distribution_shift(name: str, before: Any, now: Any) -> list[Issue]:
    if not before or not now:
        return []
    a, b = before.get("sample") or [], now.get("sample") or []
    if len(a) < 30 or len(b) < 30:
        return []
    gap = ks_statistic(a, b)
    critical = _ks_critical(len(a), len(b))
    if gap <= critical:
        return []
    return [
        Issue(
            name,
            "distribution-shift",
            f"values changed (median {before.get('median')} -> {now.get('median')}; KS D={gap:.2f} > {critical:.2f})",
            "warning",
        )
    ]


class QualityMonitor:
    """Measures dataset quality as records stream by. See the module docs.

    Args:
        schema: Validates records (they should be normalized already) and names the fields to measure.
        key: Fields identifying a record (default: the schema's key); uniqueness is measured on them.
        baseline: A :class:`QualityReport`, a path to one, or ``"auto"`` (``crawl_dir/quality.json``
            of the previous run, when the spider has a ``crawl_dir``).
        save_to: Where :meth:`close_spider` writes the report (``"auto"`` = ``crawl_dir/quality.json``).
        time_field: Field with the record's timestamp (default: the first of ``_fetched_at``,
            ``fetched_at``, ``scraped_at``... present).
        max_age: Seconds; the freshness metric is the share of records younger than this.
        outlier_z: Robust z-score (by median absolute deviation) beyond which a number is an outlier.
    """

    def __init__(
        self,
        schema: Schema | None = None,
        *,
        name: str | None = None,
        key: Iterable[str] | None = None,
        baseline: QualityReport | str | Path | None = None,
        save_to: str | Path | None = "auto",
        time_field: str | None = None,
        max_age: float = 7 * 86400,
        outlier_z: float = 8.0,
        seed: int = 0,
    ) -> None:
        self.schema = schema
        self.name = name or (schema.name if schema else "dataset")
        self.key = list(key) if key is not None else (list(schema.key) if schema else [])
        self.baseline_source = baseline
        self.baseline: QualityReport | None = baseline if isinstance(baseline, QualityReport) else None
        self.save_to = save_to
        self.time_field = time_field
        self.max_age = max_age
        self.outlier_z = outlier_z
        self.records = 0
        self.empty_records = 0
        self.fields: dict[str, FieldQuality] = {}
        self._keys = _Distinct()
        self._contents = _Distinct()
        self._key_records = 0
        self._duplicate_keys = 0
        self._ages: list[float] = []
        self._confidences: list[float] = []
        self._rng = random.Random(seed)
        self._distinct: dict[str, _Distinct] = {}
        self.report_: QualityReport | None = None
        self.comparison: list[Issue] = []

    # -- observing ------------------------------------------------------------ #
    def _field(self, name: str) -> FieldQuality:
        fq = self.fields.get(name)
        if fq is None:
            fq = self.fields[name] = FieldQuality(name)
            self._distinct[name] = _Distinct(limit=20_000)
        return fq

    def observe(self, record: Mapping[str, Any]) -> None:
        """Account for one (normalized) record."""
        self.records += 1
        names = self.schema.names if self.schema else [k for k in record if not str(k).startswith("_")]
        if self.schema is not None:
            names = [*names, *(k for k in record if k not in self.schema and not str(k).startswith("_"))]
        issues = self.schema.validate(record) if self.schema is not None else []
        by_field: dict[str, list[Issue]] = {}
        for issue in issues:
            if issue.field:
                by_field.setdefault(issue.field.split("[")[0].split(".")[0], []).append(issue)
        filled = 0
        for name in names:
            fq = self._field(name)
            value = record.get(name)
            if value is None or value == "" or value == [] or value == {}:
                fq.empty += 1
                continue
            filled += 1
            fq.present += 1
            fq.kinds[_kind(value)] += 1
            own = by_field.get(name, [])
            if any(i.severity == "error" for i in own):
                fq.errors += 1
            elif own:
                fq.warnings += 1
            if isinstance(value, str):
                if is_placeholder(value):
                    fq.placeholders += 1
                if encoding_issue(value):
                    fq.damaged_text += 1
                if len(value) <= 40:
                    fq.shapes[_shape(value)] += 1
            text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)[:200]
            if len(fq.values) < 1000 or text in fq.values:
                fq.values[text[:200]] += 1
            if self._distinct[name].add(text):
                fq.distinct = len(self._distinct[name])
            if len(fq.examples) < 3 and text not in fq.examples:
                fq.examples.append(text[:120])
            number = _as_number(value)
            if number is not None:
                fq.numeric_seen += 1
                if len(fq.numeric) < _RESERVOIR:
                    fq.numeric.append(number)
                else:
                    slot = self._rng.randrange(fq.numeric_seen)
                    if slot < _RESERVOIR:
                        fq.numeric[slot] = number
        if not filled:
            self.empty_records += 1
        key = tuple(record.get(k) for k in self.key) if self.key else None
        if key is not None and all(v not in (None, "") for v in key):
            self._key_records += 1
            if not self._keys.add(key):
                self._duplicate_keys += 1
        self._contents.add(content_hash({k: record.get(k) for k in names}))
        self._observe_time(record)
        confidence = record.get("_confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            self._confidences.append(float(confidence))

    def _observe_time(self, record: Mapping[str, Any]) -> None:
        name = self.time_field or next((f for f in _TIME_FIELDS if record.get(f)), None)
        if name is None or record.get(name) in (None, ""):
            return
        value = record[name]
        moment = (
            datetime.fromtimestamp(value, tz=timezone.utc)
            if isinstance(value, (int, float))
            else parse_datetime(str(value))
        )
        if moment is None:
            return
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        self._ages.append(max(0.0, (datetime.now(timezone.utc) - moment).total_seconds()))

    # -- pipeline hooks -------------------------------------------------------- #
    def open_spider(self, spider: Spider) -> None:
        source = self.baseline_source
        path = None
        if source == "auto":
            if spider.crawl_dir:
                path = Path(spider.crawl_dir) / "quality.json"
        elif isinstance(source, (str, Path)):
            path = Path(source)
        if path is not None and path.exists():
            try:
                self.baseline = QualityReport.load(path)
            except (OSError, ValueError) as exc:
                log.warning("could not read the quality baseline %s: %s", path, exc)

    def process_item(self, item: Any, spider: Any = None) -> Any:
        if isinstance(item, Mapping):
            self.observe(item)
        return item

    def close_spider(self, spider: Spider) -> None:
        report = self.report()
        if self.baseline is not None:
            self.comparison = report.compare(self.baseline)
            for issue in self.comparison:
                if issue.severity in ("error", "warning"):
                    spider.events.emit(
                        "quality_degraded", dataset=self.name, field=issue.field, code=issue.code,
                        message=issue.message, severity=issue.severity,
                    )  # fmt: skip
                    log.warning("quality: %s", issue)
        target = self.save_to
        if target == "auto":
            target = Path(spider.crawl_dir) / "quality.json" if spider.crawl_dir else None
        if target:
            report.save(target)

    # -- reporting ------------------------------------------------------------- #
    def report(self) -> QualityReport:
        records = self.records
        fields = {name: fq.to_dict(records) for name, fq in self.fields.items()}
        schema_fields = self.schema.names if self.schema else list(self.fields)
        completeness = [self.fields[n].completeness(records) for n in schema_fields if n in self.fields]
        validity_values = [(fq.present - fq.errors, fq.present) for fq in self.fields.values() if fq.present]
        consistency = [c for fq in self.fields.values() if (c := fq.consistency()) is not None]
        if self.key:
            uniqueness = len(self._keys) / self._key_records if self._key_records else None
        else:
            uniqueness = len(self._contents) / records if records else None
        freshness = None
        if self._ages:
            freshness = sum(1 for age in self._ages if age <= self.max_age) / len(self._ages)
        metrics: dict[str, Any] = {
            "completeness": _mean(completeness),
            "validity": (sum(v for v, _ in validity_values) / sum(n for _, n in validity_values))
            if self.schema is not None and validity_values
            else None,
            "uniqueness": uniqueness,
            "consistency": _mean(consistency),
            "freshness": freshness,
            "median_age_seconds": statistics.median(self._ages) if self._ages else None,
            "confidence": _mean(self._confidences),
        }
        weights = {"completeness": 0.3, "validity": 0.3, "uniqueness": 0.15, "consistency": 0.15, "confidence": 0.1}
        available = {k: w for k, w in weights.items() if metrics.get(k) is not None}
        metrics["score"] = (
            sum(float(metrics[k]) * w for k, w in available.items()) / sum(available.values()) if available else None
        )
        metrics = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in metrics.items()}
        report = QualityReport(
            name=self.name,
            records=records,
            created=time.time(),
            fields=fields,
            metrics=metrics,
            issues=self._anomalies(),
            key=self.key,
        )
        self.report_ = report
        return report

    def _anomalies(self) -> list[Issue]:
        issues: list[Issue] = []
        if self._duplicate_keys:
            issues.append(
                Issue(None, "duplicate", f"{self._duplicate_keys} record(s) repeat a key {self.key}", "warning")
            )
        if self.empty_records:
            issues.append(Issue(None, "empty-record", f"{self.empty_records} record(s) have no values", "warning"))
        for name, fq in self.fields.items():
            if (
                fq.present >= 20
                and fq.distinct == 1
                and fq.kinds.most_common(1)[0][0] in ("string", "number", "integer")
            ):
                value = fq.values.most_common(1)[0][0] if fq.values else ""
                issues.append(
                    Issue(name, "constant-field", f"every one of {fq.present} values is {value[:60]!r}", "warning")
                )
            if fq.placeholders:
                issues.append(
                    Issue(
                        name, "placeholder", f"{fq.placeholders} placeholder value(s) (N/A, {{{{...}}}}...)", "warning"
                    )
                )
            if fq.damaged_text:
                issues.append(Issue(name, "encoding", f"{fq.damaged_text} value(s) with damaged text", "warning"))
            outliers = _outliers(fq.numeric, self.outlier_z)
            if outliers:
                issues.append(
                    Issue(name, "outlier", f"{len(outliers)} outlier(s) in the sample, e.g. {outliers[:3]}", "warning")
                )
        return issues

    def __repr__(self) -> str:
        return f"QualityMonitor({self.name!r}, records={self.records})"


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, Mapping):
        for key in ("amount", "value"):
            inner = value.get(key)
            if isinstance(inner, (int, float)) and not isinstance(inner, bool):
                return float(inner)
    return None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _outliers(sample: list[float], z: float) -> list[float]:
    """Values whose robust z-score (``0.6745 * |x - median| / MAD``) exceeds ``z``."""
    if len(sample) < 20:
        return []
    median = statistics.median(sample)
    mad = statistics.median(abs(x - median) for x in sample)
    if mad == 0:
        return []
    return sorted({x for x in sample if 0.6745 * abs(x - median) / mad > z})
