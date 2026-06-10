"use client";

import { useCallback, useMemo, useReducer, useRef, useState } from "react";
import { RotateCcw } from "lucide-react";
import { ChatPanel } from "./ChatPanel";
import { ExpandToast } from "./ExpandToast";
import { GraphPanel } from "./GraphPanel";
import { TradeRoutesMap } from "./TradeRoutesMap";
import {
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
import { collectTradeRoutes, collectTradeSubjects } from "@/lib/map/trade-routes";
import type { ExpandKind, NodeLabel } from "@/lib/types";

/** Which lens the evidence pane shows: the React Flow graph or the routes map. */
type CenterView = "graph" | "map";

/**
 * Top-level client component. Owns:
 *  - Conversation state (useReducer over the multi-turn store).
 *  - The active turn's SSE handle (closed on new turn / reset / unmount).
 *
 * Conversation flow: first message lazily creates a conversation, then every
 * message POSTs to /messages and opens a fresh SSE stream at the returned
 * cursor. The graph accumulates across turns.
 *
 * Layout (lmcanvas reskin, spec §3): thin top header, then a split pane —
 * INVESTIGATION (~40%, the conversation cards) | EVIDENCE GRAPH (~60%, the
 * React Flow graph with the Graph|Map lens). Tool calls render inline in the
 * conversation cards, so the old Tool Feed panel is gone.
 */
export function EntityResolverApp() {
  const [state, dispatch] = useReducer(reduce, undefined, initialState);
  const handleRef = useRef<TurnHandle | null>(null);
  const [expandToast, setExpandToast] = useState<string | null>(null);
  const [hiddenLabels, setHiddenLabels] = useState<Set<NodeLabel>>(new Set());
  const [centerView, setCenterView] = useState<CenterView>("graph");

  // Trade routes for the map lens, derived from sayari_trade results already
  // in the conversation (tool-result metadata, with a graph-edge fallback).
  const tradeRoutes = useMemo(
    () => collectTradeRoutes(state.turns, state.nodes, state.edges),
    [state.turns, state.nodes, state.edges]
  );
  const tradeSubjects = useMemo(
    () => collectTradeSubjects(state.turns, state.nodes),
    [state.turns, state.nodes]
  );

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
    setCenterView("graph");
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
    <div className="flex h-screen flex-col bg-background text-foreground">
      <AppHeader state={state} onReset={resetAll} />

      <div className="grid min-h-0 flex-1 grid-cols-[minmax(420px,40%)_1fr]">
        {/* Left: INVESTIGATION — the conversation cards */}
        <aside className="flex min-h-0 flex-col border-r border-border">
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

        {/* Right: EVIDENCE GRAPH — graph with a map lens for trade routes */}
        <main className="relative flex min-h-0 flex-col">
          <EvidencePaneHeader
            state={state}
            hiddenLabels={hiddenLabels}
            onToggleLabel={toggleLabel}
            view={centerView}
            onViewChange={setCenterView}
            routeCount={tradeRoutes.length}
          />
          <div className="relative min-h-0 flex-1">
            {/* GraphPanel stays mounted under the map so the force layout and
                user-dragged positions survive flipping lenses. */}
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
            {centerView === "map" && (
              <div className="absolute inset-0 z-10">
                <TradeRoutesMap routes={tradeRoutes} subjects={tradeSubjects} />
              </div>
            )}
            <ExpandToast message={expandToast} onDismiss={() => setExpandToast(null)} />
          </div>
        </main>
      </div>
    </div>
  );
}

/** Thin top header: product name, data-route chip (pixel accent), reset. */
function AppHeader({
  state,
  onReset,
}: {
  state: ConversationState;
  onReset: () => void;
}) {
  const hasData = state.turns.length > 0 || state.nodes.size > 0;
  return (
    <header className="flex shrink-0 items-center justify-between border-b border-border bg-background px-4 py-2">
      <div className="flex items-center gap-3">
        <h1 className="text-[13px] font-semibold tracking-tight text-foreground">
          Entity Risk Resolver
        </h1>
        <span className="font-pixel rounded-md border border-border bg-muted px-2 py-0.5 text-[9px] uppercase tracking-[0.14em] text-muted-foreground">
          icij · opensanctions · sayari
        </span>
      </div>
      {hasData && (
        <button
          onClick={onReset}
          title="Clear the conversation and start over"
          className="flex cursor-pointer items-center gap-1.5 rounded-md border border-border bg-card px-2 py-1 font-mono text-[9px] font-medium uppercase tracking-[0.16em] text-foreground shadow-sm transition-colors hover:bg-muted"
        >
          <RotateCcw className="h-3 w-3" /> New investigation
        </button>
      )}
    </header>
  );
}

const LABEL_TOGGLES: NodeLabel[] = [
  "Entity",
  "Officer",
  "Intermediary",
  "Address",
  "Other",
];

/** EVIDENCE GRAPH pane header: label, node-type filters, Graph|Map lens. */
function EvidencePaneHeader({
  state,
  hiddenLabels,
  onToggleLabel,
  view,
  onViewChange,
  routeCount,
}: {
  state: ConversationState;
  hiddenLabels: Set<NodeLabel>;
  onToggleLabel: (label: NodeLabel) => void;
  view: CenterView;
  onViewChange: (view: CenterView) => void;
  routeCount: number;
}) {
  const n = state.nodes.size;
  const e = state.edges.size;
  return (
    <div className="flex items-center justify-between border-b border-border bg-background px-4 py-2">
      <div className="flex min-w-0 items-center gap-3">
        <h2 className="shrink-0 font-mono text-[9px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
          Evidence graph
        </h2>
        <span className="shrink-0 font-mono text-[9px] tabular-nums uppercase tracking-[0.14em] text-muted-foreground/70">
          {n} nodes · {e} edges
        </span>
        <div className="hidden items-center gap-1 border-l border-border pl-3 lg:flex">
          {LABEL_TOGGLES.map((label) => {
            const hidden = hiddenLabels.has(label);
            return (
              <button
                key={label}
                type="button"
                onClick={() => onToggleLabel(label)}
                title={hidden ? `Show ${label} nodes` : `Hide ${label} nodes`}
                className={
                  "cursor-pointer rounded-md px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-[0.12em] transition-colors " +
                  (hidden
                    ? "text-muted-foreground/50 line-through hover:text-muted-foreground"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground")
                }
              >
                {label}
              </button>
            );
          })}
        </div>
      </div>
      <div className="flex items-center gap-3">
        <span className="hidden font-mono text-[9px] uppercase tracking-[0.12em] text-muted-foreground/50 xl:inline">
          right-click a node to expand
        </span>
        {/* Graph | Map lens toggle */}
        <div className="flex items-center overflow-hidden rounded-md border border-border shadow-sm">
          {(["graph", "map"] as const).map((v) => (
            <button
              key={v}
              type="button"
              onClick={() => onViewChange(v)}
              title={v === "map" ? "Trade routes on a world map" : "Network graph"}
              className={
                "cursor-pointer px-2 py-1 font-mono text-[9px] font-medium uppercase tracking-[0.14em] transition-colors " +
                (view === v
                  ? "bg-foreground text-background"
                  : "bg-card text-muted-foreground hover:bg-muted hover:text-foreground")
              }
            >
              {v}
              {v === "map" && routeCount > 0 && (
                <span
                  className={
                    "ml-1 rounded-full px-1 text-[8px] tabular-nums " +
                    (view === "map"
                      ? "bg-background/25 text-background"
                      : "bg-muted text-foreground")
                  }
                >
                  {routeCount}
                </span>
              )}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
