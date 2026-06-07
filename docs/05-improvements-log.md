# Improvements & Decisions Log

This is my running record of what I changed in the Entity Risk Resolver, and more importantly why. Git history tells you what the diff was. This tells you what I was thinking: the problem I was staring at, the call I made, and how it ties back to the thing I'm actually building.

The product is an investigative copilot over corporate ownership and sanctions data. It pulls from three sources: the Sayari API (authoritative ownership, identifiers, risk), the ICIJ leak graph in Neo4j (leak provenance), and OpenSanctions (direct watchlist confirmation). This is a demo I built for a Sayari interview, so a lot of these decisions are about making the thing trustworthy and legible to an analyst, not just functional.

Newest first. Each entry tries to cover three things: what I changed, why the old behavior was wrong, and how it connects to the bigger goal.

---

## 2026-06-04: Widen the memory write path (IMS Phase A)

This is the first slice of the Investigation Memory Subsystem from `docs/09-investigation-memory-subsystem.md`. The reframe in doc 09 is the important part: the agent forgetting things it already found is a WRITE-path bug, not a retrieval bug. You cannot read back a fact you never wrote, so a bigger context window or a vector store fixes nothing. The cross-turn projection in `_build_state_delta` was depositing from a deliberately narrow set of sources, and two specific facts were falling through the cracks on the way to Redis.

Gap (a): answer-turn dismissed sanctions hits were dropped. When the agent runs `check_sanctions`, a strong match it then DISMISSES as a name collision (think "Rosneft Trading S.A." matching the subject by name only) is exactly the kind of finding the analyst asks about again two turns later. Investigation turns already captured both the confirmed and the dismissed strong hits via `build_sanctions_review`. But the answer-turn branch only read `answer.sanctions_hits`, which by construction holds just the hits the agent kept. So on the conversational-default path, which is where most turns now end, the dismissed subsidiary was computed and then thrown away. It vanished by the next turn. The fix points `build_sanctions_review` at whichever terminator the turn produced (both `RiskSummary` and `TurnAnswer` carry `sanctions_hits`), so the dismissed set lands in `sanctions_adjudicated` with `verdict: dismissed` on either turn type. Now a turn-2 follow-up recalls it via `recall_state(kind="sanctions")` with zero new tool calls.

Gap (b): entities named through structured terminator fields were deposited nowhere. The agent can lean on an entity by ID through `referenced_node_ids`, a claim's `source_refs`, or a `sayari_risk_factors` traversal path without ever traversing it onto the graph as a `turn_node`. Those IDs were not written to `resolved_entities` or `named_ids`, so an entity the agent clearly knew about (it named it, with an ID) silently failed to persist. The fix builds an in-hand identity index from the turn's traversed nodes and full lead lists, then deposits any referenced ID it can name from that index into `resolved_entities` plus the id-keyed `named_ids` cache. IDs that are referenced but not nameable in hand are left for the bounded resolver. No new Sayari calls in this phase.

The discipline that makes this safe is the one doc 09 is most emphatic about: deposit ONLY from structured outputs. Parsed tool JSON, `raw_strong_hits`, and the validated `submit_answer` / `submit_summary` schema fields. The prose `answer` markdown is never a write source. Scraping the narrative for "entities the agent mentioned" is the HaluMem trap: you would write the model's hallucinations and name collisions straight into durable memory, and every later turn would treat them as fact. So gap (b) reads `referenced_node_ids` and `source_refs` and risk-path IDs (typed fields), never the prose. It is all deterministic, no extra model calls.

Why it connects to the product: the whole pitch is sourced, traceable evidence that survives the conversation. An analyst who asks "which of those subsidiaries were sanctioned again?" should get the exact name back, including the one the agent correctly dismissed, without the tool re-spending a sanctions check or, worse, quietly forgetting. This converts a silent recall bug into something the write path actually retains.

On verification: the live LLM eval harness cannot observe this fix. `evaluate_turn` runs with `persist=False`, so `finalize_node` returns before `merge_state_doc` is ever called and `_build_state_delta` is never reached, which means none of the existing cases exercise the changed code path (and so none can regress from it). I pinned the Rosneft case as a deterministic unit-style check in `run_evals.py` instead: a synthetic answer turn where a strong hit is dismissed and a subsidiary lead is referenced by ID, asserting both land in the projection. It passes, runs instantly, and burns no credits. Compile and import are clean on the changed modules.

Status: deployed to `sayari-demo-backend` on Cloud Run (revision `sayari-demo-backend-00026-wbr`), `/health` 200 with `agent_impl=graph` and all deps ok. Everything stays within the current `state_doc` bucket shapes. The Phase B bucket rename (`entities` / `lead_sets` / `sanctions_ledger` / `claims`) and the Phase C injection shrink are deliberately later phases, not touched here.

