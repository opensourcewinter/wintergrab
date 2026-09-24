"""Adaptive selectors: remember elements and find them again after a redesign."""

from .fingerprint import fingerprint, relocate, similar_elements, similarity
from .storage import AdaptiveStorage, MemoryStorage, SQLiteStorage, default_storage, default_storage_path

__all__ = [
    "AdaptiveStorage",
    "MemoryStorage",
    "SQLiteStorage",
    "default_storage",
    "default_storage_path",
    "fingerprint",
    "relocate",
    "similar_elements",
    "similarity",
]
