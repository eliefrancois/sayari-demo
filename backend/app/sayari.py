"""Sayari Graph API data layer.

Mirrors graph.py (Neo4j) and sanctions.py (OpenSanctions): this module owns ALL
Sayari SDK calls and the mapping from Sayari shapes into our app shapes. Other
modules call Python functions here and never touch the SDK.

The three calls we use (see docs/03-sayari-data-model.md):
  - resolution.resolution(name, address, country)  -> ranked CANDIDATES
  - entity.get_entity(id)                            -> full EntityDetails
  - traversal.ownership(id) / traversal.ubo(id)      -> ownership/control paths

Auth, token rotation, and 429 retry-after are handled inside the SDK — we do
not hand-roll any of it.

Design rules baked in here:
  - resolve() returns candidates, NOT an answer. The agent disambiguates.
  - We never return the raw risk map; slimming happens in agent_common via
    slim_sayari_profile (the tool layer calls it).
  - Ownership results and risk traversal paths map onto the same Neighborhood
    node/edge shape the ICIJ tools use, tagged source_system="sayari" so the
    frontend graph can color and legend them.
"""

from __future__ import annotations

import logging
import os
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app import hs_screen
from app.config import get_settings
from app.schema import (
    GraphEdge,
    GraphNode,
    Neighborhood,
    SayariCandidate,
    SayariSearchCandidate,
    SayariShipment,
    SayariShortestPath,
    SayariShortestPathHop,
    SayariTradeEdge,
    SayariTradeParty,
)

# Hard caps on traversal breadth/depth. Sayari traversals bill per explored
# relationship and can balloon (Gazprom = 15k); these clamp the cost-per-call so
# "answer any question" can't quietly run an enormous traversal.
_MAX_TRAVERSAL_LIMIT = 40
_MAX_WATCHLIST_LIMIT = 15
_MAX_DEPTH = 4
_MAX_SEARCH_LIMIT = 20

# Trade slimming (Tier 2). We pull a bounded page of shipments, keep the top-N by
# value/recency for the model + graph, and aggregate the rest into facets so trade
# data never floods the model or the canvas (the same slimming discipline as the
# risk map). _TRADE_FETCH is the page we ask Sayari for; _TRADE_KEEP is the slice
# that becomes shipment rows + graph edges.
_TRADE_FETCH_LIMIT = 50
_TRADE_KEEP = 20
_TRADE_MAX_KEEP = 25
_TRADE_MIN_KEEP = 15

# Bounded naming for hub-entity risk paths. A hub (e.g. Gazprom) has multi-hop
# risk paths far wider than the 1-hop relationships block, so most path nodes
# arrive unnamed and render as anonymous "Unresolved entity" blobs. We spend a
# CAPPED number of cheap, relationship-free entity_summary calls to name the
# most central unknown nodes; the cap is the credit/latency budget per profile.
_MAX_RISK_PATH_RESOLUTIONS = 12
# entity_summary is I/O-bound HTTP on a sync SDK client, so fan the bounded
# batch out across a small thread pool to keep added latency reasonable.
_RISK_PATH_RESOLVE_WORKERS = 6

# Severity ordering for picking the headline risk-factor names on a search lead.
_LEVEL_ORDER = {"critical": 0, "high": 1, "elevated": 2, "relevant": 3}

log = logging.getLogger("erre.sayari")

# --- Client singleton ------------------------------------------------------

_client_singleton: Any = None


def get_client() -> Any:
    """Process-wide lazy Sayari client. Imported lazily so the module loads
    even if the SDK or creds are absent (the tool surfaces a clean error)."""
    global _client_singleton
    if _client_singleton is None:
        from sayari.client import Sayari  # local import: keep module import cheap

        s = get_settings()
        cid = s.sayari_client_id or os.getenv("SAYARI_CLIENT_ID") or os.getenv("CLIENT_ID")
        secret = (
            s.sayari_client_secret
            or os.getenv("SAYARI_CLIENT_SECRET")
            or os.getenv("CLIENT_SECRET")
        )
        if not cid or not secret:
            raise RuntimeError(
                "Sayari credentials missing: set SAYARI_CLIENT_ID / SAYARI_CLIENT_SECRET."
            )
        _client_singleton = Sayari(client_id=cid, client_secret=secret)
    return _client_singleton


def _as_dict(obj: Any) -> Any:
    """Coerce an SDK pydantic model to a plain (recursively-converted) dict."""
    for attr in ("model_dump", "dict"):
        if hasattr(obj, attr):
            try:
                return getattr(obj, attr)()
            except Exception:  # pragma: no cover - defensive
                pass
    return obj


# --- Sayari type -> our GraphNode label ------------------------------------

_PERSON_TYPES = {"person"}
_ENTITY_TYPES = {
    "company",
    "government_organization",
    "security",
    "contract",
    "legal_matter",
    "account",
    "transaction",
}


def _label_for_type(stype: str | None) -> str:
    """Map a Sayari entity type onto our bounded GraphNode label set."""
    t = (stype or "").lower()
    if t in _PERSON_TYPES:
        return "Officer"  # a controlling individual, rendered like an officer
    if t == "address":
        return "Address"
    if t in _ENTITY_TYPES:
        return "Entity"
    return "Other"


# Our own GraphNode labels, as stored (lowercased) in conversation state_doc's
# resolved_entities. A known-entity lookup can hand us either a raw Sayari type
# (company/person/...) or one of these already-mapped labels, so coerce both.
_OWN_LABELS = {
    "entity": "Entity",
    "officer": "Officer",
    "intermediary": "Intermediary",
    "address": "Address",
    "other": "Other",
}


