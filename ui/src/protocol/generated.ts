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
  | ChatFocusRequest
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
  | VoiceDelegateTaskRequest
  | VoiceHistoryRequest
  | VoiceListConversationsRequest
  | VoiceSearchConversationsRequest
  | VoiceReadConversationRequest
  | VoiceSearchMemoryRequest
  | VoicePostToActiveChatRequest
  | UiNavigateRequest
  | ChatCreateAndOpenRequest
  | ChatSendFromVoiceRequest
  | ChatRenameRequest
  | ChatDeleteRequest
  | MemoryAddFactRequest
  | MemoryDeleteFactRequest
  | ActivityListRequest
  | ActivityCancelRequest
  | OsOpenOrFocusAppRequest
  | OsControlMediaVolumeRequest
  | OsReadClipboardRequest
  | OsSelectClipboardItemRequest
  | OsOpenUrlRequest
  | OsControlSystemRequest
  | OsWriteClipboardRequest
  | WorkspaceGitInspectRequest;
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
export type Method3 = "chat.focus";
export type SessionId4 = string | null;
export type Clear = boolean;
export type FrameType4 = "request";
export type Id4 = string;
export type SessionId5 = string | null;
export type Method4 = "approval.respond";
export type ApprovalId = string;
export type Decision = "allow" | "deny";
export type FrameType5 = "request";
export type Id5 = string;
export type SessionId6 = string | null;
export type Method5 = "memory.get";
export type FrameType6 = "request";
export type Id6 = string;
export type SessionId7 = string | null;
export type Method6 = "memory.get_daily";
/**
 * ISO date, YYYY-MM-DD
 */
export type Date = string;
export type FrameType7 = "request";
export type Id7 = string;
export type SessionId8 = string | null;
export type Method7 = "memory.episodes";
export type Limit = number;
export type FrameType8 = "request";
export type Id8 = string;
export type SessionId9 = string | null;
export type Method8 = "memory.facts";
export type FrameType9 = "request";
export type Id9 = string;
export type SessionId10 = string | null;
export type Method9 = "memory.archive";
export type FrameType10 = "request";
export type Id10 = string;
export type SessionId11 = string | null;
export type Method10 = "memory.status";
export type FrameType11 = "request";
export type Id11 = string;
export type SessionId12 = string | null;
export type Method11 = "memory.reconsolidate";
export type SessionId13 = string;
export type FrameType12 = "request";
export type Id12 = string;
export type SessionId14 = string | null;
export type Method12 = "memory.forget";
/**
 * Entry ID of the fact to forget
 */
export type Id13 = string | null;
/**
 * Keywords, text, or ID of the memory to forget
 */
export type Query = string | null;
export type FrameType13 = "request";
export type Id14 = string;
export type SessionId15 = string | null;
export type Method13 = "transcripts.list";
export type Limit1 = number;
export type FrameType14 = "request";
export type Id15 = string;
export type SessionId16 = string | null;
export type Method14 = "transcripts.get";
export type SessionId17 = string;
export type FrameType15 = "request";
export type Id16 = string;
export type SessionId18 = string | null;
export type Method15 = "dev.emit_sample_task";
export type FrameType16 = "request";
export type Id17 = string;
export type SessionId19 = string | null;
export type Method16 = "conversations.list";
export type FrameType17 = "request";
export type Id18 = string;
export type SessionId20 = string | null;
export type Method17 = "conversations.get";
export type SessionId21 = string;
/**
 * ISO date, YYYY-MM-DD
 */
export type Date1 = string;
export type FrameType18 = "request";
export type Id19 = string;
export type SessionId22 = string | null;
export type Method18 = "conversation.list";
export type FrameType19 = "request";
export type Id20 = string;
export type SessionId23 = string | null;
export type Method19 = "conversation.get";
export type FrameType20 = "request";
export type Id21 = string;
export type SessionId24 = string | null;
export type Method20 = "conversation.resume";
export type SessionId25 = string;
export type FrameType21 = "request";
export type Id22 = string;
export type SessionId26 = string | null;
export type Method21 = "conversation.rename";
export type SessionId27 = string;
export type Title = string;
export type FrameType22 = "request";
export type Id23 = string;
export type SessionId28 = string | null;
export type Method22 = "conversation.pin";
export type SessionId29 = string;
export type Pinned = boolean;
export type FrameType23 = "request";
export type Id24 = string;
export type SessionId30 = string | null;
export type Method23 = "conversation.export";
export type SessionId31 = string;
export type FrameType24 = "request";
export type Id25 = string;
export type SessionId32 = string | null;
export type Method24 = "conversation.delete";
export type SessionId33 = string;
export type DeleteEpisodes = boolean;
export type FrameType25 = "request";
export type Id26 = string;
export type SessionId34 = string | null;
export type Method25 = "conversation.delete_all";
export type Confirmation = string;
export type FrameType26 = "request";
export type Id27 = string;
export type SessionId35 = string | null;
export type Method26 = "conversation.undo_delete";
export type SessionId36 = string | null;
export type FrameType27 = "request";
export type Id28 = string;
export type SessionId37 = string | null;
export type Method27 = "settings.get";
export type FrameType28 = "request";
export type Id29 = string;
export type SessionId38 = string | null;
export type Method28 = "voice.start_call";
export type Trigger = "manual" | "wake_word" | "hotkey" | "ui";
export type FrameType29 = "request";
export type Id30 = string;
export type SessionId39 = string | null;
export type Method29 = "voice.end_call";
export type CallId = string | null;
export type Reason = string | null;
export type Transcript =
  | {
      [k: string]: unknown | undefined;
    }[]
  | null;
export type FrameType30 = "request";
export type Id31 = string;
export type SessionId40 = string | null;
export type Method30 = "voice.status";
export type FrameType31 = "request";
export type Id32 = string;
export type SessionId41 = string | null;
export type Method31 = "voice.delegate_task";
export type Description = string;
export type CallId1 = string | null;
export type ToolCallId = string | null;
export type FrameType32 = "request";
export type Id33 = string;
export type SessionId42 = string | null;
export type Method32 = "voice.history";
export type Limit2 = number;
export type FrameType33 = "request";
export type Id34 = string;
export type SessionId43 = string | null;
export type Method33 = "voice.list_conversations";
export type Limit3 = number;
export type FrameType34 = "request";
export type Id35 = string;
export type SessionId44 = string | null;
export type Method34 = "voice.search_conversations";
export type Query1 = string;
export type Limit4 = number;
export type FrameType35 = "request";
export type Id36 = string;
export type SessionId45 = string | null;
export type Method35 = "voice.read_conversation";
export type ConversationId = string;
export type MaxTurns = number;
export type FrameType36 = "request";
export type Id37 = string;
export type SessionId46 = string | null;
export type Method36 = "voice.search_memory";
export type Query2 = string;
export type Limit5 = number;
export type FrameType37 = "request";
export type Id38 = string;
export type SessionId47 = string | null;
export type Method37 = "voice.post_to_active_chat";
export type Content = string;
export type Label = string | null;
export type FrameType38 = "request";
export type Id39 = string;
export type SessionId48 = string | null;
export type Method38 = "ui.navigate";
export type View = "chat" | "voice" | "activity" | "memory" | "settings";
export type ConversationId1 = string | null;
export type OpenCommandPalette = boolean;
export type FrameType39 = "request";
export type Id40 = string;
export type SessionId49 = string | null;
export type Method39 = "chat.create_and_open";
export type Title1 = string;
export type Content1 = string;
export type OpenInUi = boolean;
export type FrameType40 = "request";
export type Id41 = string;
export type SessionId50 = string | null;
export type Method40 = "chat.send_from_voice";
export type Prompt = string;
export type ConversationId2 = string | null;
export type Title2 = string | null;
export type FrameType41 = "request";
export type Id42 = string;
export type SessionId51 = string | null;
export type Method41 = "chat.rename";
export type ConversationId3 = string;
export type NewTitle = string;
export type FrameType42 = "request";
export type Id43 = string;
export type SessionId52 = string | null;
export type Method42 = "chat.delete";
export type ConversationId4 = string;
export type Confirmed = boolean;
export type DeleteEpisodes1 = boolean;
export type FrameType43 = "request";
export type Id44 = string;
export type SessionId53 = string | null;
export type Method43 = "memory.add_fact";
export type Fact = string;
export type Category = string | null;
export type FrameType44 = "request";
export type Id45 = string;
export type SessionId54 = string | null;
export type Method44 = "memory.delete_fact";
export type QueryOrId = string;
export type FrameType45 = "request";
export type Id46 = string;
export type SessionId55 = string | null;
export type Method45 = "activity.list";
export type Limit6 = number;
export type FrameType46 = "request";
export type Id47 = string;
export type SessionId56 = string | null;
export type Method46 = "activity.cancel";
export type TaskId = string;
export type FrameType47 = "request";
export type Id48 = string;
export type SessionId57 = string | null;
export type Method47 = "os.open_or_focus_app";
export type AppName = string;
export type TargetPath = string | null;
export type FrameType48 = "request";
export type Id49 = string;
export type SessionId58 = string | null;
export type Method48 = "os.control_media_volume";
export type Action =
  | "mute"
  | "unmute"
  | "toggle_mute"
  | "volume_up"
  | "volume_down"
  | "set_volume"
  | "play_pause"
  | "next_track"
  | "previous_track";
