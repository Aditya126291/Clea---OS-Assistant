import {
  Activity,
  AudioLines,
  BookOpen,
  Command,
  MessageSquare,
  MessageSquarePlus,
  Monitor,
  Moon,
  Settings,
  Sun,
} from "lucide-react";
import { useMemo } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { useTheme } from "@/lib/theme-context";
import { cn } from "@/lib/utils";
import { useCleaStore } from "@/protocol/store";
import type { AppView, ConversationSummary } from "@/protocol/types";

const NAV_ITEMS: { id: AppView; label: string; icon: typeof MessageSquare }[] = [
  { id: "chat", label: "Chat", icon: MessageSquare },
  { id: "activity", label: "Activity", icon: Activity },
  { id: "memory", label: "Memory", icon: BookOpen },
  { id: "settings", label: "Settings", icon: Settings },
];

export function Sidebar() {
  const activeView = useCleaStore((s) => s.activeView);
  const setActiveView = useCleaStore((s) => s.setActiveView);
  const conversations = useCleaStore((s) => s.conversations);
  const selectedConversation = useCleaStore((s) => s.selectedConversation);
  const openConversation = useCleaStore((s) => s.openConversation);
  const newSession = useCleaStore((s) => s.newSession);
  const tasks = useCleaStore((s) => s.tasks);
  const callOverlayOpen = useCleaStore((s) => s.callOverlayOpen);
  const setCallOverlayOpen = useCleaStore((s) => s.setCallOverlayOpen);
  const setCommandPaletteOpen = useCleaStore((s) => s.setCommandPaletteOpen);

  const { preference, cycleTheme } = useTheme();

  const runningTaskCount = tasks.filter((t) => t.status === "running").length;

  const grouped = useMemo(() => {
    const groups = new Map<string, ConversationSummary[]>();
    for (const item of conversations) {
      const list = groups.get(item.date) ?? [];
      list.push(item);
      groups.set(item.date, list);
    }
    return Array.from(groups.entries());
  }, [conversations]);

  const ThemeIcon = preference === "dark" ? Moon : preference === "light" ? Sun : Monitor;

  return (
    <aside
      aria-label="Sidebar"
      className="flex h-full w-64 shrink-0 flex-col border-r border-[var(--border)] bg-[var(--bg-subtle)]"
    >
      <div className="flex items-center justify-between px-4 py-3.5">
        <div className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-full bg-[var(--success)]" aria-hidden="true" />
          <span className="text-sm font-semibold tracking-tight text-[var(--text)]">Clea</span>
        </div>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => setCommandPaletteOpen(true)}
          aria-label="Open command palette"
          title="Command Palette (Ctrl+K)"
          className="h-7 gap-1 px-2 text-[11px]"
        >
          <Command className="h-3 w-3" aria-hidden="true" />
          <span>K</span>
        </Button>
      </div>

      <div className="px-3 pb-2">
        <Button
          variant="outline"
          size="sm"
          className="w-full justify-start bg-[var(--surface)]"
          onClick={() => void newSession()}
        >
          <MessageSquarePlus className="h-3.5 w-3.5" aria-hidden="true" />
          New chat
        </Button>
      </div>

      <nav aria-label="Main sections" className="space-y-0.5 px-3 py-1">
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon;
          const active = activeView === item.id && !(item.id === "chat" && selectedConversation);
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => setActiveView(item.id)}
              aria-current={active ? "page" : undefined}
              className={cn(
                "flex w-full items-center justify-between rounded-md px-2.5 py-1.5 text-sm transition-colors cursor-pointer",
                active
                  ? "bg-[var(--surface)] font-medium text-[var(--text)] shadow-xs"
                  : "text-[var(--text-muted)] hover:bg-[var(--surface)]/60 hover:text-[var(--text)]",
              )}
            >
              <span className="flex items-center gap-2.5">
                <Icon className="h-4 w-4" aria-hidden="true" />
                <span>{item.label}</span>
              </span>
              {item.id === "activity" && runningTaskCount > 0 && (
                <Badge variant="warning">{runningTaskCount}</Badge>
              )}
            </button>
          );
        })}
      </nav>

      <Separator className="my-2" />

      <div className="flex-1 overflow-y-auto px-3 py-1">
        <div className="px-2 pb-1.5 text-[11px] font-medium uppercase tracking-wider text-[var(--text-subtle)]">
          History
        </div>
        {grouped.length === 0 ? (
          <p className="px-2 py-2 text-xs text-[var(--text-subtle)]">
            No logged conversations yet.
          </p>
        ) : (
          <div className="space-y-3">
            {grouped.map(([date, items]) => (
              <div key={date} className="space-y-0.5">
                <div className="px-2 py-0.5 text-[11px] font-medium text-[var(--text-subtle)]">
                  {date}
                </div>
                {items.map((conv) => {
                  const selected =
                    selectedConversation?.date === conv.date &&
                    selectedConversation?.sessionId === conv.session_id &&
                    activeView === "chat";
                  return (
                    <button
                      key={`${conv.date}-${conv.session_id}`}
                      type="button"
                      onClick={() => void openConversation(conv)}
                      className={cn(
                        "flex w-full flex-col rounded-md px-2.5 py-1.5 text-left transition-colors cursor-pointer",
                        selected
                          ? "bg-[var(--surface)] text-[var(--text)]"
                          : "text-[var(--text-muted)] hover:bg-[var(--surface)]/60 hover:text-[var(--text)]",
                      )}
                    >
                      <span className="truncate text-xs font-medium">{conv.title}</span>
                      <span className="text-[10px] text-[var(--text-subtle)]">
                        {conv.turns} {conv.turns === 1 ? "turn" : "turns"} · {conv.last_time}
                      </span>
                    </button>
                  );
                })}
              </div>
            ))}
          </div>
        )}
      </div>

      <Separator />

      <div className="flex items-center justify-between gap-1 px-3 py-2.5">
        <Button
          size="sm"
          variant={callOverlayOpen ? "default" : "ghost"}
          onClick={() => setCallOverlayOpen(!callOverlayOpen)}
          aria-pressed={callOverlayOpen}
          className="text-xs"
        >
          <AudioLines className="h-3.5 w-3.5" aria-hidden="true" />
          Voice
        </Button>

        <Button
          size="sm"
          variant="ghost"
          onClick={cycleTheme}
          aria-label={`Theme: ${preference}. Click to switch`}
          title={`Theme: ${preference}`}
          className="text-xs capitalize"
        >
          <ThemeIcon className="h-3.5 w-3.5" aria-hidden="true" />
          <span>{preference}</span>
        </Button>
      </div>
    </aside>
  );
}
