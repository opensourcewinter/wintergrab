"""Generated schemas: selectors for one site, learned from the values found on its pages.

::

    generated = generate_schema(pages, "product")   # pages of one site, a record on each
    print(generated.describe())                      # each field: its selector, or why it has none
    generated.schema.save("shop.schema.json")        # a schema like any other

The values come from the extractor itself: what the pages publish for
machines (JSON-LD, meta tags), what their layout and text say and, with
``model=``, an extraction model's answers for the fields still missing
(checked against the page). Only values found with confidence are learned
from (``min_confidence``; a model's answer counts when the page holds it).

For each field, selectors are proposed from the elements holding those
values: their stable attributes and classes, their tag, a label beside them
(a table's header cell), their position. Each is tried on every sample page,
through the same code the extractor reads selectors with, and kept only
when it reads the value found there on all of them; a selector that matches
one value per page beats one that matches several. Lists, nested objects,
the page's own address, values derived from another field and fields the
schema already has selectors for are left as they are.

The result is the schema given, with those selectors. It runs through the
normal :class:`~wintergrab.extraction.Extractor`: the selectors are one more
source of evidence, weighed with the others, and values are normalized and
validated as before. It needs no model: what a model found, a selector now
reads. :func:`wintergrab.goals.generate_scraper` makes one for a goal, then
tests it before it is kept.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from lxml import etree

from ..data.schema import Schema, SchemaField
from ..errors import ConfigurationError
from ..parser import Selector
from ..parser.autoextract import _class_rank, _classes, _context_penalty, _locate
from ..parser.text import tag_name, text_content
from .engine import Extractor, FieldValue, _schema_of
from .healing import _MOUNT_ATTRS, _holding, _plain, _read, _same, _stable, _with_selectors
from .model import model_name
from .page import PageContext
from .strategies import Selectors, _class_rating, _element_value, field_kind

__all__ = ["GeneratedSchema", "LearnedField", "generate_schema"]

#: A model's answer is learned from when it scores this much: the page holds it (see ``engine``).
_MODEL_CONFIDENCE = 0.5
#: Pages whose elements are searched for a field's value (the others only check the selectors).
_SEARCHED_PAGES = 4
#: How stable each kind of selector is (lower is better): an attribute meant for machines, a class
#: or an id, a label beside the element, its tag, its place under a classed ancestor, its place on the page.
_ATTRIBUTE, _CLASS, _LABEL, _TAG, _ANCHORED, _POSITION = range(6)
#: Types whose values pages write in many ways ("£1,299.00", "1299"): found by their value too.
_WRITTEN_VARIOUSLY = frozenset({"money", "number", "integer", "rating", "quantity", "duration"})


@dataclass
class LearnedField:
    """What :func:`generate_schema` did for one field.

    Attributes:
        name: The field.
        status: ``"learned"`` (a selector reads it), ``"not learned"`` (no selector read the value on
            every sample page), ``"not found"`` (no sample page gave a value), ``"kept"`` (the schema's
            own selectors) or ``"skipped"`` (a list, an object, the page's address, a derived value).
        selector: The selector learned.
        found_by: How the values were found on the sample pages (``"json-ld"``, ``"dom"``, ``"model"``...).
        pages: Sample pages with a value.
        reproduced: Of those, the pages where the selector reads the same value.
        extra: Sample pages without a value where the selector reads one.
        tried: Selectors tried.
        note: Why, in words.
    """

    name: str
    status: str = "not found"
    selector: str | None = None
    found_by: str | None = None
    pages: int = 0
    reproduced: int = 0
    extra: int = 0
    tried: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, "")}


@dataclass
class GeneratedSchema:
    """A schema generated from sample pages (see the module docs).

    Attributes:
        schema: The generated schema: the one given, with the selectors learned.
        base: The schema it was generated from.
        fields: What was done for each field.
        urls: The sample pages.
        values: Per sample page, the values learned from (``{field: value}``, as JSON holds them).
        methods: Per sample page, how each of those values was found.
        model: The model that found values, if one did.
        usage: What the model used while generating (``{"input", "output", "requests"}``).
    """

    schema: Schema
    base: Schema
    fields: dict[str, LearnedField]
    urls: list[str | None]
    values: list[dict[str, Any]]
    methods: list[dict[str, str]]
    model: str | None = None
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def learned(self) -> list[str]:
        """The fields a selector was learned for."""
        return [name for name, f in self.fields.items() if f.status == "learned"]

    def expected(self, index: int) -> dict[str, Any]:
        """The values the generated schema must read on sample page ``index``, without a model: those
        learned from, but a model's answers for fields no selector was learned for."""
        return {
            name: value
            for name, value in self.values[index].items()
            if self.methods[index].get(name) != "model" or self.fields[name].status == "learned"
        }

    def describe(self) -> str:
        """Each field: its selector and on how many pages it read the value, or why it has none."""
        count = len(self.urls)
        lines = [
            f"{self.schema.name}: selectors learned for {len(self.learned)} of {len(self.fields)} fields, "
            f"from {count} page{'s' if count != 1 else ''}"
        ]
        width = max(12, *(len(name) for name in self.fields))
        for name, learned in self.fields.items():
            found = f" (found by {learned.found_by})" if learned.found_by else ""
            if learned.status == "learned":
                reads = f"{learned.reproduced}/{learned.pages} pages"
                lines.append(f"  {name.ljust(width)}  {learned.selector}  {reads}{found}")
            else:
                lines.append(f"  {name.ljust(width)}  -  {learned.status}: {learned.note}{found}".rstrip())
        if self.model:
            used = self.usage
            tokens = used.get("input", 0) + used.get("output", 0)
            spent = f"{used.get('requests', 0)} request(s), {tokens:,} token(s)" if used else "asked"
            lines.append(f"model {self.model}: {spent} while generating; the schema needs no model")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema.to_dict(),
            "fields": {name: f.to_dict() for name, f in self.fields.items()},
            "pages": [
                {"url": url, "values": values, "methods": methods}
                for url, values, methods in zip(self.urls, self.values, self.methods, strict=True)
            ],
            "model": self.model,
            "usage": dict(self.usage),
        }


