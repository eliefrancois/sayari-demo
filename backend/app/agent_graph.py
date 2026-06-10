"""Phase 5 agent: idiomatic LangChain + LangGraph implementation.

This is the same agent as `agent_native.run_turn`, expressed as a graph. The
native loop's shape — call model, parse tool calls, run tools, append results,
repeat until a terminator (or bare text) — maps onto three nodes:

    START -> agent --(tool_calls)--> tools --(more tools)--> agent
                  \\--(text only)---------------------------\\
                                                             v
             tools --(terminator)--> finalize -> END   <----/

Two design choices keep this 100% compatible with the existing frontend:

  1. The model call is pure LangChain (`ChatAnthropic.bind_tools`), so LangSmith
     traces every node + LLM call for free once LANGCHAIN_TRACING_V2 is set.
  2. The `tools` node is a CUSTOM node (not the prebuilt `ToolNode`) because it
     has to do more than execute functions: it emits our SSE event contract
     (tool_call_start / tool_call_result / sanctions_hit) and accumulates the
     graph delta + raw watchlist hits. Custom nodes are standard LangGraph.

Everything that isn't control flow (context block, digests, sanctions review,
graph suppression) is imported from agent_common, so native and graph never
drift. The public `run_turn` signature matches agent_native.run_turn exactly,
so the facade can swap them behind the AGENT_IMPL flag.
"""

from __future__ import annotations

import json
import logging
import operator
import re
from typing import Annotated, Any, TypedDict

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import ValidationError

from app import conversations, episodic, intent, sanctions, tracing
from app.agent_common import (
    MAX_TOKENS_PER_TURN,
    MODEL,
    bound_context_digest,
    budget_nudge,
    build_context_block,
    build_followup_prefetch,
    build_sanctions_review,
    build_turn_message,
    digest_answer,
    digest_summary,
    graph_payload,
    short_summary,
    slim_result_for_model,
)
from app.config import get_settings
from app.prompts import SYSTEM_PROMPT
from app.schema import RiskSummary, SanctionsHit, TurnAnswer
from app.tools import execute_tool
from app.tools_lc import tools_for

log = logging.getLogger("erre.agent_graph")

# Plenty of headroom: an investigation is ~6-12 tool calls = ~12-24 graph steps.
# Each agent<->tools cycle is 2 steps; 60 covers the MAX_ITERATIONS=20 budget.
_RECURSION_LIMIT = 60

_FALLBACK_ANSWER = (
    "I wasn't able to produce a structured response for that. "
    "Try naming a specific person or company to investigate."
)


# --- Graph state -----------------------------------------------------------
# List fields use operator.add so each node returns only ITS delta and LangGraph
# concatenates across the agent<->tools loop. `messages` uses add_messages (the
# message-aware reducer). Scalars use the default replace semantics.


class TurnState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    conversation_id: str
    turn_index: int
    # Branching (Stage 2a): the turn's tree coordinates. None on the eval path
    # and for the legacy native-compatible call shape; when set, the whole turn
    # runs inside conversations.turn_scope so every state read is path-scoped.
    turn_id: str | None
    parent_turn_id: str | None
    user_message: str
    prior_context: str
    persist: bool  # True for live turns (emit SSE + write Redis); False for evals
    # Intent-router's selected investigation-tool subset (None = full toolset).
    tool_names: list[str] | None
    # Intent label from the router (for the state_doc turn_log). None on fallback.
    intent: str | None
    # Node ids the user explicitly pinned for this turn (fed into state_doc).
    pinned_node_ids: list[str]
    tools_used: Annotated[list[str], operator.add]
    turn_nodes: Annotated[list[dict], operator.add]
    turn_edges: Annotated[list[dict], operator.add]
    # FULL sayari_search lead lists this turn (not just the pinned subset),
    # stamped with from_turn/from_query — the source for state_doc["leads"].
    turn_leads: Annotated[list[dict], operator.add]
    raw_strong_hits: Annotated[list[dict], operator.add]
    result_summary: RiskSummary | None
    result_answer: TurnAnswer | None
    terminated: bool


# --- Model + graph singletons ---------------------------------------------

_LLM: Any = None
_BOUND_CACHE: dict[Any, Any] = {}
_COMPILED: Any = None


def _base_llm():
    global _LLM
    if _LLM is None:
        _LLM = ChatAnthropic(
            model=MODEL,
            max_tokens=MAX_TOKENS_PER_TURN,
            api_key=get_settings().anthropic_api_key,
            timeout=120,
            # The Anthropic SDK honors the Retry-After header on 429s, so a
            # transient per-minute rate-limit hit self-heals instead of killing
            # the turn. Bumped above the default 2 for long investigations.
            max_retries=6,
        )
    return _LLM


def _bound_llm(tool_names: list[str] | None = None):
    """The model bound to the turn's tool subset (intent-router narrowed), or the
    full toolset when tool_names is None. Cached per distinct subset so we don't
    rebuild the binding every node call."""
    key = frozenset(tool_names) if tool_names else None
    if key not in _BOUND_CACHE:
        _BOUND_CACHE[key] = _base_llm().bind_tools(
            tools_for(set(tool_names) if tool_names else None)
        )
    return _BOUND_CACHE[key]


# --- Helpers ---------------------------------------------------------------


