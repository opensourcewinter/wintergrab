"""The extractor: typed records with per-field provenance and confidence.

For every field of a :class:`~wintergrab.data.Schema`, each strategy proposes
candidates (see :mod:`~wintergrab.extraction.strategies`). The extractor reads
each candidate with the field's type, groups candidates that mean the same
value (``"$299.99"`` in the page and ``"299.99"`` in JSON-LD agree), scores
the groups and keeps the best. The record is then normalized and validated
with the schema, exactly as a :class:`~wintergrab.data.Normalize` pipeline
stage would.

Confidence is computed from evidence, and the evidence is kept:

1. **Method prior**: how often the method is right. Defaults encode the
   hierarchy (data published for machines beats guesses from page layout):
   see :data:`DEFAULT_PRIORS`. :meth:`Extractor.calibrate` replaces them with
   the precision measured on pages whose correct values you know.
2. **Candidate evidence**: a method that saw several different values (three
   prices on the page) or whose value needed a guess to read (an ambiguous
   ``"1,234"``) scores lower.
3. **Agreement**: independent methods that found the same value combine as
   ``1 - (1 - p1)(1 - p2)...``.
4. **Disagreement**: a competing value lowers the winner:
   ``p * (1 - 0.5 * p_runner_up)``.
5. **Validation**: an invalid value halves confidence; a suspicious one
   (a warning) takes 20% off.
"""

from __future__ import annotations

import inspect
import logging
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..data.issues import Issue
from ..data.normalize import Money, Quantity
from ..data.schema import NormalizeContext, Schema, SchemaField
from ..data.similarity import content_hash, normalize_for_hash
from ..errors import ConfigurationError
from ..parser import Selector
from .model import ModelField, ModelRequest, call_model, grounding, model_name, parse_answer
from .page import PageContext, schema_types
from .schemaorg import target_types
from .strategies import STRATEGIES, Candidate, DomHeuristics, RecordFields, Strategy, StructuredData, field_kind

if TYPE_CHECKING:
    from .explain import FieldDiagnosis

__all__ = ["DEFAULT_PRIORS", "ExtractedRecord", "Extractor", "FieldValue"]

log = logging.getLogger("wintergrab.extraction")

#: How often each method is assumed right before any other evidence (see the module docs).
DEFAULT_PRIORS: dict[str, float] = {
    "json-ld": 0.95,  # published by the site for machines (search engines check it)
    "microdata": 0.93,
    "selector": 0.92,  # written by you for this site
    "opengraph": 0.88,  # published for link previews: titles and images are reliable, prices less often present
    "twitter": 0.85,
    "meta": 0.8,
    "embedded-json": 0.8,  # the app's own state, but it often holds several records
    "label": 0.8,  # "Weight: 1.2 kg" - the page says what the value is
    "records": 0.75,  # a field of a detected repeating record
    "dom": 0.7,  # layout conventions (the h1, an element classed "price")
    "pattern": 0.6,  # the value's shape alone
    "model": 0.6,  # an extraction model's answer (further weighed by grounding)
}
_HIERARCHY = list(DEFAULT_PRIORS)
#: Normalizer notes that mean a guess was needed to read a value.
_GUESSES = frozenset(
    {
        "ambiguous-separator",
        "ambiguous-currency",
        "ambiguous-day-month",
        "two-digit-year",
        "relative-date",
        "date-in-text",
        "unit-from-default",
        "scale-assumed",
        "currency-from-default",
        "currency-from-country",
        "first-of-many",
        "availability-from-long-text",
        "partial-address",
    }
)
_GROUNDING_FACTOR = {"exact": 1.0, "number": 0.9, "none": 0.3}


def _number_key(value: Any) -> str:
    try:
        number = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    except (InvalidOperation, ValueError):
        return repr(value)
    return str(number.normalize()) if number.is_finite() else repr(value)


