# Improvements & Decisions Log

This is my running record of what I changed in the Entity Risk Resolver, and more importantly why. Git history tells you what the diff was. This tells you what I was thinking: the problem I was staring at, the call I made, and how it ties back to the thing I'm actually building.

The product is an investigative copilot over corporate ownership and sanctions data. It pulls from three sources: the Sayari API (authoritative ownership, identifiers, risk), the ICIJ leak graph in Neo4j (leak provenance), and OpenSanctions (direct watchlist confirmation). This is a demo I built for a Sayari interview, so a lot of these decisions are about making the thing trustworthy and legible to an analyst, not just functional.

Newest first. Each entry tries to cover three things: what I changed, why the old behavior was wrong, and how it connects to the bigger goal.

---

## 2026-06-10: Stage 2b, the branching investigation canvas

Stage 2a built the tree; this builds the room you walk around it in. The linear INVESTIGATION pane is now a React Flow canvas where every turn is a draggable card, forks hang off any card, and clicking a card time-travels the evidence graph to that turn's path-accumulated state. This is the Tier 3 signature move: the conversation is a tree, the graph is a fold along whatever path you're standing on, and the UI finally shows both.

The store had to learn tree-thinking first. Turns now carry the server's `turn_id` and `parent_turn_id`, captured from the submit response for live turns and from `GET /tree` on reload. Everything else routes through that: SSE events find their card by `turn_id` (falling back to `turn_index` only for pre-branching conversations), the active path is derived by walking parent pointers from the selected card to the root, and the composer parents new messages on the selected card's id. No selection means a null parent, which the backend treats as linear append, so the old behavior is literally the default case of the new one.

Layout is lifted from local-lmcanvas and adapted to a server-backed tree. The heuristic: a parent's first child lands directly below it (the thread continues), any later child lands in a right lane (a fork). When a new card overlaps existing ones, a push-right collision cascade shoves them aside, rightward only, because a cascade that can push both ways ping-pongs forever. Heights are measured from the live DOM corrected for zoom, because cards grow as content streams and a layout that guesses heights stacks cards into each other. That exact bug appeared on reload, where the whole tree is placed before any card has rendered: the fix is a second layout pass after mount, with real heights, before the camera fits the tree.

The fork interaction is deliberately analyst-only. Hover a card, a circular plus button fades in, clicking it drops a dashed draft card in the spot its turn will occupy. Enter submits with that card's turn as parent; Esc throws it away. The agent never creates branches, it only suggests follow-ups, and a suggestion chip click forks from the card the chip lives on, not from the bottom of the conversation. Cards also infer a thread-type corner badge (Ownership, Sanctions, Trade, Identity) from the question and the tools the turn actually called, so a glance at the tree tells you which hypothesis each branch is chasing.

Time-travel is the payoff. Selecting any non-head card fetches `GET /turns/{id}/graph` and swaps the evidence graph to that turn's accumulated state: inherited nodes dim, the turn's own delta pulses in, and sibling evidence simply is not in the payload, so isolation is structural rather than cosmetic. The graph header pins an "as of turn N on this path" pill with a back-to-live button, and clicking empty canvas or the live head restores the streaming graph. The map stays on live-head data with a small note while scoped, because path-scoping shipment lanes would mean re-deriving routes per turn and the cheap version (graph deltas) does not carry them.

Verified live against prod: investigated Sergey Roldugin, deposited a case id (CASE-777) on the linear branch, forked from turn 1 and asked the fork whether a case id exists. The fork answered "no internal case ID is on file" while still knowing turn 1's Panama Papers entities, which is sibling isolation and path inheritance demonstrated in one answer. Streaming landed on the right card mid-fork, reload rebuilt the four-card tree from `/tree`, and `tsc --noEmit` plus `next build` pass clean.

Status: done. Deferred polish: phrase-level highlight-to-fork, path-scoped map lanes, a minimap or fit-view button for big trees.

Files: `frontend/lib/conversation-store.ts`, `lib/types.ts`, `lib/sse-client.ts`, `lib/canvas-layout.ts` (new), `components/canvas/InvestigationCanvas.tsx` (new, replaces `ChatPanel.tsx`), `components/canvas/TurnNode.tsx` (new), `components/canvas/TurnCard.tsx`, `components/GraphPanel.tsx`, `components/EntityResolverApp.tsx`, `app/globals.css`, `NOTICES.md`.

## 2026-06-09: Stage 2a, the branching backend

The Tier 3 spec promises branch cards that fork from any turn, sibling branches that can't see each other, and a time-travel graph that rebuilds to any turn's accumulated state. This entry is the backend half of that promise. The frontend canvas comes later; what exists now is the tree model, the isolation machinery, and the API the canvas will consume.

The core idea: a conversation is now a tree of turns, and every turn's view of the world is a fold along its own path. Each turn gets a stable `turn_id` and a `parent_turn_id` (defaulting to the previous turn, which is why linear conversations behave byte-identically to before). When a turn writes state, the exact delta that `_build_state_delta` produced gets stored under that turn's id, alongside the merged doc that linear consumers still read. When a new turn starts on a branch, its state_doc is rebuilt by folding `_apply_delta` over the deltas from root to parent. Phase F's refactor, which pulled `_apply_delta` out of `merge_state_doc` as a pure function, is the whole reason this was a composition job instead of a rewrite. The fold IS the feature.

The delivery mechanism is a `ContextVar` turn scope. The turn runner wraps execution in `turn_scope(conversation_id, turn_id, parent_turn_id)`, and inside that scope `get_state_doc` transparently returns the path-scoped doc. Nothing downstream changed: recall_state, the injected context core, the sanctions ledger, the registry, and claims all read through `get_state_doc`, so scoping the doc scoped them all for free. I verified there's no exception; every state subsystem lives inside state_doc. Prose context follows the same rule via `resolve_prior_context`, which hands a fork its parent's `context_after` rather than whatever a sibling said later. Graph deltas got the same treatment: each turn's added nodes and edges are stored per turn, and a path graph endpoint unions them root to turn for the frontend's time travel, with the turn's own delta returned separately so the UI can pulse what's new and dim what's inherited.

