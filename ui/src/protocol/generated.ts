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
  | MemorySearchEpisodicRequest
  | MemoryManageFactRequest
  | MemoryListFactsRequest
  | ActivityListRequest
  | ActivityCancelRequest
  | OsOpenOrFocusAppRequest
  | OsControlMediaVolumeRequest
  | OsReadClipboardRequest
  | OsSelectClipboardItemRequest
  | OsOpenUrlRequest
  | OsControlSystemRequest
  | OsWriteClipboardRequest
  | OsTransformClipboardRequest
  | OsSaveClipboardToFileRequest
  | OsOpenPathOrUrlRequest
  | OsInspectScreenRequest
  | OsInspectUiElementsRequest
  | OsClickUiElementRequest
  | OsGuiActionRequest
  | OsUiSnapshotRequest
  | OsUiActRequest
  | OsUiBatchRequest
  | OsCancelInflightRequest
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
export type DataBase64 = string;
export type MimeType = string;
export type Name = string | null;
export type Width = number | null;
export type Height = number | null;
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
export type Method45 = "memory.search_episodic";
export type Query3 = string;
export type Limit6 = number;
export type Surface = ("chat" | "voice") | null;
export type FrameType46 = "request";
export type Id47 = string;
export type SessionId56 = string | null;
export type Method46 = "memory.manage_fact";
export type Action = "add" | "update" | "delete";
export type FactText = string | null;
export type Category1 = string | null;
export type FactKey = string | null;
export type FactId = string | null;
export type Query4 = string | null;
export type Confidence = number;
export type SourceSurface = "chat" | "voice";
export type SourceSessionId = string;
export type FrameType47 = "request";
export type Id48 = string;
export type SessionId57 = string | null;
export type Method47 = "memory.list_facts";
export type Status = ("active" | "superseded" | "deleted") | null;
export type Category2 = string | null;
export type Limit7 = number;
export type FrameType48 = "request";
export type Id49 = string;
export type SessionId58 = string | null;
export type Method48 = "activity.list";
export type Limit8 = number;
export type FrameType49 = "request";
export type Id50 = string;
export type SessionId59 = string | null;
export type Method49 = "activity.cancel";
export type TaskId = string;
export type FrameType50 = "request";
export type Id51 = string;
export type SessionId60 = string | null;
export type Method50 = "os.open_or_focus_app";
export type AppName = string;
export type TargetPath = string | null;
export type FrameType51 = "request";
export type Id52 = string;
export type SessionId61 = string | null;
export type Method51 = "os.control_media_volume";
export type Action1 =
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
export type FrameType52 = "request";
export type Id53 = string;
export type SessionId62 = string | null;
export type Method52 = "os.read_clipboard";
export type MaxChars = number;
export type IncludeHistory = boolean;
export type MaxItems = number;
export type IncludeImageBytes = boolean;
export type FrameType53 = "request";
export type Id54 = string;
export type SessionId63 = string | null;
export type Method53 = "os.select_clipboard_item";
/**
 * Zero-based index of the clipboard history item to restore
 */
export type Index = number;
export type FrameType54 = "request";
export type Id55 = string;
export type SessionId64 = string | null;
export type Method54 = "os.open_url";
/**
 * URL to open (http or https)
 */
export type Url = string;
/**
 * Optional browser executable or name
 */
export type Browser = string | null;
/**
 * Set true only when the user explicitly requests opening in a web browser
 */
export type PreferBrowser = boolean;
/**
 * Optional user request text for explicit-browser detection
 */
export type UserRequest = string | null;
export type FrameType55 = "request";
export type Id56 = string;
export type SessionId65 = string | null;
export type Method55 = "os.control_system";
/**
 * System control action (brightness, window, lock, settings)
 */
export type Action2 = string;
/**
 * Display brightness level (0-100)
 */
export type Level1 = number | null;
/**
 * Display brightness relative step
 */
export type Step = number | null;
/**
 * Window action: minimize, maximize, restore, close, snap_left, snap_right
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
 * Window title or app substring alias
 */
export type TitleQuery = string | null;
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
export type FrameType56 = "request";
export type Id57 = string;
export type SessionId66 = string | null;
export type Method56 = "os.write_clipboard";
export type Text1 = string;
export type FrameType57 = "request";
export type Id58 = string;
export type SessionId67 = string | null;
export type Method57 = "os.transform_clipboard";
export type Action3 = "format_json" | "minify_json" | "clean_text" | "unwrap_text" | "strip_tracking_urls";
export type UpdateClipboard = boolean;
export type FrameType58 = "request";
export type Id59 = string;
export type SessionId68 = string | null;
export type Method58 = "os.save_clipboard_to_file";
export type Filename = string | null;
export type Directory = "desktop" | "documents";
export type FrameType59 = "request";
export type Id60 = string;
export type SessionId69 = string | null;
export type Method59 = "os.open_path_or_url";
export type Target = string | null;
export type UseClipboard = boolean;
export type FrameType60 = "request";
export type Id61 = string;
export type SessionId70 = string | null;
export type Method60 = "os.inspect_screen";
export type Target1 = string;
export type Question = string;
export type IncludeImage = boolean;
export type FrameType61 = "request";
export type Id62 = string;
export type SessionId71 = string | null;
export type Method61 = "os.inspect_ui_elements";
export type AppName2 = string | null;
export type MaxElements = number;
export type FrameType62 = "request";
export type Id63 = string;
export type SessionId72 = string | null;
export type Method62 = "os.click_ui_element";
export type Name1 = string;
export type ControlType = string | null;
export type AppName3 = string | null;
export type FrameType63 = "request";
export type Id64 = string;
export type SessionId73 = string | null;
export type Method63 = "os.gui_action";
export type Action4 =
  | "click_at"
  | "double_click"
  | "right_click"
  | "move"
  | "type_text"
  | "press_keys"
  | "scroll"
  | "wait"
  | "navigate_back"
  | "navigate_forward"
  | "click"
  | "type"
  | "press";
export type X = number | null;
export type Y = number | null;
export type Text2 = string | null;
export type Keys = string[] | null;
export type Clicks = number;
export type Button = "left" | "right" | "double";
export type Delta = number;
export type Seconds = number;
export type CoordSpace = ("normalized_1000" | "image" | "screen") | null;
export type ImageWidth = number | null;
export type ImageHeight = number | null;
export type ScreenWidth = number | null;
export type ScreenHeight = number | null;
export type VirtualLeft = number | null;
export type VirtualTop = number | null;
export type DpiScale = number | null;
export type Ref = string | null;
export type Key = string | null;
/**
 * Optional target application or window title for focus precondition enforcement.
 */
export type AppName4 = string | null;
/**
 * Optional explicit target HWND for focus precondition enforcement.
 */
export type ExpectedHwnd = number | null;
export type FrameType64 = "request";
export type Id65 = string;
export type SessionId74 = string | null;
export type Method64 = "os.ui_snapshot";
export type AppName5 = string | null;
export type MaxElements1 = number;
export type InteractiveOnly = boolean;
export type FrameType65 = "request";
export type Id66 = string;
export type SessionId75 = string | null;
export type Method65 = "os.ui_act";
export type Ref1 = string;
export type Action5 = "click" | "set_value" | "type" | "clear" | "double_click" | "right_click" | "press";
export type Value = string | null;
export type IntervalMs = number;
export type RefreshSnapshot = boolean;
export type FrameType66 = "request";
export type Id67 = string;
export type SessionId76 = string | null;
export type Method66 = "os.ui_batch";
export type Op = "act" | "press_keys" | "scroll" | "wait" | "click_at";
export type Ref2 = string | null;
export type Action6 = ("click" | "set_value" | "type" | "clear" | "double_click" | "right_click" | "press") | null;
export type Value1 = string | null;
export type Keys1 = string[] | null;
export type Delta1 = number | null;
export type Seconds1 = number | null;
export type X1 = number | null;
export type Y1 = number | null;
export type CoordSpace1 = ("normalized_1000" | "image" | "screen") | null;
export type ImageWidth1 = number | null;
export type ImageHeight1 = number | null;
export type ScreenWidth1 = number | null;
export type ScreenHeight1 = number | null;
export type VirtualLeft1 = number | null;
export type VirtualTop1 = number | null;
export type DpiScale1 = number | null;
export type Clicks1 = number | null;
export type Button1 = ("left" | "right" | "double") | null;
export type IntervalMs1 = number | null;
export type Steps = UiBatchStep[];
export type StopOnError = boolean;
export type StopOnWindowChange = boolean;
export type FrameType67 = "request";
export type Id68 = string;
export type SessionId77 = string | null;
export type Method67 = "os.cancel_inflight";
export type Reason1 = string;
export type FrameType68 = "request";
export type Id69 = string;
export type SessionId78 = string | null;
export type Method68 = "workspace.git_inspect";
export type RepoPath = string | null;
export type IncludeDiff = boolean;
export type DiffStatOnly = boolean;
export type FrameType69 = "response";
export type Id70 = string;
export type Ok = boolean;
export type SessionId79 = string | null;
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
  | MemoryUpdatedEvent
  | TimerFiredEvent
  | ReminderFiredEvent;
