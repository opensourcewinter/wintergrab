"""The knowledge graph: records, the things they name, resolved, and typed edges that keep their sources."""

from __future__ import annotations

import csv
import json
from xml.etree import ElementTree

import pytest

from wintergrab.cli import main
from wintergrab.data.graph import KnowledgeGraph, Relation

PRODUCTS = [
    {
        "name": "Lamp 1",
        "brand": "Acme Inc.",
        "category": "Lighting",
        "url": "https://a.example/p/1",
        "_confidence": 0.9,
    },
    {
        "name": "Lamp 2",
        "brand": "ACME INC",
        "category": "lighting",
        "url": "https://a.example/p/2",
        "_provenance": {"fields": {"brand": {"confidence": 0.97}}},
    },
    {
        "name": "Chair",
        "brand": {"@type": "Brand", "name": "Globex"},
        "category": "Furniture",
        "url": "https://a.example/p/3",
    },
    {"name": "Lamp 1", "brand": "Acme", "gtin": "0012345678905", "url": "https://b.example/lamp-1"},
]
JOBS = [
    {"title": "Engineer", "company": "Acme Inc.", "location": "Berlin", "url": "https://jobs.example/1"},
    {"title": "Designer", "company": "Initech", "location": "Munich", "url": "https://jobs.example/2"},
]
COMPANIES = [
    {"name": "Acme, Inc.", "website": "https://acme.example", "city": "Springfield", "url": "https://dir.example/acme"}
]


def _graph() -> KnowledgeGraph:
    graph = KnowledgeGraph()
    assert graph.add_records(PRODUCTS, "product") == 4
    graph.add_records(JOBS, "job")
    graph.add_records(COMPANIES, "company")
    return graph.build()


def test_names_are_resolved_into_nodes() -> None:
    graph = _graph()
    acme = graph.node("brand:acme")
    assert acme.kind == "brand" and acme.aliases == ["Acme Inc.", "ACME INC", "Acme"] and acme.count == 3
    assert graph.find("acme inc", "brand") == [acme]
    # a job's company and a company's own record are one node; a brand is not a company
    [company] = graph.find("Acme Inc.", "company")
    assert company.aliases == ["Acme Inc.", "Acme, Inc."] and "https://dir.example/acme" in company.sources
    assert company.attributes["website"] == ["https://acme.example"]
    # the same product on two sites is one node; categories are matched by name
    [lamp] = graph.find("Lamp 1", "product")
    assert lamp.count == 2 and set(lamp.sources) == {"https://a.example/p/1", "https://b.example/lamp-1"}
    assert lamp.attributes["gtin"] == ["0012345678905"] and lamp.confidence > 0.9
    assert graph.node("category:lighting").aliases == ["Lighting", "lighting"]
    jobs = [n for n in graph.nodes.values() if n.kind == "job"]
    assert [n.id for n in jobs] == ["job:https-jobs-example-1", "job:https-jobs-example-2"]  # one node per page


def test_edges_keep_their_sources_and_confidence() -> None:
    graph = _graph()
    edges = {(e.source, e.relation, e.target): e for e in graph.edges}
    made = edges[("product:lamp-1", "manufactured_by", "brand:acme")]
    assert made.count == 2 and made.sources == ["https://a.example/p/1", "https://b.example/lamp-1"]
    assert made.confidence == 0.9  # the record's _confidence
    assert edges[("product:lamp-2", "manufactured_by", "brand:acme")].confidence == 0.97  # the field's own
    assert edges[("product:chair", "manufactured_by", "brand:globex")].confidence is None  # nothing said
    assert ("job:https-jobs-example-1", "offered_by", "company:acme") in edges
    assert ("company:acme", "located_in", "location:springfield") in edges
    made_by_acme = sorted(n.name for _, n in graph.neighbors("brand:acme", "manufactured_by", direction="in"))
    assert made_by_acme == ["Lamp 1", "Lamp 2"]
    assert [n.id for _, n in graph.neighbors("product:chair")] == ["brand:globex", "category:furniture"]
    summary = graph.describe()
    assert summary.startswith("14 nodes (") and "most linked brands: Acme (2), Globex (1)" in summary
    assert "most linked categories: Lighting (2)" in summary


