"""Dataset versions and their differences: what was added, removed and changed between two runs.

::

    >>> diff = diff_records(yesterday, today, key="url")
    >>> print(diff.describe())
    +12 added, -3 removed, ~40 changed, 945 unchanged
    fields changed:
      price           31 records: 18 up, 13 down, median +4.0%
      availability     9 records: InStock -> OutOfStock 6, OutOfStock -> InStock 3

Records are matched by ``key`` (``url``, ``sku``, or several fields), compared
after normalizing keys the way :class:`~wintergrab.data.Deduplicator` does
(URLs through the URL normalizer, text without case or punctuation). Fields
starting with ``_`` (``_confidence``, ``_provenance``) are ignored unless you
ask for them; ``None`` and a missing field are the same.

:class:`DatasetVersions` keeps a directory of versions of a dataset (``v1``,
``v2``...), each a compressed JSON Lines file listed in ``versions.json``
with its record count, a content digest and its differences from the
previous version.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import secrets
import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..errors import ConfigurationError
from ..urls import normalize_url
from .dedupe import normalize_key
from .expressions import get_path

__all__ = ["DatasetDiff", "DatasetVersions", "FieldChange", "RecordChange", "Version", "diff_records"]

_MISSING: Any = object()


@dataclass
class FieldChange:
    """One field of one record, before and after.

    Attributes:
        field: The field's name.
        old, new: The values (``None`` when the field was absent).
        delta: ``new - old`` for numbers, and for prices in the same currency.
        ratio: ``delta / old`` (``None`` when ``old`` is 0 or the values are not numbers).
    """

    field: str
    old: Any
    new: Any
    delta: float | None = None
    ratio: float | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {"field": self.field, "old": self.old, "new": self.new}
        if self.delta is not None:
            out["delta"] = self.delta
        if self.ratio is not None:
            out["ratio"] = self.ratio
        return out


@dataclass
class RecordChange:
    """A record whose fields changed between the two datasets."""

    key: str
    old: dict[str, Any]
    new: dict[str, Any]
    fields: list[FieldChange]

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "fields": [f.to_dict() for f in self.fields]}


def _number(value: Any) -> tuple[float, str | None] | None:
    """``(amount, currency)`` for numbers and prices."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value), None
    if isinstance(value, Mapping) and isinstance(value.get("amount"), (int, float)):
        return float(value["amount"]), value.get("currency")
    return None


