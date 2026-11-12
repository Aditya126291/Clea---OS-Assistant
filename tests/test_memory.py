"""Tests for the Markdown memory store."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

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
