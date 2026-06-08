"""Multi-turn conversation state in Upstash Redis.

This is the product-persistence layer (distinct from any future LangGraph
agent-runtime checkpointing). It holds everything the UI needs to render and
resume an investigation across turns and page reloads:

  conversation:{id}:meta        -> JSON {title, created_at, updated_at, turn_count}
  conversation:{id}:state       -> "idle" | "running" | "error"
  conversation:{id}:lock        -> "1" (SET NX EX) while a turn runs
  conversation:{id}:events      -> list of SSE events (each tagged turn_index)
  conversation:{id}:turns       -> list of turn metadata
  conversation:{id}:summaries   -> list of RiskSummary dicts (investigation turns)
  conversation:{id}:answers     -> list of TurnAnswer dicts (follow-up turns)
  conversation:{id}:context     -> compressed episodic memory string
  conversation:{id}:graph       -> JSON {nodes, edges} accumulated across turns
  conversation:{id}:state_doc   -> JSON structured investigation state (exact recall)

Design notes:
  - Append-only lists use RPUSH; wholesale-replaced values use SET.
  - Every write refreshes the 24h TTL so an active conversation never expires
    mid-session, and abandoned ones self-clean.
  - Cross-turn continuity comes from two complementary tiers: the compressed
    prose `context` digest (narrative) and the structured `state_doc` (exact,
    ID-rich recall of resolved entities, full lead lists, and sanctions
    verdicts). This is the industry-standard episodic-memory pattern
    (Mem0/SimpleMem-style): structured summaries beat replaying every raw
    tool_result, which bloats tokens and degrades quality.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger("erre.conversations")

_TTL_SECONDS = 60 * 60 * 24  # 24h
_LOCK_TTL_SECONDS = 300  # a turn must finish (or crash) within 5 min


def _client() -> httpx.AsyncClient:
    s = get_settings()
    return httpx.AsyncClient(
        base_url=s.upstash_redis_rest_url,
        headers={"Authorization": f"Bearer {s.upstash_redis_rest_token}"},
        timeout=5.0,
    )


def _k(conversation_id: str, suffix: str) -> str:
    return f"conversation:{conversation_id}:{suffix}"


def new_conversation_id() -> str:
    return uuid.uuid4().hex


# ---------- Lifecycle ----------


async def create_conversation(title: str = "New investigation") -> str:
    cid = new_conversation_id()
    now = int(time.time())
    meta = {"title": title, "created_at": now, "updated_at": now, "turn_count": 0}
    async with _client() as c:
        await c.post("/pipeline", json=[
            ["SET", _k(cid, "meta"), json.dumps(meta), "EX", str(_TTL_SECONDS)],
            ["SET", _k(cid, "state"), "idle", "EX", str(_TTL_SECONDS)],
            ["SET", _k(cid, "graph"), json.dumps({"nodes": [], "edges": []}), "EX", str(_TTL_SECONDS)],
        ])
    return cid


async def exists(conversation_id: str) -> bool:
    async with _client() as c:
        r = await c.get(f"/exists/{_k(conversation_id, 'meta')}")
        r.raise_for_status()
        return r.json().get("result") == 1


async def get_state(conversation_id: str) -> str | None:
    async with _client() as c:
        r = await c.get(f"/get/{_k(conversation_id, 'state')}")
        r.raise_for_status()
        return r.json().get("result")


async def set_state(conversation_id: str, state: str) -> None:
    async with _client() as c:
        await c.post("/pipeline", json=[
            ["SET", _k(conversation_id, "state"), state, "EX", str(_TTL_SECONDS)],
        ])


# ---------- Lock (prevent concurrent turns) ----------


async def acquire_lock(conversation_id: str) -> bool:
    """SET key 1 NX EX 300 — returns True if we got the lock, False if held."""
    async with _client() as c:
        r = await c.post("/pipeline", json=[
            ["SET", _k(conversation_id, "lock"), "1", "NX", "EX", str(_LOCK_TTL_SECONDS)],
        ])
        r.raise_for_status()
        # pipeline returns a list of {"result": ...}; SET NX returns "OK" or null
        first = r.json()
        if isinstance(first, list) and first:
            return first[0].get("result") == "OK"
        return False


async def release_lock(conversation_id: str) -> None:
    async with _client() as c:
        await c.post("/pipeline", json=[["DEL", _k(conversation_id, "lock")]])


# ---------- Events (SSE) ----------


async def append_event(conversation_id: str, event: dict[str, Any]) -> None:
    payload = json.dumps(event, default=str)
    key = _k(conversation_id, "events")
    async with _client() as c:
        await c.post("/pipeline", json=[
            ["RPUSH", key, payload],
            ["EXPIRE", key, str(_TTL_SECONDS)],
        ])


async def read_events(conversation_id: str, start: int = 0) -> list[dict[str, Any]]:
    async with _client() as c:
        r = await c.get(f"/lrange/{_k(conversation_id, 'events')}/{start}/-1")
        r.raise_for_status()
        raw = r.json().get("result") or []
        return [json.loads(s) for s in raw]


async def event_count(conversation_id: str) -> int:
    async with _client() as c:
        r = await c.get(f"/llen/{_k(conversation_id, 'events')}")
        r.raise_for_status()
        return int(r.json().get("result") or 0)


# ---------- Turns / results ----------


async def append_turn(conversation_id: str, turn: dict[str, Any]) -> None:
    key = _k(conversation_id, "turns")
    async with _client() as c:
        await c.post("/pipeline", json=[
            ["RPUSH", key, json.dumps(turn, default=str)],
            ["EXPIRE", key, str(_TTL_SECONDS)],
        ])


async def append_summary(conversation_id: str, summary: dict[str, Any]) -> None:
    key = _k(conversation_id, "summaries")
    async with _client() as c:
        await c.post("/pipeline", json=[
            ["RPUSH", key, json.dumps(summary, default=str)],
            ["EXPIRE", key, str(_TTL_SECONDS)],
        ])


async def append_answer(conversation_id: str, answer: dict[str, Any]) -> None:
    key = _k(conversation_id, "answers")
    async with _client() as c:
        await c.post("/pipeline", json=[
            ["RPUSH", key, json.dumps(answer, default=str)],
            ["EXPIRE", key, str(_TTL_SECONDS)],
        ])


async def _read_list(conversation_id: str, suffix: str) -> list[dict[str, Any]]:
    async with _client() as c:
        r = await c.get(f"/lrange/{_k(conversation_id, suffix)}/0/-1")
        r.raise_for_status()
        raw = r.json().get("result") or []
        return [json.loads(s) for s in raw]


# ---------- Compressed episodic context ----------


async def get_context(conversation_id: str) -> str:
    async with _client() as c:
        r = await c.get(f"/get/{_k(conversation_id, 'context')}")
        r.raise_for_status()
        return r.json().get("result") or ""


async def set_context(conversation_id: str, text: str) -> None:
    async with _client() as c:
        await c.post("/pipeline", json=[
            ["SET", _k(conversation_id, "context"), text, "EX", str(_TTL_SECONDS)],
        ])


# ---------- Structured investigation state (state_doc) ----------
# Deterministic, exact-recall memory of an investigation: resolved entities by
# id, the FULL lead lists from earlier sayari_search calls (not just the pinned
# top-N), adjudicated sanctions verdicts, pinned node ids, a per-turn log, and
# a cache of names resolved for risk-path nodes (named_ids) so the bounded
# risk-path resolve compounds across turns instead of re-paying per turn.
# Merged deterministically in finalize_node from data already in hand (no LLM
# call), and rendered as an `INVESTIGATION STATE` block ahead of the prose
# digest. The agent pulls full detail on demand via the `recall_state` tool.

_MAX_LEADS = 40  # cap the stored lead list by recency so context can't bloat


_MAX_CLAIMS = 100  # cap the stored claims list by recency so context can't bloat


def _empty_state_doc() -> dict[str, Any]:
    """The empty default: every key present with empty containers.

    `named_ids` (id -> {label, type, sanctioned, pep, countries}) caches names
    resolved for risk-path nodes this conversation, so a later turn naming the
    SAME multi-hop node reuses the cached label instead of re-spending a Sayari
    entity_summary call — the bounded resolve compounds across turns.

    `entities` (Phase B, doc 09 §5) is the UNIFIED id-keyed registry: one
    id -> identity store that every tool deposits into and every consumer reads
    from. It is a deterministic PROJECTION over the other buckets (resolved
    subjects + named_ids + leads + the sanctions ledger), recomputed on every
    read/merge by `_project_entities`, so old stored docs that predate it are
    backfilled transparently (true backward-compat, no migration step). The KEY
    addition over the legacy buckets: strong check_sanctions hits become
    first-class registry entities (keyed by sanctions_id), not just ledger rows,
    so the full connected-entity set — ownership neighbors, search leads, AND
    sanctions hits — becomes one queryable, rankable pool.

    `claims` (doc 09 §5) holds the structured claims the agent emitted in its
    typed terminator (text + confidence + source_refs + the entity_ids those
    refs resolve to). Structured-only: never NLP-parsed from prose."""
    return {
        "resolved_entities": {},
        "leads": [],
        "sanctions_adjudicated": [],
        "pinned_node_ids": [],
        "turn_log": [],
        "named_ids": {},
        # Phase B unified registry + structured claims. `entities` is derived
        # (projected) from the buckets above on every read; `claims` is stored.
        "entities": {},
        "claims": [],
    }


def _normalize_subject(subject: str | None) -> str:
    """The key for resolved_entities: lowercased, trimmed."""
    return (subject or "").strip().lower()


# ---------- Unified entity registry (Phase B) ----------
# One id-keyed identity store. Built by deterministically folding the legacy
# buckets (no LLM, no network) so it stays backward-compatible: the registry is
# a VIEW over data already persisted, plus the sanctions-as-entities deposit.

# Which source named an id, ranked so a richer source's label wins a merge
# (doc 08 §3.2 merge policy: richer / more-authoritative source wins). A full
# profile/traversal beats a search snippet beats a bare referenced id.
_ENTITY_SOURCE_RANK: dict[str, int] = {
    "sayari_profile": 6,
    "sayari_ownership": 6,
    "sayari": 5,
    "resolved": 5,
    "sayari_summary": 5,
    "sayari_watchlist": 5,
    "check_sanctions": 4,
    "search": 3,
    "referenced": 2,
    "named_ids": 2,
}


def _source_rank(src: str | None) -> int:
    return _ENTITY_SOURCE_RANK.get((src or "").strip().lower(), 1)


def _is_sdn_label(label: Any) -> bool:
    """True only for the OFAC SDN (Specially Designated Nationals) blocked list —
    the most severe OFAC posture. Explicitly NOT the OFAC Consolidated/non-SDN
    list (same name-collision discipline the prompt enforces): 'non-SDN' /
    'non sdn' / 'consolidated' never count as SDN."""
    l = str(label or "").lower()
    if "sdn" not in l:
        return False
    if "non-sdn" in l or "non sdn" in l or "consolidated" in l:
        return False
    return True


def _sanctions_regimes(lists: Any) -> list[str]:
    """Distinct, normalized sanctions list/program labels on an entity — the
    basis for the 'number of distinct regimes' tiebreak in severity ranking."""
    seen: dict[str, str] = {}
    for x in lists or []:
        s = str(x or "").strip()
        if s:
            seen.setdefault(s.lower(), s)
    return list(seen.values())


def entity_severity_score(e: dict[str, Any]) -> float:
    """Deterministic severity score for ranking connected entities (doc 09 §11
    'most sanctioned' fix). Higher = more severe:

      OFAC SDN (blocked)            -> dominates everything
      then any other sanctioned     -> OFAC non-SDN / BIS / EU / UN / regulatory
      then by # of distinct regimes  -> broader listing ranks above a single one
      then PEP                       -> political exposure as a minor bump

    No network, no LLM — purely the sanctions data already folded onto the
    registry entity (`is_sdn`, `sanctioned`, `sanctions_lists`, `pep`)."""
    if not isinstance(e, dict):
        return 0.0
    score = 0.0
    if e.get("is_sdn"):
        score += 1000.0
    if e.get("sanctioned"):
        score += 100.0
    score += 10.0 * len(_sanctions_regimes(e.get("sanctions_lists")))
    if e.get("pep"):
        score += 5.0
    return score


def _merge_countries(a: Any, b: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for src in (a or [], b or []):
        for c in src:
            cs = str(c)
            if cs and cs not in seen:
                seen.add(cs)
                out.append(cs)
    return out


def _upsert_entity(entities: dict[str, dict[str, Any]], eid: str, rec: dict[str, Any]) -> None:
    """Merge one deposit into the id-keyed registry (upsert, never blind
    overwrite — doc 08 §3.2). Richer source wins the label/type; True wins for
    the boolean flags; countries + sanctions_lists union; turn span widens."""
    if not eid or not isinstance(rec, dict):
        return
    existing = entities.get(eid)
    if existing is None:
        merged: dict[str, Any] = {
            "id": eid,
            "label": rec.get("label"),
            "type": rec.get("type"),
            "sanctioned": bool(rec.get("sanctioned")),
            "pep": bool(rec.get("pep")),
            "is_sdn": bool(rec.get("is_sdn")),
            "countries": _merge_countries(rec.get("countries"), None),
            "sanctions_lists": _sanctions_regimes(rec.get("sanctions_lists")),
            "source": rec.get("source"),
            "confidence": rec.get("confidence") or "tool_output",
            "first_seen_turn": rec.get("first_seen_turn"),
            "last_seen_turn": rec.get("last_seen_turn"),
        }
        entities[eid] = merged
        return
    merged = dict(existing)
    inc_label = rec.get("label")
    if inc_label and (
        not merged.get("label")
        or _source_rank(rec.get("source")) > _source_rank(merged.get("source"))
    ):
        merged["label"] = inc_label
        merged["source"] = rec.get("source") or merged.get("source")
        if rec.get("type") is not None:
            merged["type"] = rec.get("type")
    if merged.get("type") is None and rec.get("type") is not None:
        merged["type"] = rec.get("type")
    for flag in ("sanctioned", "pep", "is_sdn"):
        if rec.get(flag):
            merged[flag] = True
    merged["countries"] = _merge_countries(merged.get("countries"), rec.get("countries"))
    merged["sanctions_lists"] = _sanctions_regimes(
        (merged.get("sanctions_lists") or []) + _sanctions_regimes(rec.get("sanctions_lists"))
    )
    # tool_output is the strongest provenance; never downgrade it.
    if rec.get("confidence") == "tool_output" or not merged.get("confidence"):
        merged["confidence"] = rec.get("confidence") or merged.get("confidence") or "tool_output"
    fs = [t for t in (merged.get("first_seen_turn"), rec.get("first_seen_turn")) if t is not None]
    ls = [t for t in (merged.get("last_seen_turn"), rec.get("last_seen_turn")) if t is not None]
    if fs:
        merged["first_seen_turn"] = min(fs)
    if ls:
        merged["last_seen_turn"] = max(ls)
    entities[eid] = merged


def _project_entities(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Fold the legacy buckets into the unified id-keyed registry. Deterministic
    and idempotent: every deposit source maps to a bucket already in the doc, so
    this is a pure projection that backfills `entities` for any doc — including
    old ones written before the registry existed.

    Deposit order is least-authoritative first so the merge policy (richer source
    wins) lands the best label: named_ids (cached risk-path names) -> leads
    (search snippets) -> resolved subjects (traversed/profiled) -> sanctions
    ledger (the KEY addition: strong check_sanctions hits as first-class
    sanctioned entities, keyed by sanctions_id)."""
    entities: dict[str, dict[str, Any]] = {}
    # Seed from any already-stored registry first so prior provenance survives.
    for eid, rec in (doc.get("entities") or {}).items():
        if isinstance(rec, dict):
            _upsert_entity(entities, eid, {**rec, "source": rec.get("source")})

    for nid, rec in (doc.get("named_ids") or {}).items():
        if isinstance(rec, dict) and rec.get("label"):
            _upsert_entity(entities, nid, {
                "label": rec.get("label"),
                "type": rec.get("type"),
                "sanctioned": rec.get("sanctioned"),
                "pep": rec.get("pep"),
                "countries": rec.get("countries"),
                "source": "named_ids",
                "confidence": "tool_output",
            })

    for lead in doc.get("leads") or []:
        if not isinstance(lead, dict):
            continue
        eid = lead.get("entity_id")
        if not eid or not lead.get("label"):
            continue
        _upsert_entity(entities, eid, {
            "label": lead.get("label"),
            "type": lead.get("type"),
            "sanctioned": lead.get("sanctioned"),
            "pep": lead.get("pep"),
            "countries": lead.get("countries"),
            "source": "search",
            "confidence": "tool_output",
            "first_seen_turn": lead.get("from_turn"),
            "last_seen_turn": lead.get("from_turn"),
        })

    for rec in (doc.get("resolved_entities") or {}).values():
        if not isinstance(rec, dict):
            continue
        eid = rec.get("entity_id")
        if not eid or not rec.get("label"):
            continue
        _upsert_entity(entities, eid, {
            "label": rec.get("label"),
            "type": rec.get("type"),
            "sanctioned": rec.get("sanctioned"),
            "pep": rec.get("pep"),
            "countries": rec.get("countries"),
            "source": rec.get("source") or "resolved",
            "confidence": "tool_output",
            "first_seen_turn": rec.get("first_seen_turn"),
            "last_seen_turn": rec.get("last_seen_turn"),
        })

    for row in doc.get("sanctions_adjudicated") or []:
        if not isinstance(row, dict):
            continue
        sid = row.get("sanctions_id")
        if not sid:
            continue
        lists = row.get("lists") or []
        _upsert_entity(entities, sid, {
            "label": row.get("matched_name"),
            "type": "sanctions_entity",
            "sanctioned": True,
            "is_sdn": any(_is_sdn_label(x) for x in lists),
            "countries": row.get("countries"),
            "sanctions_lists": lists,
            "source": "check_sanctions",
            "confidence": "tool_output",
            "first_seen_turn": row.get("from_turn"),
            "last_seen_turn": row.get("from_turn"),
        })

    return entities


