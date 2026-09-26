"""The :class:`Response` returned by every fetcher."""

from __future__ import annotations

import codecs
import json as _json
import logging
import re
from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from pathlib import Path
from re import Pattern
from typing import TYPE_CHECKING, Any

from ..errors import HTTPStatusError
from ..parser.selector import Selector, SelectorList

if TYPE_CHECKING:
    from ..adaptive.storage import AdaptiveStorage
    from ..request import Request

log = logging.getLogger("wintergrab.fetch")


class Headers(MutableMapping[str, str]):
    """Case-insensitive header mapping (repeated headers are joined with ``, ``)."""

    def __init__(self, items: Mapping[str, str] | Iterable[tuple[str, str]] | None = None) -> None:
        self._data: dict[str, tuple[str, list[str]]] = {}
        if items is None:
            return
        pairs = items.items() if isinstance(items, Mapping) else items
        for key, value in pairs:
            self.add(key, value)

    def add(self, key: str, value: str) -> None:
        low = key.lower()
        if low in self._data:
            self._data[low][1].append(value)
        else:
            self._data[low] = (key, [value])

    def get_list(self, key: str) -> list[str]:
        """Every value of a repeated header (e.g. ``Set-Cookie``)."""
        entry = self._data.get(key.lower())
        return list(entry[1]) if entry else []

    def __getitem__(self, key: str) -> str:
        return ", ".join(self._data[key.lower()][1])

    def __setitem__(self, key: str, value: str) -> None:
        self._data[key.lower()] = (key, [value])

    def __delitem__(self, key: str) -> None:
        del self._data[key.lower()]

    def __iter__(self) -> Iterator[str]:
        return (original for original, _ in self._data.values())

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key.lower() in self._data

    def __repr__(self) -> str:
        return f"Headers({dict(self.items())!r})"


_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_:.+-]+)""", re.I)
_XML_ENCODING = re.compile(rb"""^\s*<\?xml[^>]+encoding\s*=\s*["']([a-zA-Z0-9_.+-]+)""", re.I)
_BOMS = ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"))


def _valid_codec(name: str | None) -> str | None:
    if not name:
        return None
    try:
        return codecs.lookup(name.strip().strip("\"'")).name
    except LookupError:
        return None


def detect_encoding(content_type: str | None, body: bytes) -> str:
    """Charset from the Content-Type header, a BOM, ``<meta>``/XML declaration, else UTF-8."""
    if content_type:
        match = re.search(r"charset\s*=\s*([^\s;]+)", content_type, re.I)
        found = _valid_codec(match.group(1)) if match else None
        if found:
            return found
    for bom, name in _BOMS:
        if body.startswith(bom):
            return name
    head = body[:4096]
    for rx in (_META_CHARSET, _XML_ENCODING):
        declared = rx.search(head)
        if declared:
            found = _valid_codec(declared.group(1).decode("ascii", "ignore"))
            if found:
                return found
    return "utf-8"