async def _emit(conversation_id: str, turn_index: int, type_: str, **data: Any) -> None:
    payload = dict(data)
    payload["turn_index"] = turn_index
    await conversations.append_event(conversation_id, {"type": type_, "data": payload})


def _ai_text(msg: AnyMessage) -> str:
    """Plain text from an AIMessage whose content may be a string or a list of
    content blocks (ChatAnthropic returns blocks when tool calls are present)."""
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content.strip()
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(p for p in parts if p).strip()


def _chunk_text(chunk: AnyMessage) -> str:
    """Incremental text from a streaming AIMessageChunk (string or text blocks)."""
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif isinstance(block, str):
            parts.append(block)
    return "".join(parts)


# The terminator field whose value we stream as the user-facing answer text.
_TERMINATOR_TEXT_FIELD = {
    "submit_answer": "answer",
    "submit_summary": "investigation_summary",
}

_STREAM_FLUSH_CHARS = 48  # batch text deltas to keep Redis writes reasonable


def _terminator_text_field(name: str | None) -> str | None:
    return _TERMINATOR_TEXT_FIELD.get(name or "")


def _growing_string_value(partial_json: str, key: str) -> str:
    """Best-effort current value of a string field in a STILL-STREAMING JSON
    args blob. Reads from `"key":"` up to the first unescaped quote (or the end
    of the buffer if the value isn't closed yet), decoding common escapes. Lets
    us stream a terminator's `answer`/`investigation_summary` as it's generated
    without waiting for the whole tool-call JSON to complete."""
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*"', partial_json)
    if not m:
        return ""
    i = m.end()
    out: list[str] = []
    n = len(partial_json)
    while i < n:
        ch = partial_json[i]
        if ch == "\\":
            if i + 1 >= n:
                break  # incomplete escape at buffer edge; stop here
            nxt = partial_json[i + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}.get(nxt, nxt))
            i += 2
            continue
        if ch == '"':
            break  # closing quote -> value complete
        out.append(ch)
        i += 1
    return "".join(out)


def _validate_terminator(name: str, args: dict[str, Any], tools_used: list[str]):
    """Build a RiskSummary or TurnAnswer from a terminator's args, defaulting
    tools_used if the model omitted it. Raises ValidationError on bad shape."""
    payload = dict(args)
    if not payload.get("tools_used"):
        payload["tools_used"] = sorted(set(tools_used))
    if name == "submit_summary":
        return RiskSummary(**payload)
    return TurnAnswer(**payload)


# --- Nodes -----------------------------------------------------------------


async def agent_node(state: TurnState) -> dict[str, Any]:
    """Call the model with the full message history; append its reply.

    When persisting (a live turn) and streaming is enabled, consume the model
    via `astream` and emit batched `token` events so the UI types the agent's
    text out live — both reasoning narration and the terminator's narrative
    (`answer` / `investigation_summary`). The accumulated chunks reconstruct the
    exact same final AIMessage `ainvoke` would have produced, so the rest of the
    graph is unaffected. Evals (persist=False) use the cheaper `ainvoke`."""
    cid = state["conversation_id"]
    ti = state["turn_index"]
    stream = state["persist"] and get_settings().stream_tokens
    llm = _bound_llm(state.get("tool_names"))

    with tracing.span(
        "llm_call", conversation_id=cid, turn=ti, model=MODEL,
        message_count=len(state["messages"]),
    ) as sp:
        if not stream:
            ai: AIMessage = await llm.ainvoke(state["messages"])
            usage = ai.usage_metadata or {}
            sp.attach(
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                tool_calls=len(ai.tool_calls or []),
            )
            return {"messages": [ai]}

        gathered: Any = None
        buf: list[str] = []
        pending = 0
        arg_bufs: dict[int, str] = {}
        arg_names: dict[int, str] = {}
        arg_emitted: dict[int, int] = {}

        async def push(s: str, *, force: bool = False) -> None:
            """Buffer a text delta; flush to one SSE token event once batched."""
            nonlocal pending
            if s:
                buf.append(s)
                pending += len(s)
            if buf and (force or pending >= _STREAM_FLUSH_CHARS):
                await _emit(cid, ti, "token", delta="".join(buf))
                buf.clear()
                pending = 0

        async for chunk in llm.astream(state["messages"]):
            gathered = chunk if gathered is None else gathered + chunk

            await push(_chunk_text(chunk))

            # Stream the terminator's narrative field out of its partial args.
            for tcc in chunk.tool_call_chunks or []:
                idx = tcc.get("index") or 0
                if tcc.get("name"):
                    arg_names[idx] = tcc["name"]
                if tcc.get("args"):
                    arg_bufs[idx] = arg_bufs.get(idx, "") + tcc["args"]
                    field = _terminator_text_field(arg_names.get(idx))
                    if field:
                        val = _growing_string_value(arg_bufs[idx], field)
                        already = arg_emitted.get(idx, 0)
                        if len(val) > already:
                            await push(val[already:])
                            arg_emitted[idx] = len(val)

        await push("", force=True)
        usage = getattr(gathered, "usage_metadata", None) or {}
        sp.attach(
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            tool_calls=len(getattr(gathered, "tool_calls", None) or []),
        )
    return {"messages": [gathered]}


