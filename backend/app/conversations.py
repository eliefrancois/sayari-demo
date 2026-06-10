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

Plus one global key — the recents index behind GET /conversations:

  conversations:index           -> ZSET member=conversation_id score=updated_at

Branching (Stage 2a) keys — additive, same TTL discipline; a conversation that
never forks (and every conversation created before these keys existed) behaves
exactly as before because the tree keys are simply absent or form a single
chain whose folded state is byte-identical to the merged doc:

  conversation:{id}:turn_tree            -> HASH turn_id -> JSON tree entry
                                            {turn_id, parent_turn_id, turn_index,
                                             user_message, status, kind,
                                             context_after, ...terminator meta}
  conversation:{id}:turn_delta:{turn_id} -> LIST of JSON state_doc deltas this
                                            turn produced (mid-turn named_ids
                                            merges + the finalize projection)
  conversation:{id}:turn_graph:{turn_id} -> JSON {nodes, edges} the turn added
  conversation:{id}:tree_base            -> JSON {state_doc, graph, context}
                                            snapshot taken when the first
                                            tree-aware turn lands on a
                                            conversation with pre-tree turns

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

import contextlib
import json
import logging
import time
import uuid
from contextvars import ContextVar
from typing import Any, Iterator

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


# The conversation index: a ZSET of conversation_id scored by updated_at, so
# "recent investigations" is one ZREVRANGE. Same 24h TTL discipline as every
# other key — every touch refreshes it, so the index self-cleans alongside the
# conversations it points at. Members whose meta expired first are lazily
# ZREM'd by list_conversations. Conversations created before the index existed
# simply never appear (acceptable: this is a recents menu, not an archive).
_INDEX_KEY = "conversations:index"

# Per-conversation keys with a fixed suffix (the documented layout above).
# Per-turn keys (turn_delta:{id} / turn_graph:{id}) are enumerated from the
# turn_tree hash at delete time.
_STATIC_SUFFIXES = (
    "meta", "state", "lock", "events", "turns", "summaries", "answers",
    "context", "graph", "state_doc", "turn_tree", "tree_base",
)


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
            ["ZADD", _INDEX_KEY, str(now), cid],
            ["EXPIRE", _INDEX_KEY, str(_TTL_SECONDS)],
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


async def is_locked(conversation_id: str) -> bool:
    async with _client() as c:
        r = await c.get(f"/exists/{_k(conversation_id, 'lock')}")
        r.raise_for_status()
        return r.json().get("result") == 1


# ---------- Turn scope (branching) ----------
# The agent loop runs each turn inside `turn_scope(cid, turn_id, parent_turn_id)`.
# While the scope is active, every state read in this task tree (`get_state_doc`,
# and through it recall_state / the context assembly / the entity lookups) sees
# the PATH-SCOPED state document — the fold of `_apply_delta` over the deltas
# along root -> parent -> this turn — never sibling-branch deltas. Every
# `merge_state_doc` write is additionally recorded as a per-turn delta, and every
# SSE event is stamped with the turn ids so the frontend can build the tree live.
# A ContextVar (not a global) so concurrent turns of DIFFERENT conversations on
# one instance can't bleed into each other; it propagates into asyncio tasks and
# to_thread calls automatically. When no scope is active (legacy native loop,
# old conversations, hydrate), every read/write behaves exactly as before.

_TURN_SCOPE: ContextVar[tuple[str, str, str | None] | None] = ContextVar(
    "erre_turn_scope", default=None
)


@contextlib.contextmanager
def turn_scope(
    conversation_id: str, turn_id: str, parent_turn_id: str | None
) -> Iterator[None]:
    token = _TURN_SCOPE.set((conversation_id, turn_id, parent_turn_id))
    try:
        yield
    finally:
        _TURN_SCOPE.reset(token)


