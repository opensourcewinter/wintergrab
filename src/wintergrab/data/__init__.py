"""The data layer: turn scraped values into clean, typed, validated, de-duplicated records.

* :mod:`~wintergrab.data.normalize` - parsers for prices, numbers, dates, units,
  phone numbers, addresses, countries, languages, ratings, availability...
* :mod:`~wintergrab.data.schema` - typed schemas that normalize and validate records
  (and export to JSON Schema, or are inferred from samples).
* :mod:`~wintergrab.data.validate` - validation issues and custom rules.
* :mod:`~wintergrab.data.expressions` - a safe expression language for filters,
  computed fields and rules.
* :mod:`~wintergrab.data.pipeline` - declarative record pipelines (rename, transform,
  compute, filter, normalize, validate, de-duplicate, look up, enrich).
* :mod:`~wintergrab.data.dedupe` and :mod:`~wintergrab.data.similarity` - exact and
  near-duplicate detection (content hashes, SimHash, MinHash/LSH).
* :mod:`~wintergrab.data.quality` - dataset quality metrics and degradation alerts.
* :mod:`~wintergrab.data.entities` - entity resolution: which names are the same company,
  brand, product, person or place, with evidence.
* :mod:`~wintergrab.data.places` - where records are: addresses and locations read into
  countries, regions, cities, postal codes and coordinates; distances; filters and groups by place.
* :mod:`~wintergrab.data.versions` - dataset versions, and what was added, removed and
  changed between two datasets.

See ``docs/data.md`` for a guided tour.
"""

from __future__ import annotations

from .dedupe import Deduplicator
from .entities import Entity, EntityResolver, Match, Mention, Resolution
from .expressions import FUNCTIONS, Expression, compile_expression, get_path
from .inference import TypeGuess, explain_inference, infer_schema
from .issues import Issue
from .pipeline import (
    OPERATIONS,
    Analyze,
    Classify,
    Compute,
    ConfigLoader,
    ConvertCurrency,
    Deduplicate,
    Enrich,
    Exclude,
    Filter,
    Locate,
    Lookup,
    Normalize,
    Operation,
    Pipeline,
    QualityCheck,
    RecordContext,
    Rename,
    Select,
    Stage,
    Transform,
    Validate,
    register_operation,
)
from .places import Place, distance_km, group_records, place_of, places_of
from .quality import FieldQuality, QualityMonitor, QualityReport, ks_statistic
from .schema import FIELD_TYPES, FieldResult, NormalizeContext, Schema, SchemaField, load_schema, register_type
from .similarity import (
    MinHashLSH,
    SimHashIndex,
    content_hash,
    hamming,
    jaccard,
    minhash,
    minhash_similarity,
    simhash,
    simhash_similarity,
)
from .validate import Rule, is_valid, validate_record
from .versions import DatasetDiff, DatasetVersions, diff_records

__all__ = [
    "FIELD_TYPES",
    "FUNCTIONS",
    "OPERATIONS",
    "Analyze",
    "Classify",
    "Compute",
    "ConfigLoader",
    "ConvertCurrency",
    "DatasetDiff",
    "DatasetVersions",
    "Deduplicate",
    "Deduplicator",
    "Enrich",
    "Entity",
    "EntityResolver",
    "Exclude",
    "Expression",
    "FieldQuality",
    "FieldResult",
    "Filter",
    "Issue",
    "Locate",
    "Lookup",
    "Match",
    "Mention",
    "MinHashLSH",
    "Normalize",
    "NormalizeContext",
    "Operation",
    "Pipeline",
    "Place",
    "QualityCheck",
    "QualityMonitor",
    "QualityReport",
    "RecordContext",
    "Rename",
    "Resolution",
    "Rule",
    "Schema",
    "SchemaField",
    "Select",
    "SimHashIndex",
    "Stage",
    "Transform",
    "TypeGuess",
    "Validate",
    "compile_expression",
    "content_hash",
    "diff_records",
    "distance_km",
    "explain_inference",
    "get_path",
    "group_records",
    "hamming",
    "infer_schema",
    "is_valid",
    "jaccard",
    "ks_statistic",
    "load_schema",
    "minhash",
    "minhash_similarity",
    "place_of",
    "places_of",
    "register_operation",
    "register_type",
    "simhash",
    "simhash_similarity",
    "validate_record",
]
