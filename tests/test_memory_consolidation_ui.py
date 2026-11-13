"""Tests for Part A & Part B memory consolidation fix and status commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from clea.memory.consolidation import (
    ConsolidationManager,
    ConsolidationOperation,
    ConsolidationOutput,
    sanitize_error,
)
from clea.memory.store import MemoryStore
from clea.providers.base import LLMProvider, Message, StreamChunk


class _RecordingProvider(LLMProvider):
    provider_name = "recording-fake"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.call_count = 0
        self.prompts_received: list[str] = []

    async def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[Any],
        system_prompt: str,
        response_format: dict[str, Any] | None = None,
    ):
        self.call_count += 1
        prompt = messages[-1].content if messages else ""
        self.prompts_received.append(prompt)
        resp = self.responses.pop(0) if self.responses else "{}"
        yield StreamChunk(type="text_delta", text=resp)
        yield StreamChunk(type="done")


def test_alias_choices_accepts_value_and_text() -> None:
    """Exact screenshot failing shape: key 'value' instead of 'text'."""
    # Using 'value'
    op_from_value = ConsolidationOperation.model_validate(
        {"op": "add", "target_section": "Environment", "value": "lenovoloq"}
    )
    assert op_from_value.text == "lenovoloq"

    # Using standard 'text'
    op_from_text = ConsolidationOperation.model_validate(
        {"op": "add", "target_section": "Environment", "text": "lenovoloq"}
    )
    assert op_from_text.text == "lenovoloq"

    # Full output validation with 'value'
    full_output = ConsolidationOutput.model_validate(
        {
            "summary": "User configured their environment.",
            "operations": [
                {"op": "add", "target_section": "Environment", "value": "lenovoloq"},
                {
                    "op": "add",
                    "target_section": "Environment",
                    "value": "user's desktop machine",
                },
            ],
        }
    )
    assert len(full_output.operations) == 2
    assert full_output.operations[0].text == "lenovoloq"
    assert full_output.operations[1].text == "user's desktop machine"


def test_sanitize_error_strips_paths_and_tokens() -> None:
    win_path_err = r"Failed at C:\Users\Aditya Kumar\AppData\Local\clea\memory\test.json: boom"
    sanitized = sanitize_error(win_path_err)
    assert r"C:\Users" not in sanitized
    assert "[path]" in sanitized

    unix_path_err = "Error in /Users/aditya/clea/file.txt: invalid syntax"
    assert "/Users/aditya" not in sanitize_error(unix_path_err)

    secret_err = (
        "Auth failed with key AIzaSyA1234567890abcdefghijklmnopqrstuv "
        "and sk-1234567890abcdef1234567890"
    )
    sanitized_secret = sanitize_error(secret_err)
    assert "AIzaSy" not in sanitized_secret
    assert "sk-" not in sanitized_secret
    assert "[key]" in sanitized_secret


async def test_consolidation_succeeds_with_failing_screenshot_payload(tmp_path: Path) -> None:
    """Consolidation succeeds when model returns the exact screenshot payload with 'value'."""
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    store.append_transcript_message(
        session_id="67727ff3", role="user", text="My desktop machine is lenovoloq."
    )
    store.append_transcript_message(
        session_id="67727ff3", role="assistant", text="Noted your machine is lenovoloq."
    )
    store.append_transcript_message(
        session_id="67727ff3", role="user", text="I use it for development."
    )

    failing_payload_from_screenshot = json.dumps(
        {
            "summary": "User stated their desktop machine is lenovoloq and used for development.",
            "operations": [
                {
                    "op": "add",
                    "target_section": "Environment",
                    "value": "lenovoloq",
                    "source_tag": "stated",
                    "reason": "Explicit statement",
                },
                {
                    "op": "add",
                    "target_section": "Environment",
                    "value": "user's desktop machine",
                    "source_tag": "stated",
                    "reason": "Desktop machine confirmation",
                },
            ],
        }
    )

    provider = _RecordingProvider([failing_payload_from_screenshot])
    manager = ConsolidationManager(memory=store, provider=provider, min_user_turns=2)

    ok = await manager.consolidate_session("67727ff3")
    assert ok is True
    assert provider.call_count == 1

    long_term = store.read_long_term()
    assert "lenovoloq" in long_term
    assert "user's desktop machine" in long_term

    # Pending file should not contain 67727ff3
    pending = manager.load_pending()
    assert "67727ff3" not in pending.get("sessions", {})


async def test_correction_retry_sends_validation_errors_and_succeeds(tmp_path: Path) -> None:
    """When attempt 1 returns invalid output, retry sends feedback and prior output."""
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    store.append_transcript_message(session_id="s-retry", role="user", text="I prefer dark mode.")
    store.append_transcript_message(session_id="s-retry", role="assistant", text="Got it.")
    store.append_transcript_message(session_id="s-retry", role="user", text="Always use dark mode.")

    # Attempt 1: missing required target_section
    invalid_attempt_1 = json.dumps(
        {
            "summary": "User prefers dark mode.",
            "operations": [{"op": "add", "text": "Prefers dark mode."}],
        }
    )
    # Attempt 2: valid JSON
    valid_attempt_2 = json.dumps(
        {
            "summary": "User prefers dark mode.",
            "operations": [
                {
                    "op": "add",
                    "target_section": "Preferences",
                    "text": "Prefers dark mode.",
                }
            ],
        }
    )

    provider = _RecordingProvider([invalid_attempt_1, valid_attempt_2])
    manager = ConsolidationManager(memory=store, provider=provider, min_user_turns=2)

    ok = await manager.consolidate_session("s-retry")
    assert ok is True
    assert provider.call_count == 2

    # Check retry prompt contained the error feedback and previous output
    retry_prompt = provider.prompts_received[1]
    assert "[SYSTEM FEEDBACK]: Previous response failed JSON validation" in retry_prompt
    assert "target_section" in retry_prompt
    assert "Previous output was:" in retry_prompt
    assert "Prefers dark mode." in store.read_long_term()


async def test_pending_state_retry_cap_and_exponential_backoff(tmp_path: Path) -> None:
    """Never lose work: pending state, backoff, retry cap (3), and manual reconsolidate."""
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    store.append_transcript_message(
        session_id="s-cap", role="user", text="My favorite color is green."
    )
    store.append_transcript_message(session_id="s-cap", role="assistant", text="Green is great.")
    store.append_transcript_message(session_id="s-cap", role="user", text="Green everything.")

    # Provider always fails with invalid JSON
    provider = _RecordingProvider(["bad json 1", "bad json 2"] * 5)
    manager = ConsolidationManager(memory=store, provider=provider, min_user_turns=2)

    # Attempt 1 (initial consolidation) -> fails 2 model turns -> marked "pending" with backoff
    ok1 = await manager.consolidate_session("s-cap")
    assert ok1 is False
    assert provider.call_count == 2

    pending1 = manager.load_pending().get("sessions", {})
    assert "s-cap" in pending1
    assert pending1["s-cap"]["status"] == "pending"
    assert pending1["s-cap"]["retry_count"] == 1
    assert pending1["s-cap"]["next_retry_at"] is not None

    status_dict1 = manager.get_detailed_status()
    assert len(status_dict1["pending"]) == 1
    assert len(status_dict1["failed"]) == 0
    assert status_dict1["pending"][0]["session_id"] == "s-cap"

    # Attempt 2 -> force consolidation again (e.g. from crash recovery)
    ok2 = await manager.consolidate_session("s-cap", force=True)
    assert ok2 is False
    pending2 = manager.load_pending().get("sessions", {})
    assert pending2["s-cap"]["retry_count"] == 2

    # Attempt 3 -> reaches cap 3 -> marked "failed", removed from pending
    ok3 = await manager.consolidate_session("s-cap", force=True)
    assert ok3 is False

    pending3 = manager.load_pending().get("sessions", {})
    assert "s-cap" not in pending3

    state3 = manager.load_state().get("sessions", {})
    assert state3["s-cap"]["status"] == "failed"
    assert state3["s-cap"]["retry_count"] == 3

    status_dict3 = manager.get_detailed_status()
    assert len(status_dict3["pending"]) == 0
    assert len(status_dict3["failed"]) == 1
    assert status_dict3["failed"][0]["session_id"] == "s-cap"

    # Manual reconsolidate resets and succeeds once provider returns valid JSON
    valid_json = json.dumps(
        {
            "summary": "User's favorite color is green.",
            "operations": [
                {
                    "op": "add",
                    "target_section": "Preferences",
                    "text": "Favorite color is green.",
                }
            ],
        }
    )
    provider.responses.insert(0, valid_json)
    recon_ok = await manager.reconsolidate_session("s-cap")
    assert recon_ok is True
    assert "Favorite color is green." in store.read_long_term()

    status_dict_after = manager.get_detailed_status()
    assert len(status_dict_after["pending"]) == 0
    assert len(status_dict_after["failed"]) == 0
    assert status_dict_after["completed_count"] == 1


async def test_trivial_session_skipping_no_model_calls(tmp_path: Path) -> None:
    """Sessions with fewer than N user turns (default 2) and no tool use skip model calls."""
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    # Session 1: 1 user turn, no tool use -> TRIVIAL
    store.append_transcript_message(session_id="s-trivial-1", role="user", text="Hi Clea")
    store.append_transcript_message(
        session_id="s-trivial-1", role="assistant", text="Hello! How can I assist you?"
    )

    provider = _RecordingProvider([])
    manager = ConsolidationManager(memory=store, provider=provider, min_user_turns=2)

    ok = await manager.consolidate_session("s-trivial-1")
    assert ok is True
    # Zero model calls made!
    assert provider.call_count == 0

    episodes = store.get_recent_episodes()
    assert len(episodes) == 1
    assert "Short session, 1 turn" in episodes[0]["summary"]
    assert episodes[0]["trivial"] is True

    # Session 2: 1 user turn WITH tool use -> NOT trivial, model MUST be called
    store.append_transcript_message(
        session_id="s-tool-1",
        role="user",
        text="What time is it?",
    )
    store.append_transcript_message(
        session_id="s-tool-1",
        role="assistant",
        text="",
        tool_calls=[{"id": "call_1", "name": "clock", "arguments": {}}],
    )
    store.append_transcript_message(
        session_id="s-tool-1",
        role="tool",
        text="14:00",
    )
    valid_tool_summary = json.dumps({"summary": "Checked time.", "operations": []})
    provider.responses.append(valid_tool_summary)

    ok_tool = await manager.consolidate_session("s-tool-1")
    assert ok_tool is True
    assert provider.call_count == 1  # model was called because tool was used


async def test_rejected_output_never_partially_writes_facts(tmp_path: Path) -> None:
    """If consolidation fails, no facts are partially written to MEMORY.md."""
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()
    store.write_long_term(
        "# Clea Memory\n\n## Profile\n- [prof-1] (2026-11-13, stated) Engineer.\n"
    )

    initial_long_term = store.read_long_term()

    store.append_transcript_message(
        session_id="s-fail-write", role="user", text="I live in Tokyo now."
    )
    store.append_transcript_message(
        session_id="s-fail-write", role="assistant", text="Tokyo noted."
    )
    store.append_transcript_message(
        session_id="s-fail-write", role="user", text="Yes, Tokyo Japan."
    )

    # Provider returns completely invalid output both times
    provider = _RecordingProvider(["invalid json 1", "invalid json 2"])
    manager = ConsolidationManager(memory=store, provider=provider, min_user_turns=2)

    ok = await manager.consolidate_session("s-fail-write")
    assert ok is False

    # Long-term memory must be completely unchanged
    assert store.read_long_term() == initial_long_term
    assert "Tokyo" not in store.read_long_term()


def test_cli_memory_subcommands_parser() -> None:
    from clea.surfaces.cli.main import build_parser

    parser = build_parser()
    args1 = parser.parse_args(["memory", "status"])
    assert args1.command == "memory"
    assert args1.memory_subcommand == "status"

    args2 = parser.parse_args(["memory", "reconsolidate", "67727ff3"])
    assert args2.command == "memory"
    assert args2.memory_subcommand == "reconsolidate"
    assert args2.session_id == "67727ff3"


async def test_daemon_protocol_memory_status_and_reconsolidate(tmp_path: Path) -> None:
    """Test memory.status, memory.facts, memory.archive and memory.reconsolidate via daemon."""
    from clea.core import auth
    from clea.core.agent import AgentLoop
    from clea.core.daemon import CleaDaemon
    from clea.tools.registry import ToolRegistry

    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    # Add a stated fact
    store.add_or_update_entry(text="User is an engineer.", section="Profile")

    # Add transcript for reconsolidation test
    store.append_transcript_message(
        session_id="sess-recon-test", role="user", text="I prefer dark mode in all apps."
    )
    store.append_transcript_message(
        session_id="sess-recon-test", role="assistant", text="Noted your preference."
    )
    store.append_transcript_message(
        session_id="sess-recon-test", role="user", text="Yes dark mode please."
    )

    valid_json = json.dumps(
        {
            "summary": "User prefers dark mode.",
            "operations": [
                {
                    "op": "add",
                    "target_section": "Preferences",
                    "text": "User prefers dark mode.",
                }
            ],
        }
    )
    provider = _RecordingProvider([valid_json, valid_json, valid_json])
    agent = AgentLoop(provider=provider, registry=ToolRegistry(), memory=store)
    daemon = CleaDaemon(agent=agent, host="127.0.0.1", port=0)
    await daemon.start()

    token = auth.read_token(daemon.token_file)
    assert token is not None

    from websockets.asyncio.client import connect

    from clea.core.protocol import (
        MemoryArchiveRequest,
        MemoryFactsRequest,
        MemoryReconsolidateParams,
        MemoryReconsolidateRequest,
        MemoryStatusRequest,
        ResponseFrame,
        parse_frame,
    )

    ws_url = f"ws://127.0.0.1:{daemon.bound_port}"

    async def recv_resp(ws_conn) -> ResponseFrame:
        while True:
            frame = parse_frame(await ws_conn.recv())
            if isinstance(frame, ResponseFrame):
                return frame

    try:
        async with connect(ws_url, subprotocols=auth.client_subprotocols(token)) as ws:
            # 1. memory.facts
            await ws.send(MemoryFactsRequest().model_dump_json())
            f1 = await recv_resp(ws)
            assert f1.ok
            sections = f1.result.get("sections", [])
            prof_sec = next(s for s in sections if s["name"] == "Profile")
            assert len(prof_sec["entries"]) == 1
            assert "User is an engineer." in prof_sec["entries"][0]["text"]

            # 2. memory.archive
            await ws.send(MemoryArchiveRequest().model_dump_json())
            f2 = await recv_resp(ws)
            assert f2.ok
            assert "entries" in f2.result

            # 3. memory.status
            await ws.send(MemoryStatusRequest().model_dump_json())
            f3 = await recv_resp(ws)
            assert f3.ok
            assert "pending" in f3.result
            assert "failed" in f3.result

            # 4. memory.reconsolidate
            rec_req = MemoryReconsolidateRequest(
                params=MemoryReconsolidateParams(session_id="sess-recon-test")
            )
            await ws.send(rec_req.model_dump_json())
            f4 = await recv_resp(ws)
            assert f4.ok
            assert f4.result.get("ok") is True
            assert "User prefers dark mode." in store.read_long_term()
    finally:
        await daemon.stop()