async def get_state_doc(conversation_id: str) -> dict[str, Any]:
    """Read the structured investigation state, or the empty default shape."""
    async with _client() as c:
        r = await c.get(f"/get/{_k(conversation_id, 'state_doc')}")
        r.raise_for_status()
        raw = r.json().get("result")
    if not raw:
        return _empty_state_doc()
    try:
        doc = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return _empty_state_doc()
    # Forward-compat: guarantee every key exists even on older stored docs.
    base = _empty_state_doc()
    if isinstance(doc, dict):
        for k in base:
            if k in doc and doc[k] is not None:
                base[k] = doc[k]
    # Backward-compat migration (Phase B): the unified registry is a projection
    # over the other buckets, recomputed here so an OLD doc (resolved_entities /
    # named_ids / leads / sanctions but no `entities`) backfills transparently
    # and a NEW doc stays consistent even if a write path lagged. No migration
    # write needed — the registry is always reconstructable from durable buckets.
    base["entities"] = _project_entities(base)
    return base


async def merge_state_doc(conversation_id: str, delta: dict[str, Any]) -> dict[str, Any]:
    """Read-modify-write the structured state with a turn's delta.

    - resolved_entities: upsert by normalized subject key (earliest
      first_seen_turn preserved, latest last_seen_turn advanced).
    - leads: append + dedupe by entity_id keeping the most recent from_turn,
      newest-first, capped to _MAX_LEADS by recency.
    - sanctions_adjudicated: append + dedupe by sanctions_id (most recent wins).
    - pinned_node_ids: order-preserving union.
    - turn_log: append.
    Refreshes the 24h TTL.
    """
    doc = await get_state_doc(conversation_id)

    # resolved_entities -- upsert by normalized key.
    resolved = doc["resolved_entities"]
    for key, rec in (delta.get("resolved_entities") or {}).items():
        nkey = _normalize_subject(key)
        if not nkey or not isinstance(rec, dict):
            continue
        existing = resolved.get(nkey)
        if existing:
            merged = dict(existing)
            for k, v in rec.items():
                if v is not None:
                    merged[k] = v
            fs = [t for t in (existing.get("first_seen_turn"), rec.get("first_seen_turn")) if t is not None]
            ls = [t for t in (existing.get("last_seen_turn"), rec.get("last_seen_turn")) if t is not None]
            if fs:
                merged["first_seen_turn"] = min(fs)
            if ls:
                merged["last_seen_turn"] = max(ls)
            resolved[nkey] = merged
        else:
            resolved[nkey] = dict(rec)

    # leads -- append + dedupe by entity_id (keep most recent from_turn).
    by_eid: dict[str, dict[str, Any]] = {}
    for lead in doc["leads"] + list(delta.get("leads") or []):
        if not isinstance(lead, dict):
            continue
        eid = lead.get("entity_id")
        if not eid:
            continue
        prev = by_eid.get(eid)
        if prev is None or (lead.get("from_turn", -1) >= prev.get("from_turn", -1)):
            by_eid[eid] = lead
    leads = sorted(by_eid.values(), key=lambda l: l.get("from_turn", 0), reverse=True)
    doc["leads"] = leads[:_MAX_LEADS]

    # sanctions_adjudicated -- append + dedupe by sanctions_id (most recent wins).
    by_sid: dict[str, dict[str, Any]] = {}
    for row in doc["sanctions_adjudicated"] + list(delta.get("sanctions_adjudicated") or []):
        if not isinstance(row, dict):
            continue
        sid = row.get("sanctions_id")
        if not sid:
            continue
        prev = by_sid.get(sid)
        if prev is None or (row.get("from_turn", -1) >= prev.get("from_turn", -1)):
            by_sid[sid] = row
    doc["sanctions_adjudicated"] = list(by_sid.values())

    # pinned_node_ids -- order-preserving union.
    seen: set[str] = set()
    pinned: list[str] = []
    for nid in doc["pinned_node_ids"] + list(delta.get("pinned_node_ids") or []):
        if nid and nid not in seen:
            seen.add(nid)
            pinned.append(nid)
    doc["pinned_node_ids"] = pinned

    # turn_log -- append.
    doc["turn_log"] = doc["turn_log"] + list(delta.get("turn_log") or [])

    # named_ids -- upsert by entity id; non-null fields of the incoming record
    # win so a later, richer resolve can fill gaps without dropping known data.
    named = doc.get("named_ids") or {}
    for nid, rec in (delta.get("named_ids") or {}).items():
        if not nid or not isinstance(rec, dict):
            continue
        merged = dict(named.get(nid) or {})
        for k, v in rec.items():
            if v is not None:
                merged[k] = v
        named[nid] = merged
    doc["named_ids"] = named

    # claims -- append the turn's structured claims, dedupe by text (most recent
    # wins), cap by recency. Structured-only (terminator schema), never prose.
    by_text: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for claim in (doc.get("claims") or []) + list(delta.get("claims") or []):
        if not isinstance(claim, dict) or not claim.get("text"):
            continue
        key = str(claim["text"]).strip().lower()
        if key in by_text:
            by_text[key].update({k: v for k, v in claim.items() if v not in (None, [], "")})
        else:
            by_text[key] = dict(claim)
            ordered.append(by_text[key])
    doc["claims"] = ordered[-_MAX_CLAIMS:]

    # entities -- recompute the unified registry from the freshly-merged buckets
    # so every tool's deposit (search leads, profile/ownership/watchlist
    # neighbors via resolved_entities, the risk-path resolver via named_ids, and
    # strong check_sanctions hits via the sanctions ledger) lands in ONE
    # id-keyed pool. Deterministic projection — no LLM, no network.
    doc["entities"] = _project_entities(doc)

    async with _client() as c:
        await c.post("/pipeline", json=[
            ["SET", _k(conversation_id, "state_doc"), json.dumps(doc, default=str), "EX", str(_TTL_SECONDS)],
        ])
    return doc


