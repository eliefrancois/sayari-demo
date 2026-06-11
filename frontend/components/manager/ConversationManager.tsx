"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Plus, X } from "lucide-react";
import {
  deleteConversation as deleteConversationApi,
  listConversations,
} from "@/lib/sse-client";
import type { ConversationListItem } from "@/lib/types";
import { ConversationItem } from "./ConversationItem";
import { ConversationSearch, type ConversationSearchRef } from "./ConversationSearch";
import { DeleteConversationModal } from "./DeleteConversationModal";

/**
 * The conversation history panel: a fixed left slide-over listing recent
 * investigations from GET /conversations (24h server-side TTL, so this is a
 * recents menu, not an archive). Shell ported from local-lmcanvas
 * CanvasManager (see NOTICES.md); the toggle lives in the app header instead
 * of a floating button, and Electron-only features (threads, settings,
 * open-in-new-window) are dropped.
 *
 * The panel owns list fetching / search / delete-confirm; switching and
 * "what happens after a delete of the active conversation" stay with the app
 * shell via callbacks, because they touch the conversation store.
 */

type ConversationManagerProps = {
  open: boolean;
  onClose: () => void;
  activeConversationId: string | null;
  /** True while a turn is streaming: switching/deleting is blocked. */
  isRunning: boolean;
  onSelect: (conversationId: string) => void;
  onNewInvestigation: () => void;
  /** Called AFTER a successful server delete, with the deleted id. */
  onDeleted: (conversationId: string) => void;
};

export function ConversationManager({
  open,
  onClose,
  activeConversationId,
  isRunning,
  onSelect,
  onNewInvestigation,
  onDeleted,
}: ConversationManagerProps) {
  const [conversations, setConversations] = useState<ConversationListItem[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [pendingDelete, setPendingDelete] = useState<ConversationListItem | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const searchRef = useRef<ConversationSearchRef>(null);

  // Refresh on every open: the list is cheap (one indexed read) and titles /
  // turn counts move every turn.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setIsLoading(true);
    setLoadError(null);
    listConversations()
      .then((list) => {
        if (cancelled) return;
        setConversations(list);
        setIsLoading(false);
      })
      .catch((err) => {
        if (cancelled) return;
        setLoadError(err instanceof Error ? err.message : "failed to load");
        setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !pendingDelete) onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose, pendingDelete]);

  const filtered = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return conversations;
    return conversations.filter((c) => (c.title ?? "").toLowerCase().includes(q));
  }, [conversations, searchQuery]);

  const noResults = searchQuery.trim().length > 0 && filtered.length === 0;

  const handleSelect = (c: ConversationListItem) => {
    searchRef.current?.close();
    onSelect(c.conversation_id);
  };

  const confirmDelete = async () => {
    if (!pendingDelete) return;
    const { conversation_id } = pendingDelete;
    setPendingDelete(null);
    setDeletingId(conversation_id);
    try {
      await deleteConversationApi(conversation_id);
      setConversations((prev) =>
        prev.filter((c) => c.conversation_id !== conversation_id)
      );
      onDeleted(conversation_id);
    } catch (err) {
      console.error("delete conversation failed", err);
      setLoadError(err instanceof Error ? err.message : "delete failed");
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <>
      <AnimatePresence>
        {open && (
          <>
            {/* Scrim: click anywhere outside to dismiss. */}
            <motion.div
              className="fixed inset-0 z-30 bg-black/10"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.15 }}
              onClick={onClose}
            />
            <motion.div
              className="fixed left-0 top-0 z-40 flex h-screen w-80 flex-col border-r border-border bg-card shadow-lg"
              initial={{ x: -320 }}
              animate={{ x: 0 }}
              exit={{ x: -320 }}
              transition={{ type: "spring", damping: 25, stiffness: 200 }}
            >
              {/* Header */}
              <div className="flex h-10 shrink-0 items-center justify-between border-b border-border px-4">
                <h2 className="font-mono text-[10px] font-medium uppercase tracking-[0.18em] text-foreground">
                  Investigations
                </h2>
                <button
                  onClick={onClose}
                  className="cursor-pointer rounded-md p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                  aria-label="Close history"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>

              {/* New investigation */}
              <div className="p-3 pb-1">
                <button
                  onClick={onNewInvestigation}
                  className="flex w-full cursor-pointer items-center justify-center gap-1.5 rounded-md border border-border bg-background px-3 py-2 font-mono text-[9px] font-medium uppercase tracking-[0.16em] text-foreground shadow-sm transition-colors hover:bg-muted"
                  title="Start a fresh investigation (this one stays in the list)"
                >
                  <Plus className="h-3.5 w-3.5" />
                  New investigation
                </button>
              </div>

              {/* List */}
              <div className="flex-1 overflow-y-auto px-3 pt-2">
                <ConversationSearch
                  ref={searchRef}
                  searchQuery={searchQuery}
                  onSearchChange={setSearchQuery}
                />

                {isRunning && (
                  <div className="mb-2 rounded-md border border-border bg-muted px-2.5 py-1.5 font-mono text-[9px] uppercase tracking-[0.12em] text-muted-foreground">
                    a turn is streaming — switching is paused until it finishes
                  </div>
                )}

                {isLoading ? (
                  <div className="p-4 text-center font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
                    loading…
                  </div>
                ) : loadError ? (
                  <div className="p-4 text-center text-xs text-destructive">
                    {loadError}
                  </div>
                ) : noResults ? (
                  <div className="p-4 text-center text-xs text-muted-foreground">
                    No investigations match &quot;{searchQuery}&quot;
                  </div>
                ) : filtered.length === 0 ? (
                  <div className="p-4 text-center text-xs text-muted-foreground">
                    No recent investigations. They appear here as you run them,
                    and expire after 24 hours.
                  </div>
                ) : (
                  <div className="flex flex-col gap-1 pb-3">
                    {filtered.map((c) => (
                      <ConversationItem
                        key={c.conversation_id}
                        conversation={c}
                        isActive={c.conversation_id === activeConversationId}
                        isDeleting={deletingId === c.conversation_id}
                        disabled={isRunning}
                        onSelect={() => handleSelect(c)}
                        onDelete={() => setPendingDelete(c)}
                      />
                    ))}
                  </div>
                )}
              </div>

              {/* Footer: the TTL story, so nobody mistakes this for an archive. */}
              <div className="shrink-0 border-t border-border px-4 py-2">
                <p className="font-mono text-[8px] uppercase tracking-[0.12em] text-muted-foreground/70">
                  recent investigations · kept 24h after last activity
                </p>
              </div>
            </motion.div>
          </>
        )}
      </AnimatePresence>

      <DeleteConversationModal
        isOpen={pendingDelete !== null}
        title={pendingDelete?.title ?? ""}
        onClose={() => setPendingDelete(null)}
        onConfirm={() => void confirmDelete()}
      />
    </>
  );
}