def _coerce_label(type_str: str | None) -> str:
    """Resolve a GraphNode label from EITHER a raw Sayari entity type
    (company/person/government_organization/...) OR one of our own labels
    already persisted in conversation state (entity/officer/...)."""
    if not type_str:
        return "Other"
    t = type_str.strip().lower()
    if t in _OWN_LABELS:
        return _OWN_LABELS[t]
    return _label_for_type(type_str)


def related_entity_lookup(raw_profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """id -> {label, type, sanctioned, pep, countries} from the profile's
    1-hop `relationships` block. A risk factor's `traversal_path` lands on these
    connected entities, so this lets us NAME path nodes from data already in the
    profile — no per-id get_entity call (which would cost credits/latency).

    Empty when the profile carries no relationships block (e.g. the
    relationship-free `entity_summary`), in which case the caller falls back to
    conversation-level known entities, then to a placeholder."""
    rels = raw_profile.get("relationships") if isinstance(raw_profile, dict) else None
    data = rels.get("data") if isinstance(rels, dict) else None
    out: dict[str, dict[str, Any]] = {}
    for item in data or []:
        if not isinstance(item, dict):
            continue
        tgt = item.get("target") or {}
        if not isinstance(tgt, dict):
            continue
        nid = tgt.get("id")
        if not nid or not tgt.get("label"):
            continue
        out[nid] = {
            "label": tgt.get("label"),
            "type": tgt.get("type"),
            "sanctioned": tgt.get("sanctioned"),
            "pep": tgt.get("pep"),
            "countries": tgt.get("countries") or [],
        }
    return out


def _node(
    entity_id: str,
    label: str,
    name: str,
    *,
    properties: dict[str, Any] | None = None,
) -> GraphNode:
    return GraphNode(
        id=entity_id,
        label=label,  # type: ignore[arg-type]
        name=name or f"…{entity_id[-6:]}",
        source_system="sayari",
        properties=properties or {},
    )


def _entity_node(ent: dict[str, Any]) -> GraphNode:
    """Build a GraphNode from a Sayari entity-lite dict (target / path entity)."""
    return _node(
        ent.get("id") or "",
        _label_for_type(ent.get("type")),
        ent.get("label") or "(unnamed)",
        properties={
            "type": ent.get("type"),
            "countries": ent.get("countries") or [],
            "sanctioned": ent.get("sanctioned"),
            "pep": ent.get("pep"),
        },
    )


# --- Public: the three calls ----------------------------------------------


def resolve(
    name: str,
    address: str | None = None,
    country: str | None = None,
    type: str | None = None,
    limit: int = 10,
) -> list[SayariCandidate]:
    """Resolve a raw name (+ optional address/country/type) to ranked Sayari
    candidates. Returns CANDIDATES, not an answer — the agent picks."""
    res = get_client().resolution.resolution(
        name=name,
        address=address or None,
        country=country or None,
        type=type or None,
        limit=limit,
    )
    out: list[SayariCandidate] = []
    for c in getattr(res, "data", None) or []:
        d = _as_dict(c)
        if not isinstance(d, dict):
            continue
        ms = d.get("match_strength")
        if isinstance(ms, dict):
            ms = ms.get("value")
        out.append(
            SayariCandidate(
                entity_id=d.get("entity_id") or "",
                label=d.get("label") or "(unnamed)",
                type=d.get("type"),
                score=d.get("score"),
                match_strength=ms,
                countries=d.get("countries") or [],
                identifiers=[
                    {"type": i.get("type"), "value": i.get("value"), "label": i.get("label")}
                    for i in (d.get("identifiers") or [])
                    if isinstance(i, dict)
                ][:12],
                addresses=(d.get("addresses") or [])[:5],
            )
        )
    return out


def profile(entity_id: str) -> dict[str, Any]:
    """Full EntityDetails as a plain dict. Slimming for the model is the tool
    layer's job (slim_sayari_profile) — this returns the raw shape."""
    ent = _as_dict(get_client().entity.get_entity(entity_id))
    return ent if isinstance(ent, dict) else {}


def ownership(
    entity_id: str,
    direction: str = "downstream",
    limit: int = 25,
    max_depth: int = 4,
) -> dict[str, Any]:
    """Walk the ownership/control graph.

    direction="downstream" (what this entity owns) -> traversal.ownership
    direction="ubo"        (who ultimately owns it) -> traversal.ubo
    Returns the raw traversal dict (data[] of {source, target, path}).
    """
    limit = max(1, min(limit, _MAX_TRAVERSAL_LIMIT))
    max_depth = max(1, min(max_depth, _MAX_DEPTH))
    trav = get_client().traversal
    if direction == "ubo":
        res = trav.ubo(entity_id, limit=limit, max_depth=max_depth)
    else:
        res = trav.ownership(entity_id, limit=limit, max_depth=max_depth)
    out = _as_dict(res)
    return out if isinstance(out, dict) else {}


def _top_risk_names(risk: dict[str, Any], n: int = 3) -> list[str]:
    """The N most-severe risk-factor NAMES on an entity-lite dict (for triage
    cards in search results — not the full factor map)."""
    if not isinstance(risk, dict):
        return []
    ranked: list[tuple[int, str]] = []
    for name, data in risk.items():
        level = data.get("level") if isinstance(data, dict) else None
        ranked.append((_LEVEL_ORDER.get((level or "").lower(), 99), name))
    ranked.sort()
    return [name for _, name in ranked[:n]]


def search(q: str, limit: int = 10) -> list[SayariSearchCandidate]:
    """Broad/fuzzy Entity Search (lead-gen) — distinct from precise resolve().

    Returns slim candidate leads (id, label, type, country, flags, top risk
    names), NOT a ranked answer. Use to cast a wide net; the agent triages the
    leads and resolves/profiles the ones worth pursuing."""
    limit = max(1, min(limit, _MAX_SEARCH_LIMIT))
    res = get_client().search.search_entity(q=q, limit=limit)
    out: list[SayariSearchCandidate] = []
    for c in getattr(res, "data", None) or []:
        d = _as_dict(c)
        if not isinstance(d, dict):
            continue
        out.append(
            SayariSearchCandidate(
                entity_id=d.get("id") or "",
                label=d.get("label") or "(unnamed)",
                type=d.get("type"),
                countries=d.get("countries") or [],
                sanctioned=d.get("sanctioned"),
                pep=d.get("pep"),
                top_risk=_top_risk_names(d.get("risk") or {}),
            )
        )
    return out


def summary(entity_id: str) -> dict[str, Any]:
    """Relationship-free entity profile (cheaper than get_entity). Same shape as
    profile() minus the relationships block — used for SECONDARY entities to
    control tokens/credits. Slimming stays the tool layer's job."""
    ent = _as_dict(get_client().entity.entity_summary(entity_id))
    return ent if isinstance(ent, dict) else {}


def watchlist(entity_id: str, limit: int = 10, max_depth: int = 4) -> dict[str, Any]:
    """Traverse from the target to PEP/watchlisted entities. Returns the raw
    traversal dict (data[] of {source, target, path}) — same shape as ownership,
    so it maps through the same neighborhood builder. Surfaces INDIRECT exposure
    (vs OpenSanctions check_sanctions, which tests DIRECT listing)."""
    limit = max(1, min(limit, _MAX_WATCHLIST_LIMIT))
    max_depth = max(1, min(max_depth, _MAX_DEPTH))
    res = get_client().traversal.watchlist(entity_id, limit=limit, max_depth=max_depth)
    out = _as_dict(res)
    return out if isinstance(out, dict) else {}


def record(record_id: str) -> dict[str, Any]:
    """Fetch a specific source record for document-level provenance. The record
    id is URL-escaped (it contains '/' separators). Slimming is the tool's job."""
    rid = urllib.parse.quote(record_id, safe="")
    rec = _as_dict(get_client().record.get_record(rid))
    return rec if isinstance(rec, dict) else {}


# --- Pin relevance filter --------------------------------------------------
# Tunable knobs for which broad-search leads are RELEVANT enough to PIN to the
# graph. This only gates graph PINNING — the full lead list always goes to the
# model. Generic tokens (legal forms, connectors) carry no entity identity, so
# we strip them from both the query and a candidate label before measuring
# name overlap; otherwise "Rosneft Trading S.A." and the query "Rosneft Trading"
# would also "match" on the meaningless "s.a." token.
_PIN_STOPWORDS = {
    "the", "of", "and", "for", "a", "an", "de", "la",
    "sa", " sa", "ag", "plc", "ltd", "llc", "inc", "co", "corp",
    "limited", "incorporated", "company", "corporation",
    "gmbh", "pao", "oao", "ooo", "zao", "jsc", "spa", "srl", "bv", "nv",
    "group", "holding", "holdings", "trust",
}

# Sayari entity `type` strings that map onto a real legal entity / company-style
# node (vs a person, address, or unclassified hit). Observed search vocabulary
# is dominated by "company"; this set mirrors _ENTITY_TYPES so the pin ranker
# can prefer company-style leads when a query targets an organization without
# HARD-excluding anything on a type guess.
_PIN_ENTITY_LABELS = {"Entity"}


def _meaningful_tokens(text: str | None) -> set[str]:
    """The identity-bearing tokens of a name or query: lowercased, split on any
    non-alphanumeric (Latin or Cyrillic), with single-char tokens and generic
    legal-form/connector stopwords removed. Used to measure name overlap for
    pin relevance, so a label shares a token with the query only when they share
    a real word (not just a dropped suffix)."""
    raw = re.split(r"[^0-9a-zA-Zа-яёА-ЯЁ]+", (text or "").lower())
    return {t for t in raw if len(t) > 1 and t not in _PIN_STOPWORDS}


def _relevant_for_pin(
    candidate: SayariSearchCandidate, query_tokens: set[str]
) -> bool:
    """Whether a broad-search lead is relevant enough to PIN to the graph.

    Conservative, name-driven gate (applied ONLY to pinning, never to the leads
    returned to the model): the candidate label must share at least one
    meaningful token with the query. This drops fuzzy lexical hits that match
    only on a brand fragment in a different script or on stripped stopwords —
    e.g. a Russian-language "trade UNION organization of Rosneft" whose label
    shares no Latin identity token with "Rosneft Trading" — while keeping every
    real "Rosneft Trading / Rosneft Trade" company. Fails OPEN (keeps the lead)
    when the query has no usable tokens, so we never over-filter."""
    if not query_tokens:
        return True
    return bool(_meaningful_tokens(candidate.label) & query_tokens)


def _pin_rank_key(candidate: SayariSearchCandidate) -> int:
    """Stable-sort key for ordering the RELEVANT leads before taking the top-N.
    Prefers real company/legal-entity leads over person/address/other hits;
    leads with an equal key keep Sayari's own relevance order (a stable sort),
    so this nudges off-type hits down without reordering the real companies."""
    return 0 if _label_for_type(candidate.type) in _PIN_ENTITY_LABELS else 1


def search_candidate_node(c: SayariSearchCandidate) -> GraphNode:
    """A lightweight GraphNode for a single broad-search lead (no edges). Used
    both for the pinned top-N (search_to_nodes) and for the full overlay set the
    'Showing N of M leads' toggle reveals on the client."""
    return _node(
        c.entity_id,
        _label_for_type(c.type),
        c.label,
        properties={
            "type": c.type,
            "countries": c.countries,
            "sanctioned": c.sanctioned,
            "pep": c.pep,
        },
    )


def search_to_nodes(
    candidates: list[SayariSearchCandidate],
    query: str | None = None,
    limit: int = 5,
) -> list[GraphNode]:
    """Map the most RELEVANT top-N search leads onto light GraphNodes (no edges)
    so a broad search seeds the canvas without flooding it. Ids match Sayari
    entity ids, so a node here merges with a later profile/ownership traversal
    of the same id.

    Relevance filter (only affects PINNING, not the leads returned to the
    model): leads must share a meaningful name token with `query`
    (_relevant_for_pin), then are ranked to prefer company/legal-entity types
    (_pin_rank_key) before the top-N are pinned. If the filter would leave
    nothing (e.g. an all-Cyrillic query against Latin labels), it fails open to
    the raw leads so the canvas is never empty."""
    query_tokens = _meaningful_tokens(query)
    relevant = [c for c in candidates if _relevant_for_pin(c, query_tokens)]
    # Fail open: never let the filter blank the canvas for an unusual query.
    pinnable = relevant or candidates
    ranked = sorted(pinnable, key=_pin_rank_key)
    return [search_candidate_node(c) for c in ranked[:limit]]


# --- Graph mapping ---------------------------------------------------------


def ownership_to_neighborhood(
    traversal: dict[str, Any],
    root_id: str,
    root_label: str,
    direction: str,
    root_type: str | None = None,
) -> Neighborhood:
    """Map a traversal result onto our Neighborhood (source-tagged "sayari").

    Each data item carries a `path` of {field, entity} hops from `source`
    (root_id) to a `target`. We replay each path as a chain of nodes + edges so
    the full ownership/control structure renders, not just the endpoints.
    """
    nodes: dict[str, GraphNode] = {}
    edges: dict[str, GraphEdge] = {}

    nodes[root_id] = _node(root_id, _label_for_type(root_type), root_label)

    for item in traversal.get("data") or []:
        if not isinstance(item, dict):
            continue
        prev = item.get("source") or root_id
        path = item.get("path") or []
        # Fallback: no path detail -> single hop source -> target.
        if not path:
            tgt = item.get("target") or {}
            if isinstance(tgt, dict) and tgt.get("id"):
                n = _entity_node(tgt)
                nodes.setdefault(n.id, n)
                _add_edge(edges, prev, n.id, "linked_to")
            continue
        for hop in path:
            if not isinstance(hop, dict):
                continue
            ent = hop.get("entity") or {}
            field = hop.get("field") or "linked_to"
            tid = ent.get("id")
            if not tid:
                continue
            n = _entity_node(ent)
            nodes.setdefault(n.id, n)
            _add_edge(edges, prev, tid, field)
            prev = tid

    return Neighborhood(
        nodes=list(nodes.values()),
        edges=list(edges.values()),
        metadata={
            "root_id": root_id,
            "direction": direction,
            "paths": len(traversal.get("data") or []),
            "explored_count": traversal.get("explored_count"),
            "source_system": "sayari",
        },
    )


def watchlist_to_neighborhood(
    traversal: dict[str, Any],
    root_id: str,
    root_label: str,
    root_type: str | None = None,
) -> Neighborhood:
    """Map a watchlist traversal onto a Neighborhood as highlighted PEP/watchlist
    chains. Identical path shape to ownership, so we reuse that builder and tag
    the result kind='watchlist' (the frontend renders it as a flagged chain)."""
    nb = ownership_to_neighborhood(
        traversal, root_id, root_label, "watchlist", root_type
    )
    nb.metadata["kind"] = "watchlist"
    return nb


def unnamed_risk_path_ids(
    slim_profile: dict[str, Any], id_lookup: dict[str, dict[str, Any]]
) -> list[str]:
    """Risk-path entity ids that the in-hand `id_lookup` can't name, ranked by
    DEGREE (most-connected first) so a bounded resolver spends its budget on the
    most decision-relevant unknown nodes — the central hubs of the risk chains,
    not the leaf flotsam. Ties break by first appearance for stable ordering.
    Excludes the root and any id the lookup already names."""
    root_id = slim_profile.get("id")
    degree: dict[str, int] = {}
    pos: dict[str, int] = {}
    risk = slim_profile.get("risk") or {}
    for factor in risk.get("derived_factors") or []:
        for path_str in factor.get("path") or []:
            if not isinstance(path_str, str):
                continue
            ids = [t for t in path_str.split("|") if t][0::2]
            for i, nid in enumerate(ids):
                pos.setdefault(nid, len(pos))
                if i > 0:
                    degree[nid] = degree.get(nid, 0) + 1
                    degree[ids[i - 1]] = degree.get(ids[i - 1], 0) + 1
    unnamed = [
        nid
        for nid in pos
        if nid and nid != root_id and not (id_lookup.get(nid) or {}).get("label")
    ]
    unnamed.sort(key=lambda nid: (-degree.get(nid, 0), pos[nid]))
    return unnamed


def resolve_unnamed_ids(
    ids: list[str], cap: int = _MAX_RISK_PATH_RESOLUTIONS
) -> dict[str, dict[str, Any]]:
    """Name a BOUNDED batch of risk-path entity ids via Sayari entity_summary.

    WHY: the profile's 1-hop relationships block can't name the multi-hop risk
    paths of a hub entity, so most path nodes would render as anonymous blobs.
    The SDK exposes no batch entity endpoint, so we fan cheap, relationship-free
    entity_summary calls out CONCURRENTLY (I/O-bound HTTP on a sync client) and
    cap the count to keep credits/latency bounded. Per-id failures fail OPEN —
    skipped, keeping their "Unresolved" placeholder — because naming is
    best-effort and must never crash an investigation.

    Returns id -> {label, type, sanctioned, pep, countries}, the same shape as
    related_entity_lookup, for the ids we could name."""
    chosen = [i for i in ids if i][: max(0, cap)]
    if not chosen:
        return {}

    def _one(nid: str) -> tuple[str, dict[str, Any] | None]:
        try:
            ent = summary(nid)
        except Exception:  # best-effort: a failed id keeps its placeholder
            return nid, None
        label = ent.get("label") if isinstance(ent, dict) else None
        if not label:
            return nid, None
        return nid, {
            "label": label,
            "type": ent.get("type"),
            "sanctioned": ent.get("sanctioned"),
            "pep": ent.get("pep"),
            "countries": ent.get("countries") or [],
        }

    out: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(_RISK_PATH_RESOLVE_WORKERS, len(chosen))) as pool:
        for nid, info in pool.map(_one, chosen):
            if info:
                out[nid] = info
    return out


