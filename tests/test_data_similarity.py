"""Fingerprints, similarity estimates and de-duplication."""

from __future__ import annotations

import random
import statistics

import pytest

from wintergrab.data import Deduplicator, MinHashLSH, SimHashIndex, content_hash, jaccard, minhash
from wintergrab.data.similarity import (
    _simhash_reference,
    expected_collision,
    hamming,
    minhash_similarity,
    normalize_for_hash,
    shingles,
    simhash,
    simhash_similarity,
)

RNG = random.Random(7)
VOCAB = ["".join(RNG.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(RNG.randint(2, 9))) for _ in range(3000)]


def words(n: int, rng: random.Random) -> list[str]:
    return [rng.choice(VOCAB) for _ in range(n)]


def edited(text: list[str], edits: int, rng: random.Random) -> list[str]:
    out = list(text)
    for _ in range(edits):
        out[rng.randrange(len(out))] = rng.choice(VOCAB)
    return out


def test_normalization_and_content_hashes() -> None:
    assert normalize_for_hash("  Hello,   WORLD! ") == "hello world"
    assert content_hash("Hello, World!") == content_hash("hello world")
    assert content_hash({"b": 1, "a": [1, 2]}) == content_hash({"a": [1, 2], "b": 1})
    assert content_hash({"a": [1, 2]}) != content_hash({"a": [2, 1]})
    assert content_hash(None) == content_hash("")


def test_shingles() -> None:
    assert shingles("a b c d e f g", 3) == {"a b c", "b c d", "c d e", "d e f", "e f g"}
    assert shingles("short text") == {
        "short",
        "hort ",
        "ort t",
        "rt te",
        "t tex",
        " text",
    }  # characters for short texts
    assert shingles("") == set()


def test_fast_simhash_matches_the_textbook_algorithm() -> None:
    rng = random.Random(1)
    for n in (1, 3, 10, 60, 400):
        for _ in range(20):
            text = " ".join(words(n, rng))
            assert simhash(text) == _simhash_reference(text)
            assert simhash(text, bits=32) == _simhash_reference(text, bits=32)
    weighted = ["a", "a", "a", "b", "c"]
    assert simhash(weighted) == _simhash_reference(weighted)
    assert simhash("") == 0
    with pytest.raises(ValueError):
        simhash("x", bits=48)


def test_simhash_separates_edits_from_unrelated_pages() -> None:
    rng = random.Random(2)
    near, far = [], []
    for _ in range(50):
        page = words(1000, rng)
        near.append(hamming(simhash(" ".join(page)), simhash(" ".join(edited(page, 10, rng)))))
        far.append(hamming(simhash(" ".join(page)), simhash(" ".join(words(1000, rng)))))
    assert max(near) < min(far)
    assert simhash_similarity(5, 5) == 1.0


def test_simhash_index_finds_everything_within_the_distance() -> None:
    rng = random.Random(3)
    index = SimHashIndex(max_distance=3)
    stored = [rng.getrandbits(64) for _ in range(2000)]
    for i, fingerprint in enumerate(stored):
        index.add(i, fingerprint)
    assert len(index) == 2000
    for _ in range(200):
        target = rng.randrange(len(stored))
        flipped = stored[target]
        for bit in rng.sample(range(64), rng.randint(0, 3)):
            flipped ^= 1 << bit
        expected = sorted((i, hamming(flipped, f)) for i, f in enumerate(stored) if hamming(flipped, f) <= 3)
        assert sorted(index.query(flipped)) == expected  # exact: the pigeonhole index misses nothing
    assert index.find_or_add("new", stored[0]) == 0
    with pytest.raises(ValueError):
        SimHashIndex(max_distance=16)


def test_minhash_estimates_jaccard_without_bias() -> None:
    rng = random.Random(4)
    errors = []
    for n, edits in ((8, 1), (20, 1), (50, 5), (200, 20)):
        for _ in range(100):
            text = words(n, rng)
            a, b = shingles(" ".join(text)), shingles(" ".join(edited(text, edits, rng)))
            errors.append(minhash_similarity(minhash(a), minhash(b)) - jaccard(a, b))
    assert abs(statistics.fmean(errors)) < 0.01
    assert statistics.pstdev(errors) < 0.06  # about sqrt(J(1-J)/128)


