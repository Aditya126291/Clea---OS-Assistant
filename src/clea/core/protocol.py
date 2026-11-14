"""Typed WebSocket protocol shared between the core daemon and surfaces.

This module is the single source of truth for every message on the wire. TypeScript
types for the desktop UI are generated from these models (``scripts/gen_ts_types.py``).

It intentionally has zero dependencies on core internals so that CLI, voice, and
desktop UI surfaces can import it without coupling to the daemon.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

RiskTier = Literal["read_only", "reversible", "destructive"]
ApprovalDecision = Literal["allow", "deny"]
ApprovalReason = Literal["user", "timeout"]
TaskKind = Literal["chat_turn", "background_task", "scheduled", "agent_run"]
TaskState = Literal["queued", "running", "succeeded", "failed", "cancelled"]
VoiceState = Literal["idle", "listening", "thinking", "speaking", "reconnecting", "error"]
CallState = Literal["idle", "connecting", "active", "ending"]
TranscriptRole = Literal["user", "assistant"]
ScreenCaptureTarget = Literal["active_window", "full_screen"]
VoiceTrigger = Literal["manual", "wake_word", "hotkey", "ui"]
UiView = Literal["chat", "voice", "activity", "memory", "settings"]


def new_id() -> str:
    """Generate a unique identifier for protocol requests and sessions."""
    return uuid4().hex


class _Model(BaseModel):
    """Strict base: unknown fields are rejected so client and daemon cannot silently drift."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- requests


class _EmptyParams(_Model):
    pass


class ChatSendParams(_Model):
    text: str


class ChatFocusParams(_Model):
    session_id: str | None = None
    clear: bool = False


class ApprovalRespondParams(_Model):
    approval_id: str
    decision: ApprovalDecision


class DailyParams(_Model):
    date: str = Field(description="ISO date, YYYY-MM-DD")


class ConversationGetParams(_Model):
    session_id: str
    date: str = Field(default="", description="ISO date, YYYY-MM-DD")


class ConversationResumeParams(_Model):
    session_id: str


class ConversationRenameParams(_Model):
    session_id: str
    title: str


class ConversationPinParams(_Model):
    session_id: str
    pinned: bool


class ConversationExportParams(_Model):
    session_id: str


class ConversationDeleteParams(_Model):
    session_id: str
    delete_episodes: bool = False


class ConversationDeleteAllParams(_Model):
    confirmation: str


class ConversationUndoDeleteParams(_Model):
    session_id: str | None = None


class VoiceStartCallParams(_Model):
    trigger: VoiceTrigger = "manual"


class VoiceEndCallParams(_Model):
    call_id: str | None = None
    reason: str | None = None
    transcript: list[dict[str, Any]] | None = None


class VoiceDelegateTaskParams(_Model):
    description: str
    call_id: str | None = None
    tool_call_id: str | None = None


class VoiceHistoryParams(_Model):
    limit: int = Field(default=100, ge=1, le=500)


class VoiceListConversationsParams(_Model):
    limit: int = Field(default=10, ge=1, le=100)


class VoiceSearchConversationsParams(_Model):
    query: str
    limit: int = Field(default=8, ge=1, le=50)


class VoiceReadConversationParams(_Model):
    conversation_id: str
    max_turns: int = Field(default=40, ge=1, le=500)


class VoiceSearchMemoryParams(_Model):
    query: str
    limit: int = Field(default=10, ge=1, le=50)


class VoicePostToActiveChatParams(_Model):
    content: str
    label: str | None = None


class UiNavigateParams(_Model):
    view: UiView = "chat"
    conversation_id: str | None = None
    open_command_palette: bool = False


class ChatCreateAndOpenParams(_Model):
    title: str
    content: str = ""
    open_in_ui: bool = True


class ChatSendFromVoiceParams(_Model):
    prompt: str
    conversation_id: str | None = None
    title: str | None = None


class ChatRenameParams(_Model):
    conversation_id: str
    new_title: str


class ChatDeleteParams(_Model):
    conversation_id: str
    confirmed: bool = False
    delete_episodes: bool = False


class MemoryAddFactParams(_Model):
    fact: str
    category: str | None = None


