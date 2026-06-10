"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  MarkerType,
  ReactFlow,
  ReactFlowProvider,
  applyNodeChanges,
  useReactFlow,
  type Edge,
  type Node,
  type NodeChange,
  type NodeMouseHandler,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type Simulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from "d3-force";

import { EntityHullOverlay, type HullGroup } from "./GraphPanel/EntityHullOverlay";

import type {
  ExpandKind,
  GraphNode as ERGraphNode,
  GraphEdge as ERGraphEdge,
  NodeLabel,
  SayariRiskLevel,
  SourceSystem,
} from "@/lib/types";
import {
  RISK_LEVEL_COLORS,
  SOURCE_SYSTEM_META,
  sourceSystemOf,
} from "@/lib/types";

/*
 * Evidence graph, lmcanvas design language (spec §5):
 *   - neutral light chrome on a 32px grid (grid color = --grid-line, donor:
 *     local-lmcanvas Canvas.tsx, MIT, Copyright (c) 2026 Max Lee)
 *   - color carries exactly two signals: RING = source system
 *     (Sayari indigo / ICIJ magenta / OpenSanctions teal), GLOW = top risk
 *     level (critical red / high orange / elevated amber / relevant gray).
 *     A sanctioned Sayari entity = indigo ring + red glow; provenance is
 *     never lost on the scariest nodes.
 *   - thin curved labeled edges; ships_to lanes keep their dashed treatment,
 *     amber when dual-use flagged.
 */

const riskGlow = (level: SayariRiskLevel) =>
  `0 0 0 3px color-mix(in oklab, ${RISK_LEVEL_COLORS[level]} 25%, transparent), 0 0 20px color-mix(in oklab, ${RISK_LEVEL_COLORS[level]} 40%, transparent)`;

const HIGHLIGHT_GLOW =
  "0 0 0 3px color-mix(in oklab, var(--foreground) 35%, transparent), 0 0 22px color-mix(in oklab, var(--foreground) 20%, transparent)";

const DUAL_USE_COLOR = "var(--risk-elevated)";
const EDGE_COLOR = "var(--ring)";

