"""Generate TypeScript protocol types from Python Pydantic models.

Usage:
    uv run python scripts/gen_ts_types.py          # write ui/src/protocol/generated.ts
    uv run python scripts/gen_ts_types.py --check  # fail with exit code 1 on drift
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from clea.core.protocol import (
    METHOD_RESULTS,
    ActivityCancelRequest,
    ActivityListRequest,
    ApprovalDecision,
    ApprovalReason,
    ApprovalRequestedEvent,
    ApprovalResolvedEvent,
    ApprovalRespondRequest,
    AssistantTokenEvent,
    CallEndedEvent,
    CallStartedEvent,
    CallState,
    ChatCreateAndOpenRequest,
    ChatDeleteRequest,
    ChatFocusRequest,
    ChatRenameRequest,
    ChatSendFromVoiceRequest,
    ChatSendRequest,
    ClipboardItem,
    ConversationCreatedData,
    ConversationCreatedEvent,
    ConversationDeleteAllRequest,
    ConversationDeletedData,
    ConversationDeletedEvent,
    ConversationDeleteRequest,
    ConversationExportRequest,
    ConversationGetRequest,
    ConversationListRequest,
    ConversationPinRequest,
    ConversationRenameRequest,
    ConversationResumeMessage,
    ConversationResumeRequest,
    ConversationSearchHit,
    ConversationSearchSnippet,
    ConversationsGetRequest,
    ConversationsListRequest,
    ConversationSummary,
    ConversationUndoDeleteRequest,
    ConversationUpdatedData,
    ConversationUpdatedEvent,
    DevEmitSampleTaskRequest,
    ErrorEvent,
    EventFrame,
    GitCommitItem,
    GitWorktreeItem,
    HealthPingRequest,
    MediaVolumeAction,
    MemoryAddFactRequest,
    MemoryArchiveRequest,
    MemoryDeleteFactRequest,
    MemoryEpisodesRequest,
    MemoryFactsRequest,
    MemoryForgetRequest,
    MemoryGetDailyRequest,
    MemoryGetRequest,
    MemoryReconsolidateRequest,
    MemorySearchHit,
    MemoryStatusRequest,
    MemoryUpdatedData,
    MemoryUpdatedEvent,
    OsClickUiElementRequest,
    OsControlMediaVolumeRequest,
    OsControlSystemRequest,
    OsGuiActionRequest,
    OsInspectUiElementsRequest,
    OsOpenOrFocusAppRequest,
    OsOpenUrlRequest,
    OsReadClipboardRequest,
    OsSaveClipboardToFileRequest,
    OsSelectClipboardItemRequest,
    OsTransformClipboardRequest,
    OsUiActRequest,
    OsUiBatchRequest,
    OsUiSnapshotRequest,
    OsWriteClipboardRequest,
    ReminderFiredData,
    ReminderFiredEvent,
    RequestFrame,
    ResponseFrame,
    RiskTier,
    ScreenCaptureStateEvent,
    ScreenCaptureTarget,
    SessionNewRequest,
    SettingsGetRequest,
    TaskKind,
    TaskState,
    TaskStatusEvent,
    TimerFiredData,
    TimerFiredEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
    TranscriptEvent,
    TranscriptRole,
    TranscriptsGetRequest,
    TranscriptsListRequest,
    UiActAction,
    UiBatchOp,
    UiBatchStep,
    UiDiff,
    UiDiffChangedValue,
    UiElementItem,
    UiNavigateData,
    UiNavigateEvent,
    UiNavigateRequest,
    UiSnapshotElement,
    UiView,
    UsageEvent,
    VoiceConversationListItem,
    VoiceConversationTurnItem,
    VoiceDelegateTaskRequest,
    VoiceEndCallRequest,
    VoiceHistoryMessageItem,
    VoiceHistoryRequest,
    VoiceListConversationsRequest,
    VoicePostToActiveChatRequest,
    VoiceReadConversationRequest,
    VoiceSearchConversationsRequest,
    VoiceSearchMemoryRequest,
    VoiceStartCallRequest,
    VoiceState,
    VoiceStateEvent,
    VoiceStatusRequest,
    VoiceTrigger,
    WorkspaceGitInspectRequest,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_DIR = REPO_ROOT / "ui"
OUTPUT_PATH = UI_DIR / "src" / "protocol" / "generated.ts"


def _build_method_results_model() -> type[BaseModel]:
    fields: dict[str, tuple[Any, Any]] = {
        method.replace(".", "_"): (model, ...) for method, model in METHOD_RESULTS.items()
    }
    from pydantic import create_model

    return create_model("MethodResults", **fields)  # type: ignore[call-overload,no-any-return]


def build_json_schema() -> dict[str, Any]:
    """Build a single JSON Schema covering all protocol frames, events, and results."""
    MethodResults = _build_method_results_model()

    class CleaProtocolSchema(BaseModel):
        request: RequestFrame
        response: ResponseFrame
        event: EventFrame
        method_results: MethodResults  # type: ignore[valid-type]
        risk_tier: RiskTier
        approval_decision: ApprovalDecision
        approval_reason: ApprovalReason
        task_state: TaskState
        task_kind: TaskKind
        voice_state: VoiceState
        call_state: CallState
        transcript_role: TranscriptRole
        screen_capture_target: ScreenCaptureTarget
        voice_trigger: VoiceTrigger
        ui_view: UiView
        media_volume_action: MediaVolumeAction
        # Keep individual members explicit so json2ts always emits named interfaces
        health_ping_request: HealthPingRequest
        session_new_request: SessionNewRequest
        chat_send_request: ChatSendRequest
        approval_respond_request: ApprovalRespondRequest
        memory_get_request: MemoryGetRequest
        memory_get_daily_request: MemoryGetDailyRequest
        memory_episodes_request: MemoryEpisodesRequest
        memory_facts_request: MemoryFactsRequest
        memory_archive_request: MemoryArchiveRequest
        memory_status_request: MemoryStatusRequest
        memory_reconsolidate_request: MemoryReconsolidateRequest
        memory_forget_request: MemoryForgetRequest
        transcripts_list_request: TranscriptsListRequest
        transcripts_get_request: TranscriptsGetRequest
        conversations_list_request: ConversationsListRequest
        conversations_get_request: ConversationsGetRequest
        conversation_list_request: ConversationListRequest
        conversation_get_request: ConversationGetRequest
        conversation_resume_request: ConversationResumeRequest
        conversation_rename_request: ConversationRenameRequest
        conversation_pin_request: ConversationPinRequest
        conversation_export_request: ConversationExportRequest
        conversation_delete_request: ConversationDeleteRequest
        conversation_delete_all_request: ConversationDeleteAllRequest
        conversation_undo_delete_request: ConversationUndoDeleteRequest
        conversation_summary: ConversationSummary
        conversation_resume_message: ConversationResumeMessage
        settings_get_request: SettingsGetRequest
        dev_emit_sample_task_request: DevEmitSampleTaskRequest
        chat_focus_request: ChatFocusRequest
        voice_start_call_request: VoiceStartCallRequest
        voice_end_call_request: VoiceEndCallRequest
        voice_status_request: VoiceStatusRequest
        voice_delegate_task_request: VoiceDelegateTaskRequest
        voice_history_request: VoiceHistoryRequest
        voice_list_conversations_request: VoiceListConversationsRequest
        voice_search_conversations_request: VoiceSearchConversationsRequest
        voice_read_conversation_request: VoiceReadConversationRequest
        voice_search_memory_request: VoiceSearchMemoryRequest
        voice_post_to_active_chat_request: VoicePostToActiveChatRequest
        voice_conversation_list_item: VoiceConversationListItem
        conversation_search_snippet: ConversationSearchSnippet
        ui_navigate_request: UiNavigateRequest
        chat_create_and_open_request: ChatCreateAndOpenRequest
        chat_send_from_voice_request: ChatSendFromVoiceRequest
        chat_rename_request: ChatRenameRequest
        chat_delete_request: ChatDeleteRequest
        memory_add_fact_request: MemoryAddFactRequest
        memory_delete_fact_request: MemoryDeleteFactRequest
        activity_list_request: ActivityListRequest
        activity_cancel_request: ActivityCancelRequest
        os_open_or_focus_app_request: OsOpenOrFocusAppRequest
        os_control_media_volume_request: OsControlMediaVolumeRequest
        os_read_clipboard_request: OsReadClipboardRequest
        os_select_clipboard_item_request: OsSelectClipboardItemRequest
        os_open_url_request: OsOpenUrlRequest
        os_control_system_request: OsControlSystemRequest
        os_write_clipboard_request: OsWriteClipboardRequest
        os_transform_clipboard_request: OsTransformClipboardRequest
        os_save_clipboard_to_file_request: OsSaveClipboardToFileRequest
        os_inspect_ui_elements_request: OsInspectUiElementsRequest
        os_click_ui_element_request: OsClickUiElementRequest
        os_gui_action_request: OsGuiActionRequest
        os_ui_snapshot_request: OsUiSnapshotRequest
        os_ui_act_request: OsUiActRequest
        os_ui_batch_request: OsUiBatchRequest
        ui_snapshot_element: UiSnapshotElement
        ui_diff: UiDiff
        ui_diff_changed_value: UiDiffChangedValue
        ui_batch_step: UiBatchStep
        ui_act_action: UiActAction
        ui_batch_op: UiBatchOp
        clipboard_item: ClipboardItem
        ui_element_item: UiElementItem
        workspace_git_inspect_request: WorkspaceGitInspectRequest
        git_worktree_item: GitWorktreeItem
        git_commit_item: GitCommitItem
        conversation_search_hit: ConversationSearchHit
        voice_conversation_turn_item: VoiceConversationTurnItem
        memory_search_hit: MemorySearchHit
        voice_history_message_item: VoiceHistoryMessageItem
        assistant_token_event: AssistantTokenEvent
        tool_call_started_event: ToolCallStartedEvent
        tool_call_finished_event: ToolCallFinishedEvent
        approval_requested_event: ApprovalRequestedEvent
        approval_resolved_event: ApprovalResolvedEvent
        task_status_event: TaskStatusEvent
        voice_state_event: VoiceStateEvent
        call_started_event: CallStartedEvent
        call_ended_event: CallEndedEvent
        transcript_event: TranscriptEvent
        screen_capture_state_event: ScreenCaptureStateEvent
        usage_event: UsageEvent
        error_event: ErrorEvent
        conversation_updated_event: ConversationUpdatedEvent
        conversation_deleted_event: ConversationDeletedEvent
        ui_navigate_event: UiNavigateEvent
        conversation_created_event: ConversationCreatedEvent
        memory_updated_event: MemoryUpdatedEvent
        timer_fired_event: TimerFiredEvent
        timer_fired_data: TimerFiredData
        reminder_fired_event: ReminderFiredEvent
        reminder_fired_data: ReminderFiredData
        conversation_updated_data: ConversationUpdatedData
        conversation_deleted_data: ConversationDeletedData
        ui_navigate_data: UiNavigateData
        conversation_created_data: ConversationCreatedData
        memory_updated_data: MemoryUpdatedData

    return CleaProtocolSchema.model_json_schema(mode="serialization")


_NODE_SCRIPT = r"""
import { compile } from "json-schema-to-typescript";

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const schema = JSON.parse(Buffer.concat(chunks).toString("utf-8"));

