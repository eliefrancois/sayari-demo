"use client";

/*
 * Semantic branch-label overlay for the investigation canvas.
 *
 * Ported from local-lmcanvas (MIT License, Copyright (c) 2026 Max Lee):
 *   src/renderer/src/components/Canvas/GroupSummaryOverlay.tsx
 *
 * What carried over 1:1: the viewport-transform DOM sync (no React re-render
 * on pan/zoom), the debounced "are all nodes visible" check, the zoom-gating
 * thresholds, group-bounds computation from member node rects, and the
 * overlap-stagger that pushes nested groups outward.
 *
 * What was adapted: the donor's per-node LLM summaries + generation skeletons
 * are dropped (this app uses synchronous heuristic titles only — no LLM/IPC),
 * and the donor's 10-color saturated rgba palette is replaced with a single
 * neutral OKLCH treatment driven by this app's design tokens (see the
 * `.group-summary-overlay` font-lock in globals.css). Color is reserved for
 * the evidence graph's source/risk semantics, so branch labels stay neutral.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useStore, useStoreApi, type Node } from "@xyflow/react";
import { FALLBACK_NODE_HEIGHT, NODE_WIDTH } from "@/lib/canvas-layout";
import type { GroupSummary } from "@/lib/groupSummary";

const GROUP_PADDING = 28;
const OVERLAP_STEP_BASE = 28;
const VIEWPORT_VISIBILITY_TOLERANCE = 8;
const VISIBILITY_DEBOUNCE_MS = 10;
/** Show labels when zoomed nearly all the way out (the whole tree reads as
 *  regions) … */
const NEAR_MAX_ZOOM_OUT_THRESHOLD = 0.15;
/** … and cull them once zoomed in past readable-region scale. */
const TOO_ZOOMED_IN_THRESHOLD = 3.0;

type GroupSummaryOverlayProps = {
  summaries: GroupSummary[];
  nodes: Node[];
};

type GroupBounds = {
  id: string;
  title: string;
  x: number;
  y: number;
  width: number;
  height: number;
  overlapLevel: number;
};

type NodeRect = {
  x: number;
  y: number;
  width: number;
  height: number;
  right: number;
  bottom: number;
};

function toFiniteNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const parsed = Number.parseFloat(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function getNodeDimensions(node: Node): { width: number; height: number } {
  const measured = node as unknown as {
    measured?: { width?: number; height?: number };
    style?: { width?: number | string; height?: number | string };
  };

  const widthCandidates = [
    toFiniteNumber(measured.measured?.width),
    toFiniteNumber(node.width),
    toFiniteNumber(measured.style?.width),
  ].filter((v): v is number => Boolean(v && v > 0));

  const heightCandidates = [
    toFiniteNumber(measured.measured?.height),
    toFiniteNumber(node.height),
    toFiniteNumber(measured.style?.height),
  ].filter((v): v is number => Boolean(v && v > 0));

  return {
    width: widthCandidates.length > 0 ? Math.max(...widthCandidates) : NODE_WIDTH,
    height:
      heightCandidates.length > 0
        ? Math.max(...heightCandidates)
        : FALLBACK_NODE_HEIGHT,
  };
}

function getNodeRect(node: Node): NodeRect {
  const { width, height } = getNodeDimensions(node);
  const x = node.position.x ?? 0;
  const y = node.position.y ?? 0;
  return { x, y, width, height, right: x + width, bottom: y + height };
}

function computeAllNodesVisible(
  nodes: Node[],
  vpX: number,
  vpY: number,
  zoom: number,
  vpWidth: number,
  vpHeight: number,
): boolean {
  if (nodes.length === 0 || vpWidth <= 0 || vpHeight <= 0) return false;

  let minX = Number.POSITIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;

  for (const node of nodes) {
    const rect = getNodeRect(node);
    minX = Math.min(minX, rect.x);
    minY = Math.min(minY, rect.y);
    maxX = Math.max(maxX, rect.right);
    maxY = Math.max(maxY, rect.bottom);
  }

  if (
    !Number.isFinite(minX) ||
    !Number.isFinite(minY) ||
    !Number.isFinite(maxX) ||
    !Number.isFinite(maxY)
  ) {
    return false;
  }

  const left = minX * zoom + vpX;
  const top = minY * zoom + vpY;
  const right = maxX * zoom + vpX;
  const bottom = maxY * zoom + vpY;

  return (
    left >= -VIEWPORT_VISIBILITY_TOLERANCE &&
    top >= -VIEWPORT_VISIBILITY_TOLERANCE &&
    right <= vpWidth + VIEWPORT_VISIBILITY_TOLERANCE &&
    bottom <= vpHeight + VIEWPORT_VISIBILITY_TOLERANCE
  );
}

export function GroupSummaryOverlay({
  summaries,
  nodes,
}: GroupSummaryOverlayProps) {
  const storeApi = useStoreApi();
  const viewportWidth = useStore((s) => s.width);
  const viewportHeight = useStore((s) => s.height);

  const transformRef = useRef<HTMLDivElement>(null);
  const [areAllNodesVisible, setAreAllNodesVisible] = useState(false);
  const [currentZoom, setCurrentZoom] = useState(1);

  // Subscribe to the viewport transform and apply it via the DOM (no React
  // re-render on pan/zoom). Only the debounced visibility check flips state.
  useEffect(() => {
    const debounceTimerRef = {
      current: 0 as unknown as ReturnType<typeof setTimeout>,
    };

    const applyTransform = (x: number, y: number, zoom: number) => {
      if (!transformRef.current) return;
      const el = transformRef.current;
      el.style.transform = `translate(${x}px, ${y}px) scale(${zoom})`;
      const safeZoom = zoom > 0 ? zoom : 1;
      const groupTarget = Math.max(11, Math.min(20, 12 * safeZoom));
      el.style.setProperty("--group-font-size", `${groupTarget / safeZoom}px`);
    };

    const unsubscribe = storeApi.subscribe((state) => {
      const [x, y, zoom] = state.transform;
      applyTransform(x, y, zoom);
      clearTimeout(debounceTimerRef.current);
      debounceTimerRef.current = setTimeout(() => {
        setCurrentZoom(zoom);
        setAreAllNodesVisible(
          computeAllNodesVisible(nodes, x, y, zoom, state.width, state.height),
        );
      }, VISIBILITY_DEBOUNCE_MS);
    });

    const state = storeApi.getState();
    const [x, y, zoom] = state.transform;
    applyTransform(x, y, zoom);
    setCurrentZoom(zoom);
    setAreAllNodesVisible(
      computeAllNodesVisible(nodes, x, y, zoom, state.width, state.height),
    );

    return () => {
      unsubscribe();
      clearTimeout(debounceTimerRef.current);
    };
  }, [storeApi, nodes, viewportWidth, viewportHeight]);

  const nodeMap = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);

  const groups = useMemo<GroupBounds[]>(() => {
    if (summaries.length === 0 || nodes.length === 0) return [];

    const rawGroups: (GroupBounds & { nodeIdSet: Set<string> })[] = [];

    for (const summary of summaries) {
      if (summary.nodeIds.length <= 1) continue;

      const memberNodes = summary.nodeIds
        .map((id) => nodeMap.get(id))
        .filter((n): n is Node => Boolean(n));

      if (memberNodes.length === 0) continue;

      let minX = Number.POSITIVE_INFINITY;
      let minY = Number.POSITIVE_INFINITY;
      let maxX = Number.NEGATIVE_INFINITY;
      let maxY = Number.NEGATIVE_INFINITY;

      for (const node of memberNodes) {
        const rect = getNodeRect(node);
        minX = Math.min(minX, rect.x);
        minY = Math.min(minY, rect.y);
        maxX = Math.max(maxX, rect.right);
        maxY = Math.max(maxY, rect.bottom);
      }

      rawGroups.push({
        id: summary.id,
        title: summary.title,
        x: minX - GROUP_PADDING,
        y: minY - GROUP_PADDING,
        width: maxX - minX + GROUP_PADDING * 2,
        height: maxY - minY + GROUP_PADDING * 2,
        overlapLevel: 0,
        nodeIdSet: new Set(summary.nodeIds),
      });
    }

    // Stagger overlapping groups outward so nested regions stay legible.
    for (let i = 0; i < rawGroups.length; i++) {
      for (let j = 0; j < i; j++) {
        const sharesNode = [...rawGroups[i].nodeIdSet].some((id) =>
          rawGroups[j].nodeIdSet.has(id),
        );
        if (sharesNode) {
          rawGroups[i].overlapLevel = Math.max(
            rawGroups[i].overlapLevel,
            rawGroups[j].overlapLevel + 1,
          );
        }
      }
    }

    const safeZoom = Math.max(currentZoom, 0.05);
    const overlapStep = OVERLAP_STEP_BASE / safeZoom;

    return rawGroups.map(({ nodeIdSet: _drop, ...group }) => {
      void _drop;
      const expand = group.overlapLevel * overlapStep;
      return {
        ...group,
        x: group.x - expand,
        y: group.y - expand,
        width: group.width + expand * 2,
        height: group.height + expand * 2,
      };
    });
  }, [nodeMap, nodes.length, summaries, currentZoom]);

  const isNearMaxZoomOut = currentZoom <= NEAR_MAX_ZOOM_OUT_THRESHOLD;
  const isTooZoomedIn = currentZoom >= TOO_ZOOMED_IN_THRESHOLD;
  const shouldShowOverlays =
    groups.length > 0 &&
    (isNearMaxZoomOut || (areAllNodesVisible && !isTooZoomedIn));

  return (
    <div className="group-summary-overlay pointer-events-none absolute inset-0 z-[5] overflow-hidden">
      <div ref={transformRef} style={{ transformOrigin: "0 0" }}>
        <AnimatePresence mode="sync">
          {shouldShowOverlays
            ? groups.map((group, index) => (
                <motion.div
                  key={group.id}
                  className="group-summary-region absolute pointer-events-none"
                  initial={{ opacity: 0, y: 6, scale: 0.992 }}
                  animate={{ opacity: 1, y: 0, scale: 1 }}
                  exit={{ opacity: 0, y: 4, scale: 0.996 }}
                  transition={{
                    duration: 0.15,
                    ease: [0.22, 1, 0.36, 1],
                    delay: Math.min(index * 0.02, 0.06),
                  }}
                  style={{
                    left: group.x,
                    top: group.y,
                    width: group.width,
                    height: group.height,
                  }}
                >
                  <div
                    className="absolute left-0 pointer-events-none"
                    style={{ top: "calc(-1em - 8px)" }}
                  >
                    <span
                      className="group-summary-label block whitespace-nowrap"
                      style={{ fontSize: "var(--group-font-size, 12px)" }}
                    >
                      {group.title}
                    </span>
                  </div>
                </motion.div>
              ))
            : null}
        </AnimatePresence>
      </div>
    </div>
  );
}