def _active_scope(conversation_id: str) -> tuple[str, str | None] | None:
    """(turn_id, parent_turn_id) when a scope is active for THIS conversation."""
    scope = _TURN_SCOPE.get()
    if scope and scope[0] == conversation_id:
        return scope[1], scope[2]
    return None


# ---------- Events (SSE) ----------


async def append_event(conversation_id: str, event: dict[str, Any]) -> None:
    # Stamp the active turn's tree coordinates onto every event so the frontend
    # can attach streaming events to the right branch card without a lookup.
    scope = _active_scope(conversation_id)
    if scope is not None and isinstance(event.get("data"), dict):
        event = {**event, "data": {**event["data"], "turn_id": scope[0], "parent_turn_id": scope[1]}}
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


def _self_ref(eid: str, rec: dict[str, Any]) -> dict[str, Any]:
    """The source pointer for an entity, derived from WHICH structured source
    named it (doc 09 §5 `source_refs`). A check_sanctions hit points back at its
    OpenSanctions record; an ICIJ-traversed node at its node_id; everything else
    (Sayari leads / resolved subjects / risk-path names) at its Sayari entity id.
    Deterministic — read straight off the registry record, never prose."""
    src = (rec.get("source") or "").strip().lower()
    if src == "check_sanctions" or rec.get("type") == "sanctions_entity":
        ref: dict[str, Any] = {"source": "opensanctions", "sanctions_id": eid}
        lists = rec.get("sanctions_lists") or []
        if lists:
            ref["lists"] = lists
        return ref
    if src == "icij":
        return {"source": "icij", "node_id": eid}
    return {"source": "sayari", "sayari_entity_id": eid}


