"""Pydantic models used across the API, agent, and tools.

The point of this file is *enforced provenance*: every claim the agent makes about
an entity has to point back to a graph node ID or sanctions record. We enforce
that via the Pydantic types here, not just by polite request in the prompt.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

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


# --- Source provenance tag (for the graph legend + cross-source story) ---
# Which data system a node/edge/claim came from. ICIJ leak provenance,
# OpenSanctions watchlists, and Sayari (registries + risk + trade) are
# independent sources — tagging lets the UI color them and lets the agent
# make the "corroborated across independent sources" claim honestly.
SourceSystem = Literal["icij", "sanctions", "sayari"]


# --- Source references (provenance backbone) ---


class SourceRef(BaseModel):
    """A pointer to the graph node, sanctions record, or Sayari entity that
    backs a claim. Exactly one identifier field should be populated to match
    `source`."""

    source: Literal["icij", "opensanctions", "sayari"]
    node_id: str | None = None  # Neo4j internal node id, as string (icij)
    sanctions_id: str | None = None  # OpenSanctions entity id
    sayari_entity_id: str | None = None  # Sayari entity id (resolved/traversed)
    leak: str | None = None  # ICIJ sourceID, e.g. "Paradise Papers"
    # When the claim is backed by a Sayari risk factor, name it so the UI can
    # tie the claim back to the factor card and its traversal path.
    risk_factor: str | None = None

    @field_validator("source", mode="before")
    @classmethod
    def _normalize_source(cls, v: object) -> object:
        """Accept the `"sanctions"` source label and canonicalize it to
        `"opensanctions"`. The watchlist source is spelled `"sanctions"` almost
        everywhere else in the stack (SourceSystem, the SSE legend, recall_state,
        sanctions.py), so the model frequently emits `source:"sanctions"` here and
        the old strict Literal rejected it — a whole class of terminator-validation
        failures and retry loops. We normalize to `"opensanctions"` (NOT the other
        way) because that is the value the frontend reads to render the watchlist
        chip, so existing readers and stored data stay byte-compatible. Runs before
        the Literal check, so the stored/emitted value is always canonical."""
        if isinstance(v, str) and v.strip().lower() == "sanctions":
            return "opensanctions"
        return v


# --- Risk summary primitives ---


class Claim(BaseModel):
    """A single statement in the risk summary. Must be backed by >=1 source_ref."""

    text: str
    source_refs: list[SourceRef] = Field(min_length=1)
    confidence: Literal["high", "medium", "low"]


class SanctionsHit(BaseModel):
    """A match returned by OpenSanctions.

    The disambiguation fields (position, address, countries, birth_date) are
    populated from OpenSanctions' /match payload when present. The agent uses
    them to detect name-only collisions (e.g. a Wall Street banker getting a
    high name-similarity score against a debarred physician of the same name).
    """

    name_searched: str
    matched_name: str
    lists: list[str]  # e.g. ["OFAC SDN", "EU Consolidated"]
    sanctions_id: str
    score: float  # 0..1
    reason: str | None = None
    # True only when the match is on an actual sanctions/watchlist dataset
    # (OFAC, EU FSF, UN, SAM exclusions, ...). False for wikidata/PEP/registry
    # hits that happen to match by name. A "strong" sanctions hit requires
    # BOTH a high score AND on_watchlist=True.
    on_watchlist: bool = False
    # Disambiguation fields. Optional because OpenSanctions records vary in
    # completeness — some have rich PEP data, others are bare-name watchlists.
    position: list[str] | None = None  # e.g. ["PHYSICIAN (MD, DO)"]
    address: list[str] | None = None
    countries: list[str] | None = None  # ISO codes, e.g. ["us", "ru"]
    birth_date: list[str] | None = None  # ISO dates, e.g. ["1967-03-01"]


class SayariCandidate(BaseModel):
    """One ranked match from Sayari resolution.

    Resolution returns CANDIDATES, not an answer: `score` ranks relevance
    (descending) but the top score is not always the canonical entity (the
    Sberbank case — the top hit was a subsidiary). The agent disambiguates
    using score + match_strength + address + identifiers, exactly like the
    Jeffrey-Lipman discipline on the ICIJ side. Never auto-merge into ICIJ on
    name alone.
    """

    entity_id: str
    label: str
    type: str | None = None  # company / person / government_organization / ...
    score: float | None = None  # relevance rank, descending (NOT a 0-1 confidence)
    match_strength: str | None = None  # Sayari's qualitative band: weak/medium/strong
    countries: list[str] = Field(default_factory=list)  # ISO trigrams
    # Strong join keys (OFAC SDN #, LEI, SEC CIK, ru_inn/ogrn, ...).
    identifiers: list[dict] = Field(default_factory=list)
    addresses: list[str] = Field(default_factory=list)


class SayariSearchCandidate(BaseModel):
    """One lead from Sayari Entity Search (broad/fuzzy investigative search).

    Distinct from SayariCandidate (precise resolution): Entity Search is lead-gen
    — it casts a wide net to surface entities worth a closer look, NOT a ranked
    answer to "which entity is this?". We keep each lead deliberately slim (id,
    label, type, country, flags, top risk-factor names) so a broad query doesn't
    flood the model or the canvas. The agent triages these, then resolves/profiles
    the ones it wants to pursue.
    """

    entity_id: str
    label: str
    type: str | None = None  # company / person / government_organization / ...
    countries: list[str] = Field(default_factory=list)  # ISO trigrams
    sanctioned: bool | None = None
    pep: bool | None = None
    # The most severe risk-factor NAMES on the lead (not the full factor map) —
    # just enough to tell a risky lead from a benign one at triage time.
    top_risk: list[str] = Field(default_factory=list)


class SayariRecord(BaseModel):
    """A single Sayari source record (document-level provenance).

    Returned by sayari_record: the underlying source document behind a fact, with
    its `document_urls` / `source_url` so a finding can be traced to a primary
    record rather than just an aggregated entity. Slimmed to the provenance-
    relevant fields; the raw record carries large nested reference lists we drop.
    """

    id: str
    label: str | None = None
    source: str | None = None  # source dataset id
    source_url: str | None = None  # dataset landing page
    document_urls: list[str] = Field(default_factory=list)  # document-level links
    publication_date: str | None = None
    acquisition_date: str | None = None
    record_url: str | None = None
    references_count: int | None = None


# --- Tier 2: trade + supply-chain risk ---


class SayariTradeParty(BaseModel):
    """A supplier or buyer on a Sayari shipment (slimmed).

    The raw `SourceOrDestinationEntity` carries a `names[]` list that can run to
    ~50-70 aliases; we keep `name` (names[0]) plus `names_count` so a party never
    floods the model. `bis_tags` holds Sayari's NATIVE export-control / dual-use
    risk-factor names off the party's `risks` dict (provenance: Sayari), distinct
    from our HS screen. `sanctioned` is set only when the party carries a direct
    sanction tag (not mere ownership exposure).
    """

    entity_id: str
    name: str
    names_count: int = 0  # how many aliases the raw party carried (we kept 1)
    countries: list[str] = Field(default_factory=list)  # ISO trigrams
    role: Literal["supplier", "buyer"]
    sanctioned: bool = False
    bis_tags: list[str] = Field(default_factory=list)  # native Sayari export tags


class SayariShipment(BaseModel):
    """One slimmed Sayari shipment (a single supplier -> buyer movement).

    Kept to the decision-relevant fields: the two parties, the 6-digit HS codes,
    the monetary value, the route (departure -> transit -> arrival ISO-3), the
    latest date, and the dual-use verdict. `dual_use` is True when EITHER our HS
    screen fires OR a party carries a native BIS/export tag; `dual_use_hits` lists
    the screened HS codes that matched (provenance carried on each hit).
    """

    id: str
    supplier: SayariTradeParty | None = None
    buyer: SayariTradeParty | None = None
    hs_codes: list[dict] = Field(default_factory=list)  # [{code, description}]
    value: float | None = None
    currency: str | None = None
    departure_country: list[str] = Field(default_factory=list)  # ISO-3
    transit_country: list[str] = Field(default_factory=list)
    arrival_country: list[str] = Field(default_factory=list)
    last_date: str | None = None  # latest of departure/arrival dates
    dual_use: bool = False
    dual_use_hits: list[dict] = Field(default_factory=list)  # [{code, tier, provenance}]


class SayariTradeEdge(BaseModel):
    """A directed trade relationship rendered on the graph as a `ships_to` edge.

    Aggregates one or more shipments between the same supplier->buyer pair:
    `hs_codes` is the union of codes seen, `value` the summed monetary value,
    `last_date` the most recent shipment date, `dual_use` True if ANY shipment on
    the lane screened dual-use. Source-tagged "sayari" so the UI colors trade
    edges distinctly from ownership/sanctions edges.
    """

    source: str  # supplier entity_id
    target: str  # buyer entity_id
    type: Literal["ships_to"] = "ships_to"
    source_system: SourceSystem = "sayari"
    hs_codes: list[str] = Field(default_factory=list)
    value: float | None = None
    last_date: str | None = None
    shipment_count: int = 1
    dual_use: bool = False
    dual_use_hits: list[dict] = Field(default_factory=list)


class SayariShortestPathHop(BaseModel):
    """One hop on a Sayari shortest-path result: the relationship `field` that
    led to `entity` (id/label/type/sanctioned/pep/countries). The `entity` shape
    matches what the ownership/control graph builder already consumes, so a hop
    renders as a node + edge on the same canvas."""

    field: str  # relationship type, e.g. "contracted_by", "shareholder_of"
    entity_id: str
    label: str
    type: str | None = None
    sanctioned: bool = False
    pep: bool = False
    countries: list[str] = Field(default_factory=list)


class SayariShortestPath(BaseModel):
    """A Sayari shortest-path between two entities — the "hidden chain" between a
    clean subject and a sanctioned/risky target. `has_sanctioned_intermediary`
    flags when any INTERMEDIATE hop (not just the endpoints) is sanctioned, which
    is the headline supply-chain risk signal: a clean-looking counterparty linked
    to a sanctioned party through an intermediary."""

    source_id: str
    target_id: str
    target_label: str | None = None
    hops: list[SayariShortestPathHop] = Field(default_factory=list)
    has_sanctioned_intermediary: bool = False
    found: bool = True  # False when no path exists within Sayari's graph


class SayariRiskFactor(BaseModel):
    """A single slimmed Sayari risk factor.

    `level` is the severity band (critical > high > elevated > relevant).
    `value` is the raw factor value: True for direct/categorical factors,
    a number equal to the hops in the ownership chain for derived factors, or
    an index score. `path` holds the `metadata.traversal_path` strings
    (`srcId|rel|tgtId|rel|tgtId`) — the exact ownership/control chain that
    triggered the factor, which becomes a highlightable chain on the graph.
    `psa` flags ER-derived (`psa_*`) factors as lower-confidence.
    """

    name: str
    level: str  # critical | high | elevated | relevant (kept open for new bands)
    value: str | float | bool | None = None
    path: list[str] = Field(default_factory=list)
    psa: bool = False


class FollowupSuggestion(BaseModel):
    """A suggested next investigation, surfaced to the user as a clickable pill."""

    name: str  # the name to investigate (drops straight into /assess)
    reason: str  # one-sentence rationale shown on hover


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
    suggested_followups: list[FollowupSuggestion] = Field(default_factory=list)
    # Sayari risk factors the agent chose to surface (slimmed; see
    # slim_sayari_profile). Rendered grouped by level in the UI; each factor's
    # `path` highlights its ownership/control chain on the graph.
    sayari_risk_factors: list[SayariRiskFactor] = Field(default_factory=list)
    # Open questions that would sharpen the investigation (scope honesty).
    clarifying_questions: list[str] = Field(default_factory=list)


class TurnAnswer(BaseModel):
    """Lightweight terminator for follow-up / clarification turns.

    Used instead of RiskSummary when the user asks a narrow question, a
    clarification is needed, or the conversation is exploratory. Keeps the
    same provenance discipline: any factual assertion goes in `claims` with
    source_refs; pure explanation can live in `answer`.
    """

    answer: str  # markdown narrative — the main response
    claims: list[Claim] = Field(default_factory=list)
    referenced_node_ids: list[str] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)
    offer_risk_report: bool = False
    risk_report_prompt: str | None = None
    # Guarded affordance flag: the agent sets this true when the turn has gathered
    # enough for a formal memo — a resolved entity PLUS >=1 risk/ownership/sanctions
    # signal. The frontend uses it to surface a "generate risk report" button
    # (Tier 3). It does NOT auto-emit the report; the user must ask. Keeping the
    # default conversational answer + this flag is the whole point of the
    # conversational-by-default posture.
    report_ready: bool = False
    sanctions_hits: list[SanctionsHit] = Field(default_factory=list)
    suggested_followups: list[FollowupSuggestion] = Field(default_factory=list)
    sayari_risk_factors: list[SayariRiskFactor] = Field(default_factory=list)
    tools_used: list[str] = Field(default_factory=list)


# --- Tool I/O types ---


class GraphNode(BaseModel):
    """A node returned by a graph tool, ready for the frontend's React Flow canvas."""

    id: str  # Neo4j node id (icij) or Sayari entity id
    label: Literal["Entity", "Officer", "Intermediary", "Address", "Other"]
    name: str
    source: str | None = None  # ICIJ sourceID (which leak), if applicable
    # Which data system this node came from, for the graph legend. None on
    # legacy ICIJ nodes (treated as "icij" by the frontend).
    source_system: SourceSystem | None = None
    properties: dict = Field(default_factory=dict)


class GraphEdge(BaseModel):
    source: str  # source node id
    target: str  # target node id
    type: str  # relationship type, e.g. "officer_of" or "ships_to" (Tier 2 trade)
    # Which data system produced this edge. None on legacy ICIJ edges.
    source_system: SourceSystem | None = None
    # Optional edge metadata, mirroring GraphNode.properties. Tier 2 trade edges
    # ("ships_to") carry hs_codes / value / last_date / dual_use here so the
    # frontend can style and badge them without a parallel data channel. Empty
    # for ownership/sanctions/ICIJ edges, so existing readers are unaffected.
    properties: dict = Field(default_factory=dict)


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
        "sanctions_review",
        "agent_thought",
        "token",
        "summary",
        "answer",
        "error",
        "done",
    ]
    data: dict
