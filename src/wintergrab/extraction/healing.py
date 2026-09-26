"""Self-healing extractors: versions, health, validated repairs, rollback, and a person when unsure.

::

    extractor = HealingExtractor("extractors/shop-product", schema="product.schema.json",
                                 review="shop.reviews.jsonl")
    record = extractor.extract(response)      # like Extractor.extract
    print(extractor.status())                 # versions, fields' health, repairs
    print(extractor.why("seller", response))  # why a field is empty on a page

A :class:`HealingExtractor` is an :class:`~wintergrab.extraction.Extractor`
that watches the fields it reads with selectors (the schema's ``selectors``).
For each it follows how often the selectors still match: when that falls to
half of what it was (after a redesign, say), it looks for a replacement:

1. **relocation**: on the pages where the selectors failed, the element most
   like the one they used to match (tag, attributes, text, position: the
   fingerprints of :mod:`wintergrab.adaptive`);
2. **anchoring**: the element holding the value that other strategies
   (JSON-LD, meta tags...) still find on those pages.

Each candidate selector is tested on the failing pages before anything
changes: how many it reads a valid value on, whether its values look like
the field's past values (not one constant where values varied, numbers in
the same range), whether they agree with other strategies, and whether it
reproduces the regression examples (fixtures) people confirmed. A candidate
scoring 0.9 or more whose values other strategies confirm (on two pages at
least) becomes the new version of the extractor (logged, the old selectors
kept behind it): resemblance alone never repairs a field. Any other
candidate goes to the review queue, as does a field with no candidate. A
repair that does not hold on the next pages is rolled back.

Everything lives in the extractor's directory: ``v1.json``, ``v2.json``...
(the schema of each version), ``versions.json`` (which is active, and why
each exists), ``repairs.jsonl`` (every attempt, applied or not, and every
rollback) and ``fixtures/`` (pages with confirmed values).
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import time
from collections import Counter, deque
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from lxml import etree

from ..adaptive.fingerprint import fingerprint, relocate
from ..data.normalize import Money, Quantity
from ..data.schema import Schema, SchemaField
from ..errors import ConfigurationError
from ..fetchers.response import Response
from ..parser.text import own_text, tag_name, text_content
from .engine import ExtractedRecord, Extractor, value_key
from .page import PageContext
from .review import ReviewItem, ReviewQueue, unpack_html
from .strategies import Selectors

__all__ = ["ExtractorVersion", "ExtractorVersions", "Fixture", "HealingExtractor", "RepairResult"]

log = logging.getLogger("wintergrab.extraction")

_STABLE = re.compile(r"[A-Za-z_][\w-]{0,40}")
_VOLATILE = re.compile(r"\d{3,}|[0-9a-f]{8,}|^(?:ng|css|sc|jsx|emotion)-", re.I)
_NOT_VALUES = frozenset({"script", "style", "title", "meta", "head", "html", "body", "noscript", "template"})
_MOUNT_ATTRS = ("itemprop", "data-testid", "data-test", "data-qa", "data-field", "data-name", "property", "name")


# ---------------------------------------------------------------------------------------------- #
# versions
# ---------------------------------------------------------------------------------------------- #
@dataclass
class ExtractorVersion:
    """One version of an extractor's schema.

    Attributes:
        number: 1, 2, 3...
        reason: Why it exists ("initial", "repair of seller: .seller-name -> span.vendor").
        by: ``"initial"``, ``"auto"`` (a repair applied on its own) or ``"human"``.
        status: ``"active"``, ``"retired"`` (was active), ``"candidate"`` (waiting for a decision),
            ``"accepted"`` (its change went into a later version), ``"rejected"``, ``"superseded"`` (a
            later candidate replaced it) or ``"rolled back"``.
        parent: The version it was made from.
        validation: What its repair's test on real pages gave, if it came from one.
    """

    number: int
    reason: str
    by: str
    status: str
    parent: int | None = None
    created: float = dataclass_field(default_factory=time.time)
    validation: dict[str, Any] = dataclass_field(default_factory=dict)


@dataclass
class Fixture:
    """A page with values people confirmed: a regression example for later versions."""

    url: str
    expected: dict[str, Any]
    html: bytes
    by: str = "human"


class ExtractorVersions:
    """An extractor's versions, repair log and fixtures, in a directory (see the module docs).

    Args:
        directory: Where they live (created if needed).
        schema: The first version's schema (a :class:`~wintergrab.data.Schema`, its dict form or a
            file), when the directory is new; ignored otherwise.
    """

    def __init__(
        self, directory: str | os.PathLike[str], schema: Schema | Mapping[str, Any] | str | Path | None = None
    ) -> None:
        self.directory = Path(directory)
        self._manifest = self.directory / "versions.json"
        self.versions: list[ExtractorVersion] = []
        self.active_number = 0
        if self._manifest.exists():
            data = json.loads(self._manifest.read_text(encoding="utf-8"))
            self.versions = [ExtractorVersion(**v) for v in data.get("versions") or ()]
            self.active_number = int(data.get("active") or 0)
        elif schema is not None:
            self.directory.mkdir(parents=True, exist_ok=True)
            first = _as_schema(schema)
            self._write_schema(1, first)
            self.versions = [ExtractorVersion(1, "initial", "initial", "active")]
            self.active_number = 1
            self._save()
            self.log({"event": "created", "version": 1})
        else:
            raise ConfigurationError(f"{self.directory} holds no extractor: give a schema to start one")

    # -- storage ---------------------------------------------------------------------------- #
    def _write_schema(self, number: int, schema: Schema) -> None:
        data = schema.to_dict()
        data["version"] = number
        _atomic_write(self.directory / f"v{number}.json", json.dumps(data, indent=2, ensure_ascii=False))

    def _save(self) -> None:
        data = {"active": self.active_number, "versions": [asdict(v) for v in self.versions]}
        _atomic_write(self._manifest, json.dumps(data, indent=2))

    def log(self, entry: dict[str, Any]) -> None:
        """Add an entry to the repair log."""
        entry = {"at": time.time(), **entry}
        with (self.directory / "repairs.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def history(self) -> list[dict[str, Any]]:
        """The repair log, oldest first."""
        path = self.directory / "repairs.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def load_state(self) -> dict[str, Any]:
        """What a :class:`HealingExtractor` learned in earlier runs (baselines, matched elements)."""
        path = self.directory / "state.json"
        try:
            return dict(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else {}
        except (OSError, ValueError):
            return {}

    def save_state(self, state: Mapping[str, Any]) -> None:
        _atomic_write(self.directory / "state.json", json.dumps(state, ensure_ascii=False, default=str))

    # -- versions --------------------------------------------------------------------------- #
    @property
    def active(self) -> ExtractorVersion:
        return self.get(self.active_number)

    def get(self, number: int) -> ExtractorVersion:
        for version in self.versions:
            if version.number == number:
                return version
        raise ConfigurationError(f"{self.directory} has no version {number}")

    def schema(self, number: int | None = None) -> Schema:
        number = number or self.active_number
        return Schema.from_dict(json.loads((self.directory / f"v{number}.json").read_text(encoding="utf-8")))

    def add(
        self, schema: Schema, *, reason: str, by: str, status: str = "active", validation: dict[str, Any] | None = None
    ) -> ExtractorVersion:
        """Save ``schema`` as a new version; ``status="active"`` makes it the one in use."""
        number = max(v.number for v in self.versions) + 1
        self._write_schema(number, schema)
        version = ExtractorVersion(number, reason, by, status, self.active_number, validation=dict(validation or {}))
        self.versions.append(version)
        if status == "active":
            self.active.status = "retired"
            self.active_number = number
        self._save()
        self.log({"event": "version", "version": number, "status": status, "by": by, "reason": reason})
        return version

    def activate(self, number: int, *, reason: str, by: str = "human") -> ExtractorVersion:
        """Make version ``number`` the active one."""
        version = self.get(number)
        if number != self.active_number:
            self.active.status = "retired"
            version.status = "active"
            self.active_number = number
            self._save()
        self.log({"event": "activated", "version": number, "by": by, "reason": reason})
        return version

    def rollback(self, *, reason: str, by: str = "auto") -> ExtractorVersion:
        """Go back to the version the active one was made from (it is marked "rolled back")."""
        current = self.active
        if current.parent is None:
            raise ConfigurationError("the first version has nothing to roll back to")
        previous = self.get(current.parent)
        current.status = "rolled back"
        previous.status = "active"
        self.active_number = previous.number
        self._save()
        self.log({"event": "rolled back", "from": current.number, "to": previous.number, "by": by, "reason": reason})
        return previous

    def revert(self, number: int, name: str, *, reason: str, by: str = "auto") -> ExtractorVersion:
        """Undo what version ``number`` changed in field ``name``: the active version goes back to the
        one it was made from (:meth:`rollback`); an older one is undone by a new version that keeps
        the changes made since."""
        version = self.get(number)
        if number == self.active_number:
            return self.rollback(reason=reason, by=by)
        if version.parent is None or name not in self.schema(version.parent):
            raise ConfigurationError(f"version {number} changed nothing in {name} to roll back")
        restored = _with_selectors(self.schema(), name, list(self.schema(version.parent)[name].selectors))
        version.status = "rolled back"
        new = self.add(restored, reason=f"rollback of version {number} ({name}): {reason}", by=by)
        self.log({"event": "rolled back", "from": number, "to": new.number, "field": name, "by": by, "reason": reason})
        return new

    def mark(self, number: int, status: str) -> None:
        """Set a version's status (``"accepted"``: a candidate whose change went into a later version)."""
        self.get(number).status = status
        self._save()

    def reject(self, number: int, *, reason: str, by: str = "human") -> None:
        version = self.get(number)
        if version.status == "active":
            raise ConfigurationError(f"version {number} is active: roll it back instead")
        version.status = "rejected"
        self._save()
        self.log({"event": "rejected", "version": number, "by": by, "reason": reason})

    def diff(self, old: int, new: int) -> list[str]:
        """What changed between two versions, field by field (selectors and types)."""
        a = {f.name: f for f in self.schema(old).fields}
        b = {f.name: f for f in self.schema(new).fields}
        out = []
        for name in dict.fromkeys([*a, *b]):
            if name not in a:
                out.append(f"+ {name}")
            elif name not in b:
                out.append(f"- {name}")
            elif a[name].to_dict() != b[name].to_dict():
                out.append(f"~ {name}: selectors {a[name].selectors} -> {b[name].selectors}")
        return out

    # -- fixtures ----------------------------------------------------------------------------- #
    def add_fixture(self, url: str, html: bytes | str, expected: Mapping[str, Any], *, by: str = "human") -> Fixture:
        """Keep a page and values confirmed on it: later repairs must reproduce them."""
        folder = self.directory / "fixtures"
        folder.mkdir(exist_ok=True)
        raw = html.encode("utf-8") if isinstance(html, str) else html
        number = len(list(folder.glob("*.json.gz"))) + 1
        values = {str(k): _plain(v) for k, v in expected.items()}  # (typed values, a Money, as JSON holds them)
        payload = {"url": url, "expected": values, "by": by, "html": raw.decode("utf-8", "replace")}
        (folder / f"{number:04d}.json.gz").write_bytes(gzip.compress(json.dumps(payload).encode("utf-8")))
        self.log({"event": "fixture", "url": url, "fields": sorted(values), "by": by})
        return Fixture(url, values, raw, by)

    def fixtures(self) -> list[Fixture]:
        folder = self.directory / "fixtures"
        out = []
        for path in sorted(folder.glob("*.json.gz")) if folder.exists() else ():
            data = json.loads(gzip.decompress(path.read_bytes()))
            out.append(Fixture(data["url"], data["expected"], data["html"].encode("utf-8"), data.get("by", "human")))
        return out

    def check_fixtures(self, schema: Schema | None = None) -> list[str]:
        """Regression test: where ``schema`` (the active version by default) does not reproduce a fixture."""
        extractor = Extractor(schema or self.schema(), min_confidence=0)
        failures = []
        for fixture in self.fixtures():
            record = extractor.extract(_response(fixture.url, fixture.html))
            for name, expected in fixture.expected.items():
                got = record.data.get(name)
                if not _same(got, _normalized(extractor.schema, name, expected)):
                    failures.append(f"{fixture.url}: {name} is {got!r}, expected {expected!r}")
        return failures


