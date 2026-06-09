"""Multi-turn memory eval harness (Investigation Memory Subsystem, doc 09 §F).

This is the safety net that locks in Phases A-E: it runs N SEQUENTIAL turns in
ONE conversation, persisting `state_doc` between turns EXACTLY as production does
(`conversations._apply_delta`, the pure core `merge_state_doc` runs), then asserts
on later-turn RECALL behavior — specifically that a finding survives the write
path and is recoverable WITHOUT re-running the tool that produced it.

Why it is deterministic + CI-friendly (no Redis, no live model, no credits):

  - The turn-1 "investigation" is expressed as the structured graph state
    `finalize_node` hands to `agent_graph._build_state_delta` — the SAME
    projection production persists. So we exercise the real write path.
  - Turns are persisted with `conversations._apply_delta` into an in-memory doc,
    the identical transformation `merge_state_doc` writes to Redis.
  - Later-turn recall is exercised through the REAL `recall_state_tool` by
    pointing `conversations.get_state_doc` at the in-memory persisted doc, plus
    the real `build_followup_prefetch` / `build_context_block` read surfaces.

The brief (doc 09 §11) calls for the concrete Rosneft regression and the IMS
invariant encoded as a reusable check; both live here. The live-model routing
("is turn 2 labeled a follow-up, does the model avoid check_sanctions") is NOT
re-tested here on purpose: it is flaky and slow, and the deterministic guarantee
is stronger — the dismissed subsidiary is recoverable from durable state via a
ZERO-CREDIT memory read (recall_state / the prefetch), so a correct turn 2 has no
reason to re-spend check_sanctions. We assert that guarantee directly.
"""

from __future__ import annotations

import contextlib
from typing import Any

from app import agent_graph, conversations, intent
from app.agent_common import build_followup_prefetch, graph_payload
from app.schema import Claim, SanctionsHit, SourceRef, TurnAnswer
from app.tools import recall_state_tool

# A row is (case, check, passed, comment) — the same shape run_evals prints.
Row = tuple[str, str, bool, str]


# --- The persisted, in-memory conversation (production write path, no Redis) ---


class _Conversation:
    """An in-memory `state_doc` persisted across turns EXACTLY as production
    merges it. `finalize` runs the real `_build_state_delta` projection then the
    real `_apply_delta`, so the doc after N turns is byte-identical to what Redis
    would hold."""

    def __init__(self) -> None:
        self.doc = conversations._empty_state_doc()
        self.doc["entities"] = conversations._project_entities(self.doc)

    def finalize(
        self,
        state: dict[str, Any],
        summary: Any = None,
        answer: Any = None,
    ) -> dict[str, Any]:
        delta = agent_graph._build_state_delta(state, summary, answer)
        self.doc = conversations._apply_delta(self.doc, delta)
        return delta


@contextlib.asynccontextmanager
async def _recall_against(doc: dict[str, Any]):
    """Point `conversations.get_state_doc` (what `recall_state_tool` reads) at an
    in-memory doc so the REAL recall tool runs deterministically, then restore."""
    orig = conversations.get_state_doc

    async def _fake(_cid: str) -> dict[str, Any]:
        return doc

    conversations.get_state_doc = _fake  # type: ignore[assignment]
    try:
        yield
    finally:
        conversations.get_state_doc = orig  # type: ignore[assignment]


# --- The reusable IMS invariant check (doc 09 §11) -------------------------


def ims_invariant_violations(
    referenced_ids: set[str],
    entities: dict[str, Any],
) -> set[str]:
    """The IMS invariant, as a reusable deterministic check (doc 09 §11):

      Every entity the agent names with an id in its answer
      (referenced_node_ids + claims.source_refs) MUST appear in `entities`.

    Returns the set of referenced ids MISSING from the registry — empty means the
    invariant holds. Callers pass the ids that were nameable from the turn's
    STRUCTURED tool outputs (the ones the write path is obligated to persist; an
    id with no structured backing is deliberately NOT written, doc 09 §10)."""
    return {rid for rid in referenced_ids if rid not in entities}


# --- Case 1: the Rosneft regression (doc 09 §1.1 / §11) --------------------


