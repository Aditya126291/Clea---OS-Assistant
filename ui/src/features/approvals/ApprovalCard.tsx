import { ShieldAlert, ShieldCheck, ShieldQuestion } from "lucide-react";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { ApprovalDecision, ApprovalItem } from "@/protocol/types";

interface ApprovalCardProps {
  approval: ApprovalItem;
  onRespond: (approvalId: string, decision: ApprovalDecision) => void;
}

export function ApprovalCard({ approval, onRespond }: ApprovalCardProps) {
  const [confirmingDestructive, setConfirmingDestructive] = useState(false);

  const isDestructive = approval.riskLevel === "destructive";
  const isPending = approval.status === "pending";
  const isSubmitting = approval.status === "submitting";

  const riskBadgeVariant = isDestructive
    ? "danger"
    : approval.riskLevel === "reversible"
      ? "warning"
      : "neutral";

  const handleAllowClick = () => {
    if (isDestructive && !confirmingDestructive) {
      setConfirmingDestructive(true);
      return;
    }
    setConfirmingDestructive(false);
    onRespond(approval.approvalId, "allow");
  };

  const handleDenyClick = () => {
    setConfirmingDestructive(false);
    onRespond(approval.approvalId, "deny");
  };

  return (
    <div
      role="region"
      aria-label={`Approval request for ${approval.tool}`}
      className="rounded-lg border border-[var(--border-strong)] bg-[var(--surface)] p-3 text-xs"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          {isDestructive ? (
            <ShieldAlert className="h-4 w-4 text-[var(--danger)]" aria-hidden="true" />
          ) : approval.status === "resolved" && approval.decision === "allow" ? (
            <ShieldCheck className="h-4 w-4 text-[var(--success)]" aria-hidden="true" />
          ) : (
            <ShieldQuestion className="h-4 w-4 text-[var(--warning)]" aria-hidden="true" />
          )}
          <span className="font-medium text-[var(--text)]">
            Approval required: <code className="font-mono">{approval.tool}</code>
          </span>
        </div>
        <Badge variant={riskBadgeVariant}>{approval.riskLevel}</Badge>
      </div>

      <pre className="mt-2 max-h-36 overflow-auto rounded border border-[var(--border)] bg-[var(--bg-subtle)] p-2 font-mono text-[11px] text-[var(--text-muted)]">
        {JSON.stringify(approval.arguments, null, 2)}
      </pre>

      <div className="mt-2.5 flex flex-wrap items-center justify-between gap-2">
        {isPending ? (
          <>
            <span className="text-[11px] text-[var(--text-subtle)]">
              {confirmingDestructive
                ? "Destructive action: click again to confirm execution."
                : `Timeout: ${Math.round(approval.timeoutSeconds)}s`}
            </span>
            <div className="flex items-center gap-2">
              {confirmingDestructive ? (
                <>
                  <Button size="sm" variant="ghost" onClick={() => setConfirmingDestructive(false)}>
                    Cancel
                  </Button>
                  <Button size="sm" variant="destructive" onClick={handleAllowClick}>
                    Confirm Destructive Action
                  </Button>
                </>
              ) : (
                <>
                  <Button size="sm" variant="outline" onClick={handleDenyClick}>
                    Deny
                  </Button>
                  <Button
                    size="sm"
                    variant={isDestructive ? "destructive" : "default"}
                    onClick={handleAllowClick}
                  >
                    Allow
                  </Button>
                </>
              )}
            </div>
          </>
        ) : isSubmitting ? (
          <span className="text-[11px] text-[var(--text-subtle)]">Sending response…</span>
        ) : (
          <span className="text-[11px] text-[var(--text-muted)]">
            {approval.decision === "allow"
              ? "Allowed by user"
              : approval.reason === "timeout"
                ? "Denied (timed out)"
                : "Denied by user"}
          </span>
        )}
      </div>
    </div>
  );
}
