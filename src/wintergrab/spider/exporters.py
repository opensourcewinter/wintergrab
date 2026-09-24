"""Write scraped items to JSON Lines, JSON or CSV as they arrive."""

from __future__ import annotations

import csv
import dataclasses
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def to_dict(item: Any) -> Any:
    """Turn dataclasses, pydantic models, attrs classes... into plain dicts."""
    if isinstance(item, Mapping):
        return dict(item)
    if dataclasses.is_dataclass(item) and not isinstance(item, type):
        return dataclasses.asdict(item)
    for method in ("model_dump", "dict", "_asdict", "to_dict"):
        fn = getattr(item, method, None)
        if callable(fn):
            try:
                return fn()
            except TypeError:
                continue
    if hasattr(item, "__dict__"):
        return {k: v for k, v in vars(item).items() if not k.startswith("_")}
    return item


def _json_default(value: Any) -> Any:
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return str(value)


def dumps(item: Any) -> str:
    return json.dumps(to_dict(item), ensure_ascii=False, default=_json_default)


class Exporter:
    def __init__(self, path: Path, *, append: bool) -> None:
        self.path = path
        self.append = append
        self.count = 0

    def write(self, item: Any) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class JsonLinesExporter(Exporter):
    """One JSON object per line - the best format for big, resumable crawls."""

    def __init__(self, path: Path, *, append: bool) -> None:
        super().__init__(path, append=append)
        self._fh = open(path, "a" if append else "w", encoding="utf-8")  # noqa: SIM115 - closed in close()

    def write(self, item: Any) -> None:
        self._fh.write(dumps(item) + "\n")
        self._fh.flush()
        self.count += 1

    def close(self) -> None:
        self._fh.close()


class JsonExporter(Exporter):
    """A single JSON array (reopened and extended when a crawl resumes)."""

    def __init__(self, path: Path, *, append: bool) -> None:
        super().__init__(path, append=append)
        self._first = True
        if append and path.exists() and path.stat().st_size > 0:
            self._fh = open(path, "r+", encoding="utf-8")  # noqa: SIM115 - closed in close()
            content = self._fh.read()
            end = content.rstrip().rfind("]")
            if end == -1:
                raise ValueError(f"{path} is not a JSON array; cannot append")
            self._first = content[:end].strip() == "["
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(content[:end].rstrip())
        else:
            self._fh = open(path, "w", encoding="utf-8")  # noqa: SIM115 - closed in close()
            self._fh.write("[")
        self._fh.flush()

    def write(self, item: Any) -> None:
        self._fh.write(("\n" if self._first else ",\n") + dumps(item))
        self._fh.flush()
        self._first = False
        self.count += 1

    def close(self) -> None:
        self._fh.write("\n]\n")
        self._fh.close()


class CsvExporter(Exporter):
    """CSV with columns taken from the first item (nested values become JSON)."""

    def __init__(self, path: Path, *, append: bool) -> None:
        super().__init__(path, append=append)
        self._fields: list[str] | None = None
        resuming = append and path.exists() and path.stat().st_size > 0
        if resuming:
            with open(path, newline="", encoding="utf-8") as fh:
                header = next(csv.reader(fh), None)
            self._fields = header or None
        self._fh = open(path, "a" if resuming else "w", newline="", encoding="utf-8")  # noqa: SIM115 - closed in close()
        self._writer: csv.DictWriter[str] | None = None
        if self._fields:
            self._writer = csv.DictWriter(self._fh, fieldnames=self._fields, extrasaction="ignore")

    def write(self, item: Any) -> None:
        row = to_dict(item)
        if not isinstance(row, Mapping):
            row = {"value": row}
        flat = {
            k: (
                v
                if isinstance(v, (str, int, float, bool)) or v is None
                else json.dumps(v, default=_json_default, ensure_ascii=False)
            )
            for k, v in row.items()
        }
        if self._writer is None:
            self._fields = list(flat)
            self._writer = csv.DictWriter(self._fh, fieldnames=self._fields, extrasaction="ignore")
            self._writer.writeheader()
        self._writer.writerow(flat)
        self._fh.flush()
        self.count += 1

    def close(self) -> None:
        self._fh.close()


EXPORTERS: dict[str, type[Exporter]] = {
    ".jsonl": JsonLinesExporter,
    ".ndjson": JsonLinesExporter,
    ".jl": JsonLinesExporter,
    ".json": JsonExporter,
    ".csv": CsvExporter,
}


def open_exporter(path: str | os.PathLike[str], *, append: bool = False) -> Exporter:
    """Pick an exporter from the file extension (``.jsonl``, ``.json``, ``.csv``)."""
    target = Path(path)
    cls = EXPORTERS.get(target.suffix.lower())
    if cls is None:
        raise ValueError(f"Unsupported output format {target.suffix!r}; use .jsonl, .json or .csv")
    target.parent.mkdir(parents=True, exist_ok=True)
    return cls(target, append=append)


def write_items(path: str | os.PathLike[str], items: list[Any]) -> Path:
    """Write a list of items in one go (format chosen by extension)."""
    exporter = open_exporter(path)
    try:
        for item in items:
            exporter.write(item)
    finally:
        exporter.close()
    return exporter.path