# Tools whose single subject is the entity they were called on. shortest_path
# is handled separately (two subjects). Tools that don't produce subject-owned
# graph nodes (search leads, resolution, memory reads) are intentionally absent
# so their nodes stay ungrouped.
_SINGLE_SUBJECT_TOOLS = frozenset({
    "sayari_profile",
    "sayari_summary",
    "sayari_ownership",
    "sayari_watchlist",
    "sayari_trade",
})


def _tag_subject_membership(
    name: str,
    args: dict[str, Any],
    nodes: list[dict[str, Any]],
    turn_id: str | None,
) -> None:
    """Stamp `subject_ids` + `introduced_turn_id` onto a tool's emitted nodes.

    shortest_path is the multi-subject case: the source node belongs to subject
    A, the target node to subject B, and every intermediate to BOTH — so a
    shared intermediary settles in the A∩B overlap and both hulls enclose it.
    Single-subject tools attribute every node to the entity they were called on.
    Membership is unioned across turns by merge_graph_pure's id-keyed dedupe."""
    if name == "sayari_shortest_path":
        src = args.get("source_id")
        tgt = args.get("target_id")
        both = [s for s in (src, tgt) if s]
        for node in nodes:
            nid = node.get("id")
            if nid == src and src:
                subjects = [src]
            elif nid == tgt and tgt:
                subjects = [tgt]
            else:
                subjects = both
            node["subject_ids"] = subjects
            if turn_id and not node.get("introduced_turn_id"):
                node["introduced_turn_id"] = turn_id
        return
    if name in _SINGLE_SUBJECT_TOOLS:
        eid = args.get("entity_id")
        subjects = [eid] if eid else []
        for node in nodes:
            node["subject_ids"] = subjects
            if turn_id and not node.get("introduced_turn_id"):
                node["introduced_turn_id"] = turn_id


