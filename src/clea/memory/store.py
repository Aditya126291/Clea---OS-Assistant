"""Markdown-backed memory store for Clea.

Stores long-term memory in ``MEMORY.md`` and daily conversation logs in
``memory/YYYY-MM-DD.md`` inside the OS user data directory (never inside the repo).
"""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path

from platformdirs import user_data_dir

_DEFAULT_LONG_TERM_HEADER = """# Clea Long-Term Memory

Persistent preferences, facts, and project context live here.
"""


def default_data_dir() -> Path:
    """Return the user data directory for Clea state, memory, and audit logs."""
    override = os.environ.get("CLEA_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path(user_data_dir("clea", appauthor=False))


class MemoryStore:
    """Reads and appends human-readable Markdown memory files."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = (base_dir or default_data_dir()).expanduser().resolve()
        self.memory_dir = self.base_dir / "memory"
        self.long_term_path = self.base_dir / "MEMORY.md"

    def ensure_initialized(self) -> None:
        """Create memory directories and an initial MEMORY.md if missing."""
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        if not self.long_term_path.exists():
            self.long_term_path.write_text(_DEFAULT_LONG_TERM_HEADER, encoding="utf-8")

    def daily_log_path(self, day: date | None = None) -> Path:
        """Return the path for a given day's Markdown log (defaults to today)."""
        target_day = day or datetime.now().astimezone().date()
        return self.memory_dir / f"{target_day.isoformat()}.md"

    def read_long_term(self) -> str:
        """Read MEMORY.md, returning an empty string if it does not exist."""
        if not self.long_term_path.exists():
            return ""
        return self.long_term_path.read_text(encoding="utf-8")

    def write_long_term(self, content: str) -> Path:
        """Overwrite MEMORY.md with updated Markdown content."""
        self.ensure_initialized()
        self.long_term_path.write_text(content, encoding="utf-8")
        return self.long_term_path

    def read_daily(self, day: date | None = None) -> str:
        """Read a daily log file, returning an empty string if absent."""
        path = self.daily_log_path(day)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def append_daily_turn(
        self,
        *,
        session_id: str,
        user_text: str,
        assistant_text: str,
        now: datetime | None = None,
    ) -> Path:
        """Append a conversation turn to today's daily Markdown log."""
        self.ensure_initialized()
        timestamp = now or datetime.now().astimezone()
        path = self.daily_log_path(timestamp.date())

        chunks: list[str] = []
        if not path.exists():
            chunks.append(f"# Daily Log — {timestamp.date().isoformat()}\n")

        time_label = timestamp.strftime("%H:%M:%S %Z").strip()
        chunks.append(
            f"\n## {time_label} (session `{session_id}`)\n\n"
            f"**User:** {user_text.strip()}\n\n"
            f"**Clea:** {assistant_text.strip()}\n"
        )

        with path.open("a", encoding="utf-8") as handle:
            handle.write("".join(chunks))

        return path
