/**
 * Conversation client for the Entity Risk Resolver.
 *
 * Multi-turn flow:
 *   1. createConversation()                         -> conversation_id
 *   2. sendMessage(id, text, opts)                  -> { turn_index, event_cursor }
 *   3. streamTurn(id, event_cursor, callbacks)      -> SSE for that turn
 *
 * Each turn appends to ONE server-side event list; the stream endpoint takes a
 * `cursor` so a single list serves the whole thread. We open a fresh
 * EventSource per turn (starting at the cursor returned by sendMessage) and let
 * it close on the turn's `done`/`error`.
 */

import type {
  StreamEvent,
  EventType,
  ExpandKind,
  ExpandResponse,
  ConversationHydrate,
  ConversationListItem,
  TreeTurn,
  TurnGraphResponse,
} from "./types";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL ||
  "https://sayari-demo-backend-610839754420.us-central1.run.app";

export interface TurnHandle {
  close: () => void;
}

export interface TurnCallbacks {
  onEvent: (event: StreamEvent) => void;
  onClose?: (reason: "done" | "error" | "network" | "manual") => void;
}

export interface SendMessageOptions {
  pinnedNodeIds?: string[];
  forceRiskReport?: boolean;
  /**
   * Fork/extend: parent the new turn on this prior turn (stage 2a tree).
   * Omitted = linear append on the current head, exactly the old behavior.
   */
  parentTurnId?: string | null;
}

const EVENT_TYPES: EventType[] = [
  "agent_started",
  "agent_thought",
  "token",
  "tool_call_start",
  "tool_call_result",
  "sanctions_hit",
  "sanctions_review",
  "summary",
  "answer",
  "error",
  "done",
];

/**
 * Recent conversations (newest-updated first) from the server-side index.
 * Expired ones are filtered server-side; this is a recents menu, not an
 * archive (24h TTL).
 */
export async function listConversations(limit = 50): Promise<ConversationListItem[]> {
  const resp = await fetch(`${BACKEND_URL}/conversations?limit=${limit}`);
  if (!resp.ok) {
    throw new Error(`list conversations failed: ${resp.status} ${await resp.text()}`);
  }
  const { conversations } = (await resp.json()) as {
    conversations: ConversationListItem[];
  };
  return conversations ?? [];
}

/**
 * Delete a conversation (whole server-side key family + index entry).
 * The server refuses with 409 while a turn is running.
 */
export async function deleteConversation(conversationId: string): Promise<void> {
  const resp = await fetch(`${BACKEND_URL}/conversations/${conversationId}`, {
    method: "DELETE",
  });
  if (!resp.ok) {
    throw new Error(`delete conversation failed: ${resp.status} ${await resp.text()}`);
  }
}

/** Create a new (empty) conversation. Returns its id. */
export async function createConversation(): Promise<string> {
  const resp = await fetch(`${BACKEND_URL}/conversations`, { method: "POST" });
  if (!resp.ok) {
    throw new Error(`/conversations failed: ${resp.status} ${await resp.text()}`);
  }
  const { conversation_id } = (await resp.json()) as { conversation_id: string };
  return conversation_id;
}

