"""Phase 1 agent: hand-rolled Anthropic tool-use loop.

Reads top-to-bottom as a teaching artifact. Phase 2 (LangGraph) would replace
this file with one of comparable length but the SHAPE of the loop — request,
parse tool_use blocks, execute tools, append tool_results, repeat until
submit_summary — is the same. LangGraph just gives you graph-shaped control
flow, retries, durable state, and observability hooks for free.

Public entry point: run_investigation(session_id, user_query)
  - Spawned as a background task from POST /assess.
  - Emits SSE events into Upstash via app.sessions.
  - The /stream/:id endpoint reads them out independently.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import ValidationError

from app import sessions, tracing
from app.config import get_settings
from app.prompts import SUBMIT_SUMMARY_TOOL, SYSTEM_PROMPT
from app.schema import RiskSummary
from app.tools import TOOLS, execute_tool

log = logging.getLogger("erre.agent")

MODEL = "claude-sonnet-4-5-20250929"  # dated snapshot = reproducible demo behavior
MAX_ITERATIONS = 20  # safety bail-out; real investigations finish in 6-12
MAX_TOKENS_PER_TURN = 4096


def _client() -> AsyncAnthropic:
    """Async Anthropic client. Created per-investigation; no shared state."""
    return AsyncAnthropic(api_key=get_settings().anthropic_api_key)


async def _emit(session_id: str, type_: str, **data: Any) -> None:
    """Append an SSE event to the session queue. Frontend consumes via /stream/:id."""
    await sessions.append_event(session_id, {"type": type_, "data": data})


async def run_investigation(session_id: str, user_query: str) -> None:
    """Run the agent loop for one investigation. All output goes via SSE events
    written to Upstash. This function never returns anything meaningful — its
    side effect is the event stream and the final session state."""
    await sessions.set_state(session_id, "running")
    await _emit(session_id, "agent_started", input=user_query)
    tracing.log_event("investigation_started", session_id=session_id, query=user_query)

    client = _client()
    # The full tool list passed to Claude: 6 investigation tools + 1 terminator.
    all_tools = TOOLS + [SUBMIT_SUMMARY_TOOL]

    # The conversation buffer. Each turn we send the entire array and get back
    # an assistant turn that we append. Tool results go back as user-role
    # messages with type=tool_result content blocks.
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": f"Investigate this subject: {user_query}"}
    ]

    tools_used: list[str] = []

    try:
        for iteration in range(MAX_ITERATIONS):
            # ----- Send to Claude -----
            with tracing.span(
                "llm_call",
                session_id=session_id,
                iteration=iteration,
                model=MODEL,
                message_count=len(messages),
            ) as sp:
                resp = await client.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS_PER_TURN,
                    system=SYSTEM_PROMPT,
                    tools=all_tools,
                    messages=messages,
                )
                sp.attach(
                    input_tokens=resp.usage.input_tokens,
                    output_tokens=resp.usage.output_tokens,
                    stop_reason=resp.stop_reason,
                )

            # Append the assistant turn verbatim — Claude's tool_use blocks must
            # be preserved exactly when we send tool_results back.
            messages.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})

            # ----- Parse the response -----
            # A response can contain mixed text + tool_use blocks. We process
            # them in order: text becomes agent_thought events, tool_use blocks
            # get executed.
            tool_results_for_next_turn: list[dict[str, Any]] = []
            submitted_summary: RiskSummary | None = None

            for block in resp.content:
                if block.type == "text":
                    text = block.text.strip()
                    if text:
                        await _emit(session_id, "agent_thought", text=text)

                elif block.type == "tool_use":
                    name = block.name
                    args = block.input or {}

                    # The terminator. Validate via Pydantic; if it fails, send
                    # the error back as a tool_result so Claude can fix it.
                    if name == "submit_summary":
                        try:
                            args_with_used = dict(args)
                            # Ensure tools_used is populated even if Claude forgot.
                            if not args_with_used.get("tools_used"):
                                args_with_used["tools_used"] = sorted(set(tools_used))
                            submitted_summary = RiskSummary(**args_with_used)
                        except ValidationError as e:
                            tool_results_for_next_turn.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": json.dumps({"validation_error": e.errors()}),
                                "is_error": True,
                            })
                            await _emit(
                                session_id, "agent_thought",
                                text=f"(Pydantic validation failed on submit_summary, retrying...)"
                            )
                        continue

                    # Regular investigation tool.
                    tools_used.append(name)
                    await _emit(
                        session_id, "tool_call_start",
                        tool=name, args=args, call_id=block.id,
                    )

                    with tracing.span(
                        "tool_call",
                        session_id=session_id,
                        tool=name,
                        args=args,
                    ) as sp:
                        result_json = await execute_tool(name, args)
                        sp.attach(result_size=len(result_json))

                    # Parse for the SSE event (nodes/edges go to React Flow).
                    try:
                        parsed = json.loads(result_json)
                    except json.JSONDecodeError:
                        parsed = {}

                    await _emit(
                        session_id, "tool_call_result",
                        call_id=block.id,
                        tool=name,
                        nodes=parsed.get("nodes", []),
                        edges=parsed.get("edges", []),
                        metadata=parsed.get("metadata", {}),
                        summary=_short_summary(name, parsed),
                    )

                    # Special: sanctions_hit event for the UI to flash a red badge.
                    if name == "check_sanctions" and parsed.get("any_strong_match"):
                        await _emit(
                            session_id, "sanctions_hit",
                            name=parsed.get("name_searched"),
                            hits=parsed.get("hits", []),
                        )

                    tool_results_for_next_turn.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_json,
                    })

            # ----- Decide what's next -----
            if submitted_summary is not None:
                await _emit(session_id, "summary", summary=submitted_summary.model_dump())
                break

            if tool_results_for_next_turn:
                messages.append({"role": "user", "content": tool_results_for_next_turn})
                continue

            # No tool_use AND no summary submitted: Claude is just talking.
            # Treat this as done with a synthetic summary so the frontend always
            # gets *something* structured.
            await _emit(
                session_id, "agent_thought",
                text="(agent produced no tool calls and no summary; treating as done)",
            )
            fallback = RiskSummary(
                entity_name=user_query,
                entity_id=None,
                found=False,
                claims=[],
                risk_signals=[],
                sanctions_hits=[],
                investigation_summary=(
                    "Agent did not converge to a structured answer. "
                    "Most likely the subject was not found or the model bailed."
                ),
                tools_used=sorted(set(tools_used)),
            )
            await _emit(session_id, "summary", summary=fallback.model_dump())
            break

        else:
            # Hit MAX_ITERATIONS without breaking.
            await _emit(
                session_id, "error",
                message=f"Investigation exceeded {MAX_ITERATIONS} iterations without convergence.",
            )

    except Exception as e:
        log.exception("agent_failed", extra={"session_id": session_id})
        await _emit(session_id, "error", message=f"agent failed: {e}")
        await sessions.set_state(session_id, "error")
        tracing.log_event("investigation_failed", session_id=session_id, error=str(e))
        return

    await sessions.set_state(session_id, "done")
    await _emit(session_id, "done")
    tracing.log_event(
        "investigation_complete",
        session_id=session_id,
        tools_used=sorted(set(tools_used)),
    )


def _short_summary(tool_name: str, parsed: dict[str, Any]) -> str:
    """One-line human-readable summary of a tool result, shown in the tool-call feed."""
    if tool_name == "search_entity":
        n = len(parsed.get("nodes", []))
        return f"found {n} match{'es' if n != 1 else ''}"
    if tool_name in {"get_relationships", "get_officers", "find_address_connections", "find_er_links"}:
        n = len(parsed.get("nodes", []))
        e = len(parsed.get("edges", []))
        meta = parsed.get("metadata", {})
        extra = f" (capped at {meta.get('capped_at')})" if meta.get("capped_at") else ""
        return f"{n} nodes, {e} edges{extra}"
    if tool_name == "check_sanctions":
        c = parsed.get("count", 0)
        strong = parsed.get("any_strong_match", False)
        return f"{c} hit{'s' if c != 1 else ''}" + (" — STRONG MATCH" if strong else "")
    return "ok"