export type Level = number | null;
export type FrameType49 = "request";
export type Id50 = string;
export type SessionId59 = string | null;
export type Method49 = "os.read_clipboard";
export type MaxChars = number;
export type IncludeHistory = boolean;
export type MaxItems = number;
export type IncludeImageBytes = boolean;
export type FrameType50 = "request";
export type Id51 = string;
export type SessionId60 = string | null;
export type Method50 = "os.select_clipboard_item";
/**
 * Zero-based index of the clipboard history item to restore
 */
export type Index = number;
export type FrameType51 = "request";
export type Id52 = string;
export type SessionId61 = string | null;
export type Method51 = "os.open_url";
/**
 * URL to open (http or https)
 */
export type Url = string;
/**
 * Optional browser executable or name
 */
export type Browser = string | null;
export type FrameType52 = "request";
export type Id53 = string;
export type SessionId62 = string | null;
export type Method52 = "os.control_system";
/**
 * System control action (brightness, window, lock, settings)
 */
export type Action1 = string;
/**
 * Display brightness level (0-100)
 */
export type Level1 = number | null;
/**
 * Display brightness relative step
 */
export type Step = number | null;
/**
 * Window action: minimize, maximize, restore, close
 */
export type WindowAction = string | null;
/**
 * Window title or substring
 */
export type WindowTitle = string | null;
/**
 * Window title or substring (alias for window_title)
 */
export type Title3 = string | null;
/**
 * Window handle (HWND)
 */
export type Hwnd = number | null;
/**
 * Application name for window action
 */
export type AppName1 = string | null;
/**
 * Windows Settings sub-page (e.g. 'display', 'sound')
 */
export type Page = string | null;
/**
 * Direct ms-settings: URI
 */
export type Uri = string | null;
export type FrameType53 = "request";
export type Id54 = string;
export type SessionId63 = string | null;
export type Method53 = "os.write_clipboard";
export type Text1 = string;
export type FrameType54 = "request";
export type Id55 = string;
export type SessionId64 = string | null;
export type Method54 = "workspace.git_inspect";
export type RepoPath = string | null;
export type IncludeDiff = boolean;
export type DiffStatOnly = boolean;
export type FrameType55 = "response";
export type Id56 = string;
export type Ok = boolean;
export type SessionId65 = string | null;
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
  | ConversationDeletedEvent
  | UiNavigateEvent
  | ConversationCreatedEvent
  | MemoryUpdatedEvent;
export type FrameType56 = "event";
export type RequestId = string | null;
export type SessionId66 = string | null;
export type Event1 = "assistant_token";
export type Text2 = string;
export type FrameType57 = "event";
export type RequestId1 = string | null;
export type SessionId67 = string | null;
export type Event2 = "tool_call_started";
export type CallId2 = string;
export type Name = string;
export type RiskLevel = "read_only" | "reversible" | "destructive";
export type FrameType58 = "event";
export type RequestId2 = string | null;
export type SessionId68 = string | null;
export type Event3 = "tool_call_finished";
export type CallId3 = string;
export type Name1 = string;
export type IsError = boolean;
export type Output = string;
/**
 * Execution time, excluding time spent awaiting approval
 */