def _risk_path_node(
    nid: str, fname: str | None, id_lookup: dict[str, dict[str, Any]]
) -> GraphNode:
    """Build a path node, NAMING it from `id_lookup` when the connected entity is
    known (profile relationships first, then conversation-known entities). Falls
    back to a clearly-labelled placeholder — never an invented name — when the
    id is genuinely unresolved."""
    info = id_lookup.get(nid)
    if info and info.get("label"):
        props: dict[str, Any] = {"risk_factor": fname}
        if info.get("type") is not None:
            props["type"] = info.get("type")
        if info.get("sanctioned") is not None:
            props["sanctioned"] = info.get("sanctioned")
        if info.get("pep") is not None:
            props["pep"] = info.get("pep")
        if info.get("countries"):
            props["countries"] = info.get("countries")
        return _node(nid, _coerce_label(info.get("type")), info["label"], properties=props)
    # Genuinely unknown: a clearer placeholder than a bare "…id", flagged so the
    # UI/agent can tell this node was not resolved (vs an entity literally named).
    return _node(
        nid,
        "Other",
        f"Unresolved entity (…{nid[-6:]})",
        properties={"risk_factor": fname, "unresolved": True},
    )


def risk_paths_to_neighborhood(
    slim_profile: dict[str, Any],
    id_lookup: dict[str, dict[str, Any]] | None = None,
) -> Neighborhood:
    """Map the surfaced derived risk factors' traversal_paths onto a
    Neighborhood. A path like `GAZPROM|has_subsidiary|X|owner_of|Y` becomes a
    highlighted chain (the "show your work" overlay). Node ids match Sayari
    entity ids, so these merge with ownership nodes (gaining real names).

    `id_lookup` (id -> {label, type, sanctioned, pep, countries}) names the
    connected path nodes from data already in hand — the profile's relationships
    block and/or entities seen earlier this conversation — so the decision-
    relevant far node (e.g. the sanctioned related entity) shows its real name
    instead of an anonymous "…id". No extra Sayari calls."""
    nodes: dict[str, GraphNode] = {}
    edges: dict[str, GraphEdge] = {}
    lookup = id_lookup or {}

    root_id = slim_profile.get("id")
    if root_id:
        nodes[root_id] = _node(
            root_id,
            _label_for_type(slim_profile.get("type")),
            slim_profile.get("label") or "(unnamed)",
        )

    risk = slim_profile.get("risk") or {}
    for factor in risk.get("derived_factors") or []:
        fname = factor.get("name")
        for path_str in factor.get("path") or []:
            if not isinstance(path_str, str):
                continue
            tokens = [t for t in path_str.split("|") if t]
            # tokens: id, rel, id, rel, id, ...
            ids = tokens[0::2]
            rels = tokens[1::2]
            for i, nid in enumerate(ids):
                if nid not in nodes:
                    nodes[nid] = _risk_path_node(nid, fname, lookup)
                if i > 0:
                    rel = rels[i - 1] if i - 1 < len(rels) else "linked_to"
                    _add_edge(edges, ids[i - 1], nid, rel)

    return Neighborhood(
        nodes=list(nodes.values()),
        edges=list(edges.values()),
        metadata={"root_id": root_id, "source_system": "sayari", "kind": "risk_paths"},
    )


