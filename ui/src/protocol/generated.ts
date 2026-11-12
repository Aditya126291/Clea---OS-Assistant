/* eslint-disable */
/**
 * AUTO-GENERATED FILE — DO NOT EDIT BY HAND.
 * Source of truth: src/clea/core/protocol.py
 * Regenerate with: uv run python scripts/gen_ts_types.py
 */

export type Request =
  | HealthPingRequest
  | SessionNewRequest
  | ChatSendRequest
  | ApprovalRespondRequest
  | MemoryGetRequest
  | MemoryGetDailyRequest
  | ConversationsListRequest
  | ConversationsGetRequest
  | SettingsGetRequest
  | VoiceStartCallRequest
  | VoiceEndCallRequest
  | VoiceStatusRequest
  | VoiceDelegateTaskRequest;
export type FrameType = "request";
export type Id = string;
export type SessionId = string | null;
export type Method = "health.ping";
export type FrameType1 = "request";
export type Id1 = string;
export type SessionId1 = string | null;
export type Method1 = "session.new";
export type FrameType2 = "request";
export type Id2 = string;
export type SessionId2 = string | null;
export type Method2 = "chat.send";
export type Text = string;
export type FrameType3 = "request";
export type Id3 = string;
export type SessionId3 = string | null;
export type Method3 = "approval.respond";
export type ApprovalId = string;
export type Decision = "allow" | "deny";
export type FrameType4 = "request";
export type Id4 = string;
export type SessionId4 = string | null;
export type Method4 = "memory.get";
export type FrameType5 = "request";
export type Id5 = string;
export type SessionId5 = string | null;
export type Method5 = "memory.get_daily";
/**
 * ISO date, YYYY-MM-DD
 */
export type Date = string;
export type FrameType6 = "request";
export type Id6 = string;
export type SessionId6 = string | null;
export type Method6 = "conversations.list";
export type FrameType7 = "request";
export type Id7 = string;
export type SessionId7 = string | null;
export type Method7 = "conversations.get";
/**
 * ISO date, YYYY-MM-DD
 */
export type Date1 = string;
export type SessionId8 = string;
export type FrameType8 = "request";
export type Id8 = string;
export type SessionId9 = string | null;
export type Method8 = "settings.get";
export type FrameType9 = "request";
export type Id9 = string;
export type SessionId10 = string | null;
export type Method9 = "voice.start_call";
export type Trigger = "manual" | "wake_word" | "hotkey";
export type FrameType10 = "request";
export type Id10 = string;
export type SessionId11 = string | null;
export type Method10 = "voice.end_call";
export type CallId = string | null;
export type Reason = string | null;
export type FrameType11 = "request";
export type Id11 = string;
export type SessionId12 = string | null;
export type Method11 = "voice.status";
export type FrameType12 = "request";
export type Id12 = string;
export type SessionId13 = string | null;
export type Method12 = "voice.delegate_task";
export type Description = string;
export type CallId1 = string | null;
export type ToolCallId = string | null;
export type FrameType13 = "response";
export type Id13 = string;
export type Ok = boolean;
export type SessionId14 = string | null;
export type Error = string | null;
export type Event =
  | AssistantTokenEvent
  | ToolCallStartedEvent
  | ToolCallFinishedEvent
  | ApprovalRequestedEvent
  | ApprovalResolvedEvent
  | TaskStatusEvent
  | VoiceStateEvent
  | CallStartedEvent
  | CallEndedEvent
  | TranscriptEvent
  | ScreenCaptureStateEvent
  | UsageEvent
  | ErrorEvent;
export type FrameType14 = "event";
export type RequestId = string | null;
export type SessionId15 = string | null;
export type Event1 = "assistant_token";
export type Text1 = string;
export type FrameType15 = "event";
export type RequestId1 = string | null;
export type SessionId16 = string | null;
export type Event2 = "tool_call_started";
export type CallId2 = string;
export type Name = string;
export type RiskLevel = "read_only" | "reversible" | "destructive";
export type FrameType16 = "event";
export type RequestId2 = string | null;
export type SessionId17 = string | null;
export type Event3 = "tool_call_finished";
export type CallId3 = string;
export type Name1 = string;
export type IsError = boolean;
export type Output = string;
/**
 * Execution time, excluding time spent awaiting approval
 */
