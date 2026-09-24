"""The :class:`Selector` - query HTML/XML with CSS or XPath and pull data out."""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from re import Pattern
from typing import Any, SupportsIndex, Union, overload
from urllib.parse import urldefrag, urljoin, urlsplit

from lxml import etree

from ..adaptive.fingerprint import VOLATILE_ATTRS, fingerprint, relocate, similar_elements
from ..adaptive.storage import AdaptiveStorage, default_storage, storage_key
from ..errors import SelectorSyntaxError
from ..utils import fast_urljoin
from . import text as _text
from .css import css_to_xpath, looks_like_xpath, split_css_pseudo, split_xpath_tail

log = logging.getLogger("wintergrab.adaptive")

_MAX_SAVED_ELEMENTS = 3
_DEFAULT_MIN_SCORE = 0.55

_local = threading.local()


def _parser(kind: str) -> etree._FeedParser:
    """One lxml parser per thread (parsers are not thread-safe)."""
    attr = f"{kind}_parser"
    parser = getattr(_local, attr, None)
    if parser is None:
        if kind == "xml":
            parser = etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False, no_network=True)
        else:
            parser = etree.HTMLParser(recover=True, encoding="utf-8", huge_tree=True, no_network=True)
        setattr(_local, attr, parser)
    return parser


def parse_document(markup: str | bytes, kind: str = "html", base_url: str | None = None) -> etree._Element:
    """Parse markup into an lxml element tree (never raises on bad markup)."""
    data = markup.encode("utf-8") if isinstance(markup, str) else markup
    if kind == "xml" and isinstance(markup, str):
        # Strip the XML declaration: it may name an encoding we already decoded.
        data = re.sub(rb"^\s*<\?xml[^>]*\?>", b"", data, count=1)
    root = None
    if data.strip():
        try:
            root = etree.fromstring(data, parser=_parser(kind), base_url=base_url)
        except etree.XMLSyntaxError:
            root = None
    if root is None:
        root = etree.fromstring(b"<html/>" if kind == "html" else b"<root/>", parser=_parser(kind))
    return root


def _compiled_xpath(query: str, namespaces: Mapping[str, str] | None) -> etree.XPath:
    """A compiled XPath, cached per thread (compiling is a large share of a simple query's cost)."""
    cache: dict[Any, etree.XPath] | None = getattr(_local, "xpath_cache", None)
    if cache is None:
        cache = _local.xpath_cache = {}
    key = (query, tuple(sorted(namespaces.items())) if namespaces else None)
    compiled = cache.get(key)
    if compiled is None:
        if len(cache) > 2048:
            cache.clear()
        compiled = cache[key] = etree.XPath(query, namespaces=namespaces, smart_strings=False)
    return compiled


class _Document:
    """State shared by every selector created from the same page."""

    __slots__ = ("_base_url", "storage", "type", "url")

    def __init__(self, url: str | None, type: str, storage: AdaptiveStorage | None) -> None:
        self.url = url
        self.type = type
        self.storage = storage
        self._base_url: str | bool | None = False  # False = not computed yet

    def base_url(self, root: etree._Element) -> str | None:
        if self._base_url is False:
            base = None
            if self.type == "html":
                found = root.xpath("//base/@href")
                if found:
                    base = urljoin(self.url or "", str(found[0]).strip())
            self._base_url = base or self.url
        return self._base_url  # type: ignore[return-value]


QueryResult = Union["Selector", str]


