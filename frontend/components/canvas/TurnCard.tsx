"use client";

/*
 * lmcanvas-style conversation card. Card language (white hairline-border
 * rounded-[10px] shadow-sm card, corner badges, drop-in motion, divider,
 * streaming presentation, shimmer indicator) ported from local-lmcanvas
 * src/renderer/src/components/Canvas/CustomNode.tsx + NodeResponse.tsx
 * (MIT License, Copyright (c) 2026 Max Lee), adapted to this app's
 * SSE-driven Turn shape: tool calls and reasoning steps interleave inline,
 * the answer/summary terminators render below.
 */

import { useState } from "react";
import { motion } from "framer-motion";
import { Brain, ChevronDown, FileText, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { Markdown } from "@/components/ui/markdown";
import type { AgentThought, Turn } from "@/lib/conversation-store";
import type { GraphNode } from "@/lib/types";
import { inferThreadType, THREAD_TYPE_META } from "@/lib/canvas-layout";
import { ToolCallBlock } from "./ToolCallBlock";
import { AnswerCard } from "../AnswerCard";

const EASE = [0.16, 1, 0.3, 1] as const;

type TimelineItem =
  | { kind: "thought"; at: number; thought: AgentThought }
  | { kind: "tool"; at: number; call: Turn["toolCalls"][number] };

/** Thoughts and tool calls arrive on separate event streams; interleave them
 *  by wall-clock so the card reads like the agent's actual work order. */
function buildTimeline(turn: Turn): TimelineItem[] {
  const items: TimelineItem[] = [
    ...turn.thoughts.map((t) => ({ kind: "thought" as const, at: t.at, thought: t })),
    ...turn.toolCalls.map((c) => ({ kind: "tool" as const, at: c.startedAt, call: c })),
  ];
  return items.sort((a, b) => a.at - b.at);
}

export function TurnCard({
  turn,
  nodesById,
  onSend,
  onGenerateReport,
  onHighlightNodes,
  onClearHighlight,
  onFocusNode,
}: {
  turn: Turn;
  nodesById?: Map<string, GraphNode>;
  onSend: (text: string) => void;
  onGenerateReport: (prompt: string) => void;
  onHighlightNodes?: (nodeIds: string[]) => void;
  onClearHighlight?: () => void;
  onFocusNode?: (nodeId: string) => void;
}) {
  const isRunning = turn.status === "running";
  const isError = turn.status === "error";
  const timeline = buildTimeline(turn);
  const showStreamingText =
    isRunning && turn.streamingText && !turn.summary && !turn.answer;
  const showWaiting =
    isRunning && !turn.streamingText && !turn.summary && !turn.answer;
  // Thread-type corner badge (spec §4): inferred from the turn's intent and
  // the tools it actually used; neutral (no badge) when nothing matches.
  const threadType = inferThreadType(turn);
  const reportReady = turn.reportReady || turn.answer?.report_ready === true;

  return (
    <motion.div
      initial={{ scale: 0.96, opacity: 0, y: -6 }}
      animate={{ scale: 1, opacity: 1, y: 0 }}
      transition={{ duration: 0.18, ease: EASE }}
      style={{ transformOrigin: "top center" }}
      className="relative rounded-[10px] border border-border bg-card px-5 pb-4 pt-10 shadow-sm"
    >
      {/* Corner badge row (lmcanvas reserves the card's top strip for badges) */}
      <div className="absolute left-4 right-4 top-3 flex min-w-0 items-center gap-2">
        <span className="rounded-md bg-muted px-1.5 py-0.5 font-mono text-[9px] font-medium uppercase tracking-[0.16em] text-muted-foreground">
          turn {String(turn.index + 1).padStart(2, "0")}
        </span>
        {threadType && (
          <span
            className="rounded-md border px-1.5 py-0.5 font-mono text-[9px] font-medium uppercase tracking-[0.16em]"
            style={{
              color: THREAD_TYPE_META[threadType].color,
              borderColor: `color-mix(in oklab, ${THREAD_TYPE_META[threadType].color} 35%, transparent)`,
            }}
          >
            {THREAD_TYPE_META[threadType].label}
          </span>
        )}
        {turn.kind === "investigation" && (
          <span className="rounded-md border border-border px-1.5 py-0.5 font-mono text-[9px] font-medium uppercase tracking-[0.16em] text-foreground">
            report
          </span>
        )}
        {reportReady && !turn.summary && (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onGenerateReport(
                turn.answer?.risk_report_prompt ||
                  "Generate a full risk report based on what we've found."
              );
            }}
            title="The agent has enough evidence — compile the formal risk report"
            className="nodrag flex cursor-pointer items-center gap-1 rounded-md bg-foreground px-1.5 py-0.5 font-mono text-[9px] font-medium uppercase tracking-[0.16em] text-background transition-opacity hover:opacity-85"
          >
            <FileText size={9} /> report ready
          </button>
        )}
        {isError && (
          <span className="rounded-md border border-red-300 bg-red-50 px-1.5 py-0.5 font-mono text-[9px] font-medium uppercase tracking-[0.16em] text-red-700">
            error
          </span>
        )}
        <span className="ml-auto shrink-0">
          {isRunning && (
            <Loader2 size={12} className="animate-spin text-muted-foreground" />
          )}
        </span>
      </div>

      {/* Body is nodrag (donor pattern): the card drags from its chrome
          (badge strip / padding), while text stays selectable and inner
          buttons stay clickable without starting a canvas drag. */}
      <div className="nodrag select-text">
      {/* User question */}
      <div className="whitespace-pre-wrap break-words text-[13px] font-semibold leading-snug text-foreground">
        {turn.userMessage}
      </div>

      <div className="my-3 h-px bg-border" />

      {/* Agent work: reasoning + tool calls, interleaved in time order */}
      {timeline.length > 0 && (
        <div className="flex flex-col gap-0.5">
          {timeline.map((item) =>
            item.kind === "thought" ? (
              <ThoughtBlock key={`th-${item.thought.id}`} thought={item.thought} />
            ) : (
              <ToolCallBlock key={item.call.callId} call={item.call} />
            )
          )}
        </div>
      )}

      {/* Sanctions adjudication (was the Tool Feed header; now inline) */}
      {turn.sanctionsReview && (
        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 font-mono text-[9px] uppercase tracking-[0.14em]">
          <span className="text-muted-foreground">sanctions review</span>
          {turn.sanctionsReview.confirmed.length > 0 && (
            <span className="font-medium text-red-700">
              {turn.sanctionsReview.confirmed.length} confirmed
            </span>
          )}
          {turn.sanctionsReview.dismissed.length > 0 && (
            <span
              className="text-muted-foreground"
              title="Agent reviewed raw name matches and excluded likely collisions"
            >
              {turn.sanctionsReview.dismissed.length} dismissed (name collision)
            </span>
          )}
          {turn.sanctionsReview.confirmed.length === 0 &&
            turn.sanctionsReview.dismissed.length === 0 && (
              <span className="text-muted-foreground">no strong matches</span>
            )}
        </div>
      )}

      {/* Live streaming text with pulsing caret; card height auto-grows */}
      {showStreamingText && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.12, ease: EASE }}
          className="mt-2"
        >
          <Markdown className="text-[12.5px] leading-relaxed text-foreground">
            {turn.streamingText}
          </Markdown>
          <span className="streaming-caret" aria-hidden />
        </motion.div>
      )}

      {/* Waiting (tools running, no live text yet) */}
      {showWaiting && (
        <div className="mt-2 flex items-center gap-1.5 text-[10px] text-muted-foreground">
          <Loader2 size={12} className="animate-spin" />
          <span className="node-shimmer font-medium">{inflightLabel(turn)}</span>
        </div>
      )}

      {/* Conversational answer renders inline in the turn card.
          (Investigation summaries render as a distinct Risk Report card below.) */}
      {turn.answer && (
        <div className="mt-3">
          <AnswerCard
            answer={turn.answer}
            nodesById={nodesById}
            onSend={onSend}
            onGenerateReport={onGenerateReport}
            onHighlightNodes={onHighlightNodes}
            onClearHighlight={onClearHighlight}
            onFocusNode={onFocusNode}
          />
        </div>
      )}
      </div>
    </motion.div>
  );
}

