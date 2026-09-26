"""Declarative record pipelines: rename, select, transform, compute, filter, normalize, validate, de-duplicate, enrich.

A :class:`Pipeline` runs records (dicts) through a list of stages. Each stage
returns the record, possibly changed, or drops it::

    from wintergrab.data import (Compute, Deduplicate, Filter, Normalize, Pipeline, Rename,
                                 Select, Transform, Validate)

    pipeline = Pipeline([
        Rename({"cost": "price", "title": "name"}),
        Transform("name", ["clean"]),
        Normalize(schema),                    # "₹29,999" -> price 29999, currency "INR"
        Validate(schema, on_error="drop"),    # or "flag", "keep", "raise"
        Filter("price > 0"),
        Compute("discount", "round(1 - sale_price / price, 2)"),
        Deduplicate(key="url"),
        Select(["name", "price", "currency", "discount", "url"]),
    ])
    clean = pipeline.run(records)
    print(pipeline.describe())               # what each stage did

A pipeline is also an item pipeline: with ``Spider.pipelines = [pipeline]``
every scraped item goes through it, and a dropped item is counted under
``items_dropped`` with the stage and the reason.

Pipelines load from JSON, YAML or TOML files (:meth:`Pipeline.load`)::

    stages:
      - rename: {cost: price, title: name}
      - transform: {field: name, ops: [clean]}
      - normalize: {schema: product.schema.json}
      - validate: {on_error: flag, rules: [{code: sale-below-list, check: "sale_price <= price"}]}
      - filter: "price > 0"
      - compute: {discount: "round(1 - sale_price / price, 2)"}
      - dedupe: {key: url}
      - select: [name, price, currency, discount, url]

Filters, computed fields and rules use the expression language of
:mod:`wintergrab.data.expressions`, which can read records but not run code. A
configuration file can only name Python functions to call (``enrich``,
``function``) when it is loaded with ``allow_imports=True``.
"""

from __future__ import annotations

import csv
import dataclasses
import fnmatch
import importlib
import inspect
import json
import logging
import re
from collections import Counter
from collections.abc import AsyncIterable, Callable, Iterable, Iterator, Mapping, Sequence
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from typing import IO, Any, ClassVar

from ..errors import ConfigurationError, ExpressionError, ValidationError, WintergrabError
from ..files import read_structured, yaml_module
from ..utils import maybe_await
from .dedupe import Deduplicator
from .expressions import Expression, compile_expression, get_path
from .issues import Issue
from .normalize import (
    as_int_or_float,
    clean_text,
    convert,
    currency_minor_units,
    detect_currency,
    fix_mojibake,
    normalize_country,
    normalize_url_value,
    parse_boolean,
    parse_date,
    parse_datetime,
    parse_integer,
    parse_money,
    parse_number,
    parse_percent,
    parse_quantity,
    unit_info,
)
from .places import DEFAULT_PARTS, PLACE_FIELDS, PLACE_PARTS
from .quality import QualityMonitor, QualityReport
from .schema import FieldResult, NormalizeContext, Schema, load_schema
from .similarity import content_hash
from .validate import Rule, validate_record

__all__ = [
    "OPERATIONS",
    "Analyze",
    "Classify",
    "Compute",
    "ConfigLoader",
    "ConvertCurrency",
    "Deduplicate",
    "Enrich",
    "Exclude",
    "Filter",
    "Locate",
    "Lookup",
    "Normalize",
    "Operation",
    "Pipeline",
    "QualityCheck",
    "RecordContext",
    "Rename",
    "Select",
    "Stage",
    "Transform",
    "Validate",
    "register_operation",
    "register_stage",
]

log = logging.getLogger("wintergrab.pipeline")

_LOGGED_ERRORS = 5  # per stage; later errors are only counted
_DATA_ERRORS = (ValueError, TypeError, ArithmeticError, LookupError)


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, (str, list, dict, tuple)) and not value)


def _is_pattern(name: str) -> bool:
    return any(ch in name for ch in "*?[")


def _choice(value: str, allowed: Sequence[str], what: str, stage: str) -> str:
    if value not in allowed:
        raise ConfigurationError(f"{what} must be one of {', '.join(map(repr, allowed))}, not {value!r}", key=stage)
    return value


def _as_record(item: Any) -> dict[str, Any] | None:
    """A mutable dict for an item (dicts are copied), or ``None`` for items that are not records."""
    if isinstance(item, Mapping):
        return dict(item)
    if dataclasses.is_dataclass(item) and not isinstance(item, type):
        return dataclasses.asdict(item)
    dump = getattr(item, "model_dump", None)  # pydantic
    if callable(dump):
        data = dump()
        return data if isinstance(data, dict) else None
    return None


@dataclasses.dataclass
class RecordContext:
    """What one record's trip through a pipeline has gathered."""

    spider: Any = None
    #: The schema the record was last normalized with, and how each field went.
    schema: Schema | None = None
    results: dict[str, FieldResult] | None = None
    #: Which stage dropped the record, and why.
    dropped_by: str = ""
    reason: str = ""


def _item_result(record: dict[str, Any] | None, ctx: RecordContext) -> dict[str, Any]:
    if record is None:
        from ..spider.middleware import DropItem

        where = f"{ctx.dropped_by}: " if ctx.dropped_by else ""
        raise DropItem(f"{where}{ctx.reason or 'dropped'}")
    return record


# --------------------------------------------------------------------------- #
# stages
# --------------------------------------------------------------------------- #
class Stage:
    """Base class of pipeline stages: override :meth:`apply` (or :meth:`aapply`).

    Every stage counts ``in``, ``out``, ``dropped`` and ``errors`` (problems that
    did not stop it, e.g. a value an operation could not convert) in
    :attr:`stats`, and works on its own as an item pipeline.
    """

    kind: ClassVar[str] = "stage"

    def __init__(self, *, name: str | None = None) -> None:
        self.name = name or self.kind
        self.stats: Counter[str] = Counter()
        self._logged = 0

    # -- the work ----------------------------------------------------------- #
    def apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None:
        """Return the record (changed in place, or a new dict), or ``None`` to drop it (set ``ctx.reason``)."""
        raise NotImplementedError

    async def aapply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None:
        """The asynchronous version of :meth:`apply` (override it for stages that wait on I/O)."""
        return self.apply(record, ctx)

    @property
    def is_async(self) -> bool:
        """Whether the stage must run through :meth:`aprocess`."""
        return False

    def process(self, record: dict[str, Any], ctx: RecordContext | None = None) -> dict[str, Any] | None:
        """Run the stage on ``record`` (which it may change), counting what happens."""
        ctx = ctx if ctx is not None else RecordContext()
        self.stats["in"] += 1
        return self._count(self.apply(record, ctx), ctx)

    async def aprocess(self, record: dict[str, Any], ctx: RecordContext | None = None) -> dict[str, Any] | None:
        ctx = ctx if ctx is not None else RecordContext()
        self.stats["in"] += 1
        return self._count(await self.aapply(record, ctx), ctx)

    def _count(self, record: dict[str, Any] | None, ctx: RecordContext) -> dict[str, Any] | None:
        if record is None:
            self.stats["dropped"] += 1
            ctx.dropped_by = self.name
        else:
            self.stats["out"] += 1
        return record

    def error(self, message: str) -> None:
        """Count a problem that did not stop the stage (the first few are logged)."""
        self.stats["errors"] += 1
        if self._logged < _LOGGED_ERRORS:
            self._logged += 1
            more = " (further errors are only counted)" if self._logged == _LOGGED_ERRORS else ""
            log.warning("%s: %s%s", self.name, message, more)

    def details(self) -> str:
        """A short note for :meth:`Pipeline.describe`."""
        return ""

    # -- conveniences and the item-pipeline protocol -------------------------- #
    def __call__(self, record: Mapping[str, Any]) -> dict[str, Any] | None:
        """Process a copy of ``record``: the result, or ``None`` if the stage dropped it."""
        if self.is_async:
            raise ConfigurationError(f"{self.name} is asynchronous: use 'await stage.aprocess(record)'")
        return self.process(dict(record))

    def process_item(self, item: Any, spider: Any = None) -> Any:
        record = _as_record(item)
        if record is None:
            return item
        ctx = RecordContext(spider=spider)
        if self.is_async:
            return self._aprocess_item(record, ctx)
        return _item_result(self.process(record, ctx), ctx)

    async def _aprocess_item(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any]:
        return _item_result(await self.aprocess(record, ctx), ctx)

    def open_spider(self, spider: Any) -> Any:
        """Called once before a crawl."""

    def close_spider(self, spider: Any) -> Any:
        """Called once after a crawl."""
        self.close()

    def close(self) -> None:
        """Release files and other resources (the stage can still be used afterwards)."""

    # -- configuration ------------------------------------------------------- #
    @classmethod
    def from_config(cls, options: Any, loader: ConfigLoader) -> Stage:
        """A stage from its configuration-file form."""
        raise ConfigurationError(f"{cls.kind!r} stages cannot be loaded from configuration")

    def to_config(self) -> dict[str, Any]:
        """The stage's configuration-file form: ``{kind: options}``."""
        raise ConfigurationError(f"{type(self).__name__} {self.name!r} cannot be written as configuration")

    def _named(self, options: dict[str, Any]) -> dict[str, Any]:
        if self.name != self.kind and not self.name.startswith(self.kind + "#"):
            options["name"] = self.name
        return {self.kind: options}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r})"


def _options(options: Any, allowed: set[str], kind: str, *, required: Sequence[str] = ()) -> dict[str, Any]:
    if options is None:
        options = {}
    if not isinstance(options, Mapping):
        raise ConfigurationError(f"expected a mapping of options, got {type(options).__name__}", key=kind)
    unknown = set(options) - allowed
    if unknown:
        raise ConfigurationError(
            f"unknown option(s) {', '.join(sorted(map(str, unknown)))}; known: {', '.join(sorted(allowed))}", key=kind
        )
    missing = [name for name in required if name not in options]
    if missing:
        raise ConfigurationError(f"missing option(s) {', '.join(missing)}", key=kind)
    return dict(options)


def _names(value: Any, what: str, stage: str) -> list[str]:
    names = [value] if isinstance(value, str) else list(value) if isinstance(value, (list, tuple)) else None
    if not names or not all(isinstance(n, str) and n for n in names):
        raise ConfigurationError(f"{what} must be a field name or a list of field names", key=stage)
    return names


class Rename(Stage):
    """Rename fields: ``Rename({"cost": "price", "title": "name"})``.

    A renamed field keeps its position. Several old names may map to one new
    name (``{"cost": "price", "amount": "price"}``): the first with a value
    wins. An empty value never replaces a value already under the new name.
    """

    kind = "rename"

    def __init__(self, mapping: Mapping[str, str], *, name: str | None = None) -> None:
        super().__init__(name=name)
        if not isinstance(mapping, Mapping) or not mapping:
            raise ConfigurationError("expected a non-empty {old name: new name} mapping", key=self.name)
        for old, new in mapping.items():
            if not isinstance(old, str) or not isinstance(new, str) or not old or not new:
                raise ConfigurationError(f"cannot rename {old!r} to {new!r}: names must be strings", key=self.name)
        self.mapping = dict(mapping)
        self._targets: dict[str, list[str]] = {}
        for old, new in self.mapping.items():
            self._targets.setdefault(new, []).append(old)
        self._sources = frozenset(self.mapping)

    def apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None:
        if self._sources.isdisjoint(record):
            return record
        values: dict[str, Any] = {}
        for target, sources in self._targets.items():
            present = [source for source in sources if source in record]
            if not present:
                continue
            value = next((record[s] for s in present if not _empty(record[s])), record[present[0]])
            if _empty(value) and target not in self._sources and not _empty(record.get(target)):
                continue  # keep the value already under the new name
            values[target] = value
        out: dict[str, Any] = {}
        for key, value in record.items():
            if key in self._sources:
                target = self.mapping[key]
                if target in values and target not in out:
                    out[target] = values[target]
            elif key in values:
                out.setdefault(key, values[key])
            else:
                out[key] = value
        return out

    @classmethod
    def from_config(cls, options: Any, loader: ConfigLoader) -> Rename:
        if not isinstance(options, Mapping):
            raise ConfigurationError("expected a {old name: new name} mapping", key=cls.kind)
        return cls(options)

    def to_config(self) -> dict[str, Any]:
        return {self.kind: dict(self.mapping)}


