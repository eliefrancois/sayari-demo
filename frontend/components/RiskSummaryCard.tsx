"use client";

import { useMemo } from "react";
import { motion } from "framer-motion";
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
  SOURCE_SYSTEM_META,
  sayariLevelRank,
  sourceSystemOf,
  pathNodeIds,
  humanizeRiskFactor,
} from "@/lib/types";
import { RiskSignalBadge } from "./RiskSignalBadge";
import { Markdown } from "@/components/ui/markdown";
import {
  HoverCard,
  HoverCardContent,
  HoverCardTrigger,
} from "@/components/ui/hover-card";
import { cn } from "@/lib/utils";

/* Confidence is grayscale — color is reserved for risk severity and source
 * provenance (spec §2). Intensity encodes the band instead. */
const CONFIDENCE_STYLE = {
  high: "text-foreground border-foreground/40 bg-muted",
  medium: "text-muted-foreground border-border bg-muted/60",
  low: "text-muted-foreground/70 border-border bg-transparent",
} as const;

const CONFIDENCE_RANK: Record<Claim["confidence"], number> = {
  high: 0,
  medium: 1,
  low: 2,
};

const googleUrl = (q: string) =>
  `https://www.google.com/search?q=${encodeURIComponent(q)}`;

const opensanctionsUrl = (id: string) =>
  `https://www.opensanctions.org/entities/${encodeURIComponent(id)}/`;

