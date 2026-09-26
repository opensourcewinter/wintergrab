"""Why a field is what it is on a page, or why it is empty.

::

    diagnosis = extractor.why("price", response)
    print(diagnosis.describe())
    # price (money) on https://shop.example/p/1: empty
    #   seen:
    #     selector .price: 0 element(s) on this page
    #     json-ld: nothing; meta tags: nothing; dom: '$12.99' from dom:span.cost (confidence 0.70)
    #   why:
    #     likely: the page's layout changed: the field's selectors find nothing, and dom finds
    #       12.99 (span.cost reads it on this page)
    #     ...

The diagnosis keeps what was **seen** (what each strategy found, what the
field's selectors match, how sure the extractor was, how values were read
and validated) apart from **why**: causes, each saying how sure it is.

* **certain**: what the extractor did (a value too unsure to keep, a value that
  is not a price, one that breaks a rule), or the page's HTTP status;
* **likely**: strong evidence (the selectors find nothing but another strategy
  finds the value: the page's layout changed; the page is a bot check; its
  content is drawn by JavaScript; it lists records rather than holding one);
* **possibly**: what fits but is not shown (the page does not state the value).

``wintergrab get URL --extract SCHEMA --why FIELD`` prints it.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING, Any

from .page import PageContext

if TYPE_CHECKING:
    from .engine import Extractor
    from .strategies import Candidate

__all__ = ["FieldDiagnosis", "diagnose"]

_SURE_TYPE = 0.5  # page classifications at least this sure are named as causes
_NAMES = {"json-ld": "JSON-LD", "microdata": "microdata", "opengraph": "OpenGraph", "meta": "meta tags",
          "twitter": "Twitter cards", "selector": "the field's selectors", "embedded-json": "embedded JSON",
          "label": "labels", "records": "the page's records", "dom": "the page's layout", "pattern": "text patterns",
          "model": "the model"}  # fmt: skip


@dataclass
class FieldDiagnosis:
    """Why a field is what it is on a page (see the module docs).

    Attributes:
        field: The field.
        type: Its type.
        url: The page.
        value: What the record has (``None``: empty).
        status: ``"found"``, ``"empty"`` (nothing found), ``"unsure"`` (found, too unsure to keep),
            ``"unreadable"`` (found, not a value of the type) or ``"invalid"`` (found, breaks a rule).
        seen: What was observed on the page.
        causes: Why, each starting with how sure it is: ``certain:``, ``likely:`` or ``possibly:``.
        candidates: Every value found: ``{method, source, raw, value, confidence, kept}``.
    """

    field: str
    type: str
    url: str | None
    value: Any
    status: str
    seen: list[str] = dataclass_field(default_factory=list)
    causes: list[str] = dataclass_field(default_factory=list)
    candidates: list[dict[str, Any]] = dataclass_field(default_factory=list)

    def __str__(self) -> str:
        return self.describe()

    def describe(self) -> str:
        """The field and its value (or why it has none): what was seen, then why."""
        where = f" on {self.url}" if self.url else ""
        head = _show(self.value) if self.value is not None else self.status
        lines = [f"{self.field} ({self.type}){where}: {head}"]
        if self.seen:
            lines.append("  seen:")
            lines.extend(f"    {line}" for line in self.seen)
        if self.causes:
            lines.append("  why:")
            lines.extend(f"    {line}" for line in self.causes)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "type": self.type,
            "url": self.url,
            "value": _plain(self.value),
            "status": self.status,
            "seen": self.seen,
            "causes": self.causes,
            "candidates": self.candidates,
        }


def diagnose(extractor: Extractor, name: str, page: Any, *, url: str | None = None) -> FieldDiagnosis:
    """Diagnose field ``name`` of ``extractor``'s schema on ``page`` (see the module docs)."""
    schema = extractor.schema
    if name not in schema:
        raise KeyError(f"{schema.name!r} has no field {name!r}; it has {', '.join(schema.names)}")
    f = schema[name]
    ctx = page if isinstance(page, PageContext) else PageContext(page, url=url)
    record = extractor.extract(ctx)
    fv = record.fields[name]
    found = extractor._gather(ctx, f, extractor.strategies)
    decision = extractor._score(f, found, extractor._context(ctx))
    rejected = {id(c) for c in decision.rejected}
    winner = decision.winner
    status = (
        "found" if fv.value is not None and not fv.validation.startswith("error")
        else "invalid" if fv.value is not None
        else "unsure" if winner is not None
        else "unreadable" if decision.rejected
        else "empty"
    )  # fmt: skip
    out = FieldDiagnosis(name, f.type, ctx.url, fv.value, status)
    out.candidates = [_candidate(c, id(c) in rejected, winner) for c in found]

    # -- seen --------------------------------------------------------------------------------- #
    matched_any = False
    for query in f.selectors:
        try:
            matches = ctx.root.select(query)
        except Exception as exc:
            out.seen.append(f"selector {query}: not a selector wintergrab reads ({exc})")
            continue
        matched_any = matched_any or bool(matches)
        texts = [(m.get() if not m.is_element else m.text) or "" for m in matches[:3]]
        shown = ", ".join(repr(t[:40]) for t in texts if t)
        out.seen.append(
            f"selector {query}: {len(matches)} element(s) on this page" + (f", reading {shown}" if shown else "")
        )
    by_method: dict[str, list[Candidate]] = {}
    for c in found:
        by_method.setdefault(c.method, []).append(c)
    if by_method:
        for method, candidates in by_method.items():
            shown = "; ".join(_about(c, id(c) in rejected) for c in candidates[:3])
            out.seen.append(f"{_NAMES.get(method, method)}: {shown}")
    else:
        out.seen.append("no strategy found a candidate value")
    if fv.value is not None:
        agreed = f", agreeing with {', '.join(_NAMES.get(m, m) for m in fv.agreed)}" if fv.agreed else ""
        out.seen.append(f"kept: {_show(fv.value)} from {fv.source} (confidence {fv.confidence:.2f}{agreed})")
    for alternative in fv.alternatives[:3]:
        out.seen.append(
            f"not kept: {_show(alternative['value'])} from {'/'.join(alternative['methods'])}"
            f" (confidence {alternative['confidence']:.2f})"
        )

    # -- why ---------------------------------------------------------------------------------- #
    issues = [i for i in record.issues if (i.field or "").split("[")[0].split(".")[0] == name]
    if status == "invalid":
        for issue in issues:
            out.causes.append(f"certain: {_show(fv.value)} was found, and breaks a rule: {issue.message}")
    elif status == "unsure" and winner is not None:
        value = winner.members[0].value
        out.causes.append(
            f"certain: {_show(value)} was found ({'/'.join(winner.methods)}), with too little confidence to keep:"
            f" {decision.confidence:.2f}, under the extractor's {extractor.min_confidence:.2f} (min_confidence)"
        )
        if len(decision.others) and winner.score - decision.others[0].score < 0.2:
            out.causes.append("likely: several different values compete, and none stands out")
    elif status == "unreadable":
        raw = ", ".join(repr(str(c.raw)[:40]) for c in decision.rejected[:3])
        out.causes.append(f"certain: what was found ({raw}) cannot be read as {f.type}")
    if status == "found":
        if fv.method == "model":
            out.causes.append("certain: the model gave it (the page's own data and layout did not)")
        _layout_cause(out, ctx, extractor, f, matched_any, by_method)
        return out
    _page_causes(out, ctx, extractor, f, matched_any, by_method)
    return out