export type DurationMs = number;
export type FrameType59 = "event";
export type RequestId3 = string | null;
export type SessionId69 = string | null;
export type Event4 = "approval_requested";
export type ApprovalId1 = string;
export type CallId4 = string | null;
export type Tool = string;
export type RiskLevel1 = "read_only" | "reversible" | "destructive";
export type TimeoutSeconds = number;
export type FrameType60 = "event";
export type RequestId4 = string | null;
export type SessionId70 = string | null;
export type Event5 = "approval_resolved";
export type ApprovalId2 = string;
export type Decision1 = "allow" | "deny";
export type Reason1 = "user" | "timeout";
export type FrameType61 = "event";
export type RequestId5 = string | null;
export type SessionId71 = string | null;
export type Event6 = "task_status";
export type TaskId1 = string;
export type Title4 = string;
export type Status = "queued" | "running" | "succeeded" | "failed" | "cancelled";
export type StartedAt = string;
export type Kind = "chat_turn" | "background_task" | "scheduled" | "agent_run";
export type FinishedAt = string | null;
export type NextRunAt = string | null;
export type Detail = string | null;
export type FrameType62 = "event";
export type RequestId6 = string | null;
export type SessionId72 = string | null;
export type Event7 = "voice_state";
export type State = "idle" | "listening" | "thinking" | "speaking" | "reconnecting" | "error";
export type Error1 = string | null;
export type FrameType63 = "event";
export type RequestId7 = string | null;
export type SessionId73 = string | null;
export type Event8 = "call_started";
export type CallId5 = string;
export type CallState = "idle" | "connecting" | "active" | "ending";
export type StartedAt1 = string | null;
export type FrameType64 = "event";
export type RequestId8 = string | null;
export type SessionId74 = string | null;
export type Event9 = "call_ended";
export type CallId6 = string;
export type CallState1 = "idle" | "connecting" | "active" | "ending";
export type Reason2 = string | null;
export type DurationSeconds = number | null;
export type FrameType65 = "event";
export type RequestId9 = string | null;
export type SessionId75 = string | null;
export type Event10 = "transcript";
export type Role = "user" | "assistant";
export type Text3 = string;
export type Final = boolean;
export type CallId7 = string | null;
export type Timestamp = string | null;
export type FrameType66 = "event";
export type RequestId10 = string | null;
export type SessionId76 = string | null;
export type Event11 = "screen_capture_state";
export type Active = boolean;
export type Target = ("active_window" | "full_screen") | null;
export type CallId8 = string | null;
export type FrameType67 = "event";
export type RequestId11 = string | null;
export type SessionId77 = string | null;
export type Event12 = "usage";
export type CallId9 = string | null;
export type InputTokens = number;
export type OutputTokens = number;
export type TotalTokens = number;
export type CachedTokens = number | null;
export type ThoughtTokens = number | null;
export type FrameType68 = "event";
export type RequestId12 = string | null;
export type SessionId78 = string | null;
export type Event13 = "error";
export type Message = string;
export type Code = string | null;
export type FrameType69 = "event";
export type RequestId13 = string | null;
export type SessionId79 = string | null;
export type Event14 = "conversation_updated";
export type SessionId80 = string;
export type Title5 = string | null;
export type Pinned1 = boolean | null;
export type UpdatedAt = string | null;
export type UserMessage = string | null;
export type AssistantMessage = string | null;
export type Turns = number | null;
export type RequestId14 = string | null;
export type FrameType70 = "event";
export type RequestId15 = string | null;
export type SessionId81 = string | null;
export type Event15 = "conversation_deleted";
export type SessionIds = string[];
export type All = boolean;
export type FrameType71 = "event";
export type RequestId16 = string | null;
export type SessionId82 = string | null;
export type Event16 = "ui.navigate";
export type View1 = "chat" | "voice" | "activity" | "memory" | "settings";
export type ConversationId5 = string | null;
export type OpenCommandPalette1 = boolean;
export type FrameType72 = "event";
export type RequestId17 = string | null;
export type SessionId83 = string | null;
export type Event17 = "conversation_created";
export type SessionId84 = string;
export type Title6 = string;
export type StartedAt2 = string;
export type UpdatedAt1 = string;
export type Turns1 = number;
export type InitialMessage = string | null;
export type OpenInUi1 = boolean;
export type FrameType73 = "event";
export type RequestId18 = string | null;
export type SessionId85 = string | null;
export type Event18 = "memory.updated";
export type Action2 = "added" | "deleted" | "updated";
export type FactId = string | null;
export type Section = string | null;
export type Text4 = string | null;
export type RemovedCount = number;
export type RemovedIds = string[];
export type Status1 = "ok";
export type SessionId86 = string;
export type Text5 = string;
export type ActiveConversationId = string | null;
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
export type Content2 = string;
export type Date3 = string;
export type SessionId87 = string;
export type Time = string;
export type Summary = string;
export type Trivial = boolean;
export type Episodes = EpisodeItem[];
export type Name2 = string;
export type Id57 = string;
export type Date4 = string;
export type Source = string;
export type Text6 = string;
export type Entries = MemoryFactItem[];
export type Sections = MemorySectionItem[];
export type LastConsolidated1 = string | null;
export type FailedConsolidation1 = string | null;
export type Id58 = string;
export type Date5 = string;
export type Source1 = string;
export type Text7 = string;
export type ArchivedAt = string | null;
export type Reason3 = string | null;
export type Entries1 = MemoryArchiveItem[];
export type Raw = string;
export type LastConsolidatedAt = string | null;
export type SessionId88 = string;
export type Status2 = string;
export type RetryCount = number;
export type Error2 = string | null;
export type LastAttemptAt = string | null;
export type NextRetryAt = string | null;
export type Pending = ConsolidationSessionStatus[];
export type Failed = ConsolidationSessionStatus[];
export type CompletedCount = number;
export type SessionId89 = string;
export type Ok1 = boolean;
export type Status3 = string;
export type Error3 = string | null;
export type Ok2 = boolean;
export type RemovedCount1 = number;
export type RemovedIds1 = string[];
export type SessionId90 = string;
export type StartedAt3 = string;
export type Title7 = string;
export type MessageCount = number;
export type Transcripts = TranscriptSummaryItem[];
export type SessionId91 = string;
export type StartedAt4 = string;
export type Title8 = string;
export type Role1 = string;
export type Text8 = string;
export type Timestamp1 = string;
export type ToolCalls = {
  [k: string]: unknown | undefined;
}[];
export type Messages = TranscriptMessageItem[];
export type Status4 = "ok";
export type Date6 = string;
export type SessionId92 = string;
export type ConversationId6 = string;
export type Title9 = string;
export type Turns2 = number;
export type TurnCount = number;
export type LastTime = string;
export type Pinned2 = boolean;
export type UpdatedAt2 = string;
export type EpisodeSummary = string;
export type Conversations = ConversationSummary[];
export type Date7 = string;
export type SessionId93 = string;
export type Time1 = string;
export type User = string;
export type Assistant = string;
export type Turns3 = ConversationTurn[];
export type SessionId94 = string;
export type Title10 = string;
export type Pinned3 = boolean;
export type Role2 = "user" | "assistant";
export type Text9 = string;
export type Timestamp2 = string;
export type ToolCalls1 = {
  [k: string]: unknown | undefined;
}[];
export type Messages1 = ConversationResumeMessage[];
export type SessionId95 = string;
export type Title11 = string;
export type SessionId96 = string;
export type Pinned4 = boolean;
export type SessionId97 = string;
export type Title12 = string;
export type Filename = string;
export type Content3 = string;
export type SessionId98 = string;
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
export type VoiceState = "idle" | "listening" | "thinking" | "speaking" | "reconnecting" | "error";
export type MemoryContext = string | null;
export type CallId11 = string | null;
export type CallState3 = "idle" | "connecting" | "active" | "ending";
export type VoiceState1 = "idle" | "listening" | "thinking" | "speaking" | "reconnecting" | "error";
export type CallId12 = string | null;
export type CallState4 = "idle" | "connecting" | "active" | "ending";
export type VoiceState2 = "idle" | "listening" | "thinking" | "speaking" | "reconnecting" | "error";
export type ScreenCaptureActive = boolean;
export type ScreenCaptureTarget = ("active_window" | "full_screen") | null;
export type TaskId2 = string;
export type Status5 = "queued" | "running" | "succeeded" | "failed" | "cancelled";
export type Output1 = string;
export type SessionId99 = string;
export type Role3 = "user" | "assistant";
export type Text10 = string;
export type Timestamp3 = string;
export type CallId13 = string | null;
export type Messages2 = VoiceHistoryMessageItem[];
export type ConversationId7 = string;
export type SessionId100 = string;
export type Title13 = string;
export type UpdatedAt3 = string;
export type TurnCount1 = number;
export type Turns4 = number;
export type EpisodeSummary1 = string;
export type Date8 = string;
export type LastTime1 = string;
export type Pinned5 = boolean;
export type Conversations1 = VoiceConversationListItem[];
export type SessionId101 = string;
export type ConversationId8 = string;
export type Title14 = string;
export type UpdatedAt4 = string;
export type EpisodeSummary2 = string;
export type Role4 = string;
export type Timestamp4 = string;
export type Excerpt = string;
export type Snippet = string;
export type MatchingSnippets = ConversationSearchSnippet[];
export type Score = number;
export type SemanticScore = number | null;
export type LexicalScore = number;
export type Role5 = string;
export type Timestamp5 = string;
export type Snippet1 = string;
export type Source2 = string;
export type CallId14 = string | null;
export type Results = ConversationSearchHit[];
export type Found = boolean;
export type ConversationId9 = string;
export type SessionId102 = string;
export type Title15 = string;
export type UpdatedAt5 = string;
export type EpisodeSummary3 = string;
export type TotalTurns = number;
export type Truncated = boolean;
export type Role6 = string;
export type Content4 = string;
export type Timestamp6 = string;
export type Turns5 = VoiceConversationTurnItem[];
export type Message1 = string;
export type Kind1 = "fact" | "episode";
export type ItemId = string;
export type Section1 = string;
export type Date9 = string;
export type Source3 = string;
export type SessionId103 = string;
export type ConversationId10 = string;
export type Text11 = string;
export type Score1 = number;
export type SemanticScore1 = number | null;
export type LexicalScore1 = number;
export type Results1 = MemorySearchHit[];
export type Ok3 = boolean;
export type SessionId104 = string | null;
export type Title16 = string | null;
export type Message2 = string;
export type Ok4 = boolean;
export type View2 = "chat" | "voice" | "activity" | "memory" | "settings";
export type ActiveConversationId1 = string | null;
export type OpenCommandPalette2 = boolean;
export type Ok5 = boolean;
export type SessionId105 = string;
export type Title17 = string;
export type Message3 = string;
export type OpenInUi2 = boolean;
export type Ok6 = boolean;
export type SessionId106 = string;
export type Title18 = string;
export type RequestId19 = string;
export type Status6 = string;
export type Ok7 = boolean;
export type SessionId107 = string;
export type Title19 = string;
export type Ok8 = boolean;
export type SessionId108 = string;
export type Deleted1 = boolean;
export type RequiresConfirmation = boolean;
export type EpisodesRemoved = number;
export type Message4 = string;
export type Ok9 = boolean;
export type Section2 = string;
export type Ok10 = boolean;
export type RemovedCount2 = number;
export type RemovedIds2 = string[];
export type Tasks = TaskStatusData[];
export type Ok11 = boolean;
export type TaskId3 = string;
export type Status7 = string;
export type Message5 = string;
export type Ok12 = boolean;
export type Status8 = "focused" | "launched";
export type App = string;
export type WindowTitle1 = string | null;
export type Hwnd1 = number | null;
export type Executable = string | null;
export type TargetPath1 = string | null;
export type Ok13 = boolean;
export type Action3 =
  | "mute"
  | "unmute"
  | "toggle_mute"
  | "volume_up"
  | "volume_down"
  | "set_volume"
  | "play_pause"
  | "next_track"
  | "previous_track";
