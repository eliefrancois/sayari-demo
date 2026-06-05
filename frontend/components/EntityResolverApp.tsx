"use client";

import { useCallback, useReducer, useRef, useState } from "react";
import { RotateCcw } from "lucide-react";
import { ChatPanel } from "./ChatPanel";
import { ExpandToast } from "./ExpandToast";
import { GraphPanel } from "./GraphPanel";
import { ToolCallFeed } from "./ToolCallFeed";
import {
  activeTurn,
  countExpandDelta,
  initialState,
  reduce,
  type ConversationState,
} from "@/lib/conversation-store";
import {
  createConversation,
  expandNode,
  sendMessage as sendMessageApi,
  streamTurn,
  type TurnHandle,
} from "@/lib/sse-client";
import type { ExpandKind, NodeLabel } from "@/lib/types";

/**
 * Top-level client component. Owns:
 *  - Conversation state (useReducer over the multi-turn store).
 *  - The active turn's SSE handle (closed on new turn / reset / unmount).
 *
 * Conversation flow: first message lazily creates a conversation, then every
 * message POSTs to /messages and opens a fresh SSE stream at the returned
 * cursor. The graph accumulates across turns.
 */
export function EntityResolverApp() {
  const [state, dispatch] = useReducer(reduce, undefined, initialState);
  const handleRef = useRef<TurnHandle | null>(null);
  const [expandToast, setExpandToast] = useState<string | null>(null);
  const [hiddenLabels, setHiddenLabels] = useState<Set<NodeLabel>>(new Set());

  const toggleLabel = useCallback((label: NodeLabel) => {
    setHiddenLabels((prev) => {
      const next = new Set(prev);
      if (next.has(label)) next.delete(label);
      else next.add(label);
      return next;
    });
  }, []);

  // The single send path for every turn. `forceRiskReport` makes the agent
  // produce a full RiskSummary even on a follow-up (used by the "Generate
  // report" button surfaced on an answer).
  const send = useCallback(
    async (text: string, opts?: { forceRiskReport?: boolean }) => {
      handleRef.current?.close();
      handleRef.current = null;

      try {
        let cid = state.conversationId;
        if (!cid) {
          cid = await createConversation();
          dispatch({ type: "conversation_created", conversationId: cid });
        }

        const pinned = Array.from(state.pinnedNodeIds);
        const forceRiskReport = opts?.forceRiskReport ?? false;
        const { turnIndex, eventCursor } = await sendMessageApi(cid, text, {
          pinnedNodeIds: pinned,
          forceRiskReport,
        });

        dispatch({
          type: "turn_sent",
          turnIndex,
          userMessage: text,
          pinnedNodeIds: pinned,
          forceRiskReport,
        });

        handleRef.current = streamTurn(cid, eventCursor, {
          onEvent: (evt) => dispatch({ type: "event", event: evt }),
          onClose: (reason) => dispatch({ type: "closed", reason }),
        });
      } catch (err) {
        dispatch({
          type: "fatal",
          message: err instanceof Error ? err.message : "unknown error sending message",
        });
      }
    },
    [state.conversationId, state.pinnedNodeIds]
  );

  const onSend = useCallback((text: string) => send(text), [send]);
  const onGenerateReport = useCallback(
    (prompt: string) => send(prompt, { forceRiskReport: true }),
    [send]
  );

  const resetAll = useCallback(() => {
    handleRef.current?.close();
    handleRef.current = null;
    dispatch({ type: "reset" });
  }, []);

  const handleExpand = useCallback(
    async (nodeId: string, kind: ExpandKind) => {
      try {
        const resp = await expandNode(nodeId, kind);
        const { newNodes, newEdges } = countExpandDelta(state, resp.nodes, resp.edges);
        if (newNodes === 0 && newEdges === 0) {
          setExpandToast("Added 0 nodes · 0 edges (already on graph)");
          return;
        }
        dispatch({ type: "expand_result", nodes: resp.nodes, edges: resp.edges });
        setExpandToast(
          `Added ${newNodes} node${newNodes === 1 ? "" : "s"} · ${newEdges} edge${newEdges === 1 ? "" : "s"}`
        );
      } catch (err) {
        console.error("expand failed", err);
        setExpandToast("Expand failed — see console");
      }
    },
    [state]
  );

  const highlightNodes = useCallback((nodeIds: string[]) => {
    dispatch({ type: "set_highlight", nodeIds });
  }, []);
  const clearHighlight = useCallback(() => {
    dispatch({ type: "clear_highlight" });
  }, []);
  const focusNode = useCallback((nodeId: string) => {
    dispatch({ type: "focus_node", nodeId });
  }, []);
  const togglePin = useCallback((nodeId: string) => {
    dispatch({ type: "toggle_pin", nodeId });
  }, []);
  const toggleLeadsOverlay = useCallback(() => {
    dispatch({ type: "toggle_leads_overlay" });
  }, []);

  const isRunning = state.status === "running";
  const nodes = Array.from(state.nodes.values());
  const edges = Array.from(state.edges.values());

  return (
    <div className="grid h-screen grid-cols-[minmax(380px,30%)_1fr_minmax(320px,24%)] bg-zinc-950 text-zinc-100">
      {/* Left: chat */}
      <aside className="flex min-h-0 flex-col border-r border-zinc-800">
        <ChatPanel
          state={state}
          onSend={onSend}
          onGenerateReport={onGenerateReport}
          disabled={isRunning}
          onHighlightNodes={highlightNodes}
          onClearHighlight={clearHighlight}
          onFocusNode={focusNode}
          onTogglePin={togglePin}
        />
      </aside>

      {/* Center: graph */}
      <main className="relative flex min-h-0 flex-col">
        <GraphHeader
          state={state}
          onReset={resetAll}
          hiddenLabels={hiddenLabels}
          onToggleLabel={toggleLabel}
        />
        <div className="relative flex-1 min-h-0">
          <GraphPanel
            nodes={nodes}
            edges={edges}
            highlightedNodeIds={state.highlightedNodeIds}
            pinnedNodeIds={state.pinnedNodeIds}
            focusRequest={state.focusRequest}
            hiddenLabels={hiddenLabels}
            onExpandNode={handleExpand}
            onTogglePin={togglePin}
            leadsShown={state.latestSearchMeta?.shown}
            leadsTotal={state.latestSearchMeta?.total}
            overlayLeadNodes={state.unpinnedLeadNodes}
            showOverlayLeads={state.showUnpinnedLeads}
            onToggleLeads={toggleLeadsOverlay}
          />
          <ExpandToast message={expandToast} onDismiss={() => setExpandToast(null)} />
        </div>
      </main>

      {/* Right: tool call feed (current turn) */}
      <aside className="flex min-h-0">
        <ToolCallFeed turn={activeTurn(state)} />
      </aside>
    </div>
  );
}

