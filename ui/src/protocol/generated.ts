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
  | SettingsGetRequest;
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
export type FrameType9 = "response";
export type Id9 = string;
export type Ok = boolean;
export type SessionId10 = string | null;
export type Error = string | null;
export type Event =
  | AssistantTokenEvent
  | ToolCallStartedEvent
  | ToolCallFinishedEvent
  | ApprovalRequestedEvent
  | ApprovalResolvedEvent
  | TaskStatusEvent
  | VoiceStateEvent
  | ErrorEvent;
export type FrameType10 = "event";
export type RequestId = string | null;
export type SessionId11 = string | null;
export type Event1 = "assistant_token";
export type Text1 = string;
export type FrameType11 = "event";
export type RequestId1 = string | null;
export type SessionId12 = string | null;
export type Event2 = "tool_call_started";
export type CallId = string;
export type Name = string;
export type RiskLevel = "read_only" | "reversible" | "destructive";
export type FrameType12 = "event";
export type RequestId2 = string | null;
export type SessionId13 = string | null;
export type Event3 = "tool_call_finished";
export type CallId1 = string;
export type Name1 = string;
export type IsError = boolean;
export type Output = string;
/**
 * Execution time, excluding time spent awaiting approval
 */
export type DurationMs = number;
export type FrameType13 = "event";
export type RequestId3 = string | null;
export type SessionId14 = string | null;
export type Event4 = "approval_requested";
export type ApprovalId1 = string;
export type CallId2 = string | null;
export type Tool = string;
export type RiskLevel1 = "read_only" | "reversible" | "destructive";
export type TimeoutSeconds = number;
export type FrameType14 = "event";
export type RequestId4 = string | null;
export type SessionId15 = string | null;
export type Event5 = "approval_resolved";
export type ApprovalId2 = string;
export type Decision1 = "allow" | "deny";
export type Reason = "user" | "timeout";
export type FrameType15 = "event";
export type RequestId5 = string | null;
export type SessionId16 = string | null;
export type Event6 = "task_status";
export type TaskId = string;
export type Title = string;
export type Status = "running" | "succeeded" | "failed";
export type StartedAt = string;
export type FinishedAt = string | null;
export type Detail = string | null;
export type FrameType16 = "event";
export type RequestId6 = string | null;
export type SessionId17 = string | null;
export type Event7 = "voice_state";
export type State = "idle" | "listening" | "thinking" | "speaking";
export type FrameType17 = "event";
export type RequestId7 = string | null;
export type SessionId18 = string | null;
export type Event8 = "error";
export type Message = string;
export type Code = string | null;
export type Status1 = "ok";
export type SessionId19 = string;
export type Text2 = string;
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
export type SessionId20 = string;
export type Title1 = string;
export type Turns = number;
export type LastTime = string;
export type Conversations = ConversationSummary[];
export type Date4 = string;
export type SessionId21 = string;
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
export type Description = string;
export type RiskLevel2 = "read_only" | "reversible" | "destructive";
export type Tools = ToolInfo[];
export type Tier = "read_only" | "reversible" | "destructive";
export type Policy = "auto_allow" | "ask" | "ask_confirm";
export type PermissionTiers = PermissionTierInfo[];
export type RiskTier = "read_only" | "reversible" | "destructive";
export type ApprovalDecision = "allow" | "deny";
export type ApprovalReason = "user" | "timeout";
export type TaskState = "running" | "succeeded" | "failed";
export type VoiceState = "idle" | "listening" | "thinking" | "speaking";

