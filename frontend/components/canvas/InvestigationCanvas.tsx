"use client";

/*
 * The INVESTIGATION pane as a real branching canvas (stage 2b).
 *
 * Replaces the stage-1 stacked list with a React Flow canvas hosting
 * TurnCards as nodes. Donor: local-lmcanvas Canvas.tsx (MIT License,
 * Copyright (c) 2026 Max Lee) — pan/zoom configuration, 32px line grid,
 * local-rfNodes drag pattern (intermediate drag positions never hit the
 * store), drag-aware edge handle recomputation, and the draft-child fork
 * flow from useBranchFromNode. Adapted to a server-backed turn tree: node
 * identity is the server turn_id, structure comes from parent_turn_id, and
 * placement is incremental (live appends measure the DOM; reloads rebuild
 * the whole tree from /tree with fallback heights).
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Background,
  BackgroundVariant,
  ReactFlow,
  ReactFlowProvider,
  applyNodeChanges,
  useReactFlow,
  type Edge,
  type NodeChange,
  type NodeMouseHandler,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { motion } from "framer-motion";
import { ArrowUp, Search, X } from "lucide-react";
import {
  PromptInput,
  PromptInputActions,
  PromptInputTextarea,
} from "@/components/ui/prompt-input";
import { Button } from "@/components/ui/button";
import type { ConversationState, Turn } from "@/lib/conversation-store";
import { liveHeadTurn, pathToRoot } from "@/lib/conversation-store";
import {
  NODE_WIDTH,
  canvasIdOf,
  getEdgeHandles,
  layoutNewTurns,
  makeDomHeightMeasurer,
  placeChild,
  type Pos,
} from "@/lib/canvas-layout";
import {
  DraftNode,
  TurnNode,
  type DraftFlowNode,
  type TurnFlowNode,
} from "./TurnNode";

type CanvasFlowNode = TurnFlowNode | DraftFlowNode;

const nodeTypes = { turn: TurnNode, draft: DraftNode };

const EXAMPLES = ["Gazprom", "Sberbank", "Sergey Roldugin", "Huawei Technologies"];

const DRAFT_ID = "__draft__";

/** Edge chrome (donor: muted-foreground, 2.25 stroke, rounded caps). */
const edgeStyle = {
  stroke: "var(--muted-foreground)",
  strokeWidth: 2.25,
  opacity: 0.55,
} as const;
const activePathEdgeStyle = {
  stroke: "var(--foreground)",
  strokeWidth: 2.5,
  opacity: 0.75,
} as const;

export interface InvestigationCanvasProps {
  state: ConversationState;
  disabled: boolean;
  /** Send a message parented on `parentTurnId` (null = linear append). */
  onSendFrom: (
    parentTurnId: string | null,
    text: string,
    opts?: { forceRiskReport?: boolean }
  ) => void;
  onSelectTurn: (turnId: string | null) => void;
  onHighlightNodes?: (nodeIds: string[]) => void;
  onClearHighlight?: () => void;
  onFocusNode?: (nodeId: string) => void;
  onTogglePin?: (nodeId: string) => void;
}

export function InvestigationCanvas(props: InvestigationCanvasProps) {
  return (
    <ReactFlowProvider>
      <InvestigationCanvasInner {...props} />
    </ReactFlowProvider>
  );
}

type DraftState = {
  parentTurn: Turn;
  position: Pos;
};

