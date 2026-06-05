# Memory Implementation Plan

> The actionable, sequenced execution of `06-memory-architecture.md`. That doc is
> the audit + design (current state, gaps, best-practices research with sources,
> the `state_doc` proposal, the Upstash Vector path). This doc is the build plan:
> phased milestones with file-level changes, data shapes, acceptance criteria,
> and effort. Read 06 first for the "why"; this is the "what, where, and in what
> order."

> Note: `09-investigation-memory-subsystem.md` is now the consolidated,
> authoritative memory design. This doc (07) is the original phased plan; 08 is
> the registry contract. Both are folded into 09, which wins where they disagree.

All work targets the **LangGraph** agent (`agent_graph.py`). The native loop
(`agent_native.py`) is treated as a dead branch to retire, not a second target.
Don't build memory twice.

---

## Goal & decisions

**Goal:** give the Entity Risk Resolver prod-grade memory so it stops forgetting
the leads, IDs, and verdicts it just produced, and can recall older relevant
episodes on demand — without bloating context or re-running searches.

The locked decisions this plan executes:

1. **100% LangGraph.** Build only on `agent_graph.py`. Use the LangGraph
   **checkpointer** for short-term thread state and the **`BaseStore`** interface
   for long-term memory. Default `settings.agent_impl` to `"graph"`
   (`config.py:43`). Retire `agent_native.py` as a dead branch (cleanup step in
   Phase 5; not deleted day one).
2. **Two complementary layers, not one blob.**
   - **Short-term / structured investigation state** = deterministic, exact. A
     `state_doc` (resolved entities by ID, full lead lists with entity IDs +
     pinned flags, sanctions verdicts confirmed/dismissed, key paths). Merged in
     `finalize_node` from data already in hand; rendered as an `INVESTIGATION
     STATE` block by `build_context_block`.
   - **Long-term / episodic memory** = ranked retrieval via **Upstash Vector** (a
     SEPARATE Upstash product from the Upstash Redis already wired in
     `conversations.py`). Ranked by recency + salience, with an eviction/cap so
     context can't bloat.
3. **Memory as a tool, not just a context dump.** Add agent-callable tools
   (MemGPT/Letta pattern) so the agent can query its own structured state and
   episodic memory mid-turn ("enumerate the earlier leads", "profile the 3rd one").
4. **Provenance preservation in compaction.** Summaries must retain `source_refs`,
   `risk_factor` names, traversal `path`s, and sanctions verdicts — never collapse
   them into lossy prose.
5. **Memory regression evals.** New cases in `evals/run_evals.py`: after a broad
   `sayari_search`, the next turn enumerates leads/IDs WITHOUT re-searching;
   "profile the Nth lead" resolves correctly; provenance survives across turns.
6. **Cleanup.** Remove the dead `load_agent_messages`/`save_agent_messages` path
   and stop relying on the prose-only `context` string for IDs.

**Why this order:** Phase 1 is the highest ROI and needs no new infra — it
directly kills the lead-list amnesia called out as the most likely on-stage
failure (06 §2.1). Phases 2-4 layer on the tool surface, episodic recall, and
lossless compaction. Phase 5 locks it in with evals and consolidates onto
LangGraph.

---

## Architecture at a glance

| Layer | Store | Scope | Access | Phase |
| --- | --- | --- | --- | --- |
| Working memory (message array) | LangGraph checkpointer | one turn (thread) | implicit | 1 (adopt) |
| Structured investigation state (`state_doc`) | Upstash **Redis** | one conversation | always-in-context core + `recall_state` tool | 1, 2 |
| Episodic memory (per-turn episodes) | Upstash **Vector** via `BaseStore` | conversation (later cross-conversation) | `recall_memory` tool | 3 |
| Procedural memory | system prompt / code | static | n/a | (existing) |

The current app has **only** a prose always-in-context tier (the `context`
digest + 30-node roster from `build_context_block`). Phase 1 adds a structured
always-in-context tier; Phases 2-3 split it into "small core always present" +
"detail fetched on demand" (the Letta split from 06 §4.2).

---

## Phase 1 — Structured `state_doc` + `INVESTIGATION STATE` block + dead-code cleanup

