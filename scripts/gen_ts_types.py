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
    ApprovalDecision,
    ApprovalReason,
    ApprovalRequestedEvent,
    ApprovalResolvedEvent,
    ApprovalRespondRequest,
    AssistantTokenEvent,
    CallEndedEvent,
    CallStartedEvent,
    CallState,
    ChatSendRequest,
    ConversationsGetRequest,
    ConversationsListRequest,
    ErrorEvent,
    EventFrame,
    HealthPingRequest,
    MemoryGetDailyRequest,
    MemoryGetRequest,
    RequestFrame,
    ResponseFrame,
    RiskTier,
    ScreenCaptureStateEvent,
    ScreenCaptureTarget,
    SessionNewRequest,
    SettingsGetRequest,
    TaskState,
    TaskStatusEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
    TranscriptEvent,
    TranscriptRole,
    UsageEvent,
    VoiceDelegateTaskRequest,
    VoiceEndCallRequest,
    VoiceStartCallRequest,
    VoiceState,
    VoiceStateEvent,
    VoiceStatusRequest,
    VoiceTrigger,
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
        voice_state: VoiceState
        call_state: CallState
        transcript_role: TranscriptRole
        screen_capture_target: ScreenCaptureTarget
        voice_trigger: VoiceTrigger
        # Keep individual members explicit so json2ts always emits named interfaces
        health_ping_request: HealthPingRequest
        session_new_request: SessionNewRequest
        chat_send_request: ChatSendRequest
        approval_respond_request: ApprovalRespondRequest
        memory_get_request: MemoryGetRequest
        memory_get_daily_request: MemoryGetDailyRequest
        conversations_list_request: ConversationsListRequest
        conversations_get_request: ConversationsGetRequest
        settings_get_request: SettingsGetRequest
        voice_start_call_request: VoiceStartCallRequest
        voice_end_call_request: VoiceEndCallRequest
        voice_status_request: VoiceStatusRequest
        voice_delegate_task_request: VoiceDelegateTaskRequest
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


def generate_typescript() -> str:
    """Compile the Pydantic JSON Schema into formatted TypeScript via json2ts."""
    schema = build_json_schema()
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", _NODE_SCRIPT],
        cwd=UI_DIR,
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
