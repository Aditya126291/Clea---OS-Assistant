import {
  AlertTriangle,
  Archive as ArchiveIcon,
  ArrowLeft,
  Calendar,
  CheckCircle2,
  ChevronRight,
  Clock,
  FileText,
  Filter,
  History,
  Layers,
  RefreshCw,
  RotateCw,
  Search,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { MarkdownContent } from "@/features/chat/MarkdownContent";
import { cn } from "@/lib/utils";
import type { EpisodeItem, MemoryFactItem, MemorySectionItem } from "@/protocol/types";
import { useCleaStore } from "@/protocol/store";

export type MemoryTab = "facts" | "episodes" | "archive" | "status";

const FIXED_SECTIONS = [
  "Profile",
  "Preferences",
  "Environment",
  "Projects",
  "Decisions",
  "Open Threads",
];

const LOCAL_STORAGE_VIEW_KEY = "clea-memory-active-view";
const LOCAL_STORAGE_DISMISSED_WARNINGS_KEY = "clea-dismissed-consolidation-warnings";

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

function formatDate(isoStr?: string | null): string {
  if (!isoStr) return "";
  try {
    const d = new Date(isoStr);
    if (isNaN(d.getTime())) return isoStr;
    return d.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
  } catch {
    return isoStr;
  }
}

function sanitizeDisplayError(err?: string | null): string {
  if (!err) return "";
  let clean = err;
  clean = clean.replace(/[A-Za-z]:\\[^\s:,'"]+/g, "[path]");
  clean = clean.replace(/\/(?:Users|home|root|var|tmp|etc)\/[^\s:,'"]+/g, "[path]");
  clean = clean.replace(/AIza[0-9A-Za-z\-_]{35}/g, "[key]");
  clean = clean.replace(/sk-[a-zA-Z0-9]{20,}/g, "[key]");
  clean = clean.replace(/Bearer\s+[A-Za-z0-9\-_.]+/g, "Bearer [token]");
  clean = clean.replace(/\s+/g, " ").trim();
  if (clean.length > 250) {
    clean = clean.slice(0, 247) + "...";
  }
  return clean;
}

function parseMemorySections(content: string): MemorySectionItem[] {
  const sections: MemorySectionItem[] = [];
  const lines = content.split("\n");
  let current: MemorySectionItem | null = null;

  for (const line of lines) {
    if (line.startsWith("## ")) {
      if (current) sections.push(current);
      current = { name: line.slice(3).trim(), entries: [] };
    } else if (current) {
      const match = line.match(/^-\s*\[([^\]]+)\]\s*\(([^,]+),\s*([^)]+)\)\s*(.*)$/);
      if (match) {
        current.entries = current.entries ?? [];
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
  const memoryFacts = useCleaStore((s) => s.memoryFacts);
  const memoryArchive = useCleaStore((s) => s.memoryArchive);
  const memoryStatus = useCleaStore((s) => s.memoryStatus);
  const reconsolidating = useCleaStore((s) => s.reconsolidating);
  const transcripts = useCleaStore((s) => s.transcripts);
  const selectedTranscript = useCleaStore((s) => s.selectedTranscript);
  const subView = useCleaStore((s) => s.memorySubView);
  const loading = useCleaStore((s) => s.memoryLoading);

  const loadMemory = useCleaStore((s) => s.loadMemory);
  const loadDailyMemory = useCleaStore((s) => s.loadDailyMemory);
  const loadEpisodes = useCleaStore((s) => s.loadEpisodes);
  const loadMemoryFacts = useCleaStore((s) => s.loadMemoryFacts);
  const loadMemoryArchive = useCleaStore((s) => s.loadMemoryArchive);
  const loadMemoryStatus = useCleaStore((s) => s.loadMemoryStatus);
  const reconsolidate = useCleaStore((s) => s.reconsolidate);
  const forgetFact = useCleaStore((s) => s.forgetFact);
  const loadTranscripts = useCleaStore((s) => s.loadTranscripts);
  const openTranscript = useCleaStore((s) => s.openTranscript);
  const setSubView = useCleaStore((s) => s.setMemorySubView);

  // Tab selection with localStorage persistence
  const [activeTab, setActiveTab] = useState<MemoryTab>(() => {
    try {
      const saved = localStorage.getItem(LOCAL_STORAGE_VIEW_KEY);
      if (saved && ["facts", "episodes", "archive", "status"].includes(saved)) {
        return saved as MemoryTab;
      }
    } catch {
      // ignore
    }
    return "facts";
  });

  const handleTabChange = (newTab: MemoryTab) => {
    setActiveTab(newTab);
    try {
      localStorage.setItem(LOCAL_STORAGE_VIEW_KEY, newTab);
    } catch {
      // ignore
    }
  };

  // Facts search filter
  const [factsFilter, setFactsFilter] = useState("");

  // Per-fact forget confirmation dialog state
  const [factToForget, setFactToForget] = useState<MemoryFactItem | null>(null);
  const [isDeletingFact, setIsDeletingFact] = useState(false);

  // Episodes: show short sessions toggle
  const [showShortSessions, setShowShortSessions] = useState(false);

  // Collapsed sections in Facts view
  const [openSections, setOpenSections] = useState<Record<string, boolean>>({});

  // Collapsed days in Episodes view
  const [openDays, setOpenDays] = useState<Record<string, boolean>>({});

  const toggleDay = (date: string) => {
    setOpenDays((prev) => ({
      ...prev,
      [date]: prev[date] !== undefined ? !prev[date] : false,
    }));
  };

  // Banner state: details expanded & dismissed errors
  const [bannerDetailsOpen, setBannerDetailsOpen] = useState(false);
  const [dismissedWarnings, setDismissedWarnings] = useState<string[]>(() => {
    try {
      const saved = localStorage.getItem(LOCAL_STORAGE_DISMISSED_WARNINGS_KEY);
      return saved ? JSON.parse(saved) : [];
    } catch {
      return [];
    }
  });

  useEffect(() => {
    void loadMemory();
    void loadEpisodes();
    void loadMemoryFacts();
    void loadMemoryArchive();
    void loadMemoryStatus();
    void loadTranscripts();
  }, [
    loadMemory,
    loadEpisodes,
    loadMemoryFacts,
    loadMemoryArchive,
    loadMemoryStatus,
    loadTranscripts,
  ]);

  const handleRefresh = () => {
    void loadMemory();
    void loadEpisodes();
    void loadMemoryFacts();
    void loadMemoryArchive();
    void loadMemoryStatus();
    void loadTranscripts();
  };

  // Identify unresolved consolidation error for banner
  const activeWarning = useMemo(() => {
    if (failedConsolidation) {
      // Extract session id if present: e.g. "Session 67727ff3: ..."
      const match = failedConsolidation.match(/Session\s+([0-9a-zA-Z\-_]+)[:\s]+(.*)/i);
      const sessionId = match ? match[1] : undefined;
      const details = match ? match[2] : failedConsolidation;
      return {
        key: failedConsolidation,
        sessionId,
        details: sanitizeDisplayError(details),
      };
    }
    if (memoryStatus?.failed && memoryStatus.failed.length > 0) {
      const f = memoryStatus.failed[0];
      return {
        key: `failed-${f.session_id}-${f.error}`,
        sessionId: f.session_id,
        details: sanitizeDisplayError(f.error ?? "Consolidation failed"),
      };
    }
    if (memoryStatus?.pending && memoryStatus.pending.length > 0) {
      const p = memoryStatus.pending[0];
      return {
        key: `pending-${p.session_id}-${p.error}`,
        sessionId: p.session_id,
        details: sanitizeDisplayError(p.error ?? "Consolidation retry pending"),
      };
    }
    return null;
  }, [failedConsolidation, memoryStatus]);

  const isWarningDismissed = activeWarning && dismissedWarnings.includes(activeWarning.key);

  const handleDismissWarning = () => {
    if (!activeWarning) return;
    const next = [...dismissedWarnings, activeWarning.key];
    setDismissedWarnings(next);
    try {
      localStorage.setItem(LOCAL_STORAGE_DISMISSED_WARNINGS_KEY, JSON.stringify(next));
    } catch {
      // ignore
    }
  };

  const handleBannerRetry = async () => {
    if (!activeWarning) return;
    const sid =
      activeWarning.sessionId ||
      memoryStatus?.pending?.[0]?.session_id ||
      memoryStatus?.failed?.[0]?.session_id;
    if (sid) {
      await reconsolidate(sid);
    } else {
      handleRefresh();
    }
  };

  // Prepare Durable Facts with fixed sections
  const structuredSections = useMemo(() => {
    const rawParsed = parseMemorySections(longTerm);
    const serverFacts = memoryFacts && memoryFacts.length > 0 ? memoryFacts : rawParsed;

    const map = new Map<string, MemoryFactItem[]>();
    for (const sec of FIXED_SECTIONS) {
      map.set(sec, []);
    }
    for (const s of serverFacts) {
      const existing = map.get(s.name) ?? [];
      const incoming = s.entries ?? [];
      map.set(s.name, [...existing, ...incoming]);
    }

    return FIXED_SECTIONS.map((secName) => {
      const allEntries = map.get(secName) ?? [];
      const filtered = factsFilter.trim()
        ? allEntries.filter((e) => e.text.toLowerCase().includes(factsFilter.toLowerCase()))
        : allEntries;
      return {
        name: secName,
        allCount: allEntries.length,
        entries: filtered,
      };
    });
  }, [memoryFacts, longTerm, factsFilter]);

  const totalFactsCount = useMemo(
    () => structuredSections.reduce((acc, s) => acc + s.allCount, 0),
    [structuredSections],
  );

  const totalFilteredCount = useMemo(
    () => structuredSections.reduce((acc, s) => acc + s.entries.length, 0),
    [structuredSections],
  );

  // Toggle section open/close
  const toggleSection = (secName: string) => {
    setOpenSections((prev) => ({
      ...prev,
      [secName]: prev[secName] !== undefined ? !prev[secName] : false,
    }));
  };

  // Group episodes by date, newest first
  const groupedEpisodes = useMemo(() => {
    const filtered = showShortSessions
      ? episodes
      : episodes.filter((ep) => !ep.trivial && !ep.summary.startsWith("Short session,"));

    const groups: Record<string, EpisodeItem[]> = {};
    for (const ep of filtered) {
      const dateKey = ep.date || "Unknown Date";
      if (!groups[dateKey]) {
        groups[dateKey] = [];
      }
      groups[dateKey].push(ep);
    }

    // Sort dates descending
    const sortedDates = Object.keys(groups).sort((a, b) => b.localeCompare(a));
    return sortedDates.map((date) => ({
      date,
      episodes: groups[date],
    }));
  }, [episodes, showShortSessions]);

  return (
    <section aria-label="Memory" className="flex h-full flex-col">
      {/* Header */}
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border)] px-6 py-3">
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
                {subView === "transcripts" ? "Session Transcripts" : "Memory"}
              </h1>
              {(lastConsolidated || memoryStatus?.last_consolidated_at) && (
                <span className="text-[11px] text-[var(--text-subtle)]">
                  · Last consolidated{" "}
                  {formatLocalTime(lastConsolidated || memoryStatus?.last_consolidated_at)}
                </span>
              )}
            </div>
            <p className="text-xs text-[var(--text-subtle)]">
              {subView === "transcripts"
                ? "Raw message streams and tool executions across sessions."
                : "Durable facts, episode summaries, and consolidation status."}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          {subView === "overview" && (
            <div className="flex items-center gap-2 mr-2">
              <span
                id="memory-view-select-label"
                className="text-xs font-medium text-[var(--text-subtle)]"
              >
                View
              </span>
              <Select
                value={activeTab}
                onValueChange={(val) => handleTabChange(val as MemoryTab)}
                aria-labelledby="memory-view-select-label"
              >
                <SelectTrigger className="w-[175px] h-8 text-xs">
                  <SelectValue placeholder="Select view" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="facts">Durable facts</SelectItem>
                  <SelectItem value="episodes">Episode summaries</SelectItem>
                  <SelectItem value="archive">Archive</SelectItem>
                  <SelectItem value="status">Consolidation status</SelectItem>
                </SelectContent>
              </Select>
            </div>
          )}

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

      {/* Part B: Amber Recoverable Warning Banner */}
      {activeWarning && !isWarningDismissed && (
        <div
          role="alert"
          className="border-b border-amber-500/30 bg-amber-500/10 px-6 py-2.5 text-xs text-amber-900 dark:text-amber-200"
        >
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-2 min-w-0">
              <AlertTriangle className="h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
              <span className="font-medium">Couldn't update memory from one conversation.</span>
              <button
                type="button"
                onClick={() => setBannerDetailsOpen((prev) => !prev)}
                className="text-[11px] underline opacity-80 hover:opacity-100 cursor-pointer ml-1"
              >
                {bannerDetailsOpen ? "Hide details" : "Details"}
              </button>
            </div>

            <div className="flex items-center gap-2 shrink-0">
              <Button
                size="sm"
                variant="outline"
                onClick={handleBannerRetry}
                disabled={
                  activeWarning.sessionId ? !!reconsolidating[activeWarning.sessionId] : false
                }
                className="h-7 px-2.5 text-xs border-amber-500/40 hover:bg-amber-500/20"
              >
                <RotateCw
                  className={cn(
                    "h-3 w-3 mr-1",
                    activeWarning.sessionId &&
                      reconsolidating[activeWarning.sessionId] &&
                      "animate-spin",
                  )}
                />
                Retry
              </Button>
              <button
                type="button"
                onClick={handleDismissWarning}
                className="rounded p-1 hover:bg-amber-500/20 cursor-pointer text-amber-800 dark:text-amber-300"
                aria-label="Dismiss warning"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>

          {bannerDetailsOpen && (
            <div className="mt-2 rounded bg-amber-500/10 p-2 font-mono text-[11px] text-amber-950 dark:text-amber-100 border border-amber-500/20 whitespace-pre-wrap break-all">
              {activeWarning.sessionId && (
                <div className="mb-1 font-semibold">Session: {activeWarning.sessionId}</div>
              )}
              <div>{activeWarning.details}</div>
            </div>
          )}
        </div>
      )}

      {/* Main Content Area */}
      <div className="flex-1 overflow-y-auto px-6 py-6">
        {subView === "transcripts" ? (
          /* Transcripts Subview */
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
        ) : activeTab === "facts" ? (
          /* View 1: Durable Facts */
          <div className="mx-auto max-w-4xl space-y-5">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
              <div>
                <h2 className="text-xs font-semibold uppercase tracking-wider text-[var(--text-subtle)]">
                  MEMORY.md · Durable Facts
                </h2>
                <p className="text-xs text-[var(--text-subtle)]">
                  Long-term preferences, profile, and project knowledge maintained across sessions.
                </p>
              </div>

              {/* Filter search box */}
              <div className="relative w-full sm:w-64">
                <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-[var(--text-subtle)]" />
                <input
                  type="text"
                  placeholder="Search facts..."
                  value={factsFilter}
                  onChange={(e) => setFactsFilter(e.target.value)}
                  className="w-full rounded-md border border-[var(--border)] bg-[var(--surface)] pl-8 pr-3 py-1.5 text-xs text-[var(--text)] placeholder:text-[var(--text-subtle)] focus:outline-none focus:ring-1 focus:ring-[var(--focus)]"
                />
              </div>
            </div>

            {totalFactsCount === 0 && !longTerm ? (
              <Card className="border border-[var(--border)]">
                <CardContent className="flex flex-col items-center justify-center py-16 text-center">
                  <Sparkles className="mb-2 h-6 w-6 text-[var(--text-subtle)]" />
                  <p className="text-xs font-medium text-[var(--text-muted)]">
                    Nothing remembered yet. Tell Clea &apos;remember that ...&apos;
                  </p>
                  <p className="mt-1 text-[11px] text-[var(--text-subtle)]">
                    Clea remembers key details from conversations automatically when consolidated.
                  </p>
                </CardContent>
              </Card>
            ) : totalFilteredCount === 0 && factsFilter.trim() ? (
              <Card className="border border-[var(--border)]">
                <CardContent className="flex flex-col items-center justify-center py-12 text-center">
                  <Filter className="mb-2 h-5 w-5 text-[var(--text-subtle)]" />
                  <p className="text-xs font-medium text-[var(--text-muted)]">
                    No facts match &quot;{factsFilter}&quot;
                  </p>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => setFactsFilter("")}
                    className="mt-2 text-xs"
                  >
                    Clear filter
                  </Button>
                </CardContent>
              </Card>
            ) : (
              <div className="space-y-3">
                {structuredSections.map((sec) => {
                  const hasEntries = sec.entries.length > 0;
                  const isOpen =
                    openSections[sec.name] !== undefined ? openSections[sec.name] : hasEntries; // Default collapsed if empty, open if has facts

                  return (
                    <Card key={sec.name} className="border border-[var(--border)]">
                      <button
                        type="button"
                        aria-expanded={isOpen}
                        onClick={() => toggleSection(sec.name)}
                        className="w-full flex items-center justify-between px-4 py-2.5 text-left border-b border-[var(--border)] bg-[var(--surface)] hover:bg-[var(--bg-subtle)] rounded-t-lg transition-colors cursor-pointer"
                      >
                        <div className="flex items-center gap-2">
                          <ChevronRight
                            className={cn(
                              "h-3.5 w-3.5 text-[var(--text-subtle)] transition-transform",
                              isOpen && "rotate-90",
                            )}
                          />
                          <span className="text-xs font-semibold uppercase tracking-wider text-[var(--text)]">
                            {sec.name}
                          </span>
                        </div>
                        <Badge variant={hasEntries ? "neutral" : "neutral"} className="font-mono">
                          {sec.entries.length}
                        </Badge>
                      </button>

                      {isOpen && (
                        <CardContent className="p-4 space-y-2.5">
                          {sec.entries.length === 0 ? (
                            <p className="text-xs text-[var(--text-subtle)] italic">
                              No entries in this section.
                            </p>
                          ) : (
                            sec.entries.map((entry) => (
                              <div
                                key={entry.id}
                                className="flex items-start justify-between gap-3 text-xs border-b border-[var(--border)] pb-2.5 last:border-b-0 last:pb-0"
                              >
                                <div className="space-y-1">
                                  <p className="text-[var(--text)] leading-relaxed">{entry.text}</p>
                                  <div className="flex items-center gap-2 font-mono text-[10px] text-[var(--text-subtle)]">
                                    <span>[{entry.id}]</span>
                                    <span>{entry.date}</span>
                                  </div>
                                </div>
                                <div className="flex items-center gap-1.5 shrink-0">
                                  <span
                                    className={cn(
                                      "rounded px-1.5 py-0.5 text-[10px] font-medium tracking-wide uppercase",
                                      entry.source === "stated"
                                        ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border border-emerald-500/20"
                                        : entry.source === "confirmed"
                                          ? "bg-blue-500/10 text-blue-600 dark:text-blue-400 border border-blue-500/20"
                                          : "bg-amber-500/10 text-amber-600 dark:text-amber-400 border border-amber-500/20",
                                    )}
                                  >
                                    {entry.source.startsWith("inferred") ? "inferred" : entry.source}
                                  </span>
                                  <button
                                    type="button"
                                    onClick={() => setFactToForget(entry)}
                                    className="p-1 text-[var(--text-subtle)] hover:text-red-500 hover:bg-red-500/10 rounded transition-colors cursor-pointer"
                                    aria-label={`Forget fact: ${entry.text}`}
                                    title="Forget this fact"
                                  >
                                    <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                                  </button>
                                </div>
                              </div>
                            ))
                          )}
                        </CardContent>
                      )}
                    </Card>
                  );
                })}
              </div>
            )}

            {/* Unstructured long term fallback if present */}
            {longTerm && totalFactsCount === 0 && (
              <Card className="border border-[var(--border)]">
                <CardHeader>
                  <CardTitle className="text-xs font-semibold uppercase tracking-wider">
                    Raw Memory Content
                  </CardTitle>
                </CardHeader>
                <CardContent className="p-4">
                  <MarkdownContent content={longTerm} />
                </CardContent>
              </Card>
            )}

            {/* Daily Logs if available */}
            {days.length > 0 && (
              <Card className="border border-[var(--border)] mt-6">
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
        ) : activeTab === "episodes" ? (
          /* View 2: Episode Summaries */
          <div className="mx-auto max-w-4xl space-y-5">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
              <div>
                <h2 className="text-xs font-semibold uppercase tracking-wider text-[var(--text-subtle)]">
                  Episode Summaries
                </h2>
                <p className="text-xs text-[var(--text-subtle)]">
                  Chronological memory episodes grouped by date, newest first.
                </p>
              </div>

              {/* Show short sessions toggle */}
              <label className="flex items-center gap-2 text-xs text-[var(--text-subtle)] cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={showShortSessions}
                  onChange={(e) => setShowShortSessions(e.target.checked)}
                  className="rounded border-[var(--border)] text-[var(--accent)] focus:ring-[var(--focus)]"
                />
                <span>Show short sessions</span>
              </label>
            </div>

            {groupedEpisodes.length === 0 ? (
              <Card className="border border-[var(--border)]">
                <CardContent className="flex flex-col items-center justify-center py-16 text-center">
                  <Clock className="mb-2 h-6 w-6 text-[var(--text-subtle)]" />
                  <p className="text-xs text-[var(--text-muted)]">
                    No episode summaries recorded yet.
                  </p>
                  <p className="mt-1 text-[11px] text-[var(--text-subtle)]">
                    Sessions are consolidated into episodes automatically when idle or closed.
                  </p>
                </CardContent>
              </Card>
            ) : (
              <div className="space-y-4">
                {groupedEpisodes.map((group) => {
                  const isDayOpen =
                    openDays[group.date] !== undefined ? openDays[group.date] : true;
                  return (
                    <Card key={group.date} className="border border-[var(--border)]">
                      <button
                        type="button"
                        aria-expanded={isDayOpen}
                        onClick={() => toggleDay(group.date)}
                        className="w-full flex items-center justify-between py-2.5 px-4 text-left border-b border-[var(--border)] bg-[var(--surface)] hover:bg-[var(--bg-subtle)] rounded-t-lg transition-colors cursor-pointer"
                      >
                        <div className="flex items-center gap-2">
                          <ChevronRight
                            className={cn(
                              "h-3.5 w-3.5 text-[var(--text-subtle)] transition-transform",
                              isDayOpen && "rotate-90",
                            )}
                          />
                          <Calendar className="h-3.5 w-3.5 text-[var(--text-subtle)]" />
                          <span className="text-xs font-semibold text-[var(--text)] font-mono">
                            {group.date}
                          </span>
                        </div>
                        <Badge variant="neutral">{group.episodes.length} episodes</Badge>
                      </button>

                      {isDayOpen && (
                        <CardContent className="p-4 space-y-3">
                          {group.episodes.map((ep, idx) => (
                            <div
                              key={`${ep.session_id}-${idx}`}
                              className="rounded-lg border border-[var(--border)] p-3 bg-[var(--bg-subtle)] space-y-2"
                            >
                              <div className="flex items-center justify-between text-xs">
                                <div className="flex items-center gap-2">
                                  <span className="font-mono text-[11px] text-[var(--text-subtle)] font-medium">
                                    {ep.session_id.slice(0, 8)}
                                  </span>
                                  <span className="text-[11px] text-[var(--text-subtle)]">
                                    {formatLocalTime(ep.time)}
                                  </span>
                                  {ep.trivial && (
                                    <Badge variant="neutral" className="text-[10px] px-1.5 py-0">
                                      Short session
                                    </Badge>
                                  )}
                                </div>

                                <button
                                  type="button"
                                  onClick={() => {
                                    void openTranscript(ep.session_id);
                                    setSubView("transcripts");
                                  }}
                                  className="text-[11px] text-[var(--accent)] hover:underline flex items-center gap-1 cursor-pointer"
                                >
                                  <span>View transcript</span>
                                  <ChevronRight className="h-3 w-3" />
                                </button>
                              </div>

                              <p className="text-xs leading-relaxed text-[var(--text)] whitespace-pre-wrap">
                                {ep.summary}
                              </p>
                            </div>
                          ))}
                        </CardContent>
                      )}
                    </Card>
                  );
                })}
              </div>
            )}
          </div>
        ) : activeTab === "archive" ? (
          /* View 3: Archive */
          <div className="mx-auto max-w-4xl space-y-5">
            <div>
              <h2 className="text-xs font-semibold uppercase tracking-wider text-[var(--text-subtle)]">
                MEMORY_ARCHIVE.md · Archived Facts
              </h2>
              <p className="text-xs text-[var(--text-subtle)]">
                Facts and preferences that were replaced, superseded, or explicitly forgotten.
              </p>
            </div>

            {memoryArchive.length === 0 ? (
              <Card className="border border-[var(--border)]">
                <CardContent className="flex flex-col items-center justify-center py-16 text-center">
                  <ArchiveIcon className="mb-2 h-6 w-6 text-[var(--text-subtle)]" />
                  <p className="text-xs text-[var(--text-muted)]">No archived memories.</p>
                  <p className="mt-1 text-[11px] text-[var(--text-subtle)]">
                    Items forgotten or replaced during consolidation are archived safely here.
                  </p>
                </CardContent>
              </Card>
            ) : (
              <Card className="border border-[var(--border)]">
                <CardContent className="p-4 space-y-3">
                  {memoryArchive.map((item, idx) => (
                    <div
                      key={item.id || idx}
                      className="rounded-lg border border-[var(--border)] p-3 bg-[var(--bg-subtle)] space-y-2 text-xs"
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="space-y-1">
                          <p className="text-[var(--text)] leading-relaxed">{item.text}</p>
                          <div className="flex flex-wrap items-center gap-2 font-mono text-[10px] text-[var(--text-subtle)]">
                            {item.id && <span>[{item.id}]</span>}
                            {item.date && <span>Created: {item.date}</span>}
                            {item.archived_at && (
                              <span>Archived: {formatDate(item.archived_at)}</span>
                            )}
                          </div>
                        </div>

                        {item.source && (
                          <span className="shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium tracking-wide uppercase bg-[var(--surface)] border border-[var(--border)] text-[var(--text-subtle)]">
                            {item.source}
                          </span>
                        )}
                      </div>

                      {item.reason && (
                        <div className="rounded bg-[var(--surface)] px-2.5 py-1.5 text-[11px] text-[var(--text-subtle)] border border-[var(--border)]">
                          <span className="font-semibold text-[var(--text)]">Reason: </span>
                          {item.reason}
                        </div>
                      )}
                    </div>
                  ))}
                </CardContent>
              </Card>
            )}
          </div>
        ) : (
          /* View 4: Consolidation Status */
          <div className="mx-auto max-w-4xl space-y-6">
            <div>
              <h2 className="text-xs font-semibold uppercase tracking-wider text-[var(--text-subtle)]">
                Consolidation Engine Status
              </h2>
              <p className="text-xs text-[var(--text-subtle)]">
                Session consolidation progress, scheduled retries, and failed jobs.
              </p>
            </div>

            {/* Metrics cards */}
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
              <Card className="border border-[var(--border)] p-3">
                <div className="text-[11px] text-[var(--text-subtle)] font-medium">
                  Last Successful Run
                </div>
                <div className="mt-1 font-mono text-sm font-semibold text-[var(--text)]">
                  {memoryStatus?.last_consolidated_at
                    ? formatLocalTime(memoryStatus.last_consolidated_at)
                    : lastConsolidated
                      ? formatLocalTime(lastConsolidated)
                      : "Never"}
                </div>
              </Card>

              <Card className="border border-[var(--border)] p-3">
                <div className="text-[11px] text-[var(--text-subtle)] font-medium">
                  Completed Sessions
                </div>
                <div className="mt-1 font-mono text-sm font-semibold text-emerald-600 dark:text-emerald-400">
                  {memoryStatus?.completed_count ?? 0}
                </div>
              </Card>

              <Card className="border border-[var(--border)] p-3">
                <div className="text-[11px] text-[var(--text-subtle)] font-medium">
                  Pending Retries
                </div>
                <div
                  className={cn(
                    "mt-1 font-mono text-sm font-semibold",
                    (memoryStatus?.pending?.length ?? 0) > 0
                      ? "text-amber-600 dark:text-amber-400"
                      : "text-[var(--text)]",
                  )}
                >
                  {memoryStatus?.pending?.length ?? 0}
                </div>
              </Card>

              <Card className="border border-[var(--border)] p-3">
                <div className="text-[11px] text-[var(--text-subtle)] font-medium">
                  Failed Sessions
                </div>
                <div
                  className={cn(
                    "mt-1 font-mono text-sm font-semibold",
                    (memoryStatus?.failed?.length ?? 0) > 0
                      ? "text-red-600 dark:text-red-400"
                      : "text-[var(--text)]",
                  )}
                >
                  {memoryStatus?.failed?.length ?? 0}
                </div>
              </Card>
            </div>

            {/* Pending sessions list */}
            {memoryStatus?.pending && memoryStatus.pending.length > 0 && (
              <div className="space-y-3">
                <div className="flex items-center gap-2">
                  <Clock className="h-4 w-4 text-amber-500" />
                  <h3 className="text-xs font-semibold uppercase tracking-wider text-[var(--text)]">
                    Pending Retries ({memoryStatus.pending.length})
                  </h3>
                </div>

                <div className="space-y-2">
                  {memoryStatus.pending.map((session) => (
                    <Card
                      key={session.session_id}
                      className="border border-amber-500/30 bg-amber-500/5 p-4"
                    >
                      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 text-xs">
                        <div className="space-y-1">
                          <div className="flex items-center gap-2">
                            <span className="font-mono font-semibold text-[var(--text)]">
                              {session.session_id}
                            </span>
                            <Badge variant="warning">Attempt {session.retry_count} / 3</Badge>
                          </div>
                          {session.next_retry_at && (
                            <p className="text-[11px] text-[var(--text-subtle)]">
                              Next automatic retry at: {formatLocalTime(session.next_retry_at)}
                            </p>
                          )}
                          {session.error && (
                            <p className="font-mono text-[11px] text-amber-900 dark:text-amber-200 mt-1 whitespace-pre-wrap">
                              {sanitizeDisplayError(session.error)}
                            </p>
                          )}
                        </div>

                        <Button
                          size="sm"
                          variant="outline"
                          disabled={!!reconsolidating[session.session_id]}
                          onClick={() => void reconsolidate(session.session_id)}
                          className="shrink-0"
                        >
                          <RotateCw
                            className={cn(
                              "h-3 w-3 mr-1.5",
                              reconsolidating[session.session_id] && "animate-spin",
                            )}
                          />
                          Retry Now
                        </Button>
                      </div>
                    </Card>
                  ))}
                </div>
              </div>
            )}

            {/* Failed sessions list */}
            {memoryStatus?.failed && memoryStatus.failed.length > 0 && (
              <div className="space-y-3">
                <div className="flex items-center gap-2">
                  <AlertTriangle className="h-4 w-4 text-red-500" />
                  <h3 className="text-xs font-semibold uppercase tracking-wider text-[var(--text)]">
                    Failed Sessions ({memoryStatus.failed.length})
                  </h3>
                </div>

                <div className="space-y-2">
                  {memoryStatus.failed.map((session) => (
                    <Card
                      key={session.session_id}
                      className="border border-red-500/30 bg-red-500/5 p-4"
                    >
                      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 text-xs">
                        <div className="space-y-1">
                          <div className="flex items-center gap-2">
                            <span className="font-mono font-semibold text-[var(--text)]">
                              {session.session_id}
                            </span>
                            <Badge variant="danger">
                              Failed (cap {session.retry_count}/3 reached)
                            </Badge>
                          </div>
                          {session.last_attempt_at && (
                            <p className="text-[11px] text-[var(--text-subtle)]">
                              Last attempted: {formatLocalTime(session.last_attempt_at)}
                            </p>
                          )}
                          {session.error && (
                            <p className="font-mono text-[11px] text-red-900 dark:text-red-200 mt-1 whitespace-pre-wrap">
                              {sanitizeDisplayError(session.error)}
                            </p>
                          )}
                        </div>

                        <Button
                          size="sm"
                          variant="outline"
                          disabled={!!reconsolidating[session.session_id]}
                          onClick={() => void reconsolidate(session.session_id)}
                          className="shrink-0"
                        >
                          <RotateCw
                            className={cn(
                              "h-3 w-3 mr-1.5",
                              reconsolidating[session.session_id] && "animate-spin",
                            )}
                          />
                          Manual Retry
                        </Button>
                      </div>
                    </Card>
                  ))}
                </div>
              </div>
            )}

            {/* All clear state */}
            {(!memoryStatus?.pending || memoryStatus.pending.length === 0) &&
              (!memoryStatus?.failed || memoryStatus.failed.length === 0) && (
                <Card className="border border-[var(--border)] p-6 text-center">
                  <div className="flex flex-col items-center justify-center">
                    <CheckCircle2 className="mb-2 h-7 w-7 text-emerald-500" />
                    <p className="text-xs font-medium text-[var(--text)]">
                      All sessions consolidated successfully
                    </p>
                    <p className="mt-1 text-[11px] text-[var(--text-subtle)]">
                      No pending retries or failed consolidation jobs. Background consolidation is
                      healthy.
                    </p>
                  </div>
                </Card>
              )}
          </div>
        )}
      </div>

      {/* Confirmation dialog for forgetting a fact */}
      <Dialog
        open={factToForget !== null}
        onOpenChange={(open) => {
          if (!open && !isDeletingFact) {
            setFactToForget(null);
          }
        }}
      >
        <DialogContent aria-label="Confirm forgetting fact" className="p-5 max-w-md">
          <DialogTitle className="text-sm font-semibold text-[var(--text)]">
            Forget this fact?
          </DialogTitle>
          <DialogDescription className="text-xs text-[var(--text-subtle)] mt-1.5">
            This will permanently remove this fact from Clea&apos;s long-term memory.
          </DialogDescription>

          {factToForget && (
            <div className="mt-4 rounded-md border border-[var(--border)] bg-[var(--bg-subtle)] p-3">
              <p className="text-xs text-[var(--text)] leading-relaxed italic font-medium">
                &ldquo;{factToForget.text}&rdquo;
              </p>
              <div className="mt-2 flex items-center gap-2 font-mono text-[10px] text-[var(--text-subtle)]">
                <span>[{factToForget.id}]</span>
                <span>{factToForget.date}</span>
              </div>
            </div>
          )}

          <div className="mt-5 flex items-center justify-end gap-2">
            <Button
              size="sm"
              variant="ghost"
              disabled={isDeletingFact}
              onClick={() => setFactToForget(null)}
            >
              Cancel
            </Button>
            <Button
              size="sm"
              variant="destructive"
              disabled={isDeletingFact}
              onClick={async () => {
                if (!factToForget) return;
                setIsDeletingFact(true);
                try {
                  await forgetFact(factToForget.id, factToForget.text);
                } finally {
                  setIsDeletingFact(false);
                  setFactToForget(null);
                }
              }}
            >
              {isDeletingFact ? "Forgetting..." : "Forget fact"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </section>
  );
}