**Highest ROI. No new infra. Friday-demo-safe.** This is 06 §4.1 made concrete.

### Scope

Add one Redis key holding a structured, exact-recall investigation state; merge
it deterministically in `finalize_node` from data already in hand; render it as
an ID-rich `INVESTIGATION STATE` block ahead of the prose digest; remove the dead
`agent_msgs` path and bound the prose digest.

### File-level changes

**1. `conversations.py` — new key + accessors (mirror `merge_graph`).**

New key alongside the existing `conversation:{id}:*` family:

```
conversation:{id}:state_doc   ->  JSON (SET, refresh TTL each write)
```

Add three functions modeled on `get_graph` / `merge_graph` (`conversations.py:227-255`):

- `async def get_state_doc(conversation_id) -> dict` — read-or-default to the
  empty shape below.
- `async def merge_state_doc(conversation_id, delta: dict) -> dict` —
  read-modify-write: upsert `resolved_entities` by key, append/dedupe `leads` by
  `entity_id` (keep the most recent `from_turn`), append `sanctions_adjudicated`
  by `sanctions_id`, union `pinned_node_ids`, append `turn_log`. Cap `leads` to
  `_MAX_LEADS` (≈40) by recency. Refresh TTL with `_TTL_SECONDS`.
- Add `state_doc` to `hydrate()` (`conversations.py:286-302`) so a page reload can
  restore it (and so the UI can later show "what the agent knows").

`state_doc` shape (the empty default has all five keys present with empty
containers):

```jsonc
{
  "resolved_entities": {
    // keyed by a normalized subject string (lowercased, trimmed)
    "gazprom": {
      "entity_id": "ABC123",
      "label": "Public Joint Stock Company Gazprom",
      "type": "company",
      "source": "sayari",        // sayari | icij
      "sanctioned": true,
      "pep": false,
      "first_seen_turn": 1,
      "last_seen_turn": 3
    }
  },
  "leads": [
    // the FULL sayari_search lead lists, not just the pinned top-N
    {
      "entity_id": "E1", "label": "Acme Trading Ltd", "type": "company",
      "countries": ["CYP"], "sanctioned": false, "pep": false,
      "top_risk": ["shell_company_pattern"],
      "from_turn": 3, "from_query": "Gazprom-linked Cyprus shells",
      "pinned_to_graph": true
    }
    // ... capped to ~40, newest-first
  ],
  "sanctions_adjudicated": [
    {
      "sanctions_id": "ofac-30947", "matched_name": "...",
      "lists": ["OFAC Consolidated (non-SDN)"],
      "verdict": "confirmed",        // confirmed | dismissed
      "from_turn": 2
    }
  ],
  "pinned_node_ids": ["ABC123", "E1"],
  "turn_log": [
    { "turn": 1, "intent": "profile_entity", "subject": "Gazprom", "kind": "investigation" }
  ]
}
```

**2. Capture the full `sayari_search` lead list in the tools loop.**

Today `sayari_search_tool` (`tools.py:177-215`) already returns every lead with a
`pinned_to_graph` flag and `pinned_entity_ids`, but `graph_payload`
(`agent_common.py:309-321`) only forwards the pinned subset to the graph and the
full list is dropped after the turn. In `agent_graph.tools_node`
(`agent_graph.py:308-438`):

- Add a new accumulator field to `TurnState` (`agent_graph.py:86-101`):
  `turn_leads: Annotated[list[dict], operator.add]`.
- In the regular-tool branch, when `name == "sayari_search"`, extract
  `parsed["candidates"]` (each already carries `entity_id`, `label`, `type`,
  `countries`, `sanctioned`, `pep`, `top_risk`, `pinned_to_graph` — see
  `schema.SayariSearchCandidate` and `sayari_search_tool`) and append to
  `turn_leads` with `from_turn`/`from_query` stamped on.
- Return `turn_leads` from the node and add it to `_initial_state`.

Note: the leads are already parsed (`tools_node` does `json.loads(result_json)`),
so this is reading fields off `parsed`, not a new parse.

**3. `agent_graph.finalize_node` — build the delta and merge it.**

