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
  | MemoryEpisodesRequest
  | MemoryFactsRequest
  | MemoryArchiveRequest
  | MemoryStatusRequest
  | MemoryReconsolidateRequest
  | MemoryForgetRequest
  | TranscriptsListRequest
  | TranscriptsGetRequest
  | DevEmitSampleTaskRequest
  | ConversationsListRequest
  | ConversationsGetRequest
  | ConversationListRequest
  | ConversationGetRequest
  | ConversationResumeRequest
  | ConversationRenameRequest
  | ConversationPinRequest
  | ConversationExportRequest
  | ConversationDeleteRequest
  | ConversationDeleteAllRequest
  | ConversationUndoDeleteRequest
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
export type Method6 = "memory.episodes";
export type Limit = number;
export type FrameType7 = "request";
export type Id7 = string;
export type SessionId7 = string | null;
export type Method7 = "memory.facts";
export type FrameType8 = "request";
export type Id8 = string;
export type SessionId8 = string | null;
export type Method8 = "memory.archive";
export type FrameType9 = "request";
export type Id9 = string;
export type SessionId9 = string | null;
export type Method9 = "memory.status";
export type FrameType10 = "request";
export type Id10 = string;
export type SessionId10 = string | null;
export type Method10 = "memory.reconsolidate";
export type SessionId11 = string;
export type FrameType11 = "request";
export type Id11 = string;
export type SessionId12 = string | null;
export type Method11 = "memory.forget";
/**
 * Entry ID of the fact to forget
 */
export type Id12 = string | null;
/**
 * Keywords, text, or ID of the memory to forget
 */
export type Query = string | null;
export type FrameType12 = "request";
export type Id13 = string;
export type SessionId13 = string | null;
export type Method12 = "transcripts.list";
export type Limit1 = number;
export type FrameType13 = "request";
export type Id14 = string;
export type SessionId14 = string | null;
export type Method13 = "transcripts.get";
export type SessionId15 = string;
export type FrameType14 = "request";
export type Id15 = string;
export type SessionId16 = string | null;
export type Method14 = "dev.emit_sample_task";
export type FrameType15 = "request";
export type Id16 = string;
export type SessionId17 = string | null;
export type Method15 = "conversations.list";
export type FrameType16 = "request";
export type Id17 = string;
export type SessionId18 = string | null;
export type Method16 = "conversations.get";
export type SessionId19 = string;
/**
 * ISO date, YYYY-MM-DD
 */
export type Date1 = string;
export type FrameType17 = "request";
export type Id18 = string;
export type SessionId20 = string | null;
export type Method17 = "conversation.list";
export type FrameType18 = "request";
export type Id19 = string;
export type SessionId21 = string | null;
export type Method18 = "conversation.get";
export type FrameType19 = "request";
export type Id20 = string;
export type SessionId22 = string | null;
export type Method19 = "conversation.resume";
export type SessionId23 = string;
export type FrameType20 = "request";
export type Id21 = string;
export type SessionId24 = string | null;
export type Method20 = "conversation.rename";
export type SessionId25 = string;
export type Title = string;
export type FrameType21 = "request";
export type Id22 = string;
export type SessionId26 = string | null;
export type Method21 = "conversation.pin";
export type SessionId27 = string;
export type Pinned = boolean;
export type FrameType22 = "request";
export type Id23 = string;
export type SessionId28 = string | null;
export type Method22 = "conversation.export";
export type SessionId29 = string;
export type FrameType23 = "request";
export type Id24 = string;
export type SessionId30 = string | null;
export type Method23 = "conversation.delete";
export type SessionId31 = string;
export type DeleteEpisodes = boolean;
export type FrameType24 = "request";
export type Id25 = string;
export type SessionId32 = string | null;
export type Method24 = "conversation.delete_all";
export type Confirmation = string;
export type FrameType25 = "request";
export type Id26 = string;
export type SessionId33 = string | null;
export type Method25 = "conversation.undo_delete";
export type SessionId34 = string | null;
export type FrameType26 = "request";
export type Id27 = string;
export type SessionId35 = string | null;
export type Method26 = "settings.get";
export type FrameType27 = "request";
export type Id28 = string;
export type SessionId36 = string | null;
export type Method27 = "voice.start_call";
export type Trigger = "manual" | "wake_word" | "hotkey" | "ui";
export type FrameType28 = "request";
export type Id29 = string;
export type SessionId37 = string | null;
export type Method28 = "voice.end_call";
export type CallId = string | null;
export type Reason = string | null;
export type Transcript =
  | {
      [k: string]: unknown | undefined;
    }[]
  | null;
