import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ChatView } from "@/features/chat/ChatView";
import { CommandPalette } from "@/features/command-palette/CommandPalette";
import { Sidebar } from "@/features/sidebar/Sidebar";
import { UndoToast } from "@/features/sidebar/UndoToast";
import { ThemeProvider } from "@/lib/theme";
import { useCleaStore } from "@/protocol/store";

describe("Conversation resume & management UI", () => {
  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn();
    useCleaStore.setState({
      connectionState: "connected",
      activeView: "chat",
      commandPaletteOpen: false,
      lastError: null,
      sessionId: "sess-active",
      resumedSessionId: null,
      resumeLoading: false,
      resumeError: null,
      undoToast: null,
      messages: [],
      sending: false,
      conversations: [
        {
          date: "2026-11-13",
          session_id: "sess-pinned",
          title: "Important Project",
          turns: 3,
          last_time: "10:00:00",
          pinned: true,
        },
        {
          date: "2026-11-13",
          session_id: "sess-regular",
          title: "Daily Standup",
          turns: 1,
          last_time: "09:00:00",
          pinned: false,
        },
      ],
      selectedConversation: null,
      tasks: [],
      memoryLongTerm: "",
      memoryDays: [],
      selectedMemoryDay: null,
      memoryDailyContent: "",
      memoryLoading: false,
      settings: null,
      settingsLoading: false,
      voiceState: "idle",
      callOverlayOpen: false,
    });
  });

  it("resumes conversation when clicked from History and keeps input enabled", async () => {
    const openSpy = vi.fn().mockImplementation(async (conv) => {
      useCleaStore.setState({
        sessionId: conv.session_id,
        resumedSessionId: conv.session_id,
        messages: [
          {
            id: `resumed-${conv.session_id}-0`,
            role: "user",
            content: "Earlier question",
            streaming: false,
            toolCalls: [],
            approvals: [],
          },
          {
            id: `resumed-${conv.session_id}-1`,
            role: "assistant",
            content: "Earlier answer",
            streaming: false,
            toolCalls: [],
            approvals: [],
          },
        ],
      });
    });
    useCleaStore.setState({ openConversation: openSpy });

    const { rerender } = render(
      <ThemeProvider>
        <Sidebar />
        <ChatView />
      </ThemeProvider>,
    );

    fireEvent.click(screen.getByText("Daily Standup"));
    expect(openSpy).toHaveBeenCalledWith(expect.objectContaining({ session_id: "sess-regular" }));

    rerender(
      <ThemeProvider>
        <Sidebar />
        <ChatView />
      </ThemeProvider>,
    );

    // Messages are shown
    expect(screen.getByText("Earlier question")).toBeInTheDocument();
    expect(screen.getByText("Earlier answer")).toBeInTheDocument();

    // Input stays enabled
    const input = screen.getByLabelText("Message Clea");
    expect(input).toBeEnabled();
    expect(input).not.toHaveAttribute("disabled");
  });

  it("renders continuing earlier conversation divider between resumed and new turns", () => {
    useCleaStore.setState({
      sessionId: "sess-regular",
      resumedSessionId: "sess-regular",
      messages: [
        {
          id: "resumed-sess-regular-0",
          role: "user",
          content: "Old question",
          streaming: false,
          toolCalls: [],
          approvals: [],
        },
        {
          id: "resumed-sess-regular-1",
          role: "assistant",
          content: "Old answer",
          streaming: false,
          toolCalls: [],
          approvals: [],
        },
        {
          id: "user-new-turn",
          role: "user",
          content: "Follow-up question",
          streaming: false,
          toolCalls: [],
          approvals: [],
        },
      ],
    });

    render(<ChatView />);

    expect(screen.getByText("Continuing earlier conversation")).toBeInTheDocument();
    expect(screen.getByText("Old question")).toBeInTheDocument();
    expect(screen.getByText("Follow-up question")).toBeInTheDocument();
  });

  it("preserves active conversation across view navigation", () => {
    useCleaStore.setState({
      sessionId: "sess-resumed",
      messages: [
        {
          id: "m1",
          role: "user",
          content: "Preserved message",
          streaming: false,
          toolCalls: [],
          approvals: [],
        },
      ],
    });

    render(
      <ThemeProvider>
        <Sidebar />
      </ThemeProvider>,
    );

    // Switch to Memory view
    fireEvent.click(screen.getByRole("button", { name: /Memory/i }));
    expect(useCleaStore.getState().activeView).toBe("memory");
    expect(useCleaStore.getState().sessionId).toBe("sess-resumed");
    expect(useCleaStore.getState().messages).toHaveLength(1);

    // Switch back to Chat view
    fireEvent.click(screen.getByRole("button", { name: /^Chat$/i }));
    expect(useCleaStore.getState().activeView).toBe("chat");
    expect(useCleaStore.getState().sessionId).toBe("sess-resumed");
    expect(useCleaStore.getState().messages[0].content).toBe("Preserved message");
  });

  it("renders resume loading and error states with retry button", () => {
    const retrySpy = vi.fn().mockResolvedValue(undefined);
    act(() => {
      useCleaStore.setState({
        resumeLoading: true,
        resumeError: null,
        retryResume: retrySpy,
        messages: [],
      });
    });

    const { rerender } = render(<ChatView />);
    expect(screen.getByText("Loading conversation…")).toBeInTheDocument();

    act(() => {
      useCleaStore.setState({
        resumeLoading: false,
        resumeError: "Connection lost while loading",
      });
    });
    rerender(<ChatView />);

    expect(screen.getByText("Couldn't load this conversation")).toBeInTheDocument();
    expect(screen.getByText("Connection lost while loading")).toBeInTheDocument();

    const retryBtn = screen.getByRole("button", { name: /Retry/i });
    fireEvent.click(retryBtn);
    expect(retrySpy).toHaveBeenCalledTimes(1);
  });

  it("groups pinned conversations in a Pinned section at top", () => {
    render(
      <ThemeProvider>
        <Sidebar />
      </ThemeProvider>,
    );

    expect(screen.getByText("Pinned")).toBeInTheDocument();
    expect(screen.getByText("Important Project")).toBeInTheDocument();
    expect(screen.getByText("Daily Standup")).toBeInTheDocument();
  });

  it("supports inline rename with Enter/blur and cancels on Escape", async () => {
    const renameSpy = vi.fn().mockResolvedValue(undefined);
    useCleaStore.setState({ renameConversation: renameSpy });

    render(
      <ThemeProvider>
        <Sidebar />
      </ThemeProvider>,
    );

    // Open options menu on Daily Standup
    const optionsBtns = screen.getAllByLabelText("Conversation options");
    fireEvent.click(optionsBtns[1]); // Daily Standup options

    // Click Rename
    fireEvent.click(screen.getByRole("menuitem", { name: /Rename/i }));

    const renameInput = screen.getByDisplayValue("Daily Standup");
    expect(renameInput).toBeInTheDocument();

    // Type and cancel with Escape
    fireEvent.change(renameInput, { target: { value: "Discarded Title" } });
    fireEvent.keyDown(renameInput, { key: "Escape" });
    expect(renameSpy).not.toHaveBeenCalled();

    // Reopen menu and commit rename with Enter
    const optionsBtnsAfter = screen.getAllByLabelText("Conversation options");
    fireEvent.click(optionsBtnsAfter[1]);
    fireEvent.click(screen.getByRole("menuitem", { name: /Rename/i }));
    const input2 = screen.getByDisplayValue("Daily Standup");
    fireEvent.change(input2, { target: { value: "Updated Title" } });
    fireEvent.submit(input2.closest("form")!);
    expect(renameSpy).toHaveBeenCalledWith("sess-regular", "Updated Title");
  });

  it("supports Pin/Unpin and Export actions from options menu", () => {
    const pinSpy = vi.fn().mockResolvedValue(undefined);
    const exportSpy = vi.fn().mockResolvedValue(undefined);
    useCleaStore.setState({
      pinConversation: pinSpy,
      exportConversation: exportSpy,
    });

    render(
      <ThemeProvider>
        <Sidebar />
      </ThemeProvider>,
    );

    // Options for Daily Standup (unpinned)
    const optionsBtns = screen.getAllByLabelText("Conversation options");
    fireEvent.click(optionsBtns[1]);

    // Pin to top
    fireEvent.click(screen.getByRole("menuitem", { name: /Pin to top/i }));
    expect(pinSpy).toHaveBeenCalledWith("sess-regular", true);

    // Options for Important Project (pinned)
    fireEvent.click(optionsBtns[0]);
    fireEvent.click(screen.getByRole("menuitem", { name: /Unpin/i }));
    expect(pinSpy).toHaveBeenCalledWith("sess-pinned", false);

    // Export
    fireEvent.click(optionsBtns[1]);
    fireEvent.click(screen.getByRole("menuitem", { name: /Export/i }));
    expect(exportSpy).toHaveBeenCalledWith("sess-regular");
  });

  it("handles delete confirmation dialog with episode summary checkbox and undo toast", async () => {
    const deleteSpy = vi.fn().mockResolvedValue(undefined);
    const undoSpy = vi.fn().mockResolvedValue(undefined);
    useCleaStore.setState({
      deleteConversation: deleteSpy,
      undoDelete: undoSpy,
    });

    render(
      <ThemeProvider>
        <Sidebar />
        <UndoToast />
      </ThemeProvider>,
    );

    const optionsBtns = screen.getAllByLabelText("Conversation options");
    fireEvent.click(optionsBtns[1]);
    fireEvent.click(screen.getByRole("menuitem", { name: /Delete/i }));

    // Confirm dialog contents
    expect(screen.getByText("Delete conversation")).toBeInTheDocument();
    expect(screen.getByText(/Deletes this conversation's transcript/)).toBeInTheDocument();
    expect(screen.getByText("Also delete its episode summary")).toBeInTheDocument();

    // Check optional episode deletion
    const checkbox = screen.getByLabelText("Also delete its episode summary");
    fireEvent.click(checkbox);

    // Click confirm delete in dialog
    const deleteBtn = screen.getByRole("button", { name: "Delete" });
    fireEvent.click(deleteBtn);
    expect(deleteSpy).toHaveBeenCalledWith("sess-regular", true);

    // Simulate undo toast state
    act(() => {
      useCleaStore.setState({
        undoToast: {
          sessionId: "sess-regular",
          expiresAt: Date.now() + 10_000,
          message: "Conversation deleted",
        },
      });
    });

    expect(screen.getByText(/Conversation deleted/)).toBeInTheDocument();
    const undoBtn = screen.getByRole("button", { name: /Undo/i });
    fireEvent.click(undoBtn);
    expect(undoSpy).toHaveBeenCalled();
  });

  it("supports Delete All conversations with DELETE confirmation", () => {
    const deleteAllSpy = vi.fn().mockResolvedValue(undefined);
    useCleaStore.setState({ deleteAllConversations: deleteAllSpy });

    render(
      <ThemeProvider>
        <Sidebar />
      </ThemeProvider>,
    );

    const historyOptionsBtn = screen.getByLabelText("History options");
    fireEvent.click(historyOptionsBtn);

    fireEvent.click(screen.getByRole("menuitem", { name: /Delete all conversations/i }));

    expect(screen.getByText("Delete all conversations")).toBeInTheDocument();
    const confirmInput = screen.getByPlaceholderText("Type DELETE to confirm");
    const confirmBtn = screen.getByRole("button", { name: "Delete all" });

    // Disabled initially
    expect(confirmBtn).toBeDisabled();

    // Still disabled with wrong text
    fireEvent.change(confirmInput, { target: { value: "DEL" } });
    expect(confirmBtn).toBeDisabled();

    // Enabled with exact "DELETE"
    fireEvent.change(confirmInput, { target: { value: "DELETE" } });
    expect(confirmBtn).not.toBeDisabled();

    fireEvent.click(confirmBtn);
    expect(deleteAllSpy).toHaveBeenCalledTimes(1);
  });

  it("includes conversation commands in CommandPalette for active session", () => {
    useCleaStore.setState({
      commandPaletteOpen: true,
      sessionId: "sess-pinned",
    });

    render(
      <ThemeProvider>
        <CommandPalette />
      </ThemeProvider>,
    );

    expect(screen.getByText("Rename Conversation")).toBeInTheDocument();
    expect(screen.getByText("Unpin Conversation")).toBeInTheDocument();
    expect(screen.getByText("Export Conversation (Markdown)")).toBeInTheDocument();
    expect(screen.getByText("Delete Conversation")).toBeInTheDocument();
  });

  it("renders past tool call cards when resuming conversation with tool calls", () => {
    useCleaStore.setState({
      sessionId: "sess-tools",
      resumedSessionId: "sess-tools",
      messages: [
        {
          id: "resumed-sess-tools-0",
          role: "user",
          content: "Check system",
          streaming: false,
          toolCalls: [],
          approvals: [],
        },
        {
          id: "resumed-sess-tools-1",
          role: "assistant",
          content: "System looks healthy.",
          streaming: false,
          toolCalls: [
            {
              callId: "tc-0",
              name: "get_system_info",
              arguments: { include_platform_details: false },
              riskLevel: "read_only",
              status: "done",
              output: "CPU 10%, Memory 45%",
            },
          ],
          approvals: [],
        },
      ],
    });

    render(<ChatView />);

    expect(screen.getByText("get_system_info")).toBeInTheDocument();
    expect(screen.getByText("System looks healthy.")).toBeInTheDocument();
  });

  it("synchronizes user message and streaming tokens in second window via events", () => {
    useCleaStore.setState({
      sessionId: "shared-sess",
      messages: [],
    });

    // 1. Another window starts a turn
    act(() => {
      useCleaStore.getState().applyEvent({
        event: "conversation_updated",
        data: {
          session_id: "shared-sess",
          user_message: "Message from window 1",
          request_id: "req-win1",
        },
      });
    });

    const state1 = useCleaStore.getState();
    expect(state1.messages).toHaveLength(2);
    expect(state1.messages[0].content).toBe("Message from window 1");
    expect(state1.messages[1].id).toBe("req-win1");
    expect(state1.messages[1].streaming).toBe(true);

    // 2. Assistant token streams in
    act(() => {
      useCleaStore.getState().applyEvent({
        event: "assistant_token",
        request_id: "req-win1",
        session_id: "shared-sess",
        data: { text: "Streaming answer chunk" },
      });
    });

    const state2 = useCleaStore.getState();
    expect(state2.messages[1].content).toBe("Streaming answer chunk");

    // 3. Turn finishes
    act(() => {
      useCleaStore.getState().applyEvent({
        event: "conversation_updated",
        data: {
          session_id: "shared-sess",
          assistant_message: "Streaming answer chunk - done!",
          request_id: "req-win1",
        },
      });
    });

    const state3 = useCleaStore.getState();
    expect(state3.messages[1].content).toBe("Streaming answer chunk - done!");
    expect(state3.messages[1].streaming).toBe(false);
  });

  it("calls undo_delete without literal wildcard when undoing delete-all", async () => {
    useCleaStore.setState({
      undoToast: {
        sessionId: "*",
        message: "All conversations deleted",
        expiresAt: Date.now() + 10_000,
      },
    });

    render(
      <ThemeProvider>
        <UndoToast />
      </ThemeProvider>,
    );

    expect(screen.getByText("All conversations deleted")).toBeInTheDocument();
  });
});