def generate_schema(
    pages: Iterable[Any],
    schema: Schema | Mapping[str, Any] | str | Path = "product",
    *,
    model: Any = None,
    min_confidence: float = 0.7,
    max_tries: int = 12,
) -> GeneratedSchema:
    """Learn selectors for ``schema``'s fields from sample pages of one site (see the module docs).

    Args:
        pages: Pages holding one record each (:class:`~wintergrab.Response` s, HTML, selectors).
        schema: The record: a :class:`~wintergrab.data.Schema`, its dict, a file, or a template name.
        model: An extraction model for the values the pages' data and layout do not give; the
            generated schema reads them with selectors, without it.
        min_confidence: Values found with less confidence are not learned from.
        max_tries: Selectors tried per field, the most stable first.

    Raises:
        ConfigurationError: No page was given.
    """
    base = _schema_of(schema)
    contexts = [page if isinstance(page, PageContext) else PageContext(page) for page in pages]
    if not contexts:
        raise ConfigurationError("no sample pages to generate the schema from")
    before = dict(getattr(model, "usage", None) or {})
    extractor = Extractor(base, model=model)
    records = [extractor.extract(ctx) for ctx in contexts]
    found = [{name: fv for name, fv in r.fields.items() if _trusted(fv, min_confidence)} for r in records]
    fields = {f.name: _learn(f, base, contexts, found, max_tries) for f in base.fields}
    data = base.to_dict()
    for name, learned in fields.items():
        if learned.selector:
            data["fields"][name] = {**data["fields"][name], "selectors": [learned.selector]}
    hosts = sorted({urlsplit(ctx.url).hostname or "" for ctx in contexts if ctx.url} - {""})
    origin = f" of {', '.join(hosts)}" if hosts else ""
    data["description"] = " ".join(
        [base.description, f"Selectors learned from {len(contexts)} page(s){origin} by wintergrab."]
    ).strip()
    after = dict(getattr(model, "usage", None) or {})
    return GeneratedSchema(
        schema=Schema.from_dict(data),
        base=base,
        fields=fields,
        urls=[ctx.url for ctx in contexts],
        values=[{name: _plain(fv.value) for name, fv in page.items()} for page in found],
        methods=[{name: fv.method or "" for name, fv in page.items()} for page in found],
        model=model_name(model) if model is not None else None,
        usage={k: int(after.get(k, 0)) - int(before.get(k, 0)) for k in after} if model is not None else {},
    )


def _trusted(fv: FieldValue, min_confidence: float) -> bool:
    """Whether a value found on a sample page is one to learn from, and to expect."""
    if fv.value in (None, "", []) or fv.validation.startswith("error"):
        return False
    if fv.method == "model":
        return fv.confidence >= _MODEL_CONFIDENCE
    return fv.confidence >= min_confidence or (fv.method or "").startswith("derived")