In `finalize_node` (`agent_graph.py:441-508`), next to the existing
`merge_graph` / `append_summary` / `append_answer` / `set_context` calls, build a
`delta` from data already in hand and call `merge_state_doc`:

- **Resolved entities:** from `summary.entity_id` + `summary.entity_name` (or the
  `TurnAnswer.referenced_node_ids` + Sayari `source_refs`), plus
  `state["turn_nodes"]` (the agent-traversed nodes already merged into the graph).
  Normalize the subject string for the key.
- **Leads:** from the new `state["turn_leads"]`.
- **Sanctions adjudicated:** reuse `build_sanctions_review(summary,
  state["raw_strong_hits"])` (`agent_common.py:437-454`) which ALREADY computes
  `confirmed` vs `dismissed`. Map each into a `sanctions_adjudicated` row with the
  verdict. (For answer turns, fall back to `answer.sanctions_hits`.)
- **Pinned + turn_log:** `pinned_node_ids` from the turn; one `turn_log` row with
  the intent (available from the router result — thread it into state, see below).

To get the intent label into `finalize_node`, add `intent: str | None` to
`TurnState` and set it in `run_turn`/`evaluate_turn` from `_route_turn`'s result
(currently `_route_turn` at `agent_graph.py:579-606` returns only the augmented
block + tool names; have it also return the intent string).

Guard everything behind `if persist:` exactly like the other writes
(`agent_graph.py:462`), so eval mode stays side-effect-free.

**4. `agent_common.build_context_block` — render the `INVESTIGATION STATE` block.**

Change the signature to accept `state_doc` and render a compact, ID-rich block
BEFORE the existing `CONVERSATION CONTEXT` prose (`agent_common.py:232-272`):

```
INVESTIGATION STATE (structured — reuse these IDs, do NOT re-search):
Resolved: Gazprom=ABC123 (company, SANCTIONED); Sberbank=DEF456 (company)
Leads from turn 3 ("Gazprom-linked Cyprus shells") — 14 total, showing 6:
  E1 Acme Trading Ltd (CYP, shell_company_pattern) [pinned]
  E2 Beta Holdings Ltd (CYP) [not pinned]
  ... 8 more — call recall_state(kind="leads", from_turn=3) to enumerate all.
Sanctions adjudicated: ofac-30947 -> confirmed (OFAC Consolidated non-SDN);
  ofac-91002 -> dismissed (name collision)
```

Rendering rules: cap the inline lead list to ~6 (newest set first), state the
true total, and point at the Phase 2 tool for the rest. Resolved entities and
sanctions verdicts render in full (small, high-value). Keep the existing 30-node
roster and prose digest below it — they stay for narrative continuity but are no
longer the source of truth for IDs.

Thread `state_doc` through both call sites: `agent_graph.run_turn`
(`agent_graph.py:626-628`) and `evaluate_turn` (`agent_graph.py:655`). `run_turn`
already reads `context` and `graph`; add `state_doc = await
conversations.get_state_doc(conversation_id)`.

**5. Dead-code cleanup + digest bound.**

- Delete `save_agent_messages` and `load_agent_messages`
  (`conversations.py:209-221`) and the `messages_to_dict(...)` call in
  `finalize_node` (`agent_graph.py:496`). `load_agent_messages` is never read
  (06 §1.6); the structured `state_doc` is the better continuity mechanism and
  this removes a Redis write per turn.
- Bound the prose digest in `finalize_node`: keep the last ~15 lines verbatim,
  roll older lines into a single `"earlier in this investigation: ..."` line. No
  LLM call — pure string handling. (06 §4.1.)

### Acceptance criteria

- After a turn with a broad `sayari_search`, `conversation:{id}:state_doc`
  contains the full lead list (not just pinned), each with `entity_id` +
  `from_turn` + `from_query`.
- The next turn's prompt contains an `INVESTIGATION STATE` block listing resolved
  entities by ID and the lead set header with the true total.
- A confirmed/dismissed sanctions verdict from turn N appears in
  `sanctions_adjudicated` and renders on turn N+1.
- `load_agent_messages`/`save_agent_messages` are gone; no Redis `agent_msgs`
  write occurs.