export type FrameType29 = "request";
export type Id30 = string;
export type SessionId38 = string | null;
export type Method29 = "voice.status";
export type FrameType30 = "request";
export type Id31 = string;
export type SessionId39 = string | null;
export type Method30 = "voice.delegate_task";
export type Description = string;
export type CallId1 = string | null;
export type ToolCallId = string | null;
export type FrameType31 = "response";
export type Id32 = string;
export type Ok = boolean;
export type SessionId40 = string | null;
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
  | ErrorEvent
  | ConversationUpdatedEvent
  | ConversationDeletedEvent;
export type FrameType32 = "event";
export type RequestId = string | null;
export type SessionId41 = string | null;
export type Event1 = "assistant_token";
export type Text1 = string;
export type FrameType33 = "event";
export type RequestId1 = string | null;
export type SessionId42 = string | null;
export type Event2 = "tool_call_started";
export type CallId2 = string;
export type Name = string;
export type RiskLevel = "read_only" | "reversible" | "destructive";
export type FrameType34 = "event";
export type RequestId2 = string | null;
export type SessionId43 = string | null;
export type Event3 = "tool_call_finished";
export type CallId3 = string;
export type Name1 = string;
export type IsError = boolean;
export type Output = string;
/**
 * Execution time, excluding time spent awaiting approval
 */
export type DurationMs = number;
export type FrameType35 = "event";
export type RequestId3 = string | null;
export type SessionId44 = string | null;
export type Event4 = "approval_requested";
export type ApprovalId1 = string;
export type CallId4 = string | null;
export type Tool = string;
export type RiskLevel1 = "read_only" | "reversible" | "destructive";
export type TimeoutSeconds = number;
export type FrameType36 = "event";
export type RequestId4 = string | null;
export type SessionId45 = string | null;
export type Event5 = "approval_resolved";
export type ApprovalId2 = string;
export type Decision1 = "allow" | "deny";
export type Reason1 = "user" | "timeout";
export type FrameType37 = "event";
export type RequestId5 = string | null;
export type SessionId46 = string | null;
export type Event6 = "task_status";
export type TaskId = string;
export type Title1 = string;
export type Status = "queued" | "running" | "succeeded" | "failed" | "cancelled";
export type StartedAt = string;
export type Kind = "chat_turn" | "background_task" | "scheduled" | "agent_run";
export type FinishedAt = string | null;
export type NextRunAt = string | null;
export type Detail = string | null;
export type FrameType38 = "event";
export type RequestId6 = string | null;
export type SessionId47 = string | null;
export type Event7 = "voice_state";
export type State = "idle" | "listening" | "thinking" | "speaking" | "error";
export type Error1 = string | null;
export type FrameType39 = "event";
export type RequestId7 = string | null;
export type SessionId48 = string | null;
export type Event8 = "call_started";
export type CallId5 = string;
export type CallState = "idle" | "connecting" | "active" | "ending";
export type StartedAt1 = string | null;
export type FrameType40 = "event";
export type RequestId8 = string | null;
export type SessionId49 = string | null;
export type Event9 = "call_ended";
export type CallId6 = string;
export type CallState1 = "idle" | "connecting" | "active" | "ending";
export type Reason2 = string | null;
export type DurationSeconds = number | null;
export type FrameType41 = "event";
export type RequestId9 = string | null;
export type SessionId50 = string | null;
export type Event10 = "transcript";
export type Role = "user" | "assistant";
export type Text2 = string;
export type Final = boolean;
export type CallId7 = string | null;
export type FrameType42 = "event";
export type RequestId10 = string | null;
export type SessionId51 = string | null;
export type Event11 = "screen_capture_state";
export type Active = boolean;
export type Target = ("active_window" | "full_screen") | null;
export type CallId8 = string | null;
export type FrameType43 = "event";
export type RequestId11 = string | null;
export type SessionId52 = string | null;
export type Event12 = "usage";
export type CallId9 = string | null;
export type InputTokens = number;
export type OutputTokens = number;
export type TotalTokens = number;
export type CachedTokens = number | null;
export type ThoughtTokens = number | null;
export type FrameType44 = "event";
export type RequestId12 = string | null;
export type SessionId53 = string | null;
export type Event13 = "error";
export type Message = string;
export type Code = string | null;
export type FrameType45 = "event";
export type RequestId13 = string | null;
export type SessionId54 = string | null;
export type Event14 = "conversation_updated";
export type SessionId55 = string;
export type Title2 = string | null;
export type Pinned1 = boolean | null;
export type UpdatedAt = string | null;
export type UserMessage = string | null;
export type AssistantMessage = string | null;
export type Turns = number | null;
export type RequestId14 = string | null;
export type FrameType46 = "event";
export type RequestId15 = string | null;
export type SessionId56 = string | null;
export type Event15 = "conversation_deleted";
export type SessionIds = string[];
export type All = boolean;
export type Status1 = "ok";
export type SessionId57 = string;
export type Text3 = string;
export type ApprovalId3 = string;
export type Decision2 = "allow" | "deny";
export type LongTerm = string;
/**
 * ISO dates with a daily log, newest first
 */
