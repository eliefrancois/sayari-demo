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

import asyncio
import json
import logging
from typing import Any

from app import conversations, graph, sanctions, sayari
from app.agent_common import (
    relabel_identifiers,
    slim_sayari_profile,
    slim_sayari_record,
)
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


# --- Sayari tool implementations ------------------------------------------
# The Sayari SDK is synchronous; we run its calls in a worker thread so a slow
# traversal (Gazprom downstream can take ~10s) doesn't block the async event
# loop driving the SSE stream.


async def sayari_resolve_tool(
    name: str,
    address: str | None = None,
    country: str | None = None,
    type: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    candidates = await asyncio.to_thread(
        sayari.resolve, name, address, country, type, limit
    )
    cand_dicts: list[dict[str, Any]] = []
    for c in candidates:
        cd = c.model_dump()
        # Neutralize the misleadingly-named `usa_ofac_sdn_number` identifier so
        # the model can't read SDN list membership off an OFAC record number.
        cd["identifiers"] = relabel_identifiers(cd.get("identifiers") or [])
        cand_dicts.append(cd)
    return {
        "query": name,
        "candidates": cand_dicts,
        "count": len(candidates),
        "note": (
            "Ranked candidates, NOT an answer. Pick by score + match_strength + "
            "address + identifiers; the top score is not always canonical."
        ),
    }


async def _known_entity_lookup(conversation_id: str | None) -> dict[str, dict[str, Any]]:
    """id -> {label, type, sanctioned, pep, countries} for entities already seen
    THIS conversation. Reads from the UNIFIED entity registry (Phase B): one
    id-keyed store that already folds prior search leads, resolved/traversed
    subjects, cached risk-path names, AND sanctions hits with the richer-source-
    wins merge policy applied. Lets a risk-path node be named from earlier turns
    even when the current profile's relationships block doesn't carry it. Pure
    state read — no external calls, no credits. Fails open to {} so a Redis
    hiccup never breaks an investigation."""
    if not conversation_id:
        return {}
    try:
        doc = await conversations.get_state_doc(conversation_id)
    except Exception:  # pragma: no cover - defensive; naming is best-effort
        return {}
    out: dict[str, dict[str, Any]] = {}
    for eid, rec in (doc.get("entities") or {}).items():
        if isinstance(rec, dict) and rec.get("label"):
            out[eid] = {
                "label": rec.get("label"),
                "type": rec.get("type"),
                "sanctioned": rec.get("sanctioned"),
                "pep": rec.get("pep"),
                "countries": rec.get("countries") or [],
            }
    return out


async def _resolve_and_map_risk_paths(
    slim: dict[str, Any],
    id_lookup: dict[str, dict[str, Any]],
    conversation_id: str | None,
) -> Neighborhood:
    """Build the risk-path overlay, but first RESOLVE the unnamed path nodes a
    hub entity's multi-hop paths leave anonymous.

    The 1-hop relationships block can't name multi-hop risk paths, so for a hub
    (Gazprom) most path nodes would render as "Unresolved entity" blobs. We spend
    a bounded batch of cheap entity_summary calls on the most central unknown ids
    and fold the names into `id_lookup` BEFORE mapping (existing in-hand entries
    win over freshly resolved ones). Newly-resolved names are persisted to the
    conversation state_doc (named_ids) so later turns reuse them for free."""
    unnamed = sayari.unnamed_risk_path_ids(slim, id_lookup)
    if unnamed:
        resolved = await asyncio.to_thread(sayari.resolve_unnamed_ids, unnamed)
        if resolved:
            # In-hand lookup wins: don't overwrite richer data with a summary.
            id_lookup = {**resolved, **id_lookup}
            if conversation_id:
                try:
                    await conversations.merge_state_doc(
                        conversation_id, {"named_ids": resolved}
                    )
                except Exception:  # persistence is best-effort, never block naming
                    log.warning("named_ids persist failed", exc_info=True)
    return sayari.risk_paths_to_neighborhood(slim, id_lookup)


async def sayari_profile_tool(
    entity_id: str, conversation_id: str | None = None
) -> dict[str, Any]:
    raw = await asyncio.to_thread(sayari.profile, entity_id)
    slim = slim_sayari_profile(raw)
    # Name the risk-path nodes from data already in hand: the profile's own
    # relationships block wins (freshest, type-accurate), then entities seen
    # earlier this conversation. A bounded batch resolve then names the central
    # multi-hop nodes a hub entity leaves unnamed (see _resolve_and_map_risk_paths).
    id_lookup = {
        **(await _known_entity_lookup(conversation_id)),
        **sayari.related_entity_lookup(raw),
    }
    nb: Neighborhood = await _resolve_and_map_risk_paths(slim, id_lookup, conversation_id)
    risk = slim.get("risk") or {}
    return {
        "profile": slim,
        # Convenience for the agent: the factors worth surfacing, already slim.
        "risk_factors": (risk.get("direct_factors") or []) + (risk.get("derived_factors") or []),
        "nodes": [n.model_dump() for n in nb.nodes],
        "edges": [e.model_dump() for e in nb.edges],
        "metadata": nb.metadata,
    }


async def sayari_ownership_tool(
    entity_id: str,
    direction: str = "downstream",
    limit: int = 25,
) -> dict[str, Any]:
    raw = await asyncio.to_thread(sayari.ownership, entity_id, direction, limit)
    root_label = raw.get("name") or entity_id
    nb: Neighborhood = sayari.ownership_to_neighborhood(
        raw, entity_id, str(root_label), direction
    )
    return {
        "nodes": [n.model_dump() for n in nb.nodes],
        "edges": [e.model_dump() for e in nb.edges],
        "metadata": nb.metadata,
        "direction": direction,
    }


async def sayari_search_tool(query: str, limit: int = 10) -> dict[str, Any]:
    candidates = await asyncio.to_thread(sayari.search, query, limit)
    # Light graph mapping: only the top few RELEVANT leads become nodes so a
    # broad search seeds the canvas without flooding it or pinning off-type
    # fuzzy hits (full lead list still goes to the model). Passing `query` lets
    # search_to_nodes drop leads that don't share a meaningful name token with
    # the query before picking the top-N to pin.
    nodes = sayari.search_to_nodes(candidates, query=query)
    # Identify exactly which leads are pinned to the canvas so the agent can
    # narrate the SAME subset the graph shows (prevents graph/text divergence).
    pinned_entity_ids = [n.id for n in nodes]
    pinned_set = set(pinned_entity_ids)
    candidate_dicts: list[dict[str, Any]] = []
    # Lightweight node reps for EVERY lead (pinned + unpinned), each tagged with
    # `pinned`. This rides along on the search tool result / SSE event so the UI
    # can OVERLAY the unpinned leads on demand ("Showing N of M leads" toggle)
    # WITHOUT them entering `nodes` (which is what merge_graph persists). The
    # persistent, accumulated graph therefore still only gains the pinned top-N.
    all_lead_nodes: list[dict[str, Any]] = []
    for c in candidates:
        d = c.model_dump()
        is_pinned = c.entity_id in pinned_set
        d["pinned_to_graph"] = is_pinned
        candidate_dicts.append(d)
        node = sayari.search_candidate_node(c).model_dump()
        node["pinned"] = is_pinned
        all_lead_nodes.append(node)
    return {
        "query": query,
        "candidates": candidate_dicts,
        "count": len(candidates),
        "nodes": [n.model_dump() for n in nodes],
        # Full lead set as overlay-ready nodes (pinned flag per node). NOT merged
        # into the persistent graph — purely a per-search client-side overlay.
        "all_lead_nodes": all_lead_nodes,
        # The subset actually shown on the graph; the agent should highlight these
        # first so its prose and the canvas agree.
        "pinned_entity_ids": pinned_entity_ids,
        "metadata": {
            "source_system": "sayari",
            "kind": "search",
            "shown_on_graph": len(nodes),
            "count": len(candidates),
        },
        "note": (
            "Broad LEADS, not confirmed matches and not a ranked answer. Triage by "
            "label/country/flags, then sayari_resolve + sayari_profile the ones worth "
            "pursuing before making any claim. The leads in pinned_entity_ids "
            "(candidates with pinned_to_graph=true) are the ones shown on the graph — "
            "highlight those first so text and canvas agree."
        ),
    }


async def sayari_summary_tool(
    entity_id: str, conversation_id: str | None = None
) -> dict[str, Any]:
    raw = await asyncio.to_thread(sayari.summary, entity_id)
    slim = slim_sayari_profile(raw)
    # entity_summary is relationship-free, so the profile carries no relationships
    # block to name path nodes from — fall back to conversation-known entities,
    # then the placeholder.
    id_lookup = {
        **(await _known_entity_lookup(conversation_id)),
        **sayari.related_entity_lookup(raw),
    }
    nb: Neighborhood = await _resolve_and_map_risk_paths(slim, id_lookup, conversation_id)
    risk = slim.get("risk") or {}
    return {
        "profile": slim,
        "relationship_free": True,
        "risk_factors": (risk.get("direct_factors") or []) + (risk.get("derived_factors") or []),
        "nodes": [n.model_dump() for n in nb.nodes],
        "edges": [e.model_dump() for e in nb.edges],
        "metadata": nb.metadata,
    }


async def sayari_watchlist_tool(entity_id: str, limit: int = 10) -> dict[str, Any]:
    raw = await asyncio.to_thread(sayari.watchlist, entity_id, limit)
    root_label = raw.get("name") or entity_id
    nb: Neighborhood = sayari.watchlist_to_neighborhood(raw, entity_id, str(root_label))
    return {
        "nodes": [n.model_dump() for n in nb.nodes],
        "edges": [e.model_dump() for e in nb.edges],
        "metadata": nb.metadata,
        "paths": len(raw.get("data") or []),
    }


async def sayari_record_tool(record_id: str) -> dict[str, Any]:
    raw = await asyncio.to_thread(sayari.record, record_id)
    return {
        "record": slim_sayari_record(raw),
        "metadata": {"source_system": "sayari", "kind": "record"},
    }


# --- Memory tool: read-only recall over the structured investigation state ----
# Backed entirely by Redis (conversations.get_state_doc). Spends NO external
# credits and adds nothing to the canvas. `conversation_id` is injected by the
# agent loop (NOT part of the model-visible input_schema) so the model can't
# read another conversation's state.


def _entity_view(rec: dict[str, Any]) -> dict[str, Any]:
    """Augment a registry entity with the derived fields the agent ranks on:
    `regime_count` (distinct sanctions regimes) and `severity_score` (the
    deterministic ranking value). `is_sdn` is already on the record."""
    regimes = conversations._sanctions_regimes(rec.get("sanctions_lists"))
    out = dict(rec)
    out["regime_count"] = len(regimes)
    out["severity_score"] = conversations.entity_severity_score(rec)
    return out


async def recall_state_tool(
    conversation_id: str | None,
    kind: str,
    from_turn: int | None = None,
    country: str | None = None,
    sanctioned: bool | None = None,
    index: int | None = None,
    sort: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Query this conversation's structured state by kind + filters. Returns
    `{items, count, total_in_state}` where each item is the EXACT stored record
    (IDs/provenance byte-exact). No external API call, no graph nodes."""
    if not conversation_id:
        return {"items": [], "count": 0, "total_in_state": 0,
                "note": "no conversation context available for recall."}

    doc = await conversations.get_state_doc(conversation_id)
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        lim = 25
    lim = max(1, lim)

    if kind == "leads":
        leads = list(doc.get("leads") or [])
        total = len(leads)
        # `index` resolves against the most-recent lead set (highest from_turn)
        # so "profile the Nth one" maps to a concrete entity_id.
        if index is not None:
            turns = [l.get("from_turn") for l in leads if l.get("from_turn") is not None]
            if turns:
                recent = max(turns)
                # Within a from_turn group the stored order is the original
                # search order (stable sort), so index 1..N maps to the leads
                # exactly as the agent first saw them ("profile the Nth one").
                recent_set = [l for l in leads if l.get("from_turn") == recent]
                items = recent_set[index - 1: index] if index >= 1 else []
            else:
                items = []
            return {"items": items, "count": len(items), "total_in_state": total}
        filtered = leads
        if from_turn is not None:
            filtered = [l for l in filtered if l.get("from_turn") == from_turn]
        if country is not None:
            cc = country.strip().upper()
            filtered = [l for l in filtered if cc in [str(x).upper() for x in (l.get("countries") or [])]]
        if sanctioned is not None:
            filtered = [l for l in filtered if bool(l.get("sanctioned")) == sanctioned]
        return {"items": filtered[:lim], "count": len(filtered[:lim]), "total_in_state": total}

    if kind == "resolved_entities":
        recs = [r for r in (doc.get("resolved_entities") or {}).values() if isinstance(r, dict)]
        total = len(recs)
        if sanctioned is not None:
            recs = [r for r in recs if bool(r.get("sanctioned")) == sanctioned]
        return {"items": recs[:lim], "count": len(recs[:lim]), "total_in_state": total}

    if kind == "sanctions":
        rows = list(doc.get("sanctions_adjudicated") or [])
        total = len(rows)
        if from_turn is not None:
            rows = [r for r in rows if r.get("from_turn") == from_turn]
        return {"items": rows[:lim], "count": len(rows[:lim]), "total_in_state": total}

    if kind == "entities":
        # The unified registry: the FULL connected-entity pool (ownership
        # neighbors + search leads + sanctions hits), one rankable set. This is
        # what answers "the most sanctioned connected entity" — rank across ALL
        # of it, not just the resolved subjects.
        recs = [
            _entity_view(r)
            for r in (doc.get("entities") or {}).values()
            if isinstance(r, dict)
        ]
        total = len(recs)
        if sanctioned is not None:
            recs = [r for r in recs if bool(r.get("sanctioned")) == sanctioned]
        if country is not None:
            cc = country.strip().upper()
            recs = [r for r in recs if cc in [str(x).upper() for x in (r.get("countries") or [])]]
        # Default sort = severity (the SDN/sanctioned-first ranking). "recency"
        # falls back to last_seen_turn; anything else keeps severity.
        if (sort or "severity").lower() == "recency":
            recs.sort(key=lambda r: r.get("last_seen_turn") or 0, reverse=True)
        else:
            recs.sort(
                key=lambda r: (r.get("severity_score") or 0.0, r.get("last_seen_turn") or 0),
                reverse=True,
            )
        return {
            "items": recs[:lim],
            "count": len(recs[:lim]),
            "total_in_state": total,
            "sorted_by": (sort or "severity").lower(),
            "ranking_note": (
                "Ranked by severity: OFAC SDN first, then other sanctioned by # of "
                "distinct regimes, then PEP. Re-sort with sort='recency', or filter "
                "(sanctioned=true, country=...). severity_score / is_sdn / "
                "regime_count are on each item."
            ),
        }

    if kind == "claims":
        rows = list(doc.get("claims") or [])
        total = len(rows)
        if from_turn is not None:
            rows = [r for r in rows if r.get("from_turn") == from_turn]
        return {"items": rows[:lim], "count": len(rows[:lim]), "total_in_state": total}

    return {"items": [], "count": 0, "total_in_state": 0,
            "error": (
                f"unknown kind: {kind} "
                "(expected leads|entities|resolved_entities|sanctions|claims)"
            )}


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
            "company, or intermediary by name. Use this for LEAK PROVENANCE (does X appear "
            "in the offshore leaks?) and to CORROBORATE a Sayari resolution against ICIJ — "
            "it is NOT the default first call. For a named person/company, start with "
            "sayari_resolve (broader, more current coverage and stronger identifiers); reach "
            "for search_entity when the question is specifically about leak presence or to "
            "cross-reference a subject you already resolved. Returns up to 10 matches with "
            "their node id, type (Entity/Officer/Intermediary/Address), source leak (e.g. "
            "'Panama Papers'), and a Lucene relevance score. Score >=8 indicates a "
            "strong/exact name match; 4-8 is a partial match; <4 typically means only common "
            "words matched and the result is likely unrelated to the user's query. If the top "
            "score is low AND no result's name closely matches what the user asked about, "
            "treat this as NOT FOUND in ICIJ — do not invent connections."
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
            "Check if a name appears on any global sanctions/screening list (OFAC SDN, OFAC "
            "Consolidated/non-SDN, BIS Entity List, US Trade CSL, EU FSF, UK HMT, Swiss SECO, "
            "Canadian SEMA, UN, PEP lists, etc.) via OpenSanctions. Pass schema='Person' for "
            "individuals (officers, intermediaries) and schema='Organization' for entities/companies. "
            "Returns matches with `score` (0-1, with 0.70+ being a confident match), `lists` (the "
            "EXPLICIT program/list-type label for each hit), `on_watchlist`, and `any_strong_match`. "
            "Empty hits means clean (no watchlist hit). Call this on the SUBJECT first, then on each "
            "Officer surfaced by get_officers — connected-to-sanctioned is often more revealing than "
            "direct hits, since the latter are usually already known publicly. Use `on_watchlist` "
            "(NOT the label text) to tell real watchlist hits from PEP/wikidata/registry context. "
            "Report the program in `lists` VERBATIM and distinguish OFAC SDN (blocked) from OFAC "
            "Consolidated (non-SDN) from BIS Entity List (export controls) from CSL/SAM (screening/"
            "debarment) — NEVER upgrade a non-SDN/Consolidated/Entity-List hit to 'SDN'. `sanctions_id` "
            "is an OpenSanctions record id (e.g. 'ofac-30947'), NOT an OFAC SDN number — do not "
            "relabel it 'SDN #...'."
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
    {
        "name": "sayari_resolve",
        "description": (
            "Resolve a raw name (optionally + address, country, type) to ranked Sayari "
            "entity CANDIDATES. Sayari aggregates global registries, sanctions, trade, and "
            "watchlists — far broader and more current than ICIJ — so for most investigations "
            "this is your FIRST call. Returns a ranked `candidates` list, each with entity_id, "
            "label, type, `score` (relevance rank, DESCENDING — not a 0-1 confidence), "
            "`match_strength` (weak/medium/strong), countries, addresses, and `identifiers` "
            "(OFAC SDN #, LEI, SEC CIK, national reg numbers — the strong join keys). "
            "CRITICAL: this returns candidates, NOT an answer. The top score is NOT always the "
            "canonical entity (searching 'Sberbank' at the HQ address returns a subsidiary "
            "first; the parent ranks lower). Disambiguate using score + match_strength + "
            "address + identifiers, and reason about whether candidates[0] is really your "
            "target. Pass the entity_id of the match you pick to sayari_profile / "
            "sayari_ownership. Never auto-merge a Sayari entity into an ICIJ node on name alone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The person or company name, as the user provided it.",
                },
                "address": {
                    "type": "string",
                    "description": "Optional street address to disambiguate (strongly improves ranking).",
                },
                "country": {
                    "type": "string",
                    "description": "Optional ISO trigram country code, e.g. 'RUS', 'CHN'.",
                },
                "type": {
                    "type": "string",
                    "description": "Optional entity type filter, e.g. 'company' or 'person'.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max candidates to return (default 10).",
                    "default": 10,
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "sayari_profile",
        "description": (
            "Given a Sayari entity_id (from sayari_resolve), return the entity's profile: "
            "identity, flags (`sanctioned`, `pep`, `state_owned`), key identifiers, "
            "relationship counts, and — the headline — its RISK FACTORS, already slimmed to "
            "stay within budget. The risk block has: `counts_by_level` (how many factors at "
            "each severity: critical > high > elevated > relevant), `total_factors` (the full "
            "count, so you know what's summarized), `direct_factors` (directly sanctioned / "
            "state-owned / export-controlled — the headline hits, verbatim), and "
            "`derived_factors` (the top ownership-derived factors WITH their `traversal_path` "
            "— the exact ownership/control chain that triggered the risk, which renders as a "
            "highlighted chain on the graph). Factors named `psa_*` are ER-derived "
            "(Possibly-Same-As) and LOWER CONFIDENCE — treat them as leads, not hard hits, the "
            "same match discipline you apply to name collisions. Use the surfaced factors to "
            "populate `sayari_risk_factors` in your final output. Do NOT ask for the raw 95-"
            "factor map; this slimmed view is the intended input."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "Sayari entity_id of the candidate you picked from sayari_resolve.",
                },
            },
            "required": ["entity_id"],
        },
    },
    {
        "name": "sayari_ownership",
        "description": (
            "Walk a Sayari entity's ownership/control graph and return it as graph nodes + "
            "edges (rendered on the canvas, source-tagged 'sayari'). direction='downstream' "
            "(default) shows what this entity OWNS (subsidiaries, branches, holdings); "
            "direction='ubo' shows who ULTIMATELY OWNS it (beneficial owners up the chain). "
            "Use 'ubo' to answer 'who really controls X?' and 'downstream' to answer 'what "
            "does X control?'. Each returned node carries its own sanctioned/pep flags, so one "
            "traversal already reveals which owners or holdings are risky. Capped by `limit`; "
            "popular entities (Gazprom has 15k relationships) are sampled. Call this AFTER "
            "sayari_profile when ownership structure matters to the question."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "Sayari entity_id to traverse.",
                },
                "direction": {
                    "type": "string",
                    "enum": ["downstream", "ubo"],
                    "description": "'downstream' = what it owns; 'ubo' = who owns it. Default 'downstream'.",
                    "default": "downstream",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max ownership paths to return (default 25).",
                    "default": 25,
                },
            },
            "required": ["entity_id"],
        },
    },
    {
        "name": "sayari_search",
        "description": (
            "BROAD, fuzzy investigative search over Sayari for lead generation — "
            "distinct from sayari_resolve. Use this when the user has a vague or "
            "exploratory query ('companies named Rosneft Trading', 'who else is at "
            "this address', 'find shell-looking entities tied to X') and you want to "
            "cast a WIDE net, NOT pin down one canonical entity. Returns up to ~20 "
            "slim candidate LEADS: entity_id, label, type, countries, sanctioned/pep "
            "flags, and the top risk-factor names. These are LEADS, not confirmed "
            "matches and not ranked by confidence — triage them, then sayari_resolve "
            "+ sayari_profile the ones worth pursuing before making any claim. Only "
            "the top few leads are added to the graph (to avoid flooding it). For a "
            "specific named person/company you already want to identify, prefer "
            "sayari_resolve (precise resolution)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Free-text search query (name, partial name, or keywords).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max leads to return (default 10, max 20).",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "sayari_summary",
        "description": (
            "Relationship-free profile of a Sayari entity_id — a CHEAPER variant of "
            "sayari_profile. Returns the same identity + flags + slimmed RISK FACTORS "
            "(counts_by_level, direct_factors, derived_factors with traversal paths) "
            "but omits the relationships block. Use this for SECONDARY entities (an "
            "owner, subsidiary, or co-party you found while investigating the primary "
            "subject) to check their risk WITHOUT spending the credits/tokens of a "
            "full profile. Reserve the full sayari_profile for the PRIMARY investigated "
            "entity. Same psa_*-is-lower-confidence discipline applies."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "Sayari entity_id of a SECONDARY entity to risk-check cheaply.",
                },
            },
            "required": ["entity_id"],
        },
    },
    {
        "name": "sayari_watchlist",
        "description": (
            "Traverse from a Sayari entity to PEP/watchlisted entities it is "
            "connected to, returning the paths as graph nodes + edges (source-tagged "
            "'sayari', rendered as highlighted watchlist chains). This surfaces "
            "INDIRECT exposure — a clean company whose owner/subsidiary/officer is "
            "PEP or watchlisted — which complements check_sanctions (DIRECT listing of "
            "the subject itself). Use it when the question is about sanctions/PEP "
            "EXPOSURE or hidden watchlist proximity, after you've resolved the subject. "
            "Each node carries its own sanctioned/pep flags. Capped to a small number "
            "of paths and shallow depth to control cost; an empty result means no "
            "watchlist entity is reachable within that bound (itself informative)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "Sayari entity_id to traverse for watchlist exposure.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max watchlist paths to return (default 10, max 15).",
                    "default": 10,
                },
            },
            "required": ["entity_id"],
        },
    },
    {
        "name": "sayari_record",
        "description": (
            "Fetch a specific Sayari SOURCE RECORD by its record id, for document-level "
            "PROVENANCE. Sayari risk factors, identifiers, and attributes carry `record` "
            "ids (e.g. '<sourceId>/<docId>/<ts>'); pass one here to get the underlying "
            "record's `source`, `source_url`, and `document_urls` so a finding can be "
            "traced to a primary document, not just an aggregated entity. Use sparingly "
            "and only when the user asks for the source/evidence behind a specific fact, "
            "or when you need to cite a document. Returns slimmed record fields + the "
            "document URLs; the id is URL-escaped for you."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "record_id": {
                    "type": "string",
                    "description": "A Sayari record id (e.g. the `record_id` from sayari_profile/sayari_summary, or a record from a risk factor/attribute).",
                },
            },
            "required": ["record_id"],
        },
    },
    {
        "name": "recall_state",
        "description": (
            "Query your own structured investigation memory for THIS conversation. "
            "Kinds: 'entities' = the UNIFIED registry of every connected entity you've "
            "touched (ownership/control neighbors, search leads, AND check_sanctions "
            "hits) in ONE id-keyed, rankable pool; 'leads' = full lead lists from earlier "
            "sayari_search calls; 'sanctions' = adjudicated sanctions verdicts (confirmed "
            "+ dismissed); 'claims' = your prior structured claims with their source_refs; "
            "'resolved_entities' = legacy name-keyed resolved subjects. Use this to "
            "enumerate, filter, or RANK things you already found instead of re-searching. "
            "For a SUPERLATIVE / ranked question ('the most sanctioned connected entity', "
            "'highest-risk owner'), call kind='entities' with sort='severity' to rank "
            "across the FULL pool — this is the only way to compare ownership neighbors "
            "against sanctions hits, which live in different layers. Each entity item "
            "carries is_sdn, regime_count, and severity_score so you can state and re-sort "
            "the ranking. Returns exact stored records; NO external API call, no credits. "
            "Prefer this over re-running sayari_search/sayari_resolve/check_sanctions for "
            "anything already in state."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["entities", "leads", "resolved_entities", "sanctions", "claims"],
                    "description": "Which slice of state to read.",
                },
                "from_turn": {
                    "type": "integer",
                    "description": "Optional: only leads/items/claims first seen on this turn.",
                },
                "country": {
                    "type": "string",
                    "description": "Optional ISO trigram filter for leads/entities, e.g. 'CYP'.",
                },
                "sanctioned": {
                    "type": "boolean",
                    "description": "Optional: only sanctioned items (leads, resolved_entities, entities).",
                },
                "sort": {
                    "type": "string",
                    "enum": ["severity", "recency"],
                    "description": (
                        "For kind='entities': 'severity' (DEFAULT — OFAC SDN first, then "
                        "other sanctioned by distinct-regime count, then PEP) or 'recency'."
                    ),
                },
                "index": {
                    "type": "integer",
                    "description": "Optional 1-based index into the most recent lead set (for 'profile the Nth one').",
                },
                "limit": {"type": "integer", "default": 25},
            },
            "required": ["kind"],
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

# Async tools (network calls awaited directly so the SSE loop stays responsive).
_ASYNC = {
    "check_sanctions": check_sanctions_tool,
    "sayari_resolve": sayari_resolve_tool,
    "sayari_profile": sayari_profile_tool,
    "sayari_ownership": sayari_ownership_tool,
    "sayari_search": sayari_search_tool,
    "sayari_summary": sayari_summary_tool,
    "sayari_watchlist": sayari_watchlist_tool,
    "sayari_record": sayari_record_tool,
    "recall_state": recall_state_tool,
}

# Tools that read per-conversation memory. The agent loop injects the real
# conversation_id (not the model) so the model can't spoof which conversation
# it reads from; conversation_id is deliberately absent from their input_schema.
# sayari_profile/summary use it ONLY to name risk-path nodes from prior-turn
# entities (best-effort); the model neither sees nor sets it.
_NEEDS_CONVERSATION_ID = frozenset({"recall_state", "sayari_profile", "sayari_summary"})


async def execute_tool(
    name: str,
    arguments: dict[str, Any],
    conversation_id: str | None = None,
) -> str:
    """Run a tool by name. Returns a JSON string (what we'll send back to Claude
    as tool_result content). Wraps errors so a bad tool call doesn't crash the
    agent loop — Claude can read the error and decide what to do.

    `conversation_id` is injected by the caller for the memory tools (see
    `_NEEDS_CONVERSATION_ID`); it is NOT part of the model-visible schema, so the
    model can neither set nor spoof it.
    """
    # 'args' is reserved on LogRecord (printf-style logging) — use tool_args.
    log.info("tool_call", extra={"tool": name, "tool_args": arguments})
    try:
        if name in _ASYNC:
            kwargs = dict(arguments)
            if name in _NEEDS_CONVERSATION_ID:
                kwargs["conversation_id"] = conversation_id
            result = await _ASYNC[name](**kwargs)
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