One subtle bug worth recording: my first version of the write path appended the turn's delta to storage before reading the current doc. Inside a turn scope the read is path-scoped, so it picked up the just-appended delta and then applied it again, doubling every write. The fix is ordering: read first, apply, then record. The deterministic eval that compares the folded doc byte-for-byte against the old iterative merge is what caught it.

Old conversations needed an answer too. They have no per-turn deltas, so the first tree-aware turn snapshots the merged state, graph, and context into a `tree_base` key, and path folds start from that snapshot. No migration, additive keys only, same 24h TTL. Old conversations keep working linearly and can still fork from the present.

Verification: three new deterministic eval families in `evals/branching.py`. Fork isolation (a sanctions hit deposited on branch A is present in A's recall and absent from B's), path graph accumulation (graph at a turn equals its path's union only), and linear regression (a no-fork conversation produces an identical state_doc to the pre-change behavior). All 59 deterministic checks green. Full suite 99/102, where the three misses are live model-phrasing flakes (two are the documented known flakes, and the third, name_match_hedged, passed on an isolated rerun), so no regressions. Plus a live smoke test where a fork from turn 1 didn't know a name deposited on the sibling branch while the linear continuation did.

Status: done. API contract and design notes in `docs/11-branching-backend.md`.

Files: `backend/app/conversations.py`, `agent_graph.py`, `agent.py`, `main.py`, `episodic.py`, `backend/evals/branching.py`, `run_evals.py`.

## 2026-06-09: Two post-test fixes, the map didn't say whose trade it shows and sanctioned enumerations under-counted

Both of these came out of live user testing right after the Tier 2 commit. Neither is a bug in the strict sense. The map rendered correct lanes and the recall read returned exact rows. They are both legibility failures: correct data presented without enough context to be trusted.

First, the map. A tester flipped to the Map view and asked a fair question: whose trade am I looking at? The lanes carried country pairs, risk colors, widths, and values, but nothing said "these are JSC Mikron's exports." Tooltips were the same story, "RUS -> CHN" tells you geography but not who is on either end of the lane. The fix is two small data additions and a presentational pass. Backend-side, `shipments_to_routes()` now takes the queried subject's id and role and aggregates each lane's top counterparty names into a `top_parties` field (capped at 2 per lane, the payload discipline from the original routes work). The trade tool also puts `subject_name` on the result metadata, looked up from the subject's own appearance as a party on its shipments. Frontend-side, a new `collectTradeSubjects()` derives one (entity, role) subject per trade query from the tool calls already in the conversation, and the map renders a "Showing trade for" header ("JSC Mikron · exports (supplier)", both lines when the conversation queried both directions) plus a direction-aware tooltip lead like "JSC Mikron -> JXJ International Transportation". Everything is additive and optional: conversations stored before these fields existed render exactly as before, with the header falling back to the graph node's name and tooltips falling back to country pairs.

Second, the enumeration. On "Which subsidiaries were sanctioned again?", the agent called `recall_state(kind="sanctions")`, got the OpenSanctions verdict ledger, and answered with only the confirmed hits. That omitted the refinery subsidiaries that carry Sayari sanctioned flags, which earlier runs surfaced via `kind="entities"`. Both reads are faithful; the answer was still incomplete, because sanctioned status lives in two stores with two different provenances. The ledger holds what check_sanctions adjudicated. The registry holds entities whose sanctioned flag came from Sayari risk factors and never passed through a watchlist verdict. The fix is deliberately NOT to merge the stores. The provenance split is the point: an OpenSanctions-confirmed verdict and a Sayari risk-factor flag are different evidence classes. Instead the guidance got tuned at three levels. The prompt's recall section now says sanctioned-X enumerations must query BOTH kinds, take the union, and state the split. The `recall_state` tool description says the same thing, because tool descriptions are where behavior actually comes from (the SINO HUAWEI lesson). And the cheapest, most reliable layer: `kind="sanctions"` output now carries a deterministic hint line whenever registry entities hold sanctioned flags the ledger doesn't, telling the agent exactly which follow-up query completes the set. Even if the model skims the prompt, the data steers it.

Verification: a new deterministic eval case (`sanctioned_union` in `backend/evals/multiturn.py`) persists a turn containing both a confirmed OpenSanctions hit and a Sayari-flagged subsidiary through the production write path, then asserts the registry union surfaces both, the stores stay distinct, and the ledger hint fires. The existing `tier2_trade` routes check now also pins `top_parties`. All 46 deterministic checks green.

Status: done. Backend redeployed as `sayari-demo-backend-00034-m2w`, `/health` 200, `agent_impl=graph`, episodic off. Frontend typecheck and production build pass.

Files: `backend/app/sayari.py` (`shipments_to_routes` counterparties), `backend/app/tools.py` (`subject_name` metadata, sanctions hint, recall_state description), `backend/app/prompts.py` (union guidance), `backend/evals/multiturn.py` + `run_evals.py` (new checks); `frontend/lib/map/trade-routes.ts` (`top_parties`, `collectTradeSubjects`), `frontend/components/TradeRoutesMap.tsx` (header + tooltip), `frontend/components/EntityResolverApp.tsx` (wiring).

---

## 2026-06-09: Tier 2b, the risky-routes map

The deferred half of Tier 2: shipment lanes drawn on a world map. The graph already shows WHO ships to whom; the map shows WHERE the goods physically flow, which is the lens an export-control analyst flips to when the question is "does this supply chain route through somewhere I care about."

The geography problem first, because it shaped the design. Sayari shipments carry ISO-3 country codes and port/city name strings but no usable coordinates (the address x/y fields exist in the schema and are null in practice). react-simple-maps needs [lng, lat]. So the map runs on a bundled static asset: `frontend/lib/map/country-centroids.json`, an ISO3 to [lng, lat] centroid table. Country-level arcs are the deliverable; port-level (UN/LOCODE parsed out of strings like "SIKKA PORT (INSIK1)") stays a stretch goal and was not built.

Data flow is deliberately thin. Backend-side, `sayari.shipments_to_routes()` aggregates a trade result into a compact `metadata.routes` array, one row per (departure_country, arrival_country) pair: shipment_count, total_value, dual_use, sanctioned_party, top HS codes. That rides the existing tool_call_result SSE event, no new event types. Frontend-side, `lib/map/trade-routes.ts` collects routes across turns, takes the LATEST result per (entity_id, role) query so re-asking the same trade question replaces instead of double-counting, and merges lanes by country pair. After a page reload (hydration restores the graph but not tool calls) it falls back to deriving coarse routes from the persisted `ships_to` edges using each party node's country, so the map survives a refresh with slightly less precision.

The component (`frontend/components/TradeRoutesMap.tsx`) is react-simple-maps in the basic-markers style the user pointed at: world topojson from `frontend/public/maps/countries-110m.json`, arcs between centroids, markers at endpoints. Color is the same risk language as the graph: red when a lane has a directly sanctioned party, amber when the dual-use screen or a native BIS tag fired, neutral zinc otherwise. Stroke width scales with shipment count (log so a busy lane does not blot out the map), opacity with lane value. It surfaces as a Graph | Map toggle on the right panel rather than a fourth panel. The whole thing is self-contained (own component plus `lib/map/` assets) on purpose: the lmcanvas reskin lands next and should be able to restyle it without surgery.

Status: done. Backend redeployed as `sayari-demo-backend-00033-lh4` (the routes aggregation rides the trade tool result), `/health` 200, `agent_impl=graph`. Demo flow: ask "What does JSC Mikron ship, and is any of it dual-use?", then flip the right panel to Map; the dual-use lanes render amber.

Files: `backend/app/sayari.py` (`shipments_to_routes`), `backend/evals/run_evals.py` (routes check); `frontend/components/TradeRoutesMap.tsx` (new), `frontend/lib/map/trade-routes.ts` + `country-centroids.json` (new), `frontend/public/maps/countries-110m.json` (new), `frontend/components/EntityResolverApp.tsx` (toggle), `package.json` (react-simple-maps).

---

## 2026-06-09: Tier 2 core, trade and supply-chain risk

This tier takes the agent from "who owns X" to "what does X actually ship, to whom, and is any of it dual-use." Two new Sayari surfaces: trade (real shipments) and shortest path (the relationship chain between two entities). Together they answer the question an export-control analyst actually has: my counterparty screens clean, but does its supply chain route through someone who doesn't?

What I built, data layer first. `sayari.py` gained `trade_shipments(entity_id, role)` (supplier or buyer, via `trade.search_shipments` with a `TradeFilterList`) and `shortest_path(source_id, target_id)` (via `traversal.shortest_path`). The slimming discipline is the same one the risk map taught us: we fetch a bounded page of shipments, keep the top ~20 by value and date for the model and the graph, and aggregate everything else into facets (counts, total value, top HS codes, top arrival countries). A raw trade party can carry 50-70 name aliases; we keep `names[0]` plus a count, exactly like the existing tools. Shipments on the same supplier-to-buyer lane aggregate into ONE `ships_to` graph edge with summed value, unioned HS codes, and the latest date, so a busy lane is one readable edge, not twenty overlapping ones.

The dual-use screen is the part worth explaining, because it is two signals with two different owners. Sayari ships HS codes on every shipment but has no per-code dual-use flag. So I bundled a curated reference list, the BIS/E5 Common High Priority List, as a static JSON asset at `backend/app/data/hs_dual_use.json`, and `hs_screen.py` screens every shipment's 6-digit codes against it. That is OUR signal, provenance "hs_screen". Separately, Sayari's own party `risks` dict carries native export-control tags (`exports_bis_high_priority_items_*`, `controlled_by_bis_meu`, and friends), and `native_bis_tags()` lifts those out. That is SAYARI'S signal, provenance "sayari_bis_tag". A shipment is dual_use when either fires, but the tool result keeps the two lists separate and the prompt tells the agent to report which one fired. Blurring "our screen matched the HS code" with "Sayari tagged the party" would be exactly the provenance sloppiness the rest of this product exists to prevent. The JSON asset is versioned and structured so a second list (EU dual-use Annex I, say) can be appended later with its own list id without touching the screen logic.

Shortest path got the same treatment as the other traversals: the response shape ({source, target, path}) matches ownership, so `shortest_path_to_neighborhood()` reuses the existing path replayer and then folds in the `target` EntityDetails as a properly named endpoint node. The headline field is `has_sanctioned_intermediary`, which fires only when an INTERMEDIATE hop is directly sanctioned, not when an endpoint is. The endpoints' own status is already visible on their nodes; the intermediary is the thing you'd miss.

Two new tools ride the existing pattern end to end: `sayari_trade` and `sayari_shortest_path` are async wrappers over `asyncio.to_thread`, registered in the dispatcher, bound through `tools_lc.py` for free, routed by a new `trade_supply_chain` intent (plus `sayari_shortest_path` joining the ownership and sanctions intents), and described carefully because descriptions are where tool selection actually happens. The frontend renders `ships_to` edges dashed fuchsia with a legend entry, flips them amber with a "dual-use" label when the lane screened dirty, badges dual-use parties, and rings sanctioned nodes red so a sanctioned intermediary on a path is visible at a glance. Deliberately functional-minimal styling: the lmcanvas reskin is next, so nothing here is polish that would get redone.

Verification: five new deterministic eval checks (`tier2_trade`) pin the mappers with fixtures, no API spend: the HS screen fires on a CHPL code and stays quiet on a benign one, a native BIS tag flags a shipment even with clean HS codes, lanes aggregate correctly, and the sanctioned-intermediary flag fires on a sanctioned middle hop but NOT when only the target is sanctioned.

Deferred: the geographic routes map (shipment departure/transit/arrival rendered on a map) is a separate follow-up, not part of this core.

Files: `backend/app/sayari.py`, `schema.py`, `hs_screen.py` (new), `data/hs_dual_use.json` (new), `tools.py`, `tools_lc.py`, `intent.py`, `prompts.py`, `agent_common.py`, `evals/run_evals.py`; `frontend/lib/types.ts`, `components/GraphPanel.tsx`.

---

## 2026-06-08: Provenance re-citation + the multi-turn safety net (IMS Phases E and F)

These two phases finish the Investigation Memory Subsystem. With E and F done, A through F are complete. D (episodic vector) stays flag-off until someone provisions an Upstash Vector index, but everything that actually fixes recall fidelity is now in and locked down by tests.

Phase E was the smallest phase of the whole plan, and most of it was already built. The goal from doc 09 is "re-cite a turn-2 finding on turn 9 without redoing the work." When I audited what was already there, claims were fine: a claim has always carried source_refs, and recall_state(kind="claims") already hands them back byte-exact, so the agent could already pull a prior claim and reuse its source. Registry entities were most of the way there too. Each one already carried which tool named it, a confidence tier, and the turn span it was seen across. The one real gap was the per-entity source_refs pointer from doc 09 section 5: the actual record an entity traces back to. So I added _attach_source_refs in _project_entities, and it derives those pointers deterministically from the structured buckets, never from prose. The logic is plain: a check_sanctions hit points back at its OpenSanctions record, an ICIJ-traversed node points at its node_id, and everything else (Sayari leads, resolved subjects, risk-path names) points at its Sayari entity id. On top of that self-ref, an entity also picks up the OpenSanctions record behind any sanctions-ledger row it maps to, and the exact source_refs of any structured claim that cited it. That last part is what makes a re-cite faithful: the entity carries back the same ref the agent first used, including a Sayari risk_factor pointer when that backed the claim.

Why derive it instead of storing it as a new field. The registry is a projection that gets recomputed on every read and every write, so source_refs comes along for free and an old state_doc backfills it with zero migration. That is the same backward-compat property Phase B leaned on, and it is why I never have to write a migration for any of this. The read path surfaces the provenance without bloating the injected core: recall_state(kind="entities") now returns source_refs and first_seen_turn on each item, the tool description and the entities ranking note advertise it, and the system prompt got a short "re-citing a prior finding" passage that tells the agent provenance travels with the row, so re-running check_sanctions or sayari_profile just to re-source a fact it already stored is a wasted credit. The injected INVESTIGATION STATE core stayed exactly as lean as Phase C left it. Provenance lives in the recall path, not the always-on core.

Phase F is the safety net: a multi-turn eval harness in backend/evals/multiturn.py, called from the same run_evals entrypoint. The hard part of a multi-turn memory test is persisting state_doc between turns the way production does without standing up Redis or burning live model calls. To make that possible I factored the pure read-modify core out of merge_state_doc into _apply_delta, which is the exact transformation production writes to Redis, just without the I/O. The harness builds each turn as the structured graph state finalize_node hands to _build_state_delta, runs the real projection, and persists with _apply_delta into an in-memory doc. Then it exercises the real recall_state tool by pointing conversations.get_state_doc at that in-memory doc. So the test runs the actual write path and the actual recall tool, end to end, deterministically.

Three cases. The first is the Rosneft regression from doc 09 section 11, encoded straight. Turn 1 investigates a subject whose check_sanctions surfaces a closely-named SDN entity that the agent dismisses as a name collision, so it never lands in the kept sanctions_hits. Turn 2 asks to re-list the sanctioned-but-dismissed subsidiaries. The assertions are deterministic: the dismissed subsidiary is recoverable via recall_state(kind="sanctions") and via kind="entities") as a sanctioned SDN entity, it carries its OpenSanctions provenance so it can be re-cited, and the recall path (the deterministic follow-up prefetch plus the zero-credit recall_state read) supplies the answer, so a correct turn 2 re-spends no check_sanctions. I kept that last assertion deterministic on purpose rather than hitting the live model for "did it call check_sanctions": the guarantee is stronger when you assert that the answer is fully available from a zero-credit memory read, because then there is no reason to re-spend the tool.