function InvestigationCanvasInner({
  state,
  disabled,
  onSendFrom,
  onSelectTurn,
  onHighlightNodes,
  onClearHighlight,
  onFocusNode,
  onTogglePin,
}: InvestigationCanvasProps) {
  const reactFlow = useReactFlow();
  const [composerValue, setComposerValue] = useState("");
  const [positions, setPositions] = useState<Map<string, Pos>>(new Map());
  const [draft, setDraft] = useState<DraftState | null>(null);
  const [rfNodes, setRfNodes] = useState<CanvasFlowNode[]>([]);
  const draggingIdsRef = useRef<Set<string>>(new Set());
  /** Position a just-submitted draft card occupied, keyed by parent turn id,
   *  so the real turn node lands exactly where the draft was. */
  const pendingDraftPosRef = useRef<{ parentTurnId: string; pos: Pos } | null>(null);

  const liveHead = liveHeadTurn(state);
  const liveHeadId = liveHead?.turnId ?? null;
  // Effective selection: explicit pick, else follow the live head.
  const effectiveSelectedId = state.activeTurnId ?? liveHeadId;
  const isTimeTraveling =
    state.activeTurnId !== null && state.activeTurnId !== liveHeadId;

  // The ACTIVE PATH (root -> selected) — used to emphasize its edges.
  const activePathCanvasIds = useMemo(() => {
    if (!effectiveSelectedId) return new Set<string>();
    return new Set(pathToRoot(state, effectiveSelectedId).map(canvasIdOf));
  }, [state, effectiveSelectedId]);

  /* ── placement: position any turn that doesn't have coordinates yet ───── */

  useEffect(() => {
    const unplaced = state.turns.filter((t) => !positions.has(canvasIdOf(t)));
    if (unplaced.length === 0) return;

    // A submitted draft pins its turn to the draft card's spot.
    const preferred = new Map<string, Pos>();
    const pending = pendingDraftPosRef.current;
    if (pending) {
      const match = unplaced.find((t) => t.parentTurnId === pending.parentTurnId);
      if (match) {
        preferred.set(canvasIdOf(match), pending.pos);
        pendingDraftPosRef.current = null;
      }
    }

    const measure = makeDomHeightMeasurer(reactFlow.getZoom());
    const additions = layoutNewTurns(state.turns, positions, measure, preferred);
    if (additions.size === 0) return;

    setPositions((prev) => {
      const next = new Map(prev);
      for (const [id, pos] of additions) next.set(id, pos);
      return next;
    });

    // Camera: a full rebuild (reload) frames the whole tree; a single live
    // append centers on the new card (donor's centerOnNode behavior).
    const newTurnIds = unplaced.map(canvasIdOf).filter((id) => additions.has(id));
    if (newTurnIds.length > 1) {
      // A multi-node rebuild placed cards with fallback heights (they weren't
      // mounted yet), so tall cards can overlap their children. Once they
      // render, re-run the layout from scratch with real DOM heights. Safe to
      // replace wholesale: nothing has been dragged yet on a fresh rebuild.
      const turnsAtRebuild = state.turns;
      window.setTimeout(() => {
        try {
          const remeasure = makeDomHeightMeasurer(reactFlow.getZoom());
          const relaid = layoutNewTurns(turnsAtRebuild, new Map(), remeasure);
          setPositions(relaid);
          window.setTimeout(() => {
            try {
              reactFlow.fitView({ padding: 0.2, duration: 400, maxZoom: 1 });
            } catch {
              /* pane unmounted; ignore */
            }
          }, 30);
        } catch {
          /* pane unmounted; ignore */
        }
      }, 120);
    } else if (newTurnIds.length === 1) {
      const pos = additions.get(newTurnIds[0])!;
      window.setTimeout(() => {
        try {
          reactFlow.setCenter(pos.x + NODE_WIDTH / 2, pos.y + 200, {
            zoom: reactFlow.getZoom(),
            duration: 350,
          });
        } catch {
          /* ignore */
        }
      }, 60);
    }
  }, [state.turns, positions, reactFlow]);

  /* ── rfNodes sync (donor pattern): store -> local xyflow state ─────────── */

  const handleFork = useCallback(
    (turn: Turn) => {
      if (!turn.turnId) return;
      const parentCanvasId = canvasIdOf(turn);
      const parentPos = positions.get(parentCanvasId);
      if (!parentPos) return;
      const measure = makeDomHeightMeasurer(reactFlow.getZoom());
      const hasChildren = state.turns.some(
        (t) => t.parentTurnId === turn.turnId
      );
      // First child continues the thread below; siblings fork right.
      const pos = placeChild(parentPos, measure(parentCanvasId), hasChildren);
      setDraft({ parentTurn: turn, position: pos });
      window.setTimeout(() => {
        try {
          reactFlow.setCenter(pos.x + NODE_WIDTH / 2, pos.y + 120, {
            zoom: reactFlow.getZoom(),
            duration: 300,
          });
        } catch {
          /* ignore */
        }
      }, 40);
    },
    [positions, state.turns, reactFlow]
  );

  const handleDraftSubmit = useCallback(
    (text: string) => {
      if (!draft?.parentTurn.turnId) return;
      pendingDraftPosRef.current = {
        parentTurnId: draft.parentTurn.turnId,
        pos: draft.position,
      };
      onSendFrom(draft.parentTurn.turnId, text);
      setDraft(null);
    },
    [draft, onSendFrom]
  );

  const handleDraftCancel = useCallback(() => setDraft(null), []);

  const handleSendFromTurn = useCallback(
    (turn: Turn, text: string) => {
      // A chip click forks/extends from THAT card's turn (spec §4/§5);
      // legacy turns without ids fall back to linear append.
      onSendFrom(turn.turnId, text);
    },
    [onSendFrom]
  );

  const handleGenerateReportFromTurn = useCallback(
    (turn: Turn, prompt: string) => {
      onSendFrom(turn.turnId, prompt, { forceRiskReport: true });
    },
    [onSendFrom]
  );

  useEffect(() => {
    setRfNodes((prev) => {
      const prevById = new Map(prev.map((n) => [n.id, n]));
      const dragging = draggingIdsRef.current;
      const next: CanvasFlowNode[] = [];
      for (const turn of state.turns) {
        const id = canvasIdOf(turn);
        const pos = positions.get(id);
        if (!pos) continue; // not placed yet (placement effect will run)
        const existing = prevById.get(id);
        const position =
          dragging.has(id) && existing ? existing.position : pos;
        next.push({
          id,
          type: "turn",
          position,
          dragging: existing?.dragging,
          data: {
            turn,
            nodesById: state.nodes,
            isActive: effectiveSelectedId !== null && turn.turnId === effectiveSelectedId,
            isTimeTravelTarget: isTimeTraveling && turn.turnId === state.activeTurnId,
            canFork: Boolean(turn.turnId) && !disabled && !draft,
            onFork: handleFork,
            onSendFrom: handleSendFromTurn,
            onGenerateReportFrom: handleGenerateReportFromTurn,
            onHighlightNodes,
            onClearHighlight,
            onFocusNode,
          },
        });
      }
      if (draft) {
        next.push({
          id: DRAFT_ID,
          type: "draft",
          position: draft.position,
          data: {
            parentTurnIndex: draft.parentTurn.index,
            onSubmit: handleDraftSubmit,
            onCancel: handleDraftCancel,
          },
        });
      }
      return next;
    });
  }, [
    state.turns,
    state.nodes,
    state.activeTurnId,
    positions,
    draft,
    disabled,
    effectiveSelectedId,
    isTimeTraveling,
    handleFork,
    handleSendFromTurn,
    handleGenerateReportFromTurn,
    handleDraftSubmit,
    handleDraftCancel,
    onHighlightNodes,
    onClearHighlight,
    onFocusNode,
  ]);

  /* ── edges: parent -> child bezier connectors, drag-aware handles ──────── */

  const rfEdges = useMemo<Edge[]>(() => {
    const posById = new Map(rfNodes.map((n) => [n.id, n.position]));
    const turnByTurnId = new Map<string, Turn>();
    for (const t of state.turns) if (t.turnId) turnByTurnId.set(t.turnId, t);

    const edges: Edge[] = [];
    const orderedByIndex = [...state.turns].sort((a, b) => a.index - b.index);
    for (const turn of orderedByIndex) {
      const childId = canvasIdOf(turn);
      // Tree parent; legacy turns chain to the previous index (today's look).
      let parentCanvasId: string | null = null;
      if (turn.parentTurnId) {
        const parent = turnByTurnId.get(turn.parentTurnId);
        parentCanvasId = parent ? canvasIdOf(parent) : null;
      } else if (turn.index > 0) {
        const prev = orderedByIndex.find((t) => t.index === turn.index - 1);
        parentCanvasId = prev ? canvasIdOf(prev) : null;
      }
      if (!parentCanvasId) continue;
      const sourcePos = posById.get(parentCanvasId);
      const targetPos = posById.get(childId);
      if (!sourcePos || !targetPos) continue;
      const handles = getEdgeHandles(sourcePos, targetPos);
      const onActivePath =
        activePathCanvasIds.has(childId) && activePathCanvasIds.has(parentCanvasId);
      edges.push({
        id: `e-${parentCanvasId}-${childId}`,
        source: parentCanvasId,
        target: childId,
        sourceHandle: handles.sourceHandle,
        targetHandle: handles.targetHandle,
        style: onActivePath ? activePathEdgeStyle : edgeStyle,
      });
    }
    if (draft) {
      const parentCanvasId = canvasIdOf(draft.parentTurn);
      const sourcePos = posById.get(parentCanvasId);
      const targetPos = posById.get(DRAFT_ID);
      if (sourcePos && targetPos) {
        const handles = getEdgeHandles(sourcePos, targetPos);
        edges.push({
          id: `e-${parentCanvasId}-${DRAFT_ID}`,
          source: parentCanvasId,
          target: DRAFT_ID,
          sourceHandle: handles.sourceHandle,
          targetHandle: handles.targetHandle,
          style: { ...edgeStyle, strokeDasharray: "6 4" },
        });
      }
    }
    return edges;
  }, [rfNodes, state.turns, draft, activePathCanvasIds]);

  /* ── interaction handlers ──────────────────────────────────────────────── */

  const onNodesChange = useCallback(
    (changes: NodeChange<CanvasFlowNode>[]) => {
      setRfNodes((nodes) => applyNodeChanges(changes, nodes));
      for (const c of changes) {
        if (c.type === "position" && c.position) {
          if (c.dragging) {
            draggingIdsRef.current.add(c.id);
          } else if (c.dragging === false) {
            draggingIdsRef.current.delete(c.id);
            const pos = c.position;
            setPositions((prev) => {
              const next = new Map(prev);
              next.set(c.id, { x: pos.x, y: pos.y });
              return next;
            });
            if (c.id === DRAFT_ID) {
              setDraft((d) => (d ? { ...d, position: pos } : d));
            }
          }
        }
      }
    },
    []
  );

  const onNodeClick: NodeMouseHandler<CanvasFlowNode> = useCallback(
    (_e, node) => {
      if (node.type !== "turn") return;
      const turn = (node.data as { turn: Turn }).turn;
      onSelectTurn(turn.turnId ?? null);
    },
    [onSelectTurn]
  );

  const onPaneClick = useCallback(() => {
    // Clicking empty canvas returns to the live head (clears time travel).
    onSelectTurn(null);
  }, [onSelectTurn]);

  const submitComposer = () => {
    const t = composerValue.trim();
    if (!t || disabled) return;
    // Parent on the selected card so continuing a branch extends it; with no
    // selection this is null = linear append, exactly the old behavior.
    onSendFrom(state.activeTurnId, t);
    setComposerValue("");
  };

  const isEmpty = state.turns.length === 0;
  const pinned = Array.from(state.pinnedNodeIds);
  const selectedTurn = state.activeTurnId
    ? state.turns.find((t) => t.turnId === state.activeTurnId) ?? null
    : null;

  return (
    <div className="flex h-full min-h-0 flex-col bg-background">
      {/* Pane label strip */}
      <header className="flex items-center justify-between border-b border-border bg-background px-4 py-2">
        <h2 className="font-mono text-[9px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
          Investigation
        </h2>
        <StatusBadge state={state} />
      </header>

      {/* Body: the branching canvas */}
      {isEmpty ? (
        <EmptyState onPickExample={(q) => onSendFrom(null, q)} />
      ) : (
        <div className="relative min-h-0 flex-1">
          <ReactFlow<CanvasFlowNode>
            nodes={rfNodes}
            edges={rfEdges}
            nodeTypes={nodeTypes}
            onNodesChange={onNodesChange}
            onNodeClick={onNodeClick}
            onPaneClick={onPaneClick}
            nodesDraggable
            nodesConnectable={false}
            elementsSelectable={false}
            noDragClassName="nodrag"
            minZoom={0.1}
            maxZoom={2}
            defaultViewport={{ x: 40, y: 40, zoom: 0.85 }}
            panOnScroll
            zoomOnScroll={false}
            zoomOnPinch
            zoomOnDoubleClick={false}
            deleteKeyCode={null}
            disableKeyboardA11y
            proOptions={{ hideAttribution: true }}
          >
            <Background
              variant={BackgroundVariant.Lines}
              gap={32}
              size={1}
              color="var(--grid-line)"
            />
          </ReactFlow>

          {/* Radial vignette fading the grid toward the pane edges */}
          <div className="canvas-vignette pointer-events-none absolute inset-0 z-[4]" />

          {/* Branch-state hint while time-traveling */}
          {isTimeTraveling && selectedTurn && (
            <div className="pointer-events-none absolute left-3 top-3 z-10 rounded-md border border-border bg-card px-2.5 py-1 font-mono text-[9px] uppercase tracking-[0.14em] text-muted-foreground shadow-sm">
              viewing turn {String(selectedTurn.index + 1).padStart(2, "0")} · next
              message extends this branch
            </div>
          )}

          {state.errorMessage && (
            <div className="absolute bottom-3 left-3 right-3 z-10 rounded-[10px] border border-red-300 bg-red-50 p-3 text-xs text-red-700 shadow-sm">
              <strong className="font-semibold">Error:</strong> {state.errorMessage}
            </div>
          )}
        </div>
      )}

      {/* Pinned-context bar */}
      {pinned.length > 0 && (
        <div className="border-t border-border bg-background px-4 py-2">
          <div className="mb-1 font-mono text-[8px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
            Pinned context · sent with next message
          </div>
          <div className="flex flex-wrap gap-1.5">
            {pinned.map((id) => {
              const node = state.nodes.get(id);
              return (
                <span
                  key={id}
                  className="inline-flex items-center gap-1 rounded-md border border-border bg-card px-1.5 py-0.5 font-mono text-[10px] text-foreground shadow-sm"
                >
                  {node ? truncate(node.name, 28) : id.slice(-8)}
                  <button
                    type="button"
                    onClick={() => onTogglePin?.(id)}
                    className="text-muted-foreground transition-colors hover:text-foreground"
                    title="Unpin"
                  >
                    <X className="size-3" />
                  </button>
                </span>
              );
            })}
          </div>
        </div>
      )}

      {/* Composer — submits onto the active path head */}
      <div className="border-t border-border bg-background p-3">
        <PromptInput
          value={composerValue}
          onValueChange={setComposerValue}
          onSubmit={submitComposer}
          isLoading={disabled}
          className="rounded-[10px] border-border bg-card shadow-sm"
        >
          <PromptInputTextarea
            placeholder={
              disabled
                ? "Agent is working…"
                : isEmpty
                  ? "Search a person or company (e.g. Sergey Roldugin)"
                  : isTimeTraveling && selectedTurn
                    ? `Continue from turn ${selectedTurn.index + 1} (extends this branch)`
                    : "Ask a follow-up, or investigate someone new"
            }
            className="text-[13px]"
          />
          <PromptInputActions className="justify-end pt-2">
            <Button
              size="sm"
              onClick={submitComposer}
              disabled={disabled || composerValue.trim().length === 0}
              className="h-8 w-8 rounded-full bg-foreground p-0 text-background hover:bg-foreground/90"
              title={disabled ? "Agent is working" : "Send"}
            >
              <ArrowUp className="h-4 w-4" />
            </Button>
          </PromptInputActions>
        </PromptInput>
      </div>
    </div>
  );
}