def _learn(
    f: SchemaField, schema: Schema, contexts: list[PageContext], found: list[dict[str, FieldValue]], max_tries: int
) -> LearnedField:
    """The best selector for field ``f``: it reads the value found on every sample page that has one."""
    out = LearnedField(f.name)
    pages = [i for i, values in enumerate(found) if f.name in values]
    out.pages = len(pages)
    methods = Counter(found[i][f.name].method or "" for i in pages)
    out.found_by = methods.most_common(1)[0][0] if methods else None
    sources = {found[i][f.name].source for i in pages}
    if f.selectors:
        out.status, out.note = "kept", "the schema's own selectors"
    elif f.type == "object":
        out.status, out.note = "skipped", "nested fields are read as a whole"
    elif f.many:
        out.status, out.note = "skipped", "a list"
    elif not pages:
        out.note = "no sample page gave a value"
    elif sources == {"page url"}:
        out.status, out.note = "skipped", "the page's own address"
    elif all(method.startswith("derived:") for method in methods):
        out.status, out.note = "skipped", f"read from {next(iter(methods)).split(':', 1)[1]}"
    else:
        _choose(out, f, schema, contexts, found, pages, max_tries)
    return out


def _choose(
    out: LearnedField,
    f: SchemaField,
    schema: Schema,
    contexts: list[PageContext],
    found: list[dict[str, FieldValue]],
    pages: list[int],
    max_tries: int,
) -> None:
    proposed: dict[str, tuple[int, int, int]] = {}
    for i in pages[:_SEARCHED_PAGES]:
        for query, preference in _proposals(contexts[i], f, found[i][f.name], schema):
            proposed[query] = min(preference, proposed.get(query, preference))
    if not proposed:
        out.status = "not learned"
        out.note = "the value is not in the pages' text or attributes"
        return
    best: tuple[tuple[int, ...], str, _Check] | None = None
    closest = 0
    for query in sorted(proposed, key=lambda q: proposed[q])[:max_tries]:
        out.tried += 1
        check = _check(query, f, schema, contexts, found)
        closest = max(closest, check.reproduced)
        if check.reproduced == len(pages):
            order = (check.ambiguous, *proposed[query])
            if best is None or order < best[0]:
                best = (order, query, check)
    if best is None:
        out.status = "not learned"
        out.note = (
            f"{out.tried} selector(s) tried; the closest read the value on {closest} of {len(pages)} pages"
            if len(pages) > 1
            else f"{out.tried} selector(s) tried; none read the value"
        )
        return
    _, query, check = best
    out.status, out.selector = "learned", query
    out.reproduced, out.extra = check.reproduced, check.extra
    notes = []
    if len(pages) == 1:
        notes.append("checked on one page only")
    if check.ambiguous:
        notes.append(f"matches several values on {check.ambiguous} page(s): the first is read")
    if check.extra:
        notes.append(f"reads a value on {check.extra} page(s) where none was found")
    out.note = "; ".join(notes)


@dataclass
class _Check:
    reproduced: int = 0  # pages where the selector reads the value found
    wrong: int = 0  # pages where it reads another value, or nothing
    extra: int = 0  # pages without a value where it reads one
    ambiguous: int = 0  # pages where it matches several different values


def _check(query: str, f: SchemaField, schema: Schema, contexts: list[PageContext],
           found: list[dict[str, FieldValue]]) -> _Check:  # fmt: skip
    """How ``query`` reads field ``f`` on every sample page, as the extractor would read it."""
    check = _Check()
    try:
        # the whole schema (other fields shape a value: a price's currency goes to the currency
        # field), read with this selector alone
        probe = Extractor(_with_selectors(schema, f.name, [query]), strategies=[Selectors()], min_confidence=0)
    except Exception:
        check.wrong = len(contexts)
        return check
    kind = field_kind(f)
    for ctx, values in zip(contexts, found, strict=True):
        try:
            got = probe.extract(ctx).fields[f.name].value
            matched = ctx.root.select(query)
        except Exception:  # a selector the page's parser refuses, a value that cannot be read
            check.wrong += 1
            continue
        if len({repr(_element_value(m, kind)) for m in matched[:20]}) > 1:
            check.ambiguous += 1
        reference = values.get(f.name)
        if reference is None:
            check.extra += got not in (None, "", [])
        elif got not in (None, "", []) and _same(got, reference.value):
            check.reproduced += 1
        else:
            check.wrong += 1
    return check


