import { RISK_SIGNAL_META, type RiskSignal } from "@/lib/types";
import { cn } from "@/lib/utils";

const TIER_STYLES = {
  red: "border-red-500/40 bg-red-500/15 text-red-200",
  amber: "border-amber-500/40 bg-amber-500/15 text-amber-200",
  yellow: "border-yellow-500/40 bg-yellow-500/15 text-yellow-200",
} as const;

export function RiskSignalBadge({ signal, className }: { signal: RiskSignal; className?: string }) {
  const meta = RISK_SIGNAL_META[signal];
  return (
    <span
      title={meta.description}
      className={cn(
        "inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium",
        TIER_STYLES[meta.tier],
        className
      )}
    >
      {meta.label}
    </span>
  );
}
