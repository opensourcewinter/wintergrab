"""Typed records without writing a schema, and the evidence behind each value.

A template ("product", "job", "event", "property"...) is a ready-made schema. The
extractor fills each field from the page's structured data first, then its meta
tags, labels, layout and text, and says where each value came from and how sure
it is.

    python examples/11_templates_and_evidence.py
"""

from __future__ import annotations

from typing import Any

import wintergrab as wg
from wintergrab.extraction import Extractor

URL = "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"


def main(url: str = URL) -> dict[str, Any]:
    page = wg.get(url)
    record = Extractor("product").extract(page)
    print(f"{'field':<14} {'value':<40} {'from':<12} confidence")
    for name, found in record.fields.items():
        if found.value is not None:
            print(f"{name:<14} {str(found.value)[:40]:<40} {found.method or '':<12} {found.confidence:.2f}")
    return record.to_dict()


if __name__ == "__main__":
    main()