def _dedupe_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order-preserving dedupe of source_refs by their identifying fields."""
    seen: set[tuple] = set()
    out: list[dict[str, Any]] = []
    for r in refs:
        if not isinstance(r, dict):
            continue
        key = (
            r.get("source"), r.get("sanctions_id"), r.get("sayari_entity_id"),
            r.get("node_id"), r.get("risk_factor"), r.get("leak"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _attach_source_refs(
    doc: dict[str, Any], entities: dict[str, dict[str, Any]]
) -> None:
    """Phase E (doc 09 §5/§E): give every registry entity a `source_refs` list so
    a finding can be RE-CITED on a later turn with its original source, without
    re-running the tool. Built DETERMINISTICALLY from the structured buckets
    (never prose, doc 09 §10):

      1. a self-ref from the source that named the entity (`_self_ref`),
      2. the OpenSanctions record behind any sanctions-ledger row it maps to, and
      3. the exact source_refs from any structured CLAIM that cited it — so the
         entity carries back the same ref the agent first used (e.g. a sayari
         risk_factor pointer), which is what makes the re-cite faithful.
    """
    refs_by_id: dict[str, list[dict[str, Any]]] = {eid: [] for eid in entities}

    def add(eid: Any, ref: dict[str, Any] | None) -> None:
        if eid in refs_by_id and ref:
            refs_by_id[eid].append(ref)

    for eid, rec in entities.items():
        add(eid, _self_ref(eid, rec))

    for row in doc.get("sanctions_adjudicated") or []:
        if not isinstance(row, dict):
            continue
        sid = row.get("sanctions_id")
        if not sid:
            continue
        ref: dict[str, Any] = {"source": "opensanctions", "sanctions_id": sid}
        if row.get("lists"):
            ref["lists"] = row.get("lists")
        add(sid, ref)
        for eid in row.get("entity_ids") or []:
            add(eid, ref)

    for claim in doc.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        crefs = [r for r in (claim.get("source_refs") or []) if isinstance(r, dict)]
        if not crefs:
            continue
        targets = list(claim.get("entity_ids") or [])
        for r in crefs:
            for cid in (r.get("node_id"), r.get("sanctions_id"), r.get("sayari_entity_id")):
                if cid and cid not in targets:
                    targets.append(cid)
        for eid in targets:
            for r in crefs:
                add(eid, r)

    for eid in entities:
        refs = _dedupe_refs(refs_by_id.get(eid) or [])
        if refs:
            entities[eid]["source_refs"] = refs


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

    # Phase E: attach deterministic source_refs (provenance) to every entity so a
    # finding can be re-cited with its source on a later turn (doc 09 §5/§E).
    _attach_source_refs(doc, entities)
    return entities


async def get_state_doc(conversation_id: str) -> dict[str, Any]:
    """Read the structured investigation state, or the empty default shape.

    Branching: when the calling task is inside `turn_scope` AND the scoped turn
    is registered in the turn tree, this returns the PATH-SCOPED doc instead —
    the fold of `_apply_delta` over the deltas along root -> this turn. Sibling
    branches are invisible by construction. Outside a scope (legacy native loop,
    hydrate, old conversations without tree keys) the merged doc is returned
    unchanged, which is also byte-identical to the fold for linear chains."""
    scope = _active_scope(conversation_id)
    if scope is not None:
        path_doc = await _path_state_doc_for_scope(conversation_id, scope[0])
        if path_doc is not None:
            return path_doc
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


def _apply_delta(doc: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """Pure, deterministic read-modify of the structured state with a turn's
    delta (no Redis, no I/O). This is the exact transformation `merge_state_doc`
    persists; it is factored out so the multi-turn eval harness (doc 09 §F) can
    persist `state_doc` between turns EXACTLY as production does, without Redis.

    - resolved_entities: upsert by normalized subject key (earliest
      first_seen_turn preserved, latest last_seen_turn advanced).
    - leads: append + dedupe by entity_id keeping the most recent from_turn,
      newest-first, capped to _MAX_LEADS by recency.
    - sanctions_adjudicated: append + dedupe by sanctions_id (most recent wins).
    - pinned_node_ids: order-preserving union.
    - turn_log: append.
    - claims: append + dedupe by text, cap by recency.
    - entities: recompute the unified registry projection from the merged buckets.
    """
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
    return doc


async def merge_state_doc(conversation_id: str, delta: dict[str, Any]) -> dict[str, Any]:
    """Read-modify-write the structured state with a turn's delta. Reads the
    current doc, applies the delta via the pure `_apply_delta`, writes it back,
    and refreshes the 24h TTL.

    Branching: inside `turn_scope`, the read above is already path-scoped, so
    the written doc is the ACTIVE PATH's doc (for a linear conversation that is
    identical to today's merged doc). The raw delta is ALSO appended to this
    turn's per-turn delta list, which is what makes the path fold reconstructable
    for every branch later."""
    # Read BEFORE recording the delta, so the path fold backing the read does
    # not already contain it (that would apply the delta twice).
    doc = await get_state_doc(conversation_id)
    doc = _apply_delta(doc, delta)
    scope = _active_scope(conversation_id)
    if scope is not None:
        await _append_turn_delta(conversation_id, scope[0], delta)
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


def _merge_node(prev: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Fold an incoming node onto an existing one: the incoming fields win
    (last-write freshest names/props), but `subject_ids` are UNIONED across
    turns (so a node a later turn re-attributes to a second subject keeps its
    original membership) and `introduced_turn_id` keeps the EARLIEST writer."""
    merged = {**prev, **new}
    seen: list[str] = []
    for sid in (prev.get("subject_ids") or []) + (new.get("subject_ids") or []):
        if sid and sid not in seen:
            seen.append(sid)
    merged["subject_ids"] = seen
    merged["introduced_turn_id"] = (
        prev.get("introduced_turn_id") or new.get("introduced_turn_id")
    )
    return merged


