# Investigation Memory Subsystem (IMS)

> The recall failures in this demo are a WRITE-PATH bug, not a retrieval bug. A
> bigger context window or a vector store would not fix them. This doc is the
> consolidated, authoritative memory design for the Entity Risk Resolver: it
> reframes the failure, distills the principles, specifies the three-tier
> architecture with explicit read/write contracts, reconciles the prior memory
> docs (06 audit, 07 phased plan, 08 registry), and lays out the phased path. It
> verifies against the code as it stands today, not against a clean slate.

This is an analysis + design doc. Some of it is built already (the structured
`state_doc`, the `recall_state` tool, the minimal-inline `INVESTIGATION STATE`
block, the bounded prose digest, and the `named_ids` resolve cache all ship
today). The rest, the parts that actually fix fidelity, are the forward plan in
§9. It maps additively onto `agent_graph.py`, `agent_common.py`,
`conversations.py`, and `tools.py`; it is not a rewrite.

Docs 06/07/08 are not retired. 06 stays the audit and the best-practices
research with sources. 07 stays the original phased build plan. 08 stays the
entity-registry contract. This doc, 09, is the single place that says what the
target architecture IS and what order to build the remaining work in. Where 09
disagrees with 06/07, 09 wins (see §8).

---

## 1. The reframe: this is a write-path bug

Start here, because it changes what you build.

The agent forgets things it already found. The instinct is "give it more
context" or "add vector memory so it can search its history." Both are wrong for
this failure. The information never reaches durable state in the first place, so
there is nothing for a larger window or a vector index to retrieve. You cannot
read back a fact you never wrote.

### 1.1 The Rosneft case, concretely

Walk a real multi-turn trace:

- **Turn 1 (investigation).** The user asks about Rosneft. The agent resolves
  it, profiles it, runs `check_sanctions`, and in its prose answer names two
  sanctioned subsidiaries, `Rosneft Trading S.A.` and `Rosneft Trade Limited`.
  Those names live in the narrative. They were surfaced through
  `check_sanctions` and then either dismissed (treated as a name collision /
  not a strong match) or simply never written into the structured
  `sanctions_hits` the agent kept. Meanwhile a graph-pinned node like
  `Saratov Refinery`, which the agent traversed, lands in the accumulated graph.
- **Turns 2-3 (follow-up).** The user asks "which of those were sanctioned
  again?" The intent router labels this `conversational_followup`, the agent
  answers from memory, and it runs **0 tool calls**. Now look at what memory
  actually carries forward: the graph-traversed `Saratov Refinery` is in the
  30-node roster, but `Rosneft Trading S.A.` and `Rosneft Trade Limited` have
  vanished. They were never durable. Durable state only persists graph-traversed
  nodes (`turn_nodes` -> `merge_graph`), the full `sayari_search` lead lists
  (`turn_leads`), and adjudicated strong sanctions rows
  (`build_sanctions_review` over `raw_strong_hits`). A subsidiary named in prose
  but not landed in any of those buckets is gone by turn 2.

The model on turn 2 is not failing to retrieve. It is being asked to recall
something the write path threw away on turn 1.

### 1.2 The two confirmed defects

1. **Fidelity drift (narrow deposit path).** The cross-turn write path in
   `agent_graph._build_state_delta` deposits from a deliberately small set of
   sources: traversed `turn_nodes`, the summary's primary subject, `turn_leads`,
   and `build_sanctions_review(summary, raw_strong_hits)`. It does **not**
   deposit the answer's `referenced_node_ids`, the `claims[]` and their
   `source_refs`, the `sayari_risk_factors` paths, or any sanctions hit that was
   surfaced and dismissed unless it was a strong match on an investigation turn.
   Entities the agent clearly knew about (it named them, with ids, in the
   answer) silently fail to persist. This is the same disease as the "Other" /
   "Unresolved entity" graph blobs in doc 08: information arrives down the text
   pipe and never reaches the structured store.