def _page_causes(
    out: FieldDiagnosis,
    ctx: PageContext,
    extractor: Extractor,
    f: Any,
    matched_any: bool,
    by_method: dict[str, list[Candidate]],
) -> None:
    """What about the page explains an empty field, from the surest cause down."""
    response = ctx.response
    if response is not None:
        from ..fetchers.blocking import looks_blocked
        from ..fetchers.strategy import needs_javascript

        if looks_blocked(response):
            status = f" (HTTP {response.status})" if response.status != 200 else ""
            out.causes.append(f"likely: this is a bot-check or access page{status}, not the content")
            return
        if response.status >= 400:
            out.causes.append(f"certain: the page answered HTTP {response.status}: it is not the page with the value")
            return
        js = needs_javascript(response)
        if js:
            out.causes.append(f"likely: the content is drawn by JavaScript ({js}): fetch it in a browser (--browser)")
    _layout_cause(out, ctx, extractor, f, matched_any, by_method)
    kind = _page_kind(ctx, extractor)
    if kind:
        out.causes.append(kind)
    if not by_method and not out.causes:
        if f.selectors and not matched_any:
            out.causes.append(
                f"possibly: the page's layout changed (the field's selectors find nothing), or the page does not"
                f" state its {f.name}"
            )
        else:
            out.causes.append(f"possibly: the page does not state its {f.name} (no strategy found anything)")


