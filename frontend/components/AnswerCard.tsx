"use client";

import { motion } from "framer-motion";
import { FileText, HelpCircle, Search } from "lucide-react";
import type { Claim, GraphNode, TurnAnswer } from "@/lib/types";
import { SOURCE_SYSTEM_META, sourceSystemOf } from "@/lib/types";
import { Markdown } from "@/components/ui/markdown";
import { cn } from "@/lib/utils";

/* Confidence stays grayscale — color is reserved for risk severity and
 * source provenance (spec §2). */
const CONFIDENCE_DOT = {
  high: "bg-foreground",
  medium: "bg-muted-foreground",
  low: "bg-border",
} as const;

/**
 * Renders a TurnAnswer — the lighter terminator for CLARIFY / FOLLOW-UP turns.
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

      {/* Offer to generate a formal risk report */}
      {answer.offer_risk_report && onGenerateReport && (
        <motion.button
          type="button"
          whileTap={{ scale: 0.99 }}
          onClick={() =>
            onGenerateReport(
              answer.risk_report_prompt || "Generate a full risk report based on what we've found."
            )
          }
          className="mt-3 flex w-full cursor-pointer items-center justify-center gap-2 rounded-[8px] border border-border bg-muted px-3 py-2 text-xs font-medium text-foreground transition-colors hover:border-foreground/40 hover:bg-accent"
        >
          <FileText className="size-3.5" />
          {answer.risk_report_prompt
            ? truncate(answer.risk_report_prompt, 70)
            : "Generate full risk report"}
        </motion.button>
      )}

      {/* Suggested follow-ups (lmcanvas suggestion-chip language) */}
      {answer.suggested_followups.length > 0 && onSend && (
        <div className="mt-3 flex flex-col items-start gap-1.5 border-t border-border pt-2.5">
          <span className="flex items-center gap-1.5 font-mono text-[9px] uppercase tracking-[0.18em] text-muted-foreground/70">
            <Search className="size-3" /> follow-up investigations
          </span>
          <div className="flex flex-wrap gap-1.5">
            {answer.suggested_followups.map((f, i) => (
              <motion.button
                key={i}
                type="button"
                whileTap={{ scale: 0.97 }}
                whileHover={{ y: -1 }}
                onClick={() => onSend(`Investigate ${f.name}`)}
                title={f.reason}
                className="inline-flex cursor-pointer items-center gap-1.5 rounded-md border border-border bg-background px-2 py-1 font-mono text-[10px] uppercase tracking-[0.16em] text-foreground/80 transition-colors hover:border-foreground/40 hover:text-foreground focus:outline-none"
              >
                {f.name}
              </motion.button>
            ))}
          </div>
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
  const nodeIds = claim.source_refs
    .map((r) => r.node_id)
    .filter((id): id is string => !!id);

  return (
    <li
      className="group rounded px-1.5 py-1 transition-colors hover:bg-muted"
      onMouseEnter={() => nodeIds.length && onHighlightNodes?.(nodeIds)}
      onMouseLeave={() => onClearHighlight?.()}
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
          {nodeIds.length > 0 && (
            <div className="mt-1 flex flex-wrap gap-1">
              {claim.source_refs.map((r, i) => {
                if (!r.node_id) return null;
                const node = nodesById?.get(r.node_id);
                const srcColor = node
                  ? SOURCE_SYSTEM_META[sourceSystemOf(node.source_system)].color
                  : SOURCE_SYSTEM_META[r.source === "sayari" ? "sayari" : "icij"].color;
                return (
                  <button
                    key={i}
                    type="button"
                    onClick={() => onFocusNode?.(r.node_id!)}
                    className="inline-flex cursor-pointer items-center gap-1 rounded-md border border-border bg-card px-1.5 py-0.5 text-[10px] text-muted-foreground transition-colors hover:border-foreground/40 hover:text-foreground"
                    title="Focus this node in the graph"
                  >
                    <span
                      className="size-1.5 rounded-full border bg-card"
                      style={{ borderColor: srcColor }}
                    />
                    {node ? truncate(node.name, 24) : "source"}
                  </button>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </li>
  );
}

function truncate(s: string, n: number): string {
  return s.length <= n ? s : s.slice(0, n - 1).trimEnd() + "…";
}