class MemoryDeleteFactParams(_Model):
    query_or_id: str


class ActivityListParams(_Model):
    limit: int = Field(default=10, ge=1, le=100)


class ActivityCancelParams(_Model):
    task_id: str


class _Request(_Model):
    frame_type: Literal["request"] = "request"
    id: str = Field(default_factory=new_id)
    session_id: str | None = None


class HealthPingRequest(_Request):
    method: Literal["health.ping"] = "health.ping"
    params: _EmptyParams = Field(default_factory=_EmptyParams)


class SessionNewRequest(_Request):
    method: Literal["session.new"] = "session.new"
    params: _EmptyParams = Field(default_factory=_EmptyParams)


class ChatSendRequest(_Request):
    method: Literal["chat.send"] = "chat.send"
    params: ChatSendParams


class ChatFocusRequest(_Request):
    method: Literal["chat.focus"] = "chat.focus"
    params: ChatFocusParams = Field(default_factory=ChatFocusParams)


class ApprovalRespondRequest(_Request):
    method: Literal["approval.respond"] = "approval.respond"
    params: ApprovalRespondParams


class MemoryGetRequest(_Request):
    """Fetch MEMORY.md and the list of days that have a daily log."""

    method: Literal["memory.get"] = "memory.get"
    params: _EmptyParams = Field(default_factory=_EmptyParams)


class MemoryGetDailyRequest(_Request):
    method: Literal["memory.get_daily"] = "memory.get_daily"
    params: DailyParams


class MemoryEpisodesParams(_Model):
    limit: int = Field(default=20, ge=1, le=100)


class MemoryEpisodesRequest(_Request):
    method: Literal["memory.episodes"] = "memory.episodes"
    params: MemoryEpisodesParams = Field(default_factory=MemoryEpisodesParams)


class MemoryFactsRequest(_Request):
    """Fetch structured durable facts parsed by section."""

    method: Literal["memory.facts"] = "memory.facts"
    params: _EmptyParams = Field(default_factory=_EmptyParams)


class MemoryArchiveRequest(_Request):
    """Fetch archived facts from MEMORY_ARCHIVE.md."""

    method: Literal["memory.archive"] = "memory.archive"
    params: _EmptyParams = Field(default_factory=_EmptyParams)


class MemoryStatusRequest(_Request):
    """Fetch consolidation status (pending, failed, last-success)."""

    method: Literal["memory.status"] = "memory.status"
    params: _EmptyParams = Field(default_factory=_EmptyParams)


class MemoryReconsolidateParams(_Model):
    session_id: str


class MemoryReconsolidateRequest(_Request):
    """Trigger manual reconsolidation of a session."""

    method: Literal["memory.reconsolidate"] = "memory.reconsolidate"
    params: MemoryReconsolidateParams


class MemoryForgetParams(_Model):
    id: str | None = Field(default=None, description="Entry ID of the fact to forget")
    query: str | None = Field(
        default=None, description="Keywords, text, or ID of the memory to forget"
    )


class MemoryForgetRequest(_Request):
    """Forget durable facts or preferences matching an entry ID or query."""

    method: Literal["memory.forget"] = "memory.forget"
    params: MemoryForgetParams


class TranscriptsListParams(_Model):
    limit: int = Field(default=50, ge=1, le=200)


class TranscriptsListRequest(_Request):
    method: Literal["transcripts.list"] = "transcripts.list"
    params: TranscriptsListParams = Field(default_factory=TranscriptsListParams)


class TranscriptsGetParams(_Model):
    session_id: str


class TranscriptsGetRequest(_Request):
    method: Literal["transcripts.get"] = "transcripts.get"
    params: TranscriptsGetParams


class DevEmitSampleTaskRequest(_Request):
    method: Literal["dev.emit_sample_task"] = "dev.emit_sample_task"
    params: _EmptyParams = Field(default_factory=_EmptyParams)


class ConversationsListRequest(_Request):
    method: Literal["conversations.list"] = "conversations.list"
    params: _EmptyParams = Field(default_factory=_EmptyParams)