async def tools_node(state: TurnState) -> dict[str, Any]:
    """Execute the tool calls on the latest AIMessage.

    - Investigation tools: run via execute_tool, emit SSE events, accumulate the
      graph delta + raw strong watchlist hits.
    - Terminators (submit_summary / submit_answer): validate into a Pydantic
      result and flag termination. Validation errors go back as an error
      ToolMessage so the model can retry.
    Every tool_call gets a matching ToolMessage so the message history stays
    valid for the next agent turn.
    """
    cid = state["conversation_id"]
    ti = state["turn_index"]
    persist = state["persist"]
    last = state["messages"][-1]

    # Text the model emitted alongside tool calls is reasoning narration.
    narration = _ai_text(last)
    if narration and persist:
        await _emit(cid, ti, "agent_thought", text=narration)

    tool_messages: list[ToolMessage] = []
    used: list[str] = []
    new_nodes: list[dict] = []
    new_edges: list[dict] = []
    new_leads: list[dict] = []
    new_strong: list[dict] = []
    result_summary: RiskSummary | None = None
    result_answer: TurnAnswer | None = None
    terminated = False

    for tc in last.tool_calls or []:
        name = tc["name"]
        args = tc.get("args") or {}
        call_id = tc["id"]

        if name in ("submit_summary", "submit_answer"):
            try:
                # tools_used so far = prior nodes (state) + this node (used)
                result = _validate_terminator(name, args, state["tools_used"] + used)
                if isinstance(result, RiskSummary):
                    result_summary = result
                else:
                    result_answer = result
                terminated = True
                tool_messages.append(ToolMessage(content="ok", tool_call_id=call_id))
            except ValidationError as e:
                # Server-side breadcrumb so the next failure is diagnosable from
                # logs alone (pure logging, no behavior change).
                log.warning(
                    "terminator validation failed name=%s errors=%s",
                    name, e.errors(),
                )
                # Truncation-aware retry: if the model hit the output ceiling, its
                # tool-call args came back cut off (unparseable / missing fields).
                # Feeding back the giant e.errors() blob just grows the input and
                # reinforces the loop, so send a SHORT targeted nudge to emit a
                # smaller terminator instead. Otherwise (a genuine shape error),
                # the structured errors are the useful signal — keep them.
                truncated = (
                    (getattr(last, "response_metadata", None) or {}).get("stop_reason")
                    == "max_tokens"
                )
                if truncated:
                    err_content = (
                        f"Your previous {name} was CUT OFF at the output token limit, "
                        "so its arguments were truncated and could not be parsed. "
                        "Re-emit it COMPLETE but SHORTER: keep only the most important "
                        "claims (fewer, terser), trim long narrative text, and make sure "
                        "the tool-call JSON is fully closed."
                    )
                else:
                    err_content = json.dumps({"validation_error": e.errors()}, default=str)
                tool_messages.append(
                    ToolMessage(content=err_content, tool_call_id=call_id, status="error")
                )
                if persist:
                    await _emit(
                        cid, ti, "agent_thought",
                        text=(
                            f"(previous {name} was cut off at the output limit, "
                            "retrying shorter...)"
                            if truncated
                            else f"(validation failed on {name}, retrying...)"
                        ),
                    )
            continue

        # --- Regular investigation tool ---
        used.append(name)
        if persist:
            await _emit(cid, ti, "tool_call_start", tool=name, args=args, call_id=call_id)

        with tracing.span("tool_call", conversation_id=cid, tool=name, args=args) as sp:
            # conversation_id is injected for the memory tools (recall_state) and
            # ignored by everything else; it is NOT in the model-visible schema.
            result_json = await execute_tool(name, args, conversation_id=cid)
            sp.attach(result_size=len(result_json))

        try:
            parsed = json.loads(result_json)
        except json.JSONDecodeError:
            parsed = {}

        gnodes, gedges = graph_payload(name, parsed)
        # Subject-membership provenance (Phase 1): attribute each emitted node to
        # the resolved subject(s) of the tool call that produced it, so the
        # frontend can group nodes into per-subject hull regions and place
        # shared nodes in the overlap. shortest_path is the multi-subject case:
        # the source belongs to A, the target to B, and every INTERMEDIATE to
        # BOTH (that's what puts e.g. Kerimov in the Gazprom-Roldugin
        # intersection). Single-subject tools attribute every node to their
        # entity_id. The id-keyed dedupe in merge_graph_pure unions these across
        # turns. Tagging here (where the tool args are in hand) is the precise
        # per-tool attribution the plan's turn-level finalize sketch approximates.
        _tag_subject_membership(name, args, gnodes, state.get("turn_id"))
        new_nodes.extend(gnodes)
        new_edges.extend(gedges)

        # Capture the FULL sayari_search lead list (the model already parsed it
        # here) into turn_leads, stamped with provenance, so finalize_node can
        # persist every lead (not just the pinned top-N) into state_doc.
        if name == "sayari_search":
            for cand in parsed.get("candidates", []) or []:
                if not isinstance(cand, dict):
                    continue
                lead = dict(cand)
                lead["from_turn"] = ti
                lead["from_query"] = args.get("query")
                new_leads.append(lead)

        if persist:
            # Additive extra: the full broad-search lead set (pinned + unpinned)
            # rides along so the UI can overlay the unpinned leads on demand.
            # These do NOT enter `nodes`/merge_graph, so the persistent graph
            # still only gains the pinned top-N. Other consumers ignore the field.
            extra: dict[str, Any] = {}
            if name == "sayari_search":
                extra["all_lead_nodes"] = parsed.get("all_lead_nodes") or []
            await _emit(
                cid, ti, "tool_call_result",
                call_id=call_id,
                tool=name,
                nodes=gnodes,
                edges=gedges,
                metadata=parsed.get("metadata", {}),
                summary=short_summary(name, parsed),
                **extra,
            )

        if name == "check_sanctions" and parsed.get("any_strong_match"):
            if persist:
                await _emit(
                    cid, ti, "sanctions_hit",
                    name=parsed.get("name_searched"),
                    hits=parsed.get("hits", []),
                )
            for hit in parsed.get("hits", []):
                try:
                    sh = SanctionsHit(**hit)
                except Exception:
                    continue
                if sanctions.is_strong_match(sh):
                    new_strong.append(sh.model_dump())

        # Send the model a slimmed result (identity + key props); the UI already
        # got the full nodes via the tool_call_result event above. This keeps the
        # re-sent message history small enough to avoid rate-limit blowups.
        model_content = (
            json.dumps(slim_result_for_model(parsed), default=str) if parsed else result_json
        )
        tool_messages.append(ToolMessage(content=model_content, tool_call_id=call_id))

    # Soft per-turn tool budget: once crossed, append a wrap-up nudge to the last
    # tool result so "answer anything" can't explode the call count. (No-op when
    # the only tool calls this step were terminators -> tool_messages may be the
    # "ok" ack, which is fine to annotate.)
    nudge = budget_nudge(len(state["tools_used"]) + len(used))
    if nudge and tool_messages:
        last = tool_messages[-1]
        last.content = f"{last.content}\n\n{nudge}"

    return {
        "messages": tool_messages,
        "tools_used": used,
        "turn_nodes": new_nodes,
        "turn_edges": new_edges,
        "turn_leads": new_leads,
        "raw_strong_hits": new_strong,
        "result_summary": result_summary,
        "result_answer": result_answer,
        "terminated": terminated,
    }


def _in_hand_identity_index(state: TurnState) -> dict[str, dict[str, Any]]:
    """id -> identity {label, type, sanctioned, pep, countries} built ONLY from
    this turn's STRUCTURED tool outputs: the traversed graph nodes and the full
    search-lead lists. Used to name (and only then persist) the entity ids the
    agent references through structured terminator fields (gap b). No prose
    parsing and no extra calls — every entry traces to data captured in
    tools_node. A traversed node wins over a lead carrying the same id."""
    idx: dict[str, dict[str, Any]] = {}
    for n in state["turn_nodes"]:
        if not isinstance(n, dict):
            continue
        nid = n.get("id")
        nm = n.get("name")
        if not nid or not nm:
            continue
        props = n.get("properties") or {}
        idx[nid] = {
            "label": nm,
            "type": (n.get("label") or "").lower() or None,
            "sanctioned": bool(props.get("sanctioned")),
            "pep": bool(props.get("pep")),
            "countries": props.get("countries") or props.get("country_codes") or [],
        }
    for lead in state["turn_leads"]:
        if not isinstance(lead, dict):
            continue
        eid = lead.get("entity_id")
        if not eid or not lead.get("label"):
            continue
        idx.setdefault(eid, {
            "label": lead.get("label"),
            "type": lead.get("type"),
            "sanctioned": bool(lead.get("sanctioned")),
            "pep": bool(lead.get("pep")),
            "countries": lead.get("countries") or [],
        })
    return idx


