/**
 * Multi-turn conversation store. Pure data shape + reducer; EntityResolverApp
 * owns it via useReducer and panels read slices.
 *
 * Shape vs the old single-shot investigation store:
 *   - State holds an ordered list of `turns`. Each turn is one user message and
 *     the agent's response to it (an investigation RiskSummary OR a lighter
 *     TurnAnswer for clarifications / follow-ups).
 *   - The graph (nodes/edges) accumulates at the CONVERSATION level across all
 *     turns, mirroring the backend's stored graph. Follow-ups grow the same
 *     canvas instead of resetting it.
 *   - Every SSE event carries `turn_index`, so the reducer routes events to the
 *     turn they belong to.
 *   - `pinnedNodeIds` is user-curated context: right-click a node -> "add to
 *     context", and the next message ships those ids so the agent focuses there.
 */

import type {
  ConversationHydrate,
  GraphEdge,
  GraphNode,
  LeadNode,
  RegistryEntity,
  RiskSummary,
  SanctionsHit,
  SanctionsReview,
  StreamEvent,
  TurnAnswer,
} from "./types";

export type ConversationStatus = "idle" | "running" | "done" | "error";
export type TurnStatus = "running" | "done" | "error";
export type TurnKind = "pending" | "investigation" | "answer";

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

export interface Turn {
  index: number;
  /**
   * Stable server turn id (stage 2a tree). Null only for legacy turns that
   * predate branching — those render as a flat vertical chain and can't be
   * forked from or time-traveled to.
   */
  turnId: string | null;
  /**
   * Stable CLIENT-side identity (`pending:<nonce>`), set when a turn is
   * dispatched optimistically before the server assigns its turnId. The
   * canvas keys node positions on this so the card doesn't jump or re-place
   * when the server id arrives (the temp -> server reconcile). Null on
   * hydrated turns — those key on turnId directly.
   */
  clientId: string | null;
  /** Parent turn in the tree. Null for the root (and all legacy turns). */
  parentTurnId: string | null;
  userMessage: string;
  pinnedNodeIds: string[];
  forceRiskReport: boolean;
  status: TurnStatus;
  kind: TurnKind;
  /** Guarded report-ready flag restored from the tree on reload (live turns
   *  carry it on `answer.report_ready`). */
  reportReady: boolean;
  thoughts: AgentThought[];
  /** Live text of the response currently being generated (token stream).
   *  Cleared when that response finalizes into a thought / answer / summary. */
  streamingText: string;
  toolCalls: ToolCallEntry[];
  sanctionsHits: { name: string; hits: SanctionsHit[]; at: number }[];
  sanctionsReview: SanctionsReview | null;
  summary: RiskSummary | null;
  answer: TurnAnswer | null;
  startedAt: number;
  finishedAt: number | null;
}

export interface ConversationState {
  conversationId: string | null;
  /** Status of the most recent / active turn. */
  status: ConversationStatus;
  turns: Turn[];

  /**
   * The selected card on the investigation canvas. Null = follow the live
   * head (the most recently created turn), which is the default linear-append
   * behavior. A non-null selection drives both the composer's parent_turn_id
   * and the evidence graph's time-travel scope.
   */
  activeTurnId: string | null;

  /** All unique nodes discovered across the whole conversation. */
  nodes: Map<string, GraphNode>;
  /** All unique edges (deduped by source+type+target). */
  edges: Map<string, GraphEdge>;

  highlightedNodeIds: Set<string>;
  focusRequest: { nodeId: string; tick: number } | null;
  /** User-pinned nodes shipped as context with the next message. */
  pinnedNodeIds: Set<string>;

  /**
   * Lead counts from the most recent broad sayari_search: how many leads are
   * pinned to the canvas (`shown`) out of the total found (`total`). Drives the
   * "Showing N of M leads" badge on the graph. Null until a search runs.
   */
  latestSearchMeta: { shown: number; total: number } | null;

  /**
   * The UNPINNED leads from the most recent broad search — rendered only as a
   * transient overlay when `showUnpinnedLeads` is true (clicking the badge).
   * Deliberately kept OUT of `nodes`/`edges` so they never pollute the
   * persistent, accumulated conversation graph or get inherited next turn.
   */
  unpinnedLeadNodes: LeadNode[];
  /** Whether the unpinned-leads overlay is currently shown (badge toggle). */
  showUnpinnedLeads: boolean;

