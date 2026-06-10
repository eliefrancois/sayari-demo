"use client";

import { useState } from "react";
import { motion } from "framer-motion";
import { ArrowUp, Search, X } from "lucide-react";
import {
  ChatContainerContent,
  ChatContainerRoot,
} from "@/components/ui/chat-container";
import {
  PromptInput,
  PromptInputActions,
  PromptInputTextarea,
} from "@/components/ui/prompt-input";
import { ScrollButton } from "@/components/ui/scroll-button";
import { Button } from "@/components/ui/button";
import type { ConversationState } from "@/lib/conversation-store";
import { TurnCard } from "./canvas/TurnCard";
import { RiskSummaryCard } from "./RiskSummaryCard";

const EXAMPLES = ["Gazprom", "Sberbank", "Sergey Roldugin", "Huawei Technologies"];

/**
 * Left pane — INVESTIGATION. The conversation as lmcanvas-style cards stacked
 * on a 32px grid canvas (donor: local-lmcanvas, MIT). Each turn is one card:
 * bold user question → divider → interleaved reasoning + collapsible tool-call
 * blocks → streaming text / answer. Investigation turns get a distinct Risk
 * Report card connected below. Stage 1 keeps the thread linear; stage 2 turns
 * it into a real branching tree.
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
    <div className="flex h-full min-h-0 flex-col bg-background">
      {/* Pane label strip */}
      <header className="flex items-center justify-between border-b border-border bg-background px-4 py-2">
        <h2 className="font-mono text-[9px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
          Investigation
        </h2>
        <StatusBadge state={state} />
      </header>

      {/* Body: grid canvas with stacked cards */}
      {isEmpty ? (
        <EmptyState onPickExample={onSend} />
      ) : (
        <div className="canvas-grid relative flex min-h-0 flex-1 flex-col">
          <div className="canvas-vignette pointer-events-none absolute inset-0 z-0" />
          <ChatContainerRoot className="relative z-10 flex-1">
            <ChatContainerContent className="px-4 py-5">
              {state.turns.map((turn, i) => (
                <div key={turn.index} className="flex flex-col">
                  {i > 0 && <CardConnector />}
                  <TurnCard
                    turn={turn}
                    nodesById={state.nodes}
                    onSend={onSend}
                    onGenerateReport={onGenerateReport}
                    onHighlightNodes={onHighlightNodes}
                    onClearHighlight={onClearHighlight}
                    onFocusNode={onFocusNode}
                  />
                  {turn.summary && (
                    <>
                      <CardConnector />
                      <RiskSummaryCard
                        summary={turn.summary}
                        nodesById={state.nodes}
                        onFollowup={(name) => onSend(`Investigate ${name}`)}
                        onHighlightNodes={onHighlightNodes}
                        onClearHighlight={onClearHighlight}
                        onFocusNode={onFocusNode}
                      />
                    </>
                  )}
                </div>
              ))}

              {state.errorMessage && (
                <div className="mt-4 rounded-[10px] border border-red-300 bg-red-50 p-3 text-xs text-red-700 shadow-sm">
                  <strong className="font-semibold">Error:</strong>{" "}
                  {state.errorMessage}
                </div>
              )}
            </ChatContainerContent>

            <div className="pointer-events-none sticky bottom-3 z-10 flex justify-center">
              <ScrollButton className="pointer-events-auto border-border bg-card text-foreground shadow-sm hover:bg-muted" />
            </div>
          </ChatContainerRoot>
        </div>
      )}

      {/* Pinned-context bar */}
      {pinned.length > 0 && (
        <div className="border-t border-border bg-background px-4 py-2">
          <div className="mb-1 font-mono text-[8px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
            Pinned context · sent with next message
          </div>
          <div className="flex flex-wrap gap-1.5">
            {pinned.map((id) => {
              const node = state.nodes.get(id);
              return (
                <span
                  key={id}
                  className="inline-flex items-center gap-1 rounded-md border border-border bg-card px-1.5 py-0.5 font-mono text-[10px] text-foreground shadow-sm"
                >
                  {node ? truncate(node.name, 28) : id.slice(-8)}
                  <button
                    type="button"
                    onClick={() => onTogglePin?.(id)}
                    className="text-muted-foreground transition-colors hover:text-foreground"
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
      <div className="border-t border-border bg-background p-3">
        <PromptInput
          value={composerValue}
          onValueChange={setComposerValue}
          onSubmit={submit}
          isLoading={disabled}
          className="rounded-[10px] border-border bg-card shadow-sm"
        >
          <PromptInputTextarea
            placeholder={
              disabled
                ? "Agent is working…"
                : isEmpty
                  ? "Search a person or company (e.g. Sergey Roldugin)"
                  : "Ask a follow-up, or investigate someone new"
            }
            className="text-[13px]"
          />
          <PromptInputActions className="justify-end pt-2">
            <Button
              size="sm"
              onClick={submit}
              disabled={disabled || composerValue.trim().length === 0}
              className="h-8 w-8 rounded-full bg-foreground p-0 text-background hover:bg-foreground/90"
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

/** Bezier-ish connector between consecutive cards (donor edges are
 *  muted-foreground, ~2.25 stroke, rounded caps — vertical here since the
 *  stage-1 thread is linear). */
function CardConnector() {
  return (
    <div className="flex justify-center py-0.5" aria-hidden>
      <svg width="6" height="22" viewBox="0 0 6 22" fill="none">
        <path
          d="M3 1 C 3 8, 3 14, 3 21"
          stroke="var(--muted-foreground)"
          strokeWidth="2.25"
          strokeLinecap="round"
          opacity="0.6"
        />
      </svg>
    </div>
  );
}

function truncate(s: string, n: number): string {
  return s.length <= n ? s : s.slice(0, n - 1).trimEnd() + "…";
}

function StatusBadge({ state }: { state: ConversationState }) {
  const text = {
    idle: "ready",
    running: "working",
    done: "ready",
    error: "error",
  }[state.status];

  const cls = {
    idle: "text-muted-foreground border-border bg-card",
    running: "text-foreground border-border bg-muted",
    error: "text-red-700 border-red-300 bg-red-50",
    done: "text-muted-foreground border-border bg-card",
  }[state.status];

  return (
    <span
      className={
        "rounded-md border px-1.5 py-0.5 font-mono text-[9px] font-medium uppercase tracking-[0.16em] " +
        cls
      }
    >
      {state.status === "running" ? (
        <span className="node-shimmer">{text}</span>
      ) : (
        text
      )}
    </span>
  );
}

function EmptyState({ onPickExample }: { onPickExample: (q: string) => void }) {
  return (
    <div className="canvas-grid relative flex flex-1 flex-col items-center justify-center px-6 text-center">
      <div className="canvas-vignette pointer-events-none absolute inset-0" />
      <motion.div
        initial={{ scale: 0.96, opacity: 0, y: -6 }}
        animate={{ scale: 1, opacity: 1, y: 0 }}
        transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
        className="relative z-10 max-w-md rounded-[10px] border border-border bg-card px-8 py-8 shadow-sm"
      >
        <div className="mx-auto mb-4 flex size-11 items-center justify-center rounded-full border border-border bg-muted">
          <Search className="size-4.5 text-muted-foreground" />
        </div>
        <h3 className="text-[15px] font-semibold text-foreground">
          Entity Risk Resolver
        </h3>
        <p className="mt-2 text-[12.5px] leading-relaxed text-muted-foreground">
          Type a person or company name. The agent searches the ICIJ Offshore
          Leaks graph, traverses connections, and cross-checks sanctions — then
          you can ask follow-ups in the same thread.
        </p>
        <div className="mt-6 flex flex-col items-center gap-2">
          <span className="font-mono text-[9px] uppercase tracking-[0.18em] text-muted-foreground/70">
            try
          </span>
          <div className="flex flex-wrap items-center justify-center gap-1.5">
            {EXAMPLES.map((ex) => (
              <motion.button
                key={ex}
                type="button"
                whileTap={{ scale: 0.97 }}
                whileHover={{ y: -1 }}
                onClick={() => onPickExample(ex)}
                className="inline-flex cursor-pointer items-center gap-1.5 rounded-md border border-border bg-background px-2 py-1 font-mono text-[10px] uppercase tracking-[0.16em] text-foreground/80 transition-colors hover:border-foreground/40 hover:text-foreground focus:outline-none"
              >
                {ex}
              </motion.button>
            ))}
          </div>
        </div>
      </motion.div>
    </div>
  );
}
