import { AlertCircle, Play, RefreshCw, Terminal } from "lucide-react";
import { useEffect, useRef, useState } from "react";
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
  const startDaemon = typeof window !== "undefined" ? window.cleaDesktop?.startDaemon : undefined;
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (retryTimerRef.current !== null) {
        clearTimeout(retryTimerRef.current);
      }
    };
  }, []);

  const handleStartDaemon = async () => {
    if (!startDaemon || starting) return;
    setStarting(true);
    setStartError(null);
    try {
      const res = await startDaemon();
      if (!res.ok) {
        setStartError(res.error || "Could not start the background daemon.");
        return;
      }
      onRetry();
      retryTimerRef.current = setTimeout(() => {
        onRetry();
      }, 650);
    } catch (err) {
      setStartError(err instanceof Error ? err.message : "Could not start the background daemon.");
    } finally {
      setStarting(false);
    }
  };

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

          {startError && (
            <p role="alert" className="text-xs text-[var(--danger)]">
              {startError}
            </p>
          )}

          <div className="flex items-center justify-between gap-3">
            <span className="text-xs text-[var(--text-subtle)]" aria-live="polite">
              {connectionState === "connecting"
                ? "Connecting…"
                : reconnectAttempt > 0
                  ? `Auto-reconnecting (attempt ${reconnectAttempt})…`
                  : "Waiting for local daemon…"}
            </span>
            <div className="flex items-center gap-2">
              {startDaemon && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void handleStartDaemon()}
                  disabled={starting}
                >
                  <Play className="h-3.5 w-3.5" aria-hidden="true" />
                  {starting ? "Starting…" : "Start daemon"}
                </Button>
              )}
              <Button onClick={onRetry} size="sm">
                <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
                Retry now
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>
    </main>
  );
}