export interface CleaProtocolSchema {
  request: Request;
  response: ResponseFrame;
  event: Event;
  method_results: MethodResults;
  risk_tier: RiskTier;
  approval_decision: ApprovalDecision;
  approval_reason: ApprovalReason;
  task_state: TaskState;
  voice_state: VoiceState;
  health_ping_request: HealthPingRequest;
  session_new_request: SessionNewRequest;
  chat_send_request: ChatSendRequest;
  approval_respond_request: ApprovalRespondRequest;
  memory_get_request: MemoryGetRequest;
  memory_get_daily_request: MemoryGetDailyRequest;
  conversations_list_request: ConversationsListRequest;
  conversations_get_request: ConversationsGetRequest;
  settings_get_request: SettingsGetRequest;
  assistant_token_event: AssistantTokenEvent;
  tool_call_started_event: ToolCallStartedEvent;
  tool_call_finished_event: ToolCallFinishedEvent;
  approval_requested_event: ApprovalRequestedEvent;
  approval_resolved_event: ApprovalResolvedEvent;
  task_status_event: TaskStatusEvent;
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
/**
 * Daemon-to-client response paired with a request ID.
 */
export interface ResponseFrame {
  frame_type?: FrameType9;
  id: Id9;
  ok?: Ok;
  session_id?: SessionId10;
  result?: Result;
  error?: Error;
}
export interface Result {
  [k: string]: unknown | undefined;
}
export interface AssistantTokenEvent {
  frame_type?: FrameType10;
  request_id?: RequestId;
  session_id?: SessionId11;
  event?: Event1;
  data: AssistantTokenData;
}
export interface AssistantTokenData {
  text: Text1;
}
export interface ToolCallStartedEvent {
  frame_type?: FrameType11;
  request_id?: RequestId1;
  session_id?: SessionId12;
  event?: Event2;
  data: ToolCallStartedData;
}
export interface ToolCallStartedData {
  call_id: CallId;
  name: Name;
  arguments?: Arguments;
  risk_level: RiskLevel;
}
export interface Arguments {
  [k: string]: unknown | undefined;
}
export interface ToolCallFinishedEvent {
  frame_type?: FrameType12;
  request_id?: RequestId2;
  session_id?: SessionId13;
  event?: Event3;
  data: ToolCallFinishedData;
}
export interface ToolCallFinishedData {
  call_id: CallId1;
  name: Name1;
  is_error: IsError;
  output: Output;
  duration_ms: DurationMs;
}
export interface ApprovalRequestedEvent {
  frame_type?: FrameType13;
  request_id?: RequestId3;
  session_id?: SessionId14;
  event?: Event4;
  data: ApprovalRequestedData;
}
export interface ApprovalRequestedData {
  approval_id: ApprovalId1;
  call_id?: CallId2;
  tool: Tool;
  arguments?: Arguments1;
  risk_level: RiskLevel1;
  timeout_seconds: TimeoutSeconds;
}
export interface Arguments1 {
  [k: string]: unknown | undefined;
}
export interface ApprovalResolvedEvent {
  frame_type?: FrameType14;
  request_id?: RequestId4;
  session_id?: SessionId15;
  event?: Event5;
  data: ApprovalResolvedData;
}
export interface ApprovalResolvedData {
  approval_id: ApprovalId2;
  decision: Decision1;
  reason: Reason;
}
export interface TaskStatusEvent {
  frame_type?: FrameType15;
  request_id?: RequestId5;
  session_id?: SessionId16;
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
  frame_type?: FrameType16;
  request_id?: RequestId6;
  session_id?: SessionId17;
  event?: Event7;
  data: VoiceStateData;
}
export interface VoiceStateData {
  state: State;
}
export interface ErrorEvent {
  frame_type?: FrameType17;
  request_id?: RequestId7;
  session_id?: SessionId18;
  event?: Event8;
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
}
export interface HealthPingResult {
  status?: Status1;
}
export interface SessionNewResult {
  session_id: SessionId19;
}
export interface ChatSendResult {
  text: Text2;
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
  session_id: SessionId20;
  title: Title1;
  turns: Turns;
  last_time: LastTime;
}
export interface ConversationsGetResult {
  date: Date4;
  session_id: SessionId21;
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
  description: Description;
  risk_level: RiskLevel2;
}
export interface PermissionTierInfo {
  tier: Tier;
  policy: Policy;
}
