"""Extraction tests: pages, with the values an extractor must read from them.

::

    suite = FixtureSuite("tests/fixtures/product")
    suite.add(response, schema="product.schema.json")    # the page, and what the schema reads now
    report = suite.run()                                 # later, after a change (the suite's schema)
    print(report.describe())
    assert report.ok, report.describe()

A fixture is two files: ``0001-phone-x.json`` (the page's URL and the values expected) and
``0001-phone-x.html`` (the page). Both are text, so expected values are read, edited and reviewed
like code; ``"price": null`` says that no price must be found. :meth:`FixtureSuite.add` takes what a
schema reads now (check it before keeping it), and the values given to expect on top. Fixtures a
:class:`~wintergrab.extraction.healing.HealingExtractor` made from reviewed values
(``fixtures/0001.json.gz``) are read too.

On the command line: ``wintergrab fixture add URL --schema S --to DIR`` (or ``--from-run run-7``:
pages a recorded crawl kept) and ``wintergrab test DIR``, whose exit status is 1 when a value
differs, so a changed schema or extractor can be checked before it ships.

Values are compared as the field's type reads them: a price expected as ``"$799.00"`` and read as
``{"amount": 799, "currency": "USD"}`` is the same (a currency, when both say one, must match too);
numbers compare as numbers; text is compared as it is, spacing aside; lists item by item.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..data.schema import Schema
from ..errors import ConfigurationError
from ..fetchers.response import Response
from .engine import Extractor
from .healing import _as_schema, _normalized, _plain, _same

__all__ = ["FieldCheck", "Fixture", "FixtureResult", "FixtureSuite", "TestReport"]

_SUITE = "suite.json"
_EMPTY: tuple[Any, ...] = (None, "", [])


@dataclass
class Fixture:
    """A page and the values expected from it.

    Attributes:
        name: The files' name (``0001-phone-x``).
        url: The page's address.
        expected: ``{field: value}``; ``None`` for "nothing found".
        html: The page.
        path: The ``.json`` file (or the ``.json.gz`` of a healing extractor's fixture).
        note: Why it is there.
    """

    name: str
    url: str
    expected: dict[str, Any]
    html: bytes
    path: Path
    note: str = ""
    captured: float | None = None

    def response(self) -> Response:
        return Response(self.url, headers={"content-type": "text/html; charset=utf-8"}, body=self.html)


@dataclass
class FieldCheck:
    """One field of one fixture: what was expected, what was read."""

    field: str
    expected: Any
    got: Any
    ok: bool


@dataclass
class FixtureResult:
    """A fixture's checks (or the error that kept it from running)."""

    fixture: Fixture
    checks: list[FieldCheck] = dataclass_field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and all(c.ok for c in self.checks)

    @property
    def failed(self) -> list[FieldCheck]:
        return [c for c in self.checks if not c.ok]


@dataclass
class TestReport:
    """What running a suite gave."""

    results: list[FixtureResult] = dataclass_field(default_factory=list)
    schema: str = ""
    __test__ = False  # not a pytest test class

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> int:
        return len(self.results) - self.passed

    def describe(self, *, verbose: bool = False) -> str:
        """A line per fixture (with each value that differs), then the totals."""
        lines = []
        width = max((len(r.fixture.name) for r in self.results), default=0)
        for result in self.results:
            name = result.fixture.name.ljust(width)
            if result.error:
                lines.append(f"{name}  ERROR   {result.error}")
            elif result.ok:
                fields = ", ".join(c.field for c in result.checks) if verbose else f"{len(result.checks)} field(s)"
                lines.append(f"{name}  ok      {fields}")
            else:
                for i, check in enumerate(result.failed):
                    head = f"{name}  FAILED  " if i == 0 else " " * (width + 10)
                    lines.append(f"{head}{check.field}: expected {_show(check.expected)}, got {_show(check.got)}")
        checks = sum(len(r.checks) for r in self.results)
        wrong = sum(len(r.failed) for r in self.results)
        total = f"{len(self.results)} fixture(s): {self.passed} passed, {self.failed} failed"
        lines.append(total + (f" ({wrong} of {checks} value(s) differ)" if wrong else f" ({checks} value(s) checked)"))
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "passed": self.passed,
            "failed": self.failed,
            "schema": self.schema,
            "fixtures": [
                {
                    "name": r.fixture.name,
                    "url": r.fixture.url,
                    "ok": r.ok,
                    "error": r.error,
                    "failed": [{"field": c.field, "expected": c.expected, "got": c.got} for c in r.failed],
                }
                for r in self.results
            ],
        }