class Selector:
    """A node of an HTML/XML document - or a piece of text pulled out of one.

    Build one from markup (``Selector("<html>...")``) or get one from a
    fetched page (every :class:`~wintergrab.Response` has ``css``/``xpath``).

    Element selectors expose ``tag``, ``attrib``, ``text``, ``html`` and the
    navigation helpers (``parent``, ``children``, ``next``...). Selecting
    ``::text`` or ``::attr(name)`` (or ``text()``/``@name`` in XPath) gives
    *text selectors* whose :meth:`get` returns the string.
    """

    __slots__ = ("__weakref__", "_doc", "_root", "_value")

    def __init__(
        self,
        text: str | bytes | None = None,
        *,
        url: str | None = None,
        type: str = "html",
        root: etree._Element | None = None,
        adaptive_storage: AdaptiveStorage | None = None,
    ) -> None:
        if type not in ("html", "xml"):
            raise ValueError("type must be 'html' or 'xml'")
        if root is None:
            root = parse_document(text or "", type, url)
        self._root: etree._Element | None = root
        self._value: str | None = None
        self._doc = _Document(url, type, adaptive_storage)

    # ------------------------------------------------------------------ #
    # construction helpers
    # ------------------------------------------------------------------ #
    def _child(self, node: Any) -> Selector:
        sel = Selector.__new__(Selector)
        sel._doc = self._doc
        if isinstance(node, etree._Element) and isinstance(node.tag, str):
            sel._root, sel._value = node, None
        else:
            if isinstance(node, bool):
                value = "true" if node else "false"
            elif isinstance(node, float) and node.is_integer():
                value = str(int(node))
            else:
                value = str(node)
            sel._root, sel._value = None, value
        return sel

    def _wrap(self, nodes: Iterable[Any]) -> SelectorList:
        return SelectorList(
            self._child(n) for n in nodes if not isinstance(n, etree._Element) or isinstance(n.tag, str)
        )

    # ------------------------------------------------------------------ #
    # basic properties
    # ------------------------------------------------------------------ #
    @property
    def root(self) -> etree._Element | None:
        """The underlying lxml element (``None`` for text selectors)."""
        return self._root

    @property
    def url(self) -> str | None:
        """URL of the page this selector came from (if known)."""
        return self._doc.url

    @property
    def type(self) -> str:
        return self._doc.type

    @property
    def is_element(self) -> bool:
        return self._root is not None

    @property
    def tag(self) -> str | None:
        """Lower-case tag name, e.g. ``"div"`` (``None`` for text selectors)."""
        if self._root is None:
            return None
        return _text.tag_name(self._root) if self._doc.type == "html" else etree.QName(self._root).localname

    @property
    def attrib(self) -> dict[str, str]:
        """The element's attributes as a plain dict."""
        return dict(self._root.attrib) if self._root is not None else {}

    def attr(self, name: str, default: str | None = None) -> str | None:
        """Value of one attribute, or ``default``."""
        if self._root is None:
            return default
        return self._root.get(name, default)

    def __getitem__(self, name: str) -> str:
        """``link["href"]`` - like :meth:`attr` but raises ``KeyError``."""
        value = self.attr(name)
        if value is None:
            raise KeyError(name)
        return value

    @property
    def text(self) -> str:
        """All visible text inside the element, whitespace-normalized.

        For text selectors this is the text itself.
        """
        if self._root is None:
            return self._value or ""
        return _text.text_content(self._root)

    @property
    def own_text(self) -> str:
        """Only the text placed directly in this element (not in children)."""
        if self._root is None:
            return self._value or ""
        return _text.own_text(self._root)

    @property
    def html(self) -> str:
        """Outer HTML of the element (the text itself for text selectors)."""
        if self._root is None:
            return self._value or ""
        method = "xml" if self._doc.type == "xml" else "html"
        return etree.tostring(self._root, method=method, encoding="unicode", with_tail=False)

    @property
    def inner_html(self) -> str:
        """HTML of the element's content, without the element's own tag."""
        if self._root is None:
            return self._value or ""
        method = "xml" if self._doc.type == "xml" else "html"
        parts = [self._root.text or ""]
        parts.extend(etree.tostring(c, method=method, encoding="unicode", with_tail=True) for c in self._root)
        return "".join(parts)

    def get(self, default: str | None = None) -> str | None:
        """The text value (text selectors) or the outer HTML (elements)."""
        if self._root is None:
            return self._value if self._value is not None else default
        return self.html

    def getall(self) -> list[str]:
        value = self.get()
        return [] if value is None else [value]

    def get_text(self) -> str:
        """Readable text with one line per paragraph/block element."""
        if self._root is None:
            return self._value or ""
        return _text.to_text(self._root)

    def markdown(self, *, main_content: bool = False) -> str:
        """Convert the element to Markdown (links and images made absolute).

        Args:
            main_content: Only convert the page's main content (``<main>``,
                ``<article>``...) instead of the whole element.
        """
        if self._root is None:
            return self._value or ""
        el = _text.find_main_content(self._root) if main_content else self._root
        if self._doc.type == "html" and _text.tag_name(el) == "html":
            body = el.find("body")
            el = body if body is not None else el
        return _text.to_markdown(el, self._doc.base_url(self._top()))

    # ------------------------------------------------------------------ #
    # regular expressions
    # ------------------------------------------------------------------ #
    def re(self, pattern: str | Pattern[str], flags: int = 0) -> list[str]:
        """Apply a regex to the text and return every match.

        If the pattern has groups, the groups are returned instead of the whole
        match (a named group ``extract`` wins if present).
        """
        return _regex_all(pattern, self.text, flags)

    def re_first(self, pattern: str | Pattern[str], default: str | None = None, flags: int = 0) -> str | None:
        found = self.re(pattern, flags)
        return found[0] if found else default

    # ------------------------------------------------------------------ #
    # querying
    # ------------------------------------------------------------------ #
    def css(
        self,
        query: str,
        *,
        adaptive: bool = False,
        auto_save: bool = False,
        identifier: str | None = None,
        min_score: float = _DEFAULT_MIN_SCORE,
    ) -> SelectorList:
        """Select with a CSS selector. Supports ``::text`` and ``::attr(name)``.

        Args:
            query: The CSS selector.
            adaptive: Remember what the selector matches and, if it ever
                stops matching (say after a site redesign), find the most
                similar elements on the page instead.
            auto_save: Only remember the matches (no relocation).
            identifier: Name to store the fingerprint under (defaults to the
                query). Set it if you plan to change the selector later.
            min_score: How similar (0-1) a relocated element must be.
        """
        if self._root is None:
            return SelectorList()
        xpath = css_to_xpath(query, self._doc.type == "xml")
        result = self._xpath_raw(xpath, query)
        if adaptive or auto_save:
            element_query, suffix = split_css_pseudo(query)
            element_xpath = css_to_xpath(element_query, self._doc.type == "xml")
            return self._adapt(query, element_xpath, suffix, result, adaptive, identifier, min_score)
        return result

    def xpath(
        self,
        query: str,
        *,
        namespaces: Mapping[str, str] | None = None,
        adaptive: bool = False,
        auto_save: bool = False,
        identifier: str | None = None,
        min_score: float = _DEFAULT_MIN_SCORE,
        **variables: Any,
    ) -> SelectorList:
        """Select with XPath 1.0. ``$variables`` can be passed as keyword args.

        See :meth:`css` for ``adaptive``/``auto_save``/``identifier``/``min_score``.
        """
        if self._root is None:
            return SelectorList()
        result = self._xpath_raw(query, query, namespaces, variables)
        if adaptive or auto_save:
            element_query, suffix = split_xpath_tail(query)
            return self._adapt(query, element_query, suffix, result, adaptive, identifier, min_score, namespaces)
        return result

    def select(self, query: str, **kwargs: Any) -> SelectorList:
        """CSS or XPath, guessed from the query (XPath starts with ``/``, ``./`` or ``(``)."""
        if query.startswith("xpath:"):
            return self.xpath(query[6:], **kwargs)
        if query.startswith("css:"):
            return self.css(query[4:], **kwargs)
        return self.xpath(query, **kwargs) if looks_like_xpath(query) else self.css(query, **kwargs)

    def _xpath_raw(
        self,
        xpath: str,
        original: str,
        namespaces: Mapping[str, str] | None = None,
        variables: Mapping[str, Any] | None = None,
    ) -> SelectorList:
        assert self._root is not None
        try:
            result = _compiled_xpath(xpath, namespaces)(self._root, **(variables or {}))
        except etree.XPathError as exc:
            raise SelectorSyntaxError(f"Invalid XPath {original!r}: {exc}") from None
        if not isinstance(result, list):
            result = [result]
        return self._wrap(result)

    # ------------------------------------------------------------------ #
    # adaptive selection
    # ------------------------------------------------------------------ #
    def _storage(self) -> AdaptiveStorage:
        if self._doc.storage is None:
            self._doc.storage = default_storage()
        return self._doc.storage

    def _adapt(
        self,
        query: str,
        element_xpath: str,
        suffix: str | None,
        result: SelectorList,
        adaptive: bool,
        identifier: str | None,
        min_score: float,
        namespaces: Mapping[str, str] | None = None,
    ) -> SelectorList:
        assert self._root is not None
        storage = self._storage()
        domain = storage_key(self._doc.url)
        key = identifier or query
        if suffix:
            elements = [s._root for s in self._xpath_raw(element_xpath, query, namespaces) if s._root is not None]
        else:
            elements = [s._root for s in result if s._root is not None]

        if elements:
            storage.save(domain, key, _record(elements))
            return result
        if not adaptive or result:
            # Nothing to relocate - or the query matched text/values we cannot
            # fingerprint (e.g. "*::text"), which is still a valid answer.
            return result

        record = storage.load(domain, key)
        if not record:
            log.debug("adaptive: nothing saved yet for %r on %s", key, domain)
            return result
        found, score = relocate(self._root, record, min_score=min_score)
        if not found:
            log.warning("adaptive: %r matched nothing and no similar element was found (best score %.2f)", key, score)
            return result
        log.warning(
            "adaptive: %r matched nothing; relocated %d element(s) by similarity (score %.2f)",
            key,
            len(found),
            score,
        )
        storage.save(domain, key, _record(found))
        if suffix:
            out = SelectorList()
            for el in found:
                out.extend(self._child(el)._xpath_raw("." + suffix, query))
            return out
        return self._wrap(found)

    # ------------------------------------------------------------------ #
    # searching
    # ------------------------------------------------------------------ #
    def find_by_text(
        self,
        text: str,
        *,
        partial: bool = True,
        case_sensitive: bool = False,
        tag: str | None = None,
    ) -> SelectorList:
        """Elements whose text matches ``text``.

        Elements whose *own* text matches win; if none match, the deepest
        elements whose full text matches are returned instead (so
        ``"Price: $10"`` finds ``<p>Price: <b>$10</b></p>``).
        """
        needle = _text.normalize_space(text)
        if not case_sensitive:
            needle = needle.lower()

        def matches(value: str) -> bool:
            if not case_sensitive:
                value = value.lower()
            return needle in value if partial else needle == value

        return self._find(matches, tag)

    def find_by_regex(self, pattern: str | Pattern[str], *, tag: str | None = None, flags: int = 0) -> SelectorList:
        """Elements whose text matches a regular expression."""
        rx = re.compile(pattern, flags) if isinstance(pattern, str) else pattern
        return self._find(lambda value: rx.search(value) is not None, tag)

    def _find(self, matches: Callable[[str], bool], tag: str | None) -> SelectorList:
        if self._root is None:
            return SelectorList()
        elements = [e for e in self._root.iter(tag or etree.Element) if isinstance(e.tag, str)]
        elements = [e for e in elements if _text.tag_name(e) not in _text.SKIP_TAGS]
        own = [e for e in elements if matches(_text.own_text(e))]
        if own:
            return self._wrap(own)
        full = [e for e in elements if matches(_text.text_content(e))]
        full_set = set(full)
        deepest = [e for e in full if not any(d in full_set for d in e.iterdescendants())]
        return self._wrap(deepest)

    def find_similar(
        self,
        *,
        threshold: float = 0.5,
        ignore_attributes: Iterable[str] = VOLATILE_ATTRS,
    ) -> SelectorList:
        """Elements structurally similar to this one (other rows, cards...).

        Great for "give me every product like this one" without writing a
        selector: find one item by text, then ``find_similar()``.
        """
        if self._root is None:
            return SelectorList()
        return self._wrap(
            similar_elements(self._root, root=self._top(), threshold=threshold, ignore_attributes=ignore_attributes)
        )

    # ------------------------------------------------------------------ #
    # navigation
    # ------------------------------------------------------------------ #
    def _top(self) -> etree._Element:
        assert self._root is not None
        return self._root.getroottree().getroot()

    @property
    def parent(self) -> Selector | None:
        if self._root is None:
            return None
        p = self._root.getparent()
        return self._child(p) if p is not None else None

    @property
    def children(self) -> SelectorList:
        if self._root is None:
            return SelectorList()
        return self._wrap(c for c in self._root if isinstance(c.tag, str))

    @property
    def next(self) -> Selector | None:
        """The next sibling element."""
        if self._root is None:
            return None
        node = self._root.getnext()
        while node is not None and not isinstance(node.tag, str):
            node = node.getnext()
        return self._child(node) if node is not None else None

    @property
    def previous(self) -> Selector | None:
        """The previous sibling element."""
        if self._root is None:
            return None
        node = self._root.getprevious()
        while node is not None and not isinstance(node.tag, str):
            node = node.getprevious()
        return self._child(node) if node is not None else None

    @property
    def siblings(self) -> SelectorList:
        if self._root is None or self._root.getparent() is None:
            return SelectorList()
        return self._wrap(c for c in self._root.getparent() if c is not self._root and isinstance(c.tag, str))

    @property
    def ancestors(self) -> SelectorList:
        """Parent, grandparent, ... up to the document root."""
        if self._root is None:
            return SelectorList()
        return self._wrap(self._root.iterancestors())

    def closest(self, query: str) -> Selector | None:
        """The nearest ancestor (or self) matching a CSS selector."""
        if self._root is None:
            return None
        matches = set(self._top().xpath(css_to_xpath(query, self._doc.type == "xml")))
        node: etree._Element | None = self._root
        while node is not None:
            if node in matches:
                return self._child(node)
            node = node.getparent()
        return None

    # ------------------------------------------------------------------ #
    # generated selectors
    # ------------------------------------------------------------------ #
    @property
    def css_path(self) -> str | None:
        """A CSS selector that uniquely points at this element."""
        if self._root is None:
            return None
        top = self._top()
        parts: list[str] = []
        node: etree._Element | None = self._root
        while node is not None and isinstance(node.tag, str):
            tag = _text.tag_name(node)
            el_id = node.get("id")
            if el_id and re.fullmatch(r"[A-Za-z][\w-]*", el_id) and len(top.xpath("//*[@id=$i]", i=el_id)) == 1:
                parts.append(f"#{el_id}")
                break
            parent = node.getparent()
            if parent is None:
                parts.append(tag)
                break
            same = [c for c in parent if c.tag == node.tag]
            parts.append(f"{tag}:nth-of-type({same.index(node) + 1})" if len(same) > 1 else tag)
            node = parent
        return " > ".join(reversed(parts))

    @property
    def xpath_path(self) -> str | None:
        """An absolute XPath that points at this element."""
        if self._root is None:
            return None
        return self._root.getroottree().getpath(self._root)

    # ------------------------------------------------------------------ #
    # links and URLs
    # ------------------------------------------------------------------ #
    def urljoin(self, url: str) -> str:
        """Resolve a (possibly relative) URL against the page URL / ``<base>``."""
        base = self._doc.base_url(self._top()) if self._root is not None else self._doc.url
        return fast_urljoin(base or "", url.strip())

    def links(
        self,
        css: str | None = None,
        *,
        allow: str | Iterable[str] | None = None,
        deny: str | Iterable[str] | None = None,
        domains: str | Iterable[str] | None = None,
        same_domain: bool = False,
        unique: bool = True,
    ) -> list[str]:
        """Absolute http(s) URLs of the links on the page (fragments removed).

        Args:
            css: Only look at links matched by this selector (a container is
                fine too - every link inside it is used).
            allow: Regex(es) a URL must match.
            deny: Regex(es) a URL must not match.
            domains: Only keep URLs on these domains (subdomains included).
            same_domain: Only keep URLs on the page's own domain.
        """
        if self._root is None:
            return []
        nodes = self.xpath(".//a[@href] | .//area[@href]") if css is None else self.select(css)
        hrefs: list[str] = []
        for node in nodes:
            if not node.is_element:
                hrefs.append(node.get() or "")
            elif node.attr("href") is not None:
                hrefs.append(node.attr("href") or "")
            else:
                hrefs.extend(a.attr("href") or "" for a in node.xpath(".//a[@href]"))
        allow_rx = _compile_many(allow)
        deny_rx = _compile_many(deny)
        domain_list = [domains] if isinstance(domains, str) else list(domains or [])
        if same_domain and self._doc.url:
            domain_list.append(urlsplit(self._doc.url).hostname or "")
        out: list[str] = []
        seen: set[str] = set()
        for href in hrefs:
            href = href.strip()
            if not href or href.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
                continue
            try:
                url = urldefrag(self.urljoin(href))[0]
            except ValueError:
                continue
            if not url.startswith(("http://", "https://")):
                continue
            if allow_rx and not any(rx.search(url) for rx in allow_rx):
                continue
            if deny_rx and any(rx.search(url) for rx in deny_rx):
                continue
            if domain_list and not _host_in(urlsplit(url).hostname or "", domain_list):
                continue
            if unique:
                if url in seen:
                    continue
                seen.add(url)
            out.append(url)
        return out

    # ------------------------------------------------------------------ #
    # structured extraction
    # ------------------------------------------------------------------ #
    def extract(self, schema: Mapping[str, Any]) -> dict[str, Any]:
        """Pull a dict of values out using a schema of selectors.

        ``{"title": "h1", "links": ["a::attr(href)"], "price": Field(".price", ".cost", transform=float)}``

        * a string selects the first match (element -> its text),
        * a one-item list selects every match,
        * a :class:`Field` gives fallbacks, defaults, regexes and transforms,
        * a dict extracts a nested object, a callable gets this selector.
        """
        from .extract import extract_schema

        return extract_schema(self, schema)

    def extract_all(self, query: str, schema: Mapping[str, Any], **select_kwargs: Any) -> list[dict[str, Any]]:
        """Run :meth:`extract` on every element matched by ``query``.

        Extra keyword arguments (e.g. ``adaptive=True``) go to the container query.
        """
        return [item.extract(schema) for item in self.select(query, **select_kwargs) if item.is_element]

    # ------------------------------------------------------------------ #
    # zero-selector extraction
    # ------------------------------------------------------------------ #
    def structured_data(self) -> dict[str, Any]:
        """Machine-readable data the page publishes: JSON-LD, microdata, OpenGraph, Twitter cards, meta tags.

        Product pages, articles, recipes and events very often carry clean
        structured data - no selectors needed.
        """
        from .structured import structured_data

        return structured_data(self._top(), self._doc.base_url(self._top())) if self._root is not None else {}

    def embedded_json(self) -> dict[str, Any]:
        """JSON state embedded by JavaScript apps (``__NEXT_DATA__``, ``window.__INITIAL_STATE__``...).

        Lets you scrape many React/Vue/Next/Nuxt sites without a browser: the
        data is already in the HTML, just not in the markup.
        """
        from .structured import embedded_json

        return embedded_json(self._top()) if self._root is not None else {}

    def find_json(self, key: str | Callable[[str], bool], *, limit: int | None = None) -> list[Any]:
        """Every value stored under ``key`` anywhere in the page's embedded JSON and JSON-LD."""
        from .structured import find_values

        data = {"embedded": self.embedded_json(), "json_ld": self.structured_data().get("json_ld", [])}
        return find_values(data, key, limit=limit)

    def tables(self) -> list[dict[str, Any]]:
        """Every ``<table>`` as ``{"headers", "rows": [{header: value}], "caption"}`` (colspan/rowspan handled)."""
        from .structured import tables

        return tables(self._root, self._doc.base_url(self._top())) if self._root is not None else []

    def next_page(self) -> str | None:
        """URL of the "next page" link (rel=next, "Next", arrows, numbered pagination...), if any."""
        from .structured import next_page_url

        return next_page_url(self._top(), self._doc.base_url(self._top())) if self._root is not None else None

    def detect_records(self, *, min_records: int = 3) -> list[Any]:
        """Repeating record groups on the page (product cards, results, rows), best first."""
        from .autoextract import detect_records

        return detect_records(self._root, min_records=min_records) if self._root is not None else []

    def auto_extract(self, *, min_records: int = 3) -> list[dict[str, Any]]:
        """Records from the page's main repeating list, with fields inferred automatically.

        Finds the product grid / result list / table, names the fields
        (title, url, image, price, rating...) and returns one dict per record.
        """
        from .autoextract import auto_extract

        if self._root is None:
            return []
        return auto_extract(self._root, self._doc.base_url(self._top()), min_records=min_records)

    def learn(self, examples: Mapping[str, str] | list[Mapping[str, str]]) -> Any:
        """Learn an extraction schema from example values ("scraping by example").

        ``page.learn({"title": "A Light in the Attic", "price": "£51.77"})`` finds
        those values, works out the record container and a selector per field,
        and returns a reusable :class:`~wintergrab.parser.autoextract.LearnedSchema`:
        ``schema.extract(other_page)`` then works on every page with the same template.
        """
        from .autoextract import learn_schema

        if self._root is None:
            raise ValueError("cannot learn from a text selector")
        return learn_schema(self._top(), examples, base_url=self._doc.base_url(self._top()))

    # ------------------------------------------------------------------ #
    # dunder
    # ------------------------------------------------------------------ #
    def remove_namespaces(self) -> None:
        """Strip XML namespaces so ``//loc`` works on sitemaps and feeds."""
        if self._root is None:
            return
        for el in self._top().iter("*"):
            if isinstance(el.tag, str) and el.tag.startswith("{"):
                el.tag = etree.QName(el).localname
            for name in list(el.attrib):
                if name.startswith("{"):
                    el.attrib[etree.QName(name).localname] = el.attrib.pop(name)
        etree.cleanup_namespaces(self._top())

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Selector):
            return NotImplemented
        if self._root is not None or other._root is not None:
            return self._root is other._root
        return self._value == other._value

    def __hash__(self) -> int:
        return hash(self._root) if self._root is not None else hash(self._value)

    def __bool__(self) -> bool:
        return True

    def __repr__(self) -> str:
        if self._root is None:
            value = self._value or ""
            short = value if len(value) <= 40 else value[:37] + "..."
            return f"<Selector text={short!r}>"
        bits = [self.tag or "?"]
        for key in ("id", "class"):
            if self._root.get(key):
                bits.append(f"{key}={self._root.get(key)!r}")
        text = self.own_text
        if text:
            bits.append(f"text={text[:30] + '...' if len(text) > 30 else text!r}")
        return f"<Selector {' '.join(bits)}>"