Files: `backend/app/agent_graph.py` (`_build_state_delta` gap (a) + gap (b), new `_in_hand_identity_index` and `_referenced_entity_ids` helpers, `named_ids` in the delta), `agent_common.py` (`build_sanctions_review` now accepts both terminator shapes), `evals/run_evals.py` (`_memory_writepath_rows` deterministic regression).

One reconciliation note for Phase B: the brief expected gap (b) to also name referenced IDs from the prior-turn `named_ids` cache, but `_build_state_delta` only has the current `TurnState` in hand (the cache lives in Redis and is unioned later by `merge_state_doc`). I scoped gap (b) to in-hand turn data (traversed nodes plus leads), which is the genuine narrow gap, and left cross-turn cache naming to the existing tool-time `_known_entity_lookup`. Phase B unifies all of this into the one id-keyed registry anyway.

---

## 2026-06-04: Bounded batch-resolve for hub-entity risk-path blobs

A follow-on to the anonymous-node fix below. Naming path nodes from the profile's relationships block works great for a normal entity, but it falls apart on a hub. Profile Gazprom and the risk-factor "show your work" overlay fills with a dozen-plus `Other: Unresolved entity (…id)` blobs. The reason is structural: a derived risk factor's `traversal_path` runs many hops out from the subject, but the only names I had in hand came from the profile's 1-hop `relationships` block, which is also page-capped. For a hub with thousands of relationships, almost none of the multi-hop path IDs land in that 1-hop lookup, so they fall back to the placeholder. The single most decision-relevant nodes, the far sanctioned/PEP entities the factor is actually about, were the ones rendering anonymous.

The call was to RESOLVE the names rather than drop the structure. I kept every hop and edge of the risk paths and added a bounded batch-resolver that looks up the unnamed path IDs via Sayari and folds them into the same `id_lookup` before the nodes get built. The graph stays complete and gets named. The work is bounded on purpose, because this is the one place credits and latency can run away on a hub. There is no batch entity endpoint in the SDK, so `resolve_unnamed_ids` fans out cheap, relationship-free `entity_summary` calls across a small thread pool (the client is sync and these are I/O-bound HTTP), capped at `_MAX_RISK_PATH_RESOLUTIONS` (12). When there are more unnamed IDs than the cap, it spends the budget where it matters: `unnamed_risk_path_ids` ranks the unknown nodes by DEGREE in the assembled risk-path graph, so the most-connected hubs of the chains get named first and the leaf flotsam is what keeps a placeholder. Per-ID failures fail open, a timeout on one ID just leaves that node unresolved instead of crashing the investigation, and a freshly resolved name never overwrites richer data already in hand.

I also made the spend compound. The resolved names get persisted into the conversation `state_doc` under a new `named_ids` cache, which `_known_entity_lookup` now reads first. So the second time any turn touches the same multi-hop node, it reuses the cached label for free instead of paying for `entity_summary` again. Over a multi-turn investigation of one hub, the bounded cost amortizes toward zero.

Why it connects to the product: the North Star is that the evidence graph is legible and consistent with the agent's prose. A wall of anonymous blobs at the exact hops the risk factor is trying to explain is the opposite of that. Now a hub profile renders named, typed nodes (with the right label and color via `_coerce_label` instead of "Other"), the analyst can read the ownership/control chain end to end, and the cost of getting there is capped and cached.

On a synthetic Gazprom-shaped profile (one central hub node across three multi-hop paths plus a remote three-hop chain, one node pre-named in hand), the overlay went from 6 unresolved nodes out of 8 to 0, with the central hub correctly resolved first and the cap honoring degree order. The genuinely-unresolvable case (beyond the cap, or on error) still keeps the clear `Unresolved entity (…id)` placeholder, never an invented name.

One note on verification: the eval harness asserts on the agent's structured answer/summary, not on graph nodes, so no eval check can actually observe this change (it touches `nodes`/`edges` naming, which `evaluate_turn` never returns). I leaned on a deterministic synthetic trace for the naming behavior and used the live eval run only to confirm the tool plumbing didn't regress: every `sayari_profile`/`sayari_summary` call across the suite ran through the new resolve path without crashing. Adding a node-level regression assertion would mean plumbing graph nodes into the eval output, which is more machinery than this fix warrants.

Status: deployed to `sayari-demo-backend` on Cloud Run (revision `sayari-demo-backend-00025-4v5`), `/health` 200 with `agent_impl=graph` and all deps ok. The `named_ids` state_doc bucket is additive and forward-compatible (the `get_state_doc` reader already backfills missing keys), so older stored docs keep working.

