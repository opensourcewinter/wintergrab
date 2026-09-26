"""Machine-readable page data: JSON-LD, microdata, RDFa, meta tags, SPA state, tables and pagination."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from itertools import repeat
from typing import Any, NamedTuple
from urllib.parse import urldefrag, urljoin

from lxml import etree

from .text import normalize_space, tag_name, text_content

__all__ = [
    "canonical_url",
    "embedded_json",
    "find_values",
    "next_page_url",
    "structured_data",
    "table_to_records",
    "tables",
]

_FAIL: Any = object()  # "could not parse" marker (``None`` is a valid JSON value)
_NO_KEY: Any = object()  # key of list items in :func:`find_values`
_DECODER = json.JSONDecoder(strict=False)  # tolerate raw control characters inside strings

_WS = re.compile(r"\s*")
_SEPARATORS = re.compile(r"[\s,;]*")
_JSON_SPECIAL = re.compile(r'["\\,]')
_HEX = re.compile(r"[0-9A-Fa-f]+")
_SURROGATE = re.compile(r"[\ud800-\udfff]")


class _Source(str):
    """A ``str`` whose ``count``/``rfind`` are O(1).

    ``JSONDecodeError`` calls both to report line/column numbers, so every failed ``raw_decode`` at
    offset ``n`` would cost O(n) - quadratic when probing many candidates in a big script.
    """

    __slots__ = ()

    def count(self, *args: Any) -> int:
        return 0

    def rfind(self, *args: Any) -> int:
        return -1


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def _absolute(url: str | None, base_url: str | None) -> str:
    """``url`` resolved against ``base_url`` (unchanged when there is no base)."""
    url = (url or "").strip()
    if base_url and url:
        try:
            return urljoin(base_url, url)
        except ValueError:
            return url
    return url


def _add(target: dict[str, Any], key: str, value: Any) -> None:
    """Store ``value`` under ``key``, turning repeated keys into lists."""
    if key not in target:
        target[key] = value
    elif isinstance(target[key], list):
        target[key].append(value)
    else:
        target[key] = [target[key], value]


def _unique_key(target: dict[str, Any], key: str) -> str:
    """``key``, or ``key_2``, ``key_3``... if it is already taken."""
    if key not in target:
        return key
    n = 2
    while f"{key}_{n}" in target:
        n += 1
    return f"{key}_{n}"


def _skip(pattern: re.Pattern[str], text: str, pos: int) -> int:
    """Position after ``pattern`` matched at ``pos`` (for patterns that always match, like ``\\s*``)."""
    m = pattern.match(text, pos)
    return m.end() if m else pos


def _rel(el: etree._Element) -> list[str]:
    return (el.get("rel") or "").lower().split()


def _script_type(el: etree._Element) -> str:
    """The script's MIME type, lower-cased and without parameters."""
    return (el.get("type") or "").split(";", 1)[0].strip().lower()


# --------------------------------------------------------------------------- #
# lenient JSON
# --------------------------------------------------------------------------- #

_PREFIXES = ("<!--", "//<![CDATA[", "/*<![CDATA[*/", "<![CDATA[")
_SUFFIXES = ("//-->", "-->", "//]]>", "/*]]>*/", "]]>")


def _unwrap(text: str) -> str:
    """Strip BOMs and HTML comment / CDATA wrappers around a script body."""
    text = text.strip().strip("\ufeff").strip()
    changed = True
    while changed:
        changed = False
        for prefix in _PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix) :].strip().strip("\ufeff").strip()
                changed = True
        for suffix in _SUFFIXES:
            if text.endswith(suffix):
                text = text[: -len(suffix)].strip()
                changed = True
    return text


def _strip_trailing_commas(text: str) -> str:
    """Remove commas directly before ``}`` or ``]`` (outside strings)."""
    out: list[str] = []
    pos, n, in_string = 0, len(text), False
    while True:
        m = _JSON_SPECIAL.search(text, pos)
        if m is None:
            out.append(text[pos:])
            return "".join(out)
        i = m.start()
        ch = text[i]
        if ch == "\\":
            end = i + 2 if in_string else i + 1
            out.append(text[pos:end])
            pos = end
        elif ch == '"':
            in_string = not in_string
            out.append(text[pos : i + 1])
            pos = i + 1
        else:  # comma
            after = _skip(_WS, text, i + 1)
            dangling = not in_string and after < n and text[after] in "}]"
            out.append(text[pos:i] if dangling else text[pos : i + 1])
            pos = i + 1


def _load_json(raw: str) -> Any:
    """Parse a script's JSON body, forgiving common mistakes; ``_FAIL`` if hopeless."""
    text = _unwrap(raw)
    if not text:
        return _FAIL
    try:
        return _DECODER.decode(text)
    except (ValueError, RecursionError):
        pass
    text = _strip_trailing_commas(text)
    try:
        return _DECODER.decode(text)
    except (ValueError, RecursionError):
        pass
    # Several values back to back ("{...}{...}" or "{...},{...}").
    values: list[Any] = []
    pos, n = 0, len(text)
    while True:
        pos = _skip(_SEPARATORS, text, pos)
        if pos >= n:
            break
        try:
            value, pos = _DECODER.raw_decode(text, pos)
        except (ValueError, RecursionError):
            return _FAIL
        values.append(value)
    return values if len(values) > 1 else _FAIL


