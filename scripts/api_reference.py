"""Write docs/api.md: every public name of wintergrab's modules, with its signature and summary.

    python scripts/api_reference.py          # write docs/api.md
    python scripts/api_reference.py --check  # exit 1 when docs/api.md is not up to date

The public names are each module's ``__all__``; the summary is the first sentence of the
docstring. tests/test_api_reference.py runs the check, so the reference cannot fall behind.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "docs" / "api.md"

#: The modules documented, in order, with what each is for.
MODULES = [
    ("wintergrab", "Fetching, parsing and the most used names"),
    ("wintergrab.spider", "Crawling"),
    ("wintergrab.extraction", "Typed records from pages"),
    ("wintergrab.extraction.templates", "Ready-made schemas"),
    ("wintergrab.parser.layout", "Rendered layouts"),
    ("wintergrab.parser.pdf", "PDFs"),
    ("wintergrab.data", "Schemas, normalizing, validating, pipelines, quality"),
    ("wintergrab.data.io", "Reading records"),
    ("wintergrab.data.places", "Where records are"),
    ("wintergrab.data.graph", "Knowledge graphs"),
    ("wintergrab.goals", "Goals in plain words"),
    ("wintergrab.intel", "Page types, technologies, site profiles"),
    ("wintergrab.history", "What changed between crawls"),
    ("wintergrab.runs", "Run records and replay"),
    ("wintergrab.project", "Projects and the scheduler"),
    ("wintergrab.schedules", "Schedules"),
    ("wintergrab.watch", "Watching URLs for changes"),
    ("wintergrab.webhooks", "Webhooks"),
    ("wintergrab.events", "Events"),
    ("wintergrab.models", "Language models"),
    ("wintergrab.plugins", "Plugins"),
    ("wintergrab.storage.parquet", "Parquet"),
    ("wintergrab.storage.xlsx", "Excel"),
    ("wintergrab.storage.postgres", "PostgreSQL"),
    ("wintergrab.dashboard", "The dashboard"),
    ("wintergrab.builder", "The visual builder"),
    ("wintergrab.redact", "Keeping credentials out"),
    ("wintergrab.errors", "Errors"),
]


def _summary(obj: Any) -> str:
    doc = inspect.getdoc(obj) or ""
    first = doc.strip().split("\n\n", 1)[0].replace("\n", " ")
    sentence = re.split(r"(?<=[.!?])\s", first, maxsplit=1)[0]
    return sentence.strip()


def _annotation(value: Any) -> str:
    if value is inspect.Parameter.empty:
        return ""
    return value if isinstance(value, str) else inspect.formatannotation(value)


def _default(value: Any) -> str:
    """A default as it reads the same on every Python: literals, sorted sets, callables by name."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return repr(value)
    if isinstance(value, (set, frozenset)):
        return "{" + ", ".join(sorted(map(_default, value))) + "}" if value else "set()"
    if isinstance(value, (tuple, list)):
        inner = ", ".join(_default(v) for v in value)
        return f"({inner}{',' if len(value) == 1 else ''})" if isinstance(value, tuple) else f"[{inner}]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_default(k)}: {_default(v)}" for k, v in value.items()) + "}"
    if callable(value) and getattr(value, "__qualname__", None):
        return f"{getattr(value, '__module__', None) or ''}.{value.__qualname__}".lstrip(".")
    return "..."


def _signature(obj: Any) -> str:
    try:
        signature = inspect.signature(obj)
    except (TypeError, ValueError):
        return ""
    parts: list[str] = []
    star = False
    positional_only = False
    for parameter in signature.parameters.values():
        if positional_only and parameter.kind is not inspect.Parameter.POSITIONAL_ONLY:
            parts.append("/")
        positional_only = parameter.kind is inspect.Parameter.POSITIONAL_ONLY
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY and not star:
            parts.append("*")
            star = True
        name = parameter.name
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            name, star = "*" + name, True
        elif parameter.kind is inspect.Parameter.VAR_KEYWORD:
            name = "**" + name
        annotation = _annotation(parameter.annotation)
        text = f"{name}: {annotation}" if annotation else name
        if parameter.default is not inspect.Parameter.empty:
            text += f" = {_default(parameter.default)}" if annotation else f"={_default(parameter.default)}"
        parts.append(text)
    if positional_only:
        parts.append("/")
    returned = _annotation(signature.return_annotation)
    return f"({', '.join(parts)})" + (f" -> {returned}" if returned and returned != "None" else "")


def _entry(name: str, obj: Any, owner: str) -> list[str]:
    if inspect.isclass(obj):
        kind = "exception" if issubclass(obj, BaseException) else "class"
        signature = "" if kind == "exception" else _signature(obj)
        lines = [f"- **`{name}{signature}`** ({kind}). {_summary(obj)}".rstrip()]
        if kind == "class":
            for member, value in sorted(vars(obj).items()):
                if member.startswith("_") or not (
                    inspect.isfunction(value) or isinstance(value, (classmethod, staticmethod))
                ):
                    continue
                function = value.__func__ if isinstance(value, (classmethod, staticmethod)) else value
                prefix = "classmethod " if isinstance(value, classmethod) else ""
                summary = _summary(function)
                lines.append(f"  - `{prefix}{member}{_signature(function)}`" + (f": {summary}" if summary else ""))
        return lines
    if callable(obj):
        return [f"- **`{name}{_signature(obj)}`**. {_summary(obj)}".rstrip()]
    value = _default(obj)
    if len(value) > 80 or value == "...":
        value = f"a {type(obj).__name__}"
        return [f"- **`{name}`**: {value}"]
    return [f"- **`{name}`** = `{value}`"]


def render() -> str:
    out = [
        "# API reference",
        "",
        "Every public name, by module, with its signature and what it does. The guides show them",
        "in use; this page is written from the code (`python scripts/api_reference.py`), and a test",
        "checks that it is up to date.",
        "",
    ]
    documented: dict[int, str] = {}  # the objects documented, and where
    for module_name, purpose in MODULES:
        module = importlib.import_module(module_name)
        names = list(getattr(module, "__all__", ()))
        if not names:
            continue
        out += [f"## `{module_name}`: {purpose}", ""]
        for name in names:
            obj = getattr(module, name, None)
            if obj is None:
                continue
            if callable(obj) and id(obj) in documented:
                out += [f"`{name}`: see [`{documented[id(obj)]}`](#{_anchor(documented[id(obj)])}).", ""]
                continue
            if callable(obj):
                documented[id(obj)] = module_name
            out += _entry(name, obj, module_name)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _anchor(module_name: str) -> str:
    """The anchor of a module's heading on GitHub (its heading text, lowercased, punctuation dropped)."""
    title = dict(MODULES)[module_name]
    text = f"{module_name}: {title}".lower()
    return "".join(ch for ch in text if ch.isalnum() or ch in " -_").replace(" ", "-")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="exit 1 when docs/api.md is not up to date")
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != text:
            print("docs/api.md is not up to date: run python scripts/api_reference.py", file=sys.stderr)
            return 1
        return 0
    OUTPUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)} ({text.count(chr(10)):,} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