2. **Context-stuffing smell (per-turn injection that scales with size).** Every
   turn injects a prose `CONVERSATION CONTEXT` digest plus a roster of up to 30
   graph nodes plus the `INVESTIGATION STATE` block, and several of those slices
   grow with the investigation and silently truncate (the roster caps at 30 with
   a "...and N more" line; the resolved list caps at 10; lead-set headers cap at
   3). The injected block gets bigger as the case gets bigger, which is exactly
   the "Context Stuffing" anti-pattern the guide flags (`PATTERNS.md`:
   "Context Stuffing | Token waste | Retrieve relevant only"). It is also why a
   bigger window does not help: stuffing more in is the problem, not the cure.

Both defects point the same way. Fix the write path so structured facts are
durable, and fix the read path so the injected core stays small and the agent
pulls detail on demand.

---

## 2. Principles from the guide (what is actually actionable)

The `ai-system-design-guide` is the reference. The relevant pages and what each
contributes:

- **`07-agentic-systems/05-agent-memory-and-state.md` (primary).** The four-tier
  table (L1 working, L2 episodic, L3 semantic, L4 procedural) with per-tier
  write patterns and query semantics. Two rules matter most here:
  - **Provenance at write time.** "Every memory carries `source`
    (user-stated, model-inferred, tool-output), `timestamp`, and `trust_tier`."
    Our ranking for this app: `tool_output` (parsed tool JSON) is most
    trustworthy, `model_structured` (a validated `submit_answer` / `submit_summary`
    field) is next, and prose narration is **never** a write source.
  - **Never store fast-moving facts that have a live source.** The guide is
    explicit: "Fast-moving facts with a live source of truth should never enter
    memory. They become stale by definition." Live sanctions status and
    ownership are exactly this. Cache the **handle** (entity_id, sanctions_id,
    record_id) and re-fetch the value, do not freeze the value.
- **`08-memory-and-state/01-memory-architectures.md` (three-tier rationale).**
  The L1/L2/L3 cognitive hierarchy: L1 working = context window / KV cache
  (<50ms), L2 episodic = vector DB / local graph (100-300ms), L3 semantic =
  graph / SQL / Mem0 (>500ms). Memories move between tiers by **consolidation**,
  done at boundaries, not continuously.
- **`08-memory-and-state/02-short-term-context.md` (application context window
  << model limit).** "The Model Context Window is the hard limit; the
  Application Context Window is a configuration the engineer sets to manage
  latency and cost." You do not use the whole window every turn. Sliding window
  keeps recent fidelity but gets the "Dory" effect (forgets the start);
  summarization fights that but is lossy. The answer is hybrid: keep a small
  exact core, summarize the narrative, and put exact identifiers in a queryable
  store outside the lossy summary.
- **`06-retrieval-systems/08-agentic-rag.md` (just-in-time retrieval, token
  budget).** Agentic RAG is a reasoning loop that decides *when* and *what* to
  retrieve. "Token-Budgeting: allow the agent only 3-5 turns before forcing a
  final answer." Retrieval is a tool the agent calls when it needs a row, not a
  dump injected up front.
- **`PATTERNS.md` / `15-ai-design-patterns/02-anti-patterns.md`
  (context-stuffing anti-pattern).** "Context Stuffing -> Token waste -> Retrieve
  relevant only." Injecting everything every turn is the named anti-pattern this
  doc is fighting.

Distilled to what to do:

