"""The safe expression language behind filters, computed fields and rules."""

from __future__ import annotations

import re

import pytest

from wintergrab.data import Expression, compile_expression, get_path
from wintergrab.errors import ExpressionError

RECORD = {
    "price": 12.5,
    "sale_price": 10,
    "availability": "in_stock",
    "sku": "ABC-123",
    "url": "https://shop.example.co.uk/p/1?id=9",
    "tags": ["a", "b"],
    "offers": [{"price": "9.99"}],
    "list-price": 20,
    "title": "Big phone",
}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("price > 0 and availability == 'in_stock'", True),
        ("round(1 - sale_price / price, 2)", 0.2),
        ("coalesce(name, title, 'untitled')", "Big phone"),
        ("title(title)", "Big Phone"),  # a field and a function may share a name
        ("domain(url)", "example.co.uk"),
        ("host(url)", "shop.example.co.uk"),
        ("path(url)", "/p/1"),
        ("param(url, 'id')", "9"),
        ("matches(sku, '^[A-Z]{3}-[0-9]+$')", True),
        ("extract(sku, '[0-9]+')", "123"),
        ("extract(sku, '([A-Z]+)-')", "ABC"),
        ("tags[0] + tags[-1]", "ab"),
        ("tags[:1]", ["a"]),
        ("get('offers.0.price')", "9.99"),
        ("number(get('offers.0.price'))", 9.99),
        ("get('list-price')", 20),
        ("get('nothing.here', 'default')", "default"),
        ("len(tags)", 2),
        ("'a' in tags and 'z' not in tags", True),
        ("price if price > 100 else 'cheap'", "cheap"),
        ("min(price, sale_price, missing)", 10),
        ("max([1, 5, None])", 5),
        ("sum([1, None, 2])", 3),
        ("join(tags, ', ')", "a, b"),
        ("split('a, b ,c', ',')", ["a", "b", "c"]),
        ("money('₹29,999')", 29999),
        ("currency('₹29,999')", "INR"),
        ("date('3 March 2024')", "2024-03-03"),
        ("{'a': 1}['a']", 1),
        ("x in {'a', 'b'}", False),
        ("icontains(availability, 'STOCK')", True),
        ("int('12.7') + int('3')", 15),
        ("2 ** 10", 1024),
        ("-price", -12.5),
        ("not availability", False),
        ("words('hello big world')", 3),
        ("hash('x') == hash('X')", True),  # normalized like content hashes
    ],
)
def test_evaluation(source: str, expected) -> None:
    assert Expression(source)(RECORD) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("missing > 3", False),  # ordering with a missing value is false
        ("missing < 3", False),
        ("missing + 1", None),  # arithmetic with a missing value is missing
        ("price / 0", None),  # so is division by zero
        ("price % 0", None),
        ("tags[5]", None),  # past the end
        ("{'a': 1}['b']", None),
        ("missing[0]", None),
        ("len(missing)", 0),
        ("missing is None", True),
        ("missing == None", True),
        ("-missing", None),
        ("'a' in missing", False),
        ("lower(missing)", None),
        ("contains(missing, 'x')", False),
    ],
)
def test_missing_values_behave_like_sql_null(source: str, expected) -> None:
    assert Expression(source)(RECORD) == expected


def test_names_lists_the_fields_read() -> None:
    assert Expression("price > 0 and availability == 'x'").names == frozenset({"price", "availability"})
    assert Expression("len(tags) + get('offers.0.price')").names == frozenset({"tags", "offers.0.price"})
    assert Expression("now()").names == frozenset()


def test_extra_names_and_caching() -> None:
    expression = Expression("value * 2 + price")
    assert expression(RECORD, value=3) == 18.5
    assert expression(value=1, price=1) == 3
    assert compile_expression("price > 1") is compile_expression("price > 1")
    assert Expression(" price ") == Expression("price") and hash(Expression("a")) == hash(Expression("a"))


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("price.__class__", "attribute access"),
        ("().__class__.__bases__", "attribute access"),
        ("__import__('os')", "unknown function '__import__'"),
        ("open('/etc/passwd')", "unknown function 'open'"),
        ("[x for x in tags]", "comprehension"),
        ("lambda: 1", "lambda"),
        ("f'{price}'", "f-string"),
        ("(price := 3)", "assignment"),
        ("x is 'a'", "'is' only compares"),
        ("len()", "missing a required argument"),
        ("len(1, 2)", "too many"),
        ("matches(sku, '(')", "invalid regular expression"),
        ("get()", "get() takes"),
        ("tags(1)", "unknown function 'tags'"),
        ("lower(x=1)", "keyword arguments"),
        ("b'bytes'", "bytes literals"),
        ("len(*tags)", "unpacking"),
        ("{**x}", "unpacking"),
        ("price >", "invalid syntax"),
        ("", "empty expression"),
        ("a" + "+a" * 150, "nested too deeply"),
        ("1" * 20_000, "longer than"),
    ],
)
def test_rejected_at_compile_time(source: str, message: str) -> None:
    with pytest.raises(ExpressionError, match=re.escape(message)):
        Expression(source)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("price > '3'", "cannot compare float 12.5 > str '3'"),
        ("'12.99' + 1", "cannot compute str '12.99' + int 1"),
        ("'a' * 10 ** 7", "repetition"),
        ("[1] * 10 ** 7", "repetition"),
        ("2 ** 10000", "exponent"),
        ("10 ** 900 ** 2", "exponent"),
        ("int('abc')", "int() failed"),
        ("tags['x']", "cannot index"),
        ("{[1]: 2}", "cannot build a dict"),
    ],
)
def test_type_mixups_raise_when_evaluated(source: str, message: str) -> None:
    expression = Expression(source)  # compiles fine
    with pytest.raises(ExpressionError, match=re.escape(message)) as info:
        expression(RECORD)
    assert info.value.expression == source.strip()
    assert info.value.category == "expression"


def test_expressions_cannot_reach_python_objects() -> None:
    # Records may hold arbitrary objects; without attribute access or unknown
    # functions there is no path from them to modules, files or builtins.
    class Sneaky:
        secret = "hidden"

    record = {"obj": Sneaky()}
    for source in ("obj.secret", "getattr(obj, 'secret')", "vars(obj)", "type(obj)", "obj.__dict__"):
        with pytest.raises(ExpressionError):
            Expression(source)(record)
    assert Expression("str(obj) != ''")(record) is True  # values can still be read as text


def test_get_path() -> None:
    record = {"a": {"b": [{"c": 1}, {"c": 2}]}, "d.e": 3, "n": None}
    assert get_path(record, "a.b.1.c") == 2
    assert get_path(record, "a.b.-1.c") == 2
    assert get_path(record, "d.e") == 3  # an exact key wins
    assert get_path(record, "a.b.5.c", "x") == "x"
    assert get_path(record, "n", "x") == "x"
    assert get_path(record, "a.b.c") is None
