"""Turning HTML elements into clean text or Markdown."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from lxml import etree

# Elements whose content is never visible text.
SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "head", "svg", "iframe", "object", "canvas"})

BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "body",
        "center",
        "dd",
        "details",
        "dialog",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hgroup",
        "hr",
        "html",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "tbody",
        "thead",
        "tfoot",
        "tr",
        "ul",
        "caption",
        "option",
    }
)

_WS = re.compile(r"\s+")
_INDENT = "\x02"  # placeholder for indentation we want to keep
_CODE = "\x03"  # delimiter around protected (preformatted) blocks


def normalize_space(text: str) -> str:
    """Collapse runs of whitespace into single spaces and strip the ends."""
    return " ".join(text.split())


def tag_name(el: etree._Element) -> str:
    """Lower-cased local tag name ("" for comments and processing instructions)."""
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    if tag.startswith("{"):
        tag = tag.rsplit("}", 1)[1]
    return tag.lower()


def iter_text(el: etree._Element, skip: frozenset[str] = SKIP_TAGS, block_separator: str = "") -> list[str]:
    """All text fragments inside ``el`` in document order, skipping invisible elements.

    ``block_separator`` is inserted around block-level elements (and ``<br>``,
    table cells) so that text from different blocks does not run together.
    """
    out: list[str] = []
    stack: list[etree._Element | str] = [el]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            out.append(node)
            continue
        if not isinstance(node.tag, str):
            continue
        name = tag_name(node)
        if node is not el and name in skip:
            continue
        is_block = block_separator and (name in BLOCK_TAGS or name in ("br", "td", "th"))
        if is_block:
            out.append(block_separator)
            stack.append(block_separator)
        if node.text:
            out.append(node.text)
        for child in reversed(node):
            if child.tail:
                stack.append(child.tail)
            stack.append(child)
    return out


def text_content(el: etree._Element) -> str:
    """Visible text of ``el`` with whitespace normalized (blocks separated by spaces)."""
    return normalize_space("".join(iter_text(el, block_separator=" ")))


def own_text(el: etree._Element) -> str:
    """Only the text directly inside ``el`` (not inside its child elements)."""
    parts = [el.text or ""]
    parts.extend(child.tail or "" for child in el)
    return normalize_space("".join(parts))


def find_main_content(root: etree._Element) -> etree._Element:
    """Best guess at the element holding a page's main content."""
    candidates = root.xpath("//main | //*[@role='main'] | //article")
    if candidates:
        return max(candidates, key=lambda e: len(text_content(e)))
    body = root.find(".//body") if tag_name(root) != "body" else root
    return body if body is not None else root


# --------------------------------------------------------------------------- #
# Block-aware plain text
# --------------------------------------------------------------------------- #


def to_text(el: etree._Element) -> str:
    """Readable plain text: paragraphs and block elements become separate lines."""
    out: list[str] = []
    preformatted: list[str] = []
    stack: list[etree._Element | str] = [el]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            out.append(node)
            continue
        if not isinstance(node.tag, str):
            continue
        name = tag_name(node)
        if node is not el and name in SKIP_TAGS:
            continue
        if name == "br":
            out.append("\n")
            continue
        if name == "pre":
            preformatted.append("".join(node.itertext()).strip("\n"))
            out.append(f"\n{_CODE}{len(preformatted) - 1}{_CODE}\n")
            continue
        if name in BLOCK_TAGS:
            out.append("\n")
            stack.append("\n")  # the block also ends on its own line
        elif name in ("td", "th"):
            out.append(" ")
        if node.text:
            out.append(node.text)
        for child in reversed(node):
            if child.tail:
                stack.append(child.tail)
            stack.append(child)
    lines = [normalize_space(raw) for raw in "".join(out).split("\n")]
    text = "\n".join(line for line in lines if line)
    for i, block in enumerate(preformatted):
        text = text.replace(f"{_CODE}{i}{_CODE}", block)
    return text


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


