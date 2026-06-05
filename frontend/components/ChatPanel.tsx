"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowUp, Sparkles, Search, X } from "lucide-react";
import {
  ChainOfThought,
  ChainOfThoughtContent,
  ChainOfThoughtItem,
  ChainOfThoughtStep,
  ChainOfThoughtTrigger,
} from "@/components/ui/chain-of-thought";
import {
  ChatContainerContent,
  ChatContainerRoot,
} from "@/components/ui/chat-container";
import { TextShimmerLoader, TypingLoader } from "@/components/ui/loader";
import {
  Message,
  MessageAvatar,
  MessageContent,
} from "@/components/ui/message";
import {
  PromptInput,
  PromptInputActions,
  PromptInputTextarea,
} from "@/components/ui/prompt-input";
import { PromptSuggestion } from "@/components/ui/prompt-suggestion";
import { ScrollButton } from "@/components/ui/scroll-button";
import { Button } from "@/components/ui/button";
import type { AgentThought, ConversationState, Turn } from "@/lib/conversation-store";
import type { GraphNode } from "@/lib/types";
import { RiskSummaryCard } from "./RiskSummaryCard";
import { AnswerCard } from "./AnswerCard";

const EXAMPLES = ["Gazprom", "Sberbank", "Sergey Roldugin", "Huawei Technologies"];

/**
 * Left panel — the conversation thread. Renders an ordered list of turns; each
 * turn is a user message, the agent's live reasoning timeline, and the result
 * (a RiskSummaryCard for investigations, an AnswerCard for clarifications /
 * follow-ups). The composer is always available except while a turn runs.
 */
export function ChatPanel({
  state,
  onSend,
  onGenerateReport,
  disabled,
  onHighlightNodes,
  onClearHighlight,
  onFocusNode,
  onTogglePin,
}: {
  state: ConversationState;
  onSend: (text: string) => void;
  onGenerateReport: (prompt: string) => void;
  disabled: boolean;
  onHighlightNodes?: (nodeIds: string[]) => void;
  onClearHighlight?: () => void;
  onFocusNode?: (nodeId: string) => void;
  onTogglePin?: (nodeId: string) => void;
}) {
  const [composerValue, setComposerValue] = useState("");

  const submit = () => {
    const t = composerValue.trim();
    if (!t || disabled) return;
    onSend(t);
    setComposerValue("");
  };

  const isEmpty = state.turns.length === 0;
  const pinned = Array.from(state.pinnedNodeIds);

  return (
    <div className="flex h-full min-h-0 flex-col bg-zinc-950 text-zinc-100">
      {/* Header */}
      <header className="flex items-center justify-between border-b border-zinc-800 px-3 py-2">
        <div>
          <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-400">
            Investigation
          </h2>
          {state.turns.length > 0 && (
            <div className="mt-0.5 text-sm font-medium text-zinc-100">
              {state.turns[0].userMessage}
            </div>
          )}
        </div>
        <StatusBadge state={state} />
      </header>

      {/* Body */}
      {isEmpty ? (
        <EmptyState onPickExample={onSend} />
      ) : (
        <div className="relative flex min-h-0 flex-1 flex-col">
          <ChatContainerRoot className="relative flex-1">
            <ChatContainerContent className="space-y-4 px-3 py-4">
              {state.turns.map((turn) => (
                <TurnBlock
                  key={turn.index}
                  turn={turn}
                  nodesById={state.nodes}
                  onSend={onSend}
                  onGenerateReport={onGenerateReport}
                  onHighlightNodes={onHighlightNodes}
                  onClearHighlight={onClearHighlight}
                  onFocusNode={onFocusNode}
                />
              ))}

              {state.errorMessage && (
                <div className="rounded-md border border-red-900/40 bg-red-950/30 p-3 text-xs text-red-300">
                  <strong className="font-semibold">Error:</strong>{" "}
                  {state.errorMessage}
                </div>
              )}
            </ChatContainerContent>

            <div className="pointer-events-none sticky bottom-3 z-10 flex justify-center">
              <ScrollButton className="pointer-events-auto bg-zinc-900 border-zinc-700 text-zinc-200 hover:bg-zinc-800" />
            </div>
          </ChatContainerRoot>
        </div>
      )}

      {/* Pinned-context bar */}
      {pinned.length > 0 && (
        <div className="border-t border-zinc-800 bg-zinc-900/40 px-3 py-2">
          <div className="mb-1 text-[10px] font-medium uppercase tracking-wider text-zinc-500">
            Pinned context · sent with next message
          </div>
          <div className="flex flex-wrap gap-1.5">
            {pinned.map((id) => {
              const node = state.nodes.get(id);
              return (
                <span
                  key={id}
                  className="inline-flex items-center gap-1 rounded border border-sky-700/50 bg-sky-600/15 px-1.5 py-0.5 text-[11px] text-sky-200"
                >
                  {node ? truncate(node.name, 28) : id.slice(-8)}
                  <button
                    type="button"
                    onClick={() => onTogglePin?.(id)}
                    className="text-sky-300/70 hover:text-sky-100"
                    title="Unpin"
                  >
                    <X className="size-3" />
                  </button>
                </span>
              );
            })}
          </div>
        </div>
      )}

      {/* Composer */}
      <div className="border-t border-zinc-800 bg-zinc-950/40 p-3">
        <PromptInput
          value={composerValue}
          onValueChange={setComposerValue}
          onSubmit={submit}
          isLoading={disabled}
          className="border-zinc-700 bg-zinc-900/80"
        >
          <PromptInputTextarea
            placeholder={
              disabled
                ? "Agent is working…"
                : isEmpty
                  ? "Search a person or company (e.g. Sergey Roldugin)"
                  : "Ask a follow-up, or investigate someone new"
            }
          />
          <PromptInputActions className="justify-end pt-2">
            <Button
              size="sm"
              onClick={submit}
              disabled={disabled || composerValue.trim().length === 0}
              className="h-8 w-8 rounded-full p-0"
              title={disabled ? "Agent is working" : "Send"}
            >
              <ArrowUp className="h-4 w-4" />
            </Button>
          </PromptInputActions>
        </PromptInput>
      </div>
    </div>
  );
}

