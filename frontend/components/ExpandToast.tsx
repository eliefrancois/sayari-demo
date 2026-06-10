"use client";

import { useEffect } from "react";

/** Lightweight toast for manual graph expand feedback (no sonner dep). */
export function ExpandToast({
  message,
  onDismiss,
}: {
  message: string | null;
  onDismiss: () => void;
}) {
  useEffect(() => {
    if (!message) return;
    const t = window.setTimeout(onDismiss, 3200);
    return () => window.clearTimeout(t);
  }, [message, onDismiss]);

  if (!message) return null;

  return (
    <div
      role="status"
      className="pointer-events-none absolute bottom-4 left-1/2 z-40 -translate-x-1/2 rounded-[10px] border border-border bg-card px-3 py-2 font-mono text-[10px] uppercase tracking-[0.1em] text-foreground shadow-md"
    >
      {message}
    </div>
  );
}
