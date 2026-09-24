"""Element fingerprints and similarity scoring for adaptive selectors.

A fingerprint captures what an element *looks like* (tag, attributes, text)
and *where it lives* (ancestor path, parent, siblings, children). When a
selector stops matching after a site redesign, every element of the new page
is scored against the saved fingerprint and the closest match wins.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from difflib import SequenceMatcher
from typing import Any

from lxml import etree

from ..parser.text import own_text, tag_name

MAX_TEXT = 200
MAX_ATTR = 200

# Attributes whose *values* differ between otherwise identical items
# (links, images, ids...). For these only the presence of the key is compared.
VOLATILE_ATTRS = frozenset(
    {"href", "src", "srcset", "alt", "title", "id", "value", "content", "datetime", "style", "name", "for", "action"}
)

W_TAG = 1.0
W_ATTRS = 1.0
W_TEXT = 1.5
W_PATH = 1.0
W_PARENT = 0.75
W_SIBLINGS = 0.5
W_CHILDREN = 0.5
W_POSITION = 0.25


def _attrs(el: etree._Element) -> dict[str, str]:
    return {str(k): str(v)[:MAX_ATTR] for k, v in el.attrib.items()}


def _elements(nodes: Iterable[Any]) -> list[etree._Element]:
    return [n for n in nodes if isinstance(n.tag, str)]


class _Family:
    """Per-parent facts shared by all of its children (tag counts, positions)."""

    __slots__ = ("counts", "parent_info", "positions")

    def __init__(self, parent: etree._Element) -> None:
        self.counts: Counter[str] = Counter()
        self.positions: dict[etree._Element, int] = {}
        seen: Counter[Any] = Counter()
        for child in parent:
            if not isinstance(child.tag, str):
                continue
            self.counts[tag_name(child)] += 1
            self.positions[child] = seen[child.tag]
            seen[child.tag] += 1
        self.parent_info = {"tag": tag_name(parent), "attrs": _attrs(parent), "text": own_text(parent)[:MAX_TEXT]}


def fingerprint(el: etree._Element, _cache: dict[Any, _Family] | None = None) -> dict[str, Any]:
    """Describe ``el`` as a JSON-serialisable dict.

    ``_cache`` lets :func:`relocate` share per-parent work between siblings, so
    fingerprinting a page with thousands of rows stays linear.
    """
    parent = el.getparent()
    tag = tag_name(el)
    fp: dict[str, Any] = {
        "tag": tag,
        "attrs": _attrs(el),
        "text": own_text(el)[:MAX_TEXT],
        "path": [tag_name(a) for a in el.iterancestors()][::-1],
        "children": dict(Counter(tag_name(c) for c in _elements(el))),
        "parent": None,
        "siblings": {},
        "position": 0,
    }
    if parent is not None:
        family = _cache.get(parent) if _cache is not None else None
        if family is None:
            family = _Family(parent)
            if _cache is not None:
                _cache[parent] = family
        siblings = family.counts.copy()
        siblings[tag] -= 1
        fp["parent"] = dict(family.parent_info)
        fp["siblings"] = {k: v for k, v in siblings.items() if v > 0}
        fp["position"] = family.positions.get(el, 0)
    return fp


# --------------------------------------------------------------------------- #
# similarity primitives
# --------------------------------------------------------------------------- #


def _ratio(a: str | Sequence[str], b: str | Sequence[str]) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _counter_overlap(a: dict[str, int], b: dict[str, int]) -> float:
    """Multiset similarity: shared counts over total counts."""
    if not a and not b:
        return 1.0
    keys = set(a) | set(b)
    shared = sum(min(a.get(k, 0), b.get(k, 0)) for k in keys)
    total = sum(max(a.get(k, 0), b.get(k, 0)) for k in keys)
    return shared / total if total else 1.0


def _class_similarity(a: str, b: str) -> float:
    """Fuzzy token-set similarity so ``product`` ~ ``product-card``."""
    ta, tb = set(a.split()), set(b.split())
    if ta == tb:
        return 1.0
    if not ta or not tb:
        return 0.0

    def directed(x: set[str], y: set[str]) -> float:
        return sum(max(_ratio(t, u) for u in y) for t in x) / len(x)

    return (directed(ta, tb) + directed(tb, ta)) / 2


def attrs_similarity(
    a: dict[str, str],
    b: dict[str, str],
    *,
    ignore_values: frozenset[str] = frozenset(),
    partial_values: frozenset[str] = frozenset(),
) -> float:
    """0..1 similarity of two attribute dicts (class compared as fuzzy token sets).

    Keys in ``ignore_values`` (and ``data-*`` when it is set) only need to be
    present on both sides; keys in ``partial_values`` get half credit for
    being present and half for how similar their values are.
    """
    keys = set(a) | set(b)
    if not keys:
        return 1.0
    total = 0.0
    for key in keys:
        if key not in a or key not in b:
            continue
        if key == "class":
            total += _class_similarity(a[key], b[key])
        elif key in ignore_values or (ignore_values and key.startswith("data-")):
            total += 1.0  # same key present on both; the value is expected to differ
        elif key in partial_values or (partial_values and key.startswith("data-")):
            total += 0.5 + 0.5 * _ratio(a[key], b[key])
        else:
            total += _ratio(a[key], b[key])
    return total / len(keys)


def _parent_similarity(sp: dict[str, Any] | None, cp: dict[str, Any] | None) -> float:
    if not sp or not cp:
        return 1.0 if sp == cp else 0.0
    text = _ratio(sp["text"], cp["text"]) if (sp["text"] or cp["text"]) else 1.0
    return ((1.0 if sp["tag"] == cp["tag"] else 0.0) + attrs_similarity(sp["attrs"], cp["attrs"]) + text) / 3


def similarity(saved: dict[str, Any], candidate: dict[str, Any], floor: float = 0.0) -> float:
    """Weighted 0..1 similarity between two fingerprints.

    Components are computed cheapest first; if the score provably cannot reach
    ``floor`` the function stops early and returns a value below ``floor``.
    """
    has_text = bool(saved["text"] or candidate["text"])
    components: list[tuple[float, Callable[[], float]]] = [
        (W_TAG, lambda: 1.0 if saved["tag"] == candidate["tag"] else 0.0),
        (W_POSITION, lambda: 1.0 / (1 + abs(saved.get("position", 0) - candidate.get("position", 0)))),
        (W_CHILDREN, lambda: _counter_overlap(saved["children"], candidate["children"])),
        (W_SIBLINGS, lambda: _counter_overlap(saved["siblings"], candidate["siblings"])),
        (W_PATH, lambda: _ratio(saved["path"], candidate["path"])),
        (W_ATTRS, lambda: attrs_similarity(saved["attrs"], candidate["attrs"], partial_values=VOLATILE_ATTRS)),
        (W_PARENT, lambda: _parent_similarity(saved.get("parent"), candidate.get("parent"))),
    ]
    if has_text:
        components.append((W_TEXT, lambda: _ratio(saved["text"], candidate["text"])))
    total_weight = sum(w for w, _ in components)
    target = floor * total_weight
    score = 0.0
    remaining = total_weight
    for weight, compute in components:
        remaining -= weight
        score += weight * compute()
        if score + remaining < target:
            return (score + remaining) / total_weight - 1e-9
    return score / total_weight


# --------------------------------------------------------------------------- #
# relocation
# --------------------------------------------------------------------------- #


def relocate(
    root: etree._Element,
    record: dict[str, Any],
    *,
    min_score: float = 0.55,
) -> tuple[list[etree._Element], float]:
    """Find the elements in ``root`` that best match a saved record.

    Returns the matching elements (in document order) and the best score.
    When the saved selector originally matched several elements (a list of
    products, say) the best match is expanded to its structurally similar
    siblings with :func:`similar_elements`.
    """
    saved_fps: list[dict[str, Any]] = record.get("elements") or []
    if not saved_fps:
        return [], 0.0
    candidates = [el for el in root.iter(etree.Element) if isinstance(el.tag, str)]
    if not candidates:
        return [], 0.0
    cache: dict[Any, _Family] = {}
    cand_fps = [fingerprint(el, cache) for el in candidates]

    matches: dict[int, float] = {}
    best_score, best_index = 0.0, -1
    for saved in saved_fps:
        top_score, top_index = 0.0, -1
        for i, fp in enumerate(cand_fps):
            s = similarity(saved, fp, floor=max(top_score, min_score * 0.5))
            if s > top_score:
                top_score, top_index = s, i
        if top_index >= 0 and top_score >= min_score:
            matches[top_index] = max(matches.get(top_index, 0.0), top_score)
        if top_score > best_score:
            best_score, best_index = top_score, top_index

    if best_score < min_score:
        return [], best_score

    found = {candidates[i] for i in matches}
    if int(record.get("count", 1)) > 1:
        found.update(similar_elements(candidates[best_index], root=root))
    order = {el: i for i, el in enumerate(candidates)}
    return sorted(found, key=lambda el: order.get(el, 0)), best_score


def similar_elements(
    el: etree._Element,
    *,
    root: etree._Element | None = None,
    threshold: float = 0.5,
    ignore_attributes: Iterable[str] = VOLATILE_ATTRS,
) -> list[etree._Element]:
    """Elements that look structurally like ``el`` (e.g. the other cards in a grid).

    Candidates must share the tag, depth, parent tag and grandparent tag of
    ``el``; they are then scored on attribute similarity (values of volatile
    attributes like ``href`` are ignored) and on the shape of their children.
    """
    if not isinstance(el.tag, str):
        return []
    if root is None:
        root = el.getroottree().getroot()
    ignore = frozenset(ignore_attributes)
    ancestors = [tag_name(a) for a in el.iterancestors()]
    depth = len(ancestors)
    lineage = ancestors[:2]
    base_attrs = _attrs(el)
    base_children = dict(Counter(tag_name(c) for c in _elements(el)))
    out = []
    for cand in root.iter(el.tag):
        if cand is el:
            continue
        cand_ancestors = [tag_name(a) for a in cand.iterancestors()]
        if len(cand_ancestors) != depth or cand_ancestors[:2] != lineage:
            continue
        attr_score = attrs_similarity(base_attrs, _attrs(cand), ignore_values=ignore)
        child_score = _counter_overlap(base_children, dict(Counter(tag_name(c) for c in _elements(cand))))
        if 0.6 * attr_score + 0.4 * child_score >= threshold:
            out.append(cand)
    return out
