"""Excel workbooks: items as the rows of a sheet (``output = "items.xlsx"``; needs openpyxl).

The first row names the fields; each item is a row. Numbers and true/false stay numbers and
booleans; nested values are JSON text, and the workbook says which columns hold them (a
``wintergrab`` document property), so :func:`read_xlsx` gives them back as they were. Text is
always text: a value such as ``=HYPERLINK(...)``
from a crawled page is never a formula. Characters a workbook cannot hold (control characters)
are left out, text longer than a cell holds (32,767 characters) is cut, integers beyond what
Excel keeps exactly (2^53) are text, and past 1,048,575 items the rows continue on a new sheet.

As with Parquet, the items are spooled beside the file while the crawl runs, and the workbook is
written when it ends.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..spider.exporters import Exporter, _size, dumps, to_dict
from .common import Spool, as_text, column_kinds, require

__all__ = ["XlsxExporter", "read_xlsx", "write_xlsx"]

MAX_ROWS = 1_048_576  # a sheet's rows, the header included
MAX_CELL = 32_767  # characters in a cell
_EXACT = 2**53
_PROPERTY = "wintergrab"


def _openpyxl() -> Any:
    return require("openpyxl", "xlsx", "Excel output")


class XlsxExporter(Exporter):
    """Items in an Excel workbook (see the module docs)."""

    def __init__(self, path: Path, *, append: bool) -> None:
        _openpyxl()
        super().__init__(path, append=append)
        self.spool = Spool(path, append=append, seed=lambda: read_xlsx(path))

    def write(self, item: Any) -> None:
        row = to_dict(item)
        line = dumps(row if isinstance(row, dict) else {"value": row}) + "\n"
        self.spool.write(line)
        self.bytes_written += _size(line)  # type: ignore[operator]  # the items as JSON
        self.count += 1
        self._maybe_flush()

    def flush(self) -> None:
        self.spool.flush()

    def close(self) -> None:
        self.spool.finish(lambda temporary: write_xlsx(temporary, self.spool.records))


def write_xlsx(path: Path, records: Any, *, sheet: str = "items") -> None:
    """Write the records ``records()`` gives (called twice: once to find the columns) to ``path``."""
    openpyxl = _openpyxl()
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.packaging.custom import StringProperty
    from openpyxl.styles import Font

    kinds = column_kinds(records())
    columns = list(kinds)
    workbook = openpyxl.Workbook(write_only=True)
    nested = [name for name, kind in kinds.items() if kind == "json"]
    workbook.custom_doc_props.append(StringProperty(name=_PROPERTY, value=json.dumps({"json_columns": nested})))
    bold = Font(bold=True)

    def text_cell(ws: Any, text: str, *, header: bool = False) -> Any:
        text = ILLEGAL_CHARACTERS_RE.sub("", text)
        if len(text) > MAX_CELL:
            text = text[: MAX_CELL - 1] + "…"
        cell = WriteOnlyCell(ws, value=text)
        cell.data_type = "s"  # text, never a formula
        if header:
            cell.font = bold
        return cell

    def cell(ws: Any, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or (isinstance(value, (int, float)) and abs(value) < _EXACT):
            return value
        return text_cell(ws, str(value) if isinstance(value, int) else as_text(value) or "")

    def new_sheet(number: int) -> Any:
        ws = workbook.create_sheet(sheet if number == 1 else f"{sheet} ({number})")
        ws.append([text_cell(ws, name, header=True) for name in columns])
        return ws

    sheets = 1
    ws = new_sheet(sheets)
    rows = 1
    for record in records():
        if rows >= MAX_ROWS:
            sheets += 1
            ws = new_sheet(sheets)
            rows = 1
        ws.append([cell(ws, record.get(name)) for name in columns])
        rows += 1
    workbook.save(str(path))


def read_xlsx(path: str | Path) -> Iterator[dict[str, Any]]:
    """The rows of a workbook's sheets as records (the first row of each names the fields), with the
    JSON columns wintergrab wrote decoded again."""
    openpyxl = _openpyxl()
    workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    nested: set[str] = set()
    for prop in workbook.custom_doc_props.props:
        if prop.name == _PROPERTY:
            nested = set(json.loads(prop.value).get("json_columns", []))
    try:
        for ws in workbook.worksheets:
            rows = ws.iter_rows(values_only=True)
            header = next(rows, None)
            if not header:
                continue
            names = [str(h) if h is not None else f"column_{i + 1}" for i, h in enumerate(header)]
            for row in rows:
                if row is None or all(v is None for v in row):
                    continue
                record = {name: value for name, value in zip(names, row, strict=False) if value is not None}
                for name in nested & record.keys():
                    if isinstance(record[name], str):
                        record[name] = json.loads(record[name])
                yield record
    finally:
        workbook.close()