The second case encodes the IMS invariant as a reusable check, ims_invariant_violations: every entity the agent names with an id in its answer (referenced_node_ids plus claims.source_refs) that the turn's tools could actually name must appear in the registry. The case names an owner through both a claim and referenced_node_ids, names a search lead, and cites a dismissed SDN hit by sanctions_id, then asserts all of them are recoverable. It would fail if the write path dropped any referenced id, which is exactly the silent recall bug the invariant exists to catch. It also carries a HaluMem negative control: an id that appears only in the prose answer string, never in a structured field, must NOT be written, which proves the write path stays structured-only. The third case is a cheap recap check: after an investigation turn that persisted real findings, a recap routes to the lightweight submit_answer terminator and the prior claim and subject are both recall-recoverable, so the recap can stay faithful without re-running anything.

On verification: the full suite is 80 checks, and 78 pass. All 37 deterministic memory checks are green with episodic disabled, including the 10 new E and F checks. The two misses are the known live-path LLM-judgment flakes, ofac_non_sdn_labeling and used_sayari_record, which depend on the model's wording and tool choice on a given run and are not regressions. I confirmed those are the only non-deterministic misses.

Files: `conversations.py` (`_attach_source_refs` / `_self_ref` / `_dedupe_refs` for entity provenance, and `_apply_delta` factored out of `merge_state_doc`), `tools.py` (recall_state entities ranking note and tool description advertise source_refs), `prompts.py` (the re-citing passage), `evals/multiturn.py` (new: the harness, the three cases, and the reusable invariant check), `evals/run_evals.py` (wires the three multi-turn cases into the local run), `docs/09` (Phases E and F marked shipped).