# --------------------------------------------------------------------------- #
# structured data: JSON-LD, microdata, OpenGraph, Twitter cards, <meta>
# --------------------------------------------------------------------------- #


def structured_data(root: etree._Element, base_url: str | None = None) -> dict[str, Any]:
    """Everything a page declares about itself in machine-readable form.

    Returns ``{"json_ld": [...], "microdata": [...], "rdfa": [...], "opengraph": {...}, "twitter": {...},
    "meta": {...}}``. Microdata and RDFa items are nested dicts of the same shape (``@type``, ``@id``, the
    properties). ``base_url`` makes link-like values absolute (pass the page URL, or the ``<base href>`` one).
    """
    opengraph, twitter, meta = _meta_tags(root, base_url)
    return {
        "json_ld": _json_ld(root),
        "microdata": _microdata(root, base_url),
        "rdfa": _rdfa(root, base_url),
        "opengraph": opengraph,
        "twitter": twitter,
        "meta": meta,
    }


def canonical_url(root: etree._Element, base_url: str | None = None) -> str | None:
    """The page's canonical URL (``<link rel="canonical" href>``), absolute against ``base_url``; ``None`` without
    one."""
    for link in root.iter("link"):
        href = link.get("href")
        if href and "canonical" in (link.get("rel") or "").lower().split():
            return _absolute(href.strip(), base_url) if href.strip() else None
    return None


def _json_ld(root: etree._Element) -> list[dict[str, Any]]:
    """Every JSON-LD object on the page as one flat list (``@graph`` and lists expanded)."""
    out: list[dict[str, Any]] = []
    for script in root.iter("script"):
        if _script_type(script) != "application/ld+json":
            continue
        value = _load_json(script.text or "")
        if value is _FAIL:
            continue
        for item in value if isinstance(value, list) else [value]:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if graph is None:
                out.append(item)
                continue
            context = item.get("@context")
            for node in graph if isinstance(graph, list) else [graph]:
                if isinstance(node, dict):
                    if context is not None and "@context" not in node:
                        node = {"@context": context, **node}
                    out.append(node)
    return out


# Which attribute holds an itemprop's value (URL-valued ones are made absolute).
_MICRODATA_URL_ATTRS = {
    "a": "href",
    "area": "href",
    "link": "href",
    "img": "src",
    "audio": "src",
    "video": "src",
    "source": "src",
    "embed": "src",
    "iframe": "src",
    "track": "src",
    "object": "data",
}
_MICRODATA_ATTRS = {"meta": "content", "time": "datetime", "data": "value", "meter": "value"}
_MAX_ITEM_DEPTH = 64


def _microdata(root: etree._Element, base_url: str | None) -> list[dict[str, Any]]:
    """Top-level microdata items (``itemscope`` without ``itemprop``) as nested dicts."""
    scopes = root.xpath("descendant-or-self::*[@itemscope and not(@itemprop)]")
    return [_microdata_item(scope, base_url, 0) for scope in scopes]


def _microdata_item(scope: etree._Element, base_url: str | None, depth: int) -> dict[str, Any]:
    item: dict[str, Any] = {}
    item_type = normalize_space(scope.get("itemtype") or "")
    if item_type:
        item["@type"] = item_type
    item_id = (scope.get("itemid") or "").strip()
    if item_id:
        item["@id"] = _absolute(item_id, base_url)
    stack = [c for c in reversed(scope) if isinstance(c.tag, str)]
    while stack:
        el = stack.pop()
        is_scope = el.get("itemscope") is not None
        names = (el.get("itemprop") or "").split()
        if names:
            if is_scope and depth < _MAX_ITEM_DEPTH:
                value: Any = _microdata_item(el, base_url, depth + 1)
            else:
                value = _microdata_value(el, base_url)
            for name in names:
                _add(item, name, value)
        if not is_scope:  # a nested item's properties belong to it, not to us
            stack.extend(c for c in reversed(el) if isinstance(c.tag, str))
    return item


def _microdata_value(el: etree._Element, base_url: str | None) -> str:
    name = tag_name(el)
    if name in _MICRODATA_URL_ATTRS:
        url = el.get(_MICRODATA_URL_ATTRS[name])
        if url is not None:
            return _absolute(url, base_url)
    elif name in _MICRODATA_ATTRS:
        value = el.get(_MICRODATA_ATTRS[name])
        if value is not None:
            return value.strip()
    elif el.get("content") is not None:  # common (if non-standard) on <span itemprop="price" content="9.99">
        return (el.get("content") or "").strip()
    return text_content(el)


