import { ChevronRight } from "lucide-react";
import { useState, type ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface CollapsibleCardProps {
  title: ReactNode;
  badge?: ReactNode;
  meta?: ReactNode;
  defaultOpen?: boolean;
  children: ReactNode;
  className?: string;
}

export function CollapsibleCard({
  title,
  badge,
  meta,
  defaultOpen = false,
  children,
  className,
}: CollapsibleCardProps) {
  const [open, setOpen] = useState(defaultOpen);

  return (
    <div
      className={cn(
        "rounded-lg border border-[var(--border)] bg-[var(--bg-subtle)] text-xs",
        className,
      )}
    >
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((prev) => !prev)}
        className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left hover:bg-[var(--surface)] rounded-lg transition-colors cursor-pointer"
      >
        <span className="flex min-w-0 items-center gap-2">
          <ChevronRight
            className={cn(
              "h-3.5 w-3.5 shrink-0 text-[var(--text-subtle)] transition-transform",
              open && "rotate-90",
            )}
            aria-hidden="true"
          />
          <span className="truncate font-mono font-medium text-[var(--text)]">{title}</span>
          {badge}
        </span>
        {meta && <span className="shrink-0 text-[var(--text-subtle)]">{meta}</span>}
      </button>
      {open && <div className="border-t border-[var(--border)] px-3 py-2.5">{children}</div>}
    </div>
  );
}