  /**
   * The backend's unified id-keyed entity registry (state_doc.entities),
   * populated on hydrate and refreshed after each completed turn. Feeds the
   * composer autocomplete corpus and the entity detail panel — entities that
   * surfaced through tools (search leads, sanctions hits) live here even when
   * they never landed on the evidence graph.
   */
  registry: Map<string, RegistryEntity>;

  errorMessage: string | null;
}

export function initialState(): ConversationState {
  return {
    conversationId: null,
    status: "idle",
    turns: [],
    activeTurnId: null,
    nodes: new Map(),
    edges: new Map(),
    highlightedNodeIds: new Set(),
    focusRequest: null,
    pinnedNodeIds: new Set(),
    latestSearchMeta: null,
    unpinnedLeadNodes: [],
    showUnpinnedLeads: false,
    registry: new Map(),
    errorMessage: null,
  };
}

export type Action =
  | { type: "reset" }
  | { type: "conversation_created"; conversationId: string }
  | {
      /**
       * Optimistic append: the provisional turn renders the instant the user
       * sends, BEFORE createConversation / POST /messages round-trip. The
       * turn carries a stable `clientId` and no server turnId yet.
       */
      type: "turn_pending";
      clientId: string;
      parentTurnId: string | null;
      userMessage: string;
      pinnedNodeIds: string[];
      forceRiskReport: boolean;
    }
  | {
      /** Reconcile the provisional turn with the server-assigned identity. */
      type: "turn_resolved";
      clientId: string;
      turnIndex: number;
      turnId: string | null;
      parentTurnId: string | null;
    }
  | { type: "registry"; entities: Record<string, RegistryEntity> }
  | { type: "select_turn"; turnId: string | null }
  | { type: "event"; event: StreamEvent }
  | { type: "closed"; reason: "done" | "error" | "network" | "manual" }
  | { type: "fatal"; message: string }
  | { type: "hydrate"; payload: ConversationHydrate }
  | { type: "expand_result"; nodes: GraphNode[]; edges: GraphEdge[] }
  | { type: "set_highlight"; nodeIds: string[] }
  | { type: "clear_highlight" }
  | { type: "focus_node"; nodeId: string }
  | { type: "toggle_pin"; nodeId: string }
  | { type: "clear_pins" }
  | { type: "toggle_leads_overlay" };

let focusTick = 0;
let thoughtCounter = 0;

export const edgeKey = (e: GraphEdge) => `${e.source}::${e.type}::${e.target}`;

export function countExpandDelta(
  state: ConversationState,
  nodes: GraphNode[],
  edges: GraphEdge[]
): { newNodes: number; newEdges: number } {
  let newNodes = 0;
  for (const n of nodes) if (!state.nodes.has(n.id)) newNodes++;
  let newEdges = 0;
  for (const e of edges) if (!state.edges.has(edgeKey(e))) newEdges++;
  return { newNodes, newEdges };
}

/** The most recent turn, or null. */
export function activeTurn(state: ConversationState): Turn | null {
  return state.turns.length ? state.turns[state.turns.length - 1] : null;
}

/** The live head: the most recently created turn (last in creation order). */
export function liveHeadTurn(state: ConversationState): Turn | null {
  return activeTurn(state);
}

/** Lookup a turn by its server turn id. */
export function turnById(state: ConversationState, turnId: string): Turn | null {
  return state.turns.find((t) => t.turnId === turnId) ?? null;
}

/**
 * The ACTIVE PATH: root -> turn, walking parent pointers. For legacy turns
 * without ids the chain degenerates to "every turn up to this index" (the
 * linear, pre-branching view). Returns [] when turnId is unknown.
 */
export function pathToRoot(state: ConversationState, turnId: string): Turn[] {
  const start = turnById(state, turnId);
  if (!start) return [];
  const byId = new Map<string, Turn>();
  for (const t of state.turns) if (t.turnId) byId.set(t.turnId, t);
  const path: Turn[] = [start];
  let cursor: Turn | undefined = start;
  // Guard against cycles (corrupt data) by capping at the turn count.
  for (let hops = 0; hops < state.turns.length && cursor; hops++) {
    if (!cursor.parentTurnId) break;
    cursor = byId.get(cursor.parentTurnId);
    if (cursor) path.unshift(cursor);
  }
  return path;
}

/**
 * The turn the composer should parent on: the explicit selection if any,
 * otherwise null (= linear append on the server-side head, today's behavior).
 */
export function composerParentTurnId(state: ConversationState): string | null {
  return state.activeTurnId;
}