def _add_edge(
    edges: dict[str, GraphEdge],
    src: str,
    tgt: str,
    rel: str,
    properties: dict[str, Any] | None = None,
) -> None:
    key = f"{src}::{rel}::{tgt}"
    if key not in edges:
        edges[key] = GraphEdge(
            source=src,
            target=tgt,
            type=rel,
            source_system="sayari",
            properties=properties or {},
        )


# --- Tier 2: trade (shipments) ----------------------------------------------


def trade_shipments(
    entity_id: str, role: str = "supplier", limit: int = _TRADE_FETCH_LIMIT
) -> dict[str, Any]:
    """Search Sayari shipments where `entity_id` is the supplier (default) or
    the buyer. Returns the raw search dict ({data: [Shipment], ...}); slimming
    is the mapper's job. Page size is hard-capped (_TRADE_FETCH_LIMIT) so a
    trade-heavy entity can't balloon a single call."""
    from sayari.trade.types.trade_filter_list import TradeFilterList  # lazy import

    limit = max(1, min(limit, _TRADE_FETCH_LIMIT))
    if role == "buyer":
        filt = TradeFilterList(buyer_id=[entity_id])
    else:
        filt = TradeFilterList(supplier_id=[entity_id])
    res = get_client().trade.search_shipments(filter=filt, limit=limit)
    out = _as_dict(res)
    return out if isinstance(out, dict) else {}