# ---------- Accumulated graph ----------


async def get_graph(conversation_id: str) -> dict[str, list]:
    async with _client() as c:
        r = await c.get(f"/get/{_k(conversation_id, 'graph')}")
        r.raise_for_status()
        raw = r.json().get("result")
        return json.loads(raw) if raw else {"nodes": [], "edges": []}


async def merge_graph(
    conversation_id: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, list]:
    """Merge new nodes/edges into the stored graph, deduped by id / triple."""
    graph = await get_graph(conversation_id)
    by_id = {n["id"]: n for n in graph.get("nodes", [])}
    for n in nodes:
        by_id[n["id"]] = n
    edge_keys = {
        f"{e['source']}::{e['type']}::{e['target']}": e for e in graph.get("edges", [])
    }
    for e in edges:
        edge_keys[f"{e['source']}::{e['type']}::{e['target']}"] = e
    merged = {"nodes": list(by_id.values()), "edges": list(edge_keys.values())}
    async with _client() as c:
        await c.post("/pipeline", json=[
            ["SET", _k(conversation_id, "graph"), json.dumps(merged, default=str), "EX", str(_TTL_SECONDS)],
        ])
    return merged


# ---------- Meta ----------


async def get_meta(conversation_id: str) -> dict[str, Any]:
    async with _client() as c:
        r = await c.get(f"/get/{_k(conversation_id, 'meta')}")
        r.raise_for_status()
        raw = r.json().get("result")
        return json.loads(raw) if raw else {}


