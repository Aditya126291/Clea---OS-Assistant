import { create } from "zustand";
import { CleaClient, type CleaClientOptions } from "./client";
import type {
  AppView,
  ApprovalDecision,
  ChatMessage,
  CleaEvent,
  ConnectionState,
  ConversationSummary,
  ConversationTurn,
  SettingsGetResult,
  TaskStatusData,
  VoiceState,
} from "./types";

export interface SelectedConversation {
  date: string;
  sessionId: string;
  title: string;
  turns: ConversationTurn[];
}

export interface CleaStoreState {
  connectionState: ConnectionState;
  reconnectAttempt: number;
  lastError: string | null;
  activeView: AppView;
  commandPaletteOpen: boolean;

  sessionId: string | null;
  messages: ChatMessage[];
  sending: boolean;

  conversations: ConversationSummary[];
  conversationsLoading: boolean;
  selectedConversation: SelectedConversation | null;

  tasks: TaskStatusData[];

  memoryLongTerm: string;
  memoryDays: string[];
  selectedMemoryDay: string | null;
  memoryDailyContent: string;
  memoryLoading: boolean;

  settings: SettingsGetResult | null;
  settingsLoading: boolean;

  voiceState: VoiceState;
  callOverlayOpen: boolean;

  connect: (clientOptions?: CleaClientOptions) => void;
  disconnect: () => void;
  retryNow: () => void;
  setActiveView: (view: AppView) => void;
  setCommandPaletteOpen: (open: boolean) => void;
  setCallOverlayOpen: (open: boolean) => void;

  newSession: () => Promise<void>;
  sendMessage: (text: string) => Promise<void>;
  respondApproval: (approvalId: string, decision: ApprovalDecision) => Promise<void>;

  loadConversations: () => Promise<void>;
  openConversation: (summary: ConversationSummary) => Promise<void>;
  exitReadOnlyConversation: () => void;

  loadMemory: () => Promise<void>;
  loadDailyMemory: (day: string) => Promise<void>;
  loadSettings: () => Promise<void>;

  applyEvent: (event: CleaEvent) => void;
}

let activeClient: CleaClient | null = null;
let localMsgSeq = 0;

function nextRequestId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID().replace(/-/g, "");
  }
  localMsgSeq += 1;
  return `req-${Date.now().toString(16)}-${localMsgSeq}`;
}

function updateAssistantByRequest(
  messages: ChatMessage[],
  requestId: string | null | undefined,
  updater: (msg: ChatMessage) => ChatMessage,
): ChatMessage[] {
  if (requestId) {
    const idx = messages.findIndex((m) => m.id === requestId && m.role === "assistant");
    if (idx !== -1) {
      const next = [...messages];
      next[idx] = updater(next[idx]);
      return next;
    }
  }
  // Fallback to the latest streaming assistant message if request_id wasn't matched
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].role === "assistant" && messages[i].streaming) {
      const next = [...messages];
      next[i] = updater(next[i]);
      return next;
    }
  }
  return messages;
}

