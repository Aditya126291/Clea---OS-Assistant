"""Tests for the typed WebSocket protocol."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from clea.core.protocol import (
    EVENT_NAMES,
    METHOD_RESULTS,
    ApprovalRequestedData,
    ApprovalRequestedEvent,
    ApprovalRespondRequest,
    AssistantTokenData,
    AssistantTokenEvent,
    BaseEvent,
    BaseRequest,
    CallEndedData,
    CallEndedEvent,
    CallStartedData,
    CallStartedEvent,
    ChatSendParams,
    ChatSendRequest,
    DevEmitSampleTaskRequest,
    MemoryEpisodesRequest,
    MemoryForgetRequest,
    OsControlMediaVolumeParams,
    OsControlMediaVolumeRequest,
    OsOpenOrFocusAppParams,
    OsOpenOrFocusAppRequest,
    OsReadClipboardParams,
    OsReadClipboardRequest,
    OsWriteClipboardParams,
    OsWriteClipboardRequest,
    ResponseFrame,
    ScreenCaptureStateData,
    ScreenCaptureStateEvent,
    TaskStatusEvent,
    TranscriptData,
    TranscriptEvent,
    TranscriptsGetRequest,
    TranscriptsListRequest,
    UsageData,
    UsageEvent,
    VoiceDelegateTaskParams,
    VoiceDelegateTaskRequest,
    VoiceEndCallParams,
    VoiceEndCallRequest,
    VoiceStartCallParams,
    VoiceStartCallRequest,
    VoiceStateData,
    VoiceStateEvent,
    VoiceStatusRequest,
    WorkspaceGitInspectParams,
    WorkspaceGitInspectRequest,
    parse_frame,
)
from clea.tools import RiskLevel


def test_request_frame_roundtrip() -> None:
    req = ChatSendRequest(session_id="sess-1", params=ChatSendParams(text="hello"))
    parsed = parse_frame(req.model_dump_json())
    assert isinstance(parsed, ChatSendRequest)
    assert isinstance(parsed, BaseRequest)
    assert parsed.method == "chat.send"
    assert parsed.session_id == "sess-1"
    assert parsed.params.text == "hello"


def test_methods_without_params_parse() -> None:
    parsed = parse_frame('{"frame_type": "request", "method": "health.ping"}')
    assert parsed.method == "health.ping"


def test_approval_response_request_validates_decision() -> None:
    raw = {
        "frame_type": "request",
        "method": "approval.respond",
        "params": {"approval_id": "a1", "decision": "allow"},
    }
    parsed = parse_frame(json.dumps(raw))
    assert isinstance(parsed, ApprovalRespondRequest)
    assert parsed.params.decision == "allow"

    raw["params"]["decision"] = "maybe"
    with pytest.raises(ValidationError):
        parse_frame(json.dumps(raw))


def test_response_frame_roundtrip() -> None:
    resp = ResponseFrame(id="req-1", ok=True, session_id="sess-1", result={"text": "hi"})
    parsed = parse_frame(resp.model_dump_json())
    assert isinstance(parsed, ResponseFrame)
    assert parsed.id == "req-1"
    assert parsed.result == {"text": "hi"}


def test_event_frame_roundtrip() -> None:
    event = AssistantTokenEvent(
        request_id="req-1", session_id="sess-1", data=AssistantTokenData(text="hel")
    )
    parsed = parse_frame(event.model_dump_json())
    assert isinstance(parsed, AssistantTokenEvent)
    assert isinstance(parsed, BaseEvent)
    assert parsed.data.text == "hel"


def test_all_required_event_types_exist_and_roundtrip() -> None:
    required = {
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
        "conversation_created",
        "ui.navigate",
        "memory.updated",
    }
    assert set(EVENT_NAMES) == required


@pytest.mark.parametrize("state", ["idle", "listening", "thinking", "speaking", "reconnecting"])
def test_voice_state_values(state: str) -> None:
    parsed = parse_frame(VoiceStateEvent(data=VoiceStateData(state=state)).model_dump_json())
    assert parsed.data.state == state


def test_voice_state_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        VoiceStateData(state="dancing")  # type: ignore[arg-type]


def test_approval_requested_carries_risk_tier() -> None:
    event = ApprovalRequestedEvent(
        data=ApprovalRequestedData(
            approval_id="a1",
            tool="wipe",
            arguments={"p": 1},
            risk_level="destructive",
            timeout_seconds=30,
        )
    )
    assert parse_frame(event.model_dump_json()).data.risk_level == "destructive"


def test_invalid_frames_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_frame('{"frame_type": "unknown", "id": "1"}')
    with pytest.raises(ValidationError):
        parse_frame('{"frame_type": "request", "method": "unsupported.method"}')
    with pytest.raises(ValidationError):
        parse_frame(
            '{"frame_type": "request", "method": "chat.send", "params": {"text": "x", "z": 1}}'
        )


def test_every_method_has_a_result_model() -> None:
    methods = {
        parse_frame(json.dumps({"frame_type": "request", "method": m, "params": p})).method
        for m, p in [
            ("health.ping", {}),
            ("session.new", {}),
            ("chat.send", {"text": "x"}),
            ("approval.respond", {"approval_id": "a", "decision": "deny"}),
            ("memory.get", {}),
            ("memory.get_daily", {"date": "2026-01-01"}),
            ("conversations.list", {}),
            ("conversations.get", {"date": "2026-01-01", "session_id": "s"}),
            ("conversation.list", {}),
            ("conversation.get", {"session_id": "s"}),
            ("conversation.resume", {"session_id": "s"}),
            ("conversation.rename", {"session_id": "s", "title": "New Title"}),
            ("conversation.pin", {"session_id": "s", "pinned": True}),
            ("conversation.export", {"session_id": "s"}),
            ("conversation.delete", {"session_id": "s"}),
            ("conversation.delete_all", {"confirmation": "DELETE"}),
            ("conversation.undo_delete", {}),
            ("settings.get", {}),
            ("memory.episodes", {}),
            ("memory.facts", {}),
            ("memory.archive", {}),
            ("memory.status", {}),
            ("memory.reconsolidate", {"session_id": "s1"}),
            ("memory.forget", {"id": "x"}),
            ("transcripts.list", {}),
            ("transcripts.get", {"session_id": "s1"}),
            ("dev.emit_sample_task", {}),
            ("voice.start_call", {}),
            ("voice.end_call", {}),
            ("voice.status", {}),
            ("voice.delegate_task", {"description": "check calendar"}),
            ("chat.focus", {}),
            ("voice.history", {}),
            ("voice.list_conversations", {}),
            ("voice.search_conversations", {"query": "search query"}),
            ("voice.read_conversation", {"conversation_id": "s1"}),
            ("voice.search_memory", {"query": "search query"}),
            ("voice.post_to_active_chat", {"content": "sample content"}),
            ("ui.navigate", {"view": "chat"}),
            ("chat.create_and_open", {"title": "New Chat"}),
            ("chat.send_from_voice", {"prompt": "Hello"}),
            ("chat.rename", {"conversation_id": "s1", "new_title": "Renamed"}),
            ("chat.delete", {"conversation_id": "s1", "confirmed": False}),
            ("memory.add_fact", {"fact": "User likes Python"}),
            ("memory.delete_fact", {"query_or_id": "fact-1"}),
            ("activity.list", {}),
            ("activity.cancel", {"task_id": "task-1"}),
            ("os.open_or_focus_app", {"app_name": "vscode"}),
            ("os.control_media_volume", {"action": "mute"}),
            ("os.read_clipboard", {}),
            ("os.write_clipboard", {"text": "hello"}),
            ("workspace.git_inspect", {}),
        ]
    }
    assert methods == set(METHOD_RESULTS)


def test_windows_os_and_git_protocol_roundtrip() -> None:
    frames = [
        OsOpenOrFocusAppRequest(params=OsOpenOrFocusAppParams(app_name="vscode")),
        OsControlMediaVolumeRequest(
            params=OsControlMediaVolumeParams(action="set_volume", level=42)
        ),
        OsReadClipboardRequest(params=OsReadClipboardParams(max_chars=512)),
        OsWriteClipboardRequest(params=OsWriteClipboardParams(text="copied snippet")),
        WorkspaceGitInspectRequest(
            params=WorkspaceGitInspectParams(include_diff=True, diff_stat_only=False)
        ),
    ]
    for frame in frames:
        parsed = parse_frame(frame.model_dump_json())
        assert type(parsed) is type(frame)

    with pytest.raises(ValidationError):
        OsControlMediaVolumeParams(action="invalid_action")  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        OsControlMediaVolumeParams(action="set_volume", level=101)

    with pytest.raises(ValidationError):
        OsReadClipboardParams(max_chars=0)


def test_risk_tiers_match_registry_risk_levels() -> None:
    from typing import get_args

    from clea.core.protocol import RiskTier

    assert set(get_args(RiskTier)) == {level.value for level in RiskLevel}


@pytest.mark.parametrize("call_state", ["idle", "connecting", "active", "ending"])
def test_call_state_values_roundtrip_on_call_events(call_state: str) -> None:
    started = parse_frame(
        CallStartedEvent(
            data=CallStartedData(call_id="call-1", call_state=call_state)  # type: ignore[arg-type]
        ).model_dump_json()
    )
    assert isinstance(started, CallStartedEvent)
    assert started.data.call_state == call_state

    ended = parse_frame(
        CallEndedEvent(
            data=CallEndedData(call_id="call-1", call_state=call_state, reason="hangup")  # type: ignore[arg-type]
        ).model_dump_json()
    )
    assert isinstance(ended, CallEndedEvent)
    assert ended.data.call_state == call_state
    assert ended.data.reason == "hangup"


def test_call_state_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        CallStartedData(call_id="call-1", call_state="paused")  # type: ignore[arg-type]


def test_voice_events_roundtrip_and_validation() -> None:
    tr = parse_frame(
        TranscriptEvent(
            data=TranscriptData(role="user", text="Kya haal hai?", final=True, call_id="c1")
        ).model_dump_json()
    )
    assert isinstance(tr, TranscriptEvent)
    assert tr.data.role == "user"
    assert tr.data.final is True

    with pytest.raises(ValidationError):
        TranscriptData(role="system", text="nope")  # type: ignore[arg-type]

    sc = parse_frame(
        ScreenCaptureStateEvent(
            data=ScreenCaptureStateData(active=True, target="active_window", call_id="c1")
        ).model_dump_json()
    )
    assert isinstance(sc, ScreenCaptureStateEvent)
    assert sc.data.active is True
    assert sc.data.target == "active_window"

    with pytest.raises(ValidationError):
        ScreenCaptureStateData(active=True, target="webcam")  # type: ignore[arg-type]

    usage = parse_frame(
        UsageEvent(
            data=UsageData(
                call_id="c1",
                input_tokens=120,
                output_tokens=45,
                total_tokens=165,
                cached_tokens=20,
            )
        ).model_dump_json()
    )
    assert isinstance(usage, UsageEvent)
    assert usage.data.total_tokens == 165
    assert usage.data.cached_tokens == 20

    with pytest.raises(ValidationError):
        UsageData(input_tokens=-1)
    with pytest.raises(ValidationError):
        CallEndedData(call_id="c1", duration_seconds=-0.5)


def test_voice_requests_roundtrip() -> None:
    start_req = parse_frame(
        VoiceStartCallRequest(params=VoiceStartCallParams(trigger="hotkey")).model_dump_json()
    )
    assert isinstance(start_req, VoiceStartCallRequest)
    assert start_req.params.trigger == "hotkey"

    end_req = parse_frame(
        VoiceEndCallRequest(
            params=VoiceEndCallParams(call_id="c1", reason="user_hangup")
        ).model_dump_json()
    )
    assert isinstance(end_req, VoiceEndCallRequest)
    assert end_req.params.call_id == "c1"

    status_req = parse_frame(VoiceStatusRequest().model_dump_json())
    assert isinstance(status_req, VoiceStatusRequest)

    delegate_req = parse_frame(
        VoiceDelegateTaskRequest(
            params=VoiceDelegateTaskParams(
                description="Summarize desktop files",
                call_id="c1",
                tool_call_id="tc-1",
            )
        ).model_dump_json()
    )
    assert isinstance(delegate_req, VoiceDelegateTaskRequest)
    assert delegate_req.params.description == "Summarize desktop files"
    assert delegate_req.params.tool_call_id == "tc-1"

    with pytest.raises(ValidationError):
        parse_frame('{"frame_type": "request", "method": "voice.delegate_task", "params": {}}')


def test_task_status_kinds_and_fields() -> None:
    frame = parse_frame(
        json.dumps(
            {
                "frame_type": "event",
                "event": "task_status",
                "data": {
                    "task_id": "t1",
                    "kind": "background_task",
                    "title": "Summarising session 18:11",
                    "status": "running",
                    "started_at": "2026-11-13T18:11:00Z",
                    "next_run_at": "2026-11-13T18:26:00Z",
                },
            }
        )
    )
    assert isinstance(frame, TaskStatusEvent)
    assert frame.data.kind == "background_task"
    assert frame.data.title == "Summarising session 18:11"
    assert frame.data.next_run_at == "2026-11-13T18:26:00Z"

    # Default kind is chat_turn
    legacy_frame = parse_frame(
        json.dumps(
            {
                "frame_type": "event",
                "event": "task_status",
                "data": {
                    "task_id": "t2",
                    "title": "Turn",
                    "started_at": "2026-11-13T10:00:00Z",
                    "status": "succeeded",
                },
            }
        )
    )
    assert isinstance(legacy_frame, TaskStatusEvent)
    assert legacy_frame.data.kind == "chat_turn"


def test_new_memory_and_transcript_requests() -> None:
    ep_req = parse_frame(
        '{"frame_type": "request", "method": "memory.episodes", "params": {"limit": 3}}'
    )
    assert isinstance(ep_req, MemoryEpisodesRequest)
    assert ep_req.params.limit == 3

    tr_list = parse_frame('{"frame_type": "request", "method": "transcripts.list", "params": {}}')
    assert isinstance(tr_list, TranscriptsListRequest)

    tr_get = parse_frame(
        '{"frame_type": "request", "method": "transcripts.get", "params": {"session_id": "abc"}}'
    )
    assert isinstance(tr_get, TranscriptsGetRequest)
    assert tr_get.params.session_id == "abc"

    dev_req = parse_frame(
        '{"frame_type": "request", "method": "dev.emit_sample_task", "params": {}}'
    )
    assert isinstance(dev_req, DevEmitSampleTaskRequest)


def test_memory_forget_request_and_result() -> None:
    raw = (
        '{"frame_type": "request", "method": "memory.forget", '
        '"params": {"id": "pref-1", "query": "dark mode"}}'
    )
    req = parse_frame(raw)
    assert isinstance(req, MemoryForgetRequest)
    assert req.params.id == "pref-1"
    assert req.params.query == "dark mode"

    assert "memory.forget" in METHOD_RESULTS
    res = METHOD_RESULTS["memory.forget"](ok=True, removed_count=1, removed_ids=["pref-1"])
    assert res.ok is True
    assert res.removed_count == 1
    assert res.removed_ids == ["pref-1"]