class Select(Stage):
    """Keep only these fields, in this order: ``Select(["name", "price", "url"])``.

    Names may be patterns (``"price_*"``). A field the record lacks is added as
    ``None`` (``fill_missing=False`` leaves it out), so every record has the
    same columns. Metadata fields (``_issues``, ``_duplicate_of``...) are kept
    unless ``keep_metadata=False``.
    """

    kind = "select"

    def __init__(
        self,
        fields: str | Sequence[str],
        *,
        fill_missing: bool = True,
        keep_metadata: bool = True,
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        self.fields = _names(fields, "fields", self.name)
        self.fill_missing = fill_missing
        self.keep_metadata = keep_metadata

    def apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None:
        out: dict[str, Any] = {}
        for entry in self.fields:
            if _is_pattern(entry):
                for key, value in record.items():
                    if key not in out and fnmatch.fnmatchcase(str(key), entry):
                        out[key] = value
            elif entry in record:
                out[entry] = record[entry]
            elif self.fill_missing:
                out[entry] = None
        if self.keep_metadata:
            for key, value in record.items():
                if str(key).startswith("_") and key not in out:
                    out[key] = value
        return out

    @classmethod
    def from_config(cls, options: Any, loader: ConfigLoader) -> Select:
        if isinstance(options, (str, list, tuple)):
            return cls(options)
        opts = _options(options, {"fields", "fill_missing", "keep_metadata", "name"}, cls.kind, required=["fields"])
        return cls(**opts)

    def to_config(self) -> dict[str, Any]:
        if self.fill_missing and self.keep_metadata and self.name == self.kind:
            return {self.kind: list(self.fields)}
        options: dict[str, Any] = {"fields": list(self.fields)}
        if not self.fill_missing:
            options["fill_missing"] = False
        if not self.keep_metadata:
            options["keep_metadata"] = False
        return self._named(options)


class Exclude(Stage):
    """Remove fields: ``Exclude(["html", "_raw*"])`` (patterns allowed)."""

    kind = "exclude"

    def __init__(self, fields: str | Sequence[str], *, name: str | None = None) -> None:
        super().__init__(name=name)
        self.fields = _names(fields, "fields", self.name)
        self._exact = frozenset(f for f in self.fields if not _is_pattern(f))
        self._patterns = [f for f in self.fields if _is_pattern(f)]

    def _excluded(self, key: Any) -> bool:
        return key in self._exact or any(fnmatch.fnmatchcase(str(key), p) for p in self._patterns)

    def apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None:
        return {k: v for k, v in record.items() if not self._excluded(k)}

    @classmethod
    def from_config(cls, options: Any, loader: ConfigLoader) -> Exclude:
        if isinstance(options, (str, list, tuple)):
            return cls(options)
        return cls(**_options(options, {"fields", "name"}, cls.kind, required=["fields"]))

    def to_config(self) -> dict[str, Any]:
        if self.name == self.kind:
            return {self.kind: list(self.fields)}
        return self._named({"fields": list(self.fields)})


# --------------------------------------------------------------------------- #
# value operations (Transform)
# --------------------------------------------------------------------------- #
OperationFn = Callable[[Any, Mapping[str, Any]], Any]


@dataclasses.dataclass(frozen=True)
class Operation:
    """A named value operation for :class:`Transform`.

    Attributes:
        build: ``build(argument) -> fn(value, record)``; raising ``ValueError`` rejects the argument.
        elementwise: Applied to each item of a list value (otherwise to the whole value).
        strict: ``None`` from a value counts as a failure (a parser that found nothing).
        accepts_none: Also called for missing values (e.g. ``default``).
        argument: ``"none"``, ``"optional"`` or ``"required"``.
    """

    name: str
    build: Callable[[Any], OperationFn]
    elementwise: bool = True
    strict: bool = False
    accepts_none: bool = False
    argument: str = "none"


#: Operations by name. Add your own with :func:`register_operation`.
OPERATIONS: dict[str, Operation] = {}


def register_operation(
    name: str,
    build: Callable[[Any], OperationFn],
    *,
    elementwise: bool = True,
    strict: bool = False,
    accepts_none: bool = False,
    argument: str = "none",
) -> Operation:
    """Add a :class:`Transform` operation (see :class:`Operation` for the options)."""
    if argument not in ("none", "optional", "required"):
        raise ValueError("argument must be 'none', 'optional' or 'required'")
    operation = Operation(name, build, elementwise, strict, accepts_none, argument)
    OPERATIONS[name] = operation
    return operation


def _s(value: Any) -> str:
    return value if isinstance(value, str) else str(value)


def _simple(fn: Callable[[Any], Any]) -> Callable[[Any], OperationFn]:
    return lambda arg: lambda value, record: fn(value)


def _number_or_none(value: Decimal | None) -> int | float | None:
    return None if value is None else as_int_or_float(value)


def _build_strip(arg: Any) -> OperationFn:
    chars = None if arg is None else str(arg)
    return lambda value, record: _s(value).strip(chars)


def _build_number(arg: Any) -> OperationFn:
    if arg not in (None, ".", ","):
        raise ValueError("the decimal separator must be '.' or ','")
    return lambda value, record: _number_or_none(parse_number(value, decimal=arg))


def _build_date(arg: Any) -> OperationFn:
    dayfirst = _dayfirst(arg)

    def run(value: Any, record: Mapping[str, Any]) -> Any:
        parsed = parse_date(value, dayfirst=dayfirst)
        return None if parsed is None else parsed.isoformat()

    return run


def _build_datetime(arg: Any) -> OperationFn:
    dayfirst = _dayfirst(arg)

    def run(value: Any, record: Mapping[str, Any]) -> Any:
        parsed = parse_datetime(value, dayfirst=dayfirst)
        return None if parsed is None else parsed.isoformat()

    return run


def _dayfirst(arg: Any) -> bool | None:
    if arg is None or isinstance(arg, bool):
        return arg
    if arg == "dayfirst":
        return True
    raise ValueError("the argument must be true/false (day first?) or 'dayfirst'")


def _build_money(arg: Any) -> OperationFn:
    currency = None if arg is None else str(arg).upper()

    def run(value: Any, record: Mapping[str, Any]) -> Any:
        money = parse_money(value, currency=currency)
        return None if money is None else as_int_or_float(money.amount)

    return run


def _build_currency(arg: Any) -> OperationFn:
    default = None if arg is None else str(arg).upper()
    return lambda value, record: detect_currency(_s(value), default=default)


def _build_unit(arg: Any) -> OperationFn:
    if isinstance(arg, Mapping):
        target, default = arg.get("to"), arg.get("from")
    else:
        target, default = arg, None
    if not isinstance(target, str):
        raise ValueError("expected a unit ('kg') or {to: kg, from: g}")
    symbol, _ = unit_info(target)
    if default is not None:
        unit_info(str(default))

    def run(value: Any, record: Mapping[str, Any]) -> Any:
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            if default is None:
                return None  # a bare number: its unit is unknown
            value = f"{value} {default}"
        quantity = parse_quantity(_s(value), default_unit=default)
        return None if quantity is None else as_int_or_float(round(convert(quantity, symbol).value, 9))

    return run


def _build_url(arg: Any) -> OperationFn:
    fixed = None if arg is None else str(arg)

    def run(value: Any, record: Mapping[str, Any]) -> Any:
        base = fixed
        if base is None:
            page = record.get("url")
            base = page if isinstance(page, str) and page.startswith(("http://", "https://")) else None
        return normalize_url_value(_s(value), base_url=base)

    return run


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid regular expression {pattern!r}: {exc}") from None


def _build_regex(arg: Any) -> OperationFn:
    if isinstance(arg, Mapping):
        pattern, group = arg.get("pattern"), arg.get("group", 1)
    else:
        pattern, group = arg, 1
    if not isinstance(pattern, str) or not isinstance(group, (int, str)) or isinstance(group, bool):
        raise ValueError("expected a pattern or {pattern: ..., group: ...}")
    compiled = _compile_pattern(pattern)
    if compiled.groups == 0 and group == 1:
        group = 0  # no group in the pattern: the whole match
    elif isinstance(group, int) and group > compiled.groups:
        raise ValueError(f"the pattern has no group {group}")
    elif isinstance(group, str) and group not in compiled.groupindex:
        raise ValueError(f"the pattern has no group named {group!r}")

    def run(value: Any, record: Mapping[str, Any]) -> Any:
        match = compiled.search(_s(value))
        return None if match is None else match.group(group)

    return run


def _pair(arg: Any, first: str, second: str) -> tuple[str, str]:
    if isinstance(arg, Mapping):
        a, b = arg.get(first), arg.get(second)
    elif isinstance(arg, (list, tuple)) and len(arg) == 2:
        a, b = arg
    else:
        raise ValueError(f"expected [{first}, {second}] or {{{first}: ..., {second}: ...}}")
    if not isinstance(a, str) or not isinstance(b, str):
        raise ValueError(f"{first} and {second} must be strings")
    return a, b


def _build_replace(arg: Any) -> OperationFn:
    old, new = _pair(arg, "old", "new")
    return lambda value, record: _s(value).replace(old, new)


def _build_sub(arg: Any) -> OperationFn:
    pattern, replacement = _pair(arg, "pattern", "replacement")
    compiled = _compile_pattern(pattern)
    return lambda value, record: compiled.sub(replacement, _s(value))


def _build_prefix(arg: Any) -> OperationFn:
    text = str(arg)
    return lambda value, record: text + _s(value)


def _build_suffix(arg: Any) -> OperationFn:
    text = str(arg)
    return lambda value, record: _s(value) + text


def _build_truncate(arg: Any) -> OperationFn:
    if not isinstance(arg, int) or isinstance(arg, bool) or arg < 0:
        raise ValueError("expected a number of characters")
    return lambda value, record: _s(value)[:arg]


def _build_map(arg: Any) -> OperationFn:
    if not isinstance(arg, Mapping):
        raise ValueError("expected a {from: to} mapping")
    table = dict(arg)
    folded = {k.strip().casefold(): v for k, v in table.items() if isinstance(k, str)}

    def run(value: Any, record: Mapping[str, Any]) -> Any:
        try:
            if value in table:
                return table[value]
        except TypeError:  # unhashable
            return value
        if isinstance(value, str):
            key = value.strip().casefold()
            if key in folded:
                return folded[key]
        return value

    return run


def _build_round(arg: Any) -> OperationFn:
    if arg is not None and (not isinstance(arg, int) or isinstance(arg, bool)):
        raise ValueError("expected a number of digits")
    return lambda value, record: round(value) if arg is None else round(value, arg)


def _build_multiply(arg: Any) -> OperationFn:
    if not isinstance(arg, (int, float)) or isinstance(arg, bool):
        raise ValueError("expected a number")

    def run(value: Any, record: Mapping[str, Any]) -> Any:
        if not isinstance(value, (int, float, Decimal)) or isinstance(value, bool):
            raise TypeError(f"not a number: {value!r}")
        result = Decimal(str(value)) * Decimal(str(arg))
        return as_int_or_float(result)

    return run


def _build_nullif(arg: Any) -> OperationFn:
    values = list(arg) if isinstance(arg, (list, tuple)) else [arg]
    folded = {v.strip().casefold() for v in values if isinstance(v, str)}

    def run(value: Any, record: Mapping[str, Any]) -> Any:
        if isinstance(value, str):
            return None if value.strip().casefold() in folded else value
        return None if value in values else value

    return run


def _build_default(arg: Any) -> OperationFn:
    return lambda value, record: arg if _empty(value) else value


def _first(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return next((v for v in value if not _empty(v)), None)
    return value


def _last(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return next((v for v in reversed(value) if not _empty(v)), None)
    return value


def _build_join(arg: Any) -> OperationFn:
    separator = " " if arg is None else str(arg)

    def run(value: Any, record: Mapping[str, Any]) -> Any:
        if isinstance(value, (list, tuple)):
            return separator.join(_s(v) for v in value if not _empty(v))
        return value

    return run


def _build_split(arg: Any) -> OperationFn:
    separator = None if arg is None else str(arg)

    def run(value: Any, record: Mapping[str, Any]) -> Any:
        items = value if isinstance(value, (list, tuple)) else [value]
        return [
            part.strip() for item in items if item is not None for part in _s(item).split(separator) if part.strip()
        ]

    return run


def _unique(value: Any) -> Any:
    if not isinstance(value, (list, tuple)):
        return value
    seen: set[Any] = set()
    out = []
    for item in value:
        try:
            marker = ("h", item) if not isinstance(item, (dict, list)) else ("c", content_hash(item))
            hash(marker)
        except TypeError:
            marker = ("c", content_hash(repr(item)))
        if marker not in seen:
            seen.add(marker)
            out.append(item)
    return out


def _compact(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return [v for v in value if not _empty(v)]
    return value


def _length(value: Any) -> int:
    return 0 if value is None else len(value)


def _build_expr(arg: Any) -> OperationFn:
    if not isinstance(arg, str):
        raise ValueError("expected an expression")
    expression = compile_expression(arg)
    return lambda value, record: expression(record, value=value)


def _build_json(arg: Any) -> OperationFn:
    def run(value: Any, record: Mapping[str, Any]) -> Any:
        if not isinstance(value, str):
            return value
        return json.loads(value)

    return run


_BUILTIN_OPERATIONS: list[tuple[str, Callable[[Any], OperationFn], dict[str, Any]]] = [
    ("strip", _build_strip, {"argument": "optional"}),
    ("lower", _simple(lambda v: _s(v).lower()), {}),
    ("upper", _simple(lambda v: _s(v).upper()), {}),
    ("title", _simple(lambda v: _s(v).title()), {}),
    ("clean", _simple(lambda v: clean_text(_s(v))), {}),
    ("fix_encoding", _simple(lambda v: fix_mojibake(_s(v))), {}),
    ("number", _build_number, {"strict": True, "argument": "optional"}),
    ("integer", _simple(parse_integer), {"strict": True}),
    ("percent", _simple(lambda v: _number_or_none(parse_percent(v))), {"strict": True}),
    ("boolean", _simple(parse_boolean), {"strict": True}),
    ("date", _build_date, {"strict": True, "argument": "optional"}),
    ("datetime", _build_datetime, {"strict": True, "argument": "optional"}),
    ("money", _build_money, {"strict": True, "argument": "optional"}),
    ("currency", _build_currency, {"strict": True, "argument": "optional"}),
    ("unit", _build_unit, {"strict": True, "argument": "required"}),
    ("url", _build_url, {"strict": True, "argument": "optional"}),
    ("regex", _build_regex, {"strict": True, "argument": "required"}),
    ("replace", _build_replace, {"argument": "required"}),
    ("sub", _build_sub, {"argument": "required"}),
    ("prefix", _build_prefix, {"argument": "required"}),
    ("suffix", _build_suffix, {"argument": "required"}),
    ("truncate", _build_truncate, {"argument": "required"}),
    ("map", _build_map, {"argument": "required", "accepts_none": True}),
    ("round", _build_round, {"argument": "optional"}),
    ("multiply", _build_multiply, {"argument": "required"}),
    ("nullif", _build_nullif, {"argument": "required"}),
    ("json", _build_json, {"strict": True}),
    ("default", _build_default, {"argument": "required", "accepts_none": True, "elementwise": False}),
    ("first", _simple(_first), {"elementwise": False}),
    ("last", _simple(_last), {"elementwise": False}),
    ("join", _build_join, {"argument": "optional", "elementwise": False}),
    ("split", _build_split, {"argument": "optional", "elementwise": False}),
    ("unique", _simple(_unique), {"elementwise": False}),
    ("compact", _simple(_compact), {"elementwise": False}),
    ("length", _simple(_length), {"elementwise": False, "accepts_none": True}),
    ("expr", _build_expr, {"argument": "required", "elementwise": False, "accepts_none": True}),
]
for _name, _build, _flags in _BUILTIN_OPERATIONS:
    register_operation(_name, _build, **_flags)
del _name, _build, _flags


class _OpFailed(Exception):
    def __init__(self, operation: str, value: Any, message: str) -> None:
        super().__init__(f"{operation} failed on {value!r}: {message}")
        self.operation = operation
        self.value = value


@dataclasses.dataclass(frozen=True)
class _CompiledOp:
    name: str
    fn: OperationFn
    elementwise: bool
    strict: bool
    accepts_none: bool
    spec: Any

    def run(self, value: Any, record: Mapping[str, Any]) -> Any:
        try:
            result = self.fn(value, record)
        except (*_DATA_ERRORS, WintergrabError) as exc:
            raise _OpFailed(self.name, value, f"{type(exc).__name__}: {exc}") from None
        if result is None and self.strict:
            raise _OpFailed(self.name, value, "no value found")
        return result


def _compile_op(spec: Any, stage: str) -> _CompiledOp:
    if callable(spec) and not isinstance(spec, (str, Mapping)):
        fn = spec
        label = getattr(spec, "__name__", type(spec).__name__)
        return _CompiledOp(label, lambda value, record: fn(value), True, False, False, spec)
    if isinstance(spec, str):
        name, arg, given = spec, None, False
    elif isinstance(spec, Mapping) and len(spec) == 1:
        name, arg = next(iter(spec.items()))
        given = True
    else:
        raise ConfigurationError(
            f"an operation is a name, a {{name: argument}} mapping or a function: {spec!r}", key=stage
        )
    operation = OPERATIONS.get(str(name))
    if operation is None:
        raise ConfigurationError(f"unknown operation {name!r}; known: {', '.join(sorted(OPERATIONS))}", key=stage)
    if operation.argument == "required" and not given:
        raise ConfigurationError(f"operation {name!r} needs an argument: {{{name}: ...}}", key=stage)
    if operation.argument == "none" and given and arg is not None:
        raise ConfigurationError(f"operation {name!r} takes no argument", key=stage)
    try:
        fn = operation.build(arg)
    except (ValueError, TypeError, WintergrabError) as exc:
        raise ConfigurationError(f"operation {name!r}: {exc}", key=stage) from None
    return _CompiledOp(operation.name, fn, operation.elementwise, operation.strict, operation.accepts_none, spec)


_ON_ERROR = ("null", "keep", "drop", "raise")


class Transform(Stage):
    """Apply value operations to fields: ``Transform("price", ["strip", {"regex": "[0-9.,]+"}, "number"])``.

    Operations are names from :data:`OPERATIONS` (``"clean"``, ``"number"``,
    ``"date"``...), ``{name: argument}`` mappings (``{"unit": "kg"}``,
    ``{"map": {"yes": True}}``, ``{"expr": "value * 100"}``) or functions of
    the value. Most apply to each item of a list; ``first``, ``join``,
    ``unique``, ``default``... work on the whole value. Missing values skip
    the operations (except ``default``, ``map``, ``length`` and ``expr``).

    Args:
        fields: The field(s) to change; patterns (``"*_price"``) match existing fields.
        ops: The operations, applied in order.
        target: Write the result to this field instead (a single field only).
        on_error: When an operation fails (a parser found nothing, a function raised):
            ``"null"`` sets the value to ``None`` (for lists: drops the item), ``"keep"``
            leaves the field unchanged, ``"drop"`` drops the record, ``"raise"`` raises
            :class:`~wintergrab.errors.ValidationError`. Failures are counted as errors.
    """

    kind = "transform"

    def __init__(
        self,
        fields: str | Sequence[str],
        ops: Any,
        *,
        target: str | None = None,
        on_error: str = "null",
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        self.fields = _names(fields, "fields", self.name)
        specs = list(ops) if isinstance(ops, (list, tuple)) else [ops]
        if not specs:
            raise ConfigurationError("no operations given", key=self.name)
        self._ops = [_compile_op(spec, self.name) for spec in specs]
        if target is not None and (len(self.fields) != 1 or _is_pattern(self.fields[0])):
            raise ConfigurationError("target needs exactly one (non-pattern) field", key=self.name)
        self.target = target
        self.on_error = _choice(on_error, _ON_ERROR, "on_error", self.name)
        self._patterns = any(_is_pattern(f) for f in self.fields)

    def _targets(self, record: Mapping[str, Any]) -> list[str]:
        if not self._patterns:
            return self.fields
        out: list[str] = []
        for entry in self.fields:
            if _is_pattern(entry):
                out.extend(k for k in record if isinstance(k, str) and fnmatch.fnmatchcase(k, entry) and k not in out)
            elif entry not in out:
                out.append(entry)
        return out

    def _run(self, value: Any, record: Mapping[str, Any], field: str) -> Any:
        for op in self._ops:
            if value is None and not op.accepts_none:
                continue
            if op.elementwise and isinstance(value, (list, tuple)):
                items = []
                for item in value:
                    if item is None:
                        continue
                    try:
                        result = op.run(item, record)
                    except _OpFailed as failure:
                        if self.on_error != "null":
                            raise
                        self.error(f"{field}: {failure}")
                        continue
                    if result is not None:
                        items.append(result)
                value = items
            else:
                value = op.run(value, record)
        return value

    def apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None:
        for field in self._targets(record):
            present = field in record
            original = record.get(field)
            try:
                value = self._run(original, record, field)
            except _OpFailed as failure:
                self.error(f"{field}: {failure}")
                if self.on_error == "drop":
                    ctx.reason = f"{field}: {failure}"
                    return None
                if self.on_error == "raise":
                    issue = Issue(field, "transform", str(failure), value=original)
                    raise ValidationError(f"{self.name}: {field}: {failure}", issues=[issue]) from None
                value = None if self.on_error == "null" else original
            destination = self.target or field
            if value is None and not present and destination not in record:
                continue  # nothing to write for a field the record does not have
            record[destination] = value
        return record

    @classmethod
    def from_config(cls, options: Any, loader: ConfigLoader) -> Transform:
        opts = _options(options, {"field", "fields", "ops", "target", "on_error", "name"}, cls.kind, required=["ops"])
        if ("field" in opts) == ("fields" in opts):
            raise ConfigurationError("give either 'field' or 'fields'", key=cls.kind)
        fields = opts.pop("field", None) or opts.pop("fields")
        return cls(fields, opts.pop("ops"), **opts)

    def to_config(self) -> dict[str, Any]:
        ops = []
        for op in self._ops:
            if not isinstance(op.spec, (str, Mapping)):
                return super().to_config()  # a function: not expressible in a file
            ops.append(op.spec)
        options: dict[str, Any] = {"field": self.fields[0]} if len(self.fields) == 1 else {"fields": list(self.fields)}
        options["ops"] = ops
        if self.target:
            options["target"] = self.target
        if self.on_error != "null":
            options["on_error"] = self.on_error
        return self._named(options)


def _as_function(spec: Any, stage: str) -> tuple[Callable[[Mapping[str, Any]], Any], str | None]:
    """``(callable, expression source or None)`` from an expression string or a function of the record."""
    if isinstance(spec, str):
        try:
            return compile_expression(spec), spec
        except ExpressionError as exc:
            raise ConfigurationError(str(exc), key=stage) from None
    if isinstance(spec, Expression):
        return spec, spec.source
    if callable(spec):
        return spec, None
    raise ConfigurationError(f"expected an expression or a function, got {type(spec).__name__}", key=stage)


class Compute(Stage):
    """Set fields from expressions or functions of the record.

    ``Compute("discount", "round(1 - sale_price / price, 2)")`` or
    ``Compute({"domain": "domain(url)", "scraped_on": "today()"})``. Fields are
    computed in order, so later ones can use earlier ones. When a computation
    fails (a type mix-up), the field is set to ``None``; ``on_error="keep"``
    leaves it as it was, ``"drop"`` drops the record, ``"raise"`` raises.
    """

    kind = "compute"

    def __init__(
        self,
        fields: str | Mapping[str, Any],
        expression: Any = None,
        *,
        on_error: str = "null",
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        if isinstance(fields, str):
            if expression is None:
                raise ConfigurationError("Compute(field, expression) needs an expression", key=self.name)
            mapping: Mapping[str, Any] = {fields: expression}
        elif isinstance(fields, Mapping) and fields and expression is None:
            mapping = fields
        else:
            raise ConfigurationError(
                "expected Compute(field, expression) or Compute({field: expression})", key=self.name
            )
        self._fields: list[tuple[str, Callable[[Mapping[str, Any]], Any], str | None]] = []
        for field, spec in mapping.items():
            fn, source = _as_function(spec, self.name)
            self._fields.append((str(field), fn, source))
        self.on_error = _choice(on_error, _ON_ERROR, "on_error", self.name)

    @property
    def fields(self) -> list[str]:
        return [field for field, _, _ in self._fields]

    def apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None:
        for field, fn, source in self._fields:
            try:
                record[field] = fn(record)
            except Exception as exc:
                if self.on_error == "raise":
                    raise
                what = source or getattr(fn, "__name__", "function")
                self.error(f"{field} = {what}: {type(exc).__name__}: {exc}")
                if self.on_error == "drop":
                    ctx.reason = f"cannot compute {field}: {exc}"
                    return None
                if self.on_error == "null":
                    record[field] = None
        return record

    @classmethod
    def from_config(cls, options: Any, loader: ConfigLoader) -> Compute:
        if isinstance(options, Mapping) and isinstance(options.get("fields"), Mapping):
            opts = _options(options, {"fields", "on_error", "name"}, cls.kind)
            return cls(opts.pop("fields"), **opts)
        if not isinstance(options, Mapping) or not all(isinstance(v, str) for v in options.values()):
            raise ConfigurationError("expected {field: expression} (or {fields: {...}, on_error: ...})", key=cls.kind)
        return cls(dict(options))

    def to_config(self) -> dict[str, Any]:
        if any(source is None for _, _, source in self._fields):
            return super().to_config()
        fields = {field: source for field, _, source in self._fields}
        if self.on_error == "null" and self.name == self.kind:
            return {self.kind: fields}
        options: dict[str, Any] = {"fields": fields}
        if self.on_error != "null":
            options["on_error"] = self.on_error
        return self._named(options)


class Filter(Stage):
    """Keep the records a condition holds for: ``Filter("price > 0 and availability == 'in_stock'")``.

    ``keep=False`` drops those records instead. A condition that fails on a
    record (a type mix-up) drops it and counts an error (``on_error="keep"``
    keeps it; ``"raise"`` raises).
    """

    kind = "filter"

    def __init__(self, condition: Any, *, keep: bool = True, on_error: str = "drop", name: str | None = None) -> None:
        super().__init__(name=name)
        self._fn, self.source = _as_function(condition, self.name)
        self.keep = keep
        self.on_error = _choice(on_error, ("drop", "keep", "raise"), "on_error", self.name)

    def apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None:
        try:
            hit = bool(self._fn(record))
        except Exception as exc:
            if self.on_error == "raise":
                raise
            self.error(f"{self.source or 'condition'}: {type(exc).__name__}: {exc}")
            if self.on_error == "keep":
                return record
            ctx.reason = f"condition failed: {exc}"
            return None
        if hit == self.keep:
            return record
        what = self.source or getattr(self._fn, "__name__", "condition")
        ctx.reason = f"{'not ' if self.keep else ''}{what}"
        return None

    @classmethod
    def from_config(cls, options: Any, loader: ConfigLoader) -> Filter:
        if isinstance(options, str):
            return cls(options)
        opts = _options(options, {"condition", "keep", "on_error", "name"}, cls.kind, required=["condition"])
        return cls(opts.pop("condition"), **opts)

    def to_config(self) -> dict[str, Any]:
        if self.source is None:
            return super().to_config()
        if self.keep and self.on_error == "drop" and self.name == self.kind:
            return {self.kind: self.source}
        options: dict[str, Any] = {"condition": self.source}
        if not self.keep:
            options["keep"] = False
        if self.on_error != "drop":
            options["on_error"] = self.on_error
        return self._named(options)


def _schema_of(spec: Any, stage: str) -> Schema:
    if isinstance(spec, Schema):
        return spec
    if isinstance(spec, Mapping):
        return Schema.from_dict(spec)
    if isinstance(spec, (str, Path)):
        return load_schema(spec)
    raise ConfigurationError(f"expected a Schema, a schema mapping or a file, got {type(spec).__name__}", key=stage)


class Normalize(Stage):
    """Type and clean records with a :class:`~wintergrab.data.schema.Schema` (see :meth:`Schema.normalize`).

    ``notes=True`` keeps what the normalizers had to guess (``{"price": ["ambiguous-separator"]}``)
    under ``_notes``. The per-field results travel with the record to a later :class:`Validate`
    stage, which then also reports values that could not be read as their type.
    """

    kind = "normalize"

    def __init__(
        self,
        schema: Schema | Mapping[str, Any] | str | Path,
        *,
        base_url: str | None = None,
        country: str | None = None,
        currency: str | None = None,
        dayfirst: bool | None = None,
        decimal: str | None = None,
        notes: bool = False,
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        self.schema = _schema_of(schema, self.name)
        self.schema_ref: Any = schema if isinstance(schema, str) else None
        self.context = NormalizeContext(
            base_url=base_url, country=country, currency=currency, dayfirst=dayfirst, decimal=decimal
        )
        self.notes = notes

    def apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None:
        out, results = self.schema.normalize(record, context=self.context)
        ctx.schema, ctx.results = self.schema, results
        unreadable = sum(1 for result in results.values() if not result.ok)
        if unreadable:
            self.stats["unreadable"] += unreadable
        if self.notes:
            notes = {name: result.notes for name, result in results.items() if result.notes}
            if notes:
                out["_notes"] = notes
        return out

    def details(self) -> str:
        count = self.stats["unreadable"]
        return f"{count:,} value(s) unreadable" if count else ""

    @classmethod
    def from_config(cls, options: Any, loader: ConfigLoader) -> Normalize:
        if isinstance(options, str):
            options = {"schema": options}
        allowed = {"schema", "base_url", "country", "currency", "dayfirst", "decimal", "notes", "name"}
        opts = _options(options, allowed, cls.kind, required=["schema"])
        schema, ref = loader.schema(opts.pop("schema"))
        stage = cls(schema, **opts)
        stage.schema_ref = ref
        return stage

    def to_config(self) -> dict[str, Any]:
        options: dict[str, Any] = {"schema": self.schema_ref if self.schema_ref is not None else self.schema.to_dict()}
        for key in ("base_url", "country", "currency", "dayfirst", "decimal"):
            value = getattr(self.context, key)
            if value is not None:
                options[key] = value
        if self.notes:
            options["notes"] = True
        return self._named(options)


class Validate(Stage):
    """Check records against a schema and rules; drop, flag or keep the invalid ones.

    Args:
        schema: The schema to check against. Default: the one the record was
            last normalized with (by a :class:`Normalize` stage earlier in the pipeline).
        rules: Extra :class:`~wintergrab.data.validate.Rule` s (or their dict form).
        on_error: For records with errors: ``"drop"`` (default), ``"flag"`` (keep
            them, with their issues under ``issues_field``), ``"keep"`` (only count
            them) or ``"raise"`` (:class:`~wintergrab.errors.ValidationError`).
        on_warning: For records with warnings only: ``"keep"`` (default), ``"flag"`` or ``"drop"``.
        issues_field: Where flagged records carry their issues.
        rejects: A JSON Lines file that receives every dropped record with its issues.
    """

    kind = "validate"

    def __init__(
        self,
        schema: Schema | Mapping[str, Any] | str | Path | None = None,
        *,
        rules: Iterable[Rule | Mapping[str, Any]] = (),
        on_error: str = "drop",
        on_warning: str = "keep",
        issues_field: str = "_issues",
        rejects: str | Path | None = None,
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        self.schema = _schema_of(schema, self.name) if schema is not None else None
        self.schema_ref: Any = schema if isinstance(schema, str) else None
        self.rules = [rule if isinstance(rule, Rule) else Rule.from_dict(rule) for rule in rules]
        self.on_error = _choice(on_error, ("drop", "flag", "keep", "raise"), "on_error", self.name)
        self.on_warning = _choice(on_warning, ("keep", "flag", "drop"), "on_warning", self.name)
        self.issues_field = issues_field
        self.rejects = Path(rejects) if rejects is not None else None
        self.issue_counts: Counter[tuple[str | None, str, str]] = Counter()  # (field, code, severity)
        self._rejects_file: IO[str] | None = None

    def apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None:
        schema = self.schema if self.schema is not None else ctx.schema
        results = ctx.results if schema is not None and schema is ctx.schema else None
        issues = validate_record(record, schema, results, rules=self.rules)
        errors = [issue for issue in issues if issue.severity == "error"]
        self.stats["invalid" if errors else "valid"] += 1
        if not issues:
            return record
        for issue in issues:
            self.issue_counts[(issue.field, issue.code, issue.severity)] += 1
        if errors:
            policy = self.on_error
        elif any(issue.severity == "warning" for issue in issues):
            policy = self.on_warning
        else:
            policy = "keep"
        if policy == "keep":
            return record
        if policy == "flag":
            record[self.issues_field] = [issue.to_dict() for issue in issues]
            self.stats["flagged"] += 1
            return record
        summary = "; ".join(f"{i.field or 'record'}: {i.code}" for i in (errors or issues)[:3])
        if len(errors or issues) > 3:
            summary += f" (+{len(errors or issues) - 3} more)"
        if policy == "raise":
            raise ValidationError(f"{self.name}: invalid record: {summary}", issues=issues)
        ctx.reason = f"invalid: {summary}"
        self._reject(record, issues)
        return None

    def _reject(self, record: Mapping[str, Any], issues: list[Issue]) -> None:
        if self.rejects is None:
            return
        try:
            if self._rejects_file is None:
                self.rejects.parent.mkdir(parents=True, exist_ok=True)
                self._rejects_file = self.rejects.open("a", encoding="utf-8")
            line = {"stage": self.name, "issues": [i.to_dict() for i in issues], "record": record}
            self._rejects_file.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
            self._rejects_file.flush()
        except OSError as exc:
            self.error(f"cannot write rejects to {self.rejects}: {exc}")

    def close(self) -> None:
        if self._rejects_file is not None:
            self._rejects_file.close()
            self._rejects_file = None

    def summary(self, limit: int = 10) -> list[tuple[str | None, str, str, int]]:
        """The most frequent issues: ``[(field, code, severity, count), ...]``."""
        return [(field, code, severity, n) for (field, code, severity), n in self.issue_counts.most_common(limit)]

    def details(self) -> str:
        return "; ".join(f"{field or 'record'}: {code} x{count:,}" for field, code, _, count in self.summary(3))

    @classmethod
    def from_config(cls, options: Any, loader: ConfigLoader) -> Validate:
        allowed = {"schema", "rules", "on_error", "on_warning", "issues_field", "rejects", "name"}
        opts = _options(options, allowed, cls.kind)
        ref = None
        if "schema" in opts:
            opts["schema"], ref = loader.schema(opts["schema"])
        rules = opts.pop("rules", None) or []
        if not isinstance(rules, list):
            raise ConfigurationError("rules must be a list", key=cls.kind)
        try:
            parsed = [Rule.from_dict(rule) for rule in rules]
        except (WintergrabError, TypeError) as exc:
            raise ConfigurationError(str(exc), key=f"{cls.kind}.rules") from None
        if "rejects" in opts:
            opts["rejects"] = loader.path(opts["rejects"])
        stage = cls(rules=parsed, **opts)
        stage.schema_ref = ref
        return stage

    def to_config(self) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if self.schema is not None:
            options["schema"] = self.schema_ref if self.schema_ref is not None else self.schema.to_dict()
        if self.rules:
            options["rules"] = [rule.to_dict() for rule in self.rules]
        if self.on_error != "drop":
            options["on_error"] = self.on_error
        if self.on_warning != "keep":
            options["on_warning"] = self.on_warning
        if self.issues_field != "_issues":
            options["issues_field"] = self.issues_field
        if self.rejects is not None:
            options["rejects"] = str(self.rejects)
        return self._named(options)


class Deduplicate(Stage):
    """Drop (or mark) duplicate records; see :class:`~wintergrab.data.dedupe.Deduplicator`.

    ``Deduplicate(key="url")`` drops records whose URL was seen; without a key,
    records with the same content; ``near=True`` also catches lightly edited
    copies (``similarity``: the share of shared word 3-grams, default 0.8).
    """

    kind = "dedupe"

    def __init__(
        self,
        key: str | Sequence[str] | None = None,
        *,
        fields: Sequence[str] | None = None,
        near: bool = False,
        text_fields: Sequence[str] | None = None,
        similarity: float = 0.8,
        mark: bool = False,
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        if not 0 < similarity < 1:
            raise ConfigurationError("similarity must be between 0 and 1", key=self.name)
        self.deduplicator = Deduplicator(
            key, fields=fields, near=near, text_fields=text_fields, similarity=similarity, mark=mark
        )
        self._options = {
            "fields": fields,
            "near": near,
            "text_fields": text_fields,
            "similarity": similarity,
            "mark": mark,
        }

    def apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None:
        found = self.deduplicator.check(record)
        if found is None:
            return record
        kind, first = found
        self.deduplicator.duplicates[kind] += 1
        self.stats[f"duplicates/{kind}"] += 1
        if self.deduplicator.mark:
            record["_duplicate_of"] = first
            record["_duplicate_kind"] = kind
            return record
        ctx.reason = f"duplicate ({kind}) of record #{first}"
        return None

    def details(self) -> str:
        found = {k: v for k, v in self.deduplicator.duplicates.items() if v}
        return ", ".join(f"{kind} {count:,}" for kind, count in found.items())

    @classmethod
    def from_config(cls, options: Any, loader: ConfigLoader) -> Deduplicate:
        if isinstance(options, (str, list, tuple)):
            return cls(options)
        allowed = {"key", "fields", "near", "text_fields", "similarity", "mark", "name"}
        return cls(**_options(options, allowed, cls.kind))

    def to_config(self) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if self.deduplicator.key:
            options["key"] = list(self.deduplicator.key)
        defaults = {"fields": None, "near": False, "text_fields": None, "similarity": 0.8, "mark": False}
        for option, default in defaults.items():
            value = self._options[option]
            if value != default:
                options[option] = list(value) if isinstance(value, (list, tuple)) else value
        return self._named(options)


def _match_key(value: Any) -> str | None:
    """Keys that should match compare equal: case and spacing are ignored, and ``42 == "42" == 42.0``."""
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, bool):
        return str(value).casefold()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        return str(int(value))
    text = " ".join(str(value).split()).casefold()
    return text or None


def _read_table(source: Path) -> list[dict[str, Any]] | dict[str, Any]:
    suffix = source.suffix.lower()
    try:
        if suffix == ".csv":
            with source.open(newline="", encoding="utf-8-sig") as fh:
                return [dict(row) for row in csv.DictReader(fh)]
        if suffix in (".jsonl", ".ndjson"):
            with source.open(encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
    except (OSError, ValueError, csv.Error) as exc:
        raise ConfigurationError(f"cannot read the table {source}: {exc}", key="lookup") from exc
    data = read_structured(source)
    if not isinstance(data, (list, dict)):
        raise ConfigurationError(f"the table {source} must hold a list of rows or a mapping", key="lookup")
    return data


class Lookup(Stage):
    """Add fields from a reference table, matched on a field.

    ``Lookup("sku", "catalog.csv", fields=["brand", "category"])`` copies
    ``brand`` and ``category`` from the catalog row whose ``sku`` matches the
    record's. Matching ignores case and surrounding whitespace, and ``"42"``
    matches ``42``.

    Args:
        on: The record field to match.
        table: ``{key: row}``, a list of rows, or a ``.csv``/``.json``/``.jsonl``/``.yaml`` file.
        key: The table column holding the key (default: ``on``).
        fields: Table columns to copy (default: all but the key).
        prefix: Prepended to the copied fields' names.
        required: Drop records without a match (default: keep them as they are).
        overwrite: Replace values the record already has (default: only fill empty ones).
    """

    kind = "lookup"

    def __init__(
        self,
        on: str,
        table: Mapping[Any, Any] | Sequence[Mapping[str, Any]] | str | Path,
        *,
        key: str | None = None,
        fields: Sequence[str] | None = None,
        prefix: str = "",
        required: bool = False,
        overwrite: bool = False,
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        if not isinstance(on, str) or not on:
            raise ConfigurationError("'on' must be a field name", key=self.name)
        self.on = on
        self.key = key or on
        self.fields = list(fields) if fields is not None else None
        self.prefix = prefix
        self.required = required
        self.overwrite = overwrite
        self.table_ref: Any = table if isinstance(table, (str, Path)) else None
        rows = _read_table(Path(table)) if isinstance(table, (str, Path)) else table
        self._index = self._build(rows)

    def _build(self, rows: Any) -> dict[str, dict[str, Any]]:
        index: dict[str, dict[str, Any]] = {}
        items: Iterable[tuple[Any, Any]]
        if isinstance(rows, Mapping):
            items = rows.items()
        elif isinstance(rows, Sequence):
            items = ((row.get(self.key) if isinstance(row, Mapping) else None, row) for row in rows)
        else:
            raise ConfigurationError("the table must be a mapping or a list of rows", key=self.name)
        for raw_key, row in items:
            if not isinstance(row, Mapping):
                if self.fields is None or len(self.fields) != 1:
                    raise ConfigurationError(
                        "a table of plain values needs exactly one entry in 'fields'", key=self.name
                    )
                row = {self.fields[0]: row}
            match = _match_key(raw_key)
            if match is not None and match not in index:  # the first row with a key wins
                index[match] = {k: v for k, v in row.items() if k != self.key}
        return index

    def _find(self, value: Any) -> dict[str, Any] | None:
        for candidate in value if isinstance(value, (list, tuple)) else (value,):
            row = self._index.get(_match_key(candidate) or "")
            if row is not None:
                return row
        return None

    def apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None:
        row = self._find(record.get(self.on))
        if row is None:
            self.stats["unmatched"] += 1
            if self.required:
                ctx.reason = f"no {self.key} {record.get(self.on)!r} in the table"
                return None
            return record
        self.stats["matched"] += 1
        names = self.fields if self.fields is not None else list(row)
        for column in names:
            target = self.prefix + column
            if column in row and (self.overwrite or _empty(record.get(target))):
                record[target] = row[column]
        return record

    def details(self) -> str:
        return f"{self.stats['matched']:,} matched, {self.stats['unmatched']:,} unmatched"

    @classmethod
    def from_config(cls, options: Any, loader: ConfigLoader) -> Lookup:
        allowed = {"on", "table", "key", "fields", "prefix", "required", "overwrite", "name"}
        opts = _options(options, allowed, cls.kind, required=["on", "table"])
        table = opts.pop("table")
        ref = table if isinstance(table, str) else None
        if isinstance(table, str):
            table = loader.path(table)
        stage = cls(opts.pop("on"), table, **opts)
        stage.table_ref = ref
        return stage

    def to_config(self) -> dict[str, Any]:
        if self.table_ref is None:
            return super().to_config()  # an in-memory table
        options: dict[str, Any] = {"on": self.on, "table": str(self.table_ref)}
        if self.key != self.on:
            options["key"] = self.key
        if self.fields is not None:
            options["fields"] = list(self.fields)
        if self.prefix:
            options["prefix"] = self.prefix
        if self.required:
            options["required"] = True
        if self.overwrite:
            options["overwrite"] = True
        return self._named(options)


class ConvertCurrency(Stage):
    """Convert money amounts to one currency, with exchange rates you supply (none are fetched).

    ``ConvertCurrency(["price", "sale_price"], to="EUR", rates={"USD": 0.92, "GBP": 1.17})``:
    each rate is the value of one unit of that currency in ``to``. The record's
    currency field is updated, and amounts are rounded to the target currency's
    minor units. ``{"amount", "currency"}`` values are converted too. Records in
    a currency without a rate are counted as errors and, by default, left as
    they are (``on_error``: ``"keep"``, ``"null"``, ``"drop"``, ``"raise"``).
    """

    kind = "convert_currency"

    def __init__(
        self,
        fields: str | Sequence[str],
        *,
        to: str,
        rates: Mapping[str, float | str | Decimal],
        currency_field: str = "currency",
        on_error: str = "keep",
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        self.fields = _names(fields, "fields", self.name)
        self.to = str(to).upper()
        minor = currency_minor_units(self.to)
        if minor is None:
            raise ConfigurationError(f"unknown currency {to!r}", key=self.name)
        self._quantum = Decimal(1).scaleb(-minor)
        try:
            self.rates = {str(code).upper(): Decimal(str(rate)) for code, rate in rates.items()}
        except (InvalidOperation, AttributeError) as exc:
            raise ConfigurationError(f"invalid rates: {exc}", key=self.name) from None
        for code, rate in self.rates.items():
            if currency_minor_units(code) is None or not rate > 0:
                raise ConfigurationError(f"invalid rate for {code!r}: {rate}", key=self.name)
        self.rates[self.to] = Decimal(1)
        self.currency_field = currency_field
        self.on_error = _choice(on_error, _ON_ERROR, "on_error", self.name)

    def _convert(self, amount: Any, code: Any) -> Any:
        rate = self.rates.get(str(code).upper()) if code else None
        if rate is None:
            raise LookupError(f"no rate for {code!r}" if code else "the record names no currency")
        return as_int_or_float((Decimal(str(amount)) * rate).quantize(self._quantum, rounding=ROUND_HALF_EVEN))

    def _same(self, code: Any) -> bool:
        return isinstance(code, str) and code.upper() == self.to

    def apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None:
        changes: dict[str, Any] = {}
        record_currency = record.get(self.currency_field)
        try:
            for field in self.fields:
                value = record.get(field)
                if isinstance(value, Mapping) and value.get("amount") is not None:
                    code = value.get("currency") or record_currency
                    if not self._same(code):
                        changes[field] = {**value, "amount": self._convert(value["amount"], code), "currency": self.to}
                elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                    if not self._same(record_currency):
                        changes[field] = self._convert(value, record_currency)
        except (LookupError, InvalidOperation) as exc:
            self.error(str(exc))
            if self.on_error == "raise":
                issue = Issue(self.currency_field, "currency", str(exc), value=record_currency)
                raise ValidationError(f"{self.name}: {exc}", issues=[issue]) from None
            if self.on_error == "drop":
                ctx.reason = str(exc)
                return None
            if self.on_error == "null":
                for field in self.fields:
                    if field in record:
                        record[field] = None
            return record
        if changes:
            record.update(changes)
            if any(not isinstance(v, Mapping) for v in changes.values()):
                record[self.currency_field] = self.to
            self.stats["converted"] += 1
        return record

    @classmethod
    def from_config(cls, options: Any, loader: ConfigLoader) -> ConvertCurrency:
        allowed = {"fields", "to", "rates", "currency_field", "on_error", "name"}
        opts = _options(options, allowed, cls.kind, required=["fields", "to", "rates"])
        if not isinstance(opts["rates"], Mapping):
            raise ConfigurationError("rates must be a {currency: rate} mapping", key=cls.kind)
        return cls(opts.pop("fields"), **opts)

    def to_config(self) -> dict[str, Any]:
        rates = {code: str(rate) for code, rate in self.rates.items() if code != self.to}
        options: dict[str, Any] = {"fields": list(self.fields), "to": self.to, "rates": rates}
        if self.currency_field != "currency":
            options["currency_field"] = self.currency_field
        if self.on_error != "keep":
            options["on_error"] = self.on_error
        return self._named(options)


def _is_async_callable(fn: Any) -> bool:
    """Whether calling ``fn`` returns a coroutine (an async function, or an object with ``async def __call__``)."""
    if inspect.iscoroutinefunction(fn):
        return True
    call = getattr(type(fn), "__call__", None)  # noqa: B004 - looking for an async __call__, not testing callability
    return inspect.iscoroutinefunction(call)


class Enrich(Stage):
    """Merge in the fields a function returns: ``Enrich(lambda r: {"brand": brand_of(r["name"])})``.

    The function gets the record and returns a mapping of fields to set (or
    ``None``). It may be ``async`` (to call a web service or an AI provider
    adapter): the pipeline then runs asynchronously, which is automatic in a
    crawl; for lists use :meth:`Pipeline.arun`. ``overwrite=False`` only fills
    empty fields. When the function raises, the error is counted and the record
    kept unchanged (``on_error``: ``"keep"``, ``"drop"`` or ``"raise"``).
    """

    kind = "enrich"

    def __init__(
        self,
        fn: Callable[[dict[str, Any]], Any],
        *,
        overwrite: bool = True,
        on_error: str = "keep",
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        if not callable(fn):
            raise ConfigurationError("Enrich needs a function", key=self.name)
        self.fn = fn
        self.function_ref: str | None = None
        self.overwrite = overwrite
        self.on_error = _choice(on_error, ("keep", "drop", "raise"), "on_error", self.name)
        self._async = _is_async_callable(fn)

    @property
    def is_async(self) -> bool:
        return self._async

    def _merge(self, record: dict[str, Any], extra: Any) -> dict[str, Any]:
        if extra is None:
            return record
        if not isinstance(extra, Mapping):
            raise TypeError(f"the function returned {type(extra).__name__}, expected a mapping or None")
        for key, value in extra.items():
            if self.overwrite or _empty(record.get(key)):
                record[key] = value
        return record

    def _failed(self, record: dict[str, Any], ctx: RecordContext, exc: Exception) -> dict[str, Any] | None:
        if self.on_error == "raise":
            raise exc
        self.error(f"{type(exc).__name__}: {exc}")
        if self.on_error == "drop":
            ctx.reason = f"enrichment failed: {exc}"
            return None
        return record

    def apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None:
        try:
            extra = self.fn(record)
            if inspect.isawaitable(extra):
                close = getattr(extra, "close", None)
                if close is not None:
                    close()
                raise ConfigurationError(f"{self.name} returned an awaitable: run the pipeline with arun()/aprocess()")
            return self._merge(record, extra)
        except ConfigurationError:
            raise
        except Exception as exc:
            return self._failed(record, ctx, exc)

    async def aapply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None:
        try:
            return self._merge(record, await maybe_await(self.fn(record)))
        except Exception as exc:
            return self._failed(record, ctx, exc)

    @classmethod
    def from_config(cls, options: Any, loader: ConfigLoader) -> Enrich:
        opts = _options(options, {"function", "overwrite", "on_error", "name"}, cls.kind, required=["function"])
        ref = str(opts.pop("function"))
        stage = cls(loader.function(ref), **opts)
        stage.function_ref = ref
        return stage

    def to_config(self) -> dict[str, Any]:
        if self.function_ref is None:
            return super().to_config()
        options: dict[str, Any] = {"function": self.function_ref}
        if not self.overwrite:
            options["overwrite"] = False
        if self.on_error != "keep":
            options["on_error"] = self.on_error
        return self._named(options)


class Analyze(Stage):
    """Add what a text field says of itself: its language, keywords and size, with no model
    (:mod:`wintergrab.intel.content`).

    ``Analyze("body")`` adds ``language``, ``keywords``, ``words`` and ``reading_minutes``. ``add`` picks
    among those and ``language_confidence``, ``sentences``, ``characters`` and ``script``; ``prefix`` names
    them (``body_language``...). A record without text in the field is left as it is. A text written
    without spaces between words (Chinese, Japanese, Thai...) gets ``None`` words and reading time.
    """

    kind = "analyze"
    FEATURES = ("language", "language_confidence", "keywords", "words", "sentences", "characters",
                "reading_minutes", "script")  # fmt: skip

    def __init__(
        self,
        field: str,
        *,
        add: Sequence[str] = ("language", "keywords", "words", "reading_minutes"),
        prefix: str = "",
        keywords: int = 8,
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        unknown = [a for a in add if a not in self.FEATURES]
        if unknown:
            raise ConfigurationError(
                f"unknown feature(s) {', '.join(unknown)}; known: {', '.join(self.FEATURES)}", key=self.name
            )
        self.field, self.add, self.prefix, self.keywords = field, tuple(add), prefix, keywords

    def apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None:
        from ..intel.content import analyze_text

        text = _text_of(get_path(record, self.field))
        if not text:
            return record
        analysis = analyze_text(text, keywords=self.keywords)
        for feature in self.add:
            record[self.prefix + feature] = getattr(analysis, feature)
        return record

    @classmethod
    def from_config(cls, options: Any, loader: ConfigLoader) -> Analyze:
        opts = _options(options, {"field", "add", "prefix", "keywords", "name"}, cls.kind, required=["field"])
        if "add" in opts:
            opts["add"] = _names(opts["add"], "add", cls.kind)
        return cls(str(opts.pop("field")), **opts)

    def to_config(self) -> dict[str, Any]:
        options: dict[str, Any] = {"field": self.field, "add": list(self.add)}
        if self.prefix:
            options["prefix"] = self.prefix
        if self.keywords != 8:
            options["keywords"] = self.keywords
        return self._named(options)


class Classify(Stage):
    """Ask a model for a text field's topic, category (one of ``categories``), sentiment and entities,
    and add them, checked (:func:`wintergrab.intel.content.classify_text`).

    ``model`` is a model provider, or ``"provider:name"`` (keys from the environment). When the model
    fails, the error is counted and the record kept.
    """

    kind = "classify"
    FEATURES = ("topic", "category", "sentiment", "entities")

    def __init__(
        self,
        field: str,
        model: Any,
        *,
        categories: Sequence[str] | None = None,
        add: Sequence[str] | None = None,
        prefix: str = "",
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        self.model_spec = model if isinstance(model, str) else None
        if isinstance(model, str):
            from ..models import load_model

            model = load_model(model)
        self.model = model
        self.field, self.prefix = field, prefix
        self.categories = list(categories) if categories else None
        chosen = add if add is not None else [f for f in self.FEATURES if f != "category" or self.categories]
        unknown = [a for a in chosen if a not in self.FEATURES]
        if unknown:
            raise ConfigurationError(
                f"unknown feature(s) {', '.join(unknown)}; known: {', '.join(self.FEATURES)}", key=self.name
            )
        self.add = tuple(chosen)

    def apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None:
        from ..intel.content import classify_text

        text = _text_of(get_path(record, self.field))
        if not text:
            return record
        try:
            labels = classify_text(text, self.model, categories=self.categories, entities="entities" in self.add)
        except Exception as exc:  # the model is down, slow, or says nonsense: the record stays
            self.error(f"{type(exc).__name__}: {exc}")
            return record
        for feature in self.add:
            record[self.prefix + feature] = getattr(labels, feature)
        return record

    @classmethod
    def from_config(cls, options: Any, loader: ConfigLoader) -> Classify:
        opts = _options(options, {"field", "model", "categories", "add", "prefix", "name"}, cls.kind,
                        required=["field", "model"])  # fmt: skip
        if "add" in opts:
            opts["add"] = _names(opts["add"], "add", cls.kind)
        if "categories" in opts:
            opts["categories"] = _names(opts["categories"], "categories", cls.kind)
        return cls(str(opts.pop("field")), str(opts.pop("model")), **opts)

    def to_config(self) -> dict[str, Any]:
        if self.model_spec is None:
            return super().to_config()
        options: dict[str, Any] = {"field": self.field, "model": self.model_spec, "add": list(self.add)}
        if self.categories:
            options["categories"] = self.categories
        if self.prefix:
            options["prefix"] = self.prefix
        return self._named(options)


def _text_of(value: Any) -> str:
    """A field's text: a string, or the strings of a list, joined."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return " ".join(v for v in value if isinstance(v, str)).strip()
    return ""


class Locate(Stage):
    """Add where a record is: read from its address, location, city, region, postal code, country,
    coordinates and map link fields, and normalized (:func:`wintergrab.data.places.place_of`).

    ``Locate()`` adds ``country`` (ISO 3166-1), ``region`` (ISO 3166-2 where WINTERGRAB knows the country's
    regions, else as written), ``city``, ``postal_code`` and ``coordinates`` (``[lat, lon]``, when the
    record states them or links to a map showing them); ``add`` picks among those, ``street`` and
    ``remote``; ``prefix`` names them (``place_country``...). ``country`` is the country the records'
    addresses are in when they do not say it, and settles codes naming several places (``"CA"``:
    California or Canada); ``fields`` maps parts (``city``, ``address``...) to the records' own field
    names. A value a record has is not replaced by nothing. Records whose place names a code that nothing
    settled are counted as ``unsure``.
    """

    kind = "locate"
    FEATURES = PLACE_PARTS

    def __init__(
        self,
        *,
        add: Sequence[str] = DEFAULT_PARTS,
        prefix: str = "",
        country: str | None = None,
        fields: Mapping[str, str | Sequence[str]] | None = None,
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        unknown = [a for a in add if a not in self.FEATURES]
        if unknown:
            raise ConfigurationError(
                f"unknown part(s) {', '.join(unknown)}; known: {', '.join(self.FEATURES)}", key=self.name
            )
        strange = [f for f in fields or {} if f not in PLACE_FIELDS]
        if strange:
            raise ConfigurationError(
                f"fields: unknown part(s) {', '.join(strange)}; known: {', '.join(PLACE_FIELDS)}", key=self.name
            )
        if country is not None and normalize_country(country) is None:
            raise ConfigurationError(f"country: {country!r} is not a country", key=self.name)
        self.add, self.prefix, self.country = tuple(add), prefix, country
        self.fields = dict(fields or {})

    def apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None:
        from .places import place_of

        place = place_of(record, country=self.country, fields=self.fields)
        values = place.to_dict()
        for part in self.add:
            key = self.prefix + part
            if values[part] is None and not _empty(record.get(key)):
                continue  # not replaced by nothing
            record[key] = values[part]
        if place.unsure:
            self.stats["unsure"] += 1
        elif not place.known:
            self.stats["unknown"] += 1
        return record

    def details(self) -> str:
        notes = [f"{self.stats[k]} {k}" for k in ("unknown", "unsure") if self.stats[k]]
        return ", ".join(notes)

    @classmethod
    def from_config(cls, options: Any, loader: ConfigLoader) -> Locate:
        opts = _options(options, {"add", "prefix", "country", "fields", "name"}, cls.kind)
        if "add" in opts:
            opts["add"] = _names(opts["add"], "add", cls.kind)
        if "fields" in opts and not isinstance(opts["fields"], Mapping):
            raise ConfigurationError("fields must map parts to field names", key=cls.kind)
        return cls(**opts)

    def to_config(self) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if self.add != DEFAULT_PARTS:
            options["add"] = list(self.add)
        for key, default in (("prefix", ""), ("country", None)):
            if getattr(self, key) != default:
                options[key] = getattr(self, key)
        if self.fields:
            options["fields"] = dict(self.fields)
        return self._named(options)


class QualityCheck(Stage):
    """Measure dataset quality as records pass; never drops anything.

    Wraps a :class:`~wintergrab.data.quality.QualityMonitor` (``monitor``, or
    one built from ``schema`` and ``options``). In a crawl it compares the run
    with the previous one and emits ``quality_degraded`` events; after
    :meth:`Pipeline.run`, call :meth:`report`.
    """

    kind = "quality"

    def __init__(
        self,
        schema: Schema | Mapping[str, Any] | str | Path | None = None,
        *,
        monitor: QualityMonitor | None = None,
        name: str | None = None,
        **options: Any,
    ) -> None:
        super().__init__(name=name)
        self.schema_ref: Any = schema if isinstance(schema, str) else None
        self.options = dict(options)
        if monitor is None:
            resolved = _schema_of(schema, self.name) if schema is not None else None
            if "dataset" in options:
                options["name"] = options.pop("dataset")
            try:
                monitor = QualityMonitor(resolved, **options)
            except TypeError as exc:
                raise ConfigurationError(str(exc), key=self.name) from None
        self.monitor = monitor

    def apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None:
        self.monitor.observe(record)
        return record

    def report(self) -> QualityReport:
        return self.monitor.report()

    def open_spider(self, spider: Any) -> Any:
        return self.monitor.open_spider(spider)

    def close_spider(self, spider: Any) -> Any:
        return self.monitor.close_spider(spider)

    def details(self) -> str:
        score = self.monitor.report().score if self.monitor.records else None
        return f"quality score {score:.3f}" if score is not None else ""

    @classmethod
    def from_config(cls, options: Any, loader: ConfigLoader) -> QualityCheck:
        allowed = {"schema", "dataset", "key", "baseline", "save_to", "time_field", "max_age", "outlier_z", "name"}
        opts = _options(options, allowed, cls.kind)
        ref = None
        if "schema" in opts:
            opts["schema"], ref = loader.schema(opts["schema"])
        for option in ("baseline", "save_to"):
            if isinstance(opts.get(option), str) and opts[option] != "auto":
                opts[option] = loader.path(opts[option])
        stage = cls(**opts)
        stage.schema_ref = ref
        return stage

    def to_config(self) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if self.monitor.schema is not None:
            options["schema"] = self.schema_ref if self.schema_ref is not None else self.monitor.schema.to_dict()
        for key, value in self.options.items():
            options[key] = str(value) if isinstance(value, Path) else value
        return self._named(options)


class _Hook(Stage):
    """An item pipeline object (with ``process_item``) as a stage."""

    kind = "pipeline"

    def __init__(self, target: Any) -> None:
        super().__init__(name=type(target).__name__)
        self.target = target
        self._async = _is_async_callable(target.process_item)

    @property
    def is_async(self) -> bool:
        return self._async

    def _result(self, result: Any, ctx: RecordContext) -> dict[str, Any] | None:
        if result is None:
            ctx.reason = ctx.reason or "dropped"
            return None
        record = _as_record(result)
        if record is None:
            raise TypeError(f"{self.name}.process_item returned {type(result).__name__}, expected a record")
        return record

    def apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None:
        from ..spider.middleware import DropItem

        try:
            result = self.target.process_item(record, ctx.spider)
        except DropItem as exc:
            ctx.reason = str(exc) or "dropped"
            return None
        if inspect.isawaitable(result):
            close = getattr(result, "close", None)
            if close is not None:
                close()
            raise ConfigurationError(f"{self.name}.process_item is asynchronous: use arun()/aprocess()")
        return self._result(result, ctx)

    async def aapply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None:
        from ..spider.middleware import DropItem

        try:
            result = await maybe_await(self.target.process_item(record, ctx.spider))
        except DropItem as exc:
            ctx.reason = str(exc) or "dropped"
            return None
        return self._result(result, ctx)

    def open_spider(self, spider: Any) -> Any:
        opener = getattr(self.target, "open_spider", None)
        return opener(spider) if opener is not None else None

    def close_spider(self, spider: Any) -> Any:
        closer = getattr(self.target, "close_spider", None)
        return closer(spider) if closer is not None else None


class _Function(Stage):
    """A ``record -> record | None`` function as a stage."""

    kind = "function"

    def __init__(self, fn: Callable[[dict[str, Any]], Any], *, name: str | None = None) -> None:
        super().__init__(name=name or getattr(fn, "__name__", None) or self.kind)
        self.fn = fn
        self.function_ref: str | None = None
        self._named_explicitly = name is not None
        self._async = _is_async_callable(fn)

    @property
    def is_async(self) -> bool:
        return self._async

    def _result(self, result: Any, ctx: RecordContext) -> dict[str, Any] | None:
        if result is None:
            ctx.reason = ctx.reason or "dropped"
            return None
        record = _as_record(result)
        if record is None:
            raise TypeError(f"{self.name} returned {type(result).__name__}, expected a record or None")
        return record

    def apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None:
        result = self.fn(record)
        if inspect.isawaitable(result):
            close = getattr(result, "close", None)
            if close is not None:
                close()
            raise ConfigurationError(f"{self.name} is asynchronous: use arun()/aprocess()")
        return self._result(result, ctx)

    async def aapply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None:
        return self._result(await maybe_await(self.fn(record)), ctx)

    @classmethod
    def from_config(cls, options: Any, loader: ConfigLoader) -> _Function:
        if isinstance(options, str):
            options = {"function": options}
        opts = _options(options, {"function", "name"}, cls.kind, required=["function"])
        ref = str(opts["function"])
        stage = cls(loader.function(ref), name=opts.get("name"))
        stage.function_ref = ref
        return stage

    def to_config(self) -> dict[str, Any]:
        if self.function_ref is None:
            return super().to_config()
        if not self._named_explicitly:
            return {self.kind: self.function_ref}
        return {self.kind: {"function": self.function_ref, "name": self.name}}


STAGES: dict[str, type[Stage]] = {
    cls.kind: cls
    for cls in (
        Rename,
        Select,
        Exclude,
        Transform,
        Compute,
        Filter,
        Normalize,
        Validate,
        Deduplicate,
        Lookup,
        ConvertCurrency,
        Enrich,
        Analyze,
        Classify,
        Locate,
        QualityCheck,
        _Function,
    )
}


def register_stage(stage: type[Stage]) -> None:
    """Add a stage class, used in pipeline files by its ``kind`` (``{geocode: {...}}``)."""
    if not (isinstance(stage, type) and issubclass(stage, Stage)) or not getattr(stage, "kind", ""):
        raise ConfigurationError(f"a stage is a Stage subclass with a kind, not {stage!r}")
    known = STAGES.get(stage.kind)
    if known is not None and known is not stage:
        raise ConfigurationError(f"there is already a stage {stage.kind!r} ({known.__qualname__})")
    STAGES[stage.kind] = stage


def _as_stage(obj: Any) -> Stage:
    if isinstance(obj, Stage):
        return obj
    if hasattr(obj, "process_item"):
        return _Hook(obj)
    if callable(obj):
        return _Function(obj)
    raise ConfigurationError(f"{obj!r} is not a pipeline stage (a Stage, an item pipeline or a function)")


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
class ConfigLoader:
    """Resolves what stage options refer to: schema files and names, tables, functions.

    Args:
        base_dir: Relative paths are resolved against it (a pipeline file's directory).
        schemas: Named schemas stages may refer to (``normalize: {schema: product}``).
        allow_imports: Whether ``"package.module:function"`` references may be imported.
            Importing runs the module's code, so allow it only for trusted configuration.
    """

    def __init__(
        self,
        base_dir: str | Path | None = None,
        *,
        schemas: Mapping[str, Schema] | None = None,
        allow_imports: bool = False,
    ) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else None
        self.schemas = dict(schemas or {})
        self.allow_imports = allow_imports
        self._loaded: dict[str, Schema] = {}

    def path(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() or self.base_dir is None else self.base_dir / path

    def schema(self, spec: Any) -> tuple[Schema, Any]:
        """``(schema, reference)``: files are loaded once, so stages naming the same file share it."""
        if isinstance(spec, Schema):
            return spec, None
        if isinstance(spec, Mapping):
            return Schema.from_dict(spec), None
        if isinstance(spec, str):
            if spec in self.schemas:
                return self.schemas[spec], spec
            path = self.path(spec)
            cache_key = str(path.resolve())
            if cache_key not in self._loaded:
                self._loaded[cache_key] = load_schema(path)
            return self._loaded[cache_key], spec
        raise ConfigurationError("a schema is a mapping, a file path or the name of a known schema", key="schema")

    def function(self, reference: str) -> Callable[..., Any]:
        """Import ``"package.module:attribute"`` (only with ``allow_imports``)."""
        if not self.allow_imports:
            raise ConfigurationError(
                f"this configuration refers to Python code ({reference!r}); load it with allow_imports=True "
                "if you trust it"
            )
        module_name, _, attribute = reference.partition(":")
        if not module_name or not attribute:
            raise ConfigurationError(f"expected 'package.module:function', got {reference!r}")
        try:
            target: Any = importlib.import_module(module_name)
            for part in attribute.split("."):
                target = getattr(target, part)
        except (ImportError, AttributeError) as exc:
            raise ConfigurationError(f"cannot import {reference!r}: {exc}") from exc
        if not callable(target):
            raise ConfigurationError(f"{reference!r} is not callable")
        return target  # type: ignore[no-any-return]


# --------------------------------------------------------------------------- #
# the pipeline
# --------------------------------------------------------------------------- #
PIPELINE_FORMAT = "wintergrab/pipeline/v1"


class Pipeline:
    """Records through a list of stages; see the module docs.

    Stages are :class:`Stage` s, item-pipeline objects (anything with
    ``process_item``, e.g. a :class:`~wintergrab.data.quality.QualityMonitor`)
    or plain ``record -> record | None`` functions.
    """

    def __init__(self, stages: Iterable[Any] = (), *, name: str = "pipeline") -> None:
        self.name = name
        self.stages: list[Stage] = []
        for stage in stages:
            self.add(stage)

    def add(self, stage: Any) -> Pipeline:
        """Append a stage (returns the pipeline, for chaining)."""
        resolved = _as_stage(stage)
        taken = {s.name for s in self.stages}
        if resolved.name in taken:
            if resolved.name != resolved.kind and not isinstance(resolved, (_Hook, _Function)):
                raise ConfigurationError(f"two stages are named {resolved.name!r}", key=self.name)
            number = 2
            while f"{resolved.name}#{number}" in taken:
                number += 1
            resolved.name = f"{resolved.name}#{number}"
        self.stages.append(resolved)
        return self

    @property
    def is_async(self) -> bool:
        """Whether a stage is asynchronous (then use :meth:`arun` / :meth:`aprocess`)."""
        return any(stage.is_async for stage in self.stages)

    def __len__(self) -> int:
        return len(self.stages)

    def __iter__(self) -> Iterator[Stage]:
        return iter(self.stages)

    def __getitem__(self, key: int | str) -> Stage:
        if isinstance(key, int):
            return self.stages[key]
        for stage in self.stages:
            if stage.name == key:
                return stage
        raise KeyError(key)

    # -- running ------------------------------------------------------------ #
    def _sync_only(self) -> None:
        if self.is_async:
            names = ", ".join(s.name for s in self.stages if s.is_async)
            raise ConfigurationError(f"asynchronous stage(s) {names}: use arun()/aprocess()", key=self.name)

    def process(self, record: dict[str, Any], ctx: RecordContext | None = None) -> dict[str, Any] | None:
        """One record (changed in place) through every stage; ``None`` if a stage dropped it."""
        self._sync_only()
        ctx = ctx if ctx is not None else RecordContext()
        current = record
        for stage in self.stages:
            result = stage.process(current, ctx)
            if result is None:
                return None
            current = result
        return current

    async def aprocess(self, record: dict[str, Any], ctx: RecordContext | None = None) -> dict[str, Any] | None:
        """:meth:`process` for pipelines with asynchronous stages."""
        ctx = ctx if ctx is not None else RecordContext()
        current = record
        for stage in self.stages:
            result = await stage.aprocess(current, ctx) if stage.is_async else stage.process(current, ctx)
            if result is None:
                return None
            current = result
        return current

    def __call__(self, record: Mapping[str, Any]) -> dict[str, Any] | None:
        """Process a copy of ``record``."""
        return self.process(dict(record))

    def stream(self, records: Iterable[Any]) -> Iterator[dict[str, Any]]:
        """Process records one at a time, yielding those that come through (for large inputs)."""
        self._sync_only()
        try:
            for item in records:
                record = _as_record(item)
                if record is None:
                    raise TypeError(f"not a record: {type(item).__name__}")
                result = self.process(record)
                if result is not None:
                    yield result
        finally:
            self.close()

    def run(self, records: Iterable[Any]) -> list[dict[str, Any]]:
        """Process records (dicts, dataclasses...); the ones that come through, in order."""
        return list(self.stream(records))

    async def arun(self, records: Iterable[Any] | AsyncIterable[Any]) -> list[dict[str, Any]]:
        """:meth:`run` for pipelines with asynchronous stages (also accepts async iterables)."""
        out: list[dict[str, Any]] = []

        async def one(item: Any) -> None:
            record = _as_record(item)
            if record is None:
                raise TypeError(f"not a record: {type(item).__name__}")
            result = await self.aprocess(record)
            if result is not None:
                out.append(result)

        try:
            if isinstance(records, AsyncIterable):
                async for item in records:
                    await one(item)
            else:
                for item in records:
                    await one(item)
        finally:
            self.close()
        return out

    def close(self) -> None:
        for stage in self.stages:
            stage.close()

    # -- the item-pipeline protocol --------------------------------------------- #
    def process_item(self, item: Any, spider: Any = None) -> Any:
        record = _as_record(item)
        if record is None:
            return item
        ctx = RecordContext(spider=spider)
        if self.is_async:
            return self._aprocess_item(record, ctx)
        return _item_result(self.process(record, ctx), ctx)

    async def _aprocess_item(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any]:
        return _item_result(await self.aprocess(record, ctx), ctx)

    async def open_spider(self, spider: Any) -> None:
        for stage in self.stages:
            await maybe_await(stage.open_spider(spider))

    async def close_spider(self, spider: Any) -> None:
        for stage in reversed(self.stages):
            try:
                await maybe_await(stage.close_spider(spider))
            except Exception as exc:
                log.error("pipeline stage %s failed to close: %s: %s", stage.name, type(exc).__name__, exc)
        events = getattr(spider, "events", None)
        if events is not None:
            events.emit("pipeline_report", pipeline=self.name, stages=self.report())
        if any(stage.stats["in"] for stage in self.stages):
            log.info("%s", self.describe())

    # -- reporting -------------------------------------------------------------- #
    def report(self) -> list[dict[str, Any]]:
        """Per-stage counts: ``[{"stage", "kind", "in", "out", "dropped", "errors", ...}, ...]``."""
        out = []
        for stage in self.stages:
            entry: dict[str, Any] = {"stage": stage.name, "kind": stage.kind}
            entry.update({key: stage.stats.get(key, 0) for key in ("in", "out", "dropped", "errors")})
            entry.update({k: v for k, v in sorted(stage.stats.items()) if k not in entry})
            out.append(entry)
        return out

    def describe(self) -> str:
        """A table of what each stage did."""
        rows = [("stage", "in", "out", "dropped", "errors", "")]
        for stage in self.stages:
            s = stage.stats
            rows.append(
                (stage.name, f"{s['in']:,}", f"{s['out']:,}", f"{s['dropped']:,}", f"{s['errors']:,}", stage.details())
            )
        widths = [max(len(row[i]) for row in rows) for i in range(5)]
        lines = []
        for row in rows:
            cells = [row[0].ljust(widths[0])] + [
                cell.rjust(width) for cell, width in zip(row[1:5], widths[1:], strict=True)
            ]
            lines.append(("  ".join(cells) + ("  " + row[5] if row[5] else "")).rstrip())
        total_in = self.stages[0].stats["in"] if self.stages else 0
        total_out = self.stages[-1].stats["out"] if self.stages else 0
        return f"{self.name}: {total_in:,} in -> {total_out:,} out\n" + "\n".join("  " + line for line in lines)

    # -- configuration --------------------------------------------------------- #
    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any] | Sequence[Any],
        *,
        base_dir: str | Path | None = None,
        schemas: Mapping[str, Schema] | None = None,
        allow_imports: bool = False,
        loader: ConfigLoader | None = None,
    ) -> Pipeline:
        """A pipeline from its configuration form (see the module docs).

        ``config`` is ``{"stages": [...]}`` (optionally with ``"name"``) or the
        list of stages itself; each stage is a one-entry mapping ``{kind: options}``.
        """
        loader = loader or ConfigLoader(base_dir, schemas=schemas, allow_imports=allow_imports)
        name = "pipeline"
        if isinstance(config, Mapping):
            unknown = set(config) - {"$schema", "name", "stages"}
            if unknown:
                raise ConfigurationError(f"unknown option(s) {', '.join(sorted(unknown))}", key="pipeline")
            declared = config.get("$schema")
            if declared is not None and declared != PIPELINE_FORMAT:
                raise ConfigurationError(f"unsupported format {declared!r} (expected {PIPELINE_FORMAT!r})")
            name = str(config.get("name") or name)
            entries = config.get("stages")
        else:
            entries = config
        if not isinstance(entries, (list, tuple)):
            raise ConfigurationError("expected a list of stages", key="stages")
        pipeline = cls(name=name)
        for index, entry in enumerate(entries):
            where = f"stages[{index}]"
            if not isinstance(entry, Mapping) or len(entry) != 1:
                raise ConfigurationError("each stage is a one-entry mapping like {filter: 'price > 0'}", key=where)
            kind, options = next(iter(entry.items()))
            if str(kind) not in STAGES:
                from ..plugins import load_plugins

                load_plugins()  # a plugin may add it
            stage_cls = STAGES.get(str(kind))
            if stage_cls is None:
                raise ConfigurationError(f"unknown stage {kind!r}; known: {', '.join(sorted(STAGES))}", key=where)
            try:
                pipeline.add(stage_cls.from_config(options, loader))
            except ConfigurationError as exc:
                raise ConfigurationError(str(exc), key=where) from None
            except (WintergrabError, TypeError, ValueError) as exc:
                raise ConfigurationError(f"{kind}: {exc}", key=where) from None
        return pipeline

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        schemas: Mapping[str, Schema] | None = None,
        allow_imports: bool = False,
    ) -> Pipeline:
        """Read a pipeline from a JSON, YAML or TOML file (relative paths in it are relative to the file)."""
        source = Path(path)
        return cls.from_config(
            read_structured(source), base_dir=source.parent, schemas=schemas, allow_imports=allow_imports
        )

    def to_config(self) -> dict[str, Any]:
        """The configuration form (raises for stages built from Python functions)."""
        return {"$schema": PIPELINE_FORMAT, "name": self.name, "stages": [stage.to_config() for stage in self.stages]}

    def save(self, path: str | Path) -> Path:
        """Write :meth:`to_config` as JSON (or YAML for ``.yaml``/``.yml``)."""
        target = Path(path)
        data = self.to_config()
        if target.suffix.lower() in (".yaml", ".yml"):
            text = yaml_module().safe_dump(data, sort_keys=False, allow_unicode=True)
        else:
            text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        target.write_text(text, encoding="utf-8")
        return target

    def __repr__(self) -> str:
        return f"Pipeline({self.name!r}, stages=[{', '.join(s.name for s in self.stages)}])"