export type DurationMs = number;
export type FrameType17 = "event";
export type RequestId3 = string | null;
export type SessionId18 = string | null;
export type Event4 = "approval_requested";
export type ApprovalId1 = string;
export type CallId4 = string | null;
export type Tool = string;
export type RiskLevel1 = "read_only" | "reversible" | "destructive";
export type TimeoutSeconds = number;
export type FrameType18 = "event";
export type RequestId4 = string | null;
export type SessionId19 = string | null;
export type Event5 = "approval_resolved";
export type ApprovalId2 = string;
export type Decision1 = "allow" | "deny";
export type Reason1 = "user" | "timeout";
export type FrameType19 = "event";
export type RequestId5 = string | null;
export type SessionId20 = string | null;
export type Event6 = "task_status";
export type TaskId = string;
export type Title = string;
export type Status = "running" | "succeeded" | "failed";
export type StartedAt = string;
export type FinishedAt = string | null;
export type Detail = string | null;
export type FrameType20 = "event";
export type RequestId6 = string | null;
export type SessionId21 = string | null;
export type Event7 = "voice_state";
export type State = "idle" | "listening" | "thinking" | "speaking";
export type FrameType21 = "event";
export type RequestId7 = string | null;
export type SessionId22 = string | null;
export type Event8 = "call_started";
export type CallId5 = string;
export type CallState = "idle" | "connecting" | "active" | "ending";
export type StartedAt1 = string | null;
export type FrameType22 = "event";
export type RequestId8 = string | null;
export type SessionId23 = string | null;
export type Event9 = "call_ended";
export type CallId6 = string;
export type CallState1 = "idle" | "connecting" | "active" | "ending";
export type Reason2 = string | null;
export type DurationSeconds = number | null;
export type FrameType23 = "event";
export type RequestId9 = string | null;
export type SessionId24 = string | null;
export type Event10 = "transcript";
export type Role = "user" | "assistant";
export type Text2 = string;
export type Final = boolean;
export type CallId7 = string | null;
export type FrameType24 = "event";
export type RequestId10 = string | null;
export type SessionId25 = string | null;
export type Event11 = "screen_capture_state";
export type Active = boolean;
export type Target = ("active_window" | "full_screen") | null;
export type CallId8 = string | null;
export type FrameType25 = "event";
export type RequestId11 = string | null;
export type SessionId26 = string | null;
export type Event12 = "usage";
export type CallId9 = string | null;
export type InputTokens = number;
export type OutputTokens = number;
export type TotalTokens = number;
export type CachedTokens = number | null;
export type ThoughtTokens = number | null;
export type FrameType26 = "event";
export type RequestId12 = string | null;
export type SessionId27 = string | null;
export type Event13 = "error";
export type Message = string;
export type Code = string | null;
export type Status1 = "ok";
export type SessionId28 = string;
export type Text3 = string;
export type ApprovalId3 = string;
export type Decision2 = "allow" | "deny";
export type LongTerm = string;
/**
 * ISO dates with a daily log, newest first
 */
export type Days = string[];
export type Date2 = string;
export type Content = string;
export type Date3 = string;
export type SessionId29 = string;
export type Title1 = string;
export type Turns = number;
export type LastTime = string;
export type Conversations = ConversationSummary[];
export type Date4 = string;
export type SessionId30 = string;
export type Time = string;
export type User = string;
export type Assistant = string;
export type Turns1 = ConversationTurn[];
export type Provider = string;
export type Model = string;
export type Host = string;
export type Port = number;
export type ApprovalTimeoutSeconds = number;
export type Name2 = string;
export type Description1 = string;
export type RiskLevel2 = "read_only" | "reversible" | "destructive";
export type Tools = ToolInfo[];
export type Tier = "read_only" | "reversible" | "destructive";
export type Policy = "auto_allow" | "ask" | "ask_confirm";
export type PermissionTiers = PermissionTierInfo[];
export type CallId10 = string;
export type CallState2 = "idle" | "connecting" | "active" | "ending";
export type VoiceState = "idle" | "listening" | "thinking" | "speaking";
export type CallId11 = string | null;
export type CallState3 = "idle" | "connecting" | "active" | "ending";
export type VoiceState1 = "idle" | "listening" | "thinking" | "speaking";
export type CallId12 = string | null;
export type CallState4 = "idle" | "connecting" | "active" | "ending";
export type VoiceState2 = "idle" | "listening" | "thinking" | "speaking";
export type ScreenCaptureActive = boolean;
export type ScreenCaptureTarget = ("active_window" | "full_screen") | null;
export type TaskId1 = string;
export type Status2 = "running" | "succeeded" | "failed";
export type Output1 = string;
export type RiskTier = "read_only" | "reversible" | "destructive";
export type ApprovalDecision = "allow" | "deny";
export type ApprovalReason = "user" | "timeout";
export type TaskState = "running" | "succeeded" | "failed";
export type VoiceState3 = "idle" | "listening" | "thinking" | "speaking";
export type CallState5 = "idle" | "connecting" | "active" | "ending";
export type TranscriptRole = "user" | "assistant";
export type ScreenCaptureTarget1 = "active_window" | "full_screen";
export type VoiceTrigger = "manual" | "wake_word" | "hotkey";

