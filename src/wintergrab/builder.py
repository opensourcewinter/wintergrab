"""The visual scraper builder: point at the parts of a page, get an extraction schema.

::

    wintergrab build https://books.toscrape.com/ -o books.schema.json
    # in the page it opens: a book (a repeated card), its title and price (fields),
    # the "next" link (pagination); Test; Save. Then:
    wintergrab crawl https://books.toscrape.com/ --extract books.schema.json -o books.jsonl

The page is fetched once and shown in the builder without its scripts: they
are removed, the frame showing it is sandboxed, and its content security
policy allows none. Clicking an element there proposes a selector for it:

* **a field**: selectors that find it on the page (its attributes meant for
  machines, its classes, a label beside it, its tag, its place under an
  ancestor with a class, its place on the page), each with what it matches,
  and a name and type guessed from the element;
* **a repeated card**: the elements like it (the page's product cards,
  results, rows): the schema's ``container``. Fields clicked in a card then
  get selectors relative to it, chosen to work in every card;
* **a table**: a table of records (its rows the cards, a field per column),
  or a table of one record's properties (a field per row, found by its
  label);
* **the next page**: the link to the next page of records: ``next_page``.

The specification is a :class:`~wintergrab.data.Schema`, shown as JSON and
editable in place. **Test** extracts the page with it through the normal
:class:`~wintergrab.extraction.Extractor`; **Save** writes it to the file
given when the builder started (and to no other). A file that is already
there is loaded, to be carried on with.

The server listens on this machine only (``127.0.0.1``) and answers only to
its own address. Every change needs a token that only its page knows, sent
as JSON, so other sites cannot drive it.
"""

from __future__ import annotations

import copy
import hmac
import json
import logging
import os
import re
import secrets
from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from lxml import etree

from .data.schema import Schema
from .errors import ConfigurationError, SchemaError, WintergrabError
from .extraction import Extractor
from .extraction.generate import _anchored_path, _label_selector, _selectors_for
from .extraction.strategies import _class_rating, _element_value
from .fetchers.response import Response
from .parser import Selector
from .parser.autoextract import (
    _container_selector,
    _follow,
    _is_inside,
    _pick_selector,
    _rel_path,
    _vocab,
    detect_records,
)
from .parser.selector import parse_document
from .parser.text import tag_name, text_content
from .utils import replace_file

__all__ = ["DEFAULT_PORT", "BuilderSession", "serve"]

log = logging.getLogger("wintergrab.builder")

#: Where ``wintergrab build`` listens by default.
DEFAULT_PORT = 8711
_ID = "data-wg"  # the number of each element of the page shown
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_MAX_BODY = 2 * 1024 * 1024
_REMOVED = frozenset({"script", "iframe", "frame", "frameset", "object", "embed", "applet", "portal", "base"})
_URL_ATTRS = ("href", "src", "action", "formaction", "xlink:href", "data", "poster")
_STATIC = {
    "/builder.js": ("builder.js", "text/javascript; charset=utf-8"),
    "/builder.css": ("builder.css", "text/css; charset=utf-8"),
}
#: Words in an element's classes or ``itemprop``, and the field (name, type) they suggest.
_FIELD_WORDS = [
    (("price", "cost", "amount"), ("price", "money")),
    (("rating", "stars", "score"), ("rating", "rating")),
    (("availability", "stock", "instock"), ("availability", "availability")),
    (("date", "time", "published", "posted"), ("date", "date")),
    (("author", "byline"), ("author", "string")),
    (("description", "desc", "summary", "excerpt"), ("description", "text")),
    (("sku", "upc", "mpn"), ("sku", "string")),
    (("brand", "manufacturer"), ("brand", "string")),
    (("category", "breadcrumb"), ("category", "string")),
    (("image", "img", "thumb", "thumbnail", "photo"), ("image", "url")),
    (("title", "name", "heading", "headline"), ("name", "string")),
    (("email",), ("email", "email")),
    (("phone", "tel", "telephone"), ("phone", "phone")),
    (("address", "location"), ("address", "string")),
]
_MONEY = re.compile(r"^\s*(?:[$€£¥₹]|USD|EUR|GBP|INR)\s?\d[\d.,]*\s*$|^\s*\d[\d.,]*\s?(?:€|EUR|USD|GBP|kr)\s*$")
_NUMBER = re.compile(r"^\s*-?\d[\d,]*(?:\.\d+)?\s*$")