---

## 2026-06-08: Episodic vector memory, built but flag-off (IMS Phase D)

This is the L2 tier of the memory subsystem, and the only phase that needs new infrastructure. A, B, and C were the fidelity fix: widen the write path, unify the registry, shrink the injection. Those are infra-free and they are what actually stopped the Rosneft recall failure. D adds something different. It is fuzzy, by-topic recall of OLD turns in a long investigation, the "what did we look at about shell companies a while back" question, where you do not know which turn the finding came from. That is a semantic-search job, not an exact-filter job, so it gets its own store and its own tool.

The hard constraint I built to: this must be a graceful no-op until the user provisions it. The demo that is live right now uses Upstash Redis (L3) and nothing else new. Episodic memory adds a SEPARATE Upstash Vector index, which the user has to create. So I built the code, the wrapper, and the flag, and wired everything through a single gate. If the vector creds and the feature flag are not both present, finalize does not write episodes, the recall_memory tool returns a clean "not configured" result, and nothing in the existing flow changes. The live demo is untouched until someone flips the flag.

What the two tiers do, and why they do not overlap. recall_state is exact L3 recall: "list every sanctioned subsidiary", "the leads from turn 3", "the dismissed name collisions". It is byte-exact, complete, filterable, and it is the source of truth. recall_memory is fuzzy L2 recall: similarity search over one compact episode per turn, ranked by relevance, recency, and salience, returning a small bounded set. The doc 09 anti-pattern I was careful to avoid is using vector search for exact enumeration. A similarity query can miss rows, so it must never be the thing you reach for when the user wants a complete list. The tool description and the prompt both say this in plain terms: recall_memory is for vague by-topic recall, recall_state is for exact and complete enumeration, and if episodic memory is off the agent falls back to recall_state.

