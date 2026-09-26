"""Fingerprints and similarity: exact content hashes, SimHash and MinHash with fast indexes.

* :func:`content_hash`: identical text after normalization (case, whitespace,
  punctuation) -> identical hash. Catches exact duplicates.
* :func:`simhash` + :class:`SimHashIndex`: 64-bit fingerprints whose Hamming
  distance tracks how different two texts are. The index finds every stored
  fingerprint within ``max_distance`` bits without comparing against all of
  them (the pigeonhole trick: split the fingerprint into ``max_distance + 1``
  blocks; near-duplicates share at least one block exactly).
* :func:`minhash` + :class:`MinHashLSH`: signatures whose agreement rate
  estimates the Jaccard similarity of two sets of shingles; LSH banding finds
  candidates above a similarity threshold. Signatures use one-permutation
  hashing with optimal densification (Li et al. 2012; Shrivastava 2017): one
  hash per shingle instead of one per shingle *and* permutation, which in pure
  Python is about 15 times faster than classic MinHash, with the same accuracy.

SimHash suits long documents (whole pages); on short texts such as product
descriptions a few edited words move the fingerprint far (5% of words edited
typically costs 7-15 of 64 bits), so records are compared by MinHash instead.
"""

from __future__ import annotations

import hashlib
import math
import random
import re
import unicodedata
from array import array
from collections import Counter, defaultdict
from collections.abc import Hashable, Iterable, Sequence
from typing import Any

__all__ = [
    "MinHashLSH",
    "SimHashIndex",
    "content_hash",
    "hamming",
    "jaccard",
    "minhash",
    "minhash_similarity",
    "normalize_for_hash",
    "shingles",
    "simhash",
    "simhash_similarity",
]

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")
_MASK64 = (1 << 64) - 1


def normalize_for_hash(text: str) -> str:
    """Case-folded, accent-stripped (NFKC), punctuation-free, whitespace-squeezed text."""
    text = unicodedata.normalize("NFKC", text).casefold()
    return _WS.sub(" ", _PUNCT.sub(" ", text)).strip()


def content_hash(value: Any) -> str:
    """A stable hex digest of ``value``'s normalized text (dicts and lists: of their sorted items)."""
    return hashlib.blake2b(_canonical(value).encode("utf-8"), digest_size=16).hexdigest()


def _canonical(value: Any) -> str:
    if isinstance(value, dict):
        return "{" + ",".join(f"{k}:{_canonical(v)}" for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical(v) for v in value) + "]"
    if value is None:
        return ""
    return normalize_for_hash(str(value))


def shingles(text: str, k: int = 3, *, chars: bool | None = None) -> set[str]:
    """Overlapping ``k``-grams of words (or of characters for short texts / ``chars=True``)."""
    norm = normalize_for_hash(text)
    words = norm.split()
    if chars or (chars is None and len(words) < k * 2):
        n = max(1, k + 2)  # character 5-grams by default
        if len(norm) <= n:
            return {norm} if norm else set()
        return {norm[i : i + n] for i in range(len(norm) - n + 1)}
    if len(words) <= k:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + k]) for i in range(len(words) - k + 1)}


def _h64(feature: str) -> int:
    return int.from_bytes(hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest(), "little")


# Bits of a byte spread into 32-bit lanes: summing w * spread(h) counts, per bit position,
# the weight of the features whose hash has that bit set - all 64 counters in one big integer.
_LANE = 32
_SPREAD = [sum(((byte >> j) & 1) << (j * _LANE) for j in range(8)) for byte in range(256)]


