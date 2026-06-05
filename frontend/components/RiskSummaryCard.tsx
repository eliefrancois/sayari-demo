"use client";

import { useMemo } from "react";
import { ExternalLink, Search, RotateCcw, FileText, ShieldAlert, HelpCircle, GitBranch } from "lucide-react";
import type {
  Claim,
  GraphNode,
  RiskSummary,
  SayariRiskFactor,
  SourceRef,
} from "@/lib/types";
import {
  SAYARI_LEVEL_META,
  sayariLevelRank,
  pathNodeIds,
  humanizeRiskFactor,
} from "@/lib/types";
import { RiskSignalBadge } from "./RiskSignalBadge";
import { PromptSuggestion } from "@/components/ui/prompt-suggestion";
import { Markdown } from "@/components/ui/markdown";
import {
  HoverCard,
  HoverCardContent,
  HoverCardTrigger,
} from "@/components/ui/hover-card";
import { cn } from "@/lib/utils";

const CONFIDENCE_STYLE = {
  high: "text-emerald-300 border-emerald-400/40 bg-emerald-500/5",
  medium: "text-amber-300 border-amber-400/40 bg-amber-500/5",
  low: "text-zinc-400 border-zinc-500/40 bg-zinc-800/40",
} as const;

const CONFIDENCE_RANK: Record<Claim["confidence"], number> = {
  high: 0,
  medium: 1,
  low: 2,
};

const NODE_LABEL_DOT: Record<GraphNode["label"], string> = {
  Entity: "bg-blue-400",
  Officer: "bg-orange-400",
  Intermediary: "bg-violet-400",
  Address: "bg-green-400",
  Other: "bg-zinc-400",
};

const googleUrl = (q: string) =>
  `https://www.google.com/search?q=${encodeURIComponent(q)}`;

const opensanctionsUrl = (id: string) =>
  `https://www.opensanctions.org/entities/${encodeURIComponent(id)}/`;