def merge_graph_pure(
    graph: dict[str, list],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, list]:
    """Pure graph union, deduped by node id / edge (source, type, target) triple.
    The exact transformation `merge_graph` persists; factored out so the path
    graph assembler and the evals union deltas the same way production does.
    Node subject-membership is unioned (see `_merge_node`) rather than
    last-write-wins so hull membership accumulates across turns."""
    by_id = {n["id"]: n for n in graph.get("nodes", [])}
    for n in nodes:
        prev = by_id.get(n["id"])
        by_id[n["id"]] = _merge_node(prev, n) if prev is not None else n
    edge_keys = {
        f"{e['source']}::{e['type']}::{e['target']}": e for e in graph.get("edges", [])
    }
    for e in edges:
        edge_keys[f"{e['source']}::{e['type']}::{e['target']}"] = e
    return {"nodes": list(by_id.values()), "edges": list(edge_keys.values())}


async def merge_graph(
    conversation_id: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, list]:
    """Merge new nodes/edges into the stored graph, deduped by id / triple."""
    graph = await get_graph(conversation_id)
    merged = merge_graph_pure(graph, nodes, edges)
    async with _client() as c:
        await c.post("/pipeline", json=[
            ["SET", _k(conversation_id, "graph"), json.dumps(merged, default=str), "EX", str(_TTL_SECONDS)],
        ])
    return merged


# ---------- Turn tree (branching, Stage 2a) ----------
# The conversation as a TREE of turns. Every turn gets a stable `turn_id` and an
# optional `parent_turn_id` (default: the previous head, which keeps linear
# conversations a single-branch tree). Forking = submitting a new user message
# whose parent_turn_id points at any prior turn. Three stores make a branch
# self-contained:
#
#   - the tree index (turn metadata, parent pointers, status, context_after),
#   - per-turn STATE deltas (the `_build_state_delta` projection + any mid-turn
#     named_ids merges), which `assemble_state_doc` folds along a path with the
#     SAME pure `_apply_delta` production uses, and
#   - per-turn GRAPH deltas, unioned along a path for time-travel.
#
# Everything is additive: old conversations have no tree keys and keep the
# merged-doc behavior; conversations with pre-tree turns get a one-time
# `tree_base` snapshot so the fold starts from the state those turns built.

_TREE_MAX_DEPTH = 500  # cycle guard for parent-pointer walks


def new_turn_id() -> str:
    return uuid.uuid4().hex[:12]


async def get_turn_tree(conversation_id: str) -> dict[str, dict[str, Any]]:
    """turn_id -> tree entry for every registered turn (empty for old/linear
    conversations that predate branching)."""
    async with _client() as c:
        r = await c.get(f"/hgetall/{_k(conversation_id, 'turn_tree')}")
        r.raise_for_status()
        raw = r.json().get("result") or []
    # Upstash REST returns HGETALL as a flat [field, value, field, value, ...].
    tree: dict[str, dict[str, Any]] = {}
    for i in range(0, len(raw) - 1, 2):
        try:
            entry = json.loads(raw[i + 1])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(entry, dict):
            tree[raw[i]] = entry
    return tree


async def _write_tree_entry(conversation_id: str, entry: dict[str, Any]) -> None:
    key = _k(conversation_id, "turn_tree")
    async with _client() as c:
        await c.post("/pipeline", json=[
            ["HSET", key, entry["turn_id"], json.dumps(entry, default=str)],
            ["EXPIRE", key, str(_TTL_SECONDS)],
        ])


async def update_turn_entry(
    conversation_id: str, turn_id: str, **fields: Any
) -> dict[str, Any] | None:
    """Merge fields into one tree entry (status flips, terminator metadata,
    context_after). Returns the updated entry, or None if the turn is unknown."""
    tree = await get_turn_tree(conversation_id)
    entry = tree.get(turn_id)
    if entry is None:
        return None
    entry = {**entry, **{k: v for k, v in fields.items() if v is not None}}
    await _write_tree_entry(conversation_id, entry)
    return entry


def latest_turn_id(tree: dict[str, dict[str, Any]]) -> str | None:
    """The default fork parent: the registered turn with the highest turn_index
    (the current head of a linear conversation)."""
    best: dict[str, Any] | None = None
    for entry in tree.values():
        if best is None or int(entry.get("turn_index", -1)) > int(best.get("turn_index", -1)):
            best = entry
    return best.get("turn_id") if best else None


