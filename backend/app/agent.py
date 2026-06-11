"""Agent facade that dispatches a conversation turn to the active implementation.

`settings.agent_impl` picks "native" (the hand-rolled Anthropic loop, the demo
default) or "graph" (the LangGraph StateGraph, same SSE/Redis contract plus
tracing and branching). Both expose the same run_turn signature, and the setting
is read per-call so flipping AGENT_IMPL needs no restart.
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
    """Run one conversation turn through the native or graph implementation."""
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
