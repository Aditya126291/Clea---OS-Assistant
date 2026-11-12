import { cva, type VariantProps } from "class-variance-authority";
import type { ButtonHTMLAttributes } from "react";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium transition-colors disabled:pointer-events-none disabled:opacity-50 cursor-pointer",
  {
    variants: {
      variant: {
        default: "bg-[var(--accent)] text-[var(--accent-fg)] hover:opacity-90",
        secondary:
          "bg-[var(--bg-subtle)] text-[var(--text)] border border-[var(--border)] hover:bg-[var(--surface-elevated)]",
        ghost: "text-[var(--text-muted)] hover:bg-[var(--bg-subtle)] hover:text-[var(--text)]",
        destructive: "bg-[var(--danger)] text-white hover:opacity-90",
        outline:
          "border border-[var(--border)] bg-transparent text-[var(--text)] hover:bg-[var(--bg-subtle)]",
      },
      size: {
        default: "h-9 px-3.5 py-2",
        sm: "h-7.5 px-2.5 text-xs",
        icon: "h-8 w-8 p-0",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);

export interface ButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof buttonVariants> {}

export function Button({ className, variant, size, type = "button", ...props }: ButtonProps) {
  return (
    <button type={type} className={cn(buttonVariants({ variant, size, className }))} {...props} />
  );
}
