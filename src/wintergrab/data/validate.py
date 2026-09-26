"""Record validation against a :class:`~wintergrab.data.schema.Schema`, plus custom rules.

:func:`validate_record` checks a *normalized* record: required fields, values
that could not be read as their type, numeric bounds, lengths, patterns,
enums, and suspicious values (encoding damage, a zero or negative price, a
date in the far future). Custom :class:`Rule` s add checks that involve
several fields or domain knowledge, as expressions (see
:mod:`wintergrab.data.expressions`) or functions::

    rules = [
        Rule("sale-below-list", "sale_price <= price", "the sale price is above the list price",
             field="sale_price"),
        Rule("known-brand", lambda r: r["brand"] in BRANDS, field="brand", severity="warning"),
    ]
    issues = validate_record(record, schema, rules=rules)
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ..errors import SchemaError
from .expressions import compile_expression, get_path
from .issues import SEVERITIES, Issue

if TYPE_CHECKING:
    from .schema import FieldResult, Schema, SchemaField

__all__ = ["Rule", "is_valid", "validate_record"]

_FUTURE_SLACK = timedelta(days=366 * 5)  # dates more than five years ahead are suspicious


@dataclass
class Rule:
    """A custom check on a whole record.

    ``check`` is an expression (``"sale_price <= price"``), which holds when
    it is truthy, or a function ``record -> bool | str | None``: a truthy
    value or ``None`` means the record is fine, a string is the failure message.

    An expression rule is skipped when a field it reads has no value
    (``skip_missing``): whether a value must be there is the schema's
    ``required``, so ``"sale_price <= price"`` only judges records with both prices.
    """

    code: str
    check: Callable[[Mapping[str, Any]], Any] | str
    message: str = ""
    field: str | None = None
    severity: str = "error"
    skip_missing: bool = True

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise SchemaError(f"rule {self.code!r}: severity must be one of {', '.join(SEVERITIES)}")
        self._names: frozenset[str] = frozenset()
        if isinstance(self.check, str):
            expression = compile_expression(self.check)
            self._fn: Callable[[Mapping[str, Any]], Any] = expression
            self._names = expression.names
        elif callable(self.check):
            self._fn = self.check
        else:
            raise SchemaError(f"rule {self.code!r}: check must be an expression or a function")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Rule:
        """A rule from its file form: ``{"code": ..., "check": "<expression>", "message": ..., ...}``."""
        options = dict(data)
        unknown = set(options) - {"code", "check", "message", "field", "severity", "skip_missing"}
        if unknown:
            raise SchemaError(f"rule: unknown option(s) {', '.join(sorted(unknown))}")
        if not isinstance(options.get("code"), str) or not isinstance(options.get("check"), str):
            raise SchemaError("a rule needs a 'code' and a 'check' expression")
        return cls(**options)

    def to_dict(self) -> dict[str, Any]:
        if not isinstance(self.check, str):
            raise SchemaError(f"rule {self.code!r} uses a function and cannot be written to a file")
        out: dict[str, Any] = {"code": self.code, "check": self.check}
        if self.message:
            out["message"] = self.message
        if self.field:
            out["field"] = self.field
        if self.severity != "error":
            out["severity"] = self.severity
        if not self.skip_missing:
            out["skip_missing"] = False
        return out

    def apply(self, record: Mapping[str, Any]) -> Issue | None:
        if self.skip_missing and any(get_path(record, name) in (None, "") for name in self._names):
            return None
        try:
            outcome = self._fn(record)
        except Exception as exc:  # a broken rule is itself an issue, not a crash
            return Issue(self.field, self.code, f"rule {self.code!r} failed: {type(exc).__name__}: {exc}", "warning")
        if isinstance(self.check, str):
            if outcome:  # an expression holds when it is truthy
                return None
        elif outcome is None or (outcome and not isinstance(outcome, str)):
            return None  # a function passes with a truthy value or no value at all
        if isinstance(outcome, str) and not isinstance(self.check, str):
            message = outcome
        elif self.message:
            message = self.message
        elif isinstance(self.check, str):
            message = f"expected {self.check}"
        else:
            message = f"rule {self.code!r} failed"
        value = get_path(record, self.field) if self.field else None
        return Issue(self.field, self.code, message, self.severity, value)


def _numeric_value(f: SchemaField, value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if f.type == "money" and isinstance(value, Mapping):
        amount = value.get("amount")
        return float(amount) if isinstance(amount, (int, float)) else None
    if f.type == "quantity" and isinstance(value, Mapping):
        amount = value.get("value")
        return float(amount) if isinstance(amount, (int, float)) else None
    return None


def _check_value(f: SchemaField, value: Any, path: str, issues: list[Issue]) -> None:
    from .schema import NUMERIC_TYPES, encoding_issue

    if f.enum and value not in f.enum:
        issues.append(Issue(path, "enum", f"{value!r} is not one of {f.enum!r}", value=value))
    number = _numeric_value(f, value) if f.type in NUMERIC_TYPES else None
    if number is not None:
        if f.minimum is not None and number < f.minimum:
            issues.append(Issue(path, "range", f"{number:g} is below the minimum {f.minimum:g}", value=value))
        if f.maximum is not None and number > f.maximum:
            issues.append(Issue(path, "range", f"{number:g} is above the maximum {f.maximum:g}", value=value))
        if f.type == "money" and number <= 0 and f.minimum is None:
            issues.append(Issue(path, "suspicious", f"price of {number:g}", "warning", value))
    if isinstance(value, str):
        if not f.many:
            if f.min_length is not None and len(value) < f.min_length:
                issues.append(Issue(path, "length", f"shorter than {f.min_length} characters", value=value))
            if f.max_length is not None and len(value) > f.max_length:
                issues.append(Issue(path, "length", f"longer than {f.max_length} characters", value=value))
        if f._pattern is not None and not f._pattern.search(value):
            issues.append(Issue(path, "pattern", f"does not match {f.pattern!r}", value=value))
        damage = encoding_issue(value)
        if damage:
            issues.append(Issue(path, "encoding", f"text looks damaged ({damage})", "warning", value))
        if f.type in ("date", "datetime"):
            _check_date(value, path, issues)


def _check_date(value: str, path: str, issues: list[Issue]) -> None:
    try:
        moment = (
            datetime.fromisoformat(value)
            if "T" in value
            else datetime.combine(date.fromisoformat(value), datetime.min.time())
        )
    except ValueError:
        return
    now = datetime.now(timezone.utc) if moment.tzinfo else datetime.now()
    if moment - now > _FUTURE_SLACK:
        issues.append(Issue(path, "suspicious", "date is more than five years in the future", "warning", value))
    if moment.year < 1800:
        issues.append(Issue(path, "suspicious", "date is before 1800", "warning", value))


def validate_record(
    record: Mapping[str, Any],
    schema: Schema | None,
    results: Mapping[str, FieldResult] | None = None,
    *,
    rules: Sequence[Rule] = (),
    prefix: str = "",
) -> list[Issue]:
    """Every :class:`~wintergrab.data.issues.Issue` with a normalized record (``[]`` if valid).

    ``schema`` may be ``None`` to apply only the ``rules``.
    """
    issues: list[Issue] = []
    for f in schema.fields if schema is not None else ():
        path = prefix + f.name
        value = record.get(f.name)
        result = results.get(f.name) if results else None
        if result is not None and not result.ok:
            issues.append(Issue(path, "invalid", f"could not read {result.raw!r} as {f.type}", "error", result.raw))
            continue
        empty = value is None or value == "" or value == [] or value == {}
        if empty:
            if f.required:
                issues.append(Issue(path, "missing", "required field is missing"))
            continue
        if f.many:
            if not isinstance(value, list):
                issues.append(Issue(path, "type", "expected a list", value=value))
                continue
            if f.min_length is not None and len(value) < f.min_length:
                issues.append(Issue(path, "length", f"fewer than {f.min_length} values", value=value))
            if f.max_length is not None and len(value) > f.max_length:
                issues.append(Issue(path, "length", f"more than {f.max_length} values", value=value))
            for i, item in enumerate(value):
                _check_item(f, item, f"{path}[{i}]", issues)
        else:
            _check_item(f, value, path, issues)
    if schema is not None and schema.extra == "warn":
        known = {n for f in schema.fields for n in f.all_names}
        for name in record:
            if name not in known and not str(name).startswith("_"):
                issues.append(Issue(prefix + str(name), "unknown-field", "not in the schema", "info"))
    for rule in rules:
        issue = rule.apply(record)
        if issue is not None:
            issues.append(issue)
    return issues


def _check_item(f: SchemaField, value: Any, path: str, issues: list[Issue]) -> None:
    if f.type == "object" and f.schema is not None:
        if isinstance(value, Mapping):
            issues.extend(validate_record(value, f.schema, prefix=path + "."))
        else:
            issues.append(Issue(path, "type", "expected an object", value=value))
        return
    _check_value(f, value, path, issues)


def is_valid(issues: Iterable[Issue]) -> bool:
    """``True`` when none of ``issues`` is an error (warnings and info are fine)."""
    return not any(issue.severity == "error" for issue in issues)
