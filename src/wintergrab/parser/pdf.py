"""PDF documents: their text where it is drawn, as a :class:`~wintergrab.parser.layout.Layout`, and as HTML.

A response holding a PDF (``application/pdf``, or a body starting ``%PDF-``) is read with it:
``response.css()``, ``response.markdown()`` and extraction see the HTML, and ``response.layout`` the
layout, so the tables and labelled values a PDF draws are read as a page's are
(:mod:`wintergrab.extraction.visual`)::

    response = wg.get("https://oak.example/prices.pdf")
    response.pdf.title, len(response.pdf.pages)       # 'Oak furniture: prices', 1
    print(response.markdown())                        # headings, lines, tables, links
    layout_tables(response.layout)[0].records()       # [{'Product': 'Oak table', ...}, ...]

Reading PDFs needs ``pypdf`` (``pip install 'wintergrab[pdf]'``). Text is read where the PDF draws it:
each piece's position, font size and boldness come from the PDF; its width is estimated from its
characters (PDFs rarely say). Lines close together make a block; a block of aligned lines is a table.
Scanned pages (pictures of text) have no text to read.
"""

from __future__ import annotations

import html as html_module
import io
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .layout import Box, Layout

__all__ = ["MAX_PAGES", "PdfDocument", "PdfPage", "is_pdf", "read_pdf"]

log = logging.getLogger("wintergrab.pdf")

#: The most pages read from a PDF.
MAX_PAGES = 100
_PAGE_GAP = 24.0  # points between pages in the layout
_BOLD = re.compile(r"bold|black|heavy|semibold|demi", re.I)
# Average advance widths of Helvetica-like fonts, in ems (for pieces whose width a PDF does not give).
_NARROW = frozenset("fijlrtI!|.,:;'`()[]{} ")
_WIDE = frozenset("mwMW@%")


def is_pdf(body: bytes, content_type: str = "") -> bool:
    """Whether a response holds a PDF: its type says so, or its body starts like one."""
    return content_type == "application/pdf" or body[:1024].lstrip()[:5] == b"%PDF-"


@dataclass
class PdfPage:
    """A page of a PDF: its size (points) and its text boxes (from the page's top left), and links."""

    number: int
    width: float
    height: float
    boxes: list[Box] = field(default_factory=list)
    #: ``(uri, x, y, width, height)`` of the page's link annotations.
    links: list[tuple[str, float, float, float, float]] = field(default_factory=list)