class ConversationsGetRequest(_Request):
    method: Literal["conversations.get"] = "conversations.get"
    params: ConversationGetParams


class ConversationListRequest(_Request):
    method: Literal["conversation.list"] = "conversation.list"
    params: _EmptyParams = Field(default_factory=_EmptyParams)


class ConversationGetRequest(_Request):
    method: Literal["conversation.get"] = "conversation.get"
    params: ConversationGetParams


class ConversationResumeRequest(_Request):
    method: Literal["conversation.resume"] = "conversation.resume"
    params: ConversationResumeParams


class ConversationRenameRequest(_Request):
    method: Literal["conversation.rename"] = "conversation.rename"
    params: ConversationRenameParams


class ConversationPinRequest(_Request):
    method: Literal["conversation.pin"] = "conversation.pin"
    params: ConversationPinParams


class ConversationExportRequest(_Request):
    method: Literal["conversation.export"] = "conversation.export"
    params: ConversationExportParams


class ConversationDeleteRequest(_Request):
    method: Literal["conversation.delete"] = "conversation.delete"
    params: ConversationDeleteParams


class ConversationDeleteAllRequest(_Request):
    method: Literal["conversation.delete_all"] = "conversation.delete_all"
    params: ConversationDeleteAllParams


class ConversationUndoDeleteRequest(_Request):
    method: Literal["conversation.undo_delete"] = "conversation.undo_delete"
    params: ConversationUndoDeleteParams


class SettingsGetRequest(_Request):
    method: Literal["settings.get"] = "settings.get"
    params: _EmptyParams = Field(default_factory=_EmptyParams)


class VoiceStartCallRequest(_Request):
    method: Literal["voice.start_call"] = "voice.start_call"
    params: VoiceStartCallParams = Field(default_factory=VoiceStartCallParams)


class VoiceEndCallRequest(_Request):
    method: Literal["voice.end_call"] = "voice.end_call"
    params: VoiceEndCallParams = Field(default_factory=VoiceEndCallParams)


class VoiceStatusRequest(_Request):
    method: Literal["voice.status"] = "voice.status"
    params: _EmptyParams = Field(default_factory=_EmptyParams)


class VoiceDelegateTaskRequest(_Request):
    method: Literal["voice.delegate_task"] = "voice.delegate_task"
    params: VoiceDelegateTaskParams


class VoiceHistoryRequest(_Request):
    method: Literal["voice.history"] = "voice.history"
    params: VoiceHistoryParams = Field(default_factory=VoiceHistoryParams)


class VoiceListConversationsRequest(_Request):
    method: Literal["voice.list_conversations"] = "voice.list_conversations"
    params: VoiceListConversationsParams = Field(default_factory=VoiceListConversationsParams)


class VoiceSearchConversationsRequest(_Request):
    method: Literal["voice.search_conversations"] = "voice.search_conversations"
    params: VoiceSearchConversationsParams


class VoiceReadConversationRequest(_Request):
    method: Literal["voice.read_conversation"] = "voice.read_conversation"
    params: VoiceReadConversationParams


class VoiceSearchMemoryRequest(_Request):
    method: Literal["voice.search_memory"] = "voice.search_memory"
    params: VoiceSearchMemoryParams


class VoicePostToActiveChatRequest(_Request):
    method: Literal["voice.post_to_active_chat"] = "voice.post_to_active_chat"
    params: VoicePostToActiveChatParams


class UiNavigateRequest(_Request):
    method: Literal["ui.navigate"] = "ui.navigate"
    params: UiNavigateParams = Field(default_factory=UiNavigateParams)


class ChatCreateAndOpenRequest(_Request):
    method: Literal["chat.create_and_open"] = "chat.create_and_open"
    params: ChatCreateAndOpenParams


class ChatSendFromVoiceRequest(_Request):
    method: Literal["chat.send_from_voice"] = "chat.send_from_voice"
    params: ChatSendFromVoiceParams


class ChatRenameRequest(_Request):
    method: Literal["chat.rename"] = "chat.rename"
    params: ChatRenameParams


class ChatDeleteRequest(_Request):
    method: Literal["chat.delete"] = "chat.delete"
    params: ChatDeleteParams