# RDFa (Lite, and the value attributes of RDFa 1.1): ``vocab``, ``prefix``, ``typeof``, ``property``, ``about``,
# ``resource``, ``content``, ``datatype``. Items are read like microdata's: an element with ``typeof`` is an item,
# its ``property`` descendants are its properties, and a ``property`` element with ``typeof`` of its own is a nested
# item. Terms are expanded with the vocabulary in effect and the prefixes declared (the common ones are known);
# a property of the vocabulary keeps its short name (``name``), one of another vocabulary its CURIE
# (``dc:creator``), as microdata keeps its ``itemprop``.
_RDFA_PREFIXES = {
    "schema": "https://schema.org/",
    "og": "http://ogp.me/ns#",
    "article": "http://ogp.me/ns/article#",
    "book": "http://ogp.me/ns/book#",
    "profile": "http://ogp.me/ns/profile#",
    "product": "http://ogp.me/ns/product#",
    "fb": "http://ogp.me/ns/fb#",
    "dc": "http://purl.org/dc/terms/",
    "dcterms": "http://purl.org/dc/terms/",
    "dc11": "http://purl.org/dc/elements/1.1/",
    "foaf": "http://xmlns.com/foaf/0.1/",
    "gr": "http://purl.org/goodrelations/v1#",
    "v": "http://rdf.data-vocabulary.org/#",
    "vcard": "http://www.w3.org/2006/vcard/ns#",
    "sioc": "http://rdfs.org/sioc/ns#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "cc": "http://creativecommons.org/ns#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "owl": "http://www.w3.org/2002/07/owl#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "prov": "http://www.w3.org/ns/prov#",
    "dcat": "http://www.w3.org/ns/dcat#",
}
_RDFA_URL_ATTRS = {**_MICRODATA_URL_ATTRS}  # (the same elements carry a resource in an attribute)
_SCHEMA_ORG = ("https://schema.org/", "http://schema.org/")


class _RdfaEnv:
    """The vocabulary and prefixes in effect on an element (``vocab`` and ``prefix`` are inherited)."""

    __slots__ = ("prefixes", "vocab")

    def __init__(self, vocab: str | None, prefixes: dict[str, str]) -> None:
        self.vocab = vocab
        self.prefixes = prefixes

    def under(self, el: etree._Element) -> _RdfaEnv:
        """The environment inside ``el``: its own ``vocab`` and ``prefix`` declarations over the inherited ones."""
        vocab, prefixes = self.vocab, self.prefixes
        declared = el.get("vocab")
        if declared is not None:
            vocab = declared.strip() or None  # (vocab="" ends the inherited vocabulary)
        mapping = el.get("prefix")
        if mapping:
            tokens = mapping.split()
            found = {
                tokens[i].rstrip(":"): tokens[i + 1]
                for i in range(0, len(tokens) - 1, 2)
                if tokens[i].endswith(":") and tokens[i + 1]
            }
            if found:
                prefixes = {**prefixes, **found}
        return self if vocab == self.vocab and prefixes is self.prefixes else _RdfaEnv(vocab, prefixes)

    def expand(self, term: str) -> str:
        """A term or CURIE as a full IRI (``Product`` -> ``https://schema.org/Product`` under that vocabulary;
        ``schema:name`` -> ``https://schema.org/name``); an IRI, or a term without a vocabulary, as it is."""
        if "://" in term or term.startswith(("#", "/", "_:")):
            return term
        prefix, colon, local = term.partition(":")
        if colon and prefix in self.prefixes:
            return self.prefixes[prefix] + local
        if colon and prefix in ("http", "https", "urn", "mailto", "tel"):
            return term
        return f"{self.vocab}{term}" if self.vocab and not colon else term

    def property_name(self, term: str) -> str:
        """The name a property is kept under: its short name under the vocabulary in effect or schema.org,
        else the CURIE or IRI as written."""
        iri = self.expand(term)
        for namespace in ((self.vocab,) if self.vocab else ()) + _SCHEMA_ORG:
            if namespace and iri.startswith(namespace) and len(iri) > len(namespace):
                return iri[len(namespace) :]
        return term


def _rdfa_env_of(el: etree._Element) -> _RdfaEnv:
    """The environment on ``el``, from the declarations of its ancestors and its own."""
    env = _RdfaEnv(None, _RDFA_PREFIXES)
    for ancestor in reversed(list(el.iterancestors())):
        env = env.under(ancestor)
    return env.under(el)


def _rdfa(root: etree._Element, base_url: str | None) -> list[dict[str, Any]]:
    """Top-level RDFa items (``typeof`` without ``property``) as nested dicts, like microdata's."""
    scopes = root.xpath("descendant-or-self::*[@typeof and not(@property)]")
    return [_rdfa_item(scope, _rdfa_env_of(scope), base_url, 0) for scope in scopes]


def _rdfa_item(scope: etree._Element, env: _RdfaEnv, base_url: str | None, depth: int) -> dict[str, Any]:
    item: dict[str, Any] = {}
    types = [env.expand(t) for t in (scope.get("typeof") or "").split()]
    if types:
        item["@type"] = types[0] if len(types) == 1 else types
    subject = scope.get("about") if scope.get("about") is not None else scope.get("resource")
    if subject is not None and subject.strip():
        item["@id"] = _absolute(subject.strip(), base_url)
    stack = [(c, env) for c in reversed(scope) if isinstance(c.tag, str)]
    while stack:
        el, inherited = stack.pop()
        inside = inherited.under(el)
        is_scope = el.get("typeof") is not None
        names = (el.get("property") or "").split()
        if names:
            if is_scope and depth < _MAX_ITEM_DEPTH:
                value: Any = _rdfa_item(el, inside, base_url, depth + 1)
            else:
                value = _rdfa_value(el, base_url)
            for name in names:
                _add(item, inside.property_name(name), value)
        if not is_scope:  # a nested item's properties belong to it, not to us
            stack.extend((c, inside) for c in reversed(el) if isinstance(c.tag, str))
    return item