/**
 * The formal investigation memo. Per the spec this stays a visually DISTINCT
 * card vs normal conversation turns: inverted corner badge, heavier header,
 * same lmcanvas card shell (white, hairline border, rounded-[10px], shadow).
 */
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
    <motion.div
      initial={{ scale: 0.96, opacity: 0, y: -6 }}
      animate={{ scale: 1, opacity: 1, y: 0 }}
      transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
      style={{ transformOrigin: "top center" }}
      className="relative rounded-[10px] border border-foreground/25 bg-card px-5 pb-4 pt-10 text-sm shadow-sm"
    >
      {/* Corner badge row — inverted badge marks the formal report */}
      <div className="absolute left-4 right-4 top-3 flex min-w-0 items-center gap-2">
        <span className="rounded-md bg-foreground px-1.5 py-0.5 font-mono text-[9px] font-medium uppercase tracking-[0.16em] text-background">
          risk summary
        </span>
        <span
          className={
            "ml-auto rounded-md border px-1.5 py-0.5 font-mono text-[9px] font-medium uppercase tracking-[0.16em] " +
            (summary.found
              ? "border-border bg-muted text-foreground"
              : "border-border bg-transparent text-muted-foreground")
          }
        >
          {summary.found ? "identified" : "not found"}
        </span>
      </div>

      <header className="mb-3">
        <h3 className="flex items-center gap-2 text-[16px] font-semibold leading-snug text-foreground">
          {summary.entity_name}
          <a
            href={googleUrl(summary.entity_name)}
            target="_blank"
            rel="noopener noreferrer"
            title="Search Google for this entity"
            className="text-muted-foreground transition-colors hover:text-foreground"
          >
            <Search className="h-3.5 w-3.5" />
          </a>
        </h3>
      </header>

      {summary.risk_signals.length > 0 && (
        <div className="mb-3 flex flex-wrap gap-1.5">
          {summary.risk_signals.map((s) => (
            <RiskSignalBadge key={s} signal={s} />
          ))}
        </div>
      )}

      <Markdown className="mb-4 text-[12.5px] leading-relaxed text-foreground/90">
        {summary.investigation_summary}
      </Markdown>

      {sortedClaims.length > 0 && (
        <section className="mb-4">
          <h4 className="mb-2 font-mono text-[9px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
            Claims ({sortedClaims.length})
            <span className="ml-2 font-sans font-normal normal-case tracking-normal text-muted-foreground/70">
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
                  className="cursor-default rounded-[8px] border border-border bg-background p-2.5 transition-colors hover:border-foreground/30 hover:bg-muted/50"
                >
                  <div className="mb-1.5 flex items-center gap-2">
                    <span
                      className={
                        "rounded border px-1.5 py-0.5 font-mono text-[9px] font-medium uppercase tracking-[0.12em] " +
                        CONFIDENCE_STYLE[c.confidence]
                      }
                    >
                      {c.confidence}
                    </span>
                  </div>
                  <Markdown className="text-xs leading-relaxed text-foreground/90">
                    {c.text}
                  </Markdown>
                  {c.source_refs.length > 0 && (
                    <div className="mt-1.5 flex flex-wrap items-center gap-1">
                      <span className="font-mono text-[8px] uppercase tracking-[0.14em] text-muted-foreground/70">
                        sources
                      </span>
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
          <h4 className="mb-2 font-mono text-[9px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
            Sanctions hits
          </h4>
          <ul className="space-y-1.5">
            {summary.sanctions_hits.map((h, i) => (
              <li
                key={i}
                className="rounded-[8px] border border-red-300 bg-red-50/60 p-2 text-xs"
              >
                <div className="flex items-baseline justify-between gap-2">
                  <span className="flex items-center gap-1.5 font-medium text-red-800">
                    {h.matched_name}
                    <a
                      href={googleUrl(`${h.matched_name} sanctions`)}
                      target="_blank"
                      rel="noopener noreferrer"
                      title="Search Google for sanctions info"
                      className="text-red-500/70 transition-colors hover:text-red-700"
                    >
                      <ExternalLink className="h-3 w-3" />
                    </a>
                  </span>
                  <span className="font-mono text-[10px] tabular-nums text-red-600">
                    score {h.score.toFixed(2)}
                  </span>
                </div>
                <div className="mt-1 flex flex-wrap gap-1">
                  {h.lists.slice(0, 8).map((l) => (
                    <span
                      key={l}
                      className="rounded bg-red-100 px-1.5 py-0.5 font-mono text-[9px] text-red-700"
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
                  <dl className="mt-1.5 space-y-0.5 border-t border-red-200 pt-1.5 text-[10px] text-red-800/80">
                    {h.position?.length ? (
                      <div className="flex gap-1.5">
                        <dt className="shrink-0 font-mono font-semibold uppercase tracking-[0.1em] text-red-600/70">
                          Position
                        </dt>
                        <dd className="truncate" title={h.position.join("; ")}>
                          {h.position.join("; ")}
                        </dd>
                      </div>
                    ) : null}
                    {h.countries?.length ? (
                      <div className="flex gap-1.5">
                        <dt className="shrink-0 font-mono font-semibold uppercase tracking-[0.1em] text-red-600/70">
                          Country
                        </dt>
                        <dd className="uppercase">{h.countries.join(", ")}</dd>
                      </div>
                    ) : null}
                    {h.address?.length ? (
                      <div className="flex gap-1.5">
                        <dt className="shrink-0 font-mono font-semibold uppercase tracking-[0.1em] text-red-600/70">
                          Address
                        </dt>
                        <dd className="truncate" title={h.address.join("; ")}>
                          {h.address.join("; ")}
                        </dd>
                      </div>
                    ) : null}
                    {h.birth_date?.length ? (
                      <div className="flex gap-1.5">
                        <dt className="shrink-0 font-mono font-semibold uppercase tracking-[0.1em] text-red-600/70">
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
          <h4 className="mb-2 flex items-center gap-1.5 font-mono text-[9px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
            <HelpCircle className="h-3 w-3" /> Open questions
          </h4>
          <ul className="space-y-1">
            {summary.clarifying_questions.map((q, i) => (
              <li key={i} className="text-xs leading-relaxed text-muted-foreground">
                {q}
              </li>
            ))}
          </ul>
        </section>
      )}

      {summary.suggested_followups && summary.suggested_followups.length > 0 && (
        <section className="mb-3">
          <h4 className="mb-2 flex items-center gap-1.5 font-mono text-[9px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
            <RotateCcw className="h-3 w-3" /> Follow-up investigations
          </h4>
          <div className="flex flex-wrap gap-1.5">
            {summary.suggested_followups.map((s, i) => (
              <motion.button
                key={i}
                type="button"
                whileTap={{ scale: 0.97 }}
                whileHover={{ y: -1 }}
                onClick={() => onFollowup?.(s.name)}
                title={s.reason}
                className="inline-flex cursor-pointer items-center gap-1.5 rounded-md border border-border bg-background px-2 py-1 font-mono text-[10px] uppercase tracking-[0.16em] text-foreground/80 transition-colors hover:border-foreground/40 hover:text-foreground focus:outline-none"
              >
                {s.name}
              </motion.button>
            ))}
          </div>
        </section>
      )}

      <footer className="mt-3 flex items-center justify-between border-t border-border pt-2 font-mono text-[9px] uppercase tracking-[0.1em] text-muted-foreground">
        <span className="normal-case">Tools used: {summary.tools_used.join(", ")}</span>
        {summary.entity_id && (
          <span className="normal-case">…{summary.entity_id.slice(-12)}</span>
        )}
      </footer>
    </motion.div>
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
      <h4 className="mb-2 flex items-center gap-1.5 font-mono text-[9px] font-semibold uppercase tracking-[0.18em] text-muted-foreground">
        <ShieldAlert className="h-3 w-3" /> Sayari risk factors ({factors.length})
        <span className="ml-1 font-sans font-normal normal-case tracking-normal text-muted-foreground/70">
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
                    "rounded border px-1.5 py-0.5 font-mono text-[9px] font-medium uppercase tracking-[0.12em] " +
                    (meta?.className ?? "border-border text-muted-foreground")
                  }
                >
                  {meta?.label ?? level}
                </span>
                <span className="text-[10px] text-muted-foreground/70">
                  {items.length}
                </span>
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
                          "flex w-full items-center gap-1.5 rounded-[8px] border border-border bg-background px-2 py-1 text-left text-[11px] transition-colors",
                          hasPath
                            ? "cursor-pointer hover:border-foreground/40 hover:bg-muted/60"
                            : "cursor-default"
                        )}
                      >
                        {hasPath && (
                          <GitBranch className="h-3 w-3 shrink-0 text-muted-foreground" />
                        )}
                        <span className="min-w-0 flex-1 truncate text-foreground/90">
                          {humanizeRiskFactor(f.name)}
                        </span>
                        {f.psa && (
                          <span
                            className="shrink-0 rounded bg-muted px-1 py-0.5 font-mono text-[8px] uppercase tracking-[0.1em] text-muted-foreground"
                            title="Entity-resolution derived (Possibly Same As) — lower confidence"
                          >
                            ER-derived
                          </span>
                        )}
                        {typeof f.value === "number" && f.value > 0 && (
                          <span className="shrink-0 font-mono text-[9px] tabular-nums text-muted-foreground">
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
 *  - ICIJ graph node    -> chip with an ICIJ (magenta) source dot + name.
 *                          Hover shows full metadata, click highlights node.
 *  - OpenSanctions hit  -> teal-dotted chip linking to opensanctions.org.
 *  - Sayari factor      -> indigo-dotted chip.
 *  - Bare leak ref      -> grey chip with just the leak name.
 * Dot color = source system (the spec's ring/dot signal).
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
        className="inline-flex items-center gap-1 rounded-full border border-border bg-card px-1.5 py-0.5 text-[10px] text-muted-foreground transition-colors hover:border-foreground/40 hover:text-foreground"
      >
        <span className="font-medium tabular-nums">[{index}]</span>
        <span
          className="h-1.5 w-1.5 rounded-full border bg-card"
          style={{ borderColor: SOURCE_SYSTEM_META.sanctions.color }}
        />
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
        className="inline-flex items-center gap-1 rounded-full border border-border bg-card px-1.5 py-0.5 text-[10px] text-muted-foreground"
      >
        <span className="font-medium tabular-nums">[{index}]</span>
        <span
          className="h-1.5 w-1.5 rounded-full border bg-card"
          style={{ borderColor: SOURCE_SYSTEM_META.sayari.color }}
        />
        <span className="max-w-[160px] truncate">{label}</span>
      </span>
    );
  }

  if (ref_.source === "icij" && ref_.node_id) {
    const dotColor = node
      ? SOURCE_SYSTEM_META[sourceSystemOf(node.source_system)].color
      : SOURCE_SYSTEM_META.icij.color;
    const displayName = node?.name ?? `node ${ref_.node_id.slice(-6)}`;
    return (
      <HoverCard>
        <HoverCardTrigger
          render={
            <button
              onClick={() => ref_.node_id && onFocusNode?.(ref_.node_id)}
              className={cn(
                "inline-flex items-center gap-1 rounded-full border border-border bg-card px-1.5 py-0.5 text-[10px] text-muted-foreground",
                "cursor-pointer transition-colors hover:border-foreground/40 hover:text-foreground"
              )}
            />
          }
        >
          <span className="font-medium tabular-nums">[{index}]</span>
          <span
            className="h-1.5 w-1.5 rounded-full border bg-card"
            style={{ borderColor: dotColor }}
          />
          <span className="max-w-[140px] truncate">{displayName}</span>
        </HoverCardTrigger>
        <HoverCardContent className="w-72 border-border bg-card p-3 text-xs text-foreground shadow-md">
          <div className="mb-1 flex items-center gap-1.5">
            <span
              className="h-2 w-2 rounded-full border bg-card"
              style={{ borderColor: dotColor }}
            />
            <span className="font-mono text-[9px] uppercase tracking-[0.14em] text-muted-foreground">
              {node?.label ?? "ICIJ node"}
            </span>
            {ref_.leak && (
              <span className="ml-auto text-[10px] text-muted-foreground">
                {ref_.leak}
              </span>
            )}
          </div>
          <div className="break-words font-medium text-foreground">
            {displayName}
          </div>
          {node?.source && !ref_.leak && (
            <div className="mt-1 text-[10px] text-muted-foreground">
              source: {node.source}
            </div>
          )}
          <div className="mt-2 flex items-center gap-1 font-mono text-[9px] uppercase tracking-[0.12em] text-muted-foreground">
            <FileText className="h-3 w-3" /> click to focus in graph
          </div>
        </HoverCardContent>
      </HoverCard>
    );
  }

  // Fallback: leak-only ref.
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-border bg-background px-1.5 py-0.5 text-[10px] text-muted-foreground">
      <span className="tabular-nums">[{index}]</span>
      <span>{ref_.leak ?? ref_.source}</span>
    </span>
  );
}