class MemoryAddFactRequest(_Request):
    method: Literal["memory.add_fact"] = "memory.add_fact"
    params: MemoryAddFactParams


class MemoryDeleteFactRequest(_Request):
    method: Literal["memory.delete_fact"] = "memory.delete_fact"
    params: MemoryDeleteFactParams


class ActivityListRequest(_Request):
    method: Literal["activity.list"] = "activity.list"
    params: ActivityListParams = Field(default_factory=ActivityListParams)


class ActivityCancelRequest(_Request):
    method: Literal["activity.cancel"] = "activity.cancel"
    params: ActivityCancelParams


RequestFrame = Annotated[
    HealthPingRequest
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
    | ActivityCancelRequest,
    Field(discriminator="method"),
]
"""Any client-to-daemon request (discriminated by ``method``)."""

# ``isinstance`` target for "is this a request frame".
BaseRequest = _Request


# --------------------------------------------------------------------------- responses


class ResponseFrame(_Model):
    """Daemon-to-client response paired with a request ID."""

    frame_type: Literal["response"] = "response"
    id: str
    ok: bool = True
    session_id: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class HealthPingResult(_Model):
    status: Literal["ok"] = "ok"


class SessionNewResult(_Model):
    session_id: str


class ChatSendResult(_Model):
    text: str


class ChatFocusResult(_Model):
    active_conversation_id: str | None = None


class ApprovalRespondResult(_Model):
    approval_id: str
    decision: ApprovalDecision


class MemoryGetResult(_Model):
    long_term: str
    days: list[str] = Field(
        default_factory=list, description="ISO dates with a daily log, newest first"
    )
    last_consolidated: str | None = None
    failed_consolidation: str | None = None


class MemoryFactItem(_Model):
    id: str
    date: str
    source: str
    text: str


class MemorySectionItem(_Model):
    name: str
    entries: list[MemoryFactItem] = Field(default_factory=list)


class MemoryFactsResult(_Model):
    sections: list[MemorySectionItem]
    last_consolidated: str | None = None
    failed_consolidation: str | None = None


class EpisodeItem(_Model):
    date: str
    session_id: str
    time: str
    summary: str
    trivial: bool = False


class MemoryEpisodesResult(_Model):
    episodes: list[EpisodeItem]


class MemoryArchiveItem(_Model):
    id: str
    date: str
    source: str
    text: str
    archived_at: str | None = None
    reason: str | None = None


class MemoryArchiveResult(_Model):
    entries: list[MemoryArchiveItem] = Field(default_factory=list)
    raw: str = ""


class ConsolidationSessionStatus(_Model):
    session_id: str
    status: str
    retry_count: int
    error: str | None = None
    last_attempt_at: str | None = None
    next_retry_at: str | None = None


class MemoryStatusResult(_Model):
    last_consolidated_at: str | None = None
    pending: list[ConsolidationSessionStatus] = Field(default_factory=list)
    failed: list[ConsolidationSessionStatus] = Field(default_factory=list)
    completed_count: int = 0


class MemoryReconsolidateResult(_Model):
    session_id: str
    ok: bool
    status: str
    error: str | None = None


class MemoryForgetResult(_Model):
    ok: bool = True
    removed_count: int = 0
    removed_ids: list[str] = Field(default_factory=list)


class TranscriptSummaryItem(_Model):
    session_id: str
    started_at: str
    title: str
    message_count: int


class TranscriptsListResult(_Model):
    transcripts: list[TranscriptSummaryItem]


class TranscriptMessageItem(_Model):
    role: str
    text: str
    timestamp: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class TranscriptsGetResult(_Model):
    session_id: str
    started_at: str
    title: str
    messages: list[TranscriptMessageItem]


class DevEmitSampleTaskResult(_Model):
    status: Literal["ok"] = "ok"


class MemoryGetDailyResult(_Model):
    date: str
    content: str


