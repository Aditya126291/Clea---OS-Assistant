import { RotateCcw, X } from "lucide-react";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { useCleaStore } from "@/protocol/store";

export function UndoToast() {
  const undoToast = useCleaStore((s) => s.undoToast);
  const undoDelete = useCleaStore((s) => s.undoDelete);
  const dismissUndoToast = useCleaStore((s) => s.dismissUndoToast);

  const [secondsRemaining, setSecondsRemaining] = useState(10);

  useEffect(() => {
    if (!undoToast) return;
    const update = () => {
      const remaining = Math.max(0, Math.ceil((undoToast.expiresAt - Date.now()) / 1000));
      setSecondsRemaining(remaining);
    };
    update();
    const interval = setInterval(update, 500);
    return () => clearInterval(interval);
  }, [undoToast]);

  if (!undoToast) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      className="fixed bottom-5 left-1/2 z-50 flex -translate-x-1/2 items-center gap-3 rounded-xl border border-[var(--border-strong)] bg-[var(--surface)] px-4 py-2.5 shadow-2xl transition-all"
    >
      <span className="text-xs text-[var(--text)]">
        {undoToast.message} <span className="text-[var(--text-subtle)]">({secondsRemaining}s)</span>
      </span>
      <div className="flex items-center gap-1.5">
        <Button
          size="sm"
          variant="default"
          onClick={() => void undoDelete()}
          className="h-7 gap-1 px-2.5 text-xs font-medium cursor-pointer"
        >
          <RotateCcw className="h-3 w-3" aria-hidden="true" />
          Undo
        </Button>
        <button
          type="button"
          onClick={dismissUndoToast}
          aria-label="Dismiss toast"
          className="rounded p-1 text-[var(--text-subtle)] hover:bg-[var(--bg-subtle)] hover:text-[var(--text)] cursor-pointer"
        >
          <X className="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      </div>
    </div>
  );
}