def test_minhash_details() -> None:
    assert minhash("same text here", 32) == minhash("same text here", 32)
    assert minhash("same text here", 32, seed=2) != minhash("same text here", 32)
    assert minhash_similarity(minhash("", 16), minhash("", 16)) == 1.0
    assert minhash_similarity(minhash("alpha beta gamma delta"), minhash("zulu yankee xray whiskey")) < 0.1
    assert jaccard(set(), set()) == 1.0 and jaccard({1, 2}, {2, 3}) == pytest.approx(1 / 3)
    with pytest.raises(ValueError):
        minhash_similarity((1, 2), (1,))
    with pytest.raises(ValueError):
        minhash("x", num_perm=0)


def test_minhash_lsh() -> None:
    rng = random.Random(5)
    lsh = MinHashLSH(threshold=0.7, num_perm=128)
    assert lsh.bands * lsh.rows <= 128
    base = [words(60, rng) for _ in range(200)]
    for i, text in enumerate(base):
        lsh.insert(i, minhash(" ".join(text)))
    assert len(lsh) == 200
    found = 0
    for i in range(50):
        copy = " ".join(edited(base[i], 1, rng))
        matches = lsh.query(minhash(copy))
        found += bool(matches) and matches[0][0] == i
        assert all(similarity >= 0.7 for _, similarity in matches)
    assert found >= 45  # one edited word in 60 keeps the Jaccard similarity near 0.9
    assert lsh.query(minhash(" ".join(words(60, rng)))) == []
    assert expected_collision(0.9, lsh.bands, lsh.rows) > 0.99
    assert lsh.false_negative_rate(0.9) < 0.01
    with pytest.raises(ValueError):
        lsh.insert("short", (1, 2, 3))
    with pytest.raises(ValueError):
        MinHashLSH(threshold=1.5)


# --------------------------------------------------------------------------- #
# de-duplication
# --------------------------------------------------------------------------- #
def test_duplicates_by_key_after_normalization() -> None:
    dedupe = Deduplicator(key="url")
    records = [
        {"url": "https://shop.example/p/1?utm_source=mail", "name": "A"},
        {"url": "HTTPS://shop.example/p/1", "name": "A (again)"},
        {"url": "https://shop.example/p/2", "name": "B"},
        {"url": None, "name": "no key"},
    ]
    assert [r["name"] for r in dedupe.run(records)] == ["A", "B", "no key"]
    assert dedupe.duplicates == {"key": 1, "content": 0, "near": 0}


def test_duplicates_by_content() -> None:
    dedupe = Deduplicator(fields=["name", "price"])
    records = [
        {"name": "Phone", "price": 10, "seen": 1},
        {"name": " phone! ", "price": 10, "seen": 2},  # same after normalization
        {"name": "Phone", "price": 11, "seen": 3},
    ]
    assert [r["seen"] for r in dedupe.run(records)] == [1, 3]
    metadata_only = Deduplicator()
    assert len(metadata_only.run([{"a": 1, "_fetched": 1}, {"a": 1, "_fetched": 2}])) == 1  # metadata is ignored


def test_near_duplicates_and_marking() -> None:
    rng = random.Random(6)
    base = [words(60, rng) for _ in range(100)]
    records = [{"id": i, "text": " ".join(text)} for i, text in enumerate(base)]
    copies = [{"id": 1000 + i, "text": " ".join(edited(base[i], 1, rng))} for i in range(30)]
    dedupe = Deduplicator(fields=["text"], near=True, mark=True)
    out = dedupe.run(records + copies)
    assert len(out) == 130
    marked = [r for r in out if "_duplicate_of" in r]
    assert len(marked) >= 27
    assert all(r["_duplicate_kind"] == "near" and r["_duplicate_of"] == r["id"] - 1000 for r in marked)
    strict = Deduplicator(fields=["text"], near=True, similarity=0.99)
    assert len(strict.run(records + copies)) > 125


def test_deduplicator_as_an_item_pipeline() -> None:
    dedupe = Deduplicator(key=["sku", "region"])
    assert dedupe.process_item({"sku": "A", "region": "EU"}) is not None
    assert dedupe.process_item({"sku": "a", "region": "eu"}) is None  # keys compare case-insensitively
    assert dedupe.process_item({"sku": "A", "region": "US"}) is not None
    assert dedupe.process_item("not a record") == "not a record"
    assert "key" in repr(dedupe)
