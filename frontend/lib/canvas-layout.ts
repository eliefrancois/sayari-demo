/*
 * Layout engine for the branching investigation canvas.
 *
 * Ported from local-lmcanvas (MIT License, Copyright (c) 2026 Max Lee):
 *   - constants                -> src/renderer/src/lib/canvasConstants.ts
 *   - resolveCollisions        -> src/renderer/src/lib/collisionResolution.ts
 *     (push-right sibling collision cascade, rightward-only to avoid ping-pong)
 *   - getEdgeHandles           -> src/renderer/src/lib/edgeHandles.ts
 *   - child placement heuristic-> src/renderer/src/hooks/useBranchFromNode.ts
 *     (follow-up children below the parent, fork siblings in a right lane)
 *   - DOM height measurer      -> src/renderer/src/lib/nodeDom.ts
 * Adapted to this app's Turn shape: the tree comes from server turn ids
 * (parent_turn_id) instead of canvas-local edges.
 */

import type { Turn } from "./conversation-store";

export const NODE_WIDTH = 450;
export const FALLBACK_NODE_HEIGHT = 320;
/** Gap below a parent for follow-up children (donor uses 150; our cards are
 *  denser and carry an explicit edge, so a smaller gap reads better). */
export const VERTICAL_CHILD_OFFSET = 72;
/** Right-lane offset for fork siblings (donor: NODE_WIDTH + 50 * 2). */
export const RIGHT_LANE_X_OFFSET = NODE_WIDTH + 100;
/** Fork children drop slightly below the parent's top edge (donor: +30). */
export const RIGHT_LANE_Y_NUDGE = 30;
export const COLLISION_PADDING_PX = 50;
const MAX_CASCADE_ITERATIONS_MIN = 40;

export type Pos = { x: number; y: number };
type Rect = { x: number; y: number; width: number; height: number };

/**
 * Stable canvas node id for a turn. Client id first: an optimistically
 * rendered turn keeps the same canvas identity (and thus its position) when
 * the server turnId arrives. Hydrated turns key on turnId; legacy
 * pre-branching turns fall back to their index.
 */
export function canvasIdOf(turn: Turn): string {
  return turn.clientId ?? turn.turnId ?? `legacy-${turn.index}`;
}

/* ──────────────── edge handle selection (donor: edgeHandles.ts) ─────────── */

export type EdgeHandlePair = { sourceHandle: string; targetHandle: string };

/**
 * Picks the best handle pair from the relative positions of source and
 * target. Never returns target-bottom: edges should not enter a child from
 * below (it produces a confusing upward loop).
 */
export function getEdgeHandles(source: Pos, target: Pos): EdgeHandlePair {
  const deltaX = target.x - source.x;
  const deltaY = target.y - source.y;
  const absX = Math.abs(deltaX);
  const absY = Math.abs(deltaY);

  if (absX >= absY) {
    if (deltaX >= 0) {
      return { sourceHandle: "source-right", targetHandle: "target-left" };
    }
    return { sourceHandle: "source-left", targetHandle: "target-right" };
  }
  if (deltaY >= 0) {
    return { sourceHandle: "source-bottom", targetHandle: "target-top" };
  }
  if (deltaX >= 0) {
    return { sourceHandle: "source-right", targetHandle: "target-left" };
  }
  return { sourceHandle: "source-left", targetHandle: "target-right" };
}

/* ──────────────── DOM height measurement (donor: nodeDom.ts) ────────────── */

/**
 * Measures a rendered canvas node's height from the DOM, correcting for the
 * React Flow zoom transform. Falls back when the node isn't mounted yet.
 */
export function makeDomHeightMeasurer(zoom: number): (id: string) => number {
  return (id: string): number => {
    if (typeof document === "undefined") return FALLBACK_NODE_HEIGHT;
    const el = document.querySelector(`.react-flow__node[data-id="${id}"]`);
    if (!el) return FALLBACK_NODE_HEIGHT;
    const rect = (el as HTMLElement).getBoundingClientRect();
    if (!zoom || zoom <= 0) return rect.height || FALLBACK_NODE_HEIGHT;
    return rect.height / zoom || FALLBACK_NODE_HEIGHT;
  };
}

/* ─────────── collision resolution (donor: collisionResolution.ts) ───────── */

function horizontalOverlap(a: Rect, b: Rect): boolean {
  return a.x < b.x + b.width && a.x + a.width > b.x;
}
function verticalOverlap(a: Rect, b: Rect): boolean {
  return a.y < b.y + b.height && a.y + a.height > b.y;
}
function offsetRect(base: Rect, d: Pos): Rect {
  return { x: base.x + d.x, y: base.y + d.y, width: base.width, height: base.height };
}