def test_relations_of_ones_own() -> None:
    records = [{"name": "Kettle", "seller": "Initech", "tags": ["kitchen", "steel"], "url": "https://s.example/k"}]
    relations = [Relation.parse("seller=sold_by:company"), Relation("tags", "tagged", "tag")]
    graph = KnowledgeGraph.from_records(records, "product", relations=relations)
    assert {(e.relation, e.target) for e in graph.edges} == {
        ("sold_by", "company:initech"),
        ("tagged", "tag:kitchen"),
        ("tagged", "tag:steel"),
    }
    with pytest.raises(ValueError, match="FIELD=RELATION:KIND"):
        Relation.parse("seller")


def test_saving(tmp_path) -> None:
    graph = _graph()
    data = json.loads(graph.save(tmp_path / "g.json").read_text(encoding="utf-8"))
    assert len(data["nodes"]) == 14 and len(data["edges"]) == len(graph.edges) and "review" in data
    ns = {"g": "http://graphml.graphdrawing.org/xmlns"}
    root = ElementTree.parse(graph.save(tmp_path / "g.graphml")).getroot()
    assert len(root.findall(".//g:node", ns)) == 14 and len(root.findall(".//g:edge", ns)) == len(graph.edges)
    graph.save(tmp_path / "neo4j")
    with (tmp_path / "neo4j" / "nodes.csv").open(encoding="utf-8", newline="") as handle:
        nodes = list(csv.DictReader(handle))
    assert {row[":LABEL"] for row in nodes} >= {"Product", "Brand", "Company", "Category", "Location", "Job"}
    with (tmp_path / "neo4j" / "edges.csv").open(encoding="utf-8", newline="") as handle:
        edges = list(csv.DictReader(handle))
    assert {"MANUFACTURED_BY", "OFFERED_BY", "LOCATED_IN", "BELONGS_TO"} <= {row[":TYPE"] for row in edges}
    with pytest.raises(ValueError, match=r"json, \.graphml, or a directory"):
        graph.save(tmp_path / "g.xlsx")


def test_the_graph_command(site, tmp_path, capsys) -> None:
    jobs, companies = tmp_path / "jobs.jsonl", tmp_path / "companies.jsonl"
    jobs.write_text("\n".join(json.dumps(r) for r in JOBS), encoding="utf-8")
    companies.write_text("\n".join(json.dumps(r) for r in COMPANIES), encoding="utf-8")
    out = tmp_path / "graph.json"
    assert main(["data", "graph", f"job={jobs}", f"company={companies}", "-o", str(out)]) == 0
    printed = capsys.readouterr().err
    assert "2 job record(s)" in printed and "most linked companies: Acme Inc. (1), Initech (1)" in printed
    assert {n["id"] for n in json.loads(out.read_text(encoding="utf-8"))["nodes"]} >= {"company:acme"}
    assert main(["data", "graph", str(jobs)]) == 2 and "--kind KIND" in capsys.readouterr().err
    # records a crawl extracted: their kind is in their provenance, and their fields' confidence too
    books = tmp_path / "books.jsonl"
    assert main(["-q", "crawl", site.url + "/books/", "--extract", "product", "--provenance", "--follow",
                 "article.product_pod h3", "--max-pages", "6", "-o", str(books)]) == 0  # fmt: skip
    assert main(["data", "graph", str(books), "-o", str(tmp_path / "books.graph.json")]) == 0
    graph = json.loads((tmp_path / "books.graph.json").read_text(encoding="utf-8"))
    categories = {n["name"] for n in graph["nodes"] if n["kind"] == "category"}
    assert categories <= {"Poetry", "Travel"} and categories
    assert all(e["relation"] == "belongs_to" and e["confidence"] for e in graph["edges"])
