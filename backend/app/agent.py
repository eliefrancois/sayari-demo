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
) -> None:
    impl = get_settings().agent_impl
    runner = agent_graph if impl == "graph" else agent_native
    await runner.run_turn(
        conversation_id, user_message, turn_index, pinned_node_ids, force_risk_report
    )
