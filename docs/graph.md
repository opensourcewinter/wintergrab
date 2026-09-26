# Knowledge graphs

`wintergrab data graph` turns records into a graph. It holds the things
they name, each once, and how they relate, and every fact keeps the pages
that state it:

```bash
wintergrab data graph job=jobs.jsonl company=companies.jsonl -o graph.graphml
```

```
jobs.jsonl: 2 job record(s)
companies.jsonl: 1 company record(s)
7 nodes (location 3, company 2, job 2), 5 edges (located_in 3, offered_by 2)
  most linked companies: Acme Inc. (1), Initech (1)
  most linked locations: Berlin (1), Munich (1), Springfield (1)
wrote the graph to graph.graphml
```

(Two jobs, at Acme Inc. in Berlin and at Initech in Munich, and Acme's own
record, "Acme, Inc." in Springfield: the job's company and the record are
one node.)

## Nodes and edges

Each record is a node of its kind: a product, a job, an article. The fields
that name other things become nodes too, and typed edges link them. Each
extraction template has its relations:

| Records | Edges |
|---|---|
| product | `manufactured_by` brand, `belongs_to` category |
| article | `written_by` person, `belongs_to` category (section), `tagged` category (tags) |
| job | `offered_by` company, `located_in` location |
| event | `held_at` location (venue), `located_in` location (city), `organized_by` organization, `performed_by` person |
| company, place | `located_in` location (city, country), `belongs_to` category (industry, cuisine) |
| person | `works_for` organization, `located_in` location |
| review | `written_by` person, `about` product (item) |
| property | `located_in` location (city) |
| documentation | `belongs_to` category (section) |
| recipe | `written_by` person |

Add others with `--relation FIELD=RELATION:KIND` (`--relation
seller=sold_by:company`). `--only` keeps only yours. A field holding a list
gives an edge per item, and a field holding an object gives one to its
`name`.

## One node per thing

Names are resolved as in [entity resolution](entities.md):

- "Acme Inc.", "ACME INC" and "Acme" are one brand;
- "Apple" and "Apple Records" are not;
- what may be one thing but is not sure is listed for review, never merged
  silently. Two places that only share a name ("Springfield") are such a
  pair. A lower `--merge` threshold merges them.

Categories and other kinds that are not entities are matched by name,
ignoring case. Products, companies and people are resolved too, with their
identifiers as evidence (GTIN, SKU and brand; website, phone, email and
country; email and organization). So the same product found on two sites
is one node. Other records (jobs, articles, events...) are one node per
page.

Records from several files make one graph: the company named by a job and a
company's own record meet in one node. Give each file's kind as
`KIND=FILE`, or `--kind` for all of them. Records extracted with
`--provenance` say their kind themselves.

## Sources and confidence

- A **node** keeps every spelling seen, its attributes (a record's
  fields), the pages it was seen on, and how sure the resolution is (the
  weakest merge's score, 1.0 for one mention).
- An **edge** keeps the pages that state it, how many times it was stated,
  and how sure the extraction was of the field behind it. That is the
  field's confidence in the record's `_provenance`
  (`crawl --extract ... --provenance`), else the record's `_confidence`.

## Formats

| `-o` | What |
|---|---|
| `graph.json` | Nodes, edges and the pairs to review, with everything above |
| `graph.graphml` | GraphML, for Gephi, yEd, Cytoscape, NetworkX... |
| a directory | `nodes.csv` and `edges.csv` with the headers `neo4j-admin database import` reads (`id:ID`, `:LABEL`, `:START_ID`, `:END_ID`, `:TYPE`) |

## In code

```python
from wintergrab.data.graph import KnowledgeGraph, Relation
from wintergrab.data.io import read_records

graph = KnowledgeGraph()
graph.add_records(read_records("jobs.jsonl"), "job")
graph.add_records(read_records("companies.jsonl"), "company")
graph.build()

[acme] = graph.find("Acme Inc.", "company")
for edge, job in graph.neighbors(acme.id, "offered_by", direction="in"):
    print(job.name, edge.sources, edge.confidence)
graph.save("graph.graphml")
```

`KnowledgeGraph.from_records(records, "product")` does it in one go.
`graph.review` holds the pairs not merged, with their evidence.
