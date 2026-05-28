"use client";

import { useMemo } from "react";
import {
  ChatProvider,
  ChatMessages,
  ChatComposer,
  type ChatMessageData,
  type ChatUser,
  type TypingUser,
} from "@/components/ui/chat";
import type { InvestigationState } from "@/lib/investigation-store";
import { RiskSummaryCard } from "./RiskSummaryCard";

const CURRENT_USER: ChatUser = {
  id: "analyst",
  name: "Analyst",
  status: "online",
};

const AGENT_USER: ChatUser = {
  id: "agent",
  name: "Risk Agent",
  status: "online",
};

const TYPING_AGENT: TypingUser[] = [
  { id: AGENT_USER.id, name: AGENT_USER.name },
];

const EXAMPLES = ["Sergey Roldugin", "Jeffrey Epstein", "Wilbur Ross"];

export function ChatPanel({
  state,
  onSend,
  disabled,
}: {
  state: InvestigationState;
  onSend: (text: string) => void;
  disabled: boolean;
}) {
  /**
   * Convert our investigation state to chatcn's message list. We synthesize
   * one analyst message (the original query) and one agent message per
   * agent_thought event.
   */
  const messages: ChatMessageData[] = useMemo(() => {
    const out: ChatMessageData[] = [];
    if (state.query) {
      out.push({
        id: "user-query",
        senderId: CURRENT_USER.id,
        senderName: CURRENT_USER.name,
        text: `Investigate **${state.query}**`,
        timestamp: state.startedAt ?? Date.now(),
        status: state.status === "error" ? "failed" : "delivered",
      });
    }
    for (const t of state.thoughts) {
      out.push({
        id: `thought-${t.id}`,
        senderId: AGENT_USER.id,
        senderName: AGENT_USER.name,
        text: t.text,
        timestamp: t.at,
        status: "delivered",
      });
    }
    return out;
  }, [state.query, state.startedAt, state.status, state.thoughts]);

  return (
    <ChatProvider currentUser={CURRENT_USER} theme="midnight" className="flex h-full flex-col">
      {/* Header */}
      <header className="flex items-center justify-between border-b border-zinc-800 px-3 py-2">
        <div>
          <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-400">
            Investigation
          </h2>
          {state.query && (
            <div className="mt-0.5 text-sm font-medium text-zinc-100">{state.query}</div>
          )}
        </div>
        <StatusBadge state={state} />
      </header>

      {/* Messages area */}
      <div className="flex min-h-0 flex-1 flex-col">
        {state.status === "idle" && state.thoughts.length === 0 ? (
          <EmptyState onPickExample={onSend} />
        ) : (
          <ChatMessages
            messages={messages}
            typingUsers={state.status === "running" ? TYPING_AGENT : []}
            className="min-h-0"
          />
        )}
      </div>

      {/* Final summary card */}
      {state.summary && (
        <div className="border-t border-zinc-800 p-3">
          <RiskSummaryCard summary={state.summary} />
        </div>
      )}

      {state.errorMessage && (
        <div className="border-t border-red-900/40 bg-red-950/30 p-3 text-xs text-red-300">
          <strong className="font-semibold">Error:</strong> {state.errorMessage}
        </div>
      )}

      {/* Composer */}
      <div className="border-t border-zinc-800">
        <ChatComposer
          onSend={(text) => {
            const t = text.trim();
            if (t) onSend(t);
          }}
          disabled={disabled}
          placeholder={
            disabled
              ? "Investigation running..."
              : "Search a person or company (e.g. Sergey Roldugin)"
          }
        />
      </div>
    </ChatProvider>
  );
}

function StatusBadge({ state }: { state: InvestigationState }) {
  const elapsed =
    state.startedAt && (state.finishedAt ?? Date.now())
      ? (((state.finishedAt ?? Date.now()) - state.startedAt) / 1000).toFixed(1) + "s"
      : null;

  const text = {
    idle: "Ready",
    running: "Investigating…",
    done: `Done · ${elapsed}`,
    error: `Error · ${elapsed ?? ""}`,
  }[state.status];

  const cls = {
    idle: "text-zinc-500 bg-zinc-900",
    running: "text-amber-300 bg-amber-900/30 border-amber-700/40 border",
    done: "text-emerald-300 bg-emerald-900/30 border-emerald-700/40 border",
    error: "text-red-300 bg-red-900/30 border-red-700/40 border",
  }[state.status];

  return (
    <span className={"rounded px-2 py-0.5 text-[11px] font-medium tabular-nums " + cls}>
      {text}
    </span>
  );
}

function EmptyState({ onPickExample }: { onPickExample: (q: string) => void }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center px-6 text-center">
      <h3 className="text-base font-medium text-zinc-200">Entity Risk Resolver</h3>
      <p className="mt-2 max-w-sm text-sm text-zinc-500">
        Type a person or company name. The agent will search the ICIJ Offshore
        Leaks graph, traverse connections, and cross-check against sanctions.
      </p>
      <div className="mt-6 flex flex-wrap items-center justify-center gap-2">
        <span className="text-[11px] uppercase tracking-wider text-zinc-600">Try:</span>
        {EXAMPLES.map((ex) => (
          <button
            key={ex}
            onClick={() => onPickExample(ex)}
            className="rounded-full border border-zinc-700 bg-zinc-900/60 px-3 py-1 text-xs text-zinc-300 transition-colors hover:border-zinc-600 hover:bg-zinc-800 hover:text-zinc-100"
          >
            {ex}
          </button>
        ))}
      </div>
    </div>
  );
}
