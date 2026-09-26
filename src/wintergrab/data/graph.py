"""A knowledge graph from records: the things they name, resolved, how they relate, and where each fact came from.

::

    graph = KnowledgeGraph.from_records(read_records("products.jsonl"), "product")
    print(graph.describe())
    graph.save("products.graph.json")    # or .graphml (Gephi, yEd...), or a directory: nodes.csv, edges.csv (Neo4j)
    for edge, brand in graph.neighbors("product:lamp", "manufactured_by"): ...

Each record is a node of its kind: a product, a job, an article. Its fields
that name other things become nodes too, and typed edges link them
(:data:`RELATIONS` has one list per extraction template)::

    product  -manufactured_by-> brand       product  -belongs_to->   category
    job      -offered_by->      company     job      -located_in->   location
    article  -written_by->      person      article  -tagged->       category
    event    -held_at->         location    event    -organized_by-> organization
    company  -located_in->      location    person   -works_for->    organization
    review   -about->           product     review   -written_by->   person

Names are resolved as :class:`~wintergrab.data.entities.EntityResolver`
resolves them: "Apple Inc." and "APPLE INC" are one company, "Apple" and
"Apple Records" are not, and what is unsure is listed for review, never
merged silently. Categories are matched by name. Records of the kinds that
have identities of their own (products, companies, people) are resolved
too: the same product found on two sites is one node. Other records are one
node per URL.

Every node keeps its spellings, its attributes, the pages it was seen on,
and how sure the resolution is. Every edge keeps the pages that state it and
how sure the extraction was of the field behind it: the field's confidence
in the record's ``_provenance``, else the record's ``_confidence``. Records
from several files, of several kinds, make one graph: a job's company and a
company's own record meet in one node.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .entities import KINDS, Entity, EntityResolver, Match
from .expressions import get_path

__all__ = ["RELATIONS", "Edge", "KnowledgeGraph", "Node", "Relation"]


@dataclass(frozen=True)
class Relation:
    """A field of a record that names another thing: an edge from the record to it.

    Attributes:
        field: The field (a dotted path works; a list gives an edge per item).
        relation: The edge's type (``"manufactured_by"``).
        kind: What the field names: an entity kind (``company``, ``organization``, ``brand``,
            ``product``, ``person``, ``location``), resolved, or any other word (``category``),
            matched by name.
    """

    field: str
    relation: str
    kind: str

    @classmethod
    def parse(cls, text: str) -> Relation:
        """``"FIELD=RELATION:KIND"``, as the command line takes it (``brand=manufactured_by:brand``)."""
        path, _, rest = text.partition("=")
        relation, _, kind = rest.partition(":")
        if not path.strip() or not relation.strip() or not kind.strip():
            raise ValueError(f"a relation is FIELD=RELATION:KIND (brand=manufactured_by:brand), not {text!r}")
        return cls(path.strip(), relation.strip(), kind.strip())


#: The relations of each extraction template's records.
RELATIONS: dict[str, tuple[Relation, ...]] = {
    "product": (Relation("brand", "manufactured_by", "brand"), Relation("category", "belongs_to", "category")),
    "article": (
        Relation("author", "written_by", "person"),
        Relation("section", "belongs_to", "category"),
        Relation("tags", "tagged", "category"),
    ),
    "job": (Relation("company", "offered_by", "company"), Relation("location", "located_in", "location")),
    "event": (
        Relation("venue", "held_at", "location"),
        Relation("city", "located_in", "location"),
        Relation("organizer", "organized_by", "organization"),
        Relation("performer", "performed_by", "person"),
    ),
    "company": (
        Relation("city", "located_in", "location"),
        Relation("country", "located_in", "location"),
        Relation("industry", "belongs_to", "category"),
    ),
    "place": (
        Relation("city", "located_in", "location"),
        Relation("country", "located_in", "location"),
        Relation("cuisine", "belongs_to", "category"),
    ),
    "person": (
        Relation("organization", "works_for", "organization"),
        Relation("location", "located_in", "location"),
    ),
    "review": (Relation("author", "written_by", "person"), Relation("item", "about", "product")),
    "property": (Relation("city", "located_in", "location"),),
    "documentation": (Relation("section", "belongs_to", "category"),),
    "recipe": (Relation("author", "written_by", "person"),),
}
#: Record kinds resolved like the names they give (the same product on two sites is one node), and the
#: fields compared as evidence.
RESOLVED: dict[str, tuple[str, ...]] = {
    "product": ("gtin", "sku", "brand"),
    "company": ("website", "telephone", "email", "country"),
    "person": ("email", "organization"),
}
_NAME_FIELDS = ("name", "title", "headline")


@dataclass
class Node:
    """A thing in the graph (see the module docs).

    Attributes:
        id: ``"brand:acme"``, ``"product:lamp"``, ``"job:https://jobs.example/1"``...
        kind: What it is (``"brand"``, ``"product"``...).
        name: Its canonical name.
        aliases: Every spelling seen, most common first.
        attributes: Every value seen for each attribute, most common first (records: their fields).
        sources: The pages it was seen on.
        confidence: How sure the resolution is that its mentions are one thing (1.0 for one mention).
        count: Its mentions.
    """

    id: str
    kind: str
    name: str
    aliases: list[str] = field(default_factory=list)
    attributes: dict[str, list[Any]] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    confidence: float = 1.0
    count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "aliases": self.aliases,
            "attributes": self.attributes,
            "sources": self.sources,
            "confidence": round(self.confidence, 4),
            "count": self.count,
        }


@dataclass
class Edge:
    """A fact: ``source`` -``relation``-> ``target`` (node ids), the pages that state it, and how sure.

    Attributes:
        confidence: The surest extraction of it (the field's confidence), when known.
    """

    source: str
    relation: str
    target: str
    sources: list[str] = field(default_factory=list)
    count: int = 0
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "relation": self.relation,
            "target": self.target,
            "sources": self.sources,
            "count": self.count,
            "confidence": None if self.confidence is None else round(self.confidence, 4),
        }


@dataclass(eq=False)
class _Handle:
    """A mention waiting for resolution: of an entity kind (``mention``), or matched by ``key``."""

    kind: str
    name: str
    source: str | None = None
    mention: Any = None
    key: str | None = None


class KnowledgeGraph:
    """Nodes and typed edges built from records (see the module docs).

    Args:
        merge_threshold, review_threshold: As for :class:`~wintergrab.data.entities.EntityResolver`.
    """

    def __init__(self, *, merge_threshold: float = 0.95, review_threshold: float = 0.5) -> None:
        self.merge_threshold = merge_threshold
        self.review_threshold = review_threshold
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        #: Pairs of names that may be one thing, not merged (from the entity resolution).
        self.review: list[Match] = []
        self._resolvers: dict[str, EntityResolver] = {}
        self._records: list[tuple[_Handle, dict[str, Any]]] = []
        self._facts: list[tuple[_Handle, str, _Handle, str | None, float | None]] = []
        self._by_key: dict[tuple[str, str], list[_Handle]] = defaultdict(list)
        self._out: dict[str, list[Edge]] = defaultdict(list)
        self._in: dict[str, list[Edge]] = defaultdict(list)

    @classmethod
    def from_records(
        cls,
        records: Iterable[Mapping[str, Any]],
        kind: str,
        *,
        relations: Sequence[Relation] | None = None,
        **options: Any,
    ) -> KnowledgeGraph:
        """A graph of ``records`` of ``kind`` (``relations``: the edges, by default :data:`RELATIONS`)."""
        graph = cls(**options)
        graph.add_records(records, kind, relations=relations)
        return graph.build()

    # -- adding ------------------------------------------------------------------------------------ #
    def add_records(
        self,
        records: Iterable[Mapping[str, Any]],
        kind: str,
        *,
        relations: Sequence[Relation] | None = None,
        source_field: str = "url",
    ) -> int:
        """Add ``records`` of ``kind``, their relations (by default :data:`RELATIONS` ``[kind]``), and where
        they came from (``source_field``). Call :meth:`build` when every file is in. Returns how many."""
        wanted = RELATIONS.get(kind, ()) if relations is None else tuple(relations)
        added = 0
        for record in records:
            if not isinstance(record, Mapping):
                continue
            source = get_path(record, source_field)
            source = str(source) if source not in (None, "") else None
            name = next((record[f] for f in _NAME_FIELDS if record.get(f) not in (None, "", [])), None)
            name = str(name) if name is not None else source
            if name is None:
                continue
            attributes = {f: record.get(f) for f in RESOLVED.get(kind, ()) if record.get(f) not in (None, "", [])}
            subject = self._handle(kind, name, source, attributes, record_key=None if kind in RESOLVED else source)
            if subject is None:
                continue
            self._records.append((subject, dict(record)))
            for relation in wanted:
                for value in _values(get_path(record, relation.field)):
                    target = self._handle(relation.kind, str(value), source, {})
                    if target is not None:
                        self._facts.append((subject, relation.relation, target, source,
                                            _confidence(record, relation.field)))  # fmt: skip
            added += 1
        return added

    def _handle(
        self, kind: str, name: str, source: str | None, attributes: dict[str, Any], *, record_key: str | None = None
    ) -> _Handle | None:
        name = name.strip()
        if not name:
            return None
        if record_key is None and kind in KINDS:  # a name to resolve
            resolver = self._resolvers.get(kind)
            if resolver is None:
                resolver = self._resolvers[kind] = EntityResolver(
                    kind, merge_threshold=self.merge_threshold, review_threshold=self.review_threshold
                )
            mention = resolver.add(name, source=source, **attributes)
            if mention is None:
                return None
            return _Handle(kind, name, source, mention=mention)
        key = record_key or " ".join(name.casefold().split())  # a page, or a name matched as it is
        handle = _Handle(kind, name, source, key=key)
        self._by_key[(kind, key)].append(handle)
        return handle

    # -- building ---------------------------------------------------------------------------------- #
    def build(self) -> KnowledgeGraph:
        """Resolve the names and make the nodes and edges (again, after more :meth:`add_records`)."""
        self.nodes, self.edges, self.review = {}, [], []
        self._out, self._in = defaultdict(list), defaultdict(list)
        node_of: dict[int, str] = {}
        for resolver in self._resolvers.values():
            resolution = resolver.resolve()
            self.review.extend(resolution.review)
            for entity in resolution.entities:
                self.nodes[entity.id] = _node(entity)
                for mention in entity.mentions:
                    node_of[id(mention)] = entity.id
        taken = set(self.nodes)
        for (kind, key), handles in self._by_key.items():
            node_id = _unique(f"{kind}:{_slug(key)}", taken)
            spellings = Counter(h.name for h in handles)
            sources = list(dict.fromkeys(h.source for h in handles if h.source))
            self.nodes[node_id] = Node(node_id, kind, spellings.most_common(1)[0][0],
                                       aliases=[s for s, _ in spellings.most_common()], sources=sources,
                                       count=len(handles))  # fmt: skip
            for handle in handles:
                node_of[id(handle)] = node_id
        for handle, record in self._records:  # a record's fields are its node's attributes
            node = self.nodes[self._node_id(handle, node_of)]
            for name, value in record.items():
                if not name.startswith("_") and value not in (None, "", []):
                    values = node.attributes.setdefault(name, [])
                    if value not in values:
                        values.append(value)
        merged: dict[tuple[str, str, str], Edge] = {}
        for subject, relation, target, source, confidence in self._facts:
            a, b = self._node_id(subject, node_of), self._node_id(target, node_of)
            edge = merged.get((a, relation, b))
            if edge is None:
                edge = merged[(a, relation, b)] = Edge(a, relation, b)
            edge.count += 1
            if source and source not in edge.sources:
                edge.sources.append(source)
            if confidence is not None:
                edge.confidence = max(edge.confidence or 0.0, confidence)
        self.edges = list(merged.values())
        for edge in self.edges:
            self._out[edge.source].append(edge)
            self._in[edge.target].append(edge)
        return self

    @staticmethod
    def _node_id(handle: _Handle, node_of: Mapping[int, str]) -> str:
        return node_of[id(handle.mention)] if handle.key is None else node_of[id(handle)]

    # -- reading ----------------------------------------------------------------------------------- #
    def node(self, node_id: str) -> Node:
        return self.nodes[node_id]

    def find(self, name: str, kind: str | None = None) -> list[Node]:
        """Nodes spelled ``name`` (ignoring case), of ``kind`` if given."""
        wanted = " ".join(name.casefold().split())
        return [n for n in self.nodes.values() if (kind is None or n.kind == kind)
                and any(" ".join(a.casefold().split()) == wanted for a in n.aliases or [n.name])]  # fmt: skip

    def neighbors(
        self, node_id: str, relation: str | None = None, *, direction: str = "out"
    ) -> list[tuple[Edge, Node]]:
        """The edges from (``"out"``), to (``"in"``) or at (``"both"``) a node, with the node at their other end."""
        found: list[tuple[Edge, Node]] = []
        if direction in ("out", "both"):
            found += [(e, self.nodes[e.target]) for e in self._out.get(node_id, ()) if relation in (None, e.relation)]
        if direction in ("in", "both"):
            found += [(e, self.nodes[e.source]) for e in self._in.get(node_id, ()) if relation in (None, e.relation)]
        return found

    def describe(self, *, top: int = 5) -> str:
        """The nodes and edges by kind, the most connected nodes of each kind that is linked to, and what
        to review."""
        kinds = Counter(n.kind for n in self.nodes.values())
        relations = Counter(e.relation for e in self.edges)
        lines = [
            f"{len(self.nodes):,} nodes ({', '.join(f'{k} {c:,}' for k, c in kinds.most_common())}), "
            f"{len(self.edges):,} edges ({', '.join(f'{r} {c:,}' for r, c in relations.most_common()) or 'none'})"
        ]
        degree = Counter(e.target for e in self.edges)
        for kind in dict.fromkeys(self.nodes[e.target].kind for e in self.edges):
            best = [(n, c) for n, c in degree.most_common() if self.nodes[n].kind == kind][:top]
            shown = ", ".join(f"{self.nodes[n].name} ({c})" for n, c in best)
            lines.append(f"  most linked {_plural(kind)}: {shown}")
        if self.review:
            lines.append(
                f"  {len(self.review):,} pair(s) of names that may be one thing, not merged (see review; a lower"
                " merge threshold merges them)"
            )
        return "\n".join(lines)

    # -- writing ----------------------------------------------------------------------------------- #
    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges],
            "review": [m.to_dict() for m in self.review],
        }

    def save(self, path: str | Path) -> Path:
        """Write the graph: ``.json`` (nodes, edges and review), ``.graphml``, or a directory (``nodes.csv``
        and ``edges.csv``, with the headers Neo4j's import reads)."""
        target = Path(path)
        suffix = target.suffix.lower()
        if suffix == ".json":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(self.to_dict(), indent=1, ensure_ascii=False, default=str) + "\n",
                              encoding="utf-8")  # fmt: skip
        elif suffix == ".graphml":
            target.parent.mkdir(parents=True, exist_ok=True)
            ElementTree.ElementTree(self._graphml()).write(target, encoding="utf-8", xml_declaration=True)
        elif not suffix:
            self._csv(target)
        else:
            raise ValueError(f"write a graph as .json, .graphml, or a directory of CSV files, not {target.name}")
        return target

    def _graphml(self) -> ElementTree.Element:
        ns = "http://graphml.graphdrawing.org/xmlns"
        root = ElementTree.Element("graphml", xmlns=ns)
        keys = [("kind", "node"), ("name", "node"), ("confidence", "node"), ("sources", "node"),
                ("relation", "edge"), ("count", "edge"), ("confidence", "edge"), ("sources", "edge")]  # fmt: skip
        types = {"confidence": "double", "count": "int"}
        for name, scope in keys:
            ElementTree.SubElement(root, "key", {"id": f"{scope}_{name}", "for": scope, "attr.name": name,
                                                 "attr.type": types.get(name, "string")})  # fmt: skip
        graph = ElementTree.SubElement(root, "graph", id="wintergrab", edgedefault="directed")

        def data(parent: ElementTree.Element, key: str, value: Any) -> None:
            if value is not None:
                ElementTree.SubElement(parent, "data", key=key).text = str(value)

        for node in self.nodes.values():
            el = ElementTree.SubElement(graph, "node", id=node.id)
            data(el, "node_kind", node.kind)
            data(el, "node_name", node.name)
            data(el, "node_confidence", round(node.confidence, 4))
            data(el, "node_sources", " ".join(node.sources))
        for i, edge in enumerate(self.edges):
            el = ElementTree.SubElement(graph, "edge", id=f"e{i}", source=edge.source, target=edge.target)
            data(el, "edge_relation", edge.relation)
            data(el, "edge_count", edge.count)
            data(el, "edge_confidence", None if edge.confidence is None else round(edge.confidence, 4))
            data(el, "edge_sources", " ".join(edge.sources))
        return root

    def _csv(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "nodes.csv").open("w", encoding="utf-8", newline="") as handle:
            out = csv.writer(handle)
            out.writerow(["id:ID", "name", ":LABEL", "aliases", "confidence:float", "count:int", "sources"])
            for n in self.nodes.values():
                out.writerow([n.id, n.name, _label(n.kind), "|".join(n.aliases), round(n.confidence, 4), n.count,
                              "|".join(n.sources)])  # fmt: skip
        with (directory / "edges.csv").open("w", encoding="utf-8", newline="") as handle:
            out = csv.writer(handle)
            out.writerow([":START_ID", ":END_ID", ":TYPE", "count:int", "confidence:float", "sources"])
            for e in self.edges:
                confidence = "" if e.confidence is None else round(e.confidence, 4)
                out.writerow([e.source, e.target, e.relation.upper(), e.count, confidence, "|".join(e.sources)])

    def __repr__(self) -> str:
        return f"KnowledgeGraph({len(self.nodes)} nodes, {len(self.edges)} edges)"


def _node(entity: Entity) -> Node:
    return Node(entity.id, entity.kind, entity.name, aliases=entity.aliases, attributes=entity.attributes,
                sources=entity.sources, confidence=entity.confidence, count=len(entity.mentions))  # fmt: skip


def _values(value: Any) -> list[Any]:
    if value in (None, "", []):
        return []
    if isinstance(value, (list, tuple)):
        return [v for v in value if v not in (None, "", []) and not isinstance(v, (dict, list))]
    if isinstance(value, dict):
        name = value.get("name")  # {"@type": "Brand", "name": "Acme"}
        return [name] if name not in (None, "") else []
    return [value]


def _confidence(record: Mapping[str, Any], path: str) -> float | None:
    """How sure the extraction was of the field at ``path``: its ``_provenance`` confidence, else the record's."""
    provenance = record.get("_provenance")
    if isinstance(provenance, Mapping):
        found = (provenance.get("fields") or {}).get(path.split(".")[0])
        if isinstance(found, Mapping) and isinstance(found.get("confidence"), (int, float)):
            return float(found["confidence"])
    overall = record.get("_confidence")
    return float(overall) if isinstance(overall, (int, float)) and not isinstance(overall, bool) else None


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")
    return slug[:80] or "x"


def _unique(base: str, taken: set[str]) -> str:
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}~{n}", n + 1
    taken.add(candidate)
    return candidate


def _plural(kind: str) -> str:
    if kind == "person":
        return "people"
    if kind.endswith("y") and kind[-2:-1] not in "aeiou":
        return kind[:-1] + "ies"
    return kind + ("es" if kind.endswith(("s", "x", "ch", "sh")) else "s")


def _label(kind: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", kind) if part) or "Thing"