export function RiskSummaryCard({
  summary,
  nodesById,
  onFollowup,
  onHighlightNodes,
  onClearHighlight,
  onFocusNode,
}: {
  summary: RiskSummary;
  /** Lookup so source chips can show the entity name + label, not just the raw id. */
  nodesById?: Map<string, GraphNode>;
  /** Click a follow-up pill -> kick off a new investigation. */
  onFollowup?: (name: string) => void;
  /** Hover a claim -> pulse its source nodes in the graph. */
  onHighlightNodes?: (nodeIds: string[]) => void;
  onClearHighlight?: () => void;
  /** Click a source chip -> persistently highlight that one node in the graph. */
  onFocusNode?: (nodeId: string) => void;
}) {
  const sortedClaims = useMemo(
    () =>
      [...summary.claims]
        .map((c, i) => ({ c, i }))
        .sort((a, b) => {
          const r =
            CONFIDENCE_RANK[a.c.confidence] - CONFIDENCE_RANK[b.c.confidence];
          return r !== 0 ? r : a.i - b.i;
        })
        .map(({ c }) => c),
    [summary.claims]
  );

  return (
    <div className="rounded-lg border border-zinc-700 bg-zinc-900/60 p-4 text-sm shadow-lg backdrop-blur">
      <header className="mb-3 flex items-start justify-between gap-3">
        <div>
          <div className="text-xs uppercase tracking-wider text-zinc-500">
            Risk Summary
          </div>
          <h3 className="flex items-center gap-2 text-lg font-semibold text-zinc-100">
            {summary.entity_name}
            <a
              href={googleUrl(summary.entity_name)}
              target="_blank"
              rel="noopener noreferrer"
              title="Search Google for this entity"
              className="text-zinc-500 transition hover:text-zinc-200"
            >
              <Search className="h-3.5 w-3.5" />
            </a>
          </h3>
        </div>
        <span
          className={
            "rounded px-2 py-0.5 text-xs font-medium " +
            (summary.found
              ? "border border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
              : "border border-zinc-600 bg-zinc-800/60 text-zinc-400")
          }
        >
          {summary.found ? "Identified" : "Not found"}
        </span>
      </header>

      {summary.risk_signals.length > 0 && (
        <div className="mb-3 flex flex-wrap gap-1.5">
          {summary.risk_signals.map((s) => (
            <RiskSignalBadge key={s} signal={s} />
          ))}
        </div>
      )}

      <Markdown className="prose prose-sm prose-invert mb-4 max-w-none leading-relaxed text-zinc-300">
        {summary.investigation_summary}
      </Markdown>

      {sortedClaims.length > 0 && (
        <section className="mb-4">
          <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
            Claims ({sortedClaims.length})
            <span className="ml-2 font-normal normal-case tracking-normal text-zinc-600">
              hover a claim to highlight in graph · click a source chip to focus
            </span>
          </h4>
          <ul className="space-y-2">
            {sortedClaims.map((c, i) => {
              const nodeIds = c.source_refs
                .map((r) => r.node_id)
                .filter((id): id is string => !!id);
              return (
                <li
                  key={i}
                  onMouseEnter={() =>
                    nodeIds.length && onHighlightNodes?.(nodeIds)
                  }
                  onMouseLeave={() => onClearHighlight?.()}
                  className="cursor-default rounded-md border border-zinc-800 bg-zinc-950/40 p-2.5 transition hover:border-zinc-600 hover:bg-zinc-950/80"
                >
                  <div className="mb-1.5 flex items-center gap-2">
                    <span
                      className={
                        "rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide " +
                        CONFIDENCE_STYLE[c.confidence]
                      }
                    >
                      {c.confidence}
                    </span>
                  </div>
                  <Markdown className="prose prose-sm prose-invert max-w-none leading-relaxed text-zinc-200">
                    {c.text}
                  </Markdown>
                  {c.source_refs.length > 0 && (
                    <div className="mt-1.5 flex flex-wrap items-center gap-1">
                      <span className="text-[10px] text-zinc-600">Sources:</span>
                      {c.source_refs.map((ref, j) => (
                        <ClaimSourceChip
                          key={j}
                          index={j + 1}
                          ref_={ref}
                          node={
                            ref.node_id ? nodesById?.get(ref.node_id) : undefined
                          }
                          onFocusNode={onFocusNode}
                        />
                      ))}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      )}

      {summary.sanctions_hits.length > 0 && (
        <section className="mb-4">
          <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
            Sanctions hits
          </h4>
          <ul className="space-y-1.5">
            {summary.sanctions_hits.map((h, i) => (
              <li
                key={i}
                className="rounded-md border border-red-900/40 bg-red-950/20 p-2 text-xs"
              >
                <div className="flex items-baseline justify-between gap-2">
                  <span className="flex items-center gap-1.5 font-medium text-red-200">
                    {h.matched_name}
                    <a
                      href={googleUrl(`${h.matched_name} sanctions`)}
                      target="_blank"
                      rel="noopener noreferrer"
                      title="Search Google for sanctions info"
                      className="text-red-400/60 transition hover:text-red-200"
                    >
                      <ExternalLink className="h-3 w-3" />
                    </a>
                  </span>
                  <span className="text-[10px] tabular-nums text-red-400">
                    score {h.score.toFixed(2)}
                  </span>
                </div>
                <div className="mt-1 flex flex-wrap gap-1">
                  {h.lists.slice(0, 8).map((l) => (
                    <span
                      key={l}
                      className="rounded bg-red-950/60 px-1.5 py-0.5 text-[10px] text-red-300"
                    >
                      {l}
                    </span>
                  ))}
                  {h.lists.length > 8 && (
                    <span className="text-[10px] text-red-500">
                      +{h.lists.length - 8} more
                    </span>
                  )}
                </div>
                {(h.position?.length ||
                  h.countries?.length ||
                  h.address?.length ||
                  h.birth_date?.length) && (
                  <dl className="mt-1.5 space-y-0.5 border-t border-red-900/30 pt-1.5 text-[10px] text-red-300/80">
                    {h.position?.length ? (
                      <div className="flex gap-1.5">
                        <dt className="shrink-0 font-semibold uppercase tracking-wider text-red-400/60">
                          Position
                        </dt>
                        <dd className="truncate" title={h.position.join("; ")}>
                          {h.position.join("; ")}
                        </dd>
                      </div>
                    ) : null}
                    {h.countries?.length ? (
                      <div className="flex gap-1.5">
                        <dt className="shrink-0 font-semibold uppercase tracking-wider text-red-400/60">
                          Country
                        </dt>
                        <dd className="uppercase">{h.countries.join(", ")}</dd>
                      </div>
                    ) : null}
                    {h.address?.length ? (
                      <div className="flex gap-1.5">
                        <dt className="shrink-0 font-semibold uppercase tracking-wider text-red-400/60">
                          Address
                        </dt>
                        <dd className="truncate" title={h.address.join("; ")}>
                          {h.address.join("; ")}
                        </dd>
                      </div>
                    ) : null}
                    {h.birth_date?.length ? (
                      <div className="flex gap-1.5">
                        <dt className="shrink-0 font-semibold uppercase tracking-wider text-red-400/60">
                          Born
                        </dt>
                        <dd>{h.birth_date.join(", ")}</dd>
                      </div>
                    ) : null}
                  </dl>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {summary.sayari_risk_factors && summary.sayari_risk_factors.length > 0 && (
        <SayariRiskFactors
          factors={summary.sayari_risk_factors}
          onHighlightNodes={onHighlightNodes}
          onClearHighlight={onClearHighlight}
        />
      )}

      {summary.clarifying_questions && summary.clarifying_questions.length > 0 && (
        <section className="mb-4">
          <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-zinc-500">
            <HelpCircle className="h-3 w-3" /> Open questions
          </h4>
          <ul className="space-y-1">
            {summary.clarifying_questions.map((q, i) => (
              <li key={i} className="text-xs leading-relaxed text-zinc-400">
                {q}
              </li>
            ))}
          </ul>
        </section>
      )}

      {summary.suggested_followups && summary.suggested_followups.length > 0 && (
        <section className="mb-3">
          <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-zinc-500">
            <RotateCcw className="h-3 w-3" /> Follow-up investigations
          </h4>
          <div className="flex flex-wrap gap-1.5">
            {summary.suggested_followups.map((s, i) => (
              <PromptSuggestion
                key={i}
                size="sm"
                onClick={() => onFollowup?.(s.name)}
                title={s.reason}
                className="border-sky-500/40 bg-sky-500/10 text-xs text-sky-200 hover:border-sky-400 hover:bg-sky-500/20"
              >
                {s.name}
              </PromptSuggestion>
            ))}
          </div>
        </section>
      )}

      <footer className="mt-3 flex items-center justify-between border-t border-zinc-800 pt-2 text-[10px] text-zinc-500">
        <span>Tools used: {summary.tools_used.join(", ")}</span>
        {summary.entity_id && (
          <span className="font-mono">…{summary.entity_id.slice(-12)}</span>
        )}
      </footer>
    </div>
  );
}

/**
 * Sayari risk factors grouped by severity level (critical > high > elevated >
 * relevant). Each factor with a traversal path is clickable — clicking
 * highlights that ownership/control chain on the graph (the "show your work"
 * moment). psa_* factors are tagged as ER-derived / lower confidence.
 */
function SayariRiskFactors({
  factors,
  onHighlightNodes,
  onClearHighlight,
}: {
  factors: SayariRiskFactor[];
  onHighlightNodes?: (nodeIds: string[]) => void;
  onClearHighlight?: () => void;
}) {
  const groups = useMemo(() => {
    const byLevel = new Map<string, SayariRiskFactor[]>();
    for (const f of factors) {
      const arr = byLevel.get(f.level) ?? [];
      arr.push(f);
      byLevel.set(f.level, arr);
    }
    return Array.from(byLevel.entries()).sort(
      (a, b) => sayariLevelRank(a[0]) - sayariLevelRank(b[0])
    );
  }, [factors]);

  return (
    <section className="mb-4">
      <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-zinc-500">
        <ShieldAlert className="h-3 w-3" /> Sayari risk factors ({factors.length})
        <span className="ml-1 font-normal normal-case tracking-normal text-zinc-600">
          click a factor to trace its path on the graph
        </span>
      </h4>
      <div className="space-y-2.5">
        {groups.map(([level, items]) => {
          const meta = SAYARI_LEVEL_META[level as keyof typeof SAYARI_LEVEL_META];
          return (
            <div key={level}>
              <div className="mb-1 flex items-center gap-1.5">
                <span
                  className={
                    "rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide " +
                    (meta?.className ?? "border-zinc-600 text-zinc-300")
                  }
                >
                  {meta?.label ?? level}
                </span>
                <span className="text-[10px] text-zinc-600">{items.length}</span>
              </div>
              <ul className="space-y-1">
                {items.map((f, i) => {
                  const ids = pathNodeIds(f.path ?? []);
                  const hasPath = ids.length > 0;
                  return (
                    <li key={`${level}-${i}`}>
                      <button
                        type="button"
                        disabled={!hasPath}
                        onClick={() => hasPath && onHighlightNodes?.(ids)}
                        onMouseEnter={() => hasPath && onHighlightNodes?.(ids)}
                        onMouseLeave={() => onClearHighlight?.()}
                        title={
                          hasPath
                            ? "Highlight this factor's ownership/control chain on the graph"
                            : "Direct factor (no traversal path)"
                        }
                        className={cn(
                          "flex w-full items-center gap-1.5 rounded border border-zinc-800 bg-zinc-950/40 px-2 py-1 text-left text-[11px] transition",
                          hasPath
                            ? "cursor-pointer hover:border-sky-500/60 hover:bg-zinc-900"
                            : "cursor-default"
                        )}
                      >
                        {hasPath && (
                          <GitBranch className="h-3 w-3 shrink-0 text-sky-400/70" />
                        )}
                        <span className="min-w-0 flex-1 truncate text-zinc-200">
                          {humanizeRiskFactor(f.name)}
                        </span>
                        {f.psa && (
                          <span
                            className="shrink-0 rounded bg-zinc-800 px-1 py-0.5 text-[9px] uppercase tracking-wide text-zinc-400"
                            title="Entity-resolution derived (Possibly Same As) — lower confidence"
                          >
                            ER-derived
                          </span>
                        )}
                        {typeof f.value === "number" && f.value > 0 && (
                          <span className="shrink-0 text-[10px] tabular-nums text-zinc-500">
                            {f.value} hop{f.value === 1 ? "" : "s"}
                          </span>
                        )}
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>
          );
        })}
      </div>
    </section>
  );
}

/**
 * One source citation chip. Three flavors based on which fields the
 * SourceRef has:
 *  - ICIJ graph node    -> rounded chip with the node's label-dot + name.
 *                          Hover shows full metadata, click highlights node.
 *  - OpenSanctions hit  -> red chip linking to opensanctions.org.
 *  - Bare leak ref      -> grey chip with just the leak name.
 */
function ClaimSourceChip({
  index,
  ref_,
  node,
  onFocusNode,
}: {
  index: number;
  ref_: SourceRef;
  node?: GraphNode;
  onFocusNode?: (nodeId: string) => void;
}) {
  if (ref_.source === "opensanctions" && ref_.sanctions_id) {
    return (
      <a
        href={opensanctionsUrl(ref_.sanctions_id)}
        target="_blank"
        rel="noopener noreferrer"
        title="Open in OpenSanctions"
        className="inline-flex items-center gap-1 rounded-full border border-red-900/50 bg-red-950/30 px-1.5 py-0.5 text-[10px] text-red-300 transition hover:border-red-700 hover:bg-red-950/60 hover:text-red-100"
      >
        <span className="font-medium tabular-nums">[{index}]</span>
        <span className="max-w-[140px] truncate">OpenSanctions</span>
        <ExternalLink className="h-2.5 w-2.5" />
      </a>
    );
  }

  if (ref_.source === "sayari") {
    const label = ref_.risk_factor
      ? humanizeRiskFactor(ref_.risk_factor)
      : "Sayari";
    return (
      <span
        title={ref_.sayari_entity_id ? `Sayari entity ${ref_.sayari_entity_id}` : "Sayari"}
        className="inline-flex items-center gap-1 rounded-full border border-teal-700/50 bg-teal-950/30 px-1.5 py-0.5 text-[10px] text-teal-200"
      >
        <span className="font-medium tabular-nums">[{index}]</span>
        <span className="max-w-[160px] truncate">{label}</span>
      </span>
    );
  }

  if (ref_.source === "icij" && ref_.node_id) {
    const dotClass = node ? NODE_LABEL_DOT[node.label] : "bg-zinc-500";
    const displayName = node?.name ?? `node ${ref_.node_id.slice(-6)}`;
    return (
      <HoverCard>
        <HoverCardTrigger
          render={
            <button
              onClick={() => ref_.node_id && onFocusNode?.(ref_.node_id)}
              className={cn(
                "inline-flex items-center gap-1 rounded-full border border-zinc-700 bg-zinc-900 px-1.5 py-0.5 text-[10px] text-zinc-300",
                "transition hover:border-sky-500/60 hover:bg-zinc-800 hover:text-sky-200"
              )}
            />
          }
        >
          <span className="font-medium tabular-nums">[{index}]</span>
          <span className={cn("h-1.5 w-1.5 rounded-full", dotClass)} />
          <span className="max-w-[140px] truncate">{displayName}</span>
        </HoverCardTrigger>
        <HoverCardContent className="w-72 border-zinc-700 bg-zinc-900 p-3 text-xs text-zinc-200">
          <div className="mb-1 flex items-center gap-1.5">
            <span className={cn("h-2 w-2 rounded-full", dotClass)} />
            <span className="text-[10px] uppercase tracking-wide text-zinc-500">
              {node?.label ?? "ICIJ node"}
            </span>
            {ref_.leak && (
              <span className="ml-auto text-[10px] text-zinc-500">
                {ref_.leak}
              </span>
            )}
          </div>
          <div className="break-words font-medium text-zinc-100">
            {displayName}
          </div>
          {node?.source && !ref_.leak && (
            <div className="mt-1 text-[10px] text-zinc-500">
              source: {node.source}
            </div>
          )}
          <div className="mt-2 flex items-center gap-1 text-[10px] text-sky-300">
            <FileText className="h-3 w-3" /> click to focus in graph
          </div>
        </HoverCardContent>
      </HoverCard>
    );
  }

  // Fallback: leak-only ref.
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-zinc-800 bg-zinc-950 px-1.5 py-0.5 text-[10px] text-zinc-500">
      <span className="tabular-nums">[{index}]</span>
      <span>{ref_.leak ?? ref_.source}</span>
    </span>
  );
}
