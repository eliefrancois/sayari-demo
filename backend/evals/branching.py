"""Deterministic branching evals (Stage 2a: the conversation turn tree).

Branching is the riskiest change to the memory core so far: the state_doc stops
being one merged blob and becomes a PATH-SCOPED fold of per-turn deltas. These
checks pin the three properties that make that safe, with no Redis, no live
model, and no credits — the same discipline as evals/multiturn.py:

  1. FORK ISOLATION: two sibling branches forked from the same parent each see
     the parent's state but NEVER each other's deltas. Asserted through the
     pure path assembler (`conversations.assemble_state_doc`, a fold of the
     SAME `_apply_delta` production persists) AND through the real
     `recall_state` tool reading a path-scoped doc, AND through the live
     `turn_scope` contextvar wiring of `conversations.get_state_doc`.

  2. PATH GRAPH ACCUMULATION: the evidence graph at turn N on a path is the
     union of THAT path's per-turn graph deltas only (the time-travel payload),
     with the turn's own delta kept separate for the frontend pulse/dim.

  3. LINEAR REGRESSION: a conversation that never forks produces a path-folded
     state_doc byte-identical to the pre-change merged-doc behavior (the
     iterative `_apply_delta` read-modify-write), including mid-turn named_ids
     merges and the chained prose digest.

Each turn's delta comes from the real `agent_graph._build_state_delta`
projection, so the write path under test is production's, not a stand-in.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

from app import agent_graph, conversations
from app.agent_common import bound_context_digest, digest_answer
from app.schema import Claim, SanctionsHit, SourceRef, TurnAnswer
from app.tools import recall_state_tool
from evals.multiturn import _recall_against

Row = tuple[str, str, bool, str]


# --- Shared fixtures ---------------------------------------------------------


def _hit(name: str, sid: str) -> SanctionsHit:
    return SanctionsHit(
        name_searched="Rosneft",
        matched_name=name,
        lists=["OFAC SDN"],
        sanctions_id=sid,
        score=0.9,
        on_watchlist=True,
        countries=["ru"],
    )


def _turn_delta(
    turn_index: int,
    user_message: str,
    *,
    nodes: list[dict] | None = None,
    confirmed: list[SanctionsHit] | None = None,
    claims: list[Claim] | None = None,
    answer_text: str = "ok",
) -> dict[str, Any]:
    """One turn's state delta through the REAL production projection."""
    state: dict[str, Any] = {
        "turn_index": turn_index,
        "user_message": user_message,
        "intent": "profile_entity",
        "pinned_node_ids": [],
        "turn_nodes": nodes or [],
        "turn_leads": [],
        "raw_strong_hits": [h.model_dump() for h in (confirmed or [])],
    }
    answer = TurnAnswer(
        answer=answer_text,
        sanctions_hits=list(confirmed or []),
        claims=list(claims or []),
    )
    return agent_graph._build_state_delta(state, None, answer)


def _node(nid: str, name: str) -> dict[str, Any]:
    return {
        "id": nid, "name": name, "label": "Entity", "source_system": "sayari",
        "properties": {"sanctioned": False, "pep": False, "countries": ["RUS"]},
    }


# --- Case 1: fork isolation --------------------------------------------------


