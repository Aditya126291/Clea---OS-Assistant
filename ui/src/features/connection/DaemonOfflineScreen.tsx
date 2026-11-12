import { AlertCircle, RefreshCw, Terminal } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { ConnectionState } from "@/protocol/types";

interface DaemonOfflineScreenProps {
  connectionState: ConnectionState;
  reconnectAttempt: number;
  onRetry: () => void;
}

export function DaemonOfflineScreen({
  connectionState,
  reconnectAttempt,
  onRetry,
}: DaemonOfflineScreenProps) {
  const isAuthError = connectionState === "auth_error";

  return (
    <main className="flex h-full items-center justify-center bg-[var(--bg)] p-6">
      <Card className="w-full max-w-md border-[var(--border-strong)]">
        <CardHeader>
          <div className="flex items-center gap-2.5 text-[var(--text)]">
            <AlertCircle className="h-5 w-5 text-[var(--warning)]" aria-hidden="true" />
            <CardTitle className="text-base">
              {isAuthError ? "Daemon authentication failed" : "Clea daemon is not running"}
            </CardTitle>
          </div>
          <p className="text-sm text-[var(--text-muted)]">
            {isAuthError
              ? "The session token was rejected. If the daemon restarted, retry to pick up the new token."
              : "Clea connects only to a local daemon on 127.0.0.1. Start the daemon to continue."}
          </p>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="rounded-lg border border-[var(--border)] bg-[var(--bg-subtle)] p-3 font-mono text-xs text-[var(--text)]">
            <div className="mb-1.5 flex items-center gap-1.5 text-[var(--text-subtle)]">
              <Terminal className="h-3.5 w-3.5" aria-hidden="true" />
              <span>Start in a terminal</span>
            </div>
            <code>uv run clea daemon</code>
          </div>

          <div className="flex items-center justify-between gap-3">
            <span className="text-xs text-[var(--text-subtle)]" aria-live="polite">
              {connectionState === "connecting"
                ? "Connecting…"
                : reconnectAttempt > 0
                  ? `Auto-reconnecting (attempt ${reconnectAttempt})…`
                  : "Waiting for local daemon…"}
            </span>
            <Button onClick={onRetry} size="sm">
              <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
              Retry now
            </Button>
          </div>
        </CardContent>
      </Card>
    </main>
  );
}
