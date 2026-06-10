"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Background,
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
  type Simulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from "d3-force";

import type {
  ExpandKind,
  GraphNode as ERGraphNode,
  GraphEdge as ERGraphEdge,
  NodeLabel,
  SourceSystem,
} from "@/lib/types";
import { SOURCE_SYSTEM_META, sourceSystemOf } from "@/lib/types";

const LABEL_COLORS: Record<NodeLabel, { bg: string; border: string; text: string }> = {
  Entity: { bg: "rgb(34 70 120 / 0.6)", border: "rgb(96 165 250)", text: "rgb(219 234 254)" },
  Officer: { bg: "rgb(120 53 15 / 0.5)", border: "rgb(251 146 60)", text: "rgb(254 215 170)" },
  Intermediary: { bg: "rgb(76 29 149 / 0.5)", border: "rgb(167 139 250)", text: "rgb(221 214 254)" },
  Address: { bg: "rgb(20 83 45 / 0.5)", border: "rgb(74 222 128)", text: "rgb(187 247 208)" },
  Other: { bg: "rgb(63 63 70 / 0.6)", border: "rgb(161 161 170)", text: "rgb(228 228 231)" },
};

const HIGHLIGHT_GLOW = "0 0 0 3px rgb(56 189 248 / 0.6), 0 0 24px rgb(56 189 248 / 0.45)";
const PINNED_GLOW = "0 0 0 2px rgb(250 204 21 / 0.7)";

// Tier 2 trade styling: ships_to lanes render dashed fuchsia; lanes that
// screened dual-use (our HS screen or a native Sayari BIS tag) go amber.
// Sanctioned nodes get a red ring so a sanctioned intermediary on a shortest
// path is visible at a glance. Functional-minimal — full reskin comes later.
const TRADE_EDGE_COLOR = "rgb(217 70 239)"; // fuchsia
const DUAL_USE_COLOR = "rgb(251 191 36)"; // amber
const SANCTIONED_RING = "0 0 0 2px rgb(248 113 113 / 0.85)";

