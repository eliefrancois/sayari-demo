"""FastAPI app — wires the API surface to the agent + Upstash + Neo4j.

Three endpoints:
  POST /assess           : start an investigation, returns {session_id}
  GET  /stream/{id}      : SSE stream of events for that session
  GET  /health           : liveness + dependency status (Cloud Run probe)

This module is thin by design. Its job is HTTP plumbing. Investigation logic
lives in app.agent_native; data access lives in app.graph + app.sanctions;
event queue lives in app.sessions.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from app import agent, agent_native, conversations, graph, sessions
from app.config import apply_langsmith_env, get_settings

settings = get_settings()
logging.basicConfig(level=settings.log_level, format="%(message)s")
log = logging.getLogger("erre")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown. Closes the Neo4j driver pool on shutdown so Cloud
    Run revisions don't leak connections."""
    tracing_on = apply_langsmith_env(settings)
    log.info(
        "startup", extra={"agent_impl": settings.agent_impl, "langsmith": tracing_on}
    )
    yield
    log.info("shutdown")
    graph.close_driver()


app = FastAPI(title="Entity Risk Resolver", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


# ---------- Health ----------


class HealthResponse(BaseModel):
    status: str
    version: str
    agent_impl: str
    deps: dict


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Cloud Run liveness probe + dependency status."""
    neo4j_ok = graph.ping()
    redis_ok = await sessions.ping()
    return HealthResponse(
        status="ok" if (neo4j_ok and redis_ok) else "degraded",
        version="1.0.0",
        agent_impl=settings.agent_impl,
        deps={
            "neo4j": "ok" if neo4j_ok else "down",
            "redis": "ok" if redis_ok else "down",
            "anthropic_configured": bool(settings.anthropic_api_key),
            "opensanctions_configured": bool(settings.opensanctions_api_key),
        },
    )


# ---------- Assess (kick off an investigation) ----------


class AssessRequest(BaseModel):
    name: str


class AssessResponse(BaseModel):
    session_id: str


@app.post("/assess", response_model=AssessResponse)
async def assess(req: AssessRequest, background: BackgroundTasks) -> AssessResponse:
    """Create a session and spawn the agent as a background task.

    BackgroundTasks runs after the response is sent, which is what we want:
    the client gets the session_id immediately and opens the SSE stream,
    then the agent starts populating events.
    """
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name must be non-empty")
    if len(name) > 200:
        raise HTTPException(status_code=400, detail="name too long")

    session_id = await sessions.create_session(name)

    # Wrap in a task we can fire-and-forget. FastAPI's BackgroundTasks works
    # but we want this to start immediately (not after the response is sent)
    # so the client can connect to /stream right away.
    asyncio.create_task(_safe_run(session_id, name))

    return AssessResponse(session_id=session_id)


async def _safe_run(session_id: str, name: str) -> None:
    """Wrapper around agent_native.run_investigation that catches and logs
    any exception, ensuring the session state always reaches a terminal
    state ('done' or 'error') so the SSE stream doesn't hang forever."""
    try:
        await agent_native.run_investigation(session_id, name)
    except Exception as e:
        log.exception("background_agent_crashed", extra={"session_id": session_id})
        try:
            await sessions.append_event(session_id, {"type": "error", "data": {"message": str(e)}})
            await sessions.set_state(session_id, "error")
        except Exception:
            pass


# ---------- Expand (manual graph exploration) ----------


@app.get("/expand/{node_id:path}")
async def expand(node_id: str, kind: str = "relationships") -> dict:
    """Run one graph query and return the result directly (no SSE, no agent).

    Used by the frontend's right-click "Expand" menu to let the user manually
    traverse the graph after the agent's initial pass. `kind` picks the tool:
      relationships | officers | address_connections | er_links
    """
    if kind == "relationships":
        nb = graph.get_relationships(node_id, limit=50)
    elif kind == "officers":
        nb = graph.get_officers(node_id, limit=50)
    elif kind == "address_connections":
        nb = graph.find_address_connections(node_id, limit=25)
    elif kind == "er_links":
        nb = graph.find_er_links(node_id, limit=25)
    else:
        raise HTTPException(status_code=400, detail=f"unknown kind: {kind}")
    return nb.model_dump()


# ---------- Conversations (multi-turn) ----------
#
# A conversation is a persistent, lock-guarded thread of turns. Unlike /assess
# (single-shot, ephemeral session), state lives in Upstash for 24h so the user
# can reload the page and resume. Each turn appends to one event list; the
# stream endpoint takes a `cursor` so a single list serves the whole thread.


class CreateConversationResponse(BaseModel):
    conversation_id: str


class MessageRequest(BaseModel):
    message: str
    pinned_node_ids: list[str] | None = None
    force_risk_report: bool = False
    # Branching (Stage 2a): fork the conversation from any prior turn. Omitted
    # (the only thing the current frontend sends) = continue the current head,
    # i.e. exactly the linear behavior.
    parent_turn_id: str | None = None


class MessageResponse(BaseModel):
    turn_index: int
    event_cursor: int  # where the client should start streaming for this turn
    # Branching: the new turn's tree coordinates (null on the native impl,
    # which does not register tree turns). The SSE events for the turn carry
    # the same ids, so the frontend can build the tree live either way.
    turn_id: str | None = None
    parent_turn_id: str | None = None


class ConversationListItem(BaseModel):
    conversation_id: str
    title: str
    created_at: int | None = None
    updated_at: int | None = None
    turn_count: int = 0
    state: str = "idle"


class ConversationListResponse(BaseModel):
    conversations: list[ConversationListItem]


@app.post("/conversations", response_model=CreateConversationResponse)
async def create_conversation() -> CreateConversationResponse:
    cid = await conversations.create_conversation()
    return CreateConversationResponse(conversation_id=cid)


@app.get("/conversations", response_model=ConversationListResponse)
async def list_conversations(limit: int = 50) -> ConversationListResponse:
    """Recent conversations, newest-updated first, from the ZSET index.

    Ids whose meta already expired (24h per-key TTL) are filtered out and
    lazily removed from the index. Conversations created before the index
    existed don't appear — this is a recents menu, not an archive."""
    limit = max(1, min(limit, 100))
    items = await conversations.list_conversations(limit)
    return ConversationListResponse(
        conversations=[ConversationListItem(**it) for it in items]
    )


@app.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str) -> dict:
    """Delete a conversation's whole key family + its index entry.

    Refused (409) while a turn is running — deleting keys mid-turn would have
    the background task resurrect some of them on its next write."""
    if not await conversations.exists(conversation_id):
        raise HTTPException(status_code=404, detail="unknown conversation_id")
    state = await conversations.get_state(conversation_id)
    if state == "running" or await conversations.is_locked(conversation_id):
        raise HTTPException(status_code=409, detail="a turn is currently running")
    await conversations.delete_conversation(conversation_id)
    return {"deleted": conversation_id}


