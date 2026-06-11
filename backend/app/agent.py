"""Agent facade — dispatches a conversation turn to the active implementation.

The implementation is chosen at runtime by `settings.agent_impl`:
  - "native" (default): the hand-rolled Anthropic loop in agent_native. Proven;
    the safe demo default.
  - "graph": the LangChain + LangGraph StateGraph in agent_graph. Same SSE
    contract and Redis writes, plus LangSmith tracing.

Both expose an identical `run_turn(...)` signature, so flipping AGENT_IMPL is
the only change needed to swap engines. Read the setting per-call so an env
change takes effect without a process restart (and so tests can monkeypatch).

The legacy single-shot path (POST /assess -> agent_native.run_investigation)
is intentionally untouched and always native.
"""

from __future__ import annotations

from app import agent_graph, agent_native
from app.config import get_settings


async def run_turn(
    conversation_id: str,
    user_message: str,
    turn_index: int,
    pinned_node_ids: list[str] | None = None,
    force_risk_report: bool = False,
    turn_id: str | None = None,
    parent_turn_id: str | None = None,
    model: str | None = None,
) -> None:
    impl = get_settings().agent_impl
    if impl == "graph":
        # Branching (Stage 2a) is a graph-impl feature: the tree coordinates are
        # threaded through so the turn runs path-scoped. The API only registers
        # tree turns when AGENT_IMPL=graph, so native never sees them.
        await agent_graph.run_turn(
            conversation_id, user_message, turn_index, pinned_node_ids,
            force_risk_report, turn_id=turn_id, parent_turn_id=parent_turn_id,
            model=model,
        )
        return
    await agent_native.run_turn(
        conversation_id, user_message, turn_index, pinned_node_ids,
        force_risk_report, model=model,
    )
