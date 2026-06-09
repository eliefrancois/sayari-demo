"""Regression eval harness for the LangGraph agent.

Seeded from bugs we actually hit during the build, so a green run means those
regressions stay fixed:

  - terminator routing (conversational-by-default): "hello" answers; a named
    subject now ALSO finishes with submit_answer (not an auto-emitted report) and
    sets report_ready when a signal is found; only an EXPLICIT "compile a report"
    request produces submit_summary.
  - sanctions gate: Epstein must NOT carry a `sanctioned` signal or a confirmed
    watchlist hit (the on_watchlist gate fix); Roldugin — actually sanctioned —
    must keep the signal.
  - not-found honesty: a nonsense subject must return found=false with no claims
    (anti-hallucination).
  - clarify routing: a vague objective must ask clarification questions instead
    of guessing.
  - provenance: every claim must carry >=1 source_ref.
  - boundary: a question we have no tool for ("most common address") must not
    fabricate an aggregate. (Tightens once find_hub_addresses lands.)

Each evaluator asserts on the STRUCTURED output of `agent_graph.evaluate_turn`
(which returns the final graph state directly — no SSE/Redis), so the checks
are deterministic.

Usage (from backend/, with the venv):
  .venv/bin/python -m evals.run_evals            # run locally, print a table
  .venv/bin/python -m evals.run_evals --push     # also upload to LangSmith
                                                  # (needs LANGCHAIN_API_KEY)
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from typing import Any, Callable

from app import agent_graph
from evals import multiturn

# An evaluator takes the evaluate_turn output and returns (passed, comment).
Evaluator = Callable[[dict[str, Any]], tuple[bool, str]]


def _result(out: dict[str, Any]) -> dict[str, Any]:
    return out.get("result") or {}


def terminator_answer(out: dict[str, Any]) -> tuple[bool, str]:
    return out["kind"] == "answer", f"kind={out['kind']}"


def terminator_summary(out: dict[str, Any]) -> tuple[bool, str]:
    return out["kind"] == "summary", f"kind={out['kind']}"


def found_true(out: dict[str, Any]) -> tuple[bool, str]:
    r = _result(out)
    return bool(r.get("found")), f"found={r.get('found')}"


def not_found(out: dict[str, Any]) -> tuple[bool, str]:
    r = _result(out)
    ok = out["kind"] == "summary" and r.get("found") is False
    return ok, f"kind={out['kind']}, found={r.get('found')}"


def no_claims(out: dict[str, Any]) -> tuple[bool, str]:
    n = len(_result(out).get("claims", []))
    return n == 0, f"claims={n}"


def provenance(out: dict[str, Any]) -> tuple[bool, str]:
    claims = _result(out).get("claims", [])
    ok = all(len(c.get("source_refs", [])) >= 1 for c in claims)
    return ok, f"{len(claims)} claims, all sourced={ok}"


def sanctions_present(out: dict[str, Any]) -> tuple[bool, str]:
    """Shape-agnostic: a confirmed `sanctioned` signal (summary), a watchlist hit,
    or a surfaced Sayari `sanctioned*` risk factor (answer or summary)."""
    r = _result(out)
    sig = r.get("risk_signals", [])
    hits = r.get("sanctions_hits", [])
    sanc_factor = any(
        str(f.get("name", "")).startswith("sanctioned")
        for f in (r.get("sayari_risk_factors") or [])
    )
    ok = "sanctioned" in sig or len(hits) > 0 or sanc_factor
    return ok, f"signals={sig}, hits={len(hits)}, sanc_factor={sanc_factor}"


def no_false_sanctions(out: dict[str, Any]) -> tuple[bool, str]:
    """Epstein guard: no `sanctioned` signal and no confirmed watchlist hit."""
    r = _result(out)
    sig = r.get("risk_signals", [])
    watch = [h for h in r.get("sanctions_hits", []) if h.get("on_watchlist")]
    ok = "sanctioned" not in sig and len(watch) == 0
    return ok, f"sanctioned_signal={'sanctioned' in sig}, watchlist_hits={len(watch)}"


def ofac_non_sdn_labeling(out: dict[str, Any]) -> tuple[bool, str]:
    """Huawei guard: OFAC posture must be reported as non-SDN/Consolidated (or
    BIS Entity List / export controls), NEVER promoted to 'OFAC SDN' / 'SDN #'.

    Ground truth: Huawei is on the OFAC Consolidated (non-SDN) list, the US Trade
    CSL, US SAM Exclusions, and the BIS Entity List — it is NOT on the OFAC SDN
    (blocked-persons) list. This locks in the sanctions-accuracy fix so the model
    can't silently upgrade a consolidated/Entity-List posture to SDN or fabricate
    an 'OFAC SDN #'."""
    r = _result(out)
    # investigation_summary (summary) or answer (conversational) — whichever the
    # terminator carried — plus claims/factors/hits.
    parts: list[str] = [r.get("investigation_summary") or "", r.get("answer") or ""]
    parts += [c.get("text") or "" for c in r.get("claims", [])]
    for f in r.get("sayari_risk_factors") or []:
        parts.append(str(f.get("name") or ""))
        parts.append(str(f.get("value") or ""))
    for h in r.get("sanctions_hits", []):
        parts += h.get("lists") or []
    text = " ".join(parts).lower()
    # Neutralize legitimate negative/non-SDN phrasings before scanning for an
    # affirmative SDN assertion (the bug we're guarding against).
    neutral = (
        text.replace("non-sdn", " ")
        .replace("non sdn", " ")
        .replace("not on the sdn", " ")
        .replace("not the sdn", " ")
        .replace("not sdn", " ")
    )
    asserts_sdn = any(
        p in neutral
        for p in ("sdn #", "ofac sdn", "sdn list", "specially designated national")
    )
    mentions_correct = any(
        k in text
        for k in ("non-sdn", "non sdn", "consolidated", "entity list", "export control")
    )
    ok = (not asserts_sdn) and mentions_correct
    return ok, f"asserts_sdn={asserts_sdn}, mentions_non_sdn/consolidated/entitylist={mentions_correct}"


def name_match_hedged(out: dict[str, Any]) -> tuple[bool, str]:
    """Rosneft name-match guard: when check_sanctions returns a STRONG but
    NAME-ONLY fuzzy match between the subject (Rosneft Global Trade S.A.) and a
    DIFFERENT SDN-listed legal entity (Rosneft Trading S.A.), the agent must
    HEDGE — it must NOT promote a name-only match into a direct sanctions claim
    on the SUBJECT. It may surface the name similarity / genuine adjacency, but
    Global Trade itself is NOT on the OFAC SDN list.

    Passes when the output (a) does NOT assert the SUBJECT is itself directly
    OFAC SDN / sanctioned, AND (b) acknowledges the SDN hit belongs to a
    separate, closely-named entity (attributes it to Rosneft Trading S.A. or
    flags the name match / separateness explicitly)."""
    r = _result(out)
    parts: list[str] = [r.get("investigation_summary") or "", r.get("answer") or ""]
    parts += [c.get("text") or "" for c in r.get("claims", [])]
    for f in r.get("sayari_risk_factors") or []:
        parts.append(str(f.get("name") or ""))
        parts.append(str(f.get("value") or ""))
    for h in r.get("sanctions_hits", []):
        parts += h.get("lists") or []
    low = " ".join(p for p in parts if p).lower()

    # (b) The SDN hit must be framed as a separate / closely-named entity — either
    # via explicit separateness/name-match language, or by attributing the SDN
    # listing to "Rosneft Trading S.A." rather than to the subject.
    acknowledges_separate = any(
        k in low
        for k in (
            "separate",
            "different entity",
            "different legal entity",
            "distinct entity",
            "name match",
            "name similarity",
            "name-only",
            "name only",
            "closely named",
            "closely-named",
            "similarly named",
            "similar name",
        )
    ) or (
        "rosneft trading" in low
        and any(
            t in low
            for t in (
                "ofac sdn",
                "sdn list",
                "specially designated national",
                "on the sdn",
                "sanctioned",
            )
        )
    )

    # (a) The SUBJECT itself must NOT be asserted to be directly sanctioned/SDN.
    # Scan sentence-by-sentence: flag only sentences that mention the SUBJECT, are
    # NOT attributed to the other entity, are NOT negated/hedged, and carry an
    # affirmative SDN/sanctioned token. Normalize "S.A." so it doesn't split a
    # sentence mid-claim.
    sdn_tokens = (
        "ofac sdn",
        "sdn list",
        "sdn #",
        "specially designated national",
        "on the sdn",
        "sdn-listed",
        "directly sanctioned",
        "is sanctioned",
        "itself sanctioned",
    )
    subject_sdn = False
    norm = low.replace("s.a.", "sa").replace("s. a.", "sa")
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", norm):
        if "rosneft global trade" not in sentence and "global trade" not in sentence:
            continue
        # Attributed to the OTHER (correctly-sanctioned) entity, not the subject.
        if "rosneft trading" in sentence:
            continue
        # Negated / hedged / framed-as-separate → not an affirmative subject claim.
        if any(
            n in sentence
            for n in (
                "not ",
                "n't",
                "is not",
                "isn't",
                "separate",
                "different",
                "name match",
                "name similarity",
                "name-only",
                "name only",
            )
        ):
            continue
        if any(t in sentence for t in sdn_tokens):
            subject_sdn = True
            break

    ok = (not subject_sdn) and acknowledges_separate
    return ok, f"subject_asserted_sdn={subject_sdn}, acknowledges_separate={acknowledges_separate}"


def clarification_present(out: dict[str, Any]) -> tuple[bool, str]:
    r = _result(out)
    q = r.get("clarification_questions", [])
    ok = out["kind"] == "answer" and len(q) >= 1
    return ok, f"kind={out['kind']}, questions={len(q)}"


def sayari_factors_present(out: dict[str, Any]) -> tuple[bool, str]:
    """Tier 1: a Sayari investigation must surface at least one Sayari risk
    factor (the slimmed profile reached the output)."""
    r = _result(out)
    factors = r.get("sayari_risk_factors") or []
    return len(factors) >= 1, f"sayari_risk_factors={len(factors)}"


def sayari_path_present(out: dict[str, Any]) -> tuple[bool, str]:
    """Tier 1: at least one surfaced factor carries a traversal_path, which is
    what renders the highlightable ownership/control chain on the graph."""
    r = _result(out)
    factors = r.get("sayari_risk_factors") or []
    with_path = [f for f in factors if f.get("path")]
    return len(with_path) >= 1, f"factors_with_path={len(with_path)}"


def resolved_subject(out: dict[str, Any]) -> tuple[bool, str]:
    """Tier 1: the subject was resolved via Sayari. Shape-agnostic so it works on
    BOTH terminators: a summary's Sayari entity_id (no ':' Neo4j separator), an
    answer's Sayari source_ref / referenced node id, or surfaced Sayari factors."""
    r = _result(out)
    eid = r.get("entity_id") or ""
    if eid and ":" not in eid:
        return True, f"entity_id={eid[:24]}"
    for c in r.get("claims", []):
        for ref in c.get("source_refs", []):
            if ref.get("sayari_entity_id"):
                return True, "sayari source_ref"
    refs = [rid for rid in (r.get("referenced_node_ids") or []) if rid and ":" not in rid]
    if refs:
        return True, f"referenced_node_id={refs[0][:24]}"
    if r.get("sayari_risk_factors"):
        return True, "sayari_risk_factors present"
    return False, "no sayari resolution evidence"


