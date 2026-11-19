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
  ChatImageAttachment,
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
  VoiceListConversationsParams,
  VoiceListConversationsResult,
  VoiceConversationListItem,
  VoiceReadConversationParams,
  VoiceReadConversationResult,
  VoiceConversationTurnItem,
  VoiceSearchConversationsParams,
  VoiceSearchConversationsResult,
  VoiceSearchMemoryParams,
  VoiceSearchMemoryResult,
  MemorySearchHit,
  ConversationSearchHit,
  ConversationSearchSnippet,
  VoicePostToActiveChatParams,
  VoicePostToActiveChatResult,
  VoiceStartCallParams,
  VoiceStartCallResult,
  VoiceState,
  VoiceStatusResult,
  VoiceTrigger,
  MediaVolumeAction,
  ClipboardItem,
  OsOpenOrFocusAppParams,
  OsOpenOrFocusAppResult,
  OsControlMediaVolumeParams,
  OsControlMediaVolumeResult,
  OsReadClipboardParams,
  OsReadClipboardResult,
  OsSelectClipboardItemParams,
  OsSelectClipboardItemResult,
  OsOpenUrlParams,
  OsOpenUrlResult,
  OsControlSystemParams,
  OsControlSystemResult,
  OsWriteClipboardParams,
  OsWriteClipboardResult,
  OsTransformClipboardParams,
  OsTransformClipboardResult,
  OsSaveClipboardToFileParams,
  OsSaveClipboardToFileResult,
  OsOpenPathOrUrlParams,
  OsOpenPathOrUrlResult,
  OsInspectScreenParams,
  OsInspectScreenResult,
  UiElementItem,
  OsInspectUiElementsParams,
  OsInspectUiElementsResult,
  OsClickUiElementParams,
  OsClickUiElementResult,
  OsGuiActionParams,
  OsGuiActionResult,
  OsUiSnapshotParams,
  OsUiSnapshotResult,
  OsUiActParams,
  OsUiActResult,
  OsUiBatchParams,
  OsUiBatchResult,
  UiSnapshotElement,
  UiDiff,
  UiDiffChangedValue,
  UiBatchStep,
  GitWorktreeItem,
  GitCommitItem,
  WorkspaceGitInspectParams,
  WorkspaceGitInspectResult,
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
  ChatImageAttachment,
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
  VoiceListConversationsParams,
  VoiceListConversationsResult,
  VoiceConversationListItem,
  VoiceReadConversationParams,
  VoiceReadConversationResult,
  VoiceConversationTurnItem,
  VoiceSearchConversationsParams,
  VoiceSearchConversationsResult,
  VoiceSearchMemoryParams,
  VoiceSearchMemoryResult,
  MemorySearchHit,
  ConversationSearchHit,
  ConversationSearchSnippet,
  VoicePostToActiveChatParams,
  VoicePostToActiveChatResult,
  VoiceStartCallParams,
  VoiceStartCallResult,
  VoiceState,
  VoiceStatusResult,
  VoiceTrigger,
  MediaVolumeAction,
  ClipboardItem,
  OsOpenOrFocusAppParams,
  OsOpenOrFocusAppResult,
  OsControlMediaVolumeParams,
  OsControlMediaVolumeResult,
  OsReadClipboardParams,
  OsReadClipboardResult,
  OsSelectClipboardItemParams,
  OsSelectClipboardItemResult,
  OsOpenUrlParams,
  OsOpenUrlResult,
  OsControlSystemParams,
  OsControlSystemResult,
  OsWriteClipboardParams,
  OsWriteClipboardResult,
  OsTransformClipboardParams,
  OsTransformClipboardResult,
  OsSaveClipboardToFileParams,
  OsSaveClipboardToFileResult,
  OsOpenPathOrUrlParams,
  OsOpenPathOrUrlResult,
  OsInspectScreenParams,
  OsInspectScreenResult,
  UiElementItem,
  OsInspectUiElementsParams,
  OsInspectUiElementsResult,
  OsClickUiElementParams,
  OsClickUiElementResult,
  OsGuiActionParams,
  OsGuiActionResult,
  OsUiSnapshotParams,
  OsUiSnapshotResult,
  OsUiActParams,
  OsUiActResult,
  OsUiBatchParams,
  OsUiBatchResult,
  UiSnapshotElement,
  UiDiff,
  UiDiffChangedValue,
  UiBatchStep,
  GitWorktreeItem,
  GitCommitItem,
  WorkspaceGitInspectParams,
  WorkspaceGitInspectResult,
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
  "voice.list_conversations": VoiceListConversationsParams;
  "voice.search_conversations": VoiceSearchConversationsParams;
  "voice.read_conversation": VoiceReadConversationParams;
  "voice.search_memory": VoiceSearchMemoryParams;
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
  "os.open_or_focus_app": OsOpenOrFocusAppParams;
  "os.control_media_volume": OsControlMediaVolumeParams;
  "os.read_clipboard": OsReadClipboardParams;
  "os.select_clipboard_item": OsSelectClipboardItemParams;
  "os.open_url": OsOpenUrlParams;
  "os.control_system": OsControlSystemParams;
  "os.write_clipboard": OsWriteClipboardParams;
  "os.transform_clipboard": OsTransformClipboardParams;
  "os.save_clipboard_to_file": OsSaveClipboardToFileParams;
  "os.open_path_or_url": OsOpenPathOrUrlParams;
  "os.inspect_screen": OsInspectScreenParams;
  "os.inspect_ui_elements": OsInspectUiElementsParams;
  "os.click_ui_element": OsClickUiElementParams;
  "os.gui_action": OsGuiActionParams;
  "os.ui_snapshot": OsUiSnapshotParams;
  "os.ui_act": OsUiActParams;
  "os.ui_batch": OsUiBatchParams;
  "workspace.git_inspect": WorkspaceGitInspectParams;
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
  "voice.list_conversations": VoiceListConversationsResult;
  "voice.search_conversations": VoiceSearchConversationsResult;
  "voice.read_conversation": VoiceReadConversationResult;
  "voice.search_memory": VoiceSearchMemoryResult;
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
  "os.open_or_focus_app": OsOpenOrFocusAppResult;
  "os.control_media_volume": OsControlMediaVolumeResult;
  "os.read_clipboard": OsReadClipboardResult;
  "os.select_clipboard_item": OsSelectClipboardItemResult;
  "os.open_url": OsOpenUrlResult;
  "os.control_system": OsControlSystemResult;
  "os.write_clipboard": OsWriteClipboardResult;
  "os.transform_clipboard": OsTransformClipboardResult;
  "os.save_clipboard_to_file": OsSaveClipboardToFileResult;
  "os.open_path_or_url": OsOpenPathOrUrlResult;
  "os.inspect_screen": OsInspectScreenResult;
  "os.inspect_ui_elements": OsInspectUiElementsResult;
  "os.click_ui_element": OsClickUiElementResult;
  "os.gui_action": OsGuiActionResult;
  "os.ui_snapshot": OsUiSnapshotResult;
  "os.ui_act": OsUiActResult;
  "os.ui_batch": OsUiBatchResult;
  "workspace.git_inspect": WorkspaceGitInspectResult;
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