def _with_selectors(schema: Schema, name: str, selectors: list[str]) -> Schema:
    """``schema`` with other selectors for field ``name``."""
    data = schema.to_dict()
    data["fields"][name] = {**data["fields"][name], "selectors": list(selectors)}
    return Schema.from_dict(data)


def _only(schema: Schema, name: str) -> Schema:
    """A schema of the one field ``name``."""
    data = schema.to_dict()
    data.pop("key", None)
    data["fields"] = {name: data["fields"][name]}
    return Schema.from_dict(data)


def _as_schema(schema: Schema | Mapping[str, Any] | str | Path) -> Schema:
    if isinstance(schema, Schema):
        return schema
    if isinstance(schema, Mapping):
        return Schema.from_dict(schema)
    from .templates import schema_named

    return schema_named(schema)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _response(url: str, html: bytes) -> Response:
    return Response(url, headers={"content-type": "text/html; charset=utf-8"}, body=html)


def _normalized(schema: Schema, name: str, value: Any) -> Any:
    result = schema.normalize_value(name, value)
    return result.value if result.ok else value


def _plain(value: Any) -> Any:
    """``value`` as JSON can hold it."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    return str(value)


def _key(value: Any) -> Any:
    """:func:`~wintergrab.extraction.value_key` of plain values too (a price as ``{"amount", "currency"}``)."""
    if isinstance(value, dict):
        try:
            if "amount" in value and set(value) <= {"amount", "currency"}:
                return value_key(Money(Decimal(str(value["amount"])), value.get("currency")))
            if "value" in value and "unit" in value and set(value) <= {"value", "unit"}:
                return value_key(Quantity(Decimal(str(value["value"])), value["unit"]))
        except (InvalidOperation, TypeError, ValueError):
            pass
    return value_key(value)


def _same(a: Any, b: Any) -> bool:
    """Whether two values, plain or typed, are the same (prices in two different currencies are not)."""
    currencies = {c for c in (_currency(a), _currency(b)) if c}
    return _key(a) == _key(b) and len(currencies) <= 1


def _currency(value: Any) -> str | None:
    if isinstance(value, Money):
        return value.currency
    return value.get("currency") if isinstance(value, dict) else None


# ---------------------------------------------------------------------------------------------- #
# health
# ---------------------------------------------------------------------------------------------- #
class _Health:
    """How often a field's selectors matched: at first (the baseline), and lately."""

    def __init__(self, baseline_pages: int, window: int, saved: float | None = None) -> None:
        self.baseline_pages = baseline_pages
        self.saved = saved  # a baseline learned in an earlier run
        self.first: list[bool] = []
        self.recent: deque[bool] = deque(maxlen=window)

    def add(self, hit: bool) -> None:
        if self.saved is None and len(self.first) < self.baseline_pages:
            self.first.append(hit)
        self.recent.append(hit)

    @property
    def baseline(self) -> float | None:
        if self.saved is not None:
            return self.saved
        return sum(self.first) / len(self.first) if len(self.first) >= self.baseline_pages else None

    def rate(self, last: int) -> float | None:
        if len(self.recent) < last:
            return None
        tail = list(self.recent)[-last:]
        return sum(tail) / len(tail)