def path_to(tree: dict[str, dict[str, Any]], turn_id: str) -> list[str]:
    """Turn ids along root -> ... -> turn_id, following parent pointers. Ids
    missing from the tree terminate the walk (the segment before the tree
    existed is covered by the tree_base snapshot)."""
    rev: list[str] = []
    cur: str | None = turn_id
    seen: set[str] = set()
    while cur is not None and cur in tree and cur not in seen and len(rev) < _TREE_MAX_DEPTH:
        seen.add(cur)
        rev.append(cur)
        cur = tree[cur].get("parent_turn_id")
    rev.reverse()
    return rev


async def register_turn(
    conversation_id: str,
    turn_index: int,
    user_message: str,
    parent_turn_id: str | None = None,
) -> dict[str, Any]:
    """Create the tree entry for a new turn BEFORE it runs (status 'running'),
    so the submit response and the live SSE stream can carry its coordinates.

    - parent_turn_id omitted -> the current head (latest registered turn), which
      preserves linear behavior exactly. Explicit parent = a fork.
    - Raises ValueError on an unknown parent_turn_id (the API maps it to a 400).
    - First tree-aware turn on a conversation that already has pre-tree turns
      takes a one-time `tree_base` snapshot of the merged state/graph/context,
      so path folds include everything those turns built."""
    tree = await get_turn_tree(conversation_id)
    if parent_turn_id is not None and parent_turn_id not in tree:
        raise ValueError(f"unknown parent_turn_id: {parent_turn_id}")
    if parent_turn_id is None:
        parent_turn_id = latest_turn_id(tree)

    if not tree and turn_index > 0:
        await _snapshot_tree_base(conversation_id)

    entry = {
        "turn_id": new_turn_id(),
        "parent_turn_id": parent_turn_id,
        "turn_index": turn_index,
        "user_message": user_message,
        "status": "running",
        "created_at": int(time.time()),
    }
    await _write_tree_entry(conversation_id, entry)
    return entry


# --- tree_base: the pre-branching prefix of an existing conversation ---


def _empty_tree_base() -> dict[str, Any]:
    return {"state_doc": _empty_state_doc(), "graph": {"nodes": [], "edges": []}, "context": ""}


async def _snapshot_tree_base(conversation_id: str) -> None:
    """Freeze the merged state/graph/context built by pre-tree turns as the
    fold base. Taken once, under the turn lock, before the first tree turn."""
    base = {
        "state_doc": await get_state_doc(conversation_id),
        "graph": await get_graph(conversation_id),
        "context": await get_context(conversation_id),
    }
    async with _client() as c:
        await c.post("/pipeline", json=[
            ["SET", _k(conversation_id, "tree_base"), json.dumps(base, default=str), "EX", str(_TTL_SECONDS)],
        ])


async def _get_tree_base(conversation_id: str) -> dict[str, Any]:
    async with _client() as c:
        r = await c.get(f"/get/{_k(conversation_id, 'tree_base')}")
        r.raise_for_status()
        raw = r.json().get("result")
    if not raw:
        return _empty_tree_base()
    try:
        base = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return _empty_tree_base()
    out = _empty_tree_base()
    if isinstance(base, dict):
        for k in out:
            if isinstance(base.get(k), (dict, str)):
                out[k] = base[k]
    return out


# --- Per-turn state deltas + the path-state assembler ---


async def _append_turn_delta(
    conversation_id: str, turn_id: str, delta: dict[str, Any]
) -> None:
    key = _k(conversation_id, f"turn_delta:{turn_id}")
    async with _client() as c:
        await c.post("/pipeline", json=[
            ["RPUSH", key, json.dumps(delta, default=str)],
            ["EXPIRE", key, str(_TTL_SECONDS)],
        ])