def _exact(value: Any) -> str:
    """A digest of exactly this value (key order aside): unlike ``content_hash``, case and
    punctuation count."""
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _squeezed(value: Any) -> Any:
    """``value`` with runs of whitespace in its text collapsed, as :func:`_same` compares it."""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, Mapping):
        return {k: _squeezed(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_squeezed(v) for v in value]
    return value


def _same_url(a: str, b: str) -> bool:
    try:
        return normalize_url(a) == normalize_url(b)
    except ValueError:
        return False


def _same(a: Any, b: Any) -> bool:
    if a is _MISSING:
        a = None
    if b is _MISSING:
        b = None
    if isinstance(a, str) and isinstance(b, str):
        if a == b or " ".join(a.split()) == " ".join(b.split()):
            return True
        # "?utm_source=feed" does not make another page
        return a.startswith(("http://", "https://")) and b.startswith(("http://", "https://")) and _same_url(a, b)
    if isinstance(a, float) or isinstance(b, float):
        x, y = _number(a), _number(b)
        return x is not None and y is not None and math.isclose(x[0], y[0], rel_tol=1e-9, abs_tol=1e-12)
    return bool(a == b)


def _changes(old: Mapping[str, Any], new: Mapping[str, Any], wanted: Callable[[str], bool]) -> list[FieldChange]:
    out = []
    for name in dict.fromkeys([*old, *new]):
        if not wanted(name):
            continue
        a, b = old.get(name, _MISSING), new.get(name, _MISSING)
        if _same(a, b):
            continue
        a, b = (None if a is _MISSING else a), (None if b is _MISSING else b)
        change = FieldChange(name, a, b)
        x, y = _number(a), _number(b)
        if x is not None and y is not None and (x[1] == y[1] or x[1] is None or y[1] is None):
            change.delta = round(y[0] - x[0], 10)
            change.ratio = round(change.delta / abs(x[0]), 6) if x[0] else None
        out.append(change)
    return out


@dataclass
class DatasetDiff:
    """The differences between two datasets (see :func:`diff_records`).

    Attributes:
        added: Records only in the new dataset.
        removed: Records only in the old one.
        changed: Records in both whose fields differ, with the fields that did.
        unchanged: How many records are the same in both.
        stats: Counts: ``old`` and ``new`` records, ``duplicate_keys`` (a key seen twice: the last
            record wins), ``unkeyed`` (records without the key fields, matched by content).
    """

    added: list[dict[str, Any]] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    changed: list[RecordChange] = field(default_factory=list)
    unchanged: int = 0
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "added": len(self.added),
            "removed": len(self.removed),
            "changed": len(self.changed),
            "unchanged": self.unchanged,
        }

    def __bool__(self) -> bool:
        """True when the datasets differ."""
        return bool(self.added or self.removed or self.changed)

    def summary(self) -> str:
        return (
            f"+{len(self.added):,} added, -{len(self.removed):,} removed, "
            f"~{len(self.changed):,} changed, {self.unchanged:,} unchanged"
        )

    def fields(self) -> dict[str, dict[str, Any]]:
        """Per field: how many records it changed in and how.

        Numbers and prices: ``up``, ``down`` and the ``median`` relative change; short values
        (availability, flags, categories): the most common ``transitions``; others: ``appeared``
        and ``disappeared`` (the field was missing before or after).
        """
        out: dict[str, dict[str, Any]] = {}
        for record in self.changed:
            for change in record.fields:
                info = out.setdefault(change.field, {"records": 0, "_ratios": [], "_moves": Counter()})
                info["records"] += 1
                if change.old is None:
                    info["appeared"] = info.get("appeared", 0) + 1
                elif change.new is None:
                    info["disappeared"] = info.get("disappeared", 0) + 1
                elif change.delta is not None:
                    info["up" if change.delta > 0 else "down"] = info.get("up" if change.delta > 0 else "down", 0) + 1
                    if change.ratio is not None:
                        info["_ratios"].append(change.ratio)
                elif all(isinstance(v, (str, bool, int)) and len(str(v)) <= 40 for v in (change.old, change.new)):
                    info["_moves"][f"{change.old} -> {change.new}"] += 1
        for info in out.values():
            ratios, moves = info.pop("_ratios"), info.pop("_moves")
            if ratios:
                info["median"] = round(statistics.median(ratios), 6)
            if moves:
                info["transitions"] = dict(moves.most_common(3))
        return dict(sorted(out.items(), key=lambda kv: -kv[1]["records"]))

    def describe(self, limit: int = 10) -> str:
        lines = [self.summary()]
        fields = self.fields()
        if fields:
            lines.append("fields changed:")
            width = max(len(name) for name in list(fields)[:limit])
            for name, info in list(fields.items())[:limit]:
                parts = []
                if "up" in info or "down" in info:
                    parts.append(f"{info.get('up', 0)} up, {info.get('down', 0)} down")
                if "median" in info:
                    parts.append(f"median {info['median']:+.1%}")
                if "transitions" in info:
                    parts.append(", ".join(f"{move} {count}" for move, count in info["transitions"].items()))
                for what in ("appeared", "disappeared"):
                    if what in info:
                        parts.append(f"{what} in {info[what]}")
                detail = f": {', '.join(parts)}" if parts else ""
                lines.append(f"  {name.ljust(width)}  {info['records']:>6,} records{detail}")
        return "\n".join(lines)

    def rows(self) -> Iterator[dict[str, Any]]:
        """The differences as flat rows (for a JSON Lines or CSV file): one per added or removed
        record, one per changed field."""
        for record in self.added:
            yield {"change": "added", "record": record}
        for record in self.removed:
            yield {"change": "removed", "record": record}
        for change in self.changed:
            for f in change.fields:
                yield {"change": "changed", "key": change.key, **f.to_dict()}

    def to_dict(self) -> dict[str, Any]:
        return {**self.counts, "stats": dict(self.stats), "fields": self.fields()}