async def fork_isolation_rows() -> list[Row]:
    """Turn 1 (root) confirms a parent-level sanction. Turn 2A and turn 2B fork
    from turn 1 as SIBLINGS: A deposits a hit on 'Rosneft Trading S.A.', B
    deposits a hit on 'Tuapse Refinery LLC'. The A path must see the parent hit
    + A's hit and NOT B's (and symmetrically for B), through the pure assembler,
    through the real recall_state tool, and through the scoped get_state_doc."""
    case = "branch_fork_isolation"

    d1 = _turn_delta(
        0, "Investigate Rosneft.",
        nodes=[_node("sayari-rosneft", "Rosneft")],
        confirmed=[_hit("Rosneft Parent Co", "ofac-parent")],
        claims=[Claim(
            text="Rosneft Parent Co appears on the OFAC SDN list.",
            source_refs=[SourceRef(source="opensanctions", sanctions_id="ofac-parent")],
            confidence="high",
        )],
    )
    d2a = _turn_delta(
        1, "Check the trading arm.",
        confirmed=[_hit("Rosneft Trading S.A.", "ofac-branch-a")],
    )
    d2b = _turn_delta(
        2, "Check the refinery instead.",
        confirmed=[_hit("Tuapse Refinery LLC", "ofac-branch-b")],
    )

    empty = conversations._empty_state_doc()
    doc_a = conversations.assemble_state_doc(empty, [d1, d2a])
    doc_b = conversations.assemble_state_doc(empty, [d1, d2b])

    def ledger_ids(doc: dict[str, Any]) -> set[str]:
        return {r.get("sanctions_id") for r in doc.get("sanctions_adjudicated") or []}

    a_ids, b_ids = ledger_ids(doc_a), ledger_ids(doc_b)
    a_isolated = (
        {"ofac-parent", "ofac-branch-a"}.issubset(a_ids) and "ofac-branch-b" not in a_ids
    )
    b_isolated = (
        {"ofac-parent", "ofac-branch-b"}.issubset(b_ids) and "ofac-branch-a" not in b_ids
    )
    # The unified registry (what recall_state kind="entities" ranks) is scoped too.
    registry_isolated = (
        "ofac-branch-a" in doc_a["entities"] and "ofac-branch-b" not in doc_a["entities"]
        and "ofac-branch-b" in doc_b["entities"] and "ofac-branch-a" not in doc_b["entities"]
    )

    # The REAL recall tool over each path's doc: branch A's recall enumerates
    # the parent hit + its own, never the sibling's.
    async with _recall_against(doc_a):
        sanc_a = await recall_state_tool("c-fork", kind="sanctions")
    async with _recall_against(doc_b):
        sanc_b = await recall_state_tool("c-fork", kind="sanctions")
    names_a = {r.get("matched_name") for r in sanc_a.get("items", [])}
    names_b = {r.get("matched_name") for r in sanc_b.get("items", [])}
    recall_isolated = (
        {"Rosneft Parent Co", "Rosneft Trading S.A."}.issubset(names_a)
        and "Tuapse Refinery LLC" not in names_a
        and {"Rosneft Parent Co", "Tuapse Refinery LLC"}.issubset(names_b)
        and "Rosneft Trading S.A." not in names_b
    )

    # The contextvar wiring: inside `turn_scope`, the REAL get_state_doc (the
    # exact function recall_state and the context assembly call in production)
    # must return the path-scoped doc — no monkeypatched recall here.
    scoped_rows = await _scoped_get_state_doc_check(d1, d2a, d2b)

    return [
        (case, "sibling_ledgers_isolated", a_isolated and b_isolated,
         f"a={sorted(str(x) for x in a_ids)} b={sorted(str(x) for x in b_ids)}"),
        (case, "sibling_registries_isolated", registry_isolated,
         "branch hits are first-class registry entities on their own path only"),
        (case, "recall_state_path_scoped", recall_isolated,
         f"names_a={sorted(str(n) for n in names_a)}"),
        *scoped_rows,
    ]


@contextlib.asynccontextmanager
async def _fake_tree_storage(
    tree: dict[str, dict[str, Any]],
    deltas_by_turn: dict[str, list[dict[str, Any]]],
):
    """Point the Redis-facing tree fetchers at in-memory fixtures so the live
    scope wiring (`turn_scope` -> `get_state_doc` -> path fold) runs end to end
    without a store. Only the three fetchers are replaced; the assembler, the
    scope contextvar, and `get_state_doc` itself are production code."""
    orig_tree = conversations.get_turn_tree
    orig_deltas = conversations.read_turn_deltas
    orig_base = conversations._get_tree_base

    async def fake_tree(_cid: str) -> dict[str, dict[str, Any]]:
        return tree

    async def fake_deltas(_cid: str, turn_id: str) -> list[dict[str, Any]]:
        return list(deltas_by_turn.get(turn_id) or [])

    async def fake_base(_cid: str) -> dict[str, Any]:
        return conversations._empty_tree_base()

    conversations.get_turn_tree = fake_tree  # type: ignore[assignment]
    conversations.read_turn_deltas = fake_deltas  # type: ignore[assignment]
    conversations._get_tree_base = fake_base  # type: ignore[assignment]
    try:
        yield
    finally:
        conversations.get_turn_tree = orig_tree  # type: ignore[assignment]
        conversations.read_turn_deltas = orig_deltas  # type: ignore[assignment]
        conversations._get_tree_base = orig_base  # type: ignore[assignment]


