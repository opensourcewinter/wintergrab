"""A review queue: what the extraction is unsure of, waiting for a person to decide.

::

    queue = ReviewQueue("shop.reviews.jsonl")
    for item in queue.pending():
        print(item.describe())
    queue.decide("r3", "accept", choice=1)          # the second candidate is right
    queue.decide("r4", "correct", value="799.00")   # none is: this is the value
    queue.decide("r5", "reject", note="that is the shipping cost")

Items come from a :class:`~wintergrab.extraction.healing.HealingExtractor`:

* ``value``: a field found with low confidence, with the candidates the
  strategies proposed (value, confidence, method) and the page it came from;
* ``repair``: a new selector for a field whose selectors stopped working,
  validated on the pages where they failed but not convincing enough to be
  applied without a person (or rolled back after it was);
* ``broken``: a field whose selectors stopped working, with no candidate.

Decisions are kept in the file with who made them and when; the extractor
reads them back: an accepted repair becomes the active version, and a chosen
or corrected value becomes a regression example (a fixture) that later
repairs must reproduce. The file is JSON Lines, one event per line, written
by one process at a time.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any

from ..errors import ConfigurationError

__all__ = ["ReviewItem", "ReviewQueue", "pack_html", "show_value", "unpack_html"]

_DECISIONS = ("accept", "reject", "correct")
_MAX_HTML = 400_000  # bytes of a page kept with an item (compressed)


def pack_html(body: bytes | str | None) -> str | None:
    """A page's HTML, compressed and base64-encoded to travel in JSON (``None`` when too big)."""
    if not body:
        return None
    raw = body.encode("utf-8") if isinstance(body, str) else body
    packed = base64.b64encode(gzip.compress(raw, 6)).decode("ascii")
    return packed if len(packed) <= _MAX_HTML else None


def unpack_html(packed: str | None) -> bytes:
    return gzip.decompress(base64.b64decode(packed)) if packed else b""


def show_value(value: Any) -> str:
    """A value as a person reads it: a price as ``11.5 GBP`` rather than its dict."""
    if isinstance(value, dict) and "amount" in value and set(value) <= {"amount", "currency"}:
        return f"{value['amount']} {value.get('currency') or ''}".strip()
    if isinstance(value, dict) and set(value) == {"value", "unit"}:
        return f"{value['value']} {value['unit']}"
    return repr(value)