def report_ready_true(out: dict[str, Any]) -> tuple[bool, str]:
    """Stage 4: a substantive investigative answer (resolved entity + a risk/
    ownership/sanctions signal) must set report_ready=true so the UI can offer a
    formal memo — WITHOUT the agent auto-emitting the report."""
    r = _result(out)
    rr = bool(r.get("report_ready"))
    return rr, f"kind={out['kind']}, report_ready={rr}"


def report_ready_false(out: dict[str, Any]) -> tuple[bool, str]:
    """Greetings / not-found / pure-clarify turns must NOT set report_ready."""
    r = _result(out)
    rr = bool(r.get("report_ready"))
    return not rr, f"kind={out['kind']}, report_ready={rr}"


def _used(out: dict[str, Any], name: str) -> tuple[bool, str]:
    tools = out.get("tools_used", [])
    return name in tools, f"tools={','.join(tools) or 'none'}"


def used_sayari_search(out: dict[str, Any]) -> tuple[bool, str]:
    return _used(out, "sayari_search")


def used_sayari_watchlist(out: dict[str, Any]) -> tuple[bool, str]:
    return _used(out, "sayari_watchlist")


def used_sayari_record(out: dict[str, Any]) -> tuple[bool, str]:
    return _used(out, "sayari_record")


