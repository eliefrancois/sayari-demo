"use client";

import { useEffect, useRef } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, X } from "lucide-react";

/**
 * Delete confirmation modal. Ported from local-lmcanvas DeleteCanvasModal
 * (see NOTICES.md): scrim + centered card, Esc closes, the confirm button
 * takes focus so Enter confirms. Restyled to this app's tokens.
 */

type DeleteConversationModalProps = {
  isOpen: boolean;
  title: string;
  onClose: () => void;
  onConfirm: () => void;
};

export function DeleteConversationModal({
  isOpen,
  title,
  onClose,
  onConfirm,
}: DeleteConversationModalProps) {
  const confirmButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!isOpen) return;
    const t = setTimeout(() => confirmButtonRef.current?.focus(), 80);
    return () => clearTimeout(t);
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [isOpen, onClose]);

  return (
    <AnimatePresence>
      {isOpen && (
        <>
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 bg-black/30"
            onClick={onClose}
          />
          <motion.div
            initial={{ opacity: 0, scale: 0.95, y: -16 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.95, y: -16 }}
            transition={{ duration: 0.18 }}
            className="fixed left-1/2 top-24 z-[60] w-full max-w-md -translate-x-1/2 px-4"
            onClick={(e) => e.stopPropagation()}
          >
            <form
              onSubmit={(e) => {
                e.preventDefault();
                onConfirm();
              }}
              className="overflow-hidden rounded-[10px] border border-border bg-popover shadow-2xl"
            >
              <div className="flex items-center justify-between border-b border-border px-5 py-3">
                <h2 className="font-mono text-[10px] font-medium uppercase tracking-[0.16em] text-foreground">
                  Delete investigation
                </h2>
                <button
                  type="button"
                  onClick={onClose}
                  className="inline-flex h-7 w-7 cursor-pointer items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                  aria-label="Close"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>

              <div className="space-y-3 px-5 py-4">
                <p className="flex items-center gap-1.5 font-mono text-[9px] uppercase tracking-[0.14em] text-destructive">
                  <AlertTriangle className="h-3.5 w-3.5" />
                  destructive action
                </p>
                <p className="text-sm text-foreground">
                  Delete <span className="font-semibold">&quot;{title}&quot;</span>?
                  All of its turns, evidence graph and history are removed. This
                  cannot be undone.
                </p>
              </div>

              <div className="flex items-center justify-end gap-2 border-t border-border px-5 py-3">
                <button
                  type="button"
                  onClick={onClose}
                  className="cursor-pointer rounded-md border border-border bg-card px-3 py-1.5 font-mono text-[9px] font-medium uppercase tracking-[0.16em] text-foreground shadow-sm transition-colors hover:bg-muted"
                >
                  cancel
                </button>
                <button
                  ref={confirmButtonRef}
                  type="submit"
                  className="cursor-pointer rounded-md bg-destructive px-3 py-1.5 font-mono text-[9px] font-medium uppercase tracking-[0.16em] text-white transition-opacity hover:opacity-90"
                >
                  delete
                </button>
              </div>
            </form>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}
