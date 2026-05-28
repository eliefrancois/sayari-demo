"""Tool layer — the agent's capabilities.

This file is the API surface Claude sees. Three things live here:

1. Tool IMPLEMENTATIONS — thin Python functions that compose graph.py and
   sanctions.py into agent-friendly shapes. They handle small adapters
   (e.g. ICIJ label -> OpenSanctions schema) so the data layer stays pure.

2. Tool DESCRIPTORS (the `TOOLS` constant) — the JSON shapes passed to the
   Anthropic API. Each has a name, description, and JSON-schema input. The
   description is the single most important field in this file: it's what
   Claude reads to decide WHICH tool to call. Write them carefully.

3. A DISPATCHER (`execute_tool`) — the agent loop calls this with whatever
   tool_use block Claude returned. Returns a JSON-safe dict for tool_result.

When Sayari's interviewer asks "how do you tell the agent what to do?"
the answer is "by writing the descriptions in this file." Not the system
prompt, not magic — descriptions.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app import graph, sanctions
from app.schema import Neighborhood, SanctionsHit, SearchResults

log = logging.getLogger("erre.tools")


# --- Implementations -------------------------------------------------------


def _label_to_sanctions_schema(label: str) -> str:
    """ICIJ node label -> OpenSanctions schema name.

    Officers/Intermediaries are people-ish (sometimes companies, but the
    matcher tolerates Person being passed for a company). Entity is always
    a company in the ICIJ schema.
    """
    return "Organization" if label == "Entity" else "Person"


def search_entity_tool(name: str, limit: int = 10) -> dict[str, Any]:
    res: SearchResults = graph.search_entity(name, limit=limit)
    # Echo the original query in the metadata so the agent can compare
    # the user's intent to the actual matches when deciding found vs not-found.
    return {
        "query": name,
        "nodes": [n.model_dump() for n in res.nodes],
        "metadata": res.metadata,
    }


def get_relationships_tool(node_id: str, limit: int = 50) -> dict[str, Any]:
    nb: Neighborhood = graph.get_relationships(node_id, limit=limit)
    return {
        "nodes": [n.model_dump() for n in nb.nodes],
        "edges": [e.model_dump() for e in nb.edges],
        "metadata": nb.metadata,
    }


def get_officers_tool(entity_id: str, limit: int = 50) -> dict[str, Any]:
    nb: Neighborhood = graph.get_officers(entity_id, limit=limit)
    return {
        "nodes": [n.model_dump() for n in nb.nodes],
        "edges": [e.model_dump() for e in nb.edges],
        "metadata": nb.metadata,
    }


def find_address_connections_tool(node_id: str, limit: int = 20) -> dict[str, Any]:
    nb: Neighborhood = graph.find_address_connections(node_id, limit=limit)
    return {
        "nodes": [n.model_dump() for n in nb.nodes],
        "edges": [e.model_dump() for e in nb.edges],
        "metadata": nb.metadata,
    }


def find_er_links_tool(node_id: str, limit: int = 20) -> dict[str, Any]:
    nb: Neighborhood = graph.find_er_links(node_id, limit=limit)
    return {
        "nodes": [n.model_dump() for n in nb.nodes],
        "edges": [e.model_dump() for e in nb.edges],
        "metadata": nb.metadata,
    }


async def check_sanctions_tool(name: str, schema: str = "Person") -> dict[str, Any]:
    hits: list[SanctionsHit] = await sanctions.check_sanctions(name, schema=schema)
    return {
        "name_searched": name,
        "hits": [h.model_dump() for h in hits],
        "count": len(hits),
        "any_strong_match": any(sanctions.is_strong_match(h) for h in hits),
    }


# --- Tool descriptors (the API Claude sees) -------------------------------
# Description-writing principles applied below:
#   - LEAD with what the tool does in plain English.
#   - State WHEN to use it (positive guidance) and when NOT (negative guidance).
#   - Mention return shape gotchas (capped results, empty-list semantics).
#   - End with a hard rule the model should respect ("do not invent...").
# These descriptions are the agent's only documentation. Treat them like API docs.

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_entity",
        "description": (
            "Full-text fuzzy search of the ICIJ Offshore Leaks database for a person, "
            "company, or intermediary by name. ALWAYS call this first — every investigation "
            "starts here. Returns up to 10 matches with their node id, type "
            "(Entity/Officer/Intermediary/Address), source leak (e.g. 'Panama Papers'), and "
            "a Lucene relevance score. Score >=8 indicates a strong/exact name match; 4-8 "
            "is a partial match; <4 typically means only common words matched and the result "
            "is likely unrelated to the user's query. If the top score is low AND no result's "
            "name closely matches what the user asked about, treat this as NOT FOUND — set "
            "found=false in your final summary and do NOT invent connections. Never call any "
            "other tool before this one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The person or company name to search for, as the user provided it.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default 10, max 25).",
                    "default": 10,
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_relationships",
        "description": (
            "Return the immediate (1-hop) neighborhood of a node: every node directly "
            "connected to it and the relationship types between them. Pass the `node_id` "
            "returned from search_entity. Returns nodes + edges. Capped at 50 results — "
            "popular nodes (like Mossack Fonseca with 4,364 connections) are sampled and "
            "the metadata will tell you when results were capped. Use this to understand "
            "what an entity or officer is connected to. Always call after search_entity "
            "to map the subject's network before drawing conclusions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "node_id": {
                    "type": "string",
                    "description": "elementId of a node returned by search_entity.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max neighbors to return (default 50).",
                    "default": 50,
                },
            },
            "required": ["node_id"],
        },
    },
    {
        "name": "get_officers",
        "description": (
            "Given an Entity node id, return all officers (directors, shareholders, "
            "beneficiaries) of that entity. Use this AFTER finding an Entity via search_entity "
            "or get_relationships to understand who controls it. Each returned officer is a "
            "candidate for check_sanctions. Empty result means the entity has no officers in "
            "the ICIJ data, which is itself informative (suggests a shell or nominee structure)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "elementId of an Entity node (label must be 'Entity').",
                },
            },
            "required": ["entity_id"],
        },
    },
    {
        "name": "find_address_connections",
        "description": (
            "Given any node id, find other entities/officers/intermediaries that share a "
            "registered address with this node. Shared addresses are a STRUCTURAL signal of "
            "shell-company patterns or cross-leak identity — when 10+ entities register at "
            "the same address, that's a hallmark of corporate-services-firm-as-front. "
            "When 2-5 entities share an address with a known person, those are likely their "
            "controlled entities even if the formal ownership isn't disclosed. Use this on "
            "your subject after get_relationships if you want to surface hidden connections. "
            "Returns the shared-address Address nodes too so the connections are explicit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["node_id"],
        },
    },
    {
        "name": "find_er_links",
        "description": (
            "Given any node id, return EXPLICIT entity-resolution links to other nodes in "
            "DIFFERENT leaks. These are relationships ICIJ themselves curated — "
            "probably_same_officer_as, same_id_as, same_as, same_company_as (high confidence) "
            "and same_name_as (medium confidence). Empty result is common (only ~0.01% of "
            "nodes have these edges) and means 'ICIJ has not flagged this node as cross-"
            "referenced' — informative, not an error. Use this AFTER finding a subject to "
            "test for cross-leak presence. A non-empty result is strong evidence the same "
            "real-world actor appears in multiple leaks and should fire the cross_leak_presence "
            "risk signal."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["node_id"],
        },
    },
    {
        "name": "check_sanctions",
        "description": (
            "Check if a name appears on any global sanctions list (OFAC SDN, EU FSF, UK HMT, "
            "Swiss SECO, Canadian SEMA, UN, PEP lists, etc.) via OpenSanctions. Pass schema='Person' "
            "for individuals (officers, intermediaries) and schema='Organization' for entities/companies. "
            "Returns matches with `score` (0-1, with 0.70+ being a confident match), `lists` showing "
            "which watchlists, and `any_strong_match` boolean for convenience. Empty hits means clean "
            "(no watchlist hit). Call this on the SUBJECT first, then on each Officer surfaced by "
            "get_officers — connected-to-sanctioned is often more revealing than direct hits, since "
            "the latter are usually already known publicly. PEP / wikidata matches (lists not "
            "starting with country codes or 'us_/eu_/un_') are NOT actual sanctions; they're context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Subject's full name as known."},
                "schema": {
                    "type": "string",
                    "enum": ["Person", "Organization"],
                    "description": "OpenSanctions entity schema. Default 'Person'.",
                    "default": "Person",
                },
            },
            "required": ["name"],
        },
    },
]


# --- Dispatcher ------------------------------------------------------------

_SYNC = {
    "search_entity": search_entity_tool,
    "get_relationships": get_relationships_tool,
    "get_officers": get_officers_tool,
    "find_address_connections": find_address_connections_tool,
    "find_er_links": find_er_links_tool,
}


async def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    """Run a tool by name. Returns a JSON string (what we'll send back to Claude
    as tool_result content). Wraps errors so a bad tool call doesn't crash the
    agent loop — Claude can read the error and decide what to do.
    """
    # 'args' is reserved on LogRecord (printf-style logging) — use tool_args.
    log.info("tool_call", extra={"tool": name, "tool_args": arguments})
    try:
        if name == "check_sanctions":
            result = await check_sanctions_tool(**arguments)
        elif name in _SYNC:
            result = _SYNC[name](**arguments)
        else:
            return json.dumps({"error": f"unknown tool: {name}"})
        return json.dumps(result, default=str)
    except TypeError as e:
        # Bad arguments — Claude can read this and retry with correct shape.
        log.warning("tool_bad_args", extra={"tool": name, "error": str(e)})
        return json.dumps({"error": f"bad arguments for {name}: {e}"})
    except Exception as e:
        log.exception("tool_failed", extra={"tool": name})
        return json.dumps({"error": f"{name} failed: {e}"})
