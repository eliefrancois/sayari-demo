# Memory & Context Architecture

> How the agent remembers across turns today, where that breaks, what the field
> does in 2025-2026, and a concrete phased plan to make this prod-grade.

This is an analysis + design doc. Nothing here has been built yet. It maps
cleanly onto the existing code (`agent_native.py`, `agent_graph.py`,
`agent_common.py`, `conversations.py`) so the near-term work is an additive
change, not a rewrite.

---

## 1. Current state (audit)

### 1.1 What persists, and where

A conversation's state lives in Upstash Redis under `conversation:{id}:*`
(see `conversations.py`). The memory-relevant keys:

| Key | Type | Written by | Read back into the agent? |
| --- | --- | --- | --- |
| `:context` | string (one digest line per turn, appended) | `set_context` in `finalize` | **Yes** — the primary cross-turn memory |
| `:graph` | JSON `{nodes, edges}`, deduped + accumulated | `merge_graph` in `finalize` | **Yes** — rendered as an entity roster |
| `:summaries` | list of `RiskSummary` dicts | `append_summary` | No (UI hydration only) |
| `:answers` | list of `TurnAnswer` dicts | `append_answer` | No (UI hydration only) |
| `:turns` | list of compact turn metadata | `append_turn` | No (UI hydration only) |
| `:agent_msgs` | JSON of the **last turn's** raw Anthropic messages | `save_agent_messages` | **No — written every turn, never loaded** |
| `:events` | list of SSE events | per emit | No (stream resume only) |
| `:meta` | `{title, turn_count, ...}` | `bump_meta` | turn_index only |

### 1.2 How a turn assembles its prompt

Both implementations build the prompt the same way (the shared code is in
`agent_common.build_context_block` / `build_turn_message`):

1. Read `context` (the digest string) and `graph` from Redis.
2. `build_context_block(context, graph, pinned_node_ids, force_risk_report)`
   produces a single text block:
   - `CONVERSATION CONTEXT (prior turns):` followed by the raw digest string.
   - `KNOWN GRAPH ENTITIES (...)` — up to **30** nodes rendered as
     `- {name} (id={id}) [{label}]`, so the agent reuses node_ids instead of
     re-resolving subjects.
   - `PINNED NODES (...)` if the user pinned any.
   - `force_risk_report: true|false`.
3. The intent router (`intent.classify_intent`) classifies the turn and appends
   a short guidance line to that block.
4. The turn starts a **fresh** message array:
   `[system, user(context_block + "\n---\nUSER MESSAGE (turn N):\n" + msg)]`.
   - Native: `agent_native.run_turn`, ~line 338.
   - Graph: `agent_graph._initial_state`, ~line 555.

So the model never sees the previous turn's message history. It sees a fresh
system prompt, a compact text digest of prior turns, and a 30-entity roster.

### 1.3 How the "compressed context" is actually built

This is the most important and most misunderstood part. The module docstring in
`conversations.py` calls this a "Mem0/SimpleMem-style" episodic pattern. **It is
not.** There is no LLM summarization, no extraction model, no vector store. The
"episodic memory" is a deterministic, template-formatted one-liner per turn,
produced by `agent_common.digest_summary` / `digest_answer`:

- Investigation turn ->
  `Turn N [investigation]: subject=X (id=..., found=...). Top claims: <first 2>. Risk signals: <list>. Confirmed sanctions: <count>.`
- Answer turn ->
  `Turn N [follow-up]: Q='<user msg>'. A=<answer truncated to 220 chars>`
- Clarify turn ->
  `Turn N [clarify]: user said '...'. Agent asked: <questions>`

In `finalize` this digest is appended:
`new_context = (context + "\n" + digest).strip()`. The string is **append-only
and never compacted** (it grows one short line per turn forever, bounded only by
the 24h TTL and how many turns a session runs).

The win: cross-turn memory is dirt cheap. No extra model call to summarize, and
the per-turn block sent to Claude is small (a few hundred tokens of digest plus a
30-line roster). The cost: it is lossy by construction.

### 1.4 Within a turn vs. across turns (don't conflate them)

