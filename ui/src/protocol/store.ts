import { create } from "zustand";
import { CleaClient, type CleaClientOptions } from "./client";
import type {
  AppView,
  ApprovalDecision,
  CallState,
  ChatMessage,
  CleaEvent,
  ConnectionState,
  ConversationSummary,
  ConversationTurn,
  EpisodeItem,
  ScreenCaptureTarget,
  SettingsGetResult,
  TaskStatusData,
  TranscriptData,
  TranscriptSummaryItem,
  TranscriptsGetResult,
  UsageData,
  VoiceState,
} from "./types";

export interface SelectedConversation {
  date: string;
  sessionId: string;
  title: string;
  turns: ConversationTurn[];
}

export interface UndoToastState {
  message: string;
  sessionId: string | null;
  expiresAt: number;
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
  resumedSessionId: string | null;
  resumeLoading: boolean;
  resumeError: string | null;
  undoToast: UndoToastState | null;

  tasks: TaskStatusData[];

  memoryLongTerm: string;
  memoryDays: string[];
  selectedMemoryDay: string | null;
  memoryDailyContent: string;
  memoryLoading: boolean;
  lastConsolidated: string | null;
  failedConsolidation: string | null;
  episodes: EpisodeItem[];
  transcripts: TranscriptSummaryItem[];
  selectedTranscript: TranscriptsGetResult | null;
  memorySubView: "overview" | "transcripts";

  settings: SettingsGetResult | null;
  settingsLoading: boolean;

  voiceState: VoiceState;
  callState: CallState;
  activeCallId: string | null;
  callOverlayOpen: boolean;
  screenCaptureActive: boolean;
  screenCaptureTarget: ScreenCaptureTarget | null;
  voiceTranscripts: TranscriptData[];
  lastCallUsage: UsageData | null;

  connect: (clientOptions?: CleaClientOptions) => void;
  disconnect: () => void;
  retryNow: () => void;
  setActiveView: (view: AppView) => void;
  setMemorySubView: (view: "overview" | "transcripts") => void;
  setCommandPaletteOpen: (open: boolean) => void;
  setCallOverlayOpen: (open: boolean) => void;
  clearError: () => void;

  newSession: () => Promise<void>;
  sendMessage: (text: string) => Promise<void>;
  respondApproval: (approvalId: string, decision: ApprovalDecision) => Promise<void>;

  loadConversations: () => Promise<void>;
  openConversation: (summary: ConversationSummary) => Promise<void>;
  exitReadOnlyConversation: () => void;
  renameConversation: (sessionId: string, newTitle: string) => Promise<void>;
  pinConversation: (sessionId: string, pinned: boolean) => Promise<void>;
  exportConversation: (sessionId: string) => Promise<void>;
  deleteConversation: (sessionId: string, deleteEpisodes?: boolean) => Promise<void>;
  deleteAllConversations: () => Promise<void>;
  undoDelete: () => Promise<void>;
  dismissUndoToast: () => void;
  retryResume: () => Promise<void>;

  loadMemory: () => Promise<void>;
  loadDailyMemory: (day: string) => Promise<void>;
  loadEpisodes: () => Promise<void>;
  loadTranscripts: () => Promise<void>;
  openTranscript: (sessionId: string) => Promise<void>;
  emitDevSampleTask: () => Promise<void>;
  loadSettings: () => Promise<void>;

  applyEvent: (event: CleaEvent) => void;
}

let activeClient: CleaClient | null = null;
let localMsgSeq = 0;
let sessionEpoch = 0;

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

