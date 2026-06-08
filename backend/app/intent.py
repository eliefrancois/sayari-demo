"""Lightweight intent router — runs BEFORE the main agent loop.

A single cheap structured classification call labels the user's turn into one
intent, which we use to (a) narrow/emphasize the INVESTIGATION-tool subset bound
for that turn and (b) inject targeted guidance into the turn message. There is
always a safe fallback to the full toolset when the classification is low-
confidence (or the router is disabled / errors out), so a misclassification can
never strand the agent without a tool it needs.

Why a real classification step (vs prompt-only): the user explicitly chose this.
It is observable (we log the chosen intent in the structured trace), keeps the
main Sonnet loop focused on a smaller surface, and adds minimal latency/credits
because it runs on a small/fast model with a tiny JSON-only output.

This module is shared by BOTH agent implementations (native + graph), exactly
like agent_common, so the two paths never drift on routing behavior.
"""

from __future__ import annotations

import logging
from typing import Any

from anthropic import AsyncAnthropic

from app.config import get_settings

log = logging.getLogger("erre.intent")

# The bounded intent taxonomy. Each maps to the INVESTIGATION tools worth binding
# for that turn (terminators are added separately and always available). The two
# "meta" intents (conversational_followup, out_of_scope) bind the full toolset —
# a follow-up can ask anything and reuses prior context, and out_of_scope is
# safest with everything available plus strong "clarify/decline" guidance.
_INTENT_TOOLS: dict[str, list[str]] = {
    "identify_entity": ["sayari_resolve", "sayari_profile", "sayari_search"],
    "profile_entity": [
        "sayari_resolve",
        "sayari_profile",
        "sayari_summary",
        "check_sanctions",
        "recall_state",
    ],
    "ownership_network": [
        "sayari_resolve",
        "sayari_profile",
        "sayari_ownership",
        "sayari_summary",
        "recall_state",
    ],
    "sanctions_screening": [
        "sayari_resolve",
        "sayari_profile",
        "sayari_watchlist",
        "check_sanctions",
    ],
    "provenance": [
        "sayari_resolve",
        "sayari_profile",
        "sayari_record",
        "search_entity",
        "get_relationships",
        "find_er_links",
    ],
    "broad_search": ["sayari_search", "sayari_resolve", "search_entity", "recall_state"],
    # Meta intents -> full toolset (None subset). Guidance still differs.
    "conversational_followup": [],
    "out_of_scope": [],
}

_INTENTS = list(_INTENT_TOOLS.keys())

# Below this confidence we don't trust the label enough to narrow tools — fall
# back to the full toolset (but still inject the guidance as a hint).
_CONFIDENCE_FLOOR = 0.55

_GUIDANCE: dict[str, str] = {
    "identify_entity": (
        "Identity/resolution turn: sayari_resolve the subject, pick the right "
        "candidate (score+identifiers+address), then sayari_profile it."
    ),
    "profile_entity": (
        "Profile turn: resolve then sayari_profile the PRIMARY subject; use "
        "sayari_summary for any secondary entity; corroborate with check_sanctions."
    ),
    "ownership_network": (
        "Ownership/network turn: after resolving + profiling, sayari_ownership "
        "(ubo='who owns it', downstream='what it owns'); sayari_summary risky owners."
    ),
    "sanctions_screening": (
        "Sanctions/PEP exposure turn: check_sanctions for DIRECT listing and "
        "sayari_watchlist for INDIRECT exposure up/down the chain. Keep OFAC "
        "list-type discipline (never blur SDN vs non-SDN). For a SUPERLATIVE/"
        "ranked ask ('most sanctioned connected entity'), rank across the FULL "
        "pool with recall_state(kind='entities', sort='severity'), state the "
        "criterion, and offer to re-sort — do not eyeball it from the graph."
    ),
    "provenance": (
        "Provenance turn: trace the source. Use sayari_record for document-level "
        "evidence behind a fact, and search_entity for ICIJ leak provenance."
    ),
    "broad_search": (
        "Broad/lead-gen turn: sayari_search to cast a wide net, then resolve+profile "
        "the leads worth pursuing. Treat results as leads, not confirmed matches. To "
        "revisit or enumerate earlier leads, call recall_state — do NOT re-run "
        "sayari_search for a search you already ran this conversation."
    ),
    "conversational_followup": (
        "Conversational follow-up: reuse node_ids from prior context; do NOT re-run "
        "search/resolve on a subject already investigated. To recall earlier leads, "
        "resolved ids, or sanctions verdicts, call recall_state instead of re-running "
        "sayari_search. For a SUPERLATIVE/ranked ask ('which connected entity is most "
        "sanctioned?'), use recall_state(kind='entities', sort='severity') to rank the "
        "FULL pool, state the criterion you used, and offer to re-sort. Answer concisely."
    ),
    "out_of_scope": (
        "Likely out of scope / ambiguous: if you have no tool for what's asked, say "
        "so plainly and ask a clarifying question — do NOT fabricate."
    ),
}