def simhash(features: str | Iterable[str], bits: int = 64) -> int:
    """SimHash of a text (its word 3-gram shingles) or of explicit features (repeats count as weight)."""
    if bits not in (32, 64):
        raise ValueError("bits must be 32 or 64")
    counts = Counter(shingles(features) if isinstance(features, str) else features)
    if not counts:
        return 0
    accumulator = 0
    total = 0
    spread_table = _SPREAD
    for feature, weight in counts.items():
        h = _h64(feature)
        spread = 0
        for index in range(bits // 8):
            spread |= spread_table[(h >> (8 * index)) & 0xFF] << (index * 8 * _LANE)
        accumulator += weight * spread
        total += weight
    lane = (1 << _LANE) - 1
    fingerprint = 0
    for i in range(bits):
        if 2 * ((accumulator >> (i * _LANE)) & lane) > total:  # more weight with the bit set than without
            fingerprint |= 1 << i
    return fingerprint


def _simhash_reference(features: str | Iterable[str], bits: int = 64) -> int:
    """The textbook SimHash loop (tests check :func:`simhash` returns exactly this)."""
    counts = Counter(shingles(features) if isinstance(features, str) else features)
    vector = [0] * bits
    for feature, weight in counts.items():
        h = _h64(feature)
        for i in range(bits):
            vector[i] += weight if (h >> i) & 1 else -weight
    return sum(1 << i for i, v in enumerate(vector) if v > 0)


def hamming(a: int, b: int) -> int:
    """Number of differing bits."""
    return (a ^ b).bit_count()


def simhash_similarity(a: int, b: int, bits: int = 64) -> float:
    """``1 - hamming/bits``: 1.0 for identical fingerprints."""
    return 1.0 - hamming(a, b) / bits


class SimHashIndex:
    """Stored fingerprints, searchable for near-duplicates within ``max_distance`` bits."""

    def __init__(self, max_distance: int = 3, bits: int = 64) -> None:
        if not 0 <= max_distance < bits // 4:
            raise ValueError("max_distance must be between 0 and bits/4")
        self.max_distance = max_distance
        self.bits = bits
        blocks = max_distance + 1
        size = bits // blocks
        self._blocks = [(i * size, bits if i == blocks - 1 else (i + 1) * size) for i in range(blocks)]
        self._tables: list[dict[int, list[tuple[Hashable, int]]]] = [defaultdict(list) for _ in self._blocks]
        self._size = 0

    def __len__(self) -> int:
        return self._size

    def _parts(self, fingerprint: int) -> list[int]:
        return [(fingerprint >> lo) & ((1 << (hi - lo)) - 1) for lo, hi in self._blocks]

    def add(self, key: Hashable, fingerprint: int) -> None:
        for table, part in zip(self._tables, self._parts(fingerprint), strict=True):
            table[part].append((key, fingerprint))
        self._size += 1

    def query(self, fingerprint: int) -> list[tuple[Hashable, int]]:
        """``(key, distance)`` of every stored fingerprint within ``max_distance``, closest first."""
        found: dict[Hashable, int] = {}
        for table, part in zip(self._tables, self._parts(fingerprint), strict=True):
            for key, other in table.get(part, ()):
                if key not in found:
                    distance = hamming(fingerprint, other)
                    if distance <= self.max_distance:
                        found[key] = distance
        return sorted(found.items(), key=lambda kv: kv[1])

    def find_or_add(self, key: Hashable, fingerprint: int) -> Hashable | None:
        """The key of a near-duplicate already stored; otherwise store ``key`` and return ``None``."""
        matches = self.query(fingerprint)
        if matches:
            return matches[0][0]
        self.add(key, fingerprint)
        return None


def jaccard(a: set[Any], b: set[Any]) -> float:
    """Size of the intersection over size of the union (1.0 for two empty sets)."""
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


_EMPTY_BIN = _MASK64


def _probe_orders(num_perm: int, seed: int) -> list[list[int]]:
    """For each bin, the other bins in a fixed pseudo-random order (where an empty bin borrows from)."""
    rng = random.Random(f"wintergrab-densify-{num_perm}-{seed}")
    orders = []
    for i in range(num_perm):
        others = [j for j in range(num_perm) if j != i]
        rng.shuffle(others)
        orders.append(others)
    return orders


_PROBES: dict[tuple[int, int], list[list[int]]] = {}


def minhash(features: str | Iterable[str], num_perm: int = 128, seed: int = 1) -> tuple[int, ...]:
    """MinHash signature of a text's shingles (or of a set of features).

    One-permutation hashing: each feature is hashed once into one of
    ``num_perm`` bins and every bin keeps its smallest value; empty bins then
    borrow from other bins along a fixed random order ("optimal
    densification"), which keeps the estimate unbiased for small sets.
    """
    if num_perm < 1:
        raise ValueError("num_perm must be positive")
    items = shingles(features) if isinstance(features, str) else set(features)
    bins = [_EMPTY_BIN] * num_perm
    salt = seed.to_bytes(8, "little", signed=False)
    for item in items:
        h = int.from_bytes(hashlib.blake2b(item.encode("utf-8"), digest_size=8, salt=salt).digest(), "little")
        index = (h * num_perm) >> 64  # the high bits pick the bin...
        value = (h * 0x9E3779B97F4A7C15) & _MASK64  # ...and a mix of all bits orders the values inside it
        if value < bins[index]:
            bins[index] = value
    if _EMPTY_BIN in bins and len(items) > 0:
        orders = _PROBES.get((num_perm, seed))
        if orders is None:
            orders = _PROBES[(num_perm, seed)] = _probe_orders(num_perm, seed)
        filled = list(bins)
        for i, value in enumerate(bins):
            if value == _EMPTY_BIN:
                filled[i] = next(bins[j] for j in orders[i] if bins[j] != _EMPTY_BIN)
        bins = filled
    return tuple(bins)


def minhash_similarity(a: Sequence[int], b: Sequence[int]) -> float:
    """Estimated Jaccard similarity: the share of agreeing signature slots."""
    if len(a) != len(b) or not a:
        raise ValueError("signatures must have the same, non-zero length")
    return sum(1 for x, y in zip(a, b, strict=True) if x == y) / len(a)


def _bands_for(threshold: float, num_perm: int) -> tuple[int, int]:
    """``(bands, rows)`` with ``bands * rows <= num_perm`` whose S-curve midpoint is closest to ``threshold``."""
    best: tuple[float, int, int] | None = None
    for rows in range(1, num_perm + 1):
        bands = num_perm // rows
        if bands < 1:
            break
        midpoint = (1 / bands) ** (1 / rows)
        score = abs(midpoint - threshold)
        if best is None or score < best[0]:
            best = (score, bands, rows)
    assert best is not None
    return best[1], best[2]


class MinHashLSH:
    """Locality-sensitive hashing over MinHash signatures: candidate pairs above ``threshold`` Jaccard."""

    def __init__(self, threshold: float = 0.8, num_perm: int = 128) -> None:
        if not 0.0 < threshold < 1.0:
            raise ValueError("threshold must be between 0 and 1")
        self.threshold = threshold
        self.num_perm = num_perm
        self.bands, self.rows = _bands_for(threshold, num_perm)
        # Band keys are hashes of the band's values (compact; the stored signature settles collisions).
        self._tables: list[dict[int, list[Hashable]]] = [defaultdict(list) for _ in range(self.bands)]
        self._signatures: dict[Hashable, array[int]] = {}

    def __len__(self) -> int:
        return len(self._signatures)

    def _band_keys(self, signature: Sequence[int]) -> list[int]:
        if len(signature) != self.num_perm:
            raise ValueError(f"signature has {len(signature)} values, expected {self.num_perm}")
        rows = self.rows
        return [hash(tuple(signature[i * rows : (i + 1) * rows])) for i in range(self.bands)]

    def insert(self, key: Hashable, signature: Sequence[int]) -> None:
        for table, band in zip(self._tables, self._band_keys(signature), strict=True):
            table[band].append(key)
        self._signatures[key] = array("Q", signature)

    def query(self, signature: Sequence[int], *, verify: bool = True) -> list[tuple[Hashable, float]]:
        """``(key, estimated similarity)`` of stored candidates (``verify`` drops those below the threshold)."""
        candidates: set[Hashable] = set()
        for table, band in zip(self._tables, self._band_keys(signature), strict=True):
            candidates.update(table.get(band, ()))
        out = []
        for key in candidates:
            similarity = minhash_similarity(signature, self._signatures[key])
            if not verify or similarity >= self.threshold:
                out.append((key, similarity))
        out.sort(key=lambda kv: -kv[1])
        return out

    def false_negative_rate(self, similarity: float) -> float:
        """Probability that a pair with this Jaccard similarity is *not* found."""
        return (1 - similarity**self.rows) ** self.bands

    def __repr__(self) -> str:
        return f"MinHashLSH(threshold={self.threshold}, bands={self.bands}, rows={self.rows})"


def expected_collision(similarity: float, bands: int, rows: int) -> float:
    """Probability that two sets of the given Jaccard similarity share at least one LSH band."""
    return 1 - (1 - similarity**rows) ** bands


def _entropy(values: Iterable[Any]) -> float:  # used by quality reports
    counts = Counter(values)
    total = sum(counts.values())
    return -sum(c / total * math.log2(c / total) for c in counts.values()) if total else 0.0
