"use client";

import { useMemo } from "react";
import { ViewportPortal } from "@xyflow/react";
import { polygonHull } from "d3-polygon";

/**
 * Subject-grouped hull regions for the evidence graph (plan Phase 3).
 *
 * Each subject the backend attributed nodes to (GraphNode.subject_ids) gets a
 * translucent convex-hull region enclosing its member nodes. A node shared by
 * two subjects (e.g. a shortest-path intermediary) is a member of both hulls
 * and — because the group-aware layout pulls it to the centroid of its
 * subjects' anchors — physically sits in the overlap. The fills use
 * `mix-blend-mode: multiply`, so wherever two hulls overlap the region reads
 * DARKER: that darker zone IS the shared-membership intersection, with no extra
 * geometry needed.
 *
 * Rendered through `ViewportPortal` so the SVG lives inside React Flow's
 * viewport transform (pans/zooms with the graph) and behind the node layer.
 * Color is intentionally neutral — the graph reserves saturated color for the
 * source ring and risk glow (docs/04-tier3-ux-spec.md §2).
 */

export interface HullGroup {
  subjectId: string;
  label: string;
  /** Member node centers in flow coordinates. */
  points: [number, number][];
}

export interface EntityHullOverlayProps {
  groups: HullGroup[];
}

// Outward padding (flow units) so the hull clears the node pills it wraps.
const HULL_PADDING = 58;
// Corner smoothing as a fraction of each edge — keeps the region organic, not
// a hard polygon. 0 = sharp polygon, ~0.18 = pleasantly rounded.
const SMOOTHING = 0.18;
const MAX_HULL_LABELS = 6;
// Subtle per-subject hues — neutral family, distinct enough to read two regions.
const HULL_FILLS = [
  "color-mix(in oklab, var(--source-sayari) 18%, var(--foreground))",
  "color-mix(in oklab, var(--source-sanctions) 16%, var(--foreground))",
  "color-mix(in oklab, var(--risk-elevated) 14%, var(--foreground))",
];

/** Centroid of a point cloud. */
function centroid(points: [number, number][]): [number, number] {
  let x = 0;
  let y = 0;
  for (const [px, py] of points) {
    x += px;
    y += py;
  }
  return [x / points.length, y / points.length];
}

/**
 * Push each hull vertex outward from the centroid by `pad` so the region
 * clears the node it wraps. Cheap, robust, and good enough at demo graph sizes
 * (a true Minkowski offset would be overkill here).
 */
function padHull(hull: [number, number][], pad: number): [number, number][] {
  const [cx, cy] = centroid(hull);
  return hull.map(([x, y]) => {
    const dx = x - cx;
    const dy = y - cy;
    const len = Math.hypot(dx, dy) || 1;
    return [x + (dx / len) * pad, y + (dy / len) * pad] as [number, number];
  });
}

/**
 * A rounded closed SVG path through `pts` using Catmull-Rom-style control
 * points. Falls back to a straight polygon when smoothing is off.
 */
function roundedPath(pts: [number, number][], smoothing: number): string {
  const n = pts.length;
  if (n === 0) return "";
  if (n < 3 || smoothing <= 0) {
    return pts.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x},${y}`).join(" ") + " Z";
  }
  let d = `M${pts[0][0]},${pts[0][1]}`;
  for (let i = 0; i < n; i++) {
    const p0 = pts[(i - 1 + n) % n];
    const p1 = pts[i];
    const p2 = pts[(i + 1) % n];
    const p3 = pts[(i + 2) % n];
    const c1x = p1[0] + ((p2[0] - p0[0]) / 6) * smoothing * 3;
    const c1y = p1[1] + ((p2[1] - p0[1]) / 6) * smoothing * 3;
    const c2x = p2[0] - ((p3[0] - p1[0]) / 6) * smoothing * 3;
    const c2y = p2[1] - ((p3[1] - p1[1]) / 6) * smoothing * 3;
    d += ` C${c1x},${c1y} ${c2x},${c2y} ${p2[0]},${p2[1]}`;
  }
  return d + " Z";
}

/**
 * The region outline for a group. With 3+ points it's a padded convex hull;
 * with 1-2 points (a hull is degenerate) we synthesize a small rounded blob
 * around the points so the subject still reads as a region.
 */
function regionForPoints(points: [number, number][]): {
  path: string;
  labelAt: [number, number];
} | null {
  if (points.length === 0) return null;
  if (points.length < 3) {
    const [cx, cy] = centroid(points);
    const spread = points.reduce(
      (m, [x, y]) => Math.max(m, Math.hypot(x - cx, y - cy)),
      0
    );
    const r = spread + HULL_PADDING;
    const blob: [number, number][] = Array.from({ length: 12 }, (_, i) => {
      const t = (Math.PI * 2 * i) / 12;
      return [cx + r * Math.cos(t), cy + r * Math.sin(t)] as [number, number];
    });
    return { path: roundedPath(blob, SMOOTHING), labelAt: [cx, cy - r] };
  }
  const hull = polygonHull(points);
  if (!hull) return null;
  const padded = padHull(hull as [number, number][], HULL_PADDING);
  const top = padded.reduce((a, b) => (b[1] < a[1] ? b : a), padded[0]);
  return { path: roundedPath(padded, SMOOTHING), labelAt: top };
}

export function EntityHullOverlay({ groups }: EntityHullOverlayProps) {
  const regions = useMemo(
    () =>
      groups
        .map((g) => ({ group: g, region: regionForPoints(g.points) }))
        .filter((r): r is { group: HullGroup; region: NonNullable<ReturnType<typeof regionForPoints>> } => r.region !== null),
    [groups]
  );

  if (regions.length === 0) return null;

  return (
    <ViewportPortal>
      <svg
        // Anchored at the flow origin; overflow visible so hulls at negative
        // coordinates still paint. Non-interactive — purely decorative.
        style={{
          position: "absolute",
          left: 0,
          top: 0,
          overflow: "visible",
          pointerEvents: "none",
          zIndex: 0,
        }}
        width={1}
        height={1}
      >
        <g style={{ mixBlendMode: "multiply" }}>
          {regions.map(({ group, region }, index) => (
            <path
              key={`hull-${group.subjectId}`}
              d={region.path}
              fill={HULL_FILLS[index % HULL_FILLS.length]}
              fillOpacity={0.09}
              stroke={HULL_FILLS[index % HULL_FILLS.length]}
              strokeOpacity={0.22}
              strokeWidth={1.5}
            />
          ))}
        </g>
        {regions.slice(0, MAX_HULL_LABELS).map(({ group, region }) => (
          <text
            key={`hull-label-${group.subjectId}`}
            x={region.labelAt[0]}
            y={region.labelAt[1] - 8}
            textAnchor="middle"
            style={{
              fill: "var(--muted-foreground)",
              fontFamily: "var(--font-geist-sans), system-ui, sans-serif",
              fontSize: 11,
              fontWeight: 600,
            }}
          >
            {group.label}
          </text>
        ))}
      </svg>
    </ViewportPortal>
  );
}