function truncate(s: string, n: number) {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

/** Inline text badges for risk-relevant node flags (dual-use, sanctioned). */
function nodeBadges(raw: ERGraphNode): string {
  const p = (raw.properties || {}) as Record<string, unknown>;
  const out: string[] = [];
  if (p.sanctioned === true) out.push("⛔");
  if (p.dual_use === true) out.push("⚠ dual-use");
  return out.length ? ` ${out.join(" ")}` : "";
}

function styleFor(label: NodeLabel, isSubject: boolean, sourceSystem: SourceSystem) {
  const c = LABEL_COLORS[label] ?? LABEL_COLORS.Other;
  // The label drives the fill (Entity/Officer/Address semantics); the data
  // source drives the border color so provenance is visible at a glance.
  // ICIJ keeps the label's own border; Sayari/OpenSanctions get their accent.
  const borderColor =
    sourceSystem === "icij" ? c.border : SOURCE_SYSTEM_META[sourceSystem].color;
  return {
    background: c.bg,
    color: c.text,
    border: `${isSubject ? "2.5px" : "1.5px"} solid ${borderColor}`,
    borderRadius: 8,
    padding: "8px 12px",
    fontSize: 12,
    fontWeight: 500,
    maxWidth: 200,
  };
}

function buildEdges(edges: ERGraphEdge[]): Edge[] {
  return edges.map((e, i) => {
    const isTrade = e.type === "ships_to";
    const dualUse =
      isTrade && (e.properties as Record<string, unknown> | undefined)?.dual_use === true;
    const stroke = dualUse ? DUAL_USE_COLOR : isTrade ? TRADE_EDGE_COLOR : "rgb(82 82 91)";
    return {
      id: `e-${i}-${e.source}-${e.target}-${e.type}`,
      source: e.source,
      target: e.target,
      label: dualUse ? "ships_to ⚠ dual-use" : e.type,
      labelStyle: { fill: dualUse ? DUAL_USE_COLOR : "rgb(161 161 170)", fontSize: 10 },
      labelBgStyle: { fill: "rgb(24 24 27)", fillOpacity: 0.8 },
      style: {
        stroke,
        strokeWidth: isTrade ? 1.8 : 1.2,
        strokeDasharray: isTrade ? "6 3" : undefined,
      },
      animated: isTrade,
      markerEnd: { type: MarkerType.ArrowClosed, color: isTrade ? stroke : "rgb(113 113 122)" },
    };
  });
}

/** d3 augments simulation nodes with x/y/vx/vy and (for fixed nodes) fx/fy. */
type SimNode = SimulationNodeDatum & {
  id: string;
  raw: ERGraphNode;
  isSubject: boolean;
};
type SimLink = SimulationLinkDatum<SimNode>;

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
  /** Nodes the user pinned as chat context (yellow ring). */
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
  leadsShown,
  leadsTotal,
  overlayLeadNodes,
  showOverlayLeads,
  onToggleLeads,
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
    if (key === lastKeyRef.current) return;
    lastKeyRef.current = key;

    if (visibleNodes.length === 0) {
      simRef.current?.stop();
      simRef.current = null;
      setRfNodes([]);
      setRfEdges([]);
      return;
    }

    const subjectId = visibleNodes[0]?.id;

    // Seed each sim node with a known position (if any) or a small random
    // scatter near the origin. The scatter is tiny so the sim doesn't have
    // to do a big spread-out animation on the first run.
    const simNodes: SimNode[] = visibleNodes.map((n) => {
      const prev = positionsRef.current.get(n.id);
      const isSubject = n.id === subjectId;
      const sn: SimNode = {
        id: n.id,
        raw: n,
        isSubject,
        x: prev?.x ?? (Math.random() - 0.5) * 80,
        y: prev?.y ?? (Math.random() - 0.5) * 80,
      };
      if (isSubject) {
        // Anchor subject at origin so the whole graph orbits something stable.
        sn.fx = 0;
        sn.fy = 0;
      } else if (prev?.pinned) {
        // User-dragged: keep where they put it.
        sn.fx = prev.x;
        sn.fy = prev.y;
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
      // Pull toward origin. Weak — the subject anchor at (0,0) does most
      // of the centering work.
      .force("center", forceCenter(0, 0).strength(0.05))
      // Stop nodes from overlapping. Radius is a touch bigger than the
      // rendered node so labels don't collide either.
      .force("collide", forceCollide<SimNode>(72).strength(0.9))
      // Cool down a bit faster than default so the sim doesn't wobble forever.
      .alphaDecay(0.035)
      .velocityDecay(0.45);

    // Build React Flow nodes once; the tick handler updates only positions.
    const initialRfNodes: Node[] = simNodes.map((sn) => {
      const style = styleFor(
        sn.raw.label,
        sn.isSubject,
        sourceSystemOf(sn.raw.source_system)
      );
      // Sanctioned nodes carry a red ring (overridden by highlight/pin glows,
      // which is fine — those are transient attention cues).
      if ((sn.raw.properties as Record<string, unknown> | undefined)?.sanctioned === true) {
        (style as Record<string, unknown>).boxShadow = SANCTIONED_RING;
      }
      return {
        id: sn.id,
        position: { x: sn.x ?? 0, y: sn.y ?? 0 },
        data: {
          label: `${sn.raw.label}: ${truncate(sn.raw.name, 28)}${nodeBadges(sn.raw)}`,
          raw: sn.raw,
        },
        style,
      };
    });
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
    return () => {
      sim.stop();
    };
  }, [visibleNodes, visibleEdges]);

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

  // Apply highlight + pinned glow as a style overlay. Highlight (cyan) wins
  // over pinned (yellow) when both apply, since highlight is the transient
  // attention cue.
  const styledNodes = useMemo(() => {
    const hasHi = highlightedNodeIds && highlightedNodeIds.size > 0;
    const hasPin = pinnedNodeIds && pinnedNodeIds.size > 0;
    const hasOverlay = overlayIdSet.size > 0;
    if (!hasHi && !hasPin && !hasOverlay) return rfNodes;
    return rfNodes.map((n) => {
      // Overlay (unpinned-lead) nodes render dimmed + dashed so they read as
      // secondary/exploratory vs the committed graph.
      if (hasOverlay && overlayIdSet.has(n.id)) {
        return {
          ...n,
          style: { ...n.style, opacity: 0.5, borderStyle: "dashed" as const },
        };
      }
      if (hasHi && highlightedNodeIds!.has(n.id)) {
        return { ...n, style: { ...n.style, boxShadow: HIGHLIGHT_GLOW } };
      }
      if (hasPin && pinnedNodeIds!.has(n.id)) {
        return { ...n, style: { ...n.style, boxShadow: PINNED_GLOW } };
      }
      return n;
    });
  }, [rfNodes, highlightedNodeIds, pinnedNodeIds, overlayIdSet]);

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
    <div ref={containerRef} className="relative h-full w-full bg-zinc-950">
      {nodes.length === 0 && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-center">
          <div>
            <div className="text-zinc-600 text-sm">No graph yet</div>
            <div className="mt-1 text-xs text-zinc-700">
              Start an investigation to see the corporate network materialize.
            </div>
          </div>
        </div>
      )}
      <ReactFlow
        nodes={styledNodes}
        edges={rfEdges}
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
        <Background color="rgb(39 39 42)" gap={20} />
        <Controls className="!bg-zinc-900 !border-zinc-700 [&_button]:!bg-zinc-900 [&_button]:!border-zinc-700 [&_button:hover]:!bg-zinc-800 [&_button_svg]:!fill-zinc-300" />
      </ReactFlow>

      {showLeadsBadge && (
        <button
          type="button"
          onClick={onToggleLeads}
          title={
            showOverlayLeads
              ? "Hide the additional leads"
              : "Show the remaining leads on the canvas"
          }
          className="absolute right-3 top-3 z-10 flex items-center gap-1.5 rounded-md border border-zinc-700 bg-zinc-900/85 px-2.5 py-1 text-[11px] text-zinc-300 shadow-lg backdrop-blur transition hover:border-teal-500/60 hover:bg-zinc-800/90 hover:text-zinc-100"
        >
          <span
            className={
              "inline-block h-2 w-2 rounded-full " +
              (showOverlayLeads ? "bg-teal-300" : "bg-teal-400")
            }
            aria-hidden
          />
          {showOverlayLeads ? (
            <>
              <span>
                Showing all{" "}
                <span className="font-medium text-zinc-100">{leadsTotal}</span> leads
              </span>
              <span className="text-zinc-500">· hide extras</span>
            </>
          ) : (
            <>
              <span>
                Showing <span className="font-medium text-zinc-100">{leadsShown}</span> of{" "}
                <span className="font-medium text-zinc-100">{leadsTotal}</span> leads
              </span>
              <span className="text-teal-300/80">· +{leadsTotal! - leadsShown!} show all</span>
            </>
          )}
        </button>
      )}

      {presentSources.length > 0 && (
        <div className="pointer-events-none absolute bottom-3 left-3 z-10 rounded-md border border-zinc-800 bg-zinc-900/85 px-2.5 py-1.5 text-[10px] shadow-lg backdrop-blur">
          <div className="mb-1 font-medium uppercase tracking-wider text-zinc-500">
            Source
          </div>
          <div className="flex flex-col gap-1">
            {presentSources.map((s) => (
              <div key={s} className="flex items-center gap-1.5 text-zinc-300">
                <span
                  aria-hidden
                  className="inline-block h-2.5 w-2.5 rounded-sm border"
                  style={{ borderColor: SOURCE_SYSTEM_META[s].color }}
                />
                {SOURCE_SYSTEM_META[s].label}
              </div>
            ))}
            {hasTradeEdges && (
              <div className="flex items-center gap-1.5 text-zinc-300">
                <span
                  aria-hidden
                  className="inline-block h-0 w-3 border-t-2 border-dashed"
                  style={{ borderColor: TRADE_EDGE_COLOR }}
                />
                Trade (ships_to)
              </div>
            )}
            {hasDualUseEdges && (
              <div className="flex items-center gap-1.5 text-zinc-300">
                <span
                  aria-hidden
                  className="inline-block h-0 w-3 border-t-2 border-dashed"
                  style={{ borderColor: DUAL_USE_COLOR }}
                />
                Dual-use flagged
              </div>
            )}
          </div>
        </div>
      )}

      {hover && !menu && <NodeTooltip hover={hover} />}
      {menu && (
        <NodeContextMenu
          menu={menu}
          isPinned={pinnedNodeIds?.has(menu.node.id) ?? false}
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
  const propEntries = Object.entries(n.properties || {})
    .filter(([k]) => !["name", "labels"].includes(k))
    .slice(0, 6);
  return (
    <div
      className="pointer-events-none absolute z-20 max-w-xs rounded-md border border-zinc-700 bg-zinc-900/95 px-3 py-2 text-xs shadow-xl backdrop-blur"
      style={{ left: hover.x + 14, top: hover.y + 14 }}
    >
      <div className="mb-1 flex items-center gap-2">
        <span className="rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-zinc-300">
          {n.label}
        </span>
        <span
          className="rounded px-1.5 py-0.5 text-[10px]"
          style={{
            color: SOURCE_SYSTEM_META[sourceSystemOf(n.source_system)].color,
            backgroundColor: "rgb(24 24 27 / 0.6)",
          }}
        >
          {SOURCE_SYSTEM_META[sourceSystemOf(n.source_system)].label}
        </span>
        {n.source && (
          <span className="text-[10px] text-zinc-500">{n.source}</span>
        )}
      </div>
      <div className="font-medium text-zinc-100">{n.name}</div>
      {propEntries.length > 0 && (
        <dl className="mt-1.5 space-y-0.5 text-[11px]">
          {propEntries.map(([k, v]) => (
            <div key={k} className="flex gap-2">
              <dt className="shrink-0 text-zinc-500">{k}:</dt>
              <dd className="truncate text-zinc-300">{String(v)}</dd>
            </div>
          ))}
        </dl>
      )}
      <div className="mt-1.5 text-[10px] text-zinc-600">right-click to expand</div>
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
  onExpand,
  onTogglePin,
  onClose,
}: {
  menu: NonNullable<ContextMenuState>;
  isPinned: boolean;
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
      className="absolute z-30 w-56 overflow-hidden rounded-md border border-zinc-700 bg-zinc-900/95 text-xs shadow-xl backdrop-blur"
      style={{ left: menu.x, top: menu.y }}
      onContextMenu={(e) => e.preventDefault()}
    >
      <div className="border-b border-zinc-800 px-3 py-2">
        <div className="truncate font-medium text-zinc-100">{menu.node.name}</div>
        <div className="text-[10px] uppercase tracking-wide text-zinc-500">
          {menu.node.label}
        </div>
      </div>
      <ul>
        <li className="border-b border-zinc-800">
          <button
            onClick={onTogglePin}
            className="block w-full px-3 py-1.5 text-left font-medium text-amber-200 hover:bg-zinc-800"
          >
            {isPinned ? "Unpin from chat context" : "Add to chat context"}
          </button>
        </li>
        {applicable.map((o) => (
          <li key={o.kind}>
            <button
              onClick={() => onExpand(o.kind)}
              className="block w-full px-3 py-1.5 text-left text-zinc-200 hover:bg-zinc-800"
            >
              {o.label}
            </button>
          </li>
        ))}
        <li className="border-t border-zinc-800">
          <a
            href={googleUrl}
            target="_blank"
            rel="noopener noreferrer"
            onClick={onClose}
            className="block w-full px-3 py-1.5 text-zinc-300 hover:bg-zinc-800"
          >
            Search Google
          </a>
        </li>
        <li className="border-t border-zinc-800">
          <button
            onClick={() => {
              navigator.clipboard?.writeText(menu.node.id);
              onClose();
            }}
            className="block w-full px-3 py-1.5 text-left text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200"
          >
            Copy node ID
          </button>
        </li>
      </ul>
    </div>
  );
}