The write path reuses the same discipline as Phase A. The episode is built deterministically from the STRUCTURED turn delta, the exact projection finalize already computes, never from the prose answer. That matters because scraping the narrative for "entities the agent mentioned" writes hallucinations and name collisions straight into durable memory. So an episode carries the primary subjects, the structured claim text, the sanctions verdicts by name, the registry entity ids touched, the tools used, a timestamp, and a salience score. The text blob that actually gets embedded is assembled from those fields in a stable order, so re-embedding the same turn is idempotent. Salience is seeded from signals the way doc 09 §7 calls for: a confirmed sanction or a sanctioned entity bumps it, a dismissed-only collision bumps it less, claims add a little. That salience then feeds the ranking.

On the embedding choice: I used Upstash Vector's HOSTED embedding model rather than embedding the text myself. When you create the index with a model, you upsert and query raw strings and Upstash vectorizes them server-side. The reason is simple. The alternative is standing up a separate embedding provider, which means another API key, another secret on Cloud Run, another dependency, and another thing to mock in tests. The hosted model means the only new secrets are the two Upstash Vector creds, and the disabled-path tests need no network at all. I checked what credentials already exist before reaching for a new provider, and the hosted route avoids adding one.

On ranking: it is the Park et al. weighted score from the Generative Agents work, not raw cosine. A memory's retrieval score is a weighted sum of similarity, recency, and salience. I over-fetch on similarity, then re-rank by 0.5 similarity plus 0.3 recency plus 0.2 salience and return the top_k. The point is that a recent, important episode can and should outrank an older, slightly-more-similar one. doc 09 §2 names this directly: episodic ranking is recency times importance times relevance, not raw similarity.

Every Upstash call is wrapped so a vector outage degrades to a no-op instead of failing a turn, the same fail-open posture the rest of the memory code uses. Episodes are namespaced by conversation id, so recall is scoped to the investigation you are in, and the per-namespace cap keeps a single conversation bounded.

On verification: the suite runs with episodic DISABLED, which is the default and how the live demo runs. All eleven existing deterministic memory checks stayed green (two write-path, four registry, five injection-shrink), which is the proof that the no-op path does not regress anything. I added a new episodic_disabled block that locks the graceful-degradation contract: is_enabled is false with no creds, recall_memory returns configured=false with a note that steers the agent to recall_state, write_episode no-ops and returns false, and build_episode is pure and structured-only. I also added a small mock-backed enabled-path check, since the enabled path needs live creds I do not have. It injects a fake Upstash index and asserts write_episode upserts the raw-text episode under the conversation namespace and that query_episodes re-ranks by recency and salience, a recent lower-similarity episode floating above an older higher-similarity one. The mock restores the module afterward so the rest of the suite runs disabled.

### Provisioning: how to actually turn this on

None of this is live until you do these steps. It is intentionally off.

1. Create an Upstash Vector index. In the Upstash console, create a new Vector index. When it asks for an embedding model, pick a hosted text model (for example `bge-base-en-v1.5` or `mixedbread-ai/mxbai-embed-large-v1`). Choosing a model is what lets us upsert and query raw text without our own embedding key. The dimension is fixed by the model you pick (`bge-base-en-v1.5` is 768, `mxbai-embed-large-v1` is 1024), so you do not set it by hand. Use the cosine metric. Do not create a plain vector index with no model, because then raw-text upserts will fail and the wrapper will degrade to a no-op.

2. Set the three values as secrets on Cloud Run. The two creds come from the index's details page in the Upstash console (REST URL and REST token):

```bash
gcloud run services update sayari-demo-backend --region us-central1 \
  --update-env-vars EPISODIC_MEMORY_ENABLED=true \
  --update-env-vars UPSTASH_VECTOR_REST_URL=https://YOUR-INDEX.upstash.io \
  --update-env-vars UPSTASH_VECTOR_REST_TOKEN=YOUR_TOKEN
```

For a real deployment, put the token in Secret Manager and mount it with `--update-secrets UPSTASH_VECTOR_REST_TOKEN=upstash-vector-token:latest` instead of passing it inline, the same way the Sayari and Redis creds are handled today.

3. That is the whole flip. `EPISODIC_MEMORY_ENABLED=true` plus the two creds is the on switch. To turn it back off without removing the creds, set `EPISODIC_MEMORY_ENABLED=false` (or just unset it, it defaults to false). Locally, the same three env vars in `backend/.env` enable it for development.

Status: built and deployed to `sayari-demo-backend` on Cloud Run with episodic memory DISABLED (revision `sayari-demo-backend-00030-hcn`, no new secrets set), `/health` 200 with `agent_impl=graph` and all deps ok. The new code path does not break startup, and the live demo is unchanged until the provisioning steps above are done and the flag is flipped.

Files: `backend/app/episodic.py` (new: the lazy wrapper, `is_enabled` / `write_episode` / `query_episodes` / `build_episode` and the weighted re-rank), `config.py` (the three env-driven settings), `agent_graph.py` (episode write in `finalize_node`, gated), `agent_native.py` (the mirrored write in the legacy loop, gated), `tools.py` (the `recall_memory` tool impl, descriptor, dispatcher wiring, and `conversation_id` injection), `agent_common.py` (no graph nodes + a feed summary for `recall_memory`), `intent.py` (`recall_memory` added to the investigation tool subsets alongside `recall_state`), `prompts.py` (the fuzzy-vs-exact guidance), `requirements.txt` (`upstash-vector==0.8.0`), `evals/run_evals.py` (`_episodic_disabled_rows` + `_episodic_enabled_mock_rows`).