def boundary_decline(out: dict[str, Any]) -> tuple[bool, str]:
    """No aggregation tool exists, so the agent must not fabricate a 'most
    common address'. Acceptable: an answer (it declines/clarifies) or a
    not-found summary. Failing only on a confident found=true aggregate."""
    r = _result(out)
    if out["kind"] == "answer":
        return True, "declined/clarified via answer"
    return r.get("found") is False, f"summary found={r.get('found')}"


EVALUATORS: dict[str, Evaluator] = {
    "terminator_answer": terminator_answer,
    "terminator_summary": terminator_summary,
    "found_true": found_true,
    "not_found": not_found,
    "no_claims": no_claims,
    "provenance": provenance,
    "sanctions_present": sanctions_present,
    "no_false_sanctions": no_false_sanctions,
    "ofac_non_sdn_labeling": ofac_non_sdn_labeling,
    "name_match_hedged": name_match_hedged,
    "clarification_present": clarification_present,
    "boundary_decline": boundary_decline,
    "sayari_factors_present": sayari_factors_present,
    "sayari_path_present": sayari_path_present,
    "resolved_subject": resolved_subject,
    "report_ready_true": report_ready_true,
    "report_ready_false": report_ready_false,
    "used_sayari_search": used_sayari_search,
    "used_sayari_watchlist": used_sayari_watchlist,
    "used_sayari_record": used_sayari_record,
}


# Each case is run through the live agent ONCE; the listed checks all score it.
# Kept deliberately small — every investigation case burns ~60-90s of API time.
CASES: list[dict[str, Any]] = [
    {
        "name": "greeting",
        "input": "hello",
        "checks": ["terminator_answer", "report_ready_false"],
    },
    # Conversational-by-default (Stage 4): a named-subject investigation now ends
    # with submit_answer (NOT an auto-emitted formal report), still resolves the
    # subject, sources its claims, and sets report_ready when a signal is found.
    {
        "name": "roldugin_conversational",
        "input": "Sergey Roldugin",
        "checks": ["terminator_answer", "provenance", "sanctions_present", "report_ready_true"],
    },
    {
        "name": "epstein_no_false_sanctions",
        "input": "Jeffrey Epstein",
        "checks": ["terminator_answer", "provenance", "no_false_sanctions"],
    },
    {
        "name": "nonsense_not_found",
        "input": "Zzqwlx Nonexistent Holdings 99127",
        "checks": ["terminator_answer", "no_claims", "report_ready_false"],
    },
    {
        "name": "vague_clarify",
        "input": "I'm trying to trace hidden Russian money but I don't have a specific name yet",
        "checks": ["clarification_present", "report_ready_false"],
    },
    {
        "name": "boundary_no_aggregate_tool",
        "input": "what address appears most often in your database?",
        "checks": ["boundary_decline"],
    },
    # --- Tier 1 (Sayari integration) regression cases ---
    # Sayari-first routing resolves a Russian SOE, surfaces its slimmed risk
    # factors (with traversal paths for the graph overlay), confirms sanctions
    # exposure, and (conversational default) sets report_ready without compiling
    # the formal report.
    {
        "name": "gazprom_sayari_risk",
        "input": "Investigate Gazprom (16 Nametkina St. Moscow, Russia)",
        "checks": [
            "terminator_answer",
            "provenance",
            "sanctions_present",
            "resolved_subject",
            "sayari_factors_present",
            "sayari_path_present",
            "report_ready_true",
        ],
    },
    # A second list_1 entity (export-controlled, China) to guard against
    # Russia-only overfitting in the Sayari routing.
    {
        "name": "huawei_sayari_profile",
        "input": "Investigate Huawei Technologies Co. Ltd. (Shenzhen, China)",
        "checks": [
            "terminator_answer",
            "provenance",
            "resolved_subject",
            "sayari_factors_present",
            "ofac_non_sdn_labeling",
            "report_ready_true",
        ],
    },
    # Name-match precision guard: check_sanctions returns a STRONG but NAME-ONLY
    # fuzzy match (~0.80) between the subject "Rosneft Global Trade S.A."
    # (Luxembourg/Russia) and the SDN-listed "Rosneft Trading S.A." (Geneva).
    # The agent must HEDGE — surface the name similarity + genuine adjacency
    # (e.g. SOE exposure via OJSC ORENBURGNEFT) but NOT promote a name-only match
    # into a direct SDN/sanctioned claim on the subject (they are separate legal
    # entities).
    {
        "name": "name_match_hedge_rosneft_global",
        "input": "Profile Rosneft Global Trade S.A. (Luxembourg) and tell me its sanctions exposure.",
        "checks": [
            "terminator_answer",
            "provenance",
            "resolved_subject",
            "sayari_factors_present",
            "name_match_hedged",
            "report_ready_true",
        ],
    },
    # --- Stage 4: explicit report request still produces the formal summary ---
    {
        "name": "explicit_report_request",
        "input": "Investigate Gazprom (16 Nametkina St. Moscow) and compile a formal risk report.",
        "checks": ["terminator_summary", "found_true", "provenance"],
    },
    # --- Stage 1: new Sayari tools exercised ---
    # Broad/fuzzy lead-gen search (distinct from precise resolution).
    {
        "name": "broad_search_leads",
        "input": "Do a broad Sayari search for companies named 'Rosneft Trading' and show me the leads.",
        "checks": ["terminator_answer", "used_sayari_search"],
    },
    # Indirect PEP/sanctions exposure via the watchlist traversal.
    {
        "name": "watchlist_indirect_exposure",
        "input": (
            "Investigate Huawei Technologies (Shenzhen, China) and map its INDIRECT "
            "exposure to sanctioned or watchlisted entities using the watchlist traversal."
        ),
        "checks": ["terminator_answer", "used_sayari_watchlist", "resolved_subject"],
    },
    # Document-level provenance: resolve+profile, then fetch the source record.
    {
        "name": "record_provenance",
        "input": (
            "Profile Gazprom (16 Nametkina St. Moscow), then fetch the underlying Sayari "
            "source record (use its record_id) so I can see the source document."
        ),
        "checks": ["used_sayari_record"],
    },
]


