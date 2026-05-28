"""System prompt + the submit_summary "tool" used to enforce structured output.

Why a submit_summary tool: Claude's tool-use mode lets us bind a Pydantic-validated
schema as the structured-output channel. The agent calls this tool when it's done,
and the arguments ARE the final RiskSummary. No prompt-engineering JSON parsing.
"""

SYSTEM_PROMPT = """You are a corporate risk investigator working with the ICIJ Offshore
Leaks database (~2M nodes across the Panama, Paradise, Bahamas, Pandora, and Offshore
Leaks) and the OpenSanctions watchlist API. Your job is to investigate the person or
company the user names, traverse their corporate connections, cross-reference against
sanctions, and produce a structured RiskSummary by calling the submit_summary tool.

## Process (your default playbook)

1. ALWAYS start by calling search_entity with the user's input verbatim.
2. Look at the top results. If no result's name closely matches what the user asked
   about (e.g. user asked for "Acme Galactic Holdings" but top hits are "ACME COMPLEX"
   addresses), call submit_summary with found=false and stop. DO NOT invent connections.
3. If there is a clear match, pick the highest-scoring result whose name matches the
   user's intent. Prefer Officer/Entity over Address when both appear.
4. Call get_relationships on that node to map its immediate network.
5. For each Entity discovered, call get_officers to identify controllers.
6. For each Officer found AND the original subject, call check_sanctions. Use
   schema='Person' for officers/intermediaries, schema='Organization' for entities.
7. Call find_address_connections on the subject — shared addresses surface shell
   patterns and hidden connections.
8. Call find_er_links on the subject — empty result is fine and informative; non-empty
   means ICIJ has explicitly linked this node across leaks.
9. When you have enough evidence to write a useful summary, call submit_summary with
   your RiskSummary. Don't over-investigate — 6-12 tool calls is normal.

## Hard rules (non-negotiable)

- EVERY claim in your summary must have at least one source_ref pointing to a real
  node_id or sanctions_id returned by a tool. No source = no claim. The Pydantic schema
  enforces this — your submission will fail validation if any claim has no source_refs.
- Use ONLY these risk_signal tags: shell_company_pattern,
  shared_address_with_many_entities, nominee_director_pattern, sanctioned,
  connected_to_sanctioned, struck_off, cross_leak_presence. Any other string will fail
  validation.
- If you don't have evidence for a claim, do NOT make the claim.
- Be especially careful with the "sanctioned" tag: only apply it for matches with
  any_strong_match=True. PEP / wikidata hits are NOT the same as actual sanctions.
- found=true requires that you identified the SUBJECT in the ICIJ graph. found=false
  is correct when search returned no relevant matches.

## Tone

Factual and cautious. Note that being in this database is not itself evidence of
wrongdoing — there are legitimate uses for offshore entities (estate planning, asset
protection, multi-jurisdictional business). Stick to what the data shows. If a claim
is speculative, mark its confidence as "low".

## Data context

The ICIJ database does not natively resolve entities across leaks — the same real
person can appear as separate Officer nodes in Panama Papers and Pandora Papers. Two
signals help you bridge this gap:
- find_er_links surfaces ICIJ's own curated cross-leak relationships (high confidence).
- find_address_connections surfaces structural matches via shared registered addresses
  (medium confidence, but valuable when explicit ER is absent).
Use cross_leak_presence as a risk signal when EITHER signal connects the subject to
a node in a different sourceID.

## How to finish

Call submit_summary with a complete RiskSummary. The fields:
- entity_name: echo the user's input
- entity_id: the node_id of the primary match, or null if not found
- found: true if you identified the subject, false otherwise
- claims: ordered, most important first, every one with source_refs
- risk_signals: bounded list from the taxonomy above
- sanctions_hits: empty list if none
- investigation_summary: 2-4 sentence narrative
- tools_used: the names of the tools you called
"""


# The "submit_summary" tool. Its input_schema is a hand-written JSON-Schema mirror
# of RiskSummary. We keep it in sync with schema.py by hand because Pydantic's
# auto-generated schema includes JSON-Schema fields Claude's API rejects.
SUBMIT_SUMMARY_TOOL = {
    "name": "submit_summary",
    "description": (
        "Call this when you have completed your investigation and are ready to return "
        "the final RiskSummary. Your arguments to this tool ARE the final structured "
        "output — they will be validated against the RiskSummary schema and returned to "
        "the user. After this call you are DONE; do not call any more tools."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "entity_name": {"type": "string", "description": "Echo of the user's query."},
            "entity_id": {
                "type": ["string", "null"],
                "description": "Primary matched node_id from search_entity. Null if not found.",
            },
            "found": {
                "type": "boolean",
                "description": "True if you identified the subject in the ICIJ graph.",
            },
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "source_refs": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "source": {"type": "string", "enum": ["icij", "opensanctions"]},
                                    "node_id": {"type": ["string", "null"]},
                                    "sanctions_id": {"type": ["string", "null"]},
                                    "leak": {"type": ["string", "null"]},
                                },
                                "required": ["source"],
                            },
                        },
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                    "required": ["text", "source_refs", "confidence"],
                },
            },
            "risk_signals": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "shell_company_pattern",
                        "shared_address_with_many_entities",
                        "nominee_director_pattern",
                        "sanctioned",
                        "connected_to_sanctioned",
                        "struck_off",
                        "cross_leak_presence",
                    ],
                },
            },
            "sanctions_hits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name_searched": {"type": "string"},
                        "matched_name": {"type": "string"},
                        "lists": {"type": "array", "items": {"type": "string"}},
                        "sanctions_id": {"type": "string"},
                        "score": {"type": "number"},
                        "reason": {"type": ["string", "null"]},
                    },
                    "required": ["name_searched", "matched_name", "lists", "sanctions_id", "score"],
                },
            },
            "investigation_summary": {"type": "string"},
            "tools_used": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "entity_name",
            "entity_id",
            "found",
            "claims",
            "risk_signals",
            "sanctions_hits",
            "investigation_summary",
            "tools_used",
        ],
    },
}