---

## 2026-06-08: Recap routing + truncation hardening

A user asked "Summarize everything you found on Rosneft so far" and the agent reached for the heavy `submit_summary` terminator (the formal RiskSummary). Two things went wrong at once. First, the routing was wrong: a recap is a conversational readback, not a request for the formal deliverable, so it should never fire the report terminator. Second, the report it tried to build was big enough to hit the 4096 output-token ceiling, so the tool-call JSON came back truncated, failed Pydantic validation, and retried about nine times before it squeaked through. So this is two fixes, one for the routing and one for the truncation, because either one alone would have left the other failure mode live.

The routing fix. The word "summarize" was overloaded. "Summarize Rosneft's ownership structure" is a real profile turn; "summarize what you found so far" is a recap. I taught the router and the agent to tell them apart and to send recaps to the lightweight `submit_answer` / TurnAnswer terminator. In `intent.py` I added a tight rule-based recap detector that fires only mid-conversation (there is nothing to recap on turn 1) and only on clear readback phrasing: a recap word ("recap", "rundown", "catch me up", "where do we stand") or "so far" paired with a recall verb. It does not match a bare "summarize X", so a real investigation ask is untouched. A detected recap routes to `conversational_followup` with `wants_report=false`, which guarantees the agent never gets the "finish with submit_summary" nudge, and it carries a recap flag that injects a guidance overlay naming `submit_answer` and explicitly forbidding `submit_summary`. The overlay also tells the agent to ground the recap in the INVESTIGATION STATE core and `recall_state` rather than re-running the investigation, and to set `report_ready=true` plus `offer_risk_report=true` with a one-sentence prompt when a resolved subject and a real signal are already on the table, so the UI still surfaces the "generate risk report" affordance and the user can get the formal card on demand. I did NOT add a third terminator; TurnAnswer already covers the recap shape, and a duplicate would just be one more thing to keep in sync. The prompt got the matching guidance in the Turn types section and in both tool descriptions, and `submit_summary` is now reserved for explicit formal-deliverable asks ("generate a risk report", "compliance memo", "full risk profile") and the initial investigation of a new entity.

The truncation fix has four parts. The first is pure diagnosis: both the graph and native validation-failure paths now `log.warning` the terminator name and the structured errors, so the next failure is readable from the logs alone instead of inferred from a retry storm. Zero behavior change. The second is the ceiling itself: `MAX_TOKENS_PER_TURN` goes from 4096 to 8192, which is enough headroom for a legit large RiskSummary (many claims, each with source_refs, plus sanctions_hits and risk-factor paths) to land in one shot. Sonnet 4.5 supports it and you only pay for tokens actually generated, so the conversational default turn costs no more. It feeds both the graph model bind and the native client calls, which already read the shared constant. The third is a truncation-aware retry. When a terminator fails validation, I check the model's stop reason. If it is `max_tokens`, the args were cut off mid-JSON, and dumping the giant `e.errors()` blob back to the model just grows the input and reinforces the loop. Instead the agent gets a short targeted nudge: your previous terminator was cut off, re-emit it complete but shorter, fewer and terser claims, make sure the JSON closes. A genuine shape error (not a truncation) still gets the structured errors, because there the detail is the useful signal.

The fourth part kills a whole class of validation failure that was making truncation worse. `SourceRef.source` only accepted `"opensanctions"`, but the rest of the stack spells the watchlist source `"sanctions"` (the `SourceSystem` enum, the SSE legend, `recall_state`, `sanctions.py`), so the model frequently emitted `source:"sanctions"` in a claim and the strict Literal rejected it, triggering a retry that then risked truncating again. I normalized it with a pre-validator on `SourceRef.source` that coerces `"sanctions"` to `"opensanctions"` before the Literal check. I chose to canonicalize TO `"opensanctions"` rather than the other way, and to coerce at the schema rather than widening the Literal, because `"opensanctions"` is the exact value the frontend reads to render the watchlist chip (`RiskSummaryCard.tsx`), so existing readers and any stored data stay byte-compatible and the fix is centralized in one place that every construction path (both terminators, both agent impls) flows through.

On verification: the recap-routing decision the LLM makes is not deterministic, so I pinned the deterministic layer underneath it. A new `recap_routing` check asserts the detector fires on recap phrasing, does NOT false-positive on "summarize X's ownership", routes to `conversational_followup` with `wants_report=false`, skips turn 1, emits guidance that names `submit_answer` with no `submit_summary` nudge, and that `submit_answer` validates into a TurnAnswer. A new `source_enum` check asserts `source:"sanctions"` coerces to `"opensanctions"` on a bare SourceRef and through a full TurnAnswer terminator, and that the canonical values still pass. Both are instant and API-free. All eleven existing deterministic memory checks (two write-path, four registry, five injection-shrink) stay green, so the routing changes did not regress them.

On the suite itself: 63/64 live + deterministic checks passed. The one miss is the documented `record_provenance` nondeterminism (the model answered without calling `sayari_record`), unrelated to this change. The live `explicit_report_request` case proved the truncation fix end to end: the model emitted a 4860-token RiskSummary, which is above the old 4096 ceiling and would have been cut off, and the new 8192 ceiling let it land (it then retried once on an unrelated `state_owned` signal-enum slip and passed).

Status: deployed to `sayari-demo-backend` on Cloud Run (revision `sayari-demo-backend-00029-2p4`), `/health` 200 with `agent_impl=graph` and all deps ok.

Files: `backend/app/intent.py` (rule-based recap detector + shortcut, recap guidance overlay, tightened `wants_report` description), `prompts.py` (recap routing in the Turn types section and both terminator tool descriptions), `agent_common.py` (`MAX_TOKENS_PER_TURN` 4096 -> 8192), `agent_graph.py` (validation-failure logging + truncation-aware retry), `agent_native.py` (validation-failure logging + the `_terminator_retry_content` helper across all three terminator paths), `schema.py` (`SourceRef.source` pre-validator), `evals/run_evals.py` (`_recap_routing_rows` + `_source_enum_rows`).

