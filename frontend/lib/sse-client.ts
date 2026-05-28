/**
 * SSE client for the Entity Risk Resolver agent stream.
 *
 * Browser EventSource is the standard primitive. We thin-wrap it to:
 *   - Do the POST /assess handshake first.
 *   - Subscribe to typed events via a callback object.
 *   - Surface close conditions (done | error | network failure) cleanly.
 *
 * Usage:
 *   const handle = startInvestigation("Sergey Roldugin", {
 *     onEvent: (evt) => console.log(evt),
 *     onClose: (reason) => console.log("closed:", reason),
 *   });
 *   // ... later:
 *   handle.close();
 */

import type { StreamEvent, EventType } from "./types";

const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL ||
  "https://sayari-demo-backend-610839754420.us-central1.run.app";

export interface InvestigationHandle {
  sessionId: string;
  close: () => void;
}

export interface InvestigationCallbacks {
  /** Called once per inbound event, in receive order. */
  onEvent: (event: StreamEvent) => void;
  /** Called exactly once when the stream terminates. */
  onClose?: (reason: "done" | "error" | "network" | "manual") => void;
}

/** All event types the backend can send. Used to bind EventSource listeners. */
const EVENT_TYPES: EventType[] = [
  "agent_started",
  "agent_thought",
  "tool_call_start",
  "tool_call_result",
  "sanctions_hit",
  "summary",
  "error",
  "done",
];

/**
 * Kick off an investigation and stream events to callbacks.
 *
 * The returned promise resolves once the handshake succeeds (we have a session_id)
 * and the SSE connection is open. After that, events arrive via the callbacks.
 */
export async function startInvestigation(
  name: string,
  callbacks: InvestigationCallbacks
): Promise<InvestigationHandle> {
  // 1. POST /assess -> session_id
  const resp = await fetch(`${BACKEND_URL}/assess`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!resp.ok) {
    throw new Error(`/assess failed: ${resp.status} ${await resp.text()}`);
  }
  const { session_id } = (await resp.json()) as { session_id: string };

  // 2. Open SSE on /stream/:id
  const es = new EventSource(`${BACKEND_URL}/stream/${session_id}`);
  let closed = false;

  const close = (reason: "done" | "error" | "network" | "manual") => {
    if (closed) return;
    closed = true;
    es.close();
    callbacks.onClose?.(reason);
  };

  // Bind one listener per event type. EventSource dispatches by `event:` field;
  // we wrap into our typed StreamEvent union before handing to onEvent.
  EVENT_TYPES.forEach((type) => {
    es.addEventListener(type, (ev: MessageEvent) => {
      let data: unknown = {};
      try {
        data = JSON.parse(ev.data);
      } catch {
        // backend always sends valid JSON; if not, log and skip
        console.warn("SSE: non-JSON data for event", type, ev.data);
        return;
      }
      const evt = { type, data } as StreamEvent;
      callbacks.onEvent(evt);

      if (type === "done") close("done");
      if (type === "error") close("error");
    });
  });

  // EventSource onerror fires for both transient retries and permanent failures.
  // We can't reliably distinguish them, so we close on the first error after
  // a small grace period (lets the initial connection settle).
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED) close("network");
  };

  return { sessionId: session_id, close: () => close("manual") };
}