def _slim_trade_party(raw: Any, role: str) -> SayariTradeParty | None:
    """SourceOrDestinationEntity -> SayariTradeParty. names[] can carry ~50-70
    aliases, so we keep names[0] + a count. `bis_tags` are Sayari's NATIVE
    export-control risk-factor names off the party's `risks` dict; `sanctioned`
    only for a DIRECT sanctioned_* tag (not ownership exposure)."""
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    names = [n for n in (raw.get("names") or []) if n]
    risks = raw.get("risks") or {}
    sanctioned = any(str(k).lower().startswith("sanctioned") for k in risks) if isinstance(risks, dict) else False
    return SayariTradeParty(
        entity_id=raw["id"],
        name=str(names[0]) if names else f"…{raw['id'][-6:]}",
        names_count=len(names),
        countries=raw.get("countries") or [],
        role="buyer" if role == "buyer" else "supplier",
        sanctioned=sanctioned,
        bis_tags=hs_screen.native_bis_tags(risks),
    )


def _shipment_value(raw: dict[str, Any]) -> tuple[float | None, str | None]:
    """First monetary_value entry's (value, currency); Sayari ships a list of
    {value, currency, context} and the first entry is the shipment value."""
    for mv in raw.get("monetary_value") or []:
        if isinstance(mv, dict) and mv.get("value") is not None:
            try:
                return float(mv["value"]), mv.get("currency")
            except (TypeError, ValueError):
                continue
    return None, None