| Principle | What it means for IMS |
| --- | --- |
| Three memory tiers | L1 working (LangGraph turn state), L2 episodic (vector, later), L3 semantic (Redis `state_doc`, the exact SSOT). |
| Don't use the window as long-term memory | The injected block is a pointer to state, not the state. |
| Fixed per-turn memory budget | Injected core held to a fixed ~300-500 token budget regardless of investigation size. |
| Just-in-time / tool-based retrieval | `recall_state` (exact, now) and `recall_memory` (semantic, later) pull rows on demand. |
| Structured writes with provenance | Deposit only from `tool_output` and `model_structured`. Never NLP-parse prose. |
| Fast-moving facts: handle, not value | Cache entity_id / sanctions_id / record_id and re-fetch live status. |
| Letta/MemGPT core + archival paging | Small always-in-context core; everything else paged in by tool. |
| Recency x importance x relevance | Episodic ranking (Park et al. weighted), not raw similarity. |
| Scheduled, not per-turn, consolidation | No LLM summarization into Redis on every turn (write amplification). |

---

## 3. Target architecture: three tiers with explicit contracts

Three tiers, each with a clear read contract and write contract. The whole point
of being explicit is so nobody re-introduces "just inject it" later.

### 3.1 L1 WORKING (LangGraph `TurnState`)

- **What.** The current turn's message array plus the in-turn accumulators on
  `agent_graph.TurnState` (`turn_nodes`, `turn_edges`, `turn_leads`,
  `raw_strong_hits`, `tools_used`). Slim tool results
  (`slim_result_for_model`) keep the re-sent loop small.
- **Read contract.** The model sees the system prompt, the injected memory
  **core** (from L3, see §6), and this turn's tool results. Nothing else.
- **Write contract.** Lives for one turn. Nothing here is durable until
  `finalize_node` projects it into L3.
- **Budget.** The injected prompt core is held to a FIXED ~300-500 token budget.
  It does not grow with investigation size.

### 3.2 L3 SEMANTIC (Redis `state_doc`)

- **What.** The exact, immediate single source of truth, in Upstash Redis under
  `conversation:{id}:state_doc`. No eventual-consistency lag, so same-conversation
  recall is always correct. Target buckets: `entities{id -> identity}`,
  `lead_sets`, `sanctions_ledger`, `claims`, `pinned_node_ids`, `turn_log`
  (shape in §5).
- **Read contract.** Two ways in: the injected core renders a tiny navigation
  summary; the `recall_state` tool returns exact rows on demand.