export interface CleaProtocolSchema {
  request: Request;
  response: ResponseFrame;
  event: Event;
  method_results: MethodResults;
  risk_tier: RiskTier;
  approval_decision: ApprovalDecision;
  approval_reason: ApprovalReason;
  task_state: TaskState;
  voice_state: VoiceState3;
  call_state: CallState5;
  transcript_role: TranscriptRole;
  screen_capture_target: ScreenCaptureTarget1;
  voice_trigger: VoiceTrigger;
  health_ping_request: HealthPingRequest;
  session_new_request: SessionNewRequest;
  chat_send_request: ChatSendRequest;
  approval_respond_request: ApprovalRespondRequest;
  memory_get_request: MemoryGetRequest;
  memory_get_daily_request: MemoryGetDailyRequest;
  conversations_list_request: ConversationsListRequest;
  conversations_get_request: ConversationsGetRequest;
  settings_get_request: SettingsGetRequest;
  voice_start_call_request: VoiceStartCallRequest;
  voice_end_call_request: VoiceEndCallRequest;
  voice_status_request: VoiceStatusRequest;
  voice_delegate_task_request: VoiceDelegateTaskRequest;
  assistant_token_event: AssistantTokenEvent;
  tool_call_started_event: ToolCallStartedEvent;
  tool_call_finished_event: ToolCallFinishedEvent;
  approval_requested_event: ApprovalRequestedEvent;
  approval_resolved_event: ApprovalResolvedEvent;
  task_status_event: TaskStatusEvent;
  voice_state_event: VoiceStateEvent;
  call_started_event: CallStartedEvent;
  call_ended_event: CallEndedEvent;
  transcript_event: TranscriptEvent;
  screen_capture_state_event: ScreenCaptureStateEvent;
  usage_event: UsageEvent;
  error_event: ErrorEvent;
}
export interface HealthPingRequest {
  frame_type?: FrameType;
  id?: Id;
  session_id?: SessionId;
  method?: Method;
  params?: _EmptyParams;
}
export interface _EmptyParams {}
export interface SessionNewRequest {
  frame_type?: FrameType1;
  id?: Id1;
  session_id?: SessionId1;
  method?: Method1;
  params?: _EmptyParams;
}
export interface ChatSendRequest {
  frame_type?: FrameType2;
  id?: Id2;
  session_id?: SessionId2;
  method?: Method2;
  params: ChatSendParams;
}
export interface ChatSendParams {
  text: Text;
}
export interface ApprovalRespondRequest {
  frame_type?: FrameType3;
  id?: Id3;
  session_id?: SessionId3;
  method?: Method3;
  params: ApprovalRespondParams;
}
export interface ApprovalRespondParams {
  approval_id: ApprovalId;
  decision: Decision;
}
/**
 * Fetch MEMORY.md and the list of days that have a daily log.
 */