Files: `backend/app/sayari.py` (`_MAX_RISK_PATH_RESOLUTIONS`, `_RISK_PATH_RESOLVE_WORKERS`, `unnamed_risk_path_ids`, `resolve_unnamed_ids`), `tools.py` (`_resolve_and_map_risk_paths`, wired into `sayari_profile_tool`/`sayari_summary_tool`, `_known_entity_lookup` reads `named_ids`), `conversations.py` (`named_ids` bucket in the state_doc + its merge rule).

The bigger picture: this resolver and the anonymous-node fix below are the *same* bug twice (a graph node that's just an ID with no name in hand). The durable fix is a single canonical id→identity registry every source deposits into and every mapper reads from, with an invariant that no node renders without going through it. The bounded resolver here is the `resolve()` arm of that design. Full write-up in `docs/08-entity-registry.md`.

## 2026-06-04: Named the anonymous risk-path node, and made the lead badge a real toggle

Two graph-legibility fixes that surfaced during testing.

First, the provenance bug. After profiling an entity, the evidence graph showed a connected node labeled `Other: …JcvPXQ` with no real name. That node is the far end of a risk factor's traversal path, the related entity that makes the subject "sanctioned-adjacent," so it's the single most decision-relevant node on the canvas and it was rendering anonymous. The cause: `risk_paths_to_neighborhood` builds path nodes straight from the factor's `traversal_path`, which only carries entity IDs and relationship types, not names. So every path node fell back to `_node(nid, "Other", "…id")`. The fix names the node from data already in hand, no extra Sayari call. I checked the live profile response and the `relationships` block carries exactly what I needed: `relationships.data[]` where each `target` is a mini entity with `id`, `label`, `type`, `sanctioned`, `pep`, `countries`. For the Rosneft Global Trade case the anonymous `…JcvPXQ` was sitting right there as `OJSC ORENBURGNEFT` (a company, sanctioned=true), one hop away. So I build an id→{label,type,…} lookup from the profile's relationships, layered over a conversation-level lookup of entities already seen this turn-thread (resolved subjects + prior search leads from `state_doc`), and name path nodes profile-first, then known-entities, then a clearly-labelled `Unresolved entity (…id)` placeholder when genuinely unknown, never an invented name. When a type is known the node also gets the right label/color via `_coerce_label` instead of hardcoded "Other." `entity_summary` is relationship-free so those calls lean on the conversation lookup. Before/after on the example node: `[Other] …JcvPXQ` → `[Entity] OJSC ORENBURGNEFT` (sanctioned).

Second, the lead toggle. A broad search pins the top 5 relevant leads and the badge reads "Showing 5 of 20 leads." Testers wanted to click that to see the other 15 on the canvas. The constraint that mattered: do NOT pollute the persistent, accumulated conversation graph with 15 fuzzy unpinned leads. They should be a per-search, client-side overlay, not long-term graph state. So the backend now includes an `all_lead_nodes` field (every lead as a lightweight node with a `pinned` bool) on the search tool result and rides it along on the existing `tool_call_result` SSE event, additively, while the `nodes` that `merge_graph` persists stay the pinned top-N exactly as before. The unpinned set never enters Redis or the store's node map. The frontend stashes the unpinned leads from the latest search, the badge is now a button, and clicking it overlays them on the canvas dimmed and edge-less; clicking again hides them. A fresh search resets the toggle so you never see a stale overlay. Because the overlay is computed at render time in `GraphPanel`, the next turn's layout never inherits it.

Why it connects to the product: both are about the graph and the narrative agreeing, and about the canvas being legible. An anonymous node at the most important hop is the opposite of traceable evidence, and a "+15 more" you can't see is a dead end. Now the decision-relevant node shows its real name and sanctioned flag, and the analyst can expand or collapse the wider lead set without it leaking into the permanent picture.

Status: deployed to `sayari-demo-backend` on Cloud Run, `/health` 200 with `agent_impl=graph`. Verified the naming live against `Rosneft Global Trade S.A.` (the `…JcvPXQ` node now resolves to OJSC ORENBURGNEFT). Frontend `tsc --noEmit` clean; the toggle is local and hot-reloads, so only the backend needed deploying for the naming change.

Files: `backend/app/sayari.py` (`related_entity_lookup`, `_coerce_label`, `_risk_path_node`, `risk_paths_to_neighborhood` signature, `search_candidate_node`), `tools.py` (`_known_entity_lookup`, `sayari_profile_tool`/`sayari_summary_tool` thread the lookup, `sayari_search_tool` emits `all_lead_nodes`, `_NEEDS_CONVERSATION_ID`), `agent_graph.py` (carry `all_lead_nodes` on the SSE event), `frontend/lib/types.ts` (`LeadNode`), `frontend/lib/conversation-store.ts` (stash unpinned leads + `toggle_leads_overlay`), `components/GraphPanel.tsx` (overlay render + badge button), `components/EntityResolverApp.tsx` (wiring).

## 2026-06-03: Gave the agent a real memory instead of a prose blob

This is the one I think matters most for the interview, because it's about why the agent felt dumber than it should.

The problem: cross-turn memory was almost entirely a single append-only prose string. Each turn I stitched a short English digest (~220 characters per turn, no model involved) onto the prompt, plus a roster of recently seen graph nodes. That was it. So after a broad `sayari_search` returned 20 leads, the full list evaporated after one more turn. Only the pinned top few survived on the graph, and the digest kept a sentence. A follow-up like "profile the third lead from that search" had no way to recover the entity IDs, so it degraded into re-running the search and spending API credits to re-derive what the agent already knew. And because the digest is prose, structure got flattened: entity IDs, source references, and confirmed-versus-dismissed sanctions verdicts all got compressed away. For an investigative tool, losing provenance across turns is the opposite of the point.

What I did, in two parts. First I audited exactly what we retain versus drop today and researched current agent-memory practice (Mem0, Letta/MemGPT, LangMem, Anthropic's context guidance), then wrote it up as an architecture doc and a phased plan. The decisions I locked: go fully LangGraph so memory lives in one place (its checkpointer for short-term thread state, its store for long-term), split memory into two layers instead of one blob, treat memory as something the agent queries rather than something I dump into every prompt, keep scope per-conversation for now but with a namespace that can grow to per-user later, and (for the future vector layer) use Upstash's hosted embedding.

Then I built the first two phases. Phase 1 is a deterministic structured `state_doc` in Redis: resolved entities keyed by ID, the full lead lists from every search stamped with which turn and query they came from, adjudicated sanctions verdicts, pinned node IDs, and a turn log. It's merged in `finalize_node` from data the turn already has in hand, no extra model call, and rendered as an `INVESTIGATION STATE` block at the top of the prompt so IDs become the source of truth. I also bounded the old prose digest and deleted the dead `agent_msgs` path. Phase 2 is a read-only `recall_state` tool so the agent can ask its own memory questions ("list all leads from the earlier search," "which were Cyprus-registered," "what's the ID for the third one") instead of re-searching. The `conversation_id` is injected server-side, kept out of the model-visible schema, so the agent can't read another conversation's memory.

Why it connects to the product: the thing that makes a copilot feel like magic is that it remembers what you already found, exactly, and builds on it. Deterministic recall of IDs and prior findings without burning credits is what makes a multi-turn investigation feel like one coherent thread instead of a series of cold starts. And keeping provenance intact across turns is non-negotiable for a tool whose whole pitch is sourced, traceable evidence.

Status: Phase 1 and 2 implemented on the LangGraph agent. Verified with a Redis-mocked round-trip smoke test (23 assertions covering dedupe, recency cap, ordering, and every `recall_state` filter), imports and lints clean. Not deployed yet, pending a diff review before we push. The SSE contract, frontend, and native agent are untouched aside from removing one now-dead line in native. Phase 3, episodic memory over Upstash Vector with recency-plus-salience ranking, is specced but needs provisioning, and that's the layer that would let a returning analyst's past investigations resurface.

Files: `backend/app/conversations.py` (`get_state_doc`, `merge_state_doc`), `agent_graph.py` (`_build_state_delta`, `TurnState`, `finalize_node`), `agent_common.py` (`INVESTIGATION STATE` block, `bound_context_digest`), `tools.py` (`recall_state`), `intent.py`. Design docs: `docs/06-memory-architecture.md`, `docs/07-memory-implementation-plan.md`.

## 2026-06-03: Broad search was pinning an irrelevant node to the graph

A broad `sayari_search("Rosneft Trading")` was pinning the top 5 results to the evidence graph purely by Sayari's raw text-match score. One of those top hits was junk: a Rosneft employee trade-union local, the all-Cyrillic `ПЕРВИЧНАЯ ПРОФСОЮЗНАЯ ОРГАНИЗАЦИЯ … НК РОСНЕФТЬ … КУБАНЬНЕФТЕПРОДУКТ`. It scored high on fuzzy text match but had nothing to do with the trading companies the analyst was after. So a node would float onto the graph that the text answer never mentioned, which is exactly the kind of "where did that come from" moment that erodes trust.

What I changed: `sayari.search_to_nodes` now takes the original `query` and runs a conservative name-relevance gate before it picks which leads to pin. A lead is only pinnable if its label shares at least one meaningful token with the query, after I strip out legal-form and connector stopwords (LLC, trading, the, and so on). Then the relevant leads get a stable ranking that prefers company and legal-entity types. The key restraint here: the filter only affects what gets pinned to the graph. The full lead list still goes to the model, so the agent doesn't lose any options, it just doesn't visually commit to off-topic ones.

Why the type signal didn't work, and why name overlap did: I checked live, and all 20 leads came back typed `company`. So entity type couldn't tell the trade-union org apart from the real companies. The thing that actually discriminated was the name. The trade-union org is entirely Cyrillic and shares zero Latin identity tokens with "Rosneft Trading," so token overlap was the honest signal to filter on.

This matters because the graph and the text answer have to agree. The whole pitch of the tool is sourced, traceable evidence. If the graph shows an entity the narrative never resolved, the analyst can't trust either one. After the fix the trade-union org is excluded (`pinned_to_graph=false`) and the five real Rosneft trading companies are pinned, so the picture stays in sync.

Status: done. Live on `sayari-demo-backend-00022-qsd`, `/health` 200, `agent_impl=graph`. Verified live: `count=20`, `shown_on_graph=5`, pinned set is the real trading companies, trade-union org excluded. The `count` field still reports total leads, while `pinned_entity_ids` / `pinned_to_graph` / `shown_on_graph` all track the post-filter set so nothing drifts.

Files: `backend/app/sayari.py` (`_meaningful_tokens`, `_relevant_for_pin`, `_pin_rank_key`, `search_to_nodes`), `tools.py` (`sayari_search_tool` now passes `query`).

## 2026-06-03: Agent answers rendered as one flat wall of text

The `Markdown` component was already wired up (react-markdown plus remark-gfm and breaks), and I'd put `prose`/`prose-invert` classes on it expecting Tailwind Typography to style the output. It didn't. `@tailwindcss/typography` isn't installed, so those classes were inert, and Tailwind v4's preflight reset strips default styling off headings and lists. The result: every investigation summary and risk-report narrative rendered run-together, no hierarchy, no spacing, just a paragraph blob.

What I changed: I added explicit, theme-matched styles for the actual elements the agent emits, h1 through h6, p, ul/ol/li, strong/em, links, blockquote, hr, tables, and inline code, directly in the shared prompt-kit `Markdown` component. No new dependency, and it's streaming-safe because rendering is memoized per block.

Why it matters for this product specifically: the output here is a risk briefing. Analysts scan for the headline, the risk signals, the entity IDs. If a monospace entity ID looks the same as body text and lists don't break, the reader has to work to extract structure that should just be visible. Readability is part of the deliverable, not polish.

Status: done. Frontend only, `tsc` clean, not deployed.

Files: `frontend/components/ui/markdown.tsx`.

## 2026-06-03: Duplicate React keys, SSE replay on reconnect, and an invisible report terminator

After running a full investigation and then asking for a report, the console flooded with duplicate-key warnings and the tool list rendered unstably across turns. Three separate things were going wrong, all in the streaming layer.

What I changed:
- Tool calls now upsert by `callId` in the conversation reducer instead of always appending. `tool_call_start` was getting appended even when that call already existed, which is where the duplicate keys came from.
- I made the SSE stream idempotent across `EventSource` auto-reconnects. The stream replays from `?cursor=` and the events carry no SSE `id:`, so on reconnect the client was re-applying events it had already seen. I added per-connection position dedupe and made it ignore connection-level `error` Events.
- Defensive parsing on `error` events, plus a visible "Compiled risk report" / "Answered" terminator entry in the Agent Activity panel. That terminator existed but was being filtered out, so report generation looked like it just stopped.

Why it matters: this is the live activity feed an analyst watches while the agent works. A noisy console is a developer smell, but the real issue is the panel has to honestly reflect what the agent is doing. Silent reconnect replays and a missing "done" state make the system feel flaky right at the moment it's delivering the answer.

Status: done. Frontend only, `tsc` clean, not deployed, local dev hot-reloads.

Files: `frontend/lib/conversation-store.ts`, `lib/sse-client.ts`, `components/ToolCallFeed.tsx`.

## 2026-06-02: Stop auto-dumping a report; let the analyst decide when one is worth compiling

The agent used to end most turns by auto-generating a formal risk report. That's the wrong default. A copilot should push the investigation deeper with strong next-step suggestions, not fire off a one-shot report nobody asked for. Analysts are the ones who decide when an investigation is actually report-worthy.

What I changed: conversational `submit_answer` is now the default terminator, and the formal report no longer auto-generates. The agent sets a guarded `report_ready` flag, which only trips when there's a resolved entity plus at least one real risk, ownership, or sanctions signal. When it's ready, the UI surfaces a clickable report-ready badge on the chat node, a soft "Compile risk report" suggestion chip, and a one-line inline offer. Clicking it runs `submit_summary` over the evidence accumulated on that specific investigation path and produces the Risk Report card, which exports to PDF via `@react-pdf/renderer`.

Why this connects to the product: the per-path detail matters. Investigations branch, and a report should summarize the branch you're on, not a global blob. And a real PDF turns the output into something an analyst can actually hand off, which is what makes this a tool and not a chat toy.

Status: backend behavior done, live on `00021-fjm`. Default terminator is `submit_answer`, `submit_summary` only fires on explicit request or `wants_report`, and the guarded `report_ready` flag is on `TurnAnswer`. Verified: Huawei ends `kind=answer` with `report_ready=true`, and an explicit "compile a report" still returns `kind=summary`. The UI pieces (badge, report card, suggestion chips, PDF export) ship with the Tier 3 overhaul.

Files (anticipated for the UI half): `backend/app/prompts.py` (terminator routing and report-ready), `schema.py` (`report_ready`, per-path compile input); `frontend/` Risk Report card, report-ready badge, prompt-kit `PromptSuggestion` chips, `@react-pdf/renderer`.

## 2026-06-02: Geographic map view (planned)

The evidence graph answers "who connects to whom." It does not answer "where is the exposure, and how does it flow." Those are different questions, and trade data in particular is inherently geographic: every shipment is an origin and a destination.

What I'm planning: a geographic map as a secondary, linked view, with a Graph ↔ Map toggle on the right pane. Its main job is trade-flow arcs from Sayari shipment data, origin to destination, sized by volume and colored when an HS code trips the precursor or dual-use screen. Built with react-simple-maps over SVG/topojson. Selection stays linked, so clicking a country filters the graph and summary. Jurisdictional and sanctions footprint is a secondary fit for the same view.

Why it matters: a flow map is the clearest way to tell the precursor/dual-use story, especially flagged shipments crossing into sanctioned or high-risk regions. The relationship graph just can't show that.

Granularity call: country-centroid placement using a static country-to-centroid lookup, no geocoder. I have address and city text (ICIJ address strings, Sayari street addresses, shipment ports) but not lat/lng, so anything finer than country would need a geocoding dependency. Deferred. Trade arcs may upgrade to port-level later if Sayari's shipment data exposes clean port locations.

Status: planned, scheduled as Tier 2b. It lands after Tier 2 trade data, since the map's best fuel is shipment routes. The map is a flip-to lens, not the primary workspace.

Files (anticipated): `frontend/` new Map view plus toggle, `frontend/lib/types.ts`. Depends on Tier 2 `sayari_trade` shipment data and a country-centroid table.

## 2026-06-02: Rounding out the Sayari toolset and adding an intent router

The goal for this phase was to push the toolset toward an "answer any question" copilot without naively wrapping all 16 Sayari endpoints. Over-wrapping is a real trap: it degrades tool selection, inflates token counts, and burns API credits. Tools should map to analyst intents, not 1:1 to the API surface.

What I added:
- `sayari_search`: broad, fuzzy entity search for lead generation, which is a different job from precise resolution.
- `sayari_summary`: a relationship-free profile, used as a cost and latency lever for secondary entities you don't need the full graph on.
- `sayari_watchlist`: graph paths to PEP or watchlisted parties, which complements OpenSanctions. OpenSanctions confirms a direct listing; this finds indirect exposure through ownership.
- `sayari_record`: Get Record, returning a `document_url` so claims can point at the actual underlying filing.
- A lightweight intent-classification router that narrows the tool set before the agent acts, so selection stays robust as the toolset grows past ~10 tools.

Why each of these earns its place: broad search lets the agent start from a vague query, watchlist catches indirect sanctions exposure, summaries control cost on entities that don't need a full profile, and the record/document_url is the trust-unlocker. "Show me the source document" is the difference between a claim and evidence, and for a Sayari demo that provenance story is the whole point.

On cost posture, I went balanced: full profile for the primary entity, `sayari_summary` for secondaries, capped traversal depth and result size, and a per-turn tool budget. A future nice-to-have is surfacing Get Usage as a "credits used this session" indicator.

Status: done. Live on `sayari-demo-backend-00021-fjm`, `/health` 200, `agent_impl=graph`. The router runs on `claude-haiku-4-5-20251001` (the Haiku 3.5 I originally specified is EOL and 404s; the model is overridable via `INTENT_ROUTER_MODEL` and fails safe to the full toolset). Caps: traversal ≤40, watchlist ≤15, depth ≤4, search ≤20, soft per-turn tool budget 14. New `intent.py` is shared by both agent paths. Deferred from this phase: `sayari_trade`, `sayari_shortest_path`, `sayari_traversal` (Tier 2), and Projects/Notifications monitoring (later).

Follow-up I still owe: I spot-verified these behaviors live instead of running the full 12-case eval suite, since each case takes about 60 to 120 seconds. A full suite run is worth doing before I call this fully closed out.

Files: `backend/app/sayari.py`, `tools.py`, `tools_lc.py`, `prompts.py`, `schema.py`, `agent_common.py`, `evals/run_evals.py`; `frontend/lib/types.ts`, `RiskSummaryCard.tsx`, `GraphPanel.tsx`.

## 2026-06-02: OFAC SDN vs non-SDN: the agent was over-claiming sanctions severity

This is the one I care most about. On a Huawei run, the summary headline said "OFAC SDN list (SDN #30947)" while the agent's own Sayari risk factor said "sanctioned USA ofac non sdn." Those contradict each other, and the headline was the dangerous one. Huawei is on OFAC's non-SDN Consolidated list and the BIS Entity List. It is not on the SDN (blocked) list. Calling an Entity-List or non-SDN entity "SDN-blocked" is a serious analyst error, and it directly undercuts the tool's core promise of trustworthy, sourced claims.

Root cause was two layers stacked on each other:
1. `check_sanctions` was feeding the model raw dataset slugs like `us_ofac_cons`, which the model misread as SDN.
2. Sayari itself ships Huawei an identifier literally typed `usa_ofac_sdn_number=30947`, even though Huawei is non-SDN. OFAC assigns record numbers to non-SDN entries too, so that field name is a misnomer. The model saw "sdn_number" and believed it.

What I changed:
- Explicit OFAC program labels in `sanctions.py`, so the agent reports the program type verbatim instead of guessing from a slug.
- Relabel `usa_ofac_sdn_number` to `usa_ofac_record_number` in `agent_common.relabel_identifiers` before the model ever sees it, so the misnomer can't mislead it.
- Prompt discipline that forbids promoting a non-SDN, Consolidated, or Entity-List hit to "SDN," and forbids inventing an SDN number.
- An `ofac_non_sdn_labeling` regression eval on the Huawei case so this can't quietly come back.

Why it matters beyond Huawei: the value of this tool is that a claim matches its source exactly. SDN versus non-SDN versus Consolidated versus BIS Entity List versus trade screening are materially different legal statuses. Getting that wrong is the kind of error that would make an analyst stop trusting everything else the tool says.

Status: done. Verified Huawei now reads "OFAC consolidated non-SDN + BIS Entity List" with no fabricated SDN number, and Sberbank still correctly reads OFAC SDN, so I didn't over-correct in the other direction. Revision `sayari-demo-backend-00020-qpk`, 100% traffic, `/health` 200.

Files: `backend/app/sanctions.py`, `agent_common.py` (`relabel_identifiers`), `tools.py` (`check_sanctions` descriptor and `sayari_resolve`), `prompts.py`, `evals/run_evals.py`.

## 2026-06-01: Sayari SDK was missing from the deployed container

Hosted runs kept reporting "Sayari is unavailable" and silently falling back to ICIJ plus OpenSanctions. The Cloud Run logs showed the real cause: `ModuleNotFoundError: No module named 'sayari'` at the `sayari.py` import, which `execute_tool` was swallowing into a generic tool error. The SDK was installed in my local venv but never listed in `requirements.txt`, so it never made it into the Docker image.

What I changed: pinned `sayari==0.1.43` in `backend/requirements.txt` and redeployed.

Why it matters: the entire premise is Sayari-first routing. If the SDK isn't in the container, the deployed demo silently degrades to the two secondary sources and quietly drops the authoritative one. The fallback masking the import error is what made this take a log dive to find.

Status: done, revision `sayari-demo-backend-00019-9c2`, verified `sayari_resolve` succeeds live.

Files: `backend/requirements.txt`.

## 2026-06-01: Sayari credentials weren't on Cloud Run

Even with the SDK present, the deployed backend had no Sayari credentials. Eight other secrets were mounted, none for Sayari, so Sayari calls couldn't authenticate.

What I changed: added `SAYARI_CLIENT_ID` and `SAYARI_CLIENT_SECRET` as Secret Manager secrets, granted the runtime service account access, and mounted them on the service.

Why it matters: this is the second half of the same "Sayari works locally but not when deployed" problem. The SDK fix got the code into the container; this got it able to actually authenticate. Together they bring the hosted environment to parity with local.

Status: done, creds verified byte-perfect, no IAM beyond the two secret-accessor bindings.

Files: GCP Secret Manager and Cloud Run service config, no repo change.

## 2026-06-01: The central graph node was missing and neighbors floated unconnected

The evidence graph was rendering wrong. Instead of a connected network anchored on the entity you searched, you'd get a scatter of disconnected neighbor labels and no central node at all.

Root cause: the ICIJ neighborhood functions (`get_relationships`, `get_officers`, `find_address_connections`, `find_er_links`) returned only the neighbors, but their edges referenced the central node's id. The frontend's d3-force layout drops any edge where both endpoints aren't in the node set. So every edge pointed at a central node that wasn't there, every edge got discarded, and the neighbors had nothing tying them together.

What I changed: each of those ICIJ functions now seeds the central, queried entity as the first node, mirroring what the Sayari mapping already did. Right-click expand also returns the central node, and the store dedupes by id so that doesn't create a double.

Why it matters: the graph is the spatial story of the investigation. It has to be a connected network anchored on the subject, on both the ICIJ and Sayari paths, or it's just noise. This is the same "graph must be coherent" principle as the relevance-filter fix, just at the data-shape level.

Status: done, verified the central node is present with zero dangling edges using the real mapping over simulated rows.

Files: `backend/app/graph.py`.

## 2026-06-01: Sayari-first routing was being overridden by a stale tool description

The Tier 1 system prompt said resolve named entities with Sayari first, but a Huawei run still went ICIJ-first and landed on a name-collision shell company, "SINO HUAWEI," instead of the real conglomerate.

Root cause: the `search_entity` tool description still carried a pre-Tier-1 mandate, "ALWAYS call this first... Never call any other tool before this one." Tool descriptions drive tool selection for both agent implementations, and that instruction was strong enough to override the Sayari-first system prompt. The prompt said one thing, the tool description said another, and the tool description won.

What I changed: rewrote the `search_entity` description to position ICIJ as leak-provenance and corroboration, and to defer to `sayari_resolve` as the first call for named entities.

Why it matters: this is a lesson about where agent behavior actually comes from. The system prompt isn't the only lever; tool descriptions are part of the contract the model reads, and a stale one quietly wins. For this product the routing order is the thesis: resolve on the authoritative source first, then corroborate with leaks and confirm with watchlists. Landing on a name-collision shell is the exact failure that order exists to prevent.

Status: done.

Files: `backend/app/tools.py`.

## 2026-06-01: Tier 1 Sayari integration

This is the foundation everything above builds on: making Sayari the primary data source instead of just adding it alongside ICIJ and OpenSanctions.

What I built:
- A new `sayari.py` data layer that owns all SDK calls and the mapping from Sayari shapes into our app shapes: resolve, profile, ownership, and graph mapping.
- `slim_sayari_profile()` to curate the risk map before it reaches the model, so a turn doesn't drown in raw risk data.
- Schema additions: `SayariCandidate`, `SayariRiskFactor`, and `source_system` tags so every node and edge knows where it came from.
- Three new tools wired into both agent implementations, plus Sayari-first routing, scope honesty, and clarifying questions in the prompts.
- Frontend source-colored nodes, a provenance legend, and risk factors grouped by severity level with click-to-highlight paths.

Why it matters: ICIJ and OpenSanctions alone miss authoritative ownership, identifiers, breadth, freshness, and risk traversal paths. The model I settled on is that Sayari is the primary source, ICIJ provides leak provenance, and OpenSanctions confirms watchlists. Each source has a distinct job, and the graph now shows which source backs each piece of evidence. That sourcing is the whole reason a risk summary here is trustworthy rather than just plausible.

Status: done and verified end to end on Gazprom and Huawei, with Sayari-first routing, risk factors and ownership paths present, and sanctions confirmed. Known trap I'm still watching: Sberbank's top resolution candidate is a subsidiary and the real parent ranks third, so the disambiguation rides on the prompt rather than the raw ranking.

Files: `backend/app/sayari.py`, `agent_common.py`, `schema.py`, `tools.py`, `tools_lc.py`, `prompts.py`, `config.py`, `evals/run_evals.py`; `frontend/lib/types.ts`, `GraphPanel.tsx`, `RiskSummaryCard.tsx`.

## 2026-06-01: Tier 3 UX design direction (planned)

Before building the modern UI, I wanted the design decisions locked so the Tier 3 frontend would be a build, not a redesign mid-flight.

What I captured: the branching-investigation-canvas spec. A split pane with the investigation tree on the left and the evidence graph on the right, an lmcanvas-faithful light theme, neutral chrome with source rings and a risk glow, a time-travel graph, and the Risk Report card.

Why it matters: the end state is a demo-grade investigative copilot, and the UX has to support analyst-driven branching plus a path-scoped evidence graph. Workshopping that on paper first is cheaper than discovering the structural decisions during the build.

Status: planned, spec only, build deferred until after Tier 1 and Tier 2. See `docs/04-tier3-ux-spec.md`.

Files: `docs/04-tier3-ux-spec.md`.