- All existing `evals/run_evals.py` cases still pass (no behavior regression;
  the block is additive context).
- SSE contract and React Flow graph unchanged (only `finalize` writes + the
  context builder changed).

### Effort

~1-2 focused sessions (~120-180 lines across `conversations.py`,
`agent_common.py`, `agent_graph.py`). No new dependency or infra.

---

## Phase 2 — Agent-callable recall/state tool over the structured state

**Letta "archival/on-demand" split + MemGPT memory-as-a-tool.** Stop dumping the
whole state into every prompt; keep a small core inline and let the agent pull
the rest by tool call.

### Scope

Add one read-only tool, `recall_state`, that queries the `state_doc` by kind and
filter, and bind it on the intents where follow-ups happen. Trim the inline
`INVESTIGATION STATE` block to a small core now that detail is fetchable.

### File-level changes

**1. New tool descriptor in `tools.py` (`TOOLS` list, `tools.py:261`).**

```jsonc
{
  "name": "recall_state",
  "description": "Query your own structured investigation memory for THIS conversation — resolved entities (by id), the full lead lists from earlier sayari_search calls, and adjudicated sanctions verdicts. Use this to enumerate or filter things you already found instead of re-searching (e.g. 'list all leads from the earlier search', 'which leads were Cyprus-registered', 'what's the entity_id for the 3rd lead'). Returns exact stored records; does NOT call any external API or spend credits. Prefer this over re-running sayari_search/sayari_resolve for a subject already in state.",
  "input_schema": {
    "type": "object",
    "properties": {
      "kind": {"type": "string", "enum": ["leads", "resolved_entities", "sanctions"],
               "description": "Which slice of state to read."},
      "from_turn": {"type": "integer", "description": "Optional: only leads/items first seen on this turn."},
      "country": {"type": "string", "description": "Optional ISO trigram filter for leads, e.g. 'CYP'."},
      "sanctioned": {"type": "boolean", "description": "Optional: only sanctioned items."},
      "index": {"type": "integer", "description": "Optional 1-based index into the most recent lead set (for 'profile the Nth one')."},
      "limit": {"type": "integer", "default": 25}
    },
    "required": ["kind"]
  }
}
```

**Returns:** a JSON object `{ "items": [...], "count": N, "total_in_state": M }`
where each item is the exact stored record (so IDs/provenance are byte-exact).
For `index`, resolve against the most-recent lead set (highest `from_turn`) so
"profile the 3rd one" maps to a concrete `entity_id` the agent then passes to
`sayari_profile`.

**2. Implementation + dispatcher.**

- Add `recall_state_tool(conversation_id, kind, ...)` to `tools.py`. It needs the
  `conversation_id`, which the current `execute_tool(name, arguments)` signature
  (`tools.py:661`) does not pass. Smallest change: thread an optional
  `conversation_id` through `execute_tool` and inject it for `recall_state` in
  `tools_node` (the node already has `state["conversation_id"]`). Keep it out of
  the model-visible `input_schema` so the model can't spoof it.
- Register in the `_ASYNC` map (`tools.py:649-658`).
- It reads via `conversations.get_state_doc` and filters in Python. No graph
  payload (`graph_payload` already returns `[], []` for non-graph tools by
  default — add `recall_state` to that guard so it never pollutes the canvas).

**3. Bind it via the intent router (`intent.py`).**