class ConversationSummary(_Model):
    date: str
    session_id: str
    conversation_id: str = ""
    title: str
    turns: int
    turn_count: int = 0
    last_time: str
    pinned: bool = False
    updated_at: str = ""
    episode_summary: str = ""

    @model_validator(mode="after")
    def _sync_fields(self) -> ConversationSummary:
        if not self.conversation_id and self.session_id:
            self.conversation_id = self.session_id
        if self.turn_count == 0 and self.turns > 0:
            self.turn_count = self.turns
        return self


class ConversationsListResult(_Model):
    conversations: list[ConversationSummary]


class ConversationTurn(_Model):
    time: str
    user: str
    assistant: str


class ConversationsGetResult(_Model):
    date: str
    session_id: str
    turns: list[ConversationTurn]


class ConversationResumeMessage(_Model):
    role: Literal["user", "assistant"]
    text: str
    timestamp: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


class ConversationResumeResult(_Model):
    session_id: str
    title: str
    pinned: bool = False
    messages: list[ConversationResumeMessage]


class ConversationRenameResult(_Model):
    session_id: str
    title: str


class ConversationPinResult(_Model):
    session_id: str
    pinned: bool


class ConversationExportResult(_Model):
    session_id: str
    title: str
    filename: str
    content: str


class ConversationDeleteResult(_Model):
    session_id: str
    deleted: bool
    undo_window_seconds: int = 10


class ConversationDeleteAllResult(_Model):
    deleted_count: int
    undo_window_seconds: int = 10


class ConversationUndoDeleteResult(_Model):
    restored_count: int
    session_ids: list[str]


class ToolInfo(_Model):
    name: str
    description: str
    risk_level: RiskTier


class PermissionTierInfo(_Model):
    tier: RiskTier
    policy: Literal["auto_allow", "ask", "ask_confirm"]


class SettingsGetResult(_Model):
    """Non-secret daemon configuration for display. Never contains keys or tokens."""

    provider: str
    model: str
    host: str
    port: int
    approval_timeout_seconds: float
    tools: list[ToolInfo]
    permission_tiers: list[PermissionTierInfo]


class VoiceStartCallResult(_Model):
    call_id: str
    call_state: CallState
    voice_state: VoiceState = "idle"
    memory_context: str | None = None


class VoiceEndCallResult(_Model):
    call_id: str | None = None
    call_state: CallState = "idle"
    voice_state: VoiceState = "idle"


class VoiceStatusResult(_Model):
    call_id: str | None = None
    call_state: CallState = "idle"
    voice_state: VoiceState = "idle"
    screen_capture_active: bool = False
    screen_capture_target: ScreenCaptureTarget | None = None


class VoiceDelegateTaskResult(_Model):
    task_id: str
    status: TaskState
    output: str = ""


class VoiceHistoryMessageItem(_Model):
    role: TranscriptRole
    text: str
    timestamp: str = ""
    call_id: str | None = None


class VoiceHistoryResult(_Model):
    session_id: str = "voice_companion"
    messages: list[VoiceHistoryMessageItem] = Field(default_factory=list)


class VoiceConversationListItem(_Model):
    conversation_id: str = ""
    session_id: str = ""
    title: str
    updated_at: str = ""
    turn_count: int = 0
    turns: int = 0
    episode_summary: str = ""
    date: str = ""
    last_time: str = ""
    pinned: bool = False

    @model_validator(mode="after")
    def _sync_fields(self) -> VoiceConversationListItem:
        if not self.conversation_id and self.session_id:
            self.conversation_id = self.session_id
        elif not self.session_id and self.conversation_id:
            self.session_id = self.conversation_id
        if self.turn_count == 0 and self.turns > 0:
            self.turn_count = self.turns
        elif self.turns == 0 and self.turn_count > 0:
            self.turns = self.turn_count
        return self


class VoiceListConversationsResult(_Model):
    conversations: list[VoiceConversationListItem] = Field(default_factory=list)


class ConversationSearchSnippet(_Model):
    role: str
    timestamp: str
    excerpt: str = ""
    snippet: str = ""

    @model_validator(mode="after")
    def _sync_excerpt_and_snippet(self) -> ConversationSearchSnippet:
        if not self.excerpt and self.snippet:
            self.excerpt = self.snippet
        elif not self.snippet and self.excerpt:
            self.snippet = self.excerpt
        return self


