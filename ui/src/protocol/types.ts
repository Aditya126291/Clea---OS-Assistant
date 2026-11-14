import type {
  ActivityCancelParams,
  ActivityCancelResult,
  ActivityListParams,
  ActivityListResult,
  ApprovalDecision,
  ApprovalReason,
  ApprovalRequestedData,
  ApprovalResolvedData,
  ApprovalRespondParams,
  ApprovalRespondResult,
  CallEndedData,
  CallStartedData,
  CallState,
  ChatCreateAndOpenParams,
  ChatCreateAndOpenResult,
  ChatDeleteParams,
  ChatDeleteResult,
  ChatRenameParams,
  ChatRenameResult,
  ChatSendFromVoiceParams,
  ChatSendFromVoiceResult,
  ChatSendParams,
  ChatSendResult,
  ChatFocusParams,
  ChatFocusResult,
  ConsolidationSessionStatus,
  ConversationCreatedData,
  ConversationGetParams,
  ConversationResumeParams,
  ConversationResumeResult,
  ConversationRenameParams,
  ConversationRenameResult,
  ConversationPinParams,
  ConversationPinResult,
  ConversationExportParams,
  ConversationExportResult,
  ConversationDeleteParams,
  ConversationDeleteResult,
  ConversationDeleteAllParams,
  ConversationDeleteAllResult,
  ConversationUndoDeleteParams,
  ConversationUndoDeleteResult,
  ConversationResumeMessage,
  ConversationUpdatedData,
  ConversationDeletedData,
  ConversationsGetResult,
  ConversationsListResult,
  ConversationSummary,
  ConversationTurn,
  DailyParams,
  DevEmitSampleTaskResult,
  EpisodeItem,
  Event,
  HealthPingResult,
  MemoryAddFactParams,
  MemoryAddFactResult,
  MemoryArchiveItem,
  MemoryArchiveResult,
  MemoryDeleteFactParams,
  MemoryDeleteFactResult,
  MemoryEpisodesParams,
  MemoryEpisodesResult,
  MemoryFactItem,
  MemoryFactsResult,
  MemoryForgetParams,
  MemoryForgetRequest,
  MemoryForgetResult,
  MemoryGetDailyResult,
  MemoryGetResult,
  MemoryReconsolidateParams,
  MemoryReconsolidateResult,
  MemorySectionItem,
  MemoryStatusResult,
  MemoryUpdatedData,
  PermissionTierInfo,
  Request,
  ResponseFrame,
  RiskTier,
  ScreenCaptureStateData,
  ScreenCaptureTarget1 as ScreenCaptureTarget,
  SessionNewResult,
  SettingsGetResult,
  TaskKind,
  TaskState,
  TaskStatusData,
  ToolCallFinishedData,
  ToolCallStartedData,
  ToolInfo,
  TranscriptData,
  TranscriptMessageItem,
  TranscriptRole,
  TranscriptSummaryItem,
  TranscriptsGetParams,
  TranscriptsGetResult,
  TranscriptsListParams,
  TranscriptsListResult,
  UiNavigateData,
  UiNavigateParams,
  UiNavigateResult,
  UiView,
  UsageData,
  VoiceDelegateTaskParams,
  VoiceDelegateTaskResult,
  VoiceEndCallParams,
  VoiceEndCallResult,
  VoiceHistoryParams,
  VoiceHistoryResult,
  VoiceHistoryMessageItem,
  VoiceSearchConversationsParams,
  VoiceSearchConversationsResult,
  ConversationSearchHit,
  VoicePostToActiveChatParams,
  VoicePostToActiveChatResult,
  VoiceStartCallParams,
  VoiceStartCallResult,
  VoiceState,
  VoiceStatusResult,
  VoiceTrigger,
} from "./generated";

