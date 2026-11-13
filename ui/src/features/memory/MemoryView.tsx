import {
  AlertTriangle,
  ArrowLeft,
  Calendar,
  Clock,
  FileText,
  History,
  Layers,
  RefreshCw,
} from "lucide-react";
import { useEffect } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { MarkdownContent } from "@/features/chat/MarkdownContent";
import { cn } from "@/lib/utils";
import { useCleaStore } from "@/protocol/store";

function formatLocalTime(isoStr?: string | null): string {
  if (!isoStr) return "";
  try {
    const d = new Date(isoStr);
    if (isNaN(d.getTime())) return isoStr;
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  } catch {
    return isoStr;
  }
}

interface ParsedEntry {
  id: string;
  date: string;
  source: string;
  text: string;
}

interface ParsedSection {
  title: string;
  entries: ParsedEntry[];
}

function parseMemorySections(content: string): ParsedSection[] {
  const sections: ParsedSection[] = [];
  const lines = content.split("\n");
  let current: ParsedSection | null = null;

  for (const line of lines) {
    if (line.startsWith("## ")) {
      if (current) sections.push(current);
      current = { title: line.slice(3).trim(), entries: [] };
    } else if (current) {
      const match = line.match(/^-\s*\[([^\]]+)\]\s*\(([^,]+),\s*([^)]+)\)\s*(.*)$/);
      if (match) {
        current.entries.push({
          id: match[1],
          date: match[2],
          source: match[3],
          text: match[4],
        });
      }
    }
  }
  if (current) sections.push(current);
  return sections;
}

