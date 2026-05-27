"""FastAPI app. Three endpoints today:

- POST /assess         → start an investigation, return {session_id}
- GET  /stream/{id}    → SSE stream of agent events
- GET  /health         → liveness + dependency status

This module is intentionally thin. It owns request validation, CORS, and routing
the agent_impl flag. All real work happens in the agent and tools modules.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.config import get_settings

settings = get_settings()
logging.basicConfig(level=settings.log_level)
log = logging.getLogger("erre")

app = FastAPI(title="Entity Risk Resolver", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# --- Health ---


class HealthResponse(BaseModel):
    status: str
    version: str
    agent_impl: str
    deps: dict


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness check. Doesn't actually ping Neo4j/Redis yet — that lands when those
    modules exist. Cloud Run only needs a 2xx to consider the container healthy."""
    return HealthResponse(
        status="ok",
        version="0.1.0",
        agent_impl=settings.agent_impl,
        deps={
            "neo4j": "not_checked",
            "redis": "not_checked",
            "anthropic": "not_checked",
            "opensanctions": "not_checked",
        },
    )


# --- Assess / stream (stubbed until the agent layer lands) ---


class AssessRequest(BaseModel):
    name: str


class AssessResponse(BaseModel):
    session_id: str
    detail: str


@app.post("/assess", response_model=AssessResponse)
async def assess(req: AssessRequest) -> AssessResponse:
    """Kick off an investigation. Returns a session_id the client uses to open
    the SSE stream. NOTE: stubbed until agent + sessions land."""
    return AssessResponse(
        session_id="not-implemented",
        detail=f"received '{req.name}', agent layer not wired yet",
    )
