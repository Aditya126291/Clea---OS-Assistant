import { create } from "zustand";
import { CleaClient, type CleaClientOptions } from "./client";
import type {
  AppView,
  ApprovalDecision,
  CallState,
  ChatImageAttachment,
  ChatMessage,
  CleaEvent,
  ConnectionState,
  ConversationSummary,
  ConversationTurn,
  ConversationSearchHit,
  VoiceHistoryMessageItem,
  EpisodeItem,
  MemoryArchiveItem,
  MemorySectionItem,
  MemoryStatusResult,
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
  previousView: AppView;
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
  memoryFacts: MemorySectionItem[];
  memoryArchive: MemoryArchiveItem[];
  memoryArchiveRaw: string;
  memoryStatus: MemoryStatusResult | null;
  reconsolidating: Record<string, boolean>;
  transcripts: TranscriptSummaryItem[];
  selectedTranscript: TranscriptsGetResult | null;
  memorySubView: "overview" | "transcripts";

  settings: SettingsGetResult | null;
  settingsLoading: boolean;

  voiceState: VoiceState;
  voiceError: string | null;
  callState: CallState;
  activeCallId: string | null;
  callOverlayOpen: boolean;
  screenCaptureActive: boolean;
  screenCaptureTarget: ScreenCaptureTarget | null;
  voiceTranscripts: TranscriptData[];
  voiceHistoryMessages: VoiceHistoryMessageItem[];
  voiceHistoryLoading: boolean;
  lastCallUsage: UsageData | null;

  connect: (clientOptions?: CleaClientOptions) => void;
  disconnect: () => void;
  retryNow: () => void;
  setActiveView: (view: AppView) => void;
  toggleCompanionLog: () => void;
  setMemorySubView: (view: "overview" | "transcripts") => void;
  setCommandPaletteOpen: (open: boolean) => void;
  setCallOverlayOpen: (open: boolean) => void;
  syncVoiceStatus: () => Promise<boolean>;
  startVoiceCall: () => Promise<void>;
  endVoiceCall: () => Promise<void>;
  loadVoiceHistory: () => Promise<void>;
  searchConversations: (query: string, limit?: number) => Promise<ConversationSearchHit[]>;
  postToActiveChat: (content: string, label?: string) => Promise<boolean>;
  clearError: () => void;

  newSession: () => Promise<void>;
  sendMessage: (text: string, image?: ChatImageAttachment) => Promise<void>;
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
  loadMemoryFacts: () => Promise<void>;
  loadMemoryArchive: () => Promise<void>;
  loadMemoryStatus: () => Promise<void>;
  reconsolidate: (sessionId: string) => Promise<boolean>;
  forgetFact: (id: string, query?: string) => Promise<boolean>;
  loadTranscripts: () => Promise<void>;
  openTranscript: (sessionId: string) => Promise<void>;
  emitDevSampleTask: () => Promise<void>;
  loadSettings: () => Promise<void>;

  applyEvent: (event: CleaEvent) => void;
}

let activeClient: CleaClient | null = null;
let localMsgSeq = 0;
let sessionEpoch = 0;
let voiceSyncEpoch = 0;
let voiceOverlayAutoOpened = false;

function isActiveVoiceState(state: VoiceState | null | undefined): boolean {
  return (
    state === "listening" ||
    state === "thinking" ||
    state === "speaking" ||
    state === "reconnecting"
  );
}

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

function mapTranscriptMessages(
  sessionId: string,
  rawMessages: ReadonlyArray<{
    role: string;
    text: string;
    tool_calls?: Array<Record<string, unknown>>;
  }>,
): ChatMessage[] {
  return rawMessages.map((m, idx) => ({
    id: `resumed-${sessionId}-${idx}`,
    role: (m.role === "user" ? "user" : "assistant") as "user" | "assistant",
    content: m.text,
    streaming: false,
    toolCalls: (m.tool_calls || []).map((tc, tcIdx) => ({
      callId: `tc-${idx}-${tcIdx}`,
      name: (tc.name as string) || "tool",
      arguments: (tc.arguments as Record<string, unknown>) || {},
      riskLevel: "read_only" as const,
      status: "done" as const,
      output: typeof tc.output === "string" ? tc.output : undefined,
    })),
    approvals: [],
  }));
}

