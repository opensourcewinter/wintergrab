"""Zero-selector scraping: find the repeating records on a page and learn selectors from examples.

* :func:`auto_extract` returns the page's main list of records (product cards, search results,
  table rows...) as dicts - without writing a single selector.
* :func:`detect_records` exposes the ranked candidate lists, each with a CSS selector for the
  records and a relative ``::text`` / ``::attr()`` selector for every inferred field.
* :func:`learn_schema` learns reusable selectors from one (or a few) example records and applies
  them to any other page built on the same template.

Every function accepts an lxml element (``wintergrab.parse(html, url=...).root``), a
:class:`~wintergrab.Selector`, a :class:`~wintergrab.Response` or raw HTML.
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urljoin

from lxml import etree

from ..adaptive.fingerprint import similar_elements
from ..errors import SelectorSyntaxError
from .css import css_to_xpath, looks_like_xpath
from .normalize import clean_record
from .selector import Selector, parse_document
from .text import SKIP_TAGS, normalize_space, own_text, tag_name, text_content

__all__ = ["LearnedSchema", "RecordGroup", "auto_extract", "detect_records", "is_stable_class", "learn_schema"]

# Minimum score for auto_extract() to trust the best group (tuned on the test fixtures).
_MIN_AUTO_SCORE = 2.0
_MAX_DEPTH = 8  # how deep inside a record fields are looked for
_MAX_NODES = 400  # elements per record considered for field inference
_MAX_CANDIDATES = 60  # example-value matches considered per field

_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_WRAPPER_TAGS = frozenset({"div", "article", "section", "figure", "li", "main"})
_LANDMARK_TAGS = frozenset({"main", "article", "section", "table", "ol", "ul", "form"})
_TEXT_TAGS = _HEADINGS | {"p", "span", "em", "strong", "b", "i", "u", "small", "label", "code", "pre", "font"}
_NOT_MEMBERS = SKIP_TAGS | {
    "td", "th", "thead", "tbody", "tfoot", "caption", "col", "colgroup", "br", "hr", "wbr", "input", "meta",
    "link", "source", "track", "param", "area", "option", "optgroup", "head", "title",
}  # fmt: skip
_NOT_SEARCHED = frozenset({"script", "style", "noscript", "template"})
_BOILER_TAGS = frozenset({"nav", "header", "footer", "aside", "menu"})
_BOILER_ROLES = frozenset({"navigation", "menu", "menubar", "banner", "contentinfo", "complementary", "tablist"})
_BOILER_RE = re.compile(
    r"(?:^|[\s_-])(?:nav|navbar|navigation|menu|menubar|submenu|breadcrumbs?|pagination|pager|paging|footer|"
    r"header|masthead|sidebar|toolbar|social|share|sharing|tabs|topbar|dropdown|cookies?)(?:$|[\s_-])",
    re.I,
)
_CURRENCY = r"(?:US\$|C\$|A\$|R\$|[$€£¥₹₩₽₺₪฿₫]|\b(?:USD|EUR|GBP|JPY|INR|CAD|AUD|CHF|CNY|Rs\.?))"
_PRICE_RE = re.compile(rf"{_CURRENCY}\s?\d[\d,.]*|\d[\d,.]*\s?{_CURRENCY}")
_RATING_TEXT_RE = re.compile(r"\b\d(?:[.,]\d+)?\s*(?:out of|/)\s*(?:5|10)\b|\b\d(?:[.,]\d+)?\s*stars?\b", re.I)
_RATING_CLASS_RE = re.compile(r"rating|(?:^|[\s_-])stars?(?:$|[\s_-])", re.I)
# Stock wording ("In stock", "Only 3 left") or a stock-ish class; not "Add to basket" buttons.
_STOCK_TEXT_RE = re.compile(
    r"in[\s-]*stock|out[\s-]*of[\s-]*stock|sold[\s-]*out|\b(?:un)?availab|pre-?order|back-?order|\bleft\b", re.I
)
_STOCK_CLASS_RE = re.compile(r"availab|stock", re.I)
_IMAGE_ATTRS = ("data-src", "data-lazy-src", "data-original", "src", "srcset", "data-srcset")
_URL_ATTRS = frozenset(
    {"href", "src", "srcset", "data-src", "data-lazy-src", "data-original", "data-srcset", "poster", "action"}
)
# Attributes an example value may be found in, most likely first (``data-*`` rank just after these).
_MATCH_ATTRS = ("href", "src", "title", "alt", "content", "value", "srcset", "aria-label", "datetime")
_ATTR_PRIORITY = {name: i for i, name in enumerate(_MATCH_ATTRS)}

_IDENT_RE = re.compile(r"-?[A-Za-z_][A-Za-z0-9_-]*")
_GENERATED_PREFIX_RE = re.compile(r"(?:css|jsx|sc|svelte|emotion|jss|styled|astro)-(?P<rest>[A-Za-z0-9_-]+)")
_CASE_RUN_RE = re.compile(r"[A-Z]?[a-z]*")
_WORD_NUMBER_RE = re.compile(r"[A-Za-z]+\d{1,3}|\d{1,3}[a-z]+")
_UTILITY_CLASS_RE = re.compile(
    r"(?:col|row|text|font|fw|fs|lh|bg|btn|d|flex|grid|gap|w|h|[mp][tblrxyse]?|align|justify|order|float|pull|"
    r"fa|fas|far|icon|glyphicon|border|rounded|shadow|sm|md|lg|xl|xs|visible|hidden|clearfix)(?:-[A-Za-z0-9]+)*"
)
_GENERIC_CLASSES = frozenset(
    {"item", "block", "container", "wrapper", "wrap", "inner", "outer", "content", "active", "first", "last", "odd",
     "even", "selected", "disabled", "left", "right", "center", "clear", "row", "col"}
)  # fmt: skip
_FIELD_RE = re.compile(
    r"^(?P<element>.*?)(?P<space>\s*)::(?:text|attr\(\s*['\"]?(?P<attr>[\w:.-]+)['\"]?\s*\))\s*$", re.S
)


# --------------------------------------------------------------------------- #
# class tokens
# --------------------------------------------------------------------------- #


def is_stable_class(token: str) -> bool:
    """Whether a class token looks hand-written (``product_pod``) rather than generated by a build tool.

    Generated tokens contain 5+ digits, carry a CSS-in-JS prefix with a hash (``css-1x2y3z``, ``sc-AbCd``,
    ``jsx-123``), end in a CSS-modules hash (``Card_title__x1Y2z``) or contain a random-looking chunk
    (``a1b2c3``, ``kQzXpL``).
    """
    if not token or len(token) > 64 or sum(ch.isdigit() for ch in token) >= 5:
        return False
    prefixed = _GENERATED_PREFIX_RE.fullmatch(token)
    if prefixed and (any(ch.isdigit() for ch in prefixed["rest"]) or _mixed_case(prefixed["rest"])):
        return False
    if "__" in token:
        tail = token.rsplit("__", 1)[1]
        if any(ch.isdigit() for ch in tail) or _random_chunk(tail, min_len=5):
            return False
    return not any(_random_chunk(part) for part in re.split(r"[-_]+", token))


def _mixed_case(text: str) -> bool:
    return any(ch.isupper() for ch in text) and any(ch.islower() for ch in text)


def _random_chunk(part: str, min_len: int = 6) -> bool:
    """A chunk like ``a1b2c3`` or ``kQzXpL``: letters/digits or letter cases mixed at random."""
    if len(part) < 5:
        return False
    has_digit = any(ch.isdigit() for ch in part)
    if has_digit and any(ch.isalpha() for ch in part):
        if len(re.findall(r"\d+", part)) >= 2:
            return True
        return len(part) >= min_len and not _WORD_NUMBER_RE.fullmatch(part)
    if not has_digit and len(part) >= min_len and _mixed_case(part):
        runs = [run for run in _CASE_RUN_RE.findall(part) if run]  # "kQzXpL" -> k, Qz, Xp, L
        return sum(len(run) <= 2 for run in runs) * 2 > len(runs)
    return False


@lru_cache(maxsize=4096)
def _stable_tokens(class_attr: str) -> tuple[str, ...]:
    out: list[str] = []
    for token in class_attr.split():
        if token not in out and _IDENT_RE.fullmatch(token) and is_stable_class(token):
            out.append(token)
    return tuple(out)


def _classes(el: etree._Element) -> tuple[str, ...]:
    """Stable, selector-safe class tokens of ``el`` in document order."""
    return _stable_tokens(el.get("class") or "")


def _is_generic(token: str) -> bool:
    return token.lower() in _GENERIC_CLASSES or _UTILITY_CLASS_RE.fullmatch(token) is not None


def _class_rank(token: str) -> tuple[bool, int, str]:
    """Sort key putting the most descriptive class first (non-utility, then longest)."""
    return (_is_generic(token), -len(token), token)


def _signature(el: etree._Element, allowed: Collection[str] | None = None) -> str:
    """``tag.class1.class2`` using stable classes (optionally only those in ``allowed``)."""
    tokens = sorted(t for t in _classes(el) if allowed is None or t in allowed)
    return ".".join((tag_name(el), *tokens))


# --------------------------------------------------------------------------- #
# tree helpers
# --------------------------------------------------------------------------- #


def _children(el: etree._Element) -> list[etree._Element]:
    return [c for c in el if isinstance(c.tag, str)]


def _iter_elements(root: etree._Element, skip: Collection[str]) -> Iterator[etree._Element]:
    """Elements under ``root`` (inclusive) in document order, not descending into ``skip`` tags."""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(c for c in reversed(node) if isinstance(c.tag, str) and tag_name(c) not in skip)


def _is_inside(el: etree._Element, container: etree._Element) -> bool:
    return el is container or any(a is container for a in el.iterancestors())


def _owner(el: etree._Element, members: Collection[etree._Element]) -> etree._Element | None:
    """The element of ``members`` that ``el`` is (or is inside), if any."""
    if el in members:
        return el
    return next((a for a in el.iterancestors() if a in members), None)


def _lca(elements: Sequence[etree._Element]) -> etree._Element:
    """Lowest common ancestor (or self) of ``elements``."""
    chains = [[*reversed(list(el.iterancestors())), el] for el in elements]
    common = chains[0][0]
    for nodes in zip(*chains, strict=False):
        if any(node is not nodes[0] for node in nodes):
            break
        common = nodes[0]
    return common


def _distance(a: etree._Element, b: etree._Element) -> int:
    """Number of tree edges between two elements."""
    if a is b:
        return 0
    up_b = {node: i for i, node in enumerate([b, *b.iterancestors()])}
    for i, node in enumerate([a, *a.iterancestors()]):
        if node in up_b:
            return i + up_b[node]
    return 10_000


def _has_image(el: etree._Element) -> bool:
    return tag_name(el) == "img" or el.find(".//img") is not None


def _body(root: etree._Element) -> etree._Element:
    if tag_name(root) != "html":
        return root
    body = root.find("body")
    return body if body is not None else root


def _resolve(source: Any) -> tuple[etree._Element, str | None]:
    """Turn an lxml element, Selector, Response or markup into ``(element, page url)``."""
    if isinstance(source, etree._Element):
        return source, None
    if isinstance(source, (str, bytes)):
        return parse_document(source), None
    selector = getattr(source, "selector", None)
    holder = selector if getattr(selector, "root", None) is not None else source
    root = getattr(holder, "root", None)
    if not isinstance(root, etree._Element):
        raise TypeError(f"expected an lxml element, Selector, Response or HTML string, got {type(source).__name__}")
    url = getattr(source, "url", None) or getattr(holder, "url", None)
    return root, url if isinstance(url, str) else None


def _document_base(root: etree._Element, url: str | None) -> str | None:
    """Base URL for relative links: the page URL (or the parser's), adjusted by ``<base href>``."""
    tree = root.getroottree()
    base = url or tree.docinfo.URL
    found = tree.getroot().xpath("//base/@href")
    if found:
        try:
            base = urljoin(base or "", str(found[0]).strip()) or base
        except ValueError:
            pass
    return base or None


def _join(base: str | None, value: str) -> str:
    if not base:
        return value
    try:
        return urljoin(base, value)
    except ValueError:
        return value


# --------------------------------------------------------------------------- #
# selectors: generation, verification, extraction
# --------------------------------------------------------------------------- #


def _select(scope: etree._Element, query: str) -> list[etree._Element]:
    """Elements matched by a CSS (or XPath) query, in document order. Raises on invalid queries."""
    if query.startswith("css:"):
        query = query[4:]
    if looks_like_xpath(query):
        xpath = query[6:] if query.startswith("xpath:") else query
        try:
            nodes = scope.xpath(xpath)
        except etree.XPathError as exc:
            raise SelectorSyntaxError(f"Invalid XPath {query!r}: {exc}") from None
    else:
        nodes = scope.xpath(css_to_xpath(query))
    return [n for n in nodes if isinstance(n, etree._Element)] if isinstance(nodes, list) else []


def _first(scope: etree._Element, query: str) -> etree._Element | None:
    try:
        matched = _select(scope, query)
    except SelectorSyntaxError:
        return None
    return matched[0] if matched else None


def _step_variants(el: etree._Element, allowed: Collection[str] | None) -> list[str]:
    """Ways to address ``el`` on its own: ``tag.descriptive-class`` first, then ``tag``, ``tag.utility-class``..."""
    tag = tag_name(el)
    classes = sorted((c for c in _classes(el) if allowed is None or c in allowed), key=_class_rank)[:3]
    out = [f"{tag}.{c}" for c in classes if not _is_generic(c)]
    out += [tag, *(f"{tag}.{c}" for c in classes if _is_generic(c))]
    if len(classes) > 1:
        out.append(tag + "".join(f".{c}" for c in classes))
    prop = el.get("itemprop")
    if prop and _IDENT_RE.fullmatch(prop):
        out.append(f'{tag}[itemprop="{prop}"]')
    el_id = el.get("id")
    if el_id and _IDENT_RE.fullmatch(el_id) and not any(ch.isdigit() for ch in el_id):
        out.append(f"#{el_id}")
    return out


def _nth_step(el: etree._Element) -> str:
    tag = tag_name(el)
    index = 1 + sum(1 for s in el.itersiblings(preceding=True) if isinstance(s.tag, str) and tag_name(s) == tag)
    return f"{tag}:nth-of-type({index})"


def _selector_candidates(
    scope: etree._Element, target: etree._Element, allowed: Collection[str] | None
) -> Iterator[str]:
    """Selectors that may find ``target`` from ``scope``, roughly shortest first."""
    own = _step_variants(target, allowed)
    yield from own
    if target is scope:
        return
    chain: list[etree._Element] = []  # ancestors of target below scope, nearest first
    node = target.getparent()
    while node is not None and node is not scope:
        chain.append(node)
        node = node.getparent()
    if node is None:
        return
    ancestors = [_step_variants(a, allowed) for a in chain[:6]]
    for variants in ancestors:
        for anc in variants:
            for step in own:
                yield f"{anc} {step}"
    for anc in _step_variants(target.getparent(), allowed):  # direct children only: "article > p"
        for step in own[:2]:
            yield f"{anc} > {step}"
    previous = target.getprevious()
    while previous is not None and not isinstance(previous.tag, str):
        previous = previous.getprevious()
    if previous is not None:  # label -> value pairs: "th + td", "h2.label + p"
        for before in _step_variants(previous, allowed)[:3]:
            for step in own[:2]:
                yield f"{before} + {step}"
    nth = _nth_step(target)
    yield nth
    for variants in ancestors:
        for anc in variants:
            yield f"{anc} {nth}"
    yield ":scope > " + " > ".join(_nth_step(el) for el in [*reversed(chain), target])


def _pick_selector(
    scopes: Sequence[etree._Element],
    targets: Sequence[etree._Element | None],
    allowed: Collection[str] | None,
    must: Collection[int] = (0,),
) -> str | None:
    """Simplest selector whose first match inside each scope is that scope's target.

    Scopes in ``must`` have to be right; for the others the selector with the best hit rate wins
    (matching something in a scope whose target is ``None`` counts as a miss). Among perfect
    selectors, one matching only the target is preferred over the first that merely finds it first.
    """
    required = [i for i in must if targets[i] is not None]
    if not required:
        required = [i for i, t in enumerate(targets) if t is not None][:1]
    if not required:
        return None
    primary = required[0]
    target = targets[primary]
    assert target is not None
    best, best_rate = None, -1.0
    first_perfect: str | None = None
    tried: set[str] = set()
    for query in _selector_candidates(scopes[primary], target, allowed):
        if query in tried:
            continue
        tried.add(query)
        if any(_first(scopes[i], query) is not targets[i] for i in required):
            continue
        good = bad = 0
        for i, (scope, expected) in enumerate(zip(scopes, targets, strict=True)):
            if i in required:
                continue
            hit = _first(scope, query)
            if expected is None:
                bad += hit is not None
            elif hit is expected:
                good += 1
            else:
                bad += 1
        rate = good / (good + bad) if good + bad else 1.0
        if rate >= 0.999:
            if query.startswith(":scope"):  # last resort: a positional path
                return first_perfect or query
            if len(_select(scopes[primary], query)) == 1:
                return query
            first_perfect = first_perfect or query
        elif rate > best_rate:
            best, best_rate = query, rate
    return first_perfect or best


def _anchor_variants(node: etree._Element, root: etree._Element) -> list[str]:
    """Stable handles for an ancestor: a unique stable ``#id`` and/or ``tag.class``."""
    out: list[str] = []
    node_id = node.get("id")
    if (
        node_id
        and _IDENT_RE.fullmatch(node_id)
        and is_stable_class(node_id)
        and len(root.xpath("//*[@id=$v]", v=node_id)) == 1
    ):
        out.append(f"#{node_id}")
    classes = _classes(node)
    tag = tag_name(node)
    if classes:
        out.append(f"{tag}.{min(classes, key=_class_rank)}")
        if len(classes) > 1:
            out.append(tag + "".join(f".{c}" for c in classes[:4]))
    elif tag in _LANDMARK_TAGS and len(root.xpath(f"//{tag}")) == 1:
        out.append(tag)  # the page's only <main>, <table>...
    return out


def _container_selector(members: Sequence[etree._Element], root: etree._Element) -> str:
    """A CSS selector matching exactly ``members`` (or as close as possible) in the document."""
    wanted = set(members)
    first = members[0]
    tag = tag_name(first)
    shared = [c for c in _classes(first) if all(c in _classes(m) for m in members[1:])]
    base = tag + "".join(f".{c}" for c in shared[:3])
    bases = [base, f"{base}:has(> td)"] if tag == "tr" else [base]  # skip header rows
    candidates: list[str] = list(bases) if shared else []
    steps: list[str] = []  # tags between the anchor and the members
    node = first.getparent()
    while node is not None and tag_name(node) != "html" and len(steps) < 8:
        for anchor in _anchor_variants(node, root):
            middle = "".join(f"{s} > " for s in steps)
            for b in bases:
                candidates.append(f"{anchor} > {middle}{b}")
                if steps:
                    candidates.append(f"{anchor} {b}")
        steps.insert(0, tag_name(node))
        node = node.getparent()
    if not shared:
        candidates.extend(bases)
    # Structural fallback: absolute path to the members' common ancestor.
    common = _lca(members) if len(members) > 1 else first.getparent()
    if common is not None and common is not first:
        path = Selector(root=common).css_path
        between = [tag_name(a) for a in reversed(list(first.iterancestors())) if _is_inside(a, common)][1:]
        middle = "".join(f"{t} > " for t in between)
        candidates.extend(f"{path} > {middle}{b}" for b in bases)

    best, best_key = base, (2, 0)
    for query in dict.fromkeys(candidates):
        try:
            got = set(_select(root, query))
        except SelectorSyntaxError:
            continue
        if got == wanted:
            return query
        key = (0, len(got - wanted)) if wanted <= got else (1, len(wanted - got))
        if key < best_key:
            best, best_key = query, key
    return best


def _clean_attr(name: str, raw: str | None, base: str | None) -> str | None:
    value = normalize_space(raw or "")
    if not value:
        return None
    if name.endswith("srcset"):
        value = value.split(",")[0].strip().split(" ")[0]
    if name in _URL_ATTRS and not value.startswith(("data:", "javascript:", "mailto:", "tel:")):
        value = _join(base, value)
    return value


def _extract_value(scope: etree._Element, query: str, base: str | None) -> str | None:
    """Evaluate one field selector inside ``scope`` and return a clean string (or ``None``).

    ``sel::text`` gives the matched element's own text (its full text if it has none), ``sel ::text``
    its full text, ``sel::attr(name)`` the attribute (URLs made absolute). XPath is accepted too.
    """
    if looks_like_xpath(query):
        xpath = query[6:] if query.startswith("xpath:") else query
        try:
            nodes = scope.xpath(xpath)
        except etree.XPathError as exc:
            raise SelectorSyntaxError(f"Invalid XPath {query!r}: {exc}") from None
        first = (nodes[0] if nodes else None) if isinstance(nodes, list) else nodes
        if first is None:
            return None
        if isinstance(first, etree._Element):
            return text_content(first) or None
        tail = re.search(r"@([\w:.-]+)\s*$", xpath)
        return _clean_attr(tail.group(1) if tail else "", str(first), base)
    match = _FIELD_RE.match(query)
    element_query = match["element"].strip() if match else query
    if element_query:
        found = _select(scope, element_query)
        if not found:
            return None
        node = found[0]
    else:
        node = scope
    if match and match["attr"]:
        attr = match["attr"]
        if match["space"]:  # "sel ::attr(x)": the first element at any depth that has it
            node = next((d for d in node.iter() if isinstance(d.tag, str) and d.get(attr) is not None), node)
        return _clean_attr(attr, node.get(attr), base)
    text = (own_text(node) or text_content(node)) if match and not match["space"] else text_content(node)
    return text or None


def _record(scope: etree._Element, fields: Mapping[str, str], base: str | None) -> dict[str, str | None]:
    return {name: _extract_value(scope, query, base) for name, query in fields.items()}


# --------------------------------------------------------------------------- #
# record detection
# --------------------------------------------------------------------------- #


def _need(n: int) -> int:
    """How many of ``n`` records must share something for it to count as consistent (60%)."""
    return 1 if n <= 1 else max(2, math.ceil(0.6 * n))


def _vocab(members: Sequence[etree._Element]) -> frozenset[str]:
    """Class tokens used in most records - the structural ones (``star-rating``, not ``Three``)."""
    counts: Counter[str] = Counter()
    for member in members:
        tokens: set[str] = set()
        for el in member.iter():
            if isinstance(el.tag, str):
                tokens.update(_classes(el))
        counts.update(tokens)
    need = _need(len(members))
    return frozenset(t for t, c in counts.items() if c >= need)


def _member_paths(member: etree._Element, vocab: Collection[str]) -> dict[tuple[str, ...], etree._Element]:
    """Map each descendant's relative path (``("div.product_price[0]", "p.price_color[0]")``) to it."""
    out: dict[tuple[str, ...], etree._Element] = {}
    stack: list[tuple[etree._Element, tuple[str, ...]]] = [(member, ())]
    while stack:
        node, path = stack.pop()
        out[path] = node
        if len(path) >= _MAX_DEPTH or len(out) >= _MAX_NODES:
            continue
        seen: Counter[str] = Counter()
        kids: list[tuple[etree._Element, tuple[str, ...]]] = []
        for child in node:
            if not isinstance(child.tag, str) or tag_name(child) in SKIP_TAGS:
                continue
            step = _signature(child, vocab)
            kids.append((child, (*path, f"{step}[{seen[step]}]")))
            seen[step] += 1
        stack.extend(reversed(kids))
    return out


def _path_tag(path: tuple[str, ...], group: _Group) -> str:
    if not path:
        return tag_name(group.members[0])
    return re.split(r"[.\[]", path[-1], maxsplit=1)[0]


class _Group:
    """Candidate records plus the per-record structure shared by scoring and field inference."""

    __slots__ = ("field_count", "members", "paths", "vocab")

    def __init__(self, members: list[etree._Element]) -> None:
        self.members = members
        self.vocab = _vocab(members)
        self.paths = [_member_paths(m, self.vocab) for m in members]
        self.field_count = 0

    def column(self, path: tuple[str, ...]) -> list[etree._Element | None]:
        return [paths.get(path) for paths in self.paths]


def _label_row(tr: etree._Element) -> bool:
    """``<tr><th>UPC</th><td>a897fe39</td></tr>``: one item's attribute, not a record of a list."""
    cells = [c for c in tr if isinstance(c.tag, str) and tag_name(c) in ("th", "td")]
    return len(cells) == 2 and tag_name(cells[0]) == "th" and tag_name(cells[1]) == "td"


def _candidate_groups(body: etree._Element, min_records: int) -> list[list[etree._Element]]:
    """Sibling elements sharing a tag (and optionally a class), merged across sibling parents."""
    buckets: dict[tuple[Any, ...], list[etree._Element]] = {}
    for parent in _iter_elements(body, _NOT_MEMBERS - {"td", "th", "tbody", "thead", "tfoot"}):
        by_tag: dict[str, list[etree._Element]] = {}
        for child in parent:
            if not isinstance(child.tag, str):
                continue
            name = tag_name(child)
            if name in _NOT_MEMBERS or (name == "tr" and (child.find("td") is None or _label_row(child))):
                continue
            by_tag.setdefault(name, []).append(child)
        if not by_tag:
            continue
        grandparent, parent_sig = parent.getparent(), _signature(parent)
        for name, kids in by_tag.items():
            options: dict[str | None, list[etree._Element]] = {None: kids}
            counts = Counter(t for k in kids for t in _classes(k))
            for token, count in counts.items():
                if 2 <= count < len(kids):
                    options[token] = [k for k in kids if token in _classes(k)]
            for option, members in options.items():
                # Cards laid out in several sibling rows (div.row > div.card) form one list.
                buckets.setdefault((name, option, parent_sig, grandparent), []).extend(members)
    seen: set[tuple[etree._Element, ...]] = set()
    groups: list[list[etree._Element]] = []
    for members in buckets.values():
        if len(members) < min_records:
            continue
        members = _descend_wrappers(members)
        key = tuple(members)
        if key not in seen:
            seen.add(key)
            groups.append(members)
    return groups


def _descend_wrappers(members: list[etree._Element]) -> list[etree._Element]:
    """``li > article`` -> the articles: step into single-child wrappers that hold everything."""
    while True:
        kids: list[etree._Element] = []
        for member in members:
            children = _children(member)
            if len(children) != 1 or (member.text or "").strip() or (children[0].tail or "").strip():
                return members
            kids.append(children[0])
        tag = tag_name(kids[0])
        if tag not in _WRAPPER_TAGS or any(tag_name(k) != tag for k in kids):
            return members
        members = kids


def _context_penalty(first: etree._Element) -> float:
    """Records inside navigation, headers, footers, sidebars... are rarely what you want."""
    penalty = 1.0
    for depth, node in enumerate([first, *first.iterancestors()]):
        name = tag_name(node)
        if name in ("body", "html"):
            break
        if name in _BOILER_TAGS or (node.get("role") or "").lower() in _BOILER_ROLES:
            return 0.15
        if depth <= 4 and _BOILER_RE.search(f"{node.get('class') or ''} {node.get('id') or ''}"):
            penalty = 0.3
    return penalty


def _is_link_only(member: etree._Element, text: str) -> bool:
    """A menu/tag-cloud item: one short link and (almost) nothing else."""
    links = [member] if tag_name(member) == "a" else member.findall(".//a")
    return len(links) == 1 and len(text) < 40 and len(text_content(links[0])) >= 0.6 * len(text)


def _score(group: _Group, texts: list[str], page_text: int) -> float:
    """members x structural consistency x richness x page coverage, with boilerplate penalties."""
    members = group.members
    n, need = len(members), _need(len(members))
    lengths = [len(t) for t in texts]
    median = statistics.median(lengths)

    def most(predicate: Any) -> bool:
        return sum(1 for m in members if predicate(m)) >= need

    has_image = most(_has_image)
    counts = Counter(p for paths in group.paths for p in paths if p)
    common = {p for p, c in counts.items() if c >= need}
    if common:
        consistency = statistics.fmean(
            len(paths.keys() & common) / max(1, len(common | (paths.keys() - {()}))) for paths in group.paths
        )
    else:
        consistency = 1.0 if all(len(paths) == 1 for paths in group.paths) else 0.4

    own_field = sum(1 for t in texts if t) >= need and any(own_text(m) for m in members)
    field_count = int(own_field)
    for path in common:
        column = [e for e in group.column(path) if e is not None]
        tag = _path_tag(path, group)
        linked = tag == "a" and sum(1 for e in column if e.get("href")) >= need
        if tag == "img" or linked or sum(1 for e in column if own_text(e)) >= need:
            field_count += 1
    group.field_count = field_count

    has_link = most(lambda m: (tag_name(m) == "a" and m.get("href")) or m.find(".//a[@href]") is not None)
    has_heading = most(lambda m: any(tag_name(e) in _HEADINGS for e in m.iter() if isinstance(e.tag, str)))
    has_price = sum(1 for t in texts if _PRICE_RE.search(t)) >= need
    richness = (0.5 if field_count <= 1 else 1 + 0.4 * min(field_count, 6)) * (1 + min(median, 120) / 40)
    richness *= 1 + 0.3 * has_link + 0.4 * has_image + 0.4 * has_heading + 0.3 * has_price

    penalty = _context_penalty(members[0])
    if sum(1 for m, t in zip(members, texts, strict=True) if _is_link_only(m, t)) >= need:
        penalty *= 0.15
    if median < 12 and not has_image:
        penalty *= 0.3
    if tag_name(members[0]) in _TEXT_TAGS:
        penalty *= 0.25
    coverage = min(1.0, sum(lengths) / page_text)
    return math.log2(n + 1) * consistency * richness * (0.2 + coverage) * penalty


def _prefer_finer(scored: list[tuple[float, _Group]]) -> list[tuple[float, _Group]]:
    """Prefer cards over the grid rows holding them: a rich group nested across a better one wins."""
    scored.sort(key=lambda item: -item[0])
    boost: dict[int, float] = {}
    for outer_score, outer in scored[:8]:
        owners_pool = set(outer.members)
        for inner_score, inner in scored[:40]:
            if (
                inner is outer
                or inner.field_count < 2
                or len(inner.members) < 1.5 * len(outer.members)
                or inner_score < 0.25 * outer_score
            ):
                continue
            owners = {
                _owner(el.getparent(), owners_pool) if el.getparent() is not None else None for el in inner.members
            }
            if None in owners or len(owners) < max(2, 0.6 * len(outer.members)):
                continue
            boost[id(inner)] = max(boost.get(id(inner), inner_score), outer_score * 1.05)
    rescored = [(boost.get(id(group), score), group) for score, group in scored]
    rescored.sort(key=lambda item: -item[0])
    return rescored


def _same_records(group: _Group, taken: _Group) -> bool:
    """Whether ``group`` is a re-slicing of ``taken`` (nested inside it, or wrapping it)."""
    pool = set(taken.members)
    if all(_owner(el, pool) is not None for el in group.members):
        return True
    mine = set(group.members)
    return sum(1 for el in taken.members if _owner(el, mine) is not None) >= 0.5 * len(taken.members)


# --------------------------------------------------------------------------- #
# field inference
# --------------------------------------------------------------------------- #


@dataclass
class _Slot:
    path: tuple[str, ...]
    attr: str | None = None  # None: the element's text


def _snake(text: str) -> str:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    name = re.sub(r"[^0-9A-Za-z]+", "_", name).strip("_").lower()[:40]
    return name if name and not name[0].isdigit() else ""


def _unique_name(name: str, taken: Collection[str]) -> str:
    if name not in taken:
        return name
    i = 2
    while f"{name}_{i}" in taken:
        i += 1
    return f"{name}_{i}"


def _column_names(members: Sequence[etree._Element]) -> list[str]:
    """Header texts of the table the ``<tr>`` records belong to (``[]`` if none)."""
    if tag_name(members[0]) != "tr":
        return []
    table = next((a for a in members[0].iterancestors() if tag_name(a) == "table"), None)
    if table is None:
        return []
    for row in table.iter("tr"):
        cells = [c for c in row if isinstance(c.tag, str) and tag_name(c) in ("td", "th")]
        if cells and all(tag_name(c) == "th" for c in cells):
            return [text_content(c) for c in cells]
        if any(tag_name(c) == "td" for c in cells):
            break
    return []


def _field_name(group: _Group, path: tuple[str, ...], headers: list[str]) -> str:
    """Name a field after its table header or its most descriptive stable class."""
    element = next((e for e in group.column(path) if e is not None), None)
    if element is None:
        return ""
    if headers and path:
        cell = next((e for e in group.column(path[:1]) if e is not None), None)
        if cell is not None and tag_name(cell) in ("td", "th"):
            parent = cell.getparent()
            cells = (
                [c for c in parent if isinstance(c.tag, str) and tag_name(c) in ("td", "th")]
                if parent is not None
                else []
            )
            index = cells.index(cell) if cell in cells else -1
            if 0 <= index < len(headers) and _snake(headers[index]):
                return _snake(headers[index])
    for depth in range(len(path), 0, -1):
        node = next((e for e in group.column(path[:depth]) if e is not None), None)
        if node is None:
            continue
        tokens = [t for t in _classes(node) if t in group.vocab and not _is_generic(t)]
        if tokens and _snake(min(tokens, key=_class_rank)):
            return _snake(min(tokens, key=_class_rank))
    return ""


def _in_heading(group: _Group, path: tuple[str, ...]) -> bool:
    """Whether the element at ``path`` is a heading or sits inside one."""
    return any(_path_tag(path[:i], group) in _HEADINGS for i in range(1, len(path) + 1))


def _link_path(group: _Group, path: tuple[str, ...]) -> tuple[str, ...] | None:
    """Path of the ``<a>`` at or around ``path``: itself, an ancestor, or a descendant."""
    for depth in range(len(path), -1, -1):
        if _path_tag(path[:depth], group) == "a":
            return path[:depth]
    need = _need(len(group.members))
    for candidate in group.paths[0]:
        if (
            len(candidate) > len(path)
            and candidate[: len(path)] == path
            and _path_tag(candidate, group) == "a"
            and sum(1 for e in group.column(candidate) if e is not None and e.get("href")) >= need
        ):
            return candidate
    return None


def _fuller_title(pairs: Iterable[tuple[etree._Element | None, etree._Element | None]], need: int) -> bool:
    """Whether ``title`` attributes hold the untruncated text shown as "A Light in the ...".

    ``pairs`` are (element showing the text, element carrying the ``title``), one per record.
    """
    ok = longer = 0
    for text_el, holder in pairs:
        if text_el is None or holder is None:
            continue
        visible = own_text(text_el) or text_content(text_el)
        full = normalize_space(holder.get("title") or "")
        stem = visible.rstrip(" .…")
        if full and len(full) >= len(stem) and full.casefold().startswith(stem.casefold()):
            ok += 1
            longer += len(full) > len(visible) or visible.endswith(("...", "…"))
    return ok >= need and longer > 0


def _rating_slot(
    group: _Group, path: tuple[str, ...], text_paths: Collection[tuple[str, ...]], need: int
) -> _Slot | None:
    """A ``*rating*``/``*star*``-classed element: its aria-label/title, varying class, or text."""
    present = [e for e in group.column(path) if e is not None]
    classes = [e.get("class") or "" for e in present]
    if sum(1 for c in classes if _RATING_CLASS_RE.search(c)) < need:
        return None
    for attr in ("aria-label", "title"):
        if sum(1 for e in present if (e.get(attr) or "").strip()) >= need:
            return _Slot(path, attr)
    if len(set(classes)) > 1:  # "star-rating Three"
        return _Slot(path, "class")
    return _Slot(path) if path in text_paths else None


def _image_attr(elements: Iterable[etree._Element | None], need: int) -> str | None:
    present = [e for e in elements if e is not None]
    for attr in _IMAGE_ATTRS:
        values = [(e.get(attr) or "").strip() for e in present]
        if sum(1 for v in values if v and not v.startswith("data:")) >= need:
            return attr
    return None


def _infer_fields(group: _Group) -> dict[str, str]:
    """Name the sub-elements present in most records and build a relative selector for each."""
    n, need = len(group.members), _need(len(group.members))
    counts = Counter(p for paths in group.paths for p in paths)
    order: dict[tuple[str, ...], None] = {}
    for paths in group.paths:
        for p in paths:
            order.setdefault(p, None)
    consistent = [p for p in order if counts[p] >= need]

    def texts(path: tuple[str, ...]) -> list[str]:
        return [own_text(e) for e in group.column(path) if e is not None]

    def varies(path: tuple[str, ...]) -> bool:
        return n == 1 or len(set(texts(path))) > 1

    text_paths = [p for p in consistent if sum(1 for t in texts(p) if t) >= need]
    slots: dict[str, _Slot] = {}
    used: set[tuple[str, ...]] = set()

    def titled(path: tuple[str, ...]) -> bool:
        """Visible text truncated to the same stem everywhere, but the link's ``title`` differs."""
        link = _link_path(group, path)
        if link is None:
            return False
        values = {normalize_space(e.get("title") or "") for e in group.column(link) if e is not None}
        return len(values - {""}) > 1

    # title: first heading, else the most prominent link text, else strong/b
    title = next((p for p in text_paths if (varies(p) or titled(p)) and _in_heading(group, p)), None)
    if title is None:
        links = [p for p in text_paths if _path_tag(p, group) == "a" and varies(p)]
        if links:
            title = max(links, key=lambda p: statistics.median(len(t) for t in texts(p)))
    if title is None:
        title = next((p for p in text_paths if _path_tag(p, group) in ("strong", "b") and varies(p)), None)
    title_link = _link_path(group, title) if title is not None else None
    if title is not None:
        used.add(title)
        slots["title"] = _Slot(title)
        if title_link is not None and _fuller_title(
            zip(group.column(title), group.column(title_link), strict=True), need
        ):
            slots["title"] = _Slot(title_link, "title")
            used.add(title_link)

    # url: the title's link, else the first consistent link
    links_with_href = [
        p
        for p in consistent
        if _path_tag(p, group) == "a" and sum(1 for e in group.column(p) if e is not None and e.get("href")) >= need
    ]
    url = title_link if title_link in links_with_href else (links_with_href[0] if links_with_href else None)
    if url is not None:
        slots["url"] = _Slot(url, "href")

    for p in consistent:
        if _path_tag(p, group) == "img":
            attr = _image_attr(group.column(p), need)
            if attr:
                slots["image"] = _Slot(p, attr)
                break

    for p in text_paths:
        values = texts(p)
        if p not in used and sum(1 for v in values if len(v) <= 40 and _PRICE_RE.search(v)) >= need:
            slots["price"] = _Slot(p)
            used.add(p)
            break

    for p in consistent:
        rating = _rating_slot(group, p, text_paths, need)
        if rating is not None:
            slots["rating"] = rating
            used.add(p)
            break
    if "rating" not in slots:
        for p in text_paths:
            if p not in used and sum(1 for v in texts(p) if _RATING_TEXT_RE.search(v)) >= need:
                slots["rating"] = _Slot(p)
                used.add(p)
                break

    for p in text_paths:
        if p in used:
            continue
        present = [e for e in group.column(p) if e is not None]
        classed = sum(1 for e in present if _STOCK_CLASS_RE.search(e.get("class") or "")) >= need
        worded = sum(1 for v in texts(p) if len(v) <= 60 and _STOCK_TEXT_RE.search(v)) >= need
        if classed or worded:  # kept even when every record says the same ("In stock")
            slots["availability"] = _Slot(p)
            used.add(p)
            break

    headers = _column_names(group.members)
    for p in text_paths:
        if p in used or not varies(p):  # constant text is boilerplate ("Add to basket")
            continue
        name = _field_name(group, p, headers) or f"field_{sum(1 for k in slots if k.startswith('field_')) + 1}"
        slots[_unique_name(name, slots)] = _Slot(p)
        used.add(p)

    tail = [name for name in ("url", "image") if name in slots]
    slots = {**{k: v for k, v in slots.items() if k not in tail}, **{k: slots[k] for k in tail}}
    fields: dict[str, str] = {}
    for name, slot in slots.items():
        query = _pick_selector(group.members, group.column(slot.path), group.vocab)
        if query is None:
            continue
        query += f"::attr({slot.attr})" if slot.attr else "::text"
        try:
            css_to_xpath(query)
        except SelectorSyntaxError:
            continue
        fields[name] = query
    return fields


# --------------------------------------------------------------------------- #
# public API: detection
# --------------------------------------------------------------------------- #


@dataclass
class RecordGroup:
    """A list of repeating records found by :func:`detect_records`.

    Attributes:
        container_selector: CSS selector matching the record elements.
        elements: The record elements (lxml).
        score: How convincing the group is (higher is better).
        fields: Field name -> selector relative to a record, e.g. ``{"price": "p.price_color::text"}``.
    """

    container_selector: str
    elements: list[etree._Element]
    score: float
    fields: dict[str, str]

    def extract(self, base_url: str | None = None, *, clean: bool = True) -> list[dict[str, Any]]:
        """One dict per record (URLs absolute, missing values left out).

        With ``clean`` (the default) values are typed: prices become numbers plus a
        ``currency``, ratings numbers, availability ``in_stock``/``stock`` (see
        :func:`~wintergrab.parser.normalize.clean_record`). ``clean=False`` keeps the raw text.
        """
        if not self.elements:
            return []
        base = _document_base(self.elements[0], base_url)
        records = []
        for el in self.elements:
            record = {k: v for k, v in _record(el, self.fields, base).items() if v is not None}
            if record:
                records.append(clean_record(record) if clean else record)
        return records

    def as_schema(self) -> LearnedSchema:
        """The group as a reusable :class:`LearnedSchema` (to extract other pages of the site)."""
        return LearnedSchema(self.container_selector, dict(self.fields))

    def __repr__(self) -> str:
        return (
            f"RecordGroup({self.container_selector!r}, {len(self.elements)} records, "
            f"score={self.score:.2f}, fields={list(self.fields)})"
        )


def detect_records(root: Any, *, min_records: int = 3, max_groups: int = 5) -> list[RecordGroup]:
    """Find groups of repeating, content-bearing elements (product cards, results, rows), best first.

    Args:
        root: An lxml element, a :class:`~wintergrab.Selector`/``Response`` or HTML.
        min_records: Minimum number of records in a group.
        max_groups: Maximum number of groups returned.
    """
    doc, _ = _resolve(root)
    top = doc.getroottree().getroot()
    body = _body(doc)
    page_text = max(1, len(text_content(body)))
    scored: list[tuple[float, _Group]] = []
    for members in _candidate_groups(body, max(2, min_records)):
        texts = [text_content(m) for m in members]
        if not any(texts) and not any(_has_image(m) for m in members):
            continue
        group = _Group(members)
        score = _score(group, texts, page_text)
        if score > 0:
            scored.append((score, group))

    results: list[RecordGroup] = []
    taken: list[_Group] = []
    for score, group in _prefer_finer(scored):
        if any(_same_records(group, other) for other in taken):
            continue
        taken.append(group)
        selector = _container_selector(group.members, top)
        results.append(RecordGroup(selector, group.members, round(score, 3), _infer_fields(group)))
        if len(results) >= max_groups:
            break
    return results


def auto_extract(
    root: Any, base_url: str | None = None, *, min_records: int = 3, clean: bool = True
) -> list[dict[str, Any]]:
    """Records of the page's main repeating list, as dicts - no selectors needed.

    Returns ``[]`` when nothing on the page convincingly looks like a list of records.

    Example::

        page = wintergrab.get("https://books.toscrape.com/")
        auto_extract(page)  # [{"title": ..., "url": ..., "image": ..., "price": ...}, ...]
    """
    doc, url = _resolve(root)
    groups = detect_records(doc, min_records=min_records, max_groups=1)
    if not groups or groups[0].score < _MIN_AUTO_SCORE or not groups[0].fields:
        return []
    return groups[0].extract(base_url or url, clean=clean)


# --------------------------------------------------------------------------- #
# public API: learning from examples
# --------------------------------------------------------------------------- #


@dataclass
class LearnedSchema:
    """Selectors learned by :func:`learn_schema`.

    Attributes:
        container: CSS selector of the record elements, or ``None`` for a single-record page
            (then the field selectors are relative to the document).
        fields: Field name -> selector relative to a record, ending in ``::text`` or ``::attr(name)``.
    """

    container: str | None
    fields: dict[str, str]

    def extract(self, source: Any, *, base_url: str | None = None, clean: bool = True) -> list[dict[str, Any]]:
        """One dict per record of ``source`` (lxml element, Selector, Response or HTML).

        Missing values are ``None``; records where every value is missing are skipped.
        ``clean`` types the values as :meth:`RecordGroup.extract` does.
        """
        doc, url = _resolve(source)
        base = _document_base(doc, base_url or url)
        scopes = [doc] if self.container is None else _select(doc, self.container)
        records = [_record(scope, self.fields, base) for scope in scopes]
        records = [r for r in records if any(v is not None for v in r.values())]
        return [clean_record(r) for r in records] if clean else records

    def extract_one(self, source: Any, *, base_url: str | None = None, clean: bool = True) -> dict[str, Any] | None:
        """The first record (``None`` if there is none)."""
        records = self.extract(source, base_url=base_url, clean=clean)
        return records[0] if records else None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form (see :meth:`from_dict`)."""
        return {"container": self.container, "fields": dict(self.fields)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LearnedSchema:
        """Rebuild a schema saved with :meth:`to_dict`."""
        container, fields = data.get("container"), data.get("fields")
        if (container is not None and not isinstance(container, str)) or not isinstance(fields, Mapping):
            raise ValueError("expected {'container': str | None, 'fields': {name: selector}}")
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in fields.items()):
            raise ValueError("schema fields must map names to selector strings")
        return cls(container, dict(fields))

    def __repr__(self) -> str:
        return f"LearnedSchema(container={self.container!r}, fields={self.fields!r})"


@dataclass
class _Match:
    element: etree._Element
    attr: str | None  # None: found in the element's text
    deep: bool  # text spread over child elements
    rank: float  # lower is better
    order: int


def _iter_searchable(root: etree._Element) -> Iterator[tuple[etree._Element, bool]]:
    """``(element, inside <head>)`` for every element except scripts, styles and templates."""
    stack = [(root, False)]
    while stack:
        node, in_head = stack.pop()
        yield node, in_head
        in_head = in_head or tag_name(node) == "head"
        stack.extend(
            (c, in_head) for c in reversed(node) if isinstance(c.tag, str) and tag_name(c) not in _NOT_SEARCHED
        )


def _deepest(matches: list[_Match]) -> list[_Match]:
    elements = {m.element for m in matches}
    covered = {a for m in matches for a in m.element.iterancestors() if a in elements}
    return [m for m in matches if m.element not in covered]


def _locate(root: etree._Element, value: str, base: str | None) -> list[_Match]:
    """Every element whose text or attribute holds ``value``, ranked (exact text < attribute < contains)."""
    needle = normalize_space(value)
    if not needle:
        return []
    fold = needle.casefold()
    longest_word = max(fold.split(), key=len)
    target_url = _join(base, needle)
    matches: list[_Match] = []
    full_exact: list[_Match] = []
    full_partial: list[_Match] = []
    for order, (el, in_head) in enumerate(_iter_searchable(root)):
        extra = 0.5 if in_head else 0.0
        own = own_text(el)
        own_fold = own.casefold()
        if own_fold == fold:
            matches.append(_Match(el, None, False, (0.0 if own == needle else 0.1) + extra, order))
        elif own and fold in own_fold:
            matches.append(_Match(el, None, False, 3 + extra, order))
        # Cheap C-level pre-check before building the normalized text (block separators never join words).
        if len(el) and longest_word in "".join(el.itertext()).casefold():
            full = text_content(el)
            if full != own:
                full_fold = full.casefold()
                if full_fold == fold:
                    full_exact.append(_Match(el, None, True, (1.0 if full == needle else 1.1) + extra, order))
                elif fold in full_fold and fold not in own_fold:
                    full_partial.append(_Match(el, None, True, 4 + extra, order))
        for key, raw in el.attrib.items():
            name = str(key)
            priority = _ATTR_PRIORITY.get(name, len(_MATCH_ATTRS) if name.startswith("data-") else -1)
            if priority < 0:
                continue
            attr_value = normalize_space(str(raw))
            if not attr_value:
                continue
            hit = attr_value.casefold() == fold
            if not hit and (name in _URL_ATTRS or name in ("content", "value") or name.startswith("data-")):
                urls = (
                    [u.strip().split(" ")[0] for u in attr_value.split(",")]
                    if name.endswith("srcset")
                    else [attr_value]
                )
                hit = any(u == needle or _join(base, u) == target_url for u in urls if u)
            if hit:
                bare_link = name == "href" and not text_content(el)  # prefer the titled link to the image link
                matches.append(_Match(el, name, False, 2 + priority / 100 + 0.05 * bare_link + extra, order))
    found = matches + _deepest(full_exact) + _deepest(full_partial)
    for m in found:
        if _context_penalty(m.element) < 1:  # breadcrumbs, menus, footers
            m.rank += 0.4
    return found


def _choose(pools: Mapping[str, list[_Match]]) -> dict[str, _Match]:
    """One match per field: all inside the tightest common subtree (one record), then best-ranked, then closest."""
    trimmed: dict[str, list[_Match]] = {}
    best_rank: dict[str, float] = {}
    for name, found in pools.items():
        best_rank[name] = min(m.rank for m in found)
        near = sorted((m for m in found if m.rank < best_rank[name] + 2), key=lambda m: (m.rank, m.order))
        trimmed[name] = near[:_MAX_CANDIDATES]
    anchor_name = min(trimmed, key=lambda k: len(trimmed[k]))
    best: dict[str, _Match] = {}
    best_key: tuple[float, ...] | None = None
    for anchor in trimmed[anchor_name]:
        chain = [anchor.element, *anchor.element.iterancestors()]
        depth_of = {node: len(chain) - 1 - i for i, node in enumerate(chain)}
        shared = {
            name: [(_shared_depth(depth_of, m.element), m) for m in found]
            for name, found in trimmed.items()
            if name != anchor_name
        }
        # The deepest subtree around the anchor that can hold one match of every field.
        floor = min((max(d for d, _ in cands) for cands in shared.values()), default=len(chain))
        choice = {anchor_name: anchor}
        for name, cands in shared.items():
            keyed = [
                ((m.rank, _distance(anchor.element, m.element), m.order), i)
                for i, (d, m) in enumerate(cands)
                if d >= floor
            ]
            choice[name] = cands[min(keyed)[1]][1]
        rank_cost = sum(m.rank - best_rank[name] for name, m in choice.items())
        spread = sum(_distance(anchor.element, m.element) for m in choice.values())
        key = (-floor, rank_cost, spread, anchor.order)
        if best_key is None or key < best_key:
            best, best_key = choice, key
    return {name: best[name] for name in pools}


def _shared_depth(depth_of: Mapping[etree._Element, int], el: etree._Element) -> int:
    """Depth of the lowest ancestor-or-self of ``el`` found in ``depth_of`` (the anchor's lineage)."""
    for node in [el, *el.iterancestors()]:
        if node in depth_of:
            return depth_of[node]
    return -1


def _rel_path(container: etree._Element, el: etree._Element) -> list[tuple[str, frozenset[str]]]:
    steps: list[tuple[str, frozenset[str]]] = []
    node: etree._Element | None = el
    while node is not None and node is not container:
        steps.append((tag_name(node), frozenset(_classes(node))))
        node = node.getparent()
    return steps[::-1]


def _follow(container: etree._Element, steps: list[tuple[str, frozenset[str]]]) -> etree._Element | None:
    """The first element reached from ``container`` along tag/class steps (classes need only overlap)."""
    frontier = [container]
    for tag, classes in steps:
        frontier = [
            c
            for node in frontier
            for c in node
            if isinstance(c.tag, str) and tag_name(c) == tag and (not classes or classes & set(_classes(c)))
        ]
        if not frontier:
            return None
    return frontier[0]


def _peers(
    node: etree._Element,
    rels: list[list[tuple[str, frozenset[str]]]],
    examples: Sequence[Sequence[etree._Element]] = (),
) -> list[etree._Element]:
    """Elements elsewhere shaped like ``node`` holding most of the same fields (or another example record)."""
    need = max(1, math.ceil(0.75 * len(rels)))
    return [
        cand
        for cand in similar_elements(node)
        if not _is_inside(cand, node)
        and not _is_inside(node, cand)
        and (
            sum(1 for rel in rels if _follow(cand, rel) is not None) >= need
            or any(all(_is_inside(e, cand) for e in els) for els in examples)
        )
    ]


def _find_container(
    located: list[dict[str, _Match]],
) -> tuple[etree._Element | None, list[etree._Element]]:
    """The repeating record element around the first example record, plus all its peers."""
    elements = [m.element for m in located[0].values()]
    others = [[m.element for m in rec.values()] for rec in located[1:]]
    node: etree._Element | None = _lca(elements)
    found: etree._Element | None = None
    found_peers: list[etree._Element] = []
    while node is not None and tag_name(node) not in ("html", "body"):
        peers = _peers(node, [_rel_path(node, el) for el in elements], others)
        pool = [node, *peers]
        ok = bool(peers) and all(any(all(_is_inside(e, p) for e in els) for p in pool) for els in others)
        if ok and (found is None or len(peers) == len(found_peers)):
            found, found_peers = node, peers  # same repetition one level up: a fuller record
        elif found is not None:
            break
        node = node.getparent()
    if found is None:
        return None, []
    # ... but not a bare wrapper: li > article -> article.
    while True:
        kids = _children(found)
        if (
            len(kids) != 1
            or tag_name(kids[0]) not in _WRAPPER_TAGS
            or (found.text or "").strip()
            or not all(_is_inside(e, kids[0]) for e in elements)
        ):
            break
        inner_peers = _peers(kids[0], [_rel_path(kids[0], el) for el in elements], others)
        if len(inner_peers) != len(found_peers):
            break
        found, found_peers = kids[0], inner_peers
    order = {el: i for i, el in enumerate(found.getroottree().getroot().iter())}
    return found, sorted([found, *found_peers], key=lambda el: order.get(el, 0))


def _normalize_examples(examples: Any) -> list[dict[str, str]]:
    if isinstance(examples, Mapping):
        records: list[Any] = [examples]
    elif isinstance(examples, Iterable) and not isinstance(examples, (str, bytes)):
        records = list(examples)
    else:
        raise TypeError("examples must be a dict of field -> example value, or a list of such dicts")
    out: list[dict[str, str]] = []
    for record in records:
        if not isinstance(record, Mapping) or not record:
            raise ValueError("each example must be a non-empty dict of field -> example value")
        clean = {str(k): str(v) for k, v in record.items() if v is not None and str(v).strip()}
        if not clean:
            raise ValueError("an example record has no non-empty values")
        out.append(clean)
    if not out:
        raise ValueError("no example records given")
    return out


def learn_schema(root: Any, examples: Any, *, base_url: str | None = None) -> LearnedSchema:
    """Learn reusable selectors from example values ("scraping by example").

    Args:
        root: The page (lxml element, Selector, Response or HTML) the examples were copied from.
        examples: ``{field: value}`` for ONE record on the page, e.g.
            ``{"title": "A Light in the Attic", "price": "£51.77"}``, or a list of such dicts from
            several records (better generalization). Values may be visible text or attribute values
            such as a link or image URL (relative or absolute).
        base_url: Page URL used to match relative links (defaults to the page's own URL).

    Returns:
        A :class:`LearnedSchema`; call ``.extract(other_page)`` on pages with the same template.

    Raises:
        ValueError: If an example value cannot be found on the page.
    """
    doc, url = _resolve(root)
    base = _document_base(doc, base_url or url)
    records = _normalize_examples(examples)
    located: list[dict[str, _Match]] = []
    for record in records:
        pools: dict[str, list[_Match]] = {}
        for name, value in record.items():
            found = _locate(doc, value, base)
            if not found:
                raise ValueError(
                    f"Example value for field {name!r} not found on the page: {value!r}. It must equal (or be "
                    "contained in) an element's visible text, or equal an href/src/title/alt/content/value/data-* "
                    "attribute - copy it exactly as the page shows it."
                )
            pools[name] = found
        located.append(_choose(pools))

    names = list(dict.fromkeys(name for record in records for name in record))
    container, members = _find_container(located)
    if container is None:
        if len(located) > 1:
            raise ValueError(
                "The example records do not sit in a repeating container; give one record for a detail page, "
                "or values copied from records of the same list."
            )
        single = {name: _field_selector([doc], [m.element], [0], m, None) for name, m in located[0].items()}
        return LearnedSchema(None, single)

    top = doc.getroottree().getroot()
    selector = _container_selector(members, top)
    scopes = _select(top, selector)
    if container not in scopes:
        scopes = members
    vocab = _vocab(scopes)
    fields: dict[str, str] = {}
    for name in names:
        # Example records holding this field pin their scope's target; the others are inferred.
        pinned: dict[int, etree._Element] = {}
        first_match: _Match | None = None
        for chosen in located:
            match = chosen.get(name)
            if match is None:
                continue
            owner = next((i for i, s in enumerate(scopes) if _is_inside(match.element, s)), None)
            if owner is not None and owner not in pinned:
                pinned[owner] = match.element
                if first_match is None:
                    first_match = match
        if first_match is None:
            continue
        primary = min(pinned)
        rel = _rel_path(scopes[primary], pinned[primary])
        targets = [pinned[i] if i in pinned else _follow(s, rel) for i, s in enumerate(scopes)]
        present = [t for t in targets if t is not None]
        if first_match.attr is None and _fuller_title(((t, t) for t in present), _need(len(present))):
            first_match = _Match(first_match.element, "title", False, first_match.rank, first_match.order)
        fields[name] = _field_selector(scopes, targets, sorted(pinned), first_match, vocab)
    return LearnedSchema(selector, fields)


def _field_selector(
    scopes: Sequence[etree._Element],
    targets: Sequence[etree._Element | None],
    must: Sequence[int],
    match: _Match,
    vocab: Collection[str] | None,
) -> str:
    """Relative selector (ending in ``::text``/``::attr()``) for one learned field."""
    query = _pick_selector(scopes, targets, vocab, must=must)
    if query is None:  # cannot happen for a target inside its scope; keep a structural path just in case
        query = Selector(root=match.element).css_path or tag_name(match.element)
    if match.attr:
        return f"{query}::attr({match.attr})"
    deep = match.deep and own_text(match.element) != ""
    return f"{query} ::text" if deep else f"{query}::text"
