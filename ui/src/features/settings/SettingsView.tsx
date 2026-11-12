import { RefreshCw } from "lucide-react";
import { useEffect } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useTheme, type ThemePreference } from "@/lib/theme-context";
import { useCleaStore } from "@/protocol/store";

const POLICY_LABELS: Record<string, { label: string; variant: "success" | "warning" | "danger" }> =
  {
    auto_allow: { label: "Auto-allow", variant: "success" },
    ask: { label: "Ask once", variant: "warning" },
    ask_confirm: { label: "Ask + confirm", variant: "danger" },
  };

export function SettingsView() {
  const connectionState = useCleaStore((s) => s.connectionState);
  const settings = useCleaStore((s) => s.settings);
  const settingsLoading = useCleaStore((s) => s.settingsLoading);
  const loadSettings = useCleaStore((s) => s.loadSettings);
  const { preference, setPreference } = useTheme();

  useEffect(() => {
    void loadSettings();
  }, [loadSettings]);

  return (
    <section aria-label="Settings" className="flex h-full flex-col">
      <header className="flex items-center justify-between border-b border-[var(--border)] px-6 py-3">
        <div>
          <h1 className="text-sm font-medium text-[var(--text)]">Settings</h1>
          <p className="text-xs text-[var(--text-subtle)]">
            Daemon status, active model, and permission policies (read-only, no secrets).
          </p>
        </div>
        <Button
          size="sm"
          variant="outline"
          onClick={() => void loadSettings()}
          disabled={settingsLoading}
        >
          <RefreshCw
            className={`h-3.5 w-3.5 ${settingsLoading ? "animate-spin" : ""}`}
            aria-hidden="true"
          />
          Refresh
        </Button>
      </header>

      <div className="flex-1 overflow-y-auto px-6 py-6">
        <div className="mx-auto max-w-2xl space-y-5">
          <Card>
            <CardHeader>
              <CardTitle>Appearance</CardTitle>
            </CardHeader>
            <CardContent className="flex items-center justify-between gap-4">
              <span className="text-xs text-[var(--text-muted)]">
                Follow system theme or choose manually.
              </span>
              <div className="flex items-center gap-1.5" role="group" aria-label="Theme">
                {(["system", "dark", "light"] as ThemePreference[]).map((opt) => (
                  <Button
                    key={opt}
                    size="sm"
                    variant={preference === opt ? "default" : "outline"}
                    onClick={() => setPreference(opt)}
                    className="capitalize"
                  >
                    {opt}
                  </Button>
                ))}
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Connection & Provider</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2.5 text-xs">
              <div className="flex items-center justify-between">
                <span className="text-[var(--text-muted)]">Daemon status</span>
                <Badge variant={connectionState === "connected" ? "success" : "warning"}>
                  {connectionState}
                </Badge>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-[var(--text-muted)]">Endpoint</span>
                <span className="font-mono text-[var(--text)]">
                  {settings ? `ws://${settings.host}:${settings.port}` : "—"}
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-[var(--text-muted)]">Provider</span>
                <span className="font-mono text-[var(--text)]">{settings?.provider ?? "—"}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-[var(--text-muted)]">Model</span>
                <span className="font-mono text-[var(--text)]">{settings?.model ?? "—"}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-[var(--text-muted)]">Approval timeout</span>
                <span className="font-mono text-[var(--text)]">
                  {settings ? `${settings.approval_timeout_seconds}s` : "—"}
                </span>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Permission Tiers</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-xs">
              {settings?.permission_tiers.map((item) => {
                const policy = POLICY_LABELS[item.policy] ?? {
                  label: item.policy,
                  variant: "neutral" as const,
                };
                return (
                  <div
                    key={item.tier}
                    className="flex items-center justify-between rounded-md border border-[var(--border)] bg-[var(--bg-subtle)] px-3 py-2"
                  >
                    <span className="font-mono text-[var(--text)]">{item.tier}</span>
                    <Badge variant={policy.variant}>{policy.label}</Badge>
                  </div>
                );
              }) ?? <p className="text-[var(--text-subtle)]">Unavailable.</p>}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Registered Tools</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-xs">
              {settings?.tools.map((tool) => (
                <div
                  key={tool.name}
                  className="rounded-md border border-[var(--border)] bg-[var(--bg-subtle)] px-3 py-2"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono font-medium text-[var(--text)]">{tool.name}</span>
                    <Badge
                      variant={
                        tool.risk_level === "destructive"
                          ? "danger"
                          : tool.risk_level === "reversible"
                            ? "warning"
                            : "neutral"
                      }
                    >
                      {tool.risk_level}
                    </Badge>
                  </div>
                  <p className="mt-1 text-[var(--text-muted)]">{tool.description}</p>
                </div>
              )) ?? <p className="text-[var(--text-subtle)]">No tools reported.</p>}
            </CardContent>
          </Card>
        </div>
      </div>
    </section>
  );
}