let undoToastTimer: ReturnType<typeof setTimeout> | null = null;

function notifyChatFocus(sessionId: string | null): void {
  if (activeClient?.isConnected) {
    void activeClient
      .request("chat.focus", sessionId ? { session_id: sessionId, clear: false } : { clear: true })
      .catch(() => {});
  }
}

export const useCleaStore = create<CleaStoreState>((set, get) => ({
  connectionState: "idle",
  reconnectAttempt: 0,
  lastError: null,
  activeView: "chat",
  previousView: "chat",
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
  memoryFacts: [],
  memoryArchive: [],
  memoryArchiveRaw: "",
  memoryStatus: null,
  reconsolidating: {},
  transcripts: [],
  selectedTranscript: null,
  memorySubView: "overview",

  settings: null,
  settingsLoading: false,

  voiceState: "idle",
  voiceError: null,
  callState: "idle",
  activeCallId: null,
  callOverlayOpen: false,
  screenCaptureActive: false,
  screenCaptureTarget: null,
  voiceTranscripts: [],
  voiceHistoryMessages: [],
  voiceHistoryLoading: false,
  lastCallUsage: null,

  connect: (clientOptions = {}) => {
    if (activeClient) {
      activeClient.dispose();
    }
    set({ connectionState: "connecting", lastError: null });

    const client = new CleaClient({
      ...clientOptions,
      onConnected: () => {
        const disconnectedAt = new Date().toISOString();
        set((state) => ({
          connectionState: "connected",
          reconnectAttempt: 0,
          lastError: null,
          sending: false,
          messages: state.messages.map((m) => (m.streaming ? { ...m, streaming: false } : m)),
          tasks: state.tasks.map((t) =>
            t.status === "running" || t.status === "queued"
              ? {
                  ...t,
                  status: "failed",
                  detail: t.detail ?? "Interrupted by daemon restart",
                  finished_at: t.finished_at ?? disconnectedAt,
                }
              : t,
          ),
        }));
        const currentSid = get().sessionId;
        if (currentSid) {
          if (get().activeView === "chat") {
            notifyChatFocus(currentSid);
          } else {
            notifyChatFocus(null);
          }
          void activeClient
            ?.request("conversation.resume", { session_id: currentSid })
            .then((res) => {
              if (get().messages.length === 0 && res.messages) {
                set({
                  messages: res.messages.map((m, idx) => ({
                    id: `resumed-${res.session_id}-${idx}`,
                    role: m.role as "user" | "assistant",
                    content: m.text,
                    streaming: false,
                    toolCalls: (m.tool_calls || []).map((tc, tcIdx) => ({
                      callId: `tc-${idx}-${tcIdx}`,
                      name: (tc.name as string) || "tool",
                      arguments: (tc.arguments as Record<string, unknown>) || {},
                      riskLevel: "read_only" as const,
                      status: "done" as const,
                      output: typeof tc.output === "string" ? tc.output : undefined,
                    })),
                    approvals: [],
                  })),
                });
              }
            })
            .catch(() => {
              void get().newSession();
            });
        } else {
          void get().newSession();
        }
        void get().loadConversations();
        void get().loadSettings();
        void get().syncVoiceStatus();
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
    voiceOverlayAutoOpened = false;
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

  setActiveView: (view) => {
    const current = get().activeView;
    if (current !== "voice") {
      set({ previousView: current });
    }
    set({ activeView: view });
    if (view !== "chat") {
      notifyChatFocus(null);
    } else {
      const sid = get().resumedSessionId || get().sessionId || null;
      notifyChatFocus(sid);
    }
  },
  toggleCompanionLog: () => {
    const current = get().activeView;
    if (current === "voice") {
      const prev = get().previousView;
      const target = prev && prev !== "voice" ? prev : "activity";
      get().setActiveView(target);
    } else {
      set({ previousView: current });
      get().setActiveView("voice");
    }
  },
  setMemorySubView: (view) => set({ memorySubView: view }),
  setCommandPaletteOpen: (open) => set({ commandPaletteOpen: open }),
  setCallOverlayOpen: (open) => {
    if (!open) {
      voiceOverlayAutoOpened = false;
    }
    set({ callOverlayOpen: open });
  },

  syncVoiceStatus: async () => {
    if (!activeClient?.isConnected) return false;
    const epoch = voiceSyncEpoch;
    try {
      const status = await activeClient.request("voice.status", {});
      const daemonCallState = status.call_state ?? "idle";
      const daemonVoiceState = status.voice_state ?? "idle";
      const isActive =
        daemonCallState === "active" ||
        daemonCallState === "connecting" ||
        isActiveVoiceState(daemonVoiceState);

      if (epoch !== voiceSyncEpoch) {
        if (isActive && status.call_id) {
          set((state) =>
            state.callState !== "idle" && !state.activeCallId
              ? { activeCallId: status.call_id ?? null }
              : {},
          );
        }
        return isActive;
      }

      if (isActive) {
        voiceOverlayAutoOpened = true;
        set((state) => ({
          activeCallId: status.call_id ?? state.activeCallId,
          callState: daemonCallState === "idle" ? "active" : daemonCallState,
          voiceState: isActiveVoiceState(daemonVoiceState) ? daemonVoiceState : "listening",
          voiceError: null,
          callOverlayOpen: true,
          screenCaptureActive: Boolean(status.screen_capture_active),
          screenCaptureTarget: status.screen_capture_target ?? null,
        }));
        return true;
      }

      voiceOverlayAutoOpened = false;
      set((state) => {
        const preserveError = state.voiceState === "error" && Boolean(state.voiceError);
        return {
          activeCallId: null,
          callState: "idle",
          voiceState: preserveError ? "error" : "idle",
          voiceError: preserveError ? state.voiceError : null,
          callOverlayOpen: preserveError ? state.callOverlayOpen : false,
          screenCaptureActive: false,
          screenCaptureTarget: null,
        };
      });
      return false;
    } catch {
      return false;
    }
  },

  loadVoiceHistory: async () => {
    if (!activeClient?.isConnected) return;
    set({ voiceHistoryLoading: true });
    try {
      const res = await activeClient.request("voice.history", { limit: 200 });
      set({ voiceHistoryMessages: res.messages ?? [], voiceHistoryLoading: false });
    } catch {
      set({ voiceHistoryLoading: false });
    }
  },

  searchConversations: async (query: string, limit: number = 5) => {
    if (!activeClient?.isConnected) return [];
    try {
      const res = await activeClient.request("voice.search_conversations", { query, limit });
      return res.results ?? [];
    } catch {
      return [];
    }
  },

  postToActiveChat: async (content: string, label?: string) => {
    if (!activeClient?.isConnected) return false;
    try {
      const res = await activeClient.request("voice.post_to_active_chat", { content, label });
      return res.ok;
    } catch {
      return false;
    }
  },

  startVoiceCall: async () => {
    if (!activeClient?.isConnected) {
      set({
        callOverlayOpen: true,
        voiceState: "error",
        voiceError: "Daemon not connected.",
      });
      return;
    }

    const { callState, activeCallId, voiceState } = get();
    if (callState === "active" || callState === "connecting" || activeCallId) {
      set({
        callOverlayOpen: true,
        voiceError: null,
        voiceState: isActiveVoiceState(voiceState) ? voiceState : "listening",
      });
      void get().syncVoiceStatus();
      return;
    }

    voiceSyncEpoch += 1;
    set({ callOverlayOpen: true, voiceError: null });
    try {
      const res = await activeClient.request("voice.start_call", { trigger: "ui" });
      voiceSyncEpoch += 1;
      set((state) => ({
        activeCallId: res.call_id ?? state.activeCallId,
        callState: res.call_state ?? "active",
        voiceState: isActiveVoiceState(res.voice_state)
          ? (res.voice_state as VoiceState)
          : isActiveVoiceState(state.voiceState)
            ? state.voiceState
            : "thinking",
        voiceError: null,
        callOverlayOpen: true,
      }));
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      if (/already active/i.test(msg)) {
        const attached = await get().syncVoiceStatus();
        if (attached) {
          return;
        }
      }
      voiceOverlayAutoOpened = false;
      set({
        voiceState: "error",
        voiceError: msg || "Voice process not running.",
      });
    }
  },

  endVoiceCall: async () => {
    const { activeCallId, callState, voiceState } = get();
    voiceSyncEpoch += 1;
    voiceOverlayAutoOpened = false;
    const hasLiveCall =
      Boolean(activeCallId) ||
      callState === "active" ||
      callState === "connecting" ||
      isActiveVoiceState(voiceState);
    if (activeClient?.isConnected && hasLiveCall) {
      try {
        await activeClient.request(
          "voice.end_call",
          activeCallId
            ? {
                call_id: activeCallId,
                reason: "ui_button",
              }
            : {
                reason: "ui_button",
              },
        );
      } catch (err) {
        console.error("Failed ending voice call:", err);
      }
    }
    set({
      callOverlayOpen: false,
      voiceState: "idle",
      voiceError: null,
      callState: "idle",
      activeCallId: null,
      screenCaptureActive: false,
      screenCaptureTarget: null,
    });
  },
  clearError: () => set({ lastError: null }),

  newSession: async () => {
    notifyChatFocus(null);
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

  sendMessage: async (rawText: string, image?: ChatImageAttachment) => {
    const text = rawText.trim();
    if ((!text && !image) || get().sending || !activeClient?.isConnected) return;

    const epoch = ++sessionEpoch;
    let sessionId = get().sessionId;
    if (!sessionId) {
      try {
        const res = await activeClient.request("session.new", {});
        sessionId = res.session_id;
        set({ sessionId });
        notifyChatFocus(sessionId);
      } catch (err) {
        set({ lastError: err instanceof Error ? err.message : String(err) });
        return;
      }
    } else {
      notifyChatFocus(sessionId);
    }

    const requestId = nextRequestId();
    const displayText =
      text ||
      (image
        ? `Please inspect this image.\n[Attached image: ${image.name ?? "clipboard_image.png"}]`
        : "");
    const userMsg: ChatMessage = {
      id: `user-${requestId}`,
      role: "user",
      content: displayText,
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
        image ? { text, image } : { text },
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
                content: res.text || m.content || "",
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
    notifyChatFocus(summary.session_id);
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
        toolCalls: (m.tool_calls || []).map((tc, tcIdx) => ({
          callId: `tc-${idx}-${tcIdx}`,
          name: (tc.name as string) || "tool",
          arguments: (tc.arguments as Record<string, unknown>) || {},
          riskLevel: "read_only" as const,
          status: "done" as const,
          output: typeof tc.output === "string" ? tc.output : undefined,
        })),
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

  exitReadOnlyConversation: () => {
    notifyChatFocus(null);
    set({ selectedConversation: null, activeView: "chat" });
  },

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
        toolCalls: (m.tool_calls || []).map((tc, tcIdx) => ({
          callId: `tc-${idx}-${tcIdx}`,
          name: (tc.name as string) || "tool",
          arguments: (tc.arguments as Record<string, unknown>) || {},
          riskLevel: "read_only" as const,
          status: "done" as const,
          output: typeof tc.output === "string" ? tc.output : undefined,
        })),
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
      const res = await activeClient.request("conversation.rename", {
        session_id: sessionId,
        title: trimmed,
      });
      const finalTitle = res.title || trimmed;
      set((state) => ({
        conversations: state.conversations.map((c) =>
          c.session_id === sessionId ? { ...c, title: finalTitle } : c,
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
      const paramSid = target === "*" ? undefined : target || undefined;
      await activeClient.request(
        "conversation.undo_delete",
        paramSid ? { session_id: paramSid } : {},
      );
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
      void get().loadMemoryFacts();
      void get().loadMemoryArchive();
      void get().loadMemoryStatus();
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
      const res = await activeClient.request("memory.episodes", { limit: 50 });
      set({ episodes: res.episodes });
    } catch {
      // keep existing episodes
    }
  },

  loadMemoryFacts: async () => {
    if (!activeClient?.isConnected) return;
    try {
      const res = await activeClient.request("memory.facts", {});
      set({
        memoryFacts: res.sections ?? [],
        lastConsolidated: res.last_consolidated ?? null,
        failedConsolidation: res.failed_consolidation ?? null,
      });
    } catch {
      // keep existing
    }
  },

  loadMemoryArchive: async () => {
    if (!activeClient?.isConnected) return;
    try {
      const res = await activeClient.request("memory.archive", {});
      set({
        memoryArchive: res.entries ?? [],
        memoryArchiveRaw: res.raw ?? "",
      });
    } catch {
      // keep existing
    }
  },

  loadMemoryStatus: async () => {
    if (!activeClient?.isConnected) return;
    try {
      const res = await activeClient.request("memory.status", {});
      set({
        memoryStatus: res,
      });
    } catch {
      // keep existing
    }
  },

  reconsolidate: async (sessionId: string) => {
    if (!activeClient?.isConnected) return false;
    set((s) => ({ reconsolidating: { ...s.reconsolidating, [sessionId]: true } }));
    try {
      const res = await activeClient.request("memory.reconsolidate", { session_id: sessionId });
      void get().loadMemoryStatus();
      void get().loadEpisodes();
      void get().loadMemoryFacts();
      void get().loadMemory();
      return res.ok;
    } catch {
      return false;
    } finally {
      set((s) => {
        const next = { ...s.reconsolidating };
        delete next[sessionId];
        return { reconsolidating: next };
      });
    }
  },

  forgetFact: async (id: string, query?: string) => {
    if (!activeClient?.isConnected) return false;
    try {
      const res = await activeClient.request("memory.forget", { id, query: query ?? id });
      void get().loadMemoryFacts();
      void get().loadMemory();
      return res.ok ?? true;
    } catch {
      return false;
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
        voiceSyncEpoch += 1;
        const nextState = event.data.state;
        const errorMsg =
          ("error" in event.data && typeof event.data.error === "string"
            ? event.data.error
            : null) ?? (nextState === "error" ? "Voice error" : null);
        const activeVoice = isActiveVoiceState(nextState);
        let shouldHydrateCallId = false;

        set((state) => {
          if (nextState === "error") {
            voiceOverlayAutoOpened = false;
            return {
              voiceState: "error",
              voiceError: errorMsg,
              callOverlayOpen: true,
            };
          }
          if (activeVoice) {
            if (!state.callOverlayOpen) {
              voiceOverlayAutoOpened = true;
            }
            if (!state.activeCallId && activeClient?.isConnected) {
              shouldHydrateCallId = true;
            }
            return {
              voiceState: nextState,
              voiceError: null,
              callState: state.callState === "idle" ? "active" : state.callState,
              callOverlayOpen: true,
            };
          }
          // Transition back to idle: auto-close when opened by a daemon/wake-word call
          const shouldAutoClose = voiceOverlayAutoOpened || Boolean(state.activeCallId);
          voiceOverlayAutoOpened = false;
          return {
            voiceState: "idle",
            voiceError: null,
            callState: shouldAutoClose ? "idle" : state.callState,
            activeCallId: shouldAutoClose ? null : state.activeCallId,
            screenCaptureActive: shouldAutoClose ? false : state.screenCaptureActive,
            screenCaptureTarget: shouldAutoClose ? null : state.screenCaptureTarget,
            callOverlayOpen: shouldAutoClose ? false : state.callOverlayOpen,
          };
        });
        if (shouldHydrateCallId) {
          void get().syncVoiceStatus();
        }
        break;
      }
      case "call_started": {
        voiceSyncEpoch += 1;
        voiceOverlayAutoOpened = true;
        set((state) => ({
          activeCallId: event.data.call_id,
          callState: event.data.call_state ?? "active",
          voiceState: isActiveVoiceState(state.voiceState) ? state.voiceState : "thinking",
          voiceError: null,
          callOverlayOpen: true,
          voiceTranscripts: [],
        }));
        break;
      }
      case "call_ended": {
        voiceSyncEpoch += 1;
        voiceOverlayAutoOpened = false;
        set((state) => ({
          activeCallId: null,
          callState: event.data.call_state ?? "idle",
          voiceState: state.voiceState === "error" ? "error" : "idle",
          screenCaptureActive: false,
          screenCaptureTarget: null,
          callOverlayOpen: state.voiceState === "error" ? true : false,
        }));
        void get().loadVoiceHistory();
        break;
      }
      case "transcript": {
        const entry = event.data;
        set((state) => {
          const trimmedText = entry.text.trim();
          const lastTranscript = state.voiceTranscripts[state.voiceTranscripts.length - 1];
          const isDuplicateTranscript =
            Boolean(lastTranscript) &&
            lastTranscript.role === entry.role &&
            lastTranscript.text.trim() === trimmedText &&
            Boolean(lastTranscript.final) === Boolean(entry.final);
          const nextVoiceTranscripts = isDuplicateTranscript
            ? state.voiceTranscripts
            : [...state.voiceTranscripts, entry].slice(-200);
          let nextHistory = state.voiceHistoryMessages;
          if (entry.final && trimmedText) {
            const currentCallId = state.activeCallId || undefined;
            const lastHistory =
              state.voiceHistoryMessages[state.voiceHistoryMessages.length - 1];
            const isDuplicateHistory =
              Boolean(lastHistory) &&
              lastHistory.role === entry.role &&
              lastHistory.text.trim() === trimmedText &&
              lastHistory.call_id === currentCallId;
            if (!isDuplicateHistory) {
              nextHistory = [
                ...state.voiceHistoryMessages,
                {
                  role: entry.role,
                  text: entry.text,
                  timestamp: entry.timestamp || new Date().toISOString(),
                  call_id: currentCallId,
                },
              ];
            }
          }
          return {
            voiceTranscripts: nextVoiceTranscripts,
            voiceHistoryMessages: nextHistory,
          };
        });
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
        if (d.session_id === "voice_companion") {
          void get().loadVoiceHistory();
        }
        set((state) => {
          let nextMessages = state.messages;
          if (state.sessionId && state.sessionId === d.session_id) {
            if (d.user_message) {
              const userMsgId = `user-${d.request_id || nextRequestId()}`;
              const exists = state.messages.some((m) => m.id === userMsgId);
              if (!exists) {
                const userMsg: ChatMessage = {
                  id: userMsgId,
                  role: "user",
                  content: d.user_message,
                  streaming: false,
                  toolCalls: [],
                  approvals: [],
                };
                const assistantMsgId = d.request_id || nextRequestId();
                const assistantMsg: ChatMessage = {
                  id: assistantMsgId,
                  role: "assistant",
                  content: "",
                  streaming: true,
                  toolCalls: [],
                  approvals: [],
                };
                nextMessages = [...state.messages, userMsg, assistantMsg];
              }
            }
            if (d.assistant_message) {
              const reqId = d.request_id || event.request_id || undefined;
              const matched = reqId
                ? nextMessages.some((m) => m.id === reqId)
                : false;
              const hasStreaming = nextMessages.some(
                (m) => m.role === "assistant" && m.streaming,
              );
              const lastMsg = nextMessages[nextMessages.length - 1];
              const isDuplicateLastAssistant =
                Boolean(lastMsg) &&
                lastMsg.role === "assistant" &&
                (lastMsg.content.trim() === d.assistant_message.trim() ||
                  (Boolean(reqId) && lastMsg.id === reqId));
              if (matched || hasStreaming) {
                nextMessages = updateAssistantByRequest(nextMessages, reqId, (m) => ({
                  ...m,
                  content: d.assistant_message || m.content,
                  streaming: false,
                }));
              } else if (isDuplicateLastAssistant) {
                nextMessages = nextMessages.map((m, idx) =>
                  idx === nextMessages.length - 1
                    ? {
                        ...m,
                        content: d.assistant_message || m.content,
                        streaming: false,
                      }
                    : m,
                );
              } else {
                nextMessages = [
                  ...nextMessages,
                  {
                    id: reqId || nextRequestId(),
                    role: "assistant",
                    content: d.assistant_message,
                    streaming: false,
                    toolCalls: [],
                    approvals: [],
                  },
                ];
              }
            }
          }

          return {
            messages: nextMessages,
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
          };
        });
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
      case "ui.navigate": {
        const d = event.data;
        if (d.open_command_palette) {
          set({ commandPaletteOpen: true });
          break;
        }
        if (d.view === "chat" && d.conversation_id) {
          const cid = d.conversation_id;
          const switching = get().sessionId !== cid;
          const needsLoad = switching || get().messages.length === 0;
          sessionEpoch += 1;
          set((state) => ({
            activeView: "chat",
            sessionId: cid,
            resumedSessionId: cid,
            selectedConversation: null,
            resumeError: null,
            messages: switching ? [] : state.messages,
          }));
          notifyChatFocus(cid);
          void get().loadConversations();
          if (needsLoad && activeClient?.isConnected) {
            const client = activeClient;
            void client
              .request("conversation.resume", { session_id: cid })
              .then((res) => mapTranscriptMessages(res.session_id, res.messages ?? []))
              .catch(() =>
                client
                  .request("transcripts.get", { session_id: cid })
                  .then((res) => mapTranscriptMessages(res.session_id, res.messages ?? [])),
              )
              .then((loaded) => {
                if (get().sessionId !== cid) return;
                set((state) => {
                  const hasStreaming = state.messages.some((m) => m.streaming);
                  if (hasStreaming) {
                    let tailStart = -1;
                    for (let i = state.messages.length - 1; i >= 0; i -= 1) {
                      if (state.messages[i].id.startsWith("user-voice-chat-")) {
                        tailStart = i;
                        break;
                      }
                    }
                    const activeTail =
                      tailStart >= 0
                        ? state.messages.slice(tailStart)
                        : state.messages.filter((m) => m.streaming);
                    return { messages: [...loaded, ...activeTail] };
                  }
                  if (state.messages.length === 0) {
                    return { messages: loaded };
                  }
                  return {};
                });
              })
              .catch(() => {});
          }
          break;
        }

        const targetView = d.view as AppView;
        set((state) => ({
          activeView: targetView,
          selectedConversation: targetView === "chat" ? null : state.selectedConversation,
        }));
        if (targetView !== "chat") {
          notifyChatFocus(null);
        } else {
          notifyChatFocus(get().sessionId);
        }
        if (targetView === "memory") {
          void get().loadMemory();
          void get().loadMemoryFacts();
          void get().loadEpisodes();
          void get().loadMemoryStatus();
        } else if (targetView === "voice") {
          void get().loadVoiceHistory();
        } else if (targetView === "chat") {
          void get().loadConversations();
        } else if (targetView === "activity" && activeClient?.isConnected) {
          void activeClient
            .request("activity.list", { limit: 50 })
            .then((res) => set({ tasks: res.tasks ?? [] }))
            .catch(() => {});
        } else if (targetView === "settings") {
          void get().loadSettings();
        }
        break;
      }
      case "conversation_created": {
        const d = event.data;
        void get().loadConversations();
        if (d.open_in_ui) {
          sessionEpoch += 1;
          const initialMessages: ChatMessage[] = d.initial_message
            ? [
                {
                  id: `created-${d.session_id}-0`,
                  role: "assistant",
                  content: d.initial_message,
                  streaming: false,
                  toolCalls: [],
                  approvals: [],
                },
              ]
            : [];
          set({
            activeView: "chat",
            sessionId: d.session_id,
            resumedSessionId: d.session_id,
            selectedConversation: null,
            resumeError: null,
            messages: initialMessages,
          });
          notifyChatFocus(d.session_id);
        }
        break;
      }
      case "memory.updated": {
        void get().loadMemoryFacts();
        void get().loadMemory();
        void get().loadMemoryStatus();
        break;
      }
      case "error": {
        set((state) => ({
          lastError: event.data.message,
          messages: event.request_id
            ? updateAssistantByRequest(state.messages, event.request_id, (m) => ({
                ...m,
                streaming: false,
                error: event.data.message,
              }))
            : state.messages,
        }));
        break;
      }
    }
  },
}));
