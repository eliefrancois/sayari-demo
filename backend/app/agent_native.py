"""Hand-rolled Anthropic tool-use loop: request, run tools, repeat until a terminator.

The native agent impl behind POST /assess (run_investigation) and conversation
turns (run_turn). Emits SSE events into Upstash; the stream endpoints read them out.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import ValidationError

from app import episodic, intent, sessions, tracing
from app.agent_common import (
    MAX_ITERATIONS,
    MAX_TOKENS_PER_TURN,
    MODEL,
    budget_nudge,
    build_context_block,
    build_sanctions_review,
    build_turn_message,
    cache_last_tool,
    cached_system,
    digest_answer,
    digest_summary,
    graph_payload,
    resolve_model,
    short_summary,
    slim_result_for_model,
)
from app.config import get_settings
from app.prompts import SUBMIT_ANSWER_TOOL, SUBMIT_SUMMARY_TOOL, SYSTEM_PROMPT
from app.schema import RiskSummary, SanctionsHit, TurnAnswer
from app.tools import TOOLS, execute_tool
from app import conversations, sanctions

log = logging.getLogger("erre.agent")


def _client() -> AsyncAnthropic:
    """Async Anthropic client. Created per-investigation; no shared state."""
    return AsyncAnthropic(api_key=get_settings().anthropic_api_key)


def _terminator_retry_content(
    name: str, e: ValidationError, stop_reason: str | None
) -> str:
    """The tool_result content fed back after a terminator failed validation.

    Truncation-aware: when the model hit the output ceiling (stop_reason ==
    "max_tokens") its tool-call args came back cut off (unparseable / missing
    fields). Dumping the giant e.errors() blob just grows the input and reinforces
    the loop, so we send a SHORT targeted nudge to emit a SMALLER terminator.
    Otherwise (a genuine shape error) the structured errors are the useful signal,
    so we keep them."""
    if stop_reason == "max_tokens":
        return json.dumps({
            "error": (
                f"Your previous {name} was CUT OFF at the output token limit, so its "
                "arguments were truncated and could not be parsed. Re-emit it COMPLETE "
                "but SHORTER: keep only the most important claims (fewer, terser), trim "
                "long narrative text, and make sure the tool-call JSON is fully closed."
            )
        })
    return json.dumps({"validation_error": e.errors()})


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
    # cache_last_tool adds an ephemeral cache breakpoint on the last tool so the
    # whole (stable) tool-definitions block is cached across iterations.
    all_tools = cache_last_tool(TOOLS + [SUBMIT_SUMMARY_TOOL])
    # System prompt as a cached block (breakpoint at its end) — cached alongside
    # the tools so the large, stable prefix isn't re-billed every iteration.
    system_blocks = cached_system(SYSTEM_PROMPT)

    # The conversation buffer. Each turn we send the entire array and get back
    # an assistant turn that we append. Tool results go back as user-role
    # messages with type=tool_result content blocks.
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": f"Investigate this subject: {user_query}"}
    ]

    tools_used: list[str] = []
    # Raw strong watchlist hits from check_sanctions (before agent adjudication).
    raw_strong_hits: list[dict[str, Any]] = []

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
                    system=system_blocks,
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
                            log.warning(
                                "terminator validation failed name=%s errors=%s",
                                "submit_summary", e.errors(),
                            )
                            tool_results_for_next_turn.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": _terminator_retry_content(
                                    "submit_summary", e, resp.stop_reason
                                ),
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

                    graph_nodes, graph_edges = graph_payload(name, parsed)

                    await _emit(
                        session_id, "tool_call_result",
                        call_id=block.id,
                        tool=name,
                        nodes=graph_nodes,
                        edges=graph_edges,
                        metadata=parsed.get("metadata", {}),
                        summary=short_summary(name, parsed),
                    )

                    # Special: sanctions_hit event for the UI to flash a red badge.
                    if name == "check_sanctions" and parsed.get("any_strong_match"):
                        await _emit(
                            session_id, "sanctions_hit",
                            name=parsed.get("name_searched"),
                            hits=parsed.get("hits", []),
                        )
                        for hit in parsed.get("hits", []):
                            try:
                                sh = SanctionsHit(**hit)
                            except Exception:
                                continue
                            if sanctions.is_strong_match(sh):
                                raw_strong_hits.append(sh.model_dump())

                    tool_results_for_next_turn.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        # Slimmed for the model; UI got full nodes via the event.
                        "content": (
                            json.dumps(slim_result_for_model(parsed), default=str)
                            if parsed else result_json
                        ),
                    })

            # ----- Decide what's next -----
            if submitted_summary is not None:
                await _emit_sanctions_review(session_id, submitted_summary, raw_strong_hits)
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


async def _emit_sanctions_review(
    session_id: str,
    summary: RiskSummary,
    raw_strong_hits: list[dict[str, Any]],
) -> None:
    """Compare raw strong watchlist hits to what the agent kept in the summary."""
    review = build_sanctions_review(summary, raw_strong_hits)
    if review is None:
        return
    await _emit(session_id, "sanctions_review", review=review)


# =====================================================================
# Phase 2: multi-turn conversation runner.
#
# run_turn is the conversation-aware sibling of run_investigation. The agent
# loop is identical in SHAPE; the differences are:
#   - two terminators (submit_summary for investigations, submit_answer for
#     clarifications / follow-ups) — the agent picks based on the turn type,
#   - a compressed CONVERSATION CONTEXT block prepended to the user message so
#     follow-ups build on prior turns without replaying every raw tool_result,
#   - graph nodes/edges accumulate into the conversation's stored graph,
#   - events persist in Upstash under conversation:{id}:events for resume.
# =====================================================================


async def _emit_conv(conversation_id: str, turn_index: int, type_: str, **data: Any) -> None:
    """Append an SSE event to the conversation queue, tagged with its turn_index."""
    payload = dict(data)
    payload["turn_index"] = turn_index
    await conversations.append_event(conversation_id, {"type": type_, "data": payload})


async def run_turn(
    conversation_id: str,
    user_message: str,
    turn_index: int,
    pinned_node_ids: list[str] | None = None,
    force_risk_report: bool = False,
    model: str | None = None,
) -> None:
    """Run one conversation turn. Output is the SSE event stream persisted under
    the conversation, plus updated summaries/answers/graph/context in Redis.

    `model` optionally selects the main-agent model per request (allowlisted via
    resolve_model; None = default Sonnet 4.5)."""
    pinned_node_ids = pinned_node_ids or []
    model_id = resolve_model(model)
    await conversations.set_state(conversation_id, "running")
    await _emit_conv(conversation_id, turn_index, "agent_started", input=user_message)
    tracing.log_event(
        "turn_started", conversation_id=conversation_id, turn=turn_index, query=user_message
    )

    client = _client()

    context = await conversations.get_context(conversation_id)
    graph = await conversations.get_graph(conversation_id)
    context_block = build_context_block(context, graph, pinned_node_ids, force_risk_report)

    # --- Intent router: classify the turn, narrow tools, inject guidance ---
    intent_result = await intent.classify_intent(user_message, context)
    tracing.log_event(
        "intent_classified",
        conversation_id=conversation_id,
        turn=turn_index,
        impl="native",
        intent=intent_result.get("intent"),
        confidence=intent_result.get("confidence"),
        wants_report=intent_result.get("wants_report"),
        source=intent_result.get("source"),
    )
    selected_names = intent.select_tool_names(intent_result)
    investigation_tools = [
        t for t in TOOLS if selected_names is None or t["name"] in selected_names
    ]
    # Cache the (stable) tool-definitions block via a breakpoint on the last
    # tool, and the system prompt via a breakpoint at its end. The dynamic
    # per-turn context lives in the user message AFTER this prefix, so the cache
    # stays warm across iterations within the turn.
    all_tools = cache_last_tool(
        investigation_tools + [SUBMIT_SUMMARY_TOOL, SUBMIT_ANSWER_TOOL]
    )
    system_blocks = cached_system(SYSTEM_PROMPT)
    guidance = intent.build_guidance(intent_result)
    if guidance:
        context_block = f"{context_block}\n{guidance}\n"

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": build_turn_message(context_block, user_message, turn_index)}
    ]

    tools_used: list[str] = []
    raw_strong_hits: list[dict[str, Any]] = []
    turn_nodes: list[dict[str, Any]] = []
    turn_edges: list[dict[str, Any]] = []

    submitted_summary: RiskSummary | None = None
    submitted_answer: TurnAnswer | None = None

    try:
        for iteration in range(MAX_ITERATIONS):
            with tracing.span(
                "llm_call",
                conversation_id=conversation_id,
                turn=turn_index,
                iteration=iteration,
                model=model_id,
                message_count=len(messages),
            ) as sp:
                resp = await client.messages.create(
                    model=model_id,
                    max_tokens=MAX_TOKENS_PER_TURN,
                    system=system_blocks,
                    tools=all_tools,
                    messages=messages,
                )
                sp.attach(
                    input_tokens=resp.usage.input_tokens,
                    output_tokens=resp.usage.output_tokens,
                    stop_reason=resp.stop_reason,
                )

            messages.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})

            tool_results_for_next_turn: list[dict[str, Any]] = []
            # Text the model emitted this response. If the response also makes
            # tool calls, this is reasoning narration -> reasoning timeline. If
            # it makes NO tool calls (the model just talked, e.g. answered
            # "hello" directly without submit_answer), this text IS the answer.
            current_text_parts: list[str] = []
            had_tool_use = False

            for block in resp.content:
                if block.type == "text":
                    text = block.text.strip()
                    if text:
                        current_text_parts.append(text)

                elif block.type == "tool_use":
                    had_tool_use = True
                    name = block.name
                    args = block.input or {}

                    if name == "submit_summary":
                        try:
                            args_with_used = dict(args)
                            if not args_with_used.get("tools_used"):
                                args_with_used["tools_used"] = sorted(set(tools_used))
                            submitted_summary = RiskSummary(**args_with_used)
                        except ValidationError as e:
                            log.warning(
                                "terminator validation failed name=%s errors=%s",
                                "submit_summary", e.errors(),
                            )
                            tool_results_for_next_turn.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": _terminator_retry_content(
                                    "submit_summary", e, resp.stop_reason
                                ),
                                "is_error": True,
                            })
                            await _emit_conv(
                                conversation_id, turn_index, "agent_thought",
                                text="(validation failed on submit_summary, retrying...)",
                            )
                        continue

                    if name == "submit_answer":
                        try:
                            args_with_used = dict(args)
                            if not args_with_used.get("tools_used"):
                                args_with_used["tools_used"] = sorted(set(tools_used))
                            submitted_answer = TurnAnswer(**args_with_used)
                        except ValidationError as e:
                            log.warning(
                                "terminator validation failed name=%s errors=%s",
                                "submit_answer", e.errors(),
                            )
                            tool_results_for_next_turn.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": _terminator_retry_content(
                                    "submit_answer", e, resp.stop_reason
                                ),
                                "is_error": True,
                            })
                            await _emit_conv(
                                conversation_id, turn_index, "agent_thought",
                                text="(validation failed on submit_answer, retrying...)",
                            )
                        continue

                    tools_used.append(name)
                    await _emit_conv(
                        conversation_id, turn_index, "tool_call_start",
                        tool=name, args=args, call_id=block.id,
                    )

                    with tracing.span(
                        "tool_call", conversation_id=conversation_id, tool=name, args=args
                    ) as sp:
                        result_json = await execute_tool(name, args)
                        sp.attach(result_size=len(result_json))

                    try:
                        parsed = json.loads(result_json)
                    except json.JSONDecodeError:
                        parsed = {}

                    graph_nodes, graph_edges = graph_payload(name, parsed)
                    turn_nodes.extend(graph_nodes)
                    turn_edges.extend(graph_edges)

                    await _emit_conv(
                        conversation_id, turn_index, "tool_call_result",
                        call_id=block.id,
                        tool=name,
                        nodes=graph_nodes,
                        edges=graph_edges,
                        metadata=parsed.get("metadata", {}),
                        summary=short_summary(name, parsed),
                    )

                    if name == "check_sanctions" and parsed.get("any_strong_match"):
                        await _emit_conv(
                            conversation_id, turn_index, "sanctions_hit",
                            name=parsed.get("name_searched"),
                            hits=parsed.get("hits", []),
                        )
                        for hit in parsed.get("hits", []):
                            try:
                                sh = SanctionsHit(**hit)
                            except Exception:
                                continue
                            if sanctions.is_strong_match(sh):
                                raw_strong_hits.append(sh.model_dump())

                    tool_results_for_next_turn.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        # Slimmed for the model; UI got full nodes via the event.
                        "content": (
                            json.dumps(slim_result_for_model(parsed), default=str)
                            if parsed else result_json
                        ),
                    })

            # When the model made tool calls, any text it produced is reasoning
            # narration — surface it in the reasoning timeline.
            if had_tool_use:
                for t in current_text_parts:
                    await _emit_conv(conversation_id, turn_index, "agent_thought", text=t)

            # ----- Terminate or continue -----
            if submitted_summary is not None or submitted_answer is not None:
                break

            if tool_results_for_next_turn:
                # Soft per-turn tool budget: once crossed, append a wrap-up nudge
                # so "answer anything" can't explode the call count.
                nudge = budget_nudge(len(tools_used))
                if nudge:
                    tool_results_for_next_turn.append({"type": "text", "text": nudge})
                messages.append({"role": "user", "content": tool_results_for_next_turn})
                continue

            # No tool calls and no terminator: the model just talked (e.g. a
            # greeting or a direct clarification). Treat its text as the answer
            # so it lands in the response card, not the reasoning timeline.
            answer_text = "\n\n".join(current_text_parts).strip()
            submitted_answer = TurnAnswer(
                answer=answer_text
                or (
                    "I wasn't able to produce a structured response for that. "
                    "Try naming a specific person or company to investigate."
                ),
                tools_used=sorted(set(tools_used)),
            )
            break
        else:
            await _emit_conv(
                conversation_id, turn_index, "error",
                message=f"Turn exceeded {MAX_ITERATIONS} iterations without convergence.",
            )
            await conversations.set_state(conversation_id, "error")
            return

    except Exception as e:
        log.exception("turn_failed", extra={"conversation_id": conversation_id})
        await _emit_conv(conversation_id, turn_index, "error", message=f"agent failed: {e}")
        await conversations.set_state(conversation_id, "error")
        tracing.log_event("turn_failed", conversation_id=conversation_id, error=str(e))
        return

    # ----- Persist graph delta (everything the agent traversed this turn) -----
    if turn_nodes or turn_edges:
        graph = await conversations.merge_graph(conversation_id, turn_nodes, turn_edges)

    # ----- Emit + persist the terminator result -----
    digest: str
    if submitted_summary is not None:
        await _emit_sanctions_review_conv(conversation_id, turn_index, submitted_summary, raw_strong_hits)
        await _emit_conv(conversation_id, turn_index, "summary", summary=submitted_summary.model_dump())
        await conversations.append_summary(conversation_id, submitted_summary.model_dump())
        await conversations.append_turn(conversation_id, {
            "turn_index": turn_index, "kind": "investigation",
            "user_message": user_message, "entity_name": submitted_summary.entity_name,
        })
        digest = digest_summary(turn_index, submitted_summary)
    else:
        assert submitted_answer is not None
        await _emit_conv(conversation_id, turn_index, "answer", answer=submitted_answer.model_dump())
        await conversations.append_answer(conversation_id, submitted_answer.model_dump())
        await conversations.append_turn(conversation_id, {
            "turn_index": turn_index, "kind": "answer",
            "user_message": user_message,
            "offer_risk_report": submitted_answer.offer_risk_report,
        })
        digest = digest_answer(turn_index, user_message, submitted_answer)

    # ----- L2 EPISODIC (doc 09 Phase D): mirror the graph finalize -----
    # ADD one structured episode per turn for fuzzy recall of OLD turns. Built
    # from the SAME deterministic structured projection the graph path uses
    # (never the prose answer). A graceful NO-OP unless provisioned + flag-on, so
    # the legacy native loop is unaffected when episodic is disabled (the default).
    if episodic.is_enabled():
        from app.agent_graph import _build_state_delta

        episode_state: dict[str, Any] = {
            "turn_index": turn_index,
            "user_message": user_message,
            "intent": intent_result.get("intent"),
            "pinned_node_ids": pinned_node_ids,
            "turn_nodes": turn_nodes,
            "turn_leads": [],  # native loop doesn't accumulate structured leads
            "raw_strong_hits": raw_strong_hits,
        }
        delta = _build_state_delta(episode_state, submitted_summary, submitted_answer)
        episode = episodic.build_episode(
            conversation_id, turn_index, intent_result.get("intent"),
            delta, sorted(set(tools_used)),
        )
        await episodic.write_episode(episode)

    # ----- Update compressed episodic context -----
    new_context = (context + "\n" + digest).strip() if context else digest
    await conversations.set_context(conversation_id, new_context)
    title = submitted_summary.entity_name if submitted_summary else user_message[:60]
    await conversations.bump_meta(conversation_id, title=title)

    await conversations.set_state(conversation_id, "idle")
    await _emit_conv(conversation_id, turn_index, "done")
    tracing.log_event(
        "turn_complete",
        conversation_id=conversation_id,
        turn=turn_index,
        tools_used=sorted(set(tools_used)),
    )


async def _emit_sanctions_review_conv(
    conversation_id: str,
    turn_index: int,
    summary: RiskSummary,
    raw_strong_hits: list[dict[str, Any]],
) -> None:
    """Emit a sanctions-review event for a conversation turn, if there's a gap to flag."""
    review = build_sanctions_review(summary, raw_strong_hits)
    if review is None:
        return
    await _emit_conv(conversation_id, turn_index, "sanctions_review", review=review)