export type FrameType70 = "event";
export type RequestId = string | null;
export type SessionId80 = string | null;
export type Event1 = "assistant_token";
export type Text3 = string;
export type FrameType71 = "event";
export type RequestId1 = string | null;
export type SessionId81 = string | null;
export type Event2 = "tool_call_started";
export type CallId2 = string;
export type Name2 = string;
export type RiskLevel = "read_only" | "reversible" | "destructive";
export type FrameType72 = "event";
export type RequestId2 = string | null;
export type SessionId82 = string | null;
export type Event3 = "tool_call_finished";
export type CallId3 = string;
export type Name3 = string;
export type IsError = boolean;
export type Output = string;
/**
 * Execution time, excluding time spent awaiting approval
 */
export type DurationMs = number;
export type FrameType73 = "event";
export type RequestId3 = string | null;
export type SessionId83 = string | null;
export type Event4 = "approval_requested";
export type ApprovalId1 = string;
export type CallId4 = string | null;
export type Tool = string;
export type RiskLevel1 = "read_only" | "reversible" | "destructive";
export type TimeoutSeconds = number;
export type FrameType74 = "event";
export type RequestId4 = string | null;
export type SessionId84 = string | null;
export type Event5 = "approval_resolved";
export type ApprovalId2 = string;
export type Decision1 = "allow" | "deny";
export type Reason2 = "user" | "timeout";
export type FrameType75 = "event";
export type RequestId5 = string | null;
export type SessionId85 = string | null;
export type Event6 = "task_status";
export type TaskId1 = string;
export type Title4 = string;
export type Status1 = "queued" | "running" | "succeeded" | "failed" | "cancelled";
export type StartedAt = string;
export type Kind = "chat_turn" | "background_task" | "scheduled" | "agent_run";
export type FinishedAt = string | null;
export type NextRunAt = string | null;
export type Detail = string | null;
export type FrameType76 = "event";
export type RequestId6 = string | null;
export type SessionId86 = string | null;
export type Event7 = "voice_state";
export type State = "idle" | "listening" | "thinking" | "speaking" | "reconnecting" | "error";
export type Error1 = string | null;
export type FrameType77 = "event";
export type RequestId7 = string | null;
export type SessionId87 = string | null;
export type Event8 = "call_started";
export type CallId5 = string;
export type CallState = "idle" | "connecting" | "active" | "ending";
export type StartedAt1 = string | null;
export type FrameType78 = "event";
export type RequestId8 = string | null;
export type SessionId88 = string | null;
export type Event9 = "call_ended";
export type CallId6 = string;
export type CallState1 = "idle" | "connecting" | "active" | "ending";
export type Reason3 = string | null;
export type DurationSeconds = number | null;
export type FrameType79 = "event";
export type RequestId9 = string | null;
export type SessionId89 = string | null;
export type Event10 = "transcript";
export type Role = "user" | "assistant";
export type Text4 = string;
export type Final = boolean;
export type CallId7 = string | null;
export type Timestamp = string | null;
export type FrameType80 = "event";
export type RequestId10 = string | null;
export type SessionId90 = string | null;
export type Event11 = "screen_capture_state";
export type Active = boolean;
export type Target2 = ("active_window" | "full_screen") | null;
export type CallId8 = string | null;
export type FrameType81 = "event";
export type RequestId11 = string | null;
export type SessionId91 = string | null;
export type Event12 = "usage";
export type CallId9 = string | null;
export type InputTokens = number;
export type OutputTokens = number;
export type TotalTokens = number;
export type CachedTokens = number | null;
export type ThoughtTokens = number | null;
export type FrameType82 = "event";
export type RequestId12 = string | null;
export type SessionId92 = string | null;
export type Event13 = "error";
export type Message = string;
export type Code = string | null;
export type FrameType83 = "event";
export type RequestId13 = string | null;
export type SessionId93 = string | null;
export type Event14 = "conversation_updated";
export type SessionId94 = string;
export type Title5 = string | null;
export type Pinned1 = boolean | null;
export type UpdatedAt = string | null;
export type UserMessage = string | null;
export type AssistantMessage = string | null;
export type Turns = number | null;
export type RequestId14 = string | null;
export type FrameType84 = "event";
export type RequestId15 = string | null;
export type SessionId95 = string | null;
export type Event15 = "conversation_deleted";
export type SessionIds = string[];
export type All = boolean;
export type FrameType85 = "event";
export type RequestId16 = string | null;
export type SessionId96 = string | null;
export type Event16 = "ui.navigate";
export type View1 = "chat" | "voice" | "activity" | "memory" | "settings";
export type ConversationId5 = string | null;
export type OpenCommandPalette1 = boolean;
export type FrameType86 = "event";
export type RequestId17 = string | null;
export type SessionId97 = string | null;
export type Event17 = "conversation_created";
export type SessionId98 = string;
export type Title6 = string;
export type StartedAt2 = string;
export type UpdatedAt1 = string;
export type Turns1 = number;
export type InitialMessage = string | null;
export type OpenInUi1 = boolean;
export type FrameType87 = "event";
export type RequestId18 = string | null;
export type SessionId99 = string | null;
export type Event18 = "memory.updated";
export type Action7 = "added" | "deleted" | "updated";
export type FactId1 = string | null;
export type Section = string | null;
export type Text5 = string | null;
export type RemovedCount = number;
export type RemovedIds = string[];
export type FrameType88 = "event";
export type RequestId19 = string | null;
export type SessionId100 = string | null;
export type Event19 = "timer.fired";
export type TimerId = string;
export type Label1 = string;
export type DurationSeconds1 = number;
export type FiresAt = string;
export type FiredAt = string;
export type FrameType89 = "event";
export type RequestId20 = string | null;
export type SessionId101 = string | null;
export type Event20 = "reminder.fired";
export type ReminderId = string;
export type Message1 = string;
export type DueAt = string;
export type FiredAt1 = string;
export type Status2 = "ok";
export type SessionId102 = string;
export type Text6 = string;
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
export type SessionId103 = string;
export type Time = string;
export type Summary = string;
export type Trivial = boolean;
export type Episodes = EpisodeItem[];
export type Name4 = string;
export type Id71 = string;
export type Date4 = string;
export type Source = string;
export type Text7 = string;
export type Entries = MemoryFactItem[];
export type Sections = MemorySectionItem[];
export type LastConsolidated1 = string | null;
export type FailedConsolidation1 = string | null;
export type Id72 = string;
export type Date5 = string;
export type Source1 = string;
export type Text8 = string;
export type ArchivedAt = string | null;
export type Reason4 = string | null;
export type Entries1 = MemoryArchiveItem[];
export type Raw = string;
export type LastConsolidatedAt = string | null;
export type SessionId104 = string;
export type Status3 = string;
export type RetryCount = number;
export type Error2 = string | null;
export type LastAttemptAt = string | null;
export type NextRetryAt = string | null;
export type Pending = ConsolidationSessionStatus[];
export type Failed = ConsolidationSessionStatus[];
export type CompletedCount = number;
export type SessionId105 = string;
export type Ok1 = boolean;
export type Status4 = string;
export type Error3 = string | null;
export type Ok2 = boolean;
export type RemovedCount1 = number;
export type RemovedIds1 = string[];
export type SessionId106 = string;
export type StartedAt3 = string;
export type Title7 = string;
export type MessageCount = number;
export type Transcripts = TranscriptSummaryItem[];
export type SessionId107 = string;
export type StartedAt4 = string;
export type Title8 = string;
export type Role1 = string;
export type Text9 = string;
export type Timestamp1 = string;
export type ToolCalls = {
  [k: string]: unknown | undefined;
}[];
export type Messages = TranscriptMessageItem[];
export type Status5 = "ok";
export type Date6 = string;
export type SessionId108 = string;
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
export type SessionId109 = string;
export type Time1 = string;
export type User = string;
export type Assistant = string;
export type Turns3 = ConversationTurn[];
export type SessionId110 = string;
export type Title10 = string;
export type Pinned3 = boolean;
export type Role2 = "user" | "assistant";
export type Text10 = string;
export type Timestamp2 = string;
export type ToolCalls1 = {
  [k: string]: unknown | undefined;
}[];
export type Messages1 = ConversationResumeMessage[];
export type SessionId111 = string;
export type Title11 = string;
export type SessionId112 = string;
export type Pinned4 = boolean;
export type SessionId113 = string;
export type Title12 = string;
export type Filename1 = string;
export type Content3 = string;
export type SessionId114 = string;
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
export type Name5 = string;
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
export type Status6 = "queued" | "running" | "succeeded" | "failed" | "cancelled";
export type Output1 = string;
export type SessionId115 = string;
export type Role3 = "user" | "assistant";
export type Text11 = string;
export type Timestamp3 = string;
export type CallId13 = string | null;
export type Messages2 = VoiceHistoryMessageItem[];
export type ConversationId7 = string;
export type SessionId116 = string;
export type Title13 = string;
export type UpdatedAt3 = string;
export type TurnCount1 = number;
export type Turns4 = number;
export type EpisodeSummary1 = string;
export type Date8 = string;
export type LastTime1 = string;
export type Pinned5 = boolean;
export type Conversations1 = VoiceConversationListItem[];
export type SessionId117 = string;
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
export type SessionId118 = string;
export type Title15 = string;
export type UpdatedAt5 = string;
export type EpisodeSummary3 = string;
export type TotalTurns = number;
export type Truncated = boolean;
export type Role6 = string;
export type Content4 = string;
export type Timestamp6 = string;
export type Turns5 = VoiceConversationTurnItem[];
export type Message2 = string;
export type Kind1 = "fact" | "episode";
export type ItemId = string;
export type Section1 = string;
export type Date9 = string;
export type Source3 = string;
export type SessionId119 = string;
export type ConversationId10 = string;
export type Text12 = string;
export type Score1 = number;
export type SemanticScore1 = number | null;
export type LexicalScore1 = number;
export type Results1 = MemorySearchHit[];
export type Ok3 = boolean;
export type SessionId120 = string | null;
export type Title16 = string | null;
export type Message3 = string;
export type Ok4 = boolean;
export type View2 = "chat" | "voice" | "activity" | "memory" | "settings";
export type ActiveConversationId1 = string | null;
export type OpenCommandPalette2 = boolean;
export type Ok5 = boolean;
export type SessionId121 = string;
export type Title17 = string;
export type Message4 = string;
export type OpenInUi2 = boolean;
export type Ok6 = boolean;
export type SessionId122 = string;
export type Title18 = string;
export type RequestId21 = string;
export type Status7 = string;
export type Ok7 = boolean;
export type SessionId123 = string;
export type Title19 = string;
export type Ok8 = boolean;
export type SessionId124 = string;
export type Deleted1 = boolean;
export type RequiresConfirmation = boolean;
export type EpisodesRemoved = number;
export type Message5 = string;
export type Ok9 = boolean;
export type Section2 = string;
export type Ok10 = boolean;
export type RemovedCount2 = number;
export type RemovedIds2 = string[];
export type SessionId125 = string;
export type Surface1 = "chat" | "voice";
export type Timestamp7 = string;
export type Title20 = string;
export type Summary1 = string;
export type Topics = string;
export type Snippet2 = string;
export type Score2 = number;
export type Results2 = EpisodicSessionHit[];
export type Ok11 = boolean;
export type Operation = "add" | "update" | "delete" | "noop";
export type Id73 = string;
export type Category3 = "preference" | "environment" | "project" | "workflow" | "identity" | "constraint";
export type FactKey1 = string;
export type FactText1 = string;
export type Confidence1 = number;
export type SourceSurface1 = "chat" | "voice";
export type SourceSessionId1 = string;
export type Status8 = "active" | "superseded" | "deleted";
export type CreatedAt = string;
export type UpdatedAt6 = string;
export type SupersededId = string | null;
export type AffectedIds = string[];
export type Message6 = string;
export type Facts = MemoryBankFactItem[];
export type Total = number;
export type Tasks = TaskStatusData[];
export type Ok12 = boolean;
export type TaskId3 = string;
export type Status9 = string;
export type Message7 = string;
export type Ok13 = boolean;
export type Status10 = "focused" | "launched" | "non_interactive_desktop";
export type App = string;
export type WindowTitle1 = string | null;
export type Hwnd1 = number | null;
export type Executable = string | null;
export type TargetPath1 = string | null;
export type Error4 = string | null;
export type Verified = boolean;
export type Status11 = string;
export type OutcomeKind = ("verified" | "unavailable" | "failed") | null;
export type Reason5 = string;
export type ElapsedMs = number;
export type Ok14 = boolean;
export type Action8 =
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
export type Ok15 = boolean;
export type Kind2 = string;
export type Text13 = string;
export type Truncated1 = boolean;
export type CharCount = number;
export type TotalChars = number;
export type Image = {
  [k: string]: unknown | undefined;
} | null;
export type FilePaths = string[];
export type HasImageFiles = boolean;
export type HistoryEnabled = boolean;
export type TotalItems = number;
export type Index1 = number;
export type Id74 = string;
export type Timestamp8 = string;
export type Kind3 = string;
export type Text14 = string | null;
export type Truncated2 = boolean;
export type CharCount1 = number;
export type TotalChars1 = number;
export type Width1 = number | null;
export type Height1 = number | null;
export type Format = string | null;
export type ImageBytesBase64 = string | null;
export type SizeBytes = number | null;
export type Image1 = {
  [k: string]: unknown | undefined;
} | null;
export type Items = ClipboardItem[];
export type Ok16 = boolean;
export type Success = boolean;
export type Status12 = string;
export type Index2 = number;
export type ItemId1 = string | null;
export type Message8 = string | null;
export type Ok17 = boolean;
export type Success1 = boolean;
export type Status13 = string;
export type Url1 = string;
export type Title21 = string | null;
export type Browser1 = string | null;
export type Message9 = string | null;
export type RoutedToInstalledApp = boolean;
export type AppName6 = string | null;
export type WindowHwnd = number | null;
export type WindowTitle2 = string | null;
export type ResolutionLayer = string | null;
export type Guidance = string | null;
export type ReusedBrowserWindow = boolean;
export type TargetId = string | null;
export type IsPwa = boolean | null;
export type CdpUsed = boolean;
export type BeforeUrl = string | null;
export type AfterUrl = string | null;
export type StateDiff = {
  [k: string]: unknown | undefined;
} | null;
export type Ok18 = boolean;
export type Success2 = boolean;
export type Action9 = string;
export type Status14 = string;
export type Level3 = number | null;
export type CurrentBrightness = number | null;
export type WindowTitle3 = string | null;
export type Hwnd2 = number | null;
export type Page1 = string | null;
export type Uri1 = string | null;
export type Message10 = string | null;
export type Rect = {
  [k: string]: number | undefined;
} | null;
export type Status15 = string;
export type Reason6 = string;
export type StepLabel = string;
export type ExpectedHwnd1 = number | null;
export type ExpectedTitle = string;
export type ExpectedProcess = string;
export type ActualHwnd = number | null;
export type ActualTitle = string;
export type ActualProcess = string;
export type Message11 = string;
export type Ok19 = boolean;
export type CharCount2 = number;
export type Ok20 = boolean;
export type Status16 = string;
export type Action10 = string;
export type UpdatedClipboard = boolean;
export type Preview = string;
export type Error5 = string | null;
export type Ok21 = boolean;
export type Status17 = string;
export type SavedPath = string;
export type Path = string;
export type Filename2 = string;
export type SizeBytes1 = number;
export type Kind4 = string;
export type Error6 = string | null;
export type Ok22 = boolean;
export type Status18 = "opened" | "error" | "non_interactive_desktop";
export type Kind5 = ("file" | "url") | null;
export type Target3 = string;
export type Filename3 = string | null;
export type Error7 = string | null;
export type Ok23 = boolean;
export type Status19 = "ok" | "minimized" | "not_found" | "empty" | "error" | "non_interactive_desktop";
export type Source4 = string | null;
export type WindowTitle4 = string | null;
export type ProcessName = string | null;
export type Question1 = string | null;
export type Width2 = number | null;
export type Height2 = number | null;
export type ImageWidth2 = number | null;
export type ImageHeight2 = number | null;
export type ScreenWidth2 = number | null;
export type ScreenHeight2 = number | null;
export type VirtualLeft2 = number | null;
export type VirtualTop2 = number | null;
export type DpiScale2 = number | null;
export type CoordSpace2 = string | null;
export type ArtifactPath = string | null;
export type UiaText = string | null;
export type Text15 = string | null;
export type Message12 = string | null;
export type ImageBase64 = string | null;
export type ImageMimeType = string | null;
export type Ok24 = boolean;
export type Status20 = string | null;
export type Source5 = string | null;
export type CdpAvailable = boolean | null;
export type AccessibilityCoverage = ("chrome_only" | "sparse" | "rich") | null;
export type Hint = string | null;
export type Message13 = string | null;
export type Error8 = string | null;
export type WindowTitle5 = string;
export type Count = number;
export type Index3 = number;
export type Name6 = string;
export type AutomationId = string;
export type ControlType1 = string;
export type Value2 = string | null;
export type X2 = number;
export type Y2 = number;
export type Elements = UiElementItem[];
export type Ok25 = boolean;
export type Status21 = string;
export type Name7 = string;
export type ControlType2 = string;
export type X3 = number;
export type Y3 = number;
export type Method69 = string;
export type CdpAvailable1 = boolean | null;
export type Hint1 = string | null;
export type Message14 = string | null;
export type StateDiff1 = {
  [k: string]: unknown | undefined;
} | null;
export type Reason7 = string | null;
export type Error9 = string | null;
export type Ok26 = boolean;
export type Status22 = string;
export type Source6 = string | null;
export type Action11 = string;
export type Method70 = string | null;
export type Direction = string | null;
export type X4 = number | null;
export type Y4 = number | null;
export type PhysicalX = number | null;
export type PhysicalY = number | null;
export type CoordSpace3 = string | null;
export type Clicks2 = number | null;
export type Button2 = string | null;
export type CharCount3 = number | null;
export type Keys2 = string[] | null;
export type Delta2 = number | null;
export type Seconds2 = number | null;
export type Ref3 = string | null;
export type Url2 = string | null;
export type Title22 = string | null;
export type BeforeUrl1 = string | null;
export type AfterUrl1 = string | null;
export type BeforeTitle = string | null;
export type AfterTitle = string | null;
export type TargetId1 = string | null;
export type IsPwa1 = boolean | null;
export type CdpAvailable2 = boolean | null;
export type Hint2 = string | null;
export type Message15 = string | null;
export type StateDiff2 = {
  [k: string]: unknown | undefined;
} | null;
export type CircuitBreakerTripped = boolean | null;
export type SuggestedRecovery = string[] | null;
export type EntryMethod = string | null;
export type ClipboardRestored = boolean | null;
export type Cancelled = boolean | null;
export type Reason8 = string | null;
export type Error10 = string | null;
export type Ok27 = boolean;
export type Status23 = string | null;
export type Source7 = string | null;
export type SnapshotId = string;
export type WindowTitle6 = string;
export type ProcessName1 = string | null;
export type Url3 = string | null;
export type Title23 = string | null;
export type TargetId2 = string | null;
export type IsPwa2 = boolean | null;
export type CdpAvailable3 = boolean | null;
export type AccessibilityCoverage1 = ("chrome_only" | "sparse" | "rich") | null;
export type ContentElementCount = number | null;
export type Hint3 = string | null;
export type Message16 = string | null;
export type Error11 = string | null;
export type Truncated3 = boolean | null;
export type Count1 = number;
export type ElementCount = number | null;
export type Ref4 = string;
export type Role7 = string;
export type Name8 = string;
export type Value3 = string | null;
export type X5 = number;
export type Y5 = number;
export type AutomationId1 = string;
export type ControlType3 = string;
export type Enabled = boolean;
export type Focused = boolean;
export type Elements1 = UiSnapshotElement[];
export type Ok28 = boolean;
export type Status24 = string;
export type Source8 = string | null;
export type SnapshotId1 = string;
export type Action12 = string;
export type Ref5 = string;
export type Role8 = string;
export type Name9 = string;
export type Method71 = string;
export type X6 = number | null;
export type Y6 = number | null;
export type CharCount4 = number | null;
export type IntervalMs2 = number | null;
export type Url4 = string | null;
export type Title24 = string | null;
export type BeforeUrl2 = string | null;
export type AfterUrl2 = string | null;
export type BeforeTitle1 = string | null;
export type AfterTitle1 = string | null;
export type TargetId3 = string | null;
export type IsPwa3 = boolean | null;
export type CdpAvailable4 = boolean | null;
export type Hint4 = string | null;
export type Message17 = string | null;
export type StateDiff3 = {
  [k: string]: unknown | undefined;
} | null;
export type WindowChanged = boolean;
export type WindowTitle7 = string;
export type Ref6 = string;
export type Role9 = string;
export type Name10 = string;
export type OldValue = string | null;
export type NewValue = string | null;
export type ChangedValues = UiDiffChangedValue[];
export type AddedElements = UiSnapshotElement[];
export type RemovedCount3 = number;
export type ActiveWindowTitle = string | null;
export type ActiveHwnd = number | null;
export type TargetState = {
  [k: string]: unknown | undefined;
} | null;
export type SnapshotRefreshed = boolean | null;
export type StaleRef = boolean | null;
export type Reason9 = string | null;
export type Error12 = string | null;
export type Ok29 = boolean;
export type Status25 =
  "completed" | "stopped_on_window_change" | "partial_error" | "aborted_precondition" | "cancelled";