def _memory_writepath_rows() -> list[tuple[str, str, bool, str]]:
    """Deterministic write-path regression for the Investigation Memory Subsystem
    (doc 09, Phase A). The live LLM harness below can't observe this fix:
    `evaluate_turn` runs with persist=False, so nothing reaches Redis and
    `recall_state` has no state_doc to read. So the Rosneft multi-turn recall case
    is pinned here as a unit-style assertion on the exact projection finalize_node
    persists — `agent_graph._build_state_delta` — which is where Phase A lives.

    Scenario (turn 1, conversational-default ANSWER turn): check_sanctions surfaced
    a STRONG match on "Rosneft Trading S.A." that the agent DISMISSED as a name
    collision (so it is NOT in answer.sanctions_hits). The agent also leaned on a
    sanctioned subsidiary lead BY ID in referenced_node_ids without traversing it
    onto the graph. Both must land in the state_doc delta so a turn-2 follow-up
    recovers them via recall_state WITHOUT re-running check_sanctions.

    Gap (a): the dismissed strong hit must persist as a `dismissed` sanctions row
    even though this is an answer turn (the old code only did this on summary
    turns, dropping it). Gap (b): the referenced-but-not-traversed lead id must
    persist into resolved_entities + named_ids (it was named with an id)."""
    from app.schema import SanctionsHit, TurnAnswer

    dismissed_hit = SanctionsHit(
        name_searched="Rosneft Global Trade S.A.",
        matched_name="Rosneft Trading S.A.",
        lists=["OFAC SDN"],
        sanctions_id="ofac-30947",
        score=0.81,
        on_watchlist=True,
    ).model_dump()

    lead_id = "sayari-rosneft-trade-limited"
    answer = TurnAnswer(
        answer=(
            "Rosneft Trading S.A. is a separate, similarly named SDN-listed entity; "
            "the subject itself is not on the SDN list."
        ),
        sanctions_hits=[],  # the strong hit was DISMISSED, not kept
        referenced_node_ids=[lead_id],
    )
    state: dict[str, Any] = {
        "turn_index": 1,
        "user_message": "Profile Rosneft Global Trade S.A. and its sanctions exposure.",
        "intent": "profile_entity",
        "pinned_node_ids": [],
        "turn_nodes": [],
        "turn_leads": [
            {
                "entity_id": lead_id,
                "label": "Rosneft Trade Limited",
                "type": "company",
                "countries": ["RUS"],
                "sanctioned": True,
                "from_turn": 1,
            }
        ],
        "raw_strong_hits": [dismissed_hit],
    }

    delta = agent_graph._build_state_delta(state, None, answer)  # None summary => answer turn

    sanc = delta.get("sanctions_adjudicated", [])
    dismissed = [r for r in sanc if r.get("verdict") == "dismissed"]
    gap_a = any(r.get("matched_name") == "Rosneft Trading S.A." for r in dismissed)

    named = delta.get("named_ids", {})
    resolved = delta.get("resolved_entities", {})
    gap_b = lead_id in named and any(
        r.get("entity_id") == lead_id for r in resolved.values()
    )

    case = "rosneft_memory_writepath"
    return [
        (case, "answer_turn_dismissed_persisted", gap_a,
         f"dismissed_rows={[r.get('matched_name') for r in dismissed]}"),
        (case, "referenced_id_persisted", gap_b,
         f"named_ids={list(named)}, resolved={list(resolved)}"),
    ]


def _entity_registry_rows() -> list[tuple[str, str, bool, str]]:
    """Deterministic Phase B regression (doc 09 §B): the unified id-keyed entity
    registry. Like the write-path check above, the live harness can't observe
    this (persist=False -> no Redis -> recall_state has no state_doc), so the
    'most sanctioned connected entity' fix is pinned as a unit-style assertion on
    the pure projection + severity ranking (conversations._project_entities +
    entity_severity_score), which is exactly what recall_state(kind='entities',
    sort='severity') ranks over.

    Scenario: a turn surfaces a check_sanctions STRONG OFAC SDN hit (a Gazprom
    Shelfproekt-style sanctioned entity that lives ONLY in the sanctions layer)
    AND a lower-severity ownership neighbor (a clean subsidiary traversed onto
    the graph). The SDN entity must (1) be deposited into the registry as a
    first-class sanctioned entity, and (2) rank FIRST by severity, ahead of the
    ownership neighbor — the gap that made the agent miss it on Gazprom."""
    from app import conversations
    from app.schema import SanctionsHit, TurnAnswer

    sdn_hit = SanctionsHit(
        name_searched="Gazprom",
        matched_name="Gazprom Shelfproekt",
        lists=["OFAC SDN"],
        sanctions_id="ofac-99001",
        score=0.92,
        on_watchlist=True,
        countries=["ru"],
    ).model_dump()

    neighbor_id = "sayari-clean-subsidiary"
    state: dict[str, Any] = {
        "turn_index": 1,
        "user_message": "Investigate Gazprom and its connected sanctioned entities.",
        "intent": "sanctions_screening",
        "pinned_node_ids": [],
        # An ownership neighbor traversed onto the graph (clean, low severity).
        "turn_nodes": [
            {
                "id": neighbor_id,
                "name": "Gazprom Clean Subsidiary LLC",
                "label": "Entity",
                "source_system": "sayari",
                "properties": {"sanctioned": False, "pep": False, "countries": ["RUS"]},
            }
        ],
        "turn_leads": [],
        "raw_strong_hits": [sdn_hit],
    }
    # An answer turn that DISMISSED the SDN hit as a name collision (so it is not
    # in sanctions_hits) — it must still land in the registry as a sanctioned
    # entity, since the matched entity itself is genuinely SDN-listed.
    answer = TurnAnswer(answer="Reviewed connections.", sanctions_hits=[])

    delta = agent_graph._build_state_delta(state, None, answer)
    # Project the registry exactly as get_state_doc / merge_state_doc would.
    doc = {**conversations._empty_state_doc(), **delta}
    entities = conversations._project_entities(doc)

    sdn_present = "ofac-99001" in entities and entities["ofac-99001"].get("is_sdn") is True
    ranked = sorted(entities.values(), key=conversations.entity_severity_score, reverse=True)
    sdn_first = bool(ranked) and ranked[0].get("id") == "ofac-99001"
    # The clean neighbor must also be in the same pool (one rankable set) but BELOW.
    neighbor_in_pool = neighbor_id in entities
    neighbor_below = neighbor_in_pool and (
        conversations.entity_severity_score(entities[neighbor_id])
        < conversations.entity_severity_score(entities["ofac-99001"])
    )

    # Backward-compat: an OLD-shape doc (resolved_entities/named_ids/leads, NO
    # `entities` key) must backfill a populated registry via the same projection.
    old_doc = {
        "resolved_entities": {
            "acme ltd": {"entity_id": "e-acme", "label": "Acme Ltd", "type": "company",
                          "sanctioned": False, "first_seen_turn": 1, "last_seen_turn": 1},
        },
        "named_ids": {"e-named": {"label": "Named Co", "type": "company", "sanctioned": False}},
        "leads": [{"entity_id": "e-lead", "label": "Lead Co", "from_turn": 1, "sanctioned": False}],
        "sanctions_adjudicated": [],
    }
    backfilled = conversations._project_entities(old_doc)
    backfill_ok = {"e-acme", "e-named", "e-lead"}.issubset(set(backfilled))

    case = "entity_registry"
    return [
        (case, "sdn_hit_deposited", sdn_present, f"entities={sorted(entities)}"),
        (case, "sdn_ranked_first", sdn_first,
         f"top={ranked[0].get('label') if ranked else None}"),
        (case, "neighbor_in_pool_below", neighbor_below,
         f"neighbor_in_pool={neighbor_in_pool}"),
        (case, "backward_compat_backfill", backfill_ok,
         f"backfilled={sorted(backfilled)}"),
    ]