async def bump_meta(conversation_id: str, title: str | None = None) -> dict[str, Any]:
    """Increment turn_count, refresh updated_at, optionally set title (first turn)."""
    meta = await get_meta(conversation_id)
    meta["turn_count"] = int(meta.get("turn_count", 0)) + 1
    meta["updated_at"] = int(time.time())
    if title and meta.get("title") in (None, "", "New investigation"):
        meta["title"] = title
    async with _client() as c:
        await c.post("/pipeline", json=[
            ["SET", _k(conversation_id, "meta"), json.dumps(meta, default=str), "EX", str(_TTL_SECONDS)],
        ])
    return meta


# ---------- Hydration (page reload) ----------


async def hydrate(conversation_id: str) -> dict[str, Any]:
    """Everything the frontend needs to restore a conversation on load."""
    meta = await get_meta(conversation_id)
    state = await get_state(conversation_id)
    graph = await get_graph(conversation_id)
    turns = await _read_list(conversation_id, "turns")
    summaries = await _read_list(conversation_id, "summaries")
    answers = await _read_list(conversation_id, "answers")
    state_doc = await get_state_doc(conversation_id)
    return {
        "conversation_id": conversation_id,
        "meta": meta,
        "state": state,
        "graph": graph,
        "turns": turns,
        "summaries": summaries,
        "answers": answers,
        "state_doc": state_doc,
    }


async def ping() -> bool:
    try:
        async with _client() as c:
            r = await c.get("/ping")
            return r.status_code == 200
    except Exception:
        return False