def _referenced_entity_ids(
    summary: RiskSummary | None,
    answer: TurnAnswer | None,
) -> set[str]:
    """Entity ids the agent named through the typed terminator SCHEMA:
    TurnAnswer.referenced_node_ids, claim source_refs (node_id / sayari_entity_id),
    and the ids embedded in sayari_risk_factors traversal paths. This NEVER reads
    the prose `answer` string (the HaluMem / hallucinated-write trap) — only
    validated schema fields. sanctions_ids are deliberately excluded; those are
    watchlist rows, not entities."""
    ids: set[str] = set()
    for t in (summary, answer):
        if t is None:
            continue
        for c in t.claims:
            for ref in c.source_refs:
                if ref.node_id:
                    ids.add(ref.node_id)
                if ref.sayari_entity_id:
                    ids.add(ref.sayari_entity_id)
        for f in t.sayari_risk_factors:
            for seg in f.path or []:
                # traversal_path segments look like `srcId|rel|tgtId|rel|...`;
                # the pipe-delimited tokens that are entity ids match the in-hand
                # index, and the relationship tokens simply won't (filtered there).
                for tok in str(seg).split("|"):
                    tok = tok.strip()
                    if tok:
                        ids.add(tok)
    if answer is not None:
        for rid in answer.referenced_node_ids:
            if rid:
                ids.add(rid)
    return ids


def _build_state_delta(
    state: TurnState,
    summary: RiskSummary | None,
    answer: TurnAnswer | None,
) -> dict[str, Any]:
    """Build the structured state_doc delta for this turn from data already in
    hand — no extra model/tool calls. Resolved entities come from the traversed
    nodes + the summary's primary subject + the entities the agent named through
    structured terminator fields (gap b); leads from turn_leads; sanctions
    verdicts (confirmed AND dismissed, on BOTH turn types — gap a) from
    build_sanctions_review; pinned ids from the turn + pinned leads + the
    resolved subject; one turn_log row."""
    ti = state["turn_index"]
    user_message = state["user_message"]

    # Resolved entities: traversed nodes (data-driven) + the summary primary.
    resolved: dict[str, dict[str, Any]] = {}
    for n in state["turn_nodes"]:
        if not isinstance(n, dict):
            continue
        nm = n.get("name")
        if not nm:
            continue
        props = n.get("properties") or {}
        resolved[nm] = {
            "entity_id": n.get("id"),
            "label": nm,
            "type": (n.get("label") or "").lower() or None,
            "source": n.get("source_system") or "sayari",
            "sanctioned": bool(props.get("sanctioned")),
            "pep": bool(props.get("pep")),
            "first_seen_turn": ti,
            "last_seen_turn": ti,
        }
    if summary is not None and summary.entity_name:
        sanctioned = ("sanctioned" in summary.risk_signals) or bool(summary.sanctions_hits)
        rec = dict(resolved.get(summary.entity_name, {}))
        rec.update({
            "entity_id": summary.entity_id or rec.get("entity_id"),
            "label": summary.entity_name,
            "type": rec.get("type"),
            "source": rec.get("source", "sayari"),
            "sanctioned": sanctioned or bool(rec.get("sanctioned")),
            "pep": bool(rec.get("pep")),
            "first_seen_turn": rec.get("first_seen_turn", ti),
            "last_seen_turn": ti,
        })
        resolved[summary.entity_name] = rec

    # Gap (b): entities the agent named through STRUCTURED terminator fields
    # (referenced_node_ids, claim source_refs, risk-path ids) that we can name
    # from THIS turn's tool outputs but that never landed as a traversed node.
    # Deposit them so an id the agent clearly leaned on survives to recall.
    # Ids we can't name in hand are left for the bounded resolver (no new calls).
    id_index = _in_hand_identity_index(state)
    already = {r.get("entity_id") for r in resolved.values()}
    named_ids: dict[str, dict[str, Any]] = {}
    for rid in _referenced_entity_ids(summary, answer):
        ident = id_index.get(rid)
        if not ident or rid in already:
            continue
        label = ident.get("label")
        named_ids[rid] = {
            "label": label,
            "type": ident.get("type"),
            "sanctioned": bool(ident.get("sanctioned")),
            "pep": bool(ident.get("pep")),
            "countries": ident.get("countries") or [],
        }
        if label and label not in resolved:
            resolved[label] = {
                "entity_id": rid,
                "label": label,
                "type": ident.get("type"),
                "source": "referenced",
                "sanctioned": bool(ident.get("sanctioned")),
                "pep": bool(ident.get("pep")),
                "first_seen_turn": ti,
                "last_seen_turn": ti,
            }

    # Sanctions adjudicated: confirmed = the hits the agent kept in its
    # terminator (both RiskSummary and TurnAnswer carry sanctions_hits);
    # dismissed = strong check_sanctions matches this turn it did NOT keep
    # (name collisions). Gap (a): capturing the dismissed set on ANSWER turns
    # too — previously only investigation turns did, so a dismissed subsidiary
    # surfaced on an answer turn (the common conversational-default path)
    # vanished by the next turn. build_sanctions_review handles both shapes.
    sanc_rows: list[dict[str, Any]] = []
    terminator = summary if summary is not None else answer
    if terminator is not None:
        review = build_sanctions_review(terminator, state["raw_strong_hits"])
        dismissed = review["dismissed"] if review is not None else []
        for h in terminator.sanctions_hits:
            sanc_rows.append({
                "sanctions_id": h.sanctions_id,
                "matched_name": h.matched_name,
                "lists": h.lists,
                # countries enrich the registry entity this row deposits (the
                # matched sanctioned entity becomes a first-class registry row).
                "countries": h.countries or [],
                "verdict": "confirmed",
                "from_turn": ti,
            })
        for h in dismissed:
            sanc_rows.append({
                "sanctions_id": h.get("sanctions_id"),
                "matched_name": h.get("matched_name"),
                "lists": h.get("lists", []),
                "countries": h.get("countries") or [],
                "verdict": "dismissed",
                "from_turn": ti,
            })

    # Pinned node ids: the turn's pins + pinned leads + the resolved subject.
    pinned_ids: list[str] = list(state.get("pinned_node_ids") or [])
    for lead in state["turn_leads"]:
        if isinstance(lead, dict) and lead.get("pinned_to_graph") and lead.get("entity_id"):
            pinned_ids.append(lead["entity_id"])
    if summary is not None and summary.entity_id:
        pinned_ids.append(summary.entity_id)

    # Structured claims (doc 09 §5): the typed terminator's claims, with the
    # entity_ids their source_refs resolve to. Structured-only — never the prose
    # `answer` string (the HaluMem / hallucinated-write trap).
    claim_rows: list[dict[str, Any]] = []
    if terminator is not None:
        for c in terminator.claims:
            ent_ids: list[str] = []
            for ref in c.source_refs:
                for cid in (ref.node_id, ref.sanctions_id, ref.sayari_entity_id):
                    if cid and cid not in ent_ids:
                        ent_ids.append(cid)
            claim_rows.append({
                "text": c.text,
                "confidence": c.confidence,
                "source_refs": [r.model_dump() for r in c.source_refs],
                "entity_ids": ent_ids,
                "from_turn": ti,
            })

    subject = summary.entity_name if summary is not None else (user_message[:80] or None)
    turn_log_row = {
        "turn": ti,
        "intent": state.get("intent"),
        "subject": subject,
        "kind": "investigation" if summary is not None else "answer",
    }

    return {
        "resolved_entities": resolved,
        "leads": list(state["turn_leads"]),
        "sanctions_adjudicated": sanc_rows,
        "pinned_node_ids": pinned_ids,
        "turn_log": [turn_log_row],
        "named_ids": named_ids,
        "claims": claim_rows,
    }