@dataclass
class RepairResult:
    """What a repair attempt found and did.

    Attributes:
        field: The field.
        applied: A new version is active.
        queued: The candidate (or the breakage) went to the review queue.
        selector: The best candidate, if any.
        confidence: Its score, 0-1.
        version: The version it made (active, or a candidate waiting for review).
        checked: How the best candidate did on the failing pages (coverage, agreement...).
        candidates: Every candidate tried, with its score.
    """

    field: str
    applied: bool = False
    queued: bool = False
    selector: str | None = None
    confidence: float = 0.0
    version: int | None = None
    checked: dict[str, Any] = dataclass_field(default_factory=dict)
    candidates: list[dict[str, Any]] = dataclass_field(default_factory=list)


# ---------------------------------------------------------------------------------------------- #
# the extractor
# ---------------------------------------------------------------------------------------------- #
class HealingExtractor:
    """An :class:`~wintergrab.extraction.Extractor` that repairs its selectors (see the module docs).

    Args:
        directory: The extractor's directory (versions, log, fixtures).
        schema: The schema to start from when the directory is new.
        review: A :class:`~wintergrab.extraction.review.ReviewQueue` (or its file) for what needs a person.
        auto_apply: Repairs scoring at least this, whose values other strategies confirm on two of the
            failing pages or more, are applied without asking.
        min_pages: Pages it takes to know how often a field's selectors match, and to judge a repair.
        drop: A field is broken when its selectors match this share of what they used to, or less.
        keep: Failing pages kept per field to test repairs on.
        review_below: Values found with less confidence than this go to the review queue (with
            ``review``), when one or two other values were found; at most ``max_value_reviews`` per
            field and run.
        extractor_options: More :class:`~wintergrab.extraction.Extractor` options.
    """

    def __init__(
        self,
        directory: str | os.PathLike[str],
        schema: Schema | Mapping[str, Any] | str | Path | None = None,
        *,
        review: ReviewQueue | str | os.PathLike[str] | None = None,
        auto_apply: float = 0.9,
        min_pages: int = 10,
        drop: float = 0.5,
        keep: int = 12,
        review_below: float = 0.5,
        max_value_reviews: int = 20,
        **extractor_options: Any,
    ) -> None:
        self.versions = ExtractorVersions(directory, schema)
        self.review = review if isinstance(review, ReviewQueue) or review is None else ReviewQueue(review)
        self.auto_apply = auto_apply
        self.min_pages = max(3, min_pages)
        self.drop = drop
        self.keep = keep
        self.review_below = review_below
        self.max_value_reviews = max_value_reviews
        self.options = extractor_options
        self._load()
        self._resume_probation()
        self.apply_reviews()

    # -- state --------------------------------------------------------------------------------- #
    def _load(self) -> None:
        """(Re)load the active version. Fields whose selectors did not change keep what was learned
        about them (their health, the elements they matched, the pages where they failed)."""
        before = {f.name: list(f.selectors) for f in getattr(self, "_watched", [])}
        self.schema = self.versions.schema()
        self._extractor = Extractor(self.schema, **self.options)
        self._watched = [f for f in self.schema.fields if f.selectors]
        if not hasattr(self, "_health"):
            self._health: dict[str, _Health] = {}
            self._good: dict[str, deque[dict[str, Any]]] = {}
            self._failing: dict[str, deque[dict[str, Any]]] = {}
            self._tried: dict[str, int] = {}
            self._pages = 0
            self._probation: dict[str, dict[str, Any]] = {}  # field -> the automatic repair being judged
            self._value_reviews: Counter[str] = Counter()
            self._baselines: dict[tuple[str, tuple[str, ...]], float] = {}  # (field, selectors) -> baseline
        state = self.versions.load_state()
        saved = self._saved = state.get("fields", {})
        for entry in state.get("baselines") or ():
            self._baselines.setdefault((entry["field"], tuple(entry["selectors"])), entry["baseline"])
        watched = {f.name for f in self._watched}
        for gone in [n for n in self._health if n not in watched]:  # a version without selectors for it
            for table in (self._health, self._failing, self._probation):
                table.pop(gone, None)
        for f in self._watched:
            if before.get(f.name) == list(f.selectors) and f.name in self._health:
                continue
            known = saved.get(f.name) or {}
            # selectors seen before (a rollback returns to them) keep their baseline
            baseline = self._baselines.get((f.name, tuple(f.selectors)))
            self._health[f.name] = _Health(self.min_pages, 4 * self.min_pages, baseline)
            good = self._good.setdefault(f.name, deque(maxlen=5))  # what the old selectors matched helps later repairs
            if not good:
                good.extend(known.get("good") or ())
            self._failing[f.name] = deque(maxlen=self.keep)

    @property
    def name(self) -> str:
        return self._extractor.name

    def save(self) -> None:
        """Keep what was learned (baselines, matched elements) for the next run; done every 50 pages,
        when a baseline is learned, and on :meth:`close`."""
        fields = {}
        for f in self._watched:
            health = self._health[f.name]
            if health.baseline is None:
                continue
            self._baselines[(f.name, tuple(f.selectors))] = health.baseline
            fields[f.name] = {"selectors": list(f.selectors), "baseline": health.baseline,
                              "good": list(self._good[f.name])}  # fmt: skip
            seen = min(len(health.recent), self.min_pages)
            known = self._saved.get(f.name) or {}
            if seen:
                fields[f.name].update(lately=health.rate(seen), seen=seen)
            elif known.get("selectors") == list(f.selectors) and "lately" in known:
                fields[f.name].update(lately=known["lately"], seen=known.get("seen"))  # nothing new this run
        baselines = [{"field": n, "selectors": list(q), "baseline": b} for (n, q), b in self._baselines.items()]
        self.versions.save_state({"version": self.versions.active_number, "fields": fields,
                                  "baselines": baselines[-100:], "probation": self._probation})  # fmt: skip

    def _resume_probation(self) -> None:
        """Repairs the last run had no time to judge, still in effect, are judged in this one."""
        saved = self.versions.load_state().get("probation")
        for name, watch in saved.items() if isinstance(saved, dict) else ():
            try:
                repaired = self.versions.schema(int(watch["version"]))[name].selectors
            except (ConfigurationError, KeyError, OSError, TypeError, ValueError):
                continue
            if name in self.schema and self.schema[name].selectors == repaired:
                self._probation[name] = watch

    def close(self) -> None:
        self.save()

    def __enter__(self) -> HealingExtractor:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- extracting ---------------------------------------------------------------------------- #
    def extract(self, page: Any, *, url: str | None = None) -> ExtractedRecord:
        """Extract a record (like :meth:`Extractor.extract`), watching the fields and healing them."""
        ctx = PageContext(page, url=url)
        record = self._extractor.extract(ctx, url=url)
        try:
            self._observe(ctx, page, record)
        except Exception as exc:  # healing must never break extraction
            log.warning("self-healing: could not watch %s: %s", ctx.url, exc)
        return record

    def extract_all(self, page: Any, **options: Any) -> list[ExtractedRecord]:
        """Records of a listing (not watched: listings are :meth:`extract`'s job for detail pages)."""
        return self._extractor.extract_all(page, **options)

    def _observe(self, ctx: PageContext, page: Any, record: ExtractedRecord) -> None:
        self._pages += 1
        learning = [f.name for f in self._watched if self._health[f.name].baseline is None]
        body = getattr(page, "body", None) or (page.encode("utf-8") if isinstance(page, str) else b"")
        for f in self._watched:
            fv = record.fields.get(f.name)
            if fv is None:
                continue
            hit = (
                fv.method == "selector"
                or "selector" in fv.agreed
                or any("selector" in alt.get("methods", ()) for alt in fv.alternatives)
            )
            self._health[f.name].add(hit)
            if hit and fv.source and fv.source.startswith("selector:"):
                good = self._good[f.name]
                if len(good) < good.maxlen or self._pages % 10 == 0:  # type: ignore[operator]
                    self._remember(ctx, f, fv.source.split(":", 1)[1], fv.value)
            elif not hit and body:
                self._failing[f.name].append({"url": ctx.url or "", "html": body, "other": fv.value})
        self._review_values(ctx, body, record)
        if self._pages % 50 == 0 or any(self._health[n].baseline is not None for n in learning):
            self.save()  # every 50 pages, and as soon as a baseline is known
        self._judge_probation()
        for f in self._watched:
            if self._broken(f.name):
                self.repair(f.name)

    def _remember(self, ctx: PageContext, f: SchemaField, query: str, value: Any) -> None:
        try:
            matches = ctx.root.select(query)
        except Exception:
            return
        element = next((m.root for m in matches if m.root is not None), None)
        if element is not None and isinstance(element.tag, str):
            self._good[f.name].append({"url": ctx.url, "fingerprint": fingerprint(element), "value": value})

    def _broken(self, name: str) -> bool:
        health = self._health[name]
        baseline = health.baseline
        rate = health.rate(self.min_pages)
        if baseline is None or rate is None or baseline < 0.5 or rate > baseline * self.drop:
            return False
        if len(self._failing[name]) < 3:
            return False
        last = self._tried.get(name)
        return last is None or self._pages - last >= 4 * self.min_pages  # not again and again

    # -- repairing ----------------------------------------------------------------------------- #
    def repair(self, name: str) -> RepairResult:
        """Look for a replacement of ``name``'s selectors, test it, and apply it, queue it, or give up."""
        self._tried[name] = self._pages
        f = self.schema[name]
        failing = list(self._failing[name])
        candidates = self._candidates(f, failing)
        tried = []
        for selector, method, similarity in candidates[:20]:
            checked = self._check(f, selector, failing)
            score = checked["score"] * similarity
            tried.append({"selector": selector, "method": method, "confidence": round(score, 3), **checked})
        tried.sort(key=lambda c: (-c["confidence"], len(c["selector"])))
        result = RepairResult(name, candidates=tried)
        base = {"event": "repair", "field": name, "old": list(f.selectors), "pages": len(failing)}
        example = failing[0]["url"] if failing else None
        waiting = self._question(name)
        if not tried or tried[0]["confidence"] < 0.5:
            reason = (
                "no candidate"
                if not tried
                else f"best candidate {tried[0]['selector']} scored {tried[0]['confidence']:.2f}"
            )
            # a question already waiting about the field stays (a candidate beats none)
            item = waiting or self._ask("broken", name, example, tried, {"reason": reason, "selectors": f.selectors})
            self.versions.log({**base, "outcome": "flagged", "reason": reason, "candidates": tried[:5],
                               "review": item.id if item else None})  # fmt: skip
            log.warning("self-healing: %s no longer found and not repaired (%s)", name, reason)
            result.queued = item is not None
            return result
        best = tried[0]
        result.selector, result.confidence, result.checked = best["selector"], best["confidence"], best
        repaired = _with_selectors(self.schema, name, [best["selector"], *f.selectors])
        reason = f"repair of {name}: {', '.join(f.selectors)} -> {best['selector']} ({best['method']})"
        validation = {k: v for k, v in best.items() if k != "examples"}
        facts = {"selector": best["selector"], "confidence": best["confidence"]}
        # applied without a person only when it scores well AND other strategies confirm what it reads:
        # an element that merely looks like the old one may hold something else
        if best["confidence"] >= self.auto_apply and best.get("confirmed", 0) >= 2:
            version = self.versions.add(repaired, reason=reason, by="auto", validation=validation)
            self.versions.log({**base, "outcome": "applied", "version": version.number, **facts})
            log.warning("self-healing: %s repaired as version %d (%s)", name, version.number, reason)
            coverage = best["coverage"]
            self._load()
            self._probation[name] = {"version": version.number, "coverage": coverage}
            result.applied, result.version = True, version.number
            return result
        if waiting is not None and waiting.kind == "repair" and waiting.details.get("selector") == best["selector"]:
            self.versions.log({**base, "outcome": "waiting", "review": waiting.id, **facts})  # asked already
            result.queued, result.version = True, waiting.details.get("version")
            return result
        version = self.versions.add(repaired, reason=reason, by="auto", status="candidate", validation=validation)
        details = {"version": version.number, "reason": reason, "selector": best["selector"]}
        item = self._ask("repair", name, example, tried, details)
        self.versions.log({**base, "outcome": "queued", "version": version.number, **facts,
                           "review": item.id if item else None})  # fmt: skip
        result.version, result.queued = version.number, item is not None
        return result

    def _question(self, name: str) -> ReviewItem | None:
        """The question about ``name``'s selectors waiting for a person, if any."""
        if self.review is None:
            return None
        directory = str(self.versions.directory)
        for item in self.review.pending():
            if item.field == name and item.kind in ("repair", "broken") and item.details.get("extractor") == directory:
                return item
        return None

    def _ask(
        self, kind: str, name: str, url: str | None, tried: list[dict[str, Any]], details: dict[str, Any]
    ) -> ReviewItem | None:
        """Put a question about ``name``'s selectors to a person; one it replaces is closed as superseded
        (and its candidate version with it)."""
        if self.review is None:
            return None
        while (older := self._question(name)) is not None:
            self.review.decide(older.id, "reject", by="auto", note="superseded by a newer question")
            number = older.details.get("version")
            if number and self.versions.get(int(number)).status == "candidate":
                self.versions.mark(int(number), "superseded")
        return self.review.add(
            kind,
            name,
            url=url,
            candidates=[_candidate_view(c) for c in tried[:3]],
            details={"extractor": str(self.versions.directory), **{k: _plain(v) for k, v in details.items()}},
        )

    def _candidates(self, f: SchemaField, failing: list[dict[str, Any]]) -> list[tuple[str, str, float]]:
        """Candidate selectors, with how they were found and how much the element resembles the old one."""
        found: dict[str, tuple[str, float]] = {}
        good = list(self._good[f.name])
        for page in failing[:6]:
            root = _root(page["html"])
            if root is None:
                continue
            for example in good[-3:]:  # 1. the element most like the one the selectors matched
                elements, score = relocate(root, {"elements": [example["fingerprint"]], "count": 1}, min_score=0.5)
                for element in elements[:1]:
                    for selector in _selectors_for(element, root):
                        if found.get(selector, ("", 0.0))[1] < score:
                            found[selector] = ("relocated", score)
            other = page.get("other")  # 2. the element holding the value other strategies found
            if other not in (None, "", []):
                for element in _holding(root, other, lambda text: _read(self.schema, f.name, text))[:2]:
                    for selector in _selectors_for(element, root):
                        if found.get(selector, ("", 0.0))[1] < 0.95:
                            found[selector] = ("anchored", 0.95)  # the value itself: stronger than resemblance
        return sorted(((s, m, sim) for s, (m, sim) in found.items()), key=lambda c: -c[2])

    def _check(self, f: SchemaField, selector: str, failing: list[dict[str, Any]]) -> dict[str, Any]:
        """How ``selector`` does on the failing pages, against the field's past values and the fixtures."""
        try:
            probe = Extractor(_only(_with_selectors(self.schema, f.name, [selector]), f.name),
                              strategies=[Selectors()], min_confidence=0)  # fmt: skip
        except Exception:
            return {"score": 0.0, "coverage": 0.0}
        values = []
        agree = disagree = 0
        for page in failing:
            value = probe.extract(_response(page["url"], page["html"])).data.get(f.name)
            if value in (None, "", []):
                continue
            values.append(value)
            other = page.get("other")
            if other not in (None, "", []):
                if _same(value, other):
                    agree += 1
                else:
                    disagree += 1
        coverage = len(values) / len(failing) if failing else 0.0
        agreement = agree / (agree + disagree) if agree + disagree else 1.0
        plausible = _plausibility(values, [g["value"] for g in self._good[f.name]])
        fixtures_ok = 1.0
        for fixture in self.versions.fixtures():
            if f.name in fixture.expected:
                got = probe.extract(_response(fixture.url, fixture.html)).data.get(f.name)
                expected = _normalized(probe.schema, f.name, fixture.expected[f.name])
                if got not in (None, "", []) and not _same(got, expected):
                    fixtures_ok = 0.0  # it reads a confirmed page wrong
        return {
            "score": round(coverage * agreement * plausible * fixtures_ok, 4),
            "coverage": round(coverage, 3),
            "agreement": round(agreement, 3),
            "plausibility": round(plausible, 3),
            "fixtures": fixtures_ok == 1.0,
            "confirmed": agree,  # pages where another strategy found the same value
            "examples": [_plain(v) for v in values[:5]],
        }

    def _judge_probation(self) -> None:
        """After an automatic repair: confirm it, or roll it back when the new selector does not hold."""
        for name, watch in list(self._probation.items()):
            rate = self._health[name].rate(self.min_pages) if name in self._health else None
            if rate is None:
                continue
            del self._probation[name]
            if rate >= watch["coverage"] * 0.5:
                self.versions.log({"event": "confirmed", "version": watch["version"], "field": name, "rate": rate})
                continue
            reason = f"{name} matched on {rate:.0%} of the next {self.min_pages} pages"
            selector = self.versions.schema(watch["version"])[name].selectors[0]
            self.versions.revert(watch["version"], name, reason=reason)
            log.warning("self-healing: the repair of %s (version %d) rolled back: %s", name, watch["version"], reason)
            self._ask("repair", name, None, [], {"reason": f"rolled back: {reason}", "selector": selector})
            self._load()

    # -- people -------------------------------------------------------------------------------- #
    def _review_values(self, ctx: PageContext, body: bytes, record: ExtractedRecord) -> None:
        if self.review is None:
            return
        for name, fv in record.fields.items():
            # one or two other values: a question; many (a listing, a page of variants): not one to ask
            uncertain = fv.value is not None and fv.confidence < self.review_below and 0 < len(fv.alternatives) <= 2
            if not uncertain or self._value_reviews[name] >= self.max_value_reviews:
                continue
            self._value_reviews[name] += 1
            options = [{"value": fv.value, "confidence": round(fv.confidence, 3), "method": fv.method}]
            options += [
                {"value": alt["value"], "confidence": alt["confidence"], "method": "/".join(alt.get("methods", []))}
                for alt in fv.alternatives[:3]
            ]
            self.review.add("value", name, url=ctx.url, candidates=options, html=body,
                            details={"extractor": str(self.versions.directory), "version": self.versions.active_number})  # fmt: skip

    def apply_reviews(self) -> int:
        """Act on the review queue's decisions about this extractor: accepted repairs become the active
        version, rejected ones are marked so; chosen or corrected values become fixtures. Returns how
        many decisions were applied."""
        if self.review is None:
            return 0
        done = {entry.get("review") for entry in self.versions.history() if entry.get("event") == "review applied"}
        applied = 0
        for item in self.review:
            mine = item.details.get("extractor") == str(self.versions.directory)
            if item.status == "pending" or not mine or item.id in done:
                continue
            if item.kind in ("repair", "broken"):
                self._apply_repair_review(item)
            elif item.kind == "value" and item.status in ("accepted", "corrected") and item.html and item.url:
                self.versions.add_fixture(item.url, unpack_html(item.html), {item.field: item.chosen})
            self.versions.log({"event": "review applied", "review": item.id, "status": item.status})
            applied += 1
        if applied:
            self._load()
        return applied

    def _apply_repair_review(self, item: Any) -> None:
        """A decided repair: the chosen (or typed) selector goes in front of the field's selectors, on
        top of the active version (other repairs may have been made since it was proposed)."""
        number = item.details.get("version")
        try:
            proposed = self.versions.get(int(number)) if number else None
        except (ConfigurationError, TypeError, ValueError):
            proposed = None  # a version no longer there
        if item.status == "rejected":
            if proposed is not None and proposed.status == "candidate":
                self.versions.reject(proposed.number, reason=f"rejected in review {item.id}")
            return
        selector = item.chosen or item.details.get("selector")
        if not isinstance(selector, str) or not selector.strip():
            return
        active = self.versions.schema()
        if item.field not in active:
            log.warning("self-healing: review %s is about %s, which version %d does not have", item.id, item.field,
                        self.versions.active_number)  # fmt: skip
            return
        current = [s for s in active[item.field].selectors if s != selector]
        repaired = _with_selectors(active, item.field, [selector, *current])
        reason = f"repair of {item.field}: -> {selector} ({item.status} in review {item.id})"
        self.versions.add(repaired, reason=reason, by="human", validation=proposed.validation if proposed else None)
        if proposed is not None and proposed.status == "candidate":
            self.versions.mark(proposed.number, "accepted")

    # -- explaining ------------------------------------------------------------------------------ #
    def why(self, name: str, page: Any | None = None) -> str:
        """Why ``name`` is what it is (or empty) on ``page``: what each strategy saw and the likely causes
        (:meth:`Extractor.why`), then the extractor's own story: the element most like the one the
        selectors used to match, how often they matched, and repairs made or waiting."""
        f = self.schema[name]
        lines = [f"{name} ({f.type}) in {self.name}, version {self.versions.active_number}"]
        healing: list[str] = []
        if page is not None:
            ctx = PageContext(page)
            diagnosis = self._extractor.why(name, ctx)  # what the page shows, and the likely causes
            head, *rest = diagnosis.describe().splitlines()
            lines[0] += head.split(f"{name} ({f.type})", 1)[1]  # " on URL: value"
            lines.extend(rest)
            good = list(self._good.get(name, ()))
            if good and not any(ctx.root.select(q) for q in f.selectors):
                element, score = _closest(ctx, good[-1]["fingerprint"])
                if element is not None:
                    healing.append(
                        f"the element most like the one the selectors used to match: {element} ({score:.0%} alike)"
                    )
        health = self._health.get(name)
        if health is not None and health.baseline is not None:
            rate = health.rate(min(len(health.recent), self.min_pages))
            healing.append(f"selectors matched on {health.baseline:.0%} of the first pages, {rate or 0:.0%} lately")
        for entry in self.versions.history()[-20:]:
            if entry.get("field") == name and entry.get("event") == "repair":
                healing.append(f"repair ({entry['outcome']}): {entry.get('selector') or entry.get('reason')}")
        if self.review is not None:
            for item in self.review.pending():
                if item.field == name and item.details.get("extractor") == str(self.versions.directory):
                    healing.append(f"waiting for review: {item.id} ({item.kind})")
        if healing:
            lines.append("  healing:")
            lines.extend(f"    {line}" for line in healing)
        return "\n".join(lines)

    def status(self) -> str:
        """The versions, each watched field's health, and the last repairs."""
        lines = [
            f"{self.versions.directory}: {self.name}, version {self.versions.active_number} of {len(self.versions.versions)}"
        ]
        for version in self.versions.versions:
            lines.append(f"  v{version.number} {version.status:<11} {version.by:<7} {version.reason}")
        for name, health in self._health.items():
            base = f"{health.baseline:.0%}" if health.baseline is not None else "(not known yet)"
            last = min(len(health.recent), self.min_pages)
            known = self._saved.get(name) or {}
            if last:
                lately = f"{health.rate(last) or 0:.0%} on the last {last} page(s)"
            elif known.get("lately") is not None and known.get("selectors") == self.schema[name].selectors:
                lately = f"{known['lately']:.0%} on the last {known.get('seen')} page(s) of the last run"
            else:
                lately = "no page seen yet"
            lines.append(f"  {name}: selectors matched {base} at first, {lately}")
        return "\n".join(lines)