export type Days = string[];
export type LastConsolidated = string | null;
export type FailedConsolidation = string | null;
export type Date2 = string;
export type Content = string;
export type Date3 = string;
export type SessionId58 = string;
export type Time = string;
export type Summary = string;
export type Trivial = boolean;
export type Episodes = EpisodeItem[];
export type Name2 = string;
export type Id33 = string;
export type Date4 = string;
export type Source = string;
export type Text4 = string;
export type Entries = MemoryFactItem[];
export type Sections = MemorySectionItem[];
export type LastConsolidated1 = string | null;
export type FailedConsolidation1 = string | null;
export type Id34 = string;
export type Date5 = string;
export type Source1 = string;
export type Text5 = string;
export type ArchivedAt = string | null;
export type Reason3 = string | null;
export type Entries1 = MemoryArchiveItem[];
export type Raw = string;
export type LastConsolidatedAt = string | null;
export type SessionId59 = string;
export type Status2 = string;
export type RetryCount = number;
export type Error2 = string | null;
export type LastAttemptAt = string | null;
export type NextRetryAt = string | null;
export type Pending = ConsolidationSessionStatus[];
export type Failed = ConsolidationSessionStatus[];
export type CompletedCount = number;
export type SessionId60 = string;
export type Ok1 = boolean;
export type Status3 = string;
export type Error3 = string | null;
export type Ok2 = boolean;
export type RemovedCount = number;
export type RemovedIds = string[];
export type SessionId61 = string;
export type StartedAt2 = string;
export type Title3 = string;
export type MessageCount = number;
export type Transcripts = TranscriptSummaryItem[];
export type SessionId62 = string;
export type StartedAt3 = string;
export type Title4 = string;
export type Role1 = string;
export type Text6 = string;
export type Timestamp = string;
export type ToolCalls = {
  [k: string]: unknown | undefined;
}[];
export type Messages = TranscriptMessageItem[];
export type Status4 = "ok";
export type Date6 = string;
export type SessionId63 = string;
export type Title5 = string;
export type Turns1 = number;
export type LastTime = string;
export type Pinned2 = boolean;
export type UpdatedAt1 = string;
export type Conversations = ConversationSummary[];
export type Date7 = string;
export type SessionId64 = string;
export type Time1 = string;
export type User = string;
export type Assistant = string;
export type Turns2 = ConversationTurn[];
export type SessionId65 = string;
export type Title6 = string;
export type Pinned3 = boolean;
export type Role2 = "user" | "assistant";
export type Text7 = string;
export type Timestamp1 = string;
export type ToolCalls1 = {
  [k: string]: unknown | undefined;
}[];
export type Messages1 = ConversationResumeMessage[];
export type SessionId66 = string;
export type Title7 = string;
export type SessionId67 = string;
export type Pinned4 = boolean;
export type SessionId68 = string;
export type Title8 = string;
export type Filename = string;
export type Content1 = string;
export type SessionId69 = string;
export type Deleted = boolean;
export type UndoWindowSeconds = number;
export type DeletedCount = number;
export type UndoWindowSeconds1 = number;
export type RestoredCount = number;
export type SessionIds1 = string[];
export type Provider = string;
export type Model = string;
export type Host = string;
export type Port = number;
export type ApprovalTimeoutSeconds = number;
export type Name3 = string;
export type Description1 = string;
export type RiskLevel2 = "read_only" | "reversible" | "destructive";
export type Tools = ToolInfo[];
export type Tier = "read_only" | "reversible" | "destructive";
export type Policy = "auto_allow" | "ask" | "ask_confirm";
export type PermissionTiers = PermissionTierInfo[];
export type CallId10 = string;
export type CallState2 = "idle" | "connecting" | "active" | "ending";
export type VoiceState = "idle" | "listening" | "thinking" | "speaking" | "error";
export type MemoryContext = string | null;
export type CallId11 = string | null;
export type CallState3 = "idle" | "connecting" | "active" | "ending";
export type VoiceState1 = "idle" | "listening" | "thinking" | "speaking" | "error";
export type CallId12 = string | null;
export type CallState4 = "idle" | "connecting" | "active" | "ending";
export type VoiceState2 = "idle" | "listening" | "thinking" | "speaking" | "error";
export type ScreenCaptureActive = boolean;
export type ScreenCaptureTarget = ("active_window" | "full_screen") | null;
export type TaskId1 = string;
export type Status5 = "queued" | "running" | "succeeded" | "failed" | "cancelled";
export type Output1 = string;
export type RiskTier = "read_only" | "reversible" | "destructive";
export type ApprovalDecision = "allow" | "deny";
export type ApprovalReason = "user" | "timeout";
export type TaskState = "queued" | "running" | "succeeded" | "failed" | "cancelled";
export type TaskKind = "chat_turn" | "background_task" | "scheduled" | "agent_run";
export type VoiceState3 = "idle" | "listening" | "thinking" | "speaking" | "error";
export type CallState5 = "idle" | "connecting" | "active" | "ending";
export type TranscriptRole = "user" | "assistant";
export type ScreenCaptureTarget1 = "active_window" | "full_screen";
export type VoiceTrigger = "manual" | "wake_word" | "hotkey" | "ui";