async def finalize_node(state: TurnState) -> dict[str, Any]:
    """Persist + emit the terminator result. Reuses the tail of the native
    run_turn so the stored shape (summaries/answers/graph/context/meta) and the
    emitted SSE events are identical between impls."""
    cid = state["conversation_id"]
    ti = state["turn_index"]
    turn_id = state.get("turn_id")
    parent_turn_id = state.get("parent_turn_id")
    persist = state["persist"]
    user_message = state["user_message"]

    summary = state.get("result_summary")
    answer = state.get("result_answer")

    # Bare-text path (the "hello" fix): the model replied with text and no tool
    # calls. Treat that text as the answer so it lands in the response card.
    if summary is None and answer is None:
        text = _ai_text(state["messages"][-1])
        answer = TurnAnswer(
            answer=text or _FALLBACK_ANSWER,
            tools_used=sorted(set(state["tools_used"])),
        )

    if not persist:
        # Eval mode: skip all side effects; just surface the result in state.
        return {"result_answer": answer} if summary is None else {}

    # Persist the graph delta the agent traversed this turn.
    if state["turn_nodes"] or state["turn_edges"]:
        await conversations.merge_graph(cid, state["turn_nodes"], state["turn_edges"])
    # Branching: the same delta, stored first-class keyed by turn_id, so the
    # path graph (time-travel) can be accumulated per branch.
    if turn_id is not None:
        await conversations.record_turn_graph_delta(
            cid, turn_id, state["turn_nodes"], state["turn_edges"]
        )

    # Persist the structured investigation state (exact recall): resolved
    # entities, the full lead lists, sanctions verdicts, pinned ids, turn log.
    # Deterministic merge from data already in hand — no extra model/tool calls.
    delta = _build_state_delta(state, summary, answer)
    await conversations.merge_state_doc(cid, delta)

    # L2 EPISODIC (doc 09 Phase D): ADD one structured episode per turn for fuzzy
    # recall of OLD turns via recall_memory. Derived from the SAME structured
    # delta (never the prose answer). A graceful NO-OP unless episodic memory is
    # provisioned + flag-enabled, so the live demo is unaffected.
    if episodic.is_enabled():
        episode = episodic.build_episode(
            cid, ti, state.get("intent"), delta, sorted(set(state["tools_used"])),
            turn_id=turn_id, parent_turn_id=parent_turn_id,
        )
        await episodic.write_episode(episode)

    # Tree coordinates ride along on the per-turn metadata (additive fields;
    # consumers that predate branching ignore them).
    tree_fields = (
        {"turn_id": turn_id, "parent_turn_id": parent_turn_id} if turn_id else {}
    )
    if summary is not None:
        review = build_sanctions_review(summary, state["raw_strong_hits"])
        if review is not None:
            await _emit(cid, ti, "sanctions_review", review=review)
        await _emit(cid, ti, "summary", summary=summary.model_dump())
        await conversations.append_summary(cid, summary.model_dump())
        await conversations.append_turn(cid, {
            "turn_index": ti, "kind": "investigation",
            "user_message": user_message, "entity_name": summary.entity_name,
            **tree_fields,
        })
        digest = digest_summary(ti, summary)
        title = summary.entity_name
    else:
        await _emit(cid, ti, "answer", answer=answer.model_dump())
        await conversations.append_answer(cid, answer.model_dump())
        await conversations.append_turn(cid, {
            "turn_index": ti, "kind": "answer",
            "user_message": user_message,
            "offer_risk_report": answer.offer_risk_report,
            **tree_fields,
        })
        digest = digest_answer(ti, user_message, answer)
        title = user_message[:60]

    prior = state["prior_context"]
    new_context = bound_context_digest((prior + "\n" + digest).strip() if prior else digest)
    await conversations.set_context(cid, new_context)
    await conversations.bump_meta(cid, title=title)

    # Close out the tree entry: terminator metadata for the GET tree payload,
    # and `context_after` so a child turn (linear or fork) starts from THIS
    # path's narrative digest. The final state delta was already appended by
    # merge_state_doc above, so flipping status to done here is safe — a done
    # turn's delta list never grows again.
    if turn_id is not None:
        await conversations.update_turn_entry(
            cid, turn_id,
            status="done",
            kind="investigation" if summary is not None else "answer",
            entity_name=summary.entity_name if summary is not None else None,
            report_ready=(
                True if summary is not None else bool(answer.report_ready)
            ),
            offer_risk_report=(
                None if summary is not None else bool(answer.offer_risk_report)
            ),
            context_after=new_context,
        )

    await conversations.set_state(cid, "idle")
    await _emit(cid, ti, "done")
    tracing.log_event(
        "turn_complete",
        conversation_id=cid,
        turn=ti,
        impl="graph",
        tools_used=sorted(set(state["tools_used"])),
    )
    return {"result_summary": summary, "result_answer": answer}