export type Level2 = number | null;
export type Ok14 = boolean;
export type Text12 = string;
export type Truncated1 = boolean;
export type CharCount = number;
export type TotalChars = number;
export type HistoryEnabled = boolean;
export type TotalItems = number;
export type Index1 = number;
export type Id59 = string;
export type Timestamp7 = string;
export type Kind2 = string;
export type Text13 = string | null;
export type Truncated2 = boolean;
export type CharCount1 = number;
export type TotalChars1 = number;
export type Width = number | null;
export type Height = number | null;
export type Format = string | null;
export type ImageBytesBase64 = string | null;
export type SizeBytes = number | null;
export type Image = {
  [k: string]: unknown | undefined;
} | null;
export type Items = ClipboardItem[];
export type Ok15 = boolean;
export type Success = boolean;
export type Status9 = string;
export type Index2 = number;
export type ItemId1 = string | null;
export type Message6 = string | null;
export type Ok16 = boolean;
export type Success1 = boolean;
export type Status10 = string;
export type Url1 = string;
export type Browser1 = string | null;
export type Message7 = string | null;
export type Ok17 = boolean;
export type Success2 = boolean;
export type Action4 = string;
export type Status11 = string;
export type Level3 = number | null;
export type CurrentBrightness = number | null;
export type WindowTitle2 = string | null;
export type Hwnd2 = number | null;
export type Page1 = string | null;
export type Uri1 = string | null;
export type Message8 = string | null;
export type Ok18 = boolean;
export type CharCount2 = number;
export type Ok19 = boolean;
export type RepoPath1 = string;
export type Branch = string;
export type IsClean = boolean;
export type Status12 = string;
export type Staged = string[];
export type Unstaged = string[];
export type Untracked = string[];
export type Path = string;
export type Head = string;
export type Branch1 = string;
export type Worktrees = GitWorktreeItem[];
export type Sha = string;
export type Subject = string;
export type Oneline = string;
export type RecentCommits = GitCommitItem[];
export type Diff = string | null;
export type DiffTruncated = boolean;
export type RiskTier = "read_only" | "reversible" | "destructive";
export type ApprovalDecision = "allow" | "deny";
export type ApprovalReason = "user" | "timeout";
export type TaskState = "queued" | "running" | "succeeded" | "failed" | "cancelled";
export type TaskKind = "chat_turn" | "background_task" | "scheduled" | "agent_run";
export type VoiceState3 = "idle" | "listening" | "thinking" | "speaking" | "reconnecting" | "error";
export type CallState5 = "idle" | "connecting" | "active" | "ending";
export type TranscriptRole = "user" | "assistant";
export type ScreenCaptureTarget1 = "active_window" | "full_screen";
export type VoiceTrigger = "manual" | "wake_word" | "hotkey" | "ui";
export type UiView = "chat" | "voice" | "activity" | "memory" | "settings";
export type MediaVolumeAction =
  | "mute"
  | "unmute"
  | "toggle_mute"
  | "volume_up"
  | "volume_down"
  | "set_volume"
  | "play_pause"
  | "next_track"
  | "previous_track";

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
  ui_view: UiView;
  media_volume_action: MediaVolumeAction;
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
  chat_focus_request: ChatFocusRequest;
  voice_start_call_request: VoiceStartCallRequest;
  voice_end_call_request: VoiceEndCallRequest;
  voice_status_request: VoiceStatusRequest;
  voice_delegate_task_request: VoiceDelegateTaskRequest;
  voice_history_request: VoiceHistoryRequest;
  voice_list_conversations_request: VoiceListConversationsRequest;
  voice_search_conversations_request: VoiceSearchConversationsRequest;
  voice_read_conversation_request: VoiceReadConversationRequest;
  voice_search_memory_request: VoiceSearchMemoryRequest;
  voice_post_to_active_chat_request: VoicePostToActiveChatRequest;
  voice_conversation_list_item: VoiceConversationListItem;
  conversation_search_snippet: ConversationSearchSnippet;
  ui_navigate_request: UiNavigateRequest;
  chat_create_and_open_request: ChatCreateAndOpenRequest;
  chat_send_from_voice_request: ChatSendFromVoiceRequest;
  chat_rename_request: ChatRenameRequest;
  chat_delete_request: ChatDeleteRequest;
  memory_add_fact_request: MemoryAddFactRequest;
  memory_delete_fact_request: MemoryDeleteFactRequest;
  activity_list_request: ActivityListRequest;
  activity_cancel_request: ActivityCancelRequest;
  os_open_or_focus_app_request: OsOpenOrFocusAppRequest;
  os_control_media_volume_request: OsControlMediaVolumeRequest;
  os_read_clipboard_request: OsReadClipboardRequest;
  os_select_clipboard_item_request: OsSelectClipboardItemRequest;
  os_open_url_request: OsOpenUrlRequest;
  os_control_system_request: OsControlSystemRequest;
  os_write_clipboard_request: OsWriteClipboardRequest;
  clipboard_item: ClipboardItem;
  workspace_git_inspect_request: WorkspaceGitInspectRequest;
  git_worktree_item: GitWorktreeItem;
  git_commit_item: GitCommitItem;
  conversation_search_hit: ConversationSearchHit;
  voice_conversation_turn_item: VoiceConversationTurnItem;
  memory_search_hit: MemorySearchHit;
  voice_history_message_item: VoiceHistoryMessageItem;
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
  ui_navigate_event: UiNavigateEvent;
  conversation_created_event: ConversationCreatedEvent;
  memory_updated_event: MemoryUpdatedEvent;
  conversation_updated_data: ConversationUpdatedData;
  conversation_deleted_data: ConversationDeletedData;
  ui_navigate_data: UiNavigateData;
  conversation_created_data: ConversationCreatedData;
  memory_updated_data: MemoryUpdatedData;
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
export interface ChatFocusRequest {
  frame_type?: FrameType3;
  id?: Id3;
  session_id?: SessionId3;
  method?: Method3;
  params?: ChatFocusParams;
}
export interface ChatFocusParams {
  session_id?: SessionId4;
  clear?: Clear;
}
export interface ApprovalRespondRequest {
  frame_type?: FrameType4;
  id?: Id4;
  session_id?: SessionId5;
  method?: Method4;
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
  frame_type?: FrameType5;
  id?: Id5;
  session_id?: SessionId6;
  method?: Method5;
  params?: _EmptyParams;
}
export interface MemoryGetDailyRequest {
  frame_type?: FrameType6;
  id?: Id6;
  session_id?: SessionId7;
  method?: Method6;
  params: DailyParams;
}
export interface DailyParams {
  date: Date;
}
export interface MemoryEpisodesRequest {
  frame_type?: FrameType7;
  id?: Id7;
  session_id?: SessionId8;
  method?: Method7;
  params?: MemoryEpisodesParams;
}
export interface MemoryEpisodesParams {
  limit?: Limit;
}
/**
 * Fetch structured durable facts parsed by section.
 */