@dataclass
class PdfDocument:
    """A PDF, read (see the module docs).

    Attributes:
        pages: Its pages (at most :data:`MAX_PAGES`).
        page_count: How many pages it has.
        title, author, subject: From its metadata.
        truncated: It had more pages than were read.
    """

    pages: list[PdfPage]
    page_count: int
    title: str | None = None
    author: str | None = None
    subject: str | None = None
    truncated: bool = False

    @property
    def text(self) -> str:
        """Its text, line by line."""
        return "\n".join(" ".join(b.text for b in line) for page in self.pages for line in _page_lines(page.boxes))

    def layout(self) -> Layout:
        """Every page's boxes on one layout, the pages one under the other."""
        boxes: list[Box] = []
        top = 0.0
        width = 0.0
        for index, page in enumerate(self.pages, 1):
            prefix = f"body > section:nth-of-type({index})" if len(self.pages) > 1 else "body > section"
            for box in page.boxes:
                boxes.append(
                    Box(
                        box.text,
                        box.x,
                        box.y + top,
                        box.width,
                        box.height,
                        box.lines,
                        box.size,
                        box.weight,
                        box.tag,
                        box.path.replace("body > section", prefix, 1),
                    )
                )
            top += page.height + _PAGE_GAP
            width = max(width, page.width)
        return Layout(boxes, width, max(0.0, top - _PAGE_GAP), self.truncated)

    def html(self) -> str:
        """The document as simple HTML: a section per page, headings by font size, lines as paragraphs, the
        tables its lines are drawn as, and its links."""
        from ..extraction.visual import layout_tables  # the same reading as a page's

        sizes: Counter[float] = Counter()
        for page in self.pages:
            for box in page.boxes:
                sizes[round(box.size, 1)] += len(box.text)
        body_size = sizes.most_common(1)[0][0] if sizes else 10.0
        esc = html_module.escape
        parts = ["<!DOCTYPE html><html><head><meta charset='utf-8'>"]
        if self.title:
            parts.append(f"<title>{esc(self.title)}</title>")
        parts.append("</head><body>")
        for page in self.pages:
            parts.append(f'<section data-page="{page.number}">')
            tables = layout_tables(Layout(page.boxes, page.width, page.height))
            in_table = {id(b): t for t in tables for b in t.boxes}
            written: set[int] = set()
            used_links: set[int] = set()
            for block in _blocks(page.boxes):
                table = next((in_table[id(b)] for line in block for b in line if id(b) in in_table), None)
                if table is not None:
                    if id(table) not in written:
                        written.add(id(table))
                        parts.append(_table_html(table.header, table.rows))
                    continue
                for line in block:
                    text = " ".join(b.text for b in line)
                    size = max(b.size for b in line)
                    tag = "h1" if size >= 1.6 * body_size else "h2" if size >= 1.25 * body_size else "p"
                    link = _link_over(line, page.links)
                    if link is not None:
                        used_links.add(link)
                        uri = page.links[link][0]
                        text_html = f'<a href="{esc(uri)}">{esc(text)}</a>'
                    else:
                        text_html = esc(text)
                    parts.append(f"<{tag}>{text_html}</{tag}>")
            for i, (uri, *_) in enumerate(page.links):
                if i not in used_links:
                    parts.append(f'<p><a href="{esc(uri)}">{esc(uri)}</a></p>')
            parts.append("</section>")
        parts.append("</body></html>")
        return "".join(parts)


def read_pdf(data: bytes, *, max_pages: int = MAX_PAGES) -> PdfDocument:
    """Read a PDF's pages (at most ``max_pages``): their text where it is drawn, and their links.

    Raises:
        ImportError: ``pypdf`` is not installed.
        ValueError: The data is not a PDF that can be read (damaged, or encrypted with a password).
    """
    try:
        import pypdf
    except ImportError:
        raise ImportError("reading PDFs needs pypdf: pip install 'wintergrab[pdf]'") from None
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("the PDF is encrypted with a password")
        count = len(reader.pages)
        metadata: Any = reader.metadata or {}
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"not a PDF that can be read: {exc}") from None
    pages = []
    for number, page in enumerate(reader.pages[:max_pages], 1):
        try:
            pages.append(_read_page(page, number))
        except Exception as exc:  # one bad page does not lose the others
            log.warning("page %d of the PDF could not be read: %s", number, exc)
    return PdfDocument(
        pages,
        count,
        title=_meta(metadata, "/Title"),
        author=_meta(metadata, "/Author"),
        subject=_meta(metadata, "/Subject"),
        truncated=count > max_pages,
    )


def _meta(metadata: Any, key: str) -> str | None:
    value = metadata.get(key)
    text = str(value).strip() if value is not None else ""
    return text or None


def _read_page(page: Any, number: int) -> PdfPage:
    box = page.mediabox
    left, bottom = float(box.left), float(box.bottom)
    width, height = float(box.width), float(box.height)
    pieces: list[tuple[str, float, float, float, int]] = []

    def visit(text: str, cm: Any, tm: Any, font: Any, font_size: float) -> None:
        text = " ".join(text.split())
        if not text:
            return
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4] - left
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5] - bottom
        scale = abs(tm[0] * tm[3] - tm[1] * tm[2]) ** 0.5 * abs(cm[0] * cm[3] - cm[1] * cm[2]) ** 0.5
        size = round(float(font_size) * (scale or 1.0), 1)
        name = str(font.get("/BaseFont", "")) if font else ""
        pieces.append((text, x, y, size, 700 if _BOLD.search(name) else 400))

    page.extract_text(visitor_text=visit)
    boxes = []
    for text, x, baseline, size, weight in pieces:
        top = height - baseline - 0.8 * size
        boxes.append(Box(text, round(x, 1), round(top, 1), round(_width(text, size), 1), size, 1, size, weight, "span"))
    boxes = _with_paths(boxes)
    return PdfPage(number, width, height, boxes, _links(page, left, bottom, height))


