import {
  Activity,
  AudioLines,
  BookOpen,
  Command,
  Download,
  Edit2,
  MessageSquare,
  MessageSquarePlus,
  Monitor,
  Moon,
  MoreVertical,
  Pin,
  PinOff,
  Settings,
  Sun,
  Trash2,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CleaAvatar } from "@/components/ui/clea-avatar";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
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
  const connectionState = useCleaStore((s) => s.connectionState);
  const activeView = useCleaStore((s) => s.activeView);
  const setActiveView = useCleaStore((s) => s.setActiveView);
  const conversations = useCleaStore((s) => s.conversations);
  const sessionId = useCleaStore((s) => s.sessionId);
  const selectedConversation = useCleaStore((s) => s.selectedConversation);
  const openConversation = useCleaStore((s) => s.openConversation);
  const renameConversation = useCleaStore((s) => s.renameConversation);
  const pinConversation = useCleaStore((s) => s.pinConversation);
  const exportConversation = useCleaStore((s) => s.exportConversation);
  const deleteConversation = useCleaStore((s) => s.deleteConversation);
  const deleteAllConversations = useCleaStore((s) => s.deleteAllConversations);
  const newSession = useCleaStore((s) => s.newSession);
  const tasks = useCleaStore((s) => s.tasks);
  const callOverlayOpen = useCleaStore((s) => s.callOverlayOpen);
  const startVoiceCall = useCleaStore((s) => s.startVoiceCall);
  const endVoiceCall = useCleaStore((s) => s.endVoiceCall);
  const setCommandPaletteOpen = useCleaStore((s) => s.setCommandPaletteOpen);

  const [editingSessionId, setEditingSessionId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState("");
  const [menuOpenSessionId, setMenuOpenSessionId] = useState<string | null>(null);
  const [historyMenuOpen, setHistoryMenuOpen] = useState(false);
  const [deleteDialogTarget, setDeleteDialogTarget] = useState<ConversationSummary | null>(null);
  const [deleteEpisodeChecked, setDeleteEpisodeChecked] = useState(false);
  const [deleteAllDialogOpen, setDeleteAllDialogOpen] = useState(false);
  const [deleteAllConfirmation, setDeleteAllConfirmation] = useState("");

  const { preference, cycleTheme } = useTheme();

  const runningTaskCount = tasks.filter((t) => t.status === "running").length;

  useEffect(() => {
    const handleDocumentClick = () => {
      setMenuOpenSessionId(null);
      setHistoryMenuOpen(false);
    };
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setMenuOpenSessionId(null);
        setHistoryMenuOpen(false);
      }
    };
    document.addEventListener("click", handleDocumentClick);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("click", handleDocumentClick);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, []);

  const { pinnedItems, groupedUnpinned } = useMemo(() => {
    const pinned: ConversationSummary[] = [];
    const unpinnedGroups = new Map<string, ConversationSummary[]>();

    for (const item of conversations) {
      if (item.pinned) {
        pinned.push(item);
      } else {
        const list = unpinnedGroups.get(item.date) ?? [];
        list.push(item);
        unpinnedGroups.set(item.date, list);
      }
    }
    return {
      pinnedItems: pinned,
      groupedUnpinned: Array.from(unpinnedGroups.entries()),
    };
  }, [conversations]);

  const handleSaveRename = async (sid: string) => {
    const trimmed = editingTitle.trim();
    setEditingSessionId(null);
    if (trimmed) {
      await renameConversation(sid, trimmed);
    }
  };

  const ThemeIcon = preference === "dark" ? Moon : preference === "light" ? Sun : Monitor;

  const renderItem = (conv: ConversationSummary) => {
    const isSelected =
      ((sessionId && sessionId === conv.session_id) ||
        selectedConversation?.sessionId === conv.session_id) &&
      activeView === "chat";

    const isEditing = editingSessionId === conv.session_id;
    const isMenuOpen = menuOpenSessionId === conv.session_id;

    return (
      <div
        key={`${conv.date}-${conv.session_id}`}
        className={cn(
          "group relative flex w-full items-center justify-between rounded-md px-2.5 py-1.5 text-left transition-colors",
          isSelected
            ? "bg-[var(--surface)] text-[var(--text)]"
            : "text-[var(--text-muted)] hover:bg-[var(--surface)]/60 hover:text-[var(--text)]",
        )}
      >
        {isEditing ? (
          <form
            className="flex-1 mr-1"
            onSubmit={(e) => {
              e.preventDefault();
              void handleSaveRename(conv.session_id);
            }}
          >
            <input
              type="text"
              autoFocus
              value={editingTitle}
              onChange={(e) => setEditingTitle(e.target.value)}
              onBlur={() => void handleSaveRename(conv.session_id)}
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  e.stopPropagation();
                  setEditingSessionId(null);
                }
              }}
              className="w-full rounded border border-[var(--border-strong)] bg-[var(--bg)] px-1.5 py-0.5 text-xs text-[var(--text)] focus:outline-none"
            />
          </form>
        ) : (
          <button
            type="button"
            onClick={() => void openConversation(conv)}
            className="flex min-w-0 flex-1 flex-col text-left cursor-pointer"
          >
            <div className="flex items-center gap-1.5">
              {conv.pinned && (
                <Pin
                  className="h-3 w-3 shrink-0 text-[var(--text-muted)] rotate-45"
                  aria-hidden="true"
                />
              )}
              <span className="truncate text-xs font-medium">{conv.title}</span>
            </div>
            <span className="text-[10px] text-[var(--text-subtle)]">
              {conv.turns} {conv.turns === 1 ? "turn" : "turns"} · {conv.last_time}
            </span>
          </button>
        )}

        {!isEditing && (
          <div className="relative shrink-0">
            <button
              type="button"
              aria-label="Conversation options"
              aria-expanded={isMenuOpen}
              onClick={(e) => {
                e.stopPropagation();
                setMenuOpenSessionId(isMenuOpen ? null : conv.session_id);
              }}
              className={cn(
                "rounded p-1 text-[var(--text-subtle)] hover:bg-[var(--bg-subtle)] hover:text-[var(--text)] cursor-pointer transition-opacity",
                isMenuOpen ? "opacity-100" : "opacity-0 group-hover:opacity-100 focus:opacity-100",
              )}
            >
              <MoreVertical className="h-3.5 w-3.5" aria-hidden="true" />
            </button>

            {isMenuOpen && (
              <div
                role="menu"
                className="absolute right-0 top-6 z-50 min-w-[140px] rounded-lg border border-[var(--border)] bg-[var(--surface)] p-1 shadow-lg"
              >
                <button
                  type="button"
                  role="menuitem"
                  onClick={(e) => {
                    e.stopPropagation();
                    setMenuOpenSessionId(null);
                    setEditingSessionId(conv.session_id);
                    setEditingTitle(conv.title);
                  }}
                  className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-xs text-[var(--text)] hover:bg-[var(--bg-subtle)] cursor-pointer"
                >
                  <Edit2 className="h-3.5 w-3.5 text-[var(--text-muted)]" />
                  Rename
                </button>
                <button
                  type="button"
                  role="menuitem"
                  onClick={(e) => {
                    e.stopPropagation();
                    setMenuOpenSessionId(null);
                    void pinConversation(conv.session_id, !conv.pinned);
                  }}
                  className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-xs text-[var(--text)] hover:bg-[var(--bg-subtle)] cursor-pointer"
                >
                  {conv.pinned ? (
                    <>
                      <PinOff className="h-3.5 w-3.5 text-[var(--text-muted)]" />
                      Unpin
                    </>
                  ) : (
                    <>
                      <Pin className="h-3.5 w-3.5 text-[var(--text-muted)]" />
                      Pin to top
                    </>
                  )}
                </button>
                <button
                  type="button"
                  role="menuitem"
                  onClick={(e) => {
                    e.stopPropagation();
                    setMenuOpenSessionId(null);
                    void exportConversation(conv.session_id);
                  }}
                  className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-xs text-[var(--text)] hover:bg-[var(--bg-subtle)] cursor-pointer"
                >
                  <Download className="h-3.5 w-3.5 text-[var(--text-muted)]" />
                  Export
                </button>
                <Separator className="my-1" />
                <button
                  type="button"
                  role="menuitem"
                  onClick={(e) => {
                    e.stopPropagation();
                    setMenuOpenSessionId(null);
                    setDeleteDialogTarget(conv);
                    setDeleteEpisodeChecked(false);
                  }}
                  className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-xs text-[var(--danger)] hover:bg-red-500/10 cursor-pointer"
                >
                  <Trash2 className="h-3.5 w-3.5 text-[var(--danger)]" />
                  Delete
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    );
  };

  return (
    <aside
      aria-label="Sidebar"
      className="flex h-full w-64 shrink-0 flex-col border-r border-[var(--border)] bg-[var(--bg-subtle)]"
    >
      <div className="flex shrink-0 items-center justify-between px-4 py-3.5">
        <div className="flex items-center gap-2">
          <CleaAvatar
            data-testid="sidebar-avatar"
            data-connection-state={connectionState}
            className={cn(
              "w-5 h-5 rounded-full overflow-hidden ring-[1.5px]",
              connectionState === "connected"
                ? "ring-emerald-500"
                : "ring-zinc-500/60 dark:ring-zinc-600",
            )}
          />
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

      <div className="shrink-0 px-3 pb-2">
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

      <nav aria-label="Main sections" className="shrink-0 space-y-0.5 px-3 py-1">
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

      <Separator className="my-2 shrink-0" />

      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-1">
        <div className="flex items-center justify-between px-2 pb-1.5">
          <span className="text-[11px] font-medium uppercase tracking-wider text-[var(--text-subtle)]">
            History
          </span>
          {conversations.length > 0 && (
            <div className="relative">
              <button
                type="button"
                aria-label="History options"
                aria-expanded={historyMenuOpen}
                onClick={(e) => {
                  e.stopPropagation();
                  setHistoryMenuOpen(!historyMenuOpen);
                }}
                className="rounded p-1 text-[var(--text-subtle)] hover:bg-[var(--bg-subtle)] hover:text-[var(--text)] cursor-pointer"
              >
                <MoreVertical className="h-3.5 w-3.5" aria-hidden="true" />
              </button>
              {historyMenuOpen && (
                <div
                  role="menu"
                  className="absolute right-0 top-6 z-50 min-w-[170px] rounded-lg border border-[var(--border)] bg-[var(--surface)] p-1 shadow-lg"
                >
                  <button
                    type="button"
                    role="menuitem"
                    onClick={(e) => {
                      e.stopPropagation();
                      setHistoryMenuOpen(false);
                      setDeleteAllConfirmation("");
                      setDeleteAllDialogOpen(true);
                    }}
                    className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-xs text-[var(--danger)] hover:bg-red-500/10 cursor-pointer"
                  >
                    <Trash2 className="h-3.5 w-3.5 text-[var(--danger)]" />
                    Delete all conversations
                  </button>
                </div>
              )}
            </div>
          )}
        </div>

        {pinnedItems.length === 0 && groupedUnpinned.length === 0 ? (
          <p className="px-2 py-2 text-xs text-[var(--text-subtle)]">
            No logged conversations yet.
          </p>
        ) : (
          <div className="space-y-3">
            {pinnedItems.length > 0 && (
              <div className="space-y-0.5">
                <div className="flex items-center gap-1 px-2 py-0.5 text-[11px] font-medium text-[var(--text-subtle)]">
                  <Pin className="h-3 w-3 rotate-45" aria-hidden="true" />
                  <span>Pinned</span>
                </div>
                {pinnedItems.map(renderItem)}
              </div>
            )}

            {groupedUnpinned.map(([date, items]) => (
              <div key={date} className="space-y-0.5">
                <div className="px-2 py-0.5 text-[11px] font-medium text-[var(--text-subtle)]">
                  {date}
                </div>
                {items.map(renderItem)}
              </div>
            ))}
          </div>
        )}
      </div>

      <Separator className="shrink-0" />

      <div
        data-testid="sidebar-footer"
        className="flex shrink-0 items-center justify-between gap-1.5 px-3 py-2.5"
      >
        <Button
          size="sm"
          variant={callOverlayOpen ? "default" : "ghost"}
          onClick={() => {
            if (!callOverlayOpen) {
              void startVoiceCall();
            } else {
              void endVoiceCall();
            }
          }}
          aria-pressed={callOverlayOpen}
          className="h-7 shrink-0 gap-1.5 px-2.5 text-xs"
        >
          <AudioLines className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          <span>Voice</span>
        </Button>

        <Button
          size="sm"
          variant="ghost"
          onClick={cycleTheme}
          aria-label={`Theme: ${preference}. Click to switch`}
          title={`Theme: ${preference}`}
          className="h-7 shrink-0 gap-1.5 px-2 text-xs capitalize"
        >
          <ThemeIcon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          <span>{preference}</span>
        </Button>
      </div>

      {/* Delete Single Conversation Dialog */}
      <Dialog
        open={deleteDialogTarget !== null}
        onOpenChange={(open) => {
          if (!open) setDeleteDialogTarget(null);
        }}
      >
        <DialogContent aria-label="Delete conversation confirmation" className="p-5">
          <DialogTitle className="text-sm font-semibold text-[var(--text)]">
            Delete conversation
          </DialogTitle>
          <DialogDescription className="mt-2 text-xs text-[var(--text-muted)] leading-relaxed">
            Deletes this conversation's transcript. Things Clea already remembered from it (facts,
            episode summaries) are kept and can be removed in Memory.
          </DialogDescription>
          <div className="mt-4">
            <label className="flex items-center gap-2 text-xs text-[var(--text)] cursor-pointer select-none">
              <input
                type="checkbox"
                checked={deleteEpisodeChecked}
                onChange={(e) => setDeleteEpisodeChecked(e.target.checked)}
                className="rounded border-[var(--border)]"
              />
              Also delete its episode summary
            </label>
          </div>
          <div className="mt-5 flex justify-end gap-2">
            <Button
              size="sm"
              variant="outline"
              onClick={() => setDeleteDialogTarget(null)}
              className="text-xs"
            >
              Cancel
            </Button>
            <Button
              size="sm"
              variant="default"
              onClick={() => {
                if (deleteDialogTarget) {
                  void deleteConversation(deleteDialogTarget.session_id, deleteEpisodeChecked);
                  setDeleteDialogTarget(null);
                }
              }}
              className="bg-[var(--danger)] text-white hover:bg-red-600 text-xs cursor-pointer"
            >
              Delete
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {/* Delete All Conversations Dialog */}
      <Dialog
        open={deleteAllDialogOpen}
        onOpenChange={(open) => {
          if (!open) {
            setDeleteAllDialogOpen(false);
            setDeleteAllConfirmation("");
          }
        }}
      >
        <DialogContent aria-label="Delete all conversations confirmation" className="p-5">
          <DialogTitle className="text-sm font-semibold text-[var(--text)]">
            Delete all conversations
          </DialogTitle>
          <DialogDescription className="mt-2 text-xs text-[var(--text-muted)] leading-relaxed">
            This will delete transcripts for all conversations. You can undo this action for 10
            seconds. To confirm, type{" "}
            <strong className="font-semibold text-[var(--danger)]">DELETE</strong> below:
          </DialogDescription>
          <div className="mt-4">
            <input
              type="text"
              autoFocus
              placeholder="Type DELETE to confirm"
              value={deleteAllConfirmation}
              onChange={(e) => setDeleteAllConfirmation(e.target.value)}
              className="w-full rounded-md border border-[var(--border)] bg-[var(--bg)] px-3 py-1.5 text-xs text-[var(--text)] placeholder:text-[var(--text-subtle)] focus:border-[var(--border-strong)] focus:outline-none"
            />
          </div>
          <div className="mt-5 flex justify-end gap-2">
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                setDeleteAllDialogOpen(false);
                setDeleteAllConfirmation("");
              }}
              className="text-xs"
            >
              Cancel
            </Button>
            <Button
              size="sm"
              variant="default"
              disabled={deleteAllConfirmation !== "DELETE"}
              onClick={() => {
                if (deleteAllConfirmation === "DELETE") {
                  void deleteAllConversations();
                  setDeleteAllDialogOpen(false);
                  setDeleteAllConfirmation("");
                }
              }}
              className="bg-[var(--danger)] text-white hover:bg-red-600 text-xs cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Delete all
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </aside>
  );
}