def value_key(value: Any) -> Any:
    """What two values must share to count as the same (``$299.99`` = ``299.99``; case and spacing ignored)."""
    if isinstance(value, Money):
        return ("n", _number_key(value.amount))
    if isinstance(value, Quantity):
        try:
            canonical = value.canonical()
        except (KeyError, ValueError):
            return ("q", value.unit, _number_key(value.value))
        return ("q", canonical.unit, _number_key(round(canonical.value, 9)))
    if isinstance(value, bool):
        return ("b", value)
    if isinstance(value, (int, float, Decimal)):
        return ("n", _number_key(value))
    if isinstance(value, str):
        return ("s", normalize_for_hash(value))
    if isinstance(value, (list, tuple)):
        return ("l", tuple(value_key(v) for v in value))
    if isinstance(value, dict):
        return ("d", content_hash(value))
    return ("r", repr(value))


@dataclass
class FieldValue:
    """One field of an extracted record, with where it came from and how sure we are.

    Attributes:
        value: The normalized value (``None`` when not found).
        raw: The value as found on the page.
        method: The winning strategy (``"json-ld"``, ``"dom"``...), ``"derived"`` when
            another field supplied it (a price's currency), ``None`` when not found.
        source: Where exactly (``"json-ld:Product.offers.price"``, ``"dom:h1"``...).
        confidence: 0-1, see the module docs.
        agreed: Other methods that found the same value.
        alternatives: Competing values: ``{"value", "methods", "confidence"}``.
        notes: Guesses and warnings from normalizing (and ``"not-on-page"`` for ungrounded model output).
        validation: ``"ok"``, ``"warning:<code>"``, ``"error:<code>"``, ``"missing"`` (required) or ``"absent"``.
    """

    name: str
    value: Any = None
    raw: Any = None
    method: str | None = None
    source: str | None = None
    confidence: float = 0.0
    agreed: list[str] = field(default_factory=list)
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    validation: str = "absent"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"value": self.value, "method": self.method, "source": self.source,
                               "confidence": round(self.confidence, 4)}  # fmt: skip
        if self.raw is not None and self.raw != self.value:
            out["raw"] = self.raw
        if self.agreed:
            out["agreed"] = list(self.agreed)
        if self.alternatives:
            out["alternatives"] = self.alternatives
        if self.notes:
            out["notes"] = list(self.notes)
        out["validation"] = self.validation
        return out


@dataclass
class ExtractedRecord:
    """A record plus, for every field, its provenance and confidence.

    ``record.data`` is the plain record; ``record["price"]`` a value;
    ``record.fields["price"]`` its :class:`FieldValue`. ``to_dict()`` is what
    spiders export (values and ``_confidence``, plus ``_provenance`` when the
    extractor was created with ``provenance=True``).
    """

    data: dict[str, Any]
    fields: dict[str, FieldValue]
    url: str | None
    fetched_at: float
    schema: str
    issues: list[Issue] = field(default_factory=list)
    model: str | None = None
    provenance_default: bool = False

    @property
    def confidence(self) -> float:
        """Mean confidence of the fields found; a missing required field counts as 0."""
        scores = [fv.confidence for fv in self.fields.values() if fv.value is not None]
        scores += [0.0 for fv in self.fields.values() if fv.validation == "missing"]
        return round(sum(scores) / len(scores), 4) if scores else 0.0

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def __getitem__(self, name: str) -> Any:
        return self.data[name]

    def get(self, name: str, default: Any = None) -> Any:
        return self.data.get(name, default)

    def provenance(self) -> dict[str, Any]:
        """Where each value came from: the page, when, which extractor, and per-field evidence."""
        stamp = datetime.fromtimestamp(self.fetched_at, tz=timezone.utc).isoformat(timespec="seconds")
        out: dict[str, Any] = {"url": self.url, "fetched_at": stamp, "extractor": self.schema}
        if self.model:
            out["model"] = self.model
        shown = ("missing", "low-confidence")
        out["fields"] = {
            name: fv.to_dict() for name, fv in self.fields.items() if fv.value is not None or fv.validation in shown
        }
        if self.issues:
            out["issues"] = [issue.to_dict() for issue in self.issues]
        return out

    def to_dict(self, *, provenance: bool | None = None, confidence: bool = True) -> dict[str, Any]:
        out = dict(self.data)
        if confidence:
            out["_confidence"] = self.confidence
        if self.provenance_default if provenance is None else provenance:
            out["_provenance"] = self.provenance()
        return out

    def explain(self) -> str:
        """A table of every field: value, method, confidence and evidence."""
        rows = [("field", "value", "method", "conf", "evidence")]
        for name, fv in self.fields.items():
            if fv.value is None:
                rows.append((name, "-", "-", "-", fv.validation))
                continue
            text = fv.value if isinstance(fv.value, str) else repr(fv.value)
            evidence = []
            if fv.agreed:
                evidence.append("agrees: " + ", ".join(fv.agreed))
            for alt in fv.alternatives[:2]:
                evidence.append(f"vs {alt['value']!r} ({'/'.join(alt['methods'])}, {alt['confidence']:.2f})")
            if fv.notes:
                evidence.append("notes: " + ", ".join(fv.notes))
            if fv.validation not in ("ok", "absent"):
                evidence.append(fv.validation)
            rows.append(
                (
                    name,
                    text[:40] + ("..." if len(text) > 40 else ""),
                    fv.method or "-",
                    f"{fv.confidence:.2f}",
                    "; ".join(evidence),
                )
            )
        widths = [max(len(r[i]) for r in rows) for i in range(4)]
        lines = [f"{self.schema} from {self.url or 'a page'}: confidence {self.confidence:.2f}"]
        for row in rows:
            lines.append(
                "  " + "  ".join(cell.ljust(w) for cell, w in zip(row[:4], widths, strict=True)) + "  " + row[4]
            )
        for issue in self.issues:
            lines.append(f"  {issue}")
        return "\n".join(line.rstrip() for line in lines)

    def __repr__(self) -> str:
        return f"ExtractedRecord({self.data!r}, confidence={self.confidence})"