def _width(text: str, size: float) -> float:
    """The width of ``text`` in a Helvetica-like font of ``size`` (an estimate: PDFs rarely give it)."""
    ems = sum(0.28 if c in _NARROW else 0.83 if c in _WIDE else 0.67 if c.isupper() else 0.56 for c in text)
    return ems * size


def _page_lines(boxes: list[Box]) -> list[list[Box]]:
    """The page's boxes in lines, top to bottom, each line left to right."""
    lines: list[list[Box]] = []
    for box in sorted(boxes, key=lambda b: (b.middle, b.x)):
        if lines and abs(box.middle - lines[-1][0].middle) <= 0.4 * max(box.height, lines[-1][0].height):
            lines[-1].append(box)
        else:
            lines.append([box])
    return [sorted(line, key=lambda b: b.x) for line in lines]


def _blocks(boxes: list[Box]) -> list[list[list[Box]]]:
    """Lines close together, in blocks (a paragraph, a table): a gap taller than a line starts a new one."""
    blocks: list[list[list[Box]]] = []
    previous: list[Box] | None = None
    for line in _page_lines(boxes):
        if previous is not None:
            gap = min(b.y for b in line) - max(b.bottom for b in previous)
            if gap <= 0.9 * max(b.height for b in line):
                blocks[-1].append(line)
                previous = line
                continue
        blocks.append([line])
        previous = line
    return blocks


def _with_paths(boxes: list[Box]) -> list[Box]:
    """The boxes with the paths of a page's blocks, lines and pieces (``body > section > div > p > span``), so
    a layout's elements group them as a web page's do."""
    out = []
    blocks = _blocks(boxes)
    for b, block in enumerate(blocks, 1):
        block_path = "body > section > div" + (f":nth-of-type({b})" if len(blocks) > 1 else "")
        for n, line in enumerate(block, 1):
            line_path = f"{block_path} > p" + (f":nth-of-type({n})" if len(block) > 1 else "")
            for k, box in enumerate(line, 1):
                path = f"{line_path} > span" + (f":nth-of-type({k})" if len(line) > 1 else "")
                out.append(
                    Box(box.text, box.x, box.y, box.width, box.height, box.lines, box.size, box.weight, "span", path)
                )
    return out


def _links(page: Any, left: float, bottom: float, height: float) -> list[tuple[str, float, float, float, float]]:
    out = []
    for ref in page.get("/Annots") or ():
        try:
            annotation = ref.get_object()
            if annotation.get("/Subtype") != "/Link":
                continue
            action = annotation.get("/A")
            action = action.get_object() if action is not None else None
            uri = str(action.get("/URI", "")) if action is not None else ""
            if not re.match(r"(?:https?:|mailto:)", uri, re.I):
                continue  # links within the document, or scripts
            x1, y1, x2, y2 = (float(v) for v in annotation["/Rect"])
        except Exception:
            continue
        out.append((uri, min(x1, x2) - left, height - (max(y1, y2) - bottom), abs(x2 - x1), abs(y2 - y1)))
    return out


def _link_over(line: list[Box], links: list[tuple[str, float, float, float, float]]) -> int | None:
    """The link annotation drawn over a line's text, if any."""
    top, bottom = min(b.y for b in line), max(b.bottom for b in line)
    first, last = min(b.x for b in line), max(b.right for b in line)
    for i, (_, x, y, w, h) in enumerate(links):
        if y < bottom and top < y + h and x < last and first < x + w:
            return i
    return None


def _table_html(header: list[str] | None, rows: list[list[str]]) -> str:
    esc = html_module.escape
    out = ["<table>"]
    if header:
        out.append("<thead><tr>" + "".join(f"<th>{esc(c)}</th>" for c in header) + "</tr></thead>")
    out.append("<tbody>")
    out.extend("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>" for row in rows)
    out.append("</tbody></table>")
    return "".join(out)
