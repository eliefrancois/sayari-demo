"use client";

/*
 * React Flow node types for the branching investigation canvas.
 *
 * Donor: local-lmcanvas (MIT License, Copyright (c) 2026 Max Lee) —
 * invisible per-side handles (NodeHandles.tsx), hover-revealed circular "+"
 * follow-up button and nodrag content zones (CustomNode.tsx), drop-in motion.
 * Adapted to this app's Turn shape: the card content is the stage-1 TurnCard,
 * the Risk Report renders as a distinct attached card, and branching is
 * analyst-driven only (spec §4 — the agent never creates branches).
 */

import { memo, useEffect, useRef, useState } from "react";
import { Handle, Position, type NodeProps, type Node } from "@xyflow/react";
import { motion } from "framer-motion";
import { CornerDownRight, Plus, X } from "lucide-react";
import { cn } from "@/lib/utils";
import type { Turn } from "@/lib/conversation-store";
import type { GraphNode } from "@/lib/types";
import { NODE_WIDTH } from "@/lib/canvas-layout";
import { TurnCard } from "./TurnCard";
import { RiskSummaryCard } from "../RiskSummaryCard";

const HANDLE_CLASS = "!opacity-0 !pointer-events-none";

/** Invisible anchors on every side so edges can attach from any direction. */
function NodeTargetHandles() {
  return (
    <>
      <Handle isConnectable={false} type="target" position={Position.Top} id="target-top" className={HANDLE_CLASS} />
      <Handle isConnectable={false} type="target" position={Position.Right} id="target-right" className={HANDLE_CLASS} />
      <Handle isConnectable={false} type="target" position={Position.Left} id="target-left" className={HANDLE_CLASS} />
    </>
  );
}

function NodeSourceHandles() {
  return (
    <>
      <Handle isConnectable={false} type="source" position={Position.Right} id="source-right" className={HANDLE_CLASS} />
      <Handle isConnectable={false} type="source" position={Position.Bottom} id="source-bottom" className={HANDLE_CLASS} />
      <Handle isConnectable={false} type="source" position={Position.Left} id="source-left" className={HANDLE_CLASS} />
    </>
  );
}

export type TurnNodeData = {
  turn: Turn;
  nodesById: Map<string, GraphNode>;
  /** This card is the head of the active path (selected or live head). */
  isActive: boolean;
  /** True when this card is being viewed in time-travel (selected, not head). */
  isTimeTravelTarget: boolean;
  /** Fork allowed: the turn has a server id and nothing is streaming. */
  canFork: boolean;
  onFork: (turn: Turn) => void;
  /** Send a message parented on THIS turn (follow-up chips, report button). */
  onSendFrom: (turn: Turn, text: string) => void;
  onGenerateReportFrom: (turn: Turn, prompt: string) => void;
  onHighlightNodes?: (nodeIds: string[]) => void;
  onClearHighlight?: () => void;
  onFocusNode?: (nodeId: string) => void;
};

export type TurnFlowNode = Node<TurnNodeData, "turn">;

