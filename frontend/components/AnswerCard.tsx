// Inline answer body for clarify/follow-up turns: narrative, claims, follow-up chips.
"use client";

import { useRef, useState } from "react";
import { motion } from "framer-motion";
import { FileText, HelpCircle, Search } from "lucide-react";
import type { Claim, GraphNode, TurnAnswer } from "@/lib/types";
import { Markdown } from "@/components/ui/markdown";
import { ClaimSourceChip } from "./ClaimSourceChip";
import { cn } from "@/lib/utils";

/* Confidence stays grayscale. Color is reserved for risk severity and
 * source provenance (spec §2). */
const CONFIDENCE_DOT = {
  high: "bg-foreground",
  medium: "bg-muted-foreground",
  low: "bg-border",
} as const;

/**
 * Renders a TurnAnswer, the lighter terminator for CLARIFY / FOLLOW-UP turns.
 * Rendered INLINE inside the turn's lmcanvas card (no own border); the formal
 * RiskSummaryCard stays a visually distinct standalone card.
 */
export function AnswerCard({
  answer,
  nodesById,
  onSend,
  onGenerateReport,
  onHighlightNodes,
  onClearHighlight,
  onFocusNode,
}: {
  answer: TurnAnswer;
  nodesById?: Map<string, GraphNode>;
  onSend?: (text: string) => void;
  onGenerateReport?: (prompt: string) => void;
  onHighlightNodes?: (nodeIds: string[]) => void;
  onClearHighlight?: () => void;
  onFocusNode?: (nodeId: string) => void;
}) {
  const hasClarifications = answer.clarification_questions.length > 0;

  return (
    <div className="text-sm">
      {/* Main narrative */}
      <Markdown className="text-[12.5px] leading-relaxed text-foreground">
        {answer.answer}
      </Markdown>

      {/* Claims (only present on substantive follow-ups) */}
      {answer.claims.length > 0 && (
        <ul className="mt-3 space-y-1.5 border-t border-border pt-2.5">
          {answer.claims.map((c, i) => (
            <ClaimRow
              key={i}
              claim={c}
              nodesById={nodesById}
              onHighlightNodes={onHighlightNodes}
              onClearHighlight={onClearHighlight}
              onFocusNode={onFocusNode}
            />
          ))}
        </ul>
      )}

      {/* Clarification questions */}
      {hasClarifications && (
        <div className="mt-3 border-t border-border pt-2.5">
          <div className="mb-1.5 flex items-center gap-1.5 font-mono text-[9px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
            <HelpCircle className="size-3" /> To narrow the search
          </div>
          <div className="flex flex-col gap-1.5">
            {answer.clarification_questions.map((q, i) => (
              <div key={i} className="text-xs text-foreground/80">
                {q}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Offer to generate a formal risk report. The model-written pitch
          moves to the tooltip; the chip itself stays a stable, scannable
          label. The PDF download lives on the report card it produces. */}
      {answer.offer_risk_report && onGenerateReport && (
        <motion.button
          type="button"
          whileTap={{ scale: 0.99 }}
          onClick={() =>
            onGenerateReport(
              answer.risk_report_prompt || "Generate a full risk report based on what we've found."
            )
          }
          title={answer.risk_report_prompt || "Compile the formal risk report"}
          className="mt-3 flex w-full cursor-pointer items-center justify-center gap-2 rounded-[8px] border border-border bg-muted px-3 py-2 text-xs font-medium text-foreground transition-colors hover:border-foreground/40 hover:bg-accent"
        >
          <FileText className="size-3.5" />
          Generate risk report
        </motion.button>
      )}

      {/* FOLLOW UP: context-aware questions are the primary chips; entity
          followups (the older field) render as a secondary smaller row.
          Old stored answers without suggested_questions just show the row. */}
      <FollowUpSection
        questions={answer.suggested_questions ?? []}
        followups={answer.suggested_followups}
        onSend={onSend}
      />
    </div>
  );
}

/** Shared FOLLOW UP block (also used by the risk summary card). */
export function FollowUpSection({
  questions,
  followups,
  onSend,
  className,
}: {
  questions: { question: string; rationale: string }[];
  followups: { name: string; reason: string }[];
  onSend?: (text: string) => void;
  className?: string;
}) {
  if (!onSend || (questions.length === 0 && followups.length === 0)) return null;
  return (
    <div
      className={cn(
        "mt-3 flex flex-col items-start gap-1.5 border-t border-border pt-2.5",
        className
      )}
    >
      <span className="flex items-center gap-1.5 font-mono text-[9px] uppercase tracking-[0.18em] text-muted-foreground/70">
        <Search className="size-3" /> follow up
      </span>
      {questions.length > 0 && (
        <div className="flex w-full flex-col items-start gap-1">
          {questions.map((q, i) => (
            <motion.button
              key={i}
              type="button"
              whileTap={{ scale: 0.98 }}
              whileHover={{ y: -1 }}
              onClick={() => onSend(q.question)}
              title={q.rationale}
              className="inline-flex max-w-full cursor-pointer items-center gap-1.5 rounded-md border border-border bg-background px-2 py-1 text-left text-[11px] leading-snug text-foreground/85 transition-colors hover:border-foreground/40 hover:text-foreground focus:outline-none"
            >
              <span className="min-w-0">{q.question}</span>
            </motion.button>
          ))}
        </div>
      )}
      {followups.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {followups.map((f, i) => (
            <motion.button
              key={i}
              type="button"
              whileTap={{ scale: 0.97 }}
              onClick={() => onSend(`Investigate ${f.name}`)}
              title={f.reason}
              className="inline-flex cursor-pointer items-center gap-1 rounded-md border border-border/70 bg-background px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-[0.14em] text-muted-foreground transition-colors hover:border-foreground/40 hover:text-foreground focus:outline-none"
            >
              {f.name}
            </motion.button>
          ))}
        </div>
      )}
    </div>
  );
}

function ClaimRow({
  claim,
  nodesById,
  onHighlightNodes,
  onClearHighlight,
  onFocusNode,
}: {
  claim: Claim;
  nodesById?: Map<string, GraphNode>;
  onHighlightNodes?: (nodeIds: string[]) => void;
  onClearHighlight?: () => void;
  onFocusNode?: (nodeId: string) => void;
}) {
  const allNodeIds = claim.source_refs
    .map((r) => r.node_id)
    .filter((id): id is string => !!id);
  // Only ids actually on the evidence graph count. A claim whose refs all
  // point at off-graph ICIJ nodes would otherwise advertise a click that
  // highlights nothing.
  const nodeIds = nodesById
    ? allNodeIds.filter((id) => nodesById.has(id))
    : allNodeIds;
  const interactive = nodeIds.length > 0 && Boolean(onHighlightNodes);
  // Click: highlight the claim's entities on the graph and flash the source
  // chips. The flash tick remounts the chip row to retrigger the animation;
  // lastClickAt keeps the post-click mouseleave from instantly clearing.
  const [flashTick, setFlashTick] = useState(0);
  const lastClickAt = useRef(0);

  return (
    <li
      className={cn(
        "group rounded px-1.5 py-1 transition-colors",
        interactive ? "cursor-pointer hover:bg-muted" : "cursor-default"
      )}
      onMouseEnter={() => interactive && onHighlightNodes?.(nodeIds)}
      onMouseLeave={() => {
        if (Date.now() - lastClickAt.current > 1200) onClearHighlight?.();
      }}
      onClick={() => {
        if (!interactive) return;
        lastClickAt.current = Date.now();
        onHighlightNodes?.(nodeIds);
        setFlashTick((t) => t + 1);
      }}
      title={interactive ? "Click to highlight this claim's entities on the graph" : undefined}
    >
      <div className="flex items-start gap-1.5">
        <span
          className={cn(
            "mt-1 size-1.5 shrink-0 rounded-full",
            CONFIDENCE_DOT[claim.confidence]
          )}
          title={`${claim.confidence} confidence`}
        />
        <div className="min-w-0 flex-1">
          <div className="text-xs leading-relaxed text-foreground/80">
            <Markdown>{claim.text}</Markdown>
          </div>
          {claim.source_refs.length > 0 && (
            <div
              key={flashTick}
              className={cn("mt-1 flex flex-wrap items-center gap-1", flashTick > 0 && "chip-flash")}
            >
              {claim.source_refs.map((r, i) => (
                <ClaimSourceChip
                  key={i}
                  index={i + 1}
                  ref_={r}
                  node={r.node_id ? nodesById?.get(r.node_id) : undefined}
                  onFocusNode={onFocusNode}
                />
              ))}
            </div>
          )}
        </div>
      </div>
    </li>
  );
}