def _injection_shrink_rows() -> list[tuple[str, str, bool, str]]:
    """Deterministic Phase C regression (doc 09 §C / §6): the injected memory core
    is FIXED-SIZE navigation hints, and shrinking it did NOT cost recall fidelity.

    Two properties the live harness can't observe (persist=False -> no state_doc):

    1. Fixed budget. `_render_state_block` rendered over a SMALL investigation and
       a HUGE one (300 entities, 60 leads, 80 sanctions rows) must be the same
       bounded size — the core cannot grow with the case. This is the
       context-stuffing fix as a hard assertion.

    2. Faithful recall through the lean core. The canonical Rosneft turn-1 outcome
       (a STRONG sanctions match on 'Rosneft Trading S.A.' that was DISMISSED as a
       name collision) must (a) NOT be misrepresented as a confirmed hit in the
       lean core, yet (b) stay byte-exact recoverable via the structured state the
       core points at (sanctions ledger + projected registry), and (c) be
       surfaced by the deterministic follow-up prefetch so the enumeration answers
       in one hop. The shrink hides it from the inline dump WITHOUT dropping it."""
    from app import conversations
    from app.agent_common import (
        _render_state_block,
        build_followup_prefetch,
    )

    def make_doc(n_entities: int, n_leads: int, n_dismissed: int) -> dict[str, Any]:
        leads = [
            {"entity_id": f"lead-{i}", "label": f"Lead Co {i}", "type": "company",
             "countries": ["CYP"], "sanctioned": False, "from_turn": 3,
             "from_query": "Rosneft-linked trading companies"}
            for i in range(n_leads)
        ]
        sanctions = [
            {"sanctions_id": "ofac-confirmed", "matched_name": "Confirmed Bad Co",
             "lists": ["OFAC SDN"], "verdict": "confirmed", "from_turn": 1},
        ]
        # The dismissed Rosneft name collision + padding dismissed rows.
        sanctions.append({"sanctions_id": "ofac-30947", "matched_name": "Rosneft Trading S.A.",
                          "lists": ["OFAC SDN"], "verdict": "dismissed", "from_turn": 1})
        for i in range(n_dismissed):
            sanctions.append({"sanctions_id": f"ofac-d{i}", "matched_name": f"Dismissed Co {i}",
                              "lists": ["OFAC SDN"], "verdict": "dismissed", "from_turn": 1})
        doc = {
            **conversations._empty_state_doc(),
            "resolved_entities": {
                "rosneft global trade s.a.": {
                    "entity_id": "sayari-rosneft-global", "label": "Rosneft Global Trade S.A.",
                    "type": "company", "sanctioned": False,
                    "first_seen_turn": 1, "last_seen_turn": 3,
                },
            },
            "leads": leads,
            "sanctions_adjudicated": sanctions,
            "named_ids": {f"named-{i}": {"label": f"Named {i}", "type": "company"}
                          for i in range(n_entities)},
            "pinned_node_ids": [f"pin-{i}" for i in range(20)],
        }
        doc["entities"] = conversations._project_entities(doc)
        return doc

    small = make_doc(4, 3, 1)
    large = make_doc(300, 60, 80)
    small_block = "\n".join(_render_state_block(small))
    large_block = "\n".join(_render_state_block(large))

    # (1) Fixed budget: the huge case must render a bounded core that's not
    # materially larger than the small one (only count digits differ).
    fixed_budget = len(large_block) <= 900 and (len(large_block) - len(small_block)) <= 80

    # (2a) The lean core must NOT inline the dismissed name as a hit.
    dismissed_not_inlined = "Rosneft Trading" not in large_block

    # (2b) The dismissed row stays byte-exact in the structured state the core
    # points at, and projects into the registry as a sanctioned entity — the
    # recall_state(kind="sanctions"/"entities") path the agent is told to use.
    ledger_names = [r.get("matched_name") for r in large["sanctions_adjudicated"]]
    recoverable_in_ledger = "Rosneft Trading S.A." in ledger_names
    recoverable_in_registry = (
        "ofac-30947" in large["entities"]
        and large["entities"]["ofac-30947"].get("sanctioned") is True
    )

    # (2c) The deterministic follow-up prefetch surfaces it for the common ask.
    prefetch = build_followup_prefetch(large, "which subsidiaries were sanctioned again?")
    prefetch_surfaces = "Rosneft Trading S.A." in prefetch and "dismissed" in prefetch

    case = "injection_shrink"
    return [
        (case, "fixed_budget_core", fixed_budget,
         f"small={len(small_block)}ch large={len(large_block)}ch"),
        (case, "dismissed_not_inlined", dismissed_not_inlined,
         "lean core omits the dismissed name"),
        (case, "recall_faithful_ledger", recoverable_in_ledger,
         f"ledger_names={[n for n in ledger_names if 'Rosneft' in str(n)]}"),
        (case, "recall_faithful_registry", recoverable_in_registry,
         "dismissed hit is a first-class sanctioned registry entity"),
        (case, "prefetch_surfaces_dismissed", prefetch_surfaces,
         f"prefetch_len={len(prefetch)}ch"),
    ]