- **Within a turn**, the full tool-use loop runs. Tool results are re-sent on
  every iteration, which is quadratic, so `slim_result_for_model` strips node
  properties and `slim_sayari_profile` compresses the Sayari risk map before it
  ever reaches the model. This is a real, already-solved token problem, but it is
  about a *single* turn's loop, not long-term memory.
- **Across turns**, none of that survives. The next turn keeps only the digest
  line + the accumulated graph nodes. Every raw `tool_result` from prior turns is
  gone.

### 1.5 What is retained vs. dropped across turns

**Retained:**
- A one-line digest per turn (subject, entity_id, found, first 2 claim texts,
  risk-signal tags, sanctions count; or a 220-char answer snippet).
- The accumulated graph: deduped nodes/edges the agent chose to traverse, capped
  to 30 in the roster (id + name + label only; **properties are dropped from the
  roster**, though they remain in the stored graph JSON).
- Pinned node IDs (only for the turn they're passed on).

**Dropped:**
- All raw tool results.
- **Broad-search lead lists.** `sayari_search` can return ~20 leads, but
  `graph_payload` only adds the pinned top-N subset to the graph; the full lead
  list is never stored as queryable state. The answer digest keeps at most a
  220-char snippet. So a turn after "search for X-linked shell companies", the
  agent cannot enumerate the leads it just found unless they happened to land in
  the graph roster or the truncated snippet.
- **Resolved candidate sets.** `sayari_resolve` / `search_entity` results are
  deliberately kept off the graph (`graph_payload` returns `[], []` for them), so
  the ranked candidates and their entity_ids vanish after the turn.
- **Provenance.** `source_refs`, `risk_factor` names, traversal paths,
  `sayari_record` document URLs, sanctions adjudication verdicts (confirmed vs.
  dismissed) are all in the structured `summaries`/`answers` lists in Redis but
  are **never fed back to the agent**, only to the UI on hydration.
- Claims beyond the first two; the full answer beyond 220 chars.

### 1.6 The dead-code finding

`load_agent_messages` exists in `conversations.py` and `save_agent_messages` is
called at the end of every turn, but **`load_agent_messages` is never called
anywhere in the codebase** (verified by global search). The last turn's raw
messages are serialized to Redis on every turn and never read. This is either a
half-finished feature or vestigial. It costs a Redis write per turn and implies a
continuity that does not exist.

### 1.7 Token / credit implications

- Cross-turn memory is cheap (no summarization model call; small digest + roster).
  This is good and worth preserving.
- The asymmetry is the problem: the **frontend** has richer memory than the
  **agent**. Full `RiskSummary` / `TurnAnswer` objects (with IDs, claims, source
  refs, sanctions hits) are already persisted for hydration but are invisible to
  the model. We are paying to store structured state and then not using it where
  it would help most.

---

## 2. Gaps (ranked by demo impact)

1. **Lead-list / candidate-set amnesia (highest impact).** After a broad search
   or a resolution step, the agent cannot reliably recall the specific entities
   and IDs it just surfaced. Follow-ups like "profile the third one" or "which of
   those were Cyprus-registered?" degrade to re-searching or guessing. This is the
   exact failure called out in the brief and the most likely thing to break on
   stage.
2. **No structured, queryable investigation state.** Everything cross-turn is a
   prose digest. There is no `resolved_entities` map, no `leads` list, no
   sanctions-adjudication ledger the agent can read or query by field.
3. **Provenance loss in compaction.** The digest throws away source_refs, factor
   names, paths, and document URLs, the very things this product sells
   ("trustworthy, sourced provenance"). A later turn can restate a finding but
   can't re-cite it without redoing the work.
4. **No semantic retrieval.** Recall is "whatever is in the last N digest lines +
   the 30-node roster." There is no similarity search, so an older-but-relevant
   episode (e.g. a sanctions call from turn 2 that matters again at turn 9) won't
   resurface unless it's still in the roster.
5. **Unbounded, uncompacted digest string.** Fine for a demo, but it grows
   linearly with turns and has no salience/recency weighting or eviction.
6. **Dead `agent_msgs` path.** Write-only; either wire it up or remove it.

---

## 3. Best practices (2025-2026), with sources

### 3.1 Memory taxonomy

The field has converged on the CoALA framing (Princeton/Stanford, 2023) of four
memory types, now standard across vendors:

- **Working / short-term memory:** the current task state and message window,
  held in-process. In LangGraph this is the graph state + checkpointer.
- **Episodic memory:** time-stamped records of what happened (past interactions,
  actions, outcomes), typically in a vector store for similarity recall.
- **Semantic memory:** facts about the world/user/entities, often a
  relational/structured store or knowledge graph, retrieved by entity identity.
- **Procedural memory:** reusable skills/rules/policies, usually baked into the
  prompt, code, or model weights.

Sources:
- Synthara, "Agent Memory Architectures: Working, Episodic, Semantic, Procedural" — https://www.syntharatechnologies.com/blog/agent-memory-architectures
- Atlan, "Types of AI Agent Memory" (CoALA framing) — https://atlan.com/know/types-of-ai-agent-memory/
- CalibreOS, "Agent Memory Systems" — https://www.calibreos.com/learn/genai-memory-systems
- MachineLearningMastery, "Beyond Short-term Memory: The 3 Types of Long-term Memory" — https://machinelearningmastery.com/beyond-short-term-memory-the-3-types-of-long-term-memory-ai-agents-need/
- MIRIX (six-component memory: core, episodic, semantic, procedural, resource, knowledge vault) — https://arxiv.org/html/2507.07957v1

Key reusable principle from Synthara: a **projection function** on tool output.
"Every tool output passes through a projection function that extracts only what
the next step needs." This app already does the intra-turn version of this
(`slim_result_for_model`); the gap is there's no cross-turn projection into
durable state.

### 3.2 Summarization / compaction strategies

- **Rolling summaries** (replace old turns with a running summary) are the most
  common production technique. Anthropic now offers this server-side as
  **compaction** and client-side via SDK helpers.
- **Recency + salience scoring** (from the Generative Agents paper, Park et al.
  2023) beats pure recency or pure relevance. CalibreOS: "pure semantic retrieval
  misses important recent events; pure recency misses highly relevant older
  memories." A weighted combination (recency * importance * relevance) approximates
  human recall.
- **Tradeoff:** every compaction step is lossy. The guidance is to compact text
  but keep structured identifiers (IDs, entities) outside the lossy summary, in a
  store you can query exactly.

Sources:
- Anthropic, "Effective context engineering for AI agents" — https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Anthropic, "Context editing" (compaction + `clear_tool_uses_20250919`) — https://platform.claude.com/docs/en/build-with-claude/context-editing
- CalibreOS (recency+salience formula) — https://www.calibreos.com/learn/genai-memory-systems

### 3.3 Retrieval-augmented memory

**Mem0** (arXiv 2504.19413) is the reference architecture. Two phases:
- **Extraction:** an LLM distills salient facts from the conversation (using a
  rolling summary + a recency window of recent messages).
- **Update:** for each fact, retrieve the top-k semantically similar existing
  memories and let the LLM choose `ADD` / `UPDATE` / `DELETE` / `NOOP`. The
  managed product is **ADD-only** (nothing overwritten; old and new facts both
  survive to preserve temporal context).
- **Storage is three-tier:** a vector DB (semantic), an entity store (entity-based
  boost), and a SQL/history log (audit + dedup). Retrieval fuses semantic +
  BM25 keyword + entity matching.

Sources:
- Mem0 paper — https://arxiv.org/html/2504.19413
- Mem0 memory-evaluation docs (ADD-only, three-tier, hybrid retrieval) — https://docs.mem0.ai/core-concepts/memory-evaluation
- Mem0 AI memory layer guide ("start with vector search; add graph only when you need explicit relationships") — https://mem0.ai/blog/ai-memory-layer-guide

**LangGraph / LangMem.** LangGraph separates the **checkpointer** (short-term,
thread-scoped state, e.g. our per-turn message history) from the **`BaseStore`**
(long-term, cross-thread, namespaced, optional semantic index). LangMem adds
agent-facing `create_manage_memory_tool` / `create_search_memory_tool` so the
agent reads/writes memory "in the hot path", plus a background manager that
extracts and consolidates. Stores support `index={"dims": ..., "embed": ...}` for
semantic search via `store.asearch(namespace, query=..., limit=...)`.

Sources:
- LangMem SDK launch — https://www.langchain.com/blog/langmem-sdk-launch
- LangMem repo — https://github.com/langchain-ai/langmem
- LangGraph memory docs (checkpointer vs. store, semantic search) — https://docs.langchain.com/oss/python/langgraph/add-memory

**Letta / MemGPT.** Memory is split into **core memory blocks** (always in
context, labeled, size-limited, agent-editable via `memory_replace` /
`memory_insert`) and **archival memory** (out-of-context vector DB, retrieved
only via explicit `archival_memory_search`). Guidance: keep core memory under
~80% of the window; one block per functional unit; archive/consolidate on
overflow. This is the cleanest mental model for "what's always present vs. what's
fetched on demand."

Sources:
- Letta core memory / memory blocks — https://docs.letta.com/guides/core-concepts/memory/memory-blocks/
- Letta "Memory Blocks: The Key to Agentic Context Management" — https://www.letta.com/blog/memory-blocks

**Anthropic-native.** Two relevant primitives:
- **Memory tool** (public beta, Sonnet 4.5 launch): a file-based `/memories`
  store the model checks before tasks and writes notes to. This is "structured
  note-taking / agentic memory."
- **Context editing** (`context-management-2025-06-27` beta header): server-side
  `clear_tool_uses_20250919` clears old tool results past a token threshold while
  keeping recent ones; compaction summarizes the whole conversation server-side.
  Anthropic reports a 39% improvement on agentic search and an 84% token reduction
  on a 100-turn web-search eval when memory + context editing are combined
  (reported via the Claude memory cookbook; summarized at
  https://thomas-wiegold.com/blog/claude-api-memory-tool-guide/).

Caveat for this app: context editing operates on one long, growing message array.
Our app **rebuilds the message array fresh each turn**, so context editing is a
weaker fit than the memory tool / structured note-taking pattern, which maps
directly onto "persist structured artifacts and re-inject them."

Sources:
- Anthropic memory tool docs — https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool
- Anthropic context engineering (just-in-time retrieval, structured note-taking) — https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents

### 3.4 Patterns specific to tool-using investigative agents

The consistent recommendation across Synthara, Anthropic, and Mem0: **persist
structured tool outputs as queryable state, don't replay raw tool_results.**
- Keep lightweight identifiers (entity IDs, stored-query handles, record IDs) and
  re-load detail on demand ("just-in-time" retrieval, per Anthropic). Claude Code
  does exactly this over large datasets.
- Maintain an explicit **provenance trail**: each memory record carries
  `source`, `created_at`, `confidence`, and supersession links (CalibreOS:
  "this is how memory becomes a governed subsystem instead of an unbounded
  append-only hallucination amplifier").
- Make writes **deliberate**, not automatic logging of everything (Synthara), to
  avoid noisy low-confidence memory.

This is a near-perfect description of what an entity-investigation agent needs:
resolved entity IDs, lead lists, and sanctions verdicts are exactly the
"lightweight identifiers" that should live in queryable state with provenance.

### 3.5 Practical guidance for an Upstash-Redis app

- Redis stays the home of **hot, exact-recall state** (the investigation state
  doc, the graph). It's already wired up and has no eventual-consistency lag.
- **Upstash Vector** exists and fits episodic/semantic recall when you want it:
  serverless, REST, namespaces, metadata filtering with SQL-like syntax
  (`filter="country = 'CY' AND sanctioned = true"`), and **hybrid indexes** (dense
  + sparse, fused with RRF or DBSF) for semantic + keyword recall in one call.
- Watch the **eventual-consistency** note: "there may be a delay before newly
  inserted vectors are ready for querying." So a vector you write at the end of
  turn N may not be queryable at the very start of turn N+1. Keep same-conversation
  recall in Redis (immediate); use Vector for older or cross-conversation recall.

Sources:
- Upstash Vector getting started — https://upstash.com/docs/vector/overall/getstarted
- Upstash Vector hybrid indexes (RRF/DBSF fusion) — https://upstash.com/docs/vector/features/hybridindexes
- Upstash Vector query + metadata filter — https://upstash.com/docs/vector/api/endpoints/query

---

## 4. Proposed architecture

Design rule borrowed from Letta: be explicit about what is **always in context**
(cheap, structured, small) vs. **fetched on demand** (richer, retrieved). The
current app has only the always-in-context tier, and it's prose. The plan adds a
structured always-in-context tier first (near-term), then an on-demand retrieval
tier (mid-term).

### 4.1 Near-term (demo-grade, low effort, no new infra)

**Goal:** kill the lead-list amnesia and preserve IDs/provenance, using only
Redis. This is the Friday-demo-safe change.

**Add one key: a structured investigation-state document.**

```
conversation:{id}:state_doc   ->  JSON (SET, refreshed TTL, merged each turn)
```

Shape:

```jsonc
{
  "resolved_entities": {
    // keyed by a normalized subject string the agent searched
    "gazprom": {
      "entity_id": "ABC123",
      "label": "Public Joint Stock Company Gazprom",
      "type": "company",
      "source": "sayari",
      "sanctioned": true,
      "first_seen_turn": 1
    }
  },
  "leads": [
    // the full broad-search lead lists, NOT just the pinned top-N
    {
      "entity_id": "E1", "label": "Acme Trading Ltd", "type": "company",
      "countries": ["CYP"], "sanctioned": false, "pep": false,
      "top_risk": ["shell_company_pattern"],
      "from_turn": 3, "from_query": "Gazprom-linked Cyprus shells",
      "pinned_to_graph": true
    }
    // ... up to ~40 leads, capped
  ],
  "sanctions_adjudicated": [
    {
      "sanctions_id": "ofac-30947", "name": "...", "lists": ["OFAC SDN"],
      "verdict": "confirmed",         // confirmed | dismissed (name collision)
      "from_turn": 2
    }
  ],
  "pinned_node_ids": ["ABC123", "E1"],
  "turn_log": [
    { "turn": 1, "intent": "profile_entity", "subject": "Gazprom", "kind": "investigation" }
  ]
}
```

**Where it plugs in (read-only audit; no code written yet):**

1. `conversations.py`: add `get_state_doc(id)` / `merge_state_doc(id, delta)`
   mirroring the existing `merge_graph` pattern (read-modify-write JSON, refresh
   TTL). Same shape of code as `merge_graph`, ~30 lines.
2. `finalize` (both `agent_native.run_turn` tail ~line 534-565 and
   `agent_graph.finalize_node` ~line 466-497): build a `delta` from the turn's
   structured result. The data is **already in hand**: the `RiskSummary` /
   `TurnAnswer`, the `turn_nodes`, the `raw_strong_hits`, and the build of the
   sanctions review (which already computes confirmed vs. dismissed in
   `build_sanctions_review`). Capture the broad-search leads by parsing the
   `sayari_search` tool result in the tools loop (the leads are already parsed
   there for the SSE event), stashing them on turn state, and folding them into
   the delta at finalize.
3. `agent_common.build_context_block`: render a compact, ID-rich
   `INVESTIGATION STATE` section from `state_doc` ahead of the existing prose
   digest. Example rendering:

```
INVESTIGATION STATE (structured, reuse these IDs):
Resolved: Gazprom=ABC123 (company, SANCTIONED); Sberbank=DEF456 (company)
Open leads from turn 3 ("Gazprom-linked Cyprus shells"):
  E1 Acme Trading Ltd (CYP, shell_company_pattern) [pinned]
  E2 Beta Holdings Ltd (CYP) [not pinned]
  ... 12 more leads available; ask to expand or profile by id.
Sanctions adjudicated: ofac-30947 -> confirmed (OFAC SDN); ofac-91002 -> dismissed (name collision)
```

**Compaction that preserves provenance.** Keep the prose digest, but stop
relying on it for IDs. The structured state holds IDs/leads/verdicts exactly; the
digest stays for narrative continuity. Optionally bound the digest (keep the last
~15 lines verbatim, roll older ones into a single "earlier in this investigation:
..." line) so it can't grow without limit. No LLM call needed.

**Also:** either wire `load_agent_messages` into the prompt (re-inject the
immediately prior turn's messages for tight follow-ups) or delete it. Recommend
**delete** for the demo: the structured state is a better continuity mechanism
than replaying one stale turn's raw messages, and it removes a write per turn.

**Effort:** ~80-120 lines across `conversations.py`, `agent_common.py`, and the
two finalize tails. No new dependency, no new infra, no eventual-consistency
risk. Touches `finalize` only, so the SSE contract and the React Flow graph are
unaffected.

### 4.2 Mid-term (prod-grade direction)

Split memory along the standard taxonomy and add an on-demand retrieval tier.

**Semantic memory = the structured state, made queryable via a tool.** Promote
the near-term `state_doc` into the source of truth for entity facts and expose it
to the agent as a tool instead of always dumping it into the prompt:

```
query_investigation_state(filter) ->
  e.g. filter="leads where country='CYP' and not pinned"
       filter="resolved_entities where sanctioned=true"
```

This is the Letta "archival/on-demand" split: keep a small always-in-context
core (resolved primary subject + pinned IDs + last lead-set header) and let the
agent pull the rest by tool call. Retrieval rule for semantic memory is
**by entity/field**, not similarity (per Synthara: "a join, not a vector search").

**Episodic memory = Upstash Vector.** At finalize, embed a per-turn episode
record (the digest + key claims + entity IDs) and upsert into Upstash Vector,
namespaced by conversation (and later by tenant/user for cross-conversation
recall):

```
namespace = conversation:{id}            # mid-term: also a global/user namespace
id        = {conversation_id}:{turn}
metadata  = { turn, intent, entity_ids:[...], sanctioned:bool,
              countries:[...], created_at, source_refs:[...] }
data/text = the digest + claim texts (for embedding + display)
```

Expose `recall_memory(query, filter)` (LangMem/Letta `search_memory` pattern) so
the agent retrieves older relevant episodes just-in-time. Use a **hybrid index**
(dense + sparse, RRF fusion) so "OFAC" keyword hits and semantic matches both
surface, and use metadata filters (`sanctioned = true`, `country = 'CY'`) to
scope. Apply a recency + salience weighting on top of the vector score (per
CalibreOS) rather than raw similarity.

Keep same-conversation exact recall in Redis (immediate, no consistency lag);
use Vector for "earlier in a long investigation" and (later) cross-conversation
recall. Mem0's ADD-only stance is the safe default: don't overwrite episodes;
mark superseded facts rather than deleting, to keep the provenance trail.

**Consolidation (optional, background).** A periodic job promotes repeated
episodic observations into the semantic `state_doc` (e.g. an entity profiled
across three turns collapses to one resolved-entity record with the latest risk
posture), dedupes near-identical leads, and applies eviction by recency *
importance. This is the Mem0 update phase and the "consolidation loop" all the
sources recommend.

**How it fits the existing LangGraph variant (`agent_graph.py`).** This is the
clean part:
- The `state_doc` retrieval becomes a **memory node** before `agent_node` (load
  the always-in-context core into state), or the `query_investigation_state` /
  `recall_memory` tools are bound alongside the investigation tools (the intent
  router already narrows the tool set, so add them to the relevant intents, e.g.
  `broad_search` and `conversational_followup`).
- Episodic write happens in `finalize_node`, next to the existing
  `set_context` / `merge_graph` calls.
- If you adopt LangGraph's `BaseStore`, back it with Upstash Vector and let
  `store.asearch(namespace, query=...)` drive `recall_memory`. The checkpointer
  stays the per-turn working memory; the store is the long-term tier. This is the
  textbook LangGraph split and a strong talking point for the "what does LangGraph
  buy you" question the architecture doc already anticipates.

---

## 5. Tradeoffs

| Dimension | Near-term (Redis state_doc) | Mid-term (state tool + Upstash Vector) |
| --- | --- | --- |
| Tokens | Slightly larger always-on block (structured state). Net neutral if the digest is bounded. | Lower steady-state context (core only); detail pulled on demand. But tool-call round-trips add tokens per retrieval. |
| Credits | None extra (no model calls; digests are string formatting). | Embedding cost per turn (cheap); optional consolidation LLM calls; extra agent tool calls. |
| Latency | None added (one more Redis read/write per turn). | Vector query latency per `recall_memory`; eventual-consistency lag on just-written episodes. |
| Complexity | Low. One key, mirrors `merge_graph`. Touches `finalize` only. | Higher. New index, namespaces, retrieval ranking, optional background job, a new tool surface. |
| Recall quality | Fixes lead-list / ID / provenance gaps exactly within a conversation. | Adds cross-turn semantic recall and (later) cross-conversation memory. |
| Risk to demo | Minimal; additive, behind `finalize`. | Eventual-consistency and ranking quality need tuning; not Friday-safe. |

**Demo deadline vs. real product:** for Friday, ship only the near-term
`state_doc`. It directly removes the most likely on-stage failure (the agent
forgetting the leads it just listed) with no new infra and no consistency
surprises. For a real product, layer in the on-demand state tool + Upstash Vector
episodic memory + a consolidation loop, and adopt the LangGraph `BaseStore` so the
store/checkpointer split is explicit.

---

## 6. Recommended next step (Friday demo)

Build the near-term **`conversation:{id}:state_doc`** (Section 4.1) and render it
as an `INVESTIGATION STATE` block in `build_context_block`, ahead of the existing
prose digest. Concretely:

1. `conversations.py`: add `get_state_doc` / `merge_state_doc` (copy the
   `merge_graph` pattern).
2. In both finalize paths, build a delta from data already in hand (the
   `RiskSummary`/`TurnAnswer`, `turn_nodes`, `raw_strong_hits`, the sanctions
   review's confirmed/dismissed split) plus the full `sayari_search` lead lists
   captured in the tools loop, and call `merge_state_doc`.
3. `agent_common.build_context_block`: render resolved entities (name -> id),
   the most recent lead set with IDs and pinned flags, and sanctions verdicts.
4. Delete the unused `save_agent_messages` / `load_agent_messages` path (or wire
   it in), and bound the prose digest to its last ~15 lines.

This is ~1-2 hours of additive work, keeps the SSE/graph contract intact, and
turns "I think I found some Cyprus shells earlier" into "here are the 14 leads
from turn 3, by ID, three of them sanctioned."

---

## Sources

- Anthropic, Effective context engineering for AI agents — https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Anthropic, Context editing (compaction, clear_tool_uses) — https://platform.claude.com/docs/en/build-with-claude/context-editing
- Anthropic, Memory tool — https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool
- Anthropic memory + context-editing eval numbers (summarized) — https://thomas-wiegold.com/blog/claude-api-memory-tool-guide/
- Mem0 paper (extraction/update, three-tier) — https://arxiv.org/html/2504.19413
- Mem0 memory evaluation docs (ADD-only, hybrid retrieval) — https://docs.mem0.ai/core-concepts/memory-evaluation
- Mem0 AI memory layer guide — https://mem0.ai/blog/ai-memory-layer-guide
- LangMem SDK launch — https://www.langchain.com/blog/langmem-sdk-launch
- LangMem repo — https://github.com/langchain-ai/langmem
- LangGraph memory docs (checkpointer vs. store, semantic search) — https://docs.langchain.com/oss/python/langgraph/add-memory
- Letta memory blocks (core memory) — https://docs.letta.com/guides/core-concepts/memory/memory-blocks/
- Letta, Memory Blocks: The Key to Agentic Context Management — https://www.letta.com/blog/memory-blocks
- Synthara, Agent Memory Architectures — https://www.syntharatechnologies.com/blog/agent-memory-architectures
- Atlan, Types of AI Agent Memory (CoALA) — https://atlan.com/know/types-of-ai-agent-memory/
- CalibreOS, Agent Memory Systems (recency+salience) — https://www.calibreos.com/learn/genai-memory-systems
- MachineLearningMastery, 3 Types of Long-term Memory — https://machinelearningmastery.com/beyond-short-term-memory-the-3-types-of-long-term-memory-ai-agents-need/
- MIRIX, Multi-Agent Memory System — https://arxiv.org/html/2507.07957v1
- Upstash Vector getting started — https://upstash.com/docs/vector/overall/getstarted
- Upstash Vector hybrid indexes — https://upstash.com/docs/vector/features/hybridindexes
- Upstash Vector query + metadata filter — https://upstash.com/docs/vector/api/endpoints/query
