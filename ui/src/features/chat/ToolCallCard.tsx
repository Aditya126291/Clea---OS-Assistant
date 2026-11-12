import { CheckCircle2, Loader2, XCircle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { CollapsibleCard } from "@/components/ui/collapsible";
import type { ToolCallItem } from "@/protocol/types";

interface ToolCallCardProps {
  call: ToolCallItem;
}

export function ToolCallCard({ call }: ToolCallCardProps) {
  const statusIcon =
    call.status === "running" ? (
      <Loader2 className="h-3.5 w-3.5 animate-spin text-[var(--text-muted)]" aria-hidden="true" />
    ) : call.status === "error" ? (
      <XCircle className="h-3.5 w-3.5 text-[var(--danger)]" aria-hidden="true" />
    ) : (
      <CheckCircle2 className="h-3.5 w-3.5 text-[var(--success)]" aria-hidden="true" />
    );

  const meta =
    call.status === "running"
      ? "running…"
      : call.durationMs !== undefined
        ? `${call.durationMs} ms`
        : call.status;

  return (
    <CollapsibleCard
      title={
        <span className="inline-flex items-center gap-1.5">
          {statusIcon}
          <span>{call.name}</span>
        </span>
      }
      badge={
        <Badge
          variant={
            call.riskLevel === "destructive"
              ? "danger"
              : call.riskLevel === "reversible"
                ? "warning"
                : "neutral"
          }
        >
          {call.riskLevel}
        </Badge>
      }
      meta={meta}
    >
      <div className="space-y-2">
        <div>
          <div className="mb-1 text-[11px] font-medium text-[var(--text-subtle)]">Arguments</div>
          <pre className="max-h-40 overflow-auto rounded border border-[var(--border)] bg-[var(--surface)] p-2 font-mono text-[11px] text-[var(--text-muted)]">
            {JSON.stringify(call.arguments, null, 2)}
          </pre>
        </div>
        {call.output !== undefined && (
          <div>
            <div className="mb-1 text-[11px] font-medium text-[var(--text-subtle)]">
              {call.status === "error" ? "Error" : "Result"}
            </div>
            <pre className="max-h-52 overflow-auto whitespace-pre-wrap break-words rounded border border-[var(--border)] bg-[var(--surface)] p-2 font-mono text-[11px] text-[var(--text)]">
              {call.output}
            </pre>
          </div>
        )}
      </div>
    </CollapsibleCard>
  );
}
