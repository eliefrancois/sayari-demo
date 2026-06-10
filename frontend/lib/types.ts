/**
 * Type mirrors for the backend Pydantic models in backend/app/schema.py.
 *
 * Keep these in sync by hand. The shapes are simple enough that an OpenAPI
 * codegen step would be overkill for v1, but if we add more endpoints, we
 * should switch to openapi-typescript.
 */

export type NodeLabel = "Entity" | "Officer" | "Intermediary" | "Address" | "Other";

/** Which data system a node/edge/claim came from (graph legend + cross-source story). */
export type SourceSystem = "icij" | "sanctions" | "sayari";

export type RiskSignal =
  | "shell_company_pattern"
  | "shared_address_with_many_entities"
  | "nominee_director_pattern"
  | "sanctioned"
  | "connected_to_sanctioned"
  | "struck_off"
  | "cross_leak_presence";

export type Confidence = "high" | "medium" | "low";

export interface GraphNode {
  id: string;
  label: NodeLabel;
  name: string;
  source: string | null;
  /** Data system this node came from. Null on legacy ICIJ nodes (treat as "icij"). */
  source_system?: SourceSystem | null;
  properties: Record<string, unknown>;
}

export interface GraphEdge {
  source: string;
  target: string;
  type: string;
  source_system?: SourceSystem | null;
  /**
   * Optional edge metadata (mirrors backend GraphEdge.properties). Tier 2
   * "ships_to" trade edges carry kind/hs_codes/value/last_date/dual_use here;
   * empty or absent on ownership/sanctions/ICIJ edges.
   */
  properties?: Record<string, unknown> | null;
}

/**
 * A broad-search lead node: a GraphNode plus whether it was pinned to the
 * canvas. Unpinned ones are rendered only as a transient overlay (the
 * "Showing N of M leads" toggle), never merged into the persistent graph.
 */
export interface LeadNode extends GraphNode {
  pinned: boolean;
}

export interface SourceRef {
  source: "icij" | "opensanctions" | "sayari";
  node_id: string | null;
  sanctions_id: string | null;
  sayari_entity_id?: string | null;
  leak: string | null;
  /** Sayari risk factor backing this claim, if any. */
  risk_factor?: string | null;
}

export interface Claim {
  text: string;
  source_refs: SourceRef[];
  confidence: Confidence;
}

export interface SanctionsHit {
  name_searched: string;
  matched_name: string;
  lists: string[];
  sanctions_id: string;
  score: number;
  /** True only for actual sanctions/watchlist datasets, not wikidata/PEP/registry. */
  on_watchlist?: boolean;
  reason: string | null;
  /** Matched person's occupation(s), used to disambiguate name collisions. */
  position?: string[] | null;
  address?: string[] | null;
  /** ISO country codes of the matched record. */
  countries?: string[] | null;
  birth_date?: string[] | null;
}

export interface FollowupSuggestion {
  name: string;
  reason: string;
}

export type SayariRiskLevel = "critical" | "high" | "elevated" | "relevant";

/** A slimmed Sayari risk factor (mirrors backend schema.SayariRiskFactor). */
export interface SayariRiskFactor {
  name: string;
  /** Severity band; kept as string since Sayari may add new bands. */
  level: string;
  value?: string | number | boolean | null;
  /** traversal_path strings: "srcId|rel|tgtId|rel|tgtId". */
  path: string[];
  /** ER-derived (psa_*) — lower confidence. */
  psa?: boolean;
}

/** A ranked Sayari resolution candidate (mirrors backend schema.SayariCandidate). */
export interface SayariCandidate {
  entity_id: string;
  label: string;
  type?: string | null;
  score?: number | null;
  match_strength?: string | null;
  countries: string[];
  identifiers: { type?: string; value?: string; label?: string }[];
  addresses: string[];
}

/** Agent's adjudication of raw sanctions tool hits vs final summary. */
export interface SanctionsReview {
  raw_strong_count: number;
  confirmed: SanctionsHit[];
  dismissed: SanctionsHit[];
}

export interface RiskSummary {
  entity_name: string;
  entity_id: string | null;
  found: boolean;
  claims: Claim[];
  risk_signals: RiskSignal[];
  sanctions_hits: SanctionsHit[];
  investigation_summary: string;
  tools_used: string[];
  suggested_followups: FollowupSuggestion[];
  sayari_risk_factors?: SayariRiskFactor[];
  clarifying_questions?: string[];
}

/**
 * Lightweight terminator for CLARIFY / FOLLOW-UP turns (mirrors backend
 * schema.TurnAnswer). Used instead of RiskSummary when the agent answers a
 * narrow question, asks for clarification, or chats — rather than producing a
 * full investigation memo.
 */
