"""CSS to XPath translation with Scrapy-style ``::text`` and ``::attr(name)``."""

from __future__ import annotations

import re
from functools import lru_cache

from cssselect import GenericTranslator, HTMLTranslator
from cssselect.parser import FunctionalPseudoElement, SelectorError
from cssselect.xpath import ExpressionError, XPathExpr

from ..errors import SelectorSyntaxError


class _SuffixedXPathExpr(XPathExpr):
    """An XPath expression ending in ``text()`` or ``@attr`` (parsel-compatible).

    ``h1 ::text`` (note the space) becomes ``h1/descendant-or-self::text()`` so
    it returns every text node inside ``<h1>``, as in Scrapy/parsel.
    """

    def __init__(self, base: XPathExpr, *, text: bool = False, attribute: str | None = None) -> None:
        super().__init__(base.path, base.element, base.condition)
        self.text = text
        self.attribute = attribute

    def __str__(self) -> str:
        path = super().__str__()
        if self.text:
            if path == "*":
                path = "text()"
            elif path.endswith("::*/*"):
                path = path[:-3] + "text()"
            else:
                path += "/text()"
        if self.attribute is not None:
            if path.endswith("::*/*"):
                path = path[:-2]
            path += f"/@{self.attribute}"
        return path


class _PseudoElementMixin:
    def xpath_pseudo_element(self, xpath, pseudo_element):  # type: ignore[no-untyped-def]
        if isinstance(pseudo_element, FunctionalPseudoElement):
            name = pseudo_element.name.lower()
            if name != "attr":
                raise ExpressionError(f"unknown pseudo-element ::{name}()")
            args = [tok.value for tok in pseudo_element.arguments if tok.type in ("IDENT", "STRING")]
            if len(args) != 1 or not re.fullmatch(r"[\w:.-]+", str(args[0])):
                raise ExpressionError("::attr() takes exactly one attribute name")
            return _SuffixedXPathExpr(xpath, attribute=str(args[0]))
        name = str(pseudo_element).lower()
        if name == "text":
            return _SuffixedXPathExpr(xpath, text=True)
        raise ExpressionError(f"unknown pseudo-element ::{name}")


class _HTMLTranslator(_PseudoElementMixin, HTMLTranslator):
    pass


class _XMLTranslator(_PseudoElementMixin, GenericTranslator):
    pass


_html_translator = _HTMLTranslator()
_xml_translator = _XMLTranslator()

# A trailing pseudo-element, e.g. "div.price::text" or "a::attr(href)".
_PSEUDO_RE = re.compile(r"^(?P<element>.*?)(?P<pseudo>::(?:text|attr\(\s*['\"]?[\w:.-]+['\"]?\s*\)))\s*$", re.S)
# A trailing text/attribute step in XPath, e.g. "//a/@href" or "//h1//text()".
_XPATH_TAIL_RE = re.compile(r"^(?P<element>.*?)(?P<pseudo>/{1,2}(?:text\(\)|@[\w:.-]+))\s*$", re.S)


@lru_cache(maxsize=2048)
def css_to_xpath(query: str, xml: bool = False) -> str:
    """Translate a CSS selector (with optional ``::text``/``::attr()``) to XPath."""
    translator = _xml_translator if xml else _html_translator
    try:
        return translator.css_to_xpath(query)
    except (SelectorError, ExpressionError) as exc:
        raise SelectorSyntaxError(f"Invalid CSS selector {query!r}: {exc}") from None


def split_css_pseudo(query: str) -> tuple[str, str | None]:
    """Split ``"a.link::attr(href)"`` into ``("a.link", "/@href")``.

    Returns the element part of the selector and the XPath suffix that turns an
    element into the requested text/attribute (``None`` if there is none).
    Selector groups (commas) with pseudo-elements are left untouched.
    """
    match = _PSEUDO_RE.match(query)
    if not match or "," in match.group("element") or "::" in match.group("element"):
        return query, None
    element, pseudo = match.group("element"), match.group("pseudo")
    # "div ::text" / "div *::text" mean "inside div, at any depth" (parsel
    # semantics), while "div > *::text" means "in div's children".
    stripped = element.rstrip()
    star = stripped.endswith("*") and (len(stripped) == 1 or stripped[-2].isspace() or stripped[-2] in ">+~")
    before = stripped[:-1] if star else element
    deep = False
    if before != before.rstrip() or (star and not before):
        base = before.rstrip()
        if not base:
            return query, None  # "*::text": nothing to fingerprint
        if base[-1] in ">+~":
            element = base + " *"
        else:
            element, deep = base, True
    if pseudo == "::text":
        return element, "/descendant-or-self::text()" if deep else "/text()"
    attr = re.search(r"\(\s*['\"]?([\w:.-]+)['\"]?\s*\)", pseudo)
    if not attr:
        return query, None
    return element, (f"/descendant-or-self::*/@{attr.group(1)}" if deep else f"/@{attr.group(1)}")


def split_xpath_tail(query: str) -> tuple[str, str | None]:
    """Split ``"//a/@href"`` into ``("//a", "/@href")``."""
    match = _XPATH_TAIL_RE.match(query)
    if not match or not match.group("element").strip() or "|" in query:
        return query, None
    return match.group("element"), match.group("pseudo")


def looks_like_xpath(query: str) -> bool:
    """Heuristic used by :meth:`Selector.select` and extraction schemas."""
    q = query.lstrip()
    return q.startswith(("/", "./", "(", "..")) or q.startswith("xpath:")