export interface MemoryGetRequest {
  frame_type?: FrameType4;
  id?: Id4;
  session_id?: SessionId4;
  method?: Method4;
  params?: _EmptyParams;
}
export interface MemoryGetDailyRequest {
  frame_type?: FrameType5;
  id?: Id5;
  session_id?: SessionId5;
  method?: Method5;
  params: DailyParams;
}
export interface DailyParams {
  date: Date;
}
export interface ConversationsListRequest {
  frame_type?: FrameType6;
  id?: Id6;
  session_id?: SessionId6;
  method?: Method6;
  params?: _EmptyParams;
}
export interface ConversationsGetRequest {
  frame_type?: FrameType7;
  id?: Id7;
  session_id?: SessionId7;
  method?: Method7;
  params: ConversationGetParams;
}
export interface ConversationGetParams {
  date: Date1;
  session_id: SessionId8;
}
export interface SettingsGetRequest {
  frame_type?: FrameType8;
  id?: Id8;
  session_id?: SessionId9;
  method?: Method8;
  params?: _EmptyParams;
}
export interface VoiceStartCallRequest {
  frame_type?: FrameType9;
  id?: Id9;
  session_id?: SessionId10;
  method?: Method9;
  params?: VoiceStartCallParams;
}
export interface VoiceStartCallParams {
  trigger?: Trigger;
}
export interface VoiceEndCallRequest {
  frame_type?: FrameType10;
  id?: Id10;
  session_id?: SessionId11;
  method?: Method10;
  params?: VoiceEndCallParams;
}
export interface VoiceEndCallParams {
  call_id?: CallId;
  reason?: Reason;
}
export interface VoiceStatusRequest {
  frame_type?: FrameType11;
  id?: Id11;
  session_id?: SessionId12;
  method?: Method11;
  params?: _EmptyParams;
}
export interface VoiceDelegateTaskRequest {
  frame_type?: FrameType12;
  id?: Id12;
  session_id?: SessionId13;
  method?: Method12;
  params: VoiceDelegateTaskParams;
}
export interface VoiceDelegateTaskParams {
  description: Description;
  call_id?: CallId1;
  tool_call_id?: ToolCallId;
}
/**
 * Daemon-to-client response paired with a request ID.
 */