function TurnNodeComponent({ data }: NodeProps<TurnFlowNode>) {
  const {
    turn,
    nodesById,
    isActive,
    isTimeTravelTarget,
    canFork,
    onFork,
    onSendFrom,
    onGenerateReportFrom,
    onHighlightNodes,
    onClearHighlight,
    onFocusNode,
  } = data;
  const [hovered, setHovered] = useState(false);

  return (
    <div
      style={{ width: NODE_WIDTH }}
      className="relative"
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <NodeTargetHandles />

      {/* Selection ring: the active-path head gets a steady foreground ring;
          a time-travel target gets a dashed one so "viewing the past" reads
          differently from "this is where my next message goes". */}
      <div
        aria-hidden
        className={cn(
          "pointer-events-none absolute -inset-1 rounded-[13px] transition-opacity duration-150",
          isActive || isTimeTravelTarget ? "opacity-100" : "opacity-0"
        )}
        style={{
          outline: isTimeTravelTarget
            ? "2px dashed var(--muted-foreground)"
            : "2px solid color-mix(in oklab, var(--foreground) 55%, transparent)",
          outlineOffset: 0,
        }}
      />

      <TurnCard
        turn={turn}
        nodesById={nodesById}
        onSend={(text) => onSendFrom(turn, text)}
        onGenerateReport={(prompt) => onGenerateReportFrom(turn, prompt)}
        onHighlightNodes={onHighlightNodes}
        onClearHighlight={onClearHighlight}
        onFocusNode={onFocusNode}
      />

      {/* The formal Risk Report stays a visually distinct card, attached
          below its turn with a short connector (stage-1 look, now per-node). */}
      {turn.summary && (
        <>
          <div className="flex justify-center py-0.5" aria-hidden>
            <svg width="6" height="22" viewBox="0 0 6 22" fill="none">
              <path
                d="M3 1 C 3 8, 3 14, 3 21"
                stroke="var(--muted-foreground)"
                strokeWidth="2.25"
                strokeLinecap="round"
                opacity="0.6"
              />
            </svg>
          </div>
          <div className="nodrag select-text">
            <RiskSummaryCard
              summary={turn.summary}
              nodesById={nodesById}
              onFollowup={(name) => onSendFrom(turn, `Investigate ${name}`)}
              onHighlightNodes={onHighlightNodes}
              onClearHighlight={onClearHighlight}
              onFocusNode={onFocusNode}
            />
          </div>
        </>
      )}

      {/* Fork affordance (donor's circular "+", analyst-driven only): hover
          reveals it at the bottom edge; it creates a DRAFT child card. */}
      <div
        className={cn(
          "nodrag absolute -bottom-4 left-0 right-0 z-10 flex justify-center transition-opacity duration-150",
          hovered && canFork ? "opacity-100" : "pointer-events-none opacity-0"
        )}
      >
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onFork(turn);
          }}
          onMouseDown={(e) => e.stopPropagation()}
          disabled={!canFork}
          className="flex h-7 w-7 cursor-pointer items-center justify-center rounded-full bg-foreground text-background shadow-lg transition hover:opacity-90"
          title="Branch from this turn"
          aria-label="Branch from this turn"
        >
          <Plus className="h-3.5 w-3.5" />
        </button>
      </div>

      <NodeSourceHandles />
    </div>
  );
}

export const TurnNode = memo(TurnNodeComponent);

/* ──────────────────────────── draft fork card ───────────────────────────── */

export type DraftNodeData = {
  parentTurnIndex: number;
  onSubmit: (text: string) => void;
  onCancel: () => void;
};

export type DraftFlowNode = Node<DraftNodeData, "draft">;

/**
 * The empty child card a fork creates: a bare lmcanvas card with a textarea.
 * Enter submits (parented on the forked turn), Esc cancels. Mirrors the
 * donor's blank-node prompt input, simplified to a single uncontrolled field.
 */
function DraftNodeComponent({ data }: NodeProps<DraftFlowNode>) {
  const { parentTurnIndex, onSubmit, onCancel } = data;
  const ref = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    // Focus after mount; rAF lets React Flow finish placing the node first.
    const raf = requestAnimationFrame(() => ref.current?.focus());
    return () => cancelAnimationFrame(raf);
  }, []);

  const submit = () => {
    const text = ref.current?.value.trim();
    if (text) onSubmit(text);
  };

  return (
    <motion.div
      initial={{ scale: 0.96, opacity: 0, y: -6 }}
      animate={{ scale: 1, opacity: 1, y: 0 }}
      transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
      style={{ transformOrigin: "top center", width: NODE_WIDTH }}
      className="relative rounded-[10px] border border-border bg-card px-5 pb-4 pt-10 shadow-sm"
    >
      <NodeTargetHandles />
      <div className="absolute left-4 right-4 top-3 flex min-w-0 items-center gap-2">
        <span className="flex items-center gap-1 rounded-md bg-muted px-1.5 py-0.5 font-mono text-[9px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
          <CornerDownRight size={9} /> branch from turn{" "}
          {String(parentTurnIndex + 1).padStart(2, "0")}
        </span>
        <button
          type="button"
          onClick={onCancel}
          className="nodrag ml-auto cursor-pointer rounded-md p-0.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          title="Cancel branch (Esc)"
          aria-label="Cancel branch"
        >
          <X size={12} />
        </button>
      </div>
      <textarea
        ref={ref}
        rows={3}
        placeholder="Ask a different question from this point…"
        className="nodrag w-full resize-none bg-transparent text-[13px] font-medium leading-snug text-foreground outline-none placeholder:text-muted-foreground/60"
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            submit();
          } else if (e.key === "Escape") {
            e.preventDefault();
            onCancel();
          }
        }}
      />
      <div className="mt-1 flex items-center justify-between font-mono text-[8px] uppercase tracking-[0.16em] text-muted-foreground/60">
        <span>enter to branch · esc to cancel</span>
        <span>sibling branches stay isolated</span>
      </div>
    </motion.div>
  );
}

export const DraftNode = memo(DraftNodeComponent);