export interface TurnAnswer {
  answer: string;
  claims: Claim[];
  referenced_node_ids: string[];
  clarification_questions: string[];
  offer_risk_report: boolean;
  risk_report_prompt: string | null;
  /**
   * Guarded affordance flag (Tier 1 backend): true when the turn has a resolved
   * entity + >=1 risk/ownership/sanctions signal. Tier 3 will surface a
   * "generate report" button off this; until then it's just carried through.
   */
  report_ready?: boolean;
  sanctions_hits: SanctionsHit[];
  suggested_followups: FollowupSuggestion[];
  sayari_risk_factors?: SayariRiskFactor[];
  tools_used: string[];
}

/** Response from GET /expand/{node_id}?kind=... */
export interface ExpandResponse {
  nodes: GraphNode[];
  edges: GraphEdge[];
  metadata: Record<string, unknown>;
}

export type ExpandKind =
  | "relationships"
  | "officers"
  | "address_connections"
  | "er_links";

/**
 * SSE event union. The `type` field discriminates. See backend/app/agent_native.py
 * for where each one is emitted.
 */
/**
 * Conversation events all carry an optional `turn_index` so the reducer can
 * route each event to the correct turn in the thread. Single-shot /assess
 * events omit it (treated as turn 0). Stage 2a additionally stamps every
 * event with the running turn's `turn_id` + `parent_turn_id`, which is what
 * lets the branching canvas attach a stream to the right card without a
 * lookup (docs/11-branching-backend.md).
 */
type WithTurn<T> = T & {
  turn_index?: number;
  turn_id?: string;
  parent_turn_id?: string | null;
};

export type StreamEvent =
  | { type: "agent_started"; data: WithTurn<{ input: string }> }
  | { type: "agent_thought"; data: WithTurn<{ text: string }> }
  | { type: "token"; data: WithTurn<{ delta: string }> }
  | {
      type: "tool_call_start";
      data: WithTurn<{ tool: string; args: Record<string, unknown>; call_id: string }>;
    }
  | {
      type: "tool_call_result";
      data: WithTurn<{
        call_id: string;
        tool: string;
        nodes: GraphNode[];
        edges: GraphEdge[];
        metadata: Record<string, unknown>;
        summary: string;
        /**
         * Only on broad sayari_search: lightweight node reps for EVERY lead,
         * each flagged `pinned`. The unpinned ones power the "Showing N of M
         * leads" overlay toggle. These never enter the persistent graph.
         */
        all_lead_nodes?: LeadNode[];
      }>;
    }
  | {
      type: "sanctions_hit";
      data: WithTurn<{ name: string; hits: SanctionsHit[] }>;
    }
  | { type: "summary"; data: WithTurn<{ summary: RiskSummary }> }
  | { type: "answer"; data: WithTurn<{ answer: TurnAnswer }> }
  | {
      type: "sanctions_review";
      data: WithTurn<{ review: SanctionsReview }>;
    }
  | { type: "error"; data: WithTurn<{ message: string }> }
  | { type: "done"; data: WithTurn<Record<string, never>> };

export type EventType = StreamEvent["type"];

/**
 * One turn's metadata in the conversation turn tree (GET /conversations/{id}/tree,
 * also under `tree` in the hydrate payload). Pre-branching conversations return
 * an empty list — fall back to the flat `turns`.
 */
export interface TreeTurn {
  turn_id: string;
  parent_turn_id: string | null;
  turn_index: number;
  user_message: string;
  status: "running" | "done" | "error";
  created_at?: number;
  /** Set once the turn finishes: "answer" | "summary" | "clarification". */
  kind?: string | null;
  report_ready?: boolean;
  offer_risk_report?: boolean;
}

/**
 * Response from GET /conversations/{id}/turns/{turn_id}/graph — the
 * time-travel payload. `graph` is accumulated along the root -> turn path
 * (sibling branches excluded); `turn_delta` is this turn's own contribution,
 * separated so the canvas can pulse new-this-turn nodes and dim inherited ones.
 */
export interface TurnGraphResponse {
  turn_id: string;
  path: string[];
  graph: { nodes: GraphNode[]; edges: GraphEdge[] };
  turn_delta: { nodes: GraphNode[]; edges: GraphEdge[] };
}

/**
 * One row from GET /conversations — the recents index behind the history
 * menu. `state` is the conversation's live state key ("idle" | "running" |
 * "error"); rows whose meta expired server-side never appear.
 */
export interface ConversationListItem {
  conversation_id: string;
  title: string;
  created_at: number | null;
  updated_at: number | null;
  turn_count: number;
  state: string;
}

/** Payload from GET /conversations/{id} — used to restore on page reload. */
export interface ConversationHydrate {
  conversation_id: string;
  meta: { title?: string; created_at?: number; updated_at?: number; turn_count?: number };
  state: string | null;
  graph: { nodes: GraphNode[]; edges: GraphEdge[] };
  turns: { turn_index: number; kind: string; user_message: string; entity_name?: string; offer_risk_report?: boolean }[];
  summaries: RiskSummary[];
  answers: TurnAnswer[];
  /** Turn tree (stage 2a). Empty/absent for pre-branching conversations. */
  tree?: TreeTurn[];
}