@app.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str) -> dict:
    """Full hydration payload for restoring a conversation on page load."""
    if not await conversations.exists(conversation_id):
        raise HTTPException(status_code=404, detail="unknown conversation_id")
    return await conversations.hydrate(conversation_id)


@app.post("/conversations/{conversation_id}/messages", response_model=MessageResponse)
async def post_message(conversation_id: str, req: MessageRequest) -> MessageResponse:
    """Submit one user turn. Lock-guarded so two turns can't run concurrently."""
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message must be non-empty")
    if len(message) > 500:
        raise HTTPException(status_code=400, detail="message too long")
    if not await conversations.exists(conversation_id):
        raise HTTPException(status_code=404, detail="unknown conversation_id")

    if req.parent_turn_id is not None and settings.agent_impl != "graph":
        raise HTTPException(
            status_code=400, detail="branching requires AGENT_IMPL=graph"
        )

    got_lock = await conversations.acquire_lock(conversation_id)
    if not got_lock:
        raise HTTPException(status_code=409, detail="a turn is already running")

    meta = await conversations.get_meta(conversation_id)
    turn_index = int(meta.get("turn_count", 0))
    event_cursor = await conversations.event_count(conversation_id)

    # Branching (graph impl only): register the turn in the tree BEFORE it runs,
    # so the response and the live SSE stream carry its coordinates. With no
    # parent_turn_id this defaults to the current head — the linear case.
    turn_id: str | None = None
    parent_turn_id: str | None = None
    if settings.agent_impl == "graph":
        try:
            entry = await conversations.register_turn(
                conversation_id, turn_index, message, req.parent_turn_id
            )
        except ValueError as e:
            await conversations.release_lock(conversation_id)
            raise HTTPException(status_code=400, detail=str(e))
        turn_id = entry["turn_id"]
        parent_turn_id = entry.get("parent_turn_id")

    asyncio.create_task(
        _safe_run_turn(
            conversation_id, message, turn_index,
            req.pinned_node_ids or [], req.force_risk_report,
            turn_id, parent_turn_id,
        )
    )
    return MessageResponse(
        turn_index=turn_index, event_cursor=event_cursor,
        turn_id=turn_id, parent_turn_id=parent_turn_id,
    )


