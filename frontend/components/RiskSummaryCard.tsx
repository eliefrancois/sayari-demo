import type { RiskSummary } from "@/lib/types";
import { RiskSignalBadge } from "./RiskSignalBadge";

const CONFIDENCE_STYLE = {
  high: "text-emerald-300 border-emerald-400/40",
  medium: "text-amber-300 border-amber-400/40",
  low: "text-zinc-400 border-zinc-500/40",
} as const;

export function RiskSummaryCard({ summary }: { summary: RiskSummary }) {
  return (
    <div className="rounded-lg border border-zinc-700 bg-zinc-900/60 p-4 text-sm shadow-lg backdrop-blur">
      <header className="mb-3 flex items-start justify-between gap-3">
        <div>
          <div className="text-xs uppercase tracking-wider text-zinc-500">
            Risk Summary
          </div>
          <h3 className="text-lg font-semibold text-zinc-100">{summary.entity_name}</h3>
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

      <p className="mb-4 text-zinc-300 leading-relaxed">{summary.investigation_summary}</p>

      {summary.claims.length > 0 && (
        <section className="mb-4">
          <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
            Claims ({summary.claims.length})
          </h4>
          <ul className="space-y-2">
            {summary.claims.map((c, i) => (
              <li key={i} className="rounded-md border border-zinc-800 bg-zinc-950/40 p-2.5">
                <div className="mb-1 flex items-center gap-2">
                  <span
                    className={
                      "rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide " +
                      CONFIDENCE_STYLE[c.confidence]
                    }
                  >
                    {c.confidence}
                  </span>
                  <span className="text-[10px] text-zinc-500">
                    {c.source_refs.length} source{c.source_refs.length === 1 ? "" : "s"}
                  </span>
                </div>
                <p className="text-zinc-200 leading-relaxed">{c.text}</p>
              </li>
            ))}
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
              <li key={i} className="rounded-md border border-red-900/40 bg-red-950/20 p-2 text-xs">
                <div className="flex items-baseline justify-between gap-2">
                  <span className="font-medium text-red-200">{h.matched_name}</span>
                  <span className="text-[10px] tabular-nums text-red-400">
                    score {h.score.toFixed(2)}
                  </span>
                </div>
                <div className="mt-1 flex flex-wrap gap-1">
                  {h.lists.slice(0, 8).map((l) => (
                    <span key={l} className="rounded bg-red-950/60 px-1.5 py-0.5 text-[10px] text-red-300">
                      {l}
                    </span>
                  ))}
                  {h.lists.length > 8 && (
                    <span className="text-[10px] text-red-500">+{h.lists.length - 8} more</span>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      <footer className="mt-3 flex items-center justify-between border-t border-zinc-800 pt-2 text-[10px] text-zinc-500">
        <span>Tools used: {summary.tools_used.join(", ")}</span>
        {summary.entity_id && <span className="font-mono">…{summary.entity_id.slice(-12)}</span>}
      </footer>
    </div>
  );
}