/**
 * Source-system display metadata for the graph legend. Each data system gets a
 * distinct accent so nodes/edges are visually attributable to their origin.
 *
 * Color semantics (locked in docs/04-tier3-ux-spec.md §2): the chrome is
 * neutral lmcanvas grayscale; color is reserved for exactly two signals —
 * SOURCE (ring/dot): Sayari indigo, ICIJ magenta/violet, OpenSanctions teal,
 * and RISK severity (glow/fill): critical red, high orange, elevated amber,
 * relevant gray. Ring = source, glow = risk, so provenance is never lost.
 */
export const SOURCE_SYSTEM_META: Record<
  SourceSystem,
  { label: string; color: string }
> = {
  icij: { label: "ICIJ Leaks", color: "var(--source-icij)" }, // magenta/violet
  sanctions: { label: "OpenSanctions", color: "var(--source-sanctions)" }, // teal
  sayari: { label: "Sayari", color: "var(--source-sayari)" }, // indigo
};

/** Risk-severity accent colors (glow/fill scale). CSS vars from globals.css. */
export const RISK_LEVEL_COLORS: Record<SayariRiskLevel, string> = {
  critical: "var(--risk-critical)",
  high: "var(--risk-high)",
  elevated: "var(--risk-elevated)",
  relevant: "var(--risk-relevant)",
};

/** Default a node/edge with no explicit tag to ICIJ (legacy graph data). */
export const sourceSystemOf = (s?: SourceSystem | null): SourceSystem => s ?? "icij";

/**
 * Severity ordering + display for Sayari risk levels. Lower rank = more severe.
 */
export const SAYARI_LEVEL_META: Record<
  SayariRiskLevel,
  { label: string; rank: number; className: string }
> = {
  critical: { label: "Critical", rank: 0, className: "text-red-700 border-red-300 bg-red-50" },
  high: { label: "High", rank: 1, className: "text-orange-700 border-orange-300 bg-orange-50" },
  elevated: { label: "Elevated", rank: 2, className: "text-amber-700 border-amber-300 bg-amber-50" },
  relevant: { label: "Relevant", rank: 3, className: "text-muted-foreground border-border bg-muted" },
};

export const sayariLevelRank = (level: string): number =>
  SAYARI_LEVEL_META[level as SayariRiskLevel]?.rank ?? 99;

/**
 * Extract the entity ids from Sayari traversal_path strings. A path looks like
 * "srcId|rel|tgtId|rel|tgtId" — the even-indexed tokens are entity ids. Returns
 * the unique ids across all of a factor's paths, so the graph can highlight the
 * whole chain when the factor is clicked.
 */
export function pathNodeIds(paths: string[]): string[] {
  const ids = new Set<string>();
  for (const p of paths ?? []) {
    const tokens = p.split("|").filter(Boolean);
    for (let i = 0; i < tokens.length; i += 2) ids.add(tokens[i]);
  }
  return Array.from(ids);
}

/** Humanize a Sayari risk-factor name, e.g. "owner_of_sanctioned_che_seco_entity". */
export function humanizeRiskFactor(name: string): string {
  return name
    .replace(/_/g, " ")
    .replace(/\bpsa\b/i, "(possibly same as)")
    .replace(/\b(soe)\b/i, "state-owned enterprise")
    .replace(/\b(usa|eu|uk|gbr|che|can|ukr|blr|rus|chn)\b/gi, (m) => m.toUpperCase())
    .trim();
}

/**
 * Risk-signal display metadata. Color tier roughly maps to severity.
 */
export const RISK_SIGNAL_META: Record<
  RiskSignal,
  { label: string; tier: "red" | "amber" | "yellow"; description: string }
> = {
  sanctioned: {
    label: "Sanctioned",
    tier: "red",
    description: "Subject directly appears on a sanctions list.",
  },
  connected_to_sanctioned: {
    label: "Connected to sanctioned",
    tier: "amber",
    description: "Subject is connected (via ownership, address, or officer overlap) to a sanctioned individual.",
  },
  struck_off: {
    label: "Struck off",
    tier: "amber",
    description: "Associated entity was deregistered (often shortly after a leak).",
  },
  shell_company_pattern: {
    label: "Shell-company pattern",
    tier: "amber",
    description: "Bearer shares, nominee directors, or layered offshore structures detected.",
  },
  shared_address_with_many_entities: {
    label: "Mass-registration address",
    tier: "yellow",
    description: "Registered address is shared with 10+ other entities (corporate-services-firm signature).",
  },
  nominee_director_pattern: {
    label: "Nominee directors",
    tier: "yellow",
    description: "Officers appear to be nominees (e.g. underlying chains, repeating across many entities).",
  },
  cross_leak_presence: {
    label: "Cross-leak presence",
    tier: "yellow",
    description: "Subject appears in multiple ICIJ leaks (via explicit ER or shared structural signals).",
  },
};
