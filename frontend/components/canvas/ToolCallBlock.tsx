"use client";

/*
 * Collapsible inline tool-call row, ported from local-lmcanvas
 * src/renderer/src/components/Canvas/blocks/ToolUseView.tsx + toolMeta.ts
 * (MIT License, Copyright (c) 2026 Max Lee) and adapted to this app's
 * ToolCallEntry shape (SSE-driven; result is a summary string + metadata,
 * not raw tool output).
 */

import { useState } from "react";
import {
  CheckCircle2,
  ChevronDown,
  Link2,
  Loader2,
  MapPin,
  Network,
  Search,
  Ship,
  ShieldAlert,
  Users,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { ToolCallEntry } from "@/lib/conversation-store";

function getToolIcon(tool: string): LucideIcon {
  switch (tool) {
    case "search_entity":
    case "sayari_search":
      return Search;
    case "get_relationships":
      return Network;
    case "get_officers":
      return Users;
    case "find_address_connections":
      return MapPin;
    case "find_er_links":
      return Link2;
    case "check_sanctions":
      return ShieldAlert;
    case "sayari_trade":
      return Ship;
    default:
      return Wrench;
  }
}

function getToolSummary(args: Record<string, unknown>): string {
  for (const v of Object.values(args)) {
    if (typeof v === "string" && v.length > 0) return truncate(v, 80);
  }
  try {
    const s = JSON.stringify(args);
    return s === "{}" ? "" : truncate(s, 80);
  } catch {
    return "";
  }
}

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n)}…` : s;
}

function prettyJson(v: unknown): string {
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

export function ToolCallBlock({ call }: { call: ToolCallEntry }) {
  const [expanded, setExpanded] = useState(false);
  const Icon = getToolIcon(call.tool);
  const summary = getToolSummary(call.args);
  const running = !call.hasResult;
  // A raw strong sanctions match is a RISK signal, so it may carry the
  // critical-red accent (the only color allowed outside source dots).
  const isStrongHit =
    call.tool === "check_sanctions" && call.resultSummary?.includes("STRONG MATCH");

  return (
    <div
      className={cn(
        "my-1 overflow-hidden rounded-[8px] border",
        isStrongHit ? "border-red-300 bg-red-50/50" : "border-border bg-card"
      )}
    >
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className={cn(
          "flex w-full cursor-pointer items-center gap-1.5 px-2 py-1 text-left transition-colors",
          isStrongHit ? "hover:bg-red-50" : "hover:bg-muted"
        )}
        aria-label={expanded ? "Collapse tool call" : "Expand tool call"}
      >
        <Icon
          size={11}
          className={cn(
            "shrink-0",
            isStrongHit ? "text-red-600" : "text-muted-foreground"
          )}
        />
        <span
          className={cn(
            "shrink-0 text-[10px] font-medium",
            isStrongHit ? "text-red-700" : "text-foreground"
          )}
        >
          {call.tool}
        </span>
        {summary && (
          <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-muted-foreground">
            {summary}
          </span>
        )}
        <div className="ml-auto flex shrink-0 items-center gap-1">
          {isStrongHit && (
            <span className="font-mono text-[8px] font-medium uppercase tracking-wide text-red-600">
              strong match
            </span>
          )}
          {running ? (
            <Loader2 size={11} className="animate-spin text-muted-foreground" />
          ) : (
            <CheckCircle2 size={11} className="text-foreground" />
          )}
          <ChevronDown
            size={11}
            className={cn(
              "text-muted-foreground transition-transform",
              expanded && "rotate-180"
            )}
          />
        </div>
      </button>
      {expanded && (
        <div className="border-t border-border px-2 py-1.5">
          <SectionLabel>input</SectionLabel>
          <pre className="mt-0.5 overflow-x-auto whitespace-pre-wrap break-words rounded-[6px] bg-muted px-2 py-1 font-mono text-[9.5px] leading-snug text-foreground">
            {prettyJson(call.args)}
          </pre>
          {call.hasResult && (
            <>
              <SectionLabel>result</SectionLabel>
              <pre className="mt-0.5 overflow-x-auto whitespace-pre-wrap break-words rounded-[6px] bg-muted px-2 py-1 font-mono text-[9.5px] leading-snug text-foreground">
                {call.resultSummary || "(no summary)"}
              </pre>
              {call.resultMeta && Object.keys(call.resultMeta).length > 0 && (
                <>
                  <SectionLabel>metadata</SectionLabel>
                  <pre className="mt-0.5 overflow-x-auto whitespace-pre-wrap break-words rounded-[6px] bg-muted px-2 py-1 font-mono text-[9.5px] leading-snug text-muted-foreground">
                    {prettyJson(call.resultMeta)}
                  </pre>
                </>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="mt-1.5 font-mono text-[8px] font-medium uppercase tracking-wide text-muted-foreground first:mt-0">
      {children}
    </div>
  );
}
