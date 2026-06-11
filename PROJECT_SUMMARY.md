# Entity Risk Resolver: Sayari FDE Take-Home

<p align="center">
  <img src="assets/sayari-logo.png" alt="Sayari" width="200" />
</p>

## Vision

I built an investigative copilot. You give it a person or a company, it investigates across sources, and it hands back a sourced risk picture you can keep pulling on.

The data spine is three layers, each doing one job:

- **Sayari (authoritative):** ownership and UBO traversal, identifiers, risk factors, breadth the others miss. This is what turns the product into more than a leak browser.
- **ICIJ Offshore Leaks (leak provenance):** whether a name shows up in the Panama or Paradise Papers. A leak hit tells you a name appeared years ago, not who owns the company today.
- **OpenSanctions (watchlist confirmation):** direct screening against OFAC, BIS, EU, UN, and PEP lists.

You may ask why three data sources. A robust product verifies its sources from multiple vantage points. I also wanted to highlight how Sayari data can be integrated with other data sources to provide a holistic picture. Sayari answers who owns what now and which holdings are sanctioned. ICIJ and OpenSanctions corroborate from different angles.

Real investigations don't run in a straight line. You find a shell, you fork, you come back. So the UX is a branching canvas instead of a linear chat. I designed for an analyst or a journalist, someone who needs the evidence traceable along with the conclusion.

The goal was a production-ready mirror of a proof of concept, something I could hand to an analyst in an FDE engagement or spin into a product. I kept scope honest: one investigation is one subject entity, depth first.

## Architecture

<img width="2680" height="2717" alt="image" src="https://github.com/user-attachments/assets/4425fb24-7516-48c6-89b5-70093519f460" />

The agent flow:

- User message comes in
- Haiku intent router classifies the turn and narrows which tools are bound
- Sonnet runs the investigation loop
- Reasoning streams to the UI over SSE
- Turn ends on a structured terminator: `submit_answer` for a conversational reply, `submit_summary` for the formal risk report

Only an explicit "compile a report" request triggers the summary. A bare name gets an answer.

For this use case, ReAct and MemGPT were essential. ReAct handles the think-call-observe loop: what you do next depends on what the last tool returned. MemGPT handles tiered memory for the session so the agent can page structured state in and out without blowing the per-turn context budget.

**Redis (Upstash) as the state layer.** What lives where:

- **L1 working memory:** per-conversation `state_doc`, merged each turn
- **Branching:** turn tree, per-turn deltas, path-scoped graph keys so sibling branches stay isolated
- **Session index:** conversation list for the history menu

The system is modular. Tools, the data layer, and SSE shapes stay stable. Adding a new tool or swapping the agent orchestration layer is straightforward.

## Memory

I first started playing with how to handle memory for this project and I ran into an early bug. I shipped a prose-summary memory first, one digest line per turn. It was cheap and felt fine until it dropped a sanctioned subsidiary on a recall turn. Turn 1 surfaces `Rosneft Trading S.A.` through `check_sanctions`, the agent correctly dismisses it as a name collision against the SDN-listed entity, and names it in prose. Turn 2 asks which subsidiaries were sanctioned, the agent answers from memory with zero tool calls, and the name is gone. It was never written to durable state, only narrated.

That's a write-path bug. A bigger context window or a vector store fixes nothing, because you can't read back a fact you never wrote. I needed a deterministic way to deposit retrieved info into memory, not just narrate it.

So I rebuilt the write path. A deterministic projection runs at the end of every turn and deposits only from structured outputs: parsed tool JSON and the validated terminator schema, never the prose answer. Scraping the narrative would write the model's name collisions straight into memory and treat them as fact on every later turn. Everything lands in an id-keyed entity registry with provenance: where each name came from, which turn, its source refs. Dismissed sanctions hits get kept too, with the verdict attached. I also added a `recall_state` tool so the agent pages exact rows back on demand. Now turn 2 recovers `Rosneft Trading S.A.` byte-exact and spends no `check_sanctions` call doing it.

## Tools

The talking point is `recall_state`: the agent querying its own structured memory for the conversation, by kind and filter, with no external call and no credits spent. Ask for the most sanctioned connected entity and it ranks the full pool by severity, OFAC SDN first, then distinct sanctions regimes, then PEP. That ranking is how you compare an ownership neighbor against a sanctions hit when they live in different layers.

Below are some helper tools at the agent's disposal:

