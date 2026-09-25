"""Extraction with a strategy hierarchy, per-field provenance and confidence.

::

    from wintergrab.data import Schema
    from wintergrab.extraction import Extractor

    extractor = Extractor(Schema.load("product.schema.json"))
    record = extractor.extract(response)      # a Response, a Selector or HTML
    record.data                               # {"name": ..., "price": 299.99, "currency": "USD", ...}
    record.fields["price"].method             # "json-ld"
    record.fields["price"].confidence         # 0.98
    print(record.explain())                   # where every value came from

See ``docs/extraction.md``.
"""

from __future__ import annotations

from .engine import DEFAULT_PRIORS, ExtractedRecord, Extractor, FieldValue, value_key
from .model import ExtractionModel, ModelField, ModelRequest, grounding
from .page import PageContext, schema_types
from .strategies import (
    STRATEGIES,
    Candidate,
    DomHeuristics,
    EmbeddedJson,
    LabelledValues,
    MetaTags,
    Patterns,
    RecordFields,
    Selectors,
    Strategy,
    StructuredData,
    field_kind,
)

__all__ = [
    "DEFAULT_PRIORS",
    "STRATEGIES",
    "Candidate",
    "DomHeuristics",
    "EmbeddedJson",
    "ExtractedRecord",
    "ExtractionModel",
    "Extractor",
    "FieldValue",
    "LabelledValues",
    "MetaTags",
    "ModelField",
    "ModelRequest",
    "PageContext",
    "Patterns",
    "RecordFields",
    "Selectors",
    "Strategy",
    "StructuredData",
    "field_kind",
    "grounding",
    "schema_types",
    "value_key",
]