async def read_turn_deltas(conversation_id: str, turn_id: str) -> list[dict[str, Any]]:
    async with _client() as c:
        r = await c.get(f"/lrange/{_k(conversation_id, f'turn_delta:{turn_id}')}/0/-1")
        r.raise_for_status()
        raw = r.json().get("result") or []
    out: list[dict[str, Any]] = []
    for s in raw:
        try:
            d = json.loads(s)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def _normalize_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Every key present + the registry projection recomputed — the same
    normalization `get_state_doc` applies to a doc read from Redis."""
    base = _empty_state_doc()
    if isinstance(doc, dict):
        for k in base:
            if k in doc and doc[k] is not None:
                base[k] = doc[k]
    base["entities"] = _project_entities(base)
    return base


def assemble_state_doc(
    base_doc: dict[str, Any],
    deltas: list[dict[str, Any]],
) -> dict[str, Any]:
    """PURE path-state assembler: fold `_apply_delta` (the exact read-modify
    core `merge_state_doc` persists) over a path's deltas, starting from a deep
    copy of the base. Deterministic, no I/O — the branching evals exercise this
    directly, and `get_path_state_doc` is just the Redis wrapper around it."""
    doc = _normalize_doc(json.loads(json.dumps(base_doc, default=str)))
    for delta in deltas:
        doc = _apply_delta(doc, delta)
    return _normalize_doc(doc)


# Folded docs for COMPLETED turns are immutable (a finished turn's delta list
# never grows again), so cache them per (conversation, turn). The current,
# still-running turn's deltas are re-read and folded on top fresh each time.
_PATH_FOLD_CACHE: dict[tuple[str, str], str] = {}
_PATH_FOLD_CACHE_MAX = 64


def _cache_fold(key: tuple[str, str], doc: dict[str, Any]) -> None:
    if len(_PATH_FOLD_CACHE) >= _PATH_FOLD_CACHE_MAX:
        _PATH_FOLD_CACHE.pop(next(iter(_PATH_FOLD_CACHE)))
    _PATH_FOLD_CACHE[key] = json.dumps(doc, default=str)


async def get_path_state_doc(
    conversation_id: str,
    turn_id: str,
    tree: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """The state_doc as seen along root -> turn_id: tree_base + the fold of that
    path's per-turn deltas, and NOTHING from sibling branches. None when the
    turn is not in the tree (legacy conversations fall back to the merged doc)."""
    if tree is None:
        tree = await get_turn_tree(conversation_id)
    if turn_id not in tree:
        return None
    path = path_to(tree, turn_id)

    # Longest cached completed prefix of the path, then fold forward from there.
    doc: dict[str, Any] | None = None
    start = 0
    for i in range(len(path) - 1, -1, -1):
        cached = _PATH_FOLD_CACHE.get((conversation_id, path[i]))
        if cached is not None:
            doc = json.loads(cached)
            start = i + 1
            break
    if doc is None:
        base = await _get_tree_base(conversation_id)
        doc = assemble_state_doc(base.get("state_doc") or {}, [])

    for tid in path[start:]:
        deltas = await read_turn_deltas(conversation_id, tid)
        for delta in deltas:
            doc = _apply_delta(doc, delta)
        if (tree.get(tid) or {}).get("status") == "done":
            _cache_fold((conversation_id, tid), doc)
    return _normalize_doc(doc)


async def _path_state_doc_for_scope(
    conversation_id: str, turn_id: str
) -> dict[str, Any] | None:
    """Scope hook used by `get_state_doc`: best-effort path assembly; any
    failure falls back to the merged doc rather than breaking a live turn."""
    try:
        return await get_path_state_doc(conversation_id, turn_id)
    except Exception:  # pragma: no cover - defensive: never fail a turn on this
        log.warning("path_state_doc failed; falling back to merged doc", exc_info=True)
        return None


# --- Per-turn graph deltas + the path graph (time-travel payload) ---


async def record_turn_graph_delta(
    conversation_id: str,
    turn_id: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    """Persist the turn's graph delta first-class (the nodes/edges this turn
    added), keyed by turn_id. Written once at finalize."""
    delta = {"nodes": nodes, "edges": edges}
    key = _k(conversation_id, f"turn_graph:{turn_id}")
    async with _client() as c:
        await c.post("/pipeline", json=[
            ["SET", key, json.dumps(delta, default=str), "EX", str(_TTL_SECONDS)],
        ])


async def read_turn_graph_delta(
    conversation_id: str, turn_id: str
) -> dict[str, list]:
    async with _client() as c:
        r = await c.get(f"/get/{_k(conversation_id, f'turn_graph:{turn_id}')}")
        r.raise_for_status()
        raw = r.json().get("result")
    if not raw:
        return {"nodes": [], "edges": []}
    try:
        delta = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"nodes": [], "edges": []}
    if not isinstance(delta, dict):
        return {"nodes": [], "edges": []}
    return {"nodes": delta.get("nodes") or [], "edges": delta.get("edges") or []}


def accumulate_path_graph(
    base_graph: dict[str, list],
    deltas: list[dict[str, list]],
) -> dict[str, list]:
    """PURE path-graph accumulator: union the per-turn graph deltas along a path
    onto the base, with the same dedupe production's merge_graph uses."""
    graph = {
        "nodes": list(base_graph.get("nodes") or []),
        "edges": list(base_graph.get("edges") or []),
    }
    for d in deltas:
        graph = merge_graph_pure(graph, d.get("nodes") or [], d.get("edges") or [])
    return graph