@dataclass
class ReviewItem:
    """Something to decide (see the module docs).

    Attributes:
        id: ``"r1"``, ``"r2"``...
        kind: ``"value"``, ``"repair"`` or ``"broken"``.
        field: The field it is about.
        url: The page (a value), or an example page (a repair).
        candidates: The values or selectors to choose from: ``{"value", "confidence", "method"...}``.
        details: Anything else (the extractor, the version, validation figures...).
        status: ``"pending"``, ``"accepted"``, ``"rejected"`` or ``"corrected"``.
        decision: ``{"decision", "by", "at", "choice", "value", "note"}`` once decided.
        html: The page, packed (:func:`pack_html`), for regression examples.
    """

    id: str
    kind: str
    field: str
    url: str | None = None
    candidates: list[dict[str, Any]] = dataclass_field(default_factory=list)
    details: dict[str, Any] = dataclass_field(default_factory=dict)
    status: str = "pending"
    created: float = dataclass_field(default_factory=time.time)
    decision: dict[str, Any] | None = None
    html: str | None = None

    @property
    def chosen(self) -> Any:
        """The value (or selector) the decision settled on: a chosen candidate, or a correction."""
        if self.decision is None or self.status == "rejected":
            return None
        if self.status == "corrected":
            return self.decision.get("value")
        index = self.decision.get("choice") or 0
        if 0 <= index < len(self.candidates):
            candidate = self.candidates[index]
            return candidate.get("selector", candidate.get("value"))
        return None

    def describe(self) -> str:
        head = {
            "value": f"{self.id}  low confidence: {self.field} on {self.url}",
            "repair": f"{self.id}  {self.details.get('reason') or f'repair of {self.field}'}",
            "broken": f"{self.id}  {self.field} no longer found: {self.details.get('reason', 'no replacement found')}",
        }.get(self.kind, f"{self.id}  {self.kind}: {self.field}")
        lines = [head + ("" if self.status == "pending" else f"  [{self.status}]")]
        for i, candidate in enumerate(self.candidates):
            what = candidate.get("selector") or show_value(candidate.get("value"))
            confidence = candidate.get("confidence")
            facts = f"{confidence:.0%}" if isinstance(confidence, (int, float)) else ""
            if candidate.get("method"):
                facts += f" via {candidate['method']}"
            if candidate.get("examples"):
                facts += "; e.g. " + ", ".join(show_value(v) for v in candidate["examples"][:3])
            lines.append(f"    {chr(ord('A') + i) if i < 26 else i}: {what}  ({facts.strip()})")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ReviewQueue:
    """The items waiting for a decision, and the decisions (see the module docs)."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._items: dict[str, ReviewItem] = {}
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        with self.path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except ValueError as exc:
                    raise ConfigurationError(f"{self.path}, line {number}: not JSON ({exc})") from exc
                if "decision" in event and "kind" not in event:
                    item = self._items.get(event["id"])
                    if item is not None:
                        item.decision = event
                        item.status = {"accept": "accepted", "reject": "rejected", "correct": "corrected"}[
                            event["decision"]
                        ]
                else:
                    known = {k: v for k, v in event.items() if k in ReviewItem.__dataclass_fields__}
                    self._items[event["id"]] = ReviewItem(**known)

    def _append(self, event: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    def add(
        self,
        kind: str,
        field: str,
        *,
        url: str | None = None,
        candidates: list[dict[str, Any]] | None = None,
        details: dict[str, Any] | None = None,
        html: bytes | str | None = None,
    ) -> ReviewItem:
        """Queue an item; returns it (with its ``id``)."""
        item = ReviewItem(
            id=f"r{len(self._items) + 1}",
            kind=kind,
            field=field,
            url=url,
            candidates=list(candidates or []),
            details=dict(details or {}),
            html=pack_html(html),
        )
        self._items[item.id] = item
        event = item.to_dict()
        del event["decision"]
        self._append(event)
        return item

    def decide(
        self,
        item_id: str,
        decision: str,
        *,
        choice: int | None = None,
        value: Any = None,
        by: str = "human",
        note: str = "",
    ) -> ReviewItem:
        """Record a decision: ``"accept"`` (candidate ``choice``, the first by default), ``"reject"``,
        or ``"correct"`` (with the right ``value``)."""
        item = self.get(item_id)
        if decision not in _DECISIONS:
            raise ConfigurationError(f"decision must be one of {', '.join(_DECISIONS)}, not {decision!r}")
        if decision == "correct" and value is None:
            raise ConfigurationError("a correction needs the right value")
        if decision == "accept" and item.candidates and choice is not None and not 0 <= choice < len(item.candidates):
            raise ConfigurationError(f"{item_id} has {len(item.candidates)} candidate(s); there is no {choice + 1}th")
        event = {"id": item_id, "decision": decision, "by": by, "at": time.time(), "choice": choice or 0,
                 "value": value, "note": note}  # fmt: skip
        item.decision = event
        item.status = {"accept": "accepted", "reject": "rejected", "correct": "corrected"}[decision]
        self._append(event)
        return item

    def get(self, item_id: str) -> ReviewItem:
        try:
            return self._items[item_id]
        except KeyError:
            raise ConfigurationError(f"no review item {item_id!r} in {self.path}") from None

    def items(self, *, status: str | None = None, kind: str | None = None) -> list[ReviewItem]:
        return [
            item
            for item in self._items.values()
            if (status is None or item.status == status) and (kind is None or item.kind == kind)
        ]

    def pending(self, kind: str | None = None) -> list[ReviewItem]:
        return self.items(status="pending", kind=kind)

    def __iter__(self) -> Iterator[ReviewItem]:
        return iter(self._items.values())

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"ReviewQueue({str(self.path)!r}, {len(self.pending())} pending of {len(self)})"
