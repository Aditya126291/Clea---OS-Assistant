import { RefreshCw } from "lucide-react";
import { useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { MarkdownContent } from "@/features/chat/MarkdownContent";
import { cn } from "@/lib/utils";
import { useCleaStore } from "@/protocol/store";

export function MemoryView() {
  const longTerm = useCleaStore((s) => s.memoryLongTerm);
  const days = useCleaStore((s) => s.memoryDays);
  const selectedDay = useCleaStore((s) => s.selectedMemoryDay);
  const dailyContent = useCleaStore((s) => s.memoryDailyContent);
  const loading = useCleaStore((s) => s.memoryLoading);
  const loadMemory = useCleaStore((s) => s.loadMemory);
  const loadDailyMemory = useCleaStore((s) => s.loadDailyMemory);

  useEffect(() => {
    void loadMemory();
  }, [loadMemory]);

  return (
    <section aria-label="Memory" className="flex h-full flex-col">
      <header className="flex items-center justify-between border-b border-[var(--border)] px-6 py-3">
        <div>
          <h1 className="text-sm font-medium text-[var(--text)]">Memory</h1>
          <p className="text-xs text-[var(--text-subtle)]">
            Read-only view of MEMORY.md and daily logs served by the daemon.
          </p>
        </div>
        <Button size="sm" variant="outline" onClick={() => void loadMemory()} disabled={loading}>
          <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} aria-hidden="true" />
          Refresh
        </Button>
      </header>

      <div className="flex-1 overflow-y-auto px-6 py-6">
        <div className="mx-auto grid max-w-4xl gap-6 lg:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle>Long-term memory (MEMORY.md)</CardTitle>
            </CardHeader>
            <CardContent>
              {longTerm ? (
                <MarkdownContent content={longTerm} />
              ) : (
                <p className="text-xs text-[var(--text-subtle)]">MEMORY.md is empty.</p>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Daily logs</CardTitle>
              {days.length > 0 && (
                <div
                  className="flex flex-wrap gap-1.5 pt-2"
                  role="tablist"
                  aria-label="Daily log dates"
                >
                  {days.map((day) => (
                    <button
                      key={day}
                      type="button"
                      role="tab"
                      aria-selected={selectedDay === day}
                      onClick={() => void loadDailyMemory(day)}
                      className={cn(
                        "rounded-md border px-2 py-1 font-mono text-[11px] transition-colors cursor-pointer",
                        selectedDay === day
                          ? "border-[var(--border-strong)] bg-[var(--accent)] text-[var(--accent-fg)]"
                          : "border-[var(--border)] bg-[var(--bg-subtle)] text-[var(--text-muted)] hover:text-[var(--text)]",
                      )}
                    >
                      {day}
                    </button>
                  ))}
                </div>
              )}
            </CardHeader>
            <CardContent>
              {days.length === 0 ? (
                <p className="text-xs text-[var(--text-subtle)]">No daily logs recorded yet.</p>
              ) : dailyContent ? (
                <MarkdownContent content={dailyContent} />
              ) : (
                <p className="text-xs text-[var(--text-subtle)]">Select a date to view its log.</p>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </section>
  );
}