- `sayari_resolve` turns a raw name into ranked Sayari candidates with identifiers. It deliberately returns candidates because the top score isn't always canonical.
- `sayari_ownership` walks the ownership graph, downstream for what an entity controls, UBO for who controls it.
- `check_sanctions` screens against OFAC, BIS, EU, UN and PEP lists via OpenSanctions, and reports the program verbatim so a non-SDN hit never gets upgraded to SDN.
- `search_entity` is the ICIJ leak lookup, full-text over the Offshore Leaks graph for provenance and corroboration.
- `sayari_record` fetches the underlying source record by id, so a finding traces to a primary document.

The ICIJ tools sit on Neo4j: a Lucene full-text index over ~2M nodes as the entry point, then Cypher walks the neighborhood, shared addresses, and ICIJ's cross-leak entity-resolution links.

One build fix doubles as an FDE lesson. The intent router was quietly forbidding steps the prompt ordered. Haiku classifies each turn and narrows which tools are available based on the user prompt. Agent behavior comes from the whole contract: the prompt, tool descriptions, and what's bound. Early on, most narrowed tool subsets excluded `check_sanctions` and `search_entity`, so the agent couldn't corroborate across sources even when the prompt told it to. I made sure corroboration tools stay available on investigative turns.

## UX

The surface is a branching investigation canvas. Each turn is a node. Forking from any prior turn starts a new branch that sees only its own path's state; sibling branches stay invisible to each other, and that holds for injected context, `recall_state` reads, and the evidence graph alike. Time travel renders the graph as it stood at any turn. Entities click into a detail panel, claims highlight back to the nodes they reference, and the whole investigation exports to a PDF report. The evidence graph groups nodes by subject with convex hulls and semantic branch labels on the canvas so a long investigation stays readable when multiple entities share the view.

## Evals

The golden dataset is 12 curated cases in LangSmith:

- Both terminators (`submit_answer` and `submit_summary`)
- Sanctioned vs name collision vs non-SDN labeling discipline
- No false positives on clean entities (Epstein, Spotify)
- Provenance on every claim
- Not-found and clarify negatives
- Tool-path coverage for watchlist, record, and search tools

The runner is `langsmith_eval.py`. It pulls the live dataset, runs each case through the real agent turn, and scores with 6 reference evaluators (terminator kind, found, report-ready, sanctions status with labeling discipline, expected-entity recall, a must-not structural guard), an optional Anthropic LLM judge for faithfulness and coverage, and per-case deterministic checks reused from the local regression suite. Two standalone evals ride along: recall-over-distance, which establishes the Rosneft subsidiaries on turn 1, runs intervening turns, then asserts they're still recoverable, and a token-budget guardrail proving the injected core stays flat as the investigation grows.

| Case | Input | What it tests |
| --- | --- | --- |
| gazprom_sayari_risk | Investigate Gazprom (16 Nametkina St. Moscow) | Full Sayari risk profile, factors with traversal paths, sanctions, report-ready |
| roldugin_conversational | Sergey Roldugin | Bare-name conversational turn on a genuinely sanctioned individual |
| epstein_no_false_sanctions | Jeffrey Epstein | Notorious but not sanctioned, no invented watchlist hit |
| nonsense_not_found | Zzqwlx Nonexistent Holdings 99127 | Not found, no claims, anti-hallucination |
| vague_clarify | Tracing hidden Russian money, no name yet | Too vague, asks a clarifying question instead of guessing |
| huawei_sayari_profile | Investigate Huawei Technologies Co. Ltd. (Shenzhen) | Export-controlled, labeled non-SDN, never promoted to OFAC SDN |
| spotify_clean_lowrisk | lets look into Spotify | Resolves to Spotify AB under Spotify Technology S.A., clean, no false hits |
| name_match_hedge_rosneft_global | Profile Rosneft Global Trade S.A. (Luxembourg) | Hedges the name collision against SDN-listed Rosneft Trading S.A. |
| gazprom_ownership_ubo | Who owns Gazprom? | UBO traversal shows majority Russian state ownership, sourced |
| explicit_report_request | Investigate Gazprom and compile a formal risk report | Explicit report request terminates on submit_summary |
| watchlist_indirect_exposure | Map Huawei's indirect exposure via watchlist traversal | Uses the Sayari watchlist traversal, labels accurately |
| record_provenance | Profile Gazprom, then fetch the underlying source record | Fetches the source record by id for document-level provenance |

The "Who owns Gazprom?" smoke run swept all reference graders and the extra evals passed 5/5, proving the wiring end to end.

### Results