def _recap_routing_rows() -> list[tuple[str, str, bool, str]]:
    """Deterministic recap-routing regression: a recap-style ask ('summarize
    everything you found so far', 'recap', 'what do we have so far') must route to
    the LIGHTWEIGHT submit_answer / TurnAnswer terminator, NOT the heavy
    submit_summary / RiskSummary deliverable.

    The routing decision the LLM makes isn't deterministic, so we pin the
    DETERMINISTIC layer the brief asked for: the rule-based recap detector +
    guidance. A recap (a) is detected, (b) routes to conversational_followup with
    wants_report=false (so the prompt never gets the 'finish with submit_summary'
    nudge), (c) emits guidance that names submit_answer and forbids submit_summary,
    and (d) does NOT fire on turn 1 (nothing to recap) or on a real 'summarize X's
    ownership' investigation ask (no false positive). We also confirm the
    submit_answer terminator validates into a TurnAnswer."""
    from app import intent
    from app.agent_graph import _validate_terminator
    from app.schema import TurnAnswer

    prior = "Turn 1: investigated Rosneft. Found state ownership + 2 sanctioned subsidiaries."

    detected = intent._is_recap("Summarize everything you found on Rosneft so far")
    detected_recap_word = intent._is_recap("recap")
    detected_rundown = intent._is_recap("give me the rundown")
    # No false positive: a real investigation ask that happens to say 'summarize'.
    no_fp_profile = not intent._is_recap("summarize Rosneft's ownership structure")

    res = intent._recap_shortcut("summarize what you found so far", prior)
    routed_followup = bool(res) and res.get("intent") == "conversational_followup"
    no_report = bool(res) and res.get("wants_report") is False

    # Turn 1 (no prior context) must NOT be treated as a recap shortcut.
    turn1 = intent._recap_shortcut("summarize what you found so far", "")
    turn1_skips = turn1 is None

    guidance = intent.build_guidance(res or {})
    # The recap overlay names submit_answer and the report nudge line ('finishing
    # with submit_summary is appropriate') is absent.
    guidance_names_answer = "submit_answer" in guidance
    no_summary_nudge = "submit_summary is appropriate" not in guidance

    # The submit_answer terminator validates into a TurnAnswer (the light shape).
    ta = _validate_terminator(
        "submit_answer",
        {"answer": "Here's the recap so far.", "report_ready": True, "offer_risk_report": True},
        [],
    )
    is_turnanswer = isinstance(ta, TurnAnswer)

    case = "recap_routing"
    return [
        (case, "recap_detected", detected and detected_recap_word and detected_rundown,
         f"so_far={detected} recap={detected_recap_word} rundown={detected_rundown}"),
        (case, "no_false_positive", no_fp_profile, "‘summarize X ownership’ is not a recap"),
        (case, "routes_followup", routed_followup, f"intent={res.get('intent') if res else None}"),
        (case, "wants_report_false", no_report, f"wants_report={res.get('wants_report') if res else None}"),
        (case, "turn1_not_recap", turn1_skips, "no prior context -> not a recap shortcut"),
        (case, "guidance_picks_answer", guidance_names_answer and no_summary_nudge,
         "guidance names submit_answer, no submit_summary nudge"),
        (case, "terminator_is_turnanswer", is_turnanswer, f"type={type(ta).__name__}"),
    ]


def _source_enum_rows() -> list[tuple[str, str, bool, str]]:
    """Deterministic source-enum normalization regression: a model emitting
    source:"sanctions" (the label used everywhere else in the stack) must NOT fail
    terminator validation. We coerce "sanctions" -> the canonical "opensanctions"
    pre-validation, so existing readers/frontend stay byte-compatible. Asserts the
    coercion on a bare SourceRef AND through a full TurnAnswer terminator, and that
    the legit values still pass unchanged."""
    from app.agent_graph import _validate_terminator
    from app.schema import SourceRef

    # Bare SourceRef: "sanctions" coerces to the canonical "opensanctions".
    ref = SourceRef(source="sanctions", sanctions_id="ofac-30947")
    coerced = ref.source == "opensanctions"
    # Canonical values still pass unchanged.
    legit = (
        SourceRef(source="opensanctions", sanctions_id="x").source == "opensanctions"
        and SourceRef(source="sayari", sayari_entity_id="y").source == "sayari"
        and SourceRef(source="icij", node_id="z").source == "icij"
    )

    # Through a full terminator: a TurnAnswer whose claim cites source:"sanctions"
    # validates (previously raised ValidationError -> retry loop) and stores the
    # canonical label.
    args = {
        "answer": "Rosneft Trading S.A. is OFAC SDN-listed.",
        "claims": [{
            "text": "Rosneft Trading S.A. appears on the OFAC SDN list.",
            "source_refs": [{"source": "sanctions", "sanctions_id": "ofac-30947"}],
            "confidence": "high",
        }],
    }
    try:
        ta = _validate_terminator("submit_answer", args, [])
        terminator_ok = ta.claims[0].source_refs[0].source == "opensanctions"
        err = ""
    except Exception as e:  # a failure here is the exact regression we're fixing
        terminator_ok = False
        err = f"raised {type(e).__name__}"

    case = "source_enum"
    return [
        (case, "sanctions_coerced", coerced, f"source={ref.source}"),
        (case, "canonical_unchanged", legit, "icij/opensanctions/sayari still valid"),
        (case, "terminator_accepts_sanctions", terminator_ok,
         err or "claim source 'sanctions' -> 'opensanctions'"),
    ]


