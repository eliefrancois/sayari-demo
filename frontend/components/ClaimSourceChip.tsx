// Inline source-citation chip rendering a claim's provenance per source system.
"use client";

import { ExternalLink, FileText } from "lucide-react";
import type { GraphNode, SourceRef } from "@/lib/types";
import { SOURCE_SYSTEM_META, sourceSystemOf, humanizeRiskFactor } from "@/lib/types";
import {
  HoverCard,
  HoverCardContent,
  HoverCardTrigger,
} from "@/components/ui/hover-card";
import { cn } from "@/lib/utils";

const opensanctionsUrl = (id: string) =>
  `https://www.opensanctions.org/entities/${encodeURIComponent(id)}/`;

/**
 * One source citation chip, shared by the risk-summary card and the
 * conversational answer card so inline provenance reads the same everywhere.
 * Four flavors, keyed off which SourceRef fields are populated:
 *  - OpenSanctions hit -> teal-dotted chip linking to opensanctions.org.
 *  - Sayari factor     -> indigo-dotted chip (risk-factor name when present).
 *  - ICIJ graph node   -> magenta-dotted chip; hover shows metadata, click focuses.
 *  - Bare leak ref     -> grey chip with the leak name.
 * Dot color = source system (the spec's ring/dot signal). The `[index]`
 * superscript keeps it compact; never renders the literal word "source".
 */
export function ClaimSourceChip({
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
        title={`OpenSanctions · ${ref_.sanctions_id}`}
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
    const label = ref_.risk_factor ? humanizeRiskFactor(ref_.risk_factor) : "Sayari";
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
              onClick={(e) => {
                e.stopPropagation();
                if (ref_.node_id) onFocusNode?.(ref_.node_id);
              }}
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
              <span className="ml-auto text-[10px] text-muted-foreground">{ref_.leak}</span>
            )}
          </div>
          <div className="break-words font-medium text-foreground">{displayName}</div>
          {node?.source && !ref_.leak && (
            <div className="mt-1 text-[10px] text-muted-foreground">source: {node.source}</div>
          )}
          <div className="mt-2 flex items-center gap-1 font-mono text-[9px] uppercase tracking-[0.12em] text-muted-foreground">
            <FileText className="h-3 w-3" /> click to focus in graph
          </div>
        </HoverCardContent>
      </HoverCard>
    );
  }

  // Fallback: leak-only ref (still meaningful: the leak name, never "source").
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-border bg-background px-1.5 py-0.5 text-[10px] text-muted-foreground">
      <span className="tabular-nums">[{index}]</span>
      <span className="max-w-[140px] truncate">{ref_.leak ?? "OpenSanctions"}</span>
    </span>
  );
}
