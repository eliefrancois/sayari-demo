# Entity Risk Resolver: FDE Take-Home Submission

<p align="center">
  <img src="assets/sayari-logo.png" alt="Sayari" width="160" />
</p>

## Approach

I built **Entity Risk Resolver**, an investigative copilot for analysts and journalists. You give it a person or company, it investigates across sources, and it returns a sourced risk picture you can keep pulling on.

I chose a hybrid of Scenario 1 (enrichment) and Scenario 2 (analytics). Sayari is the authoritative spine: entity resolution, ownership and UBO traversal, risk factors, watchlist traversal, shortest path, and source records. ICIJ Offshore Leaks adds leak provenance (whether a name appeared in the Panama or Paradise Papers). OpenSanctions confirms watchlist hits against OFAC, BIS, EU, UN, and PEP lists. The three sources corroborate each other; they are not merged into a single entity record. Sayari answers who owns what now. The others confirm from different angles.

Real investigations fork. You find a shell company, follow a lead, come back. So the UX is a branching canvas, not a linear chat. Each turn is a node. Forking from any prior turn starts a new branch with path-scoped state. Sibling branches stay isolated. The evidence graph groups entities by subject so a long investigation stays readable.

The agent flow is straightforward. A Haiku intent router classifies each turn and narrows the toolset. Sonnet runs the investigation loop, calling tools and streaming reasoning to the UI over SSE. The turn ends on a structured terminator: `submit_answer` for a conversational reply, or `submit_summary` for a formal risk report. Only an explicit report request triggers the summary. Structured memory deposits facts from tool JSON and terminator schemas, not prose, so recall stays faithful across turns.

<img width="480" height="487" alt="Architecture" src="https://github.com/user-attachments/assets/4425fb24-7516-48c6-89b5-70093519f460" />

## Assumptions

- **One subject per investigation, depth first.** Each branch tracks one entity at a time. I kept scope honest rather than building a portfolio triage tool.
- **Three sources corroborate, not merge.** Sayari owns the current ownership graph. ICIJ and OpenSanctions add provenance and confirmation from different angles.
- **Conversational by default.** A bare name gets an answer. A formal risk report only when the user asks for one.
- **Production-ready PoC.** I treated this as something I could hand to an analyst in an FDE engagement, not a demo that cuts corners on memory, evals, or provenance.

## Challenges

**Memory write-path.** My first memory design was a prose summary, one digest line per turn. It worked until it didn't. On a Rosneft investigation, turn 1 surfaced `Rosneft Trading S.A.` through sanctions screening. The agent correctly dismissed it as a name collision against the SDN-listed entity and said so in prose. Turn 2 asked which subsidiaries were sanctioned. The agent answered from memory with zero tool calls, and the name was gone. It was never written to durable state, only narrated. That's a write-path bug. A bigger context window fixes nothing if you never wrote the fact down. I rebuilt the write path as a deterministic projection from structured tool outputs and terminator schemas into an id-keyed entity registry with provenance. Dismissed hits stay in memory with the verdict attached. A `recall_state` tool lets the agent page exact rows back on demand.

**Intent-router contract.** The Haiku router narrows which tools are bound each turn. Early on, investigative turns often dropped corroboration and record-fetch tools even when the prompt ordered them. The agent couldn't cross-check ICIJ or OpenSanctions, and on one eval case it couldn't fetch the underlying source record the user asked for. Agent behavior is the whole contract: prompt, tool descriptions, and what's actually bound. I fixed tool binding so investigative turns keep corroboration and record tools available. The eval harness caught the record-provenance gap; I fixed it and reverified.

## Sayari Value and Validation

ICIJ tells you a name appeared in a leak years ago. OpenSanctions tells you whether a name matches a list entry. Sayari tells you who owns what today, what's sanctioned, and how entities connect. That's why it's the spine.

Concrete capabilities I used: entity resolution with ranked candidates, ownership and UBO traversal, risk factor profiles, watchlist traversal for indirect exposure, shortest path between entities, and source record fetch for document-level provenance. Without Sayari, this is a leak browser with a sanctions checker. With it, an analyst gets a live ownership picture tied to primary documents.

I validated with a 12-case golden dataset in LangSmith plus a 67/67 local deterministic regression suite. Cases cover both terminators, sanctioned vs name-collision vs non-SDN labeling, clean entities with no false positives, provenance on every claim, and negative cases (not found, too vague). Live runs compared Sonnet 4.5 and Haiku 4.5. Most reference evaluators landed between 0.83 and 1.0. Haiku stayed competitive at roughly half the tokens and 2.3x faster, which matters for a screening pass where a human reviews the output anyway. The harness also caught the tool-binding gap on record fetch, which I fixed and reverified on both models.

| | Sonnet 4.5 | Haiku 4.5 |
| --- | --- | --- |
| Reference evaluators (avg) | 0.83–1.0 | 0.83–1.0 |
| Latency / tokens per case | ~96s / ~129k | ~42s / ~68k |

Next steps I'd pitch to a client: batch intake for portfolio triage, and adverse media monitoring via Sayari's Negative News API paired with the existing sanctions and ownership tools.
