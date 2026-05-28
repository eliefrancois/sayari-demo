# Neo4j + Cypher — the parts we use

> Read this before reading `graph.py`. It explains every Cypher pattern that appears.

## What a graph database actually is

Postgres stores tables. Neo4j stores **nodes** (entities) and **relationships** (the lines
between them). Both nodes and relationships have:
- A **label** (`Officer`, `Entity`, or for relationships: `officer_of`, `registered_address`)
- **Properties** — key-value pairs (e.g. `name`, `sourceID`, `country_codes`)

The reason this matters for our use case: questions like *"who shares an address with this
officer across leaks"* are a single line of Cypher but would be a multi-table self-join
nightmare in SQL.

## The Cypher mental model

Cypher reads like ASCII-art of the graph you want to match.

```cypher
(o:Officer)-[:officer_of]->(e:Entity)
```

Reads as: *"an Officer node `o` connected by an outgoing `officer_of` relationship to an
Entity node `e`."* The arrow direction matters. The variable names (`o`, `e`) let you
reference matched nodes in the `RETURN` clause.

A complete query has three main clauses:

```cypher
MATCH (o:Officer)-[:officer_of]->(e:Entity)
WHERE e.name CONTAINS "Acme"
RETURN o.name, e.name, e.sourceID
LIMIT 10
```

`MATCH` finds patterns. `WHERE` filters them. `RETURN` projects. Same shape as SQL but
walking edges instead of joining tables.

## Parameter binding (always do this)

Never string-interpolate values into Cypher. Always use `$param`:

```cypher
MATCH (n) WHERE elementId(n) = $node_id RETURN n
```

Then in Python:
```python
session.run(query, node_id="4:abc:123")
```

Two reasons: (1) prevents injection, (2) the database caches query plans by query shape,
so parameterized queries reuse cached plans and run faster.

## Node IDs in Neo4j 5

Old Neo4j (4.x) used `id(n)` — an integer that could change if you reloaded the database.
Neo4j 5 introduces `elementId(n)` which returns a string like
`"4:8057c561-b705-4cc4-a798-1cc502702ef5:1152923"` that's stable across restarts.

We use `elementId()` everywhere. To round-trip — return it from one query, pass it back
into another — you `MATCH (n) WHERE elementId(n) = $id`.

## Indexes (why your full-text query is fast)

A naive search `MATCH (n) WHERE n.name CONTAINS "Roldugin"` would scan all ~2M nodes. Slow.

Neo4j supports a few index types:
- **Range index** — sorted lookups on a single property. Useful for `WHERE n.country_codes = 'US'`.
- **Full-text index** — Lucene-backed inverted index. Useful for fuzzy text search.

The ICIJ dump ships with a full-text index named `search` that covers Entity, Officer,
Intermediary, and Address nodes on both `name` and `address` properties. We use it via:

```cypher
CALL db.index.fulltext.queryNodes("search", "Sergey Roldugin") YIELD node, score
RETURN node, score
ORDER BY score DESC
LIMIT 10
```

That's a procedure call (`CALL`) into Neo4j's full-text engine. `YIELD` extracts the
output columns. `score` is Lucene's relevance score — higher = more relevant. We can
add `WHERE` after to filter (e.g. `WHERE "Officer" IN labels(node)`).

## The ICIJ schema we're working with

**Node labels:** `Entity`, `Officer`, `Intermediary`, `Address`, `Other`.

**Core relationship types (spec'd):**
- `(:Officer|Intermediary)-[:officer_of]->(:Entity)` — directorships, beneficiaries
- `(:Intermediary)-[:intermediary_of]->(:Entity)` — agent relationships
- `(*)-[:registered_address]->(:Address)` — where the entity/person is registered
- `(:Officer|Intermediary)-[:underlying]->(:Officer|Intermediary)` — nominee chains
- `(:Entity)-[:related_entity]->(:Entity)` — entity-to-entity links

**Entity-resolution proxy relationships (newer dump, we added a tool for these):**
- `[:probably_same_officer_as]` — explicit ER, very high confidence
- `[:same_id_as]`, `[:same_as]`, `[:same_company_as]` — also high confidence
- `[:same_name_as]` — medium (name match alone is noisy)
- `[:similar]`, `[:similar_company_as]` — fuzzy
- `[:connected_to]` — generic

**Properties to know about:**
- `sourceID` — which leak the node came from (`"Panama Papers"`, `"Paradise Papers - Appleby"`, etc.)
- `name` — the canonical name
- `jurisdiction` (Entity only) — country of registration
- `struck_off_date` (Entity only) — when deregistered, if applicable

## Patterns we'll write in `graph.py`

### 1. Full-text search (the entry point)
```cypher
CALL db.index.fulltext.queryNodes("search", $name) YIELD node, score
WHERE labels(node)[0] IN ["Entity", "Officer", "Intermediary"]
RETURN elementId(node) AS id, labels(node) AS labels, properties(node) AS props, score
ORDER BY score DESC LIMIT $limit
```

### 2. Neighborhood (1-hop, capped)
```cypher
MATCH (n) WHERE elementId(n) = $node_id
MATCH (n)-[r]-(m)
RETURN n, r, m, elementId(m) AS mid, type(r) AS rtype, labels(m) AS mlabels
LIMIT $limit
```

The `-[r]-` syntax (no arrow direction) matches relationships in either direction. Use
`-[r]->` or `<-[r]-` if direction matters.

### 3. Address-sharing pattern (structural ER proxy)
```cypher
MATCH (n) WHERE elementId(n) = $node_id
MATCH (n)-[:registered_address]->(a:Address)<-[:registered_address]-(other)
WHERE n <> other
RETURN other, a, labels(other) AS olabels
LIMIT $limit
```

The pattern `(n)->(a)<-(other)` is a two-hop traversal that lands on every node sharing
an Address with `n`. The arrow flips because both edges point INTO the Address.

### 4. Explicit ER relationships (new, leverages the richer dump)
```cypher
MATCH (n) WHERE elementId(n) = $node_id
MATCH (n)-[r:probably_same_officer_as|same_id_as|same_as|same_company_as|same_name_as|same_intermediary_as]-(other)
WHERE n <> other AND other.sourceID <> n.sourceID
RETURN other, type(r) AS rel_type, n.sourceID AS from_leak, other.sourceID AS to_leak
LIMIT $limit
```

The `r:type1|type2|type3` syntax matches any of the listed relationship types. The
`other.sourceID <> n.sourceID` filter ensures we only surface CROSS-leak matches, which
is the interesting signal.

## How we'll talk to Neo4j from Python

The `neo4j` driver is async-capable but we'll start with the sync interface for clarity.
Pattern:

```python
from neo4j import GraphDatabase

driver = GraphDatabase.driver(uri, auth=(user, password))

with driver.session() as session:
    result = session.run(query, **params)
    for record in result:
        ...
driver.close()
```

We'll wrap the driver in a singleton so we don't open a new connection per request.

## What you should be able to do after reading this

- Read a Cypher query and explain what graph pattern it matches.
- Spot the difference between `()`, `[]`, and `{}` in Cypher (nodes, relationships, properties).
- Know why `elementId()` over `id()` matters in Neo4j 5.
- Explain why we use parameter binding.
- Understand what `CALL db.index.fulltext.queryNodes` is doing under the hood (Lucene).

Then `graph.py` will read like prose.