Add `recall_state` to the `_INTENT_TOOLS` lists where recall matters:
`conversational_followup` (currently `[]` = full set, so it's already available),
`broad_search`, `profile_entity`, and `ownership_network` (`intent.py:35-67`).
Because the meta intents already bind the full toolset, the main lift is the
specific intents. Add a one-line note to `_GUIDANCE["broad_search"]` and
`_GUIDANCE["conversational_followup"]`: "to revisit earlier leads, call
recall_state — do NOT re-run sayari_search."

**4. Trim the inline block (`agent_common.build_context_block`).**

Now that detail is fetchable, shrink the always-in-context core to: resolved
primary subjects (name→id), pinned IDs, and a one-line lead-set header per recent
search ("14 leads from turn 3 — call recall_state to enumerate"). This is the
token win the Letta split buys (06 §5 mid-term row).

### Acceptance criteria

- A turn can call `recall_state(kind="leads", from_turn=3)` and get back every
  lead from that search, with exact `entity_id`s, without any `sayari_search`
  call in `tools_used`.
- `recall_state(kind="leads", index=3)` returns the 3rd lead of the most recent
  set; the agent then `sayari_profile`s that exact `entity_id`.
- `recall_state` never adds nodes/edges to the graph and never calls Sayari/ICIJ.
- Inline context block is materially smaller than Phase 1 (core only).

### Effort

~1 session (~90-130 lines, mostly `tools.py` + a signature thread + intent
wiring). No new infra.

---

## Phase 3 — Upstash Vector episodic memory via the LangGraph store

**Prod-grade long-term recall.** This is the new-infra phase (06 §4.2, §3.5).

### Scope

Provision Upstash Vector, back a LangGraph `BaseStore` with it, write a per-turn
episode at finalize, and expose `recall_memory` as an agent tool with
recency+salience ranking and an eviction cap.

### Provisioning + config (do this first)

- Create an **Upstash Vector** index (separate product from Upstash Redis).
  Choose embedding model — either Upstash's hosted embedding (set on the index,
  zero client embedding code) or a client-side model (see Open Questions). Prefer
  a **hybrid index** (dense + sparse, RRF fusion) so "OFAC" keyword hits and
  semantic matches both surface (06 §3.5).
- Add to `config.py:Settings`: `upstash_vector_rest_url` (alias
  `UPSTASH_VECTOR_REST_URL`) and `upstash_vector_rest_token`
  (alias `UPSTASH_VECTOR_REST_TOKEN`), plus a feature flag
  `episodic_memory_enabled: bool = Field(default=False, alias=
  "EPISODIC_MEMORY_ENABLED")` so the phase ships dark and flips on safely.
- SDK choice: the `upstash-vector` Python SDK (REST, async-capable), matching the
  existing `httpx`-against-REST style in `conversations.py`. Add to the backend
  deps. Document the two new secrets in the deploy/runbook.

### File-level changes

**1. New module `app/episodic.py` (or extend `conversations.py`).**

- `async def write_episode(conversation_id, turn, episode)` — embed + upsert one
  vector per turn:

