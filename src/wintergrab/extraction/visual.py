"""Reading what a page looks like: tables and labelled values from its rendered layout.

HTML does not always say which values belong together. A pricing grid made of ``<div>`` s, a
dashboard's tiles ("1,284" in large type over "Active users"), a specification drawn as two
aligned columns: their structure is in how they are drawn. A browser fetch with ``layout=True``
records where each piece of text was drawn (:mod:`wintergrab.parser.layout`), and this module reads
it::

    response = BrowserFetcher().get(url, layout=True)
    for table in layout_tables(response.layout):
        print(table.header, table.rows)          # ['Plan', 'Price', 'Seats'] [['Starter', '$9', '1'], ...]
    for pair in layout_pairs(response.layout):
        print(pair.label, pair.value, pair.how)  # Active users 1,284 below

* **Tables**: an element whose text is drawn in rows with the same columns, left to right (at
  least three rows and two columns, and most of the element's text). Pieces of one cell (``$`` and
  ``9`` in two spans) are joined. The first row is a header when it is bold and the others are not,
  or when it holds only words over a column of numbers.
* **Labelled values**: a label with its value beside it on the same line (``Weight   1.2 kg``), or
  the two stacked alone in a small element, a tile: the larger text is the value, whichever is on
  top.

The extractor uses both (the ``visual`` method, :class:`VisualLayout`) when a page has a layout: a
field is filled from the value of a label named like it. Everything is read from the layout; nothing
is guessed from pixels (text drawn on a canvas or inside an image is not in it).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from ..parser.layout import Box, Layout
from .schemaorg import candidate_names, field_key
from .strategies import Candidate, Strategy, _distinct, _uniqueness

if TYPE_CHECKING:
    from ..data.schema import Schema, SchemaField
    from .page import PageContext

__all__ = ["LabelledPair", "VisualLayout", "VisualTable", "layout_pairs", "layout_tables"]

_NUMERIC = re.compile("^[\\s$€£¥₹%+\\-\u2212.,:/0-9kKmMbB]*\\d[\\s$€£¥₹%+\\-\u2212.,:/0-9kKmMbB]*$")
_WIDE_COLON = "\uff1a"
_LETTER = re.compile(r"[^\W\d_]")


@dataclass
class _Cell:
    """Pieces of text drawn together on one line: one cell."""

    boxes: list[Box]

    @property
    def text(self) -> str:
        out = self.boxes[0].text
        for before, box in zip(self.boxes, self.boxes[1:], strict=False):
            out += (" " if box.x - before.right > 0.2 * (box.size or 10) else "") + box.text
        return out

    @property
    def x(self) -> float:
        return self.boxes[0].x

    @property
    def right(self) -> float:
        return max(b.right for b in self.boxes)

    @property
    def bold(self) -> bool:
        return all(b.bold for b in self.boxes)

    @property
    def style(self) -> tuple[float, int]:
        return self.boxes[0].size, self.boxes[0].weight


def _lines(boxes: Sequence[Box]) -> list[list[_Cell]]:
    """``boxes`` in lines (their vertical centres close), each line's pieces joined into cells."""
    rows: list[list[Box]] = []
    middles: list[float] = []
    for box in sorted(boxes, key=lambda b: (b.middle, b.x)):
        if rows and abs(box.middle - middles[-1]) <= 0.4 * max(box.height, max(b.height for b in rows[-1])):
            rows[-1].append(box)
            middles[-1] = sum(b.middle for b in rows[-1]) / len(rows[-1])
        else:
            rows.append([box])
            middles.append(box.middle)
    lines = []
    for row in rows:
        cells: list[_Cell] = []
        for box in sorted(row, key=lambda b: b.x):
            if cells and box.x - cells[-1].right <= max(1.0, 0.35 * (box.size or 10)):
                cells[-1].boxes.append(box)
            else:
                cells.append(_Cell([box]))
        lines.append(cells)
    return lines


# ---------------------------------------------------------------------------------------------- #
# tables
# ---------------------------------------------------------------------------------------------- #
@dataclass
class VisualTable:
    """A table read from a layout.

    Attributes:
        header: The header row's texts, or ``None`` when the first row is data.
        rows: The data rows' texts, cell by cell.
        path: The CSS path of the element holding it.
        top, left, bottom, right: Where it is drawn.
    """

    header: list[str] | None
    rows: list[list[str]]
    path: str
    top: float
    left: float
    bottom: float
    right: float
    boxes: list[Box] = field(default_factory=list, repr=False)

    @property
    def columns(self) -> int:
        return len(self.rows[0]) if self.rows else 0

    def records(self) -> list[dict[str, str]]:
        """The rows as records, keyed by the header (``column_1``... without one)."""
        keys = self.header or [f"column_{i + 1}" for i in range(self.columns)]
        return [dict(zip(keys, row, strict=False)) for row in self.rows]

    def to_dict(self) -> dict[str, Any]:
        return {"header": self.header, "rows": self.rows, "path": self.path,
                "box": [self.left, self.top, self.right, self.bottom]}  # fmt: skip


