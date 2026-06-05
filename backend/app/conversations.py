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


def _empty_state_doc() -> dict[str, Any]:
    """The empty default: every key present with empty containers.

    `named_ids` (id -> {label, type, sanctioned, pep, countries}) caches names
    resolved for risk-path nodes this conversation, so a later turn naming the
    SAME multi-hop node reuses the cached label instead of re-spending a Sayari
    entity_summary call — the bounded resolve compounds across turns."""
    return {
        "resolved_entities": {},
        "leads": [],
        "sanctions_adjudicated": [],
        "pinned_node_ids": [],
        "turn_log": [],
        "named_ids": {},
    }


def _normalize_subject(subject: str | None) -> str:
    """The key for resolved_entities: lowercased, trimmed."""
    return (subject or "").strip().lower()


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
