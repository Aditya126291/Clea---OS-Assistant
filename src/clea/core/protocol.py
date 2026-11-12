"""Typed WebSocket protocol shared between the core daemon and surfaces.

This module is the single source of truth for every message on the wire. TypeScript
types for the desktop UI are generated from these models (``scripts/gen_ts_types.py``).

It intentionally has zero dependencies on core internals so that CLI, voice, and
desktop UI surfaces can import it without coupling to the daemon.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

RiskTier = Literal["read_only", "reversible", "destructive"]
ApprovalDecision = Literal["allow", "deny"]
ApprovalReason = Literal["user", "timeout"]
TaskState = Literal["running", "succeeded", "failed"]
VoiceState = Literal["idle", "listening", "thinking", "speaking"]
CallState = Literal["idle", "connecting", "active", "ending"]
TranscriptRole = Literal["user", "assistant"]
ScreenCaptureTarget = Literal["active_window", "full_screen"]
VoiceTrigger = Literal["manual", "wake_word", "hotkey"]


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


class ApprovalRespondParams(_Model):
    approval_id: str
    decision: ApprovalDecision


class DailyParams(_Model):
    date: str = Field(description="ISO date, YYYY-MM-DD")


class ConversationGetParams(_Model):
    date: str = Field(description="ISO date, YYYY-MM-DD")
    session_id: str


class VoiceStartCallParams(_Model):
    trigger: VoiceTrigger = "manual"


class VoiceEndCallParams(_Model):
    call_id: str | None = None
    reason: str | None = None


class VoiceDelegateTaskParams(_Model):
    description: str
    call_id: str | None = None
    tool_call_id: str | None = None


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


class ConversationsListRequest(_Request):
    method: Literal["conversations.list"] = "conversations.list"
    params: _EmptyParams = Field(default_factory=_EmptyParams)


class ConversationsGetRequest(_Request):
    method: Literal["conversations.get"] = "conversations.get"
    params: ConversationGetParams


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


RequestFrame = Annotated[
    HealthPingRequest
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
    | VoiceDelegateTaskRequest,
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


class ApprovalRespondResult(_Model):
    approval_id: str
    decision: ApprovalDecision


class MemoryGetResult(_Model):
    long_term: str
    days: list[str] = Field(description="ISO dates with a daily log, newest first")


class MemoryGetDailyResult(_Model):
    date: str
    content: str


class ConversationSummary(_Model):
    date: str
    session_id: str
    title: str
    turns: int
    last_time: str


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


METHOD_RESULTS: dict[str, type[_Model]] = {
    "health.ping": HealthPingResult,
    "session.new": SessionNewResult,
    "chat.send": ChatSendResult,
    "approval.respond": ApprovalRespondResult,
    "memory.get": MemoryGetResult,
    "memory.get_daily": MemoryGetDailyResult,
    "conversations.list": ConversationsListResult,
    "conversations.get": ConversationsGetResult,
    "settings.get": SettingsGetResult,
    "voice.start_call": VoiceStartCallResult,
    "voice.end_call": VoiceEndCallResult,
    "voice.status": VoiceStatusResult,
    "voice.delegate_task": VoiceDelegateTaskResult,
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
    finished_at: str | None = None
    detail: str | None = None


class VoiceStateData(_Model):
    state: VoiceState


class CallStartedData(_Model):
    call_id: str
    call_state: CallState = "active"
    started_at: str | None = None


class CallEndedData(_Model):
    call_id: str
    call_state: CallState = "idle"
    reason: str | None = None
    duration_seconds: float | None = None


class TranscriptData(_Model):
    role: TranscriptRole
    text: str
    final: bool = False
    call_id: str | None = None


class ScreenCaptureStateData(_Model):
    active: bool
    target: ScreenCaptureTarget | None = None
    call_id: str | None = None


class UsageData(_Model):
    call_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int | None = None
    thought_tokens: int | None = None


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
    | ErrorEvent,
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