/** One turn: user bubble + reasoning timeline + result card. */
function TurnBlock({
  turn,
  nodesById,
  onSend,
  onGenerateReport,
  onHighlightNodes,
  onClearHighlight,
  onFocusNode,
}: {
  turn: Turn;
  nodesById: Map<string, GraphNode>;
  onSend: (text: string) => void;
  onGenerateReport: (prompt: string) => void;
  onHighlightNodes?: (nodeIds: string[]) => void;
  onClearHighlight?: () => void;
  onFocusNode?: (nodeId: string) => void;
}) {
  const isRunning = turn.status === "running";
  return (
    <div className="space-y-3">
      {/* User message */}
      <Message className="justify-end">
        <MessageContent
          markdown
          className="bg-sky-600/20 border border-sky-700/40 text-zinc-100 max-w-[85%]"
        >
          {turn.userMessage}
        </MessageContent>
      </Message>

      {/* Agent reasoning timeline */}
      {turn.thoughts.length > 0 && (
        <Message>
          <MessageAvatar
            src=""
            alt="Agent"
            fallback="A"
            className="bg-emerald-700/60 text-emerald-100"
          />
          <div className="min-w-0 flex-1 rounded-lg border border-zinc-800 bg-zinc-900/40 px-3 py-2">
            <div className="mb-1.5 flex items-center gap-1.5 text-xs text-zinc-400">
              <Sparkles className="size-3" /> Agent reasoning
              <span className="ml-1 text-[10px] text-zinc-600">
                {turn.thoughts.length} step{turn.thoughts.length === 1 ? "" : "s"}
              </span>
            </div>
            <TurnReasoning thoughts={turn.thoughts} isRunning={isRunning} />
          </div>
        </Message>
      )}

      {/* Live streaming text (the response currently being generated) */}
      {isRunning && turn.streamingText && !turn.summary && !turn.answer && (
        <Message>
          <MessageAvatar
            src=""
            alt="Agent"
            fallback="A"
            className="bg-emerald-700/60 text-emerald-100"
          />
          <div className="min-w-0 flex-1 rounded-lg border border-zinc-800 bg-zinc-900/40 px-3 py-2">
            <MessageContent
              markdown
              className="bg-transparent p-0 text-sm leading-relaxed text-zinc-200"
            >
              {turn.streamingText}
            </MessageContent>
            <span className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse bg-emerald-400/80 align-middle" />
          </div>
        </Message>
      )}

      {/* In-flight loader — only while waiting (no live text yet, e.g. tools running) */}
      {isRunning && !turn.streamingText && !turn.summary && !turn.answer && (
        <Message>
          <MessageAvatar
            src=""
            alt="Agent"
            fallback="A"
            className="bg-emerald-700/60 text-emerald-100"
          />
          <div className="flex items-center gap-3 rounded-lg border border-zinc-800 bg-zinc-900/40 px-3 py-2">
            <TypingLoader size="sm" />
            <TextShimmerLoader size="sm" text={inflightLabel(turn)} />
          </div>
        </Message>
      )}

      {/* Result: investigation summary or lighter answer */}
      {turn.summary && (
        <RiskSummaryCard
          summary={turn.summary}
          nodesById={nodesById}
          onFollowup={(name) => onSend(`Investigate ${name}`)}
          onHighlightNodes={onHighlightNodes}
          onClearHighlight={onClearHighlight}
          onFocusNode={onFocusNode}
        />
      )}
      {turn.answer && (
        <AnswerCard
          answer={turn.answer}
          nodesById={nodesById}
          onSend={onSend}
          onGenerateReport={onGenerateReport}
          onHighlightNodes={onHighlightNodes}
          onClearHighlight={onClearHighlight}
          onFocusNode={onFocusNode}
        />
      )}
    </div>
  );
}

