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
        "recall_memory",
    ],
    "ownership_network": [
        "sayari_resolve",
        "sayari_profile",
        "sayari_ownership",
        "sayari_summary",
        "recall_state",
        "recall_memory",
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
    "broad_search": [
        "sayari_search", "sayari_resolve", "search_entity", "recall_state", "recall_memory",
    ],
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
        "search/resolve on a subject already investigated. The INVESTIGATION STATE "
        "core is navigation hints only, NOT the full record. For any EXACT or COMPLETE "
        "ENUMERATION ('list all sanctioned subsidiaries', 'which leads were there', "
        "'name every connected entity', 'the dismissed matches'), call recall_state for "
        "the exact rows instead of answering from the thin core or re-running tools: "
        "kind='sanctions' (confirmed AND dismissed verdicts), kind='leads' "
        "(from_turn/index), kind='entities' (the full pool). For a SUPERLATIVE/ranked "
        "ask ('which connected entity is most sanctioned?'), use "
        "recall_state(kind='entities', sort='severity') to rank the FULL pool, state "
        "the criterion you used, and offer to re-sort. Answer concisely."
    ),
    # Recap-specific overlay (appended on top of the followup guidance when the
    # rule-based recap detector fires). A recap is a conversational READBACK of
    # the investigation so far, NOT a request for the formal deliverable.
    "recap": (
        "RECAP ask ('summarize what you found so far', 'recap', 'what do we have so "
        "far', 'give me the rundown'): this is a conversational readback, NOT a "
        "request for the formal risk report. Finish with submit_answer (TurnAnswer), "
        "writing the recap as the `answer` markdown narrative grounded in the "
        "INVESTIGATION STATE core and recall_state (kind='entities'/'sanctions'/"
        "'leads' for exact rows) — do NOT re-run the investigation and do NOT call "
        "submit_summary. Put any factual assertions in `claims` for provenance. When "
        "the conversation has a resolved subject plus a substantive risk/ownership/"
        "sanctions signal, set report_ready=true and offer_risk_report=true with a "
        "one-sentence risk_report_prompt, so the user can still get the formal card "
        "if they want it. Only switch to submit_summary if they EXPLICITLY ask to "
        "'generate the report'."
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
        "ONLY if the user explicitly asks to generate/compile a formal risk report, "
        "memo, or full risk profile (e.g. 'generate a risk report', 'write it up', "
        "'compliance memo'). A RECAP ask ('summarize what you found so far', 'recap', "
        "'what do we have so far', 'give me the rundown') is NOT a report request — it "
        "is a conversational_followup, so set wants_report=false for it."
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


# Rule-based recap detection. A recap is a conversational readback of the
# investigation so far; it must route to submit_answer (TurnAnswer), never the
# heavy submit_summary deliverable. Detecting it with a tight rule (instead of
# trusting the LLM not to over-read the word "summarize") makes the routing
# deterministic and testable, and guarantees wants_report stays false.
_RECAP_PHRASES = (
    "recap",
    "rundown",
    "run down",
    "catch me up",
    "where do we stand",
    "where are we",
)
# "so far" / "so far?" paired with a recall verb is the other strong recap tell
# ("summarize everything you found so far", "what do we have so far").
_RECAP_SO_FAR_VERBS = ("summar", "found", "have", "know", "got", "got so", "discover")


def _is_recap(msg: str) -> bool:
    """True for conversational-readback asks ('summarize what you found so far',
    'recap', 'what do we have so far', 'give me the rundown'). Deliberately tight
    so a real investigation ask like 'summarize Rosneft's ownership structure'
    does NOT match: a bare 'summarize X' with a subject is a profile turn, not a
    recap. Only fires as a recap when paired with a so-far/where-are-we framing or
    an explicit recap word."""
    m = msg.strip().lower()
    if not m:
        return False
    if any(p in m for p in _RECAP_PHRASES):
        return True
    if "so far" in m and any(v in m for v in _RECAP_SO_FAR_VERBS):
        return True
    return False


def _recap_shortcut(msg: str, prior_context: str) -> dict[str, Any] | None:
    """Route a recap to conversational_followup with wants_report=false and a
    recap flag (drives the recap guidance overlay). Only fires mid-conversation
    (prior_context present) — on turn 1 there is nothing to recap, so the phrase
    is treated as a normal turn and classified by the model."""
    if prior_context.strip() and _is_recap(msg):
        return {
            "intent": "conversational_followup",
            "confidence": 0.95,
            "wants_report": False,
            "recap": True,
            "source": "rule",
        }
    return None


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

    recap = _recap_shortcut(user_message, prior_context)
    if recap is not None:
        return recap

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
    # Recap overlay: a readback ask finishes with submit_answer, never the heavy
    # report. Appended on top of the followup guidance so the instruction is
    # explicit and unambiguous at routing time.
    if result.get("recap"):
        lines.append(_GUIDANCE["recap"])
    elif result.get("wants_report"):
        lines.append(
            "The user appears to want a FORMAL RISK REPORT — finishing with "
            "submit_summary is appropriate this turn."
        )
    return "\n".join(p for p in lines if p)