def _show(value: Any) -> str:
    if value in _EMPTY:
        return "nothing"
    if isinstance(value, dict) and "amount" in value:
        return f"{value['amount']} {value.get('currency') or ''}".strip()
    return json.dumps(value, ensure_ascii=False, default=str)


def _squeeze(text: str) -> str:
    return " ".join(text.split())


def matches(schema: Schema, name: str, expected: Any, got: Any) -> bool:
    """Whether ``got`` is the value ``expected`` for field ``name`` (see the module docs)."""
    if expected in _EMPTY:
        return got in _EMPTY
    if got in _EMPTY:
        return False
    if isinstance(expected, list) or isinstance(got, list):
        return (
            isinstance(expected, list)
            and isinstance(got, list)
            and len(expected) == len(got)
            and all(matches(schema, name, e, g) for e, g in zip(expected, got, strict=True))
        )
    if isinstance(expected, str) and isinstance(got, str):
        return _squeeze(expected) == _squeeze(got)
    if isinstance(expected, str):
        expected = _normalized(schema, name, expected)  # "$799.00" for a price
    return _same(expected, got)


def _slug(url: str, fallback: str) -> str:
    """A name for a page: its path's last segment (the one before it for ``index.html`` and the like)."""
    segments = [re.sub(r"\.(html?|php|aspx?|jsp)$", "", s, flags=re.I) for s in urlsplit(url).path.split("/") if s]
    while len(segments) > 1 and segments[-1].lower() in ("index", "default", "home", "main"):
        segments.pop()
    last = segments[-1] if segments else urlsplit(url).hostname or fallback
    slug = re.sub(r"[^a-z0-9]+", "-", last.lower()).strip("-")[:40]
    return slug or fallback