@dataclass
class _Group:
    key: Any
    members: list[Candidate]
    score: float

    @property
    def methods(self) -> list[str]:
        return list(dict.fromkeys(c.method for c in self.members))


@dataclass
class _Decision:
    winner: _Group | None
    others: list[_Group]
    rejected: list[Candidate]
    candidates: list[Candidate] = field(default_factory=list)
    extra_notes: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        if self.winner is None:
            return 0.0
        runner_up = self.others[0].score if self.others else 0.0
        return self.winner.score * (1 - 0.5 * runner_up)


def _schema_of(schema: Schema | Mapping[str, Any] | str | Path) -> Schema:
    if isinstance(schema, Schema):
        return schema
    if isinstance(schema, Mapping):
        return Schema.from_dict(schema)
    if isinstance(schema, (str, Path)):
        from .templates import schema_named

        return schema_named(schema)  # a file, or a template's name ("product", "job"...)
    raise ConfigurationError(
        f"expected a Schema, a schema mapping, a file or a template name, got {type(schema).__name__}"
    )


def _is_async(model: Any) -> bool:
    fn = getattr(model, "extract", None)
    target = fn if callable(fn) else model
    return inspect.iscoroutinefunction(target) or inspect.iscoroutinefunction(getattr(type(target), "__call__", None))  # noqa: B004