def slim_shipment(raw: dict[str, Any]) -> SayariShipment:
    """One raw Sayari Shipment -> the slim, decision-relevant SayariShipment.
    Runs the HS dual-use screen here so every shipment carries its verdict:
    `dual_use` fires on EITHER an HS-screen hit (provenance hs_screen) OR a
    party carrying a native Sayari BIS/export tag (provenance sayari_bis_tag)."""
    suppliers = raw.get("supplier") or []
    buyers = raw.get("buyer") or []
    supplier = _slim_trade_party(suppliers[0] if suppliers else None, "supplier")
    buyer = _slim_trade_party(buyers[0] if buyers else None, "buyer")

    hs = [
        {"code": h.get("code"), "description": h.get("description")}
        for h in (raw.get("hs_codes") or [])
        if isinstance(h, dict) and h.get("code")
    ]
    hits = hs_screen.screen_hs_codes([h["code"] for h in hs])
    native = bool((supplier and supplier.bis_tags) or (buyer and buyer.bis_tags))

    value, currency = _shipment_value(raw)
    dates = [d for d in (raw.get("departure_date"), raw.get("arrival_date")) if isinstance(d, str) and d]
    return SayariShipment(
        id=str(raw.get("id") or raw.get("record") or ""),
        supplier=supplier,
        buyer=buyer,
        hs_codes=hs,
        value=value,
        currency=currency,
        departure_country=raw.get("departure_country") or [],
        transit_country=raw.get("transit_country") or [],
        arrival_country=raw.get("arrival_country") or [],
        last_date=max(dates) if dates else None,  # ISO strings sort correctly
        dual_use=bool(hits) or native,
        dual_use_hits=hits,
    )


def slim_shipments(raw_search: dict[str, Any]) -> list[SayariShipment]:
    """Map a raw search_shipments result to slim shipments, ranked by value
    (desc) then date (desc) so the kept top slice is the most decision-relevant."""
    out = [
        slim_shipment(s) for s in (raw_search.get("data") or []) if isinstance(s, dict)
    ]
    out.sort(key=lambda s: ((s.value or 0.0), s.last_date or ""), reverse=True)
    return out


def trade_facets(shipments: list[SayariShipment]) -> dict[str, Any]:
    """Aggregate facets over ALL fetched shipments (kept + dropped) so the slice
    we slim away is still summarized for the model: counts, total value, top
    HS codes, top arrival countries, dual-use tally."""
    hs_counts: dict[str, int] = {}
    arr_counts: dict[str, int] = {}
    total_value = 0.0
    dual = 0
    for s in shipments:
        for h in s.hs_codes:
            c = str(h.get("code") or "")
            if c:
                hs_counts[c] = hs_counts.get(c, 0) + 1
        for c in s.arrival_country:
            arr_counts[c] = arr_counts.get(c, 0) + 1
        total_value += s.value or 0.0
        dual += int(s.dual_use)
    top = lambda d, n: sorted(d.items(), key=lambda kv: -kv[1])[:n]  # noqa: E731
    return {
        "shipment_count": len(shipments),
        "total_value": round(total_value, 2) if total_value else None,
        "dual_use_count": dual,
        "top_hs_codes": [{"code": c, "count": n} for c, n in top(hs_counts, 8)],
        "top_arrival_countries": [{"country": c, "count": n} for c, n in top(arr_counts, 8)],
    }


_ROUTES_MAX = 40  # cap the routes array so a trade-heavy entity stays slim
_ROUTE_PARTIES_MAX = 2  # top counterparty names kept per lane (payload stays slim)


