import { Command } from "cmdk";
import {
  Activity,
  AudioLines,
  BookOpen,
  MessageSquare,
  MessageSquarePlus,
  RefreshCw,
  Settings,
  SunMoon,
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
  const callOverlayOpen = useCleaStore((s) => s.callOverlayOpen);
  const setCallOverlayOpen = useCleaStore((s) => s.setCallOverlayOpen);
  const retryNow = useCleaStore((s) => s.retryNow);
  const { cycleTheme } = useTheme();

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
