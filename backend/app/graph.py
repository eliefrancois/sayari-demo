"""Neo4j data layer.

This module owns ALL Cypher. Other modules call Python functions here and never
see a Cypher string. That separation means:
  - If we swap Neo4j for TigerGraph tomorrow, only this file changes.
  - If a query is slow, there's one place to look.
  - If a Cypher pattern is wrong, the agent's behavior is unaffected — it just
    gets back fewer/empty results.

The queries are Cypher 5.x syntax. See docs/01-neo4j-and-cypher.md for the
patterns and why each one is shaped the way it is.
"""

from __future__ import annotations

from typing import Any

from neo4j import Driver, GraphDatabase, Record

from app.config import get_settings
from app.schema import GraphEdge, GraphNode, Neighborhood, SearchResults

# --- Driver singleton ---------------------------------------------------------

_driver: Driver | None = None


def get_driver() -> Driver:
    """Return the process-wide Neo4j driver, creating it on first call.

    Why a singleton: each Driver maintains a connection pool. We want one pool
    per process, not one per request. Cloud Run handles concurrency by sharing
    a single process across requests, so this is exactly the right granularity.
    """
    global _driver
    if _driver is None:
        s = get_settings()
        _driver = GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password))
    return _driver


def close_driver() -> None:
    """Close the driver on app shutdown. Called from FastAPI's lifespan handler."""
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None


def ping() -> bool:
    """Cheap connectivity check for /health. Runs `RETURN 1`. ~1ms."""
    try:
        with get_driver().session() as s:
            return s.run("RETURN 1 AS ok").single()["ok"] == 1
    except Exception:
        return False


# --- Cypher constants ---------------------------------------------------------
# Keeping these at module level so they're grep-able, reviewable in one place,
# and easy to copy into Neo4j Browser when debugging.

# Full-text search across the four node types we care about. The "search" index
# is shipped pre-built with the ICIJ dump and covers name + address fields.
_SEARCH_ENTITY = """
CALL db.index.fulltext.queryNodes('search', $query) YIELD node, score
WHERE labels(node)[0] IN ['Entity', 'Officer', 'Intermediary', 'Address']
RETURN elementId(node) AS id,
       labels(node)    AS labels,
       properties(node) AS props,
       score
ORDER BY score DESC
LIMIT $limit
"""

# 1-hop neighborhood. The `-[r]-` (no arrow) matches both directions, which is
# what we want — we don't care if Officer→Entity or Entity←Officer, we want
# both sides surfaced.
_GET_NEIGHBORHOOD = """
MATCH (n) WHERE elementId(n) = $node_id
MATCH (n)-[r]-(m)
RETURN elementId(n)   AS source_id,
       labels(n)      AS source_labels,
       properties(n)  AS source_props,
       elementId(m)   AS target_id,
       labels(m)      AS target_labels,
       properties(m)  AS target_props,
       type(r)        AS rel_type,
       startNode(r) = n AS is_outgoing
LIMIT $limit
"""

# Officers of a specific Entity. Convenience over get_neighborhood for the
# common "who controls this company" question. Note the direction: officer_of
# points FROM Officer TO Entity, so we match incoming.
_GET_OFFICERS = """
MATCH (e:Entity) WHERE elementId(e) = $entity_id
MATCH (o)-[r:officer_of]->(e)
RETURN elementId(o)  AS officer_id,
       labels(o)     AS officer_labels,
       properties(o) AS officer_props,
       elementId(e)  AS entity_id,
       labels(e)     AS entity_labels,
       properties(e) AS entity_props
LIMIT $limit
"""

# Structural cross-leak proxy: any node sharing a registered_address with this one.
# The pattern (n)->(a)<-(other) is a two-hop walk through the Address node.
_FIND_ADDRESS_CONNECTIONS = """
MATCH (n) WHERE elementId(n) = $node_id
MATCH (n)-[:registered_address]->(a:Address)<-[:registered_address]-(other)
WHERE n <> other
RETURN elementId(n)     AS subject_id,
       labels(n)        AS subject_labels,
       properties(n)    AS subject_props,
       elementId(other) AS other_id,
       labels(other)    AS other_labels,
       properties(other) AS other_props,
       elementId(a)     AS address_id,
       a.address        AS address,
       a.sourceID       AS address_source
LIMIT $limit
"""