# ---------------------------------------------------------------------------------------------- #
# proposing selectors
# ---------------------------------------------------------------------------------------------- #
def _proposals(
    ctx: PageContext, f: SchemaField, fv: FieldValue, schema: Schema
) -> list[tuple[str, tuple[int, int, int]]]:
    """Selectors reading ``fv``'s value on this page, each with its preference: an element in the page's
    content before one in a menu or a breadcrumb, the most stable kind of selector (see ``_ATTRIBUTE``),
    then the order proposed (the likeliest element first, its most descriptive class first)."""
    root = ctx.root.root
    if root is None:
        return []
    doc = root.getroottree().getroot()
    holders: list[tuple[etree._Element, str | None]] = []
    raw = fv.raw if isinstance(fv.raw, str) else None if fv.raw is None else str(fv.raw)
    if raw and len(raw) <= 300:  # where the page shows the value as it was found
        holders += [(m.element, m.attr) for m in sorted(_locate(doc, raw, ctx.url), key=lambda m: m.rank)[:4]]
    value = _plain(fv.value)
    if not holders or f.type in _WRITTEN_VARIOUSLY:  # where the page shows it written another way
        holders += [(el, None) for el in _holding(doc, value, lambda text: _read(schema, f.name, text))[:3]]
    if field_kind(f) == "rating":  # "star-rating Three": the rating is in the class
        for el in doc.iter(etree.Element):
            word = _class_rating(el.get("class") or "")
            if word and _same(_read(schema, f.name, word), fv.value):
                holders.append((el, "class"))
                break
    out: dict[str, tuple[int, int, int]] = {}
    for element, attr in holders:
        if tag_name(element) in ("html", "head", "body", "title"):
            continue  # the title is the meta tags' to read
        aside = int(_context_penalty(element) < 1)  # in a menu, a breadcrumb, a footer...
        suffix = f"::attr({attr})" if attr else ""
        for query, rank in _selectors_for(element, doc):
            out.setdefault(query + suffix, (aside, rank, len(out)))
    return list(out.items())


def _selectors_for(element: etree._Element, doc: etree._Element) -> list[tuple[str, int]]:
    """Selectors whose first match on the page is ``element``, with how stable each is, the most
    descriptive first."""
    tag = tag_name(element)
    proposed: list[tuple[str, int]] = []
    for attr in _MOUNT_ATTRS:  # itemprop, data-testid...: meant to be read by machines
        value = element.get(attr)
        if value and _stable(value) and '"' not in value:
            proposed.append((f'{tag}[{attr}="{value}"]', _ATTRIBUTE))
    ident = element.get("id")
    if ident and _stable(ident):
        proposed.append((f"#{ident}", _CLASS))
    classes = sorted(_classes(element), key=_class_rank)[:2]
    proposed += [(f"{tag}.{c}", _CLASS) for c in classes]
    if len(classes) == 2:
        proposed.append((f"{tag}.{classes[0]}.{classes[1]}", _CLASS))
    proposed.append((tag, _TAG))
    parent = element.getparent()
    if parent is not None and isinstance(parent.tag, str):
        for c in sorted(_classes(parent), key=_class_rank)[:2]:
            proposed.append((f"{tag_name(parent)}.{c} > {tag}", _TAG))
            proposed += [(f"{tag_name(parent)}.{c} > {tag}.{own}", _CLASS) for own in classes[:1]]
    label = _label_selector(element)
    if label:
        proposed.append((label, _LABEL))
    anchored = _anchored_path(element)
    if anchored:
        proposed.append((anchored, _ANCHORED))
    position = Selector(root=element).css_path
    if position:
        proposed.append((position, _POSITION))
    kept: list[tuple[str, int]] = []
    page = Selector(root=doc)
    for query, rank in dict.fromkeys(proposed):
        try:
            matched = page.select(query)
        except Exception:
            continue
        if matched and matched[0].root is element:
            kept.append((query, rank))
    return kept


def _anchored_path(element: etree._Element) -> str | None:
    """The element's place under its nearest ancestor with a class: ``ul.breadcrumb > li:nth-of-type(3) > a``."""
    steps: list[str] = []
    node = element
    for _ in range(4):
        parent = node.getparent()
        if parent is None or not isinstance(parent.tag, str) or tag_name(parent) in ("html", "body"):
            return None
        tag = tag_name(node)
        same = [child for child in parent if isinstance(child.tag, str) and tag_name(child) == tag]
        steps.insert(0, tag if len(same) == 1 else f"{tag}:nth-of-type({same.index(node) + 1})")
        classes = sorted(_classes(parent), key=_class_rank)
        if classes:
            return f"{tag_name(parent)}.{classes[0]} > " + " > ".join(steps)
        node = parent
    return None


def _label_selector(element: etree._Element) -> str | None:
    """An XPath reading ``element`` by the label beside it: a table row's header cell, or a
    definition list's term."""
    tag = tag_name(element)
    if tag == "td":
        row = element.getparent()
        heads = [c for c in row if isinstance(c.tag, str) and tag_name(c) == "th"] if row is not None else []
        if row is None or tag_name(row) != "tr" or len(heads) != 1:
            return None
        label, path = text_content(heads[0]), "//tr[th[normalize-space()={}]]/td"
    elif tag == "dd":
        term = element.getprevious()
        while term is not None and (not isinstance(term.tag, str) or tag_name(term) != "dt"):
            term = term.getprevious()
        if term is None:
            return None
        label, path = text_content(term), "//dt[normalize-space()={}]/following-sibling::dd[1]"
    else:
        return None
    if not label or len(label) > 40 or ('"' in label and "'" in label):
        return None
    return path.format(f"'{label}'" if '"' in label else f'"{label}"')