async def _safe_run_turn(
    conversation_id: str,
    message: str,
    turn_index: int,
    pinned_node_ids: list[str],
    force_risk_report: bool,
    turn_id: str | None = None,
    parent_turn_id: str | None = None,
) -> None:
    """Run a turn, always releasing the lock and reaching a terminal state.

    Dispatches through the agent facade, which picks native vs graph based on
    AGENT_IMPL. The legacy /assess path stays native (see agent_native)."""
    try:
        await agent.run_turn(
            conversation_id, message, turn_index, pinned_node_ids,
            force_risk_report, turn_id=turn_id, parent_turn_id=parent_turn_id,
        )
    except Exception as e:
        log.exception("background_turn_crashed", extra={"conversation_id": conversation_id})
        try:
            await conversations.append_event(
                conversation_id,
                {"type": "error", "data": {"message": str(e), "turn_index": turn_index}},
            )
            await conversations.set_state(conversation_id, "error")
            if turn_id is not None:
                await conversations.update_turn_entry(
                    conversation_id, turn_id, status="error"
                )
        except Exception:
            pass
    finally:
        await conversations.release_lock(conversation_id)


@app.get("/conversations/{conversation_id}/stream")
async def conversation_stream(conversation_id: str, cursor: int = 0) -> EventSourceResponse:
    """SSE stream of conversation events starting at `cursor`. Closes when the
    current turn reaches 'done'/'error'; the client reopens with an updated
    cursor for the next turn."""
    if not await conversations.exists(conversation_id):
        raise HTTPException(status_code=404, detail="unknown conversation_id")

    async def event_generator() -> AsyncIterator[dict]:
        pos = cursor
        deadline = asyncio.get_event_loop().time() + _STREAM_TIMEOUT_S
        while True:
            if asyncio.get_event_loop().time() > deadline:
                yield {"event": "error", "data": '{"message": "stream timeout"}'}
                return
            new_events = await conversations.read_events(conversation_id, start=pos)
            for evt in new_events:
                yield {"event": evt.get("type", "message"),
                       "data": _json_dump(evt.get("data", {}))}
                if evt.get("type") in {"done", "error"}:
                    return
            pos += len(new_events)
            await asyncio.sleep(_POLL_INTERVAL_S)

    return EventSourceResponse(event_generator())


# ---------- Branching (Stage 2a): turn tree + path graph ----------


@app.get("/conversations/{conversation_id}/tree")
async def conversation_tree(conversation_id: str) -> dict:
    """The conversation as a tree of turns (sorted by turn_index): ids, parent
    pointers, user text, status, terminator kind + report flags. Old/linear
    conversations that predate branching return an empty list and the frontend
    falls back to the flat `turns` from hydrate."""
    if not await conversations.exists(conversation_id):
        raise HTTPException(status_code=404, detail="unknown conversation_id")
    tree = await conversations.get_turn_tree(conversation_id)
    turns = sorted(tree.values(), key=lambda e: int(e.get("turn_index", 0)))
    # context_after is an internal continuation payload, not frontend data.
    for t in turns:
        t.pop("context_after", None)
    return {"conversation_id": conversation_id, "turns": turns}


@app.get("/conversations/{conversation_id}/turns/{turn_id}/graph")
async def turn_path_graph(conversation_id: str, turn_id: str) -> dict:
    """Time-travel payload: the evidence graph accumulated along this turn's
    path (root -> turn, union of that path's per-turn deltas only — sibling
    branches excluded), plus the turn's OWN delta separately so the frontend
    can pulse new-this-turn nodes and dim inherited ones."""
    if not await conversations.exists(conversation_id):
        raise HTTPException(status_code=404, detail="unknown conversation_id")
    payload = await conversations.get_path_graph(conversation_id, turn_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="unknown turn_id")
    return payload


# ---------- Stream (SSE) ----------


_POLL_INTERVAL_S = 0.25
_STREAM_TIMEOUT_S = 300  # 5 minutes max per session


@app.get("/stream/{session_id}")
async def stream(session_id: str) -> EventSourceResponse:
    """Server-Sent Events stream of agent events for the given session.

    Pulls new events from Upstash on a poll loop (every 250ms). Stops when:
      - session state is 'done' or 'error' and the cursor has caught up
      - the connection times out after STREAM_TIMEOUT_S
      - the client disconnects (handled by sse-starlette)
    """
    state = await sessions.get_state(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="unknown session_id")

    async def event_generator() -> AsyncIterator[dict]:
        cursor = 0
        deadline = asyncio.get_event_loop().time() + _STREAM_TIMEOUT_S

        while True:
            if asyncio.get_event_loop().time() > deadline:
                yield {"event": "error", "data": '{"message": "stream timeout"}'}
                return

            new_events = await sessions.read_events(session_id, start=cursor)
            for evt in new_events:
                # sse-starlette uses 'event' for the SSE 'event:' field
                # and 'data' for the 'data:' field. We package each backend
                # StreamEvent as {event: <type>, data: <json>}.
                yield {"event": evt.get("type", "message"),
                       "data": _json_dump(evt.get("data", {}))}
                if evt.get("type") in {"done", "error"}:
                    return
            cursor += len(new_events)

            await asyncio.sleep(_POLL_INTERVAL_S)

    return EventSourceResponse(event_generator())


def _json_dump(data: object) -> str:
    import json
    return json.dumps(data, default=str)
