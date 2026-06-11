"""Manual structured tracing: print one JSON object per span to stdout.

Cloud Logging on Cloud Run parses stdout JSON into structured, queryable log
entries, so each span (llm_call, tool_call) becomes a trace with no extra wiring.
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