/** Collapsible reasoning step: compact echo of lmcanvas's ThinkingView. */
function ThoughtBlock({ thought }: { thought: AgentThought }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="my-0.5 overflow-hidden rounded-[8px] border border-transparent">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full cursor-pointer items-center gap-1.5 rounded-[8px] px-1 py-0.5 text-left transition-colors hover:bg-muted"
        aria-label={expanded ? "Collapse reasoning" : "Expand reasoning"}
      >
        <Brain size={11} className="shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1 truncate text-[10px] italic text-muted-foreground">
          {firstSentence(thought.text)}
        </span>
        <ChevronDown
          size={11}
          className={cn(
            "shrink-0 text-muted-foreground transition-transform",
            expanded && "rotate-180"
          )}
        />
      </button>
      {expanded && (
        <div className="px-1.5 pb-1.5 pt-0.5 text-[11px] leading-relaxed text-muted-foreground">
          {thought.text}
        </div>
      )}
    </div>
  );
}

function firstSentence(text: string): string {
  const first = text.split(/(?<=[.!?])\s/)[0] ?? text;
  if (first.length <= 90) return first;
  return first.slice(0, 87).trimEnd() + "…";
}

function inflightLabel(turn: Turn): string {
  const latest = turn.toolCalls[turn.toolCalls.length - 1];
  if (!latest || latest.hasResult) return "Thinking";
  switch (latest.tool) {
    case "search_entity":
      return "Searching ICIJ Offshore Leaks";
    case "get_relationships":
      return "Traversing relationships";
    case "get_officers":
      return "Pulling officers";
    case "find_address_connections":
      return "Cross-referencing addresses";
    case "find_er_links":
      return "Checking entity-resolution links";
    case "check_sanctions":
      return "Querying OpenSanctions";
    case "sayari_search":
      return "Searching Sayari";
    case "sayari_trade":
      return "Pulling trade data";
    default:
      return "Investigating";
  }
}
