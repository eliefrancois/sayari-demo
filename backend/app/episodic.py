"""L2 episodic memory: fuzzy semantic recall of old turns via Upstash Vector.

One structured episode per turn, built deterministically from the turn's
structured outputs (never prose), for the "what did we find about X a while ago"
question. Flag-gated and fully no-op when disabled, so the live demo is untouched
until provisioning. Upserts raw text so Upstash's hosted model embeds it server
side, with no separate embedding key to manage.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.config import get_settings

log = logging.getLogger("erre.episodic")

# Per-namespace episode cap (doc 09 §7 compaction). Upstash holds far more than
# this; the cap is a guard so a single conversation's namespace stays bounded.
_NAMESPACE_CAP = 200

# Park et al. (Generative Agents) weighted ranking: a memory's retrieval score is
# a weighted sum of similarity (relevance), recency, and salience (importance) —
# NOT raw cosine similarity (doc 09 §2 "Recency x importance x relevance"). These
# weights are the knob; similarity leads, recency and salience re-rank ties.
_W_SIMILARITY = 0.5
_W_RECENCY = 0.3
_W_SALIENCE = 0.2

# How many raw candidates to pull before re-ranking. We over-fetch on similarity,
# then re-rank by the weighted score and return the requested top_k.
_OVERFETCH = 4

_index: Any = None
_index_creds: tuple[str, str] | None = None


def is_enabled() -> bool:
    """True ONLY when the feature flag is on AND both Upstash Vector creds are
    present. The single gate every public function checks — when this is False,
    the whole subsystem is a no-op and the existing flow is untouched."""
    s = get_settings()
    return bool(
        s.episodic_memory_enabled
        and s.upstash_vector_rest_url
        and s.upstash_vector_rest_token
    )


def _get_index() -> Any:
    """Lazily build (and cache) the Upstash Vector client from creds. Returns
    None if creds are missing or the SDK import fails. Rebuilds if the creds
    changed (e.g. settings cache cleared in a test)."""
    global _index, _index_creds
    s = get_settings()
    creds = (s.upstash_vector_rest_url, s.upstash_vector_rest_token)
    if not all(creds):
        return None
    if _index is not None and _index_creds == creds:
        return _index
    try:
        from upstash_vector import Index  # lazy import; only when enabled
    except Exception:  # pragma: no cover - import guard
        log.warning("upstash_vector import failed; episodic memory disabled")
        return None
    _index = Index(url=creds[0], token=creds[1])
    _index_creds = creds
    return _index


# --- Episode construction (pure, deterministic, structured-only) -----------


def _salience(delta: dict[str, Any]) -> float:
    """Seed an episode's importance from signals already in the structured
    delta (doc 09 §7: 'salience seeded from signals — sanctioned/PEP -> high').
    Pure: no LLM, no network. Range [0, 1]."""
    score = 0.3  # a turn happened at all
    sanc = delta.get("sanctions_adjudicated") or []
    if any(r.get("verdict") == "confirmed" for r in sanc if isinstance(r, dict)):
        score += 0.4
    elif sanc:  # dismissed-only collisions still matter, just less
        score += 0.2
    resolved = (delta.get("resolved_entities") or {}).values()
    if any(isinstance(r, dict) and r.get("sanctioned") for r in resolved):
        score += 0.3
    if any(isinstance(r, dict) and r.get("pep") for r in resolved):
        score += 0.1
    if delta.get("claims"):
        score += 0.1
    return min(1.0, round(score, 3))


def build_episode(
    conversation_id: str,
    turn_index: int,
    intent: str | None,
    delta: dict[str, Any],
    tools_used: list[str],
    turn_id: str | None = None,
    parent_turn_id: str | None = None,
) -> dict[str, Any]:
    """Build ONE structured episode from the turn's STRUCTURED outputs (the same
    `_build_state_delta` projection L3 persists) — never the prose answer. Pure
    and deterministic, so it is unit-testable and carries no hallucination risk.

    Returns a dict with the episode fields (doc 09 Phase D): conversation_id,
    turn, primary subject(s), key findings (structured claim text), entities
    touched (registry ids), tools used, timestamp, a compact `text` blob built
    deterministically from those fields (what gets embedded), and `salience`."""
    # Primary subject(s): the resolved subjects this turn (the registry-keyed
    # focus), capped tiny. Falls back to the turn_log subject.
    resolved = delta.get("resolved_entities") or {}
    subjects: list[str] = []
    for rec in resolved.values():
        if isinstance(rec, dict) and rec.get("label"):
            subjects.append(str(rec["label"]))
    if not subjects:
        for row in delta.get("turn_log") or []:
            if isinstance(row, dict) and row.get("subject"):
                subjects.append(str(row["subject"]))
    # De-dupe, keep order, cap.
    seen: set[str] = set()
    subjects = [s for s in subjects if not (s in seen or seen.add(s))][:4]

    # Entities touched: the registry ids this turn deposited (resolved subjects'
    # entity_ids, the referenced-but-not-traversed named_ids, and the sanctions
    # entity ids). Capped so the metadata stays small.
    entity_ids: list[str] = []
    eseen: set[str] = set()

    def _add_id(eid: Any) -> None:
        if eid and isinstance(eid, str) and eid not in eseen:
            eseen.add(eid)
            entity_ids.append(eid)

    for rec in resolved.values():
        if isinstance(rec, dict):
            _add_id(rec.get("entity_id"))
    for nid in (delta.get("named_ids") or {}):
        _add_id(nid)
    for row in delta.get("sanctions_adjudicated") or []:
        if isinstance(row, dict):
            _add_id(row.get("sanctions_id"))
    entity_ids = entity_ids[:25]

    # Key findings: structured claim text (a validated schema field, NOT prose
    # narration) plus sanctions verdicts by name. Capped + terse.
    findings: list[str] = []
    for c in (delta.get("claims") or [])[:5]:
        if isinstance(c, dict) and c.get("text"):
            findings.append(str(c["text"]))
    sanc_bits: list[str] = []
    for row in (delta.get("sanctions_adjudicated") or [])[:6]:
        if not isinstance(row, dict):
            continue
        nm = row.get("matched_name") or row.get("sanctions_id")
        verdict = row.get("verdict") or "?"
        if nm:
            sanc_bits.append(f"{nm} ({verdict})")

    tools = sorted(set(tools_used or []))

    # The compact text blob that gets embedded — built DETERMINISTICALLY from the
    # structured fields above. Stable wording so re-embedding the same turn is
    # idempotent. This is what a fuzzy "what did we find about X" matches on.
    text_parts: list[str] = []
    if subjects:
        text_parts.append("Subjects: " + ", ".join(subjects))
    if intent:
        text_parts.append(f"Intent: {intent}")
    if findings:
        text_parts.append("Findings: " + " | ".join(findings))
    if sanc_bits:
        text_parts.append("Sanctions: " + "; ".join(sanc_bits))
    if tools:
        text_parts.append("Tools: " + ", ".join(tools))
    text = f"Turn {turn_index}. " + ". ".join(text_parts) if text_parts else f"Turn {turn_index}."

    return {
        "conversation_id": conversation_id,
        "turn": turn_index,
        # Branching (Stage 2a): tree coordinates ride on every episode so a
        # later path-aware episodic phase can filter by branch. Deliberately
        # NOT used for filtering yet — this just keeps the store unpoisoned.
        "turn_id": turn_id,
        "parent_turn_id": parent_turn_id,
        "intent": intent,
        "subjects": subjects,
        "findings": findings,
        "sanctions": sanc_bits,
        "entity_ids": entity_ids,
        "tools_used": tools,
        "timestamp": int(time.time()),
        "salience": _salience(delta),
        "text": text,
    }


# --- Write path (finalize) -------------------------------------------------


async def write_episode(episode: dict[str, Any]) -> bool:
    """ADD-only episode write at finalize (doc 09 §3.3 write contract). No-op
    (returns False) when episodic memory is disabled, so the finalize path is
    untouched on the live demo. Namespaced by conversation so recall is scoped to
    the same investigation. Best-effort: a vector outage logs and returns False
    rather than failing the turn."""
    if not is_enabled():
        return False
    index = _get_index()
    if index is None:
        return False
    cid = episode.get("conversation_id")
    turn = episode.get("turn")
    if not cid or turn is None:
        return False

    vector_id = f"{cid}:{turn}"
    # Metadata stays SMALL (doc 09 §6 budget discipline): scalars + short lists.
    metadata = {
        "conversation_id": cid,
        "turn": turn,
        "turn_id": episode.get("turn_id"),
        "parent_turn_id": episode.get("parent_turn_id"),
        "intent": episode.get("intent"),
        "subjects": episode.get("subjects") or [],
        "entity_ids": episode.get("entity_ids") or [],
        "tools_used": episode.get("tools_used") or [],
        "salience": episode.get("salience") or 0.0,
        "timestamp": episode.get("timestamp") or int(time.time()),
        # Carry the human-readable findings so recall can surface them without a
        # second fetch. Kept short by build_episode.
        "findings": episode.get("findings") or [],
        "sanctions": episode.get("sanctions") or [],
    }
    text = episode.get("text") or ""

    def _do_upsert() -> bool:
        # Tuple form (id, data, metadata): `data` is RAW TEXT, embedded by the
        # index's hosted model. Namespaced to this conversation.
        index.upsert(vectors=[(vector_id, text, metadata)], namespace=str(cid))
        return True

    try:
        return await asyncio.to_thread(_do_upsert)
    except Exception as e:  # never let a vector hiccup break a turn
        log.warning("episode_write_failed", extra={"error": str(e), "cid": cid, "turn": turn})
        return False


# --- Read path (recall_memory tool) ----------------------------------------


def _rerank(matches: list[Any], top_k: int) -> list[dict[str, Any]]:
    """Re-rank similarity matches by the Park et al. weighted score
    (similarity x recency x salience), NOT raw cosine. Returns the top_k as
    plain dicts the tool can serialize."""
    rows: list[dict[str, Any]] = []
    turns = [int((getattr(m, "metadata", None) or {}).get("turn") or 0) for m in matches]
    max_turn = max(turns) if turns else 0
    for m in matches:
        meta = getattr(m, "metadata", None) or {}
        sim = float(getattr(m, "score", 0.0) or 0.0)
        turn = int(meta.get("turn") or 0)
        recency = (turn / max_turn) if max_turn else 0.0
        salience = float(meta.get("salience") or 0.0)
        combined = _W_SIMILARITY * sim + _W_RECENCY * recency + _W_SALIENCE * salience
        rows.append({
            "turn": turn,
            "subjects": meta.get("subjects") or [],
            "findings": meta.get("findings") or [],
            "sanctions": meta.get("sanctions") or [],
            "entity_ids": meta.get("entity_ids") or [],
            "tools_used": meta.get("tools_used") or [],
            "intent": meta.get("intent"),
            "similarity": round(sim, 4),
            "recency": round(recency, 4),
            "salience": round(salience, 4),
            "score": round(combined, 4),
        })
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows[:top_k]


async def query_episodes(
    conversation_id: str | None,
    query: str,
    top_k: int = 5,
    sanctioned: bool | None = None,
) -> dict[str, Any]:
    """Fuzzy semantic recall over THIS conversation's episodes, ranked by
    similarity x recency x salience (doc 09 §3.3 read contract). Returns a
    structured result the recall_memory tool serializes.

    Contract when disabled: returns `{"configured": False, ...}` so the tool can
    tell the agent episodic memory is off and to fall back to recall_state. Never
    raises — a vector outage degrades to an empty, configured-but-errored result."""
    if not is_enabled():
        return {
            "configured": False,
            "episodes": [],
            "count": 0,
            "note": (
                "Episodic memory is not configured. Use recall_state for exact "
                "recall of this conversation's structured findings instead."
            ),
        }
    if not conversation_id:
        return {"configured": True, "episodes": [], "count": 0,
                "note": "no conversation context available for recall."}
    index = _get_index()
    if index is None:
        return {"configured": False, "episodes": [], "count": 0,
                "note": "episodic memory client unavailable."}

    try:
        k = max(1, int(top_k))
    except (TypeError, ValueError):
        k = 5
    overfetch = min(_NAMESPACE_CAP, k * _OVERFETCH)

    filter_str = ""
    if sanctioned is True:
        # Episodes that touched at least one confirmed sanction. Upstash metadata
        # filtering can't express "non-empty list" directly, so we filter loosely
        # on the presence of a sanctions bit via the salience floor instead and
        # apply the strict check after re-rank.
        filter_str = "salience >= 0.6"

    def _do_query() -> list[Any]:
        return index.query(
            data=query,
            top_k=overfetch,
            include_metadata=True,
            namespace=str(conversation_id),
            filter=filter_str,
        )

    try:
        matches = await asyncio.to_thread(_do_query)
    except Exception as e:
        log.warning("episode_query_failed", extra={"error": str(e), "cid": conversation_id})
        return {"configured": True, "episodes": [], "count": 0,
                "note": f"episodic query failed: {e}"}

    episodes = _rerank(list(matches or []), k)
    if sanctioned is True:
        episodes = [e for e in episodes if e.get("sanctions")]
    return {
        "configured": True,
        "episodes": episodes,
        "count": len(episodes),
        "ranking_note": (
            "Fuzzy recall of OLDER turns, ranked by similarity x recency x "
            "salience (not raw similarity). For EXACT/complete enumeration of "
            "this conversation's findings, use recall_state instead."
        ),
    }


def reset_for_test() -> None:
    """Drop the cached client (tests that flip env vars call this)."""
    global _index, _index_creds
    _index = None
    _index_creds = None