I ran the full 12-case golden set live against two models: Claude Sonnet 4.5 (`claude-sonnet-4-5-20250929`, the default) and Claude Haiku 4.5 (`claude-haiku-4-5-20251001`). One live run per model, n=12, so read the margins as directional, not statistically tight.

Performance held up well across the board. Most reference evaluators landed between 0.83 and 1.0, the local deterministic regression suite came back 67/67, and both standalone evals passed: recall-over-distance (3/3) and the token-budget guardrail (2/2).

The interesting part is the two-model comparison. Haiku 4.5 matched or beat Sonnet 4.5 on 4 of the 6 reference evaluators, including sanctions labeling and the must-not structural guard, at about half the tokens and roughly 2.3x faster. That's exactly the kind of finding the harness exists to surface.

Reference evaluators, averaged over the 12 cases:

| Evaluator | Sonnet 4.5 | Haiku 4.5 |
| --- | --- | --- |
| terminator_kind_match | 1.000 | 0.917 |
| found_match | 0.917 | 0.917 |
| report_ready_match | 0.833 | 0.917 |
| sanctions_status_match | 0.750 | 1.000 |
| expected_entities_recall | 0.917 | 0.833 |
| must_not_absent | 0.667 | 0.917 |

Cost and latency: Sonnet 4.5 averaged about 96s and ~128,990 tokens per case; Haiku 4.5 about 42s and ~68,067 tokens per case. Roughly half the tokens and about 2.3x faster. For a screening pass where a human reviews the output anyway, that tradeoff is worth a real look.

Both runs live in LangSmith as separately named experiments (`sayari-demo-sonnet-4-5-*` and `sayari-demo-haiku-4-5-*`) so you can pull them up side by side.

There's also a stricter per-case metric that only counts a case as clean when every evaluator passes on it; I don't lead with it because it's all-or-nothing, so a single miss on any grader fails the whole case.

## Findings

### What the evals surfaced

The harness did its job. A few things it flagged:

- A real tool-binding gap on the record-provenance case: the agent wasn't fetching the underlying record. I fixed it and reverified, and it now passes on both models.
- The cost/accuracy tradeoff between a larger and a smaller model, with the smaller one staying competitive.
- Export-controlled and non-SDN labeling as the hardest area to get exactly right, which is the discipline I care most about.

The record-provenance case is worth a closer look because it failed the same way on both models, for a structural reason rather than a model one. A confident intent classification narrowed the toolset and left the agent without `sayari_record` bound, so it couldn't fetch the source record the case asks for. The profile also wasn't surfacing a clean, fetchable record id, so even with the tool there was nothing good to hand it. I fixed both and reverified: the record tool now stays available on profiling turns, and the profile surfaces a fetchable record id. That case now passes on both models.

It's the same class of failure as the earlier intent-router contract bug, where the router quietly forbade a step the prompt ordered. Same lesson too: agent behavior is the whole contract, and the router can't narrow away a tool the task needs.

## Future enhancements

The arc from here keeps the same shape, one subject entity and evidence first, but takes the analyst off the keyboard.

- **Autonomous monitoring.** Flag an entity and the agent watches it on a schedule or on demand. I'd add Sayari's Negative News beta endpoint (`GET /v1/negative_news`) as a new tool: it screens entity names against news and public records with ML-classified risk topics (sanctions, financial, environmental, and others) and returns articles with risk flags and source links. Pair that with the existing sanctions, ownership, and leak tools. A new designation, ownership change, leak appearance, or adverse media hit triggers an alert that says what moved and why it matters, instead of the analyst re-running the same investigation by hand.
- **Batch intake.** Upload a list and it spins out one branch per entity and runs them in parallel. You triage a whole portfolio at once rather than typing names one at a time.
- **Context upload, confirm or deny.** Drop in a document or a set of assumptions about an entity and the agent checks each claim against the data, marking it confirmed, refuted, or unverifiable, with sources attached.

On the technical side, two I'd pull forward:

- **Cross-source entity resolution.** What I shipped is subject grouping: nodes tagged to investigation subjects, convex hulls on the canvas, shared intermediates surfaced in overlap zones. That's a visualization and scoping layer. Collapsing an ICIJ node and a Sayari node for the same real-world entity into one canonical id, so the graph stops double-counting, is still future work.
- **Episodic memory across sessions.** The L2 vector store exists behind a flag but I left it off for this demo. L1 structured memory was the priority for recall fidelity within a session, and episodic adds latency and cost on every turn. The flag is there when cross-session fuzzy recall matters more than that tradeoff.