function truncate(s: string, n: number) {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

/**
 * Top risk level for a node, mapped from the flags the backend puts on
 * properties. Sanctioned = critical (red glow); dual-use = elevated (amber).
 */
function nodeRiskLevel(raw: ERGraphNode): SayariRiskLevel | null {
  const p = (raw.properties || {}) as Record<string, unknown>;
  if (p.sanctioned === true) return "critical";
  if (p.dual_use === true) return "elevated";
  return null;
}

/** Rendered label: mono uppercase node-type strip + the entity name. */
function NodeLabelContent({ raw }: { raw: ERGraphNode }) {
  const risk = nodeRiskLevel(raw);
  return (
    <div className="text-center">
      <div className="mb-0.5 font-mono text-[8px] uppercase leading-none tracking-[0.14em] text-muted-foreground">
        {raw.label}
        {risk === "critical" && (
          <span style={{ color: RISK_LEVEL_COLORS.critical }}> · sanctioned</span>
        )}
        {risk === "elevated" && (
          <span style={{ color: RISK_LEVEL_COLORS.elevated }}> · dual-use</span>
        )}
      </div>
      <div className="text-[11px] font-medium leading-tight text-foreground">
        {truncate(raw.name, 28)}
      </div>
    </div>
  );
}

function styleFor(raw: ERGraphNode, isSubject: boolean, sourceSystem: SourceSystem) {
  const ring = SOURCE_SYSTEM_META[sourceSystem].color;
  const risk = nodeRiskLevel(raw);
  return {
    background: "var(--card)",
    color: "var(--foreground)",
    border: `${isSubject ? "2.5px" : "1.5px"} solid ${ring}`,
    borderRadius: 999,
    padding: "7px 14px",
    fontSize: 11,
    fontWeight: 500,
    maxWidth: 200,
    boxShadow: risk ? riskGlow(risk) : "0 1px 2px rgb(0 0 0 / 0.05)",
  };
}

function buildEdges(edges: ERGraphEdge[]): Edge[] {
  return edges.map((e, i) => {
    const isTrade = e.type === "ships_to";
    const dualUse =
      isTrade && (e.properties as Record<string, unknown> | undefined)?.dual_use === true;
    const stroke = dualUse ? DUAL_USE_COLOR : EDGE_COLOR;
    return {
      id: `e-${i}-${e.source}-${e.target}-${e.type}`,
      // ER edge identity, matching the store's edgeKey — lets the time-travel
      // overlay dim inherited edges without re-deriving type from the id.
      data: { erKey: `${e.source}::${e.type}::${e.target}` },
      source: e.source,
      target: e.target,
      label: dualUse ? "ships_to ⚠ dual-use" : e.type,
      labelStyle: {
        fill: dualUse ? DUAL_USE_COLOR : "var(--muted-foreground)",
        fontSize: 9,
        fontFamily: "var(--font-geist-mono), monospace",
      },
      labelBgStyle: { fill: "var(--card)", fillOpacity: 0.85 },
      labelBgPadding: [3, 1] as [number, number],
      labelBgBorderRadius: 3,
      style: {
        stroke,
        strokeWidth: isTrade ? 1.6 : 1.2,
        strokeDasharray: isTrade ? "6 3" : undefined,
      },
      animated: isTrade,
      markerEnd: { type: MarkerType.ArrowClosed, color: stroke },
    };
  });
}

/** d3 augments simulation nodes with x/y/vx/vy and (for fixed nodes) fx/fy. */
type SimNode = SimulationNodeDatum & {
  id: string;
  raw: ERGraphNode;
  isSubject: boolean;
  /** Group-aware layout target: the centroid of this node's subject anchors. */
  tx: number;
  ty: number;
};
type SimLink = SimulationLinkDatum<SimNode>;

// Approximate half-size of a rendered node pill, used to turn React Flow's
// top-left node position into a center point for layout targets + hulls.
const NODE_HALF_W = 70;
const NODE_HALF_H = 18;

/** Deterministic 32-bit hash (FNV-1a) of a string — seeds reproducible jitter. */
function hashStr(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

/** Stable per-node jitter in [-amp, amp], so first-frame seeds don't stack. */
function seededJitter(id: string, axis: string, amp: number): number {
  return ((hashStr(id + axis) % 1000) / 1000 - 0.5) * 2 * amp;
}

/**
 * Stable anchor position per subject id: subjects are sorted by id and laid out
 * on a ring (single subject -> origin) so the arrangement is deterministic
 * across renders and reloads. A node is later pulled toward the centroid of the
 * anchors of every subject it belongs to, so shared nodes settle in the overlap.
 */
function computeSubjectAnchors(
  nodes: ERGraphNode[]
): Map<string, { x: number; y: number }> {
  const ids = new Set<string>();
  for (const n of nodes) for (const s of n.subject_ids ?? []) if (s) ids.add(s);
  const sorted = Array.from(ids).sort();
  const anchors = new Map<string, { x: number; y: number }>();
  const count = sorted.length;
  if (count === 0) return anchors;
  if (count === 1) {
    anchors.set(sorted[0], { x: 0, y: 0 });
    return anchors;
  }
  // Radius grows with subject count so neighborhoods don't crowd each other.
  const radius = Math.max(320, count * 150);
  sorted.forEach((id, i) => {
    const theta = (2 * Math.PI * i) / count - Math.PI / 2;
    anchors.set(id, { x: radius * Math.cos(theta), y: radius * Math.sin(theta) });
  });
  return anchors;
}

/** Layout target for a node: centroid of its subjects' anchors, else origin. */
function layoutTarget(
  node: ERGraphNode,
  anchors: Map<string, { x: number; y: number }>
): { x: number; y: number } {
  const subs = (node.subject_ids ?? []).filter((s) => anchors.has(s));
  if (subs.length === 0) return { x: 0, y: 0 };
  let x = 0;
  let y = 0;
  for (const s of subs) {
    const a = anchors.get(s)!;
    x += a.x;
    y += a.y;
  }
  return { x: x / subs.length, y: y / subs.length };
}

/** Stable hash of the input dataset; lets us detect "nothing changed, skip restart". */
function datasetKey(nodes: ERGraphNode[], edges: ERGraphEdge[]) {
  return (
    nodes.length +
    ":" +
    edges.length +
    ":" +
    nodes
      .map((n) => n.id)
      .sort()
      .join(",")
  );
}

type ContextMenuState = {
  node: ERGraphNode;
  x: number;
  y: number;
} | null;

type HoverState = {
  node: ERGraphNode;
  x: number;
  y: number;
} | null;

export interface GraphPanelProps {
  nodes: ERGraphNode[];
  edges: ERGraphEdge[];
  highlightedNodeIds?: Set<string>;
  /** Nodes the user pinned as chat context (dashed outline). */
  pinnedNodeIds?: Set<string>;
  /**
   * A request to pan/zoom the camera onto a specific node. Object identity
   * changes (via `tick`) drive the effect, so clicking the same node twice
   * still re-zooms.
   */
  focusRequest?: { nodeId: string; tick: number } | null;
  /** Node labels toggled off in the legend — hidden from layout and render. */
  hiddenLabels?: Set<NodeLabel>;
  /** User chose an expand kind from the right-click menu. */
  onExpandNode?: (nodeId: string, kind: ExpandKind) => void;
  /** User chose "pin/unpin to context" from the right-click menu. */
  onTogglePin?: (nodeId: string) => void;
  /** User chose "Open detail view" — opens the right-hand EntityDetailPanel. */
  onOpenDetail?: (nodeId: string) => void;
  /**
   * Lead counts from the latest broad search: how many leads are pinned to the
   * canvas vs total found. When total > shown, a small "Showing N of M leads"
   * badge renders so the user knows the graph is a top-N subset.
   */
  leadsShown?: number;
  leadsTotal?: number;
  /**
   * The UNPINNED leads from the latest search. Rendered as a dimmed, edge-less
   * overlay only while `showOverlayLeads` is true (badge toggle). They are NOT
   * part of the persistent graph, so they vanish on toggle-off and never carry
   * into the next turn's layout.
   */
  overlayLeadNodes?: ERGraphNode[];
  showOverlayLeads?: boolean;
  /** Toggle the unpinned-leads overlay (clicking the "N of M leads" badge). */
  onToggleLeads?: () => void;
  /**
   * Time-travel scope (spec §6). When set, the rendered nodes/edges are the
   * path-accumulated graph of a selected turn: members of `nodeIds`/`edgeKeys`
   * are the turn's OWN delta and pulse in; everything else is inherited from
   * ancestor turns and renders dimmed. `tick` increments per scope change so
   * the pulse retriggers on nodes that stay mounted across selections.
   */
  scopedDelta?: {
    nodeIds: Set<string>;
    edgeKeys: Set<string>;
    tick: number;
  } | null;
}

/**
 * Public wrapper. React Flow hooks like `useReactFlow` require a
 * `ReactFlowProvider` ancestor, so we keep that here and render the real
 * panel as an inner component.
 */
export function GraphPanel(props: GraphPanelProps) {
  return (
    <ReactFlowProvider>
      <GraphPanelInner {...props} />
    </ReactFlowProvider>
  );
}

/**
 * Force-directed graph view powered by d3-force.
 *
 * Why d3-force instead of a hierarchical (dagre) layout: investigation graphs
 * are messy and multi-rooted (officers cross-link to entities cross-link to
 * addresses cross-link to other entities via shared addresses). A force
 * simulation makes the structure legible — strongly-connected clusters
 * physically clump, shared-address shell patterns become visible, and the
 * "alive" motion during agent runs matches the dynamic investigation feel.
 * This is the same layout style Sayari Graph and other RegTech tools use.
 *
 * Lifecycle:
 *   1. When `nodes` or `edges` change, we (re)build the d3 simulation.
 *   2. The simulation runs ~5 seconds, emitting `tick` events ~60Hz.
 *   3. Each tick we copy the simulation's computed positions into
 *      `rfNodes` (React Flow's render state).
 *   4. User drags pin a node via `fx`/`fy` so the sim stops moving it.
 *   5. The subject (first node) is anchored at (0,0) so the rest of the
 *      graph orbits around a stable focal point — otherwise the whole
 *      cluster drifts every time new neighbors arrive.
 */
function GraphPanelInner({
  nodes,
  edges,
  highlightedNodeIds,
  pinnedNodeIds,
  focusRequest,
  hiddenLabels,
  onExpandNode,
  onTogglePin,
  onOpenDetail,
  leadsShown,
  leadsTotal,
  overlayLeadNodes,
  showOverlayLeads,
  onToggleLeads,
  scopedDelta,
}: GraphPanelProps) {
  const reactFlow = useReactFlow();

  // Only show the broad-search badge when there are genuinely more leads than
  // are pinned to the canvas (a top-N subset of a wider lead list).
  const showLeadsBadge =
    typeof leadsShown === "number" &&
    typeof leadsTotal === "number" &&
    leadsTotal > leadsShown &&
    leadsShown > 0;

  // The unpinned leads to overlay (when toggled on), minus any that already
  // exist as persistent nodes — those are real graph members, not overlay.
  const overlayNodes = useMemo(() => {
    if (!showOverlayLeads || !overlayLeadNodes?.length) return [] as ERGraphNode[];
    const baseIds = new Set(nodes.map((n) => n.id));
    return overlayLeadNodes.filter((n) => !baseIds.has(n.id));
  }, [showOverlayLeads, overlayLeadNodes, nodes]);

  const overlayIdSet = useMemo(
    () => new Set(overlayNodes.map((n) => n.id)),
    [overlayNodes]
  );

  // Persistent nodes + the transient overlay. The overlay never enters the
  // store's node map, so this merge is purely for rendering/layout this frame.
  const effectiveNodes = useMemo(
    () => (overlayNodes.length ? [...nodes, ...overlayNodes] : nodes),
    [nodes, overlayNodes]
  );

  const visibleNodes = useMemo(() => {
    if (!hiddenLabels?.size) return effectiveNodes;
    return effectiveNodes.filter((n) => !hiddenLabels.has(n.label));
  }, [effectiveNodes, hiddenLabels]);

  const visibleNodeIds = useMemo(
    () => new Set(visibleNodes.map((n) => n.id)),
    [visibleNodes]
  );

  // Which data sources are present, for the provenance legend.
  const presentSources = useMemo(() => {
    const set = new Set<SourceSystem>();
    for (const n of visibleNodes) set.add(sourceSystemOf(n.source_system));
    return Array.from(set);
  }, [visibleNodes]);

  // Trade-edge legend flags (Tier 2): any ships_to lane / any dual-use lane.
  const hasTradeEdges = useMemo(
    () => edges.some((e) => e.type === "ships_to"),
    [edges]
  );
  const hasDualUseEdges = useMemo(
    () =>
      edges.some(
        (e) =>
          e.type === "ships_to" &&
          (e.properties as Record<string, unknown> | undefined)?.dual_use === true
      ),
    [edges]
  );

  const visibleEdges = useMemo(() => {
    if (!hiddenLabels?.size) return edges;
    return edges.filter(
      (e) => visibleNodeIds.has(e.source) && visibleNodeIds.has(e.target)
    );
  }, [edges, hiddenLabels, visibleNodeIds]);

  // Persisted across renders so user drags survive sim restarts and so new
  // sims warm-start from the prior layout (avoiding the "everything explodes
  // out of (0,0)" startup jitter on every data change).
  const positionsRef = useRef<
    Map<string, { x: number; y: number; pinned: boolean }>
  >(new Map());
  const simRef = useRef<Simulation<SimNode, SimLink> | null>(null);
  const lastKeyRef = useRef<string>("");

  const [rfNodes, setRfNodes] = useState<Node[]>([]);
  const [rfEdges, setRfEdges] = useState<Edge[]>([]);

  // Build / rebuild the simulation whenever the underlying ER data changes.
  // Inside-effect logic is split into helpers but kept in the same effect so
  // there's exactly one running simulation at a time.
  useEffect(() => {
    const key = datasetKey(visibleNodes, visibleEdges);
    if (key === lastKeyRef.current) {
      // Deps changed identity without the dataset changing (the parent
      // rebuilds the node/edge arrays on every render, which during SSE
      // streaming means every token). The cleanup below already stopped the
      // still-relevant sim — resume it instead of leaving the layout frozen.
      simRef.current?.restart();
      return;
    }
    lastKeyRef.current = key;

    if (visibleNodes.length === 0) {
      simRef.current?.stop();
      simRef.current = null;
      setRfNodes([]);
      setRfEdges([]);
      return;
    }

    const subjectId = visibleNodes[0]?.id;

    // Group-aware layout (plan Phase 2): stable per-subject anchors. When the
    // graph carries subject membership we pull each node toward the centroid of
    // its subjects' anchors (multi-subject nodes land in the overlap). Legacy /
    // ICIJ graphs with no membership fall back to the original origin-anchored
    // behavior, so nothing about those layouts changes.
    const subjectAnchors = computeSubjectAnchors(visibleNodes);
    const grouped = subjectAnchors.size > 0;

    // Seed each sim node from its warm-started position (if any) or, for a
    // reproducible first frame, its layout target plus a deterministic hash
    // jitter (replaces the old Math.random scatter that reshuffled clusters).
    const simNodes: SimNode[] = visibleNodes.map((n) => {
      const prev = positionsRef.current.get(n.id);
      const isSubject = n.id === subjectId;
      const target = layoutTarget(n, subjectAnchors);
      const sn: SimNode = {
        id: n.id,
        raw: n,
        isSubject,
        tx: target.x,
        ty: target.y,
        x: prev?.x ?? target.x + seededJitter(n.id, "x", 40),
        y: prev?.y ?? target.y + seededJitter(n.id, "y", 40),
      };
      if (prev?.pinned) {
        // User-dragged: keep where they put it (wins over group pull).
        sn.fx = prev.x;
        sn.fy = prev.y;
      } else if (!grouped && isSubject) {
        // Ungrouped legacy graphs: keep the subject pinned at origin so the
        // network orbits a stable focal point (the pre-grouping behavior).
        sn.fx = 0;
        sn.fy = 0;
      }
      return sn;
    });

    // d3-force mutates link objects to point at node refs, so build fresh
    // each time. Filter out any edges whose endpoints aren't in our node set
    // (shouldn't happen, but defensive).
    const idSet = new Set(simNodes.map((n) => n.id));
    const simLinks: SimLink[] = visibleEdges
      .filter((e) => idSet.has(e.source) && idSet.has(e.target))
      .map((e) => ({ source: e.source, target: e.target }));

    simRef.current?.stop();

    const sim = forceSimulation<SimNode>(simNodes)
      // Repulsion. Stronger for bigger graphs so clusters don't smush together.
      .force(
        "charge",
        forceManyBody<SimNode>()
          .strength(-450)
          .distanceMax(800)
      )
      // Links want to be ~160px long. Lower strength = more "give"; the link
      // length is a hint, not a constraint.
      .force(
        "link",
        forceLink<SimNode, SimLink>(simLinks)
          .id((d) => d.id)
          .distance(160)
          .strength(0.55)
      )
      // Pull toward origin. Weak — the subject anchors / origin pin do most
      // of the centering work.
      .force("center", forceCenter(0, 0).strength(0.05))
      // Stop nodes from overlapping. Radius is a touch bigger than the
      // rendered node so labels don't collide either.
      .force("collide", forceCollide<SimNode>(72).strength(0.9))
      // Group pull: draw each node toward its subject-anchor centroid so
      // per-subject neighborhoods cluster and shared nodes sit between them.
      // Stronger when there are multiple subjects to keep regions separated;
      // gentle for a single subject so its cluster still breathes naturally.
      .force(
        "groupX",
        forceX<SimNode>((d) => d.tx).strength(
          grouped ? (subjectAnchors.size > 1 ? 0.16 : 0.04) : 0
        )
      )
      .force(
        "groupY",
        forceY<SimNode>((d) => d.ty).strength(
          grouped ? (subjectAnchors.size > 1 ? 0.16 : 0.04) : 0
        )
      )
      // Cool down a bit faster than default so the sim doesn't wobble forever.
      .alphaDecay(0.035)
      .velocityDecay(0.45);

    // Build React Flow nodes once; the tick handler updates only positions.
    const initialRfNodes: Node[] = simNodes.map((sn) => ({
      id: sn.id,
      position: { x: sn.x ?? 0, y: sn.y ?? 0 },
      data: {
        label: <NodeLabelContent raw={sn.raw} />,
        raw: sn.raw,
      },
      style: styleFor(sn.raw, sn.isSubject, sourceSystemOf(sn.raw.source_system)),
    }));
    setRfNodes(initialRfNodes);
    setRfEdges(buildEdges(visibleEdges));

    sim.on("tick", () => {
      // Copy sim positions back into React Flow + our cache.
      const byId = new Map(simNodes.map((sn) => [sn.id, sn]));
      setRfNodes((prev) =>
        prev.map((rn) => {
          const sn = byId.get(rn.id);
          if (!sn) return rn;
          const x = sn.x ?? 0;
          const y = sn.y ?? 0;
          positionsRef.current.set(rn.id, {
            x,
            y,
            pinned: positionsRef.current.get(rn.id)?.pinned ?? false,
          });
          return { ...rn, position: { x, y } };
        })
      );
    });

    simRef.current = sim;

    // Re-frame the camera so the growing network stays in view: once shortly
    // after each dataset change (the layout has had a moment to spread), and
    // once more when the simulation settles for a clean final frame.
    const refit = () => {
      try {
        reactFlow.fitView({ padding: 0.25, duration: 500, maxZoom: 1.1 });
      } catch {
        // pane unmounted mid-stream; ignore
      }
    };
    const fitTimer = window.setTimeout(refit, 700);
    sim.on("end", refit);

    return () => {
      window.clearTimeout(fitTimer);
      sim.stop();
    };
  }, [visibleNodes, visibleEdges, reactFlow]);

  // React Flow callback. Wired so that:
  //   - position drags during active drag update visuals smoothly
  //   - drag-end pins the node via fx/fy on the live simulation
  const handleNodesChange = useCallback((changes: NodeChange[]) => {
    setRfNodes((prev) => applyNodeChanges(changes, prev));
    const sim = simRef.current;
    for (const c of changes) {
      if (c.type !== "position" || !c.position) continue;
      const id = c.id;
      const { x, y } = c.position;
      if (sim) {
        const sn = sim.nodes().find((n) => n.id === id);
        if (sn) {
          // While dragging, fix the node to the pointer position so the sim
          // doesn't fight the user. On drag-end (`dragging === false`) we
          // mark it pinned so it stays put across future sim restarts.
          sn.fx = x;
          sn.fy = y;
          if (c.dragging === false) {
            sim.alpha(0.3).restart();
          } else {
            sim.alphaTarget(0.2).restart();
          }
        }
      }
      const wasPinned = positionsRef.current.get(id)?.pinned ?? false;
      positionsRef.current.set(id, {
        x,
        y,
        pinned: wasPinned || c.dragging === false,
      });
      if (c.dragging === false && sim) {
        sim.alphaTarget(0);
      }
    }
  }, []);

  // Pan/zoom onto the requested node. Using fitView with a single-node
  // selection is simpler than computing center coords ourselves and avoids
  // depending on the live `rfNodes` array (which changes every tick).
  useEffect(() => {
    if (!focusRequest) return;
    // `setTimeout` so we don't fight an in-progress fitView from the
    // initial render.
    const t = window.setTimeout(() => {
      try {
        reactFlow.fitView({
          nodes: [{ id: focusRequest.nodeId }],
          duration: 600,
          padding: 0.5,
          maxZoom: 1.5,
        });
      } catch {
        // node not in graph yet; ignore
      }
    }, 30);
    return () => window.clearTimeout(t);
  }, [focusRequest, reactFlow]);

  // Apply highlight (traversal path / claim hover), pinned-context cues, and
  // the time-travel scope as style overlays. Highlight is a neutral
  // foreground glow — color stays reserved for risk + source — and wins over
  // pinned when both apply. Time-travel: this turn's own delta pulses in,
  // inherited (ancestor-path) evidence dims (spec §6).
  const styledNodes = useMemo(() => {
    const hasHi = highlightedNodeIds && highlightedNodeIds.size > 0;
    const hasPin = pinnedNodeIds && pinnedNodeIds.size > 0;
    const hasOverlay = overlayIdSet.size > 0;
    if (!hasHi && !hasPin && !hasOverlay && !scopedDelta) return rfNodes;
    // Alternate keyframe names per scope change so the pulse retriggers on
    // DOM nodes React Flow keeps mounted across selections.
    const pulseName =
      scopedDelta && scopedDelta.tick % 2 === 0
        ? "node-pulse-in"
        : "node-pulse-in-b";
    return rfNodes.map((n) => {
      // Overlay (unpinned-lead) nodes render dimmed + dashed so they read as
      // secondary/exploratory vs the committed graph.
      if (hasOverlay && overlayIdSet.has(n.id)) {
        return {
          ...n,
          style: { ...n.style, opacity: 0.45, borderStyle: "dashed" as const },
        };
      }
      if (hasHi && highlightedNodeIds!.has(n.id)) {
        return { ...n, style: { ...n.style, boxShadow: HIGHLIGHT_GLOW } };
      }
      if (scopedDelta) {
        if (scopedDelta.nodeIds.has(n.id)) {
          return {
            ...n,
            style: {
              ...n.style,
              animation: `${pulseName} 750ms cubic-bezier(0.16, 1, 0.3, 1)`,
            },
          };
        }
        return { ...n, style: { ...n.style, opacity: 0.4 } };
      }
      if (hasPin && pinnedNodeIds!.has(n.id)) {
        return {
          ...n,
          style: {
            ...n.style,
            outline: "2px dashed var(--muted-foreground)",
            outlineOffset: 3,
          },
        };
      }
      return n;
    });
  }, [rfNodes, highlightedNodeIds, pinnedNodeIds, overlayIdSet, scopedDelta]);

  // Edge counterpart of the time-travel scope: inherited edges dim, the
  // turn's own edges keep full presence.
  const styledEdges = useMemo(() => {
    if (!scopedDelta) return rfEdges;
    return rfEdges.map((e) => {
      const erKey = (e.data as { erKey?: string } | undefined)?.erKey;
      if (erKey && scopedDelta.edgeKeys.has(erKey)) return e;
      return { ...e, style: { ...e.style, opacity: 0.35 } };
    });
  }, [rfEdges, scopedDelta]);

  // Subject id -> display name, for hull labels. A subject's name is the name
  // of its own node when that node is on the canvas; otherwise show a short id.
  const subjectLabels = useMemo(() => {
    const byId = new Map(visibleNodes.map((n) => [n.id, n.name] as const));
    const labels = new Map<string, string>();
    for (const n of visibleNodes) {
      for (const s of n.subject_ids ?? []) {
        if (s && !labels.has(s)) labels.set(s, byId.get(s) ?? `…${s.slice(-6)}`);
      }
    }
    return labels;
  }, [visibleNodes]);

  // Subject-grouped hull regions (plan Phase 3): collect each subject's member
  // node centers in flow coordinates from the live React Flow positions. Derived
  // from rfNodes so hulls track the simulation as it settles and follow drags.
  const hullGroups = useMemo<HullGroup[]>(() => {
    if (subjectLabels.size === 0) return [];
    const points = new Map<string, [number, number][]>();
    for (const rn of rfNodes) {
      const raw = (rn.data as { raw?: ERGraphNode }).raw;
      const subs = raw?.subject_ids ?? [];
      if (!subs.length) continue;
      const cx = rn.position.x + NODE_HALF_W;
      const cy = rn.position.y + NODE_HALF_H;
      for (const s of subs) {
        const arr = points.get(s);
        if (arr) arr.push([cx, cy]);
        else points.set(s, [[cx, cy]]);
      }
    }
    const groups: HullGroup[] = [];
    for (const [subjectId, pts] of points) {
      groups.push({
        subjectId,
        label: subjectLabels.get(subjectId) ?? `…${subjectId.slice(-6)}`,
        points: pts,
      });
    }
    return groups;
  }, [rfNodes, subjectLabels]);

  const [menu, setMenu] = useState<ContextMenuState>(null);
  const [hover, setHover] = useState<HoverState>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const handleContextMenu: NodeMouseHandler = useCallback((event, node) => {
    event.preventDefault();
    const raw = (node.data as { raw?: ERGraphNode }).raw;
    if (!raw) return;
    const rect = containerRef.current?.getBoundingClientRect();
    setMenu({
      node: raw,
      x: (event as unknown as MouseEvent).clientX - (rect?.left ?? 0),
      y: (event as unknown as MouseEvent).clientY - (rect?.top ?? 0),
    });
  }, []);

  const handleNodeMouseEnter: NodeMouseHandler = useCallback((event, node) => {
    const raw = (node.data as { raw?: ERGraphNode }).raw;
    if (!raw) return;
    const rect = containerRef.current?.getBoundingClientRect();
    setHover({
      node: raw,
      x: (event as unknown as MouseEvent).clientX - (rect?.left ?? 0),
      y: (event as unknown as MouseEvent).clientY - (rect?.top ?? 0),
    });
  }, []);

  const handleNodeMouseLeave = useCallback(() => setHover(null), []);
  const handlePaneClick = useCallback(() => {
    setMenu(null);
    setHover(null);
  }, []);

  return (
    <div ref={containerRef} className="relative h-full w-full bg-background">
      {nodes.length === 0 && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center text-center">
          <div>
            <div className="text-sm text-muted-foreground">
              {scopedDelta ? "No evidence at this turn" : "No graph yet"}
            </div>
            <div className="mt-1 text-xs text-muted-foreground/60">
              {scopedDelta
                ? "This turn's path hasn't accumulated graph evidence yet."
                : "Start an investigation to see the corporate network materialize."}
            </div>
          </div>
        </div>
      )}
      <ReactFlow
        nodes={styledNodes}
        edges={styledEdges}
        onNodesChange={handleNodesChange}
        onNodeContextMenu={handleContextMenu}
        onNodeMouseEnter={handleNodeMouseEnter}
        onNodeMouseLeave={handleNodeMouseLeave}
        onPaneClick={handlePaneClick}
        nodesDraggable
        nodesConnectable={false}
        elementsSelectable
        fitView
        fitViewOptions={{ padding: 0.2 }}
        proOptions={{ hideAttribution: true }}
        minZoom={0.2}
        maxZoom={2}
      >
        <Background
          variant={BackgroundVariant.Lines}
          gap={32}
          size={1}
          color="var(--grid-line)"
        />
        {/* Subject-grouped hull regions, behind the node layer (viewport-synced
            via ViewportPortal). Hidden during time-travel so the pulse/dim
            scope reads cleanly. */}
        {!scopedDelta && <EntityHullOverlay groups={hullGroups} />}
        <Controls />
      </ReactFlow>

      {/* Radial vignette fading the grid toward the pane edges (spec §1). */}
      <div className="canvas-vignette pointer-events-none absolute inset-0 z-4" />

      {showLeadsBadge && (
        <button
          type="button"
          onClick={onToggleLeads}
          title={
            showOverlayLeads
              ? "Hide the additional leads"
              : "Show the remaining leads on the canvas"
          }
          className="absolute right-3 top-3 z-10 flex cursor-pointer items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1 font-mono text-[10px] text-muted-foreground shadow-sm transition-colors hover:bg-muted hover:text-foreground"
        >
          <span
            className="inline-block h-2 w-2 rounded-full"
            style={{ backgroundColor: "var(--source-sayari)" }}
            aria-hidden
          />
          {showOverlayLeads ? (
            <>
              <span>
                Showing all{" "}
                <span className="font-medium text-foreground">{leadsTotal}</span>{" "}
                leads
              </span>
              <span className="text-muted-foreground/60">· hide extras</span>
            </>
          ) : (
            <>
              <span>
                Showing <span className="font-medium text-foreground">{leadsShown}</span>{" "}
                of <span className="font-medium text-foreground">{leadsTotal}</span> leads
              </span>
              <span className="text-foreground/70">
                · +{leadsTotal! - leadsShown!} show all
              </span>
            </>
          )}
        </button>
      )}

      {/* Legend card (bottom-left, spec §5): RISK + SOURCE scales. */}
      {nodes.length > 0 && (
        <div className="pointer-events-none absolute bottom-3 left-3 z-10 rounded-[10px] border border-border bg-card px-3 py-2 text-[10px] shadow-sm">
          <div className="mb-1 font-mono text-[8px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
            Risk
          </div>
          <div className="flex flex-col gap-0.5">
            {(
              [
                ["critical", "Critical"],
                ["high", "High"],
                ["elevated", "Elevated"],
                ["relevant", "Relevant"],
              ] as const
            ).map(([level, label]) => (
              <div key={level} className="flex items-center gap-1.5 text-foreground/80">
                <span
                  aria-hidden
                  className="inline-block h-2 w-2 rounded-full"
                  style={{ backgroundColor: RISK_LEVEL_COLORS[level] }}
                />
                {label}
              </div>
            ))}
          </div>
          {presentSources.length > 0 && (
            <>
              <div className="mb-1 mt-2 font-mono text-[8px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
                Source
              </div>
              <div className="flex flex-col gap-0.5">
                {presentSources.map((s) => (
                  <div key={s} className="flex items-center gap-1.5 text-foreground/80">
                    <span
                      aria-hidden
                      className="inline-block h-2 w-2 rounded-full border-2 bg-card"
                      style={{ borderColor: SOURCE_SYSTEM_META[s].color }}
                    />
                    {SOURCE_SYSTEM_META[s].label}
                  </div>
                ))}
              </div>
            </>
          )}
          {hasTradeEdges && (
            <div className="mt-2 flex flex-col gap-0.5">
              <div className="flex items-center gap-1.5 text-foreground/80">
                <span
                  aria-hidden
                  className="inline-block h-0 w-3 border-t-2 border-dashed"
                  style={{ borderColor: "var(--ring)" }}
                />
                Trade (ships_to)
              </div>
              {hasDualUseEdges && (
                <div className="flex items-center gap-1.5 text-foreground/80">
                  <span
                    aria-hidden
                    className="inline-block h-0 w-3 border-t-2 border-dashed"
                    style={{ borderColor: "var(--risk-elevated)" }}
                  />
                  Dual-use flagged
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {hover && !menu && <NodeTooltip hover={hover} />}
      {menu && (
        <NodeContextMenu
          menu={menu}
          isPinned={pinnedNodeIds?.has(menu.node.id) ?? false}
          onOpenDetail={
            onOpenDetail
              ? () => {
                  onOpenDetail(menu.node.id);
                  setMenu(null);
                }
              : undefined
          }
          onExpand={(kind) => {
            onExpandNode?.(menu.node.id, kind);
            setMenu(null);
          }}
          onTogglePin={() => {
            onTogglePin?.(menu.node.id);
            setMenu(null);
          }}
          onClose={() => setMenu(null)}
        />
      )}
    </div>
  );
}

function NodeTooltip({ hover }: { hover: NonNullable<HoverState> }) {
  const n = hover.node;
  const src = sourceSystemOf(n.source_system);
  const propEntries = Object.entries(n.properties || {})
    .filter(([k]) => !["name", "labels"].includes(k))
    .slice(0, 6);
  return (
    <div
      className="pointer-events-none absolute z-20 max-w-xs rounded-[10px] border border-border bg-card px-3 py-2 text-xs shadow-md"
      style={{ left: hover.x + 14, top: hover.y + 14 }}
    >
      <div className="mb-1 flex items-center gap-2">
        <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-[0.12em] text-muted-foreground">
          {n.label}
        </span>
        <span
          className="flex items-center gap-1 font-mono text-[9px] uppercase tracking-[0.12em]"
          style={{ color: SOURCE_SYSTEM_META[src].color }}
        >
          <span
            aria-hidden
            className="inline-block h-1.5 w-1.5 rounded-full border bg-card"
            style={{ borderColor: SOURCE_SYSTEM_META[src].color }}
          />
          {SOURCE_SYSTEM_META[src].label}
        </span>
        {n.source && (
          <span className="text-[9px] text-muted-foreground/70">{n.source}</span>
        )}
      </div>
      <div className="font-medium text-foreground">{n.name}</div>
      {propEntries.length > 0 && (
        <dl className="mt-1.5 space-y-0.5 text-[11px]">
          {propEntries.map(([k, v]) => (
            <div key={k} className="flex gap-2">
              <dt className="shrink-0 text-muted-foreground">{k}:</dt>
              <dd className="truncate text-foreground/80">{String(v)}</dd>
            </div>
          ))}
        </dl>
      )}
      <div className="mt-1.5 font-mono text-[8px] uppercase tracking-[0.14em] text-muted-foreground/60">
        right-click to expand
      </div>
    </div>
  );
}

const MENU_OPTIONS: { kind: ExpandKind; label: string; appliesTo: NodeLabel[] | "*" }[] = [
  { kind: "relationships", label: "Expand all relationships", appliesTo: "*" },
  { kind: "officers", label: "Show officers", appliesTo: ["Entity"] },
  { kind: "address_connections", label: "Find shared-address entities", appliesTo: ["Address"] },
  { kind: "er_links", label: "Show entity-resolution links", appliesTo: "*" },
];

function NodeContextMenu({
  menu,
  isPinned,
  onOpenDetail,
  onExpand,
  onTogglePin,
  onClose,
}: {
  menu: NonNullable<ContextMenuState>;
  isPinned: boolean;
  onOpenDetail?: () => void;
  onExpand: (kind: ExpandKind) => void;
  onTogglePin: () => void;
  onClose: () => void;
}) {
  const applicable = MENU_OPTIONS.filter(
    (o) => o.appliesTo === "*" || o.appliesTo.includes(menu.node.label)
  );
  const googleUrl = `https://www.google.com/search?q=${encodeURIComponent(menu.node.name)}`;
  return (
    <div
      className="absolute z-30 w-56 overflow-hidden rounded-[10px] border border-border bg-card text-xs shadow-md"
      style={{ left: menu.x, top: menu.y }}
      onContextMenu={(e) => e.preventDefault()}
    >
      <div className="border-b border-border px-3 py-2">
        <div className="truncate font-medium text-foreground">{menu.node.name}</div>
        <div className="font-mono text-[9px] uppercase tracking-[0.14em] text-muted-foreground">
          {menu.node.label}
        </div>
      </div>
      <ul>
        {onOpenDetail && (
          <li className="border-b border-border">
            <button
              onClick={onOpenDetail}
              className="block w-full cursor-pointer px-3 py-1.5 text-left font-medium text-foreground hover:bg-muted"
            >
              Open detail view
            </button>
          </li>
        )}
        <li className="border-b border-border">
          <button
            onClick={onTogglePin}
            className="block w-full cursor-pointer px-3 py-1.5 text-left font-medium text-foreground hover:bg-muted"
          >
            {isPinned ? "Unpin from chat context" : "Add to chat context"}
          </button>
        </li>
        {applicable.map((o) => (
          <li key={o.kind}>
            <button
              onClick={() => onExpand(o.kind)}
              className="block w-full cursor-pointer px-3 py-1.5 text-left text-foreground/80 hover:bg-muted"
            >
              {o.label}
            </button>
          </li>
        ))}
        <li className="border-t border-border">
          <a
            href={googleUrl}
            target="_blank"
            rel="noopener noreferrer"
            onClick={onClose}
            className="block w-full px-3 py-1.5 text-foreground/80 hover:bg-muted"
          >
            Search Google
          </a>
        </li>
        <li className="border-t border-border">
          <button
            onClick={() => {
              navigator.clipboard?.writeText(menu.node.id);
              onClose();
            }}
            className="block w-full cursor-pointer px-3 py-1.5 text-left text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            Copy node ID
          </button>
        </li>
      </ul>
    </div>
  );
}