class Extractor:
    """Extract typed records from pages with the strategy hierarchy. See the module docs.

    Args:
        schema: The record to extract (a :class:`~wintergrab.data.Schema`, its dict form or a file).
        strategies: Strategy classes or instances, in priority order (default: :data:`~wintergrab.extraction.STRATEGIES`).
        model: An extraction model (see :mod:`~wintergrab.extraction.model`), asked only for fields
            the other strategies did not find with at least ``model_threshold`` confidence.
        min_confidence: Values below this confidence are left out of the record (they stay in
            the field's ``alternatives``, and its ``validation`` is ``"low-confidence"``): a guess
            is not data. 0 keeps everything.
        priors: Per-method prior confidence (see :data:`DEFAULT_PRIORS` and :meth:`calibrate`).
        provenance: Include ``_provenance`` in :meth:`ExtractedRecord.to_dict` by default.
        country, currency, dayfirst, decimal: How to read local formats (see
            :class:`~wintergrab.data.schema.NormalizeContext`).
    """

    def __init__(
        self,
        schema: Schema | Mapping[str, Any] | str | Path,
        *,
        strategies: Sequence[type[Strategy] | Strategy] | None = None,
        model: Any = None,
        model_threshold: float = 0.5,
        min_confidence: float = 0.3,
        priors: Mapping[str, float] | None = None,
        provenance: bool = False,
        country: str | None = None,
        currency: str | None = None,
        dayfirst: bool | None = None,
        decimal: str | None = None,
    ) -> None:
        self.schema = _schema_of(schema)
        if strategies is None:
            from ..plugins import load_plugins

            load_plugins()  # plugins may add strategies
        self.strategies: list[Strategy] = [s() if isinstance(s, type) else s for s in (strategies or STRATEGIES)]
        self.model = model
        self.model_threshold = model_threshold
        self.min_confidence = min_confidence
        self.priors = {**DEFAULT_PRIORS, **dict(priors or {})}
        self.provenance = provenance
        self.context = NormalizeContext(country=country, currency=currency, dayfirst=dayfirst, decimal=decimal)
        self._async = model is not None and _is_async(model)
        self.name = f"{self.schema.name}@{self.schema.version}"

    # -- candidates and decisions -------------------------------------------------- #
    def _context(self, ctx: PageContext) -> NormalizeContext:
        c = self.context
        return NormalizeContext(ctx.url, c.country, c.currency, c.dayfirst, c.decimal)

    def _gather(self, ctx: PageContext, f: SchemaField, strategies: Sequence[Strategy]) -> list[Candidate]:
        out: list[Candidate] = []
        for strategy in strategies:
            if ctx.scope is not None and strategy.page_level:
                continue
            try:
                out.extend(strategy.candidates(ctx, f, self.schema))
            except Exception as exc:  # a broken strategy must not stop the others
                log.warning("strategy %s failed on %s for %s: %s", type(strategy).__name__, ctx.url, f.name, exc)
        return out

    def _score(self, f: SchemaField, candidates: list[Candidate], nctx: NormalizeContext) -> _Decision:
        groups: dict[Any, list[Candidate]] = {}
        rejected: list[Candidate] = []
        for c in candidates:
            result = self.schema.normalize_value(f.name, c.raw, context=nctx)
            if not result.ok or result.value in (None, "", []):
                rejected.append(c)
                continue
            c.value, c.notes = result.value, result.notes
            guesses = sum(1 for note in result.notes if note in _GUESSES)
            c.confidence = min(0.99, self.priors.get(c.method, 0.5) * c.factor * 0.9**guesses)
            groups.setdefault(value_key(result.value), []).append(c)
        if f.type in ("text", "string"):
            groups = _merge_contained(groups)
        scored = []
        for key, members in groups.items():
            best: dict[str, float] = {}
            for c in members:
                best[c.method] = max(best.get(c.method, 0.0), c.confidence)
            score = 1 - math.prod(1 - p for p in best.values())
            members.sort(key=lambda c: -c.confidence)
            scored.append(_Group(key, members, min(score, 0.999)))
        scored.sort(key=lambda g: (-g.score, _rank(g.members[0].method)))
        return _Decision(scored[0] if scored else None, scored[1:], rejected, list(candidates))

    def candidates(self, page: Any, *, url: str | None = None) -> dict[str, list[Candidate]]:
        """Every candidate each strategy found, per field (for debugging an extraction)."""
        ctx = PageContext(page, url=url)
        nctx = self._context(ctx)
        out = {}
        for f in self.schema.fields:
            found = self._gather(ctx, f, self.strategies)
            self._score(f, found, nctx)
            out[f.name] = found
        return out

    def _decide(self, ctx: PageContext, strategies: Sequence[Strategy]) -> dict[str, _Decision]:
        nctx = self._context(ctx)
        return {f.name: self._score(f, self._gather(ctx, f, strategies), nctx) for f in self.schema.fields}

    def _needs_model(self, decisions: Mapping[str, _Decision]) -> list[SchemaField]:
        if self.model is None:
            return []
        # A price read with its currency ("12,99 €") settles the currency field too.
        priced = any(
            isinstance(d.winner.members[0].value, Money) and d.winner.members[0].value.currency
            for d in decisions.values()
            if d.winner is not None
        )
        return [
            f
            for f in self.schema.fields
            if decisions[f.name].confidence < self.model_threshold and not (f.type == "currency" and priced)
        ]

    def _model_request(
        self, ctx: PageContext, wanted: list[SchemaField], decisions: Mapping[str, _Decision]
    ) -> ModelRequest:
        known = {
            name: d.winner.members[0].raw
            for name, d in decisions.items()
            if d.winner is not None and d.confidence >= self.model_threshold
        }
        return ModelRequest([ModelField.of(f) for f in wanted], ctx.main_text, ctx.url, self.schema.name, known)

    def _apply_model(
        self, ctx: PageContext, wanted: list[SchemaField], decisions: dict[str, _Decision], answer: Any
    ) -> None:
        values = parse_answer(answer)
        name = model_name(self.model)
        nctx = self._context(ctx)
        for f in wanted:
            value = values.get(f.name)
            if value in (None, "", []):
                continue
            support = grounding(value, ctx.text)
            detail = "not found on the page" if support == "none" else f"grounding: {support}"
            candidate = Candidate(value, "model", f"model:{name}", _GROUNDING_FACTOR[support], detail)
            decision = self._score(f, [*decisions[f.name].candidates, candidate], nctx)
            if support == "none" and decision.winner is not None and candidate in decision.winner.members:
                decision.extra_notes.append("not-on-page")
            decisions[f.name] = decision

    # -- records ------------------------------------------------------------------ #
    def _build(self, ctx: PageContext, decisions: dict[str, _Decision]) -> ExtractedRecord:
        schema = self.schema
        raw: dict[str, Any] = {}
        currency_fields = [f.name for f in schema.fields if f.type == "currency"]
        has_currency = any(decisions[name].winner is not None for name in currency_fields)
        for f in schema.fields:
            decision = decisions[f.name]
            if decision.winner is not None and decision.confidence < self.min_confidence:
                decision.others.insert(0, decision.winner)
                decision.winner = None
                decision.extra_notes.append("low-confidence")
            if decision.winner is None:
                continue
            members = decision.winner.members
            chosen = members[0]
            if f.type == "money" and isinstance(chosen.value, Money) and not chosen.value.currency and not has_currency:
                # Same amount elsewhere with its currency ("$299.99" next to JSON-LD "299.99"): keep the currency.
                chosen = next((c for c in members if isinstance(c.value, Money) and c.value.currency), chosen)
            elif f.type == "text":
                chosen = max(members, key=lambda c: len(str(c.value)))  # the fullest version of a long text
            raw[f.name] = chosen.raw
        data, results = schema.normalize(raw, context=self._context(ctx))
        issues = schema.validate(data, results)
        by_field: dict[str, list[Issue]] = {}
        for issue in issues:
            if issue.field:
                by_field.setdefault(issue.field.split("[")[0].split(".")[0], []).append(issue)
        fields: dict[str, FieldValue] = {}
        for f in schema.fields:
            decision = decisions[f.name]
            fv = FieldValue(f.name, value=data.get(f.name))
            own = by_field.get(f.name, [])
            if decision.winner is not None and fv.value is not None:
                winner = decision.winner
                fv.raw = raw.get(f.name)
                fv.method = winner.members[0].method
                fv.source = winner.members[0].source
                fv.agreed = [m for m in winner.methods if m != fv.method]
                fv.alternatives = [
                    {"value": _plain(g.members[0].value), "methods": g.methods, "confidence": round(g.score, 4)}
                    for g in decision.others[:3]
                ]
                fv.confidence = decision.confidence
                fv.notes = list(dict.fromkeys([*results[f.name].notes, *decision.extra_notes]))
            elif fv.value is not None:
                # Supplied by another field (the currency of a price) or by the field's default.
                fv.method, fv.confidence = self._derived(f, fields, data)
                fv.source = fv.method
            if fv.value is None and decision.winner is None and decision.others:
                fv.alternatives = [
                    {"value": _plain(g.members[0].value), "methods": g.methods, "confidence": round(g.score, 4)}
                    for g in decision.others[:3]
                ]
                fv.notes = list(decision.extra_notes)
            if fv.value is None:
                fv.validation = (
                    "low-confidence"
                    if "low-confidence" in decision.extra_notes
                    else "missing"
                    if f.required
                    else "absent"
                )
            elif any(i.severity == "error" for i in own):
                fv.validation = "error:" + next(i.code for i in own if i.severity == "error")
                fv.confidence *= 0.5
            elif any(i.severity == "warning" for i in own):
                fv.validation = "warning:" + next(i.code for i in own if i.severity == "warning")
                fv.confidence *= 0.8
            else:
                fv.validation = "ok"
            fv.confidence = round(fv.confidence, 4)
            fields[f.name] = fv
        return ExtractedRecord(
            data=data,
            fields=fields,
            url=ctx.url,
            fetched_at=ctx.fetched_at,
            schema=self.name,
            issues=issues,
            model=model_name(self.model) if self.model is not None else None,
            provenance_default=self.provenance,
        )

    def _derived(self, f: SchemaField, fields: Mapping[str, FieldValue], data: Mapping[str, Any]) -> tuple[str, float]:
        if f.type == "currency":
            for other in self.schema.fields:
                source = fields.get(other.name)
                if other.type == "money" and source is not None and source.value is not None:
                    return f"derived:{other.name}", source.confidence
        return "default", 0.5

    # -- public API ---------------------------------------------------------------- #
    def extract(self, page: Any, *, url: str | None = None) -> ExtractedRecord:
        """One record from a page (a :class:`~wintergrab.Response`, a :class:`~wintergrab.Selector` or HTML)."""
        if self._async:
            raise ConfigurationError("the extraction model is asynchronous: use 'await extractor.aextract(page)'")
        ctx = page if isinstance(page, PageContext) else PageContext(page, url=url)
        decisions = self._decide(ctx, self.strategies)
        wanted = self._needs_model(decisions)
        if wanted:
            try:
                answer = call_model(self.model, self._model_request(ctx, wanted, decisions))
            except Exception as exc:
                log.warning("extraction model %s failed on %s: %s", model_name(self.model), ctx.url, exc)
            else:
                self._apply_model(ctx, wanted, decisions, answer)
        return self._build(ctx, decisions)

    async def aextract(self, page: Any, *, url: str | None = None) -> ExtractedRecord:
        """:meth:`extract` with an asynchronous model (works with a plain one too)."""
        ctx = page if isinstance(page, PageContext) else PageContext(page, url=url)
        decisions = self._decide(ctx, self.strategies)
        wanted = self._needs_model(decisions)
        if wanted:
            try:
                answer = call_model(self.model, self._model_request(ctx, wanted, decisions))
                if inspect.isawaitable(answer):
                    answer = await answer
            except Exception as exc:
                log.warning("extraction model %s failed on %s: %s", model_name(self.model), ctx.url, exc)
            else:
                self._apply_model(ctx, wanted, decisions, answer)
        return self._build(ctx, decisions)

    def extract_all(
        self, page: Any, *, container: str | None = None, min_records: int = 2, url: str | None = None
    ) -> list[ExtractedRecord]:
        """Every record of a listing page.

        Uses, in this order: several schema.org objects of the schema's type
        (an ``ItemList`` of products...), the elements matching ``container``
        (by default the schema's own ``container``), or the page's main
        repeating group found by :func:`~wintergrab.parser.autoextract.detect_records`.
        Inside a record only record-level strategies run (selectors, labels, DOM,
        patterns).
        """
        ctx = PageContext(page, url=url)
        container = container or self.schema.container
        wanted = target_types(self.schema.name)
        if wanted and container is None:
            for kind in ("json-ld", "microdata"):
                nodes = [(p, n) for p, n in ctx.nodes(kind) if wanted & set(schema_types(n))]
                if len(nodes) >= min_records:
                    records = []
                    for node in nodes:
                        strategy = StructuredData(node=node, kind=kind)
                        records.append(self._build(ctx, self._decide(ctx, [strategy])))
                    return records
        strategies = [s for s in self.strategies if not s.page_level]
        if container is not None:
            elements = ctx.selector.select(container)
        else:
            groups = ctx.selector.detect_records(min_records=max(2, min_records))
            if not groups:
                return []
            group = groups[0]
            elements = ctx.selector.css(group.container_selector)
            position = next((i for i, s in enumerate(strategies) if isinstance(s, DomHeuristics)), len(strategies))
            strategies.insert(position, RecordFields(group.fields))
        records = []
        for element in elements:
            if isinstance(element, Selector) and element.is_element:
                scoped = ctx.scoped(element)
                records.append(self._build(scoped, self._decide(scoped, strategies)))
        return [r for r in records if any(v is not None for v in r.data.values())]

    def explain(self, page: Any, *, url: str | None = None) -> str:
        """:meth:`ExtractedRecord.explain` of the page's record."""
        return self.extract(page, url=url).explain()

    def why(self, name: str, page: Any, *, url: str | None = None) -> FieldDiagnosis:
        """Why field ``name`` is what it is on ``page``, or why it is empty: what each strategy saw,
        and the likely causes, each saying how sure it is (see :mod:`~wintergrab.extraction.explain`)."""
        from .explain import diagnose

        return diagnose(self, name, page, url=url)

    def calibrate(self, examples: Iterable[tuple[Any, Mapping[str, Any]]], *, min_samples: int = 5) -> dict[str, float]:
        """Measure how often each method is right on pages whose correct values you know.

        ``examples`` are ``(page, expected record)`` pairs. For every field with
        an expected value, every candidate a method proposes counts as right
        when it means the same value. Methods with at least ``min_samples``
        candidates get their measured precision as prior (kept in
        :attr:`priors`, and returned).
        """
        right: Counter[str] = Counter()
        total: Counter[str] = Counter()
        for page, expected in examples:
            ctx = PageContext(page)
            nctx = self._context(ctx)
            for f in self.schema.fields:
                if expected.get(f.name) in (None, "", []):
                    continue
                truth = self.schema.normalize_value(f.name, expected[f.name], context=nctx).value
                if truth is None:
                    continue
                target = value_key(truth)
                for c in self._gather(ctx, f, self.strategies):
                    result = self.schema.normalize_value(f.name, c.raw, context=nctx)
                    total[c.method] += 1
                    if result.ok and result.value is not None and value_key(result.value) == target:
                        right[c.method] += 1
        measured = {m: round(min(0.99, max(0.05, right[m] / n)), 3) for m, n in total.items() if n >= min_samples}
        self.priors.update(measured)
        return measured

    def __repr__(self) -> str:
        return f"Extractor({self.name}, strategies={[type(s).__name__ for s in self.strategies]})"


def _merge_contained(groups: dict[Any, list[Candidate]]) -> dict[Any, list[Candidate]]:
    """Texts where one contains the other (a meta description and the full one) support each other."""
    keys = sorted((k for k in groups if k[0] == "s" and len(k[1]) >= 20), key=lambda k: len(k[1]), reverse=True)
    for i, longer in enumerate(keys):
        if longer not in groups:
            continue
        for shorter in keys[i + 1 :]:
            if shorter in groups and shorter[1] in longer[1]:
                groups[longer].extend(groups.pop(shorter))
    return groups


def _rank(method: str) -> int:
    return _HIERARCHY.index(method) if method in _HIERARCHY else len(_HIERARCHY)


def _plain(value: Any) -> Any:
    if isinstance(value, Money):
        return value.to_dict()
    if isinstance(value, Quantity):
        return value.to_dict()
    return value


def kind_of(f: SchemaField) -> str:
    """The heuristic kind of a field (re-exported for convenience)."""
    return field_kind(f)