export const useCleaStore = create<CleaStoreState>((set, get) => ({
  connectionState: "idle",
  reconnectAttempt: 0,
  lastError: null,
  activeView: "chat",
  commandPaletteOpen: false,

  sessionId: null,
  messages: [],
  sending: false,

  conversations: [],
  conversationsLoading: false,
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

  connect: (clientOptions = {}) => {
    if (activeClient) {
      activeClient.dispose();
    }
    set({ connectionState: "connecting", lastError: null });

    const client = new CleaClient({
      ...clientOptions,
      onConnected: () => {
        set({ connectionState: "connected", reconnectAttempt: 0, lastError: null });
        void get().newSession();
        void get().loadConversations();
        void get().loadSettings();
        clientOptions.onConnected?.();
      },
      onDisconnected: (reason, attempt) => {
        set({
          connectionState: reason,
          reconnectAttempt: attempt,
        });
        clientOptions.onDisconnected?.(reason, attempt);
      },
      onEvent: (event) => {
        get().applyEvent(event);
        clientOptions.onEvent?.(event);
      },
    });
    activeClient = client;
    void client.connect();
  },

  disconnect: () => {
    if (activeClient) {
      activeClient.dispose();
      activeClient = null;
    }
    set({ connectionState: "idle", reconnectAttempt: 0 });
  },

  retryNow: () => {
    if (!activeClient) {
      get().connect();
      return;
    }
    set({ connectionState: "connecting", lastError: null });
    activeClient.retryNow();
  },

  setActiveView: (view) => set({ activeView: view }),
  setCommandPaletteOpen: (open) => set({ commandPaletteOpen: open }),
  setCallOverlayOpen: (open) => set({ callOverlayOpen: open }),

  newSession: async () => {
    if (!activeClient?.isConnected) return;
    try {
      const res = await activeClient.request("session.new", {});
      set({
        sessionId: res.session_id,
        messages: [],
        selectedConversation: null,
        activeView: "chat",
        sending: false,
      });
    } catch (err) {
      set({ lastError: err instanceof Error ? err.message : String(err) });
    }
  },

  sendMessage: async (rawText: string) => {
    const text = rawText.trim();
    if (!text || !activeClient?.isConnected) return;

    let sessionId = get().sessionId;
    if (!sessionId) {
      const res = await activeClient.request("session.new", {});
      sessionId = res.session_id;
      set({ sessionId });
    }

    const requestId = nextRequestId();
    const userMsg: ChatMessage = {
      id: `user-${requestId}`,
      role: "user",
      content: text,
      streaming: false,
      toolCalls: [],
      approvals: [],
    };

    const assistantMsg: ChatMessage = {
      id: requestId,
      role: "assistant",
      content: "",
      streaming: true,
      toolCalls: [],
      approvals: [],
    };

    set((state) => ({
      selectedConversation: null,
      activeView: "chat",
      sending: true,
      lastError: null,
      messages: [...state.messages, userMsg, assistantMsg],
    }));

    try {
      // chat.send may wait for tool calls and user approvals; give it a generous timeout (10m)
      const res = await activeClient.request(
        "chat.send",
        { text },
        { id: requestId, sessionId, timeoutMs: 600_000 },
      );
      set((state) => ({
        sending: false,
        messages: state.messages.map((m) =>
          m.id === requestId
            ? {
                ...m,
                streaming: false,
                content: m.content || res.text || "",
              }
            : m,
        ),
      }));
      void get().loadConversations();
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      set((state) => ({
        sending: false,
        lastError: message,
        messages: state.messages.map((m) =>
          m.id === requestId ? { ...m, streaming: false, error: message } : m,
        ),
      }));
    }
  },

  respondApproval: async (approvalId, decision) => {
    if (!activeClient?.isConnected) return;
    set((state) => ({
      messages: state.messages.map((m) => ({
        ...m,
        approvals: m.approvals.map((a) =>
          a.approvalId === approvalId && a.status === "pending"
            ? { ...a, status: "submitting", decision }
            : a,
        ),
      })),
    }));

    try {
      await activeClient.request(
        "approval.respond",
        { approval_id: approvalId, decision },
        { sessionId: get().sessionId },
      );
    } catch (err) {
      set({ lastError: err instanceof Error ? err.message : String(err) });
    }
  },

  loadConversations: async () => {
    if (!activeClient?.isConnected) return;
    set({ conversationsLoading: true });
    try {
      const res = await activeClient.request("conversations.list", {});
      set({ conversations: res.conversations, conversationsLoading: false });
    } catch {
      set({ conversationsLoading: false });
    }
  },

  openConversation: async (summary) => {
    if (!activeClient?.isConnected) return;
    try {
      const res = await activeClient.request("conversations.get", {
        date: summary.date,
        session_id: summary.session_id,
      });
      set({
        activeView: "chat",
        selectedConversation: {
          date: summary.date,
          sessionId: summary.session_id,
          title: summary.title,
          turns: res.turns,
        },
      });
    } catch (err) {
      set({ lastError: err instanceof Error ? err.message : String(err) });
    }
  },

  exitReadOnlyConversation: () => set({ selectedConversation: null, activeView: "chat" }),

  loadMemory: async () => {
    if (!activeClient?.isConnected) return;
    set({ memoryLoading: true });
    try {
      const res = await activeClient.request("memory.get", {});
      const firstDay = res.days[0] ?? null;
      let dailyContent = "";
      if (firstDay) {
        const daily = await activeClient.request("memory.get_daily", { date: firstDay });
        dailyContent = daily.content;
      }
      set({
        memoryLongTerm: res.long_term,
        memoryDays: res.days,
        selectedMemoryDay: firstDay,
        memoryDailyContent: dailyContent,
        memoryLoading: false,
      });
    } catch {
      set({ memoryLoading: false });
    }
  },

  loadDailyMemory: async (day: string) => {
    if (!activeClient?.isConnected) return;
    set({ selectedMemoryDay: day, memoryLoading: true });
    try {
      const daily = await activeClient.request("memory.get_daily", { date: day });
      set({ memoryDailyContent: daily.content, memoryLoading: false });
    } catch {
      set({ memoryLoading: false });
    }
  },

  loadSettings: async () => {
    if (!activeClient?.isConnected) return;
    set({ settingsLoading: true });
    try {
      const settings = await activeClient.request("settings.get", {});
      set({ settings, settingsLoading: false });
    } catch {
      set({ settingsLoading: false });
    }
  },

  applyEvent: (event: CleaEvent) => {
    switch (event.event) {
      case "assistant_token": {
        const delta = event.data.text;
        set((state) => ({
          messages: updateAssistantByRequest(state.messages, event.request_id, (m) => ({
            ...m,
            content: m.content + delta,
          })),
        }));
        break;
      }
      case "tool_call_started": {
        const d = event.data;
        set((state) => ({
          messages: updateAssistantByRequest(state.messages, event.request_id, (m) => ({
            ...m,
            toolCalls: [
              ...m.toolCalls,
              {
                callId: d.call_id,
                name: d.name,
                arguments: d.arguments ?? {},
                riskLevel: d.risk_level,
                status: "running",
              },
            ],
          })),
        }));
        break;
      }
      case "tool_call_finished": {
        const d = event.data;
        set((state) => ({
          messages: updateAssistantByRequest(state.messages, event.request_id, (m) => ({
            ...m,
            toolCalls: m.toolCalls.map((tc) =>
              tc.callId === d.call_id
                ? {
                    ...tc,
                    status: d.is_error ? "error" : "done",
                    output: d.output,
                    durationMs: d.duration_ms,
                  }
                : tc,
            ),
          })),
        }));
        break;
      }
      case "approval_requested": {
        const d = event.data;
        set((state) => ({
          messages: updateAssistantByRequest(state.messages, event.request_id, (m) => ({
            ...m,
            approvals: [
              ...m.approvals,
              {
                approvalId: d.approval_id,
                callId: d.call_id ?? null,
                tool: d.tool,
                arguments: d.arguments ?? {},
                riskLevel: d.risk_level,
                timeoutSeconds: d.timeout_seconds,
                status: "pending",
              },
            ],
          })),
        }));
        break;
      }
      case "approval_resolved": {
        const d = event.data;
        set((state) => ({
          messages: state.messages.map((m) => ({
            ...m,
            approvals: m.approvals.map((a) =>
              a.approvalId === d.approval_id
                ? { ...a, status: "resolved", decision: d.decision, reason: d.reason }
                : a,
            ),
          })),
        }));
        break;
      }
      case "task_status": {
        const task = event.data;
        set((state) => {
          const existingIdx = state.tasks.findIndex((t) => t.task_id === task.task_id);
          if (existingIdx === -1) {
            return { tasks: [task, ...state.tasks].slice(0, 100) };
          }
          const next = [...state.tasks];
          next[existingIdx] = task;
          return { tasks: next };
        });
        break;
      }
      case "voice_state": {
        set({ voiceState: event.data.state });
        break;
      }
      case "error": {
        set({ lastError: event.data.message });
        break;
      }
    }
  },
}));