export type StepsExecuted = number;
export type StepResults = {
  [k: string]: unknown | undefined;
}[];
export type StateDiff4 = {
  [k: string]: unknown | undefined;
} | null;
export type Cancelled1 = boolean | null;
export type Reason10 = string | null;
export type Error13 = string | null;
export type Ok30 = boolean;
export type Cancelled2 = boolean;
export type CancelledCount = number;
export type Ok31 = boolean;
export type RepoPath1 = string;
export type Branch = string;
export type IsClean = boolean;
export type Status26 = string;
export type Staged = string[];
export type Unstaged = string[];
export type Untracked = string[];
export type Path1 = string;
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
export type FactCategory = "preference" | "environment" | "project" | "workflow" | "identity" | "constraint";
export type FactStatus = "active" | "superseded" | "deleted";
export type FactOperation = "add" | "update" | "delete" | "noop";
export type UiActAction = "click" | "set_value" | "type" | "clear" | "double_click" | "right_click" | "press";
export type UiBatchOp = "act" | "press_keys" | "scroll" | "wait" | "click_at";
export type AccessibilityCoverage2 = "chrome_only" | "sparse" | "rich";

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
  fact_category: FactCategory;
  fact_status: FactStatus;
  fact_operation: FactOperation;
  health_ping_request: HealthPingRequest;
  session_new_request: SessionNewRequest;
  chat_send_request: ChatSendRequest;
  chat_image_attachment: ChatImageAttachment;
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
  memory_search_episodic_request: MemorySearchEpisodicRequest;
  memory_manage_fact_request: MemoryManageFactRequest;
  memory_list_facts_request: MemoryListFactsRequest;
  episodic_session_hit: EpisodicSessionHit;
  memory_bank_fact_item: MemoryBankFactItem;
  activity_list_request: ActivityListRequest;
  activity_cancel_request: ActivityCancelRequest;
  os_open_or_focus_app_request: OsOpenOrFocusAppRequest;
  os_control_media_volume_request: OsControlMediaVolumeRequest;
  os_read_clipboard_request: OsReadClipboardRequest;
  os_select_clipboard_item_request: OsSelectClipboardItemRequest;
  os_open_url_request: OsOpenUrlRequest;
  os_control_system_request: OsControlSystemRequest;
  os_write_clipboard_request: OsWriteClipboardRequest;
  os_transform_clipboard_request: OsTransformClipboardRequest;
  os_save_clipboard_to_file_request: OsSaveClipboardToFileRequest;
  os_open_path_or_url_request: OsOpenPathOrUrlRequest;
  os_inspect_screen_request: OsInspectScreenRequest;
  os_inspect_ui_elements_request: OsInspectUiElementsRequest;
  os_click_ui_element_request: OsClickUiElementRequest;
  os_gui_action_request: OsGuiActionRequest;
  os_ui_snapshot_request: OsUiSnapshotRequest;
  os_ui_act_request: OsUiActRequest;
  os_ui_batch_request: OsUiBatchRequest;
  os_cancel_inflight_request: OsCancelInflightRequest;
  ui_snapshot_element: UiSnapshotElement;
  ui_diff: UiDiff;
  ui_diff_changed_value: UiDiffChangedValue;
  ui_batch_step: UiBatchStep;
  ui_act_action: UiActAction;
  ui_batch_op: UiBatchOp;
  accessibility_coverage: AccessibilityCoverage2;
  clipboard_item: ClipboardItem;
  ui_element_item: UiElementItem;
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
  timer_fired_event: TimerFiredEvent;
  timer_fired_data: TimerFiredData;
  reminder_fired_event: ReminderFiredEvent;
  reminder_fired_data: ReminderFiredData;
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
  text?: Text;
  image?: ChatImageAttachment | null;
}
export interface ChatImageAttachment {
  data_base64: DataBase64;
  mime_type?: MimeType;
  name?: Name;
  width?: Width;
  height?: Height;
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
export interface MemorySearchEpisodicRequest {
  frame_type?: FrameType45;
  id?: Id46;
  session_id?: SessionId55;
  method?: Method45;
  params: MemorySearchEpisodicParams;
}
export interface MemorySearchEpisodicParams {
  query: Query3;
  limit?: Limit6;
  surface?: Surface;
}
export interface MemoryManageFactRequest {
  frame_type?: FrameType46;
  id?: Id47;
  session_id?: SessionId56;
  method?: Method46;
  params: MemoryManageFactParams;
}
export interface MemoryManageFactParams {
  action: Action;
  fact_text?: FactText;
  category?: Category1;
  fact_key?: FactKey;
  fact_id?: FactId;
  query?: Query4;
  confidence?: Confidence;
  source_surface?: SourceSurface;
  source_session_id?: SourceSessionId;
}
export interface MemoryListFactsRequest {
  frame_type?: FrameType47;
  id?: Id48;
  session_id?: SessionId57;
  method?: Method47;
  params?: MemoryListFactsParams;
}
export interface MemoryListFactsParams {
  status?: Status;
  category?: Category2;
  limit?: Limit7;
}
export interface ActivityListRequest {
  frame_type?: FrameType48;
  id?: Id49;
  session_id?: SessionId58;
  method?: Method48;
  params?: ActivityListParams;
}
export interface ActivityListParams {
  limit?: Limit8;
}
export interface ActivityCancelRequest {
  frame_type?: FrameType49;
  id?: Id50;
  session_id?: SessionId59;
  method?: Method49;
  params: ActivityCancelParams;
}
export interface ActivityCancelParams {
  task_id: TaskId;
}
export interface OsOpenOrFocusAppRequest {
  frame_type?: FrameType50;
  id?: Id51;
  session_id?: SessionId60;
  method?: Method50;
  params: OsOpenOrFocusAppParams;
}
export interface OsOpenOrFocusAppParams {
  app_name: AppName;
  target_path?: TargetPath;
}
export interface OsControlMediaVolumeRequest {
  frame_type?: FrameType51;
  id?: Id52;
  session_id?: SessionId61;
  method?: Method51;
  params: OsControlMediaVolumeParams;
}
export interface OsControlMediaVolumeParams {
  action: Action1;
  level?: Level;
}
export interface OsReadClipboardRequest {
  frame_type?: FrameType52;
  id?: Id53;
  session_id?: SessionId62;
  method?: Method52;
  params?: OsReadClipboardParams;
}
export interface OsReadClipboardParams {
  max_chars?: MaxChars;
  include_history?: IncludeHistory;
  max_items?: MaxItems;
  include_image_bytes?: IncludeImageBytes;
}
export interface OsSelectClipboardItemRequest {
  frame_type?: FrameType53;
  id?: Id54;
  session_id?: SessionId63;
  method?: Method53;
  params: OsSelectClipboardItemParams;
}
export interface OsSelectClipboardItemParams {
  index: Index;
}
export interface OsOpenUrlRequest {
  frame_type?: FrameType54;
  id?: Id55;
  session_id?: SessionId64;
  method?: Method54;
  params: OsOpenUrlParams;
}
export interface OsOpenUrlParams {
  url: Url;
  browser?: Browser;
  prefer_browser?: PreferBrowser;
  user_request?: UserRequest;
}
export interface OsControlSystemRequest {
  frame_type?: FrameType55;
  id?: Id56;
  session_id?: SessionId65;
  method?: Method55;
  params: OsControlSystemParams;
}
export interface OsControlSystemParams {
  action: Action2;
  level?: Level1;
  step?: Step;
  window_action?: WindowAction;
  window_title?: WindowTitle;
  title?: Title3;
  title_query?: TitleQuery;
  hwnd?: Hwnd;
  app_name?: AppName1;
  page?: Page;
  uri?: Uri;
}
export interface OsWriteClipboardRequest {
  frame_type?: FrameType56;
  id?: Id57;
  session_id?: SessionId66;
  method?: Method56;
  params: OsWriteClipboardParams;
}
export interface OsWriteClipboardParams {
  text: Text1;
}
export interface OsTransformClipboardRequest {
  frame_type?: FrameType57;
  id?: Id58;
  session_id?: SessionId67;
  method?: Method57;
  params: OsTransformClipboardParams;
}
export interface OsTransformClipboardParams {
  action: Action3;
  update_clipboard?: UpdateClipboard;
}
export interface OsSaveClipboardToFileRequest {
  frame_type?: FrameType58;
  id?: Id59;
  session_id?: SessionId68;
  method?: Method58;
  params?: OsSaveClipboardToFileParams;
}
export interface OsSaveClipboardToFileParams {
  filename?: Filename;
  directory?: Directory;
}
export interface OsOpenPathOrUrlRequest {
  frame_type?: FrameType59;
  id?: Id60;
  session_id?: SessionId69;
  method?: Method59;
  params?: OsOpenPathOrUrlParams;
}
export interface OsOpenPathOrUrlParams {
  target?: Target;
  use_clipboard?: UseClipboard;
}
export interface OsInspectScreenRequest {
  frame_type?: FrameType60;
  id?: Id61;
  session_id?: SessionId70;
  method?: Method60;
  params?: OsInspectScreenParams;
}
export interface OsInspectScreenParams {
  target?: Target1;
  question?: Question;
  include_image?: IncludeImage;
}
export interface OsInspectUiElementsRequest {
  frame_type?: FrameType61;
  id?: Id62;
  session_id?: SessionId71;
  method?: Method61;
  params?: OsInspectUiElementsParams;
}
export interface OsInspectUiElementsParams {
  app_name?: AppName2;
  max_elements?: MaxElements;
}
export interface OsClickUiElementRequest {
  frame_type?: FrameType62;
  id?: Id63;
  session_id?: SessionId72;
  method?: Method62;
  params: OsClickUiElementParams;
}
export interface OsClickUiElementParams {
  name: Name1;
  control_type?: ControlType;
  app_name?: AppName3;
}
export interface OsGuiActionRequest {
  frame_type?: FrameType63;
  id?: Id64;
  session_id?: SessionId73;
  method?: Method63;
  params: OsGuiActionParams;
}
export interface OsGuiActionParams {
  action: Action4;
  x?: X;
  y?: Y;
  text?: Text2;
  keys?: Keys;
  clicks?: Clicks;
  button?: Button;
  delta?: Delta;
  seconds?: Seconds;
  coord_space?: CoordSpace;
  image_width?: ImageWidth;
  image_height?: ImageHeight;
  screen_width?: ScreenWidth;
  screen_height?: ScreenHeight;
  virtual_left?: VirtualLeft;
  virtual_top?: VirtualTop;
  dpi_scale?: DpiScale;
  ref?: Ref;
  key?: Key;
  app_name?: AppName4;
  expected_hwnd?: ExpectedHwnd;
}
export interface OsUiSnapshotRequest {
  frame_type?: FrameType64;
  id?: Id65;
  session_id?: SessionId74;
  method?: Method64;
  params?: OsUiSnapshotParams;
}
export interface OsUiSnapshotParams {
  app_name?: AppName5;
  max_elements?: MaxElements1;
  interactive_only?: InteractiveOnly;
}
export interface OsUiActRequest {
  frame_type?: FrameType65;
  id?: Id66;
  session_id?: SessionId75;
  method?: Method65;
  params: OsUiActParams;
}
export interface OsUiActParams {
  ref: Ref1;
  action: Action5;
  value?: Value;
  interval_ms?: IntervalMs;
  refresh_snapshot?: RefreshSnapshot;
}
export interface OsUiBatchRequest {
  frame_type?: FrameType66;
  id?: Id67;
  session_id?: SessionId76;
  method?: Method66;
  params: OsUiBatchParams;
}
export interface OsUiBatchParams {
  steps?: Steps;
  stop_on_error?: StopOnError;
  stop_on_window_change?: StopOnWindowChange;
}
export interface UiBatchStep {
  op: Op;
  ref?: Ref2;
  action?: Action6;
  value?: Value1;
  keys?: Keys1;
  delta?: Delta1;
  seconds?: Seconds1;
  x?: X1;
  y?: Y1;
  coord_space?: CoordSpace1;
  image_width?: ImageWidth1;
  image_height?: ImageHeight1;
  screen_width?: ScreenWidth1;
  screen_height?: ScreenHeight1;
  virtual_left?: VirtualLeft1;
  virtual_top?: VirtualTop1;
  dpi_scale?: DpiScale1;
  clicks?: Clicks1;
  button?: Button1;
  interval_ms?: IntervalMs1;
}
export interface OsCancelInflightRequest {
  frame_type?: FrameType67;
  id?: Id68;
  session_id?: SessionId77;
  method?: Method67;
  params?: OsCancelInflightParams;
}
export interface OsCancelInflightParams {
  reason?: Reason1;
}
export interface WorkspaceGitInspectRequest {
  frame_type?: FrameType68;
  id?: Id69;
  session_id?: SessionId78;
  method?: Method68;
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
  frame_type?: FrameType69;
  id: Id70;
  ok?: Ok;
  session_id?: SessionId79;
  result?: Result;
  error?: Error;
}
export interface Result {
  [k: string]: unknown | undefined;
}
export interface AssistantTokenEvent {
  frame_type?: FrameType70;
  request_id?: RequestId;
  session_id?: SessionId80;
  event?: Event1;
  data: AssistantTokenData;
}
export interface AssistantTokenData {
  text: Text3;
}
export interface ToolCallStartedEvent {
  frame_type?: FrameType71;
  request_id?: RequestId1;
  session_id?: SessionId81;
  event?: Event2;
  data: ToolCallStartedData;
}
export interface ToolCallStartedData {
  call_id: CallId2;
  name: Name2;
  arguments?: Arguments;
  risk_level: RiskLevel;
}
export interface Arguments {
  [k: string]: unknown | undefined;
}
export interface ToolCallFinishedEvent {
  frame_type?: FrameType72;
  request_id?: RequestId2;
  session_id?: SessionId82;
  event?: Event3;
  data: ToolCallFinishedData;
}
export interface ToolCallFinishedData {
  call_id: CallId3;
  name: Name3;
  is_error: IsError;
  output: Output;
  duration_ms: DurationMs;
}
export interface ApprovalRequestedEvent {
  frame_type?: FrameType73;
  request_id?: RequestId3;
  session_id?: SessionId83;
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
  frame_type?: FrameType74;
  request_id?: RequestId4;
  session_id?: SessionId84;
  event?: Event5;
  data: ApprovalResolvedData;
}
export interface ApprovalResolvedData {
  approval_id: ApprovalId2;
  decision: Decision1;
  reason: Reason2;
}
export interface TaskStatusEvent {
  frame_type?: FrameType75;
  request_id?: RequestId5;
  session_id?: SessionId85;
  event?: Event6;
  data: TaskStatusData;
}
export interface TaskStatusData {
  task_id: TaskId1;
  title: Title4;
  status: Status1;
  started_at: StartedAt;
  kind?: Kind;
  finished_at?: FinishedAt;
  next_run_at?: NextRunAt;
  detail?: Detail;
}
export interface VoiceStateEvent {
  frame_type?: FrameType76;
  request_id?: RequestId6;
  session_id?: SessionId86;
  event?: Event7;
  data: VoiceStateData;
}
export interface VoiceStateData {
  state: State;
  error?: Error1;
}
export interface CallStartedEvent {
  frame_type?: FrameType77;
  request_id?: RequestId7;
  session_id?: SessionId87;
  event?: Event8;
  data: CallStartedData;
}
export interface CallStartedData {
  call_id: CallId5;
  call_state?: CallState;
  started_at?: StartedAt1;
}
export interface CallEndedEvent {
  frame_type?: FrameType78;
  request_id?: RequestId8;
  session_id?: SessionId88;
  event?: Event9;
  data: CallEndedData;
}
export interface CallEndedData {
  call_id: CallId6;
  call_state?: CallState1;
  reason?: Reason3;
  duration_seconds?: DurationSeconds;
}
export interface TranscriptEvent {
  frame_type?: FrameType79;
  request_id?: RequestId9;
  session_id?: SessionId89;
  event?: Event10;
  data: TranscriptData;
}
export interface TranscriptData {
  role: Role;
  text: Text4;
  final?: Final;
  call_id?: CallId7;
  timestamp?: Timestamp;
}
export interface ScreenCaptureStateEvent {
  frame_type?: FrameType80;
  request_id?: RequestId10;
  session_id?: SessionId90;
  event?: Event11;
  data: ScreenCaptureStateData;
}
export interface ScreenCaptureStateData {
  active: Active;
  target?: Target2;
  call_id?: CallId8;
}
export interface UsageEvent {
  frame_type?: FrameType81;
  request_id?: RequestId11;
  session_id?: SessionId91;
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
  frame_type?: FrameType82;
  request_id?: RequestId12;
  session_id?: SessionId92;
  event?: Event13;
  data: ErrorData;
}
export interface ErrorData {
  message: Message;
  code?: Code;
}
export interface ConversationUpdatedEvent {
  frame_type?: FrameType83;
  request_id?: RequestId13;
  session_id?: SessionId93;
  event?: Event14;
  data: ConversationUpdatedData;
}
export interface ConversationUpdatedData {
  session_id: SessionId94;
  title?: Title5;
  pinned?: Pinned1;
  updated_at?: UpdatedAt;
  user_message?: UserMessage;
  assistant_message?: AssistantMessage;
  turns?: Turns;
  request_id?: RequestId14;
}
export interface ConversationDeletedEvent {
  frame_type?: FrameType84;
  request_id?: RequestId15;
  session_id?: SessionId95;
  event?: Event15;
  data: ConversationDeletedData;
}
export interface ConversationDeletedData {
  session_ids?: SessionIds;
  all?: All;
}
export interface UiNavigateEvent {
  frame_type?: FrameType85;
  request_id?: RequestId16;
  session_id?: SessionId96;
  event?: Event16;
  data: UiNavigateData;
}
export interface UiNavigateData {
  view?: View1;
  conversation_id?: ConversationId5;
  open_command_palette?: OpenCommandPalette1;
}
export interface ConversationCreatedEvent {
  frame_type?: FrameType86;
  request_id?: RequestId17;
  session_id?: SessionId97;
  event?: Event17;
  data: ConversationCreatedData;
}
export interface ConversationCreatedData {
  session_id: SessionId98;
  title: Title6;
  started_at?: StartedAt2;
  updated_at?: UpdatedAt1;
  turns?: Turns1;
  initial_message?: InitialMessage;
  open_in_ui?: OpenInUi1;
}
export interface MemoryUpdatedEvent {
  frame_type?: FrameType87;
  request_id?: RequestId18;
  session_id?: SessionId99;
  event?: Event18;
  data: MemoryUpdatedData;
}
export interface MemoryUpdatedData {
  action?: Action7;
  fact_id?: FactId1;
  section?: Section;
  text?: Text5;
  removed_count?: RemovedCount;
  removed_ids?: RemovedIds;
}
export interface TimerFiredEvent {
  frame_type?: FrameType88;
  request_id?: RequestId19;
  session_id?: SessionId100;
  event?: Event19;
  data: TimerFiredData;
}
export interface TimerFiredData {
  timer_id: TimerId;
  label?: Label1;
  duration_seconds?: DurationSeconds1;
  fires_at: FiresAt;
  fired_at: FiredAt;
}
export interface ReminderFiredEvent {
  frame_type?: FrameType89;
  request_id?: RequestId20;
  session_id?: SessionId101;
  event?: Event20;
  data: ReminderFiredData;
}
export interface ReminderFiredData {
  reminder_id: ReminderId;
  message: Message1;
  due_at: DueAt;
  fired_at: FiredAt1;
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
  memory_search_episodic: MemorySearchEpisodicResult;
  memory_manage_fact: MemoryManageFactResult;
  memory_list_facts: MemoryListFactsResult;
  activity_list: ActivityListResult;
  activity_cancel: ActivityCancelResult;
  os_open_or_focus_app: OsOpenOrFocusAppResult;
  os_control_media_volume: OsControlMediaVolumeResult;
  os_read_clipboard: OsReadClipboardResult;
  os_select_clipboard_item: OsSelectClipboardItemResult;
  os_open_url: OsOpenUrlResult;
  os_control_system: OsControlSystemResult;
  os_write_clipboard: OsWriteClipboardResult;
  os_transform_clipboard: OsTransformClipboardResult;
  os_save_clipboard_to_file: OsSaveClipboardToFileResult;
  os_open_path_or_url: OsOpenPathOrUrlResult;
  os_inspect_screen: OsInspectScreenResult;
  os_inspect_ui_elements: OsInspectUiElementsResult;
  os_click_ui_element: OsClickUiElementResult;
  os_gui_action: OsGuiActionResult;
  os_ui_snapshot: OsUiSnapshotResult;
  os_ui_act: OsUiActResult;
  os_ui_batch: OsUiBatchResult;
  os_cancel_inflight: OsCancelInflightResult;
  workspace_git_inspect: WorkspaceGitInspectResult;
}
export interface HealthPingResult {
  status?: Status2;
}
export interface SessionNewResult {
  session_id: SessionId102;
}
export interface ChatSendResult {
  text: Text6;
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
  session_id: SessionId103;
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
  name: Name4;
  entries?: Entries;
}
export interface MemoryFactItem {
  id: Id71;
  date: Date4;
  source: Source;
  text: Text7;
}
export interface MemoryArchiveResult {
  entries?: Entries1;
  raw?: Raw;
}
export interface MemoryArchiveItem {
  id: Id72;
  date: Date5;
  source: Source1;
  text: Text8;
  archived_at?: ArchivedAt;
  reason?: Reason4;
}
export interface MemoryStatusResult {
  last_consolidated_at?: LastConsolidatedAt;
  pending?: Pending;
  failed?: Failed;
  completed_count?: CompletedCount;
}
export interface ConsolidationSessionStatus {
  session_id: SessionId104;
  status: Status3;
  retry_count: RetryCount;
  error?: Error2;
  last_attempt_at?: LastAttemptAt;
  next_retry_at?: NextRetryAt;
}
export interface MemoryReconsolidateResult {
  session_id: SessionId105;
  ok: Ok1;
  status: Status4;
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
  session_id: SessionId106;
  started_at: StartedAt3;
  title: Title7;
  message_count: MessageCount;
}
export interface TranscriptsGetResult {
  session_id: SessionId107;
  started_at: StartedAt4;
  title: Title8;
  messages: Messages;
}
export interface TranscriptMessageItem {
  role: Role1;
  text: Text9;
  timestamp: Timestamp1;
  tool_calls?: ToolCalls;
}
export interface DevEmitSampleTaskResult {
  status?: Status5;
}
export interface ConversationsListResult {
  conversations: Conversations;
}
export interface ConversationSummary {
  date: Date6;
  session_id: SessionId108;
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
  session_id: SessionId109;
  turns: Turns3;
}
export interface ConversationTurn {
  time: Time1;
  user: User;
  assistant: Assistant;
}
export interface ConversationResumeResult {
  session_id: SessionId110;
  title: Title10;
  pinned?: Pinned3;
  messages: Messages1;
}
export interface ConversationResumeMessage {
  role: Role2;
  text: Text10;
  timestamp?: Timestamp2;
  tool_calls?: ToolCalls1;
}
export interface ConversationRenameResult {
  session_id: SessionId111;
  title: Title11;
}
export interface ConversationPinResult {
  session_id: SessionId112;
  pinned: Pinned4;
}
export interface ConversationExportResult {
  session_id: SessionId113;
  title: Title12;
  filename: Filename1;
  content: Content3;
}
export interface ConversationDeleteResult {
  session_id: SessionId114;
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
  name: Name5;
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
  status: Status6;
  output?: Output1;
}
export interface VoiceHistoryResult {
  session_id?: SessionId115;
  messages?: Messages2;
}
export interface VoiceHistoryMessageItem {
  role: Role3;
  text: Text11;
  timestamp?: Timestamp3;
  call_id?: CallId13;
}
export interface VoiceListConversationsResult {
  conversations?: Conversations1;
}
export interface VoiceConversationListItem {
  conversation_id?: ConversationId7;
  session_id?: SessionId116;
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
  session_id?: SessionId117;
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
  session_id?: SessionId118;
  title?: Title15;
  updated_at?: UpdatedAt5;
  episode_summary?: EpisodeSummary3;
  total_turns?: TotalTurns;
  truncated?: Truncated;
  turns?: Turns5;
  message?: Message2;
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
  session_id?: SessionId119;
  conversation_id?: ConversationId10;
  text: Text12;
  score?: Score1;
  semantic_score?: SemanticScore1;
  lexical_score?: LexicalScore1;
}
export interface VoicePostToActiveChatResult {
  ok: Ok3;
  session_id?: SessionId120;
  title?: Title16;
  message?: Message3;
}
export interface UiNavigateResult {
  ok?: Ok4;
  view?: View2;
  active_conversation_id?: ActiveConversationId1;
  open_command_palette?: OpenCommandPalette2;
}
export interface ChatCreateAndOpenResult {
  ok?: Ok5;
  session_id: SessionId121;
  title: Title17;
  message?: Message4;
  open_in_ui?: OpenInUi2;
}
export interface ChatSendFromVoiceResult {
  ok?: Ok6;
  session_id: SessionId122;
  title?: Title18;
  request_id?: RequestId21;
  status?: Status7;
}
export interface ChatRenameResult {
  ok?: Ok7;
  session_id: SessionId123;
  title: Title19;
}
export interface ChatDeleteResult {
  ok?: Ok8;
  session_id: SessionId124;
  deleted?: Deleted1;
  requires_confirmation?: RequiresConfirmation;
  episodes_removed?: EpisodesRemoved;
  message?: Message5;
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
export interface MemorySearchEpisodicResult {
  results?: Results2;
}
export interface EpisodicSessionHit {
  session_id: SessionId125;
  surface?: Surface1;
  timestamp?: Timestamp7;
  title?: Title20;
  summary?: Summary1;
  topics?: Topics;
  snippet?: Snippet2;
  score?: Score2;
}
export interface MemoryManageFactResult {
  ok?: Ok11;
  operation: Operation;
  fact?: MemoryBankFactItem | null;
  superseded_id?: SupersededId;
  affected_ids?: AffectedIds;
  message?: Message6;
}
export interface MemoryBankFactItem {
  id: Id73;
  category: Category3;
  fact_key: FactKey1;
  fact_text: FactText1;
  confidence?: Confidence1;
  source_surface?: SourceSurface1;
  source_session_id?: SourceSessionId1;
  status?: Status8;
  created_at?: CreatedAt;
  updated_at?: UpdatedAt6;
}
export interface MemoryListFactsResult {
  facts?: Facts;
  total?: Total;
}
export interface ActivityListResult {
  tasks?: Tasks;
}
export interface ActivityCancelResult {
  ok?: Ok12;
  task_id: TaskId3;
  status: Status9;
  message?: Message7;
}
export interface OsOpenOrFocusAppResult {
  ok?: Ok13;
  status: Status10;
  app: App;
  window_title?: WindowTitle1;
  hwnd?: Hwnd1;
  executable?: Executable;
  target_path?: TargetPath1;
  error?: Error4;
  verification?: ActionVerificationInfo | null;
}
export interface ActionVerificationInfo {
  verified: Verified;
  status?: Status11;
  outcome_kind?: OutcomeKind;
  reason?: Reason5;
  actual?: Actual;
  elapsed_ms?: ElapsedMs;
}
export interface Actual {
  [k: string]: unknown | undefined;
}
export interface OsControlMediaVolumeResult {
  ok?: Ok14;
  action: Action8;
  level?: Level2;
  verification?: ActionVerificationInfo | null;
}
export interface OsReadClipboardResult {
  ok?: Ok15;
  kind?: Kind2;
  text?: Text13;
  truncated?: Truncated1;
  char_count?: CharCount;
  total_chars?: TotalChars;
  image?: Image;
  file_paths?: FilePaths;
  has_image_files?: HasImageFiles;
  history_enabled?: HistoryEnabled;
  total_items?: TotalItems;
  items?: Items;
}
export interface ClipboardItem {
  index?: Index1;
  id?: Id74;
  timestamp?: Timestamp8;
  kind?: Kind3;
  text?: Text14;
  truncated?: Truncated2;
  char_count?: CharCount1;
  total_chars?: TotalChars1;
  width?: Width1;
  height?: Height1;
  format?: Format;
  image_bytes_base64?: ImageBytesBase64;
  size_bytes?: SizeBytes;
  image?: Image1;
}
export interface OsSelectClipboardItemResult {
  ok?: Ok16;
  success?: Success;
  status?: Status12;
  index?: Index2;
  item_id?: ItemId1;
  message?: Message8;
}
export interface OsOpenUrlResult {
  ok?: Ok17;
  success?: Success1;
  status?: Status13;
  url: Url1;
  title?: Title21;
  browser?: Browser1;
  message?: Message9;
  routed_to_installed_app?: RoutedToInstalledApp;
  app_name?: AppName6;
  window_hwnd?: WindowHwnd;
  window_title?: WindowTitle2;
  resolution_layer?: ResolutionLayer;
  guidance?: Guidance;
  reused_browser_window?: ReusedBrowserWindow;
  target_id?: TargetId;
  is_pwa?: IsPwa;
  cdp_used?: CdpUsed;
  before_url?: BeforeUrl;
  after_url?: AfterUrl;
  state_diff?: StateDiff;
}
export interface OsControlSystemResult {
  ok?: Ok18;
  success?: Success2;
  action?: Action9;
  status?: Status14;
  level?: Level3;
  current_brightness?: CurrentBrightness;
  window_title?: WindowTitle3;
  hwnd?: Hwnd2;
  page?: Page1;
  uri?: Uri1;
  message?: Message10;
  rect?: Rect;
  verification?: ActionVerificationInfo | null;
  aborted_precondition?: PreconditionAbortInfo | null;
}
export interface PreconditionAbortInfo {
  status?: Status15;
  reason?: Reason6;
  step_label?: StepLabel;
  expected_hwnd?: ExpectedHwnd1;
  expected_title?: ExpectedTitle;
  expected_process?: ExpectedProcess;
  actual_hwnd?: ActualHwnd;
  actual_title?: ActualTitle;
  actual_process?: ActualProcess;
  message?: Message11;
}
export interface OsWriteClipboardResult {
  ok?: Ok19;
  char_count?: CharCount2;
}
export interface OsTransformClipboardResult {
  ok?: Ok20;
  status?: Status16;
  action?: Action10;
  updated_clipboard?: UpdatedClipboard;
  preview?: Preview;
  error?: Error5;
}
export interface OsSaveClipboardToFileResult {
  ok?: Ok21;
  status?: Status17;
  saved_path?: SavedPath;
  path?: Path;
  filename?: Filename2;
  size_bytes?: SizeBytes1;
  kind?: Kind4;
  error?: Error6;
}
export interface OsOpenPathOrUrlResult {
  ok?: Ok22;
  status?: Status18;
  kind?: Kind5;
  target?: Target3;
  filename?: Filename3;
  error?: Error7;
}
export interface OsInspectScreenResult {
  ok?: Ok23;
  status?: Status19;
  source?: Source4;
  window_title?: WindowTitle4;
  process_name?: ProcessName;
  question?: Question1;
  width?: Width2;
  height?: Height2;
  image_width?: ImageWidth2;
  image_height?: ImageHeight2;
  screen_width?: ScreenWidth2;
  screen_height?: ScreenHeight2;
  virtual_left?: VirtualLeft2;
  virtual_top?: VirtualTop2;
  dpi_scale?: DpiScale2;
  coord_space?: CoordSpace2;
  artifact_path?: ArtifactPath;
  uia_text?: UiaText;
  text?: Text15;
  message?: Message12;
  image_base64?: ImageBase64;
  image_mime_type?: ImageMimeType;
}
export interface OsInspectUiElementsResult {
  ok?: Ok24;
  status?: Status20;
  source?: Source5;
  cdp_available?: CdpAvailable;
  accessibility_coverage?: AccessibilityCoverage;
  hint?: Hint;
  message?: Message13;
  error?: Error8;
  window_title?: WindowTitle5;
  count?: Count;
  elements?: Elements;
}
export interface UiElementItem {
  index: Index3;
  name?: Name6;
  automation_id?: AutomationId;
  control_type?: ControlType1;
  value?: Value2;
  x?: X2;
  y?: Y2;
}
export interface OsClickUiElementResult {
  ok?: Ok25;
  status?: Status21;
  name?: Name7;
  control_type?: ControlType2;
  x?: X3;
  y?: Y3;
  method?: Method69;
  cdp_available?: CdpAvailable1;
  hint?: Hint1;
  message?: Message14;
  state_diff?: StateDiff1;
  reason?: Reason7;
  error?: Error9;
  aborted_precondition?: PreconditionAbortInfo | null;
}
export interface OsGuiActionResult {
  ok?: Ok26;
  status?: Status22;
  source?: Source6;
  action?: Action11;
  method?: Method70;
  direction?: Direction;
  x?: X4;
  y?: Y4;
  physical_x?: PhysicalX;
  physical_y?: PhysicalY;
  coord_space?: CoordSpace3;
  clicks?: Clicks2;
  button?: Button2;
  char_count?: CharCount3;
  keys?: Keys2;
  delta?: Delta2;
  seconds?: Seconds2;
  ref?: Ref3;
  url?: Url2;
  title?: Title22;
  before_url?: BeforeUrl1;
  after_url?: AfterUrl1;
  before_title?: BeforeTitle;
  after_title?: AfterTitle;
  target_id?: TargetId1;
  is_pwa?: IsPwa1;
  cdp_available?: CdpAvailable2;
  hint?: Hint2;
  message?: Message15;
  state_diff?: StateDiff2;
  circuit_breaker_tripped?: CircuitBreakerTripped;
  suggested_recovery?: SuggestedRecovery;
  entry_method?: EntryMethod;
  clipboard_restored?: ClipboardRestored;
  cancelled?: Cancelled;
  reason?: Reason8;
  error?: Error10;
  aborted_precondition?: PreconditionAbortInfo | null;
}
export interface OsUiSnapshotResult {
  ok?: Ok27;
  status?: Status23;
  source?: Source7;
  snapshot_id?: SnapshotId;
  window_title?: WindowTitle6;
  process_name?: ProcessName1;
  url?: Url3;
  title?: Title23;
  target_id?: TargetId2;
  is_pwa?: IsPwa2;
  cdp_available?: CdpAvailable3;
  accessibility_coverage?: AccessibilityCoverage1;
  content_element_count?: ContentElementCount;
  hint?: Hint3;
  message?: Message16;
  error?: Error11;
  truncated?: Truncated3;
  count?: Count1;
  element_count?: ElementCount;
  elements?: Elements1;
}
export interface UiSnapshotElement {
  ref: Ref4;
  role?: Role7;
  name?: Name8;
  value?: Value3;
  x?: X5;
  y?: Y5;
  automation_id?: AutomationId1;
  control_type?: ControlType3;
  enabled?: Enabled;
  focused?: Focused;
}
export interface OsUiActResult {
  ok?: Ok28;
  status?: Status24;
  source?: Source8;
  snapshot_id?: SnapshotId1;
  action?: Action12;
  ref?: Ref5;
  role?: Role8;
  name?: Name9;
  method?: Method71;
  x?: X6;
  y?: Y6;
  char_count?: CharCount4;
  interval_ms?: IntervalMs2;
  url?: Url4;
  title?: Title24;
  before_url?: BeforeUrl2;
  after_url?: AfterUrl2;
  before_title?: BeforeTitle1;
  after_title?: AfterTitle1;
  target_id?: TargetId3;
  is_pwa?: IsPwa3;
  cdp_available?: CdpAvailable4;
  hint?: Hint4;
  message?: Message17;
  state_diff?: StateDiff3;
  diff?: UiDiff | null;
  snapshot?: OsUiSnapshotResult | null;
  active_window_title?: ActiveWindowTitle;
  active_hwnd?: ActiveHwnd;
  target_state?: TargetState;
  snapshot_refreshed?: SnapshotRefreshed;
  stale_ref?: StaleRef;
  reason?: Reason9;
  error?: Error12;
  aborted_precondition?: PreconditionAbortInfo | null;
}
export interface UiDiff {
  window_changed?: WindowChanged;
  window_title?: WindowTitle7;
  changed_values?: ChangedValues;
  added_elements?: AddedElements;
  removed_count?: RemovedCount3;
}
export interface UiDiffChangedValue {
  ref?: Ref6;
  role?: Role9;
  name?: Name10;
  old_value?: OldValue;
  new_value?: NewValue;
}
export interface OsUiBatchResult {
  ok?: Ok29;
  status?: Status25;
  steps_executed?: StepsExecuted;
  step_results?: StepResults;
  state_diff?: StateDiff4;
  diff?: UiDiff | null;
  snapshot?: OsUiSnapshotResult | null;
  cancelled?: Cancelled1;
  reason?: Reason10;
  error?: Error13;
  aborted_precondition?: PreconditionAbortInfo | null;
}
export interface OsCancelInflightResult {
  ok?: Ok30;
  cancelled?: Cancelled2;
  cancelled_count?: CancelledCount;
}
export interface WorkspaceGitInspectResult {
  ok?: Ok31;
  repo_path: RepoPath1;
  branch: Branch;
  is_clean?: IsClean;
  status?: Status26;
  staged?: Staged;
  unstaged?: Unstaged;
  untracked?: Untracked;
  worktrees?: Worktrees;
  recent_commits?: RecentCommits;
  diff?: Diff;
  diff_truncated?: DiffTruncated;
}
export interface GitWorktreeItem {
  path: Path1;
  head?: Head;
  branch?: Branch1;
}
export interface GitCommitItem {
  sha: Sha;
  subject: Subject;
  oneline: Oneline;
}
