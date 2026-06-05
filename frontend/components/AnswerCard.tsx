"use client";

import { FileText, HelpCircle, Search } from "lucide-react";
import type { Claim, GraphNode, TurnAnswer } from "@/lib/types";
import { Markdown } from "@/components/ui/markdown";
import { PromptSuggestion } from "@/components/ui/prompt-suggestion";
import { cn } from "@/lib/utils";

const CONFIDENCE_DOT = {
  high: "bg-emerald-400",
  medium: "bg-amber-400",
  low: "bg-zinc-500",
} as const;

const NODE_LABEL_DOT: Record<GraphNode["label"], string> = {
  Entity: "bg-blue-400",
  Officer: "bg-orange-400",
  Intermediary: "bg-violet-400",
  Address: "bg-green-400",
  Other: "bg-zinc-400",
};

/**
 * Renders a TurnAnswer — the lighter terminator for CLARIFY / FOLLOW-UP turns.
 * Distinct visual treatment from RiskSummaryCard (no big risk header) so the
 * user can tell a conversational reply from a formal investigation memo.
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
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/40 p-3 text-sm">
      {/* Main narrative */}
      <div className="prose prose-invert prose-sm max-w-none text-zinc-200">
        <Markdown>{answer.answer}</Markdown>
      </div>

      {/* Claims (only present on substantive follow-ups) */}
      {answer.claims.length > 0 && (
        <ul className="mt-3 space-y-1.5 border-t border-zinc-800 pt-2.5">
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

      {/* Clarification questions -> clickable to answer */}
      {hasClarifications && (
        <div className="mt-3 border-t border-zinc-800 pt-2.5">
          <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-zinc-500">
            <HelpCircle className="size-3" /> To narrow the search
          </div>
          <div className="flex flex-col gap-1.5">
            {answer.clarification_questions.map((q, i) => (
              <div key={i} className="text-xs text-zinc-300">
                {q}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Offer to generate a formal risk report */}
      {answer.offer_risk_report && onGenerateReport && (
        <button
          type="button"
          onClick={() =>
            onGenerateReport(
              answer.risk_report_prompt || "Generate a full risk report based on what we've found."
            )
          }
          className="mt-3 flex w-full items-center justify-center gap-2 rounded-md border border-sky-700/50 bg-sky-600/15 px-3 py-2 text-xs font-medium text-sky-200 transition hover:border-sky-600 hover:bg-sky-600/25"
        >
          <FileText className="size-3.5" />
          {answer.risk_report_prompt
            ? truncate(answer.risk_report_prompt, 70)
            : "Generate full risk report"}
        </button>
      )}

      {/* Suggested follow-ups */}
      {answer.suggested_followups.length > 0 && onSend && (
        <div className="mt-3 border-t border-zinc-800 pt-2.5">
          <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-zinc-500">
            <Search className="size-3" /> Follow-up investigations
          </div>
          <div className="flex flex-wrap gap-1.5">
            {answer.suggested_followups.map((f, i) => (
              <PromptSuggestion
                key={i}
                size="sm"
                onClick={() => onSend(`Investigate ${f.name}`)}
                title={f.reason}
                className="border-zinc-700 bg-zinc-900/60 text-xs text-zinc-300 hover:border-zinc-600 hover:bg-zinc-800 hover:text-zinc-100"
              >
                {f.name}
              </PromptSuggestion>
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
      className="group rounded px-1.5 py-1 transition hover:bg-zinc-800/40"
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
          <div className="text-xs leading-relaxed text-zinc-300">
            <Markdown>{claim.text}</Markdown>
          </div>
          {nodeIds.length > 0 && (
            <div className="mt-1 flex flex-wrap gap-1">
              {claim.source_refs.map((r, i) => {
                if (!r.node_id) return null;
                const node = nodesById?.get(r.node_id);
                return (
                  <button
                    key={i}
                    type="button"
                    onClick={() => onFocusNode?.(r.node_id!)}
                    className="inline-flex items-center gap-1 rounded border border-zinc-700 bg-zinc-900/60 px-1.5 py-0.5 text-[10px] text-zinc-400 transition hover:border-zinc-500 hover:text-zinc-200"
                    title="Focus this node in the graph"
                  >
                    {node && (
                      <span
                        className={cn(
                          "size-1.5 rounded-full",
                          NODE_LABEL_DOT[node.label]
                        )}
                      />
                    )}
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
