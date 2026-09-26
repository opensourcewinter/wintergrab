# Entity resolution

Scraped data names the same thing many ways: "Apple Inc.", "APPLE INC",
"Apple". `EntityResolver` finds which names are the same company, brand,
product, person or place. It merges them only where the evidence supports
it, and lists the uncertain cases for you to review instead of guessing.

```python
from wintergrab.data import EntityResolver

resolver = EntityResolver("company")
for name in ("Apple Inc.", "APPLE INC", "Apple", "Apple Computer, Inc.", "Apple Computer", "Apple Records"):
    resolver.add(name, source="https://directory.example/...")
result = resolver.resolve()

result.summary()
# '6 mentions -> 3 entities (2 with several mentions); 1 pairs to review'
result.entities
# [Entity('company:apple', 'Apple Inc.', mentions=3, confidence=0.982),
#  Entity('company:apple-computer', 'Apple Computer, Inc.', mentions=2, confidence=0.982),
#  Entity('company:apple-records', 'Apple Records', mentions=1, confidence=1.0)]
result.review
# [Match('Apple Inc.' ~ 'Apple Computer, Inc.', score=0.818, 'review', ["+3.5 same name apart from 'computer'"])]
```

"Apple" and "Apple Computer" may be one company or two; with nothing else
to go on, they are not merged. Add evidence and they are:

```python
resolver = EntityResolver("company")
resolver.add("Apple Inc.", source="https://a.example/1", website="https://www.apple.com")
resolver.add("Apple Computer, Inc.", source="https://b.example/2", website="apple.com")
resolver.add("Apple", source="https://c.example/3")
print(resolver.resolve().entities[0].explain())
```

```
company:apple: 'Apple Inc.', 3 mentions, confidence 0.98
     1 x Apple Inc.
     1 x Apple Computer, Inc.
     1 x Apple
  'Apple Inc.' ~ 'Apple Computer, Inc.': 0.996 (+3.5 same name apart from 'computer'; +4.0 same website: apple.com)
  'Apple Inc.' ~ 'Apple': 0.982 (+6.0 same name apart from the legal form)
```

## Kinds

`EntityResolver(kind)` reads names as one of these kinds (`normalize_name(text,
kind)` shows the result):

| Kind | Understands |
|---|---|
| `company` | Legal forms set aside (`Inc.`, `Corp.`, `GmbH`, `S.A.`, `Pty Ltd`...), accents, punctuation, `&`/`and`, `The`, spelled-out initials (`P&G`, `I.B.M.`), generic words that come and go (`Group`, `Holdings`, `International`, `Computer`...), initialisms (`IBM` = International Business Machines). A company is a legal entity: `Acme GmbH` and `Acme Inc.` are different companies (`Inc.`/`Corp.`/`Co.` are one kind of form, `Ltd`/`Limited`/`PLC` another) |
| `organization`, `brand` | The same, but spanning legal entities: `Acme GmbH` and `Acme Inc.` can be one organization. The canonical name is the plain one ("Nike", not "Nike, Inc.") |
| `person` | Titles (`Dr.`, `Prof.`), `"Smith, John"` order, initials (`J. Smith` fits `John Smith`), degrees, and generations (`Jr.` and `Sr.` are never the same person) |
| `product` | Units (`128 GB` = `128GB`, `55"` = `55in`), model numbers (`WH-1000XM4` = `WH1000XM4`), word order, the brand in the name. Numbers must match: `iPhone 14` is not `iPhone 15` |
| `location` | `St.`/`Mt.`/`Ft.`, `City of`, and the area after a comma (`Springfield, IL`): `IL` can be Illinois or Israel, `Illinois` is Illinois |

## Evidence

Every pair of mentions that might match gets a score from the evidence, as
log-odds: a prior against a match (-2), plus a weight for each signal,
turned into a probability. Positive weights are for a shared value,
negative ones for two different values:

| Signal | Weight |
|---|---:|
| the same name (organizations) | +6 |
| the same name apart from spacing | +5 |
| the same name apart from generic words ("Apple" / "Apple Computer") | +3.5 |
| the initials of the other name, or the same words in another order | +3 |
| similar names (one typo in a long word) | +2 to +5.5 |
| the same name (people) | +3; +1.5 for compatible initials |
| the same name (products) | +3.5 to +6, more for longer names |
| the same name (places) | +4 |
| different legal forms ("Inc." / "GmbH") | -1.5 |
| different numbers in the name ("Studio 54" / "Studio 55") | -4; -6 for product model numbers |
| the same website or company e-mail domain / different ones | +4 / -3 |
| the same phone number | +4 |
| the same LEI, VAT number, DUNS, EIN, company number, ORCID, Wikidata id / different ones | +8 / -8 |
| the same GTIN (EAN, UPC, ISBN) / different ones | +8 / -8 |
| the same MPN / different ones | +4 / -3 |
| a product's brand, the same / different | +1 / -5 |
| a person's e-mail address or profile URL | +6 |
| a person's employer | +2 |
| a place's country, the same / different | +0.5 / -6 |
| a place's region, the same / different | +1.5 / -4 |
| coordinates less than 1 km apart / more than 50 km apart (places) | +4 / -6 |