---

## 2026-06-07: Shrink the injected memory core (IMS Phase C)

Phase A made facts durable. Phase B unified them into one rankable registry. Both were write-path work. Phase C is the read-path counterpart: stop using the prompt as long-term memory. Every turn was injecting a prose digest PLUS a roster of up to 30 graph nodes PLUS the INVESTIGATION STATE block, and several of those slices grew with the investigation and silently truncated. That is the named context-stuffing anti-pattern from the guide, and it is why a bigger window never helped: stuffing more in was the problem, not the cure. The fix is to hold the injected core to a small FIXED budget of navigation hints and lean on `recall_state` (now registry-backed) for the exact rows.

What I cut. The "KNOWN GRAPH ENTITIES" roster was a UI artifact that leaked into the memory mechanism. It is a capped, truncating list of traversed nodes, and the registry already holds every one of those entities with better provenance. On the graph path I dropped it entirely. It survives only as a small bounded fallback (cap 8) for the native loop, which keeps no `state_doc` and so has no registry pointer to fall back on. Then I rewrote `_render_state_block` from a row dump into pure navigation hints: the primary subject(s) from `resolved_entities` (newest first, cap 2), pinned ids (cap 8), one header line per recent search (cap 3), the top few CONFIRMED sanctions BY NAME (cap 5), and a single registry pointer that states how many entities are tracked, how many are sanctioned, and which `recall_state` call ranks or enumerates them. The old inline top-10 entity dump and the `sanctions_id -> verdict` list are gone. The agent pages the exact rows on demand.

The one subtlety worth calling out: the inline sanctions line shows CONFIRMED verdicts only. The registry deposits dismissed name collisions as sanctioned entities too (correctly, since "Rosneft Trading S.A." really is SDN-listed even though it is not the subject), so naively filtering the registry by `sanctioned=true` for the core would have printed a dismissed collision as if it were a hit. Confirmed-only inline keeps the always-on core accurate; the dismissed rows stay one `recall_state(kind="sanctions")` away.

I measured the result on a representative multi-turn investigation, rendering the old block and the new one against the same state at three sizes. Old: roughly 461, 695, and 754 tokens for a small, medium, and large case. New: 251, 260, and 261 tokens. So 45 percent, 62 percent, then 65 percent smaller, and the more important property is that the new core is FLAT. It does not grow as the case grows. The percentage win widens with investigation size precisely because the old block scaled and the new one does not.

Making the core lean has an obvious risk: if the agent answers an enumeration from the thin core instead of recalling, it will under-count. So I tightened the read contract on both ends. The prompt got a "Recalling prior findings" section that says, in plain terms, the INVESTIGATION STATE block is navigation hints and not the record, so any exact or complete enumeration ("list all the sanctioned subsidiaries", "which leads were there") must go through `recall_state`, with the kinds spelled out. The `conversational_followup` intent guidance says the same thing at routing time. The point is that shrinking the core must push the agent toward the tool, not toward guessing.

I also shipped the optional Phase 2.5 prefetch, kept deliberately small. When a `conversational_followup` message keyword-matches a known bucket (sanctions words like "sanctioned"/"sdn"/"subsidiar", or lead words like "lead"/"candidate"), `build_followup_prefetch` injects ONE bounded slice of at most six exact rows. This is retrieval (a deterministic keyword match producing one bounded result), not stuffing (everything, every turn). The sanctions slice surfaces confirmed AND dismissed verdicts by name, so the canonical Rosneft "which of those were sanctioned again?" answers in one hop without a round-trip. It is pure and deterministic, reads only `state_doc`, spends no credits, and makes no model call. It only fires on the live path; the eval harness runs with an empty `state_doc`, so it no-ops there.

On the recall-degradation question the brief flagged: reasoning through the two canonical flows against the new core, both hold. The Rosneft three-turn flow is safe because the dismissed subsidiary never lived in the core to begin with; it lives in the sanctions ledger and the projected registry (Phase A and B), which the lean core points at and the prefetch surfaces. The Gazprom "most sanctioned" flow routes entirely through `recall_state(kind="entities", sort="severity")`, which reads the registry, not the injected core, so shrinking the core cannot touch it. The deterministic `entity_registry` check confirms the SDN entity still ranks first, unchanged.

On verification: the live harness cannot see the read-path changes (it builds context with an empty graph and empty `state_doc`, so no roster and no state core render), which is the same `persist=False` limitation as A and B. So I pinned a deterministic `injection_shrink` regression in `run_evals.py`. It renders `_render_state_block` over a tiny investigation and a huge one (300 entities, 60 leads, 80 sanctions rows) and asserts the rendered core is the same bounded size, the lean core does NOT inline the dismissed name, the dismissed row stays byte-exact recoverable in both the ledger and the projected registry, and the prefetch surfaces it. All five pass, alongside the six existing A and B memory checks, so all eleven deterministic memory checks are green. The live suite ran 51/54; the three misses are the same LLM tool-choice and OFAC-phrasing nondeterminism documented under Phase B (Huawei OFAC-label drift, the watchlist case picking ownership instead of the watchlist traversal, and the provenance case skipping `sayari_record`), none of which touch Phase C code or even exercise the changed read path.

Status: deployed to `sayari-demo-backend` on Cloud Run (revision `sayari-demo-backend-00028-cmp`), `/health` 200 with `agent_impl=graph` and all deps ok.

Files: `backend/app/agent_common.py` (rewrote `_render_state_block` to the fixed-budget core, fixed-budget cap constants, local `_is_sdn` helper, the `build_context_block` roster demotion, and the new `build_followup_prefetch`), `agent_graph.py` (import + wire the prefetch into `run_turn` for `conversational_followup`), `prompts.py` (the "Recalling prior findings" section), `intent.py` (enumeration guidance in `conversational_followup`), `evals/run_evals.py` (`_injection_shrink_rows` regression).

---

## 2026-06-06: Unify entity identity into one registry (IMS Phase B)