```
namespace = f"conversation:{conversation_id}"   # later also a user/global namespace
id        = f"{conversation_id}:{turn}"
data      = digest + key claim texts            # the text to embed/display
metadata  = { "turn": N, "intent": "...", "entity_ids": [...],
              "sanctioned": bool, "countries": [...], "created_at": ts,
              "source_refs": [...], "salience": float }
```

  `source_refs` and `risk_factor` names ride in metadata so retrieval is
  provenance-preserving (ties into Phase 4). ADD-only (never overwrite/delete an
  episode — mark superseded instead; Mem0's safe default, 06 §3.3).

- `async def search_episodes(conversation_id, query, *, filter=None, limit=8)` —
  vector query with metadata filter (`sanctioned = true AND country = 'CY'`
  SQL-like syntax), then re-rank by **recency + salience** on top of the vector
  score rather than raw similarity (CalibreOS formula, 06 §3.2):
  `score = w_sim * sim + w_recency * recency_decay(age) + w_salience * salience`.
- Eviction/cap: keep newest/most-salient ≈200 episodes per namespace; drop the
  tail. Salience seeded from signals present (sanctioned/PEP/confirmed hit → high;
  greeting/clarify → low).

**2. Wire the LangGraph `BaseStore`.**

- Implement a thin `BaseStore` backed by `episodic.py` (or use LangGraph's store
  + Upstash as the index backend). Pass `store=...` when compiling the graph
  (`agent_graph._graph()`, `agent_graph.py:527-543`). The **checkpointer** stays
  the per-turn working memory; the **store** is the long-term tier — the textbook
  LangGraph split (06 §3.3, §4.2).
- Episodic **write** happens in `finalize_node`, next to `set_context` /
  `merge_graph` / `merge_state_doc` (`agent_graph.py:466-497`), behind
  `if persist and settings.episodic_memory_enabled:`.

**3. New tool `recall_memory` (`tools.py` `TOOLS`).**

```jsonc
{
  "name": "recall_memory",
  "description": "Semantically recall earlier moments in this (long) investigation that aren't in your immediate context — e.g. 'did we already screen this owner?', 'what did the turn-2 sanctions check find?'. Ranked by relevance + recency + importance, with optional filters (sanctioned, country). Returns past episodes with their entity_ids and source_refs so you can re-cite without redoing work. Use for OLDER context; for exact current-conversation state (lead lists, resolved ids) prefer recall_state.",
  "input_schema": {
    "type": "object",
    "properties": {
      "query": {"type": "string"},
      "country": {"type": "string"},
      "sanctioned": {"type": "boolean"},
      "limit": {"type": "integer", "default": 8}
    },
    "required": ["query"]
  }
}
```

  Backed by `episodic.search_episodes`, scoped to the current conversation
  namespace. Same `conversation_id`-injection mechanism as `recall_state`.
  Bind it on `conversational_followup`, `sanctions_screening`, and `provenance`
  intents.

### Eventual-consistency guard

Upstash Vector has a write→queryable lag (06 §3.5). **Keep same-conversation exact
recall in Redis** (`state_doc` + `recall_state`, Phases 1-2, immediate). Use Vector
only for "earlier in a long investigation" and (later) cross-conversation recall,
where a few seconds of lag is harmless. Document this split in the tool
descriptions so the agent reaches for `recall_state` first.

### Acceptance criteria

- With the flag on, each persisted turn writes exactly one episode vector with
  metadata carrying `entity_ids` + `source_refs`.
- `recall_memory("the sanctions check on the owner")` returns the relevant past
  episode ranked above noise, with its `source_refs` intact.
- Metadata filters work (`sanctioned=true` returns only sanctioned episodes).
- Flag off → zero Vector calls, identical behavior to Phase 2.
- No regression in `run_evals.py` (evals run with the flag off / `persist=False`).

### Effort

~2-3 sessions including provisioning, ranking tuning, and the store wiring. New
dependency + new infra + new secrets.

---

## Phase 4 — Provenance-preserving compaction

**Make every compaction lossless on the things this product sells.**

### Scope

Replace lossy prose collapse with structured retention so `source_refs`,
`risk_factor` names, traversal `path`s, and sanctions verdicts survive across
turns and into episodic memory.

### File-level changes

**1. Provenance-aware digests (`agent_common.digest_summary` / `digest_answer`,
`agent_common.py:283-303`).**

Today `digest_summary` keeps the first 2 claim texts + signal tags + a sanctions
*count*; `digest_answer` keeps a 220-char snippet. Both throw away IDs and
provenance (06 §1.5). Change them to emit a structured episode object (consumed by
`merge_state_doc` and `write_episode`) carrying, per claim: `text`, the
`source_refs` (the `node_id`/`sanctions_id`/`sayari_entity_id`/`risk_factor`/`leak`
already on `schema.SourceRef`), and `confidence`; plus the surfaced
`sayari_risk_factors` WITH their `path`; plus the confirmed/dismissed sanctions
rows. The human-readable prose line stays for the narrative digest, but the
structured payload is what feeds memory.

**2. `state_doc` provenance fields.**

Extend the Phase 1 records: add `source_refs` to resolved-entity and lead records
where available, and keep `path` on any surfaced risk factor stored in state. The
`INVESTIGATION STATE` renderer cites them compactly (e.g.
`SANCTIONED via factor sanctioned_usa_ofac_non_sdn`).

**3. Bounded narrative roll-up keeps structure.**

When the prose digest rolls older lines (Phase 1 §5), the IDs/verdicts those lines
referenced must already live in `state_doc` (exact) and episodic memory
(retrievable) — so the roll-up loses only narration, never provenance. Add a test
asserting that after a roll-up, a confirmed sanctions verdict and its
`sanctions_id` are still recoverable via `recall_state`.

### Acceptance criteria

- A claim made on turn 2 can be re-cited on turn 9 with its original `source_ref`
  (node_id / sanctions_id / sayari_entity_id / risk_factor) intact, without
  re-running the tool that produced it.
- A surfaced risk factor's traversal `path` survives into `state_doc` /
  episodic metadata (not collapsed to prose).
- Sanctions verdicts (confirmed vs dismissed) persist with their `sanctions_id`
  and `lists` verbatim — no upgrade/blur of OFAC list type (consistent with the
  `ofac_non_sdn_labeling` discipline already enforced in `run_evals.py`).

### Effort

~1 session (~80-120 lines, mostly in `agent_common.py` + the Phase 1 merge/render
code). No new infra.

---

## Phase 5 — Memory regression evals + LangGraph consolidation / native retirement

**Lock it in, then retire the dead branch.**

### Scope

Add memory regression cases to the eval harness; flip the default impl to graph;
retire `agent_native.py`.

### File-level changes

**1. New evaluators + multi-turn cases (`evals/run_evals.py`).**

The harness currently runs single-turn `evaluate_turn(input)` (`run_evals.py:355-388`).
Memory needs a **multi-turn** path. Add `evaluate_conversation(messages: list[str])`
to `agent_graph.py` (a thin loop over `evaluate_turn` that threads
`state_doc`/`context` between turns, with `persist=False` but an in-memory
`state_doc`), and new evaluators:

- `enumerates_leads_without_research`: turn 1 does a broad `sayari_search`; turn 2
  ("list those leads with their ids") returns the lead `entity_id`s AND
  `"sayari_search" not in turn-2 tools_used` (it used `recall_state` instead).
- `profile_nth_lead`: turn 2 = "profile the 3rd company you found" resolves to the
  3rd lead's `entity_id` and profiles it (a `sayari_profile`/`sayari_summary` call
  on that exact id).
