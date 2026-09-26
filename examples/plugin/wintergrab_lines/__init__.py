"""An example plugin: a ``.lines`` output (one item per line, as ``key=value`` pairs), and a
``wintergrab count-lines FILE`` command.

    pip install -e examples/plugin
    wintergrab crawl https://quotes.toscrape.com/ --each .quote --field "text=.text::text" -o quotes.lines
    wintergrab count-lines quotes.lines
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from wintergrab.spider.exporters import Exporter, to_dict


class Lines(Exporter):
    """One item per line: ``key=value`` pairs separated by tabs."""

    def __init__(self, path: Path, *, append: bool) -> None:
        super().__init__(path, append=append)
        self.file = open(path, "a" if append else "w", encoding="utf-8")  # noqa: SIM115 - closed in close()

    def write(self, item: Any) -> None:
        record = to_dict(item)
        line = "\t".join(f"{key}={value}" for key, value in record.items()) + "\n"
        self.file.write(line)
        self.bytes_written += len(line.encode())  # type: ignore[operator]
        self.count += 1
        self._maybe_flush()

    def flush(self) -> None:
        self.file.flush()

    def close(self) -> None:
        self.file.close()


def add_count_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file", help="a .lines file")


def count(args: argparse.Namespace) -> int:
    with open(args.file, encoding="utf-8") as handle:
        print(sum(1 for line in handle if line.strip()))
    return 0


def plugin(registry: Any) -> None:
    registry.exporter(".lines", Lines)
    registry.command("count-lines", add_count_arguments, count, help="count the items of a .lines file")
