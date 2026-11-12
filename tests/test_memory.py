"""Tests for the Markdown memory store."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from clea.memory import MemoryStore, default_data_dir


def test_default_data_dir_is_outside_repo() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    data_dir = default_data_dir()
    assert not data_dir.is_relative_to(repo_root)


def test_long_term_and_daily_markdown_persistence(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    assert store.long_term_path.exists()
    assert "Clea Long-Term Memory" in store.read_long_term()

    store.write_long_term("# Memory\n\n- User prefers concise answers.\n")
    assert "concise answers" in store.read_long_term()

    ts = datetime(2026, 11, 12, 16, 30, 0, tzinfo=UTC)
    log_path = store.append_daily_turn(
        session_id="abc123",
        user_text="What time is it?",
        assistant_text="It is 16:30 UTC.",
        now=ts,
    )
    assert log_path == tmp_path / "memory" / "2026-11-12.md"
    assert log_path.exists()

    store.append_daily_turn(
        session_id="abc123",
        user_text="Thanks!",
        assistant_text="You're welcome.",
        now=ts,
    )

    daily_content = store.read_daily(ts.date())
    assert "# Daily Log — 2026-11-12" in daily_content
    assert "**User:** What time is it?" in daily_content
    assert "**Clea:** It is 16:30 UTC." in daily_content
    assert "**User:** Thanks!" in daily_content
    assert "**Clea:** You're welcome." in daily_content


def test_list_days_and_conversations_and_traversal_guard(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()
    (store.memory_dir / "notes.md").write_text("ignore me", encoding="utf-8")

    day1 = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
    day2 = datetime(2026, 11, 12, 14, 15, tzinfo=UTC)
    store.append_daily_turn(
        session_id="sess_a", user_text="First question", assistant_text="First answer", now=day1
    )
    store.append_daily_turn(
        session_id="sess_a", user_text="Follow up", assistant_text="Second answer", now=day1
    )
    store.append_daily_turn(
        session_id="sess_b", user_text="Another day", assistant_text="Hi there", now=day2
    )

    assert store.list_days() == ["2026-11-12", "2026-11-10"]
    assert "First question" in store.read_daily_on("2026-11-10")

    with pytest.raises(ValueError, match="Invalid date"):
        store.read_daily_on("../MEMORY")

    conversations = store.list_conversations()
    assert [(c["date"], c["session_id"], c["turns"]) for c in conversations] == [
        ("2026-11-12", "sess_b", 1),
        ("2026-11-10", "sess_a", 2),
    ]
    assert conversations[1]["title"] == "First question"

    turns = store.get_conversation("2026-11-10", "sess_a")
    assert len(turns) == 2
    assert turns[0]["user"] == "First question"
    assert turns[0]["assistant"] == "First answer"
    assert turns[1]["user"] == "Follow up"
    assert store.get_conversation("2026-11-10", "missing") == []