- `provenance_survives_turns`: a `source_ref` (sanctions_id / sayari_entity_id)
  from turn 1 is present in turn 3's output without re-running the source tool.
- `no_phantom_research`: a follow-up that state can answer shows no redundant
  `sayari_resolve`/`sayari_search` in `tools_used`.

Register them in `EVALUATORS` and add cases to `CASES` (note the file's own
warning that each investigation case burns ~60-90s — keep the multi-turn set
small). These upload to LangSmith via the existing `--push` path unchanged.

**2. Default to graph + retire native.**

- Flip `agent_impl` default to `"graph"` in `config.py:43`.
- Mark `agent_native.run_turn` deprecated; remove it once the graph path has run
  the full eval suite green and a manual demo pass is clean. The facade
  (`agent.py:30-31`) keeps the swap until then. The legacy single-shot
  `/assess` → `agent_native.run_investigation` path (noted in `agent.py:13-14`)
  must be re-pointed or retired explicitly — flag it in the retirement PR.
- Once native is gone, `agent_common.py` stops being a "shared, no-drift" module
  and can fold into `agent_graph.py` / `episodic.py` if desired (optional).

### Acceptance criteria

- All new memory evaluators pass on `agent_graph` (local run + `--push` to
  LangSmith).
- All pre-existing evals still pass.
- `settings.agent_impl` defaults to `"graph"`; a clean manual demo run confirms
  parity (same SSE events, same graph).
- `agent_native.py` either removed or clearly quarantined with no live callers
  besides the explicitly-handled `/assess` path.

### Effort

~1-2 sessions (eval harness multi-turn support is the bulk). No new infra.

---

## Open questions / decisions to confirm