class BuilderSession:
    """A page being built on (see the module docs).

    Args:
        page: The page, as fetched.
        output: The schema file Save writes (loaded when it is there already).
        name: The records' name, for a new schema.
    """

    def __init__(self, page: Response, output: str | os.PathLike[str], *, name: str | None = None) -> None:
        self.page = page
        self.url = page.url
        self.output = Path(output)
        # The page parsed twice from the same bytes: one tree to read, one to show, numbered alike.
        self.root = parse_document(page.body, "html", self.url)
        self.selector = Selector(root=self.root, url=self.url)
        self.elements = [el for el in self.root.iter() if isinstance(el.tag, str)]
        self._numbers = {el: i for i, el in enumerate(self.elements)}
        self._view = parse_document(page.body, "html", self.url)
        shown = [el for el in self._view.iter() if isinstance(el.tag, str)]
        if len(shown) != len(self.elements):  # cannot happen: the same bytes parse alike
            raise ConfigurationError("the page parsed differently twice")
        for number, el in enumerate(shown):
            el.set(_ID, str(number))
        self.spec: dict[str, Any] = {"name": name or "record", "fields": {}}
        if self.output.exists():
            try:
                self.spec = Schema.load(self.output).to_dict()
            except (WintergrabError, OSError, ValueError) as exc:
                raise ConfigurationError(f"{self.output} is not a schema to carry on with: {exc}") from exc

    # -- the page shown -------------------------------------------------------------------------- #
    def view_html(self) -> str:
        """The page as the builder shows it: numbered elements, no scripts, links resolved against
        the page's address."""
        doc = copy.deepcopy(self._view)
        for el in list(doc.iter()):
            if not isinstance(el.tag, str):
                continue
            tag = tag_name(el)
            refresh = tag == "meta" and (el.get("http-equiv") or "").lower() == "refresh"
            if tag in _REMOVED or refresh:
                _remove(el)
                continue
            for name in list(el.attrib):
                value = (el.get(name) or "").strip().lower()
                if name.lower().startswith("on") or (
                    name in _URL_ATTRS and value.startswith(("javascript:", "vbscript:"))
                ):
                    del el.attrib[name]
        head = doc.find("head")
        if head is None:
            head = etree.Element("head")
            doc.insert(0, head)
        base = etree.Element("base", href=self.url)
        head.insert(0, base)
        return "<!DOCTYPE html>\n" + etree.tostring(doc, method="html", encoding="unicode")

    def element(self, number: Any) -> etree._Element:
        """The page's element numbered ``number``."""
        try:
            index = int(number)
            if index < 0:
                raise IndexError(index)
            return self.elements[index]
        except (TypeError, ValueError, IndexError) as exc:
            raise ConfigurationError(f"no element {number!r} on the page") from exc

    def number(self, el: etree._Element) -> int | None:
        """``el``'s number (to show it in the page)."""
        return self._numbers.get(el)

    # -- what the builder proposes --------------------------------------------------------------- #
    def field(self, number: Any, container: str | None = None) -> dict[str, Any]:
        """Selectors for a field read from element ``number`` (inside a card when ``container`` is given),
        each with what it reads, and a name, a type and a way to read it guessed from the element."""
        el = self.element(number)
        name, kind, attr = _guess(el)
        suffix = f"::attr({attr})" if attr else ""
        if container:
            cards = [c for c in self._select(container) if isinstance(c.tag, str)]
            holder = next((i for i, card in enumerate(cards) if _is_inside(el, card)), None)
            if holder is None:
                raise ConfigurationError("that element is not inside a card: click one inside the cards")
            rel = _rel_path(cards[holder], el)
            targets = [el if i == holder else _follow(card, rel) for i, card in enumerate(cards)]
            query = _pick_selector(cards, targets, _vocab(cards), must=[holder]) or tag_name(el)
            found = [self._read_in(card, query + suffix, kind) for card in cards]
            candidates = [{"selector": query + suffix, "matches": sum(1 for v in found if v not in (None, "")),
                           "values": found[:8]}]  # fmt: skip
        else:
            candidates = []
            proposed = _selectors_for(el, self.root)
            label = _label_selector(el)
            if label and all(q != label for q, _ in proposed):
                proposed.append((label, 2))
            for query, _rank in proposed[:8]:
                matched = self._select(query)
                values = [_value(Selector(root=m, url=self.url), kind, attr) for m in matched[:5]]
                candidates.append({"selector": query + suffix, "matches": len(matched), "values": values})
            candidates.sort(key=lambda c: (c["matches"] != 1,))  # one match first: it names this element
        return {
            "id": self.number(el),
            "tag": tag_name(el),
            "text": text_content(el)[:200],
            "name": name,
            "type": kind,
            "read": f"attribute {attr}" if attr else "text",
            "candidates": candidates,
        }

    def card(self, number: Any) -> dict[str, Any]:
        """The repeated card element ``number`` is in: its selector, how many there are, their numbers,
        and fields found in them."""
        el = self.element(number)
        groups = detect_records(self.root, min_records=2, max_groups=10)
        group = next((g for g in groups if any(_is_inside(el, m) for m in g.elements)), None)
        if group is not None:
            members, container, fields = list(group.elements), group.container_selector, dict(group.fields)
        else:
            from .adaptive.fingerprint import similar_elements

            members, fields = [], {}
            for node in [el, *el.iterancestors()]:
                if tag_name(node) in ("body", "html"):
                    break
                peers = similar_elements(node, root=self.root)
                if peers:
                    members = sorted([node, *peers], key=lambda e: self._numbers.get(e, 0))
                    break
            if not members:
                raise ConfigurationError("nothing like that element repeats on the page")
            container = _container_selector(members, self.root)
        matched = [c for c in self._select(container) if isinstance(c.tag, str)]
        suggested = [{"name": name, "type": _type_named(name), "selector": query} for name, query in fields.items()]
        return {
            "container": container,
            "count": len(matched),
            "ids": [self.number(c) for c in matched[:1000]],
            "suggested": suggested,
        }

    def table(self, number: Any) -> dict[str, Any]:
        """The table element ``number`` is in: a table of records (a card per row, a field per column),
        or of one record's properties (a field per row, found by its label)."""
        el = self.element(number)
        table = el if tag_name(el) == "table" else next((a for a in el.iterancestors() if tag_name(a) == "table"), None)
        if table is None:
            raise ConfigurationError("that element is not in a table")
        rows = [
            r for r in table.iter("tr") if next((a for a in r.iterancestors() if tag_name(a) == "table"), None) is table
        ]
        cells = [[c for c in r if isinstance(c.tag, str) and tag_name(c) in ("td", "th")] for r in rows]
        if rows and all(len(c) == 2 and tag_name(c[0]) == "th" for c in cells):  # label, value
            fields = []
            for row in cells:
                label = _label_selector(row[1])
                if label:
                    fields.append({"name": _slug(text_content(row[0])), "type": _type_for(text_content(row[1])),
                                   "selector": label, "value": text_content(row[1])[:120]})  # fmt: skip
            return {"kind": "properties", "fields": fields}
        data = [(r, c) for r, c in zip(rows, cells, strict=True) if any(tag_name(x) == "td" for x in c)]
        if not data:
            raise ConfigurationError("that table has no rows of data")
        header = next((c for c in cells if c and all(tag_name(x) == "th" for x in c)), [])
        names = [_slug(text_content(h)) for h in header]
        first = data[0][1]
        fields = []
        for j, cell in enumerate(first):
            tag = tag_name(cell)
            k = 1 + sum(1 for x in first[:j] if tag_name(x) == tag)
            name = names[j] if j < len(names) and names[j] else f"column_{j + 1}"
            fields.append({"name": name, "type": _type_for(text_content(cell)), "selector": f"{tag}:nth-of-type({k})",
                           "value": text_content(cell)[:120]})  # fmt: skip
        container = _container_selector([r for r, _ in data], self.root)
        matched = [c for c in self._select(container) if isinstance(c.tag, str)]
        return {
            "kind": "records",
            "container": container,
            "count": len(matched),
            "ids": [self.number(c) for c in matched[:1000]],
            "fields": fields,
        }

    def next_page(self, number: Any) -> dict[str, Any]:
        """A selector for the link to the next page (element ``number``, or the link it is in)."""
        el = self.element(number)
        link = el if tag_name(el) == "a" else next((a for a in el.iterancestors() if tag_name(a) == "a"), None)
        if link is None:
            inside = el.findall(".//a[@href]")
            link = inside[0] if len(inside) == 1 else None
        if link is None or not link.get("href"):
            raise ConfigurationError("that is not a link")
        proposed = [q for q, _ in _selectors_for(link, self.root)]
        anchored = _anchored_path(link)
        if anchored:  # "li.next > a": the pager's own classes first
            proposed = [anchored, *(q for q in proposed if q != anchored)]
        # the selector should find this link only: the "next" among pagination links
        unique = [q for q in proposed if len(self._select(q)) == 1] or proposed
        return {
            "selector": unique[0] if unique else None,
            "candidates": unique[:5],
            "url": urljoin(self.url, link.get("href") or ""),
            "detected": self.page.next_page(),
        }

    # -- testing and saving ----------------------------------------------------------------------- #
    def test(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        """The records ``spec`` reads on the page, as the extractor reads them."""
        schema = _schema(spec)
        extractor = Extractor(schema)
        records = extractor.extract_all(self.page) if schema.container else [extractor.extract(self.page)]
        rows = []
        for record in records[:50]:
            rows.append({name: {"value": _plain(fv.value), "method": fv.method, "confidence": fv.confidence}
                         for name, fv in record.fields.items()})  # fmt: skip
        filled = {name: sum(1 for r in records if r.data.get(name) not in (None, "", [])) for name in schema.names}
        following = self.page.links(schema.next_page) if schema.next_page else []
        return {"records": len(records), "rows": rows, "filled": filled, "next": following[0] if following else None}

    def save(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        """Write ``spec`` to the builder's file (nowhere else), and say how to use it."""
        schema = _schema(spec)
        self.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.output.with_name(f".{self.output.name}.{os.getpid()}.tmp")
        schema.save(temporary)
        replace_file(temporary, self.output)
        self.spec = schema.to_dict()
        if schema.container:
            command = f"wintergrab crawl {self.url} --extract {self.output} -o {schema.name}.jsonl"
        else:
            command = f"wintergrab get {self.url} --extract {self.output}"
        return {"path": str(self.output), "command": command}

    def state(self) -> dict[str, Any]:
        return {"url": self.url, "output": str(self.output), "spec": self.spec}

    def _select(self, query: str) -> list[etree._Element]:
        try:
            return [m.root for m in self.selector.select(query) if m.root is not None]
        except Exception as exc:
            raise ConfigurationError(f"{query!r} is not a selector wintergrab reads: {exc}") from exc

    def _read_in(self, card: etree._Element, query: str, kind: str) -> Any:
        try:
            matched = Selector(root=card, url=self.url).select(query)
        except Exception:
            return None
        return _value(matched[0], kind, None) if matched else None


def _remove(el: etree._Element) -> None:
    """Take ``el`` out of its tree, keeping the text after it."""
    parent = el.getparent()
    if parent is None:
        return
    if el.tail:
        previous = el.getprevious()
        if previous is not None:
            previous.tail = (previous.tail or "") + el.tail
        else:
            parent.text = (parent.text or "") + el.tail
    parent.remove(el)


def _schema(spec: Mapping[str, Any]) -> Schema:
    if not isinstance(spec, Mapping):
        raise ConfigurationError("the specification must be a JSON object")
    try:
        return Schema.from_dict(spec)
    except SchemaError as exc:
        raise ConfigurationError(f"the specification is not a valid schema: {exc}") from exc


def _plain(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    return str(value)


def _value(match: Selector, kind: str, attr: str | None) -> Any:
    """What a selector's match reads, as the extractor would read it."""
    if attr and match.is_element:
        return match.attr(attr)
    kinds = {"url": "url", "money": "price", "rating": "rating"}
    return _element_value(match, kinds.get(kind, "other"))


def _guess(el: etree._Element) -> tuple[str, str, str | None]:
    """A name, a type and an attribute to read (``None``: its text) for a field read from ``el``."""
    tag = tag_name(el)
    text = text_content(el)
    words = re.split(r"[\s_\-]+", " ".join(filter(None, [el.get("itemprop"), el.get("class")])).lower())
    title = el.get("title") or ""
    shortened = text.rstrip().endswith(("...", "…")) and title.startswith(text.rstrip(" .…"))
    if tag == "img":
        return "image", "url", None
    if tag in ("time",) or el.get("datetime"):
        return "date", "date", None
    if el.get("class") and _class_rating(el.get("class") or "") and not text:
        return "rating", "rating", "class"
    for keys, (name, kind) in _FIELD_WORDS:
        if any(word in keys for word in words):
            return name, kind, "title" if shortened else None
    if tag == "h1":
        return "name", "string", None
    if tag == "a":
        return ("title" if text else "url"), ("string" if text else "url"), "title" if shortened else None
    kind = _type_for(text)
    return {"money": "price", "number": "number"}.get(kind, "field"), kind, None


def _type_for(text: str) -> str:
    if _MONEY.match(text):
        return "money"
    if _NUMBER.match(text):
        return "number"
    return "string"


def _type_named(name: str) -> str:
    """The type of a field named ``name`` (the fields :func:`detect_records` names)."""
    return {"price": "money", "url": "url", "image": "url", "rating": "rating", "date": "date",
            "availability": "availability"}.get(name, "string")  # fmt: skip


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40]
    return slug if slug and not slug[0].isdigit() else f"field_{slug}" if slug else "field"


# ---------------------------------------------------------------------------------------------- #
# the server
# ---------------------------------------------------------------------------------------------- #
#: The builder's own page: its script and style only, and requests to itself.
_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "frame-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


#: The page shown: its images, styles and fonts from its site; no scripts, forms, frames or plugins, and
#: sandboxed even when opened on its own.
_VIEW_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; img-src * data: blob:; style-src * 'unsafe-inline'; "
    "font-src * data:; media-src *; form-action 'none'; frame-ancestors 'self'; sandbox allow-same-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = "wintergrab-builder"

    def version_string(self) -> str:
        return self.server_version  # (the Server header names no Python version)

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("%s " + format, self.address_string(), *args)

    def do_GET(self) -> None:
        if not self._host_allowed():
            self._send(HTTPStatus.FORBIDDEN, "text/plain; charset=utf-8", b"not for this host\n")
            return
        path = urlsplit(self.path).path
        session = self.server.session
        if path == "/":
            self._send(HTTPStatus.OK, "text/html; charset=utf-8", _index(self.server.token).encode("utf-8"))
        elif path in _STATIC:
            name, kind = _STATIC[path]
            self._send(HTTPStatus.OK, kind, (files("wintergrab") / "static" / name).read_bytes())
        elif path == "/page":
            body = session.view_html().encode("utf-8")
            self._send(HTTPStatus.OK, "text/html; charset=utf-8", body, _VIEW_HEADERS)
        elif path == "/api/state":
            self._json(HTTPStatus.OK, session.state())
        else:
            self._send(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"not found\n")

    def do_POST(self) -> None:
        if not self._host_allowed() or not self._origin_allowed():
            self._send(HTTPStatus.FORBIDDEN, "text/plain; charset=utf-8", b"not for this host\n")
            return
        token = self.headers.get("X-Wintergrab-Token", "")
        if not hmac.compare_digest(token.encode(), self.server.token.encode()):
            self._json(HTTPStatus.FORBIDDEN, {"error": "the builder's token is missing: use the builder's page"})
            return
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "send JSON"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > _MAX_BODY:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "too big"})
            return
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("expected an object")
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": f"not JSON: {exc}"})
            return
        session = self.server.session
        actions = {
            "/api/field": lambda: session.field(data.get("id"), data.get("container") or None),
            "/api/card": lambda: session.card(data.get("id")),
            "/api/table": lambda: session.table(data.get("id")),
            "/api/next": lambda: session.next_page(data.get("id")),
            "/api/test": lambda: session.test(data.get("spec") or {}),
            "/api/save": lambda: session.save(data.get("spec") or {}),
        }
        action = actions.get(urlsplit(self.path).path)
        if action is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "no such action"})
            return
        try:
            self._json(HTTPStatus.OK, action())
        except ConfigurationError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception:
            log.exception("builder: %s failed", self.path)
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "something went wrong (see the log)"})

    def do_PUT(self) -> None:
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, "text/plain; charset=utf-8", b"not allowed\n")

    do_DELETE = do_PATCH = do_PUT

    def _host_allowed(self) -> bool:
        if not self.server.loopback:
            return True  # listening beyond this machine was asked for
        host = self.headers.get("Host", "")
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host[1:].split("]", 1)[0]
        return name.lower() in _LOOPBACK

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True  # not sent by a browser page
        return urlsplit(origin).netloc == self.headers.get("Host", "")

    def _json(self, status: HTTPStatus, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body)

    def _send(self, status: HTTPStatus, kind: str, body: bytes, headers: Mapping[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or _HEADERS).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def _index(token: str) -> str:
    """The builder's page (its script and style are served beside it)."""
    return (files("wintergrab") / "static" / "builder.html").read_text(encoding="utf-8").replace("{{token}}", token)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], session: BuilderSession) -> None:
        super().__init__(address, _Handler)
        self.session = session
        self.token = secrets.token_urlsafe(24)
        self.loopback = address[0] in _LOOPBACK or address[0].startswith("127.")

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        name = host.decode() if isinstance(host, bytes) else str(host)
        shown = f"[{name}]" if ":" in name else name
        return f"http://{shown}:{port}/"


def serve(
    session: BuilderSession,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
) -> _Server:
    """A builder server for ``session`` (not started: call ``serve_forever()``, or run it in a thread)."""
    server = _Server((host, port), session)
    if not server.loopback:
        log.warning("the builder listens on %s: anyone who can reach it can read the page and save the schema", host)
    return server