function newTurn(
  index: number,
  userMessage: string,
  pinnedNodeIds: string[],
  forceRiskReport: boolean,
  turnId: string | null = null,
  parentTurnId: string | null = null,
  clientId: string | null = null
): Turn {
  return {
    index,
    turnId,
    clientId,
    parentTurnId,
    reportReady: false,
    userMessage,
    pinnedNodeIds,
    forceRiskReport,
    status: "running",
    kind: "pending",
    thoughts: [],
    streamingText: "",
    toolCalls: [],
    sanctionsHits: [],
    sanctionsReview: null,
    summary: null,
    answer: null,
    startedAt: Date.now(),
    finishedAt: null,
  };
}

export function reduce(state: ConversationState, action: Action): ConversationState {
  switch (action.type) {
    case "reset":
      return initialState();

    case "conversation_created":
      return { ...state, conversationId: action.conversationId };

    case "turn_pending": {
      // Provisional index: one past the highest known. The server's value
      // arrives with turn_resolved (they agree except in pathological races).
      const nextIndex = state.turns.length
        ? Math.max(...state.turns.map((t) => t.index)) + 1
        : 0;
      return {
        ...state,
        status: "running",
        errorMessage: null,
        turns: [
          ...state.turns,
          newTurn(
            nextIndex,
            action.userMessage,
            action.pinnedNodeIds,
            action.forceRiskReport,
            null,
            action.parentTurnId,
            action.clientId
          ),
        ],
        // The new turn is the live head now — clear the selection so the
        // graph returns to live mode and streaming renders normally.
        activeTurnId: null,
        // Pins are consumed by the turn; clear so they don't leak forward.
        pinnedNodeIds: new Set(),
      };
    }

    case "turn_resolved":
      return {
        ...state,
        turns: state.turns.map((t) =>
          t.clientId === action.clientId
            ? {
                ...t,
                index: action.turnIndex,
                turnId: action.turnId,
                // The server resolves the actual parent (the current head
                // when we omitted one) — store its answer, not our request.
                parentTurnId: action.parentTurnId,
              }
            : t
        ),
      };

    case "registry": {
      const registry = new Map<string, RegistryEntity>();
      for (const [id, rec] of Object.entries(action.entities)) {
        if (rec) registry.set(id, rec);
      }
      return { ...state, registry };
    }

    case "select_turn":
      return state.activeTurnId === action.turnId
        ? state
        : { ...state, activeTurnId: action.turnId };

    case "fatal":
      return {
        ...state,
        status: "error",
        errorMessage: action.message,
        turns: patchActive(state.turns, (t) => ({
          ...t,
          status: "error",
          finishedAt: Date.now(),
        })),
      };

    case "closed": {
      if (state.status !== "running") return state;
      const reason = action.reason;
      return {
        ...state,
        status: reason === "done" ? "done" : "error",
        errorMessage:
          reason === "done" ? null : state.errorMessage || `stream closed: ${reason}`,
        turns: patchActive(state.turns, (t) =>
          t.status === "running"
            ? {
                ...t,
                status: reason === "done" ? "done" : "error",
                finishedAt: Date.now(),
              }
            : t
        ),
      };
    }

    case "event":
      return applyEvent(state, action.event);

    case "hydrate":
      return hydrate(state, action.payload);

    case "expand_result": {
      const nodes = new Map(state.nodes);
      const edges = new Map(state.edges);
      const newIds: string[] = [];
      for (const n of action.nodes) {
        if (!nodes.has(n.id)) {
          nodes.set(n.id, n);
          newIds.push(n.id);
        }
      }
      for (const e of action.edges) {
        const k = edgeKey(e);
        if (!edges.has(k)) edges.set(k, e);
      }
      return { ...state, nodes, edges, highlightedNodeIds: new Set(newIds) };
    }

    case "set_highlight":
      return { ...state, highlightedNodeIds: new Set(action.nodeIds) };

    case "clear_highlight":
      return state.highlightedNodeIds.size === 0
        ? state
        : { ...state, highlightedNodeIds: new Set() };

    case "focus_node":
      return {
        ...state,
        highlightedNodeIds: new Set([action.nodeId]),
        focusRequest: { nodeId: action.nodeId, tick: ++focusTick },
      };

    case "toggle_pin": {
      const next = new Set(state.pinnedNodeIds);
      if (next.has(action.nodeId)) next.delete(action.nodeId);
      else next.add(action.nodeId);
      return { ...state, pinnedNodeIds: next };
    }

    case "clear_pins":
      return state.pinnedNodeIds.size === 0
        ? state
        : { ...state, pinnedNodeIds: new Set() };

    case "toggle_leads_overlay":
      // No unpinned leads to reveal -> no-op (badge isn't actionable).
      return state.unpinnedLeadNodes.length === 0
        ? state
        : { ...state, showUnpinnedLeads: !state.showUnpinnedLeads };

    default:
      return state;
  }
}