/** Submit one user turn. Returns the turn ids and the event cursor to stream from. */
export async function sendMessage(
  conversationId: string,
  message: string,
  opts: SendMessageOptions = {}
): Promise<{
  turnIndex: number;
  eventCursor: number;
  turnId: string | null;
  parentTurnId: string | null;
}> {
  const body: Record<string, unknown> = {
    message,
    pinned_node_ids: opts.pinnedNodeIds ?? [],
    force_risk_report: opts.forceRiskReport ?? false,
  };
  // Only ship the field when forking. Omitting it keeps the old linear
  // contract byte-identical (and stays safe against older backends).
  if (opts.parentTurnId) body.parent_turn_id = opts.parentTurnId;
  const resp = await fetch(`${BACKEND_URL}/conversations/${conversationId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    throw new Error(`send message failed: ${resp.status} ${await resp.text()}`);
  }
  const { turn_index, event_cursor, turn_id, parent_turn_id } =
    (await resp.json()) as {
      turn_index: number;
      event_cursor: number;
      turn_id?: string;
      parent_turn_id?: string | null;
    };
  return {
    turnIndex: turn_index,
    eventCursor: event_cursor,
    turnId: turn_id ?? null,
    parentTurnId: parent_turn_id ?? null,
  };
}

/** Fetch the turn tree. Empty list for pre-branching conversations. */
export async function fetchTree(conversationId: string): Promise<TreeTurn[]> {
  const resp = await fetch(`${BACKEND_URL}/conversations/${conversationId}/tree`);
  if (!resp.ok) {
    throw new Error(`fetch tree failed: ${resp.status} ${await resp.text()}`);
  }
  const { turns } = (await resp.json()) as { turns: TreeTurn[] };
  return turns ?? [];
}

/**
 * Time-travel: the evidence graph accumulated along a turn's root -> turn
 * path, plus the turn's own delta (for pulse-in vs dim rendering).
 */
export async function fetchTurnGraph(
  conversationId: string,
  turnId: string
): Promise<TurnGraphResponse> {
  const resp = await fetch(
    `${BACKEND_URL}/conversations/${conversationId}/turns/${encodeURIComponent(turnId)}/graph`
  );
  if (!resp.ok) {
    throw new Error(`fetch turn graph failed: ${resp.status} ${await resp.text()}`);
  }
  return (await resp.json()) as TurnGraphResponse;
}

/** Open an SSE stream for the current turn, starting at `cursor`. */
export function streamTurn(
  conversationId: string,
  cursor: number,
  callbacks: TurnCallbacks
): TurnHandle {
  const es = new EventSource(
    `${BACKEND_URL}/conversations/${conversationId}/stream?cursor=${cursor}`
  );
  let closed = false;

  // Idempotency across reconnects. The backend replays the event list from the
  // original `cursor` on EVERY (re)connection, and the browser's EventSource
  // silently auto-reconnects on transient drops, so without guarding we'd
  // re-apply already-seen events (duplicate tool calls, doubled token text,
  // repeated thoughts). The endpoint sets no SSE `id:`, so we can't lean on
  // Last-Event-ID; instead we dedupe by position. Each connection re-emits the
  // backend events in the same order, so the i-th backend event of any
  // connection is always backend event `cursor + i`. We track a high-water mark
  // of events already delivered and drop anything at or below it.
  let highWater = 0; // count of backend events delivered to the callback
  let connIndex = 0; // position within the current connection (reset on (re)open)

  const close = (reason: "done" | "error" | "network" | "manual") => {
    if (closed) return;
    closed = true;
    es.close();
    callbacks.onClose?.(reason);
  };

  // A new (or resumed) connection replays from the original cursor.
  es.addEventListener("open", () => {
    connIndex = 0;
  });

  // Handle one real backend SSE message (a named event with a string payload).
  const handleBackendEvent = (type: EventType, raw: string) => {
    const pos = connIndex++;
    if (pos < highWater) {
      // Replayed on reconnect; already applied. Still honor terminal events so
      // a stream that ends mid-replay tears down cleanly.
      if (type === "done") close("done");
      if (type === "error") close("error");
      return;
    }
    highWater = pos + 1;

    let data: unknown;
    try {
      data = JSON.parse(raw);
    } catch {
      if (type === "error") {
        // The error payload isn't guaranteed to be JSON, so treat the raw string
        // as the message rather than throwing/logging spuriously.
        data = { message: raw };
      } else {
        console.warn("SSE: non-JSON data for event", type, raw);
        return;
      }
    }
    callbacks.onEvent({ type, data } as StreamEvent);
    if (type === "done") close("done");
    if (type === "error") close("error");
  };

  EVENT_TYPES.forEach((type) => {
    es.addEventListener(type, (ev: MessageEvent) => {
      // The `error` listener is overloaded: the browser dispatches a
      // connection-level Event here (with no `.data`) on every transient drop,
      // in addition to the backend's application-level "error" SSE event (a
      // MessageEvent carrying a payload). Only the latter has string data; let
      // `onerror` deal with connection-level signals so we don't misparse them.
      if (type === "error" && typeof ev.data !== "string") return;
      handleBackendEvent(type, ev.data);
    });
  });

  es.onerror = () => {
    // EventSource auto-reconnects unless it has permanently closed. Only treat
    // a CLOSED socket as a network failure; otherwise let it resume (the
    // position-based dedupe above keeps the replay idempotent).
    if (es.readyState === EventSource.CLOSED) close("network");
  };

  return { close: () => close("manual") };
}

/** Full hydration payload for restoring a conversation (page reload / share). */
export async function fetchConversation(
  conversationId: string
): Promise<ConversationHydrate> {
  const resp = await fetch(`${BACKEND_URL}/conversations/${conversationId}`);
  if (!resp.ok) {
    throw new Error(`fetch conversation failed: ${resp.status} ${await resp.text()}`);
  }
  return (await resp.json()) as ConversationHydrate;
}

/**
 * Manually expand a graph node via the backend's /expand endpoint (single
 * Cypher query, no agent). Used by the right-click "Expand" menu.
 */
export async function expandNode(
  nodeId: string,
  kind: ExpandKind = "relationships"
): Promise<ExpandResponse> {
  const url = `${BACKEND_URL}/expand/${encodeURIComponent(nodeId)}?kind=${kind}`;
  const resp = await fetch(url);
  if (!resp.ok) {
    throw new Error(`/expand failed: ${resp.status} ${await resp.text()}`);
  }
  return (await resp.json()) as ExpandResponse;
}