/**
 * When a node is inserted, any node whose box overlaps it on the same
 * horizontal lane is pushed right. Pushed nodes cascade further pushes, but
 * only rightward, never back leftward, to avoid ping-pong.
 *
 * Returns { nodeId -> new absolute position } for every node that must move.
 */
export function resolveCollisions(
  targetId: string,
  positions: Map<string, Pos>,
  measureHeight: (id: string) => number,
  options: { excludeIds?: string[]; paddingPx?: number } = {}
): Map<string, Pos> {
  const padding = options.paddingPx ?? COLLISION_PADDING_PX;
  const excludeIds = new Set(options.excludeIds ?? []);
  const moves = new Map<string, Pos>();

  const target = positions.get(targetId);
  if (!target) return moves;

  const rectOf = (id: string, pos: Pos): Rect => ({
    x: pos.x,
    y: pos.y,
    width: NODE_WIDTH,
    height: measureHeight(id),
  });

  const targetBox = rectOf(targetId, target);
  const candidates: string[] = [];
  const baseBoxes = new Map<string, Rect>();
  for (const [id, pos] of positions) {
    if (id === targetId || excludeIds.has(id)) continue;
    candidates.push(id);
    baseBoxes.set(id, rectOf(id, pos));
  }
  if (candidates.length === 0) return moves;

  let hasOverlap = false;
  for (const id of candidates) {
    const box = baseBoxes.get(id)!;
    if (verticalOverlap(targetBox, box) && horizontalOverlap(targetBox, box)) {
      hasOverlap = true;
      break;
    }
  }
  if (!hasOverlap) return moves;

  const displacements = new Map<string, Pos>();
  const queue: { nodeId: string | null; box: Rect }[] = [
    { nodeId: null, box: targetBox },
  ];
  const maxIterations = Math.max(
    MAX_CASCADE_ITERATIONS_MIN,
    candidates.length * candidates.length
  );
  let iterations = 0;

  while (queue.length > 0 && iterations < maxIterations) {
    iterations += 1;
    const source = queue.shift();
    if (!source) break;

    for (const id of candidates) {
      if (source.nodeId === id) continue;
      const baseBox = baseBoxes.get(id)!;
      const current = displacements.get(id) ?? { x: 0, y: 0 };
      const currentBox = offsetRect(baseBox, current);

      if (!verticalOverlap(source.box, currentBox)) continue;
      // Rightward-only push (donor invariant that prevents ping-pong).
      if (currentBox.x < source.box.x) continue;
      if (!horizontalOverlap(source.box, currentBox)) continue;

      const requiredX = source.box.x + source.box.width + padding;
      const deltaX = requiredX - currentBox.x;
      if (deltaX <= 0) continue;

      const next = { x: current.x + deltaX, y: current.y };
      displacements.set(id, next);
      queue.push({ nodeId: id, box: offsetRect(baseBox, next) });
    }
  }

  for (const [id, d] of displacements) {
    const baseBox = baseBoxes.get(id)!;
    moves.set(id, { x: baseBox.x + d.x, y: baseBox.y + d.y });
  }
  return moves;
}

/* ──────────── child placement + incremental tree layout ─────────────────── */

/**
 * Where a new child of `parent` should be placed: below the parent if it's
 * the parent's first child (the thread continues), in the right lane if the
 * parent already has children (a fork sibling). Donor heuristic from
 * useBranchFromNode, with the fork/follow-up decision driven by the tree
 * instead of prefill state.
 */
export function placeChild(
  parentPos: Pos,
  parentHeight: number,
  isFork: boolean
): Pos {
  if (isFork) {
    return {
      x: parentPos.x + RIGHT_LANE_X_OFFSET,
      y: parentPos.y + RIGHT_LANE_Y_NUDGE,
    };
  }
  return { x: parentPos.x, y: parentPos.y + parentHeight + VERTICAL_CHILD_OFFSET };
}

/**
 * Incremental layout: position every turn that doesn't have a position yet,
 * preserving everything already placed (including user drags). Works for
 * both single live appends (parents are mounted, DOM heights are real) and
 * full rebuilds on reload (fallback heights, processed in turn_index order so
 * parents land before children). Mutates nothing; returns the additions plus
 * any collision pushes.
 */
