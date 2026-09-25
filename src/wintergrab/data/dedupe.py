"""De-duplicating records as they stream through a crawl.

:class:`Deduplicator` catches three kinds of duplicates, cheapest first:

1. **Same key**: records whose key fields (``url``, ``sku``...) are equal after
   normalization (case, whitespace, punctuation; URLs via the URL normalizer).
2. **Same content**: records whose chosen fields hash identically after normalization.
3. **Near duplicates** (``near=True``): records whose text shares at least
   ``similarity`` of its word 3-grams with an earlier record's (Jaccard
   similarity, estimated with MinHash and found with LSH): lightly edited or
   re-ordered copies. Short template texts can look alike while describing
   different things ("Blue shirt, size M" / "Red shirt, size M"), so near
   duplicates are opt-in and best checked with ``mark=True`` first.

It is an item pipeline (``Spider.pipelines = [Deduplicator(key=["url"])]``) and
also works on plain lists (:meth:`Deduplicator.run`). Duplicates are dropped,
or kept and marked with ``_duplicate_of`` when ``mark=True``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..urls import normalize_url
from .similarity import MinHashLSH, content_hash, minhash, normalize_for_hash

__all__ = ["Deduplicator"]

_NUM_PERM = 64  # MinHash slots per record: +-0.05 on the similarity estimate, 512 bytes of memory


class Deduplicator:
    """Drop (or mark) duplicate records. See the module docs.

    Args:
        key: Fields forming the record's identity. Records missing any of them are not key-checked.
        fields: Fields compared for content duplicates (default: all non-metadata fields).
        near: Also catch near duplicates, comparing ``text_fields`` (default ``fields``).
        similarity: The Jaccard similarity (0-1) from which texts count as near duplicates.
        mark: Keep duplicates and set ``_duplicate_of`` (and ``_duplicate_kind``) instead of dropping them.
    """

    def __init__(
        self,
        key: str | Sequence[str] | None = None,
        *,
        fields: Sequence[str] | None = None,
        near: bool = False,
        text_fields: Sequence[str] | None = None,
        similarity: float = 0.8,
        mark: bool = False,
    ) -> None:
        self.key = [key] if isinstance(key, str) else list(key or [])
        self.fields = list(fields) if fields is not None else None
        self.near = near
        self.text_fields = list(text_fields) if text_fields is not None else self.fields
        self.mark = mark
        self._keys: dict[tuple[str, ...], int] = {}
        self._hashes: dict[str, int] = {}
        self.similarity = similarity
        self._lsh = MinHashLSH(threshold=similarity, num_perm=_NUM_PERM) if near else None
        self.seen = 0
        self.duplicates: dict[str, int] = {"key": 0, "content": 0, "near": 0}

    # -- identity ----------------------------------------------------------- #
    @staticmethod
    def _norm(name: str, value: Any) -> str:
        if isinstance(value, str) and (
            name == "url" or name.endswith("_url") or value.startswith(("http://", "https://"))
        ):
            try:
                return normalize_url(value)
            except ValueError:
                return value
        return normalize_for_hash(str(value)) if not isinstance(value, (dict, list)) else content_hash(value)

    def _key_of(self, record: Mapping[str, Any]) -> tuple[str, ...] | None:
        if not self.key:
            return None
        parts = []
        for name in self.key:
            value = record.get(name)
            if value is None or value == "":
                return None
            parts.append(self._norm(name, value))
        return tuple(parts)

    def _content(self, record: Mapping[str, Any]) -> dict[str, Any]:
        names = self.fields if self.fields is not None else [k for k in record if not str(k).startswith("_")]
        return {k: record.get(k) for k in names}

    def _text(self, record: Mapping[str, Any]) -> str:
        names = self.text_fields if self.text_fields is not None else [k for k in record if not str(k).startswith("_")]
        return " ".join(str(record.get(k)) for k in names if record.get(k) not in (None, ""))

    # -- checking ----------------------------------------------------------- #
    def check(self, record: Mapping[str, Any]) -> tuple[str, int] | None:
        """``(kind, index of the first record it duplicates)`` or ``None``; remembers new records."""
        index = self.seen
        key = self._key_of(record)
        if key is not None and key in self._keys:
            return "key", self._keys[key]
        digest = content_hash(self._content(record))
        if digest in self._hashes:
            return "content", self._hashes[digest]
        signature = None
        if self._lsh is not None:
            text = self._text(record)
            if text.strip():
                signature = minhash(text, num_perm=_NUM_PERM)
                matches = self._lsh.query(signature)
                if matches:
                    return "near", int(matches[0][0])  # type: ignore[call-overload]
        if key is not None:
            self._keys[key] = index
        self._hashes[digest] = index
        if signature is not None and self._lsh is not None:
            self._lsh.insert(index, signature)
        self.seen += 1
        return None

    def process_item(self, item: Any, spider: Any = None) -> Any:
        if not isinstance(item, Mapping):
            return item
        found = self.check(item)
        if found is None:
            return item
        kind, first = found
        self.duplicates[kind] += 1
        if not self.mark:
            return None
        marked = dict(item)
        marked["_duplicate_of"] = first
        marked["_duplicate_kind"] = kind
        return marked

    def run(self, records: Iterable[Mapping[str, Any]]) -> list[Any]:
        """De-duplicate a list (the first occurrence is kept)."""
        out = []
        for record in records:
            result = self.process_item(record)
            if result is not None:
                out.append(result)
        return out

    def __repr__(self) -> str:
        return f"Deduplicator(key={self.key}, near={self.near}, duplicates={self.duplicates})"
