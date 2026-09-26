"""A page's rendered layout: where each piece of visible text was drawn.

A browser fetch with ``layout=True`` records it (``response.layout``)::

    response = BrowserFetcher().get(url, layout=True)
    for box in response.layout.boxes[:5]:
        print(box.text, box.x, box.y, box.width, box.height, box.size, box.weight)

Each :class:`Box` is one piece of text (a text node, SVG labels included) with its place on the page
(in CSS pixels, from the page's top left), its font's size and weight, and the path of the element
holding it. HTML alone does not say what the page looks like: which values sit in one row, which
label is beside which number. The layout does, and :mod:`wintergrab.extraction.visual` reads tables
and labelled values from it. Text a page draws on a ``<canvas>`` or in an image is not in the layout
(a model reading the screenshot can see it: :mod:`wintergrab.extraction.model`).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["LAYOUT_SCRIPT", "MAX_BOXES", "Box", "Layout", "element_path"]

#: The most boxes recorded for a page.
MAX_BOXES = 5000

#: Run in the page (``page.evaluate(LAYOUT_SCRIPT, MAX_BOXES)``): the visible text nodes' boxes.
LAYOUT_SCRIPT = """
(maxBoxes) => {
  const out = [];
  const sx = window.scrollX, sy = window.scrollY;
  const paths = new Map(), styles = new Map();
  const pathOf = (el) => {
    if (paths.has(el)) return paths.get(el);
    const parts = [];
    for (let node = el; node && node.nodeType === 1 && node !== document.documentElement; node = node.parentElement) {
      const tag = node.tagName.toLowerCase();
      let index = 1, alike = false;
      for (let s = node.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === node.tagName) index++;
      for (let s = node.nextElementSibling; s && !alike; s = s.nextElementSibling) alike = s.tagName === node.tagName;
      parts.unshift(index > 1 || alike ? `${tag}:nth-of-type(${index})` : tag);
    }
    const path = parts.join(" > ");
    paths.set(el, path);
    return path;
  };
  const styleOf = (el) => {
    if (!styles.has(el)) styles.set(el, getComputedStyle(el));
    return styles.get(el);
  };
  const skip = new Set(["script", "style", "noscript", "template", "textarea", "option"]);
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  for (let node = walker.nextNode(); node && out.length < maxBoxes; node = walker.nextNode()) {
    const text = node.nodeValue.replace(/\\s+/g, " ").trim();
    const el = node.parentElement;
    if (!text || !el || skip.has(el.tagName.toLowerCase())) continue;
    const style = styleOf(el);
    if (style.visibility === "hidden" || style.display === "none" || parseFloat(style.opacity || "1") === 0) continue;
    range.selectNodeContents(node);
    const lines = Array.from(range.getClientRects()).filter((r) => r.width > 0 && r.height > 0);
    if (!lines.length) continue;
    const r = range.getBoundingClientRect();
    out.push({
      text,
      x: Math.round((r.left + sx) * 10) / 10,
      y: Math.round((r.top + sy) * 10) / 10,
      width: Math.round(r.width * 10) / 10,
      height: Math.round(r.height * 10) / 10,
      lines: lines.length,
      size: parseFloat(style.fontSize) || 0,
      weight: parseInt(style.fontWeight, 10) || 400,
      tag: el.tagName.toLowerCase(),
      path: pathOf(el),
    });
  }
  const root = document.documentElement;
  return {boxes: out, width: root.scrollWidth, height: root.scrollHeight, truncated: out.length >= maxBoxes};
}
"""


@dataclass(frozen=True)
class Box:
    """A piece of visible text and where it was drawn (CSS pixels, from the page's top left).

    Attributes:
        text: The text, spaces collapsed.
        x, y, width, height: Its box (the union of its lines' boxes when it wraps).
        lines: How many lines it takes.
        size: The font size, in pixels.
        weight: The font weight (400 regular, 700 bold).
        tag: The element holding it (``"td"``, ``"span"``, ``"text"`` for SVG labels).
        path: That element's CSS path from ``body`` (``"body > main > div:nth-of-type(2) > span"``).
    """

    text: str
    x: float
    y: float
    width: float
    height: float
    lines: int = 1
    size: float = 0.0
    weight: int = 400
    tag: str = ""
    path: str = ""

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def middle(self) -> float:
        """The vertical centre."""
        return self.y + self.height / 2

    @property
    def center(self) -> float:
        """The horizontal centre."""
        return self.x + self.width / 2

    @property
    def bold(self) -> bool:
        return self.weight >= 600 or self.tag in ("b", "strong", "th")

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text, "x": self.x, "y": self.y, "width": self.width, "height": self.height,
            "lines": self.lines, "size": self.size, "weight": self.weight, "tag": self.tag, "path": self.path,
        }  # fmt: skip


@dataclass
class Layout:
    """A page's rendered layout: its text boxes in document order, and the page's size.

    ``truncated`` says the page had more than :data:`MAX_BOXES` pieces of text (the rest are not recorded).
    """

    boxes: list[Box] = field(default_factory=list)
    width: float = 0.0
    height: float = 0.0
    truncated: bool = False

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Layout:
        """A layout from :meth:`to_dict` (or what :data:`LAYOUT_SCRIPT` returns)."""
        boxes = [Box(**{k: v for k, v in raw.items() if k in _BOX_FIELDS}) for raw in data.get("boxes") or ()]
        return cls(boxes, float(data.get("width") or 0), float(data.get("height") or 0), bool(data.get("truncated")))

    def to_dict(self) -> dict[str, Any]:
        return {
            "boxes": [b.to_dict() for b in self.boxes],
            "width": self.width,
            "height": self.height,
            "truncated": self.truncated,
        }

    def within(self, path: str) -> Layout:
        """The boxes inside the element at ``path`` (a record's card)."""
        prefix = path + " > "
        return Layout([b for b in self.boxes if b.path == path or b.path.startswith(prefix)], self.width, self.height)

    def find(self, text: str) -> list[Box]:
        """The boxes whose text is ``text`` (ignoring case and surrounding spaces)."""
        wanted = " ".join(text.split()).casefold()
        return [b for b in self.boxes if b.text.casefold() == wanted]

    def __len__(self) -> int:
        return len(self.boxes)

    def __iter__(self) -> Iterator[Box]:
        return iter(self.boxes)


_BOX_FIELDS = frozenset(Box.__dataclass_fields__)


def element_path(element: Any) -> str:
    """The CSS path of a parsed element (lxml) from ``body``, written as :data:`LAYOUT_SCRIPT` writes paths:
    a tag, with ``:nth-of-type(n)`` when its parent has other children of that tag."""
    parts: list[str] = []
    node = element
    while node is not None and isinstance(node.tag, str) and node.tag.lower() != "html":
        tag = node.tag.lower()
        parent = node.getparent()
        siblings = (
            [c for c in parent if isinstance(c.tag, str) and c.tag.lower() == tag] if parent is not None else [node]
        )
        index = next(i for i, sibling in enumerate(siblings, 1) if sibling is node)
        parts.insert(0, f"{tag}:nth-of-type({index})" if len(siblings) > 1 else tag)
        node = parent
    return " > ".join(parts)