class _MarkdownRenderer:
    def __init__(self, base_url: str | None) -> None:
        self.base_url = base_url
        self.code_blocks: list[str] = []

    # -- helpers ---------------------------------------------------------- #
    def url(self, value: str | None) -> str:
        value = (value or "").strip()
        if self.base_url and value:
            try:
                return urljoin(self.base_url, value)
            except ValueError:
                return value
        return value

    def inline(self, el: etree._Element) -> str:
        parts: list[str] = [_WS.sub(" ", el.text) if el.text else ""]
        for child in el:
            parts.append(self.node(child))
            if child.tail:
                parts.append(_WS.sub(" ", child.tail))
        return "".join(parts)

    def protect(self, code: str) -> str:
        self.code_blocks.append(code)
        return f"{_CODE}{len(self.code_blocks) - 1}{_CODE}"

    # -- dispatch --------------------------------------------------------- #
    def node(self, el: etree._Element) -> str:
        name = tag_name(el)
        if not name or name in SKIP_TAGS:
            return ""
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = normalize_space(self.inline(el))
            return f"\n\n{'#' * int(name[1])} {text}\n\n" if text else ""
        if name == "br":
            return "  \n"
        if name == "hr":
            return "\n\n---\n\n"
        if name in ("strong", "b"):
            return self.wrap(self.inline(el), "**")
        if name in ("em", "i"):
            return self.wrap(self.inline(el), "*")
        if name in ("del", "s", "strike"):
            return self.wrap(self.inline(el), "~~")
        if name == "code":
            text = "".join(el.itertext())
            return f"`{text}`" if text.strip() else ""
        if name == "pre":
            code = "".join(el.itertext()).strip("\n")
            return "\n\n" + self.protect(f"```\n{code}\n```") + "\n\n"
        if name == "a":
            text = normalize_space(self.inline(el))
            href = el.get("href", "")
            if not text:
                return ""
            if not href or href.startswith(("javascript:", "#")):
                return text
            return f"[{text}]({self.url(href)})"
        if name == "img":
            src = el.get("src") or el.get("data-src") or ""
            if not src:
                return ""
            alt = normalize_space(el.get("alt", ""))
            return f"![{alt}]({self.url(src)})"
        if name in ("ul", "ol"):
            return "\n\n" + self.list(el, ordered=name == "ol") + "\n\n"
        if name == "blockquote":
            inner = self.block_content(el)
            quoted = "\n".join(f"> {line}" if line else ">" for line in inner.split("\n"))
            return f"\n\n{quoted}\n\n"
        if name == "table":
            return "\n\n" + self.table(el) + "\n\n"
        if name == "dt":
            text = normalize_space(self.inline(el))
            return f"\n\n**{text}**\n" if text else ""
        if name == "dd":
            return f"\n{normalize_space(self.inline(el))}\n"
        if name in BLOCK_TAGS:
            inner = self.block_content(el)
            return f"\n\n{inner}\n\n" if inner else ""
        return self.inline(el)

    @staticmethod
    def wrap(text: str, marker: str) -> str:
        stripped = text.strip()
        if not stripped:
            return text
        lead = " " if text[:1].isspace() else ""
        trail = " " if text[-1:].isspace() else ""
        return f"{lead}{marker}{stripped}{marker}{trail}"

    def block_content(self, el: etree._Element) -> str:
        return _clean(self.inline(el))

    def list(self, el: etree._Element, ordered: bool) -> str:
        lines: list[str] = []
        number = int(el.get("start", "1")) if (el.get("start") or "").isdigit() else 1
        for li in el:
            if tag_name(li) != "li":
                continue
            marker = f"{number}. " if ordered else "- "
            number += 1
            content = _clean(self.inline(li)) or ""
            pad = _INDENT * len(marker)
            item_lines = content.split("\n") if content else [""]
            lines.append(marker + item_lines[0])
            lines.extend((pad + line) if line else "" for line in item_lines[1:])
        return "\n".join(lines)

    def table(self, el: etree._Element) -> str:
        rows = []
        for tr in el.iter("tr"):
            cells = [normalize_space(self.inline(td)).replace("|", "\\|") for td in tr if tag_name(td) in ("td", "th")]
            if cells:
                rows.append(cells)
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        out = ["| " + " | ".join(rows[0]) + " |", "|" + "|".join(["---"] * width) + "|"]
        out.extend("| " + " | ".join(r) + " |" for r in rows[1:])
        return "\n".join(out)

    def render(self, el: etree._Element) -> str:
        text = _clean(self.node(el) if tag_name(el) not in BLOCK_TAGS else self.inline(el))
        text = text.replace(_INDENT, " ")
        for i, block in enumerate(self.code_blocks):
            text = text.replace(f"{_CODE}{i}{_CODE}", block)
        return text.strip() + "\n" if text.strip() else ""


def _clean(text: str) -> str:
    """Strip stray spaces around lines and collapse blank-line runs."""
    lines = [line.strip(" \t") for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip("\n")


def to_markdown(el: etree._Element, base_url: str | None = None) -> str:
    """Convert an element (usually ``<body>`` or ``<main>``) to Markdown."""
    try:
        return _MarkdownRenderer(base_url).render(el)
    except RecursionError:  # absurdly deep documents
        return to_text(el)