class FixtureSuite:
    """The fixtures in a directory (see the module docs).

    Args:
        directory: Where the fixtures are (created by :meth:`add`).
    """

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self.directory = Path(directory)

    # -- the suite's schema -------------------------------------------------------------------- #
    @property
    def schema_path(self) -> Path | None:
        """The schema the suite was made with (kept in ``suite.json``, relative to the directory)."""
        try:
            data = json.loads((self.directory / _SUITE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        schema = data.get("schema")
        return (self.directory / schema).resolve() if schema else None

    def _remember(self, schema: Any) -> None:
        if not isinstance(schema, (str, os.PathLike)) or (self.directory / _SUITE).exists():
            return
        relative = os.path.relpath(Path(schema).resolve(), self.directory.resolve())
        (self.directory / _SUITE).write_text(json.dumps({"schema": relative}, indent=2) + "\n", encoding="utf-8")

    def _extractor(self, schema: Any) -> Extractor:
        if isinstance(schema, Extractor):
            return schema
        if schema is None:
            schema = self.schema_path
            if schema is None:
                raise ConfigurationError(f"which schema? {self.directory} does not say (give one)")
        return Extractor(_as_schema(schema))

    # -- fixtures ------------------------------------------------------------------------------ #
    def fixtures(self, names: Iterable[str] | None = None) -> list[Fixture]:
        """The fixtures, by name (``names``: only these, or those whose names start with them)."""
        found: list[Fixture] = []
        if not self.directory.exists():
            return found
        for path in sorted(self.directory.glob("*.json")):
            if path.name == _SUITE:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            page = path.with_suffix(".html")
            html = page.read_bytes() if page.exists() else b""
            found.append(Fixture(path.stem, data.get("url", ""), dict(data.get("expected") or {}), html, path,
                                 data.get("note", ""), data.get("captured")))  # fmt: skip
        for path in sorted(self.directory.glob("*.json.gz")):  # a healing extractor's fixtures
            data = json.loads(gzip.decompress(path.read_bytes()))
            found.append(Fixture(path.name[: -len(".json.gz")], data["url"], dict(data["expected"]),
                                 data["html"].encode("utf-8"), path, data.get("by", "")))  # fmt: skip
        if names is not None:
            wanted = list(names)
            found = [f for f in found if any(f.name == n or f.name.startswith(n) for n in wanted)]
        return found

    def add(
        self,
        page: Response | str | bytes,
        *,
        schema: Any = None,
        expect: Mapping[str, Any] | None = None,
        only: bool = False,
        url: str | None = None,
        name: str | None = None,
        note: str = "",
    ) -> Fixture:
        """Keep ``page`` as a fixture.

        Args:
            page: A :class:`Response`, or HTML (then give ``url``).
            schema: What reads the page (a schema, its file, or an :class:`Extractor`); its values now
                are expected (the empty ones and ``_`` fields aside). The first file given is the suite's.
            expect: Values to expect, over what the schema reads (``None``: nothing must be found).
            only: Expect only ``expect``.
            name: The files' name after the number (by default from the URL).
        """
        response = page if isinstance(page, Response) else Response(
            url or "", headers={"content-type": "text/html; charset=utf-8"},
            body=page.encode("utf-8") if isinstance(page, str) else page)  # fmt: skip
        address = url or response.url
        values: dict[str, Any] = {}
        if not only and (schema is not None or self.schema_path is not None):
            record = self._extractor(schema).extract(response)
            values = {k: _plain(v) for k, v in record.data.items() if not k.startswith("_") and v not in _EMPTY}
        values.update(expect or {})
        if not values:
            raise ConfigurationError("nothing to expect: give a schema that reads something, or values")
        self.directory.mkdir(parents=True, exist_ok=True)
        if schema is not None:
            self._remember(schema)
        numbers = [int(p.name[:4]) for p in self.directory.glob("[0-9][0-9][0-9][0-9]*.json*")]
        stem = f"{max(numbers, default=0) + 1:04d}-{name or _slug(address, 'page')}"
        path = self.directory / f"{stem}.json"
        data = {"url": address, "expected": values, "captured": round(time.time(), 3), "note": note}
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        path.with_suffix(".html").write_bytes(response.body)
        return Fixture(stem, address, values, response.body, path, note, data["captured"])  # type: ignore[arg-type]

    # -- running ------------------------------------------------------------------------------- #
    def run(self, schema: Any = None, *, names: Sequence[str] | None = None) -> TestReport:
        """Read every fixture's page with ``schema`` (the suite's by default) and check the values."""
        extractor = self._extractor(schema)
        report = TestReport(schema=str(schema if schema is not None else self.schema_path or ""))
        for fixture in self.fixtures(names):
            result = FixtureResult(fixture)
            try:
                data = extractor.extract(fixture.response()).data
            except Exception as exc:  # a broken schema, a page it chokes on: the report says
                result.error = f"{type(exc).__name__}: {exc}"
                report.results.append(result)
                continue
            for name, expected in fixture.expected.items():
                got = _plain(data.get(name))
                known = name in extractor.schema  # a field the schema does not have reads nothing
                ok = matches(extractor.schema, name, expected, got) if known else expected in _EMPTY
                result.checks.append(FieldCheck(name, expected, got, ok))
            report.results.append(result)
        return report

    def update(self, report: TestReport) -> int:
        """Accept what a run read where it differed: its values become the expected ones (in the
        ``.json`` fixtures; review the change). Returns how many fixtures changed."""
        changed = 0
        for result in report.results:
            fixture = result.fixture
            if result.ok or result.error or fixture.path.suffix != ".json":
                continue
            data = json.loads(fixture.path.read_text(encoding="utf-8"))
            for check in result.failed:
                data["expected"][check.field] = check.got if check.got not in _EMPTY else None
            fixture.path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n",
                                    encoding="utf-8")  # fmt: skip
            changed += 1
        return changed

    def __repr__(self) -> str:
        return f"FixtureSuite({str(self.directory)!r})"