export interface MemoryFactsRequest {
  frame_type?: FrameType8;
  id?: Id8;
  session_id?: SessionId9;
  method?: Method8;
  params?: _EmptyParams;
}
/**
 * Fetch archived facts from MEMORY_ARCHIVE.md.
 */
export interface MemoryArchiveRequest {
  frame_type?: FrameType9;
  id?: Id9;
  session_id?: SessionId10;
  method?: Method9;
  params?: _EmptyParams;
}
/**
 * Fetch consolidation status (pending, failed, last-success).
 */
export interface MemoryStatusRequest {
  frame_type?: FrameType10;
  id?: Id10;
  session_id?: SessionId11;
  method?: Method10;
  params?: _EmptyParams;
}
/**
 * Trigger manual reconsolidation of a session.
 */
export interface MemoryReconsolidateRequest {
  frame_type?: FrameType11;
  id?: Id11;
  session_id?: SessionId12;
  method?: Method11;
  params: MemoryReconsolidateParams;
}
export interface MemoryReconsolidateParams {
  session_id: SessionId13;
}
/**
 * Forget durable facts or preferences matching an entry ID or query.
 */
export interface MemoryForgetRequest {
  frame_type?: FrameType12;
  id?: Id12;
  session_id?: SessionId14;
  method?: Method12;
  params: MemoryForgetParams;
}
export interface MemoryForgetParams {
  id?: Id13;
  query?: Query;
}
export interface TranscriptsListRequest {
  frame_type?: FrameType13;
  id?: Id14;
  session_id?: SessionId15;
  method?: Method13;
  params?: TranscriptsListParams;
}
export interface TranscriptsListParams {
  limit?: Limit1;
}
export interface TranscriptsGetRequest {
  frame_type?: FrameType14;
  id?: Id15;
  session_id?: SessionId16;
  method?: Method14;
  params: TranscriptsGetParams;
}
export interface TranscriptsGetParams {
  session_id: SessionId17;
}
export interface DevEmitSampleTaskRequest {
  frame_type?: FrameType15;
  id?: Id16;
  session_id?: SessionId18;
  method?: Method15;
  params?: _EmptyParams;
}
export interface ConversationsListRequest {
  frame_type?: FrameType16;
  id?: Id17;
  session_id?: SessionId19;
  method?: Method16;
  params?: _EmptyParams;
}
export interface ConversationsGetRequest {
  frame_type?: FrameType17;
  id?: Id18;
  session_id?: SessionId20;
  method?: Method17;
  params: ConversationGetParams;
}
export interface ConversationGetParams {
  session_id: SessionId21;
  date?: Date1;
}
export interface ConversationListRequest {
  frame_type?: FrameType18;
  id?: Id19;
  session_id?: SessionId22;
  method?: Method18;
  params?: _EmptyParams;
}
export interface ConversationGetRequest {
  frame_type?: FrameType19;
  id?: Id20;
  session_id?: SessionId23;
  method?: Method19;
  params: ConversationGetParams;
}
export interface ConversationResumeRequest {
  frame_type?: FrameType20;
  id?: Id21;
  session_id?: SessionId24;
  method?: Method20;
  params: ConversationResumeParams;
}
export interface ConversationResumeParams {
  session_id: SessionId25;
}
export interface ConversationRenameRequest {
  frame_type?: FrameType21;
  id?: Id22;
  session_id?: SessionId26;
  method?: Method21;
  params: ConversationRenameParams;
}
export interface ConversationRenameParams {
  session_id: SessionId27;
  title: Title;
}
export interface ConversationPinRequest {
  frame_type?: FrameType22;
  id?: Id23;
  session_id?: SessionId28;
  method?: Method22;
  params: ConversationPinParams;
}
export interface ConversationPinParams {
  session_id: SessionId29;
  pinned: Pinned;
}
export interface ConversationExportRequest {
  frame_type?: FrameType23;
  id?: Id24;
  session_id?: SessionId30;
  method?: Method23;
  params: ConversationExportParams;
}
export interface ConversationExportParams {
  session_id: SessionId31;
}
export interface ConversationDeleteRequest {
  frame_type?: FrameType24;
  id?: Id25;
  session_id?: SessionId32;
  method?: Method24;
  params: ConversationDeleteParams;
}
export interface ConversationDeleteParams {
  session_id: SessionId33;
  delete_episodes?: DeleteEpisodes;
}
export interface ConversationDeleteAllRequest {
  frame_type?: FrameType25;
  id?: Id26;
  session_id?: SessionId34;
  method?: Method25;
  params: ConversationDeleteAllParams;
}
export interface ConversationDeleteAllParams {
  confirmation: Confirmation;
}
export interface ConversationUndoDeleteRequest {
  frame_type?: FrameType26;
  id?: Id27;
  session_id?: SessionId35;
  method?: Method26;
  params: ConversationUndoDeleteParams;
}
export interface ConversationUndoDeleteParams {
  session_id?: SessionId36;
}
export interface SettingsGetRequest {
  frame_type?: FrameType27;
  id?: Id28;
  session_id?: SessionId37;
  method?: Method27;
  params?: _EmptyParams;
}
export interface VoiceStartCallRequest {
  frame_type?: FrameType28;
  id?: Id29;
  session_id?: SessionId38;
  method?: Method28;
  params?: VoiceStartCallParams;
}
export interface VoiceStartCallParams {
  trigger?: Trigger;
}
export interface VoiceEndCallRequest {
  frame_type?: FrameType29;
  id?: Id30;
  session_id?: SessionId39;
  method?: Method29;
  params?: VoiceEndCallParams;
}
export interface VoiceEndCallParams {
  call_id?: CallId;
  reason?: Reason;
  transcript?: Transcript;
}
export interface VoiceStatusRequest {
  frame_type?: FrameType30;
  id?: Id31;
  session_id?: SessionId40;
  method?: Method30;
  params?: _EmptyParams;
}
export interface VoiceDelegateTaskRequest {
  frame_type?: FrameType31;
  id?: Id32;
  session_id?: SessionId41;
  method?: Method31;
  params: VoiceDelegateTaskParams;
}
export interface VoiceDelegateTaskParams {
  description: Description;
  call_id?: CallId1;
  tool_call_id?: ToolCallId;
}
export interface VoiceHistoryRequest {
  frame_type?: FrameType32;
  id?: Id33;
  session_id?: SessionId42;
  method?: Method32;
  params?: VoiceHistoryParams;
}
export interface VoiceHistoryParams {
  limit?: Limit2;
}
export interface VoiceListConversationsRequest {
  frame_type?: FrameType33;
  id?: Id34;
  session_id?: SessionId43;
  method?: Method33;
  params?: VoiceListConversationsParams;
}
export interface VoiceListConversationsParams {
  limit?: Limit3;
}
export interface VoiceSearchConversationsRequest {
  frame_type?: FrameType34;
  id?: Id35;
  session_id?: SessionId44;
  method?: Method34;
  params: VoiceSearchConversationsParams;
}
export interface VoiceSearchConversationsParams {
  query: Query1;
  limit?: Limit4;
}
export interface VoiceReadConversationRequest {
  frame_type?: FrameType35;
  id?: Id36;
  session_id?: SessionId45;
  method?: Method35;
  params: VoiceReadConversationParams;
}
export interface VoiceReadConversationParams {
  conversation_id: ConversationId;
  max_turns?: MaxTurns;
}
export interface VoiceSearchMemoryRequest {
  frame_type?: FrameType36;
  id?: Id37;
  session_id?: SessionId46;
  method?: Method36;
  params: VoiceSearchMemoryParams;
}
export interface VoiceSearchMemoryParams {
  query: Query2;
  limit?: Limit5;
}
export interface VoicePostToActiveChatRequest {
  frame_type?: FrameType37;
  id?: Id38;
  session_id?: SessionId47;
  method?: Method37;
  params: VoicePostToActiveChatParams;
}
export interface VoicePostToActiveChatParams {
  content: Content;
  label?: Label;
}
export interface UiNavigateRequest {
  frame_type?: FrameType38;
  id?: Id39;
  session_id?: SessionId48;
  method?: Method38;
  params?: UiNavigateParams;
}
export interface UiNavigateParams {
  view?: View;
  conversation_id?: ConversationId1;
  open_command_palette?: OpenCommandPalette;
}
export interface ChatCreateAndOpenRequest {
  frame_type?: FrameType39;
  id?: Id40;
  session_id?: SessionId49;
  method?: Method39;
  params: ChatCreateAndOpenParams;
}
export interface ChatCreateAndOpenParams {
  title: Title1;
  content?: Content1;
  open_in_ui?: OpenInUi;
}
export interface ChatSendFromVoiceRequest {
  frame_type?: FrameType40;
  id?: Id41;
  session_id?: SessionId50;
  method?: Method40;
  params: ChatSendFromVoiceParams;
}
export interface ChatSendFromVoiceParams {
  prompt: Prompt;
  conversation_id?: ConversationId2;
  title?: Title2;
}
export interface ChatRenameRequest {
  frame_type?: FrameType41;
  id?: Id42;
  session_id?: SessionId51;
  method?: Method41;
  params: ChatRenameParams;
}
export interface ChatRenameParams {
  conversation_id: ConversationId3;
  new_title: NewTitle;
}
export interface ChatDeleteRequest {
  frame_type?: FrameType42;
  id?: Id43;
  session_id?: SessionId52;
  method?: Method42;
  params: ChatDeleteParams;
}
export interface ChatDeleteParams {
  conversation_id: ConversationId4;
  confirmed?: Confirmed;
  delete_episodes?: DeleteEpisodes1;
}
export interface MemoryAddFactRequest {
  frame_type?: FrameType43;
  id?: Id44;
  session_id?: SessionId53;
  method?: Method43;
  params: MemoryAddFactParams;
}
export interface MemoryAddFactParams {
  fact: Fact;
  category?: Category;
}
export interface MemoryDeleteFactRequest {
  frame_type?: FrameType44;
  id?: Id45;
  session_id?: SessionId54;
  method?: Method44;
  params: MemoryDeleteFactParams;
}
export interface MemoryDeleteFactParams {
  query_or_id: QueryOrId;
}
export interface ActivityListRequest {
  frame_type?: FrameType45;
  id?: Id46;
  session_id?: SessionId55;
  method?: Method45;
  params?: ActivityListParams;
}
export interface ActivityListParams {
  limit?: Limit6;
}
export interface ActivityCancelRequest {
  frame_type?: FrameType46;
  id?: Id47;
  session_id?: SessionId56;
  method?: Method46;
  params: ActivityCancelParams;
}
export interface ActivityCancelParams {
  task_id: TaskId;
}
export interface OsOpenOrFocusAppRequest {
  frame_type?: FrameType47;
  id?: Id48;
  session_id?: SessionId57;
  method?: Method47;
  params: OsOpenOrFocusAppParams;
}
export interface OsOpenOrFocusAppParams {
  app_name: AppName;
  target_path?: TargetPath;
}
export interface OsControlMediaVolumeRequest {
  frame_type?: FrameType48;
  id?: Id49;
  session_id?: SessionId58;
  method?: Method48;
  params: OsControlMediaVolumeParams;
}
export interface OsControlMediaVolumeParams {
  action: Action;
  level?: Level;
}
export interface OsReadClipboardRequest {
  frame_type?: FrameType49;
  id?: Id50;
  session_id?: SessionId59;
  method?: Method49;
  params?: OsReadClipboardParams;
}
export interface OsReadClipboardParams {
  max_chars?: MaxChars;
  include_history?: IncludeHistory;
  max_items?: MaxItems;
  include_image_bytes?: IncludeImageBytes;
}
export interface OsSelectClipboardItemRequest {
  frame_type?: FrameType50;
  id?: Id51;
  session_id?: SessionId60;
  method?: Method50;
  params: OsSelectClipboardItemParams;
}
export interface OsSelectClipboardItemParams {
  index: Index;
}
export interface OsOpenUrlRequest {
  frame_type?: FrameType51;
  id?: Id52;
  session_id?: SessionId61;
  method?: Method51;
  params: OsOpenUrlParams;
}
export interface OsOpenUrlParams {
  url: Url;
  browser?: Browser;
}
export interface OsControlSystemRequest {
  frame_type?: FrameType52;
  id?: Id53;
  session_id?: SessionId62;
  method?: Method52;
  params: OsControlSystemParams;
}
export interface OsControlSystemParams {
  action: Action1;
  level?: Level1;
  step?: Step;
  window_action?: WindowAction;
  window_title?: WindowTitle;
  title?: Title3;
  hwnd?: Hwnd;
  app_name?: AppName1;
  page?: Page;
  uri?: Uri;
}
export interface OsWriteClipboardRequest {
  frame_type?: FrameType53;
  id?: Id54;
  session_id?: SessionId63;
  method?: Method53;
  params: OsWriteClipboardParams;
}
export interface OsWriteClipboardParams {
  text: Text1;
}
export interface WorkspaceGitInspectRequest {
  frame_type?: FrameType54;
  id?: Id55;
  session_id?: SessionId64;
  method?: Method54;
  params?: WorkspaceGitInspectParams;
}
export interface WorkspaceGitInspectParams {
  repo_path?: RepoPath;
  include_diff?: IncludeDiff;
  diff_stat_only?: DiffStatOnly;
}
/**
 * Daemon-to-client response paired with a request ID.
 */