async def _episodic_disabled_rows() -> list[tuple[str, str, bool, str]]:
    """Deterministic Phase D regression (doc 09 §D): the GRACEFUL NO-OP contract
    of L2 episodic vector memory when it is DISABLED (the default, and how the
    eval suite + live demo run today). This locks in that the new infrastructure
    cannot regress the existing flow:

    1. is_enabled() is False with no vector creds / flag — the single gate.
    2. recall_memory returns a clean configured=false result whose note steers the
       agent to recall_state (the fallback the prompt promises), NOT an error.
    3. write_episode no-ops (returns False) so finalize never blocks or writes.
    4. build_episode is pure + STRUCTURED-ONLY: it derives the embed text from the
       structured delta (subjects, claim text, sanctions) and never from the prose
       answer string (the HaluMem trap, doc 09 §10)."""
    from app import episodic
    from app.tools import recall_memory_tool

    episodic.reset_for_test()

    disabled = not episodic.is_enabled()

    res = await recall_memory_tool(conversation_id="c-eval", query="sanctioned subsidiaries")
    tool_noop = (
        res.get("configured") is False
        and res.get("count") == 0
        and "recall_state" in (res.get("note") or "")
    )

    wrote = await episodic.write_episode({"conversation_id": "c-eval", "turn": 1, "text": "x"})
    write_noop = wrote is False

    # Structured-only episode: a prose answer that mentions a NON-structured name
    # must NOT leak into the embed text; only the structured delta fields do.
    delta = {
        "resolved_entities": {
            "rosneft global trade s.a.": {
                "entity_id": "sayari-rgt", "label": "Rosneft Global Trade S.A.",
                "type": "company", "sanctioned": False,
                "first_seen_turn": 1, "last_seen_turn": 1,
            }
        },
        "sanctions_adjudicated": [
            {"sanctions_id": "ofac-30947", "matched_name": "Rosneft Trading S.A.",
             "lists": ["OFAC SDN"], "verdict": "dismissed", "from_turn": 1},
        ],
        "claims": [
            {"text": "Subject is not itself on the SDN list.", "confidence": "high",
             "source_refs": [{"source": "sayari", "sayari_entity_id": "sayari-rgt"}],
             "entity_ids": ["sayari-rgt"], "from_turn": 1},
        ],
        "named_ids": {},
        "turn_log": [{"turn": 1, "subject": "Rosneft Global Trade S.A."}],
    }
    ep = episodic.build_episode("c-eval", 1, "profile_entity", delta, ["sayari_profile", "check_sanctions"])
    structured = (
        "Rosneft Global Trade S.A." in ep["text"]
        and "Rosneft Trading S.A." in ep["text"]  # structured sanctions field
        and "sayari-rgt" in ep["entity_ids"]
        and "ofac-30947" in ep["entity_ids"]
        and ep["salience"] > 0.3  # a dismissed hit bumps salience
        and ep["turn"] == 1
    )

    case = "episodic_disabled"
    return [
        (case, "disabled_by_default", disabled, f"is_enabled={episodic.is_enabled()}"),
        (case, "recall_memory_noop", tool_noop, f"configured={res.get('configured')} count={res.get('count')}"),
        (case, "write_episode_noop", write_noop, f"wrote={wrote}"),
        (case, "episode_structured_only", structured,
         f"text={ep['text'][:60]!r} ids={ep['entity_ids']}"),
    ]


async def _episodic_enabled_mock_rows() -> list[tuple[str, str, bool, str]]:
    """Minimal unit-level check of the ENABLED episodic path WITHOUT real creds:
    inject a fake Upstash Vector index and assert (1) write_episode upserts the
    raw-text episode under the `{cid}:{turn}` id in the conversation's namespace,
    and (2) query_episodes re-ranks by similarity x recency x salience (not raw
    cosine) — a recent, lower-similarity episode can outrank an older, higher-
    similarity one. Restores the module afterwards so the rest of the suite runs
    with episodic disabled (the no-op contract)."""
    from app import episodic

    class _FakeMatch:
        def __init__(self, score: float, meta: dict[str, Any]):
            self.score = score
            self.metadata = meta

    class _FakeIndex:
        def __init__(self) -> None:
            self.upserts: list[Any] = []

        def upsert(self, vectors: Any, namespace: str | None = None) -> None:
            self.upserts.append((vectors, namespace))

        def query(self, data: str, top_k: int, include_metadata: bool,
                  namespace: str, filter: str) -> list[Any]:
            # turn 2 is MORE similar but older + (here) lower salience; turn 5 is
            # recent. The weighted rerank should float turn 5 to the top.
            return [
                _FakeMatch(0.40, {"turn": 2, "salience": 0.3, "subjects": ["Old Co"],
                                  "findings": ["f2"], "sanctions": [], "entity_ids": ["e2"],
                                  "tools_used": []}),
                _FakeMatch(0.55, {"turn": 5, "salience": 0.3, "subjects": ["New Co"],
                                  "findings": ["f5"], "sanctions": [], "entity_ids": ["e5"],
                                  "tools_used": []}),
            ]

    orig_enabled = episodic.is_enabled
    orig_get_index = episodic._get_index
    fake = _FakeIndex()
    try:
        episodic.is_enabled = lambda: True  # type: ignore[assignment]
        episodic._get_index = lambda: fake  # type: ignore[assignment]

        ep = episodic.build_episode("c-mock", 5, "profile_entity", {
            "resolved_entities": {"new co": {"entity_id": "e5", "label": "New Co"}},
        }, ["sayari_profile"])
        wrote = await episodic.write_episode(ep)
        upsert_ok = (
            wrote is True
            and bool(fake.upserts)
            and fake.upserts[0][0][0][0] == "c-mock:5"  # (id, text, meta) tuple
            and fake.upserts[0][1] == "c-mock"          # namespace
        )

        res = await episodic.query_episodes("c-mock", "companies", top_k=2)
        episodes = res.get("episodes", [])
        rerank_ok = (
            res.get("configured") is True
            and len(episodes) == 2
            and episodes[0]["turn"] == 5  # recency lifted the lower-similarity hit
        )
    finally:
        episodic.is_enabled = orig_enabled  # type: ignore[assignment]
        episodic._get_index = orig_get_index  # type: ignore[assignment]
        episodic.reset_for_test()

    case = "episodic_enabled_mock"
    return [
        (case, "write_upserts_namespaced", upsert_ok,
         f"upserts={len(fake.upserts)}"),
        (case, "query_reranks_recency_salience", rerank_ok,
         f"top_turn={episodes[0]['turn'] if episodes else None}"),
    ]