# --- Routing ---------------------------------------------------------------


def _route_after_agent(state: TurnState) -> str:
    """Tool calls -> run them; bare text -> finalize as an answer."""
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return "tools"
    return "finalize"


def _route_after_tools(state: TurnState) -> str:
    """Terminator was called -> finalize; otherwise loop back to the model."""
    return "finalize" if state["terminated"] else "agent"


def _graph():
    global _COMPILED
    if _COMPILED is None:
        builder = StateGraph(TurnState)
        builder.add_node("agent", agent_node)
        builder.add_node("tools", tools_node)
        builder.add_node("finalize", finalize_node)
        builder.add_edge(START, "agent")
        builder.add_conditional_edges(
            "agent", _route_after_agent, {"tools": "tools", "finalize": "finalize"}
        )
        builder.add_conditional_edges(
            "tools", _route_after_tools, {"agent": "agent", "finalize": "finalize"}
        )
        builder.add_edge("finalize", END)
        _COMPILED = builder.compile()
    return _COMPILED


def _initial_state(
    conversation_id: str,
    turn_index: int,
    user_message: str,
    prior_context: str,
    context_block: str,
    persist: bool,
    tool_names: list[str] | None = None,
    intent: str | None = None,
    pinned_node_ids: list[str] | None = None,
    turn_id: str | None = None,
    parent_turn_id: str | None = None,
) -> TurnState:
    return {
        "messages": [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=build_turn_message(context_block, user_message, turn_index)),
        ],
        "conversation_id": conversation_id,
        "turn_index": turn_index,
        "turn_id": turn_id,
        "parent_turn_id": parent_turn_id,
        "user_message": user_message,
        "prior_context": prior_context,
        "persist": persist,
        "tool_names": tool_names,
        "intent": intent,
        "pinned_node_ids": pinned_node_ids or [],
        "tools_used": [],
        "turn_nodes": [],
        "turn_edges": [],
        "turn_leads": [],
        "raw_strong_hits": [],
        "result_summary": None,
        "result_answer": None,
        "terminated": False,
    }


# --- Public API ------------------------------------------------------------


async def _route_turn(
    user_message: str,
    context_block: str,
    prior_context: str,
    *,
    conversation_id: str,
    turn_index: int,
    impl: str,
) -> tuple[str, list[str] | None, str | None]:
    """Run the intent router: log the chosen intent, return the (guidance-
    augmented context_block, selected tool-name subset, intent label). Shared by
    the live and eval entry points so routing behavior is identical. The intent
    label is threaded into state for the state_doc turn_log."""
    result = await intent.classify_intent(user_message, prior_context)
    tracing.log_event(
        "intent_classified",
        conversation_id=conversation_id,
        turn=turn_index,
        impl=impl,
        intent=result.get("intent"),
        confidence=result.get("confidence"),
        wants_report=result.get("wants_report"),
        source=result.get("source"),
    )
    guidance = intent.build_guidance(result)
    if guidance:
        context_block = f"{context_block}\n{guidance}\n"
    selected = intent.select_tool_names(result)
    return context_block, (sorted(selected) if selected else None), result.get("intent")