async def multiturn_recall_rows() -> list[Row]:
    """Turn 1 investigates Rosneft Global Trade S.A.; check_sanctions surfaces a
    STRONG match on the closely-named SDN entity "Rosneft Trading S.A." which the
    agent DISMISSES as a name collision (so it is NOT in answer.sanctions_hits).
    Turn 2 asks to re-list the sanctioned-but-dismissed subsidiaries.

    Asserts (doc 09 §11): (a) the dismissed subsidiary is recoverable in turn 2
    via recall_state(kind="sanctions") AND kind="entities"); (b) it does NOT
    require re-running check_sanctions — the recall path (the deterministic
    follow-up prefetch + the zero-credit recall_state read) supplies the answer,
    so a correct turn 2 re-spends no tool call."""
    case = "multiturn_recall_rosneft"
    conv = _Conversation()

    # --- Turn 1: conversational-default ANSWER turn ---
    dismissed_hit = SanctionsHit(
        name_searched="Rosneft Global Trade S.A.",
        matched_name="Rosneft Trading S.A.",
        lists=["OFAC SDN"],
        sanctions_id="ofac-30947",
        score=0.81,
        on_watchlist=True,
        countries=["ch"],
    ).model_dump()

    subject_node = {
        "id": "sayari-rgt",
        "name": "Rosneft Global Trade S.A.",
        "label": "Entity",
        "source_system": "sayari",
        "properties": {"sanctioned": False, "pep": False, "countries": ["LUX"]},
    }
    turn1_state: dict[str, Any] = {
        "turn_index": 1,
        "user_message": "Profile Rosneft Global Trade S.A. and its sanctions exposure.",
        "intent": "profile_entity",
        "pinned_node_ids": [],
        "turn_nodes": [subject_node],
        "turn_leads": [],
        "raw_strong_hits": [dismissed_hit],
    }
    turn1_answer = TurnAnswer(
        answer=(
            "Rosneft Trading S.A. is a separate, similarly named SDN-listed entity; "
            "the subject itself is not on the OFAC SDN list."
        ),
        sanctions_hits=[],  # the strong hit was DISMISSED, not kept
        claims=[
            Claim(
                text="Rosneft Trading S.A. appears on the OFAC SDN list (separate entity).",
                source_refs=[SourceRef(source="opensanctions", sanctions_id="ofac-30947")],
                confidence="high",
            )
        ],
    )
    conv.finalize(turn1_state, summary=None, answer=turn1_answer)

    # --- Turn 2: the re-list follow-up. Recall, do not re-run. ---
    turn2_msg = "Re-list the subsidiaries that came up as sanctioned but were dismissed as name collisions."

    async with _recall_against(conv.doc):
        sanc = await recall_state_tool("c-mt", kind="sanctions")
        ents = await recall_state_tool("c-mt", kind="entities", sanctioned=True)

    sanc_items = sanc.get("items", [])
    recoverable_sanctions = any(
        r.get("matched_name") == "Rosneft Trading S.A." and r.get("verdict") == "dismissed"
        for r in sanc_items
    )

    ent_items = ents.get("items", [])
    sdn_entity = next((r for r in ent_items if r.get("id") == "ofac-30947"), None)
    recoverable_entities = bool(
        sdn_entity and sdn_entity.get("sanctioned") and sdn_entity.get("is_sdn")
    )
    # Phase E: the recovered entity carries its provenance, so turn 2 can re-cite
    # it with its OpenSanctions record without re-running check_sanctions.
    entity_has_provenance = bool(
        sdn_entity
        and any(
            ref.get("source") == "opensanctions" and ref.get("sanctions_id") == "ofac-30947"
            for ref in (sdn_entity.get("source_refs") or [])
        )
    )

    # (b) The deterministic follow-up prefetch surfaces the dismissed row by name,
    # so the agent answers in one hop. recall_state is a zero-credit memory read
    # (no graph nodes, no external call), so recovering this way re-spends NO tool.
    prefetch = build_followup_prefetch(conv.doc, turn2_msg)
    prefetch_surfaces = "Rosneft Trading S.A." in prefetch and "dismissed" in prefetch
    recall_is_zero_credit = graph_payload("recall_state", {}) == ([], [])
    check_sanctions_not_respent = (
        recoverable_sanctions
        and recoverable_entities
        and prefetch_surfaces
        and recall_is_zero_credit
    )

    return [
        (case, "dismissed_recoverable_sanctions", recoverable_sanctions,
         f"sanctions_items={[r.get('matched_name') for r in sanc_items]}"),
        (case, "dismissed_recoverable_entities", recoverable_entities,
         f"sdn_entity={'ofac-30947' if sdn_entity else None}"),
        (case, "recovered_entity_has_provenance", entity_has_provenance,
         f"source_refs={(sdn_entity or {}).get('source_refs')}"),
        (case, "check_sanctions_not_respent", check_sanctions_not_respent,
         f"prefetch_surfaces={prefetch_surfaces} zero_credit_recall={recall_is_zero_credit}"),
    ]