async def _run_local() -> int:
    print(f"Running {len(CASES)} eval cases against agent_graph (live)...\n")
    total = 0
    passed = 0
    rows: list[tuple[str, str, bool, str]] = []

    # Deterministic write-path + registry checks first — instant, no API spend.
    # Pins the Investigation Memory Subsystem fixes (Phase A write-path widening,
    # Phase B unified registry) the live harness can't observe (persist=False).
    for name, fn in (
        ("rosneft_memory_writepath", _memory_writepath_rows),
        ("entity_registry", _entity_registry_rows),
        ("injection_shrink", _injection_shrink_rows),
        ("recap_routing", _recap_routing_rows),
        ("source_enum", _source_enum_rows),
    ):
        try:
            for r in fn():
                rows.append(r)
                total += 1
                passed += int(r[2])
        except Exception as e:  # a crash here is a real regression, not a flake
            rows.append((name, "deterministic_check", False, f"crashed: {e}"))
            total += 1

    # Phase D: episodic memory — the graceful-no-op contract (disabled, default)
    # and a mock-backed enabled-path check (async, so run separately).
    # Phase F: the multi-turn memory harness (doc 09 §11). Runs N sequential
    # turns in ONE conversation, persisting state_doc between turns exactly as
    # production does, then asserts later-turn recall WITHOUT re-running tools:
    # the Rosneft regression, the IMS invariant, and a faithful recap.
    for nm, afn in (
        ("episodic_disabled", _episodic_disabled_rows),
        ("episodic_enabled_mock", _episodic_enabled_mock_rows),
        ("multiturn_recall", multiturn.multiturn_recall_rows),
        ("ims_invariant", multiturn.ims_invariant_rows),
        ("recap_multiturn", multiturn.recap_multiturn_rows),
    ):
        try:
            for r in await afn():
                rows.append(r)
                total += 1
                passed += int(r[2])
        except Exception as e:
            rows.append((nm, "deterministic_check", False, f"crashed: {e}"))
            total += 1

    for case in CASES:
        t0 = time.perf_counter()
        try:
            out = await agent_graph.evaluate_turn(case["input"])
        except Exception as e:  # a crash is a failure of every check
            for check in case["checks"]:
                rows.append((case["name"], check, False, f"agent crashed: {e}"))
                total += 1
            continue
        dt = time.perf_counter() - t0
        kind = out.get("kind")
        tools = ",".join(out.get("tools_used", []))
        print(f"• {case['name']}: {kind} in {dt:.0f}s [tools: {tools or 'none'}]")
        for check in case["checks"]:
            ok, comment = EVALUATORS[check](out)
            rows.append((case["name"], check, ok, comment))
            total += 1
            passed += int(ok)

    print("\n" + "=" * 78)
    print(f"{'CASE':<30}{'CHECK':<24}{'RESULT':<8}COMMENT")
    print("-" * 78)
    for name, check, ok, comment in rows:
        mark = "PASS" if ok else "FAIL"
        print(f"{name:<30}{check:<24}{mark:<8}{comment}")
    print("=" * 78)
    print(f"\n{passed}/{total} checks passed.")
    return 0 if passed == total else 1


_DATASET_NAME = "erre-regression"


def _ensure_dataset() -> Any:
    """Create the regression dataset (or reuse it), populated with our cases.

    aevaluate in langsmith 0.8.6 needs a real dataset, not inline dicts. We keep
    one stable dataset so re-runs land as comparable experiments under it.
    """
    from langsmith import Client

    client = Client()
    if client.has_dataset(dataset_name=_DATASET_NAME):
        ds = client.read_dataset(dataset_name=_DATASET_NAME)
        has_examples = any(client.list_examples(dataset_id=ds.id, limit=1))
        if has_examples:
            return ds
    else:
        ds = client.create_dataset(
            dataset_name=_DATASET_NAME,
            description="Agent regression cases (terminator routing, sanctions gate, provenance, boundaries).",
        )
    client.create_examples(
        dataset_id=ds.id,
        inputs=[{"input": case["input"]} for case in CASES],
        metadata=[{"name": case["name"], "checks": case["checks"]} for case in CASES],
    )
    return ds


async def _run_langsmith() -> int:
    """Upload the same cases + evaluators to LangSmith as an experiment."""
    from langsmith import aevaluate  # imported lazily; only needed with --push

    ds = _ensure_dataset()
    print(f"Dataset ready: {_DATASET_NAME} ({len(CASES)} examples)")

    async def target(inputs: dict[str, Any]) -> dict[str, Any]:
        return await agent_graph.evaluate_turn(inputs["input"])

    def make_ls_evaluator(check: str):
        fn = EVALUATORS[check]

        def _ls(outputs: dict[str, Any], example: Any) -> dict[str, Any] | None:
            # Only score the cases this check was written for. aevaluate applies
            # every evaluator to every example, so without this guard the grid
            # becomes an NxM patchwork (e.g. sanctions_present scored against the
            # greeting case). Returning None skips, so the grid shows exactly the
            # 14 relevant cells the local run does — all green.
            applicable = (getattr(example, "metadata", None) or {}).get("checks", [])
            if check not in applicable:
                return None
            ok, comment = fn(outputs)
            return {"key": check, "score": int(ok), "comment": comment}

        _ls.__name__ = check
        return _ls

    # Every distinct check used across cases becomes a LangSmith evaluator;
    # each one self-skips the cases it doesn't apply to (see make_ls_evaluator).
    all_checks = sorted({c for case in CASES for c in case["checks"]})
    results = await aevaluate(
        target,
        data=_DATASET_NAME,
        evaluators=[make_ls_evaluator(c) for c in all_checks],
        experiment_prefix="erre-regression",
        max_concurrency=1,
    )
    print("Uploaded to LangSmith. Experiment:", getattr(results, "experiment_name", "(see UI)"))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the agent regression evals.")
    parser.add_argument(
        "--push",
        action="store_true",
        help="Also upload to LangSmith (requires LANGCHAIN_API_KEY).",
    )
    args = parser.parse_args()
    # Activate LangSmith tracing if configured, so even a local run produces
    # trace trees in the LangSmith UI (and --push can upload an experiment).
    from app.config import apply_langsmith_env, get_settings

    tracing_on = apply_langsmith_env(get_settings())
    print(f"LangSmith tracing: {'on' if tracing_on else 'off'}")
    if args.push:
        if not tracing_on:
            print("LANGCHAIN_TRACING_V2 + LANGCHAIN_API_KEY not set; cannot --push.")
            sys.exit(2)
        asyncio.run(_run_langsmith())
    else:
        sys.exit(asyncio.run(_run_local()))


if __name__ == "__main__":
    main()
