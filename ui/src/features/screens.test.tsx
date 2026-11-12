import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "@/App";
import { ActivityView } from "@/features/activity/ActivityView";
import { CommandPalette } from "@/features/command-palette/CommandPalette";
import { MemoryView } from "@/features/memory/MemoryView";
import { QuickAskWindow } from "@/features/quick-ask/QuickAskWindow";
import { SettingsView } from "@/features/settings/SettingsView";
import { Sidebar } from "@/features/sidebar/Sidebar";
import { ThemeProvider } from "@/lib/theme";
import { useCleaStore } from "@/protocol/store";

describe("UI screens & navigation", () => {
  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn();
    useCleaStore.setState({
      connectionState: "connected",
      activeView: "chat",
      commandPaletteOpen: false,
      lastError: null,
      sessionId: "sess-1234",
      messages: [],
      sending: false,
      conversations: [
        {
          date: "2026-11-12",
          session_id: "hist-1",
          title: "Morning check-in",
          turns: 2,
          last_time: "09:15:00",
        },
      ],
      selectedConversation: null,
      tasks: [
        {
          task_id: "t1",
          title: "Check weather",
          status: "running",
          started_at: "2026-11-12T09:00:00Z",
          finished_at: null,
          detail: null,
        },
        {
          task_id: "t2",
          title: "List files",
          status: "succeeded",
          started_at: "2026-11-12T08:50:00Z",
          finished_at: "2026-11-12T08:50:01Z",
          detail: null,
        },
      ],
      memoryLongTerm: "# Memory\n\n- Likes concise answers.",
      memoryDays: ["2026-11-12"],
      selectedMemoryDay: "2026-11-12",
      memoryDailyContent: "**User:** Hi\n\n**Clea:** Hello!",
      memoryLoading: false,
      settings: {
        provider: "gemini",
        model: "gemini-3.8-flash",
        host: "127.0.0.1",
        port: 8765,
        approval_timeout_seconds: 120,
        tools: [
          {
            name: "get_system_info",
            description: "Read local clock and platform.",
            risk_level: "read_only",
          },
        ],
        permission_tiers: [
          { tier: "read_only", policy: "auto_allow" },
          { tier: "reversible", policy: "ask" },
          { tier: "destructive", policy: "ask_confirm" },
        ],
      },
      settingsLoading: false,
      voiceState: "idle",
      callOverlayOpen: false,
    });
  });

  it("renders grouped conversations in Sidebar and navigates across views", () => {
    const openConv = vi.fn().mockResolvedValue(undefined);
    useCleaStore.setState({ openConversation: openConv });

    render(
      <ThemeProvider>
        <Sidebar />
      </ThemeProvider>,
    );

    expect(screen.getByText("2026-11-12")).toBeInTheDocument();
    expect(screen.getByText("Morning check-in")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Morning check-in"));
    expect(openConv).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: /Activity/i }));
    expect(useCleaStore.getState().activeView).toBe("activity");

    fireEvent.click(screen.getByRole("button", { name: /Memory/i }));
    expect(useCleaStore.getState().activeView).toBe("memory");

    fireEvent.click(screen.getByRole("button", { name: /Settings/i }));
    expect(useCleaStore.getState().activeView).toBe("settings");
  });

  it("renders running and finished tasks in ActivityView", () => {
    render(<ActivityView />);
    expect(screen.getByText("Check weather")).toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();
    expect(screen.getByText("List files")).toBeInTheDocument();
    expect(screen.getByText("succeeded")).toBeInTheDocument();
  });

  it("renders long-term memory and daily logs in MemoryView", () => {
    const loadMemory = vi.fn().mockResolvedValue(undefined);
    const loadDailyMemory = vi.fn().mockResolvedValue(undefined);
    useCleaStore.setState({ loadMemory, loadDailyMemory });

    render(<MemoryView />);
    expect(loadMemory).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/Likes concise answers/i)).toBeInTheDocument();
    expect(screen.getByText(/Hello!/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "2026-11-12" }));
    expect(loadDailyMemory).toHaveBeenCalledWith("2026-11-12");
  });

  it("renders non-secret provider, endpoint, and permission tiers in SettingsView", () => {
    const loadSettings = vi.fn().mockResolvedValue(undefined);
    useCleaStore.setState({ loadSettings });

    render(
      <ThemeProvider>
        <SettingsView />
      </ThemeProvider>,
    );
    expect(screen.getByText("gemini")).toBeInTheDocument();
    expect(screen.getByText("gemini-3.8-flash")).toBeInTheDocument();
    expect(screen.getByText("ws://127.0.0.1:8765")).toBeInTheDocument();
    expect(screen.getByText("Ask + confirm")).toBeInTheDocument();
  });

  it("toggles CommandPalette on Ctrl+K", () => {
    render(
      <ThemeProvider>
        <CommandPalette />
      </ThemeProvider>,
    );

    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    expect(useCleaStore.getState().commandPaletteOpen).toBe(true);
    expect(screen.getByPlaceholderText(/Type a command/i)).toBeInTheDocument();
  });

  it("sends a prompt from QuickAskWindow and closes on Escape", () => {
    const sendSpy = vi.fn().mockResolvedValue(undefined);
    const closeSpy = vi.fn();
    window.cleaDesktop = {
      isElectron: true,
      getConnection: async () => null,
      closeQuickAsk: closeSpy,
    };
    useCleaStore.setState({ sendMessage: sendSpy });

    render(<QuickAskWindow />);
    const input = screen.getByLabelText("Quick ask Clea");
    fireEvent.change(input, { target: { value: "Quick question" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(sendSpy).toHaveBeenCalledWith("Quick question");

    fireEvent.keyDown(window, { key: "Escape" });
    expect(closeSpy).toHaveBeenCalledTimes(1);
    delete window.cleaDesktop;
  });

  it("renders and dismisses daemon error events in the shell", () => {
    const connectSpy = vi.fn();
    const disconnectSpy = vi.fn();
    useCleaStore.setState({ connect: connectSpy, disconnect: disconnectSpy });

    render(<App />);
    act(() => {
      useCleaStore.getState().applyEvent({
        frame_type: "event",
        event: "error",
        data: { message: "Test daemon error", code: "invalid_frame" },
      });
    });

    expect(screen.getByRole("alert")).toHaveTextContent("Test daemon error");
    fireEvent.click(screen.getByRole("button", { name: /Dismiss error/i }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
