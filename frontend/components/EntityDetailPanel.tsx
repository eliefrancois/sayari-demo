// Right-hand slide-over showing one entity's registry record, risk flags, relationships, and sources.
"use client";

import { useEffect, useRef } from "react";
import { motion } from "framer-motion";
import { Crosshair, X } from "lucide-react";
import type { GraphNode, RegistryEntity } from "@/lib/types";
import { SOURCE_SYSTEM_META, sourceSystemOf, type SourceSystem } from "@/lib/types";

export interface EntityRelationship {
  type: string;
  direction: "out" | "in";
  otherId: string;
  otherName: string;
}

/**
 * Map a SourceRef / registry `source` string to its display source system.
 * OpenSanctions is spelled several ways across the stack ("opensanctions",
 * "sanctions", "check_sanctions", "check_watchlist"); Sayari covers the
 * resolved/search/profile/ownership buckets. Returns null for an empty source.
 */
function classifyRefSource(src?: string | null): SourceSystem | null {
  const s = (src ?? "").trim().toLowerCase();
  if (!s) return null;
  if (s === "icij") return "icij";
  if (s === "opensanctions" || s === "sanctions" || s.startsWith("check_")) {
    return "sanctions";
  }
  return "sayari";
}

/**
 * Friendlier label for the registry `confidence` field. The backend stores the
 * coarse provenance tag "tool_output" (the finding came from a tool call, not a
 * model guess); show that as plain language rather than the raw token.
 */
function confidenceLabel(confidence: string): string {
  if (confidence === "tool_output") return "Tool-verified";
  return confidence.charAt(0).toUpperCase() + confidence.slice(1);
}

/**
 * Right-hand entity detail slide-over (item: clickable entities). Opens when a
 * bolded entity name in answer/summary markdown is clicked; shows the entity's
 * registry record (risk + sanctions), its graph relationships, and source
 * attribution. Closes on X / Escape / outside click.
 */