export function MemoryView() {
  const longTerm = useCleaStore((s) => s.memoryLongTerm);
  const days = useCleaStore((s) => s.memoryDays);
  const selectedDay = useCleaStore((s) => s.selectedMemoryDay);
  const dailyContent = useCleaStore((s) => s.memoryDailyContent);
  const lastConsolidated = useCleaStore((s) => s.lastConsolidated);
  const failedConsolidation = useCleaStore((s) => s.failedConsolidation);
  const episodes = useCleaStore((s) => s.episodes);
  const transcripts = useCleaStore((s) => s.transcripts);
  const selectedTranscript = useCleaStore((s) => s.selectedTranscript);
  const subView = useCleaStore((s) => s.memorySubView);
  const loading = useCleaStore((s) => s.memoryLoading);

  const loadMemory = useCleaStore((s) => s.loadMemory);
  const loadDailyMemory = useCleaStore((s) => s.loadDailyMemory);
  const loadEpisodes = useCleaStore((s) => s.loadEpisodes);
  const loadTranscripts = useCleaStore((s) => s.loadTranscripts);
  const openTranscript = useCleaStore((s) => s.openTranscript);
  const setSubView = useCleaStore((s) => s.setMemorySubView);

  useEffect(() => {
    void loadMemory();
    void loadEpisodes();
    void loadTranscripts();
  }, [loadMemory, loadEpisodes, loadTranscripts]);

  const parsedSections = parseMemorySections(longTerm);
  const hasStructuredSections = parsedSections.length > 0;

  const handleRefresh = () => {
    void loadMemory();
    void loadEpisodes();
    void loadTranscripts();
  };

  return (
    <section aria-label="Memory" className="flex h-full flex-col">
      <header className="flex items-center justify-between border-b border-[var(--border)] px-6 py-3">
        <div className="flex items-center gap-3">
          {subView === "transcripts" && (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setSubView("overview")}
              aria-label="Back to Memory Overview"
            >
              <ArrowLeft className="h-4 w-4" />
            </Button>
          )}
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-sm font-medium text-[var(--text)]">
                {subView === "transcripts" ? "Session Transcripts" : "Durable Memory"}
              </h1>
              {lastConsolidated && (
                <span className="text-[11px] text-[var(--text-subtle)]">
                  · Last consolidated {formatLocalTime(lastConsolidated)}
                </span>
              )}
            </div>
            <p className="text-xs text-[var(--text-subtle)]">
              {subView === "transcripts"
                ? "Structured JSONL transcripts for all chat sessions."
                : "Curated facts, preferences, and episode summaries outside the repository."}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          {subView === "overview" ? (
            <Button size="sm" variant="outline" onClick={() => setSubView("transcripts")}>
              <History className="h-3.5 w-3.5" aria-hidden="true" />
              Transcripts ({transcripts.length})
            </Button>
          ) : (
            <Button size="sm" variant="outline" onClick={() => setSubView("overview")}>
              <Layers className="h-3.5 w-3.5" aria-hidden="true" />
              Memory Overview
            </Button>
          )}
          <Button size="sm" variant="outline" onClick={handleRefresh} disabled={loading}>
            <RefreshCw
              className={cn("h-3.5 w-3.5", loading && "animate-spin")}
              aria-hidden="true"
            />
            Refresh
          </Button>
        </div>
      </header>

      {failedConsolidation && (
        <div className="border-b border-red-500/20 bg-red-500/10 px-6 py-2 text-xs text-[var(--danger)] flex items-center gap-2">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
          <span>Consolidation warning: {failedConsolidation}</span>
        </div>
      )}

      <div className="flex-1 overflow-y-auto px-6 py-6">
        {subView === "transcripts" ? (
          <div className="mx-auto grid max-w-5xl gap-6 lg:grid-cols-3">
            {/* Left list: transcripts */}
            <div className="space-y-2 lg:col-span-1">
              <h2 className="text-xs font-semibold uppercase tracking-wider text-[var(--text-subtle)]">
                Sessions ({transcripts.length})
              </h2>
              {transcripts.length === 0 ? (
                <p className="text-xs text-[var(--text-subtle)]">No transcripts found.</p>
              ) : (
                transcripts.map((tr) => {
                  const isSelected = selectedTranscript?.session_id === tr.session_id;
                  return (
                    <button
                      key={tr.session_id}
                      type="button"
                      onClick={() => void openTranscript(tr.session_id)}
                      className={cn(
                        "w-full text-left rounded-lg border p-3 transition-colors cursor-pointer",
                        isSelected
                          ? "border-[var(--border-strong)] bg-[var(--accent)] text-[var(--accent-fg)]"
                          : "border-[var(--border)] bg-[var(--surface)] text-[var(--text)] hover:bg-[var(--bg-subtle)]",
                      )}
                    >
                      <div className="flex items-center justify-between text-xs">
                        <span className="font-mono">{tr.session_id.slice(0, 8)}</span>
                        <span className="text-[11px] opacity-70">
                          {formatLocalTime(tr.started_at)}
                        </span>
                      </div>
                      <p className="mt-1 line-clamp-1 text-xs font-medium">{tr.title}</p>
                      <span className="mt-1 inline-block text-[10px] opacity-60">
                        {tr.message_count} messages
                      </span>
                    </button>
                  );
                })
              )}
            </div>

            {/* Right: selected transcript messages */}
            <div className="lg:col-span-2">
              <Card className="border border-[var(--border)] min-h-[400px]">
                <CardHeader>
                  <CardTitle className="flex items-center justify-between text-sm">
                    <span>
                      {selectedTranscript
                        ? selectedTranscript.title
                        : "Select a transcript to inspect"}
                    </span>
                    {selectedTranscript && (
                      <span className="font-mono text-xs text-[var(--text-subtle)]">
                        {formatLocalTime(selectedTranscript.started_at)}
                      </span>
                    )}
                  </CardTitle>
                </CardHeader>
                <CardContent className="space-y-4">
                  {!selectedTranscript ? (
                    <div className="flex flex-col items-center justify-center py-20 text-center">
                      <FileText className="mb-2 h-6 w-6 text-[var(--text-subtle)]" />
                      <p className="text-xs text-[var(--text-muted)]">
                        Select a transcript from the list to view its message stream.
                      </p>
                    </div>
                  ) : selectedTranscript.messages.length === 0 ? (
                    <p className="text-xs text-[var(--text-subtle)]">
                      No messages recorded in this transcript.
                    </p>
                  ) : (
                    selectedTranscript.messages.map((m, idx) => (
                      <div
                        key={idx}
                        className="space-y-1 rounded-lg border border-[var(--border)] p-3 bg-[var(--bg-subtle)]"
                      >
                        <div className="flex items-center justify-between text-[11px] text-[var(--text-subtle)]">
                          <span className="font-semibold uppercase tracking-wider">{m.role}</span>
                          <span>{formatLocalTime(m.timestamp)}</span>
                        </div>
                        <p className="text-xs text-[var(--text)] whitespace-pre-wrap">{m.text}</p>
                        {m.tool_calls && m.tool_calls.length > 0 && (
                          <div className="mt-2 text-[10px] text-[var(--text-subtle)] bg-[var(--surface)] p-1.5 rounded">
                            Tools: {m.tool_calls.map((c) => c.name).join(", ")}
                          </div>
                        )}
                      </div>
                    ))
                  )}
                </CardContent>
              </Card>
            </div>
          </div>
        ) : (
          <div className="mx-auto grid max-w-5xl gap-6 lg:grid-cols-2">
            {/* Left: MEMORY.md parsed by section */}
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <h2 className="text-xs font-semibold uppercase tracking-wider text-[var(--text-subtle)]">
                  MEMORY.md (Durable Facts)
                </h2>
              </div>

              {!hasStructuredSections ? (
                <Card className="border border-[var(--border)]">
                  <CardContent className="p-4">
                    {longTerm ? (
                      <MarkdownContent content={longTerm} />
                    ) : (
                      <p className="text-xs text-[var(--text-subtle)]">MEMORY.md is empty.</p>
                    )}
                  </CardContent>
                </Card>
              ) : (
                parsedSections.map((sec) => (
                  <Card key={sec.title} className="border border-[var(--border)]">
                    <CardHeader className="py-3 px-4 border-b border-[var(--border)] bg-[var(--surface)]">
                      <div className="flex items-center justify-between">
                        <CardTitle className="text-xs font-semibold uppercase tracking-wider text-[var(--text)]">
                          {sec.title}
                        </CardTitle>
                        <Badge variant="neutral">{sec.entries.length}</Badge>
                      </div>
                    </CardHeader>
                    <CardContent className="p-4 space-y-2">
                      {sec.entries.length === 0 ? (
                        <p className="text-xs text-[var(--text-subtle)] italic">No entries yet.</p>
                      ) : (
                        sec.entries.map((entry) => (
                          <div
                            key={entry.id}
                            className="flex items-start justify-between gap-3 text-xs border-b border-[var(--border)] pb-2 last:border-b-0 last:pb-0"
                          >
                            <div className="space-y-0.5">
                              <p className="text-[var(--text)]">{entry.text}</p>
                              <div className="flex items-center gap-2 font-mono text-[10px] text-[var(--text-subtle)]">
                                <span>[{entry.id}]</span>
                                <span>{entry.date}</span>
                              </div>
                            </div>
                            <span
                              className={cn(
                                "shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium",
                                entry.source === "stated"
                                  ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                                  : entry.source === "confirmed"
                                    ? "bg-blue-500/10 text-blue-600 dark:text-blue-400"
                                    : "bg-amber-500/10 text-amber-600 dark:text-amber-400",
                              )}
                            >
                              {entry.source}
                            </span>
                          </div>
                        ))
                      )}
                    </CardContent>
                  </Card>
                ))
              )}
            </div>

            {/* Right: Recent Episode Summaries */}
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <h2 className="text-xs font-semibold uppercase tracking-wider text-[var(--text-subtle)]">
                  Recent Episode Summaries
                </h2>
                <button
                  type="button"
                  onClick={() => setSubView("transcripts")}
                  className="text-xs text-[var(--accent)] hover:underline flex items-center gap-1 cursor-pointer"
                >
                  View full transcripts →
                </button>
              </div>

              {episodes.length === 0 ? (
                <Card className="border border-[var(--border)]">
                  <CardContent className="flex flex-col items-center justify-center py-16 text-center">
                    <Clock className="mb-2 h-6 w-6 text-[var(--text-subtle)]" />
                    <p className="text-xs text-[var(--text-muted)]">
                      No episode summaries recorded yet.
                    </p>
                    <p className="mt-1 text-[11px] text-[var(--text-subtle)]">
                      Sessions are summarized into episodes automatically when idle or closed.
                    </p>
                  </CardContent>
                </Card>
              ) : (
                episodes.map((ep, idx) => (
                  <Card key={`${ep.session_id}-${idx}`} className="border border-[var(--border)]">
                    <CardHeader className="py-2.5 px-4 border-b border-[var(--border)] bg-[var(--surface)]">
                      <div className="flex items-center justify-between text-xs">
                        <div className="flex items-center gap-2">
                          <Calendar className="h-3.5 w-3.5 text-[var(--text-subtle)]" />
                          <span className="font-medium text-[var(--text)]">{ep.date}</span>
                          <span className="text-[var(--text-subtle)]">
                            {formatLocalTime(ep.time)}
                          </span>
                        </div>
                        <span className="font-mono text-[11px] text-[var(--text-subtle)]">
                          {ep.session_id.slice(0, 8)}
                        </span>
                      </div>
                    </CardHeader>
                    <CardContent className="p-4">
                      <p className="text-xs leading-relaxed text-[var(--text)] whitespace-pre-wrap">
                        {ep.summary}
                      </p>
                    </CardContent>
                  </Card>
                ))
              )}
              {/* Daily Logs if available */}
              {days.length > 0 && (
                <Card className="border border-[var(--border)]">
                  <CardHeader className="py-3 px-4 border-b border-[var(--border)] bg-[var(--surface)]">
                    <CardTitle className="text-xs font-semibold uppercase tracking-wider text-[var(--text)]">
                      Daily Logs
                    </CardTitle>
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
                  </CardHeader>
                  <CardContent className="p-4">
                    {dailyContent ? (
                      <MarkdownContent content={dailyContent} />
                    ) : (
                      <p className="text-xs text-[var(--text-subtle)]">
                        Select a date to view its log.
                      </p>
                    )}
                  </CardContent>
                </Card>
              )}
            </div>
          </div>
        )}
      </div>
    </section>
  );
}