/** Immutably replace the most recent turn via an updater. */
function patchActive(turns: Turn[], fn: (t: Turn) => Turn): Turn[] {
  if (!turns.length) return turns;
  const last = turns.length - 1;
  return turns.map((t, i) => (i === last ? fn(t) : t));
}

type TurnRoute = { turnId?: string; turnIndex?: number };

/**
 * Immutably update the turn an event belongs to. Routing precedence:
 * `turn_id` (stage 2a — branch-safe, every SSE event carries it), then
 * `turn_index` (legacy linear), then the last turn.
 */
function patchTurn(turns: Turn[], route: TurnRoute, fn: (t: Turn) => Turn): Turn[] {
  if (!turns.length) return turns;
  let target = -1;
  if (route.turnId) {
    target = turns.findIndex((t) => t.turnId === route.turnId);
  }
  if (target < 0 && typeof route.turnIndex === "number") {
    target = turns.findIndex((t) => t.index === route.turnIndex);
  }
  if (target < 0) target = turns.length - 1;
  return turns.map((t, i) => (i === target ? fn(t) : t));
}

function applyEvent(state: ConversationState, evt: StreamEvent): ConversationState {
  const ti: TurnRoute = {
    turnId: evt.data.turn_id,
    turnIndex: evt.data.turn_index,
  };

  switch (evt.type) {
    case "agent_started":
      return state;

    case "token":
      // Append a live text delta to the response currently generating.
      return {
        ...state,
        turns: patchTurn(state.turns, ti, (t) => ({
          ...t,
          streamingText: t.streamingText + evt.data.delta,
        })),
      };

    case "agent_thought":
      // The streaming response just finalized into a durable reasoning step;
      // commit it to the timeline and clear the live buffer.
      return {
        ...state,
        turns: patchTurn(state.turns, ti, (t) => ({
          ...t,
          streamingText: "",
          thoughts: [
            ...t.thoughts,
            { id: ++thoughtCounter, text: evt.data.text, at: Date.now() },
          ],
        })),
      };

    case "tool_call_start":
      // Upsert by call_id. The same tool call can surface more than once —
      // a replayed event after an SSE reconnect, or a backend re-emit — and
      // appending blindly would put two entries with the same `callId` in the
      // list, producing duplicate React keys. Merge into the existing entry
      // (preserving any result already attached) instead of pushing.
      return {
        ...state,
        turns: patchTurn(state.turns, ti, (t) => {
          const existing = t.toolCalls.findIndex((c) => c.callId === evt.data.call_id);
          const entry: ToolCallEntry = {
            callId: evt.data.call_id,
            tool: evt.data.tool,
            args: evt.data.args,
            startedAt: existing >= 0 ? t.toolCalls[existing].startedAt : Date.now(),
            hasResult: existing >= 0 ? t.toolCalls[existing].hasResult : false,
            resultSummary: existing >= 0 ? t.toolCalls[existing].resultSummary : undefined,
            resultMeta: existing >= 0 ? t.toolCalls[existing].resultMeta : undefined,
          };
          return {
            ...t,
            streamingText: "",
            toolCalls:
              existing >= 0
                ? t.toolCalls.map((c, i) => (i === existing ? entry : c))
                : [...t.toolCalls, entry],
          };
        }),
      };

    case "tool_call_result": {
      const nodes = new Map(state.nodes);
      const edges = new Map(state.edges);
      for (const n of evt.data.nodes) if (!nodes.has(n.id)) nodes.set(n.id, n);
      for (const e of evt.data.edges) {
        const k = edgeKey(e);
        if (!edges.has(k)) edges.set(k, e);
      }
      // A broad search reports how many leads it pinned vs found; carry the
      // latest so the graph can render a "Showing N of M leads" badge, and stash
      // the UNPINNED leads as an overlay-ready set (kept out of nodes/edges).
      let latestSearchMeta = state.latestSearchMeta;
      let unpinnedLeadNodes = state.unpinnedLeadNodes;
      let showUnpinnedLeads = state.showUnpinnedLeads;
      if (evt.data.tool === "sayari_search") {
        const meta = evt.data.metadata || {};
        const shown = Number(meta.shown_on_graph);
        const total = Number(meta.count);
        if (Number.isFinite(shown) && Number.isFinite(total)) {
          latestSearchMeta = { shown, total };
        }
        // Replace the overlay set with THIS search's unpinned leads and reset
        // the toggle so a fresh search never shows a stale overlay.
        unpinnedLeadNodes = (evt.data.all_lead_nodes ?? []).filter((n) => !n.pinned);
        showUnpinnedLeads = false;
      }
      return {
        ...state,
        nodes,
        edges,
        latestSearchMeta,
        unpinnedLeadNodes,
        showUnpinnedLeads,
        turns: patchTurn(state.turns, ti, (t) => ({
          ...t,
          toolCalls: t.toolCalls.map((c) =>
            c.callId === evt.data.call_id
              ? {
                  ...c,
                  hasResult: true,
                  resultSummary: evt.data.summary,
                  resultMeta: evt.data.metadata,
                }
              : c
          ),
        })),
      };
    }

    case "sanctions_hit":
      return {
        ...state,
        turns: patchTurn(state.turns, ti, (t) => ({
          ...t,
          sanctionsHits: [
            ...t.sanctionsHits,
            { name: evt.data.name, hits: evt.data.hits, at: Date.now() },
          ],
        })),
      };

    case "summary":
      return {
        ...state,
        turns: patchTurn(state.turns, ti, (t) => ({
          ...t,
          kind: "investigation",
          streamingText: "",
          summary: evt.data.summary,
        })),
      };

    case "answer":
      return {
        ...state,
        turns: patchTurn(state.turns, ti, (t) => ({
          ...t,
          kind: "answer",
          streamingText: "",
          answer: evt.data.answer,
          reportReady: evt.data.answer.report_ready ?? false,
        })),
      };

    case "sanctions_review":
      return {
        ...state,
        turns: patchTurn(state.turns, ti, (t) => ({
          ...t,
          sanctionsReview: evt.data.review,
        })),
      };

    case "error":
      return {
        ...state,
        status: "error",
        errorMessage: evt.data.message,
        turns: patchTurn(state.turns, ti, (t) => ({
          ...t,
          status: "error",
          finishedAt: Date.now(),
        })),
      };

    case "done":
      return state; // 'closed' action transitions status

    default:
      return state;
  }
}