Phase A widened the write path so structured facts stop falling on the floor. Phase B fixes the next layer of the same disease: even when we DID keep an entity, its identity was scattered across three different stores that never reconciled. `resolved_entities` was name-keyed. `named_ids` was id-keyed. Sanctions hits lived only as ledger rows. And each graph mapper named its nodes from whatever it happened to be holding. Doc 08 calls this "two pipes, one missing source of truth," and it is exactly why the agent missed the "most sanctioned connected entity" on Gazprom. It could rank the entities in `resolved_entities`, but the OFAC SDN entity that surfaced through `check_sanctions` was never an entity in its memory, just a sanctions row, so it was invisible to any ranking over the connected set.

The fix is the registry doc 08 designed: one id-keyed `entities` store that every tool deposits into and every consumer reads from. The shape is `{id: {label, type, sanctioned, pep, is_sdn, countries, sanctions_lists, source, confidence, first_seen_turn, last_seen_turn}}`. The KEY addition over the legacy buckets is that strong `check_sanctions` hits become first-class registry entities keyed by their `sanctions_id`, not just ledger rows. That single change is what makes the full connected set, ownership neighbors plus search leads plus sanctions hits, one queryable, rankable pool.

The implementation choice that made this safe and small: the registry is a deterministic PROJECTION over the buckets we already persist, not a fourth parallel store. `_project_entities` folds `named_ids`, `leads`, `resolved_entities`, and the sanctions ledger into the id-keyed registry with a merge policy (richer source wins the label, True wins the flags, countries and sanctions lists union). `get_state_doc` recomputes it on every read and `merge_state_doc` recomputes it on every write. Because it is reconstructable from durable buckets, backward compatibility is automatic: an old stored doc that predates the registry backfills a populated `entities` on the next read with no migration step, and nothing that still reads the legacy buckets (the frontend hydrate, the existing `recall_state` kinds) breaks because those buckets are still written exactly as before. Every deposit point the brief asked for is covered for free, because each one already lands in a bucket the projection folds: search leads via `leads`, profile/ownership/summary/watchlist neighbors via the traversed nodes in `resolved_entities`, the bounded risk-path resolver via `named_ids`, and the sanctions hits via the ledger.

Consumers now read the one registry. `_known_entity_lookup` (which feeds every graph mapper's `id_lookup`) reads `entities` first, so the mappers name nodes from the unified store instead of each doing its own best-effort. `_render_state_block` sources its entity line from `entities`, so a sanctioned entity that only ever appeared through `check_sanctions` now shows up in the injected core where it was previously invisible. I deliberately did not change the block's size or budget; that trimming is Phase C.

For the "most sanctioned" question itself, I added `recall_state(kind="entities", sort="severity")` with a deterministic severity score: OFAC SDN dominates, then any other sanctioned entity ranked by the number of distinct sanctions regimes, then a small bump for PEP. Each returned item carries `is_sdn`, `regime_count`, and `severity_score` so the agent can both rank and state its criterion. The prompt now tells the agent that for any superlative or ranked ask it should rank across the FULL registry with this tool, STATE the criterion it used, and OFFER to re-sort by an alternative, without stopping to ask a clarifying question every time. I also added `kind="claims"` (the structured claims the terminator emitted, with their `source_refs` and the entity ids they resolve to) since doc 09's state shape includes it and it was cheap to persist structured-only.

On the name-collision worry: depositing a dismissed `check_sanctions` hit as a sanctioned registry entity is correct, not a bug. The dismissal means the matched entity is not the SAME as the subject, but the matched entity itself is genuinely on the list. "Rosneft Trading S.A." really is SDN-listed; the registry should know it as a sanctioned entity in the neighborhood. The hallucinated-write trap is about scraping prose, and we still never do that. Every deposit traces to parsed tool output or a typed schema field.

On verification: same `persist=False` limitation as Phase A, so the live harness cannot observe the registry. I pinned a deterministic `entity_registry` regression in `run_evals.py`: a turn surfaces a Gazprom-Shelfproekt-style OFAC SDN `check_sanctions` hit alongside a clean ownership neighbor, and it asserts the SDN entity is deposited, ranks first by severity ahead of the neighbor, and that an old-shape doc with no `entities` key backfills correctly. All six deterministic memory checks pass. The live suite ran 46/49; the three misses are LLM-nondeterminism on cases unrelated to the registry (Huawei factor-surfacing and OFAC-label drift, plus a malformed `$FUNCTIONS.sayari_record` tool-name the model emitted), none of which touch Phase B code.

Status: deployed to `sayari-demo-backend` on Cloud Run (revision `sayari-demo-backend-00027-ztq`), `/health` 200 with `agent_impl=graph` and all deps ok.

Files: `backend/app/conversations.py` (the `entities` + `claims` buckets, `_project_entities`, `_upsert_entity`, `entity_severity_score`, `_is_sdn_label`, `_sanctions_regimes`, backfill in `get_state_doc`, recompute + claims merge in `merge_state_doc`), `agent_graph.py` (`_build_state_delta` deposits claims and enriches sanctions rows with countries), `agent_common.py` (`_render_state_block` sources from `entities`), `tools.py` (`_known_entity_lookup` reads the registry, `recall_state_tool` gains `kind="entities"`/`kind="claims"` + `sort`, `_entity_view`, updated descriptor), `prompts.py` (the ranking/superlative section), `intent.py` (ranking guidance), `evals/run_evals.py` (`_entity_registry_rows` regression).

Where the code diverged from doc 09 (reconcile before Phase C): doc 09 §5 said "write only the new shape" and fold the legacy buckets away. I kept the legacy buckets written as-is and made `entities` a projection over them instead. The reason is backward compatibility, which the brief made mandatory: the frontend hydrate and the existing `recall_state` kinds still read the old buckets, and a read-time projection backfills old docs with zero migration risk. The net effect (one id-keyed registry everything reads) is identical; only the storage strategy differs. I also did not rename `leads` to `lead_sets` or `sanctions_adjudicated` to `sanctions_ledger`, for the same reason. If Phase C or later wants the literal doc-09 storage shape, that is a focused follow-up: switch the projection to a stored bucket and update the legacy readers in the same change.

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
