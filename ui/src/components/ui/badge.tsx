import { cva, type VariantProps } from "class-variance-authority";
import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium tracking-wide",
  {
    variants: {
      variant: {
        neutral: "border-[var(--border)] bg-[var(--bg-subtle)] text-[var(--text-muted)]",
        success: "border-emerald-500/30 bg-emerald-500/10 text-[var(--success)]",
        warning: "border-amber-500/35 bg-amber-500/10 text-[var(--warning)]",
        danger: "border-red-500/35 bg-red-500/10 text-[var(--danger)]",
      },
    },
    defaultVariants: {
      variant: "neutral",
    },
  },
);

export interface BadgeProps
  extends HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}
