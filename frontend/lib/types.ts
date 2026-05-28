/**
 * Type mirrors for the backend Pydantic models in backend/app/schema.py.
 *
 * Keep these in sync by hand. The shapes are simple enough that an OpenAPI
 * codegen step would be overkill for v1, but if we add more endpoints, we
 * should switch to openapi-typescript.
 */

export type NodeLabel = "Entity" | "Officer" | "Intermediary" | "Address" | "Other";

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
  properties: Record<string, unknown>;
}

export interface GraphEdge {
  source: string;
  target: string;
  type: string;
}

export interface SourceRef {
  source: "icij" | "opensanctions";
  node_id: string | null;
  sanctions_id: string | null;
  leak: string | null;
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
  reason: string | null;
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
}

/**
 * SSE event union. The `type` field discriminates. See backend/app/agent_native.py
 * for where each one is emitted.
 */
export type StreamEvent =
  | { type: "agent_started"; data: { input: string } }
  | { type: "agent_thought"; data: { text: string } }
  | {
      type: "tool_call_start";
      data: { tool: string; args: Record<string, unknown>; call_id: string };
    }
  | {
      type: "tool_call_result";
      data: {
        call_id: string;
        tool: string;
        nodes: GraphNode[];
        edges: GraphEdge[];
        metadata: Record<string, unknown>;
        summary: string;
      };
    }
  | {
      type: "sanctions_hit";
      data: { name: string; hits: SanctionsHit[] };
    }
  | { type: "summary"; data: { summary: RiskSummary } }
  | { type: "error"; data: { message: string } }
  | { type: "done"; data: Record<string, never> };

export type EventType = StreamEvent["type"];

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
