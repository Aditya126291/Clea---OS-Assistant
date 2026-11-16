"""Tests for Streamline Companion Logs, Retention Pruning, and End-of-Session Compaction."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from clea.core.agent import AgentLoop
from clea.memory.consolidation import (
    ConsolidationManager,
    is_meaningful_turn,
)
from clea.memory.retrieval import EmbeddingProvider
from clea.memory.store import (
    VOICE_COMPANION_SESSION_ID,
    MemoryStore,
    prune_diagnostic_logs,
)
from clea.providers.base import LLMProvider, Message, StreamChunk
from clea.tools.registry import ToolRegistry


class _MockConsolidationProvider(LLMProvider):
    """Mock LLM Provider that returns structured consolidation outputs or tracks calls."""

    provider_name = "mock-consolidation"

    def __init__(self, response_payload: dict[str, Any] | None = None) -> None:
        self.call_count = 0
        self.prompts: list[str] = []
        self.response_payload = response_payload

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
        self.prompts.append(prompt)

        payload = self.response_payload or {
            "summary": "Mock session summary",
            "operations": [],
        }
        yield StreamChunk(type="text_delta", text=json.dumps(payload))
        yield StreamChunk(type="done")


class _FailingProvider(LLMProvider):
    """Provider that raises an exception to test error resilience."""

    provider_name = "failing-provider"

    def __init__(self) -> None:
        self.call_count = 0

    async def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[Any],
        system_prompt: str,
        response_format: dict[str, Any] | None = None,
    ):
        self.call_count += 1
        raise RuntimeError("Transient provider rate limit or network failure")
        yield StreamChunk(type="done")


def test_meaningful_turn_filter() -> None:
    """Verify filler chatter is rejected and substantive utterances are accepted."""
    assert not is_meaningful_turn("")
    assert not is_meaningful_turn("   ")
    assert not is_meaningful_turn("um")
    assert not is_meaningful_turn("uh")
    assert not is_meaningful_turn("wait")
    assert not is_meaningful_turn("hold on")
    assert not is_meaningful_turn("try again")
    assert not is_meaningful_turn("hello")
    assert not is_meaningful_turn("ok")
    assert not is_meaningful_turn("um, wait...")
    assert not is_meaningful_turn("uh okay yeah")

    assert is_meaningful_turn("User prefers Python and Rust for new backend services.")
    assert is_meaningful_turn("My project is located at C:/Users/Projects/Clea.")
    assert is_meaningful_turn("Please remember that I prefer dark theme.")
    assert is_meaningful_turn("Switch our default port to 8765.")


def _mock_embed(text: str) -> list[float]:
    vec = [0.0] * 768
    t = text.lower()
    if "typescript" in t:
        vec[0] = 1.0
    elif "frontend" in t:
        vec[1] = 1.0
    elif "diagnostic" in t:
        vec[2] = 1.0
    return vec


def test_strict_isolation_from_long_term_memory(tmp_path: Path) -> None:
    """Verify raw companion logs and diagnostic files are NEVER queried or injected."""
    mem = MemoryStore(
        base_dir=tmp_path,
        embedding_provider=EmbeddingProvider(embed_fn=_mock_embed),
    )
    mem.ensure_initialized()

    # 1. Curate a durable fact and episode into long-term memory
    mem.add_or_update_entry(
        text="User prefers TypeScript for web development.",
        section="Preferences",
        entry_id="pref-ts01",
    )
    mem.append_episode(
        session_id="chat-1",
        summary="User discussed frontend architecture and state management.",
    )

    # 2. Append raw voice companion turns with distinct keywords
    mem.append_voice_companion_call(
        call_id="call-diag-1",
        entries=[
            ("user", "diagnostic_raw_voice_turn_speech", datetime.now().astimezone()),
            ("assistant", "diagnostic_raw_voice_turn_reply", datetime.now().astimezone()),
        ],
    )

    # 3. Write diagnostic logs (daemon.log, voice.log, audit.jsonl)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "daemon.log").write_text(
        "DEBUG: diagnostic_daemon_raw_log_entry\n", encoding="utf-8"
    )
    (logs_dir / "voice.log").write_text("DEBUG: diagnostic_voice_raw_log_entry\n", encoding="utf-8")
    (logs_dir / "audit.jsonl").write_text(
        json.dumps({"timestamp": datetime.now().isoformat(), "tool": "diagnostic_audit_call"})
        + "\n",
        encoding="utf-8",
    )

    # 4. Search memory directly: assert ONLY durable facts and episodes are searchable
    hits = mem.search_memory("diagnostic_raw_voice_turn")
    assert len(hits) == 0

    hits_daemon = mem.search_memory("diagnostic_daemon_raw_log")
    assert len(hits_daemon) == 0

    hits_audit = mem.search_memory("diagnostic_audit_call")
    assert len(hits_audit) == 0

    fact_hits = mem.search_memory("TypeScript")
    assert len(fact_hits) > 0
    assert fact_hits[0]["text"] == "User prefers TypeScript for web development."

    # 5. Agent system prompt: assert raw companion turns and logs are NEVER injected
    agent = AgentLoop(
        provider=_MockConsolidationProvider(),
        registry=ToolRegistry(),
        memory=mem,
    )
    system_prompt = agent._build_system_prompt()
    assert "diagnostic_raw_voice_turn" not in system_prompt
    assert "diagnostic_daemon_raw_log" not in system_prompt
    assert "diagnostic_audit_call" not in system_prompt
    assert "User prefers TypeScript for web development." in system_prompt

    # 6. Embeddings backfill: assert only facts and episodes are embedded
    mem.backfill_embeddings()
    # fact:pref-ts01 and episode:chat-1
    assert mem.embedding_store.get_vector("fact:pref-ts01") is not None
    assert mem.embedding_store.get_vector("episode:chat-1") is not None
    assert mem.embedding_store.get_vector(f"turn:{VOICE_COMPANION_SESSION_ID}") is None
    assert mem.embedding_store.get_vector("log:daemon") is None


def test_prune_diagnostic_logs_ttl_and_caps(tmp_path: Path) -> None:
    """Verify prune_diagnostic_logs prunes entries older than 7 days and caps records."""
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()

    now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)
    old_time = now - timedelta(days=10)
    recent_time = now - timedelta(days=2)

    # 1. Populate voice_companion.jsonl with old and recent entries
    vc_entries: list[tuple[str, str, datetime]] = []
    # 50 old turns (> 7 days)
    for i in range(50):
        vc_entries.append(("user", f"Old turn {i}", old_time + timedelta(minutes=i)))
    # 550 recent turns (< 7 days)
    for i in range(550):
        vc_entries.append(("user", f"Recent turn {i}", recent_time + timedelta(minutes=i)))

    mem.append_voice_companion_call(call_id="call-old", entries=vc_entries[:50])
    mem.append_voice_companion_call(call_id="call-recent", entries=vc_entries[50:])

    # 2. Populate voice_metrics.jsonl with old and excess records
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = logs_dir / "voice_metrics.jsonl"
    with metrics_path.open("w", encoding="utf-8") as f:
        for i in range(30):
            f.write(
                json.dumps(
                    {
                        "call_id": f"m-old-{i}",
                        "timestamp": (old_time + timedelta(hours=i)).isoformat(),
                    }
                )
                + "\n"
            )
        for i in range(520):
            f.write(
                json.dumps(
                    {
                        "call_id": f"m-rec-{i}",
                        "timestamp": (recent_time + timedelta(minutes=i)).isoformat(),
                    }
                )
                + "\n"
            )

    # 3. Populate audit.jsonl with old and excess records
    audit_path = logs_dir / "audit.jsonl"
    with audit_path.open("w", encoding="utf-8") as f:
        for i in range(25):
            f.write(
                json.dumps(
                    {
                        "action": f"tool_old_{i}",
                        "timestamp": (old_time + timedelta(hours=i)).isoformat(),
                    }
                )
                + "\n"
            )
        for i in range(510):
            f.write(
                json.dumps(
                    {
                        "action": f"tool_rec_{i}",
                        "timestamp": (recent_time + timedelta(minutes=i)).isoformat(),
                    }
                )
                + "\n"
            )

    # 4. Create rotated log files with old mtime
    old_daemon_log = logs_dir / "daemon.log.1"
    old_daemon_log.write_text("Old daemon log\n", encoding="utf-8")
    import os

    old_epoch = (old_time).timestamp()
    os.utime(old_daemon_log, (old_epoch, old_epoch))

    recent_daemon_log = logs_dir / "daemon.log.2"
    recent_daemon_log.write_text("Recent daemon log\n", encoding="utf-8")
    recent_epoch = (recent_time).timestamp()
    os.utime(recent_daemon_log, (recent_epoch, recent_epoch))

    # 5. Populate curated MEMORY.md and saved HISTORY conversation threads
    mem.add_or_update_entry(
        text="User's primary project is Clea.",
        section="Projects",
        entry_id="proj-clea",
    )
    # Save a chat thread in transcripts/
    chat_file = tmp_path / "logs" / "transcripts" / "saved_history_chat.jsonl"
    chat_header = {
        "session_id": "saved_history_chat",
        "title": "Old Important Chat",
        "updated_at": old_time.isoformat(),
    }
    chat_turns = [
        json.dumps(
            {
                "role": "user",
                "text": "Very old conversation turn",
                "timestamp": old_time.isoformat(),
            }
        ),
        json.dumps(
            {
                "role": "assistant",
                "text": "Very old conversation answer",
                "timestamp": old_time.isoformat(),
            }
        ),
    ]
    chat_file.write_text(
        json.dumps(chat_header) + "\n" + "\n".join(chat_turns) + "\n", encoding="utf-8"
    )

    # RUN PRUNING
    result = prune_diagnostic_logs(
        max_age_days=7,
        max_entries=500,
        data_dir=tmp_path,
        now=now,
    )

    # ASSERT PRUNING RESULTS
    # 50 old turns + 50 excess (> 500) = 100 turns pruned from voice_companion
    assert result["pruned_companion_turns"] == 100
    # 30 old + 20 excess = 50 metrics pruned
    assert result["pruned_voice_metrics"] == 50
    # 25 old + 10 excess = 35 audit records pruned
    assert result["pruned_audit_records"] == 35
    # Old daemon log rotated file pruned
    assert result["pruned_log_files"] == 1
    assert not old_daemon_log.exists()
    assert recent_daemon_log.exists()

    # Verify voice companion remaining lines
    with mem.transcript_path(VOICE_COMPANION_SESSION_ID).open("r", encoding="utf-8") as f:
        vc_lines = [line.strip() for line in f if line.strip()]
    assert len(vc_lines) == 501  # 1 header + 500 messages

    # GUARANTEE: Curated Memory is 100% untouched
    sections = mem.parse_sections()
    assert any(
        e.id == "proj-clea" and e.text == "User's primary project is Clea."
        for e in sections["Projects"]
    )

    # GUARANTEE: Saved HISTORY conversation threads are 100% untouched
    assert chat_file.exists()
    tr = mem.get_transcript("saved_history_chat")
    assert tr is not None
    assert len(tr["messages"]) == 2
    assert tr["messages"][0]["text"] == "Very old conversation turn"

    # IDEMPOTENCY: Running prune_diagnostic_logs again results in 0 pruned
    idempotent_result = prune_diagnostic_logs(
        max_age_days=7,
        max_entries=500,
        data_dir=tmp_path,
        now=now,
    )
    assert idempotent_result["pruned_companion_turns"] == 0
    assert idempotent_result["pruned_voice_metrics"] == 0
    assert idempotent_result["pruned_audit_records"] == 0
    assert idempotent_result["pruned_log_files"] == 0
    assert idempotent_result["total_pruned"] == 0


@pytest.mark.asyncio
async def test_end_of_session_compaction_reconciliation(tmp_path: Path) -> None:
    """Verify asynchronous session compaction reconciles ADD, UPDATE, and DELETE operations."""
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()

    # Pre-populate existing facts in MEMORY.md
    mem.add_or_update_entry(
        text="User's preferred editor is VS Code.",
        section="Preferences",
        entry_id="pref-edit01",
    )
    mem.add_or_update_entry(
        text="User lives in Seattle.",
        section="Profile",
        entry_id="prof-loc01",
    )

    # Append voice call turns
    mem.append_voice_companion_call(
        call_id="call-reconcile-1",
        entries=[
            (
                "user",
                "I moved from Seattle to San Francisco, and I switched my editor to Cursor.",
                datetime.now().astimezone(),
            ),
            (
                "assistant",
                "Got it, I will remember that you moved to San Francisco and now use Cursor.",
                datetime.now().astimezone(),
            ),
            (
                "user",
                "Also, I started working on a new Rust CLI project.",
                datetime.now().astimezone(),
            ),
            (
                "assistant",
                "Understood, noted your new Rust CLI project.",
                datetime.now().astimezone(),
            ),
        ],
    )

    # Mock provider returning ADD (new fact), UPDATE (replaces editor), and DELETE
    mock_provider = _MockConsolidationProvider(
        response_payload={
            "summary": (
                "User updated location to San Francisco, editor preference to Cursor, "
                "and started Rust CLI project."
            ),
            "operations": [
                {
                    "op": "add",
                    "target_section": "Projects",
                    "text": "User is building a new Rust CLI project.",
                    "reason": "New project mentioned",
                },
                {
                    "op": "update",
                    "entry_id": "pref-edit01",
                    "target_section": "Preferences",
                    "text": "User's preferred editor is Cursor.",
                    "reason": "User switched from VS Code to Cursor",
                },
                {
                    "op": "delete",
                    "entry_id": "prof-loc01",
                    "target_section": "Profile",
                    "text": "User lives in Seattle.",
                    "reason": "User moved away from Seattle",
                },
                {
                    "op": "add",
                    "target_section": "Profile",
                    "text": "User lives in San Francisco.",
                    "reason": "New location stated",
                },
            ],
        }
    )

    manager = ConsolidationManager(
        memory=mem,
        provider=mock_provider,
    )

    # Run consolidation
    success = await manager.consolidate_session(VOICE_COMPANION_SESSION_ID, force=True)
    assert success is True
    assert mock_provider.call_count == 1

    # Verify prompt provided existing memory entries to the LLM
    prompt_sent = mock_provider.prompts[0]
    assert "Existing Long-Term Memory Entries:" in prompt_sent
    assert "[pref-edit01] (Preferences) User's preferred editor is VS Code." in prompt_sent
    assert "[prof-loc01] (Profile) User lives in Seattle." in prompt_sent

    # Verify MEMORY.md was accurately updated:
    # 1. Old location deleted
    sections = mem.parse_sections()
    assert not any(e.id == "prof-loc01" for e in sections["Profile"])
    assert not any("Seattle" in e.text for e in sections["Profile"])
    # 2. New location added
    assert any("San Francisco" in e.text for e in sections["Profile"])
    # 3. Editor updated in place under same ID
    editor_entry = next((e for e in sections["Preferences"] if e.id == "pref-edit01"), None)
    assert editor_entry is not None
    assert editor_entry.text == "User's preferred editor is Cursor."
    # 4. New project added
    assert any("Rust CLI" in e.text for e in sections["Projects"])


@pytest.mark.asyncio
async def test_short_and_empty_sessions_handled_gracefully(tmp_path: Path) -> None:
    """Verify sessions with < 2 meaningful turns skip LLM call gracefully without raising."""
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()

    mock_provider = _MockConsolidationProvider()
    manager = ConsolidationManager(memory=mem, provider=mock_provider)

    # 1. Empty session
    success_empty = await manager.consolidate_session("empty-sess", force=True)
    assert success_empty is True
    assert mock_provider.call_count == 0

    # 2. Short session with only filler chatter ("um", "wait")
    filler_call_id = "filler-call"
    mem.append_voice_companion_call(
        call_id=filler_call_id,
        entries=[
            ("user", "um...", datetime.now().astimezone()),
            ("assistant", "Hello?", datetime.now().astimezone()),
            ("user", "wait, never mind", datetime.now().astimezone()),
        ],
    )
    # Check is_trivial_session detects < 2 meaningful turns
    tr = mem.get_transcript(filler_call_id)
    assert tr is not None
    is_triv, meaningful_turns = manager.is_trivial_session(tr)
    assert is_triv is True
    assert meaningful_turns < 2

    # Consolidating filler call completes without calling LLM
    success_filler = await manager.consolidate_session(filler_call_id, force=True)
    assert success_filler is True
    assert mock_provider.call_count == 0


@pytest.mark.asyncio
async def test_transient_api_errors_handled_gracefully(tmp_path: Path) -> None:
    """Verify transient provider errors during consolidation log safely without crashing."""
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()

    mem.append_voice_companion_call(
        call_id="call-fail-1",
        entries=[
            ("user", "I prefer using Linux for production servers.", datetime.now().astimezone()),
            ("assistant", "Noted, Linux for production.", datetime.now().astimezone()),
            ("user", "And we use Docker Compose for orchestration.", datetime.now().astimezone()),
        ],
    )

    failing_provider = _FailingProvider()
    manager = ConsolidationManager(memory=mem, provider=failing_provider)

    # Consolidate fails safely, returns False, never raises unhandled exception
    status = await manager.consolidate_session(VOICE_COMPANION_SESSION_ID, force=True)
    assert status is False
    assert failing_provider.call_count > 0

    # State records failed status without crashing
    state = manager.load_state()
    session_state = state["sessions"].get(VOICE_COMPANION_SESSION_ID)
    assert session_state is not None
    assert session_state["status"] == "failed"