def shipments_to_routes(
    shipments: list[SayariShipment],
    subject_id: str | None = None,
    role: str = "supplier",
) -> list[dict[str, Any]]:
    """Aggregate shipments into country-pair routes for the frontend map.

    One entry per (departure, arrival) ISO-3 pair across ALL fetched shipments
    (kept + dropped, same coverage as trade_facets): shipment count, summed
    value, dual_use if ANY shipment on the route screened dual-use, and
    sanctioned_party if ANY shipment's supplier or buyer is directly
    sanctioned. Geography falls back to the party's own country when the
    shipment record lacks a departure/arrival country. Routes ride the tool
    result `metadata` so they reach the UI on the existing tool_call_result
    event without a new channel.

    `top_parties` names the lane's most frequent COUNTERPARTIES (the party
    opposite the queried subject: buyers when role='supplier', suppliers when
    role='buyer'), capped at _ROUTE_PARTIES_MAX so tooltips can read
    "Mikron -> JXJ International Transportation" instead of bare ISO pairs.
    Additive + optional: old stored results simply lack the key.
    """
    routes: dict[tuple[str, str], dict[str, Any]] = {}
    party_counts: dict[tuple[str, str], dict[str, int]] = {}
    for s in shipments:
        dep = next(iter(s.departure_country), None) or (
            next(iter(s.supplier.countries), None) if s.supplier else None
        )
        arr = next(iter(s.arrival_country), None) or (
            next(iter(s.buyer.countries), None) if s.buyer else None
        )
        if not dep or not arr:
            continue
        key = (str(dep).upper(), str(arr).upper())
        sanctioned = bool(
            (s.supplier and s.supplier.sanctioned) or (s.buyer and s.buyer.sanctioned)
        )
        codes = [str(h.get("code")) for h in s.hs_codes if h.get("code")]
        # The counterparty: whichever party is NOT the queried subject, falling
        # back to the role's opposite when ids don't line up.
        counterparty = s.buyer if role == "supplier" else s.supplier
        if subject_id:
            for p in (s.supplier, s.buyer):
                if p and p.entity_id != subject_id:
                    counterparty = p
                    break
        if counterparty and counterparty.name:
            counts = party_counts.setdefault(key, {})
            counts[counterparty.name] = counts.get(counterparty.name, 0) + 1
        r = routes.get(key)
        if r is None:
            routes[key] = {
                "departure_country": key[0],
                "arrival_country": key[1],
                "shipment_count": 1,
                "total_value": s.value,
                "dual_use": s.dual_use,
                "sanctioned_party": sanctioned,
                "hs_codes": codes[:5],
            }
            continue
        r["shipment_count"] += 1
        if s.value is not None:
            r["total_value"] = (r["total_value"] or 0.0) + s.value
        r["dual_use"] = r["dual_use"] or s.dual_use
        r["sanctioned_party"] = r["sanctioned_party"] or sanctioned
        for c in codes:
            if c not in r["hs_codes"] and len(r["hs_codes"]) < 5:
                r["hs_codes"].append(c)
    for key, r in routes.items():
        counts = party_counts.get(key) or {}
        r["top_parties"] = [
            name for name, _ in sorted(counts.items(), key=lambda kv: -kv[1])
        ][:_ROUTE_PARTIES_MAX]
    out = sorted(
        routes.values(),
        key=lambda r: (r["total_value"] or 0.0, r["shipment_count"]),
        reverse=True,
    )
    return out[:_ROUTES_MAX]


def shipments_to_trade_edges(shipments: list[SayariShipment]) -> list[SayariTradeEdge]:
    """Aggregate shipments into one `ships_to` lane per supplier->buyer pair:
    union of HS codes, summed value, latest date, dual_use if ANY shipment on
    the lane screened dual-use (hits unioned with provenance preserved)."""
    lanes: dict[tuple[str, str], SayariTradeEdge] = {}
    for s in shipments:
        if not (s.supplier and s.buyer):
            continue
        key = (s.supplier.entity_id, s.buyer.entity_id)
        codes = [str(h.get("code")) for h in s.hs_codes if h.get("code")]
        lane = lanes.get(key)
        if lane is None:
            lanes[key] = SayariTradeEdge(
                source=key[0],
                target=key[1],
                hs_codes=codes,
                value=s.value,
                last_date=s.last_date,
                shipment_count=1,
                dual_use=s.dual_use,
                dual_use_hits=list(s.dual_use_hits),
            )
            continue
        lane.shipment_count += 1
        for c in codes:
            if c not in lane.hs_codes:
                lane.hs_codes.append(c)
        if s.value is not None:
            lane.value = (lane.value or 0.0) + s.value
        if s.last_date and (not lane.last_date or s.last_date > lane.last_date):
            lane.last_date = s.last_date
        if s.dual_use:
            lane.dual_use = True
            seen = {h.get("code") for h in lane.dual_use_hits}
            lane.dual_use_hits.extend(
                h for h in s.dual_use_hits if h.get("code") not in seen
            )
    return list(lanes.values())


def _trade_party_node(p: SayariTradeParty, dual_use: bool) -> GraphNode:
    """A graph node for a shipment party. `dual_use` marks parties touching a
    dual-use lane (our HS screen or a native BIS tag) so the UI can badge them."""
    return _node(
        p.entity_id,
        "Entity",
        p.name,
        properties={
            "countries": p.countries,
            "sanctioned": p.sanctioned,
            "trade_role": p.role,
            "bis_tags": p.bis_tags,
            "dual_use": dual_use or bool(p.bis_tags),
        },
    )


