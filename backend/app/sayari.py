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

from app.config import get_settings
from app.schema import (
    GraphEdge,
    GraphNode,
    Neighborhood,
    SayariCandidate,
    SayariSearchCandidate,
)

# Hard caps on traversal breadth/depth. Sayari traversals bill per explored
# relationship and can balloon (Gazprom = 15k); these clamp the cost-per-call so
# "answer any question" can't quietly run an enormous traversal.
_MAX_TRAVERSAL_LIMIT = 40
_MAX_WATCHLIST_LIMIT = 15
_MAX_DEPTH = 4
_MAX_SEARCH_LIMIT = 20

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


def _add_edge(edges: dict[str, GraphEdge], src: str, tgt: str, rel: str) -> None:
    key = f"{src}::{rel}::{tgt}"
    if key not in edges:
        edges[key] = GraphEdge(source=src, target=tgt, type=rel, source_system="sayari")