function truncate(s: string, n: number): string {
  return s.length <= n ? s : s.slice(0, n - 1).trimEnd() + "…";
}

function StatusBadge({ state }: { state: ConversationState }) {
  const text = {
    idle: "ready",
    running: "working",
    done: "ready",
    error: "error",
  }[state.status];

  const cls = {
    idle: "text-muted-foreground border-border bg-card",
    running: "text-foreground border-border bg-muted",
    error: "text-red-700 border-red-300 bg-red-50",
    done: "text-muted-foreground border-border bg-card",
  }[state.status];

  return (
    <span
      className={
        "rounded-md border px-1.5 py-0.5 font-mono text-[9px] font-medium uppercase tracking-[0.16em] " +
        cls
      }
    >
      {state.status === "running" ? (
        <span className="node-shimmer">{text}</span>
      ) : (
        text
      )}
    </span>
  );
}

function EmptyState({ onPickExample }: { onPickExample: (q: string) => void }) {
  return (
    <div className="canvas-grid relative flex flex-1 flex-col items-center justify-center px-6 text-center">
      <div className="canvas-vignette pointer-events-none absolute inset-0" />
      <motion.div
        initial={{ scale: 0.96, opacity: 0, y: -6 }}
        animate={{ scale: 1, opacity: 1, y: 0 }}
        transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
        className="relative z-10 max-w-md rounded-[10px] border border-border bg-card px-8 py-8 shadow-sm"
      >
        <div className="mx-auto mb-4 flex size-11 items-center justify-center rounded-full border border-border bg-muted">
          <Search className="size-4.5 text-muted-foreground" />
        </div>
        <h3 className="text-[15px] font-semibold text-foreground">
          Entity Risk Resolver
        </h3>
        <p className="mt-2 text-[12.5px] leading-relaxed text-muted-foreground">
          Type a person or company name. The agent searches the ICIJ Offshore
          Leaks graph, traverses connections, and cross-checks sanctions — then
          fork the investigation at any turn to explore parallel hypotheses.
        </p>
        <div className="mt-6 flex flex-col items-center gap-2">
          <span className="font-mono text-[9px] uppercase tracking-[0.18em] text-muted-foreground/70">
            try
          </span>
          <div className="flex flex-wrap items-center justify-center gap-1.5">
            {EXAMPLES.map((ex) => (
              <motion.button
                key={ex}
                type="button"
                whileTap={{ scale: 0.97 }}
                whileHover={{ y: -1 }}
                onClick={() => onPickExample(ex)}
                className="inline-flex cursor-pointer items-center gap-1.5 rounded-md border border-border bg-background px-2 py-1 font-mono text-[10px] uppercase tracking-[0.16em] text-foreground/80 transition-colors hover:border-foreground/40 hover:text-foreground focus:outline-none"
              >
                {ex}
              </motion.button>
            ))}
          </div>
        </div>
      </motion.div>
    </div>
  );
}