/** Rebuild conversation state from a hydration payload (page reload / resume). */
function hydrate(state: ConversationState, p: ConversationHydrate): ConversationState {
  const nodes = new Map<string, GraphNode>();
  for (const n of p.graph?.nodes ?? []) nodes.set(n.id, n);
  const edges = new Map<string, GraphEdge>();
  for (const e of p.graph?.edges ?? []) edges.set(edgeKey(e), e);

  // Tree metadata by turn_index (stage 2a). Pre-branching conversations have
  // an empty/absent tree; their turns keep null ids and render as the flat
  // vertical chain, exactly the old look.
  const treeByIndex = new Map<number, NonNullable<ConversationHydrate["tree"]>[number]>();
  for (const entry of p.tree ?? []) treeByIndex.set(entry.turn_index, entry);

  // Reconstruct turns from the persisted turn list, attaching the matching
  // summary/answer in order. (We don't persist per-turn thoughts/toolCalls;
  // reload shows results, not the live reasoning trail.)
  let summaryCursor = 0;
  let answerCursor = 0;
  const turns: Turn[] = (p.turns ?? []).map((tmeta) => {
    const tree = treeByIndex.get(tmeta.turn_index);
    const t = newTurn(
      tmeta.turn_index,
      tmeta.user_message,
      [],
      false,
      tree?.turn_id ?? null,
      tree?.parent_turn_id ?? null
    );
    t.status = "done";
    t.finishedAt = t.startedAt;
    t.reportReady = tree?.report_ready ?? false;
    if (tmeta.kind === "investigation") {
      t.kind = "investigation";
      t.summary = p.summaries?.[summaryCursor++] ?? null;
    } else {
      t.kind = "answer";
      t.answer = p.answers?.[answerCursor++] ?? null;
    }
    return t;
  });

  const registry = new Map<string, RegistryEntity>();
  for (const [id, rec] of Object.entries(p.state_doc?.entities ?? {})) {
    if (rec) registry.set(id, rec);
  }

  return {
    ...initialState(),
    conversationId: p.conversation_id,
    // A hydrated "running" state has no live stream to re-attach, so treat it
    // as done — otherwise the composer would be locked forever.
    status: "done",
    turns,
    nodes,
    edges,
    registry,
  };
}