**Resolved 2026-06-03** (questions 1–3 decided with the user; #4 stands on its recommended default):

1. **Embedding model for Upstash Vector (Phase 3).** ✅ **DECIDED: hosted Upstash
   embedding.** Model set on the index at creation; no client embedding code or
   extra dependency. Chosen for lowest effort/fewest moving parts; retrieval-quality
   delta vs. a client-side model is marginal for this app.
2. **Memory scope: single-conversation vs cross-conversation (Phase 3+).** ✅
   **DECIDED: per-conversation now, namespace designed to extend later.** Episodes
   namespaced by `conversation:{id}` with 24h TTL parity to Redis, but the namespace
   key is structured so a future tenant/user-global scope (and a returning analyst's
   cross-session recall) can be added without a rewrite. Cross-session/global memory
   is explicitly deferred until there's a stable user identity.
3. **How much state to inline vs. retrieve (Phases 1→2).** ✅ **DECIDED: minimal
   inline + retrieve on demand.** The always-present `INVESTIGATION STATE` core keeps
   only essentials (resolved primary entity, pinned nodes, lead-set headers/counts);
   the agent calls `recall_state` to pull a full lead list when it actually needs it.
   Keeps steady-state tokens flat as an investigation grows. (Phase 1 may inline a
   slightly fuller block transitionally; Phase 2 trims it to this core.)
4. **Salience scoring approach (Phase 3).** Start with a simple rule-based salience
   (sanctioned/PEP/confirmed-hit → high; greeting/clarify → low) or invest in an
   LLM-scored salience? Rule-based is the cheaper, deterministic default and is
   recommended for v1; revisit only if recall quality is poor.

---

## Tradeoffs & risks

| Dimension | Phase 1 (`state_doc`) | Phase 2 (`recall_state`) | Phase 3 (Vector) | Phase 4 (provenance) |
| --- | --- | --- | --- | --- |
| Tokens | Slightly larger always-on block; net neutral once digest is bounded | Lower steady-state (core only); tool round-trips add tokens on use | Lower steady-state; retrieval round-trips add tokens | Marginally larger structured payloads |
| Credits | None (no model calls; deterministic merges) | None extra | Embedding per turn (cheap) + extra tool calls; optional consolidation LLM | None |
| Latency | +1 Redis read/write per turn (negligible) | Vector-free; in-Redis filter is fast | Vector query latency per `recall_memory`; write→query lag | None |
| Complexity | Low — mirrors `merge_graph`, touches `finalize` only | Low-med — one tool + signature thread | High — new index, namespaces, ranking, eviction, store wiring | Low-med — touches digests + merge |
| Demo risk | Minimal; additive behind `finalize` | Low; read-only tool | Not Friday-safe; consistency + ranking need tuning | Low |

**Key risks:**

- **`conversation_id` injection for the memory tools.** `execute_tool` is currently
  `(name, arguments)` only. Threading `conversation_id` for `recall_state` /
  `recall_memory` without exposing it in the model-visible schema is the one
  cross-cutting change; get it right once in `tools_node` so the model can't spoof
  the conversation it reads from.
- **Eventual consistency (Vector).** A vector written at end of turn N may not be
  queryable at the start of turn N+1. Mitigated by keeping same-conversation exact
  recall in Redis (`state_doc`); Vector is for older/cross-conversation only.
- **Eval cost.** Multi-turn memory cases multiply the ~60-90s/turn cost. Keep the
  memory suite to a handful of conversations.
- **Native/graph drift during transition.** Until Phase 5 retires native, all
  memory lives only on the graph path — `agent_native.py` will silently have worse
  memory. Acceptable because the default flips to graph in Phase 5, but don't demo
  on native after Phase 1.

**If the timeline tightens:** ship **Phase 1 only**. It removes the most likely
on-stage failure (the agent forgetting the leads it just listed) with no new infra
and no consistency surprises — "I think I found some Cyprus shells earlier" becomes
"here are the 14 leads from turn 3, by ID, three of them sanctioned." Phase 2 is the
next-cheapest add (no infra). Defer Phases 3-4 (Vector + lossless compaction) and
Phase 5's native retirement until after the demo.

---

## Sequencing summary

```
Phase 1  state_doc + INVESTIGATION STATE block + dead-code cleanup   ~1-2 sessions, no infra   [HIGHEST ROI]
Phase 2  recall_state tool + trim inline core                        ~1 session,  no infra
Phase 3  Upstash Vector episodic memory + recall_memory via store    ~2-3 sessions, NEW infra + secrets
Phase 4  provenance-preserving compaction                            ~1 session,  no infra
Phase 5  memory regression evals + default-to-graph + retire native  ~1-2 sessions, no infra
```

Phases 1-2 are demo-safe and infra-free. Phase 3 is the only one needing
provisioning. Phases 4-5 harden and lock in.