def _candidate_view(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        k: candidate[k]
        for k in ("selector", "method", "confidence", "coverage", "agreement", "examples")
        if k in candidate
    }


def _root(html: bytes) -> Any:
    try:
        return _response("https://x.invalid/", html).selector.root
    except Exception:
        return None


def _stable(value: str) -> bool:
    return bool(_STABLE.fullmatch(value)) and not _VOLATILE.search(value)


def _selectors_for(element: Any, root: Any) -> list[str]:
    """Selectors for ``element`` that should hold on other pages of the template: its stable attributes
    and classes, its parent's class, and at worst its position."""
    tag = tag_name(element)
    out: list[str] = []
    for attr in _MOUNT_ATTRS:
        value = element.get(attr)
        if value and _stable(value):
            out.append(f'{tag}[{attr}="{value}"]')
    classes = [c for c in (element.get("class") or "").split() if _stable(c)]
    if classes:
        out.append(tag + "".join(f".{c}" for c in classes[:2]))
        out.append(f".{classes[0]}")
    ident = element.get("id")
    if ident and _stable(ident):
        out.append(f"#{ident}")
    parent = element.getparent()
    if parent is not None and isinstance(parent.tag, str):
        parent_classes = [c for c in (parent.get("class") or "").split() if _stable(c)]
        if parent_classes:
            out.append(f"{tag_name(parent)}.{parent_classes[0]} > {tag}")
    # keep those that point at this element on its page
    kept = []
    for selector in dict.fromkeys(out):
        try:
            matches = root.cssselect(selector) if hasattr(root, "cssselect") else []
        except Exception:
            continue
        if matches and matches[0] is element:
            kept.append(selector)
    return kept


