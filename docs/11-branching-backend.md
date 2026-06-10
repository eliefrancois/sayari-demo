# Branching Backend (Stage 2a)

This document covers the backend half of the branching-investigation feature: the conversation turn tree, path-scoped state, per-turn graph deltas, and the API contract the frontend canvas consumes. The frontend (fork UI, time-travel rendering) is a separate build; everything it needs from the server is specified here.

## The mental model

A conversation used to be a list of turns. It is now a tree of turns. Every turn has a stable `turn_id` and an optional `parent_turn_id`. Submitting a message without a parent appends to the current head, which is exactly the old linear behavior. Submitting a message with `parent_turn_id` pointing at any prior turn forks a new branch from that point.

The invariant that makes branching trustworthy: a turn sees only the state accumulated along its own path from the root. Sibling branches are invisible to each other. That holds for the agent's injected context, for `recall_state` reads, and for the evidence graph.

## Redis layout (additive only)

Four new keys per conversation, same 24h TTL discipline as everything else. No migration: old conversations lack these keys and keep behaving linearly.

```
conversation:{id}:turn_tree            HASH  turn_id -> JSON tree entry
conversation:{id}:turn_delta:{turn_id} LIST  state_doc deltas written this turn
conversation:{id}:turn_graph:{turn_id} JSON  {nodes, edges} added this turn
conversation:{id}:tree_base            JSON  {state_doc, graph, context} snapshot
```

A tree entry holds `turn_id`, `parent_turn_id`, `turn_index`, `user_message`, `status` (`running` / `done` / `error`), `created_at`, and after the turn finishes: `kind` (`answer` / `summary` / `clarification`), `report_ready`, `offer_risk_report`, and `context_after` (the prose continuation context, internal only).

`tree_base` exists for conversations that started before branching. The first tree-aware turn snapshots the merged state_doc, graph, and context built by the pre-tree turns. Path folds start from that snapshot instead of an empty doc, so old conversations can fork without replaying history they never recorded per turn.

## Path-scoped state: the assembler

Phase F factored `_apply_delta` out of `merge_state_doc` as a pure read-modify core. That refactor is what makes branching cheap. The state_doc for any turn is now a fold:

```
state_doc(turn) = fold(_apply_delta, tree_base.state_doc, deltas along root -> turn)
```

The pieces in `backend/app/conversations.py`:

- `merge_state_doc` still updates the merged top-level doc (backward compatibility, and the trivial single-path case), but when a turn scope is active it also appends the exact delta to that turn's `turn_delta` list.
- `path_to(tree, turn_id)` walks parent pointers to produce the root -> turn path.
- `assemble_state_doc(base, deltas)` is the pure fold.
- `get_path_state_doc(conversation_id, turn_id)` reads the path's deltas and folds them. Folded results for completed turns are cached in-process (`_PATH_FOLD_CACHE`) keyed by path head, so repeated reads do not re-walk the path.

The plumbing trick is a `ContextVar` turn scope. `run_turn` wraps the whole turn in `turn_scope(conversation_id, turn_id, parent_turn_id)`. Inside that scope, `get_state_doc` transparently returns the path-scoped doc instead of the merged one. Nothing downstream changed signature: `recall_state`, the injected core in context assembly, the sanctions ledger, the entity registry, and claims all read through `get_state_doc`, so path-scoping the doc scopes them all for free. No exceptions found: every state subsystem lives inside state_doc.

Prose context follows the same rule. `resolve_prior_context` returns the parent turn's `context_after`, so a fork continues the narrative from its fork point, not from whatever a sibling said later.

## Per-turn graph deltas

`finalize_node` already knows which nodes and edges the turn added; it now writes them as a first-class delta under `turn_graph:{turn_id}`. `accumulate_path_graph` unions deltas along a path (dedupe by node id and edge identity, same logic as the merged graph via `merge_graph_pure`). The merged top-level graph keeps updating as before for linear consumers.

## API contract for the frontend

### Submit a message (existing endpoint, extended)

```
POST /conversations/{id}/messages
{ "message": "...", "parent_turn_id": "abc123def456" }   # parent optional
```

Response:

```
{ "turn_index": 2, "event_cursor": 43, "turn_id": "7c7486aee307", "parent_turn_id": "d248f2fd5f57" }
```

Omitting `parent_turn_id` parents the turn on the current head (linear). Sending it forks. Sending an unknown parent returns 400. Branching requires `AGENT_IMPL=graph`; the native impl returns 400 if a parent is supplied.

### SSE additions

Every event's `data` payload now carries `turn_id` and `parent_turn_id` while a turn is running, so the frontend can attach streaming events to the right branch card without a lookup. Event types and the stream lifecycle are unchanged.

### Get the tree

```
GET /conversations/{id}/tree
->
{ "conversation_id": "...", "turns": [
    { "turn_id": "...", "parent_turn_id": null, "turn_index": 0,
      "user_message": "...", "status": "done", "created_at": 1781063116,
      "kind": "answer", "report_ready": false, "offer_risk_report": false }, ... ] }
```

Sorted by `turn_index`. Old conversations that predate branching return an empty list; fall back to the flat `turns` from hydrate. Hydrate (`GET /conversations/{id}`) also includes the same list under `tree`.

### Get the path graph (time travel)

```
GET /conversations/{id}/turns/{turn_id}/graph
->
{ "turn_id": "...", "path": ["root_turn", ..., "turn_id"],
  "graph": { "nodes": [...], "edges": [...] },        # accumulated along the path
  "turn_delta": { "nodes": [...], "edges": [...] } }  # added by this turn only
```

`graph` is the union of the path's deltas only; sibling branches are excluded. `turn_delta` is the turn's own contribution, included separately so the frontend can pulse new-this-turn nodes and dim inherited ones. Unknown `turn_id` returns 404.

## Verification

Deterministic evals in `backend/evals/branching.py`, wired into `run_evals.py`:

- Fork isolation: two siblings forked from the same parent each see the parent's sanctions hit but not each other's deposits, checked through pure assembly, `recall_state`, and the live `turn_scope` wiring with mocked Redis.
- Path graph accumulation: graph at turn N equals the union of that path's deltas, sibling nodes excluded, own-delta separable.
- Linear regression: a no-fork conversation's path-folded state_doc is byte-identical to the pre-change iterative merge, and `resolve_prior_context` returns the parent's context for both linear continuations and forks.

A live smoke test on a local server confirmed the behavior end to end: a fork from turn 1 did not know a fact deposited on turn 2 of the sibling branch, while the linear continuation did.