# --- Case 2: the IMS invariant (doc 09 §11) --------------------------------


async def ims_invariant_rows() -> list[Row]:
    """A turn where the agent NAMES ids in its structured answer that came through
    this turn's tools: an ownership owner (traversed node) cited in both a claim
    source_ref AND referenced_node_ids, a search lead named in referenced_node_ids,
    and a dismissed SDN hit cited in a claim by sanctions_id. The IMS invariant
    must hold — every NAMED id that the tools could name appears in the registry —
    and this case would FAIL if the write path dropped any of them.

    A HaluMem negative control rides along: an id that exists ONLY in the prose
    `answer` string (never a structured field) must NOT be written, proving the
    write path is structured-only (doc 09 §10)."""
    case = "ims_invariant"
    conv = _Conversation()

    subject_node = {
        "id": "sayari-subject", "name": "Subject Holdings Ltd", "label": "Entity",
        "source_system": "sayari",
        "properties": {"sanctioned": False, "pep": False, "countries": ["RUS"]},
    }
    owner_node = {
        "id": "sayari-owner", "name": "Sanctioned Owner LLC", "label": "Entity",
        "source_system": "sayari",
        "properties": {"sanctioned": True, "pep": False, "countries": ["RUS"]},
    }
    lead = {
        "entity_id": "sayari-lead", "label": "Lead Trading Co", "type": "company",
        "countries": ["CYP"], "sanctioned": False, "from_turn": 1,
    }
    sdn_hit = SanctionsHit(
        name_searched="Subject Holdings Ltd",
        matched_name="Bad Sub S.A.",
        lists=["OFAC SDN"],
        sanctions_id="ofac-55501",
        score=0.9,
        on_watchlist=True,
        countries=["ru"],
    ).model_dump()

    state: dict[str, Any] = {
        "turn_index": 1,
        "user_message": "Map Subject Holdings Ltd's ownership and sanctions exposure.",
        "intent": "ownership_analysis",
        "pinned_node_ids": [],
        "turn_nodes": [subject_node, owner_node],
        "turn_leads": [lead],
        "raw_strong_hits": [sdn_hit],
    }
    answer = TurnAnswer(
        answer=(
            "Subject Holdings Ltd is owned by Sanctioned Owner LLC. A name-only SDN "
            "match (Bad Sub S.A.) was dismissed. (A fabricated Ghost Co "
            "id=sayari-ghost-999 is mentioned in prose only.)"
        ),
        referenced_node_ids=["sayari-owner", "sayari-lead"],
        claims=[
            Claim(
                text="Subject Holdings Ltd is owned by a sanctioned entity.",
                source_refs=[SourceRef(source="sayari", sayari_entity_id="sayari-owner")],
                confidence="high",
            ),
            Claim(
                text="Bad Sub S.A. is a separate OFAC SDN-listed entity (name collision).",
                source_refs=[SourceRef(source="opensanctions", sanctions_id="ofac-55501")],
                confidence="medium",
            ),
        ],
        sanctions_hits=[],  # the SDN hit was dismissed as a collision
    )
    conv.finalize(state, summary=None, answer=answer)
    entities = conv.doc["entities"]

    # The ids the agent named through the SCHEMA (never the prose string).
    referenced = agent_graph._referenced_entity_ids(None, answer)
    # The ids this turn's structured tool outputs could name (the write path's
    # obligation). The owner + lead are in-hand; the prose-only ghost is not.
    nameable = set(agent_graph._in_hand_identity_index(state))
    must_persist = referenced & nameable
    missing = ims_invariant_violations(must_persist, entities)
    invariant_holds = (not missing) and {"sayari-owner", "sayari-lead"}.issubset(must_persist)

    # The dismissed SDN hit, cited in a claim by sanctions_id, lands in the
    # registry via the ledger — so it is re-citable on a later turn.
    sanctions_entity_present = (
        "ofac-55501" in entities and entities["ofac-55501"].get("sanctioned") is True
    )

    # The traversed owner is graph-bound (turn_nodes -> merge_graph); the lead is
    # registry-only (named but not traversed) — both recoverable, per Phase A.
    graph_node_ids = {n["id"] for n in state["turn_nodes"]}
    traversed_on_graph = "sayari-owner" in graph_node_ids
    lead_in_registry_only = "sayari-lead" in entities and "sayari-lead" not in graph_node_ids

    # HaluMem negative control: a prose-only id must NOT be written (structured-only).
    fabricated_absent = "sayari-ghost-999" not in entities

    return [
        (case, "invariant_holds", invariant_holds,
         f"missing={sorted(missing)} must_persist={sorted(must_persist)}"),
        (case, "claim_sanctions_id_re_citable", sanctions_entity_present,
         "ofac-55501 (dismissed, cited in a claim) is a registry entity"),
        (case, "traversed_on_graph_lead_in_registry", traversed_on_graph and lead_in_registry_only,
         f"owner_on_graph={traversed_on_graph} lead_registry_only={lead_in_registry_only}"),
        (case, "prose_only_id_not_written", fabricated_absent,
         "sayari-ghost-999 (prose only) absent from registry"),
    ]


