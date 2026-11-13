import { Command } from "cmdk";
import {
  Activity,
  AudioLines,
  BookOpen,
  Download,
  Edit2,
  MessageSquare,
  MessageSquarePlus,
  Pin,
  PinOff,
  RefreshCw,
  Settings,
  SunMoon,
  Trash2,
} from "lucide-react";
import { useEffect } from "react";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { useTheme } from "@/lib/theme-context";
import { useCleaStore } from "@/protocol/store";

export function CommandPalette() {
  const open = useCleaStore((s) => s.commandPaletteOpen);
  const setOpen = useCleaStore((s) => s.setCommandPaletteOpen);
  const setActiveView = useCleaStore((s) => s.setActiveView);
  const newSession = useCleaStore((s) => s.newSession);
  const sessionId = useCleaStore((s) => s.sessionId);
  const conversations = useCleaStore((s) => s.conversations);
  const renameConversation = useCleaStore((s) => s.renameConversation);
  const pinConversation = useCleaStore((s) => s.pinConversation);
  const exportConversation = useCleaStore((s) => s.exportConversation);
  const deleteConversation = useCleaStore((s) => s.deleteConversation);
  const callOverlayOpen = useCleaStore((s) => s.callOverlayOpen);
  const setCallOverlayOpen = useCleaStore((s) => s.setCallOverlayOpen);
  const retryNow = useCleaStore((s) => s.retryNow);
  const { cycleTheme } = useTheme();

  const currentConv = conversations.find((c) => c.session_id === sessionId);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen(!open);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, setOpen]);

  const runAndClose = (fn: () => void) => {
    fn();
    setOpen(false);
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent aria-label="Command palette" className="overflow-hidden p-0">
        <DialogTitle className="sr-only">Command Palette</DialogTitle>
        <DialogDescription className="sr-only">
          Jump to a screen or run a Clea command.
        </DialogDescription>
        <Command className="flex w-full flex-col bg-[var(--surface)] text-[var(--text)]">
          <Command.Input
            autoFocus
            placeholder="Type a command or jump to…"
            className="h-11 w-full border-b border-[var(--border)] bg-transparent px-4 text-sm text-[var(--text)] placeholder:text-[var(--text-subtle)] focus:outline-none"
          />
          <Command.List className="max-h-72 overflow-y-auto p-2 text-sm">
            <Command.Empty className="py-6 text-center text-xs text-[var(--text-subtle)]">
              No matching commands.
            </Command.Empty>

            {sessionId && (
              <Command.Group
                heading="Current Conversation"
                className="px-1 py-1 text-[11px] font-medium text-[var(--text-subtle)]"
              >
                <Command.Item
                  onSelect={() =>
                    runAndClose(() => {
                      const newTitle = window.prompt(
                        "Enter new title for conversation:",
                        currentConv?.title || "",
                      );
                      if (newTitle && newTitle.trim()) {
                        void renameConversation(sessionId, newTitle.trim());
                      }
                    })
                  }
                  className="flex cursor-pointer items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-[var(--text)] aria-selected:bg-[var(--bg-subtle)]"
                >
                  <Edit2 className="h-4 w-4 text-[var(--text-muted)]" aria-hidden="true" />
                  Rename Conversation
                </Command.Item>
                <Command.Item
                  onSelect={() =>
                    runAndClose(() => {
                      void pinConversation(sessionId, !currentConv?.pinned);
                    })
                  }
                  className="flex cursor-pointer items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-[var(--text)] aria-selected:bg-[var(--bg-subtle)]"
                >
                  {currentConv?.pinned ? (
                    <>
                      <PinOff className="h-4 w-4 text-[var(--text-muted)]" aria-hidden="true" />
                      Unpin Conversation
                    </>
                  ) : (
                    <>
                      <Pin className="h-4 w-4 text-[var(--text-muted)]" aria-hidden="true" />
                      Pin Conversation to Top
                    </>
                  )}
                </Command.Item>
                <Command.Item
                  onSelect={() =>
                    runAndClose(() => {
                      void exportConversation(sessionId);
                    })
                  }
                  className="flex cursor-pointer items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-[var(--text)] aria-selected:bg-[var(--bg-subtle)]"
                >
                  <Download className="h-4 w-4 text-[var(--text-muted)]" aria-hidden="true" />
                  Export Conversation (Markdown)
                </Command.Item>
                <Command.Item
                  onSelect={() =>
                    runAndClose(() => {
                      if (
                        window.confirm(
                          "Deletes this conversation's transcript. Things Clea already remembered from it (facts, episode summaries) are kept and can be removed in Memory.\n\nDelete this conversation?",
                        )
                      ) {
                        void deleteConversation(sessionId);
                      }
                    })
                  }
                  className="flex cursor-pointer items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-[var(--danger)] aria-selected:bg-[var(--bg-subtle)]"
                >
                  <Trash2 className="h-4 w-4 text-[var(--danger)]" aria-hidden="true" />
                  Delete Conversation
                </Command.Item>
              </Command.Group>
            )}

            <Command.Group
              heading="Navigation"
              className="px-1 py-1 text-[11px] font-medium text-[var(--text-subtle)]"
            >
              <Command.Item
                onSelect={() => runAndClose(() => setActiveView("chat"))}
                className="flex cursor-pointer items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-[var(--text)] aria-selected:bg-[var(--bg-subtle)]"
              >
                <MessageSquare className="h-4 w-4 text-[var(--text-muted)]" aria-hidden="true" />
                Go to Chat
              </Command.Item>
              <Command.Item
                onSelect={() => runAndClose(() => setActiveView("activity"))}
                className="flex cursor-pointer items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-[var(--text)] aria-selected:bg-[var(--bg-subtle)]"
              >
                <Activity className="h-4 w-4 text-[var(--text-muted)]" aria-hidden="true" />
                Go to Activity
              </Command.Item>
              <Command.Item
                onSelect={() => runAndClose(() => setActiveView("memory"))}
                className="flex cursor-pointer items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-[var(--text)] aria-selected:bg-[var(--bg-subtle)]"
              >
                <BookOpen className="h-4 w-4 text-[var(--text-muted)]" aria-hidden="true" />
                Go to Memory
              </Command.Item>
              <Command.Item
                onSelect={() => runAndClose(() => setActiveView("settings"))}
                className="flex cursor-pointer items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-[var(--text)] aria-selected:bg-[var(--bg-subtle)]"
              >
                <Settings className="h-4 w-4 text-[var(--text-muted)]" aria-hidden="true" />
                Go to Settings
              </Command.Item>
            </Command.Group>

            <Command.Group
              heading="Actions"
              className="px-1 py-1 text-[11px] font-medium text-[var(--text-subtle)]"
            >
              <Command.Item
                onSelect={() => runAndClose(() => void newSession())}
                className="flex cursor-pointer items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-[var(--text)] aria-selected:bg-[var(--bg-subtle)]"
              >
                <MessageSquarePlus
                  className="h-4 w-4 text-[var(--text-muted)]"
                  aria-hidden="true"
                />
                New Chat Session
              </Command.Item>
              <Command.Item
                onSelect={() => runAndClose(() => setCallOverlayOpen(!callOverlayOpen))}
                className="flex cursor-pointer items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-[var(--text)] aria-selected:bg-[var(--bg-subtle)]"
              >
                <AudioLines className="h-4 w-4 text-[var(--text-muted)]" aria-hidden="true" />
                {callOverlayOpen ? "Close Voice Overlay" : "Open Voice Overlay"}
              </Command.Item>
              <Command.Item
                onSelect={() => runAndClose(cycleTheme)}
                className="flex cursor-pointer items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-[var(--text)] aria-selected:bg-[var(--bg-subtle)]"
              >
                <SunMoon className="h-4 w-4 text-[var(--text-muted)]" aria-hidden="true" />
                Cycle Theme (System / Dark / Light)
              </Command.Item>
              <Command.Item
                onSelect={() => runAndClose(retryNow)}
                className="flex cursor-pointer items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-[var(--text)] aria-selected:bg-[var(--bg-subtle)]"
              >
                <RefreshCw className="h-4 w-4 text-[var(--text-muted)]" aria-hidden="true" />
                Reconnect to Daemon
              </Command.Item>
            </Command.Group>
          </Command.List>
        </Command>
      </DialogContent>
    </Dialog>
  );
}
