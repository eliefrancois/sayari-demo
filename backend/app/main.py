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

from app import agent_native, graph, sessions
from app.config import get_settings

settings = get_settings()
logging.basicConfig(level=settings.log_level, format="%(message)s")
log = logging.getLogger("erre")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown. Closes the Neo4j driver pool on shutdown so Cloud
    Run revisions don't leak connections."""
    log.info("startup")
    yield
    log.info("shutdown")
    graph.close_driver()


app = FastAPI(title="Entity Risk Resolver", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
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
