// Small pill rendering one risk signal with severity-tiered color and a tooltip description.
import { RISK_SIGNAL_META, type RiskSignal } from "@/lib/types";
import { cn } from "@/lib/utils";

/* Risk severity is one of the two signals allowed to carry color (spec §2).
 * Light-theme fills: red = sanctioned-grade, amber = strong pattern,
 * yellow = softer pattern. */
const TIER_STYLES = {
  red: "border-red-300 bg-red-50 text-red-700",
  amber: "border-orange-300 bg-orange-50 text-orange-700",
  yellow: "border-amber-300 bg-amber-50 text-amber-700",
} as const;

export function RiskSignalBadge({ signal, className }: { signal: RiskSignal; className?: string }) {
  const meta = RISK_SIGNAL_META[signal];
  return (
    <span
      title={meta.description}
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 font-mono text-[10px] font-medium uppercase tracking-[0.1em]",
        TIER_STYLES[meta.tier],
        className
      )}
    >
      {meta.label}
    </span>
  );
}
