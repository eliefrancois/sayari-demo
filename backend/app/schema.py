"""Pydantic models used across the API, agent, and tools.

The point of this file is *enforced provenance*: every claim the agent makes about
an entity has to point back to a graph node ID or sanctions record. We enforce
that via the Pydantic types here, not just by polite request in the prompt.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# --- Risk signal taxonomy (bounded list, enforced via Literal) ---
# The agent must only emit signals from this list. If it tries to invent a new
# one, Pydantic validation rejects it and the agent has to retry.

RiskSignal = Literal[
    "shell_company_pattern",
    "shared_address_with_many_entities",
    "nominee_director_pattern",
    "sanctioned",
    "connected_to_sanctioned",
    "struck_off",
    "cross_leak_presence",
]


# --- Source references (provenance backbone) ---


class SourceRef(BaseModel):
    """A pointer to the graph node or sanctions record that backs a claim."""

    source: Literal["icij", "opensanctions"]
    node_id: str | None = None  # Neo4j internal node id, as string
    sanctions_id: str | None = None  # OpenSanctions entity id
    leak: str | None = None  # ICIJ sourceID, e.g. "Paradise Papers"


# --- Risk summary primitives ---


class Claim(BaseModel):
    """A single statement in the risk summary. Must be backed by >=1 source_ref."""

    text: str
    source_refs: list[SourceRef] = Field(min_length=1)
    confidence: Literal["high", "medium", "low"]


class SanctionsHit(BaseModel):
    """A match returned by OpenSanctions."""

    name_searched: str
    matched_name: str
    lists: list[str]  # e.g. ["OFAC SDN", "EU Consolidated"]
    sanctions_id: str
    score: float  # 0..1
    reason: str | None = None


class RiskSummary(BaseModel):
    """Final structured output of an investigation. The agent must return this shape."""

    entity_name: str
    entity_id: str | None
    found: bool  # explicit not-found path; anti-hallucination
    claims: list[Claim]
    risk_signals: list[RiskSignal]
    sanctions_hits: list[SanctionsHit]
    investigation_summary: str
    tools_used: list[str]


# --- Tool I/O types ---


class GraphNode(BaseModel):
    """A node returned by a graph tool, ready for the frontend's React Flow canvas."""

    id: str  # Neo4j node id as string
    label: Literal["Entity", "Officer", "Intermediary", "Address", "Other"]
    name: str
    source: str | None = None  # ICIJ sourceID (which leak), if applicable
    properties: dict = Field(default_factory=dict)


class GraphEdge(BaseModel):
    source: str  # source node id
    target: str  # target node id
    type: str  # relationship type, e.g. "officer_of"


class Neighborhood(BaseModel):
    """Generic graph-tool return shape."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]
    metadata: dict = Field(default_factory=dict)


class SearchResults(BaseModel):
    """Return of search_entity."""

    nodes: list[GraphNode]
    metadata: dict = Field(default_factory=dict)


# --- SSE event envelope (mirrored on the frontend in lib/types.ts) ---


class StreamEvent(BaseModel):
    """Wire format for the SSE stream. `type` discriminates the payload shape."""

    type: Literal[
        "agent_started",
        "tool_call_start",
        "tool_call_result",
        "sanctions_hit",
        "agent_thought",
        "summary",
        "error",
        "done",
    ]
    data: dict
