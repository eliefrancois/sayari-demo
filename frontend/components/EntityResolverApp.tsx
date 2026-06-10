"use client";

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { PanelLeft, RotateCcw, Undo2 } from "lucide-react";
import { Group, Panel, Separator, usePanelRef } from "react-resizable-panels";
import { EntityDetailPanel, type EntityRelationship } from "./EntityDetailPanel";
import { ExpandToast } from "./ExpandToast";
import { GraphPanel } from "./GraphPanel";
import { TradeRoutesMap } from "./TradeRoutesMap";
import { InvestigationCanvas } from "./canvas/InvestigationCanvas";
import { ConversationManager } from "./manager/ConversationManager";
import { EntityInteractionContext } from "./ui/markdown";
import {
  buildEntityLookup,
  matchEntity,
  normalizeEntityName,
  type EntityMatch,
} from "@/lib/entity-lookup";
import {
  countExpandDelta,
  edgeKey,
  initialState,
  liveHeadTurn,
  pathToRoot,
  reduce,
  turnById,
  type ConversationState,
} from "@/lib/conversation-store";
import {
  createConversation,
  expandNode,
  fetchConversation,
  fetchTurnGraph,
  sendMessage as sendMessageApi,
  streamTurn,
  type TurnHandle,
} from "@/lib/sse-client";
import { collectTradeRoutes, collectTradeSubjects } from "@/lib/map/trade-routes";
import type { ExpandKind, GraphEdge, GraphNode, NodeLabel } from "@/lib/types";

/** Which lens the evidence pane shows: the React Flow graph or the routes map. */
type CenterView = "graph" | "map";

/** localStorage key for resuming the conversation across reloads. */
const CONVERSATION_KEY = "err:conversation_id";

/**
 * localStorage key for the canvas/graph split ratio. Persistence is applied
 * imperatively after mount (panel ref resize) rather than via useDefaultLayout
 * so server and first client render agree — reading localStorage during render
 * would cause a hydration mismatch.
 */
const PANEL_LAYOUT_KEY = "err:panel-split";

/**
 * The evidence graph regenerated to a selected turn's path-accumulated state
 * (GET /turns/{id}/graph). `deltaNodeIds`/`deltaEdgeKeys` mark the turn's own
 * contribution (pulse-in); the rest of the graph is inherited (dimmed).
 */
type ScopedGraph = {
  turnId: string;
  turnIndex: number;
  pathLength: number;
  nodes: GraphNode[];
  edges: GraphEdge[];
  deltaNodeIds: Set<string>;
  deltaEdgeKeys: Set<string>;
  tick: number;
};

/**
 * Top-level client component. Owns:
 *  - Conversation state (useReducer over the multi-turn tree store).
 *  - The active turn's SSE handle (closed on new turn / reset / unmount).
 *  - The time-travel scope (selected turn -> path-scoped evidence graph).
 *
 * Conversation flow: first message lazily creates a conversation, then every
 * message POSTs to /messages (optionally with a parent_turn_id when forking)
 * and opens a fresh SSE stream at the returned cursor. The MERGED graph
 * accumulates across all branches; selecting a non-head card swaps the right
 * pane to that turn's path-scoped graph (sibling evidence never appears).
 */