/**
 * Per-turn reasoning timeline. Auto-opens the latest step while the turn is
 * running; keeps it collapsed once the turn is done. Controlled open-state lets
 * the user toggle any step manually without us clobbering it on re-render.
 */
function TurnReasoning({
  thoughts,
  isRunning,
}: {
  thoughts: AgentThought[];
  isRunning: boolean;
}) {
  const [openSteps, setOpenSteps] = useState<Record<string, boolean>>({});
  const lastCount = useRef(0);

  useEffect(() => {
    const count = thoughts.length;
    if (count === lastCount.current) return;
    setOpenSteps((prev) => {
      const next: Record<string, boolean> = {};
      thoughts.forEach((t, i) => {
        const isLatest = i === count - 1;
        if (isLatest && isRunning) next[t.id] = true;
        else if (t.id in prev) next[t.id] = prev[t.id];
        else next[t.id] = false;
      });
      return next;
    });
    lastCount.current = count;
  }, [thoughts, isRunning]);

  return (
    <ChainOfThought className="text-zinc-300">
      {thoughts.map((t) => (
        <ChainOfThoughtStep
          key={t.id}
          open={openSteps[t.id] ?? false}
          onOpenChange={(open) => setOpenSteps((p) => ({ ...p, [t.id]: open }))}
        >
          <ChainOfThoughtTrigger>{summarizeThought(t.text)}</ChainOfThoughtTrigger>
          <ChainOfThoughtContent>
            <ChainOfThoughtItem className="text-sm leading-relaxed text-zinc-300">
              {t.text}
            </ChainOfThoughtItem>
          </ChainOfThoughtContent>
        </ChainOfThoughtStep>
      ))}
    </ChainOfThought>
  );
}

function summarizeThought(text: string): string {
  const firstSentence = text.split(/(?<=[.!?])\s/)[0] ?? text;
  if (firstSentence.length <= 80) return firstSentence;
  return firstSentence.slice(0, 77).trimEnd() + "…";
}

function inflightLabel(turn: Turn): string {
  const latest = turn.toolCalls[turn.toolCalls.length - 1];
  if (!latest || latest.hasResult) return "Thinking";
  switch (latest.tool) {
    case "search_entity":
      return "Searching ICIJ Offshore Leaks";
    case "get_relationships":
      return "Traversing relationships";
    case "get_officers":
      return "Pulling officers";
    case "find_address_connections":
      return "Cross-referencing addresses";
    case "find_er_links":
      return "Checking entity-resolution links";
    case "check_sanctions":
      return "Querying OpenSanctions";
    default:
      return "Investigating";
  }
}

function truncate(s: string, n: number): string {
  return s.length <= n ? s : s.slice(0, n - 1).trimEnd() + "…";
}

function StatusBadge({ state }: { state: ConversationState }) {
  const text = {
    idle: "Ready",
    running: "Working…",
    done: "Ready",
    error: "Error",
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
      <div className="mb-3 flex size-12 items-center justify-center rounded-full border border-zinc-800 bg-zinc-900">
        <Search className="size-5 text-zinc-400" />
      </div>
      <h3 className="text-base font-medium text-zinc-200">Entity Risk Resolver</h3>
      <p className="mt-2 max-w-sm text-sm text-zinc-500">
        Type a person or company name. The agent searches the ICIJ Offshore Leaks
        graph, traverses connections, and cross-checks sanctions — then you can ask
        follow-ups in the same thread.
      </p>
      <div className="mt-6 flex flex-wrap items-center justify-center gap-2">
        <span className="text-[11px] uppercase tracking-wider text-zinc-600">Try:</span>
        {EXAMPLES.map((ex) => (
          <PromptSuggestion
            key={ex}
            size="sm"
            onClick={() => onPickExample(ex)}
            className="border-zinc-700 bg-zinc-900/60 text-xs text-zinc-300 hover:border-zinc-600 hover:bg-zinc-800 hover:text-zinc-100"
          >
            {ex}
          </PromptSuggestion>
        ))}
      </div>
    </div>
  );
}
