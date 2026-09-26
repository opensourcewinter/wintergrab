"""A small, safe expression language for filters, computed fields and validation rules.

::

    price > 0 and availability == "in_stock"
    round(1 - sale_price / price, 2)
    coalesce(title, name, "untitled")
    domain(url) in ("shop.example", "store.example")
    matches(sku, "^[A-Z]{3}-[0-9]+$")
    distance_km(coordinates, [52.52, 13.405]) <= 25

The syntax is Python's, restricted to literals, field names, arithmetic
(``+ - * / // % **``), comparisons (``== != < <= > >= in``, ``not in``,
``is None``), ``and``/``or``/``not``, ``a if condition else b``, indexing and
slicing, and calls to the functions in :data:`FUNCTIONS`. There is no
attribute access, no imports, no comprehensions and no lambdas: an expression
can read the record and call those functions, nothing else. Expressions from
configuration files (or written by a planner) can read records, not the machine.

Missing data behaves like SQL's ``NULL`` instead of raising:

* a field the record does not have is ``None``;
* arithmetic with ``None`` gives ``None``, and so does division by zero;
* ordering comparisons (``<``, ``>=``...) involving ``None`` are false;
* an index past the end, or a missing key, gives ``None``.

Mixing up types (``"12.99" > 10``, ``"a" - 1``) raises :class:`ExpressionError`:
it usually means a value was not normalized, which is worth seeing.

Field names that are not Python identifiers, and nested values, are read with
``get``: ``get("list-price")``, ``get("offers.0.price")``.
"""

from __future__ import annotations

import ast
import functools
import inspect
import operator
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..errors import ExpressionError
from ..fetchers.resources import registrable_domain
from .normalize import (
    as_int_or_float,
    clean_text,
    detect_currency,
    parse_boolean,
    parse_date,
    parse_datetime,
    parse_integer,
    parse_money,
    parse_number,
)
from .similarity import content_hash

__all__ = ["FUNCTIONS", "Expression", "compile_expression", "get_path"]

MAX_LENGTH = 10_000  # characters of source
MAX_DEPTH = 100  # nesting of the syntax tree
MAX_REPEAT = 1_000_000  # items a "*" repetition may produce

Evaluator = Callable[[Mapping[str, Any]], Any]


def get_path(record: Any, path: str, default: Any = None) -> Any:
    """The value at a dotted ``path`` (``"offers.0.price"``); a key with that exact name wins."""
    if isinstance(record, Mapping) and path in record:
        value = record[path]
        return default if value is None else value
    current = record
    for part in path.split("."):
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, (list, tuple)) and part.lstrip("-").isdigit():
            index = int(part)
            current = current[index] if -len(current) <= index < len(current) else None
        else:
            return default
        if current is None:
            return default
    return current


# --------------------------------------------------------------------------- #
# functions
# --------------------------------------------------------------------------- #
def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


