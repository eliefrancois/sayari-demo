/**
 * In-memory store for the active investigation. Pure data shape + a reducer.
 * The top-level component owns the state via useReducer; panels read slices.
 *
 * We use a reducer (not multiple useStates) because each SSE event mutates
 * several slices at once (e.g. a tool_call_result adds tool entries AND adds
 * graph nodes/edges), and the reducer keeps those updates atomic.
 */

import type {
  GraphEdge,
  GraphNode,
  RiskSummary,
  SanctionsHit,
  StreamEvent,
} from "./types";

export type InvestigationStatus =
  | "idle"
  | "running"
  | "done"
  | "error";

export interface ToolCallEntry {
  callId: string;
  tool: string;
  args: Record<string, unknown>;
  startedAt: number;
  resultSummary?: string;
  resultMeta?: Record<string, unknown>;
  hasResult: boolean;
}

export interface AgentThought {
  id: number;
  text: string;
  at: number;
}

export interface InvestigationState {
  status: InvestigationStatus;
  query: string | null;
  sessionId: string | null;
  startedAt: number | null;
  finishedAt: number | null;
  errorMessage: string | null;

  thoughts: AgentThought[];
  toolCalls: ToolCallEntry[];
  sanctionsHits: { name: string; hits: SanctionsHit[]; at: number }[];

  /** All unique nodes discovered so far. */
  nodes: Map<string, GraphNode>;
  /** All unique edges (deduped by source+target+type). */
  edges: Map<string, GraphEdge>;

  summary: RiskSummary | null;
}

export function initialState(): InvestigationState {
  return {
    status: "idle",
    query: null,
    sessionId: null,
    startedAt: null,
    finishedAt: null,
    errorMessage: null,
    thoughts: [],
    toolCalls: [],
    sanctionsHits: [],
    nodes: new Map(),
    edges: new Map(),
    summary: null,
  };
}

export type Action =
  | { type: "reset" }
  | { type: "started"; query: string; sessionId: string }
  | { type: "event"; event: StreamEvent }
  | { type: "closed"; reason: "done" | "error" | "network" | "manual" }
  | { type: "fatal"; message: string };

const edgeKey = (e: GraphEdge) => `${e.source}::${e.type}::${e.target}`;
let thoughtCounter = 0;

export function reduce(state: InvestigationState, action: Action): InvestigationState {
  switch (action.type) {
    case "reset":
      return initialState();

    case "started":
      return {
        ...initialState(),
        status: "running",
        query: action.query,
        sessionId: action.sessionId,
        startedAt: Date.now(),
      };

    case "fatal":
      return {
        ...state,
        status: "error",
        errorMessage: action.message,
        finishedAt: Date.now(),
      };

    case "closed":
      if (state.status === "running") {
        return {
          ...state,
          status: action.reason === "done" ? "done" : "error",
          finishedAt: Date.now(),
          errorMessage: action.reason === "done" ? null : state.errorMessage || `stream closed: ${action.reason}`,
        };
      }
      return state;

    case "event":
      return applyEvent(state, action.event);

    default:
      return state;
  }
}

function applyEvent(state: InvestigationState, evt: StreamEvent): InvestigationState {
  switch (evt.type) {
    case "agent_started":
      return state;

    case "agent_thought": {
      const next = { ...state, thoughts: [...state.thoughts, { id: ++thoughtCounter, text: evt.data.text, at: Date.now() }] };
      return next;
    }

    case "tool_call_start": {
      const entry: ToolCallEntry = {
        callId: evt.data.call_id,
        tool: evt.data.tool,
        args: evt.data.args,
        startedAt: Date.now(),
        hasResult: false,
      };
      return { ...state, toolCalls: [...state.toolCalls, entry] };
    }

    case "tool_call_result": {
      const nextCalls = state.toolCalls.map((c) =>
        c.callId === evt.data.call_id
          ? { ...c, hasResult: true, resultSummary: evt.data.summary, resultMeta: evt.data.metadata }
          : c
      );

      // Add a synthetic node for the subject (whichever node the tool was called on)
      // and merge in returned nodes/edges. Subject inference: for tools other than
      // search_entity, the subject is in the tool's args.node_id / entity_id.
      const nodes = new Map(state.nodes);
      const edges = new Map(state.edges);
      for (const n of evt.data.nodes) {
        if (!nodes.has(n.id)) nodes.set(n.id, n);
      }
      for (const e of evt.data.edges) {
        const k = edgeKey(e);
        if (!edges.has(k)) edges.set(k, e);
      }
      return { ...state, toolCalls: nextCalls, nodes, edges };
    }

    case "sanctions_hit":
      return {
        ...state,
        sanctionsHits: [
          ...state.sanctionsHits,
          { name: evt.data.name, hits: evt.data.hits, at: Date.now() },
        ],
      };

    case "summary":
      return { ...state, summary: evt.data.summary };

    case "error":
      return {
        ...state,
        status: "error",
        errorMessage: evt.data.message,
        finishedAt: Date.now(),
      };

    case "done":
      return state; // closed action handles status transition

    default:
      return state;
  }
}