export function layoutNewTurns(
  turns: Turn[],
  existing: Map<string, Pos>,
  measureHeight: (id: string) => number,
  preferredPositions?: Map<string, Pos>
): Map<string, Pos> {
  const working = new Map(existing);
  const additions = new Map<string, Pos>();
  const byTurnId = new Map<string, Turn>();
  for (const t of turns) if (t.turnId) byTurnId.set(t.turnId, t);

  // Children placed so far, per parent canvas id. Drives fork detection.
  const childCount = new Map<string, number>();
  const bumpChildren = (parentCanvasId: string) =>
    childCount.set(parentCanvasId, (childCount.get(parentCanvasId) ?? 0) + 1);

  // Seed child counts from already-placed turns so a reload mid-pass and a
  // live fork both see the parent's true fan-out.
  const ordered = [...turns].sort((a, b) => a.index - b.index);
  const parentCanvasIdOf = (turn: Turn): string | null => {
    if (turn.parentTurnId) {
      const parent = byTurnId.get(turn.parentTurnId);
      if (parent) return canvasIdOf(parent);
      return null;
    }
    // Legacy linear chain: parent is simply the previous turn.
    if (turn.index > 0) {
      const prev = ordered.find((t) => t.index === turn.index - 1);
      return prev ? canvasIdOf(prev) : null;
    }
    return null;
  };
  for (const t of ordered) {
    if (!working.has(canvasIdOf(t))) continue;
    const pid = parentCanvasIdOf(t);
    if (pid) bumpChildren(pid);
  }

  // The measurer already falls back for unmounted nodes (fresh placements in
  // this same pass), so it's safe for every id.
  const heightOf = measureHeight;

  for (const turn of ordered) {
    const id = canvasIdOf(turn);
    if (working.has(id)) continue;

    const parentCanvasId = parentCanvasIdOf(turn);
    let pos: Pos;
    const preferred = preferredPositions?.get(id);
    if (preferred) {
      pos = preferred;
      if (parentCanvasId) bumpChildren(parentCanvasId);
    } else if (parentCanvasId && working.has(parentCanvasId)) {
      const parentPos = working.get(parentCanvasId)!;
      const siblings = childCount.get(parentCanvasId) ?? 0;
      pos = placeChild(parentPos, heightOf(parentCanvasId), siblings > 0);
      bumpChildren(parentCanvasId);
    } else {
      // Root (or orphan): origin, nudged down per prior root.
      pos = { x: 0, y: 0 };
    }

    working.set(id, pos);
    additions.set(id, pos);

    // Push overlapping non-ancestor nodes right (donor cascade).
    const moves = resolveCollisions(id, working, heightOf, {
      excludeIds: parentCanvasId ? [parentCanvasId] : [],
    });
    for (const [movedId, movedPos] of moves) {
      working.set(movedId, movedPos);
      additions.set(movedId, movedPos);
    }
  }
  return additions;
}

/* ──────────── thread-type inference (corner badge, spec §4) ─────────────── */

export type ThreadType = "ownership" | "sanctions" | "trade" | "identity";

export const THREAD_TYPE_META: Record<ThreadType, { label: string; color: string }> = {
  ownership: { label: "Ownership", color: "var(--source-sayari)" },
  sanctions: { label: "Sanctions", color: "var(--source-sanctions)" },
  trade: { label: "Trade", color: "var(--risk-elevated)" },
  identity: { label: "Identity", color: "var(--source-icij)" },
};

/**
 * Infer the thread type for a turn's corner badge from its intent (the user
 * message) and the tools it actually used. Live turns have toolCalls; reloads
 * fall back to the terminator's tools_used. Returns null = neutral badge.
 */
export function inferThreadType(turn: Turn): ThreadType | null {
  const tools = new Set<string>(turn.toolCalls.map((c) => c.tool));
  for (const t of turn.summary?.tools_used ?? turn.answer?.tools_used ?? []) {
    tools.add(t);
  }
  const msg = turn.userMessage.toLowerCase();

  if (tools.has("sayari_trade") || /\btrade|shipment|export|import|supply.?chain/.test(msg)) {
    return "trade";
  }
  if (
    /\bsanction|watchlist|ofac|sdn\b/.test(msg) ||
    (turn.sanctionsReview?.confirmed.length ?? 0) > 0 ||
    turn.sanctionsHits.length > 0
  ) {
    return "sanctions";
  }
  if (
    /\bowner|ownership|officer|subsidiar|parent|sharehold|beneficia|structure/.test(msg) ||
    tools.has("get_officers") ||
    tools.has("get_relationships")
  ) {
    return "ownership";
  }
  if (
    /\bsame (person|entity)|identity|alias|who is\b/.test(msg) ||
    tools.has("find_er_links") ||
    tools.has("sayari_resolve") ||
    tools.has("search_entity")
  ) {
    return "identity";
  }
  return null;
}