def trade_to_neighborhood(
    shipments: list[SayariShipment],
    trade_edges: list[SayariTradeEdge],
    root_id: str,
    role: str,
) -> Neighborhood:
    """Map kept shipments onto Neighborhood nodes + `ships_to` edges. One edge
    per supplier->buyer lane (pre-aggregated by shipments_to_trade_edges); the
    edge `properties` carry hs_codes / value / last_date / dual_use so the
    frontend styles trade lanes without a parallel data channel."""
    nodes: dict[str, GraphNode] = {}
    dual_lane_parties: set[str] = set()
    for e in trade_edges:
        if e.dual_use:
            dual_lane_parties.update((e.source, e.target))
    for s in shipments:
        for p in (s.supplier, s.buyer):
            if p and p.entity_id not in nodes:
                nodes[p.entity_id] = _trade_party_node(
                    p, p.entity_id in dual_lane_parties
                )
    edges = [
        GraphEdge(
            source=e.source,
            target=e.target,
            type="ships_to",
            source_system="sayari",
            properties={
                "kind": "trade",
                "hs_codes": e.hs_codes[:10],
                "value": e.value,
                "last_date": e.last_date,
                "shipment_count": e.shipment_count,
                "dual_use": e.dual_use,
                "dual_use_hits": e.dual_use_hits,
            },
        )
        for e in trade_edges
    ]
    return Neighborhood(
        nodes=list(nodes.values()),
        edges=edges,
        metadata={
            "root_id": root_id,
            "role": role,
            "kind": "trade",
            "source_system": "sayari",
            "lanes": len(edges),
        },
    )


# --- Tier 2: shortest path ---------------------------------------------------


def shortest_path(source_id: str, target_id: str) -> dict[str, Any]:
    """Sayari shortest-path between two entities. Returns the raw dict
    ({data: [{source, target: EntityDetails, path: [{field, entity, ...}]}]})."""
    res = get_client().traversal.shortest_path(entities=[source_id, target_id])
    out = _as_dict(res)
    return out if isinstance(out, dict) else {}


def parse_shortest_path(
    raw: dict[str, Any], source_id: str, target_id: str
) -> SayariShortestPath:
    """Raw shortest-path dict -> SayariShortestPath. `has_sanctioned_intermediary`
    fires only on INTERMEDIATE hops (the endpoints' own status is visible on the
    nodes themselves) — the headline 'clean counterparty, dirty chain' signal."""
    items = raw.get("data") or []
    item = items[0] if items and isinstance(items[0], dict) else {}
    target = item.get("target") or {}
    hops: list[SayariShortestPathHop] = []
    for hop in item.get("path") or []:
        if not isinstance(hop, dict):
            continue
        ent = hop.get("entity") or {}
        if not ent.get("id"):
            continue
        hops.append(
            SayariShortestPathHop(
                field=str(hop.get("field") or "linked_to"),
                entity_id=ent["id"],
                label=ent.get("label") or f"…{ent['id'][-6:]}",
                type=ent.get("type"),
                sanctioned=bool(ent.get("sanctioned")),
                pep=bool(ent.get("pep")),
                countries=ent.get("countries") or [],
            )
        )
    # Intermediates exclude the final hop when it IS the target endpoint.
    intermediates = hops[:-1] if hops and hops[-1].entity_id == (target.get("id") or target_id) else hops
    return SayariShortestPath(
        source_id=source_id,
        target_id=target.get("id") or target_id,
        target_label=target.get("label"),
        hops=hops,
        has_sanctioned_intermediary=any(h.sanctioned for h in intermediates),
        found=bool(hops),
    )


def shortest_path_to_neighborhood(
    raw: dict[str, Any],
    source_id: str,
    source_label: str,
    source_type: str | None = None,
    id_lookup: dict[str, dict[str, Any]] | None = None,
) -> Neighborhood:
    """Map a shortest-path result onto the Neighborhood shape, reusing the
    ownership path replayer (same {source, target, path} item shape), then fold
    in the `target` endpoint node — the EntityDetails on the result names it
    better than a bare path hop (and guarantees it renders when the replay
    produced no hop for it).

    The ownership replayer only adds an edge per hop (prev -> hop), so the FINAL
    last-hop -> target leg is never drawn when the path is non-empty. That left
    intermediaries (e.g. Roldugin -> Kerimov -> Gazprom) as disconnected leaves:
    the Kerimov -> Gazprom edge was missing. We always close the chain into the
    target here. `id_lookup` (entities seen this conversation) names hop/target
    nodes the raw result leaves anonymous so intermediates don't fall back to
    "(unnamed)"."""
    nb = ownership_to_neighborhood(
        raw, source_id, source_label, "shortest_path", source_type
    )
    nodes = {n.id: n for n in nb.nodes}
    items = raw.get("data") or []
    item = items[0] if items and isinstance(items[0], dict) else {}
    target = item.get("target") or {}
    tid = target.get("id")
    if tid:
        # The EntityDetails endpoint node wins over an unnamed hop placeholder.
        nodes[tid] = _entity_node(target)
        edge_map = {f"{e.source}::{e.type}::{e.target}": e for e in nb.edges}
        # The last node before the target: the final path hop's entity, falling
        # back to the source when the path is empty (direct source -> target).
        last_id = source_id
        last_field = "linked_to"
        for hop in item.get("path") or []:
            if not isinstance(hop, dict):
                continue
            ent = hop.get("entity") or {}
            if ent.get("id"):
                last_id = ent["id"]
                last_field = str(hop.get("field") or "linked_to")
        # Always close the chain into the target (skip only when the path already
        # ends AT the target, where the replay drew that final edge for us).
        if last_id != tid:
            _add_edge(edge_map, last_id, tid, last_field)
        nb.edges = list(edge_map.values())
    if id_lookup:
        # Name any hop/target node the raw result left anonymous from entities
        # already seen this conversation (richer in-hand data wins, so a real
        # label is never overwritten by a placeholder).
        for nid, node in nodes.items():
            named = (id_lookup.get(nid) or {}).get("label")
            if named and (not node.name or node.name == "(unnamed)"
                          or node.name == f"…{nid[-6:]}"):
                node.name = named
    nb.nodes = list(nodes.values())
    nb.metadata["kind"] = "shortest_path"
    nb.metadata["target_id"] = tid
    return nb