class Response:
    """A downloaded page. Query it directly: ``response.css("h1::text").get()``.

    Attributes:
        url: Final URL (after redirects).
        status: HTTP status code.
        headers: Case-insensitive response headers.
        body: Raw bytes.
        request: The :class:`~wintergrab.Request` that produced it.
        cookies: Cookies set by the server (name -> value).
        elapsed: Seconds the download took.
        history: URLs of redirects that were followed.
        source: ``"http"`` or ``"browser"``.
    """

    def __init__(
        self,
        url: str,
        *,
        status: int = 200,
        headers: Headers | Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
        body: bytes = b"",
        request: Request | None = None,
        reason: str = "",
        encoding: str | None = None,
        cookies: Mapping[str, str] | None = None,
        elapsed: float = 0.0,
        history: list[str] | None = None,
        http_version: str | None = None,
        source: str = "http",
        adaptive_storage: AdaptiveStorage | None = None,
    ) -> None:
        self.url = url
        self.status = status
        self.headers = headers if isinstance(headers, Headers) else Headers(headers)
        self.body = body
        self.request = request
        self.reason = reason
        self.cookies = dict(cookies or {})
        self.elapsed = elapsed
        self.history = list(history or [])
        self.http_version = http_version
        self.source = source
        self._encoding = encoding
        self._text: str | None = None
        self._selector: Selector | None = None
        self._adaptive_storage = adaptive_storage
        #: ``"hit"`` (served from cache), ``"revalidated"`` (a 304 confirmed the
        #: cached copy), ``"stored"`` (downloaded and cached) or ``None``.
        self.cache_status: str | None = None
        #: XHR/fetch responses recorded by a browser fetcher with ``capture=``.
        self.captured: list[Any] = []
        #: Full cookie records (domain, path, expiry...) for browser responses.
        self.cookie_jar: list[dict[str, Any]] = []
        #: IP address of the server that answered (``None`` if unknown, e.g. from the cache).
        self.ip: str | None = None
        #: Browser sub-requests blocked while rendering, by reason (``"type:image"``, ``"list"``, ``"policy"``...).
        self.blocked_resources: dict[str, int] = {}
        #: A browser fetch's full-page PNG screenshot (``screenshot=True``).
        self.screenshot: bytes | None = None
        #: What a browser fetch's ``actions`` did, step by step: ``{"step", "ok", "detail"}``.
        self.actions: list[dict[str, Any]] = []
        #: The page's HTML kept by ``tabs`` and ``snapshot`` actions: ``{"after", "html"}``.
        self.snapshots: list[dict[str, str]] = []
        #: Files kept by ``download`` actions: ``{"url", "path", "name", "bytes"}``.
        self.downloads: list[dict[str, Any]] = []
        #: The page's console messages and uncaught script errors (browser fetches): ``{"type", "text"}``.
        self.console: list[dict[str, str]] = []
        self._layout: Any = None
        self._pdf: Any = None

    # ------------------------------------------------------------------ #
    # body
    # ------------------------------------------------------------------ #
    @property
    def encoding(self) -> str:
        if self._encoding is None:
            self._encoding = detect_encoding(self.headers.get("content-type"), self.body)
        return self._encoding

    @property
    def text(self) -> str:
        """The body decoded to a string (like ``requests``' ``.text``)."""
        if self._text is None:
            self._text = self.body.decode(self.encoding, errors="replace")
            if self._text.startswith("﻿"):
                self._text = self._text[1:]
        return self._text

    @property
    def content(self) -> bytes:
        """Alias of :attr:`body`."""
        return self.body

    def json(self, **kwargs: Any) -> Any:
        """Parse the body as JSON."""
        return _json.loads(self.text, **kwargs)

    @property
    def content_type(self) -> str:
        """Media type without parameters, e.g. ``"text/html"``."""
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def is_pdf(self) -> bool:
        """Whether the body is a PDF (its type says so, or it starts like one)."""
        from ..parser.pdf import is_pdf

        return is_pdf(self.body, self.content_type)

    @property
    def pdf(self) -> Any:
        """The PDF the body holds, read (a :class:`~wintergrab.parser.pdf.PdfDocument`), or ``None`` for other
        bodies. Needs ``pypdf`` (``pip install 'wintergrab[pdf]'``)."""
        if self._pdf is None and self.is_pdf:
            from ..parser.pdf import read_pdf

            self._pdf = read_pdf(self.body)
        return self._pdf

    @property
    def layout(self) -> Any:
        """Where the page's text is drawn (a :class:`~wintergrab.parser.layout.Layout`): recorded by a browser
        fetch with ``layout=True``, or read from a PDF; ``None`` otherwise."""
        if self._layout is None and self.is_pdf:
            document = self._readable_pdf()
            if document is not None:
                self._layout = document.layout()
        return self._layout

    @layout.setter
    def layout(self, value: Any) -> None:
        self._layout = value

    def _readable_pdf(self) -> Any:
        """The PDF, read; ``None`` (said once) when it cannot be: no ``pypdf``, or a damaged or locked file."""
        try:
            return self.pdf
        except (ImportError, ValueError) as exc:
            if not getattr(self, "_pdf_warned", False):
                self._pdf_warned = True
                log.warning("%s: %s", self.url, exc)
            return None

    @property
    def is_html(self) -> bool:
        ctype = self.content_type
        if ctype:
            return "html" in ctype
        return self.body.lstrip()[:100].lower().startswith((b"<!doctype html", b"<html"))

    @property
    def from_cache(self) -> bool:
        """``True`` if the body came from the HTTP cache (a hit or a 304 revalidation)."""
        return self.cache_status in ("hit", "revalidated")

    @property
    def ok(self) -> bool:
        """``True`` for 1xx-3xx statuses."""
        return self.status < 400

    @property
    def meta(self) -> dict[str, Any]:
        """``request.meta`` - data you attached to the request in a spider."""
        return self.request.meta if self.request is not None else {}

    def captured_json(self, url_contains: str | None = None) -> list[Any]:
        """Parsed JSON bodies of captured API calls (browser fetches with ``capture=``)."""
        out = []
        for item in self.captured:
            if url_contains and url_contains not in item.url:
                continue
            try:
                out.append(item.json())
            except ValueError:
                continue
        return out

    def raise_for_status(self) -> Response:
        """Raise :class:`~wintergrab.errors.HTTPStatusError` for 4xx/5xx."""
        if self.status >= 400:
            raise HTTPStatusError(self)
        return self

    # ------------------------------------------------------------------ #
    # parsing (delegated to a lazily built Selector)
    # ------------------------------------------------------------------ #
    @property
    def selector(self) -> Selector:
        """The parsed document (built on first use)."""
        if self._selector is None:
            ctype = self.content_type
            if self.is_pdf:  # its text, headings, tables and links, as HTML (or nothing when it cannot be read)
                document = self._readable_pdf()
                markup = document.html() if document is not None else "<html><body></body></html>"
                self._selector = Selector(markup, url=self.url, adaptive_storage=self._adaptive_storage)
                return self._selector
            kind = "xml" if ("xml" in ctype and "html" not in ctype) else "html"
            self._selector = Selector(self.text, url=self.url, type=kind, adaptive_storage=self._adaptive_storage)
        return self._selector

    def css(self, query: str, **kwargs: Any) -> SelectorList:
        """CSS query on the page. See :meth:`Selector.css` (``adaptive=True`` etc.)."""
        return self.selector.css(query, **kwargs)

    def xpath(self, query: str, **kwargs: Any) -> SelectorList:
        """XPath query on the page. See :meth:`Selector.xpath`."""
        return self.selector.xpath(query, **kwargs)

    def select(self, query: str, **kwargs: Any) -> SelectorList:
        """CSS or XPath, guessed from the query."""
        return self.selector.select(query, **kwargs)

    def find_by_text(self, text: str, **kwargs: Any) -> SelectorList:
        return self.selector.find_by_text(text, **kwargs)

    def find_by_regex(self, pattern: str | Pattern[str], **kwargs: Any) -> SelectorList:
        return self.selector.find_by_regex(pattern, **kwargs)

    def extract(self, schema: Mapping[str, Any]) -> dict[str, Any]:
        """Extract a dict with a schema of selectors. See :meth:`Selector.extract`."""
        return self.selector.extract(schema)

    def extract_all(self, query: str, schema: Mapping[str, Any], **kwargs: Any) -> list[dict[str, Any]]:
        """Extract one dict per element matched by ``query``."""
        return self.selector.extract_all(query, schema, **kwargs)

    def links(self, css: str | None = None, **kwargs: Any) -> list[str]:
        """Absolute URLs of the page's links. See :meth:`Selector.links`."""
        return self.selector.links(css, **kwargs)

    def re(self, pattern: str | Pattern[str], flags: int = 0) -> list[str]:
        """Regex over the raw body text."""
        from ..parser.selector import _regex_all

        return _regex_all(pattern, self.text, flags)

    def re_first(self, pattern: str | Pattern[str], default: str | None = None, flags: int = 0) -> str | None:
        found = self.re(pattern, flags)
        return found[0] if found else default

    @property
    def title(self) -> str | None:
        """Contents of ``<title>``."""
        return self.selector.css("title").text

    def get_text(self) -> str:
        """Readable text of the page body."""
        body = self.selector.css("body")
        return (body[0] if body else self.selector).get_text()

    def markdown(self, *, main_content: bool = False) -> str:
        """The page converted to Markdown."""
        return self.selector.markdown(main_content=main_content)

    # ------------------------------------------------------------------ #
    # zero-selector extraction (see Selector)
    # ------------------------------------------------------------------ #
    def structured_data(self) -> dict[str, Any]:
        """JSON-LD, microdata, RDFa, OpenGraph, Twitter cards and meta tags of the page."""
        return self.selector.structured_data()

    def embedded_json(self) -> dict[str, Any]:
        """JSON state embedded by JavaScript apps (``__NEXT_DATA__``, ``window.__STATE__``...)."""
        return self.selector.embedded_json()

    def find_json(self, key: Any, *, limit: int | None = None) -> list[Any]:
        """Every value under ``key`` in the page's embedded JSON / JSON-LD - or in the body, for JSON responses."""
        if "json" in self.content_type:
            from ..parser.structured import find_values

            try:
                return find_values(self.json(), key, limit=limit)
            except ValueError:
                return []
        return self.selector.find_json(key, limit=limit)

    def tables(self) -> list[dict[str, Any]]:
        """Every HTML table as records."""
        return self.selector.tables()

    def next_page(self) -> str | None:
        """URL of the "next page" link, if the page has pagination."""
        return self.selector.next_page()

    def detect_records(self, **kwargs: Any) -> list[Any]:
        """Repeating record groups on the page, best first (see :meth:`Selector.detect_records`)."""
        return self.selector.detect_records(**kwargs)

    def auto_extract(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Records from the page's main repeating list, fields inferred automatically."""
        return self.selector.auto_extract(**kwargs)

    def learn(self, examples: Any) -> Any:
        """Learn a reusable extraction schema from example values (see :meth:`Selector.learn`)."""
        return self.selector.learn(examples)

    def urljoin(self, url: str) -> str:
        """Resolve a relative URL against this page."""
        return self.selector.urljoin(url) if self.is_html else _urljoin(self.url, url)

    # ------------------------------------------------------------------ #
    # following links (spiders)
    # ------------------------------------------------------------------ #
    def follow(self, url: str | Selector, callback: Any = None, **kwargs: Any) -> Request:
        """A :class:`Request` for a link on this page (relative URLs are fine).

        ``url`` may be a string, an ``<a>`` element selector, or a
        ``::attr(href)`` text selector.
        """
        from ..request import Request

        if isinstance(url, Selector):
            href = url.get() if not url.is_element else (url.attr("href") or url.attr("src"))
            if not href:
                raise ValueError(f"{url!r} has no href")
            url = href
        return Request(self.urljoin(url), callback=callback, **kwargs)

    def follow_next(self, callback: Any = None, **kwargs: Any) -> Request | None:
        """A :class:`Request` for the next page of a paginated listing (``None`` on the last page).

        ``yield response.follow_next()`` in a callback is all a pagination loop needs.
        """
        url = self.next_page()
        return self.follow(url, callback=callback, **kwargs) if url else None

    def follow_all(
        self,
        css: str | None = None,
        *,
        urls: Iterable[str | Selector] | None = None,
        callback: Any = None,
        allow: str | Iterable[str] | None = None,
        deny: str | Iterable[str] | None = None,
        same_domain: bool = False,
        **kwargs: Any,
    ) -> list[Request]:
        """Requests for every link matched by ``css`` (or given in ``urls``).

        ``css`` may select ``<a>`` elements, containers holding links, or
        ``::attr(href)`` values. With neither ``css`` nor ``urls``, every link
        on the page is followed.
        """
        if urls is not None:
            return [self.follow(u, callback=callback, **kwargs) for u in urls]
        found = self.links(css, allow=allow, deny=deny, same_domain=same_domain)
        return [self.follow(u, callback=callback, **kwargs) for u in found]

    # ------------------------------------------------------------------ #
    # misc
    # ------------------------------------------------------------------ #
    def save(self, path: str | Path) -> Path:
        """Write the raw body to a file and return its path."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.body)
        return target

    def __repr__(self) -> str:
        return f"<Response {self.status} {self.url}>"


def _urljoin(base: str, url: str) -> str:
    from urllib.parse import urljoin

    return urljoin(base, url.strip())