def layout_tables(layout: Layout, *, min_rows: int = 3, min_columns: int = 2) -> list[VisualTable]:
    """The tables drawn on the page (see the module docs), in the order they are drawn."""
    members: dict[str, list[int]] = defaultdict(list)
    for i, box in enumerate(layout.boxes):
        parts = box.path.split(" > ")
        for depth in range(1, len(parts)):
            members[" > ".join(parts[:depth])].append(i)
    found: list[VisualTable] = []
    seen: list[frozenset[int]] = []
    for path, indices in sorted(members.items(), key=lambda item: -item[0].count(" > ")):
        if len(indices) < min_rows * min_columns:
            continue
        boxes = [layout.boxes[i] for i in indices]
        table, used = _table(boxes, path, min_rows, min_columns)
        if table is None:
            continue
        drawn = frozenset(id(b) for b in used)
        if any(drawn <= earlier for earlier in seen):
            continue  # the same table, in an element around it
        seen.append(drawn)
        found.append(table)
    return sorted(found, key=lambda t: (t.top, t.left))


def _table(boxes: list[Box], path: str, min_rows: int, min_columns: int) -> tuple[VisualTable | None, list[Box]]:
    """The table these boxes (one element's) are drawn as, if they are: most of them, in aligned rows."""
    lines = _lines(boxes)
    best: list[list[_Cell]] = []
    run: list[list[_Cell]] = []
    for line in lines:
        if run and len(line) == len(run[0]) and _aligned(run, line):
            run.append(line)
            continue
        if len(run) > len(best):
            best = run
        run = [line] if len(line) >= min_columns else []
    if len(run) > len(best):
        best = run
    used = [b for line in best for cell in line for b in cell.boxes]
    if len(best) < min_rows or len(used) < 0.75 * len(boxes):
        return None, []
    header: list[str] | None = None
    body = best
    first, rest = best[0], best[1:]
    numeric = [sum(1 for line in rest if _NUMERIC.match(line[i].text)) >= 0.6 * len(rest) for i in range(len(first))]
    first_words = all(_LETTER.search(c.text) and not _NUMERIC.match(c.text) for c in first)
    if (all(c.bold for c in first) and not all(c.bold for line in rest for c in line)) or (
        first_words and any(numeric)
    ):
        header, body = [c.text for c in first], rest
    if len(body) < min_rows - 1:
        return None, []
    xs = [c.x for line in best for c in line]
    rights = [c.right for line in best for c in line]
    table = VisualTable(
        header,
        [[c.text for c in line] for line in body],
        path,
        top=min(b.y for b in used),
        left=min(xs),
        bottom=max(b.bottom for b in used),
        right=max(rights),
        boxes=used,
    )
    return table, used


def _aligned(run: list[list[_Cell]], line: list[_Cell]) -> bool:
    """Whether ``line``'s cells fall in the columns of ``run`` (each column's cells overlap one band, and
    the bands do not overlap each other)."""
    bands = []
    for i, cell in enumerate(line):
        left = min([cell.x, *(row[i].x for row in run)])
        right = max([cell.right, *(row[i].right for row in run)])
        overlaps = all(cell.x < row[i].right and row[i].x < cell.right for row in run[-1:])
        starts = abs(cell.x - run[-1][i].x) <= 2 or abs(cell.right - run[-1][i].right) <= 2
        if not (overlaps or starts):
            return False
        bands.append((left, right))
    return all(bands[i][1] <= bands[i + 1][0] for i in range(len(bands) - 1))


# ---------------------------------------------------------------------------------------------- #
# labelled values
# ---------------------------------------------------------------------------------------------- #
@dataclass
class LabelledPair:
    """A label and its value, read from where they are drawn.

    Attributes:
        label, value: Their texts.
        how: ``"beside"`` (the value right of the label), ``"below"`` or ``"above"`` (a tile: the value
            under or over its label), or ``"table"`` (a two-column table's row).
        close: Whether the two are alone in a small element (a row, a tile): surer.
    """

    label: str
    value: str
    how: str
    close: bool = False
    boxes: tuple[Box, ...] = field(default=(), repr=False)


