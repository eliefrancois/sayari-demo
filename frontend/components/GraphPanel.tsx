"use client";

import { useMemo } from "react";
import {
  Background,
  Controls,
  MarkerType,
  ReactFlow,
  type Edge,
  type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import type { GraphNode as ERGraphNode, GraphEdge as ERGraphEdge, NodeLabel } from "@/lib/types";

const LABEL_COLORS: Record<NodeLabel, { bg: string; border: string; text: string }> = {
  Entity: { bg: "rgb(34 70 120 / 0.6)", border: "rgb(96 165 250)", text: "rgb(219 234 254)" },
  Officer: { bg: "rgb(120 53 15 / 0.5)", border: "rgb(251 146 60)", text: "rgb(254 215 170)" },
  Intermediary: { bg: "rgb(76 29 149 / 0.5)", border: "rgb(167 139 250)", text: "rgb(221 214 254)" },
  Address: { bg: "rgb(20 83 45 / 0.5)", border: "rgb(74 222 128)", text: "rgb(187 247 208)" },
  Other: { bg: "rgb(63 63 70 / 0.6)", border: "rgb(161 161 170)", text: "rgb(228 228 231)" },
};

/**
 * Lays out nodes in a radial pattern with the subject (first node) at center.
 * Quick deterministic layout — good enough for v1, swap to dagre later for big graphs.
 */
function layoutNodes(nodes: ERGraphNode[]): Node[] {
  if (nodes.length === 0) return [];

  // Pick subject: the first node added. (In practice the agent always searches first
  // and the top-match Officer node is what subsequent traversals start from, so it's
  // usually the first node we receive.)
  const [subject, ...rest] = nodes;
  const out: Node[] = [];

  const styleFor = (label: NodeLabel) => {
    const c = LABEL_COLORS[label] ?? LABEL_COLORS.Other;
    return {
      background: c.bg,
      color: c.text,
      border: `1.5px solid ${c.border}`,
      borderRadius: 8,
      padding: "8px 12px",
      fontSize: 12,
      fontWeight: 500,
      maxWidth: 200,
    };
  };

  // Center subject
  out.push({
    id: subject.id,
    position: { x: 0, y: 0 },
    data: { label: `${subject.label}: ${truncate(subject.name, 28)}` },
    style: { ...styleFor(subject.label), border: `2.5px solid ${LABEL_COLORS[subject.label]?.border}` },
  });

  // Lay neighbors on concentric rings.
  const r1 = 220;
  const r2 = 380;
  const ring1Count = Math.min(rest.length, 8);
  const ring2Count = rest.length - ring1Count;

  rest.forEach((n, i) => {
    let angle: number;
    let radius: number;
    if (i < ring1Count) {
      angle = (i / Math.max(ring1Count, 1)) * Math.PI * 2;
      radius = r1;
    } else {
      const j = i - ring1Count;
      angle = (j / Math.max(ring2Count, 1)) * Math.PI * 2 + Math.PI / Math.max(ring2Count, 1);
      radius = r2;
    }
    out.push({
      id: n.id,
      position: { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius },
      data: { label: `${n.label}: ${truncate(n.name, 28)}` },
      style: styleFor(n.label),
    });
  });

  return out;
}

function truncate(s: string, n: number) {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function buildEdges(edges: ERGraphEdge[]): Edge[] {
  return edges.map((e, i) => ({
    id: `e-${i}-${e.source}-${e.target}-${e.type}`,
    source: e.source,
    target: e.target,
    label: e.type,
    labelStyle: { fill: "rgb(161 161 170)", fontSize: 10 },
    labelBgStyle: { fill: "rgb(24 24 27)", fillOpacity: 0.8 },
    style: { stroke: "rgb(82 82 91)", strokeWidth: 1.2 },
    markerEnd: { type: MarkerType.ArrowClosed, color: "rgb(113 113 122)" },
  }));
}

export function GraphPanel({
  nodes,
  edges,
}: {
  nodes: ERGraphNode[];
  edges: ERGraphEdge[];
}) {
  const rfNodes = useMemo(() => layoutNodes(nodes), [nodes]);
  const rfEdges = useMemo(() => buildEdges(edges), [edges]);

  return (
    <div className="relative h-full w-full bg-zinc-950">
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
        nodes={rfNodes}
        edges={rfEdges}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        proOptions={{ hideAttribution: true }}
        minZoom={0.2}
        maxZoom={2}
      >
        <Background color="rgb(39 39 42)" gap={20} />
        <Controls className="!bg-zinc-900 !border-zinc-700 [&_button]:!bg-zinc-900 [&_button]:!border-zinc-700 [&_button:hover]:!bg-zinc-800 [&_button_svg]:!fill-zinc-300" />
      </ReactFlow>
    </div>
  );
}
