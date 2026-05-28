"""Manual structured tracing — Phase 1 observability.

Cloud Logging on Cloud Run automatically parses stdout JSON into structured
log entries. So all we need to do is print one JSON object per "span"
(llm_call, tool_call) and we get queryable traces in the Cloud console.

This is the muscle that becomes "drop in LangSmith" in Phase 2. Same data
model, different transport.

Usage from the agent loop:
    with span("llm_call", session_id=sid, model="claude-sonnet-4") as s:
        resp = await client.messages.create(...)
        s.attach(tokens=resp.usage.input_tokens + resp.usage.output_tokens,
                 stop_reason=resp.stop_reason)
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator

log = logging.getLogger("erre.trace")


class _Span:
    """Mutable span recorder. Whatever you attach() shows up in the final log row."""

    def __init__(self, kind: str, base: dict[str, Any]) -> None:
        self.kind = kind
        self.start = time.perf_counter()
        self.fields: dict[str, Any] = dict(base)

    def attach(self, **kv: Any) -> None:
        self.fields.update(kv)


@contextmanager
def span(kind: str, **base: Any) -> Iterator[_Span]:
    """Open a structured-log span. On exit, emits one JSON line."""
    s = _Span(kind, base)
    try:
        yield s
    finally:
        elapsed_ms = int((time.perf_counter() - s.start) * 1000)
        row = {"trace_kind": kind, "elapsed_ms": elapsed_ms, **s.fields}
        # default=str so any non-serializable value (datetime, etc.) falls
        # back to str() rather than crashing the log call.
        print(json.dumps(row, default=str), flush=True)


def log_event(kind: str, **fields: Any) -> None:
    """Emit a one-shot structured log line (no timing). Useful for milestone
    events like 'session_created', 'agent_done', etc."""
    print(json.dumps({"trace_kind": kind, **fields}, default=str), flush=True)