class SelectorList(list):  # type: ignore[type-arg]
    """A list of :class:`Selector` objects with the same query helpers."""

    @overload
    def __getitem__(self, index: SupportsIndex) -> Selector: ...

    @overload
    def __getitem__(self, index: slice) -> SelectorList: ...

    def __getitem__(self, index):  # type: ignore[no-untyped-def]
        result = super().__getitem__(index)
        return SelectorList(result) if isinstance(index, slice) else result

    def __iter__(self) -> Iterator[Selector]:
        return super().__iter__()

    def css(self, query: str, **kwargs: Any) -> SelectorList:
        """Run a CSS query on every selector and flatten the results."""
        return SelectorList(r for s in self for r in s.css(query, **kwargs))

    def xpath(self, query: str, **kwargs: Any) -> SelectorList:
        return SelectorList(r for s in self for r in s.xpath(query, **kwargs))

    def select(self, query: str, **kwargs: Any) -> SelectorList:
        return SelectorList(r for s in self for r in s.select(query, **kwargs))

    def get(self, default: str | None = None) -> str | None:
        """``get()`` of the first selector, or ``default`` if the list is empty."""
        for sel in self:
            return sel.get(default)
        return default

    def getall(self) -> list[str]:
        return [v for s in self for v in s.getall()]

    @property
    def first(self) -> Selector | None:
        return self[0] if self else None

    @property
    def last(self) -> Selector | None:
        return self[-1] if self else None

    @property
    def text(self) -> str | None:
        """Text of the first selector (``None`` if empty)."""
        return self[0].text if self else None

    @property
    def texts(self) -> list[str]:
        """Text of every selector."""
        return [s.text for s in self]

    @property
    def attrib(self) -> dict[str, str]:
        """Attributes of the first element (``{}`` if empty)."""
        return self[0].attrib if self else {}

    def attr(self, name: str, default: str | None = None) -> str | None:
        """An attribute of the first element that has it."""
        for sel in self:
            value = sel.attr(name)
            if value is not None:
                return value
        return default

    def attrs(self, name: str) -> list[str]:
        """An attribute of every element that has it."""
        return [v for v in (s.attr(name) for s in self) if v is not None]

    def re(self, pattern: str | Pattern[str], flags: int = 0) -> list[str]:
        return [m for s in self for m in s.re(pattern, flags)]

    def re_first(self, pattern: str | Pattern[str], default: str | None = None, flags: int = 0) -> str | None:
        for sel in self:
            found = sel.re(pattern, flags)
            if found:
                return found[0]
        return default

    def filter(self, predicate: Callable[[Selector], bool]) -> SelectorList:
        return SelectorList(s for s in self if predicate(s))

    def __repr__(self) -> str:
        if len(self) > 5:
            return f"[{', '.join(map(repr, list.__getitem__(self, slice(0, 5))))}, ...({len(self)} total)]"
        return list.__repr__(self)


def _record(elements: list[etree._Element]) -> dict[str, Any]:
    return {
        "count": len(elements),
        "elements": [fingerprint(el) for el in elements[:_MAX_SAVED_ELEMENTS]],
    }


def _regex_all(pattern: str | Pattern[str], text: str, flags: int = 0) -> list[str]:
    rx = re.compile(pattern, flags) if isinstance(pattern, str) else pattern
    if "extract" in rx.groupindex:
        return [m.group("extract") for m in rx.finditer(text) if m.group("extract") is not None]
    out: list[str] = []
    for m in rx.finditer(text):
        if rx.groups:
            out.extend(g for g in m.groups() if g is not None)
        else:
            out.append(m.group(0))
    return out


def _compile_many(patterns: str | Iterable[str] | None) -> list[Pattern[str]]:
    if not patterns:
        return []
    if isinstance(patterns, str):
        patterns = [patterns]
    return [re.compile(p) for p in patterns]


def _host_in(host: str, domains: list[str]) -> bool:
    host = host.lower()
    for d in domains:
        d = d.lower().lstrip(".")
        if d.startswith("www."):
            d = d[4:]
        if host == d or host.endswith("." + d) or host == "www." + d:
            return True
    return False