const banner = [
  "/* eslint-disable */",
  "/**",
  " * AUTO-GENERATED FILE — DO NOT EDIT BY HAND.",
  " * Source of truth: src/clea/core/protocol.py",
  " * Regenerate with: uv run python scripts/gen_ts_types.py",
  " */",
].join("\n");

const ts = await compile(schema, "CleaProtocolSchema", {
  bannerComment: banner,
  additionalProperties: false,
  strictIndexSignatures: true,
  unknownAny: true,
  style: {
    singleQuote: false,
    semi: true,
    tabWidth: 2,
    trailingComma: "all",
  },
});

process.stdout.write(ts);
"""


def _find_node_cwd() -> Path:
    if (UI_DIR / "node_modules").is_dir():
        return UI_DIR
    sibling_ui = REPO_ROOT.parent / "Clea" / "ui"
    if (sibling_ui / "node_modules").is_dir():
        return sibling_ui
    return UI_DIR


def generate_typescript() -> str:
    """Compile the Pydantic JSON Schema into formatted TypeScript via json2ts."""
    schema = build_json_schema()
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", _NODE_SCRIPT],
        cwd=_find_node_cwd(),
        input=json.dumps(schema),
        text=True,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"json-schema-to-typescript failed (exit {proc.returncode}):\n{proc.stderr}"
        )
    # Normalize CRLF -> LF for cross-platform check consistency
    return proc.stdout.replace("\r\n", "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate TS types from clea.core.protocol")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check that generated.ts matches protocol.py without writing",
    )
    args = parser.parse_args(argv)

    generated = generate_typescript()

    if args.check:
        if not OUTPUT_PATH.exists():
            print(
                f"Missing {OUTPUT_PATH}. Run: uv run python scripts/gen_ts_types.py",
                file=sys.stderr,
            )
            return 1
        current = OUTPUT_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
        if current != generated:
            print(
                f"{OUTPUT_PATH} is out of date with src/clea/core/protocol.py.\n"
                "Regenerate with: uv run python scripts/gen_ts_types.py",
                file=sys.stderr,
            )
            return 1
        print(f"Protocol TypeScript types are up to date ({OUTPUT_PATH.relative_to(REPO_ROOT)}).")
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(generated, encoding="utf-8", newline="\n")
    print(f"Wrote {OUTPUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