class ConversationSearchHit(_Model):
    session_id: str = ""
    conversation_id: str = ""
    title: str
    updated_at: str = ""
    episode_summary: str = ""
    matching_snippets: list[ConversationSearchSnippet] = Field(default_factory=list)
    score: float = 0.0
    semantic_score: float | None = None
    lexical_score: float = 0.0
    role: str = "user"
    timestamp: str = ""
    snippet: str = ""
    source: str = "chat"
    call_id: str | None = None

    @model_validator(mode="after")
    def _sync_ids(self) -> ConversationSearchHit:
        if not self.conversation_id and self.session_id:
            self.conversation_id = self.session_id
        elif not self.session_id and self.conversation_id:
            self.session_id = self.conversation_id
        return self


class VoiceSearchConversationsResult(_Model):
    results: list[ConversationSearchHit] = Field(default_factory=list)


class VoiceConversationTurnItem(_Model):
    role: str
    content: str
    timestamp: str = ""


class VoiceReadConversationResult(_Model):
    found: bool = True
    conversation_id: str = ""
    session_id: str = ""
    title: str = ""
    updated_at: str = ""
    episode_summary: str = ""
    total_turns: int = 0
    truncated: bool = False
    turns: list[VoiceConversationTurnItem] = Field(default_factory=list)
    message: str = ""


class MemorySearchHit(_Model):
    kind: Literal["fact", "episode"]
    item_id: str
    section: str = ""
    date: str = ""
    source: str = ""
    session_id: str = ""
    conversation_id: str = ""
    text: str
    score: float = 0.0
    semantic_score: float | None = None
    lexical_score: float = 0.0


class VoiceSearchMemoryResult(_Model):
    results: list[MemorySearchHit] = Field(default_factory=list)


class VoicePostToActiveChatResult(_Model):
    ok: bool
    session_id: str | None = None
    title: str | None = None
    message: str = ""


class UiNavigateResult(_Model):
    ok: bool = True
    view: UiView = "chat"
    active_conversation_id: str | None = None
    open_command_palette: bool = False


class ChatCreateAndOpenResult(_Model):
    ok: bool = True
    session_id: str
    title: str
    message: str = ""
    open_in_ui: bool = True


class ChatSendFromVoiceResult(_Model):
    ok: bool = True
    session_id: str
    title: str = ""
    request_id: str = ""
    status: str = "started"


class ChatRenameResult(_Model):
    ok: bool = True
    session_id: str
    title: str


class ChatDeleteResult(_Model):
    ok: bool = True
    session_id: str
    deleted: bool = False
    requires_confirmation: bool = False
    episodes_removed: int = 0
    message: str = ""


class MemoryAddFactResult(_Model):
    ok: bool = True
    fact: MemoryFactItem
    section: str


class MemoryDeleteFactResult(_Model):
    ok: bool = True
    removed_count: int = 0
    removed_ids: list[str] = Field(default_factory=list)


class ActivityListResult(_Model):
    tasks: list[TaskStatusData] = Field(default_factory=list)


class ActivityCancelResult(_Model):
    ok: bool = True
    task_id: str
    status: str
    message: str = ""