let undoToastTimer: ReturnType<typeof setTimeout> | null = null;

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
  resumedSessionId: null,
  resumeLoading: false,
  resumeError: null,
  undoToast: null,

  tasks: [],

  memoryLongTerm: "",
  memoryDays: [],
  selectedMemoryDay: null,
  memoryDailyContent: "",
  memoryLoading: false,
  lastConsolidated: null,
  failedConsolidation: null,
  episodes: [],
  transcripts: [],
  selectedTranscript: null,
  memorySubView: "overview",

  settings: null,
  settingsLoading: false,

  voiceState: "idle",
  callState: "idle",
  activeCallId: null,
  callOverlayOpen: false,
  screenCaptureActive: false,
  screenCaptureTarget: null,
  voiceTranscripts: [],
  lastCallUsage: null,

  connect: (clientOptions = {}) => {
    if (activeClient) {
      activeClient.dispose();
    }
    set({ connectionState: "connecting", lastError: null });

    const client = new CleaClient({
      ...clientOptions,
      onConnected: () => {
        set({ connectionState: "connected", reconnectAttempt: 0, lastError: null, sending: false });
        const currentSid = get().sessionId;
        if (currentSid) {
          void activeClient
            ?.request("conversation.resume", { session_id: currentSid })
            .catch(() => {
              void get().newSession();
            });
        } else {
          void get().newSession();
        }
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
  setMemorySubView: (view) => set({ memorySubView: view }),
  setCommandPaletteOpen: (open) => set({ commandPaletteOpen: open }),
  setCallOverlayOpen: (open) => set({ callOverlayOpen: open }),
  clearError: () => set({ lastError: null }),

  newSession: async () => {
    const epoch = ++sessionEpoch;
    set({
      sessionId: null,
      messages: [],
      selectedConversation: null,
      resumedSessionId: null,
      resumeLoading: false,
      resumeError: null,
      activeView: "chat",
      sending: false,
      lastError: null,
    });
    if (!activeClient?.isConnected) return;
    try {
      const res = await activeClient.request("session.new", {});
      if (epoch !== sessionEpoch || get().sessionId !== null) return;
      set({ sessionId: res.session_id });
    } catch (err) {
      if (epoch === sessionEpoch) {
        set({ lastError: err instanceof Error ? err.message : String(err) });
      }
    }
  },

  sendMessage: async (rawText: string) => {
    const text = rawText.trim();
    if (!text || !activeClient?.isConnected) return;

    const epoch = ++sessionEpoch;
    let sessionId = get().sessionId;
    if (!sessionId) {
      try {
        const res = await activeClient.request("session.new", {});
        sessionId = res.session_id;
        set({ sessionId });
      } catch (err) {
        set({ lastError: err instanceof Error ? err.message : String(err) });
        return;
      }
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
      if (epoch !== sessionEpoch) return;
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
      if (epoch !== sessionEpoch) return;
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
      set((state) => ({
        lastError: err instanceof Error ? err.message : String(err),
        messages: state.messages.map((m) => ({
          ...m,
          approvals: m.approvals.map((a) =>
            a.approvalId === approvalId && a.status === "submitting"
              ? { ...a, status: "pending", decision: undefined }
              : a,
          ),
        })),
      }));
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
    set({
      activeView: "chat",
      sessionId: summary.session_id,
      resumedSessionId: summary.session_id,
      resumeLoading: true,
      resumeError: null,
      selectedConversation: null,
    });
    try {
      const res = await activeClient.request("conversation.resume", {
        session_id: summary.session_id,
      });
      const msgs: ChatMessage[] = res.messages.map((m, idx) => ({
        id: `resumed-${res.session_id}-${idx}`,
        role: m.role as "user" | "assistant",
        content: m.text,
        streaming: false,
        toolCalls: [],
        approvals: [],
      }));
      set({
        sessionId: res.session_id,
        resumedSessionId: res.session_id,
        messages: msgs,
        resumeLoading: false,
        resumeError: null,
      });
    } catch (err) {
      set({
        resumeLoading: false,
        resumeError: err instanceof Error ? err.message : String(err),
      });
    }
  },

  exitReadOnlyConversation: () => set({ selectedConversation: null, activeView: "chat" }),

  retryResume: async () => {
    const sid = get().resumedSessionId || get().sessionId;
    if (!sid || !activeClient?.isConnected) return;
    set({ resumeLoading: true, resumeError: null });
    try {
      const res = await activeClient.request("conversation.resume", {
        session_id: sid,
      });
      const msgs: ChatMessage[] = res.messages.map((m, idx) => ({
        id: `resumed-${res.session_id}-${idx}`,
        role: m.role as "user" | "assistant",
        content: m.text,
        streaming: false,
        toolCalls: [],
        approvals: [],
      }));
      set({
        sessionId: res.session_id,
        resumedSessionId: res.session_id,
        messages: msgs,
        resumeLoading: false,
        resumeError: null,
      });
    } catch (err) {
      set({
        resumeLoading: false,
        resumeError: err instanceof Error ? err.message : String(err),
      });
    }
  },

  renameConversation: async (sessionId: string, newTitle: string) => {
    if (!activeClient?.isConnected) return;
    const trimmed = newTitle.trim();
    if (!trimmed) return;
    try {
      await activeClient.request("conversation.rename", {
        session_id: sessionId,
        title: trimmed,
      });
      set((state) => ({
        conversations: state.conversations.map((c) =>
          c.session_id === sessionId ? { ...c, title: trimmed } : c,
        ),
      }));
    } catch (err) {
      set({ lastError: err instanceof Error ? err.message : String(err) });
    }
  },

  pinConversation: async (sessionId: string, pinned: boolean) => {
    if (!activeClient?.isConnected) return;
    try {
      await activeClient.request("conversation.pin", {
        session_id: sessionId,
        pinned,
      });
      set((state) => ({
        conversations: state.conversations.map((c) =>
          c.session_id === sessionId ? { ...c, pinned } : c,
        ),
      }));
      void get().loadConversations();
    } catch (err) {
      set({ lastError: err instanceof Error ? err.message : String(err) });
    }
  },

  exportConversation: async (sessionId: string) => {
    if (!activeClient?.isConnected) return;
    try {
      const res = await activeClient.request("conversation.export", {
        session_id: sessionId,
      });
      if (window.cleaDesktop?.saveFile) {
        await window.cleaDesktop.saveFile(res.filename, res.content);
      } else {
        const blob = new Blob([res.content], { type: "text/markdown;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = res.filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
      }
    } catch (err) {
      set({ lastError: err instanceof Error ? err.message : String(err) });
    }
  },

  deleteConversation: async (sessionId: string, deleteEpisodes?: boolean) => {
    if (!activeClient?.isConnected) return;
    try {
      await activeClient.request("conversation.delete", {
        session_id: sessionId,
        delete_episodes: !!deleteEpisodes,
      });

      if (get().sessionId === sessionId || get().resumedSessionId === sessionId) {
        void get().newSession();
      }

      if (undoToastTimer) {
        clearTimeout(undoToastTimer);
        undoToastTimer = null;
      }
      set({
        undoToast: {
          sessionId,
          expiresAt: Date.now() + 10_000,
          message: "Conversation deleted",
        },
      });
      undoToastTimer = setTimeout(() => {
        set((state) => (state.undoToast?.sessionId === sessionId ? { undoToast: null } : {}));
        undoToastTimer = null;
      }, 10_000);

      void get().loadConversations();
    } catch (err) {
      set({ lastError: err instanceof Error ? err.message : String(err) });
    }
  },

  deleteAllConversations: async () => {
    if (!activeClient?.isConnected) return;
    try {
      await activeClient.request("conversation.delete_all", {
        confirmation: "DELETE",
      });
      void get().newSession();

      if (undoToastTimer) {
        clearTimeout(undoToastTimer);
        undoToastTimer = null;
      }
      set({
        undoToast: {
          sessionId: "*",
          expiresAt: Date.now() + 10_000,
          message: "All conversations deleted",
        },
      });
      undoToastTimer = setTimeout(() => {
        set((state) => (state.undoToast?.sessionId === "*" ? { undoToast: null } : {}));
        undoToastTimer = null;
      }, 10_000);

      void get().loadConversations();
    } catch (err) {
      set({ lastError: err instanceof Error ? err.message : String(err) });
    }
  },

  undoDelete: async () => {
    const toast = get().undoToast;
    if (!toast || !activeClient?.isConnected) return;
    const target = toast.sessionId;
    if (undoToastTimer) {
      clearTimeout(undoToastTimer);
      undoToastTimer = null;
    }
    set({ undoToast: null });
    try {
      await activeClient.request("conversation.undo_delete", { session_id: target });
      await get().loadConversations();
    } catch (err) {
      set({ lastError: err instanceof Error ? err.message : String(err) });
    }
  },

  dismissUndoToast: () => {
    if (undoToastTimer) {
      clearTimeout(undoToastTimer);
      undoToastTimer = null;
    }
    set({ undoToast: null });
  },

  loadMemory: async () => {
    if (!activeClient?.isConnected) return;
    set({ memoryLoading: true });
    try {
      const res = await activeClient.request("memory.get", {});
      const days = res.days ?? [];
      const firstDay = days[0] ?? null;
      let dailyContent = "";
      if (firstDay) {
        const daily = await activeClient.request("memory.get_daily", { date: firstDay });
        dailyContent = daily.content;
      }
      set({
        memoryLongTerm: res.long_term,
        memoryDays: days,
        selectedMemoryDay: firstDay,
        memoryDailyContent: dailyContent,
        lastConsolidated: res.last_consolidated ?? null,
        failedConsolidation: res.failed_consolidation ?? null,
        memoryLoading: false,
      });
      void get().loadEpisodes();
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

  loadEpisodes: async () => {
    if (!activeClient?.isConnected) return;
    try {
      const res = await activeClient.request("memory.episodes", { limit: 20 });
      set({ episodes: res.episodes });
    } catch {
      // keep existing episodes
    }
  },

  loadTranscripts: async () => {
    if (!activeClient?.isConnected) return;
    try {
      const res = await activeClient.request("transcripts.list", { limit: 50 });
      set({ transcripts: res.transcripts });
    } catch {
      // keep existing transcripts
    }
  },

  openTranscript: async (sessionId: string) => {
    if (!activeClient?.isConnected) return;
    try {
      const res = await activeClient.request("transcripts.get", { session_id: sessionId });
      set({ selectedTranscript: res });
    } catch {
      set({ selectedTranscript: null });
    }
  },

  emitDevSampleTask: async () => {
    if (!activeClient?.isConnected) return;
    try {
      await activeClient.request("dev.emit_sample_task", {});
    } catch {
      // ignore
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
        if (task.kind === "chat_turn") {
          break;
        }
        set((state) => {
          const existingIdx = state.tasks.findIndex((t) => t.task_id === task.task_id);
          if (existingIdx === -1) {
            return { tasks: [task, ...state.tasks].slice(0, 50) };
          }
          const next = [...state.tasks];
          next[existingIdx] = task;
          return { tasks: next.slice(0, 50) };
        });
        break;
      }
      case "voice_state": {
        set({ voiceState: event.data.state });
        break;
      }
      case "call_started": {
        set({
          activeCallId: event.data.call_id,
          callState: event.data.call_state ?? "active",
          callOverlayOpen: true,
          voiceTranscripts: [],
        });
        break;
      }
      case "call_ended": {
        set({
          activeCallId: null,
          callState: event.data.call_state ?? "idle",
          voiceState: "idle",
          screenCaptureActive: false,
          screenCaptureTarget: null,
          callOverlayOpen: false,
        });
        break;
      }
      case "transcript": {
        const entry = event.data;
        set((state) => ({
          voiceTranscripts: [...state.voiceTranscripts, entry].slice(-200),
        }));
        break;
      }
      case "screen_capture_state": {
        set({
          screenCaptureActive: event.data.active,
          screenCaptureTarget: event.data.target ?? null,
        });
        break;
      }
      case "usage": {
        set({ lastCallUsage: event.data });
        break;
      }
      case "conversation_updated": {
        const d = event.data;
        set((state) => ({
          conversations: state.conversations.map((c) =>
            c.session_id === d.session_id
              ? {
                  ...c,
                  title: d.title ?? c.title,
                  pinned: typeof d.pinned === "boolean" ? d.pinned : c.pinned,
                  turns: d.turns ?? c.turns,
                  updated_at: d.updated_at ?? c.updated_at,
                }
              : c,
          ),
        }));
        void get().loadConversations();
        break;
      }
      case "conversation_deleted": {
        const d = event.data;
        if (d.all) {
          set({ conversations: [] });
          if (get().sessionId) {
            void get().newSession();
          }
        } else if (d.session_ids && d.session_ids.length > 0) {
          const idSet = new Set(d.session_ids);
          set((state) => ({
            conversations: state.conversations.filter((c) => !idSet.has(c.session_id)),
          }));
          const current = get().sessionId;
          const resumed = get().resumedSessionId;
          if ((current && idSet.has(current)) || (resumed && idSet.has(resumed))) {
            void get().newSession();
          }
        }
        break;
      }
      case "error": {
        set({ lastError: event.data.message });
        break;
      }
    }
  },
}));