async def run_turn(
    conversation_id: str,
    user_message: str,
    turn_index: int,
    pinned_node_ids: list[str] | None = None,
    force_risk_report: bool = False,
    turn_id: str | None = None,
    parent_turn_id: str | None = None,
) -> None:
    """Run one conversation turn through the graph. Drop-in replacement for
    agent_native.run_turn — same SSE events and same Redis writes.

    Branching (Stage 2a): when `turn_id` is set (the API registered the turn in
    the tree), the whole turn runs inside `conversations.turn_scope`, which makes
    every state read path-scoped (root -> parent -> this turn; sibling branches
    invisible), records every state delta per-turn, and stamps the tree
    coordinates onto every SSE event. With `turn_id=None` (old call shape) the
    behavior is exactly the pre-branching one."""
    if turn_id is None:
        await _run_turn_scoped(
            conversation_id, user_message, turn_index,
            pinned_node_ids or [], force_risk_report, None, None,
        )
        return
    with conversations.turn_scope(conversation_id, turn_id, parent_turn_id):
        await _run_turn_scoped(
            conversation_id, user_message, turn_index,
            pinned_node_ids or [], force_risk_report, turn_id, parent_turn_id,
        )


async def _run_turn_scoped(
    conversation_id: str,
    user_message: str,
    turn_index: int,
    pinned_node_ids: list[str],
    force_risk_report: bool,
    turn_id: str | None,
    parent_turn_id: str | None,
) -> None:
    await conversations.set_state(conversation_id, "running")
    await _emit(conversation_id, turn_index, "agent_started", input=user_message)
    tracing.log_event(
        "turn_started", conversation_id=conversation_id, turn=turn_index,
        query=user_message, impl="graph",
    )

    if turn_id is not None:
        # Path-scoped context assembly: the prose digest comes from the PARENT
        # turn's stored context_after (never a sibling's), and the graph is the
        # accumulation along this turn's path. For a linear conversation both
        # are byte-identical to the legacy global reads.
        context = await conversations.resolve_prior_context(
            conversation_id, parent_turn_id
        )
        path_graph = await conversations.get_path_graph(conversation_id, turn_id)
        graph = (
            path_graph["graph"] if path_graph is not None
            else await conversations.get_graph(conversation_id)
        )
    else:
        context = await conversations.get_context(conversation_id)
        graph = await conversations.get_graph(conversation_id)
    # Path-scoped automatically when the turn scope is active.
    state_doc = await conversations.get_state_doc(conversation_id)
    context_block = build_context_block(
        context, graph, pinned_node_ids, force_risk_report, state_doc
    )
    context_block, tool_names, turn_intent = await _route_turn(
        user_message, context_block, context,
        conversation_id=conversation_id, turn_index=turn_index, impl="graph",
    )
    # Phase 2.5 (doc 09 §6.4): for a conversational follow-up that keyword-matches
    # a known bucket, inject ONE bounded slice up front so the common enumeration
    # follow-up answers in one hop instead of round-tripping through recall_state.
    if turn_intent == "conversational_followup":
        prefetch = build_followup_prefetch(state_doc, user_message)
        if prefetch:
            context_block = f"{context_block}\n{prefetch}\n"
    state = _initial_state(
        conversation_id, turn_index, user_message, context, context_block,
        persist=True, tool_names=tool_names, intent=turn_intent,
        pinned_node_ids=pinned_node_ids,
        turn_id=turn_id, parent_turn_id=parent_turn_id,
    )

    try:
        await _graph().ainvoke(state, config={"recursion_limit": _RECURSION_LIMIT})
    except Exception as e:
        log.exception("turn_failed", extra={"conversation_id": conversation_id})
        await _emit(conversation_id, turn_index, "error", message=f"agent failed: {e}")
        await conversations.set_state(conversation_id, "error")
        if turn_id is not None:
            try:
                await conversations.update_turn_entry(
                    conversation_id, turn_id, status="error"
                )
            except Exception:
                pass
        tracing.log_event("turn_failed", conversation_id=conversation_id, error=str(e))


async def evaluate_turn(
    user_message: str,
    force_risk_report: bool = False,
) -> dict[str, Any]:
    """Run a turn to completion with NO persistence and NO SSE, returning the
    structured result directly from the final graph state. This is the synergy
    of going LangGraph-first: the eval harness gets the RiskSummary/TurnAnswer
    without parsing SSE or touching Redis."""
    context_block = build_context_block(
        "", {"nodes": [], "edges": []}, [], force_risk_report, {}
    )
    context_block, tool_names, turn_intent = await _route_turn(
        user_message, context_block, "",
        conversation_id="eval", turn_index=0, impl="graph",
    )
    state = _initial_state(
        "eval", 0, user_message, "", context_block, persist=False,
        tool_names=tool_names, intent=turn_intent,
    )
    final = await _graph().ainvoke(state, config={"recursion_limit": _RECURSION_LIMIT})

    summary = final.get("result_summary")
    answer = final.get("result_answer")
    tools_used = sorted(set(final.get("tools_used", [])))
    if summary is not None:
        return {"kind": "summary", "result": summary.model_dump(), "tools_used": tools_used}
    return {
        "kind": "answer",
        "result": answer.model_dump() if answer is not None else None,
        "tools_used": tools_used,
    }
