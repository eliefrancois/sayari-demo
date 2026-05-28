"use client";

import { useCallback, useReducer, useRef } from "react";
import { ChatPanel } from "./ChatPanel";
import { GraphPanel } from "./GraphPanel";
import { ToolCallFeed } from "./ToolCallFeed";
import {
  initialState,
  reduce,
  type InvestigationState,
} from "@/lib/investigation-store";
import { startInvestigation, type InvestigationHandle } from "@/lib/sse-client";

/**
 * Top-level client component. Owns:
 *  - Investigation state (useReducer over the store).
 *  - The active SSE handle (so we can close on unmount or when a new search starts).
 *
 * Children are presentational: they take a state slice + (optional) callbacks.
 */
export function EntityResolverApp() {
  const [state, dispatch] = useReducer(reduce, undefined, initialState);
  const handleRef = useRef<InvestigationHandle | null>(null);

  const startSearch = useCallback(async (name: string) => {
    // Close any prior stream first.
    handleRef.current?.close();
    handleRef.current = null;

    dispatch({ type: "reset" });

    try {
      const handle = await startInvestigation(name, {
        onEvent: (evt) => dispatch({ type: "event", event: evt }),
        onClose: (reason) => dispatch({ type: "closed", reason }),
      });
      handleRef.current = handle;
      dispatch({ type: "started", query: name, sessionId: handle.sessionId });
    } catch (err) {
      dispatch({
        type: "fatal",
        message: err instanceof Error ? err.message : "unknown error starting investigation",
      });
    }
  }, []);

  const isRunning = state.status === "running";
  const nodes = Array.from(state.nodes.values());
  const edges = Array.from(state.edges.values());

  return (
    <div className="grid h-screen grid-cols-[minmax(380px,28%)_1fr_minmax(320px,24%)] bg-zinc-950 text-zinc-100">
      {/* Left: chat */}
      <aside className="flex min-h-0 flex-col border-r border-zinc-800">
        <ChatPanel state={state} onSend={startSearch} disabled={isRunning} />
      </aside>

      {/* Center: graph */}
      <main className="relative flex min-h-0 flex-col">
        <GraphHeader state={state} />
        <div className="flex-1 min-h-0">
          <GraphPanel nodes={nodes} edges={edges} />
        </div>
      </main>

      {/* Right: tool call feed */}
      <aside className="flex min-h-0">
        <ToolCallFeed state={state} />
      </aside>
    </div>
  );
}

function GraphHeader({ state }: { state: InvestigationState }) {
  const n = state.nodes.size;
  const e = state.edges.size;
  return (
    <div className="flex items-center justify-between border-b border-zinc-800 bg-zinc-950/60 px-3 py-2 backdrop-blur">
      <h1 className="text-sm font-semibold tracking-tight text-zinc-200">
        Entity Risk Resolver
        <span className="ml-2 text-[11px] font-normal text-zinc-500">
          ICIJ Offshore Leaks × OpenSanctions
        </span>
      </h1>
      <span className="text-[11px] tabular-nums text-zinc-500">
        {n} node{n === 1 ? "" : "s"} · {e} edge{e === 1 ? "" : "s"}
      </span>
    </div>
  );
}
