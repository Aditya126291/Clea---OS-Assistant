import type {
  ApprovalDecision,
  ApprovalReason,
  ApprovalRequestedData,
  ApprovalResolvedData,
  ApprovalRespondParams,
  ApprovalRespondResult,
  CallEndedData,
  CallStartedData,
  CallState,
  ChatSendParams,
  ChatSendResult,
  ConversationGetParams,
  ConversationsGetResult,
  ConversationsListResult,
  ConversationSummary,
  ConversationTurn,
  DailyParams,
  Event,
  HealthPingResult,
  MemoryGetDailyResult,
  MemoryGetResult,
  PermissionTierInfo,
  Request,
  ResponseFrame,
  RiskTier,
  ScreenCaptureStateData,
  ScreenCaptureTarget1 as ScreenCaptureTarget,
  SessionNewResult,
  SettingsGetResult,
  TaskState,
  TaskStatusData,
  ToolCallFinishedData,
  ToolCallStartedData,
  ToolInfo,
  TranscriptData,
  TranscriptRole,
  UsageData,
  VoiceDelegateTaskParams,
  VoiceDelegateTaskResult,
  VoiceEndCallParams,
  VoiceEndCallResult,
  VoiceStartCallParams,
  VoiceStartCallResult,
  VoiceState,
  VoiceStatusResult,
  VoiceTrigger,
} from "./generated";

export type {
  ApprovalDecision,
  ApprovalReason,
  ApprovalRequestedData,
  ApprovalResolvedData,
  ApprovalRespondParams,
  ApprovalRespondResult,
  CallEndedData,
  CallStartedData,
  CallState,
  ChatSendParams,
  ChatSendResult,
  ConversationGetParams,
  ConversationsGetResult,
  ConversationsListResult,
  ConversationSummary,
  ConversationTurn,
  DailyParams,
  Event as CleaEvent,
  HealthPingResult,
  MemoryGetDailyResult,
  MemoryGetResult,
  PermissionTierInfo,
  Request as CleaRequest,
  ResponseFrame,
  RiskTier,
  ScreenCaptureStateData,
  ScreenCaptureTarget,
  SessionNewResult,
  SettingsGetResult,
  TaskState,
  TaskStatusData,
  ToolCallFinishedData,
  ToolCallStartedData,
  ToolInfo,
  TranscriptData,
  TranscriptRole,
  UsageData,
  VoiceDelegateTaskParams,
  VoiceDelegateTaskResult,
  VoiceEndCallParams,
  VoiceEndCallResult,
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
  "conversations.list": Record<string, never>;
  "conversations.get": ConversationGetParams;
  "settings.get": Record<string, never>;
  "voice.start_call": VoiceStartCallParams;
  "voice.end_call": VoiceEndCallParams;
  "voice.status": Record<string, never>;
  "voice.delegate_task": VoiceDelegateTaskParams;
}

export interface MethodResultMap {
  "health.ping": HealthPingResult;
  "session.new": SessionNewResult;
  "chat.send": ChatSendResult;
  "approval.respond": ApprovalRespondResult;
  "memory.get": MemoryGetResult;
  "memory.get_daily": MemoryGetDailyResult;
  "conversations.list": ConversationsListResult;
  "conversations.get": ConversationsGetResult;
  "settings.get": SettingsGetResult;
  "voice.start_call": VoiceStartCallResult;
  "voice.end_call": VoiceEndCallResult;
  "voice.status": VoiceStatusResult;
  "voice.delegate_task": VoiceDelegateTaskResult;
}

export type ConnectionState =
  "idle" | "connecting" | "connected" | "reconnecting" | "daemon_offline" | "auth_error";

export type AppView = "chat" | "activity" | "memory" | "settings";

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
  onQuickAskFocus?: (cb: () => void) => () => void;
  closeQuickAsk?: () => void;
}

declare global {
  interface Window {
    cleaDesktop?: CleaDesktopBridge;
  }
}