def _layout_cause(
    out: FieldDiagnosis,
    ctx: PageContext,
    extractor: Extractor,
    f: Any,
    matched_any: bool,
    by_method: dict[str, list[Candidate]],
) -> None:
    """The field's selectors find nothing, but other strategies find a value: the layout changed, and
    perhaps a selector reads the value on this page."""
    readable = [c for m, cs in by_method.items() if m != "selector" for c in cs if c.value is not None]
    if not f.selectors or matched_any or not readable:
        return
    best = max(readable, key=lambda c: c.confidence)
    replacement = _replacement(ctx, extractor, f, best)
    where = f"; {replacement} reads it on this page" if replacement else ""
    out.causes.append(
        f"likely: the page's layout changed: the field's selectors find nothing, but"
        f" {_NAMES.get(best.method, best.method)} finds {_show(best.value)}{where}"
    )


def _page_kind(ctx: PageContext, extractor: Extractor) -> str | None:
    """A cause from what kind of page this is, when it is not the kind the schema is about."""
    from ..goals.goal import ENTITIES
    from ..intel.classify import classify_page

    kind = ENTITIES.get(extractor.schema.name)
    if kind is None or ctx.scope is not None:
        return None
    try:
        page = classify_page(ctx.response if ctx.response is not None else ctx.selector, url=ctx.url)
    except Exception:
        return None
    if page.confidence < _SURE_TYPE or page.type in kind.page_types:
        return None
    if page.type in kind.listing_types:
        return (
            f"likely: the page lists {kind.name}s ({page.type} page, {page.confidence:.0%} sure) rather than"
            " holding one: read every one of them (--all, a container)"
        )
    return (
        f"possibly: the page is not a {kind.name} page (it looks like a {page.type} page, {page.confidence:.0%} sure)"
    )


def _replacement(ctx: PageContext, extractor: Extractor, f: Any, candidate: Candidate) -> str | None:
    """A selector reading ``candidate``'s value on this page, to replace the field's own (on this page only)."""
    from .engine import FieldValue
    from .generate import _check, _proposals

    fv = FieldValue(f.name, value=candidate.value, raw=candidate.raw, method=candidate.method)
    for query, _ in sorted(_proposals(ctx, f, fv, extractor.schema), key=lambda p: p[1])[:8]:
        check = _check(query, f, extractor.schema, [ctx], [{f.name: fv}])
        if check.reproduced == 1 and not check.ambiguous:
            return query
    return None


def _candidate(c: Candidate, rejected: bool, winner: Any) -> dict[str, Any]:
    kept = winner is not None and any(m is c for m in winner.members)
    return {
        "method": c.method,
        "source": c.source,
        "raw": _plain(c.raw),
        "value": None if rejected else _plain(c.value),
        "confidence": round(c.confidence, 4),
        "kept": kept,
        "readable": not rejected,
    }


def _about(c: Candidate, rejected: bool) -> str:
    raw = repr(str(c.raw)[:40]) if not isinstance(c.raw, (int, float)) else repr(c.raw)
    if rejected:
        return f"{raw} from {c.source} (not readable)"
    return f"{raw} from {c.source} (confidence {c.confidence:.2f})"


def _show(value: Any) -> str:
    """A value as the diagnosis writes it: text quoted, a price or a quantity as it reads."""
    if isinstance(value, str):
        return repr(value)
    if isinstance(value, dict) and "amount" in value:
        return f"{value['amount']} {value.get('currency') or ''}".strip()
    if isinstance(value, (int, float, bool, list, dict, type(None))):
        return repr(value)
    return str(value)  # Money, Quantity...


def _plain(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    return str(value)
