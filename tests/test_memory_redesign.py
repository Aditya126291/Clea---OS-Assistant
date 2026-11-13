"""Tests for Part B: Memory Redesign, Storage, Consolidation, and Tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clea.memory.consolidation import ConsolidationManager
from clea.memory.store import MemoryStore, is_secret_or_sensitive
from clea.providers.base import LLMProvider, Message, StreamChunk, ToolCall
from clea.tools.memory_tools import register_memory_tools
from clea.tools.registry import RiskLevel, ToolRegistry


class _FakeConsolidationProvider(LLMProvider):
    provider_name = "fake-consolidation"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.call_count = 0

    async def stream_turn(self, *, messages: list[Message], tools: list[Any], system_prompt: str):
        self.call_count += 1
        resp = self.responses.pop(0) if self.responses else "{}"
        yield StreamChunk(type="text_delta", text=resp)
        yield StreamChunk(type="done")


def test_secret_filter_rejects_credentials() -> None:
    assert is_secret_or_sensitive("api_key = 'AIzaSyA1234567890abcdefghijklmnopqrstuv'")
    assert is_secret_or_sensitive("sk-abcdefghijklmnopqrstuvwxyz123456")
    assert is_secret_or_sensitive("password: supersecretpassword123")
    assert is_secret_or_sensitive("bearer: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xyz")
    assert is_secret_or_sensitive("My card number is 4532 0123 4567 8910")
    assert is_secret_or_sensitive("Aadhaar: 1234 5678 9012")
    assert not is_secret_or_sensitive("I live in Bengaluru, India.")
    assert not is_secret_or_sensitive("I prefer concise Python code.")


def test_memory_store_fixed_sections_and_entry_formatting(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    entry = store.add_or_update_entry(
        text="Lives in Bengaluru, India.",
        section="Profile",
        source="stated",
    )
    assert entry.id.startswith("prof-")
    assert "Lives in Bengaluru, India." in store.read_long_term()
    assert ", stated)" in store.read_long_term()

    # Secret entry is rejected
    with pytest.raises(ValueError, match="sensitive credentials"):
        store.add_or_update_entry(
            text="API key is sk-1234567890abcdef1234567890",
            section="Preferences",
        )


def test_memory_atomic_write_and_backup(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    store.write_long_term("# Version 1\n")
    bak_path = store.long_term_path.with_name(store.long_term_path.name + ".bak")

    store.write_long_term("# Version 2\n")
    assert store.read_long_term() == "# Version 2\n"
    assert bak_path.exists()
    assert bak_path.read_text(encoding="utf-8") == "# Version 1\n"


def test_memory_deduplication_and_contradiction_handling(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    # Add initial preference
    e1 = store.add_or_update_entry(
        text="Prefers short concise answers.",
        section="Preferences",
        source="stated",
    )
    content1 = store.read_long_term()
    assert "Prefers short concise answers." in content1

    # Adding a near-duplicate updates the entry instead of appending
    e2 = store.add_or_update_entry(
        text="Prefers short concise answers.",
        section="Preferences",
        source="confirmed",
        entry_id=e1.id,
    )
    content2 = store.read_long_term()
    assert content2.count("concise answers") == 1
    assert e2.source == "confirmed"


def test_memory_size_cap_archives_low_confidence_to_archive_md(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path, token_cap=50)  # very small cap
    store.ensure_initialized()

    # Add a stated entry (MUST NEVER be archived)
    store.add_or_update_entry(
        text="User is an engineer named Aditya.",
        section="Profile",
        source="stated",
    )

    # Add old low-confidence entry
    store.add_or_update_entry(
        text="Might possibly enjoy reading sci-fi novels in spare time.",
        section="Preferences",
        source="inferred-low-confidence",
    )

    # Trigger another entry that puts it over the token cap
    store.add_or_update_entry(
        text="Another extra inferred entry about hobbies and sports.",
        section="Preferences",
        source="inferred-low-confidence",
    )

    archive_file = tmp_path / "memory" / "MEMORY_ARCHIVE.md"
    assert archive_file.exists()
    archive_text = archive_file.read_text(encoding="utf-8")
    assert "Might possibly enjoy reading sci-fi" in archive_text

    # Stated entry remains in MEMORY.md
    long_term = store.read_long_term()
    assert "Aditya" in long_term


def test_legacy_daily_log_migration_is_idempotent(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    # Create legacy daily log
    legacy_file = store.memory_dir / "2026-11-10.md"
    legacy_file.write_text(
        "# Daily Log — 2026-11-10\n\n"
        "## 10:15:00 UTC (session `sess_legacy_1`)\n\n"
        "**User:** How do I run tests?\n\n"
        "**Clea:** Run uv run pytest.\n\n"
        "## 10:16:00 UTC (session `sess_legacy_1`)\n\n"
        "**User:** Thanks!\n\n"
        "**Clea:** You're welcome!\n",
        encoding="utf-8",
    )

    res1 = store.migrate_legacy_daily_logs()
    assert res1["migrated_sessions"] == 1
    assert (store.backup_legacy_dir / "2026-11-10.md").exists()

    transcript_path = store.transcripts_dir / "sess_legacy_1.jsonl"
    assert transcript_path.exists()

    transcript = store.get_transcript("sess_legacy_1")
    assert transcript is not None
    assert transcript["session_id"] == "sess_legacy_1"
    assert len(transcript["messages"]) == 4
    assert transcript["messages"][0]["text"] == "How do I run tests?"
    assert transcript["messages"][1]["text"] == "Run uv run pytest."

    # Running migration again is idempotent (safe to run twice)
    res2 = store.migrate_legacy_daily_logs()
    assert res2["migrated_sessions"] == 0
    # Data is intact
    assert len(store.get_transcript("sess_legacy_1")["messages"]) == 4


async def test_remember_and_forget_tools(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()
    registry = ToolRegistry()
    register_memory_tools(registry, store)

    remember_tool = registry.get("remember")
    assert remember_tool.risk_level == RiskLevel.REVERSIBLE

    forget_tool = registry.get("forget")
    assert forget_tool.risk_level == RiskLevel.DESTRUCTIVE

    # Run remember tool
    remember_res = await registry.execute(
        "remember",
        {"fact": "Prefers dark mode in all applications.", "section": "Preferences"},
        confirm=lambda defn, args: True,
    )
    assert "Remembered" in remember_res
    assert "dark mode" in store.read_long_term()

    # Forget tool without approval is denied
    with pytest.raises(Exception, match="Permission denied"):
        await registry.execute(
            "forget",
            {"query": "dark mode"},
            confirm=lambda defn, args: False,
        )

    # Forget tool with approval deletes the entry
    forget_res = await registry.execute(
        "forget",
        {"query": "dark mode"},
        confirm=lambda defn, args: True,
    )
    assert "Forgot 1 entries" in forget_res
    assert "dark mode" not in store.read_long_term()


async def test_consolidation_success_and_crash_recovery(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    # Append a session transcript (2 turns so not skipped as trivial)
    store.append_transcript_message(
        session_id="s-cons-1",
        role="user",
        text="I am working on Project Clea in Bengaluru.",
        title="Project Clea setup",
    )
    store.append_transcript_message(
        session_id="s-cons-1",
        role="assistant",
        text="Understood, Clea is a local desktop assistant.",
    )
    store.append_transcript_message(
        session_id="s-cons-1",
        role="user",
        text="Clea should store memory locally in markdown files.",
    )
    store.append_transcript_message(
        session_id="s-cons-1",
        role="assistant",
        text="Got it, local markdown memory.",
    )

    valid_json = json.dumps(
        {
            "summary": (
                "User introduced Project Clea and their location in Bengaluru. "
                "Agreed on architecture goals."
            ),
            "operations": [
                {
                    "op": "add",
                    "target_section": "Profile",
                    "text": "Located in Bengaluru.",
                    "source_tag": "stated",
                    "reason": "User explicitly stated their location",
                },
                {
                    "op": "add",
                    "target_section": "Projects",
                    "text": "Working on Project Clea.",
                    "source_tag": "stated",
                    "reason": "User stated active project",
                },
            ],
        }
    )

    provider = _FakeConsolidationProvider([valid_json])
    manager = ConsolidationManager(memory=store, provider=provider)

    task_status_calls = []

    async def on_status(st: str, detail: str | None = None) -> None:
        task_status_calls.append((st, detail))

    ok = await manager.consolidate_session("s-cons-1", on_task_status=on_status)
    assert ok is True
    assert task_status_calls == [("running", None), ("succeeded", None)]

    # Check episode summary created
    episodes = store.get_recent_episodes()
    assert len(episodes) == 1
    assert "Project Clea" in episodes[0]["summary"]

    # Check MEMORY.md updated
    long_term = store.read_long_term()
    assert "Located in Bengaluru." in long_term
    assert "Working on Project Clea." in long_term

    # Check state recorded
    assert manager.is_consolidated("s-cons-1")

    # Calling consolidate again does not re-process
    ok2 = await manager.consolidate_session("s-cons-1")
    assert ok2 is True
    assert provider.call_count == 1  # no new LLM calls


async def test_consolidation_invalid_json_retries_once_then_marks_failed(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    store.append_transcript_message(
        session_id="s-bad-json",
        role="user",
        text="Hello world",
    )
    store.append_transcript_message(
        session_id="s-bad-json",
        role="assistant",
        text="Hi there",
    )
    store.append_transcript_message(
        session_id="s-bad-json",
        role="user",
        text="Please save that I like python",
    )

    # Provider returns invalid JSON both on attempt 1 and attempt 2 (retry)
    provider = _FakeConsolidationProvider(["not json at all", "{invalid json again}"])
    manager = ConsolidationManager(memory=store, provider=provider)

    ok = await manager.consolidate_session("s-bad-json")
    assert ok is False
    assert provider.call_count == 2  # exactly one retry

    # Transcript must not be lost
    assert store.get_transcript("s-bad-json") is not None

    # Status reflects failure
    state = manager.load_state()
    assert state["sessions"]["s-bad-json"]["status"] == "failed"
    _, failed_notice = manager.get_status()
    assert failed_notice is not None
    assert "s-bad-js" in failed_notice


def test_memory_injection_token_cap_and_no_raw_transcripts(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    store.write_long_term(
        "# Clea Memory\n\n## Profile\n- [prof-1] (2026-11-13, stated) Engineer.\n"
    )
    store.append_episode(
        session_id="s1",
        summary="Discussed architecture and memory redesign.",
    )
    store.append_transcript_message(
        session_id="s1",
        role="user",
        text="Raw confidential transcript text that should never be injected.",
    )

    injected = store.build_injected_context(max_episodes=5, token_cap=2000)
    assert "Engineer." in injected
    assert "Discussed architecture" in injected
    assert "Raw confidential transcript text" not in injected


def test_get_recent_episodes_returns_strictly_newest_first_same_day(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    # Append 6 episodes across the day
    for i in range(1, 7):
        store.append_episode(
            session_id=f"sess-{i}",
            summary=f"Episode summary for session {i}",
        )

    recent = store.get_recent_episodes(limit=3)
    assert len(recent) == 3
    # Must be newest first: sess-6, sess-5, sess-4
    assert recent[0]["session_id"] == "sess-6"
    assert recent[1]["session_id"] == "sess-5"
    assert recent[2]["session_id"] == "sess-4"


async def test_forget_tool_effective_confirm_with_matches_in_agent_loop(tmp_path: Path) -> None:
    """Ensure agent loop effective_confirm enriches matches without raising AttributeError."""
    from clea.core.agent import AgentLoop
    from clea.core.sessions import Session
    from clea.tools.registry import ToolDefinition

    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()
    store.add_or_update_entry(
        text="User prefers light theme.",
        section="Preferences",
        source="stated",
    )

    registry = ToolRegistry()
    register_memory_tools(registry, store)

    class _ForgetInvokingProvider(LLMProvider):
        provider_name = "forget-invoker"

        def __init__(self) -> None:
            self.turn = 0

        async def stream_turn(
            self, *, messages: list[Message], tools: list[Any], system_prompt: str
        ):
            self.turn += 1
            if self.turn == 1:
                yield StreamChunk(
                    type="tool_call",
                    tool_call=ToolCall(
                        id="c_forget", name="forget", arguments={"query": "light theme"}
                    ),
                )
                yield StreamChunk(type="done")
            else:
                yield StreamChunk(type="text_delta", text="I forgot that preference for you.")
                yield StreamChunk(type="done")

    agent = AgentLoop(
        provider=_ForgetInvokingProvider(),
        registry=registry,
        memory=store,
    )
    session = Session(id="s-forget")

    confirmed_args = []

    async def confirm_cb(definition: ToolDefinition, arguments: dict[str, Any]) -> bool:
        confirmed_args.append(arguments)
        return True

    res = await agent.run_turn(
        session=session,
        user_text="Forget my light theme preference",
        confirm=confirm_cb,
    )
    assert "I forgot that preference" in res
    assert len(confirmed_args) == 1
    assert "matches" in confirmed_args[0]
    matched = confirmed_args[0]["matches"]
    assert len(matched) == 1
    assert matched[0]["section"] == "Preferences"
    assert "light theme" in matched[0]["text"]
    assert "light theme" not in store.read_long_term()


def test_memory_decision2_condensation_merges_before_archiving(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path, token_cap=70)
    store.ensure_initialized()

    # Add two near-duplicate/overlapping entries in Preferences
    store.add_or_update_entry(
        text="Prefers short concise terminal answers.",
        section="Preferences",
        source="stated",
    )
    store.add_or_update_entry(
        text="Prefers concise terminal answers with code.",
        section="Preferences",
        source="confirmed",
    )

    long_term = store.read_long_term()
    # Condensation merged them rather than archiving
    assert "terminal answers" in long_term


def test_history_cap_preserves_user_turn_boundary_without_orphaning_tools(tmp_path: Path) -> None:
    from clea.core.agent import AgentLoop
    from clea.core.sessions import Session

    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    agent = AgentLoop(
        provider=_FakeConsolidationProvider([]),
        registry=ToolRegistry(),
        memory=store,
        max_history_messages=4,
    )
    session = Session(id="s-cap-tool")

    # Set up messages with a tool call in the middle
    session.messages = [
        Message(role="user", content="Turn 1 user"),
        Message(role="assistant", content="Turn 1 assistant"),
        Message(role="user", content="Turn 2 user"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="call_1", name="dummy", arguments={})],
        ),
        Message(role="tool", content="tool output", tool_call_id="call_1"),
        Message(role="assistant", content="Turn 2 final assistant"),
    ]

    agent._apply_history_cap(session, keep_recent=2)

    # First message in history must be a user message (not an orphaned tool or assistant)
    assert session.messages[0].role == "user"
    assert "[Earlier conversation summary:" in session.messages[0].content
    # Subsequent messages must preserve tool call/result pairing
    for idx, msg in enumerate(session.messages):
        if msg.role == "tool":
            prev = session.messages[idx - 1]
            assert prev.role == "assistant"
            assert prev.tool_calls is not None
