"use client";

import { useState, useImperativeHandle, forwardRef } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Search, X } from "lucide-react";

/**
 * Collapsed-by-default search row for the history panel: a "Recent" micro
 * label with a search icon that swaps to an inline filter input. Ported from
 * local-lmcanvas CanvasSearch (see NOTICES.md) and restyled to this app's
 * mono-label language.
 */

interface ConversationSearchProps {
  searchQuery: string;
  onSearchChange: (query: string) => void;
}

export interface ConversationSearchRef {
  close: () => void;
}

export const ConversationSearch = forwardRef<
  ConversationSearchRef,
  ConversationSearchProps
>(({ searchQuery, onSearchChange }, ref) => {
  const [isOpen, setIsOpen] = useState(false);

  useImperativeHandle(ref, () => ({
    close: () => {
      setIsOpen(false);
      onSearchChange("");
    },
  }));

  const handleClose = () => {
    setIsOpen(false);
    onSearchChange("");
  };

  return (
    <div className="relative flex h-8 items-center pb-2">
      <AnimatePresence mode="wait">
        {isOpen ? (
          <motion.div
            key="search"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="absolute inset-x-0 flex items-center gap-2"
          >
            <div className="relative flex-1">
              <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => onSearchChange(e.target.value)}
                placeholder="Filter by title…"
                className="w-full rounded-md border border-border bg-background py-1.5 pl-8 pr-3 text-xs text-foreground placeholder:text-muted-foreground focus:outline-none"
                autoFocus
              />
            </div>
            <button
              onClick={handleClose}
              className="flex-shrink-0 cursor-pointer rounded-md p-1 transition-colors hover:bg-muted"
              aria-label="Close search"
            >
              <X className="h-3.5 w-3.5 text-muted-foreground" />
            </button>
          </motion.div>
        ) : (
          <motion.div
            key="label"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="absolute inset-x-0 flex items-center justify-between px-1"
          >
            <span className="font-mono text-[9px] font-medium uppercase tracking-[0.18em] text-muted-foreground">
              Recent
            </span>
            <button
              onClick={() => setIsOpen(true)}
              className="cursor-pointer rounded-md p-1 transition-colors hover:bg-muted"
              aria-label="Search investigations"
            >
              <Search className="h-3.5 w-3.5 text-muted-foreground" />
            </button>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
});

ConversationSearch.displayName = "ConversationSearch";
