"""Where a record's values came from: the page or the API call, the extractor's evidence per field, the crawl's
run and output, and what the pipeline did to each value.

Records collected with ``provenance=True`` (``crawl``/``goal --provenance``) carry ``_provenance``
(:meth:`~wintergrab.extraction.ExtractedRecord.provenance`): the page URL, the fetch time, the extractor, per
field the method, source, raw value, confidence, agreement and alternatives; a record from a site's API names
the call, its page and the field of the answer each value was read from. A crawl adds ``run`` (its id in the
run registry) and ``output`` (where the record went). A :class:`~wintergrab.data.Pipeline` adds, per field,
``transforms``: each stage that changed the value (with the value before), renamed the field (with its old
name), added it or dropped it.

::

    for record in find_records("items.jsonl", {"url": "https://shop.example/p/1"}):
        print(describe_provenance(record, ["price"]))

On the command line: ``wintergrab data trace items.jsonl price --where url=https://shop.example/p/1``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from .io import read_records

__all__ = ["describe_provenance", "find_records"]


def find_records(
    path: str | Path, where: Mapping[str, Any] | None = None, *, limit: int | None = None
) -> Iterator[dict[str, Any]]:
    """The records of ``path`` (see :func:`~wintergrab.data.io.read_records`) whose fields have the values
    ``where`` gives, compared as text (``{"url": "https://shop.example/p/1"}``); all of them without ``where``."""
    wanted = {k: str(v) for k, v in (where or {}).items()}
    found = 0
    for record in read_records(path):
        if all(str(record.get(k)) == v for k, v in wanted.items()):
            yield record
            found += 1
            if limit is not None and found >= limit:
                return


def describe_provenance(record: Mapping[str, Any], fields: Sequence[str] | None = None) -> str:
    """Where the values of ``fields`` (every field by default) came from, as lines: the page or API call, the
    fetch time, the extractor, the run and the output; then per field how it was read, what else was found,
    and what the pipeline did to it. A record without ``_provenance`` says so."""
    where = record.get("_provenance")
    if not isinstance(where, Mapping):
        return "no provenance: the records were collected without it (crawl/goal --provenance, provenance=True)"
    api = where.get("api") if isinstance(where.get("api"), Mapping) else None
    origin = str(where.get("url") or "?")
    if api is not None:
        origin = f"{api.get('method', 'GET')} {origin} (page {api.get('page', '?')} of the site's API)"
    facts = [origin]
    for key, label in (("fetched_at", "fetched"), ("extractor", "extractor"), ("run", "run"), ("output", "output")):
        if where.get(key):
            facts.append(f"{label} {where[key]}")
    lines = ["from: " + ", ".join(facts)]
    evidence: Mapping[str, Any] = where["fields"] if isinstance(where.get("fields"), Mapping) else {}
    names = list(fields) if fields else [k for k in record if not k.startswith("_")]
    for name in names:
        lines.append(f"{name}: {_show(record[name])}" if name in record else f"{name}: (not in the record)")
        if api is not None and isinstance(api.get("fields"), Mapping) and name in api["fields"]:
            lines.append(f"  read from the answer's {api['fields'][name]}")
        fv = evidence.get(name)
        if not isinstance(fv, Mapping):
            if api is None:
                lines.append("  not read by the extractor")
            continue
        if fv.get("method"):
            how = f"read by {fv['method']}" + (f" from {fv['source']}" if fv.get("source") else "")
            if fv.get("raw") is not None:
                how += f" ({_show(fv['raw'])})"
            if fv.get("confidence") is not None:
                how += f", confidence {float(fv['confidence']):.2f}"
            if fv.get("agreed"):
                how += ", agreeing with " + ", ".join(str(m) for m in fv["agreed"])
            lines.append("  " + how)
        alternatives = fv.get("alternatives") or ()
        if alternatives:
            others = "; ".join(
                f"{_show(a.get('value'))} ({'/'.join(a.get('methods') or ()) or '?'}, {float(a.get('confidence') or 0):.2f})"
                for a in alternatives[:3]
                if isinstance(a, Mapping)
            )
            lines.append(f"  also found: {others}")
        if fv.get("notes"):
            lines.append("  notes: " + ", ".join(str(n) for n in fv["notes"]))
        if fv.get("validation") not in (None, "ok"):
            lines.append(f"  validation: {fv['validation']}")
        for step in fv.get("transforms") or ():
            if isinstance(step, Mapping):
                lines.append("  " + _transform(step))
    return "\n".join(lines)


def _transform(step: Mapping[str, Any]) -> str:
    kind, stage = str(step.get("kind", "stage")), str(step.get("stage", ""))
    what = kind if not stage or stage == kind else f"{kind} ({stage})"
    if "from" in step:
        return f"{what}: was named {step['from']}"
    if step.get("dropped"):
        return f"{what}: dropped"
    if step.get("added"):
        return f"{what}: added"
    return f"{what}: was {_show(step.get('before'))}"


def _show(value: Any) -> str:
    if isinstance(value, str):
        return repr(value) if len(value) <= 60 else repr(value[:57] + "...")
    return repr(value)