async def _scoped_get_state_doc_check(
    d1: dict[str, Any], d2a: dict[str, Any], d2b: dict[str, Any]
) -> list[Row]:
    case = "branch_fork_isolation"
    cid = "c-scope-eval"  # unique so the fold cache can't be pre-populated
    tree = {
        "t1": {"turn_id": "t1", "parent_turn_id": None, "turn_index": 0, "status": "done"},
        "t2a": {"turn_id": "t2a", "parent_turn_id": "t1", "turn_index": 1, "status": "running"},
        "t2b": {"turn_id": "t2b", "parent_turn_id": "t1", "turn_index": 2, "status": "running"},
    }
    deltas = {"t1": [d1], "t2a": [d2a], "t2b": [d2b]}

    async with _fake_tree_storage(tree, deltas):
        with conversations.turn_scope(cid, "t2a", "t1"):
            doc_in_a = await conversations.get_state_doc(cid)
            # recall_state inside the scope goes through the same get_state_doc.
            sanc_in_a = await recall_state_tool(cid, kind="sanctions")
        with conversations.turn_scope(cid, "t2b", "t1"):
            doc_in_b = await conversations.get_state_doc(cid)

    ids_a = {r.get("sanctions_id") for r in doc_in_a.get("sanctions_adjudicated") or []}
    ids_b = {r.get("sanctions_id") for r in doc_in_b.get("sanctions_adjudicated") or []}
    scope_routes = (
        "ofac-branch-a" in ids_a and "ofac-branch-b" not in ids_a
        and "ofac-branch-b" in ids_b and "ofac-branch-a" not in ids_b
        and "ofac-parent" in ids_a and "ofac-parent" in ids_b
    )
    recall_in_scope = any(
        r.get("matched_name") == "Rosneft Trading S.A."
        for r in sanc_in_a.get("items", [])
    ) and not any(
        r.get("matched_name") == "Tuapse Refinery LLC"
        for r in sanc_in_a.get("items", [])
    )
    return [
        (case, "turn_scope_routes_get_state_doc", scope_routes,
         f"in_a={sorted(str(x) for x in ids_a)} in_b={sorted(str(x) for x in ids_b)}"),
        (case, "recall_inside_scope_isolated", recall_in_scope,
         f"items={[r.get('matched_name') for r in sanc_in_a.get('items', [])]}"),
    ]


# --- Case 2: path graph accumulation ----------------------------------------