export interface ResponseFrame {
  frame_type?: FrameType13;
  id: Id13;
  ok?: Ok;
  session_id?: SessionId14;
  result?: Result;
  error?: Error;
}
export interface Result {
  [k: string]: unknown | undefined;
}
export interface AssistantTokenEvent {
  frame_type?: FrameType14;
  request_id?: RequestId;
  session_id?: SessionId15;
  event?: Event1;
  data: AssistantTokenData;
}
export interface AssistantTokenData {
  text: Text1;
}
export interface ToolCallStartedEvent {
  frame_type?: FrameType15;
  request_id?: RequestId1;
  session_id?: SessionId16;
  event?: Event2;
  data: ToolCallStartedData;
}
export interface ToolCallStartedData {
  call_id: CallId2;
  name: Name;
  arguments?: Arguments;
  risk_level: RiskLevel;
}
export interface Arguments {
  [k: string]: unknown | undefined;
}
export interface ToolCallFinishedEvent {
  frame_type?: FrameType16;
  request_id?: RequestId2;
  session_id?: SessionId17;
  event?: Event3;
  data: ToolCallFinishedData;
}
export interface ToolCallFinishedData {
  call_id: CallId3;
  name: Name1;
  is_error: IsError;
  output: Output;
  duration_ms: DurationMs;
}
export interface ApprovalRequestedEvent {
  frame_type?: FrameType17;
  request_id?: RequestId3;
  session_id?: SessionId18;
  event?: Event4;
  data: ApprovalRequestedData;
}
export interface ApprovalRequestedData {
  approval_id: ApprovalId1;
  call_id?: CallId4;
  tool: Tool;
  arguments?: Arguments1;
  risk_level: RiskLevel1;
  timeout_seconds: TimeoutSeconds;
}
export interface Arguments1 {
  [k: string]: unknown | undefined;
}
export interface ApprovalResolvedEvent {
  frame_type?: FrameType18;
  request_id?: RequestId4;
  session_id?: SessionId19;
  event?: Event5;
  data: ApprovalResolvedData;
}
export interface ApprovalResolvedData {
  approval_id: ApprovalId2;
  decision: Decision1;
  reason: Reason1;
}
export interface TaskStatusEvent {
  frame_type?: FrameType19;
  request_id?: RequestId5;
  session_id?: SessionId20;
  event?: Event6;
  data: TaskStatusData;
}
export interface TaskStatusData {
  task_id: TaskId;
  title: Title;
  status: Status;
  started_at: StartedAt;
  finished_at?: FinishedAt;
  detail?: Detail;
}
export interface VoiceStateEvent {
  frame_type?: FrameType20;
  request_id?: RequestId6;
  session_id?: SessionId21;
  event?: Event7;
  data: VoiceStateData;
}
export interface VoiceStateData {
  state: State;
}
export interface CallStartedEvent {
  frame_type?: FrameType21;
  request_id?: RequestId7;
  session_id?: SessionId22;
  event?: Event8;
  data: CallStartedData;
}
export interface CallStartedData {
  call_id: CallId5;
  call_state?: CallState;
  started_at?: StartedAt1;
}
export interface CallEndedEvent {
  frame_type?: FrameType22;
  request_id?: RequestId8;
  session_id?: SessionId23;
  event?: Event9;
  data: CallEndedData;
}
export interface CallEndedData {
  call_id: CallId6;
  call_state?: CallState1;
  reason?: Reason2;
  duration_seconds?: DurationSeconds;
}
export interface TranscriptEvent {
  frame_type?: FrameType23;
  request_id?: RequestId9;
  session_id?: SessionId24;
  event?: Event10;
  data: TranscriptData;
}
export interface TranscriptData {
  role: Role;
  text: Text2;
  final?: Final;
  call_id?: CallId7;
}
export interface ScreenCaptureStateEvent {
  frame_type?: FrameType24;
  request_id?: RequestId10;
  session_id?: SessionId25;
  event?: Event11;
  data: ScreenCaptureStateData;
}
export interface ScreenCaptureStateData {
  active: Active;
  target?: Target;
  call_id?: CallId8;
}
export interface UsageEvent {
  frame_type?: FrameType25;
  request_id?: RequestId11;
  session_id?: SessionId26;
  event?: Event12;
  data: UsageData;
}
export interface UsageData {
  call_id?: CallId9;
  input_tokens?: InputTokens;
  output_tokens?: OutputTokens;
  total_tokens?: TotalTokens;
  cached_tokens?: CachedTokens;
  thought_tokens?: ThoughtTokens;
}
export interface ErrorEvent {
  frame_type?: FrameType26;
  request_id?: RequestId12;
  session_id?: SessionId27;
  event?: Event13;
  data: ErrorData;
}
export interface ErrorData {
  message: Message;
  code?: Code;
}
export interface MethodResults {
  health_ping: HealthPingResult;
  session_new: SessionNewResult;
  chat_send: ChatSendResult;
  approval_respond: ApprovalRespondResult;
  memory_get: MemoryGetResult;
  memory_get_daily: MemoryGetDailyResult;
  conversations_list: ConversationsListResult;
  conversations_get: ConversationsGetResult;
  settings_get: SettingsGetResult;
  voice_start_call: VoiceStartCallResult;
  voice_end_call: VoiceEndCallResult;
  voice_status: VoiceStatusResult;
  voice_delegate_task: VoiceDelegateTaskResult;
}
export interface HealthPingResult {
  status?: Status1;
}
export interface SessionNewResult {
  session_id: SessionId28;
}
export interface ChatSendResult {
  text: Text3;
}
export interface ApprovalRespondResult {
  approval_id: ApprovalId3;
  decision: Decision2;
}
export interface MemoryGetResult {
  long_term: LongTerm;
  days: Days;
}
export interface MemoryGetDailyResult {
  date: Date2;
  content: Content;
}
export interface ConversationsListResult {
  conversations: Conversations;
}
export interface ConversationSummary {
  date: Date3;
  session_id: SessionId29;
  title: Title1;
  turns: Turns;
  last_time: LastTime;
}
export interface ConversationsGetResult {
  date: Date4;
  session_id: SessionId30;
  turns: Turns1;
}
export interface ConversationTurn {
  time: Time;
  user: User;
  assistant: Assistant;
}
/**
 * Non-secret daemon configuration for display. Never contains keys or tokens.
 */
export interface SettingsGetResult {
  provider: Provider;
  model: Model;
  host: Host;
  port: Port;
  approval_timeout_seconds: ApprovalTimeoutSeconds;
  tools: Tools;
  permission_tiers: PermissionTiers;
}
export interface ToolInfo {
  name: Name2;
  description: Description1;
  risk_level: RiskLevel2;
}
export interface PermissionTierInfo {
  tier: Tier;
  policy: Policy;
}
export interface VoiceStartCallResult {
  call_id: CallId10;
  call_state: CallState2;
  voice_state?: VoiceState;
}
export interface VoiceEndCallResult {
  call_id?: CallId11;
  call_state?: CallState3;
  voice_state?: VoiceState1;
}
export interface VoiceStatusResult {
  call_id?: CallId12;
  call_state?: CallState4;
  voice_state?: VoiceState2;
  screen_capture_active?: ScreenCaptureActive;
  screen_capture_target?: ScreenCaptureTarget;
}
export interface VoiceDelegateTaskResult {
  task_id: TaskId1;
  status: Status2;
  output?: Output1;
}