export type {
  ActivityCancelParams,
  ActivityCancelResult,
  ActivityListParams,
  ActivityListResult,
  ApprovalDecision,
  ApprovalReason,
  ApprovalRequestedData,
  ApprovalResolvedData,
  ApprovalRespondParams,
  ApprovalRespondResult,
  CallEndedData,
  CallStartedData,
  CallState,
  ChatCreateAndOpenParams,
  ChatCreateAndOpenResult,
  ChatDeleteParams,
  ChatDeleteResult,
  ChatRenameParams,
  ChatRenameResult,
  ChatSendFromVoiceParams,
  ChatSendFromVoiceResult,
  ChatSendParams,
  ChatSendResult,
  ChatFocusParams,
  ChatFocusResult,
  ConsolidationSessionStatus,
  ConversationCreatedData,
  ConversationGetParams,
  ConversationResumeParams,
  ConversationResumeResult,
  ConversationRenameParams,
  ConversationRenameResult,
  ConversationPinParams,
  ConversationPinResult,
  ConversationExportParams,
  ConversationExportResult,
  ConversationDeleteParams,
  ConversationDeleteResult,
  ConversationDeleteAllParams,
  ConversationDeleteAllResult,
  ConversationUndoDeleteParams,
  ConversationUndoDeleteResult,
  ConversationResumeMessage,
  ConversationUpdatedData,
  ConversationDeletedData,
  ConversationsGetResult,
  ConversationsListResult,
  ConversationSummary,
  ConversationTurn,
  DailyParams,
  DevEmitSampleTaskResult,
  EpisodeItem,
  Event as CleaEvent,
  HealthPingResult,
  MemoryAddFactParams,
  MemoryAddFactResult,
  MemoryArchiveItem,
  MemoryArchiveResult,
  MemoryDeleteFactParams,
  MemoryDeleteFactResult,
  MemoryEpisodesParams,
  MemoryEpisodesResult,
  MemoryFactItem,
  MemoryFactsResult,
  MemoryForgetParams,
  MemoryForgetRequest,
  MemoryForgetResult,
  MemoryGetDailyResult,
  MemoryGetResult,
  MemoryReconsolidateParams,
  MemoryReconsolidateResult,
  MemorySectionItem,
  MemoryStatusResult,
  MemoryUpdatedData,
  PermissionTierInfo,
  Request as CleaRequest,
  ResponseFrame,
  RiskTier,
  ScreenCaptureStateData,
  ScreenCaptureTarget,
  SessionNewResult,
  SettingsGetResult,
  TaskKind,
  TaskState,
  TaskStatusData,
  ToolCallFinishedData,
  ToolCallStartedData,
  ToolInfo,
  TranscriptData,
  TranscriptMessageItem,
  TranscriptRole,
  TranscriptSummaryItem,
  TranscriptsGetParams,
  TranscriptsGetResult,
  TranscriptsListParams,
  TranscriptsListResult,
  UiNavigateData,
  UiNavigateParams,
  UiNavigateResult,
  UiView,
  UsageData,
  VoiceDelegateTaskParams,
  VoiceDelegateTaskResult,
  VoiceEndCallParams,
  VoiceEndCallResult,
  VoiceHistoryParams,
  VoiceHistoryResult,
  VoiceHistoryMessageItem,
  VoiceSearchConversationsParams,
  VoiceSearchConversationsResult,
  ConversationSearchHit,
  VoicePostToActiveChatParams,
  VoicePostToActiveChatResult,
  VoiceStartCallParams,
  VoiceStartCallResult,
  VoiceState,
  VoiceStatusResult,
  VoiceTrigger,
};

export type CleaMethod = NonNullable<Request["method"]>;

export interface MethodParamsMap {
  "health.ping": Record<string, never>;
  "session.new": Record<string, never>;
  "chat.send": ChatSendParams;
  "approval.respond": ApprovalRespondParams;
  "memory.get": Record<string, never>;
  "memory.get_daily": DailyParams;
  "memory.episodes": MemoryEpisodesParams;
  "memory.facts": Record<string, never>;
  "memory.archive": Record<string, never>;
  "memory.status": Record<string, never>;
  "memory.reconsolidate": MemoryReconsolidateParams;
  "memory.forget": MemoryForgetParams;
  "transcripts.list": TranscriptsListParams;
  "transcripts.get": TranscriptsGetParams;
  "conversations.list": Record<string, never>;
  "conversations.get": ConversationGetParams;
  "conversation.list": Record<string, never>;
  "conversation.get": ConversationGetParams;
  "conversation.resume": ConversationResumeParams;
  "conversation.rename": ConversationRenameParams;
  "conversation.pin": ConversationPinParams;
  "conversation.export": ConversationExportParams;
  "conversation.delete": ConversationDeleteParams;
  "conversation.delete_all": ConversationDeleteAllParams;
  "conversation.undo_delete": ConversationUndoDeleteParams;
  "settings.get": Record<string, never>;
  "dev.emit_sample_task": Record<string, never>;
  "voice.start_call": VoiceStartCallParams;
  "voice.end_call": VoiceEndCallParams;
  "voice.status": Record<string, never>;
  "voice.delegate_task": VoiceDelegateTaskParams;
  "chat.focus": ChatFocusParams;
  "voice.history": VoiceHistoryParams;
  "voice.search_conversations": VoiceSearchConversationsParams;
  "voice.post_to_active_chat": VoicePostToActiveChatParams;
  "ui.navigate": UiNavigateParams;
  "chat.create_and_open": ChatCreateAndOpenParams;
  "chat.send_from_voice": ChatSendFromVoiceParams;
  "chat.rename": ChatRenameParams;
  "chat.delete": ChatDeleteParams;
  "memory.add_fact": MemoryAddFactParams;
  "memory.delete_fact": MemoryDeleteFactParams;
  "activity.list": ActivityListParams;
  "activity.cancel": ActivityCancelParams;
}