const LEGEND: { label: NodeLabel; color: string }[] = [
  { label: "Entity", color: "rgb(96 165 250)" },
  { label: "Officer", color: "rgb(251 146 60)" },
  { label: "Intermediary", color: "rgb(167 139 250)" },
  { label: "Address", color: "rgb(74 222 128)" },
  { label: "Other", color: "rgb(161 161 170)" },
];

function GraphHeader({
  state,
  onReset,
  hiddenLabels,
  onToggleLabel,
}: {
  state: ConversationState;
  onReset: () => void;
  hiddenLabels: Set<NodeLabel>;
  onToggleLabel: (label: NodeLabel) => void;
}) {
  const n = state.nodes.size;
  const e = state.edges.size;
  const hasData = state.turns.length > 0 || n > 0;
  return (
    <div className="flex items-center justify-between border-b border-zinc-800 bg-zinc-950/60 px-3 py-2 backdrop-blur">
      <div className="flex items-center gap-4">
        <h1 className="text-sm font-semibold tracking-tight text-zinc-200">
          Entity Risk Resolver
          <span className="ml-2 text-[11px] font-normal text-zinc-500">
            ICIJ Offshore Leaks × OpenSanctions
          </span>
        </h1>
        <div className="hidden items-center gap-2 border-l border-zinc-800 pl-4 lg:flex">
          {LEGEND.map((l) => {
            const hidden = hiddenLabels.has(l.label);
            return (
              <button
                key={l.label}
                type="button"
                onClick={() => onToggleLabel(l.label)}
                title={hidden ? `Show ${l.label} nodes` : `Hide ${l.label} nodes`}
                className={
                  "flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] transition " +
                  (hidden
                    ? "text-zinc-600 line-through opacity-50 hover:opacity-70"
                    : "text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200")
                }
              >
                <span
                  aria-hidden
                  className="h-2.5 w-2.5 rounded-sm"
                  style={{ backgroundColor: l.color, opacity: hidden ? 0.35 : 1 }}
                />
                {l.label}
              </button>
            );
          })}
        </div>
      </div>
      <div className="flex items-center gap-3">
        <span className="text-[11px] tabular-nums text-zinc-500">
          {n} node{n === 1 ? "" : "s"} · {e} edge{e === 1 ? "" : "s"}
        </span>
        <span className="hidden text-[11px] text-zinc-700 xl:inline">
          right-click any node to expand
        </span>
        {hasData && (
          <button
            onClick={onReset}
            title="Clear the conversation and start over"
            className="flex items-center gap-1 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-[11px] text-zinc-300 transition hover:border-zinc-500 hover:bg-zinc-800 hover:text-zinc-100"
          >
            <RotateCcw className="h-3 w-3" /> New investigation
          </button>
        )}
      </div>
    </div>
  );
}