def layout_pairs(layout: Layout) -> list[LabelledPair]:
    """The labelled values drawn on the page (see the module docs)."""
    pairs: list[LabelledPair] = []
    counts = _element_counts(layout)
    tables = layout_tables(layout)
    in_tables = {id(box) for table in tables if table.columns > 2 or table.header for box in table.boxes}
    for line in _lines([b for b in layout.boxes if id(b) not in in_tables]):
        for left, right in pairwise(line):
            size = left.boxes[0].size or 16
            if right.x - left.right > max(6 * size, 100) or not _label_like(left.text):
                continue
            close = _alone(left.boxes + right.boxes, counts)
            if close or left.text.rstrip().endswith((":", _WIDE_COLON)):  # a row of its own, or a label saying so
                boxes = tuple(left.boxes + right.boxes)
                pairs.append(LabelledPair(_unlabel(left.text), right.text, "beside", close, boxes))
    for upper, lower in _stacked(layout.boxes):
        if not _alone([upper, lower], counts):
            continue  # stacked texts are a tile's only when they are alone in their element
        if lower.size > 1.15 * upper.size and _label_like(upper.text):
            pairs.append(LabelledPair(_unlabel(upper.text), lower.text, "below", True, (upper, lower)))
        elif upper.size > 1.15 * lower.size and _label_like(lower.text):
            pairs.append(LabelledPair(_unlabel(lower.text), upper.text, "above", True, (upper, lower)))
        elif abs(upper.size - lower.size) <= 0.15 * upper.size and _label_like(upper.text) and upper.bold:
            pairs.append(LabelledPair(_unlabel(upper.text), lower.text, "below", True, (upper, lower)))
    for table in tables:  # a two-column table of labels and their values: a specification
        rows = table.rows if table.header is None else [table.header, *table.rows]
        if table.columns == 2 and all(_label_like(label) for label, _ in rows):
            pairs.extend(LabelledPair(_unlabel(label), value, "table") for label, value in rows)
    return pairs


def _unlabel(text: str) -> str:
    """A label without the colon after it."""
    return text.rstrip(" :" + _WIDE_COLON)


def _stacked(boxes: Sequence[Box]) -> list[tuple[Box, Box]]:
    """Pairs of boxes drawn one right under the other, overlapping horizontally."""
    out = []
    ordered = sorted(boxes, key=lambda b: (b.y, b.x))
    for i, upper in enumerate(ordered):
        for lower in ordered[i + 1 :]:
            gap = lower.y - upper.bottom
            if gap > 1.2 * max(upper.size, lower.size, 10):
                break
            if gap >= -2 and lower.x < upper.right and upper.x < lower.right:
                out.append((upper, lower))
                break
    return out


def _element_counts(layout: Layout) -> dict[str, int]:
    """How many boxes each element holds (its own and its descendants')."""
    counts: dict[str, int] = defaultdict(int)
    for box in layout.boxes:
        parts = box.path.split(" > ")
        for depth in range(1, len(parts) + 1):
            counts[" > ".join(parts[:depth])] += 1
    return counts


def _alone(boxes: Sequence[Box], counts: dict[str, int]) -> bool:
    """Whether the boxes are alone (with at most one more) in the smallest element holding them all."""
    paths = [b.path.split(" > ") for b in boxes]
    common: list[str] = []
    for parts in zip(*paths, strict=False):
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    return bool(common) and counts.get(" > ".join(common), 0) <= len(boxes) + 1


def _label_like(text: str) -> bool:
    """Short words (a label), not a number, a sentence or a lone symbol."""
    stripped = text.strip()
    return (
        0 < len(stripped) <= 40
        and bool(_LETTER.search(stripped))
        and not _NUMERIC.match(stripped)
        and not stripped.endswith((".", "!", "?"))
    )


# ---------------------------------------------------------------------------------------------- #
# the extraction strategy
# ---------------------------------------------------------------------------------------------- #
class VisualLayout(Strategy):
    """Values beside, under or over a label named like the field, where the page draws them (a page
    fetched in a browser with ``layout=True``; see the module docs)."""

    method = "visual"

    def candidates(self, page: PageContext, f: SchemaField, schema: Schema) -> list[Candidate]:
        layout = page.layout
        if layout is None or not layout.boxes:
            return []
        cache = page.__dict__.setdefault("_layout_pairs", {})
        key = id(layout)
        if key not in cache:
            cache[key] = layout_pairs(layout)
        names = set(candidate_names(f.name, f.aliases))
        found = []
        for pair in cache[key]:
            if field_key(pair.label) in names:
                found.append((pair.value, f"visual:{pair.how}:{pair.label}", 1.0 if pair.close else 0.9))
        distinct = _distinct([(value, source) for value, source, _ in found])
        factors = {source: factor for _, source, factor in found}
        detail = f"{len(distinct)} different values by that label" if len(distinct) > 1 else ""
        return [
            Candidate(value, self.method, source, _uniqueness(len(distinct)) * factors.get(source, 0.9), detail)
            for value, source in distinct
        ]
