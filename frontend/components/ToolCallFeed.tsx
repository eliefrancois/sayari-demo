"use client";

import { useEffect, useRef } from "react";
import type { InvestigationState, ToolCallEntry } from "@/lib/investigation-store";
import { Search, Network, Users, MapPin, GitMerge, ShieldAlert, Check, Loader2 } from "lucide-react";

const TOOL_ICONS: Record<string, React.ComponentType<{ className?: string }>> = {
  search_entity: Search,
  get_relationships: Network,
  get_officers: Users,
  find_address_connections: MapPin,
  find_er_links: GitMerge,
  check_sanctions: ShieldAlert,
};

function ToolCallRow({ call }: { call: ToolCallEntry }) {
  const Icon = TOOL_ICONS[call.tool] || Search;
  const elapsed = call.startedAt ? ((Date.now() - call.startedAt) / 1000).toFixed(1) : "—";
  const isSanctionsHit = call.tool === "check_sanctions" && call.resultSummary?.includes("STRONG MATCH");

  return (
    <li className="rounded-md border border-zinc-800 bg-zinc-950/40 p-2.5 text-xs">
      <div className="flex items-start gap-2">
        <div
          className={
            "mt-0.5 flex size-6 shrink-0 items-center justify-center rounded " +
            (isSanctionsHit
              ? "bg-red-900/40 text-red-300"
              : call.hasResult
              ? "bg-emerald-900/30 text-emerald-300"
              : "bg-zinc-800 text-zinc-400")
          }
        >
          <Icon className="size-3.5" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline justify-between gap-2">
            <span className="font-mono text-zinc-200">{call.tool}</span>
            {call.hasResult ? (
              <Check className="size-3 text-emerald-400" />
            ) : (
              <Loader2 className="size-3 animate-spin text-zinc-500" />
            )}
          </div>
          {Object.keys(call.args).length > 0 && (
            <div className="mt-0.5 truncate font-mono text-[10px] text-zinc-500">
              {Object.entries(call.args)
                .map(([k, v]) => `${k}=${String(v).slice(0, 40)}`)
                .join(", ")}
            </div>
          )}
          {call.resultSummary && (
            <div
              className={
                "mt-1 text-[11px] " + (isSanctionsHit ? "font-semibold text-red-300" : "text-zinc-400")
              }
            >
              {call.resultSummary}
            </div>
          )}
          {!call.hasResult && (
            <div className="mt-0.5 text-[10px] text-zinc-600">running {elapsed}s</div>
          )}
        </div>
      </div>
    </li>
  );
}

export function ToolCallFeed({ state }: { state: InvestigationState }) {
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // auto-scroll to latest as events stream in
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [state.toolCalls.length, state.thoughts.length]);

  const hasContent = state.toolCalls.length > 0 || state.thoughts.length > 0;

  return (
    <div className="flex h-full flex-col border-l border-zinc-800 bg-zinc-950/40">
      <header className="flex items-center justify-between border-b border-zinc-800 px-3 py-2">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-400">
          Agent activity
        </h2>
        <span className="text-[10px] tabular-nums text-zinc-500">
          {state.toolCalls.length} tool call{state.toolCalls.length === 1 ? "" : "s"}
        </span>
      </header>
      <div ref={scrollRef} className="flex-1 overflow-y-auto p-3">
        {!hasContent && (
          <p className="mt-8 text-center text-xs text-zinc-600">
            Tool calls will stream in here as the agent investigates.
          </p>
        )}
        <ul className="space-y-1.5">
          {state.toolCalls.map((c) => (
            <ToolCallRow key={c.callId} call={c} />
          ))}
        </ul>
      </div>
    </div>
  );
}
