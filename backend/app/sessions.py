"""Session state in Upstash Redis, the queue between POST /assess and GET /stream.

Browsers can't read SSE on a POST, so /assess creates a session and queues the
work, and /stream reads events as the agent emits them. Redis layout:
  session:{id}:state   -> "pending" | "running" | "done" | "error"
  session:{id}:input   -> the original user query
  session:{id}:events  -> list of JSON StreamEvent dicts (agent appends, SSE reads)

Keys have a 1h TTL so there's nothing to clean up. We use Upstash's REST API
(not raw Redis) so it works cleanly from Cloud Run with no socket-pool issues.
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
    is HTTP/1.1 and the calls are tiny, so connection overhead is dwarfed by
    network latency."""
    s = get_settings()
    return httpx.AsyncClient(
        base_url=s.upstash_redis_rest_url,
        headers={"Authorization": f"Bearer {s.upstash_redis_rest_token}"},
        timeout=5.0,
    )


def new_session_id() -> str:
    """A fresh random session id."""
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
    """Set the session's lifecycle state, refreshing its TTL."""
    async with _client() as c:
        await c.post("/pipeline", json=[
            ["SET", f"session:{session_id}:state", state, "EX", str(_TTL_SECONDS)],
        ])


async def get_state(session_id: str) -> str | None:
    """Read the session's lifecycle state, or None if it expired."""
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
