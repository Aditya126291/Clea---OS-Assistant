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


def test_default_min_user_turns_is_two_without_explicit_kwarg(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()
    provider = _RecordingProvider([])
    # Instantiate without min_user_turns argument
    manager = ConsolidationManager(memory=store, provider=provider)
    assert manager.min_user_turns == 2


async def test_reconsolidate_resolves_short_session_id_prefix(tmp_path: Path) -> None:
    """clea memory reconsolidate 67727ff3 succeeds even if the full ID is 32-hex characters."""
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    full_session_id = "67727ff3abcdef1234567890abcdef12"
    store.append_transcript_message(
        session_id=full_session_id,
        role="user",
        text="My desktop machine is lenovoloq.",
    )
    store.append_transcript_message(
        session_id=full_session_id,
        role="assistant",
        text="Understood.",
    )
    store.append_transcript_message(
        session_id=full_session_id,
        role="user",
        text="It runs Windows 11.",
    )

    valid_json = json.dumps(
        {
            "summary": "User desktop machine is lenovoloq on Windows 11.",
            "operations": [
                {
                    "op": "add",
                    "target_section": "Environment",
                    "text": "Desktop machine is lenovoloq running Windows 11.",
                }
            ],
        }
    )
    provider = _RecordingProvider([valid_json])
    manager = ConsolidationManager(memory=store, provider=provider)

    # Call with 8-character prefix as in user incident
    ok = await manager.reconsolidate_session("67727ff3")
    assert ok is True
    assert provider.call_count == 1
    assert "lenovoloq" in store.read_long_term()


def test_get_transcript_handles_corrupted_lines_gracefully(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    sess_id = "sess-corrupted-jsonl"
    path = store.transcript_path(sess_id)
    # Write a valid header, a corrupted middle line, and a valid message
    path.write_text(
        json.dumps({"session_id": sess_id, "title": "Corrupted Session"})
        + "\n"
        + "{malformed json line that is incomplete\n"
        + json.dumps({"role": "user", "text": "Valid message after corruption."})
        + "\n",
        encoding="utf-8",
    )

    transcript = store.get_transcript(sess_id)
    assert transcript is not None
    assert transcript["title"] == "Corrupted Session"
    assert len(transcript["messages"]) == 1
    assert transcript["messages"][0]["text"] == "Valid message after corruption."


def test_extract_json_block_handles_nested_code_fences_and_backticks() -> None:
    from clea.memory.consolidation import _extract_json_block

    # Markdown code fence with nested code block and backticks inside a fact string
    raw_response = (
        "Here is the requested consolidation JSON:\n"
        "```json\n"
        "{\n"
        '  "summary": "Discussed code formatting.",\n'
        '  "operations": [\n'
        "    {\n"
        '      "op": "add",\n'
        '      "target_section": "Preferences",\n'
        '      "text": "Prefers snippet: ```python\\nprint(\'hello\')\\n``` for python files",\n'
        '      "source_tag": "stated",\n'
        '      "reason": "Explicit preference"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "```\n"
        "Hope this helps!"
    )

    extracted = _extract_json_block(raw_response)
    parsed = json.loads(extracted)
    assert parsed["summary"] == "Discussed code formatting."
    assert "print('hello')" in parsed["operations"][0]["text"]


async def test_daemon_protocol_memory_forget(tmp_path: Path) -> None:
    """Test memory.forget removes facts via daemon WebSocket."""
    from websockets.asyncio.client import connect

    from clea.core import auth
    from clea.core.agent import AgentLoop
    from clea.core.daemon import CleaDaemon
    from clea.core.protocol import (
        MemoryForgetParams,
        MemoryForgetRequest,
        ResponseFrame,
        parse_frame,
    )
    from clea.tools.registry import ToolRegistry

    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    entry = store.add_or_update_entry(text="Prefers Vim keybindings.", section="Preferences")
    assert "Prefers Vim keybindings." in store.read_long_term()

    provider = _RecordingProvider([])
    agent = AgentLoop(provider=provider, registry=ToolRegistry(), memory=store)
    daemon = CleaDaemon(agent=agent, host="127.0.0.1", port=0)
    await daemon.start()

    token = auth.read_token(daemon.token_file)
    assert token is not None

    ws_url = f"ws://127.0.0.1:{daemon.bound_port}"

    async def recv_resp(ws_conn) -> ResponseFrame:
        while True:
            frame = parse_frame(await ws_conn.recv())
            if isinstance(frame, ResponseFrame):
                return frame

    try:
        async with connect(ws_url, subprotocols=auth.client_subprotocols(token)) as ws:
            # Forget by entry ID
            req = MemoryForgetRequest(params=MemoryForgetParams(id=entry.id))
            await ws.send(req.model_dump_json())
            resp = await recv_resp(ws)
            assert resp.ok
            assert resp.result.get("ok") is True
            assert resp.result.get("removed_count") == 1
            assert resp.result.get("removed_ids") == [entry.id]
            assert "Prefers Vim keybindings." not in store.read_long_term()

            # Add another entry and test fallback from unknown id to query
            store.add_or_update_entry(
                text="Enjoys green tea in the morning.", section="Preferences"
            )
            req2 = MemoryForgetRequest(
                params=MemoryForgetParams(id="nonexistent-id", query="green tea")
            )
            await ws.send(req2.model_dump_json())
            resp2 = await recv_resp(ws)
            assert resp2.ok
            assert resp2.result.get("removed_count") == 1
            assert "green tea" not in store.read_long_term()
    finally:
        await daemon.stop()


async def test_resumed_conversation_re_runs_consolidation_and_updates_episode(
    tmp_path: Path,
) -> None:
    """Test that resuming conversation with more turns triggers consolidation and updates."""
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    session_id = "sess-resumed-conversation"

    # Turn 1 & 2
    store.append_transcript_message(session_id=session_id, role="user", text="I work at Acme Corp.")
    store.append_transcript_message(
        session_id=session_id, role="assistant", text="Noted Acme Corp."
    )

    # Turn 3 & 4 (2 user turns total)
    store.append_transcript_message(
        session_id=session_id, role="user", text="My primary stack is TypeScript."
    )
    store.append_transcript_message(
        session_id=session_id, role="assistant", text="Got it, TypeScript stack."
    )

    json_1 = json.dumps(
        {
            "summary": "Initial discussion about company and stack.",
            "operations": [
                {
                    "op": "add",
                    "target_section": "Profile",
                    "text": "Works at Acme Corp.",
                },
                {
                    "op": "add",
                    "target_section": "Preferences",
                    "text": "Primary stack is TypeScript.",
                },
            ],
        }
    )

    json_2 = json.dumps(
        {
            "summary": "Updated discussion: now also uses Rust for services.",
            "operations": [
                {
                    "op": "add",
                    "target_section": "Preferences",
                    "text": "Uses Rust for microservices.",
                },
            ],
        }
    )

    provider = _RecordingProvider([json_1, json_2])
    manager = ConsolidationManager(memory=store, provider=provider)

    # First consolidation
    ok1 = await manager.consolidate_session(session_id)
    assert ok1 is True
    assert provider.call_count == 1

    # Check state has last_consolidated_turn == 4
    state1 = manager.load_state()
    sess_entry1 = state1["sessions"][session_id]
    assert sess_entry1["status"] == "completed"
    assert sess_entry1["last_consolidated_turn"] == 4

    # Episode summary exists
    episodes1 = store.get_recent_episodes()
    assert len(episodes1) == 1
    assert episodes1[0]["summary"] == "Initial discussion about company and stack."
    assert "Works at Acme Corp." in store.read_long_term()
    assert "Primary stack is TypeScript." in store.read_long_term()

    # Calling consolidate again without new turns should no-op
    assert manager.is_consolidated(session_id) is True
    ok_noop = await manager.consolidate_session(session_id)
    assert ok_noop is True
    assert provider.call_count == 1  # No new model calls!

    # Now resume the conversation: add turns 5 & 6
    store.append_transcript_message(
        session_id=session_id, role="user", text="We are rewriting the backend in Rust."
    )
    store.append_transcript_message(
        session_id=session_id, role="assistant", text="Noted Rust backend rewrite."
    )

    # is_consolidated should now return False because message count (6) > last_consolidated_turn (4)
    assert manager.is_consolidated(session_id) is False

    # Consolidate again
    ok2 = await manager.consolidate_session(session_id)
    assert ok2 is True
    assert provider.call_count == 2

    # Verify state was updated to last_consolidated_turn == 6
    state2 = manager.load_state()
    sess_entry2 = state2["sessions"][session_id]
    assert sess_entry2["last_consolidated_turn"] == 6

    # Verify episode was UPDATED, NOT DUPLICATED!
    episodes2 = store.get_recent_episodes()
    assert len(episodes2) == 1  # STILL EXACTLY ONE EPISODE!
    assert episodes2[0]["summary"] == "Updated discussion: now also uses Rust for services."

    # Verify new fact was added to MEMORY.md
    long_term = store.read_long_term()
    assert "Works at Acme Corp." in long_term
    assert "Primary stack is TypeScript." in long_term
    assert "Uses Rust for microservices." in long_term


async def test_resumed_trivial_session_upgrades_to_full_consolidation(tmp_path: Path) -> None:
    """Test that resuming trivial session upgrades stub episode and extracts facts."""
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    session_id = "sess-trivial-then-resumed"

    # Only 1 user turn (trivial)
    store.append_transcript_message(session_id=session_id, role="user", text="Hi Clea!")
    store.append_transcript_message(session_id=session_id, role="assistant", text="Hello!")

    valid_json = json.dumps(
        {
            "summary": "User introduced their name as Alex and setup.",
            "operations": [
                {
                    "op": "add",
                    "target_section": "Profile",
                    "text": "User name is Alex.",
                }
            ],
        }
    )

    provider = _RecordingProvider([valid_json])
    manager = ConsolidationManager(memory=store, provider=provider)

    # First consolidation: trivial session skipped
    ok1 = await manager.consolidate_session(session_id)
    assert ok1 is True
    assert provider.call_count == 0  # no model call

    # State records trivial and last_consolidated_turn == 2
    state1 = manager.load_state()
    assert state1["sessions"][session_id]["trivial"] is True
    assert state1["sessions"][session_id]["last_consolidated_turn"] == 2

    episodes1 = store.get_recent_episodes()
    assert len(episodes1) == 1
    assert episodes1[0]["trivial"] is True
    assert "Short session" in episodes1[0]["summary"]

    # Resume session with substantive turns
    store.append_transcript_message(
        session_id=session_id, role="user", text="My name is Alex and I use macOS."
    )
    store.append_transcript_message(
        session_id=session_id, role="assistant", text="Nice to meet you, Alex."
    )

    assert manager.is_consolidated(session_id) is False

    # Second consolidation: now non-trivial (2 user turns)
    ok2 = await manager.consolidate_session(session_id)
    assert ok2 is True
    assert provider.call_count == 1

    state2 = manager.load_state()
    assert state2["sessions"][session_id]["last_consolidated_turn"] == 4

    episodes2 = store.get_recent_episodes()
    assert len(episodes2) == 1  # Still exactly one episode!
    assert episodes2[0]["trivial"] is False
    assert episodes2[0]["summary"] == "User introduced their name as Alex and setup."
    assert "User name is Alex." in store.read_long_term()


def test_remove_entries_with_bracketed_and_unbracketed_ids(tmp_path: Path) -> None:
    """Test memory store remove_entries matches entry IDs both with and without brackets."""
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    entry1 = store.add_or_update_entry(text="Prefers pytest for testing.", section="Preferences")
    entry2 = store.add_or_update_entry(text="Uses PostgreSQL for databases.", section="Environment")

    # Match by unbracketed ID
    matches_unbracketed = store.find_matches(entry1.id)
    assert len(matches_unbracketed) == 1
    assert matches_unbracketed[0].id == entry1.id

    # Match by bracketed ID [id]
    matches_bracketed = store.find_matches(f"[{entry2.id}]")
    assert len(matches_bracketed) == 1
    assert matches_bracketed[0].id == entry2.id

    # Remove with bracketed query
    removed = store.remove_entries(f"[{entry2.id}]")
    assert len(removed) == 1
    assert removed[0].id == entry2.id
    assert "PostgreSQL" not in store.read_long_term()
    assert "pytest" in store.read_long_term()


async def test_legacy_consolidation_state_migrates_and_triggers_on_resumption(
    tmp_path: Path,
) -> None:
    """Test that a legacy session in state without last_consolidated_turn triggers when resumed."""
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    session_id = "sess-legacy-upgrade"
    store.append_transcript_message(session_id=session_id, role="user", text="I write Python.")
    store.append_transcript_message(session_id=session_id, role="assistant", text="Noted Python.")

    json_resumed = json.dumps(
        {
            "summary": "User writes Python and Go.",
            "operations": [
                {
                    "op": "add",
                    "target_section": "Preferences",
                    "text": "Writes Python and Go.",
                }
            ],
        }
    )

    provider = _RecordingProvider([json_resumed])
    manager = ConsolidationManager(memory=store, provider=provider)

    # Simulate legacy state entry WITHOUT last_consolidated_turn
    state = manager.load_state()
    state.setdefault("sessions", {})[session_id] = {
        "status": "completed",
        "consolidated_at": "2026-11-12T10:00:00Z",
    }
    manager.save_state(state)

    # Initial check should identify as consolidated and populate last_consolidated_turn == 2
    assert manager.is_consolidated(session_id) is True
    updated_state = manager.load_state()
    assert updated_state["sessions"][session_id]["last_consolidated_turn"] == 2

    # Now user resumes conversation
    store.append_transcript_message(session_id=session_id, role="user", text="I also write Go.")
    store.append_transcript_message(session_id=session_id, role="assistant", text="Noted Go.")

    # Should detect new turns and re-consolidate
    assert manager.is_consolidated(session_id) is False
    ok = await manager.consolidate_session(session_id)
    assert ok is True
    assert provider.call_count == 1

    final_state = manager.load_state()
    assert final_state["sessions"][session_id]["last_consolidated_turn"] == 4
    assert "Writes Python and Go." in store.read_long_term()
