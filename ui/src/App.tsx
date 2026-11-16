import { X } from "lucide-react";
import { useEffect, useLayoutEffect } from "react";
import { ActivityView } from "@/features/activity/ActivityView";
import { ChatView } from "@/features/chat/ChatView";
import { CommandPalette } from "@/features/command-palette/CommandPalette";
import { DaemonOfflineScreen } from "@/features/connection/DaemonOfflineScreen";
import { MemoryView } from "@/features/memory/MemoryView";
import { QuickAskWindow } from "@/features/quick-ask/QuickAskWindow";
import { SettingsView } from "@/features/settings/SettingsView";
import { Sidebar } from "@/features/sidebar/Sidebar";
import { UndoToast } from "@/features/sidebar/UndoToast";
import { CallOverlay } from "@/features/voice/CallOverlay";
import { VoiceView } from "@/features/voice/VoiceView";
import { ThemeProvider } from "@/lib/theme";
import { useCleaStore } from "@/protocol/store";

function isQuickAskMode(): boolean {
  if (typeof window === "undefined") return false;
  return new URLSearchParams(window.location.search).get("window") === "quick-ask";
}

function CleaShell() {
  const connectionState = useCleaStore((s) => s.connectionState);
  const reconnectAttempt = useCleaStore((s) => s.reconnectAttempt);
  const activeView = useCleaStore((s) => s.activeView);
  const lastError = useCleaStore((s) => s.lastError);
  const clearError = useCleaStore((s) => s.clearError);
  const connect = useCleaStore((s) => s.connect);
  const disconnect = useCleaStore((s) => s.disconnect);
  const retryNow = useCleaStore((s) => s.retryNow);
  const toggleCompanionLog = useCleaStore((s) => s.toggleCompanionLog);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "l") {
        const target = e.target as HTMLElement | null;
        if (target) {
          const tag = target.tagName ? target.tagName.toLowerCase() : "";
          if (
            tag === "input" ||
            tag === "textarea" ||
            tag === "select" ||
            target.isContentEditable ||
            target.getAttribute?.("contenteditable") === "true" ||
            Boolean(target.closest?.("input, textarea, select, [contenteditable='true']"))
          ) {
            return;
          }
        }
        e.preventDefault();
        toggleCompanionLog();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [toggleCompanionLog]);

  const quickAsk = isQuickAskMode();

  useLayoutEffect(() => {
    if (quickAsk) {
      document.documentElement.dataset.window = "quick-ask";
    }
  }, [quickAsk]);

  useEffect(() => {
    connect();
    return () => disconnect();
  }, [connect, disconnect]);

  if (quickAsk) {
    return <QuickAskWindow />;
  }

  if (connectionState !== "connected") {
    return (
      <DaemonOfflineScreen
        connectionState={connectionState}
        reconnectAttempt={reconnectAttempt}
        onRetry={retryNow}
      />
    );
  }

  return (
    <div className="flex h-full w-full overflow-hidden bg-[var(--bg)] text-[var(--text)]">
      <Sidebar />
      <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
        {lastError && (
          <div
            role="alert"
            className="flex items-center justify-between gap-3 border-b border-red-500/30 bg-red-500/10 px-6 py-2 text-xs text-[var(--danger)]"
          >
            <span className="truncate">{lastError}</span>
            <button
              type="button"
              onClick={clearError}
              aria-label="Dismiss error"
              className="rounded p-0.5 hover:bg-red-500/15 cursor-pointer"
            >
              <X className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          </div>
        )}
        <div className="min-h-0 flex-1 overflow-hidden">
          {activeView === "chat" && <ChatView />}
          {activeView === "activity" && <ActivityView />}
          {activeView === "memory" && <MemoryView />}
          {activeView === "settings" && <SettingsView />}
          {activeView === "voice" && <VoiceView />}
        </div>
      </main>
      <CallOverlay />
      <CommandPalette />
      <UndoToast />
    </div>
  );
}

export function App() {
  return (
    <ThemeProvider>
      <CleaShell />
    </ThemeProvider>
  );
}
export default App;
