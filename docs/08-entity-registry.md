# Entity Naming Registry

> Why the graph keeps growing anonymous nodes ("Other", "Unresolved entity"),
> why those are the *same* bug, and the single primitive that retires the whole
> class: one canonical id-to-identity registry that every source writes to and
> every graph mapper reads from.

> Note: `09-investigation-memory-subsystem.md` is now the consolidated,
> authoritative memory design. This doc (08) is the registry contract; 07 is the
> original phased plan. Both are folded into 09, where the registry merges into
> `state_doc.entities` as the single L3 entity store.

This is an analysis + design doc. The bounded resolver (see §6) is in flight; the
registry consolidation is the planned follow-up. It maps additively onto the
existing code (`sayari.py` graph mappers, `tools.py` lookup assembly,
`conversations.py` state_doc) and is not a rewrite.

---

## 1. The symptom

We have hit the same failure twice, in two different graph mappers:

- **"Other: ..." nodes** in the ownership/risk traversal graph: an intermediate
  node rendered with no real name (e.g. `Other: ...JcvPXQ`). Patched in
  `risk_paths_to_neighborhood` by resolving labels from profile data / state_doc.
- **"Unresolved entity (…id)" blobs** on a hub entity like Gazprom: the
  risk-factor "show your work" overlay floods the canvas with anonymous nodes.

These look like two bugs. They are one disease.

## 2. Root cause: two pipes, one missing source of truth

The system carries entity information down **two independent pipes** that never
reconcile:

1. **The text pipe** (what the agent says). The LLM reads everything — profile,
   `check_sanctions`, ICIJ, watchlist — and reasons over all of it, so it names
   entities richly (Kerimov, Gazprom Shelfproekt, the BVI/Bermuda shells).
2. **The graph pipe** (what gets drawn). Each tool output is mapped to nodes by a
   *separate* function in `sayari.py`. A node can only show a name if *that
   specific mapper* happened to have the name in hand.

A graph node is just an entity **id**. The traversal paths are
`id | has_subsidiary | id | owner_of | id`. For a small entity, the one neighbor
was sitting in the profile we already pulled, so it resolved. For a hub like
Gazprom the paths run multiple hops past what any single tool returned, so most
ids have no name and fall back to a placeholder.

The unreliability is structural, not random: **there is no single place that
knows the names.** Every mapper does its own best-effort naming in isolation, so
every new mapper — or every entity big enough to exceed one tool's output — can
re-introduce the blob. We have been patching the symptom mapper by mapper.

## 3. The fix: a canonical entity registry

One id-to-identity dictionary that everything writes to and everything reads
from. Three verbs:

- **`deposit(id, identity)`** — called from *every* tool result (search,
  profile, ownership, watchlist, sanctions, ICIJ). Upsert/merge, never blind
  overwrite (see merge policy below).
- **`lookup(id)`** — called by *every* graph mapper before it renders a node, and
  usable to enrich text too.
- **`resolve(id)`** — on a `lookup` miss, fetch the entity once (cheap
  `entity_summary`), then `deposit` so the next `lookup` is free.

### 3.1 Identity shape

```
id -> {
  label: str,
  type: str | None,
  sanctioned: bool | None,
  pep: bool | None,
  countries: list[str] | None,
  source: str,        # where this name came from
  confidence: ...     # to arbitrate merges
}
```

### 3.2 Merge policy

Two sources can both know an id, and they do not know it equally well: a full
profile knows more than a one-line search snippet. So `deposit` is an **upsert
with a merge policy** — richer / more-authoritative source wins, and the
`source` tag records provenance. Without this, a thin later hit could clobber a
good earlier name.

### 3.3 Persistence

This is **not** ephemeral UI state. A name learned on turn 1 must be available on
turn 5. The authoritative registry lives on the **backend, persisted in
`state_doc` / Redis** (`conversations.py`). The frontend may mirror a read-only
copy for rendering, but the source of truth is server-side. Think of it as a
cross-turn **cache/dictionary**, not a component store.

## 4. The invariant that makes it stick

The dictionary alone is not the fix. The value is in the **contract**:

> No node enters the graph without passing through `lookup`.

Enforce it with a cheap consistency check / eval:

> Every entity the agent names with an id in its answer appears as a *named*
> node on the graph.

That converts a silent visual bug into a test failure we catch before the user
does, and it stops a future mapper from quietly bypassing the registry and
reintroducing blobs. Registry **plus** invariant is the actual fix.

## 5. How it maps onto current code

- **Mappers** (`sayari.py`): `ownership_to_neighborhood`, `watchlist_to_neighborhood`,
  `risk_paths_to_neighborhood`, `_risk_path_node`, `search_to_nodes` all currently
  name nodes from whatever they hold. They become thin consumers of `lookup`.
- **Lookup assembly** (`tools.py`): the per-call `id_lookup` / `related_entity_lookup`
  built today from the profile relationships block + conversation-known entities is
  the embryo of the registry. Promote it from a per-call dict to a persisted,
  merge-on-write store.
- **Persistence** (`conversations.py`): `get_state_doc` / `merge_state_doc` already
  carry an id_lookup for the earlier "Other"-node fix; the registry generalizes
  this and every tool deposits into it.

## 6. Status & phasing

- **Phase 0 — bounded resolver (in flight).** A capped, concurrent
  `entity_summary` lookup that names the most central unnamed risk-path ids for
  hub entities. This is the `resolve()` arm of the registry, built first to fix
  Gazprom; it is not throwaway.
- **Phase 1 — consolidate.** Move the scattered naming logic in the mappers
  behind `deposit` / `lookup` / `resolve`. Have every tool deposit. Persist in
  `state_doc` with the merge policy.
- **Phase 2 — enforce the invariant.** Route all node creation through `lookup`
  and add the "named in the answer ⇒ named on the graph" consistency eval.

Outcome: both "Other" and "Unresolved" collapse into a single condition — "the
registry does not know this id yet" — which `resolve` fixes once, permanently,
and the invariant keeps fixed.