async def path_graph_rows() -> list[Row]:
    """A tree t1 -> (t2a | t2b), t2a -> t3a, each turn adding distinct graph
    nodes. The accumulated graph at t3a must be the union of ITS path's deltas
    only (t1 + t2a + t3a), the sibling's nodes must be absent, the turn's own
    delta stays distinguishable, and edges dedupe by (source, type, target)
    exactly like production's merge_graph."""
    case = "branch_path_graph"

    def gd(nodes: list[dict], edges: list[dict]) -> dict[str, list]:
        return {"nodes": nodes, "edges": edges}

    n1, n2, n3, n4 = (
        _node("e1", "Rosneft"), _node("e2", "Trading Arm"),
        _node("e3", "Refinery"), _node("e4", "Shell Co"),
    )
    e12 = {"source": "e1", "type": "owns", "target": "e2"}
    e13 = {"source": "e1", "type": "owns", "target": "e3"}
    e24 = {"source": "e2", "type": "owns", "target": "e4"}

    tree = {
        "t1": {"turn_id": "t1", "parent_turn_id": None, "turn_index": 0},
        "t2a": {"turn_id": "t2a", "parent_turn_id": "t1", "turn_index": 1},
        "t2b": {"turn_id": "t2b", "parent_turn_id": "t1", "turn_index": 2},
        "t3a": {"turn_id": "t3a", "parent_turn_id": "t2a", "turn_index": 3},
    }
    deltas = {
        "t1": gd([n1], []),
        # t2a re-adds n1 (a traversal revisiting the subject) — must dedupe.
        "t2a": gd([n1, n2], [e12]),
        "t2b": gd([n3], [e13]),
        "t3a": gd([n4], [e24, e12]),  # e12 repeated — must dedupe to one edge
    }

    path = conversations.path_to(tree, "t3a")
    path_ok = path == ["t1", "t2a", "t3a"]

    base = {"nodes": [], "edges": []}
    acc = conversations.accumulate_path_graph(base, [deltas[t] for t in path])
    node_ids = {n["id"] for n in acc["nodes"]}
    edge_keys = {(e["source"], e["type"], e["target"]) for e in acc["edges"]}
    union_ok = node_ids == {"e1", "e2", "e4"} and edge_keys == {
        ("e1", "owns", "e2"), ("e2", "owns", "e4"),
    }
    sibling_excluded = "e3" not in node_ids and ("e1", "owns", "e3") not in edge_keys

    # Sibling time-travel: the graph at t2b = t1 + t2b only.
    acc_b = conversations.accumulate_path_graph(
        base, [deltas[t] for t in conversations.path_to(tree, "t2b")]
    )
    b_ids = {n["id"] for n in acc_b["nodes"]}
    sibling_path_ok = b_ids == {"e1", "e3"}

    # The turn's OWN delta (the frontend pulse) stays separate from inherited.
    own = deltas["t3a"]
    own_ok = {n["id"] for n in own["nodes"]} == {"e4"}
    inherited = node_ids - {n["id"] for n in own["nodes"]}
    pulse_dim_ok = own_ok and inherited == {"e1", "e2"}

    # Default fork parent: the latest registered turn by turn_index.
    head_ok = conversations.latest_turn_id(tree) == "t3a"

    return [
        (case, "path_walk_root_to_turn", path_ok, f"path={path}"),
        (case, "union_of_own_path_only", union_ok and sibling_excluded,
         f"nodes={sorted(node_ids)} edges={sorted(edge_keys)}"),
        (case, "sibling_path_accumulates_its_own", sibling_path_ok,
         f"b_nodes={sorted(b_ids)}"),
        (case, "own_delta_vs_inherited", pulse_dim_ok,
         f"own={sorted(n['id'] for n in own['nodes'])} inherited={sorted(inherited)}"),
        (case, "default_parent_is_head", head_ok,
         f"head={conversations.latest_turn_id(tree)}"),
    ]


# --- Case 3: linear regression (no fork == pre-change behavior) ---------------


