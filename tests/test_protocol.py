"""Tests for the typed WebSocket protocol."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
    ClipboardItem,
    DevEmitSampleTaskRequest,
    EpisodicSessionHit,
    MemoryBankFactItem,
    MemoryEpisodesRequest,
    MemoryForgetRequest,
    MemoryListFactsParams,
    MemoryListFactsRequest,
    MemoryListFactsResult,
    MemoryManageFactParams,
    MemoryManageFactRequest,
    MemoryManageFactResult,
    MemorySearchEpisodicParams,
    MemorySearchEpisodicRequest,
    MemorySearchEpisodicResult,
    OsClickUiElementParams,
    OsClickUiElementRequest,
    OsControlMediaVolumeParams,
    OsControlMediaVolumeRequest,
    OsControlSystemParams,
    OsControlSystemRequest,
    OsControlSystemResult,
    OsGuiActionParams,
    OsGuiActionRequest,
    OsInspectUiElementsParams,
    OsInspectUiElementsRequest,
    OsOpenOrFocusAppParams,
    OsOpenOrFocusAppRequest,
    OsOpenUrlParams,
    OsOpenUrlRequest,
    OsOpenUrlResult,
    OsReadClipboardParams,
    OsReadClipboardRequest,
    OsReadClipboardResult,
    OsSelectClipboardItemParams,
    OsSelectClipboardItemRequest,
    OsSelectClipboardItemResult,
    OsUiActParams,
    OsUiActRequest,
    OsUiBatchParams,
    OsUiBatchRequest,
    OsUiSnapshotParams,
    OsUiSnapshotRequest,
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
    UiBatchStep,
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
            ("memory.search_episodic", {"query": "test"}),
            ("memory.manage_fact", {"action": "add", "fact_text": "prefers uv"}),
            ("memory.list_facts", {}),
            ("activity.list", {}),
            ("activity.cancel", {"task_id": "task-1"}),
            ("os.open_or_focus_app", {"app_name": "vscode"}),
            ("os.control_media_volume", {"action": "mute"}),
            ("os.read_clipboard", {}),
            ("os.select_clipboard_item", {"index": 0}),
            ("os.open_url", {"url": "https://example.com"}),
            ("os.control_system", {"action": "lock"}),
            ("os.write_clipboard", {"text": "hello"}),
            ("os.transform_clipboard", {"action": "format_json"}),
            ("os.save_clipboard_to_file", {}),
            ("os.open_path_or_url", {}),
            ("os.inspect_screen", {}),
            ("os.inspect_ui_elements", {}),
            ("os.click_ui_element", {"name": "Equals"}),
            ("os.gui_action", {"action": "wait", "seconds": 0.5}),
            ("os.ui_snapshot", {}),
            ("os.ui_act", {"ref": "e0", "action": "click"}),
            (
                "os.ui_batch",
                {"steps": [{"op": "act", "ref": "e0", "action": "click"}]},
            ),
            ("os.cancel_inflight", {}),
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
        OsReadClipboardRequest(
            params=OsReadClipboardParams(
                max_chars=512,
                include_history=True,
                max_items=5,
                include_image_bytes=True,
            )
        ),
        OsSelectClipboardItemRequest(params=OsSelectClipboardItemParams(index=2)),
        OsOpenUrlRequest(params=OsOpenUrlParams(url="https://example.com", browser="chrome")),
        OsControlSystemRequest(
            params=OsControlSystemParams(action="set_brightness", level=75, step=5)
        ),
        OsWriteClipboardRequest(params=OsWriteClipboardParams(text="copied snippet")),
        OsInspectUiElementsRequest(
            params=OsInspectUiElementsParams(app_name="Calculator", max_elements=45)
        ),
        OsClickUiElementRequest(
            params=OsClickUiElementParams(
                name="Equals", control_type="Button", app_name="Calculator"
            )
        ),
        OsGuiActionRequest(params=OsGuiActionParams(action="press_keys", keys=["ctrl", "l"])),
        OsUiSnapshotRequest(params=OsUiSnapshotParams(app_name="Calculator", max_elements=30)),
        OsUiActRequest(params=OsUiActParams(ref="e2", action="set_value", value="42")),
        OsUiBatchRequest(
            params=OsUiBatchParams(
                steps=[
                    UiBatchStep(op="act", ref="e1", action="click"),
                    UiBatchStep(op="wait", seconds=0.2),
                ],
                stop_on_error=True,
                stop_on_window_change=True,
            )
        ),
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

    with pytest.raises(ValidationError):
        OsReadClipboardParams(max_items=0)

    with pytest.raises(ValidationError):
        OsReadClipboardParams(max_items=101)

    with pytest.raises(ValidationError):
        OsSelectClipboardItemParams(index=-1)

    with pytest.raises(ValidationError):
        OsControlSystemParams(action="set_brightness", level=101)

    with pytest.raises(ValidationError):
        OsControlSystemParams(action="set_brightness", level=-1)


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


def test_clipboard_history_protocol_models() -> None:
    # Default params
    params_default = OsReadClipboardParams()
    assert params_default.max_chars == 8000
    assert params_default.include_history is False
    assert params_default.max_items == 10
    assert params_default.include_image_bytes is False

    # Custom params
    req = OsReadClipboardRequest(
        params=OsReadClipboardParams(
            max_chars=4000,
            include_history=True,
            max_items=25,
            include_image_bytes=True,
        )
    )
    raw = req.model_dump_json()
    parsed = parse_frame(raw)
    assert isinstance(parsed, OsReadClipboardRequest)
    assert parsed.params.include_history is True
    assert parsed.params.max_items == 25
    assert parsed.params.include_image_bytes is True

    # Result with history and image item
    item = ClipboardItem(
        index=0,
        id="hist-001",
        timestamp="2026-11-15T10:00:00Z",
        kind="image",
        text="Screenshot preview",
        width=1920,
        height=1080,
        format="png",
        image_bytes_base64="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
        size_bytes=68,
    )
    result = OsReadClipboardResult(
        ok=True,
        text="",
        history_enabled=True,
        total_items=1,
        items=[item],
    )
    result_raw = result.model_dump_json()
    parsed_result = OsReadClipboardResult.model_validate_json(result_raw)
    assert parsed_result.history_enabled is True
    assert parsed_result.total_items == 1
    assert len(parsed_result.items) == 1
    assert parsed_result.items[0].kind == "image"
    assert parsed_result.items[0].width == 1920
    assert parsed_result.items[0].size_bytes == 68

    # Also test image item with text=None and image dictionary
    item_none_text = ClipboardItem(
        index=1,
        id="hist-002",
        kind="image",
        text=None,
        image={"width": 100, "height": 100, "format": "png", "bytes": "abc"},
    )
    assert item_none_text.text is None or item_none_text.text == ""
    assert item_none_text.image == {"width": 100, "height": 100, "format": "png", "bytes": "abc"}

    # Also test text item with image=None
    item_text_none_img = ClipboardItem(
        index=2,
        id="hist-003",
        kind="text",
        text="plain text",
        image=None,
    )
    assert item_text_none_img.image is None
    assert item_text_none_img.text == "plain text"


def test_select_clipboard_item_protocol_models() -> None:
    req = OsSelectClipboardItemRequest(params=OsSelectClipboardItemParams(index=4))
    raw = req.model_dump_json()
    parsed = parse_frame(raw)
    assert isinstance(parsed, OsSelectClipboardItemRequest)
    assert parsed.method == "os.select_clipboard_item"
    assert parsed.params.index == 4

    # Validation
    with pytest.raises(ValidationError):
        OsSelectClipboardItemParams(index=-1)

    # Result model
    assert "os.select_clipboard_item" in METHOD_RESULTS
    res = METHOD_RESULTS["os.select_clipboard_item"](
        ok=True, success=True, status="selected", index=4, item_id="hist-004"
    )
    assert isinstance(res, OsSelectClipboardItemResult)
    assert res.ok is True
    assert res.success is True
    assert res.status == "selected"
    assert res.index == 4


def test_open_url_protocol_models() -> None:
    req = OsOpenUrlRequest(
        params=OsOpenUrlParams(url="https://github.com/project", browser="chrome")
    )
    raw = req.model_dump_json()
    parsed = parse_frame(raw)
    assert isinstance(parsed, OsOpenUrlRequest)
    assert parsed.method == "os.open_url"
    assert parsed.params.url == "https://github.com/project"
    assert parsed.params.browser == "chrome"

    # Result model
    assert "os.open_url" in METHOD_RESULTS
    res = METHOD_RESULTS["os.open_url"](
        ok=True,
        success=True,
        status="opened",
        url="https://github.com/project",
        browser="chrome",
    )
    assert isinstance(res, OsOpenUrlResult)
    assert res.ok is True
    assert res.url == "https://github.com/project"
    assert res.browser == "chrome"


def test_control_system_protocol_models() -> None:
    # Brightness control
    req_bright = OsControlSystemRequest(
        params=OsControlSystemParams(action="set_brightness", level=80, step=10)
    )
    parsed_bright = parse_frame(req_bright.model_dump_json())
    assert isinstance(parsed_bright, OsControlSystemRequest)
    assert parsed_bright.params.action == "set_brightness"
    assert parsed_bright.params.level == 80

    # Window control
    req_win = OsControlSystemRequest(
        params=OsControlSystemParams(
            action="minimize_window",
            window_action="minimize",
            window_title="Visual Studio Code",
            hwnd=65538,
        )
    )
    parsed_win = parse_frame(req_win.model_dump_json())
    assert isinstance(parsed_win, OsControlSystemRequest)
    assert parsed_win.params.hwnd == 65538

    # Title alias
    req_title_alias = OsControlSystemRequest(
        params=OsControlSystemParams(action="minimize", title="Notepad")
    )
    assert req_title_alias.params.title == "Notepad"

    # Screen lock
    req_lock = OsControlSystemRequest(params=OsControlSystemParams(action="lock"))
    parsed_lock = parse_frame(req_lock.model_dump_json())
    assert isinstance(parsed_lock, OsControlSystemRequest)
    assert parsed_lock.params.action == "lock"

    # Settings navigation
    req_set = OsControlSystemRequest(
        params=OsControlSystemParams(
            action="open_settings", page="display", uri="ms-settings:display"
        )
    )
    parsed_set = parse_frame(req_set.model_dump_json())
    assert isinstance(parsed_set, OsControlSystemRequest)
    assert parsed_set.params.page == "display"

    # Validation
    with pytest.raises(ValidationError):
        OsControlSystemParams(action="set_brightness", level=101)

    with pytest.raises(ValidationError):
        OsControlSystemParams(action="set_brightness", level=-1)

    # Result model
    assert "os.control_system" in METHOD_RESULTS
    res = METHOD_RESULTS["os.control_system"](
        ok=True,
        success=True,
        action="set_brightness",
        status="success",
        level=80,
        current_brightness=80,
    )
    assert isinstance(res, OsControlSystemResult)
    assert res.ok is True
    assert res.action == "set_brightness"
    assert res.level == 80


@pytest.mark.asyncio
async def test_daemon_windows_os_rpc_handlers(tmp_path: Path) -> None:
    from clea.core.daemon import CleaDaemon, _Connection

    mock_agent = MagicMock()
    mock_agent.memory.base_dir = tmp_path
    mock_agent.memory.resolve_session_id.side_effect = lambda sid: sid
    mock_agent.provider.provider_name = "test"
    mock_agent.provider.model_name = "test-model"
    mock_agent.registry.list_definitions.return_value = []

    daemon = CleaDaemon(
        agent=mock_agent,
        token="test-token",
        token_file=tmp_path / "token",
        lock_file=tmp_path / "lock",
    )

    ws = MagicMock()
    ws.send = AsyncMock()
    conn = _Connection(ws)

    # 1. os.read_clipboard
    clipboard_mock_data = {
        "ok": True,
        "text": "test content",
        "truncated": False,
        "char_count": 12,
        "total_chars": 12,
        "history_enabled": True,
        "total_items": 1,
        "items": [
            {
                "index": 0,
                "id": "item-1",
                "timestamp": "2026-11-15T10:00:00Z",
                "kind": "text",
                "text": "test content",
            }
        ],
    }
    with patch("clea.core.daemon.read_clipboard", return_value=clipboard_mock_data) as mock_read:
        req = OsReadClipboardRequest(
            params=OsReadClipboardParams(
                max_chars=4000,
                include_history=True,
                max_items=5,
                include_image_bytes=True,
            )
        )
        await daemon._dispatch_request(conn, req)
        mock_read.assert_called_once_with(
            max_chars=4000,
            include_history=True,
            max_items=5,
            include_image_bytes=True,
        )
        payload = json.loads(ws.send.call_args[0][0])
        assert payload["id"] == req.id
        assert payload["ok"] is True
        assert payload["result"]["history_enabled"] is True
        assert payload["result"]["total_items"] == 1
        assert len(payload["result"]["items"]) == 1

    # 2. os.select_clipboard_item
    select_mock_data = {
        "ok": True,
        "success": True,
        "status": "selected",
        "index": 3,
        "item_id": "hist-3",
    }
    with patch("clea.core.daemon.select_clipboard_item", return_value=select_mock_data) as mock_sel:
        req_sel = OsSelectClipboardItemRequest(params=OsSelectClipboardItemParams(index=3))
        await daemon._dispatch_request(conn, req_sel)
        mock_sel.assert_called_once_with(3)
        payload_sel = json.loads(ws.send.call_args[0][0])
        assert payload_sel["id"] == req_sel.id
        assert payload_sel["ok"] is True
        assert payload_sel["result"]["success"] is True
        assert payload_sel["result"]["index"] == 3

    # 3. os.open_url
    url_mock_data = {
        "ok": True,
        "success": True,
        "status": "opened",
        "url": "https://example.org",
        "browser": "edge",
    }
    with patch("clea.core.daemon.open_url", return_value=url_mock_data) as mock_url:
        req_url = OsOpenUrlRequest(
            params=OsOpenUrlParams(url="https://example.org", browser="edge")
        )
        await daemon._dispatch_request(conn, req_url)
        mock_url.assert_called_once_with("https://example.org", "edge")
        payload_url = json.loads(ws.send.call_args[0][0])
        assert payload_url["id"] == req_url.id
        assert payload_url["ok"] is True
        assert payload_url["result"]["url"] == "https://example.org"
        assert payload_url["result"]["browser"] == "edge"

    # 4. os.control_system
    ctrl_mock_data = {
        "ok": True,
        "success": True,
        "action": "set_brightness",
        "status": "success",
        "level": 90,
    }
    with patch("clea.core.daemon.control_system", return_value=ctrl_mock_data) as mock_ctrl:
        req_ctrl = OsControlSystemRequest(
            params=OsControlSystemParams(action="set_brightness", level=90)
        )
        await daemon._dispatch_request(conn, req_ctrl)
        mock_ctrl.assert_called_once_with("set_brightness", level=90)
        payload_ctrl = json.loads(ws.send.call_args[0][0])
        assert payload_ctrl["id"] == req_ctrl.id
        assert payload_ctrl["ok"] is True
        assert payload_ctrl["result"]["action"] == "set_brightness"
        assert payload_ctrl["result"]["level"] == 90

    # 5. os.inspect_ui_elements
    inspect_mock_data = {
        "ok": True,
        "window_title": "Calculator",
        "count": 1,
        "elements": [
            {
                "index": 0,
                "name": "One",
                "automation_id": "num1Button",
                "control_type": "Button",
                "value": None,
                "x": 120,
                "y": 400,
            }
        ],
    }
    with patch("clea.core.daemon.inspect_ui_elements", return_value=inspect_mock_data) as m_insp:
        req_insp = OsInspectUiElementsRequest(
            params=OsInspectUiElementsParams(app_name="Calculator", max_elements=30)
        )
        await daemon._dispatch_request(conn, req_insp)
        m_insp.assert_called_once_with("Calculator", 30)
        payload_insp = json.loads(ws.send.call_args[0][0])
        assert payload_insp["id"] == req_insp.id
        assert payload_insp["ok"] is True
        assert payload_insp["result"]["window_title"] == "Calculator"
        assert payload_insp["result"]["elements"][0]["name"] == "One"

    # 6. os.click_ui_element
    click_mock_data = {
        "ok": True,
        "status": "clicked",
        "name": "One",
        "control_type": "Button",
        "x": 120,
        "y": 400,
        "method": "invoke",
    }
    with patch("clea.core.daemon.click_ui_element", return_value=click_mock_data) as m_clk:
        req_clk = OsClickUiElementRequest(
            params=OsClickUiElementParams(name="One", control_type="Button", app_name="Calculator")
        )
        await daemon._dispatch_request(conn, req_clk)
        m_clk.assert_called_once_with("One", "Button", "Calculator")
        payload_clk = json.loads(ws.send.call_args[0][0])
        assert payload_clk["id"] == req_clk.id
        assert payload_clk["ok"] is True
        assert payload_clk["result"]["status"] == "clicked"

    # 7. os.gui_action
    gui_mock_data = {
        "ok": True,
        "status": "completed",
        "action": "type_text",
        "char_count": 3,
    }
    with patch("clea.core.daemon.gui_action", return_value=gui_mock_data) as m_gui:
        req_gui = OsGuiActionRequest(params=OsGuiActionParams(action="type_text", text="10!"))
        await daemon._dispatch_request(conn, req_gui)
        m_gui.assert_called_once_with("type_text", None, None, "10!", None, 1, "left", 0, 1.0)
        payload_gui = json.loads(ws.send.call_args[0][0])
        assert payload_gui["id"] == req_gui.id
        assert payload_gui["ok"] is True
        assert payload_gui["result"]["char_count"] == 3


@pytest.mark.asyncio
async def test_daemon_windows_os_rpc_error_handling(tmp_path: Path) -> None:
    from clea.core.daemon import CleaDaemon, _Connection

    mock_agent = MagicMock()
    mock_agent.memory.base_dir = tmp_path
    mock_agent.memory.resolve_session_id.side_effect = lambda sid: sid
    mock_agent.provider.provider_name = "test"
    mock_agent.provider.model_name = "test-model"
    mock_agent.registry.list_definitions.return_value = []

    daemon = CleaDaemon(
        agent=mock_agent,
        token="test-token",
        token_file=tmp_path / "token",
        lock_file=tmp_path / "lock",
    )

    ws = MagicMock()
    ws.send = AsyncMock()
    conn = _Connection(ws)

    # Tool raises ValueError
    with patch(
        "clea.core.daemon.open_url",
        side_effect=ValueError("Invalid URL scheme 'ftp': only http/https allowed."),
    ):
        req = OsOpenUrlRequest(params=OsOpenUrlParams(url="ftp://malicious.com"))
        await daemon._dispatch_request(conn, req)
        payload = json.loads(ws.send.call_args[0][0])
        assert payload["id"] == req.id
        assert payload["ok"] is False
        assert "Invalid URL scheme 'ftp'" in payload["error"]

    # Tool raises TypeError (e.g. unexpected keyword argument)
    with patch(
        "clea.core.daemon.control_system",
        side_effect=TypeError("control_system() got an unexpected keyword argument 'unknown_arg'"),
    ):
        req_ctrl = OsControlSystemRequest(
            params=OsControlSystemParams(action="minimize", window_title="Notepad")
        )
        await daemon._dispatch_request(conn, req_ctrl)
        payload_ctrl = json.loads(ws.send.call_args[0][0])
        assert payload_ctrl["id"] == req_ctrl.id
        assert payload_ctrl["ok"] is False
        assert "unexpected keyword argument" in payload_ctrl["error"]

    # UIA TimeoutError returns clean JSON-RPC error frame
    with patch(
        "clea.core.daemon.inspect_ui_elements",
        side_effect=TimeoutError("UI Automation inspection timed out after 5.0s."),
    ):
        req_insp = OsInspectUiElementsRequest(params=OsInspectUiElementsParams(app_name="Calc"))
        await daemon._dispatch_request(conn, req_insp)
        payload_insp = json.loads(ws.send.call_args[0][0])
        assert payload_insp["id"] == req_insp.id
        assert payload_insp["ok"] is False
        assert "timed out" in payload_insp["error"]

    with patch(
        "clea.core.daemon.click_ui_element",
        side_effect=TimeoutError("UI Automation click timed out after 5.0s."),
    ):
        req_clk = OsClickUiElementRequest(params=OsClickUiElementParams(name="Equals"))
        await daemon._dispatch_request(conn, req_clk)
        payload_clk = json.loads(ws.send.call_args[0][0])
        assert payload_clk["id"] == req_clk.id
        assert payload_clk["ok"] is False
        assert "timed out" in payload_clk["error"]


@pytest.mark.asyncio
async def test_daemon_windows_os_clipboard_history_real_items(tmp_path: Path) -> None:
    from clea.core.daemon import CleaDaemon, _Connection

    mock_agent = MagicMock()
    mock_agent.memory.base_dir = tmp_path
    mock_agent.memory.resolve_session_id.side_effect = lambda sid: sid
    mock_agent.provider.provider_name = "test"
    mock_agent.provider.model_name = "test-model"
    mock_agent.registry.list_definitions.return_value = []

    daemon = CleaDaemon(
        agent=mock_agent,
        token="test-token",
        token_file=tmp_path / "token",
        lock_file=tmp_path / "lock",
    )
    ws = MagicMock()
    ws.send = AsyncMock()
    conn = _Connection(ws)

    # Real windows_os.py clipboard history structure with text and image items
    real_clipboard_history = {
        "ok": True,
        "text": "first text item",
        "truncated": False,
        "char_count": 15,
        "total_chars": 15,
        "history_enabled": True,
        "total_items": 2,
        "items": [
            {
                "index": 0,
                "id": "{A1B2C3D4-0001}",
                "timestamp": "2026-11-15T10:00:00.000Z",
                "kind": "text",
                "text": "first text item",
                "truncated": False,
                "image": None,
            },
            {
                "index": 1,
                "id": "{A1B2C3D4-0002}",
                "timestamp": "2026-11-15T10:01:00.000Z",
                "kind": "image",
                "text": None,
                "truncated": False,
                "image": {
                    "width": 640,
                    "height": 480,
                    "format": "png",
                    "bytes": "iVBORw0KGgoAAAANSUhEUgAAAA==",
                },
            },
        ],
    }

    with patch("clea.core.daemon.read_clipboard", return_value=real_clipboard_history):
        req = OsReadClipboardRequest(
            params=OsReadClipboardParams(include_history=True, max_items=10)
        )
        await daemon._dispatch_request(conn, req)

    payload = json.loads(ws.send.call_args[0][0])
    assert payload["id"] == req.id
    assert payload["ok"] is True
    result = payload["result"]
    assert result["history_enabled"] is True
    assert result["total_items"] == 2
    items = result["items"]
    assert len(items) == 2

    # Item 0: text item
    assert items[0]["kind"] == "text"
    assert items[0]["text"] == "first text item"
    assert items[0]["image"] is None

    # Item 1: image item
    assert items[1]["kind"] == "image"
    assert items[1]["text"] == ""
    assert items[1]["width"] == 640
    assert items[1]["height"] == 480
    assert items[1]["format"] == "png"
    assert items[1]["image_bytes_base64"] == "iVBORw0KGgoAAAANSUhEUgAAAA=="
    assert items[1]["image"]["width"] == 640


@pytest.mark.asyncio
async def test_daemon_windows_os_control_system_window_actions(tmp_path: Path) -> None:
    from clea.core.daemon import CleaDaemon, _Connection

    mock_agent = MagicMock()
    mock_agent.memory.base_dir = tmp_path
    mock_agent.memory.resolve_session_id.side_effect = lambda sid: sid
    mock_agent.provider.provider_name = "test"
    mock_agent.provider.model_name = "test-model"
    mock_agent.registry.list_definitions.return_value = []

    daemon = CleaDaemon(
        agent=mock_agent,
        token="test-token",
        token_file=tmp_path / "token",
        lock_file=tmp_path / "lock",
    )
    ws = MagicMock()
    ws.send = AsyncMock()
    conn = _Connection(ws)

    # 1. Dispatch with window_title="Notepad" and action="minimize"
    with patch(
        "clea.core.daemon.control_system",
        return_value={"ok": True, "success": True, "action": "minimize_window", "hwnd": 12345},
    ) as mock_ctrl:
        req = OsControlSystemRequest(
            params=OsControlSystemParams(action="minimize", window_title="Notepad")
        )
        await daemon._dispatch_request(conn, req)

        # control_system should be called with action and title
        assert mock_ctrl.call_count == 1
        call_args, call_kwargs = mock_ctrl.call_args
        assert call_args[0] == "minimize"
        assert call_kwargs.get("title") == "Notepad"

        payload = json.loads(ws.send.call_args[0][0])
        assert payload["id"] == req.id
        assert payload["ok"] is True
        assert payload["result"]["window_title"] == "Notepad"

    # 2. Dispatch with action="window", window_action="minimize", window_title="Notepad"
    with patch(
        "clea.core.daemon.control_system",
        return_value={"ok": True, "success": True, "action": "minimize_window", "hwnd": 12345},
    ) as mock_ctrl:
        req = OsControlSystemRequest(
            params=OsControlSystemParams(
                action="window", window_action="minimize", window_title="Notepad"
            )
        )
        await daemon._dispatch_request(conn, req)

        assert mock_ctrl.call_count == 1
        call_args, call_kwargs = mock_ctrl.call_args
        assert call_args[0] == "minimize"
        assert call_kwargs.get("title") == "Notepad"

        payload = json.loads(ws.send.call_args[0][0])
        assert payload["id"] == req.id
        assert payload["ok"] is True

    # 3. Dispatch with title="Notepad" (alias)
    with patch(
        "clea.core.daemon.control_system",
        return_value={"ok": True, "success": True, "action": "minimize_window", "hwnd": 12345},
    ) as mock_ctrl:
        req = OsControlSystemRequest(
            params=OsControlSystemParams(action="minimize_window", title="Notepad")
        )
        await daemon._dispatch_request(conn, req)

        assert mock_ctrl.call_count == 1
        call_args, call_kwargs = mock_ctrl.call_args
        assert call_args[0] == "minimize_window"
        assert call_kwargs.get("title") == "Notepad"

        payload = json.loads(ws.send.call_args[0][0])
        assert payload["id"] == req.id
        assert payload["ok"] is True
        assert payload["result"]["window_title"] == "Notepad"


def test_three_tier_memory_protocol_roundtrip() -> None:
    search_req = MemorySearchEpisodicRequest(
        params=MemorySearchEpisodicParams(query="three-tier memory", limit=10, surface="voice")
    )
    parsed_search = parse_frame(search_req.model_dump_json())
    assert isinstance(parsed_search, MemorySearchEpisodicRequest)
    assert parsed_search.method == "memory.search_episodic"
    assert parsed_search.params.query == "three-tier memory"
    assert parsed_search.params.limit == 10
    assert parsed_search.params.surface == "voice"

    manage_req = MemoryManageFactRequest(
        params=MemoryManageFactParams(
            action="update",
            fact_text="Prefers uv over pip",
            category="workflow",
            fact_key="package_manager",
            fact_id="fact-1",
            confidence=0.95,
            source_surface="chat",
            source_session_id="sess-1",
        )
    )
    parsed_manage = parse_frame(manage_req.model_dump_json())
    assert isinstance(parsed_manage, MemoryManageFactRequest)
    assert parsed_manage.method == "memory.manage_fact"
    assert parsed_manage.params.action == "update"
    assert parsed_manage.params.confidence == 0.95

    list_default_req = parse_frame('{"frame_type": "request", "method": "memory.list_facts"}')
    assert isinstance(list_default_req, MemoryListFactsRequest)
    assert list_default_req.params.status == "active"
    assert list_default_req.params.limit == 25

    list_req = MemoryListFactsRequest(
        params=MemoryListFactsParams(status="superseded", category="workflow", limit=50)
    )
    parsed_list = parse_frame(list_req.model_dump_json())
    assert isinstance(parsed_list, MemoryListFactsRequest)
    assert parsed_list.params.status == "superseded"
    assert parsed_list.params.category == "workflow"
    assert parsed_list.params.limit == 50

    with pytest.raises(ValidationError):
        MemorySearchEpisodicParams(query="q", limit=0)
    with pytest.raises(ValidationError):
        MemorySearchEpisodicParams(query="q", limit=51)
    with pytest.raises(ValidationError):
        MemoryManageFactParams(action="replace")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        MemoryManageFactParams(action="add", confidence=1.5)
    with pytest.raises(ValidationError):
        MemoryListFactsParams(limit=201)

    hit = EpisodicSessionHit(
        session_id="sess-1",
        surface="chat",
        timestamp="2026-11-21T09:00:00Z",
        title="Memory design",
        summary="Designed three-tier memory",
        topics="memory,sqlite",
        snippet="...three-tier memory...",
        score=4.2,
    )
    search_res = METHOD_RESULTS["memory.search_episodic"](results=[hit])
    assert isinstance(search_res, MemorySearchEpisodicResult)
    assert search_res.results[0].session_id == "sess-1"

    fact = MemoryBankFactItem(
        id="fact-2",
        category="workflow",
        fact_key="package_manager",
        fact_text="Prefers uv over pip",
        confidence=0.95,
        source_surface="chat",
        source_session_id="sess-1",
        status="active",
        created_at="2026-11-21T09:00:00Z",
        updated_at="2026-11-21T09:00:00Z",
    )
    manage_res = METHOD_RESULTS["memory.manage_fact"](
        ok=True,
        operation="update",
        fact=fact,
        superseded_id="fact-1",
        affected_ids=["fact-1", "fact-2"],
        message="Updated fact",
    )
    assert isinstance(manage_res, MemoryManageFactResult)
    assert manage_res.operation == "update"
    assert manage_res.superseded_id == "fact-1"

    list_res = METHOD_RESULTS["memory.list_facts"](facts=[fact], total=1)
    assert isinstance(list_res, MemoryListFactsResult)
    assert list_res.total == 1
    assert list_res.facts[0].category == "workflow"