async def get_path_graph(
    conversation_id: str,
    turn_id: str,
    tree: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """The accumulated evidence graph along root -> turn_id (the time-travel
    payload), plus the turn's OWN delta so the frontend can pulse new-this-turn
    nodes and dim inherited ones. None when the turn is not in the tree."""
    if tree is None:
        tree = await get_turn_tree(conversation_id)
    if turn_id not in tree:
        return None
    path = path_to(tree, turn_id)
    base = await _get_tree_base(conversation_id)
    deltas = [await read_turn_graph_delta(conversation_id, tid) for tid in path]
    graph = accumulate_path_graph(base.get("graph") or {"nodes": [], "edges": []}, deltas)
    own = deltas[-1] if deltas else {"nodes": [], "edges": []}
    return {
        "turn_id": turn_id,
        "path": path,
        "graph": graph,
        "turn_delta": own,
    }


async def resolve_prior_context(
    conversation_id: str, parent_turn_id: str | None
) -> str:
    """The prose digest a turn should start from: the parent turn's stored
    `context_after` when the parent is in the tree (path-scoped narrative), the
    tree_base context for a first tree turn on an old conversation, else the
    legacy global context string."""
    tree = await get_turn_tree(conversation_id)
    if parent_turn_id is not None and parent_turn_id in tree:
        ctx = tree[parent_turn_id].get("context_after")
        if isinstance(ctx, str):
            return ctx
    if tree and parent_turn_id is None:
        base = await _get_tree_base(conversation_id)
        return base.get("context") or ""
    return await get_context(conversation_id)


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
            # Keep the recents index in step: re-score by updated_at and refresh
            # its TTL, the same write discipline as every other key.
            ["ZADD", _INDEX_KEY, str(meta["updated_at"]), conversation_id],
            ["EXPIRE", _INDEX_KEY, str(_TTL_SECONDS)],
        ])
    return meta


# ---------- Conversation index (list / delete) ----------