async def linear_equivalence_rows() -> list[Row]:
    """A 3-turn conversation that never forks. The path fold (tree_base ->
    per-turn deltas through `assemble_state_doc`) must produce a state_doc
    IDENTICAL to the pre-change behavior (iterative `_apply_delta`
    read-modify-write of one merged doc), including a mid-turn named_ids merge
    like the one `_resolve_and_map_risk_paths` performs. The chained
    `context_after` digests must likewise equal the legacy global context."""
    case = "branch_linear_regression"

    d1 = _turn_delta(
        0, "Investigate Rosneft.",
        nodes=[_node("sayari-rosneft", "Rosneft")],
        confirmed=[_hit("Rosneft Parent Co", "ofac-parent")],
    )
    # Turn 2 performs a MID-TURN named_ids merge (the risk-path resolver write)
    # followed by its finalize delta — two deltas attributed to one turn.
    d2_mid = {"named_ids": {"e-midturn": {
        "label": "Mid-Turn Resolved Co", "type": "company",
        "sanctioned": False, "pep": False, "countries": ["CYP"],
    }}}
    d2 = _turn_delta(
        1, "Map the ownership.",
        nodes=[_node("e-sub", "Subsidiary LLC")],
        claims=[Claim(
            text="Subsidiary LLC is wholly owned.",
            source_refs=[SourceRef(source="sayari", sayari_entity_id="e-sub")],
            confidence="high",
        )],
    )
    d3 = _turn_delta(
        2, "Any sanctions on the subsidiary?",
        confirmed=[_hit("Subsidiary LLC", "ofac-sub")],
    )

    # Pre-change behavior: iterative read-modify-write of ONE merged doc, the
    # exact transformation merge_state_doc persisted before branching existed.
    merged = conversations._empty_state_doc()
    merged["entities"] = conversations._project_entities(merged)
    for delta in (d1, d2_mid, d2, d3):
        merged = conversations._apply_delta(merged, delta)
    merged = conversations._normalize_doc(merged)

    # Branching behavior on the same linear chain: fold the per-turn deltas.
    folded = conversations.assemble_state_doc(
        conversations._empty_state_doc(), [d1, d2_mid, d2, d3]
    )

    same = json.dumps(merged, sort_keys=True, default=str) == json.dumps(
        folded, sort_keys=True, default=str
    )

    # And through the live scope wiring with per-turn delta attribution.
    tree = {
        "t1": {"turn_id": "t1", "parent_turn_id": None, "turn_index": 0, "status": "done"},
        "t2": {"turn_id": "t2", "parent_turn_id": "t1", "turn_index": 1, "status": "done"},
        "t3": {"turn_id": "t3", "parent_turn_id": "t2", "turn_index": 2, "status": "running"},
    }
    deltas = {"t1": [d1], "t2": [d2_mid, d2], "t3": [d3]}
    async with _fake_tree_storage(tree, deltas):
        with conversations.turn_scope("c-linear-eval", "t3", "t2"):
            scoped = await conversations.get_state_doc("c-linear-eval")
    scoped_same = json.dumps(merged, sort_keys=True, default=str) == json.dumps(
        scoped, sort_keys=True, default=str
    )

    msgs = ["Investigate Rosneft.", "Map the ownership."]

    # Prose digest: a turn's prior context comes from its PARENT's stored
    # context_after — so a fork from turn 1 starts from turn 1's narrative,
    # not from the sibling turn 2's. Linear continuation (parent = head) gets
    # the head's context_after, which is exactly what the legacy global
    # context string held after that turn.
    ctx_after_t1 = bound_context_digest(
        digest_answer(0, msgs[0], TurnAnswer(answer="Found the parent hit."))
    )
    ctx_after_t2 = bound_context_digest(
        (ctx_after_t1 + "\n" + digest_answer(1, msgs[1], TurnAnswer(answer="Mapped it.")))
        .strip()
    )
    ctx_tree = {
        "t1": {"turn_id": "t1", "parent_turn_id": None, "turn_index": 0,
               "status": "done", "context_after": ctx_after_t1},
        "t2": {"turn_id": "t2", "parent_turn_id": "t1", "turn_index": 1,
               "status": "done", "context_after": ctx_after_t2},
    }
    async with _fake_tree_storage(ctx_tree, {}):
        linear_prior = await conversations.resolve_prior_context("c-ctx-eval", "t2")
        fork_prior = await conversations.resolve_prior_context("c-ctx-eval", "t1")
    context_ok = (
        linear_prior == ctx_after_t2  # continuing the head = legacy behavior
        and fork_prior == ctx_after_t1  # forking rewinds the narrative to t1
        and "Mapped it" not in fork_prior  # the sibling turn's digest is absent
    )

    return [
        (case, "fold_equals_merged_doc", same,
         f"keys={sorted(merged) == sorted(folded)}"),
        (case, "scoped_read_equals_merged_doc", scoped_same,
         "live turn_scope fold matches the iterative merge byte for byte"),
        (case, "prior_context_follows_parent", context_ok,
         f"fork_prior_is_t1={fork_prior == ctx_after_t1}"),
    ]
