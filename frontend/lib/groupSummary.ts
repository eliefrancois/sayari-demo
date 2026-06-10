/*
 * Group-summary types for the conversation-canvas semantic branch labels.
 *
 * Ported from local-lmcanvas (MIT License, Copyright (c) 2026 Max Lee):
 *   src/renderer/src/lib/groupSummary.ts
 * Trimmed to the heuristic-only path: this app derives group titles purely
 * from each turn's user message (no LLM / Electron IPC), so the Message-block
 * extraction, RAG-prefix stripping, and per-node LLM summary types from the
 * donor are intentionally dropped. `NodeId` collapses to a plain string (the
 * canvas node id = server turn id).
 *
 * Group-summary types live here (NOT in lib/types.ts) on purpose.
 */

/** A clustering input: one canvas node and the prompt text it represents. */
export type GroupSummaryCandidate = {
  /** Canvas node id (matches the React Flow node id for the turn). */
  nodeId: string;
  /** The user message used as the clustering / title signal. */
  prompt: string;
};

/** A heuristic clustering result before it's assigned a stable overlay id. */
export type GeneratedGroupSummary = {
  title: string;
  nodeIds: string[];
  metadata?: { confidence?: number };
};

/** A rendered group: stable id + title + the member node ids it encloses. */
export type GroupSummary = {
  id: string;
  title: string;
  nodeIds: string[];
};