METHOD_RESULTS: dict[str, type[_Model]] = {
    "health.ping": HealthPingResult,
    "session.new": SessionNewResult,
    "chat.send": ChatSendResult,
    "chat.focus": ChatFocusResult,
    "approval.respond": ApprovalRespondResult,
    "memory.get": MemoryGetResult,
    "memory.get_daily": MemoryGetDailyResult,
    "memory.episodes": MemoryEpisodesResult,
    "memory.facts": MemoryFactsResult,
    "memory.archive": MemoryArchiveResult,
    "memory.status": MemoryStatusResult,
    "memory.reconsolidate": MemoryReconsolidateResult,
    "memory.forget": MemoryForgetResult,
    "transcripts.list": TranscriptsListResult,
    "transcripts.get": TranscriptsGetResult,
    "dev.emit_sample_task": DevEmitSampleTaskResult,
    "conversations.list": ConversationsListResult,
    "conversations.get": ConversationsGetResult,
    "conversation.list": ConversationsListResult,
    "conversation.get": ConversationsGetResult,
    "conversation.resume": ConversationResumeResult,
    "conversation.rename": ConversationRenameResult,
    "conversation.pin": ConversationPinResult,
    "conversation.export": ConversationExportResult,
    "conversation.delete": ConversationDeleteResult,
    "conversation.delete_all": ConversationDeleteAllResult,
    "conversation.undo_delete": ConversationUndoDeleteResult,
    "settings.get": SettingsGetResult,
    "voice.start_call": VoiceStartCallResult,
    "voice.end_call": VoiceEndCallResult,
    "voice.status": VoiceStatusResult,
    "voice.delegate_task": VoiceDelegateTaskResult,
    "voice.history": VoiceHistoryResult,
    "voice.list_conversations": VoiceListConversationsResult,
    "voice.search_conversations": VoiceSearchConversationsResult,
    "voice.read_conversation": VoiceReadConversationResult,
    "voice.search_memory": VoiceSearchMemoryResult,
    "voice.post_to_active_chat": VoicePostToActiveChatResult,
    "ui.navigate": UiNavigateResult,
    "chat.create_and_open": ChatCreateAndOpenResult,
    "chat.send_from_voice": ChatSendFromVoiceResult,
    "chat.rename": ChatRenameResult,
    "chat.delete": ChatDeleteResult,
    "memory.add_fact": MemoryAddFactResult,
    "memory.delete_fact": MemoryDeleteFactResult,
    "activity.list": ActivityListResult,
    "activity.cancel": ActivityCancelResult,
}
"""Result payload model for each request method (carried in ``ResponseFrame.result``)."""


# --------------------------------------------------------------------------- events


class _Event(_Model):
    frame_type: Literal["event"] = "event"
    request_id: str | None = None
    session_id: str | None = None


class AssistantTokenData(_Model):
    text: str


class ToolCallStartedData(_Model):
    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    risk_level: RiskTier


class ToolCallFinishedData(_Model):
    call_id: str
    name: str
    is_error: bool
    output: str
    duration_ms: int = Field(description="Execution time, excluding time spent awaiting approval")


class ApprovalRequestedData(_Model):
    approval_id: str
    call_id: str | None = None
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    risk_level: RiskTier
    timeout_seconds: float


class ApprovalResolvedData(_Model):
    approval_id: str
    decision: ApprovalDecision
    reason: ApprovalReason


class TaskStatusData(_Model):
    task_id: str
    title: str
    status: TaskState
    started_at: str
    kind: TaskKind = "chat_turn"
    finished_at: str | None = None
    next_run_at: str | None = None
    detail: str | None = None


class VoiceStateData(_Model):
    state: VoiceState
    error: str | None = None


class CallStartedData(_Model):
    call_id: str
    call_state: CallState = "active"
    started_at: str | None = None


class CallEndedData(_Model):
    call_id: str
    call_state: CallState = "idle"
    reason: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0.0)


class TranscriptData(_Model):
    role: TranscriptRole
    text: str
    final: bool = False
    call_id: str | None = None
    timestamp: str | None = None


class ScreenCaptureStateData(_Model):
    active: bool
    target: ScreenCaptureTarget | None = None
    call_id: str | None = None


class UsageData(_Model):
    call_id: str | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    thought_tokens: int | None = Field(default=None, ge=0)


class ErrorData(_Model):
    message: str
    code: str | None = None


class AssistantTokenEvent(_Event):
    event: Literal["assistant_token"] = "assistant_token"
    data: AssistantTokenData


class ToolCallStartedEvent(_Event):
    event: Literal["tool_call_started"] = "tool_call_started"
    data: ToolCallStartedData


class ToolCallFinishedEvent(_Event):
    event: Literal["tool_call_finished"] = "tool_call_finished"
    data: ToolCallFinishedData


class ApprovalRequestedEvent(_Event):
    event: Literal["approval_requested"] = "approval_requested"
    data: ApprovalRequestedData


class ApprovalResolvedEvent(_Event):
    event: Literal["approval_resolved"] = "approval_resolved"
    data: ApprovalResolvedData