def _rdfa_value(el: etree._Element, base_url: str | None) -> str:
    content = el.get("content")
    if content is not None:
        return content.strip()
    resource = el.get("resource")
    if resource is not None and resource.strip():
        return _absolute(resource.strip(), base_url)
    name = tag_name(el)
    if name in _RDFA_URL_ATTRS:
        url = el.get(_RDFA_URL_ATTRS[name])
        if url is not None:
            return _absolute(url, base_url)
    elif name in _MICRODATA_ATTRS:
        value = el.get(_MICRODATA_ATTRS[name])
        if value is not None:
            return value.strip()
    return text_content(el)


_OG_PREFIXES = ("og:", "article:", "product:", "book:", "profile:", "music:", "video:", "fb:", "place:")
_OG_URL_KEYS = frozenset(
    {
        "url",
        "image",
        "image:url",
        "image:secure_url",
        "video",
        "video:url",
        "video:secure_url",
        "audio",
        "audio:url",
        "audio:secure_url",
    }
)
_TWITTER_URL_KEYS = frozenset({"url", "image", "image:src", "player", "player:stream"})
_META_NAMES = ("description", "keywords", "author", "robots", "geo.position", "icbm", "geo.region", "geo.placename")


def _meta_tags(root: etree._Element, base_url: str | None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(opengraph, twitter, meta) dicts read from ``<meta>``, ``<link>``, ``<title>`` and ``<html lang>``."""
    opengraph: dict[str, Any] = {}
    twitter: dict[str, Any] = {}
    named: dict[str, str] = {}
    for el in root.iter("meta"):
        content = el.get("content")
        content = (content if content is not None else el.get("value") or "").strip()
        if not content:
            continue
        if (el.get("http-equiv") or "").strip().lower() == "content-language":
            named.setdefault("content-language", content)
        seen: list[str] = []
        for attr in ("property", "name"):
            key = (el.get(attr) or "").strip().lower()
            if not key or key in seen:
                continue
            seen.append(key)
            if key.startswith("twitter:"):
                key = key[8:]
                _add(twitter, key, _absolute(content, base_url) if key in _TWITTER_URL_KEYS else content)
            elif key.startswith(_OG_PREFIXES):
                key = key[3:] if key.startswith("og:") else key
                _add(opengraph, key, _absolute(content, base_url) if key in _OG_URL_KEYS else content)
            elif attr == "name":
                named.setdefault(key, content)

    meta: dict[str, Any] = {}
    title = _title(root)
    if title:
        meta["title"] = title
    for name in _META_NAMES:
        if name in named:
            meta[name] = named[name]
    point = meta.get("geo.position") or meta.get("icbm")  # "52.5163;13.3777", "52.5163, 13.3777"
    if point:
        from ..data.normalize.geo import parse_coordinates

        found = parse_coordinates(point.replace(";", ","))
        if found:
            meta["geo.latitude"], meta["geo.longitude"] = str(found[0]), str(found[1])

    canonical = favicon = touch_icon = None
    feeds: list[str] = []
    for el in root.iter("link"):
        href = (el.get("href") or "").strip()
        if not href:
            continue
        rel = _rel(el)
        if canonical is None and "canonical" in rel:
            canonical = _absolute(href, base_url)
        if favicon is None and "icon" in rel:
            favicon = _absolute(href, base_url)
        if touch_icon is None and any(r.startswith("apple-touch-icon") for r in rel):
            touch_icon = _absolute(href, base_url)
        if "alternate" in rel:
            kind = (el.get("type") or "").lower()
            url = _absolute(href, base_url)
            if ("rss" in kind or "atom" in kind) and url not in feeds:
                feeds.append(url)
    if canonical:
        meta["canonical"] = canonical
    language = _language(root) or named.get("content-language")
    if language:
        meta["language"] = language
    if favicon or touch_icon:
        meta["favicon"] = favicon or touch_icon
    if feeds:
        meta["feeds"] = feeds
    return opengraph, twitter, meta


def _title(root: etree._Element) -> str | None:
    for el in root.iter("title"):
        if not any(tag_name(a) == "svg" for a in el.iterancestors()):
            return text_content(el) or None
    return None


def _language(root: etree._Element) -> str | None:
    html = root if tag_name(root) == "html" else next(root.iter("html"), None)
    if html is None:
        return None
    lang = html.get("lang") or html.get("xml:lang") or html.get("{http://www.w3.org/XML/1998/namespace}lang")
    return (lang or "").strip() or None


# --------------------------------------------------------------------------- #
# embedded SPA state
# --------------------------------------------------------------------------- #

# ``window.X =``, ``window["X"] =``, ``self.X =``, ``var|let|const X =`` and bare ``X =``.
_ASSIGNMENT = re.compile(
    r"""
    (?:
        (?<![\w$.])(?:window|self|globalThis)\s*
        (?:
            \.\s*(?P<dot>[A-Za-z_$][\w$]*)
          | \[\s*(?P<quote>["'])(?P<sub>[^"'\\\r\n]{1,256})(?P=quote)\s*\]
        )
      | (?<![\w$.])(?:var|let|const)\s+(?P<decl>[A-Za-z_$][\w$]*)
      | (?<![\w$.])(?P<bare>[A-Za-z_$][\w$]*)
    )
    \s*=(?![=>])
    """,
    re.VERBOSE,
)
_JS_STRING_END = {'"': re.compile(r'["\\\r\n]'), "'": re.compile(r"['\\\r\n]")}
_JS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def embedded_json(root: etree._Element) -> dict[str, Any]:
    """JSON state that single-page apps embed in their HTML, keyed by name.

    Reads ``<script type="application/json">`` (and other ``application/*+json`` types except
    JSON-LD) keyed by ``id`` / ``data-target`` / ``data-name`` (else ``json_script_<n>``), plus
    inline-script assignments such as ``window.__INITIAL_STATE__ = {...}``, ``var state = [...]``
    or ``__APOLLO_STATE__ = JSON.parse("...")`` keyed by the assigned name. Only non-empty dicts and
    lists are kept; values that are not valid JSON are skipped. Repeated names get ``_2``, ``_3``...
    """
    found: dict[str, Any] = {}
    unnamed = 0
    for script in root.iter("script"):
        code = script.text or ""
        if not code.strip():
            continue
        kind = _script_type(script)
        if kind == "application/json" or (
            kind.startswith("application/") and kind.endswith("+json") and kind != "application/ld+json"
        ):
            value = _load_json(code)
            if not _meaningful(value):
                continue
            name = next((v.strip() for v in map(script.get, ("id", "data-target", "data-name")) if v and v.strip()), "")
            if not name:
                unnamed += 1
                name = f"json_script_{unnamed}"
            found[_unique_key(found, name)] = value
        elif kind in ("", "module", "text/babel", "text/jsx") or "javascript" in kind or "ecmascript" in kind:
            for name, value in _assignments(code):
                if _meaningful(value):
                    found[_unique_key(found, name)] = value
    return found


def _meaningful(value: Any) -> bool:
    return isinstance(value, (dict, list)) and bool(value)


def _assignments(code: str) -> Iterator[tuple[str, Any]]:
    """``(name, value)`` for every assignment of a JSON literal (or ``JSON.parse("...")``) in ``code``."""
    code = _Source(code)
    pos = 0
    while True:
        m = _ASSIGNMENT.search(code, pos)
        if m is None:
            return
        name = m.group("dot") or m.group("sub") or m.group("decl") or m.group("bare")
        value, end = _assigned_value(code, _skip(_WS, code, m.end()))
        if value is _FAIL:
            pos = m.end()
        else:
            yield name, value
            pos = end  # never rescan inside a parsed value: keeps the scan linear


def _assigned_value(code: str, start: int) -> tuple[Any, int]:
    """Parse the right-hand side starting at ``start``: a JSON literal or ``JSON.parse(<string>)``."""
    if start >= len(code):
        return _FAIL, start
    if code[start] in "{[":
        try:
            return _DECODER.raw_decode(code, start)
        except (ValueError, RecursionError):
            return _FAIL, start
    if code.startswith("JSON.parse", start):
        pos = _skip(_WS, code, start + 10)
        if code.startswith("(", pos):
            literal = _js_string(code, _skip(_WS, code, pos + 1))
            if literal is not None:
                try:
                    return _DECODER.decode(literal[0].strip()), literal[1]
                except (ValueError, RecursionError):
                    pass
    return _FAIL, start


def _js_string(code: str, start: int) -> tuple[str, int] | None:
    """Decode the JavaScript string literal at ``code[start]``: ``(value, end)`` or ``None``."""
    stop = _JS_STRING_END.get(code[start : start + 1])
    if stop is None:
        return None
    quote = code[start]
    parts: list[str] = []
    pos = start + 1
    while True:
        m = stop.search(code, pos)
        if m is None:
            return None
        i = m.start()
        parts.append(code[pos:i])
        if code[i] == quote:
            break
        if code[i] != "\\":  # raw line break: unterminated literal
            return None
        esc = code[i + 1 : i + 2]
        pos = i + 2
        if not esc:
            return None
        if esc in _JS_ESCAPES:
            parts.append(_JS_ESCAPES[esc])
        elif esc == "x" and (char := _hex_char(code[pos : pos + 2], 2)):
            parts.append(char)
            pos += 2
        elif esc == "u" and code.startswith("{", pos):
            close = code.find("}", pos, pos + 9)
            char = _hex_char(code[pos + 1 : close], 0) if close != -1 else ""
            parts.append(char or esc)
            pos = close + 1 if char else pos
        elif esc == "u" and (char := _hex_char(code[pos : pos + 4], 4)):
            parts.append(char)
            pos += 4
        elif esc == "\r":  # line continuation
            pos += code.startswith("\n", pos)
        elif esc not in "\n\u2028\u2029":
            parts.append(esc)  # \\ \" \' \/ and unknown escapes
    value = "".join(parts)
    if _SURROGATE.search(value):  # join "\ud83d\ude00"-style surrogate pairs
        try:
            value = value.encode("utf-16", "surrogatepass").decode("utf-16")
        except UnicodeDecodeError:
            pass
    return value, i + 1


def _hex_char(digits: str, size: int) -> str:
    """The character for hex ``digits`` ("" unless valid and exactly ``size`` long; 0 = any length)."""
    if not digits or (size and len(digits) != size) or not _HEX.fullmatch(digits):
        return ""
    code = int(digits, 16)
    return chr(code) if code <= 0x10FFFF else ""


# --------------------------------------------------------------------------- #
# searching nested data
# --------------------------------------------------------------------------- #


def find_values(data: Any, key: str | Callable[[Any], bool], *, limit: int | None = None) -> list[Any]:
    """Every value stored under ``key`` in nested dicts/lists, depth-first in document order.

    ``key`` may also be a predicate called with each dict key. ``limit`` stops after that many
    values. Iterative, so arbitrarily deep data is fine; cycles are not followed.
    """
    if limit is not None and limit <= 0:
        return []
    matches: Callable[[Any], bool] = key if callable(key) else (lambda k: k == key)
    out: list[Any] = []
    first = _items(data)
    if first is None:
        return out
    stack: list[tuple[int, Iterator[tuple[Any, Any]]]] = [(id(data), first)]
    walking = {id(data)}  # containers currently on the stack, to break cycles
    while stack:
        pair = next(stack[-1][1], None)
        if pair is None:
            walking.discard(stack.pop()[0])
            continue
        k, value = pair
        if k is not _NO_KEY and matches(k):
            out.append(value)
            if limit is not None and len(out) >= limit:
                break
        children = _items(value)
        if children is not None and id(value) not in walking:
            stack.append((id(value), children))
            walking.add(id(value))
    return out


def _items(value: Any) -> Iterator[tuple[Any, Any]] | None:
    if isinstance(value, dict):
        return iter(value.items())
    if isinstance(value, (list, tuple)):
        return zip(repeat(_NO_KEY), value)
    return None


# --------------------------------------------------------------------------- #
# tables
# --------------------------------------------------------------------------- #

_MAX_SPAN = 1000
_DIGITS = re.compile(r"\s*(\d+)")


class _Row(NamedTuple):
    values: list[str]
    all_th: bool  # every cell of the row itself (not spans from above) is a <th>
    spanned: bool  # some cell has colspan/rowspan > 1
    head: bool  # the row is inside <thead>


def tables(root: etree._Element, base_url: str | None = None) -> list[dict[str, Any]]:
    """Every ``<table>`` with at least one cell, as records (see :func:`table_to_records`)."""
    return [record for table in root.iter("table") if (record := _table_records(table)) is not None]


def table_to_records(table_el: etree._Element, base_url: str | None = None) -> dict[str, Any]:
    """One table as ``{"caption", "headers", "rows", "id", "class"}``.

    ``rows`` is a list of ``{header: cell text}`` dicts. ``colspan``/``rowspan`` are expanded, so a
    spanning cell's text repeats in every column/row it covers. Headers come from ``<thead>``
    (stacked header rows joined with " / "), else from a first row made only of ``<th>``, else
    ``column_1``... Nested tables' rows are not included. ``base_url`` is accepted for symmetry
    with the other helpers; cell values are plain text, so it is currently unused.
    """
    record = _table_records(table_el)
    if record is None:
        return {"caption": _caption(table_el), "headers": [], "rows": [], **_table_attrs(table_el)}
    return record


def _table_records(table: etree._Element) -> dict[str, Any] | None:
    grid = _table_grid(table)
    if not grid:
        return None
    width = max(len(row.values) for row in grid)
    head = [row for row in grid if row.head]
    body = [row for row in grid if not row.head]
    if not head and grid[0].all_th:
        # A header row with spans may continue on the next all-<th> row (multi-level headers).
        n = 1
        while n < len(grid) - 1 and grid[n - 1].spanned and grid[n].all_th:
            n += 1
        head, body = grid[:n], grid[n:]
    headers = _header_names(head, width)
    rows: list[dict[str, str]] = []
    for row in body:
        values = row.values + [""] * (width - len(row.values))
        if any(values):
            rows.append(dict(zip(headers, values, strict=True)))
    return {"caption": _caption(table), "headers": headers, "rows": rows, **_table_attrs(table)}


def _caption(table: etree._Element) -> str | None:
    for child in table:
        if tag_name(child) == "caption":
            return text_content(child) or None
    return None


def _table_attrs(table: etree._Element) -> dict[str, str | None]:
    return {"id": (table.get("id") or "").strip() or None, "class": normalize_space(table.get("class") or "") or None}


def _table_grid(table: etree._Element) -> list[_Row]:
    """This table's rows (thead, body, tfoot order) with colspan/rowspan expanded."""
    sections: dict[str, list[tuple[etree._Element | None, etree._Element]]] = {"thead": [], "body": [], "tfoot": []}
    stack: list[tuple[etree._Element, etree._Element | None]] = [(c, None) for c in reversed(table)]
    while stack:
        el, group = stack.pop()
        name = tag_name(el)
        if not name or name == "table":  # nested tables own their rows
            continue
        if name == "tr":
            section = tag_name(group) if group is not None else "body"
            sections[section if section in sections else "body"].append((group, el))
            continue
        if group is None and name in ("thead", "tbody", "tfoot"):
            group = el
        stack.extend((c, group) for c in reversed(el))

    grid: list[_Row] = []
    carried: dict[int, list[Any]] = {}  # column -> [rows still covered, text] for rowspans
    last_group: Any = _NO_KEY
    for section, rows in sections.items():
        for group, tr in rows:
            if group is not last_group:  # rowspans never cross row groups
                carried.clear()
                last_group = group
            filled: dict[int, str] = {}
            for col, entry in list(carried.items()):
                filled[col] = entry[1]
                entry[0] -= 1
                if entry[0] <= 0:
                    del carried[col]
            cells = [c for c in tr if tag_name(c) in ("td", "th")]
            if not cells:
                continue
            col = 0
            spanned = False
            for cell in cells:
                while col in filled:
                    col += 1
                colspan = _span(cell.get("colspan"), zero=1)
                rowspan = _span(cell.get("rowspan"), zero=_MAX_SPAN)
                spanned = spanned or colspan > 1 or rowspan > 1
                value = text_content(cell)
                for k in range(col, col + colspan):
                    filled[k] = value
                    if rowspan > 1:
                        carried[k] = [rowspan - 1, value]
                col += colspan
            values = [filled.get(i, "") for i in range(max(filled) + 1)]
            all_th = all(tag_name(c) == "th" for c in cells)
            grid.append(_Row(values, all_th, spanned, section == "thead"))
    return grid


def _span(raw: str | None, zero: int) -> int:
    """A colspan/rowspan value clamped to 1..1000 (``0`` means ``zero``)."""
    m = _DIGITS.match(raw or "")
    if m is None:
        return 1
    n = int(m.group(1))
    return min(n if n > 0 else zero, _MAX_SPAN)


def _header_names(head: list[_Row], width: int) -> list[str]:
    """Unique column names from the header rows (stacked texts joined with " / ")."""
    names: list[str] = []
    for i in range(width):
        parts: list[str] = []
        for row in head:
            text = row.values[i] if i < len(row.values) else ""
            if text and text not in parts:
                parts.append(text)
        name = " / ".join(parts) or f"column_{i + 1}"
        if name in names:
            n = 2
            while f"{name}_{n}" in names:
                n += 1
            name = f"{name}_{n}"
        names.append(name)
    return names


# --------------------------------------------------------------------------- #
# pagination
# --------------------------------------------------------------------------- #

_NEXT_WORD = re.compile(r"\bnext\b", re.IGNORECASE)
# Link texts meaning "next page", best first (lower rank wins).
_NEXT_TEXTS = {
    "next": 0,
    "next page": 0,
    "older posts": 1,
    "older entries": 1,
    "older": 1,
    "load more": 1,
    "more results": 1,
}
_RIGHT_ARROWS = ">\u203a\u2192\u276f\u3009\u27e9"  # > and similar single arrows
_DOUBLE_ARROWS = (">>", "\u00bb", "\u226b")  # >>, guillemet, much-greater-than
_ARROW_TEXTS = {**dict.fromkeys(_RIGHT_ARROWS, 2), **dict.fromkeys(_DOUBLE_ARROWS, 3)}
# Arrows, guillemets and punctuation around words such as "Next" or "Older posts".
_DECORATION = (
    " \u00a0.\u2026:|<>\u2190\u2192\u2039\u203a\u00ab\u00bb\u276e\u276f\u3008\u3009\u27e8\u27e9"
    "\u226a\u226b\u25b6\u25ba\u25b8\u2794\u279c\u279d\u279e\u27f6"
)
_PAGE_NUMBER = re.compile(r"\D{0,20}?(\d{1,6})\D{0,20}")
_CURRENT_MARKERS = ("active", "current", "selected")
_NUMBER_TAGS = frozenset({"span", "strong", "em", "b", "i", "li", "button"})


def next_page_url(root: etree._Element, base_url: str | None = None) -> str | None:
    """Absolute URL of the "next page" link, or ``None`` on the last page.

    Tries, in order: ``<link rel="next">``, ``<a rel="next">``, an ``aria-label``/``title``
    mentioning "next", link text such as "Next", "Next \u2192", a lone arrow, "Older posts" or "Load more", a
    ``class``/``id`` containing "next" (not "prev"), and finally numbered pagination (the link
    to the current page number + 1). ``javascript:``/``#``/``mailto:`` links, disabled links and
    links back to ``base_url`` itself are ignored.
    """
    page = urldefrag(base_url)[0] if base_url else None
    for link in root.iter("link"):
        if "next" in _rel(link):
            url = _usable_href(link, base_url, page)
            if url:
                return url

    def usable(a: etree._Element) -> str | None:
        return None if _disabled(a) else _usable_href(a, base_url, page)

    # Pass by pass, resolving URLs only for the candidates: most pages have no "next" link at all.
    anchors = [a for a in root.iter("a") if _href(a)]
    for a in anchors:
        if "next" in _rel(a) and (url := usable(a)):
            return url
    for a in anchors:
        label = " ".join(filter(None, (a.get("aria-label"), a.get("title"))))
        if label and _NEXT_WORD.search(label) and (url := usable(a)):
            return url
    texts = [text_content(a) for a in anchors]
    ranked: list[tuple[int, int, etree._Element]] = []
    for i, (a, text) in enumerate(zip(anchors, texts, strict=True)):
        rank = _next_text_rank(text)
        if rank is not None:
            ranked.append((rank, i, a))
    for _, _, a in sorted(ranked, key=lambda r: r[:2]):  # best rank first, then page order
        if url := usable(a):
            return url
    for a in anchors:
        if (_marked_next(a) or _marked_next(a.getparent())) and (url := usable(a)):
            return url
    numbered = [a for a, text in zip(anchors, texts, strict=True) if _page_number(text) is not None]
    return _numbered_next(root, numbered, usable) if numbered else None


def _href(el: etree._Element) -> str | None:
    """The element's ``href``, unless it goes nowhere (``#top``, ``javascript:``, ``mailto:``...)."""
    href = (el.get("href") or "").strip()
    if not href or href.startswith("#") or href.lower().startswith(("javascript:", "mailto:", "tel:", "data:")):
        return None
    return href


def _usable_href(el: etree._Element, base_url: str | None, page: str | None) -> str | None:
    href = _href(el)
    if href is None:
        return None
    url = _absolute(href, base_url)
    if page and urldefrag(url)[0] == page:
        return None
    return url


def _disabled(a: etree._Element) -> bool:
    if (a.get("aria-disabled") or "").lower() == "true":
        return True
    parent = a.getparent()
    return any("disabled" in (el.get("class") or "").lower().split() for el in (a, parent) if el is not None)


def _next_text_rank(text: str) -> int | None:
    text = text.lower()
    if not text or len(text) > 40:
        return None
    if text in _ARROW_TEXTS:
        return _ARROW_TEXTS[text]
    return _NEXT_TEXTS.get(text.strip(_DECORATION))


def _marked_next(el: etree._Element | None) -> bool:
    if el is None:
        return False
    marker = f"{el.get('class') or ''} {el.get('id') or ''}".lower()
    return "next" in marker and "prev" not in marker


def _is_pager(el: etree._Element) -> bool:
    if tag_name(el) == "nav" or (el.get("role") or "").lower() == "navigation":
        return True
    return "pag" in " ".join(filter(None, (el.get("class"), el.get("id"), el.get("aria-label")))).lower()


def _page_number(text: str) -> int | None:
    m = _PAGE_NUMBER.fullmatch(text) if len(text) <= 50 else None
    return int(m.group(1)) if m else None


def _current_pages(container: etree._Element) -> list[int]:
    """Candidate current page numbers: aria-current first, then active/current classes, then bare numbers."""
    aria: list[int] = []
    marked: list[int] = []
    plain: list[int] = []
    for el in container.iter(etree.Element):
        if el is container:
            continue
        if (el.get("aria-current") or "").lower() in ("page", "true"):
            bucket = aria
        elif any(word in (el.get("class") or "").lower() for word in _CURRENT_MARKERS):
            bucket = marked
        elif (
            tag_name(el) in _NUMBER_TAGS
            and len(el) <= 2
            and el.find(".//a") is None
            and not any(tag_name(a) == "a" for a in el.iterancestors())
        ):
            bucket = plain
        else:
            continue
        number = _page_number(text_content(el))
        if number is not None:
            bucket.append(number)
    return list(dict.fromkeys(aria + marked + plain))


def _numbered_next(
    root: etree._Element, numbered: list[etree._Element], usable: Callable[[etree._Element], str | None]
) -> str | None:
    """In a pagination block, the link numbered one past the current page. Only the blocks around
    ``numbered`` (the links whose text is a number) can have one."""
    containers: list[etree._Element] = []
    seen: set[etree._Element] = set()
    for a in numbered:
        el: etree._Element | None = a
        while el is not None and el not in seen:
            seen.add(el)
            if _is_pager(el):
                containers.append(el)
            el = None if el is root else el.getparent()
    if len(containers) > 1:  # innermost (smallest) first, then in page order
        order = {el: i for i, el in enumerate(root.iter())}
        containers.sort(key=lambda el: (sum(1 for _ in el.iter()), order[el]))
    for container in containers:
        for current in _current_pages(container):
            for a in container.iter("a"):
                if _page_number(text_content(a)) == current + 1 and (url := usable(a)):
                    return url
    return None
