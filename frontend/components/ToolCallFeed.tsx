"use client";

import { useEffect, useMemo, useRef } from "react";
import { FileCheck2, MessageSquareReply } from "lucide-react";
import type { ToolCallEntry, Turn } from "@/lib/conversation-store";
import { Tool, type ToolPart } from "@/components/ui/tool";

/**
 * Map our internal ToolCallEntry to prompt-kit's ToolPart shape.
 *
 * Why a mapper: the prompt-kit `Tool` component was designed for the Vercel
 * AI SDK shape, where every tool call passes through these 4 lifecycle states:
 *   input-streaming  -> args being assembled
 *   input-available  -> args ready, executing
 *   output-available -> done with result
 *   output-error     -> failed
 *
 * We collapse it into "running" vs "done" since our backend emits both in one
 * shot. The visual treatment (icon, badge color) follows the SDK's conventions
 * so users coming from other AI apps get a familiar mental model.
 */
function toToolPart(call: ToolCallEntry): ToolPart {
  const state: ToolPart["state"] = call.hasResult ? "output-available" : "input-streaming";

  // Build a compact output object. We don't have the raw tool result on the
  // frontend (it's in the LLM's context only), so we synthesize a useful
  // structured view from the summary string + counts in the metadata event.
  const output: Record<string, unknown> | undefined = call.hasResult
    ? {
        summary: call.resultSummary,
        ...(call.resultMeta && Object.keys(call.resultMeta).length > 0
          ? { metadata: call.resultMeta }
          : {}),
      }
    : undefined;

  return {
    type: call.tool,
    state,
    input: call.args,
    output,
    toolCallId: call.callId,
  };
}

/**
 * Custom Tool wrapper that flags STRONG sanctions matches in red.
 * (The base Tool component doesn't know our domain semantics.)
 */
function ToolCard({ call }: { call: ToolCallEntry }) {
  const part = useMemo(() => toToolPart(call), [call]);
  const isStrongHit =
    call.tool === "check_sanctions" && call.resultSummary?.includes("STRONG MATCH");
  return (
    <div
      className={
        "mt-0 " +
        (isStrongHit ? "[&_>div]:border-red-700/50 [&_>div]:bg-red-950/20" : "")
      }
    >
      <Tool toolPart={part} defaultOpen={false} className="mt-0!" />
    </div>
  );
}

/**
 * The agent finishes every turn with a terminator "tool" (submit_summary for a
 * full risk report, submit_answer for a conversational reply). The backend does
 * NOT emit those as tool_call_start events — they're output-formatting steps,
 * not data-gathering tools — so they never reach `toolCalls`. We surface them
 * as a distinct activity entry so report/answer generation is visible here
 * rather than silently absent. Returns null while the turn is still working.
 */
function TerminatorEntry({ turn }: { turn: Turn }) {
  if (turn.summary) {
    return (
      <div className="mt-1 flex items-center gap-2 rounded-md border border-emerald-800/50 bg-emerald-950/20 px-2.5 py-1.5 text-xs text-emerald-200">
        <FileCheck2 className="size-3.5 shrink-0" />
        <span className="font-medium">Compiled risk report</span>
      </div>
    );
  }
  if (turn.answer) {
    return (
      <div className="mt-1 flex items-center gap-2 rounded-md border border-zinc-700/60 bg-zinc-900/40 px-2.5 py-1.5 text-xs text-zinc-300">
        <MessageSquareReply className="size-3.5 shrink-0" />
        <span className="font-medium">Answered</span>
      </div>
    );
  }
  return null;
}

export function ToolCallFeed({ turn }: { turn: Turn | null }) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const toolCalls = useMemo(() => turn?.toolCalls ?? [], [turn]);
  const sanctionsReview = turn?.sanctionsReview ?? null;
  const hasTerminator = !!(turn?.summary || turn?.answer);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [toolCalls.length, hasTerminator]);

  const hasContent = toolCalls.length > 0 || hasTerminator;

  // Quick stats for the header — gives the panel a clear purpose at a glance.
  const stats = useMemo(() => {
    const total = toolCalls.length;
    const done = toolCalls.filter((c) => c.hasResult).length;
    const review = sanctionsReview;
    if (review) {
      return {
        total,
        done,
        confirmed: review.confirmed.length,
        dismissed: review.dismissed.length,
        hasReview: true as const,
      };
    }
    const strongHits = toolCalls.filter(
      (c) => c.tool === "check_sanctions" && c.resultSummary?.includes("STRONG MATCH")
    ).length;
    return { total, done, strongHits, hasReview: false as const };
  }, [toolCalls, sanctionsReview]);

  return (
    <div className="flex h-full flex-col border-l border-zinc-800 bg-zinc-950/40">
      <header className="border-b border-zinc-800 px-3 py-2">
        <div className="flex items-center justify-between">
          <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-400">
            Agent activity
          </h2>
          <span className="text-[10px] tabular-nums text-zinc-500">
            {stats.done}/{stats.total} tool call{stats.total === 1 ? "" : "s"}
          </span>
        </div>
        {stats.hasReview ? (
          <div className="mt-1 space-y-0.5 text-[10px]">
            {stats.confirmed > 0 && (
              <div className="font-medium text-red-300">
                {stats.confirmed} confirmed in report
              </div>
            )}
            {stats.dismissed > 0 && (
              <div
                className="text-zinc-500"
                title="Agent reviewed raw name matches and excluded likely collisions"
              >
                {stats.dismissed} dismissed (name collision)
              </div>
            )}
            {stats.confirmed === 0 && stats.dismissed === 0 && (
              <div className="text-zinc-500">No strong sanctions matches</div>
            )}
          </div>
        ) : (
          !stats.hasReview &&
          stats.strongHits > 0 && (
            <div className="mt-1 text-[10px] font-medium text-red-300">
              {stats.strongHits} raw strong match
              {stats.strongHits === 1 ? "" : "es"} (reviewing…)
            </div>
          )
        )}
      </header>
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-3 py-2">
        {!hasContent && (
          <p className="mt-8 text-center text-xs text-zinc-600">
            Tool calls will stream in here as the agent investigates. Click any
            call to expand its arguments and result.
          </p>
        )}
        <div className="space-y-1">
          {toolCalls.map((c) => (
            <ToolCard key={c.callId} call={c} />
          ))}
          {turn && <TerminatorEntry turn={turn} />}
        </div>
      </div>
    </div>
  );
}
