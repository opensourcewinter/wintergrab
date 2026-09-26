"""Typed data schemas: declare the records you want, then normalize, validate and export them.

A :class:`Schema` is a list of typed :class:`SchemaField` s. It is the contract
between extraction and everything downstream::

    schema = Schema.from_dict({
        "name": "product",
        "key": ["url"],
        "fields": {
            "name": {"type": "string", "required": True},
            "price": {"type": "money", "required": True, "minimum": 0},
            "currency": "currency",
            "rating": {"type": "rating", "best": 5},
            "review_count": {"type": "integer", "minimum": 0},
            "availability": "availability",
            "url": {"type": "url", "required": True},
        },
    })
    record, results = schema.normalize({"name": " Phone ", "price": "₹29,999", "url": "/p/1"},
                                       base_url="https://shop.example/")
    # {"name": "Phone", "price": 29999, "currency": "INR", "url": "https://shop.example/p/1"}
    schema.validate(record)   # -> [] when everything is fine, else Issues

Schemas load from and save to JSON (or YAML, with PyYAML installed) in a
versioned format (``"$schema": "wintergrab/schema/v1"``), export to JSON
Schema (:meth:`Schema.to_json_schema`), and can be inferred from sample records
(:meth:`Schema.infer`).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import MISSING, dataclass, field, fields, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..errors import SchemaError
from ..files import read_structured, yaml_module
from .issues import Issue
from .normalize import (
    AVAILABILITY,
    Money,
    Quantity,
    as_int_or_float,
    clean_text,
    convert,
    has_replacement_characters,
    mojibake_score,
    normalize_availability,
    normalize_country,
    normalize_email,
    normalize_language,
    normalize_phone,
    normalize_region,
    normalize_url_value,
    parse_address,
    parse_boolean,
    parse_coordinates,
    parse_date,
    parse_datetime,
    parse_duration,
    parse_integer,
    parse_money,
    parse_number,
    parse_quantity,
    parse_rating,
    unit_info,
)
from .reference import CURRENCIES

__all__ = [
    "FIELD_TYPES",
    "SCHEMA_FORMAT",
    "FieldResult",
    "NormalizeContext",
    "Schema",
    "SchemaField",
    "load_schema",
    "register_type",
]

SCHEMA_FORMAT = "wintergrab/schema/v1"
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


@dataclass
class NormalizeContext:
    """What normalizers may need besides the value itself."""

    base_url: str | None = None  # resolves relative URLs
    country: str | None = None  # reads national phone numbers, "$", "kr"...
    currency: str | None = None  # when a price names none
    dayfirst: bool | None = None  # 03/05/2024 = 3 May?
    decimal: str | None = None  # the site's decimal separator, if known
    record: Mapping[str, Any] = field(default_factory=dict)  # the whole record (cross-field lookups)


@dataclass
class FieldResult:
    """How normalizing one field went."""

    raw: Any
    value: Any
    ok: bool  # False: there was a value, but it could not be read as the field's type
    notes: list[str] = field(default_factory=list)


_DEFAULT_CONTEXT = NormalizeContext()


# --------------------------------------------------------------------------- #
# field types
# --------------------------------------------------------------------------- #
def _number(raw: Any, f: SchemaField, ctx: NormalizeContext, notes: list[str]) -> Any:
    value = parse_number(raw, decimal=ctx.decimal, notes=notes)
    return None if value is None else as_int_or_float(value)


def _integer(raw: Any, f: SchemaField, ctx: NormalizeContext, notes: list[str]) -> Any:
    return parse_integer(raw, decimal=ctx.decimal, notes=notes)


def _string(raw: Any, f: SchemaField, ctx: NormalizeContext, notes: list[str]) -> Any:
    if isinstance(raw, (dict, list)):
        return None
    return clean_text(str(raw)) or None


def _text(raw: Any, f: SchemaField, ctx: NormalizeContext, notes: list[str]) -> Any:
    if isinstance(raw, (dict, list)):
        return None
    return clean_text(str(raw), keep_newlines=True) or None


def _boolean(raw: Any, f: SchemaField, ctx: NormalizeContext, notes: list[str]) -> Any:
    return parse_boolean(raw, notes=notes)


def _date(raw: Any, f: SchemaField, ctx: NormalizeContext, notes: list[str]) -> Any:
    value = parse_date(raw, dayfirst=ctx.dayfirst, notes=notes)
    return value.isoformat() if value else None


def _datetime(raw: Any, f: SchemaField, ctx: NormalizeContext, notes: list[str]) -> Any:
    value = parse_datetime(raw, dayfirst=ctx.dayfirst, notes=notes)
    return value.isoformat() if value else None


def _duration(raw: Any, f: SchemaField, ctx: NormalizeContext, notes: list[str]) -> Any:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return raw
    value = parse_duration(str(raw))
    return as_int_or_float(Decimal(str(value.total_seconds()))) if value is not None else None


def _url(raw: Any, f: SchemaField, ctx: NormalizeContext, notes: list[str]) -> Any:
    return normalize_url_value(str(raw), base_url=ctx.base_url)


def _email(raw: Any, f: SchemaField, ctx: NormalizeContext, notes: list[str]) -> Any:
    return normalize_email(str(raw), notes=notes)


def _phone(raw: Any, f: SchemaField, ctx: NormalizeContext, notes: list[str]) -> Any:
    return normalize_phone(str(raw), country=f.country or ctx.country, notes=notes)


def _money(raw: Any, f: SchemaField, ctx: NormalizeContext, notes: list[str]) -> Any:
    if isinstance(raw, Mapping) and "amount" in raw:  # already {"amount", "currency"}
        amount = parse_number(raw.get("amount"))
        currency = raw.get("currency") or f.currency or ctx.currency
        return Money(amount, str(currency).upper() if currency else None) if amount is not None else None
    return parse_money(raw, currency=f.currency or ctx.currency, country=f.country or ctx.country,
                       decimal=ctx.decimal, notes=notes)  # fmt: skip


def _currency(raw: Any, f: SchemaField, ctx: NormalizeContext, notes: list[str]) -> Any:
    text = str(raw).strip()
    if text.upper() in CURRENCIES:
        return text.upper()
    money = parse_money("1 " + text, country=f.country or ctx.country, notes=notes)
    if money is not None and money.currency:
        return money.currency
    money = parse_money(text + " 1", country=f.country or ctx.country, notes=notes)
    return money.currency if money is not None else None


def _country(raw: Any, f: SchemaField, ctx: NormalizeContext, notes: list[str]) -> Any:
    return normalize_country(str(raw), notes=notes)


def _region(raw: Any, f: SchemaField, ctx: NormalizeContext, notes: list[str]) -> Any:
    country = f.country or ctx.country
    if f.country_field and ctx.record.get(f.country_field):
        country = normalize_country(str(ctx.record[f.country_field])) or country
    return normalize_region(str(raw), country, notes=notes) or clean_text(str(raw))


def _language(raw: Any, f: SchemaField, ctx: NormalizeContext, notes: list[str]) -> Any:
    return normalize_language(str(raw), notes=notes)


def _quantity(raw: Any, f: SchemaField, ctx: NormalizeContext, notes: list[str]) -> Any:
    if isinstance(raw, Quantity):
        return raw
    return parse_quantity(str(raw), default_unit=f.unit, notes=notes)


def _rating(raw: Any, f: SchemaField, ctx: NormalizeContext, notes: list[str]) -> Any:
    rating = parse_rating(raw, best=f.best if f.best_given else None, notes=notes)
    return None if rating is None else as_int_or_float(round(rating.normalized(int(f.best)), 4))


def _availability(raw: Any, f: SchemaField, ctx: NormalizeContext, notes: list[str]) -> Any:
    return normalize_availability(raw, notes=notes)


def _address(raw: Any, f: SchemaField, ctx: NormalizeContext, notes: list[str]) -> Any:
    if isinstance(raw, Mapping):
        return dict(raw)
    address = parse_address(str(raw), country=f.country or ctx.country, notes=notes)
    return address.to_dict() if address else None


def _coordinates(raw: Any, f: SchemaField, ctx: NormalizeContext, notes: list[str]) -> Any:
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        raw = f"{raw[0]}, {raw[1]}"
    value = parse_coordinates(str(raw))
    return list(value) if value else None


def _enum(raw: Any, f: SchemaField, ctx: NormalizeContext, notes: list[str]) -> Any:
    text = clean_text(str(raw)) or ""
    for option in f.enum or ():
        if str(option) == text:
            return option
    folded = text.casefold()
    for option in f.enum or ():
        if str(option).casefold() == folded:
            return option
    return text or None  # validation reports a value outside the enum


def _any(raw: Any, f: SchemaField, ctx: NormalizeContext, notes: list[str]) -> Any:
    return raw


def _object(raw: Any, f: SchemaField, ctx: NormalizeContext, notes: list[str]) -> Any:
    if not isinstance(raw, Mapping) or f.schema is None:
        return None
    value, _ = f.schema.normalize(raw, context=replace(ctx, record=raw))
    return value


Normalizer = Callable[[Any, "SchemaField", NormalizeContext, list[str]], Any]

#: ``type name -> (normalizer, JSON Schema fragment)``. Register new types with :func:`register_type`.
FIELD_TYPES: dict[str, tuple[Normalizer, dict[str, Any]]] = {
    "string": (_string, {"type": "string"}),
    "text": (_text, {"type": "string"}),
    "integer": (_integer, {"type": "integer"}),
    "number": (_number, {"type": "number"}),
    "boolean": (_boolean, {"type": "boolean"}),
    "date": (_date, {"type": "string", "format": "date"}),
    "datetime": (_datetime, {"type": "string", "format": "date-time"}),
    "duration": (_duration, {"type": "number", "description": "seconds"}),
    "url": (_url, {"type": "string", "format": "uri"}),
    "email": (_email, {"type": "string", "format": "email"}),
    "phone": (_phone, {"type": "string", "pattern": r"^\+[1-9]\d{3,14}$"}),
    "money": (_money, {"type": "number"}),
    "currency": (_currency, {"type": "string", "pattern": "^[A-Z]{3}$"}),
    "country": (_country, {"type": "string", "pattern": "^[A-Z]{2}$"}),
    "region": (_region, {"type": "string"}),
    "language": (_language, {"type": "string"}),
    "quantity": (_quantity, {"type": "number"}),
    "rating": (_rating, {"type": "number", "minimum": 0}),
    "availability": (_availability, {"type": "string", "enum": list(AVAILABILITY)}),
    "address": (_address, {"type": "object"}),
    "coordinates": (_coordinates, {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}),
    "enum": (_enum, {}),
    "object": (_object, {"type": "object"}),
    "any": (_any, {}),
}
_ALIASES = {"str": "string", "int": "integer", "float": "number", "decimal": "number", "bool": "boolean",
            "price": "money", "uri": "url", "link": "url", "timestamp": "datetime", "dict": "object",
            "telephone": "phone", "tel": "phone"}  # fmt: skip
_NUMERIC = {"integer", "number", "money", "quantity", "rating", "duration"}


def register_type(name: str, normalizer: Normalizer, json_schema: Mapping[str, Any] | None = None) -> None:
    """Add a field type: ``normalizer(raw, field, context, notes) -> value | None``."""
    if not _NAME.match(name):
        raise SchemaError(f"invalid type name {name!r}")
    FIELD_TYPES[name] = (normalizer, dict(json_schema or {}))


# --------------------------------------------------------------------------- #
# fields and schemas
# --------------------------------------------------------------------------- #
@dataclass
class SchemaField:
    """One field of a :class:`Schema`.

    Args:
        name: Field name (``[A-Za-z_][A-Za-z0-9_.-]*``).
        type: One of :data:`FIELD_TYPES` (``string``, ``integer``, ``number``, ``money``, ``date``, ``url``...).
        required: The record is invalid without a value.
        many: A list of values of ``type``.
        description: What the field means (used by planners and AI adapters).
        enum: Allowed values (``type="enum"``, or a restriction on any type).
        minimum/maximum: Numeric bounds (for money: the amount; for quantity: in ``unit``).
        min_length/max_length: String length bounds, or list length bounds with ``many``.
        pattern: A regular expression string values must match.
        unit: For ``quantity``: convert to this unit (the value becomes a number in it).
        currency: For ``money``: the currency when the text names none.
        country: For ``phone``/``money``/``region``/``address``: the country to read local formats with.
        country_field: For ``region``: another field holding the country.
        currency_field: For ``money``: where the detected currency goes (default: a ``currency``-typed
            field, if the schema has exactly one; otherwise the value stays ``{"amount", "currency"}``).
        best: For ``rating``: the scale values are converted to (default 5).
        key: Part of the record's identity (de-duplication, dataset versions).
        selectors: Extraction hints: CSS/XPath selectors for this field.
        sources: Extraction hints: structured-data paths (``"jsonld:Product.offers.price"``).
        aliases: Other names the field goes by (``"cost"`` for ``price``).
        default: Value used when there is none.
        fields: For ``object``: nested fields (a :class:`Schema` is built from them).
    """

    name: str
    type: str = "string"
    required: bool = False
    many: bool = False
    description: str = ""
    enum: list[Any] | None = None
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    unit: str | None = None
    currency: str | None = None
    country: str | None = None
    country_field: str | None = None
    currency_field: str | None = None
    best: float = 5
    key: bool = False
    selectors: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    default: Any = None
    fields: list[SchemaField] | None = None
    best_given: bool = field(default=False, repr=False)
    schema: Schema | None = field(default=None, repr=False, compare=False)
    _pattern: re.Pattern[str] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME.match(self.name):
            raise SchemaError(f"invalid field name {self.name!r}")
        self.type = _ALIASES.get(str(self.type).lower(), str(self.type).lower())
        if self.type not in FIELD_TYPES:
            from ..plugins import load_plugins

            load_plugins()  # a plugin may add it
        if self.type not in FIELD_TYPES:
            raise SchemaError(
                f"field {self.name!r}: unknown type {self.type!r}; known: {', '.join(sorted(FIELD_TYPES))}"
            )
        if self.type == "enum" and not self.enum:
            raise SchemaError(f"field {self.name!r}: type 'enum' needs an 'enum' list")
        if self.pattern is not None:
            try:
                self._pattern = re.compile(self.pattern)
            except re.error as exc:
                raise SchemaError(f"field {self.name!r}: invalid pattern {self.pattern!r}: {exc}") from exc
        if self.unit is not None:
            try:
                symbol, _ = unit_info(self.unit)
            except ValueError as exc:
                raise SchemaError(f"field {self.name!r}: {exc}") from exc
            self.unit = symbol
        if self.currency is not None:
            if self.currency.upper() not in CURRENCIES:
                raise SchemaError(f"field {self.name!r}: unknown currency {self.currency!r}")
            self.currency = self.currency.upper()
        if self.type == "object":
            if not self.fields:
                raise SchemaError(f"field {self.name!r}: type 'object' needs 'fields'")
            self.schema = Schema(name=self.name, fields=list(self.fields))
        for bound in ("minimum", "maximum"):
            value = getattr(self, bound)
            if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)):
                raise SchemaError(f"field {self.name!r}: {bound} must be a number")

    @property
    def all_names(self) -> list[str]:
        return [self.name, *self.aliases]

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        for f in fields(self):
            if f.name in ("name", "type", "best_given", "schema", "_pattern", "fields"):
                continue
            value = getattr(self, f.name)
            if f.default is not MISSING:
                default = f.default
            elif f.default_factory is not MISSING:
                default = f.default_factory()
            else:
                default = None
            if value != default and value not in (None, [], ""):
                out[f.name] = value
        if self.best_given:
            out["best"] = self.best
        if self.fields:
            out["fields"] = {sub.name: sub.to_dict() for sub in self.fields}
        return out

    @classmethod
    def from_spec(cls, name: str, spec: Any) -> SchemaField:
        """A field from its file form: a type name (``"money"``) or a dict of options."""
        if isinstance(spec, str):
            many = spec.endswith("[]")
            return cls(name=name, type=spec[:-2] if many else spec, many=many)
        if not isinstance(spec, Mapping):
            raise SchemaError(f"field {name!r}: expected a type name or a mapping, got {spec!r}")
        options = dict(spec)
        options.pop("name", None)
        allowed = {f.name for f in fields(cls)} - {"name", "best_given", "schema", "_pattern"}
        unknown = set(options) - allowed
        if unknown:
            raise SchemaError(f"field {name!r}: unknown option(s) {', '.join(sorted(unknown))}")
        if "fields" in options:
            options["fields"] = _parse_fields(options["fields"], f"{name}.")
        for list_option in ("selectors", "sources", "aliases"):
            value = options.get(list_option)
            if isinstance(value, str):
                options[list_option] = [value]
        best_given = "best" in options
        instance = cls(name=name, **options)
        instance.best_given = best_given
        return instance


def _parse_fields(spec: Any, prefix: str = "") -> list[SchemaField]:
    if isinstance(spec, Mapping):
        return [SchemaField.from_spec(str(name), sub) for name, sub in spec.items()]
    if isinstance(spec, Sequence) and not isinstance(spec, str):
        out = []
        for entry in spec:
            if isinstance(entry, SchemaField):
                out.append(entry)
            elif isinstance(entry, Mapping) and "name" in entry:
                out.append(SchemaField.from_spec(str(entry["name"]), entry))
            else:
                raise SchemaError(f"{prefix}fields: each entry needs a 'name', got {entry!r}")
        return out
    raise SchemaError(f"{prefix}fields must be a mapping or a list")


@dataclass
class Schema:
    """A named, versioned list of typed fields. See the module docs."""

    name: str = "record"
    fields: list[SchemaField] = field(default_factory=list)
    version: int = 1
    description: str = ""
    key: list[str] = field(default_factory=list)
    #: Fields in records but not in the schema: ``"keep"``, ``"drop"`` or ``"warn"`` (validation info).
    extra: str = "keep"

    def __post_init__(self) -> None:
        names = [f.name for f in self.fields]
        duplicates = [n for n, c in Counter(names).items() if c > 1]
        if duplicates:
            raise SchemaError(f"schema {self.name!r}: duplicate field(s) {', '.join(duplicates)}")
        if self.extra not in ("keep", "drop", "warn"):
            raise SchemaError("extra must be 'keep', 'drop' or 'warn'")
        self.key = list(self.key) or [f.name for f in self.fields if f.key]
        missing = [k for k in self.key if k not in names]
        if missing:
            raise SchemaError(f"schema {self.name!r}: key field(s) {', '.join(missing)} are not defined")
        self._by_name = {f.name: f for f in self.fields}
        self._lookup = [(f, tuple(f.all_names)) for f in self.fields]
        currency_fields = [f.name for f in self.fields if f.type == "currency" and not f.many]
        self._currency_field = currency_fields[0] if len(currency_fields) == 1 else None

    # -- access ---------------------------------------------------------- #
    def __getitem__(self, name: str) -> SchemaField:
        return self._by_name[name]

    def __contains__(self, name: object) -> bool:
        return name in self._by_name

    def __iter__(self) -> Any:
        return iter(self.fields)

    def __len__(self) -> int:
        return len(self.fields)

    @property
    def names(self) -> list[str]:
        return [f.name for f in self.fields]

    @property
    def required(self) -> list[str]:
        return [f.name for f in self.fields if f.required]

    def key_of(self, record: Mapping[str, Any]) -> tuple[Any, ...] | None:
        """The record's identity (its ``key`` fields' values), ``None`` without a key or with missing parts."""
        if not self.key:
            return None
        values = tuple(record.get(k) for k in self.key)
        return None if any(v is None or v == "" for v in values) else values

    # -- file format ------------------------------------------------------- #
    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Schema:
        if not isinstance(data, Mapping):
            raise SchemaError("a schema must be a mapping")
        declared = data.get("$schema")
        if declared is not None and declared != SCHEMA_FORMAT:
            raise SchemaError(f"unsupported schema format {declared!r} (expected {SCHEMA_FORMAT!r})")
        known = {"$schema", "name", "fields", "version", "description", "key", "extra"}
        unknown = set(data) - known
        if unknown:
            raise SchemaError(f"unknown schema option(s): {', '.join(sorted(unknown))}")
        if "fields" not in data:
            raise SchemaError("a schema needs 'fields'")
        key = data.get("key") or []
        return cls(
            name=str(data.get("name") or "record"),
            fields=_parse_fields(data["fields"]),
            version=int(data.get("version") or 1),
            description=str(data.get("description") or ""),
            key=[key] if isinstance(key, str) else list(key),
            extra=str(data.get("extra") or "keep"),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"$schema": SCHEMA_FORMAT, "name": self.name, "version": self.version}
        if self.description:
            out["description"] = self.description
        if self.key:
            out["key"] = list(self.key)
        if self.extra != "keep":
            out["extra"] = self.extra
        out["fields"] = {f.name: f.to_dict() for f in self.fields}
        return out

    @classmethod
    def load(cls, path: str | Path) -> Schema:
        """Read a schema from ``.json``, ``.yaml``/``.yml`` (needs PyYAML) or ``.toml`` (Python 3.11+ or tomli)."""
        return load_schema(path)

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        data = self.to_dict()
        if target.suffix.lower() in (".yaml", ".yml"):
            text = yaml_module(SchemaError).safe_dump(data, sort_keys=False, allow_unicode=True)
            target.write_text(text, encoding="utf-8")
        else:
            target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return target

    def to_json_schema(self) -> dict[str, Any]:
        """The schema as JSON Schema (draft 2020-12), e.g. for validating output elsewhere."""
        properties: dict[str, Any] = {}
        for f in self.fields:
            properties[f.name] = _json_schema_of(f, self)
        out: dict[str, Any] = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": self.name,
            "type": "object",
            "properties": properties,
        }
        if self.description:
            out["description"] = self.description
        if self.required:
            out["required"] = self.required
        if self.extra == "drop":
            out["additionalProperties"] = False
        return out

    # -- normalization ----------------------------------------------------- #
    def normalize(
        self,
        record: Mapping[str, Any],
        *,
        context: NormalizeContext | None = None,
        base_url: str | None = None,
        country: str | None = None,
        currency: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, FieldResult]]:
        """Typed, cleaned copy of ``record`` and a :class:`FieldResult` per schema field.

        Values found under a field's ``aliases`` are moved to the field's name.
        Fields that are not in the schema are kept, dropped or kept for a
        warning according to :attr:`extra`.
        """
        base = context or _DEFAULT_CONTEXT
        page = record.get("url")
        ctx = NormalizeContext(
            base_url=base_url or base.base_url or (page if isinstance(page, str) else None),
            country=country or base.country,
            currency=currency or base.currency,
            dayfirst=base.dayfirst,
            decimal=base.decimal,
            record=record,
        )
        out: dict[str, Any] = {}
        results: dict[str, FieldResult] = {}
        consumed: set[str] = set()
        for f, names in self._lookup:
            raw = None
            for name in names:
                if name in record:
                    consumed.add(name)
                    if raw is None:
                        candidate = record[name]
                        if not (candidate is None or (isinstance(candidate, (str, list, dict)) and not candidate)):
                            raw = candidate
            notes: list[str] = []
            value = _normalize_value(f, raw, ctx, notes) if raw is not None else None
            ok = raw is None or (value is not None and value != [])
            if value is None and f.default is not None:
                value = f.default
            results[f.name] = FieldResult(raw=raw, value=value, ok=ok, notes=notes)
            out[f.name] = value
        self._split_money(out, results)
        self._finish_quantities(out, results)
        if self.extra != "drop":
            for name, value in record.items():
                if name not in consumed and name not in out:
                    out[name] = value
        return out, results

    def normalize_value(self, name: str, raw: Any, *, context: NormalizeContext | None = None) -> FieldResult:
        """Read one raw value as field ``name`` (without record-level steps such as the currency split).

        Extractors use it to compare candidate values: ``"$299.99"`` and
        ``"299.99"`` both become amount 299.99.
        """
        f = self._by_name[name]
        notes: list[str] = []
        value = _normalize_value(f, raw, context or _DEFAULT_CONTEXT, notes) if raw is not None else None
        return FieldResult(raw=raw, value=value, ok=raw is None or (value is not None and value != []), notes=notes)

    def _split_money(self, out: dict[str, Any], results: dict[str, FieldResult]) -> None:
        """Money values: amount into the field, currency into the currency field (or keep both together)."""
        for f in self.fields:
            if f.type != "money":
                continue
            target = f.currency_field or self._currency_field
            value = out.get(f.name)
            values = value if f.many and isinstance(value, list) else [value]
            converted = []
            for item in values:
                if not isinstance(item, Money):
                    converted.append(item)
                    continue
                if target is not None and target in self._by_name:
                    if item.currency and not out.get(target):
                        out[target] = item.currency
                    elif item.currency and out.get(target) and out[target] != item.currency:
                        results[f.name].notes.append("currency-conflict")
                    converted.append(as_int_or_float(item.amount))
                else:
                    converted.append(item.to_dict())
            out[f.name] = converted if f.many and isinstance(value, list) else converted[0]
            results[f.name].value = out[f.name]

    def _finish_quantities(self, out: dict[str, Any], results: dict[str, FieldResult]) -> None:
        for f in self.fields:
            if f.type != "quantity":
                continue
            value = out.get(f.name)
            values = value if f.many and isinstance(value, list) else [value]
            converted = []
            for item in values:
                if not isinstance(item, Quantity):
                    converted.append(item)
                    continue
                if f.unit:
                    try:
                        converted.append(as_int_or_float(round(convert(item, f.unit).value, 9)))
                    except ValueError:
                        results[f.name].ok = False
                        results[f.name].notes.append("unit-mismatch")
                        converted.append(None)
                else:
                    converted.append(item.to_dict())
            out[f.name] = converted if f.many and isinstance(value, list) else converted[0]
            results[f.name].value = out[f.name]

    # -- validation -------------------------------------------------------- #
    def validate(self, record: Mapping[str, Any], results: Mapping[str, FieldResult] | None = None) -> list[Issue]:
        """Issues with an already normalized record (``[]`` if it is valid).

        Pass the ``results`` from :meth:`normalize` to also report raw values
        that could not be read as their field's type.
        """
        from .validate import validate_record

        return validate_record(record, self, results)

    # -- inference ----------------------------------------------------------- #
    @classmethod
    def infer(cls, records: Iterable[Mapping[str, Any]], name: str = "inferred", *, sample: int = 1000) -> Schema:
        """Guess a schema from sample records (see :func:`wintergrab.data.inference.infer_schema`)."""
        from .inference import infer_schema

        return infer_schema(records, name=name, sample=sample)


def _json_schema_of(f: SchemaField, schema: Schema) -> dict[str, Any]:
    base = dict(FIELD_TYPES[f.type][1])
    if f.type == "money" and not (f.currency_field or schema._currency_field):
        base = {
            "type": "object",
            "properties": {"amount": {"type": "number"}, "currency": {"type": ["string", "null"]}},
        }
    if f.type == "quantity" and not f.unit:
        base = {"type": "object", "properties": {"value": {"type": "number"}, "unit": {"type": "string"}}}
    if f.type == "rating":
        base["maximum"] = f.best
    if f.type == "object" and f.schema is not None:
        base = f.schema.to_json_schema()
        base.pop("$schema", None)
    if f.enum:
        base["enum"] = list(f.enum)
    if f.minimum is not None:
        base["minimum"] = f.minimum
    if f.maximum is not None:
        base["maximum"] = f.maximum
    if f.pattern and not f.many:
        base["pattern"] = f.pattern
    if f.min_length is not None and not f.many:
        base["minLength"] = f.min_length
    if f.max_length is not None and not f.many:
        base["maxLength"] = f.max_length
    if f.description:
        base["description"] = f.description
    if f.many:
        base = {"type": "array", "items": base}
        if f.min_length is not None:
            base["minItems"] = f.min_length
        if f.max_length is not None:
            base["maxItems"] = f.max_length
    if not f.required:  # optional fields may be null
        if "type" in base:
            kind = base["type"]
            base["type"] = [*kind, "null"] if isinstance(kind, list) else [kind, "null"]
        if "enum" in base and None not in base["enum"]:
            base["enum"] = [*base["enum"], None]
    return base


def _normalize_value(f: SchemaField, raw: Any, ctx: NormalizeContext, notes: list[str]) -> Any:
    normalizer = FIELD_TYPES[f.type][0]
    if f.many:
        items = raw if isinstance(raw, (list, tuple)) else [raw]
        values = [_one(normalizer, f, item, ctx, notes) for item in items if item not in (None, "")]
        return [v for v in values if v is not None]
    return _one(normalizer, f, raw, ctx, notes)


def _one(normalizer: Normalizer, f: SchemaField, raw: Any, ctx: NormalizeContext, notes: list[str]) -> Any:
    if isinstance(raw, (list, tuple)) and f.type not in ("coordinates", "any"):
        raw = next((item for item in raw if item not in (None, "")), None)  # first of several matches
        if raw is None:
            return None
        notes.append("first-of-many")
    if isinstance(raw, (date, datetime)) and f.type in ("date", "datetime"):
        return raw.isoformat() if f.type == "datetime" or not isinstance(raw, datetime) else raw.date().isoformat()
    if isinstance(raw, timedelta) and f.type == "duration":
        return as_int_or_float(Decimal(str(raw.total_seconds())))
    try:
        return normalizer(raw, f, ctx, notes)
    except (ArithmeticError, TypeError, ValueError) as exc:
        notes.append(f"error:{type(exc).__name__}")
        return None


def load_schema(path: str | Path) -> Schema:
    """Read a schema file (``.json``, ``.yaml``/``.yml``, ``.toml``)."""
    return Schema.from_dict(read_structured(path, error=SchemaError))


def encoding_issue(value: Any) -> str | None:
    """``"mojibake"`` / ``"replacement-characters"`` when a string shows encoding damage."""
    if not isinstance(value, str) or not value:
        return None
    if has_replacement_characters(value):
        return "replacement-characters"
    if mojibake_score(value) > 0.02:
        return "mojibake"
    return None


NUMERIC_TYPES = frozenset(_NUMERIC)