# Explicit ER relationships introduced in the newer ICIJ dump. We restrict to
# CROSS-leak matches (different sourceID) because that's the interesting signal
# — same-leak ER edges are usually data-cleaning artifacts.
_FIND_ER_LINKS = """
MATCH (n) WHERE elementId(n) = $node_id
MATCH (n)-[r:probably_same_officer_as|same_id_as|same_as|same_company_as|same_name_as|same_intermediary_as]-(other)
WHERE n <> other
  AND coalesce(other.sourceID, '') <> coalesce(n.sourceID, '')
RETURN elementId(n)     AS subject_id,
       labels(n)        AS subject_labels,
       properties(n)    AS subject_props,
       elementId(other) AS other_id,
       labels(other)    AS other_labels,
       properties(other) AS other_props,
       type(r)          AS rel_type,
       n.sourceID       AS from_leak,
       other.sourceID   AS to_leak
LIMIT $limit
"""


# --- Record → Pydantic helpers ------------------------------------------------

_VALID_LABELS = {"Entity", "Officer", "Intermediary", "Address", "Other"}


def _pick_label(labels: list[str]) -> str:
    """A node may have multiple labels in theory. Pick the first one we recognize.

    Falls back to 'Other' so we never emit a label outside our Literal type
    (which would cause Pydantic validation to fail in the agent layer).
    """
    for lbl in labels:
        if lbl in _VALID_LABELS:
            return lbl
    return "Other"


def _stringify_props(props: dict[str, Any]) -> dict[str, Any]:
    """Neo4j can return DateTime, Date, etc. — coerce non-JSON-safe values to str.

    Keeps the GraphNode.properties dict safe to ship over SSE as JSON.
    """
    out: dict[str, Any] = {}
    for k, v in props.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, list):
            out[k] = [str(x) if not isinstance(x, (str, int, float, bool)) else x for x in v]
        else:
            out[k] = str(v)
    return out


def _node_from_props(node_id: str, labels: list[str], props: dict[str, Any]) -> GraphNode:
    """Build a GraphNode from raw Cypher outputs."""
    safe_props = _stringify_props(props)
    return GraphNode(
        id=node_id,
        label=_pick_label(labels),  # type: ignore[arg-type]
        name=str(safe_props.get("name") or safe_props.get("address") or "(unnamed)"),
        source=safe_props.get("sourceID"),
        properties=safe_props,
    )


# --- Query functions ----------------------------------------------------------


def search_entity(query: str, limit: int = 10) -> SearchResults:
    """Full-text fuzzy search across Entity/Officer/Intermediary/Address.

    This is the agent's entry point — every investigation starts here. The
    'search' full-text index ships pre-built with the ICIJ dump and is the
    only reason this is fast on 2M nodes.

    Score interpretation (Lucene):
      - >=8  strong match (exact name, or unique surname)
      - 4-8  partial match (one rare token, others fuzzy)
      - <4   weak match (likely unrelated, just shares common tokens)

    We deliberately do NOT filter by score here. The agent layer reasons
    about whether the matches actually answer the user's query — that's
    the AI judgment call we want to keep in the LLM, not bury in the tool.
    """
    nodes: list[GraphNode] = []
    with get_driver().session() as s:
        # Pass params as a dict (not kwargs) — `query` is also the name of
        # the first positional arg of Session.run, which would collide.
        for rec in s.run(_SEARCH_ENTITY, {"query": query, "limit": limit}):
            nodes.append(_node_from_props(rec["id"], rec["labels"], rec["props"]))
    return SearchResults(nodes=nodes, metadata={"query": query, "returned": len(nodes)})


def get_relationships(node_id: str, limit: int = 50) -> Neighborhood:
    """Return the 1-hop neighborhood of a node as nodes + edges.

    Capped at `limit` rows because popular nodes (Mossack Fonseca has 4,364
    direct neighbors) would otherwise nuke the frontend's React Flow canvas.
    """
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    seen: set[str] = set()
    with get_driver().session() as s:
        records = list(s.run(_GET_NEIGHBORHOOD, {"node_id": node_id, "limit": limit}))
    # Seed the central node FIRST so every edge has a real endpoint to attach
    # to and so it lands as the subject (first node) on the canvas. Without it
    # the edges reference an id that isn't in the node set, the frontend's
    # d3-force layout drops them, and the neighbors float disconnected with no
    # center. Mirrors sayari.ownership_to_neighborhood, which seeds root_id.
    if records:
        rec0 = records[0]
        nodes.append(_node_from_props(rec0["source_id"], rec0["source_labels"], rec0["source_props"]))
        seen.add(rec0["source_id"])
    for rec in records:
        if rec["target_id"] not in seen:
            nodes.append(_node_from_props(rec["target_id"], rec["target_labels"], rec["target_props"]))
            seen.add(rec["target_id"])
        src, tgt = (rec["source_id"], rec["target_id"]) if rec["is_outgoing"] else (rec["target_id"], rec["source_id"])
        edges.append(GraphEdge(source=src, target=tgt, type=rec["rel_type"]))
    return Neighborhood(
        nodes=nodes,
        edges=edges,
        metadata={"node_id": node_id, "returned": len(nodes), "capped_at": limit},
    )