export interface ResponseFrame {
  frame_type?: FrameType55;
  id: Id56;
  ok?: Ok;
  session_id?: SessionId65;
  result?: Result;
  error?: Error;
}
export interface Result {
  [k: string]: unknown | undefined;
}
export interface AssistantTokenEvent {
  frame_type?: FrameType56;
  request_id?: RequestId;
  session_id?: SessionId66;
  event?: Event1;
  data: AssistantTokenData;
}
export interface AssistantTokenData {
  text: Text2;
}
export interface ToolCallStartedEvent {
  frame_type?: FrameType57;
  request_id?: RequestId1;
  session_id?: SessionId67;
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
  frame_type?: FrameType58;
  request_id?: RequestId2;
  session_id?: SessionId68;
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
  frame_type?: FrameType59;
  request_id?: RequestId3;
  session_id?: SessionId69;
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
  frame_type?: FrameType60;
  request_id?: RequestId4;
  session_id?: SessionId70;
  event?: Event5;
  data: ApprovalResolvedData;
}
export interface ApprovalResolvedData {
  approval_id: ApprovalId2;
  decision: Decision1;
  reason: Reason1;
}
export interface TaskStatusEvent {
  frame_type?: FrameType61;
  request_id?: RequestId5;
  session_id?: SessionId71;
  event?: Event6;
  data: TaskStatusData;
}
export interface TaskStatusData {
  task_id: TaskId1;
  title: Title4;
  status: Status;
  started_at: StartedAt;
  kind?: Kind;
  finished_at?: FinishedAt;
  next_run_at?: NextRunAt;
  detail?: Detail;
}
export interface VoiceStateEvent {
  frame_type?: FrameType62;
  request_id?: RequestId6;
  session_id?: SessionId72;
  event?: Event7;
  data: VoiceStateData;
}
export interface VoiceStateData {
  state: State;
  error?: Error1;
}
export interface CallStartedEvent {
  frame_type?: FrameType63;
  request_id?: RequestId7;
  session_id?: SessionId73;
  event?: Event8;
  data: CallStartedData;
}
export interface CallStartedData {
  call_id: CallId5;
  call_state?: CallState;
  started_at?: StartedAt1;
}
export interface CallEndedEvent {
  frame_type?: FrameType64;
  request_id?: RequestId8;
  session_id?: SessionId74;
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
  frame_type?: FrameType65;
  request_id?: RequestId9;
  session_id?: SessionId75;
  event?: Event10;
  data: TranscriptData;
}
export interface TranscriptData {
  role: Role;
  text: Text3;
  final?: Final;
  call_id?: CallId7;
  timestamp?: Timestamp;
}
export interface ScreenCaptureStateEvent {
  frame_type?: FrameType66;
  request_id?: RequestId10;
  session_id?: SessionId76;
  event?: Event11;
  data: ScreenCaptureStateData;
}
export interface ScreenCaptureStateData {
  active: Active;
  target?: Target;
  call_id?: CallId8;
}
export interface UsageEvent {
  frame_type?: FrameType67;
  request_id?: RequestId11;
  session_id?: SessionId77;
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
  frame_type?: FrameType68;
  request_id?: RequestId12;
  session_id?: SessionId78;
  event?: Event13;
  data: ErrorData;
}
export interface ErrorData {
  message: Message;
  code?: Code;
}
export interface ConversationUpdatedEvent {
  frame_type?: FrameType69;
  request_id?: RequestId13;
  session_id?: SessionId79;
  event?: Event14;
  data: ConversationUpdatedData;
}
export interface ConversationUpdatedData {
  session_id: SessionId80;
  title?: Title5;
  pinned?: Pinned1;
  updated_at?: UpdatedAt;
  user_message?: UserMessage;
  assistant_message?: AssistantMessage;
  turns?: Turns;
  request_id?: RequestId14;
}
export interface ConversationDeletedEvent {
  frame_type?: FrameType70;
  request_id?: RequestId15;
  session_id?: SessionId81;
  event?: Event15;
  data: ConversationDeletedData;
}
export interface ConversationDeletedData {
  session_ids?: SessionIds;
  all?: All;
}
export interface UiNavigateEvent {
  frame_type?: FrameType71;
  request_id?: RequestId16;
  session_id?: SessionId82;
  event?: Event16;
  data: UiNavigateData;
}
export interface UiNavigateData {
  view?: View1;
  conversation_id?: ConversationId5;
  open_command_palette?: OpenCommandPalette1;
}
export interface ConversationCreatedEvent {
  frame_type?: FrameType72;
  request_id?: RequestId17;
  session_id?: SessionId83;
  event?: Event17;
  data: ConversationCreatedData;
}
export interface ConversationCreatedData {
  session_id: SessionId84;
  title: Title6;
  started_at?: StartedAt2;
  updated_at?: UpdatedAt1;
  turns?: Turns1;
  initial_message?: InitialMessage;
  open_in_ui?: OpenInUi1;
}
export interface MemoryUpdatedEvent {
  frame_type?: FrameType73;
  request_id?: RequestId18;
  session_id?: SessionId85;
  event?: Event18;
  data: MemoryUpdatedData;
}
export interface MemoryUpdatedData {
  action?: Action2;
  fact_id?: FactId;
  section?: Section;
  text?: Text4;
  removed_count?: RemovedCount;
  removed_ids?: RemovedIds;
}
export interface MethodResults {
  health_ping: HealthPingResult;
  session_new: SessionNewResult;
  chat_send: ChatSendResult;
  chat_focus: ChatFocusResult;
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
  voice_history: VoiceHistoryResult;
  voice_list_conversations: VoiceListConversationsResult;
  voice_search_conversations: VoiceSearchConversationsResult;
  voice_read_conversation: VoiceReadConversationResult;
  voice_search_memory: VoiceSearchMemoryResult;
  voice_post_to_active_chat: VoicePostToActiveChatResult;
  ui_navigate: UiNavigateResult;
  chat_create_and_open: ChatCreateAndOpenResult;
  chat_send_from_voice: ChatSendFromVoiceResult;
  chat_rename: ChatRenameResult;
  chat_delete: ChatDeleteResult;
  memory_add_fact: MemoryAddFactResult;
  memory_delete_fact: MemoryDeleteFactResult;
  activity_list: ActivityListResult;
  activity_cancel: ActivityCancelResult;
  os_open_or_focus_app: OsOpenOrFocusAppResult;
  os_control_media_volume: OsControlMediaVolumeResult;
  os_read_clipboard: OsReadClipboardResult;
  os_select_clipboard_item: OsSelectClipboardItemResult;
  os_open_url: OsOpenUrlResult;
  os_control_system: OsControlSystemResult;
  os_write_clipboard: OsWriteClipboardResult;
  workspace_git_inspect: WorkspaceGitInspectResult;
}
export interface HealthPingResult {
  status?: Status1;
}
export interface SessionNewResult {
  session_id: SessionId86;
}
export interface ChatSendResult {
  text: Text5;
}
export interface ChatFocusResult {
  active_conversation_id?: ActiveConversationId;
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
  content: Content2;
}
export interface MemoryEpisodesResult {
  episodes: Episodes;
}
export interface EpisodeItem {
  date: Date3;
  session_id: SessionId87;
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
  id: Id57;
  date: Date4;
  source: Source;
  text: Text6;
}
export interface MemoryArchiveResult {
  entries?: Entries1;
  raw?: Raw;
}
export interface MemoryArchiveItem {
  id: Id58;
  date: Date5;
  source: Source1;
  text: Text7;
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
  session_id: SessionId88;
  status: Status2;
  retry_count: RetryCount;
  error?: Error2;
  last_attempt_at?: LastAttemptAt;
  next_retry_at?: NextRetryAt;
}
export interface MemoryReconsolidateResult {
  session_id: SessionId89;
  ok: Ok1;
  status: Status3;
  error?: Error3;
}
export interface MemoryForgetResult {
  ok?: Ok2;
  removed_count?: RemovedCount1;
  removed_ids?: RemovedIds1;
}
export interface TranscriptsListResult {
  transcripts: Transcripts;
}
export interface TranscriptSummaryItem {
  session_id: SessionId90;
  started_at: StartedAt3;
  title: Title7;
  message_count: MessageCount;
}
export interface TranscriptsGetResult {
  session_id: SessionId91;
  started_at: StartedAt4;
  title: Title8;
  messages: Messages;
}
export interface TranscriptMessageItem {
  role: Role1;
  text: Text8;
  timestamp: Timestamp1;
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
  session_id: SessionId92;
  conversation_id?: ConversationId6;
  title: Title9;
  turns: Turns2;
  turn_count?: TurnCount;
  last_time: LastTime;
  pinned?: Pinned2;
  updated_at?: UpdatedAt2;
  episode_summary?: EpisodeSummary;
}
export interface ConversationsGetResult {
  date: Date7;
  session_id: SessionId93;
  turns: Turns3;
}
export interface ConversationTurn {
  time: Time1;
  user: User;
  assistant: Assistant;
}
export interface ConversationResumeResult {
  session_id: SessionId94;
  title: Title10;
  pinned?: Pinned3;
  messages: Messages1;
}
export interface ConversationResumeMessage {
  role: Role2;
  text: Text9;
  timestamp?: Timestamp2;
  tool_calls?: ToolCalls1;
}
export interface ConversationRenameResult {
  session_id: SessionId95;
  title: Title11;
}
export interface ConversationPinResult {
  session_id: SessionId96;
  pinned: Pinned4;
}
export interface ConversationExportResult {
  session_id: SessionId97;
  title: Title12;
  filename: Filename;
  content: Content3;
}
export interface ConversationDeleteResult {
  session_id: SessionId98;
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
  task_id: TaskId2;
  status: Status5;
  output?: Output1;
}
export interface VoiceHistoryResult {
  session_id?: SessionId99;
  messages?: Messages2;
}
export interface VoiceHistoryMessageItem {
  role: Role3;
  text: Text10;
  timestamp?: Timestamp3;
  call_id?: CallId13;
}
export interface VoiceListConversationsResult {
  conversations?: Conversations1;
}
export interface VoiceConversationListItem {
  conversation_id?: ConversationId7;
  session_id?: SessionId100;
  title: Title13;
  updated_at?: UpdatedAt3;
  turn_count?: TurnCount1;
  turns?: Turns4;
  episode_summary?: EpisodeSummary1;
  date?: Date8;
  last_time?: LastTime1;
  pinned?: Pinned5;
}
export interface VoiceSearchConversationsResult {
  results?: Results;
}
export interface ConversationSearchHit {
  session_id?: SessionId101;
  conversation_id?: ConversationId8;
  title: Title14;
  updated_at?: UpdatedAt4;
  episode_summary?: EpisodeSummary2;
  matching_snippets?: MatchingSnippets;
  score?: Score;
  semantic_score?: SemanticScore;
  lexical_score?: LexicalScore;
  role?: Role5;
  timestamp?: Timestamp5;
  snippet?: Snippet1;
  source?: Source2;
  call_id?: CallId14;
}
export interface ConversationSearchSnippet {
  role: Role4;
  timestamp: Timestamp4;
  excerpt?: Excerpt;
  snippet?: Snippet;
}
export interface VoiceReadConversationResult {
  found?: Found;
  conversation_id?: ConversationId9;
  session_id?: SessionId102;
  title?: Title15;
  updated_at?: UpdatedAt5;
  episode_summary?: EpisodeSummary3;
  total_turns?: TotalTurns;
  truncated?: Truncated;
  turns?: Turns5;
  message?: Message1;
}
export interface VoiceConversationTurnItem {
  role: Role6;
  content: Content4;
  timestamp?: Timestamp6;
}
export interface VoiceSearchMemoryResult {
  results?: Results1;
}
export interface MemorySearchHit {
  kind: Kind1;
  item_id: ItemId;
  section?: Section1;
  date?: Date9;
  source?: Source3;
  session_id?: SessionId103;
  conversation_id?: ConversationId10;
  text: Text11;
  score?: Score1;
  semantic_score?: SemanticScore1;
  lexical_score?: LexicalScore1;
}
export interface VoicePostToActiveChatResult {
  ok: Ok3;
  session_id?: SessionId104;
  title?: Title16;
  message?: Message2;
}
export interface UiNavigateResult {
  ok?: Ok4;
  view?: View2;
  active_conversation_id?: ActiveConversationId1;
  open_command_palette?: OpenCommandPalette2;
}
export interface ChatCreateAndOpenResult {
  ok?: Ok5;
  session_id: SessionId105;
  title: Title17;
  message?: Message3;
  open_in_ui?: OpenInUi2;
}
export interface ChatSendFromVoiceResult {
  ok?: Ok6;
  session_id: SessionId106;
  title?: Title18;
  request_id?: RequestId19;
  status?: Status6;
}
export interface ChatRenameResult {
  ok?: Ok7;
  session_id: SessionId107;
  title: Title19;
}
export interface ChatDeleteResult {
  ok?: Ok8;
  session_id: SessionId108;
  deleted?: Deleted1;
  requires_confirmation?: RequiresConfirmation;
  episodes_removed?: EpisodesRemoved;
  message?: Message4;
}
export interface MemoryAddFactResult {
  ok?: Ok9;
  fact: MemoryFactItem;
  section: Section2;
}
export interface MemoryDeleteFactResult {
  ok?: Ok10;
  removed_count?: RemovedCount2;
  removed_ids?: RemovedIds2;
}
export interface ActivityListResult {
  tasks?: Tasks;
}
export interface ActivityCancelResult {
  ok?: Ok11;
  task_id: TaskId3;
  status: Status7;
  message?: Message5;
}
export interface OsOpenOrFocusAppResult {
  ok?: Ok12;
  status: Status8;
  app: App;
  window_title?: WindowTitle1;
  hwnd?: Hwnd1;
  executable?: Executable;
  target_path?: TargetPath1;
}
export interface OsControlMediaVolumeResult {
  ok?: Ok13;
  action: Action3;
  level?: Level2;
}
export interface OsReadClipboardResult {
  ok?: Ok14;
  text?: Text12;
  truncated?: Truncated1;
  char_count?: CharCount;
  total_chars?: TotalChars;
  history_enabled?: HistoryEnabled;
  total_items?: TotalItems;
  items?: Items;
}
export interface ClipboardItem {
  index?: Index1;
  id?: Id59;
  timestamp?: Timestamp7;
  kind?: Kind2;
  text?: Text13;
  truncated?: Truncated2;
  char_count?: CharCount1;
  total_chars?: TotalChars1;
  width?: Width;
  height?: Height;
  format?: Format;
  image_bytes_base64?: ImageBytesBase64;
  size_bytes?: SizeBytes;
  image?: Image;
}
export interface OsSelectClipboardItemResult {
  ok?: Ok15;
  success?: Success;
  status?: Status9;
  index?: Index2;
  item_id?: ItemId1;
  message?: Message6;
}
export interface OsOpenUrlResult {
  ok?: Ok16;
  success?: Success1;
  status?: Status10;
  url: Url1;
  browser?: Browser1;
  message?: Message7;
}
export interface OsControlSystemResult {
  ok?: Ok17;
  success?: Success2;
  action?: Action4;
  status?: Status11;
  level?: Level3;
  current_brightness?: CurrentBrightness;
  window_title?: WindowTitle2;
  hwnd?: Hwnd2;
  page?: Page1;
  uri?: Uri1;
  message?: Message8;
}
export interface OsWriteClipboardResult {
  ok?: Ok18;
  char_count?: CharCount2;
}
export interface WorkspaceGitInspectResult {
  ok?: Ok19;
  repo_path: RepoPath1;
  branch: Branch;
  is_clean?: IsClean;
  status?: Status12;
  staged?: Staged;
  unstaged?: Unstaged;
  untracked?: Untracked;
  worktrees?: Worktrees;
  recent_commits?: RecentCommits;
  diff?: Diff;
  diff_truncated?: DiffTruncated;
}
export interface GitWorktreeItem {
  path: Path;
  head?: Head;
  branch?: Branch1;
}
export interface GitCommitItem {
  sha: Sha;
  subject: Subject;
  oneline: Oneline;
}