@functools.lru_cache(maxsize=256)
def _regex(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ExpressionError(f"invalid regular expression {pattern!r}: {exc}") from exc


def _len(value: Any) -> int:
    return 0 if value is None else len(value)


def _lower(value: Any) -> str | None:
    text = _text(value)
    return None if text is None else text.lower()


def _upper(value: Any) -> str | None:
    text = _text(value)
    return None if text is None else text.upper()


def _title(value: Any) -> str | None:
    text = _text(value)
    return None if text is None else text.title()


def _strip(value: Any) -> str | None:
    text = _text(value)
    return None if text is None else text.strip()


def _clean(value: Any) -> str | None:
    text = _text(value)
    return None if text is None else clean_text(text)


def _contains(haystack: Any, needle: Any) -> bool:
    if haystack is None or needle is None:
        return False
    if isinstance(haystack, str):
        return str(needle) in haystack
    return needle in haystack


def _icontains(haystack: Any, needle: Any) -> bool:
    if haystack is None or needle is None:
        return False
    if isinstance(haystack, str):
        return str(needle).casefold() in haystack.casefold()
    folded = str(needle).casefold()
    return any(isinstance(item, str) and item.casefold() == folded for item in haystack)


def _startswith(value: Any, prefix: Any) -> bool:
    text = _text(value)
    return text is not None and prefix is not None and text.startswith(str(prefix))


def _endswith(value: Any, suffix: Any) -> bool:
    text = _text(value)
    return text is not None and suffix is not None and text.endswith(str(suffix))


def _matches(value: Any, pattern: str) -> bool:
    text = _text(value)
    return text is not None and _regex(pattern).search(text) is not None


def _extract(value: Any, pattern: str, group: int | str = 1) -> str | None:
    text = _text(value)
    if text is None:
        return None
    compiled = _regex(pattern)
    match = compiled.search(text)
    if match is None:
        return None
    if compiled.groups == 0 and group == 1:
        return match.group(0)
    return match.group(group)


def _replace(value: Any, old: str, new: str) -> str | None:
    text = _text(value)
    return None if text is None else text.replace(str(old), str(new))


def _sub(value: Any, pattern: str, replacement: str) -> str | None:
    text = _text(value)
    return None if text is None else _regex(pattern).sub(str(replacement), text)


def _split(value: Any, separator: str | None = None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [part for item in value if item is not None for part in _split(item, separator)]
    return [part.strip() for part in str(value).split(separator) if part.strip()]


def _join(value: Any, separator: str = " ") -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(separator).join(str(item) for item in value if item not in (None, ""))


def _first(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _last(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return value[-1] if value else None
    return value


def _coalesce(*values: Any) -> Any:
    for value in values:
        if not (value is None or (isinstance(value, (str, list, dict, tuple)) and not value)):
            return value
    return None


def _values(args: tuple[Any, ...]) -> list[Any]:
    items = args[0] if len(args) == 1 and isinstance(args[0], (list, tuple, set)) else args
    return [item for item in items if item is not None]


def _min(*args: Any) -> Any:
    values = _values(args)
    return min(values) if values else None


def _max(*args: Any) -> Any:
    values = _values(args)
    return max(values) if values else None


def _sum(value: Any) -> Any:
    if value is None:
        return None
    return sum(item for item in value if item is not None)


def _abs(value: Any) -> Any:
    return None if value is None else abs(value)


def _round(value: Any, digits: int | None = None) -> Any:
    if value is None:
        return None
    return round(value) if digits is None else round(value, int(digits))


def _int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return int(float(text)) if any(c in text for c in ".eE") else int(text)
    return int(value)


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def _str(value: Any) -> str | None:
    return _text(value)


def _bool(value: Any) -> bool:
    return bool(value)


def _any(value: Any) -> bool:
    return value is not None and any(value)


def _all(value: Any) -> bool:
    return value is not None and all(value)


def _number(value: Any) -> int | float | None:
    parsed = parse_number(value)
    return None if parsed is None else as_int_or_float(parsed)


def _integer(value: Any) -> int | None:
    return parse_integer(value)


def _money(value: Any, currency: str | None = None) -> int | float | None:
    parsed = parse_money(value, currency=currency)
    return None if parsed is None else as_int_or_float(parsed.amount)


def _currency(value: Any, default: str | None = None) -> str | None:
    text = _text(value)
    return None if text is None else detect_currency(text, default=default)


def _date(value: Any) -> str | None:
    parsed = parse_date(value)
    return None if parsed is None else parsed.isoformat()


def _datetime(value: Any) -> str | None:
    parsed = parse_datetime(value)
    return None if parsed is None else parsed.isoformat()


def _boolean(value: Any) -> bool | None:
    return parse_boolean(value)


def _host(url: Any) -> str | None:
    text = _text(url)
    if not text:
        return None
    return urlsplit(text if "//" in text else "//" + text).hostname


def _domain(url: Any) -> str | None:
    host = _host(url)
    return registrable_domain(host) if host else None


def _path(url: Any) -> str | None:
    text = _text(url)
    return None if not text else urlsplit(text).path


def _param(url: Any, name: str) -> str | None:
    text = _text(url)
    if not text:
        return None
    values = parse_qs(urlsplit(text).query).get(str(name))
    return values[0] if values else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _hash(value: Any) -> str:
    return content_hash(value)


def _words(value: Any) -> int:
    text = _text(value)
    return 0 if not text else len(text.split())


#: The functions expressions may call. Every one tolerates ``None`` (see the module docs).
def _coordinates(value: Any) -> list[float] | None:
    """``[latitude, longitude]`` of a pair, text, ``{"latitude", "longitude"}``, a GeoJSON point or a map link."""
    from .places import coordinates_of

    found = coordinates_of(value)
    return list(found) if found else None


def _distance_km(a: Any, b: Any) -> float | None:
    """Kilometres between two points (anything :func:`_coordinates` reads); ``None`` when one has none."""
    from .places import distance_km

    return distance_km(a, b)


def _in_box(point: Any, south: Any, west: Any, north: Any, east: Any) -> bool | None:
    """Whether a point is within latitudes ``south``..``north`` and longitudes ``west``..``east``."""
    from .places import in_box

    for bound in (south, west, north, east):
        if isinstance(bound, bool) or not isinstance(bound, (int, float)):
            raise ExpressionError(f"in_box() takes numbers for its bounds, not {bound!r}")
    return in_box(point, south, west, north, east)


FUNCTIONS: dict[str, Callable[..., Any]] = {
    # text
    "len": _len,
    "lower": _lower,
    "upper": _upper,
    "title": _title,
    "strip": _strip,
    "clean": _clean,
    "contains": _contains,
    "icontains": _icontains,
    "startswith": _startswith,
    "endswith": _endswith,
    "matches": _matches,
    "extract": _extract,
    "replace": _replace,
    "sub": _sub,
    "split": _split,
    "join": _join,
    "words": _words,
    # values and lists
    "first": _first,
    "last": _last,
    "coalesce": _coalesce,
    "min": _min,
    "max": _max,
    "sum": _sum,
    "any": _any,
    "all": _all,
    "abs": _abs,
    "round": _round,
    "int": _int,
    "float": _float,
    "str": _str,
    "bool": _bool,
    # parsing (the data layer's normalizers)
    "number": _number,
    "integer": _integer,
    "money": _money,
    "currency": _currency,
    "date": _date,
    "datetime": _datetime,
    "boolean": _boolean,
    # places
    "coordinates": _coordinates,
    "distance_km": _distance_km,
    "in_box": _in_box,
    # URLs
    "host": _host,
    "domain": _domain,
    "path": _path,
    "param": _param,
    # misc
    "now": _now,
    "today": _today,
    "hash": _hash,
}
_REGEX_ARGS = {"matches": 1, "extract": 1, "sub": 1}  # function -> position of its pattern argument


# --------------------------------------------------------------------------- #
# operators with missing-data semantics
# --------------------------------------------------------------------------- #
def _arith(fn: Callable[[Any, Any], Any]) -> Callable[[Any, Any], Any]:
    def op(a: Any, b: Any) -> Any:
        if a is None or b is None:
            return None
        return fn(a, b)

    return op


def _divide(fn: Callable[[Any, Any], Any]) -> Callable[[Any, Any], Any]:
    def op(a: Any, b: Any) -> Any:
        if a is None or b is None:
            return None
        try:
            return fn(a, b)
        except ZeroDivisionError:
            return None

    return op


def _multiply(a: Any, b: Any) -> Any:
    if a is None or b is None:
        return None
    for sequence, count in ((a, b), (b, a)):
        if isinstance(sequence, (str, list, tuple)) and isinstance(count, int) and len(sequence) * count > MAX_REPEAT:
            raise ExpressionError(f"repetition would produce more than {MAX_REPEAT:,} items")
    return a * b


def _power(a: Any, b: Any) -> Any:
    if a is None or b is None:
        return None
    if not isinstance(b, (int, float)) or abs(b) > 1000:
        raise ExpressionError("exponent out of range (at most 1000)")
    if isinstance(a, int) and isinstance(b, int) and abs(a).bit_length() * abs(b) > 100_000:
        raise ExpressionError("result of ** too large")
    try:
        return a**b
    except ZeroDivisionError:
        return None


def _ordering(fn: Callable[[Any, Any], bool]) -> Callable[[Any, Any], bool]:
    def op(a: Any, b: Any) -> bool:
        if a is None or b is None:
            return False
        return fn(a, b)

    return op


def _contained(a: Any, b: Any) -> bool:
    if b is None or (a is None and isinstance(b, str)):
        return False
    return a in b


def _not_contained(a: Any, b: Any) -> bool:
    return not _contained(a, b)


def _negate(value: Any) -> Any:
    return None if value is None else -value


def _positive(value: Any) -> Any:
    return None if value is None else +value


_BINARY: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: _arith(operator.add),
    ast.Sub: _arith(operator.sub),
    ast.Mult: _multiply,
    ast.Div: _divide(operator.truediv),
    ast.FloorDiv: _divide(operator.floordiv),
    ast.Mod: _divide(operator.mod),
    ast.Pow: _power,
}
_COMPARE: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: _ordering(operator.lt),
    ast.LtE: _ordering(operator.le),
    ast.Gt: _ordering(operator.gt),
    ast.GtE: _ordering(operator.ge),
    ast.In: _contained,
    ast.NotIn: _not_contained,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}
_UNARY: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.Not: operator.not_,
    ast.USub: _negate,
    ast.UAdd: _positive,
}
_SYMBOLS = {
    ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.FloorDiv: "//", ast.Mod: "%", ast.Pow: "**",
    ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.In: "in",
    ast.NotIn: "not in", ast.Is: "is", ast.IsNot: "is not",
}  # fmt: skip


# --------------------------------------------------------------------------- #
# compiling: syntax tree -> closures
# --------------------------------------------------------------------------- #
class _Compiler:
    def __init__(self, source: str) -> None:
        self.source = source
        self.names: set[str] = set()

    def error(self, node: ast.AST | None, message: str) -> ExpressionError:
        where = f" (column {node.col_offset + 1})" if node is not None and hasattr(node, "col_offset") else ""
        return ExpressionError(f"{message}{where} in {self.source!r}", expression=self.source)

    def compile(self, node: ast.AST, depth: int = 0) -> Evaluator:
        if depth > MAX_DEPTH:
            raise self.error(node, "expression is nested too deeply")
        method = getattr(self, "_" + type(node).__name__, None)
        if method is None:
            raise self.error(node, f"{_describe(node)} is not allowed")
        result: Evaluator = method(node, depth + 1)
        return result

    # -- leaves ------------------------------------------------------------ #
    def _Constant(self, node: ast.Constant, depth: int) -> Evaluator:
        value = node.value
        if not (value is None or isinstance(value, (bool, int, float, str))):
            raise self.error(node, f"{type(value).__name__} literals are not allowed")
        return lambda record: value

    def _Name(self, node: ast.Name, depth: int) -> Evaluator:
        name = node.id  # always a field: functions are only looked up when called ("title" can be both)
        self.names.add(name)

        def load(record: Mapping[str, Any]) -> Any:
            return record.get(name)

        return load

    # -- containers -------------------------------------------------------- #
    def _items(self, nodes: list[ast.expr], depth: int) -> list[Evaluator]:
        out = []
        for element in nodes:
            if isinstance(element, ast.Starred):
                raise self.error(element, "* unpacking is not allowed")
            out.append(self.compile(element, depth))
        return out

    def _List(self, node: ast.List, depth: int) -> Evaluator:
        items = self._items(node.elts, depth)
        return lambda record: [item(record) for item in items]

    def _Tuple(self, node: ast.Tuple, depth: int) -> Evaluator:
        items = self._items(node.elts, depth)
        return lambda record: tuple(item(record) for item in items)

    def _Set(self, node: ast.Set, depth: int) -> Evaluator:
        items = self._items(node.elts, depth)

        def build(record: Mapping[str, Any]) -> Any:
            try:
                return {item(record) for item in items}
            except TypeError as exc:
                raise self.error(node, f"cannot build a set: {exc}") from None

        return build

    def _Dict(self, node: ast.Dict, depth: int) -> Evaluator:
        if any(key is None for key in node.keys):
            raise self.error(node, "** unpacking is not allowed")
        keys = [self.compile(key, depth) for key in node.keys if key is not None]
        values = [self.compile(value, depth) for value in node.values]

        def build(record: Mapping[str, Any]) -> Any:
            try:
                return {k(record): v(record) for k, v in zip(keys, values, strict=True)}
            except TypeError as exc:
                raise self.error(node, f"cannot build a dict: {exc}") from None

        return build

    # -- operators ----------------------------------------------------------- #
    def _BinOp(self, node: ast.BinOp, depth: int) -> Evaluator:
        fn = _BINARY.get(type(node.op))
        if fn is None:
            raise self.error(node, f"operator {type(node.op).__name__} is not allowed")
        left, right = self.compile(node.left, depth), self.compile(node.right, depth)
        symbol = _SYMBOLS[type(node.op)]

        def binary(record: Mapping[str, Any]) -> Any:
            a, b = left(record), right(record)
            try:
                return fn(a, b)
            except ExpressionError as exc:
                raise self.error(node, str(exc)) from None
            except (TypeError, ValueError, ArithmeticError) as exc:
                raise self.error(
                    node, f"cannot compute {_kind(a)} {symbol} {_kind(b)} ({type(exc).__name__}: {exc})"
                ) from None

        return binary

    def _UnaryOp(self, node: ast.UnaryOp, depth: int) -> Evaluator:
        fn = _UNARY.get(type(node.op))
        if fn is None:
            raise self.error(node, f"operator {type(node.op).__name__} is not allowed")
        operand = self.compile(node.operand, depth)

        def unary(record: Mapping[str, Any]) -> Any:
            value = operand(record)
            try:
                return fn(value)
            except TypeError as exc:
                raise self.error(node, f"bad operand {_kind(value)} ({exc})") from None

        return unary

    def _BoolOp(self, node: ast.BoolOp, depth: int) -> Evaluator:
        parts = [self.compile(value, depth) for value in node.values]
        if isinstance(node.op, ast.And):

            def all_of(record: Mapping[str, Any]) -> Any:
                value = None
                for part in parts:
                    value = part(record)
                    if not value:
                        return value
                return value

            return all_of

        def any_of(record: Mapping[str, Any]) -> Any:
            value = None
            for part in parts:
                value = part(record)
                if value:
                    return value
            return value

        return any_of

    def _Compare(self, node: ast.Compare, depth: int) -> Evaluator:
        operands = [node.left, *node.comparators]
        for op, left_node, right_node in zip(node.ops, operands, operands[1:], strict=False):
            if isinstance(op, (ast.Is, ast.IsNot)) and not (_is_singleton(left_node) or _is_singleton(right_node)):
                raise self.error(node, "'is' only compares with None, True or False; use == for values")
        left = self.compile(node.left, depth)
        steps = [
            (_COMPARE[type(op)], _SYMBOLS[type(op)], self.compile(comparator, depth))
            for op, comparator in zip(node.ops, node.comparators, strict=True)
        ]

        def compare(record: Mapping[str, Any]) -> bool:
            a = left(record)
            for fn, symbol, comparator in steps:
                b = comparator(record)
                try:
                    if not fn(a, b):
                        return False
                except TypeError:
                    raise self.error(node, f"cannot compare {_kind(a)} {symbol} {_kind(b)}") from None
                a = b
            return True

        return compare

    def _IfExp(self, node: ast.IfExp, depth: int) -> Evaluator:
        test, body, orelse = (self.compile(n, depth) for n in (node.test, node.body, node.orelse))
        return lambda record: body(record) if test(record) else orelse(record)

    # -- indexing -------------------------------------------------------------- #
    def _Subscript(self, node: ast.Subscript, depth: int) -> Evaluator:
        container = self.compile(node.value, depth)
        index = self.compile(node.slice, depth)

        def subscript(record: Mapping[str, Any]) -> Any:
            value = container(record)
            if value is None:
                return None
            key = index(record)
            try:
                return value[key]
            except (KeyError, IndexError):
                return None
            except (TypeError, ValueError) as exc:
                raise self.error(node, f"cannot index {_kind(value)} with {_kind(key)} ({exc})") from None

        return subscript

    def _Slice(self, node: ast.Slice, depth: int) -> Evaluator:
        parts = [self.compile(n, depth) if n is not None else None for n in (node.lower, node.upper, node.step)]

        def build(record: Mapping[str, Any]) -> slice:
            lower, upper, step = (p(record) if p is not None else None for p in parts)
            return slice(lower, upper, step)

        return build

    # -- calls ----------------------------------------------------------------- #
    def _Call(self, node: ast.Call, depth: int) -> Evaluator:
        if not isinstance(node.func, ast.Name):
            raise self.error(node, "only the built-in functions can be called")
        name = node.func.id
        if node.keywords:
            raise self.error(node, f"{name}(): keyword arguments are not supported")
        args = self._items(node.args, depth)
        if name == "get":
            return self._get(node, args)
        fn = FUNCTIONS.get(name)
        if fn is None:
            raise self.error(node, f"unknown function {name!r} (known: get, {', '.join(sorted(FUNCTIONS))})")
        try:
            inspect.signature(fn).bind(*args)
        except TypeError as exc:
            raise self.error(node, f"{name}(): {exc}") from None
        position = _REGEX_ARGS.get(name)
        if position is not None and len(node.args) > position:
            pattern = node.args[position]
            if isinstance(pattern, ast.Constant) and isinstance(pattern.value, str):
                try:
                    _regex(pattern.value)  # report a bad pattern now, not on the first record
                except ExpressionError as exc:
                    raise self.error(pattern, str(exc)) from None

        def call(record: Mapping[str, Any]) -> Any:
            values = [arg(record) for arg in args]
            try:
                return fn(*values)
            except ExpressionError as exc:
                raise self.error(node, f"{name}(): {exc}") from None
            except (TypeError, ValueError, ArithmeticError, re.error) as exc:
                raise self.error(node, f"{name}() failed on {', '.join(map(_kind, values))}: {exc}") from None

        return call

    def _get(self, node: ast.Call, args: list[Evaluator]) -> Evaluator:
        if not 1 <= len(args) <= 2:
            raise self.error(node, "get() takes a path and an optional default")
        path_node = node.args[0]
        if isinstance(path_node, ast.Constant) and isinstance(path_node.value, str):
            self.names.add(path_node.value)
        path, default = args[0], args[1] if len(args) == 2 else (lambda record: None)

        def get(record: Mapping[str, Any]) -> Any:
            return get_path(record, str(path(record)), default(record))

        return get


def _is_singleton(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and (node.value is None or node.value is True or node.value is False)


def _describe(node: ast.AST) -> str:
    names = {
        "Attribute": "attribute access (a.b)",
        "Lambda": "lambda",
        "ListComp": "a comprehension",
        "SetComp": "a comprehension",
        "DictComp": "a comprehension",
        "GeneratorExp": "a comprehension",
        "JoinedStr": "an f-string",
        "NamedExpr": "assignment (:=)",
        "Await": "await",
        "Yield": "yield",
        "YieldFrom": "yield",
        "Starred": "* unpacking",
    }
    return names.get(type(node).__name__, type(node).__name__)


def _kind(value: Any) -> str:
    if value is None:
        return "None"
    text = repr(value)
    if len(text) > 40:
        text = text[:37] + "..."
    return f"{type(value).__name__} {text}"


class Expression:
    """A compiled expression. Call it with a record (a mapping) to evaluate it.

    >>> is_deal = Expression("price < 20 and availability == 'in_stock'")
    >>> is_deal({"price": 12.5, "availability": "in_stock"})
    True
    >>> is_deal.names
    frozenset({'availability', 'price'})

    Raises:
        ExpressionError: For invalid syntax or disallowed constructs (when created),
            or a type mix-up in a record (when evaluated).
    """

    __slots__ = ("_evaluate", "names", "source")

    def __init__(self, source: str) -> None:
        if isinstance(source, Expression):
            source = source.source
        if not isinstance(source, str):
            raise ExpressionError(f"an expression must be a string, got {type(source).__name__}")
        source = source.strip()
        if not source:
            raise ExpressionError("empty expression", expression=source)
        if len(source) > MAX_LENGTH:
            raise ExpressionError(f"expression longer than {MAX_LENGTH:,} characters", expression=source[:80])
        try:
            tree = ast.parse(source, mode="eval")
        except SyntaxError as exc:
            where = f" (column {exc.offset})" if exc.offset else ""
            raise ExpressionError(f"invalid syntax{where} in {source!r}: {exc.msg}", expression=source) from None
        except (RecursionError, MemoryError, ValueError) as exc:
            raise ExpressionError(f"cannot parse {source!r}: {type(exc).__name__}", expression=source) from None
        compiler = _Compiler(source)
        self._evaluate = compiler.compile(tree.body)
        self.source = source
        #: The field names (and ``get`` paths) the expression reads.
        self.names = frozenset(compiler.names)

    def __call__(self, record: Mapping[str, Any] | None = None, /, **values: Any) -> Any:
        """Evaluate against ``record``; keyword arguments add (or override) names."""
        scope: Mapping[str, Any] = record if record is not None else {}
        if values:
            scope = {**scope, **values}
        return self._evaluate(scope)

    evaluate = __call__

    def __repr__(self) -> str:
        return f"Expression({self.source!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Expression) and other.source == self.source

    def __hash__(self) -> int:
        return hash(self.source)


@functools.lru_cache(maxsize=512)
def compile_expression(source: str) -> Expression:
    """:class:`Expression` with a cache (the same source compiles once)."""
    return Expression(source)