- **Write contract.** `merge_state_doc` does a deterministic read-modify-write
  per turn from `tool_output` and `model_structured` only. Upsert with conflict
  resolution at write (richer source wins, per doc 08's merge policy). The
  `sanctions_ledger` is append-only.
- **Provenance.** Every record carries `source` and `confidence`
  (`tool_output` > `model_structured`), and entity rows carry `source_refs`.

### 3.3 L2 EPISODIC (Upstash Vector, later phase)

- **What.** Per-turn episode records in Upstash Vector, namespaced by
  conversation, for "earlier in a long investigation" recall.
- **Read contract.** A `recall_memory` tool, ranked by similarity x recency x
  salience (not raw similarity), with metadata filters (`sanctioned`, `country`).
- **Write contract.** ADD-only at `finalize_node`, behind a feature flag.
  Eventual-consistency lag means it is for OLD episodes only; same-conversation
  exact recall stays in L3/Redis.
- **Status.** Not built. This is the new-infra phase (D in §9), and it is
  deliberately after the write-path fix, because vector memory of garbage is
  still garbage.

---

## 4. Write path: one unified projection, structured-only

The fix for fidelity drift is a single, deterministic projection from the turn's
structured outputs into L3. No new model calls.

### 4.1 The function

A single `project_turn_to_memory(state, summary | answer) -> MemoryDelta`,
called from `finalize_node` (it replaces and generalizes today's
`_build_state_delta`). It composes deposits from every structured source the
turn produced:

- **Every tool result captured in `tools_node`:**
  - `sayari_search` candidates -> `lead_sets` (already captured as `turn_leads`).
  - ALL strong `check_sanctions` hits, **including dismissed ones** ->
    `sanctions_ledger` (this is the Rosneft fix, see §4.3).
  - ownership / watchlist neighbor ids and their flags -> `entities` registry.
- **Structured `RiskSummary` / `TurnAnswer` fields:**
  - `referenced_node_ids` -> `entities` (the ids the agent leaned on).
  - `claims[]` with their `source_refs` -> `claims` (with `entity_ids` resolved
    from the refs).
  - `sanctions_hits` -> `sanctions_ledger` (verdict `confirmed`).
  - `sayari_risk_factors` with their traversal `path` -> entity provenance.
- **`raw_strong_hits` on BOTH investigation and answer turns.** Today
  `_build_state_delta` only runs `build_sanctions_review` when `summary is not
  None` (investigation turns); answer turns fall back to `answer.sanctions_hits`,
  which by construction holds only kept/confirmed hits. The dismissed hits on an
  answer turn are dropped. Project both.
- **Risk-path resolver `named_ids`.** The bounded resolver in
  `tools._resolve_and_map_risk_paths` already deposits `named_ids` mid-turn (the
  doc 08 registry embryo). Fold those into `entities` (see §5 migration).

### 4.2 The critical rule

> Deposit only from STRUCTURED outputs: parsed tool JSON (`tool_output`) and the
> validated `submit_answer` / `submit_summary` schema (`model_structured`).
> NEVER NLP-parse the prose `answer` string to extract entities or facts.

This is the HaluMem / hallucinated-write trap. If you scrape the narrative for
"entities the agent mentioned," you write the model's hallucinations and name
collisions straight into durable memory, and every later turn treats them as
fact. The guide's provenance rule exists for exactly this: prose is not a write
source. The agent already emits everything we need in structured form
(`referenced_node_ids`, `claims`, `source_refs`, `sanctions_hits`,
`sayari_risk_factors`); project from those, never from the prose.

### 4.3 How this fixes the Rosneft case

On turn 1, `Rosneft Trading S.A.` came through `check_sanctions` as a strong
match and was then dismissed (name-collision discipline). Today that dismissal
means the row is computed by `build_sanctions_review` (it appears in
`review["dismissed"]`) and, on an investigation turn, it IS already deposited as
a `dismissed` row by `_build_state_delta`. The gaps that still bite:

- If the subsidiary was surfaced on an **answer** turn, the dismissed row is
  dropped (the `summary is None` branch only reads `answer.sanctions_hits`).
- If the agent named the subsidiary with an id in `referenced_node_ids` or a
  `claim.source_ref` but it was never a strong `check_sanctions` hit, it is not
  deposited anywhere.

The unified projection deposits the dismissed hit as a `sanctions_ledger` row
with `verdict: dismissed` on either turn type, and deposits the
`referenced_node_ids` / claim entities into `entities`. Then turn 2's
`recall_state(kind="sanctions")` or `recall_state(kind="entities")` returns
`Rosneft Trading S.A.` exactly, with no re-run of `check_sanctions`. The name
stops vanishing because it stops being prose-only.

---

## 5. `state_doc` shape evolution

Today's `state_doc` (`conversations._empty_state_doc`) has
`resolved_entities` (name-keyed), `leads`, `sanctions_adjudicated`,
`pinned_node_ids`, `turn_log`, and `named_ids` (id-keyed resolve cache). The
target consolidates the two entity buckets into one id-keyed registry and adds
structured `claims`.

```jsonc
{
  // L3 SEMANTIC. The id-keyed canonical registry. Folds today's
  // resolved_entities (name-keyed) + named_ids (id-keyed) into ONE store,
  // matching doc 08's "one id-to-identity dictionary everything reads/writes."
  "entities": {
    "abc123": {
      "label": "Rosneft Trading S.A.",
      "type": "company",
      "sanctioned": true,
      "pep": false,
      "countries": ["CHE"],
      "source": "check_sanctions",     // which tool/field named it
      "confidence": "tool_output",     // tool_output > model_structured; never prose
      "first_seen_turn": 1,
      "last_seen_turn": 1,
      "source_refs": [
        { "source": "opensanctions", "sanctions_id": "ofac-12345" }
      ]
    }
  },

  // Lead lists grouped by the search that produced them (from_turn + from_query),
  // not a flat list. Keeps "the leads from turn 3" answerable as a unit.
  "lead_sets": [
    {
      "from_turn": 3,
      "from_query": "Rosneft-linked trading companies",
      "leads": [
        {
          "entity_id": "E1", "label": "Acme Trading Ltd", "type": "company",
          "countries": ["CYP"], "sanctioned": false, "pep": false,
          "top_risk": ["shell_company_pattern"], "pinned_to_graph": true
        }
        // ... capped by recency so context can't bloat
      ]
    }
  ],

  // ALL strong sanctions hits, confirmed AND dismissed. Append-only, never
  // delete. Dedupe by sanctions_id. This is the bucket that retains the
  // dismissed subsidiary so it survives to turn 2.
  "sanctions_ledger": [
    {
      "sanctions_id": "ofac-30947",
      "matched_name": "Rosneft Trading S.A.",
      "lists": ["OFAC SDN"],
      "verdict": "dismissed",          // confirmed | dismissed
      "from_turn": 1,
      "entity_ids": ["abc123"]
    }
  ],

  // Structured claims with provenance + the entities they reference.
  "claims": [
    {
      "text": "Rosneft Trading S.A. is a sanctioned subsidiary.",
      "confidence": "high",
      "source_refs": [{ "source": "sayari", "sayari_entity_id": "abc123" }],
      "entity_ids": ["abc123"],
      "from_turn": 1
    }
  ],

  "pinned_node_ids": ["abc123"],

  "turn_log": [
    { "turn": 1, "intent": "profile_entity", "subject": "Rosneft", "kind": "investigation" }
  ]
}
```

**Migration.** Fold `resolved_entities` + `named_ids` into `entities`
(id-keyed). Today `resolved_entities` is keyed by normalized subject string and
`named_ids` is keyed by entity id; the target is one id-keyed store, which also
fixes doc 08's "two pipes, one missing source of truth." `leads` becomes
`lead_sets` (grouped). `sanctions_adjudicated` becomes `sanctions_ledger`
(append-only). Keep backward-compatible readers in `get_state_doc`: it already
defaults missing keys from `_empty_state_doc`, so an older stored doc still
loads. Read both the old and new bucket names during the transition, write only
the new shape.

> **What shipped (Phase B, 2026-06-06).** The mandatory backward-compat
> requirement pushed a cleaner variant of the above: `entities` ships as a
> deterministic PROJECTION over the legacy buckets (`_project_entities` folds
> `named_ids` + `leads` + `resolved_entities` + the sanctions ledger into the
> id-keyed registry), recomputed in `get_state_doc` (read/backfill) and
> `merge_state_doc` (write). The legacy buckets are still WRITTEN as-is rather
> than retired, and `leads`/`sanctions_adjudicated` were NOT renamed to
> `lead_sets`/`sanctions_ledger`. The net is identical (one id-keyed registry
> everything reads/ranks, with strong `check_sanctions` hits as first-class
> entities keyed by `sanctions_id`), and an old doc backfills with zero
> migration. Retiring the legacy buckets / renaming to the literal shape above
> is a focused follow-up (flip the projection to a stored bucket + update the
> legacy readers, namely the frontend hydrate and the old `recall_state` kinds,
> in one change). `claims` shipped as a real stored bucket. `recall_state` gained
> `kind="entities"` (default `sort="severity"`: OFAC SDN > other sanctioned by
> distinct-regime count > PEP) and `kind="claims"`.

---

## 6. Read path: hybrid minimal-inject + tools

The read path has one job: keep the always-in-context core tiny and let the
agent pull exact rows when it needs them.

### 6.1 The injected core (navigation only)

`agent_common._render_state_block` already renders a minimal core: resolved
subjects (name -> id, capped), pinned ids, one-line lead-set headers, and the
small high-value sanctions verdicts. Hold it to a FIXED ~300-500 token budget.
The core answers ONE question:

> What exists and where to look.

It is navigation hints: primary subject id, pinned ids, lead-set counts, the top
few confirmed sanctions. It is NOT the data.

### 6.2 The tools (exact rows on demand)

`recall_state` already exists and returns byte-exact stored records for
`kind in {leads, resolved_entities, sanctions}`, with `from_turn` / `country` /
`sanctioned` / `index` filters, spending no credits and adding nothing to the
graph (`graph_payload` returns `[], []` for it). Extend it with
`kind="entities"` (the unified registry) and `kind="claims"`. `recall_state`
answers the complementary question:

> Give me the rows.

The crisp division of labor:

| Surface | Question it answers | Cost |
| --- | --- | --- |
| Injected core | What exists and where to look | Fixed ~300-500 tokens/turn |
| `recall_state` | Give me the exact rows (enumerate / filter) | One tool round-trip, no credits |
| `recall_memory` (later) | What happened earlier in a long case | Vector query, OLD episodes only |

The bounded prose digest stays for **narrative continuity only**. IDs must NOT
live in the prose. The digest is "what we were doing," not "the ids we found";
`bound_context_digest` already caps it to the last 15 lines and rolls older ones
into one line, with a note that exact ids live in `state_doc` / `recall_state`.

### 6.3 Demote the 30-node graph roster

`build_context_block` still renders up to 30 graph nodes as
`- {name} (id={id}) [{label}]`. That roster is a UI / canvas concern that leaked
into the memory mechanism, and it is half the context-stuffing smell. Shrink it
to pinned + primary subject only, and let `recall_state(kind="entities")` cover
the rest. The roster is not a memory mechanism; the registry is.

### 6.4 Optional Phase 2.5: server-side prefetch

A deterministic, retrieval-shaped optimization: when the intent router sees a
follow-up whose keywords match a known bucket (e.g. "those leads", "the
sanctioned one"), the server prefetches ONE bounded slice (~200 tokens) and
injects it. This is retrieval (a deterministic keyword match producing one
bounded result), not stuffing (everything, every turn). It saves a round-trip on
the common follow-up without re-introducing the anti-pattern. Strictly optional.

> **What shipped (Phase C, 2026-06-07).** `_render_state_block` was rewritten
> from a row dump into FIXED-BUDGET navigation hints: primary subject(s) (from
> `resolved_entities`, newest first, cap 2), pinned ids (cap 8), one header line
> per recent search (cap 3), the top few CONFIRMED sanctions BY NAME (cap 5,
> `confirmed` verdicts only so the core can't misrepresent a dismissed name
> collision), and a single registry pointer (`N entities tracked, M sanctioned`
> + how to rank/enumerate via `recall_state`). The old inline top-10 entity dump
> and the `sanctions_id -> verdict` list are gone; the agent pages exact rows
> with `recall_state`. The up-to-30-node "KNOWN GRAPH ENTITIES" roster
> (doc §6.3) is dropped on the graph path (the registry pointer covers it) and
> survives only as a small bounded fallback (cap 8) for the native loop, which
> keeps no `state_doc`. Measured on a representative multi-turn investigation,
> the injected core went from ~461/695/754 tokens (small/medium/large case) to a
> flat ~251/260/261 tokens, i.e. 45%/62%/65% smaller, and crucially FLAT as the
> case grows. The optional Phase 2.5 prefetch shipped too: for a
> `conversational_followup` whose message keyword-matches a bucket
> ("sanctioned"/"subsidiar"/"sdn" or "lead"/"candidate"), `build_followup_prefetch`
> injects ONE bounded slice (<=6 rows; the sanctions slice surfaces confirmed AND
> dismissed verdicts BY NAME, so the canonical Rosneft enumeration answers in one
> hop). The prompt + `conversational_followup` intent guidance were tightened so
> the agent reaches for `recall_state` on any exact/complete enumeration rather
> than guessing from the now-intentionally-thin core.

---

## 7. Compaction / eviction

Every store needs a bound so context cannot grow without limit. The rule that
makes this safe: compaction loses NARRATION, never IDENTIFIERS. Exact ids and
verdicts live in the structured stores; the lossy prose digest only summarizes
"what we were doing."

| Store | Bound | Eviction policy |
| --- | --- | --- |
| Prose digest | Keep last 15 lines | Roll older lines into one summary line (`bound_context_digest`, built). No LLM call. |
| `entities` | Cap ~200 | Evict by `last_seen_turn` x salience. PIN sanctioned / PEP (never evict). |
| `lead_sets` | Last 3 sets full | Older sets keep the header (count + query) only; drop the rows. |
| `sanctions_ledger` | Append-only | Never delete. Dedupe by `sanctions_id`. |
| `claims` | Cap ~100 | Prefer claims with `source_refs`; evict ref-less claims first. |
| Episodic (L2, later) | Cap ~200 / namespace | Recency x salience; salience seeded from signals (sanctioned/PEP -> high). |

Explicitly **no per-turn LLM summarization into memory.** That is write
amplification: an extra model call every turn to compress facts you already have
in structured form. Consolidation (Mem0-style extract / dedupe / upsert) is a
SCHEDULED, optional, later job (§9 phase F-adjacent), run at boundaries, not in
the hot path. The guide is explicit that consolidation moves memories between
tiers at boundaries, not continuously.

---

## 8. Reconciliation with 06 / 07 / 08 and the guide

09 is the consolidated authoritative plan. 06 stays the audit, 07 the original
phased plan, 08 the registry contract. Where they disagree, this table resolves
it.

| Topic | What 06/07 said | What the guide says | Which wins |
| --- | --- | --- | --- |
| Storage layers | Two layers: Redis `state_doc` (exact) + Upstash Vector (episodic) | Three tiers L1/L2/L3; L1 is the window itself | Keep the two-STORE plan; map it onto three tiers (L1 = turn state, L3 = Redis, L2 = Vector). Same thing, clearer names. |
| How much to inline | 06 §4.1 inlined a fuller `INVESTIGATION STATE` block (resolved + lead rows + verdicts) | Fixed application context budget; retrieve relevant only | 07's decision (minimal inline + `recall_state`) wins, and the guide confirms it. 06 §4.1's fuller inline is superseded. |
| Consolidation | 06 §4.2 floated a background Mem0-style consolidation loop | Consolidation at boundaries, not per-turn | Deterministic projection NOW (no model call); optional scheduled batch consolidation LATER. No per-turn LLM summarization. |
| Entity store | 08 defines a standalone id-to-identity registry; 06/07 have name-keyed `resolved_entities` + a separate `named_ids` cache | L3 semantic = one structured entity store | 08's registry MERGES into `state_doc.entities` as the single L3 entity store. One id-keyed bucket, not three. |
| Prose digest vs episodic | 06 keeps the bounded prose digest for continuity | Hybrid: summarize narrative, keep ids exact elsewhere | Keep the bounded prose digest for short-term narrative; episodic (L2) supersedes it for turn-10+ recall. Both, at different ranges. |
| Provenance | 07 §Phase 4 adds `source_refs` to records | Provenance at write time: source + trust tier | Same direction. 09 makes it a write-path invariant from Phase A, not a later phase. |

The net: 09 keeps 07's locked decisions (LangGraph-only, two stores, memory-as-a-tool,
provenance, evals), adopts 08's registry as the L3 entity store, and drops 06
§4.1's fuller inline in favor of the minimal-inline + retrieve split the guide
endorses.

---

## 9. Phased path

The ordering principle: **A + B are the real fidelity fix and come BEFORE
vector.** Widening the write path and unifying the registry is what stops the
Rosneft failure. Episodic vector memory (D) is useless until the facts are
durable.

| Phase | Size | What it does | What it fixes |
| --- | --- | --- | --- |
| **A. Widen the write path** | small | In the projection: deposit answer-turn `raw_strong_hits` as dismissed `sanctions_ledger` rows; deposit `referenced_node_ids`, `claims` + `source_refs`, and `sayari_risk_factors` paths. | The Rosneft drift directly. Named-but-not-traversed entities now persist. |
| **B. Entity registry** (SHIPPED 2026-06-06) | medium | Unify `resolved_entities` + `named_ids` into id-keyed `entities`; `deposit` from ALL tools (search, profile, ownership, watchlist, sanctions, ICIJ); extend `recall_state` with `kind="entities"` / `kind="claims"`. | Doc 08's "two pipes" disease and the registry contract. |
| **C. Shrink injection** (SHIPPED 2026-06-07) | small | Cut the 30-node roster to pinned + primary; hold `_render_state_block` to a fixed token budget; optional intent prefetch (§6.4). | The context-stuffing smell. Flat token cost as the case grows. |
| **D. Episodic vector** | real build | Provision Upstash Vector; per-turn episode write at finalize; `recall_memory` tool, recency x salience ranked, behind a flag. | Turn-10+ recall of old episodes. New infra + secrets. |
| **E. Provenance / claims** | small-medium | Every claim and entity carries `source_refs`; the renderer cites them compactly. | "Re-cite a turn-2 finding on turn 9 without redoing the work." |
| **F. Multi-turn memory evals** | medium | Multi-turn eval harness; cases asserting recall without re-running tools. | Locks A-E in; catches regressions. |

A + B + C are infra-free and are the fidelity fix. D is the only phase needing
provisioning. E + F harden.

---

## 10. Do NOT (anti-patterns)

A short list of things that look helpful and are not:

- **Do NOT stuff the full `state_doc` or graph into every prompt.** That is the
  named anti-pattern (`PATTERNS.md`). Inject a fixed-budget navigation core; page
  the rest in with `recall_state`.
- **Do NOT LLM-parse the prose answer into memory.** Prose is not a write source
  (HaluMem / hallucinated-write trap). Project only from structured outputs.
- **Do NOT replay raw prior tool results across turns.** That is the quadratic
  token blowup `slim_result_for_model` already fights within a turn; do not undo
  it across turns. Persist structured state, not transcripts.
- **Do NOT do per-turn LLM summarization into Redis.** Write amplification.
  Deterministic projection now; scheduled consolidation later.
- **Do NOT use vector search for exact lead enumeration.** "List the leads from
  turn 3" is an exact filter on L3, not a similarity query. Vector is for fuzzy
  recall of old episodes.
- **Do NOT rely on the 30-node graph roster as memory.** It is a capped,
  truncating UI artifact. The registry is the memory.

---

## 11. The invariant and the eval

The invariant that makes IMS stick (generalizing doc 08's "named in the answer =>
named on the graph"):

> Every entity the agent names with an id in its answer
> (`referenced_node_ids` + `claims.source_refs`) MUST appear in
> `entities` (and on the graph).

This converts a silent recall bug into a testable assertion. If the agent
referenced an id it cannot later recall, the write path dropped it, and the eval
catches it before the user does.

The concrete multi-turn eval:

> Turn 1 investigates a subject whose `check_sanctions` surfaces a sanctioned
> subsidiary that is then dismissed (name collision). Turn 2 asks to re-list the
> sanctioned-but-dismissed subsidiaries. Assert that the dismissed subsidiary is
> recoverable in turn 2 via `recall_state(kind="sanctions")`
> (or `kind="entities"`) AND that `check_sanctions` does NOT appear in turn 2's
> `tools_used`.

That is the Rosneft case as a regression test: the name must survive the write
path, and the recall must not re-spend a tool call. Add it to the multi-turn
harness in Phase F.