export function EntityDetailPanel({
  name,
  node,
  registryEntry,
  relationships,
  onClose,
  onFocusNode,
  onOpenEntity,
}: {
  name: string;
  node: GraphNode | null;
  registryEntry: RegistryEntity | null;
  relationships: EntityRelationship[];
  onClose: () => void;
  onFocusNode: (nodeId: string) => void;
  onOpenEntity: (name: string) => void;
}) {
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    const onMouseDown = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) onClose();
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onMouseDown);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onMouseDown);
    };
  }, [onClose]);

  const entityType = node?.label ?? registryEntry?.type ?? null;

  // The distinct source SYSTEMS backing this entity, derived from real
  // provenance (the graph node + every source_ref + the registry bucket), not a
  // single fallback. This is what surfaces cross-source corroboration honestly:
  // "ICIJ Leaks" shows ONLY when an icij/leak ref actually exists, instead of
  // the old code defaulting every unrecognized source (e.g. a check_sanctions
  // hit) to ICIJ.
  const sourceSystems: SourceSystem[] = (() => {
    const set = new Set<SourceSystem>();
    if (node) set.add(sourceSystemOf(node.source_system));
    for (const r of registryEntry?.source_refs ?? []) {
      const sys = classifyRefSource(r.source);
      if (sys) set.add(sys);
      if (r.leak) set.add("icij");
    }
    const fromBucket = classifyRefSource(registryEntry?.source);
    if (fromBucket) set.add(fromBucket);
    if (registryEntry?.type === "sanctions_entity") set.add("sanctions");
    if (set.size === 0) set.add(node ? sourceSystemOf(node.source_system) : "sayari");
    // Stable, source-ring order: Sayari, OpenSanctions, ICIJ.
    return (["sayari", "sanctions", "icij"] as const).filter((s) => set.has(s));
  })();
  // Primary system drives the single-badge fallbacks (node-only Sources block).
  const primarySourceSystem: SourceSystem = sourceSystems[0] ?? "sayari";

  const sanctioned = Boolean(registryEntry?.sanctioned);
  const isSdn = Boolean(registryEntry?.is_sdn);
  const pep = Boolean(registryEntry?.pep);
  const lists = registryEntry?.sanctions_lists ?? [];
  const countries = registryEntry?.countries ?? [];
  const sourceRefs = registryEntry?.source_refs ?? [];

  // Risk flags from graph node properties as a fallback when the entity isn't
  // in the registry (e.g. fresh node mid-turn before the registry refresh).
  const nodeSanctioned = Boolean(node?.properties?.sanctioned);

  return (
    <motion.div
      ref={panelRef}
      initial={{ x: 24, opacity: 0 }}
      animate={{ x: 0, opacity: 1 }}
      transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
      className="absolute bottom-3 right-3 top-3 z-30 flex w-[300px] flex-col overflow-hidden rounded-[10px] border border-border bg-card shadow-lg"
    >
      {/* header */}
      <div className="flex items-start justify-between gap-2 border-b border-border px-3.5 py-3">
        <div className="min-w-0">
          <div className="font-mono text-[9px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
            Entity detail
          </div>
          <div className="mt-1 break-words text-[14px] font-semibold leading-tight text-foreground">
            {name}
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            {entityType && (
              <span className="rounded-md border border-border bg-muted px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-[0.12em] text-muted-foreground">
                {entityType}
              </span>
            )}
            {sourceSystems.map((sys) => (
              <span
                key={sys}
                className="rounded-md border bg-card px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-[0.12em] text-muted-foreground"
                style={{ borderColor: SOURCE_SYSTEM_META[sys].color }}
              >
                {SOURCE_SYSTEM_META[sys].label}
              </span>
            ))}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {node && (
            <button
              type="button"
              onClick={() => onFocusNode(node.id)}
              title="Center this entity on the graph"
              className="cursor-pointer rounded-md p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            >
              <Crosshair className="h-3.5 w-3.5" />
            </button>
          )}
          <button
            type="button"
            onClick={onClose}
            title="Close (Esc)"
            className="cursor-pointer rounded-md p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-3.5 py-3">
        {/* risk */}
        {(sanctioned || nodeSanctioned || isSdn || pep || lists.length > 0) && (
          <section>
            <SectionLabel>Risk &amp; sanctions</SectionLabel>
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {(sanctioned || nodeSanctioned) && <RiskBadge>Sanctioned</RiskBadge>}
              {isSdn && <RiskBadge>OFAC SDN</RiskBadge>}
              {pep && (
                <span className="rounded-md border border-amber-300 bg-amber-50 px-1.5 py-0.5 font-mono text-[9px] font-semibold uppercase tracking-[0.12em] text-amber-700">
                  PEP
                </span>
              )}
            </div>
            {lists.length > 0 && (
              <div className="mt-1.5 flex flex-wrap gap-1">
                {lists.map((l) => (
                  <span
                    key={l}
                    className="rounded bg-red-50 px-1.5 py-0.5 font-mono text-[9px] text-red-700"
                  >
                    {l}
                  </span>
                ))}
              </div>
            )}
          </section>
        )}

        {/* registry facts */}
        {(countries.length > 0 ||
          registryEntry?.confidence ||
          registryEntry?.first_seen_turn != null) && (
          <section>
            <SectionLabel>Registry</SectionLabel>
            <dl className="mt-1.5 space-y-1 text-[11px] text-foreground">
              {countries.length > 0 && (
                <Fact label="Countries" value={countries.join(", ")} />
              )}
              {registryEntry?.confidence && (
                <Fact label="Provenance" value={confidenceLabel(registryEntry.confidence)} />
              )}
              {registryEntry?.first_seen_turn != null && (
                <Fact
                  label="First seen"
                  value={`turn ${registryEntry.first_seen_turn + 1}`}
                />
              )}
            </dl>
          </section>
        )}

        {/* relationships on the evidence graph */}
        {relationships.length > 0 && (
          <section>
            <SectionLabel>Relationships ({relationships.length})</SectionLabel>
            <ul className="mt-1.5 space-y-0.5">
              {relationships.map((rel, i) => (
                <li key={`${rel.otherId}-${rel.type}-${i}`}>
                  <button
                    type="button"
                    onClick={() => {
                      onFocusNode(rel.otherId);
                      onOpenEntity(rel.otherName);
                    }}
                    title={`Show ${rel.otherName} on the graph`}
                    className="flex w-full cursor-pointer items-baseline gap-1.5 rounded-md px-1.5 py-1 text-left transition-colors hover:bg-muted"
                  >
                    <span className="shrink-0 font-mono text-[9px] uppercase tracking-[0.1em] text-muted-foreground">
                      {rel.direction === "out" ? "→" : "←"} {rel.type.replace(/_/g, " ")}
                    </span>
                    <span className="min-w-0 truncate text-[11px] font-medium text-foreground">
                      {rel.otherName}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}

        {/* source attribution */}
        {(sourceRefs.length > 0 || node) && (
          <section>
            <SectionLabel>Sources</SectionLabel>
            <div className="mt-1.5 flex flex-wrap gap-1">
              {sourceRefs.map((r, i) => {
                const sys: SourceSystem =
                  r.source === "opensanctions"
                    ? "sanctions"
                    : r.source === "sayari"
                      ? "sayari"
                      : "icij";
                const label =
                  r.leak ??
                  (r.sanctions_id
                    ? `${SOURCE_SYSTEM_META[sys].label} · ${r.sanctions_id}`
                    : SOURCE_SYSTEM_META[sys].label);
                return (
                  <span
                    key={`${label}-${i}`}
                    className="rounded-md border bg-card px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground"
                    style={{ borderColor: SOURCE_SYSTEM_META[sys].color }}
                  >
                    {label}
                  </span>
                );
              })}
              {sourceRefs.length === 0 && node && (
                <span
                  className="rounded-md border bg-card px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground"
                  style={{ borderColor: SOURCE_SYSTEM_META[primarySourceSystem].color }}
                >
                  {typeof node.source === "string" && node.source
                    ? node.source
                    : SOURCE_SYSTEM_META[primarySourceSystem].label}
                </span>
              )}
            </div>
          </section>
        )}

        {!node && !registryEntry && (
          <p className="text-[11px] text-muted-foreground">
            Known entity, but no structured record is available on this client yet.
          </p>
        )}
      </div>
    </motion.div>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="font-mono text-[9px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
      {children}
    </h3>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline gap-2">
      <dt className="w-[72px] shrink-0 font-mono text-[9px] uppercase tracking-[0.12em] text-muted-foreground">
        {label}
      </dt>
      <dd className="min-w-0 break-words">{value}</dd>
    </div>
  );
}

function RiskBadge({ children }: { children: React.ReactNode }) {
  return (
    <span className="rounded-md border border-red-300 bg-red-50 px-1.5 py-0.5 font-mono text-[9px] font-semibold uppercase tracking-[0.12em] text-red-700">
      {children}
    </span>
  );
}