_CLASSIFY_TOOL = {
    "name": "classify_intent",
    "description": (
        "Classify the user's latest turn for an investigative corporate-risk agent. "
        "Pick exactly one intent and a confidence in [0,1]. Set wants_report=true "
        "ONLY if the user explicitly asks for a formal/compiled risk report or memo."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": _INTENTS,
                "description": (
                    "identify_entity: who/what is X. profile_entity: risk profile of "
                    "X. ownership_network: who owns / what does X control. "
                    "sanctions_screening: sanctions/PEP exposure (direct or indirect). "
                    "provenance: source/document/evidence behind a fact or leak "
                    "presence. broad_search: vague/exploratory lead-gen with no single "
                    "subject. conversational_followup: greeting, meta, or a narrow "
                    "follow-up in an ongoing conversation. out_of_scope: nothing the "
                    "agent's tools can answer."
                ),
            },
            "confidence": {
                "type": "number",
                "description": "0-1 confidence in the chosen intent.",
            },
            "wants_report": {
                "type": "boolean",
                "description": "True ONLY if the user explicitly requested a formal risk report/memo.",
            },
        },
        "required": ["intent", "confidence"],
    },
}

_SYSTEM = (
    "You are a fast intent classifier for an investigative corporate-risk agent. "
    "Read the user's latest turn (and any brief prior context) and respond ONLY by "
    "calling classify_intent. Do not investigate; do not add prose."
)


def _greeting_shortcut(msg: str) -> dict[str, Any] | None:
    """Rule-based shortcut for trivially-conversational turns — skips the LLM
    call entirely (zero latency/credits) for greetings and thanks."""
    m = msg.strip().lower().rstrip("!.?")
    if len(m) <= 2 or m in {
        "hi",
        "hey",
        "hello",
        "yo",
        "sup",
        "thanks",
        "thank you",
        "ty",
        "ok",
        "okay",
        "cool",
        "what can you do",
        "help",
    }:
        return {
            "intent": "conversational_followup",
            "confidence": 0.97,
            "wants_report": False,
            "source": "rule",
        }
    return None


async def classify_intent(user_message: str, prior_context: str = "") -> dict[str, Any]:
    """Classify a turn. Returns {intent, confidence, wants_report, source}. On
    disable/error/empty, returns a low-confidence fallback (intent=None) so the
    caller binds the full toolset."""
    fallback = {"intent": None, "confidence": 0.0, "wants_report": False, "source": "fallback"}

    s = get_settings()
    if not s.intent_router_enabled:
        return {**fallback, "source": "disabled"}

    shortcut = _greeting_shortcut(user_message)
    if shortcut is not None:
        return shortcut

    # Keep the prompt tiny: the latest turn + a short tail of prior context.
    ctx = (prior_context or "").strip()
    if len(ctx) > 600:
        ctx = "..." + ctx[-600:]
    prompt = (
        (f"PRIOR CONTEXT (recent turns, may be empty):\n{ctx}\n\n" if ctx else "")
        + f"USER TURN:\n{user_message.strip()}"
    )

    try:
        client = AsyncAnthropic(api_key=s.anthropic_api_key)
        resp = await client.messages.create(
            model=s.intent_router_model,
            max_tokens=256,
            system=_SYSTEM,
            tools=[_CLASSIFY_TOOL],
            tool_choice={"type": "tool", "name": "classify_intent"},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "classify_intent":
                args = dict(block.input or {})
                intent = args.get("intent")
                if intent not in _INTENT_TOOLS:
                    return fallback
                return {
                    "intent": intent,
                    "confidence": float(args.get("confidence") or 0.0),
                    "wants_report": bool(args.get("wants_report")),
                    "source": "llm",
                }
    except Exception as e:  # never let routing crash a turn
        log.warning("intent_classify_failed", extra={"error": str(e)})
    return fallback


def select_tool_names(result: dict[str, Any]) -> set[str] | None:
    """The INVESTIGATION-tool subset to bind for the turn, or None for the full
    toolset. None when: low confidence, fallback, or a meta intent (which maps to
    an empty subset = full toolset)."""
    intent = result.get("intent")
    if not intent or result.get("confidence", 0.0) < _CONFIDENCE_FLOOR:
        return None
    tools = _INTENT_TOOLS.get(intent) or []
    return set(tools) if tools else None


def build_guidance(result: dict[str, Any]) -> str:
    """A short guidance block (incl. the chosen intent label, so it's visible in
    the prompt and the trace) injected ahead of the turn. Empty on fallback."""
    intent = result.get("intent")
    if not intent:
        return ""
    conf = result.get("confidence", 0.0)
    narrowed = select_tool_names(result) is not None
    lines = [
        f"INTENT ROUTER: intent={intent} confidence={conf:.2f} "
        f"(tools {'narrowed' if narrowed else 'full set'}).",
        _GUIDANCE.get(intent, ""),
    ]
    if result.get("wants_report"):
        lines.append(
            "The user appears to want a FORMAL RISK REPORT — finishing with "
            "submit_summary is appropriate this turn."
        )
    return "\n".join(p for p in lines if p)