def assemble_conversation_list(
    ids: list[str], raw: list[Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    """PURE assembler for the conversation list: pair each index member with
    its fetched (meta_raw, state) and split live items from dead ids.

    `raw` is the flat pipeline result [meta_0, state_0, meta_1, state_1, ...]
    in `ids` order. An id whose meta is missing (per-key 24h TTL expired before
    the index entry) or unparseable is DEAD: it goes in the second list so the
    caller can lazily ZREM it. Factored pure so the deterministic evals can pin
    the filtering without Redis."""
    items: list[dict[str, Any]] = []
    dead: list[str] = []
    for i, cid in enumerate(ids):
        meta_raw = raw[2 * i] if 2 * i < len(raw) else None
        state = raw[2 * i + 1] if 2 * i + 1 < len(raw) else None
        if not meta_raw:
            dead.append(cid)
            continue
        try:
            meta = json.loads(meta_raw)
        except (TypeError, json.JSONDecodeError):
            dead.append(cid)
            continue
        if not isinstance(meta, dict):
            dead.append(cid)
            continue
        items.append({
            "conversation_id": cid,
            "title": meta.get("title") or "New investigation",
            "created_at": meta.get("created_at"),
            "updated_at": meta.get("updated_at"),
            "turn_count": int(meta.get("turn_count", 0)),
            "state": state or "idle",
        })
    return items, dead


async def list_conversations(limit: int = 50) -> list[dict[str, Any]]:
    """Most-recently-updated conversations from the ZSET index, newest first.

    One pipeline reads the index page, a second fetches every member's
    meta + state; ids whose meta expired are filtered out AND lazily removed
    from the index (the per-key TTL is the source of truth, the index is just
    a pointer set)."""
    async with _client() as c:
        r = await c.post("/pipeline", json=[
            ["ZREVRANGE", _INDEX_KEY, "0", str(max(limit - 1, 0))],
        ])
        r.raise_for_status()
        first = r.json()
        ids = (first[0].get("result") or []) if isinstance(first, list) and first else []
        if not ids:
            return []
        cmds: list[list[str]] = []
        for cid in ids:
            cmds.append(["GET", _k(cid, "meta")])
            cmds.append(["GET", _k(cid, "state")])
        r2 = await c.post("/pipeline", json=cmds)
        r2.raise_for_status()
        raw = [row.get("result") for row in r2.json()]
        items, dead = assemble_conversation_list(ids, raw)
        if dead:
            await c.post("/pipeline", json=[["ZREM", _INDEX_KEY, *dead]])
    return items


async def delete_conversation(conversation_id: str) -> None:
    """Delete the conversation's whole key family and drop it from the index.

    The per-turn keys (turn_delta:{id} / turn_graph:{id}) are enumerated via
    the turn_tree hash BEFORE the hash itself is deleted. The caller (the API
    layer) is responsible for refusing deletes while a turn is running."""
    tree = await get_turn_tree(conversation_id)
    keys = [_k(conversation_id, s) for s in _STATIC_SUFFIXES]
    for tid in tree:
        keys.append(_k(conversation_id, f"turn_delta:{tid}"))
        keys.append(_k(conversation_id, f"turn_graph:{tid}"))
    async with _client() as c:
        await c.post("/pipeline", json=[
            ["DEL", *keys],
            ["ZREM", _INDEX_KEY, conversation_id],
        ])
    # Drop any cached path folds for this conversation (stale otherwise).
    for key in [k for k in _PATH_FOLD_CACHE if k[0] == conversation_id]:
        _PATH_FOLD_CACHE.pop(key, None)


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
    tree = await get_turn_tree(conversation_id)
    return {
        "conversation_id": conversation_id,
        "meta": meta,
        "state": state,
        "graph": graph,
        "turns": turns,
        "summaries": summaries,
        "answers": answers,
        "state_doc": state_doc,
        # Branching (additive): the turn tree, sorted by turn_index, so a
        # reloaded page can rebuild the canvas. Empty for old conversations.
        "tree": sorted(tree.values(), key=lambda e: int(e.get("turn_index", 0))),
    }


async def ping() -> bool:
    try:
        async with _client() as c:
            r = await c.get("/ping")
            return r.status_code == 200
    except Exception:
        return False