export function EntityResolverApp() {
  const [state, dispatch] = useReducer(reduce, undefined, initialState);
  const handleRef = useRef<TurnHandle | null>(null);
  const [expandToast, setExpandToast] = useState<string | null>(null);
  const [hiddenLabels, setHiddenLabels] = useState<Set<NodeLabel>>(new Set());
  const [centerView, setCenterView] = useState<CenterView>("graph");
  const [scoped, setScoped] = useState<ScopedGraph | null>(null);
  const [managerOpen, setManagerOpen] = useState(false);
  // Entity clicked in answer/summary markdown — drives the right-hand detail
  // slide-over (item: clickable entities). Null = closed.
  const [detailEntity, setDetailEntity] = useState<EntityMatch | null>(null);
  // Bumped when the workspace is replaced wholesale (switch / new / delete) so
  // the tree canvas remounts and lays out fresh, exactly like a page reload.
  // Live turns and the mount-time resume hydrate keep the same canvas instance.
  const [canvasEpoch, setCanvasEpoch] = useState(0);
  const scopeTickRef = useRef(0);

  // Canvas/graph split ratio, persisted to localStorage across reloads.
  // Applied after mount (hydration-safe), saved on every drag. The Group
  // fires onLayoutChanged with the DEFAULT split during mount, before the
  // restore effect below runs — persisting that first call would clobber the
  // saved ratio with 40/60 on every reload, so writes are gated until the
  // restore has happened.
  const investigationPanelRef = usePanelRef();
  const layoutRestoredRef = useRef(false);
  useEffect(() => {
    try {
      const raw = localStorage.getItem(PANEL_LAYOUT_KEY);
      if (raw) {
        const pct = (JSON.parse(raw) as Record<string, number>).investigation;
        if (typeof pct === "number" && pct >= 24 && pct <= 70) {
          investigationPanelRef.current?.resize(`${pct}%`);
        }
      }
    } catch {
      // corrupt entry: fall back to the default split
    }
    layoutRestoredRef.current = true;
  }, [investigationPanelRef]);
  const persistLayout = useCallback((layout: Record<string, number>) => {
    if (!layoutRestoredRef.current) return;
    try {
      localStorage.setItem(PANEL_LAYOUT_KEY, JSON.stringify(layout));
    } catch {
      // storage full/unavailable: split simply won't persist
    }
  }, []);

  // Resume the last conversation on reload: hydrate restores turns + the
  // merged graph, and the tree (stage 2a) restores branch structure. Old
  // pre-branching conversations come back as the flat vertical chain.
  useEffect(() => {
    const cid =
      typeof window !== "undefined" ? localStorage.getItem(CONVERSATION_KEY) : null;
    if (!cid) return;
    let cancelled = false;
    fetchConversation(cid)
      .then((payload) => {
        if (cancelled) return;
        if ((payload.turns ?? []).length === 0) return; // nothing to restore
        dispatch({ type: "hydrate", payload });
      })
      .catch(() => {
        // Expired (24h TTL) or unreachable — forget it.
        localStorage.removeItem(CONVERSATION_KEY);
      });
    return () => {
      cancelled = true;
    };
  }, []);

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

  // The single send path for every turn. `parentTurnId` forks/extends from
  // that turn (stage 2a tree); null keeps today's linear append.
  const send = useCallback(
    async (
      parentTurnId: string | null,
      text: string,
      opts?: { forceRiskReport?: boolean }
    ) => {
      handleRef.current?.close();
      handleRef.current = null;

      // Optimistic render: the user's turn card appears the instant they hit
      // send — BEFORE createConversation / POST /messages (several serial
      // Upstash round-trips server-side). The provisional turn carries a
      // stable clientId; the canvas keys its position on that id, so when
      // the server identity arrives via turn_resolved nothing jumps.
      const pinned = Array.from(state.pinnedNodeIds);
      const forceRiskReport = opts?.forceRiskReport ?? false;
      const clientId = `pending:${
        typeof crypto !== "undefined" && crypto.randomUUID
          ? crypto.randomUUID()
          : Math.random().toString(36).slice(2)
      }`;
      dispatch({
        type: "turn_pending",
        clientId,
        parentTurnId,
        userMessage: text,
        pinnedNodeIds: pinned,
        forceRiskReport,
      });

      try {
        let cid = state.conversationId;
        if (!cid) {
          cid = await createConversation();
          localStorage.setItem(CONVERSATION_KEY, cid);
          dispatch({ type: "conversation_created", conversationId: cid });
        }

        const { turnIndex, eventCursor, turnId, parentTurnId: resolvedParent } =
          await sendMessageApi(cid, text, {
            pinnedNodeIds: pinned,
            forceRiskReport,
            parentTurnId,
          });

        dispatch({
          type: "turn_resolved",
          clientId,
          turnIndex,
          turnId,
          parentTurnId: resolvedParent,
        });

        const convId = cid;
        handleRef.current = streamTurn(cid, eventCursor, {
          onEvent: (evt) => dispatch({ type: "event", event: evt }),
          onClose: (reason) => {
            dispatch({ type: "closed", reason });
            // Refresh the entity registry once the turn lands so the
            // composer autocomplete and entity detail panel see what this
            // turn surfaced (the registry otherwise only loads on hydrate).
            if (reason === "done") {
              fetchConversation(convId)
                .then((p) =>
                  dispatch({ type: "registry", entities: p.state_doc?.entities ?? {} })
                )
                .catch(() => {
                  /* best-effort refresh; the next hydrate catches up */
                });
            }
          },
        });
      } catch (err) {
        // The fatal path marks the provisional (last) turn failed, so the
        // optimistic card lands in the error state instead of stranding.
        dispatch({
          type: "fatal",
          message: err instanceof Error ? err.message : "unknown error sending message",
        });
      }
    },
    [state.conversationId, state.pinnedNodeIds]
  );

  const onSendFrom = useCallback(
    (parentTurnId: string | null, text: string, opts?: { forceRiskReport?: boolean }) =>
      send(parentTurnId, text, opts),
    [send]
  );

  const selectTurn = useCallback((turnId: string | null) => {
    dispatch({ type: "select_turn", turnId });
  }, []);

  const resetAll = useCallback(() => {
    handleRef.current?.close();
    handleRef.current = null;
    setCenterView("graph");
    setScoped(null);
    setDetailEntity(null);
    localStorage.removeItem(CONVERSATION_KEY);
    setCanvasEpoch((n) => n + 1);
    dispatch({ type: "reset" });
  }, []);

  /* ── conversation history menu: switch / new / delete ── */

  const isRunning = state.status === "running";

  // Switching reuses the EXACT page-reload restore path (fetchConversation ->
  // hydrate), so turns, the tree canvas, the merged graph and the map data all
  // come back identically. Blocked while a turn is streaming — the manager
  // disables its rows, and this guard backs that up.
  const switchConversation = useCallback(
    (cid: string) => {
      if (isRunning) return;
      setManagerOpen(false);
      if (cid === state.conversationId) return;
      handleRef.current?.close();
      handleRef.current = null;
      fetchConversation(cid)
        .then((payload) => {
          localStorage.setItem(CONVERSATION_KEY, cid);
          setCenterView("graph");
          setScoped(null);
          setDetailEntity(null);
          setCanvasEpoch((n) => n + 1);
          dispatch({ type: "hydrate", payload });
        })
        .catch((err) => {
          // Expired (24h TTL) or unreachable — leave the current state alone.
          console.error("switch conversation failed", err);
        });
    },
    [isRunning, state.conversationId]
  );

  const newInvestigation = useCallback(() => {
    // Reset the workspace and clear the active pointer. The old conversation
    // stays on the server (and in the history menu) until its TTL expires.
    setManagerOpen(false);
    resetAll();
  }, [resetAll]);

  const onConversationDeleted = useCallback(
    (cid: string) => {
      // Deleting the conversation we're standing in resets the workspace.
      if (cid === state.conversationId) resetAll();
    },
    [state.conversationId, resetAll]
  );

  /* ── time-travel: selected card -> path-scoped evidence graph (spec §6) ── */

  const liveHead = liveHeadTurn(state);
  const liveHeadId = liveHead?.turnId ?? null;
  // Time travel only when an explicit selection points away from the live
  // head. Selecting the head (or nothing) = live mode: merged graph + streaming.
  const timeTravelTurnId =
    state.activeTurnId && state.activeTurnId !== liveHeadId
      ? state.activeTurnId
      : null;

  useEffect(() => {
    if (!timeTravelTurnId || !state.conversationId) {
      setScoped(null);
      return;
    }
    const cid = state.conversationId;
    const turn = turnById(state, timeTravelTurnId);
    const pathLength = pathToRoot(state, timeTravelTurnId).length;
    let cancelled = false;
    fetchTurnGraph(cid, timeTravelTurnId)
      .then((resp) => {
        if (cancelled) return;
        setScoped({
          turnId: resp.turn_id,
          turnIndex: turn?.index ?? 0,
          pathLength: resp.path?.length ?? pathLength,
          nodes: resp.graph?.nodes ?? [],
          edges: resp.graph?.edges ?? [],
          deltaNodeIds: new Set((resp.turn_delta?.nodes ?? []).map((n) => n.id)),
          deltaEdgeKeys: new Set((resp.turn_delta?.edges ?? []).map(edgeKey)),
          tick: ++scopeTickRef.current,
        });
      })
      .catch((err) => {
        console.error("time-travel graph fetch failed", err);
        if (!cancelled) setScoped(null);
      });
    return () => {
      cancelled = true;
    };
    // `state` is intentionally not a dep — the scope refetches only when the
    // selected turn changes, not on every streaming tick.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [timeTravelTurnId, state.conversationId]);

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

  /* ── clickable entities: known-entity lookup + right-hand detail panel ── */

  const entityLookupMap = useMemo(
    () => buildEntityLookup(state.nodes, state.registry),
    [state.nodes, state.registry]
  );
  const lookupEntity = useCallback(
    (name: string) => matchEntity(entityLookupMap, name),
    [entityLookupMap]
  );
  const openEntityDetail = useCallback(
    (match: EntityMatch) => {
      // Highlight + center the node when the entity is on the graph;
      // registry-only entities just open the panel.
      if (match.nodeId) dispatch({ type: "focus_node", nodeId: match.nodeId });
      setDetailEntity(match);
    },
    []
  );
  const openEntityByName = useCallback(
    (name: string) => {
      const match = lookupEntity(name);
      if (match) openEntityDetail(match);
    },
    [lookupEntity, openEntityDetail]
  );
  // Right-click "Open detail view": open the same right-hand panel the
  // click-to-open path uses, built from the graph node directly.
  const openNodeDetail = useCallback(
    (nodeId: string) => {
      const node = state.nodes.get(nodeId);
      openEntityDetail({ name: node?.name ?? nodeId, nodeId });
    },
    [state.nodes, openEntityDetail]
  );
  const entityInteraction = useMemo(
    () => ({ lookup: lookupEntity, onEntityClick: openEntityDetail }),
    [lookupEntity, openEntityDetail]
  );

  // Index graph nodes by normalized name so an entity opened from prose (which
  // may carry only a registry id) still resolves to its on-graph node and shows
  // the same relationship list as a graph-pinned node (issue 5).
  const nodeIdByName = useMemo(() => {
    const map = new Map<string, string>();
    for (const node of state.nodes.values()) {
      const key = normalizeEntityName(node.name);
      if (key && !map.has(key)) map.set(key, node.id);
    }
    return map;
  }, [state.nodes]);

  // Resolve the open entity against live state on every render so the panel
  // tracks graph growth (e.g. relationships streaming in mid-turn).
  const detailData = useMemo(() => {
    if (!detailEntity) return null;
    // Prefer the match's own node id; fall back to matching the entity's name
    // against the graph so a prose-opened entity that IS on the canvas (under a
    // different id linkage) still mirrors its connections.
    const resolvedNodeId =
      (detailEntity.nodeId && state.nodes.has(detailEntity.nodeId)
        ? detailEntity.nodeId
        : nodeIdByName.get(normalizeEntityName(detailEntity.name))) ?? null;
    const node = resolvedNodeId ? state.nodes.get(resolvedNodeId) ?? null : null;
    const registryEntry =
      (detailEntity.registryId ? state.registry.get(detailEntity.registryId) : undefined) ??
      (detailEntity.nodeId ? state.registry.get(detailEntity.nodeId) : undefined) ??
      (resolvedNodeId ? state.registry.get(resolvedNodeId) : undefined) ??
      null;
    const relationships: EntityRelationship[] = [];
    if (resolvedNodeId) {
      for (const edge of state.edges.values()) {
        if (relationships.length >= 14) break;
        const out = edge.source === resolvedNodeId;
        const inn = edge.target === resolvedNodeId;
        if (!out && !inn) continue;
        const otherId = out ? edge.target : edge.source;
        const other = state.nodes.get(otherId);
        if (!other) continue;
        relationships.push({
          type: edge.type,
          direction: out ? "out" : "in",
          otherId,
          otherName: other.name,
        });
      }
    }
    return { match: detailEntity, node, registryEntry, relationships };
  }, [detailEntity, state.nodes, state.edges, state.registry, nodeIdByName]);

  // Live mode renders the merged conversation graph; time travel swaps in the
  // selected turn's path-accumulated graph (sibling branches excluded).
  const liveNodes = useMemo(() => Array.from(state.nodes.values()), [state.nodes]);
  const liveEdges = useMemo(() => Array.from(state.edges.values()), [state.edges]);
  const graphNodes = scoped ? scoped.nodes : liveNodes;
  const graphEdges = scoped ? scoped.edges : liveEdges;
  const scopedDelta = scoped
    ? { nodeIds: scoped.deltaNodeIds, edgeKeys: scoped.deltaEdgeKeys, tick: scoped.tick }
    : null;

  return (
    <EntityInteractionContext.Provider value={entityInteraction}>
      <div className="flex h-screen flex-col bg-background text-foreground">
        <AppHeader
          state={state}
          onReset={newInvestigation}
          onToggleHistory={() => setManagerOpen((o) => !o)}
          historyOpen={managerOpen}
        />

        <ConversationManager
          open={managerOpen}
          onClose={() => setManagerOpen(false)}
          activeConversationId={state.conversationId}
          isRunning={isRunning}
          onSelect={switchConversation}
          onNewInvestigation={newInvestigation}
          onDeleted={onConversationDeleted}
        />

        {/* User-adjustable canvas/graph split; the ratio persists to
            localStorage (applied post-mount, saved on drag). React Flow
            observes its container, so both panes relayout live while the
            handle drags. */}
        <Group
          orientation="horizontal"
          onLayoutChanged={persistLayout}
          className="min-h-0 flex-1"
        >
          {/* Left: INVESTIGATION — the branching turn canvas */}
          <Panel
            id="investigation"
            panelRef={investigationPanelRef}
            defaultSize="40%"
            minSize="24%"
            className="flex min-h-0"
            style={{ overflow: "hidden" }}
          >
            <aside className="flex min-h-0 min-w-0 flex-1 flex-col">
              <InvestigationCanvas
                key={canvasEpoch}
                state={state}
                disabled={isRunning}
                onSendFrom={onSendFrom}
                onSelectTurn={selectTurn}
                onHighlightNodes={highlightNodes}
                onClearHighlight={clearHighlight}
                onFocusNode={focusNode}
                onTogglePin={togglePin}
              />
            </aside>
          </Panel>

          {/* 1px visual hairline; the library widens the actual hit target
              (resizeTargetMinimumSize) so the thin line stays grabbable. */}
          <Separator
            title="Drag to resize panels"
            className="w-px bg-border outline-none transition-colors data-[separator=active]:bg-foreground/50 data-[separator=focus]:bg-foreground/30 data-[separator=hover]:bg-foreground/30"
          />

          {/* Right: EVIDENCE GRAPH — graph with a map lens for trade routes */}
          <Panel
            id="evidence"
            minSize="30%"
            className="flex min-h-0"
            style={{ overflow: "hidden" }}
          >
            <main className="relative flex min-h-0 min-w-0 flex-1 flex-col">
              <EvidencePaneHeader
                nodeCount={graphNodes.length}
                edgeCount={graphEdges.length}
                hiddenLabels={hiddenLabels}
                onToggleLabel={toggleLabel}
                view={centerView}
                onViewChange={setCenterView}
                routeCount={tradeRoutes.length}
                scopeLabel={
                  scoped
                    ? `as of turn ${String(scoped.turnIndex + 1).padStart(2, "0")} on this path`
                    : null
                }
                onBackToLive={scoped ? () => selectTurn(null) : undefined}
              />
              <div className="relative min-h-0 flex-1">
                {/* GraphPanel stays mounted under the map so the force layout and
                    user-dragged positions survive flipping lenses. */}
                <GraphPanel
                  nodes={graphNodes}
                  edges={graphEdges}
                  highlightedNodeIds={state.highlightedNodeIds}
                  pinnedNodeIds={state.pinnedNodeIds}
                  focusRequest={state.focusRequest}
                  hiddenLabels={hiddenLabels}
                  onExpandNode={handleExpand}
                  onTogglePin={togglePin}
                  onOpenDetail={openNodeDetail}
                  leadsShown={scoped ? undefined : state.latestSearchMeta?.shown}
                  leadsTotal={scoped ? undefined : state.latestSearchMeta?.total}
                  overlayLeadNodes={scoped ? [] : state.unpinnedLeadNodes}
                  showOverlayLeads={scoped ? false : state.showUnpinnedLeads}
                  onToggleLeads={toggleLeadsOverlay}
                  scopedDelta={scopedDelta}
                />
                {centerView === "map" && (
                  <div className="absolute inset-0 z-10">
                    <TradeRoutesMap routes={tradeRoutes} subjects={tradeSubjects} />
                    {/* Path-scoping the map isn't cheap (routes derive from
                        per-turn tool metadata) — the map stays on live-head data
                        and says so while time-traveling. */}
                    {scoped && (
                      <div className="pointer-events-none absolute left-3 top-3 z-20 rounded-md border border-border bg-card px-2.5 py-1 font-mono text-[9px] uppercase tracking-[0.14em] text-muted-foreground shadow-sm">
                        map shows live data (not path-scoped)
                      </div>
                    )}
                  </div>
                )}
                <ExpandToast message={expandToast} onDismiss={() => setExpandToast(null)} />
                {detailData && (
                  <EntityDetailPanel
                    name={detailData.match.name}
                    node={detailData.node}
                    registryEntry={detailData.registryEntry}
                    relationships={detailData.relationships}
                    onClose={() => setDetailEntity(null)}
                    onFocusNode={focusNode}
                    onOpenEntity={openEntityByName}
                  />
                )}
              </div>
            </main>
          </Panel>
        </Group>
      </div>
    </EntityInteractionContext.Provider>
  );
}

/** Thin top header: history toggle, product name, data-route chip, reset. */
function AppHeader({
  state,
  onReset,
  onToggleHistory,
  historyOpen,
}: {
  state: ConversationState;
  onReset: () => void;
  onToggleHistory: () => void;
  historyOpen: boolean;
}) {
  const hasData = state.turns.length > 0 || state.nodes.size > 0;
  return (
    <header className="flex shrink-0 items-center justify-between border-b border-border bg-background px-4 py-2">
      <div className="flex items-center gap-3">
        <button
          onClick={onToggleHistory}
          title={historyOpen ? "Close investigation history" : "Open investigation history"}
          className="flex cursor-pointer items-center justify-center rounded-md p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        >
          <PanelLeft className="h-4 w-4" />
        </button>
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

/** EVIDENCE GRAPH pane header: label, scope state, node-type filters, lens. */
function EvidencePaneHeader({
  nodeCount,
  edgeCount,
  hiddenLabels,
  onToggleLabel,
  view,
  onViewChange,
  routeCount,
  scopeLabel,
  onBackToLive,
}: {
  nodeCount: number;
  edgeCount: number;
  hiddenLabels: Set<NodeLabel>;
  onToggleLabel: (label: NodeLabel) => void;
  view: CenterView;
  onViewChange: (view: CenterView) => void;
  routeCount: number;
  /** Non-null while time-traveling, e.g. "as of turn 02 on this path". */
  scopeLabel?: string | null;
  onBackToLive?: () => void;
}) {
  return (
    <div className="flex items-center justify-between border-b border-border bg-background px-4 py-2">
      <div className="flex min-w-0 items-center gap-3">
        <h2 className="shrink-0 font-mono text-[9px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
          Evidence graph
        </h2>
        {scopeLabel ? (
          <span className="flex shrink-0 items-center gap-2">
            <span className="rounded-md border border-foreground/30 bg-muted px-1.5 py-0.5 font-mono text-[9px] font-medium uppercase tracking-[0.14em] text-foreground">
              {scopeLabel}
            </span>
            {onBackToLive && (
              <button
                type="button"
                onClick={onBackToLive}
                title="Return to the live merged graph"
                className="flex cursor-pointer items-center gap-1 rounded-md border border-border bg-card px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-[0.14em] text-muted-foreground shadow-sm transition-colors hover:bg-muted hover:text-foreground"
              >
                <Undo2 className="h-2.5 w-2.5" /> back to live
              </button>
            )}
          </span>
        ) : (
          <span className="shrink-0 font-mono text-[9px] tabular-nums uppercase tracking-[0.14em] text-muted-foreground/70">
            {nodeCount} nodes · {edgeCount} edges
          </span>
        )}
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
