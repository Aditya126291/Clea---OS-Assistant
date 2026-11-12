"""Markdown-backed memory store for Clea.

Stores long-term memory in ``MEMORY.md`` and daily conversation logs in
``memory/YYYY-MM-DD.md`` inside the OS user data directory (never inside the repo).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from clea.paths import default_data_dir

_DEFAULT_LONG_TERM_HEADER = """# Clea Long-Term Memory

Persistent preferences, facts, and project context live here.
"""


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

    # --- Read-only views used by the daemon for surfaces (memory viewer, sidebar) ---

    def list_days(self) -> list[str]:
        """Return ISO dates that have a daily log, newest first."""
        if not self.memory_dir.is_dir():
            return []
        days = [p.stem for p in self.memory_dir.glob("*.md") if _DAY_RE.fullmatch(p.stem)]
        return sorted(days, reverse=True)

    def read_daily_on(self, day: str) -> str:
        """Read the daily log for an ISO date string. Raises ValueError on malformed input."""
        return self.read_daily(parse_day(day))

    def get_conversation(self, day: str, session_id: str) -> list[dict[str, str]]:
        """Return the logged turns (time, user, assistant) for one session on one day."""
        return [
            {"time": time, "user": user, "assistant": assistant}
            for sid, time, user, assistant in _parse_turns(self.read_daily_on(day))
            if sid == session_id
        ]

    def list_conversations(self) -> list[dict[str, str | int]]:
        """Group daily-log session headings into conversations, newest first."""
        conversations: list[dict[str, str | int]] = []
        for day in self.list_days():
            by_session: dict[str, dict[str, str | int]] = {}
            for sid, time, user, _assistant in _parse_turns(self.read_daily_on(day)):
                entry = by_session.get(sid)
                if entry is None:
                    entry = {
                        "date": day,
                        "session_id": sid,
                        "title": _truncate(user),
                        "turns": 0,
                        "last_time": time,
                    }
                    by_session[sid] = entry
                entry["turns"] = int(entry["turns"]) + 1
                entry["last_time"] = time
            conversations.extend(reversed(by_session.values()))
        return conversations


_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_HEADING_RE = re.compile(r"^## (?P<time>.+?) \(session `(?P<sid>[A-Za-z0-9_-]+)`\)\s*$", re.M)
_TURN_RE = re.compile(r"\*\*User:\*\* ?(?P<user>.*?)\n\n\*\*Clea:\*\* ?(?P<assistant>.*)\Z", re.S)


def parse_day(value: str) -> date:
    """Strictly parse YYYY-MM-DD (also guards against path traversal in log lookups)."""
    if not _DAY_RE.fullmatch(value):
        raise ValueError(f"Invalid date '{value}'. Expected YYYY-MM-DD.")
    return date.fromisoformat(value)


def _truncate(text: str, limit: int = 60) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _parse_turns(markdown: str) -> list[tuple[str, str, str, str]]:
    """Parse a daily log into (session_id, time, user_text, assistant_text) tuples."""
    matches = list(_HEADING_RE.finditer(markdown))
    turns: list[tuple[str, str, str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        body = markdown[match.end() : end].strip()
        parsed = _TURN_RE.search(body)
        if parsed:
            user, assistant = parsed.group("user").strip(), parsed.group("assistant").strip()
        else:
            user, assistant = body, ""
        turns.append((match.group("sid"), match.group("time"), user, assistant))
    return turns