def _key_function(key: str | Sequence[str] | None) -> Callable[[Mapping[str, Any]], str | None]:
    names = [key] if isinstance(key, str) else list(key or [])

    def key_of(record: Mapping[str, Any]) -> str | None:
        if not names:
            return None
        parts = []
        for name in names:
            value = get_path(record, name)
            if value is None or value == "":
                return None
            parts.append(normalize_key(name.rsplit(".", 1)[-1], value))
        return " | ".join(parts)

    return key_of


def diff_records(
    old: Iterable[Mapping[str, Any]],
    new: Iterable[Mapping[str, Any]],
    key: str | Sequence[str] | None = None,
    *,
    ignore: Iterable[str] = (),
    private: bool = False,
) -> DatasetDiff:
    """The differences between two datasets.

    Args:
        old, new: The records.
        key: The field(s) identifying a record (dotted paths work). Without a key, or for records
            missing it, records are matched by their whole content: they can be added or removed,
            not changed.
        ignore: Fields not to compare (a timestamp, a session id...).
        private: Also compare fields starting with ``_``.
    """
    key_of = _key_function(key)
    # the key is the record's identity: matched records have the same one
    skipped = set(ignore) | ({key} if isinstance(key, str) else set(key or ()))

    def wanted(name: str) -> bool:
        return name not in skipped and (private or not str(name).startswith("_"))

    def comparable(record: Mapping[str, Any]) -> str:
        return _exact({k: _squeezed(v) for k, v in record.items() if wanted(k) and v is not None})

    stats: Counter[str] = Counter()

    def index(
        records: Iterable[Mapping[str, Any]], side: str
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        keyed: dict[str, dict[str, Any]] = {}
        unkeyed: list[dict[str, Any]] = []
        for record in records:
            stats[side] += 1
            record = dict(record)
            k = key_of(record)
            if k is None:
                unkeyed.append(record)
                stats["unkeyed"] += 1
                continue
            if k in keyed:
                stats["duplicate_keys"] += 1
            keyed[k] = record
        return keyed, unkeyed

    before, loose_before = index(old, "old")
    after, loose_after = index(new, "new")
    diff = DatasetDiff()
    for k, record in before.items():
        if k not in after:
            diff.removed.append(record)
    for k, record in after.items():
        previous = before.get(k)
        if previous is None:
            diff.added.append(record)
            continue
        fields = _changes(previous, record, wanted)
        if fields:
            diff.changed.append(RecordChange(k, previous, record, fields))
        else:
            diff.unchanged += 1
    remaining = Counter(comparable(r) for r in loose_before)
    for record in loose_after:
        digest = comparable(record)
        if remaining[digest] > 0:
            remaining[digest] -= 1
            diff.unchanged += 1
        else:
            diff.added.append(record)
    for record in loose_before:
        digest = comparable(record)
        if remaining[digest] > 0:
            remaining[digest] -= 1
            diff.removed.append(record)
    diff.stats = {name: stats[name] for name in ("old", "new", "duplicate_keys", "unkeyed")}
    return diff


# --------------------------------------------------------------------------------------------- #
# versions
# --------------------------------------------------------------------------------------------- #
@dataclass
class Version:
    """One saved version of a dataset.

    Attributes:
        number: 1, 2, 3...
        created: When it was saved (ISO 8601, UTC).
        records: How many records it holds.
        digest: SHA-256 of its records, independent of their order.
        file: Its file in the versions directory.
        message: A note given when it was saved.
        key: The key fields used to compare it with the previous version.
        changes: Its differences from the previous version (``added``, ``removed``, ``changed``,
            ``unchanged``), when there was one and a key.
    """

    number: int
    created: str
    records: int
    digest: str
    file: str
    message: str | None = None
    key: list[str] | None = None
    changes: dict[str, int] | None = None

    @property
    def name(self) -> str:
        return f"v{self.number}"

    def describe(self) -> str:
        changes = ""
        if self.changes:
            c = self.changes
            changes = f"  +{c['added']:,} -{c['removed']:,} ~{c['changed']:,} ={c['unchanged']:,}"
        message = f"  {self.message}" if self.message else ""
        return f"{self.name:<5} {self.created}  {self.records:>9,} records{changes}{message}"


def _digest(records: Iterable[Mapping[str, Any]]) -> tuple[str, int]:
    hashes = sorted(_exact(dict(r)) for r in records)
    return hashlib.sha256("\n".join(hashes).encode()).hexdigest(), len(hashes)


class DatasetVersions:
    """A directory of versions of a dataset: ``v1.jsonl.gz``, ``v2.jsonl.gz``... and ``versions.json``.

    Args:
        directory: Where the versions live (created if missing).
        key: The field(s) identifying a record, used to compare versions (remembered in
            ``versions.json`` after the first :meth:`commit`).
    """

    MANIFEST = "versions.json"

    def __init__(self, directory: str | os.PathLike[str], *, key: str | Sequence[str] | None = None) -> None:
        self.directory = Path(directory)
        self.key: list[str] | None = [key] if isinstance(key, str) else (list(key) if key else None)
        self._versions: list[Version] = []
        manifest = self.directory / self.MANIFEST
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                self._versions = [Version(**entry) for entry in data.get("versions", [])]
            except (ValueError, TypeError) as exc:
                raise ConfigurationError(f"{manifest} is not a versions file: {exc}") from exc
            if self.key is None:
                self.key = data.get("key")

    @property
    def versions(self) -> list[Version]:
        return list(self._versions)

    @property
    def latest(self) -> Version | None:
        return self._versions[-1] if self._versions else None

    def get(self, ref: int | str) -> Version:
        """A version by number (``3``), name (``"v3"``), ``"latest"`` or ``"previous"``."""
        if not self._versions:
            raise ConfigurationError(f"{self.directory} holds no versions yet")
        text = str(ref).strip().lower()
        if text == "latest":
            return self._versions[-1]
        if text == "previous":
            if len(self._versions) < 2:
                raise ConfigurationError(f"{self.directory} holds only one version")
            return self._versions[-2]
        number = int(text.removeprefix("v")) if text.removeprefix("v").isdigit() else None
        for version in self._versions:
            if version.number == number:
                return version
        known = ", ".join(v.name for v in self._versions)
        raise ConfigurationError(f"no version {ref!r} in {self.directory} (known: {known})")

    def load(self, ref: int | str = "latest") -> list[dict[str, Any]]:
        """The records of a version."""
        version = self.get(ref)
        with gzip.open(self.directory / version.file, "rt", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def commit(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        message: str | None = None,
        key: str | Sequence[str] | None = None,
        force: bool = False,
    ) -> Version:
        """Save ``records`` as the next version, and compare it with the previous one.

        When the records are the same as the latest version's (in any order), no version is
        added and the latest is returned, unless ``force``.
        """
        if key is not None:
            self.key = [key] if isinstance(key, str) else list(key)
        rows = [dict(r) for r in records]
        digest, count = _digest(rows)
        latest = self.latest
        if latest is not None and latest.digest == digest and not force:
            return latest
        changes = None
        if latest is not None:
            changes = diff_records(self.load(latest.number), rows, self.key).counts
        number = (latest.number if latest else 0) + 1
        self.directory.mkdir(parents=True, exist_ok=True)
        file = f"v{number}.jsonl.gz"
        _atomic_write(self.directory / file, _gzip_lines(rows))
        version = Version(
            number,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            count,
            digest,
            file,
            message,
            list(self.key) if self.key else None,
            changes,
        )
        self._versions.append(version)
        manifest = {"key": self.key, "versions": [asdict(v) for v in self._versions]}
        _atomic_write(self.directory / self.MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False).encode())
        return version

    def diff(self, a: int | str = "previous", b: int | str = "latest", **options: Any) -> DatasetDiff:
        """The differences between two versions (by default the last two)."""
        options.setdefault("key", self.key)
        return diff_records(self.load(a), self.load(b), **options)

    def __repr__(self) -> str:
        return f"DatasetVersions({str(self.directory)!r}, versions={len(self._versions)})"


def _gzip_lines(rows: list[dict[str, Any]]) -> bytes:
    text = "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows)
    return gzip.compress(text.encode("utf-8"), mtime=0)


def _atomic_write(path: Path, data: bytes) -> None:
    """Write through a temporary file and rename, so a crash never leaves half a file."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with open(temporary, "xb") as out:  # the usual permissions (umask), unlike mkstemp's 0600
            out.write(data)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
