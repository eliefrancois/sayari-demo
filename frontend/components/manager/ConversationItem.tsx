"use client";

import { useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Loader2, MoreHorizontal, Trash2 } from "lucide-react";
import clsx from "clsx";
import type { ConversationListItem } from "@/lib/types";

/**
 * One row in the history panel: title, relative updated-at, turn-count badge,
 * active-row highlight, and a hover-revealed "…" menu with Delete. Ported
 * from local-lmcanvas CanvasItem (see NOTICES.md) minus the Electron-specific
 * actions (threads, open-in-new-window, rename — titles here are server-derived).
 */

type ConversationItemProps = {
  conversation: ConversationListItem;
  isActive: boolean;
  isDeleting: boolean;
  /** Switching is blocked while a turn is streaming in the open conversation. */
  disabled: boolean;
  onSelect: () => void;
  onDelete: () => void;
};

/** Compact relative timestamp ("just now", "5m ago", "3h ago", "2d ago"). */
function relativeTime(unixSeconds: number | null): string {
  if (!unixSeconds) return "";
  const deltaS = Math.max(0, Math.floor(Date.now() / 1000) - unixSeconds);
  if (deltaS < 60) return "just now";
  const m = Math.floor(deltaS / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export function ConversationItem({
  conversation,
  isActive,
  isDeleting,
  disabled,
  onSelect,
  onDelete,
}: ConversationItemProps) {
  const [showMenu, setShowMenu] = useState(false);
  const [isHovered, setIsHovered] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!showMenu) return;
    const handleClickOutside = (e: MouseEvent) => {
      const target = e.target as Node;
      if (
        menuRef.current &&
        !menuRef.current.contains(target) &&
        buttonRef.current &&
        !buttonRef.current.contains(target)
      ) {
        setShowMenu(false);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [showMenu]);

  const isRunning = conversation.state === "running";

  return (
    <div
      className={clsx(
        "flex items-center justify-between rounded-md border py-1.5 pl-2.5 pr-1.5 transition-colors",
        isActive
          ? "border-foreground/25 bg-muted"
          : "border-transparent hover:bg-muted",
        disabled ? "cursor-default opacity-60" : "cursor-pointer"
      )}
      onClick={() => {
        if (!disabled) onSelect();
      }}
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
      onKeyDown={(e) => {
        if (disabled) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
      role="button"
      tabIndex={0}
      aria-current={isActive ? "true" : undefined}
    >
      <div className="min-w-0 flex-1">
        <div className="truncate text-[12px] font-medium text-foreground">
          {conversation.title || "New investigation"}
        </div>
        <div className="mt-0.5 flex items-center gap-2 font-mono text-[9px] uppercase tracking-[0.12em] text-muted-foreground">
          <span>{relativeTime(conversation.updated_at)}</span>
          <span className="rounded-full border border-border bg-card px-1.5 py-px tabular-nums">
            {conversation.turn_count} {conversation.turn_count === 1 ? "turn" : "turns"}
          </span>
          {isRunning && (
            <span className="flex items-center gap-1 text-foreground">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-foreground" />
              running
            </span>
          )}
          {isActive && <span className="text-foreground">active</span>}
        </div>
      </div>

      <div className="relative ml-2">
        <motion.button
          ref={buttonRef}
          onClick={(e) => {
            e.stopPropagation();
            setShowMenu((v) => !v);
          }}
          disabled={isDeleting}
          className={clsx(
            "cursor-pointer rounded-md p-1 text-muted-foreground transition-opacity hover:bg-background hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50",
            isHovered || showMenu ? "opacity-100" : "opacity-0"
          )}
          title="Options"
        >
          {isDeleting ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <MoreHorizontal className="h-3.5 w-3.5" />
          )}
        </motion.button>

        <AnimatePresence>
          {showMenu && (
            <motion.div
              ref={menuRef}
              className="absolute right-0 top-full z-50 mt-1 w-36 overflow-hidden rounded-md border border-border bg-popover shadow-lg"
              initial={{ opacity: 0, scale: 0.95, y: -6 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.95, y: -6 }}
              transition={{ duration: 0.15 }}
              onClick={(e) => e.stopPropagation()}
            >
              <button
                className="flex w-full cursor-pointer items-center gap-2 px-3 py-2 text-left text-xs text-destructive transition-colors hover:bg-destructive/10"
                onClick={(e) => {
                  e.stopPropagation();
                  setShowMenu(false);
                  onDelete();
                }}
              >
                <Trash2 className="h-3.5 w-3.5" />
                Delete
              </button>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}
