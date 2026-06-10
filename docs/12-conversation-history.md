# Conversation History (the investigations menu)

Stage: shipped 2026-06-10. Backend index + list/delete endpoints, frontend slide-over menu.

## The problem: orphaned keys you could not list

Every conversation has always been persisted in Upstash Redis under its own key family (`conversation:{id}:meta`, `:state`, `:graph`, and so on, see the docstring at the top of `backend/app/conversations.py`). But nothing pointed AT those families. Redis can fetch a key it knows the name of in O(1), and it can SCAN the whole keyspace, but there was no middle ground: no set of "all conversation ids". The only pointer in the system was the frontend's single localStorage slot (`err:conversation_id`), and "New investigation" overwrote it. The conversation survived on the server for 24 hours, fully hydratable, with a title already computed by `bump_meta`. It was just unreachable, because nobody remembered its id.

## The fix: a ZSET index, maintained on write

One new global key:

```
conversations:index   -> ZSET  member=conversation_id  score=updated_at
```

A sorted set is the right shape because the menu's question is "most recently active first, give me a page". That is exactly one `ZREVRANGE`. The index is maintained at the two places conversation metadata is already written: `create_conversation` adds the member scored by `created_at`, and `bump_meta` (which runs at the end of every turn) re-scores it with the fresh `updated_at`. No new write path, no background job.

Endpoints, both additive:

- `GET /conversations?limit=50` returns `{conversations: [{conversation_id, title, created_at, updated_at, turn_count, state}]}`, newest-updated first. One pipeline reads the index page, a second pipelines every member's `meta` + `state` GETs.
- `DELETE /conversations/{id}` removes the index entry and the conversation's whole key family. Per-turn keys (`turn_delta:{turn_id}`, `turn_graph:{turn_id}`) do not have a fixed suffix list, so they are enumerated from the `turn_tree` hash before the hash itself is deleted. 404 when the meta is already gone, 409 while a turn is running (deleting under a live turn would have the background task resurrect keys on its next write).

## TTL implications: the index lies, gently

Every conversation key carries a 24h TTL refreshed on write, and that stays the source of truth. The index follows the same discipline (every touch refreshes its TTL too), but a member can outlive its conversation: each `conversation:{id}:*` key expires 24h after THAT conversation's last write, while the index expires 24h after ANY conversation's last write. So the index can hold pointers to families that already evaporated.

The list endpoint treats this as expected behavior, not corruption. It fetches every member's meta, filters out the ones that came back empty, and lazily `ZREM`s those dead members in the same request. The pure filtering logic (`assemble_conversation_list`) is pinned by a deterministic eval (`conversation_index` in `evals/run_evals.py`) so the expired-member contract cannot silently regress.

Conversations created before the index existed never appear in it. That is deliberate: a SCAN backfill would be a one-shot migration for data that fully expires within 24 hours anyway.

## What this is, and is not

This is a "recent investigations" menu, not an archive. Everything in it dies 24 hours after its last activity, the same lifecycle the conversations themselves always had. The menu makes the existing 24h window navigable; it does not extend it. If durable history is ever wanted, that is a different feature (a real database, an export, or a much longer TTL with explicit cleanup), and nothing in this design blocks it: the index is just pointers.

## Frontend

The menu is a fixed left slide-over (`frontend/components/manager/`), ported from local-lmcanvas's CanvasManager family (MIT, attribution in `frontend/NOTICES.md`) and restyled to this app's tokens. A PanelLeft toggle in the app header opens it. Rows show title, relative updated-at, a turn-count badge, and the active conversation is highlighted. Search filters by title client-side.

Switching a conversation reuses the exact page-reload restore path: set the localStorage pointer, `GET /conversations/{id}`, dispatch the same `hydrate` action. There is deliberately no second hydration path, so turns, the tree canvas, the merged evidence graph and the map data come back identically to a refresh. While a turn is streaming, switching and deleting are paused (rows disabled plus a guard in the switch handler) rather than risking a half-hydrated state under a live SSE stream. "New investigation" now only clears the active pointer and resets the workspace; the old conversation remains reachable from the menu until its TTL ends.

One subtlety the reload path hides: the tree canvas keeps its node positions and camera in component state, and it only fits the viewport on a fresh layout. A page reload gets that for free because the component mounts fresh. A mid-session switch does not, so the previous conversation's stale positions pushed the new conversation's cards outside the visible pane. The fix is a `canvasEpoch` counter on `EntityResolverApp` that bumps on switch, new investigation, and delete-reset, and is used as the canvas `key`. Remounting via key makes a switch behave exactly like a reload without touching the live-turn layout logic.