# --- Case 3: recap multi-turn (cheap) --------------------------------------


async def recap_multiturn_rows() -> list[Row]:
    """After an investigation turn that persisted real findings, a recap turn must
    (a) route to the LIGHTWEIGHT submit_answer / TurnAnswer terminator (not the
    heavy submit_summary), and (b) be answerable FAITHFULLY from durable state —
    the prior claim + the resolved sanctioned subject are recoverable via
    recall_state, so the recap can be grounded without re-running the tools."""
    case = "recap_multiturn"
    conv = _Conversation()

    subject_node = {
        "id": "sayari-gazprom", "name": "Gazprom", "label": "Entity",
        "source_system": "sayari",
        "properties": {"sanctioned": True, "pep": False, "countries": ["RUS"]},
    }
    state: dict[str, Any] = {
        "turn_index": 1,
        "user_message": "Investigate Gazprom.",
        "intent": "profile_entity",
        "pinned_node_ids": [],
        "turn_nodes": [subject_node],
        "turn_leads": [],
        "raw_strong_hits": [],
    }
    answer = TurnAnswer(
        answer="Gazprom is a Russian state-owned entity with sanctions exposure.",
        claims=[
            Claim(
                text="Gazprom is majority state-owned.",
                source_refs=[SourceRef(source="sayari", sayari_entity_id="sayari-gazprom",
                                       risk_factor="state_owned")],
                confidence="high",
            )
        ],
        sanctions_hits=[],
        report_ready=True,
    )
    conv.finalize(state, summary=None, answer=answer)

    # (a) The recap ask routes to the conversational follow-up terminator.
    prior = "Turn 1 [investigation]: subject=Gazprom (state-owned, sanctions exposure)."
    res = intent._recap_shortcut("recap what you found on Gazprom so far", prior)
    routes_followup = bool(res) and res.get("intent") == "conversational_followup"
    no_report = bool(res) and res.get("wants_report") is False

    # (b) The prior findings are recoverable, so the recap stays faithful.
    async with _recall_against(conv.doc):
        claims = await recall_state_tool("c-recap", kind="claims")
        ents = await recall_state_tool("c-recap", kind="entities")

    claim_recoverable = any(
        "state-owned" in (c.get("text") or "") for c in claims.get("items", [])
    )
    subject_recoverable = any(
        r.get("id") == "sayari-gazprom" for r in ents.get("items", [])
    )

    return [
        (case, "recap_routes_to_answer", routes_followup and no_report,
         f"intent={res.get('intent') if res else None} wants_report={res.get('wants_report') if res else None}"),
        (case, "prior_findings_recall_faithful", claim_recoverable and subject_recoverable,
         f"claim={claim_recoverable} subject={subject_recoverable}"),
    ]
