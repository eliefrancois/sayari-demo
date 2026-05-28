"""Session state in Upstash Redis.

Two-step handshake reason: browsers can't read SSE on a POST. So:
  1. POST /assess  -> create session_id, queue an investigation
  2. GET /stream/{id} -> read events as they're emitted

This file is the queue between those two halves. Layout in Redis:
  session:{id}:state   -> "pending" | "running" | "done" | "error"
  session:{id}:input   -> the original user query
  session:{id}:events  -> list of JSON-encoded StreamEvent dicts (appended by
                          the agent, popped/iterated by the SSE endpoint)

Both keys have a TTL of 1h so we don't have to clean up. Demo investigations
finish in <60s; 1h is generous.

We use Upstash's REST API (not the raw Redis protocol). Two reasons: works
cleanly from Cloud Run with no socket-pool issues, and the REST API supports
the same primitives we need (SET, RPUSH, LRANGE, EXPIRE).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger("erre.sessions")

_TTL_SECONDS = 60 * 60  # 1h


def _client() -> httpx.AsyncClient:
    """One-shot client. We don't hold a long-lived pool because Upstash REST
    is HTTP/1.1 and the calls are tiny — connection overhead is dwarfed by
    network latency."""
    s = get_settings()
    return httpx.AsyncClient(
        base_url=s.upstash_redis_rest_url,
        headers={"Authorization": f"Bearer {s.upstash_redis_rest_token}"},
        timeout=5.0,
    )


def new_session_id() -> str:
    return uuid.uuid4().hex


async def create_session(input_name: str) -> str:
    """Create a new session with state=pending. Returns session_id."""
    sid = new_session_id()
    async with _client() as c:
        # Upstash REST supports pipelined commands via POST to /pipeline.
        body = [
            ["SET", f"session:{sid}:state", "pending", "EX", str(_TTL_SECONDS)],
            ["SET", f"session:{sid}:input", input_name, "EX", str(_TTL_SECONDS)],
        ]
        r = await c.post("/pipeline", json=body)
        r.raise_for_status()
    return sid


async def set_state(session_id: str, state: str) -> None:
    async with _client() as c:
        await c.post("/pipeline", json=[
            ["SET", f"session:{session_id}:state", state, "EX", str(_TTL_SECONDS)],
        ])


async def get_state(session_id: str) -> str | None:
    async with _client() as c:
        r = await c.get(f"/get/session:{session_id}:state")
        r.raise_for_status()
        return r.json().get("result")


async def append_event(session_id: str, event: dict[str, Any]) -> None:
    """Append a StreamEvent dict to the session's event list."""
    payload = json.dumps(event, default=str)
    async with _client() as c:
        await c.post("/pipeline", json=[
            ["RPUSH", f"session:{session_id}:events", payload],
            ["EXPIRE", f"session:{session_id}:events", str(_TTL_SECONDS)],
        ])


async def read_events(session_id: str, start: int = 0) -> list[dict[str, Any]]:
    """Read all events from index `start` onwards. Used by the SSE endpoint
    to poll for new events since the last cursor position."""
    async with _client() as c:
        r = await c.get(f"/lrange/session:{session_id}:events/{start}/-1")
        r.raise_for_status()
        raw = r.json().get("result") or []
        return [json.loads(s) for s in raw]


async def ping() -> bool:
    """Cheap connectivity check for /health."""
    try:
        async with _client() as c:
            r = await c.get("/ping")
            return r.status_code == 200
    except Exception:
        return False
