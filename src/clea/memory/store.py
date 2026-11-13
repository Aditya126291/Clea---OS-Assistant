"""Memory store for Clea managing durable facts, episodes, and transcripts.

Storage layout (all inside user data dir via platformdirs, never in repo):
- memory/MEMORY.md: durable facts and preferences structured into fixed sections.
- memory/MEMORY.md.bak: backup copy preserved before every merge/write.
- memory/MEMORY_ARCHIVE.md: archived low-confidence entries when size cap is reached.
- memory/episodes/YYYY-MM-DD.md: short per-session summaries.
- logs/transcripts/<session_id>.jsonl: raw structured session transcripts.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel

from clea.paths import default_data_dir

logger = logging.getLogger(__name__)

FIXED_SECTIONS = [
    "Profile",
    "Preferences",
    "Environment",
    "Projects",
    "Decisions",
    "Open Threads",
]

SourceTag = Literal["stated", "confirmed", "inferred-low-confidence"]


class MemoryEntry(BaseModel):
    """A single factual memory entry."""

    id: str
    date: str
    source: SourceTag = "stated"
    text: str
    section: str = ""

    def format_line(self) -> str:
        return f"- [{self.id}] ({self.date}, {self.source}) {self.text.strip()}"


# Secret patterns: API keys, tokens, passwords, credit cards, national ID numbers
_SECRET_PATTERNS = [
    re.compile(
        r"(?i)\b(?:api[_-]?key|secret|password|passwd|bearer|token|access[_-]?token|auth[_-]?token|credential)[\s:=]+[\'\"]?[A-Za-z0-9_\-\.]{8,}[\'\"]?"
    ),
    re.compile(r"(?i)\bsk-[a-zA-Z0-9_-]{20,}\b"),
    re.compile(r"(?i)\bAIza[0-9A-Za-z-_]{35}\b"),
    re.compile(r"\b(?:\d[ -]*?){13,19}\b"),  # Credit/debit card numbers
    re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}\b"),  # 12-digit ID numbers (e.g. Aadhaar)
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # US SSN
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
]


def is_secret_or_sensitive(text: str) -> bool:
    """Return True if text appears to contain secrets or credentials."""
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return True
    return False


def estimate_tokens(text: str) -> int:
    """Approximate token count (~4 UTF-8 chars per token)."""
    if not text:
        return 0
    return (len(text) + 3) // 4


_ENTRY_STRICT_RE = re.compile(
    r"^-\s*\[(?P<id>[^\]]+)\]\s*\((?P<date>\d{4}-\d{2}-\d{2}),\s*(?P<source>stated|confirmed|inferred-low-confidence)\)\s*(?P<text>.+)$"
)
_ENTRY_FALLBACK_RE = re.compile(
    r"^-\s*(?:\[(?P<id>[^\]]+)\]\s*)?(?:[\(\[](?P<date>\d{4}-\d{2}-\d{2})?(?:,\s*|\s+)?(?P<source>stated|confirmed|inferred-low-confidence)?[\)\]]\s*)?(?P<text>.+)$"
)


def _format_time_local(dt: datetime) -> str:
    """Format time in local format (e.g. 18:11), without verbose timezone names."""
    return dt.strftime("%H:%M")


class MemoryStore:
    """Manages MEMORY.md, episode summaries, and structured transcripts."""

    def __init__(self, base_dir: Path | None = None, token_cap: int = 2000) -> None:
        self.base_dir = (base_dir or default_data_dir()).expanduser().resolve()
        self.memory_dir = self.base_dir / "memory"
        self.episodes_dir = self.memory_dir / "episodes"
        self.transcripts_dir = self.base_dir / "logs" / "transcripts"
        self.long_term_path = self.base_dir / "MEMORY.md"
        self.archive_path = self.memory_dir / "MEMORY_ARCHIVE.md"
        self.backup_legacy_dir = self.memory_dir / "backup_legacy_logs"
        self.token_cap = token_cap

    def ensure_initialized(self) -> None:
        """Create directories and default MEMORY.md if missing."""
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.episodes_dir.mkdir(parents=True, exist_ok=True)
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)

        if not self.long_term_path.exists():
            initial_text = self._build_empty_long_term_markdown()
            self.long_term_path.write_text(initial_text, encoding="utf-8")

        # Run idempotent migration of legacy daily logs if any exist
        try:
            self.migrate_legacy_daily_logs()
        except Exception as exc:
            logger.warning("Legacy daily log migration notice: %s", exc)

    def _build_empty_long_term_markdown(self) -> str:
        lines = [
            "# Clea Long-Term Memory",
            "",
            "Persistent preferences, facts, and project context live here.",
            "",
        ]
        for sec in FIXED_SECTIONS:
            lines.append(f"## {sec}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def read_long_term(self) -> str:
        """Read MEMORY.md, returning an empty string if missing."""
        if not self.long_term_path.exists():
            return ""
        return self.long_term_path.read_text(encoding="utf-8")

    def write_long_term(self, content: str) -> Path:
        """Overwrite MEMORY.md atomically, keeping MEMORY.md.bak."""
        self.ensure_initialized()
        bak_path = self.long_term_path.with_name(self.long_term_path.name + ".bak")
        if self.long_term_path.exists():
            try:
                shutil.copyfile(self.long_term_path, bak_path)
            except Exception as exc:
                logger.warning("Could not create MEMORY.md.bak: %s", exc)

        tmp_path = self.long_term_path.with_name(f"{self.long_term_path.name}.tmp.{uuid4().hex}")
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(self.long_term_path)
        return self.long_term_path

    # --- Section Parsing & Formatting ---

    def parse_sections(self, content: str | None = None) -> dict[str, list[MemoryEntry]]:
        """Parse MEMORY.md into a map of section names to lists of MemoryEntry."""
        text = content if content is not None else self.read_long_term()
        sections: dict[str, list[MemoryEntry]] = {sec: [] for sec in FIXED_SECTIONS}
        current_sec: str | None = None
        entry_idx = 1

        for line in text.splitlines():
            line_str = line.strip()
            if line_str.startswith("## "):
                sec_name = line_str[3:].strip()
                if sec_name in sections:
                    current_sec = sec_name
                else:
                    current_sec = sec_name
                    if current_sec not in sections:
                        sections[current_sec] = []
                continue

            if not line_str.startswith("- ") or current_sec is None:
                continue

            match = _ENTRY_STRICT_RE.match(line_str)
            if match:
                sections[current_sec].append(
                    MemoryEntry(
                        id=match.group("id"),
                        date=match.group("date"),
                        source=match.group("source"),  # type: ignore[arg-type]
                        text=match.group("text"),
                    )
                )
            else:
                flex_match = _ENTRY_FALLBACK_RE.match(line_str)
                if flex_match:
                    src = flex_match.group("source") or "stated"
                    dt = flex_match.group("date") or datetime.now().astimezone().date().isoformat()
                    eid = flex_match.group("id") or f"e{entry_idx}"
                    entry_idx += 1
                    sections[current_sec].append(
                        MemoryEntry(
                            id=eid,
                            date=dt,
                            source=src,  # type: ignore[arg-type]
                            text=flex_match.group("text"),
                        )
                    )

        return sections

    def format_sections(self, sections: dict[str, list[MemoryEntry]]) -> str:
        """Format parsed memory sections into canonical Markdown text."""
        lines = [
            "# Clea Long-Term Memory",
            "",
            "Persistent preferences, facts, and project context live here.",
            "",
        ]
        for sec in FIXED_SECTIONS:
            lines.append(f"## {sec}")
            entries = sections.get(sec, [])
            for entry in entries:
                lines.append(entry.format_line())
            lines.append("")

        for sec, entries in sections.items():
            if sec not in FIXED_SECTIONS and entries:
                lines.append(f"## {sec}")
                for entry in entries:
                    lines.append(entry.format_line())
                lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    # --- Memory Operations (Remember, Forget, Deduplicate, Archive) ---

    def add_or_update_entry(
        self,
        *,
        text: str,
        section: str = "Preferences",
        source: SourceTag = "stated",
        entry_id: str | None = None,
        now: datetime | None = None,
    ) -> MemoryEntry:
        """Add or update an entry in MEMORY.md with safety and deduplication checks."""
        if is_secret_or_sensitive(text):
            raise ValueError("Entry contains sensitive credentials or secrets and was rejected.")

        self.ensure_initialized()
        sections = self.parse_sections()
        target_section = section if section in sections else "Preferences"
        target_list = sections[target_section]

        today_str = (now or datetime.now().astimezone()).date().isoformat()
        resolved_id = entry_id or f"{target_section[:4].lower()}-{uuid4().hex[:4]}"

        existing_idx: int | None = None
        norm_new_text = " ".join(text.lower().split())

        for idx, existing in enumerate(target_list):
            if existing.id == resolved_id:
                existing_idx = idx
                break
            norm_existing = " ".join(existing.text.lower().split())
            if norm_new_text == norm_existing or (
                len(norm_new_text) > 15
                and (norm_new_text in norm_existing or norm_existing in norm_new_text)
            ):
                existing_idx = idx
                resolved_id = existing.id
                break

        new_entry = MemoryEntry(
            id=resolved_id,
            date=today_str,
            source=source,
            text=text.strip(),
        )

        if existing_idx is not None:
            old_entry = target_list[existing_idx]
            logger.info(
                "Updating memory entry in %s [%s]: '%s' -> '%s'",
                target_section,
                resolved_id,
                old_entry.text,
                text,
            )
            target_list[existing_idx] = new_entry
        else:
            target_list.append(new_entry)

        # Enforce size cap (Decision 2)
        self._enforce_size_cap_and_archive(sections)

        updated_markdown = self.format_sections(sections)
        self.write_long_term(updated_markdown)
        return new_entry

    def find_matches(self, query: str) -> list[MemoryEntry]:
        """Find entries across all sections matching a search string (for forget confirmation)."""
        sections = self.parse_sections()
        norm_q = query.strip().lower()
        if not norm_q:
            return []

        matches: list[MemoryEntry] = []
        for entries in sections.values():
            for e in entries:
                if norm_q in e.text.lower() or norm_q == e.id.lower():
                    matches.append(e)
        return matches

    def remove_entries(self, query: str) -> list[MemoryEntry]:
        """Remove entries matching a query, atomically saving MEMORY.md."""
        self.ensure_initialized()
        sections = self.parse_sections()
        norm_q = query.strip().lower()
        if not norm_q:
            return []

        removed: list[MemoryEntry] = []
        for sec_name, entries in sections.items():
            kept: list[MemoryEntry] = []
            for e in entries:
                if norm_q in e.text.lower() or norm_q == e.id.lower():
                    removed.append(e)
                else:
                    kept.append(e)
            sections[sec_name] = kept

        if removed:
            logger.info("Removed %d memory entries matching '%s'", len(removed), query)
            self.write_long_term(self.format_sections(sections))
        return removed

    def _enforce_size_cap_and_archive(self, sections: dict[str, list[MemoryEntry]]) -> None:
        """Archive oldest inferred low-confidence entries to MEMORY_ARCHIVE.md if over cap."""
        current_md = self.format_sections(sections)
        current_tokens = estimate_tokens(current_md)
        if current_tokens <= self.token_cap:
            return

        logger.info(
            "MEMORY.md tokens (%d) exceeds cap (%d); applying Decision 2 condensation/archive",
            current_tokens,
            self.token_cap,
        )

        low_conf_entries: list[tuple[str, int, MemoryEntry]] = []
        for sec_name, entries in sections.items():
            for idx, entry in enumerate(entries):
                if entry.source == "inferred-low-confidence":
                    low_conf_entries.append((sec_name, idx, entry))

        low_conf_entries.sort(key=lambda item: item[2].date)

        archived: list[MemoryEntry] = []
        for sec_name, _, entry in low_conf_entries:
            sections[sec_name] = [e for e in sections[sec_name] if e.id != entry.id]
            archived.append(entry)
            candidate_tokens = estimate_tokens(self.format_sections(sections))
            if candidate_tokens <= self.token_cap:
                break

        if archived:
            self._append_to_archive(archived)
            logger.info(
                "Archived %d low-confidence entries to %s: %s",
                len(archived),
                self.archive_path,
                [e.id for e in archived],
            )

    def _append_to_archive(self, entries: list[MemoryEntry]) -> None:
        """Append archived entries to MEMORY_ARCHIVE.md in the user data dir."""
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        header = ""
        if not self.archive_path.exists():
            header = (
                "# Clea Memory Archive\n\n"
                "Oldest low-confidence facts archived when size cap was reached.\n\n"
            )

        lines = [header] if header else []
        for e in entries:
            lines.append(f"{e.format_line()} (archived: {datetime.now().astimezone().date()})\n")

        with self.archive_path.open("a", encoding="utf-8") as handle:
            handle.writelines(lines)

    # --- Episodes ---

    def append_episode(
        self,
        *,
        session_id: str,
        summary: str,
        now: datetime | None = None,
    ) -> Path:
        """Append an episode summary (3-5 lines) to memory/episodes/YYYY-MM-DD.md."""
        self.ensure_initialized()
        dt = now or datetime.now().astimezone()
        date_str = dt.date().isoformat()
        time_str = _format_time_local(dt)
        path = self.episodes_dir / f"{date_str}.md"

        chunks: list[str] = []
        if not path.exists():
            chunks.append(f"# Episodes — {date_str}\n")

        chunks.append(f"\n### Session `{session_id}` ({time_str})\n\n{summary.strip()}\n")

        with path.open("a", encoding="utf-8") as handle:
            handle.write("".join(chunks))

        return path

    def get_recent_episodes(self, limit: int = 5) -> list[dict[str, str]]:
        """Return the most recent episode summaries across all episode files, newest first."""
        if not self.episodes_dir.is_dir():
            return []

        day_files = sorted(
            [p for p in self.episodes_dir.glob("*.md") if _DAY_RE.fullmatch(p.stem)],
            key=lambda p: p.stem,
            reverse=True,
        )

        episodes: list[dict[str, str]] = []
        session_re = re.compile(
            r"^### Session `(?P<sid>[^`]+)` \((?P<time>[^)]+)\)\s*\n(?P<body>.+?)(?=\n###|\Z)",
            re.M | re.S,
        )

        for f in day_files:
            content = f.read_text(encoding="utf-8")
            for m in session_re.finditer(content):
                episodes.append(
                    {
                        "date": f.stem,
                        "session_id": m.group("sid"),
                        "time": m.group("time"),
                        "summary": m.group("body").strip(),
                    }
                )
                if len(episodes) >= limit:
                    return episodes

        return episodes

    def build_injected_context(self, max_episodes: int = 5, token_cap: int = 2000) -> str:
        """Combine size-capped MEMORY.md and recent episodes without raw transcripts."""
        parts: list[str] = []
        long_term = self.read_long_term().strip()
        if long_term:
            parts.append(long_term)

        episodes = self.get_recent_episodes(limit=max_episodes)
        if episodes:
            ep_lines = ["\n## Recent Episodes"]
            for ep in episodes:
                ep_lines.append(
                    f"- **{ep['date']} {ep['time']} ({ep['session_id'][:8]}):** {ep['summary']}"
                )
            parts.append("\n".join(ep_lines))

        combined = "\n\n".join(parts)
        if estimate_tokens(combined) > token_cap:
            while episodes and estimate_tokens(combined) > token_cap:
                episodes.pop()
                ep_lines = ["\n## Recent Episodes"]
                for ep in episodes:
                    ep_lines.append(
                        f"- **{ep['date']} {ep['time']} ({ep['session_id'][:8]}):** {ep['summary']}"
                    )
                combined = long_term + ("\n\n" + "\n".join(ep_lines) if episodes else "")

        return combined.strip()

    # --- Structured Transcripts (logs/transcripts/*.jsonl) ---

    def transcript_path(self, session_id: str) -> Path:
        return self.transcripts_dir / f"{session_id}.jsonl"

    def append_transcript_message(
        self,
        *,
        session_id: str,
        role: str,
        text: str,
        tool_calls: list[dict[str, Any]] | None = None,
        title: str | None = None,
        timestamp: datetime | None = None,
    ) -> Path:
        """Append a message line to the session's structured JSONL transcript file."""
        self.ensure_initialized()
        dt = timestamp or datetime.now().astimezone()
        path = self.transcript_path(session_id)

        if not path.exists():
            header = {
                "session_id": session_id,
                "started_at": dt.isoformat(),
                "title": _truncate(title or text or "New conversation"),
            }
            with path.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(header, ensure_ascii=False) + "\n")

        msg_obj = {
            "role": role,
            "text": text,
            "timestamp": dt.isoformat(),
            "tool_calls": tool_calls or [],
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(msg_obj, ensure_ascii=False) + "\n")

        return path

    def list_transcripts(self, limit: int = 50) -> list[dict[str, Any]]:
        """List session transcripts newest first based on file modified time."""
        if not self.transcripts_dir.is_dir():
            return []

        files = sorted(
            self.transcripts_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        results: list[dict[str, Any]] = []
        for f in files[:limit]:
            try:
                with f.open("r", encoding="utf-8") as handle:
                    first_line = handle.readline()
                    if not first_line:
                        continue
                    header = json.loads(first_line)
                    line_count = sum(1 for _ in handle)
                    results.append(
                        {
                            "session_id": header.get("session_id", f.stem),
                            "started_at": header.get("started_at", ""),
                            "title": header.get("title", "Conversation"),
                            "message_count": line_count,
                        }
                    )
            except Exception:
                continue

        return results

    def get_transcript(self, session_id: str) -> dict[str, Any] | None:
        """Read full structured transcript for a session."""
        path = self.transcript_path(session_id)
        if not path.exists():
            return None

        messages: list[dict[str, Any]] = []
        header: dict[str, Any] = {}
        with path.open("r", encoding="utf-8") as handle:
            first_line = handle.readline()
            if first_line:
                header = json.loads(first_line)
            for line in handle:
                line_str = line.strip()
                if line_str:
                    messages.append(json.loads(line_str))

        return {
            "session_id": header.get("session_id", session_id),
            "started_at": header.get("started_at", ""),
            "title": header.get("title", "Conversation"),
            "messages": messages,
        }

    # --- Backward-Compatible Daily Turn Logging & Migration ---

    def daily_log_path(self, day: date | None = None) -> Path:
        target_day = day or datetime.now().astimezone().date()
        return self.memory_dir / f"{target_day.isoformat()}.md"

    def read_daily(self, day: date | None = None) -> str:
        path = self.daily_log_path(day)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def read_daily_on(self, day: str) -> str:
        return self.read_daily(parse_day(day))

    def list_days(self) -> list[str]:
        if not self.memory_dir.is_dir():
            return []
        days = [p.stem for p in self.memory_dir.glob("*.md") if _DAY_RE.fullmatch(p.stem)]
        return sorted(days, reverse=True)

    def append_daily_turn(
        self,
        *,
        session_id: str,
        user_text: str,
        assistant_text: str,
        now: datetime | None = None,
    ) -> Path:
        """Append turn to structured transcript AND today's daily Markdown log."""
        self.ensure_initialized()
        dt = now or datetime.now().astimezone()

        self.append_transcript_message(
            session_id=session_id,
            role="user",
            text=user_text,
            title=user_text,
            timestamp=dt,
        )
        self.append_transcript_message(
            session_id=session_id,
            role="assistant",
            text=assistant_text,
            timestamp=dt,
        )

        path = self.daily_log_path(dt.date())
        chunks: list[str] = []
        if not path.exists():
            chunks.append(f"# Daily Log — {dt.date().isoformat()}\n")

        time_label = dt.strftime("%H:%M:%S %Z").strip()
        chunks.append(
            f"\n## {time_label} (session `{session_id}`)\n\n"
            f"**User:** {user_text.strip()}\n\n"
            f"**Clea:** {assistant_text.strip()}\n"
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write("".join(chunks))

        return path

    def list_conversations(self) -> list[dict[str, str | int]]:
        """List conversations for the sidebar, reading structured transcripts first."""
        conversations: list[dict[str, str | int]] = []
        transcripts = self.list_transcripts()

        if transcripts:
            for item in transcripts:
                sid = item["session_id"]
                full = self.get_transcript(sid)
                if not full:
                    continue
                started = item.get("started_at", "")
                dt_obj: datetime | None = None
                try:
                    dt_obj = datetime.fromisoformat(started)
                except Exception:
                    pass

                date_str = dt_obj.date().isoformat() if dt_obj else "unknown"
                time_str = _format_time_local(dt_obj) if dt_obj else ""

                conversations.append(
                    {
                        "date": date_str,
                        "session_id": sid,
                        "title": item.get("title", "Conversation"),
                        "turns": (item.get("message_count", 0) + 1) // 2,
                        "last_time": time_str,
                    }
                )
            return conversations

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

    def get_conversation(self, day: str, session_id: str) -> list[dict[str, str]]:
        """Return conversation turns for a session from transcripts or daily log."""
        full = self.get_transcript(session_id)
        if full and full.get("messages"):
            turns: list[dict[str, str]] = []
            user_msg = ""
            user_time = ""
            for m in full["messages"]:
                role = m.get("role")
                text = m.get("text", "")
                ts_str = m.get("timestamp", "")
                try:
                    dt = datetime.fromisoformat(ts_str)
                    time_fmt = _format_time_local(dt)
                except Exception:
                    time_fmt = ts_str

                if role == "user":
                    user_msg = text
                    user_time = time_fmt
                elif role == "assistant":
                    turns.append(
                        {
                            "time": user_time or time_fmt,
                            "user": user_msg,
                            "assistant": text,
                        }
                    )
                    user_msg = ""
            return turns

        return [
            {"time": time, "user": user, "assistant": assistant}
            for sid, time, user, assistant in _parse_turns(self.read_daily_on(day))
            if sid == session_id
        ]

    def migrate_legacy_daily_logs(self) -> dict[str, int]:
        """Convert existing memory/YYYY-MM-DD.md logs into logs/transcripts/ idempotently."""
        if not self.memory_dir.is_dir():
            return {"migrated_sessions": 0, "migrated_files": 0}

        day_files = [p for p in self.memory_dir.glob("*.md") if _DAY_RE.fullmatch(p.stem)]
        if not day_files:
            return {"migrated_sessions": 0, "migrated_files": 0}

        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        self.backup_legacy_dir.mkdir(parents=True, exist_ok=True)

        migrated_sessions = 0
        migrated_files = 0

        for f in day_files:
            content = f.read_text(encoding="utf-8")
            parsed_turns = _parse_turns(content)
            if not parsed_turns:
                continue

            by_sid: dict[str, list[tuple[str, str, str, str]]] = {}
            for sid, time_str, user, assistant in parsed_turns:
                by_sid.setdefault(sid, []).append((sid, time_str, user, assistant))

            for sid, turns in by_sid.items():
                tpath = self.transcript_path(sid)
                if tpath.exists():
                    continue

                try:
                    day_d = date.fromisoformat(f.stem)
                    started_iso = (
                        datetime.combine(day_d, datetime.min.time()).astimezone().isoformat()
                    )
                except Exception:
                    started_iso = datetime.now().astimezone().isoformat()

                first_user = turns[0][2]
                title = _truncate(first_user or "Conversation")

                header = {"session_id": sid, "started_at": started_iso, "title": title}
                lines = [json.dumps(header, ensure_ascii=False) + "\n"]

                for _, _turn_time, user, assistant in turns:
                    if user:
                        lines.append(
                            json.dumps(
                                {
                                    "role": "user",
                                    "text": user,
                                    "timestamp": started_iso,
                                    "tool_calls": [],
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                    if assistant:
                        lines.append(
                            json.dumps(
                                {
                                    "role": "assistant",
                                    "text": assistant,
                                    "timestamp": started_iso,
                                    "tool_calls": [],
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )

                tpath.write_text("".join(lines), encoding="utf-8")
                migrated_sessions += 1

            bak_dest = self.backup_legacy_dir / f.name
            if not bak_dest.exists():
                shutil.copyfile(f, bak_dest)
            migrated_files += 1

        logger.info(
            "Legacy daily log migration complete: %d sessions from %d files",
            migrated_sessions,
            migrated_files,
        )
        return {"migrated_sessions": migrated_sessions, "migrated_files": migrated_files}


_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_HEADING_RE = re.compile(r"^## (?P<time>.+?) \(session `(?P<sid>[A-Za-z0-9_-]+)`\)\s*$", re.M)
_TURN_RE = re.compile(r"\*\*User:\*\* ?(?P<user>.*?)\n\n\*\*Clea:\*\* ?(?P<assistant>.*)\Z", re.S)


def parse_day(value: str) -> date:
    """Strictly parse YYYY-MM-DD (guards against path traversal)."""
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