def get_officers(entity_id: str, limit: int = 50) -> Neighborhood:
    """Convenience: officers of a given Entity. Returns nodes + officer_of edges."""
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    seen: set[str] = set()
    with get_driver().session() as s:
        records = list(s.run(_GET_OFFICERS, {"entity_id": entity_id, "limit": limit}))
    # Seed the Entity itself so the officer_of edges connect to a real node
    # (otherwise they dangle and the officers float; see get_relationships).
    if records:
        rec0 = records[0]
        nodes.append(_node_from_props(rec0["entity_id"], rec0["entity_labels"], rec0["entity_props"]))
        seen.add(rec0["entity_id"])
    for rec in records:
        if rec["officer_id"] not in seen:
            nodes.append(_node_from_props(rec["officer_id"], rec["officer_labels"], rec["officer_props"]))
            seen.add(rec["officer_id"])
        edges.append(GraphEdge(source=rec["officer_id"], target=rec["entity_id"], type="officer_of"))
    return Neighborhood(
        nodes=nodes,
        edges=edges,
        metadata={"entity_id": entity_id, "officer_count": len(seen) - 1 if records else 0},
    )


def find_address_connections(node_id: str, limit: int = 20) -> Neighborhood:
    """Structural cross-leak proxy: nodes sharing a registered_address with this one.

    Output edges point from each `other` node to the shared Address, surfacing
    the Address itself as a connecting node so the React Flow viz makes sense.
    """
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    seen: set[str] = set()
    other_ids: set[str] = set()  # track distinct "other" nodes (i.e. cross-actor matches)
    with get_driver().session() as s:
        records = list(s.run(_FIND_ADDRESS_CONNECTIONS, {"node_id": node_id, "limit": limit}))
    # Seed the subject node so the registered_address edges from it connect to
    # a real node instead of dangling (see get_relationships).
    if records:
        rec0 = records[0]
        nodes.append(_node_from_props(rec0["subject_id"], rec0["subject_labels"], rec0["subject_props"]))
        seen.add(rec0["subject_id"])
    for rec in records:
        other_ids.add(rec["other_id"])
        if rec["other_id"] not in seen:
            nodes.append(_node_from_props(rec["other_id"], rec["other_labels"], rec["other_props"]))
            seen.add(rec["other_id"])
        if rec["address_id"] not in seen:
            nodes.append(_node_from_props(
                rec["address_id"], ["Address"],
                {"address": rec["address"], "sourceID": rec["address_source"]},
            ))
            seen.add(rec["address_id"])
        edges.append(GraphEdge(source=rec["other_id"], target=rec["address_id"], type="registered_address"))
        edges.append(GraphEdge(source=node_id, target=rec["address_id"], type="registered_address"))
    return Neighborhood(
        nodes=nodes,
        edges=edges,
        metadata={"node_id": node_id, "connections_via_address": len(other_ids)},
    )


def find_er_links(node_id: str, limit: int = 20) -> Neighborhood:
    """Explicit ER relationships from the newer ICIJ dump.

    Only returns CROSS-leak matches (different sourceID). The rel_type tells
    you the confidence band:
      - probably_same_officer_as, same_id_as, same_as, same_company_as → high
      - same_name_as → medium
      - same_intermediary_as → niche

    Empty result for many nodes — these edges exist for ~0.01% of the graph.
    That's expected; absence is informative ("ICIJ has not flagged this node
    as cross-referenced").
    """
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    seen: set[str] = set()
    cross_leak_matches = 0
    with get_driver().session() as s:
        records = list(s.run(_FIND_ER_LINKS, {"node_id": node_id, "limit": limit}))
    # Seed the subject node so the ER edges from it connect to a real node
    # instead of dangling (see get_relationships).
    if records:
        rec0 = records[0]
        nodes.append(_node_from_props(rec0["subject_id"], rec0["subject_labels"], rec0["subject_props"]))
        seen.add(rec0["subject_id"])
    for rec in records:
        if rec["other_id"] not in seen:
            nodes.append(_node_from_props(rec["other_id"], rec["other_labels"], rec["other_props"]))
            seen.add(rec["other_id"])
            cross_leak_matches += 1
        edges.append(GraphEdge(source=node_id, target=rec["other_id"], type=rec["rel_type"]))
    return Neighborhood(
        nodes=nodes,
        edges=edges,
        metadata={
            "node_id": node_id,
            "cross_leak_matches": cross_leak_matches,
            "leak_pairs": [
                (rec["from_leak"], rec["to_leak"], rec["rel_type"]) for rec in records
            ],
        },
    )