class TaskStatusEvent(_Event):
    event: Literal["task_status"] = "task_status"
    data: TaskStatusData


class VoiceStateEvent(_Event):
    event: Literal["voice_state"] = "voice_state"
    data: VoiceStateData


class CallStartedEvent(_Event):
    event: Literal["call_started"] = "call_started"
    data: CallStartedData


class CallEndedEvent(_Event):
    event: Literal["call_ended"] = "call_ended"
    data: CallEndedData


class TranscriptEvent(_Event):
    event: Literal["transcript"] = "transcript"
    data: TranscriptData


class ScreenCaptureStateEvent(_Event):
    event: Literal["screen_capture_state"] = "screen_capture_state"
    data: ScreenCaptureStateData


class UsageEvent(_Event):
    event: Literal["usage"] = "usage"
    data: UsageData


class ErrorEvent(_Event):
    event: Literal["error"] = "error"
    data: ErrorData


class ConversationUpdatedData(_Model):
    session_id: str
    title: str | None = None
    pinned: bool | None = None
    updated_at: str | None = None
    user_message: str | None = None
    assistant_message: str | None = None
    turns: int | None = None
    request_id: str | None = None


class ConversationUpdatedEvent(_Event):
    event: Literal["conversation_updated"] = "conversation_updated"
    data: ConversationUpdatedData


class ConversationDeletedData(_Model):
    session_ids: list[str] = Field(default_factory=list)
    all: bool = False


class ConversationDeletedEvent(_Event):
    event: Literal["conversation_deleted"] = "conversation_deleted"
    data: ConversationDeletedData


class UiNavigateData(_Model):
    view: UiView = "chat"
    conversation_id: str | None = None
    open_command_palette: bool = False


class UiNavigateEvent(_Event):
    event: Literal["ui.navigate"] = "ui.navigate"
    data: UiNavigateData


class ConversationCreatedData(_Model):
    session_id: str
    title: str
    started_at: str = ""
    updated_at: str = ""
    turns: int = 0
    initial_message: str | None = None
    open_in_ui: bool = True


class ConversationCreatedEvent(_Event):
    event: Literal["conversation_created"] = "conversation_created"
    data: ConversationCreatedData


class MemoryUpdatedData(_Model):
    action: Literal["added", "deleted", "updated"] = "updated"
    fact_id: str | None = None
    section: str | None = None
    text: str | None = None
    removed_count: int = 0
    removed_ids: list[str] = Field(default_factory=list)


class MemoryUpdatedEvent(_Event):
    event: Literal["memory.updated"] = "memory.updated"
    data: MemoryUpdatedData


EventFrame = Annotated[
    AssistantTokenEvent
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
    | MemoryUpdatedEvent,
    Field(discriminator="event"),
]
"""Any daemon-to-client event (discriminated by ``event``)."""

# ``isinstance`` target for "is this an event frame".
BaseEvent = _Event

EVENT_NAMES: tuple[str, ...] = (
    "assistant_token",
    "tool_call_started",
    "tool_call_finished",
    "approval_requested",
    "approval_resolved",
    "task_status",
    "voice_state",
    "call_started",
    "call_ended",
    "transcript",
    "screen_capture_state",
    "usage",
    "error",
    "conversation_updated",
    "conversation_deleted",
    "ui.navigate",
    "conversation_created",
    "memory.updated",
)


# --------------------------------------------------------------------------- parsing

ClientFrame = RequestFrame
ServerFrame = Annotated[ResponseFrame | EventFrame, Field(discriminator="frame_type")]
ProtocolFrame = Annotated[
    RequestFrame | ResponseFrame | EventFrame, Field(discriminator="frame_type")
]

_FRAME_ADAPTER: TypeAdapter[Any] = TypeAdapter(ProtocolFrame)
CLIENT_ADAPTER: TypeAdapter[Any] = TypeAdapter(ClientFrame)
SERVER_ADAPTER: TypeAdapter[Any] = TypeAdapter(ServerFrame)


def parse_frame(raw: str | bytes) -> Any:
    """Parse and validate a raw JSON WebSocket message into a typed frame."""
    return _FRAME_ADAPTER.validate_json(raw)