export interface MethodResultMap {
  "health.ping": HealthPingResult;
  "session.new": SessionNewResult;
  "chat.send": ChatSendResult;
  "approval.respond": ApprovalRespondResult;
  "memory.get": MemoryGetResult;
  "memory.get_daily": MemoryGetDailyResult;
  "memory.episodes": MemoryEpisodesResult;
  "memory.facts": MemoryFactsResult;
  "memory.archive": MemoryArchiveResult;
  "memory.status": MemoryStatusResult;
  "memory.reconsolidate": MemoryReconsolidateResult;
  "memory.forget": MemoryForgetResult;
  "transcripts.list": TranscriptsListResult;
  "transcripts.get": TranscriptsGetResult;
  "conversations.list": ConversationsListResult;
  "conversations.get": ConversationsGetResult;
  "conversation.list": ConversationsListResult;
  "conversation.get": ConversationsGetResult;
  "conversation.resume": ConversationResumeResult;
  "conversation.rename": ConversationRenameResult;
  "conversation.pin": ConversationPinResult;
  "conversation.export": ConversationExportResult;
  "conversation.delete": ConversationDeleteResult;
  "conversation.delete_all": ConversationDeleteAllResult;
  "conversation.undo_delete": ConversationUndoDeleteResult;
  "settings.get": SettingsGetResult;
  "dev.emit_sample_task": DevEmitSampleTaskResult;
  "voice.start_call": VoiceStartCallResult;
  "voice.end_call": VoiceEndCallResult;
  "voice.status": VoiceStatusResult;
  "voice.delegate_task": VoiceDelegateTaskResult;
  "chat.focus": ChatFocusResult;
  "voice.history": VoiceHistoryResult;
  "voice.search_conversations": VoiceSearchConversationsResult;
  "voice.post_to_active_chat": VoicePostToActiveChatResult;
  "ui.navigate": UiNavigateResult;
  "chat.create_and_open": ChatCreateAndOpenResult;
  "chat.send_from_voice": ChatSendFromVoiceResult;
  "chat.rename": ChatRenameResult;
  "chat.delete": ChatDeleteResult;
  "memory.add_fact": MemoryAddFactResult;
  "memory.delete_fact": MemoryDeleteFactResult;
  "activity.list": ActivityListResult;
  "activity.cancel": ActivityCancelResult;
}

export type ConnectionState =
  "idle" | "connecting" | "connected" | "reconnecting" | "daemon_offline" | "auth_error";

export type AppView = "chat" | "activity" | "memory" | "settings" | "voice";

export interface ToolCallItem {
  callId: string;
  name: string;
  arguments: Record<string, unknown>;
  riskLevel: RiskTier;
  status: "running" | "done" | "error";
  output?: string;
  durationMs?: number;
}

export interface ApprovalItem {
  approvalId: string;
  callId: string | null;
  tool: string;
  arguments: Record<string, unknown>;
  riskLevel: RiskTier;
  timeoutSeconds: number;
  status: "pending" | "submitting" | "resolved";
  decision?: ApprovalDecision;
  reason?: ApprovalReason;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  streaming: boolean;
  error?: string;
  toolCalls: ToolCallItem[];
  approvals: ApprovalItem[];
}

export interface CleaConnectionInfo {
  token: string;
  wsUrl: string;
}

export interface StartDaemonResult {
  ok: boolean;
  error?: string;
}

export interface CleaDesktopBridge {
  isElectron: true;
  getConnection: () => Promise<CleaConnectionInfo | null>;
  getAutoLaunch?: () => Promise<boolean>;
  setAutoLaunch?: (enabled: boolean) => Promise<boolean>;
  startDaemon?: () => Promise<StartDaemonResult>;
  saveFile?: (defaultPath: string, content: string) => Promise<boolean>;
  onQuickAskFocus?: (cb: () => void) => () => void;
  closeQuickAsk?: () => void;
}

declare global {
  interface Window {
    cleaDesktop?: CleaDesktopBridge;
  }
}