def _holding(root: Any, value: Any, read: Any) -> list[Any]:
    """Elements whose own text ``read`` (the field's type) reads as ``value``, in document order."""
    numeric = isinstance(value, (int, float, dict)) and not isinstance(value, bool)  # a number, a price, a weight
    words = [] if numeric else str(value).casefold().split(maxsplit=1)
    sample = words[0] if words else ""
    out = []
    for element in root.iter(etree.Element):
        if not isinstance(element.tag, str) or tag_name(element) in _NOT_VALUES:
            continue
        own = own_text(element)
        if not own or len(own) > 300:
            continue
        if (numeric and not any(c.isdigit() for c in own)) or (sample and sample not in own.casefold()):
            continue  # cheap checks first
        got = read(own)
        if got not in (None, "", []) and _same(got, value):
            out.append(element)
    return out


def _read(schema: Schema, name: str, text: str) -> Any:
    """``text`` read as field ``name`` would read it (``None`` when it is not a valid value)."""
    try:
        result = schema.normalize_value(name, text)
    except Exception:
        return None
    return result.value if result.ok else None


def _number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def _plausibility(values: list[Any], history: list[Any]) -> float:
    """How much new values look like past ones: 1.0 when they do (or nothing is known)."""
    if not values or not history:
        return 1.0 if values else 0.0
    score = 1.0
    past = [_key(v) for v in history]
    now = [_key(v) for v in values]
    if len(set(past)) >= 2 and len(now) >= 3 and len(set(now)) == 1:
        score *= 0.3  # one value everywhere, where values used to differ: a label, a constant
    numbers = [n for n in (_as_number(v) for v in history) if n is not None]
    fresh = [n for n in (_as_number(v) for v in values) if n is not None]
    if numbers and len(numbers) == len(history) and len(fresh) == len(values):
        low, high = min(numbers), max(numbers)
        inside = sum(1 for n in fresh if low / 5 <= n <= high * 5) if low > 0 else len(fresh)
        score *= inside / len(fresh)
    elif all(isinstance(v, str) for v in history) and all(isinstance(v, str) for v in values):
        lengths = [len(v) for v in history]
        low, high = min(lengths), max(lengths)
        inside = sum(1 for v in values if low / 3 <= len(v) <= high * 3 + 10)
        score *= inside / len(values)
    return score


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict) and isinstance(value.get("amount"), (int, float)):
        return float(value["amount"])
    return None


def _closest(ctx: PageContext, saved: dict[str, Any]) -> tuple[str | None, float]:
    root = ctx.selector.root
    if root is None:
        return None, 0.0
    elements, score = relocate(root, {"elements": [saved], "count": 1}, min_score=0.3)
    if not elements:
        return None, score
    element = elements[0]
    text = (text_content(element) or "")[:40]
    return f"<{tag_name(element)} class={element.get('class')!r}> {text!r}", score