export interface CleaProtocolSchema {
  request: Request;
  response: ResponseFrame;
  event: Event;
  method_results: MethodResults;
  risk_tier: RiskTier;
  approval_decision: ApprovalDecision;
  approval_reason: ApprovalReason;
  task_state: TaskState;
  task_kind: TaskKind;
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
  memory_episodes_request: MemoryEpisodesRequest;
  memory_facts_request: MemoryFactsRequest;
  memory_archive_request: MemoryArchiveRequest;
  memory_status_request: MemoryStatusRequest;
  memory_reconsolidate_request: MemoryReconsolidateRequest;
  memory_forget_request: MemoryForgetRequest;
  transcripts_list_request: TranscriptsListRequest;
  transcripts_get_request: TranscriptsGetRequest;
  conversations_list_request: ConversationsListRequest;
  conversations_get_request: ConversationsGetRequest;
  conversation_list_request: ConversationListRequest;
  conversation_get_request: ConversationGetRequest;
  conversation_resume_request: ConversationResumeRequest;
  conversation_rename_request: ConversationRenameRequest;
  conversation_pin_request: ConversationPinRequest;
  conversation_export_request: ConversationExportRequest;
  conversation_delete_request: ConversationDeleteRequest;
  conversation_delete_all_request: ConversationDeleteAllRequest;
  conversation_undo_delete_request: ConversationUndoDeleteRequest;
  conversation_summary: ConversationSummary;
  conversation_resume_message: ConversationResumeMessage;
  settings_get_request: SettingsGetRequest;
  dev_emit_sample_task_request: DevEmitSampleTaskRequest;
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
  conversation_updated_event: ConversationUpdatedEvent;
  conversation_deleted_event: ConversationDeletedEvent;
  conversation_updated_data: ConversationUpdatedData;
  conversation_deleted_data: ConversationDeletedData;
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
export interface MemoryEpisodesRequest {
  frame_type?: FrameType6;
  id?: Id6;
  session_id?: SessionId6;
  method?: Method6;
  params?: MemoryEpisodesParams;
}
export interface MemoryEpisodesParams {
  limit?: Limit;
}
/**
 * Fetch structured durable facts parsed by section.
 */
export interface MemoryFactsRequest {
  frame_type?: FrameType7;
  id?: Id7;
  session_id?: SessionId7;
  method?: Method7;
  params?: _EmptyParams;
}
/**
 * Fetch archived facts from MEMORY_ARCHIVE.md.
 */
export interface MemoryArchiveRequest {
  frame_type?: FrameType8;
  id?: Id8;
  session_id?: SessionId8;
  method?: Method8;
  params?: _EmptyParams;
}
/**
 * Fetch consolidation status (pending, failed, last-success).
 */
export interface MemoryStatusRequest {
  frame_type?: FrameType9;
  id?: Id9;
  session_id?: SessionId9;
  method?: Method9;
  params?: _EmptyParams;
}
/**
 * Trigger manual reconsolidation of a session.
 */
export interface MemoryReconsolidateRequest {
  frame_type?: FrameType10;
  id?: Id10;
  session_id?: SessionId10;
  method?: Method10;
  params: MemoryReconsolidateParams;
}
export interface MemoryReconsolidateParams {
  session_id: SessionId11;
}
/**
 * Forget durable facts or preferences matching an entry ID or query.
 */
export interface MemoryForgetRequest {
  frame_type?: FrameType11;
  id?: Id11;
  session_id?: SessionId12;
  method?: Method11;
  params: MemoryForgetParams;
}
export interface MemoryForgetParams {
  id?: Id12;
  query?: Query;
}
export interface TranscriptsListRequest {
  frame_type?: FrameType12;
  id?: Id13;
  session_id?: SessionId13;
  method?: Method12;
  params?: TranscriptsListParams;
}
export interface TranscriptsListParams {
  limit?: Limit1;
}
export interface TranscriptsGetRequest {
  frame_type?: FrameType13;
  id?: Id14;
  session_id?: SessionId14;
  method?: Method13;
  params: TranscriptsGetParams;
}
export interface TranscriptsGetParams {
  session_id: SessionId15;
}
export interface DevEmitSampleTaskRequest {
  frame_type?: FrameType14;
  id?: Id15;
  session_id?: SessionId16;
  method?: Method14;
  params?: _EmptyParams;
}
export interface ConversationsListRequest {
  frame_type?: FrameType15;
  id?: Id16;
  session_id?: SessionId17;
  method?: Method15;
  params?: _EmptyParams;
}
export interface ConversationsGetRequest {
  frame_type?: FrameType16;
  id?: Id17;
  session_id?: SessionId18;
  method?: Method16;
  params: ConversationGetParams;
}
export interface ConversationGetParams {
  session_id: SessionId19;
  date?: Date1;
}
export interface ConversationListRequest {
  frame_type?: FrameType17;
  id?: Id18;
  session_id?: SessionId20;
  method?: Method17;
  params?: _EmptyParams;
}
export interface ConversationGetRequest {
  frame_type?: FrameType18;
  id?: Id19;
  session_id?: SessionId21;
  method?: Method18;
  params: ConversationGetParams;
}
export interface ConversationResumeRequest {
  frame_type?: FrameType19;
  id?: Id20;
  session_id?: SessionId22;
  method?: Method19;
  params: ConversationResumeParams;
}
export interface ConversationResumeParams {
  session_id: SessionId23;
}
export interface ConversationRenameRequest {
  frame_type?: FrameType20;
  id?: Id21;
  session_id?: SessionId24;
  method?: Method20;
  params: ConversationRenameParams;
}
export interface ConversationRenameParams {
  session_id: SessionId25;
  title: Title;
}
export interface ConversationPinRequest {
  frame_type?: FrameType21;
  id?: Id22;
  session_id?: SessionId26;
  method?: Method21;
  params: ConversationPinParams;
}
export interface ConversationPinParams {
  session_id: SessionId27;
  pinned: Pinned;
}
export interface ConversationExportRequest {
  frame_type?: FrameType22;
  id?: Id23;
  session_id?: SessionId28;
  method?: Method22;
  params: ConversationExportParams;
}
export interface ConversationExportParams {
  session_id: SessionId29;
}
export interface ConversationDeleteRequest {
  frame_type?: FrameType23;
  id?: Id24;
  session_id?: SessionId30;
  method?: Method23;
  params: ConversationDeleteParams;
}
export interface ConversationDeleteParams {
  session_id: SessionId31;
  delete_episodes?: DeleteEpisodes;
}
export interface ConversationDeleteAllRequest {
  frame_type?: FrameType24;
  id?: Id25;
  session_id?: SessionId32;
  method?: Method24;
  params: ConversationDeleteAllParams;
}
export interface ConversationDeleteAllParams {
  confirmation: Confirmation;
}
export interface ConversationUndoDeleteRequest {
  frame_type?: FrameType25;
  id?: Id26;
  session_id?: SessionId33;
  method?: Method25;
  params: ConversationUndoDeleteParams;
}
export interface ConversationUndoDeleteParams {
  session_id?: SessionId34;
}
export interface SettingsGetRequest {
  frame_type?: FrameType26;
  id?: Id27;
  session_id?: SessionId35;
  method?: Method26;
  params?: _EmptyParams;
}
export interface VoiceStartCallRequest {
  frame_type?: FrameType27;
  id?: Id28;
  session_id?: SessionId36;
  method?: Method27;
  params?: VoiceStartCallParams;
}
export interface VoiceStartCallParams {
  trigger?: Trigger;
}
export interface VoiceEndCallRequest {
  frame_type?: FrameType28;
  id?: Id29;
  session_id?: SessionId37;
  method?: Method28;
  params?: VoiceEndCallParams;
}
export interface VoiceEndCallParams {
  call_id?: CallId;
  reason?: Reason;
  transcript?: Transcript;
}
export interface VoiceStatusRequest {
  frame_type?: FrameType29;
  id?: Id30;
  session_id?: SessionId38;
  method?: Method29;
  params?: _EmptyParams;
}
export interface VoiceDelegateTaskRequest {
  frame_type?: FrameType30;
  id?: Id31;
  session_id?: SessionId39;
  method?: Method30;
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
  frame_type?: FrameType31;
  id: Id32;
  ok?: Ok;
  session_id?: SessionId40;
  result?: Result;
  error?: Error;
}
export interface Result {
  [k: string]: unknown | undefined;
}
export interface AssistantTokenEvent {
  frame_type?: FrameType32;
  request_id?: RequestId;
  session_id?: SessionId41;
  event?: Event1;
  data: AssistantTokenData;
}
export interface AssistantTokenData {
  text: Text1;
}
export interface ToolCallStartedEvent {
  frame_type?: FrameType33;
  request_id?: RequestId1;
  session_id?: SessionId42;
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
  frame_type?: FrameType34;
  request_id?: RequestId2;
  session_id?: SessionId43;
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
  frame_type?: FrameType35;
  request_id?: RequestId3;
  session_id?: SessionId44;
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
  frame_type?: FrameType36;
  request_id?: RequestId4;
  session_id?: SessionId45;
  event?: Event5;
  data: ApprovalResolvedData;
}
export interface ApprovalResolvedData {
  approval_id: ApprovalId2;
  decision: Decision1;
  reason: Reason1;
}
export interface TaskStatusEvent {
  frame_type?: FrameType37;
  request_id?: RequestId5;
  session_id?: SessionId46;
  event?: Event6;
  data: TaskStatusData;
}
export interface TaskStatusData {
  task_id: TaskId;
  title: Title1;
  status: Status;
  started_at: StartedAt;
  kind?: Kind;
  finished_at?: FinishedAt;
  next_run_at?: NextRunAt;
  detail?: Detail;
}
export interface VoiceStateEvent {
  frame_type?: FrameType38;
  request_id?: RequestId6;
  session_id?: SessionId47;
  event?: Event7;
  data: VoiceStateData;
}
export interface VoiceStateData {
  state: State;
  error?: Error1;
}
export interface CallStartedEvent {
  frame_type?: FrameType39;
  request_id?: RequestId7;
  session_id?: SessionId48;
  event?: Event8;
  data: CallStartedData;
}
export interface CallStartedData {
  call_id: CallId5;
  call_state?: CallState;
  started_at?: StartedAt1;
}
export interface CallEndedEvent {
  frame_type?: FrameType40;
  request_id?: RequestId8;
  session_id?: SessionId49;
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
  frame_type?: FrameType41;
  request_id?: RequestId9;
  session_id?: SessionId50;
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
  frame_type?: FrameType42;
  request_id?: RequestId10;
  session_id?: SessionId51;
  event?: Event11;
  data: ScreenCaptureStateData;
}
export interface ScreenCaptureStateData {
  active: Active;
  target?: Target;
  call_id?: CallId8;
}
export interface UsageEvent {
  frame_type?: FrameType43;
  request_id?: RequestId11;
  session_id?: SessionId52;
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
  frame_type?: FrameType44;
  request_id?: RequestId12;
  session_id?: SessionId53;
  event?: Event13;
  data: ErrorData;
}
export interface ErrorData {
  message: Message;
  code?: Code;
}
export interface ConversationUpdatedEvent {
  frame_type?: FrameType45;
  request_id?: RequestId13;
  session_id?: SessionId54;
  event?: Event14;
  data: ConversationUpdatedData;
}
export interface ConversationUpdatedData {
  session_id: SessionId55;
  title?: Title2;
  pinned?: Pinned1;
  updated_at?: UpdatedAt;
  user_message?: UserMessage;
  assistant_message?: AssistantMessage;
  turns?: Turns;
  request_id?: RequestId14;
}
export interface ConversationDeletedEvent {
  frame_type?: FrameType46;
  request_id?: RequestId15;
  session_id?: SessionId56;
  event?: Event15;
  data: ConversationDeletedData;
}
export interface ConversationDeletedData {
  session_ids?: SessionIds;
  all?: All;
}
export interface MethodResults {
  health_ping: HealthPingResult;
  session_new: SessionNewResult;
  chat_send: ChatSendResult;
  approval_respond: ApprovalRespondResult;
  memory_get: MemoryGetResult;
  memory_get_daily: MemoryGetDailyResult;
  memory_episodes: MemoryEpisodesResult;
  memory_facts: MemoryFactsResult;
  memory_archive: MemoryArchiveResult;
  memory_status: MemoryStatusResult;
  memory_reconsolidate: MemoryReconsolidateResult;
  memory_forget: MemoryForgetResult;
  transcripts_list: TranscriptsListResult;
  transcripts_get: TranscriptsGetResult;
  dev_emit_sample_task: DevEmitSampleTaskResult;
  conversations_list: ConversationsListResult;
  conversations_get: ConversationsGetResult;
  conversation_list: ConversationsListResult;
  conversation_get: ConversationsGetResult;
  conversation_resume: ConversationResumeResult;
  conversation_rename: ConversationRenameResult;
  conversation_pin: ConversationPinResult;
  conversation_export: ConversationExportResult;
  conversation_delete: ConversationDeleteResult;
  conversation_delete_all: ConversationDeleteAllResult;
  conversation_undo_delete: ConversationUndoDeleteResult;
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
  session_id: SessionId57;
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
  days?: Days;
  last_consolidated?: LastConsolidated;
  failed_consolidation?: FailedConsolidation;
}
export interface MemoryGetDailyResult {
  date: Date2;
  content: Content;
}
export interface MemoryEpisodesResult {
  episodes: Episodes;
}
export interface EpisodeItem {
  date: Date3;
  session_id: SessionId58;
  time: Time;
  summary: Summary;
  trivial?: Trivial;
}
export interface MemoryFactsResult {
  sections: Sections;
  last_consolidated?: LastConsolidated1;
  failed_consolidation?: FailedConsolidation1;
}
export interface MemorySectionItem {
  name: Name2;
  entries?: Entries;
}
export interface MemoryFactItem {
  id: Id33;
  date: Date4;
  source: Source;
  text: Text4;
}
export interface MemoryArchiveResult {
  entries?: Entries1;
  raw?: Raw;
}
export interface MemoryArchiveItem {
  id: Id34;
  date: Date5;
  source: Source1;
  text: Text5;
  archived_at?: ArchivedAt;
  reason?: Reason3;
}
export interface MemoryStatusResult {
  last_consolidated_at?: LastConsolidatedAt;
  pending?: Pending;
  failed?: Failed;
  completed_count?: CompletedCount;
}
export interface ConsolidationSessionStatus {
  session_id: SessionId59;
  status: Status2;
  retry_count: RetryCount;
  error?: Error2;
  last_attempt_at?: LastAttemptAt;
  next_retry_at?: NextRetryAt;
}
export interface MemoryReconsolidateResult {
  session_id: SessionId60;
  ok: Ok1;
  status: Status3;
  error?: Error3;
}
export interface MemoryForgetResult {
  ok?: Ok2;
  removed_count?: RemovedCount;
  removed_ids?: RemovedIds;
}
export interface TranscriptsListResult {
  transcripts: Transcripts;
}
export interface TranscriptSummaryItem {
  session_id: SessionId61;
  started_at: StartedAt2;
  title: Title3;
  message_count: MessageCount;
}
export interface TranscriptsGetResult {
  session_id: SessionId62;
  started_at: StartedAt3;
  title: Title4;
  messages: Messages;
}
export interface TranscriptMessageItem {
  role: Role1;
  text: Text6;
  timestamp: Timestamp;
  tool_calls?: ToolCalls;
}
export interface DevEmitSampleTaskResult {
  status?: Status4;
}
export interface ConversationsListResult {
  conversations: Conversations;
}
export interface ConversationSummary {
  date: Date6;
  session_id: SessionId63;
  title: Title5;
  turns: Turns1;
  last_time: LastTime;
  pinned?: Pinned2;
  updated_at?: UpdatedAt1;
}
export interface ConversationsGetResult {
  date: Date7;
  session_id: SessionId64;
  turns: Turns2;
}
export interface ConversationTurn {
  time: Time1;
  user: User;
  assistant: Assistant;
}
export interface ConversationResumeResult {
  session_id: SessionId65;
  title: Title6;
  pinned?: Pinned3;
  messages: Messages1;
}
export interface ConversationResumeMessage {
  role: Role2;
  text: Text7;
  timestamp?: Timestamp1;
  tool_calls?: ToolCalls1;
}
export interface ConversationRenameResult {
  session_id: SessionId66;
  title: Title7;
}
export interface ConversationPinResult {
  session_id: SessionId67;
  pinned: Pinned4;
}
export interface ConversationExportResult {
  session_id: SessionId68;
  title: Title8;
  filename: Filename;
  content: Content1;
}
export interface ConversationDeleteResult {
  session_id: SessionId69;
  deleted: Deleted;
  undo_window_seconds?: UndoWindowSeconds;
}
export interface ConversationDeleteAllResult {
  deleted_count: DeletedCount;
  undo_window_seconds?: UndoWindowSeconds1;
}
export interface ConversationUndoDeleteResult {
  restored_count: RestoredCount;
  session_ids: SessionIds1;
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
  name: Name3;
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
  memory_context?: MemoryContext;
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
  status: Status5;
  output?: Output1;
}