Attributes are given by name when adding a mention: `website` (or `url`,
`domain`), `email`, `phone`, `country`, `region` (or `state`), `city`,
`postal_code`, `coordinates` (or `latitude` and `longitude`), `gtin` (or
`ean`, `upc`, `isbn`), `mpn`, `brand`, `employer`, `lei`, `vat`, `duns`,
`ein`, `tax_id`, `company_number`, `orcid`, `wikidata`. Values are
normalized before they are compared (phone numbers to E.164, websites to
their registrable domain, countries to ISO codes...). Other attributes are
kept for the merged records but not compared.

The weights are chosen by hand to make the decisions below come out right;
they are not trained probabilities.

## Decisions

- A pair scoring at least `merge_threshold` (0.95) is merged. Strongest
  pairs merge first.
- A pair scoring at least `review_threshold` (0.5) but less is listed in
  `result.review`, with its evidence.
- A merge is refused, and listed for review with a `note`, when:
  - the two groups have different identifiers that an entity can only have
    once (two LEIs, two GTINs, a person's generation, a place's country or
    region, a company's kind of legal form);
  - it would join two mentions that compared as different entities (below
    the review threshold). "Delta Stone" cannot join "Delta Stone Inc."
    (deltastone.com, US) and "Delta Stone GmbH" (delta-stein.de, DE) into one;
  - a mention matches two such groups equally well: "Acme" next to "Acme Corp"
    and "ACME Corporation" with different LEIs is ambiguous, so it merges with
    neither.
- Identical mentions are compared once. If they do not merge with each other
  (two "John Smith" with nothing else in common), each stays its own entity
  and one review item stands for the group.

An entity's `confidence` is the score of the weakest merge that formed it.

Tune the thresholds to your tolerance: `merge_threshold=0.8` merges on a
matching name alone ("Apple" and "Apple Computer"); `0.99` merges only with
strong identifiers.

## Results and provenance

```python
entity = result.entities[0]
entity.id, entity.name, entity.confidence     # 'company:apple', 'Apple Inc.', 0.982
entity.aliases                                # every spelling, most common first
entity.sources                                # where it was seen
entity.attributes                             # every value of every attribute, most common first
entity.mentions                               # Mention(name, kind, source, attributes, id)
entity.links                                  # the matches that merged them, with their evidence
entity.to_dict()                              # all of the above

result.records()          # one merged record per entity: id, name, the most common attribute values,
                          # aliases, confidence, count, sources
result.entity_of(mention) # the entity of a mention (or mention id)
result.find("APPLE INC")  # entities mentioned with exactly this name
resolver.compare("Apple Inc.", "Apple GmbH")
# Match('Apple Inc.' ~ 'Apple GmbH', score=0.924, 'review',
#       ['+6.0 same name apart from the legal form', '-1.5 different legal forms: inc / gmbh'])
```

From records:

```python
mentions = resolver.add_records(
    records, "brand.name", source_field="url", attributes={"website": "brand_url", "country": "country"}
)
result = resolver.resolve()
for record, mention in zip(records, mentions):
    if mention is not None:
        record["brand_id"] = result.entity_of(mention).id
```

Empty names and placeholders ("N/A", "-") are skipped (`None` in the list).

## Command line

```bash
wintergrab data entities companies.jsonl --field name --attribute website --attribute phone \
    -o entities.jsonl --review-output review.jsonl
wintergrab data entities products.jsonl --field brand --kind brand --annotate products.resolved.jsonl
```

`--attribute ATTR=FIELD` maps a record field to an attribute (`--attribute
website=company_url`); `--source` names the field saying where a record came
from (default `url`). `-o` writes one merged record per entity,
`--review-output` the pairs to review with their evidence, and `--annotate`
the input records with `FIELD_entity` (the entity id) and `FIELD_canonical`
(its name) added. `--merge` and `--review` set the thresholds.

## Scale

Mentions are only compared when they share a significant word, a pair of
words, their name without generic words, their initials or an identifier.
Words shared by more than `max_block` (300) distinct mentions are too common
to suggest a match on their own; pairs of words still are. On one core,
resolving 20,000 generated company names (15,558 distinct, about 300,000
comparisons) takes about 5 seconds: 3,769 mentions per second
(`benchmarks/bench_data.py`).

## Limits

Resolution uses the names and the attributes you give it, and no outside
knowledge: "Meta Platforms" and "Facebook" (a renamed company), or "NYC" and
"New York", are not matched without shared evidence such as a website or a
Wikidata id. Transliterations (Cyrillic, Chinese...) are not matched to Latin
spellings.
