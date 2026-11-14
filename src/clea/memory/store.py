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
import time
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
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


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
        self.trash_dir = self.base_dir / "logs" / "transcripts_trash"
        self.long_term_path = self.memory_dir / "MEMORY.md"
        self.archive_path = self.memory_dir / "MEMORY_ARCHIVE.md"
        self.backup_legacy_dir = self.memory_dir / "backup_legacy_logs"
        self.background_tasks_path = self.memory_dir / "background_tasks.json"
        self.token_cap = token_cap

    def ensure_initialized(self) -> None:
        """Create directories and default MEMORY.md if missing."""
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.episodes_dir.mkdir(parents=True, exist_ok=True)
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        self.trash_dir.mkdir(parents=True, exist_ok=True)

        # Migrate legacy root MEMORY.md if present
        legacy_root_long_term = self.base_dir / "MEMORY.md"
        if legacy_root_long_term.exists() and not self.long_term_path.exists():
            try:
                shutil.copyfile(legacy_root_long_term, self.long_term_path)
            except Exception as exc:
                logger.warning("Could not migrate legacy root MEMORY.md: %s", exc)

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
                        section=current_sec,
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
                            section=current_sec,
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
            section=target_section,
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

        norm_id = norm_q.strip("[]")
        matches: list[MemoryEntry] = []
        for entries in sections.values():
            for e in entries:
                if norm_q in e.text.lower() or norm_q == e.id.lower() or norm_id == e.id.lower():
                    matches.append(e)
        return matches

    def remove_entries(self, query: str) -> list[MemoryEntry]:
        """Remove entries matching a query, atomically saving MEMORY.md."""
        self.ensure_initialized()
        sections = self.parse_sections()
        norm_q = query.strip().lower()
        if not norm_q:
            return []

        norm_id = norm_q.strip("[]")
        removed: list[MemoryEntry] = []
        for sec_name, entries in sections.items():
            kept: list[MemoryEntry] = []
            for e in entries:
                if norm_q in e.text.lower() or norm_q == e.id.lower() or norm_id == e.id.lower():
                    removed.append(e)
                else:
                    kept.append(e)
            sections[sec_name] = kept

        if removed:
            logger.info("Removed %d memory entries matching '%s'", len(removed), query)
            self.write_long_term(self.format_sections(sections))
        return removed

    def _condense_sections(self, sections: dict[str, list[MemoryEntry]]) -> int:
        """Condense and merge near-duplicate or overlapping entries in the same section.

        Returns count of merged pairs. Stated/confirmed facts are never dropped.
        """
        total_condensed = 0
        sources = ["stated", "confirmed", "inferred-low-confidence"]
        for sec_name, entries in sections.items():
            if len(entries) < 2:
                continue
            i = 0
            while i < len(entries):
                e1 = entries[i]
                words1 = set(re.findall(r"\w+", e1.text.lower()))
                j = i + 1
                while j < len(entries):
                    e2 = entries[j]
                    words2 = set(re.findall(r"\w+", e2.text.lower()))
                    overlap = len(words1 & words2)
                    union = len(words1 | words2)
                    similarity = overlap / union if union > 0 else 0.0

                    # Merge if substantial word overlap or substring match
                    if (
                        similarity >= 0.55
                        or (len(e1.text) > 12 and e1.text.lower() in e2.text.lower())
                        or (len(e2.text) > 12 and e2.text.lower() in e1.text.lower())
                    ):
                        merged_text = e1.text if len(e1.text) >= len(e2.text) else e2.text
                        src1_rank = sources.index(e1.source) if e1.source in sources else 2
                        src2_rank = sources.index(e2.source) if e2.source in sources else 2
                        merged_src = e1.source if src1_rank <= src2_rank else e2.source
                        merged_date = max(e1.date, e2.date)

                        logger.info(
                            "Condensed memory entries [%s] and [%s] in %s: '%s' + '%s' -> '%s'",
                            e1.id,
                            e2.id,
                            sec_name,
                            e1.text,
                            e2.text,
                            merged_text,
                        )
                        entries[i] = MemoryEntry(
                            id=e1.id,
                            date=merged_date,
                            source=merged_src,  # type: ignore[arg-type]
                            text=merged_text,
                            section=sec_name,
                        )
                        entries.pop(j)
                        words1 = set(re.findall(r"\w+", entries[i].text.lower()))
                        total_condensed += 1
                    else:
                        j += 1
                i += 1
        return total_condensed

    def _enforce_size_cap_and_archive(self, sections: dict[str, list[MemoryEntry]]) -> None:
        """Condense and merge first, then archive oldest low-confidence entries to archive."""
        current_md = self.format_sections(sections)
        current_tokens = estimate_tokens(current_md)
        if current_tokens <= self.token_cap:
            return

        logger.info(
            "MEMORY.md tokens (%d) exceeds cap (%d); applying Decision 2 condensation/archive",
            current_tokens,
            self.token_cap,
        )

        # 1. Decision 2: Condense and merge entries within sections first
        condensed_count = self._condense_sections(sections)
        if condensed_count > 0:
            logger.info("Decision 2 condensation merged %d entry pairs", condensed_count)
            if estimate_tokens(self.format_sections(sections)) <= self.token_cap:
                return

        # 2. Archive oldest inferred low-confidence entries (never drop stated/confirmed)
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
        trivial: bool = False,
    ) -> Path:
        """Append or update an episode summary (3-5 lines) in memory/episodes/."""
        self.ensure_initialized()
        dt = now or datetime.now().astimezone()
        date_str = dt.date().isoformat()
        time_str = _format_time_local(dt)
        trivial_suffix = " [trivial]" if trivial else ""

        resolved_sid = self.resolve_session_id(session_id)
        # Check if an episode for this session already exists in any episode file
        if self.episodes_dir.is_dir():
            sids = [session_id]
            if resolved_sid and resolved_sid != session_id:
                sids.append(resolved_sid)
            for ep_file in sorted(self.episodes_dir.glob("*.md"), reverse=True):
                content = ep_file.read_text(encoding="utf-8")
                for s in sids:
                    pattern = re.compile(
                        rf"(?ms)^### Session `{re.escape(s)}`[^\n]*\n.*?(?=(?:\n### |\Z))"
                    )
                    if pattern.search(content):
                        new_block = (
                            f"### Session `{s}` ({time_str}){trivial_suffix}\n\n{summary.strip()}\n"
                        )
                        updated_content = pattern.sub(lambda _, b=new_block: b, content, count=1)
                        ep_file.write_text(updated_content, encoding="utf-8")
                        return ep_file

        path = self.episodes_dir / f"{date_str}.md"

        chunks: list[str] = []
        if not path.exists():
            chunks.append(f"# Episodes — {date_str}\n")

        header = f"\n### Session `{session_id}` ({time_str}){trivial_suffix}\n\n"
        chunks.append(f"{header}{summary.strip()}\n")

        with path.open("a", encoding="utf-8") as handle:
            handle.write("".join(chunks))

        return path

    def get_recent_episodes(self, limit: int = 5) -> list[dict[str, Any]]:
        """Return the most recent episode summaries across all episode files, newest first."""
        if not self.episodes_dir.is_dir():
            return []

        day_files = sorted(
            [p for p in self.episodes_dir.glob("*.md") if _DAY_RE.fullmatch(p.stem)],
            key=lambda p: p.stem,
            reverse=True,
        )

        episodes: list[dict[str, Any]] = []
        session_re = re.compile(
            r"^### Session `(?P<sid>[^`]+)` \((?P<time>[^)]+)\)"
            r"(?P<trivial_tag>\s*\[trivial\])?\s*\n(?P<body>.+?)(?=\n###|\Z)",
            re.M | re.S,
        )

        for f in day_files:
            content = f.read_text(encoding="utf-8")
            matches = list(session_re.finditer(content))
            for m in reversed(matches):
                body_text = m.group("body").strip()
                is_trivial = (
                    bool(m.group("trivial_tag"))
                    or body_text.startswith("Short session")
                    or "greeting session" in body_text.lower()
                    or "nothing discussed" in body_text.lower()
                )
                episodes.append(
                    {
                        "date": f.stem,
                        "session_id": m.group("sid"),
                        "time": m.group("time"),
                        "summary": body_text,
                        "trivial": is_trivial,
                    }
                )
                if limit > 0 and len(episodes) >= limit:
                    return episodes

        return episodes

    def read_archive(self) -> tuple[list[dict[str, Any]], str]:
        """Read and parse MEMORY_ARCHIVE.md into structured entry items and raw markdown."""
        if not self.archive_path.exists():
            return [], ""
        raw_text = self.archive_path.read_text(encoding="utf-8")
        entry_re = re.compile(
            r"^-\s*\[(?P<id>[^\]]+)\]\s*\((?P<date>[^,]+),\s*(?P<source>[^)]+)\)\s*(?P<text>.*?)(?:\s*\(archived:\s*(?P<archived_at>[^)]+)\))?$",
            re.M,
        )
        entries: list[dict[str, Any]] = []
        for m in entry_re.finditer(raw_text):
            entries.append(
                {
                    "id": m.group("id"),
                    "date": m.group("date"),
                    "source": m.group("source"),
                    "text": m.group("text").strip(),
                    "archived_at": m.group("archived_at") or None,
                    "reason": "Token capacity cap exceeded",
                }
            )
        return entries, raw_text

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

    def validate_session_id(self, session_id: str) -> str:
        """Validate session_id format and ensure it stays inside transcripts directory."""
        if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
            raise ValueError(f"Invalid session id format: {session_id!r}")
        target = (self.transcripts_dir / f"{session_id}.jsonl").resolve()
        if not target.is_relative_to(self.transcripts_dir.resolve()):
            raise ValueError(f"Path traversal detected in session id: {session_id!r}")
        return session_id

    def transcript_path(self, session_id: str) -> Path:
        self.validate_session_id(session_id)
        return self.transcripts_dir / f"{session_id}.jsonl"

    def resolve_session_id(self, session_id: str) -> str:
        """Resolve an exact or prefix session_id to full session_id if unique."""
        path = self.transcript_path(session_id)
        if path.exists():
            return session_id
        if self.transcripts_dir.exists():
            matches = list(self.transcripts_dir.glob(f"{session_id}*.jsonl"))
            if len(matches) == 1:
                return matches[0].stem
        return session_id

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
                "pinned": False,
                "updated_at": dt.isoformat(),
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
                            "pinned": bool(header.get("pinned", False)),
                            "updated_at": header.get("updated_at", header.get("started_at", "")),
                            "message_count": line_count,
                        }
                    )
            except Exception:
                continue

        return results

    def get_transcript(self, session_id: str) -> dict[str, Any] | None:
        """Read full structured transcript for a session."""
        resolved_id = self.resolve_session_id(session_id)
        path = self.transcript_path(resolved_id)
        if not path.exists():
            return None

        messages: list[dict[str, Any]] = []
        header: dict[str, Any] = {}
        try:
            with path.open("r", encoding="utf-8") as handle:
                first_line = handle.readline()
                if first_line:
                    try:
                        header = json.loads(first_line)
                    except json.JSONDecodeError:
                        header = {}
                for line in handle:
                    line_str = line.strip()
                    if line_str:
                        try:
                            messages.append(json.loads(line_str))
                        except json.JSONDecodeError:
                            logger.warning("Skipping corrupted transcript line in %s", path)
        except Exception as exc:
            logger.warning("Failed to read transcript for %s: %s", session_id, exc)
            return None

        return {
            "session_id": header.get("session_id", resolved_id),
            "started_at": header.get("started_at", ""),
            "title": header.get("title", "Conversation"),
            "pinned": bool(header.get("pinned", False)),
            "updated_at": header.get("updated_at", header.get("started_at", "")),
            "messages": messages,
        }

    def update_transcript_header(
        self,
        session_id: str,
        *,
        title: str | None = None,
        pinned: bool | None = None,
    ) -> dict[str, Any]:
        """Atomically update header fields (title, pinned, updated_at) in a transcript file."""
        self.validate_session_id(session_id)
        path = self.transcript_path(session_id)
        if not path.exists():
            raise FileNotFoundError(f"Transcript for session {session_id} not found.")

        with path.open("r", encoding="utf-8") as handle:
            first_line = handle.readline()
            rest = handle.read()

        header: dict[str, Any] = json.loads(first_line) if first_line else {}
        if title is not None:
            clean_title = re.sub(r"<[^>]*>", "", title).strip()
            if not clean_title:
                raise ValueError("Title cannot be empty or contain only HTML tags.")
            if len(clean_title) > 100:
                clean_title = clean_title[:100]
            header["title"] = clean_title

        if pinned is not None:
            header["pinned"] = bool(pinned)

        header["updated_at"] = datetime.now().astimezone().isoformat()

        tmp = path.with_name(f"{session_id}.tmp.{uuid4().hex}")
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(header, ensure_ascii=False) + "\n")
            handle.write(rest)
        tmp.replace(path)
        return header

    def delete_transcript(self, session_id: str, *, delete_episodes: bool = False) -> Path:
        """Soft delete a transcript into the trash folder, optionally deleting episode summary."""
        self.validate_session_id(session_id)
        src_path = self.transcript_path(session_id)
        if not src_path.exists():
            raise FileNotFoundError(f"Transcript for session {session_id} not found.")

        self.trash_dir.mkdir(parents=True, exist_ok=True)
        dst_path = self.trash_dir / f"{session_id}.jsonl"
        if dst_path.exists():
            dst_path.unlink()
        shutil.move(str(src_path), str(dst_path))

        meta_path = self.trash_dir / f"{session_id}.meta.json"
        meta_data = {
            "session_id": session_id,
            "deleted_at": time.time(),
            "delete_episodes": delete_episodes,
        }
        meta_path.write_text(json.dumps(meta_data), encoding="utf-8")

        if delete_episodes:
            self.delete_episode(session_id, backup_in_trash=True)

        return dst_path

    def delete_all_transcripts(self) -> int:
        """Soft delete all transcripts into trash."""
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        now_ts = time.time()
        count = 0
        if self.transcripts_dir.is_dir():
            for p in self.transcripts_dir.glob("*.jsonl"):
                sid = p.stem
                dst = self.trash_dir / p.name
                if dst.exists():
                    dst.unlink()
                shutil.move(str(p), str(dst))
                meta_path = self.trash_dir / f"{sid}.meta.json"
                meta_data = {
                    "session_id": sid,
                    "deleted_at": now_ts,
                    "delete_episodes": False,
                }
                meta_path.write_text(json.dumps(meta_data), encoding="utf-8")
                count += 1
        return count

    def undo_delete_transcript(self, session_id: str | None = None) -> list[str]:
        """Restore transcript(s) from trash back to transcripts directory."""
        if not self.trash_dir.is_dir():
            return []
        restored: list[str] = []
        is_restore_all = not session_id or session_id in ("*", "all")

        if not is_restore_all:
            assert session_id is not None
            self.validate_session_id(session_id)
            src = self.trash_dir / f"{session_id}.jsonl"
            if src.exists():
                dst = self.transcript_path(session_id)
                if dst.exists():
                    dst.unlink()
                shutil.move(str(src), str(dst))
                self._restore_episode_from_trash(session_id)
                (self.trash_dir / f"{session_id}.meta.json").unlink(missing_ok=True)
                restored.append(session_id)
        else:
            for p in list(self.trash_dir.glob("*.jsonl")):
                sid = p.stem
                try:
                    self.validate_session_id(sid)
                    dst = self.transcript_path(sid)
                    if dst.exists():
                        dst.unlink()
                    shutil.move(str(p), str(dst))
                    self._restore_episode_from_trash(sid)
                    (self.trash_dir / f"{sid}.meta.json").unlink(missing_ok=True)
                    restored.append(sid)
                except Exception:
                    continue
        return restored

    def purge_trash(self, min_age_seconds: float = 0.0) -> int:
        """Permanently delete files in the trash folder that exceed min_age_seconds."""
        if not self.trash_dir.is_dir():
            return 0
        now = time.time()
        count = 0
        for p in list(self.trash_dir.glob("*.jsonl")):
            sid = p.stem
            meta_path = self.trash_dir / f"{sid}.meta.json"
            deleted_at = p.stat().st_mtime
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    deleted_at = float(meta.get("deleted_at", deleted_at))
                except Exception:
                    pass
            if min_age_seconds <= 0 or (now - deleted_at) >= min_age_seconds:
                try:
                    p.unlink(missing_ok=True)
                    meta_path.unlink(missing_ok=True)
                    (self.trash_dir / f"{sid}.episode.json").unlink(missing_ok=True)
                    count += 1
                except Exception as exc:
                    logger.warning("Could not purge trash file %s: %s", p, exc)
        return count

    def delete_episode(self, session_id: str, *, backup_in_trash: bool = False) -> bool:
        """Remove session episode entry from memory/episodes/*.md."""
        if not self.episodes_dir.is_dir():
            return False
        modified = False
        pattern = re.compile(
            rf"\n*### Session `{re.escape(session_id)}`[^\n]*\n.*?(?=\n###|\Z)",
            re.DOTALL,
        )
        for f in self.episodes_dir.glob("*.md"):
            try:
                content = f.read_text(encoding="utf-8")
                match = pattern.search(content)
                if match:
                    if backup_in_trash:
                        self.trash_dir.mkdir(parents=True, exist_ok=True)
                        ep_backup = self.trash_dir / f"{session_id}.episode.json"
                        ep_data = {
                            "session_id": session_id,
                            "day": f.stem,
                            "raw_entry": match.group(0),
                        }
                        ep_backup.write_text(json.dumps(ep_data), encoding="utf-8")
                    new_content = pattern.sub("", content).strip()
                    if new_content and new_content != f"# Episodes — {f.stem}".strip():
                        f.write_text(new_content + "\n", encoding="utf-8")
                    else:
                        f.unlink()
                    modified = True
            except Exception as exc:
                logger.warning("Error deleting episode for session %s: %s", session_id, exc)
        return modified

    def _restore_episode_from_trash(self, session_id: str) -> bool:
        ep_backup = self.trash_dir / f"{session_id}.episode.json"
        if not ep_backup.exists():
            return False
        try:
            ep_data = json.loads(ep_backup.read_text(encoding="utf-8"))
            day = ep_data.get("day")
            raw_entry = ep_data.get("raw_entry", "")
            if day and raw_entry:
                day_path = self.episodes_dir / f"{day}.md"
                if not day_path.exists():
                    day_path.write_text(
                        f"# Episodes — {day}\n\n{raw_entry.strip()}\n", encoding="utf-8"
                    )
                else:
                    current_text = day_path.read_text(encoding="utf-8")
                    if raw_entry.strip() not in current_text:
                        day_path.write_text(
                            f"{current_text.rstrip()}\n\n{raw_entry.strip()}\n", encoding="utf-8"
                        )
            ep_backup.unlink(missing_ok=True)
            return True
        except Exception as exc:
            logger.warning("Could not restore episode for session %s: %s", session_id, exc)
            return False

    def export_transcript_markdown(self, session_id: str) -> tuple[str, str]:
        """Export a transcript as clean Markdown with short tool summaries."""
        self.validate_session_id(session_id)
        data = self.get_transcript(session_id)
        if not data:
            raise FileNotFoundError(f"Transcript for session {session_id} not found.")

        title = data.get("title", "Conversation")
        started_at = data.get("started_at", "")
        lines: list[str] = [
            f"# {title}",
            f"**Date:** {started_at} | **Session:** `{session_id}`",
            "",
            "---",
            "",
        ]
        for msg in data.get("messages", []):
            role = msg.get("role")
            text = msg.get("text", "")
            tool_calls = msg.get("tool_calls", [])
            if role == "user":
                lines.append("### User\n")
                lines.append(f"{text.strip()}\n")
            elif role == "assistant":
                lines.append("### Clea\n")
                if tool_calls:
                    for tc in tool_calls:
                        name = tc.get("name", "tool")
                        args = tc.get("arguments", {})
                        arg_str = ", ".join(f"{k}={v!r}" for k, v in list(args.items())[:3])[:60]
                        output = tc.get("output", "")
                        out_summary = f" -> {str(output).strip()[:60]}" if output else ""
                        lines.append(f"> *Used tool `{name}` ({arg_str}){out_summary}*\n")
                if text:
                    lines.append(f"{text.strip()}\n")
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", title.lower()).strip("-")[:30] or "chat"
        filename = f"conversation-{session_id[:8]}-{slug}.md"
        return filename, "\n".join(lines)

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
        tool_calls: list[dict[str, Any]] | None = None,
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
            tool_calls=tool_calls,
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
                last_time_str = ""
                messages = full.get("messages", [])
                if messages:
                    last_ts = messages[-1].get("timestamp", "")
                    try:
                        last_time_str = _format_time_local(datetime.fromisoformat(last_ts))
                    except Exception:
                        pass
                if not last_time_str:
                    last_time_str = _format_time_local(dt_obj) if dt_obj else ""

                conversations.append(
                    {
                        "date": date_str,
                        "session_id": sid,
                        "title": item.get("title", "Conversation"),
                        "turns": (item.get("message_count", 0) + 1) // 2,
                        "last_time": last_time_str,
                        "pinned": bool(item.get("pinned", False)),
                        "updated_at": item.get("updated_at", started),
                        "_sort_key": f"{date_str} {started}",
                    }
                )
            conversations.sort(key=lambda c: str(c.get("_sort_key", "")), reverse=True)
            for c in conversations:
                c.pop("_sort_key", None)
            pinned_list = [c for c in conversations if c.get("pinned")]
            unpinned_list = [c for c in conversations if not c.get("pinned")]
            return pinned_list + unpinned_list

        for day in self.list_days():
            by_session: dict[str, dict[str, str | int]] = {}
            for sid, time_str, user, _assistant in _parse_turns(self.read_daily_on(day)):
                entry = by_session.get(sid)
                t_match = re.search(r"(\d{1,2}:\d{2}(?::\d{2})?)", time_str)
                fmt_time = t_match.group(1)[:5] if t_match else time_str
                if entry is None:
                    entry = {
                        "date": day,
                        "session_id": sid,
                        "title": _truncate(user),
                        "turns": 0,
                        "last_time": fmt_time,
                        "pinned": False,
                        "updated_at": f"{day}T00:00:00",
                    }
                    by_session[sid] = entry
                entry["turns"] = int(entry["turns"]) + 1
                entry["last_time"] = fmt_time
            conversations.extend(reversed(by_session.values()))
        pinned_list = [c for c in conversations if c.get("pinned")]
        unpinned_list = [c for c in conversations if not c.get("pinned")]
        return pinned_list + unpinned_list

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

                day_d = None
                try:
                    day_d = date.fromisoformat(f.stem)
                except Exception:
                    pass

                first_time_str = turns[0][1] if turns else ""
                t_match = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", first_time_str)
                if day_d and t_match:
                    h, m = int(t_match.group(1)), int(t_match.group(2))
                    s = int(t_match.group(3)) if t_match.group(3) else 0
                    started_iso = (
                        datetime(day_d.year, day_d.month, day_d.day, h, m, s)
                        .astimezone()
                        .isoformat()
                    )
                elif day_d:
                    started_iso = (
                        datetime.combine(day_d, datetime.min.time()).astimezone().isoformat()
                    )
                else:
                    started_iso = datetime.now().astimezone().isoformat()

                first_user = turns[0][2]
                title = _truncate(first_user or "Conversation")

                header = {"session_id": sid, "started_at": started_iso, "title": title}
                lines = [json.dumps(header, ensure_ascii=False) + "\n"]

                for _, turn_time_str, user, assistant in turns:
                    msg_time_iso = started_iso
                    t_match_msg = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", turn_time_str)
                    if day_d and t_match_msg:
                        h, m = int(t_match_msg.group(1)), int(t_match_msg.group(2))
                        s = int(t_match_msg.group(3)) if t_match_msg.group(3) else 0
                        msg_time_iso = (
                            datetime(day_d.year, day_d.month, day_d.day, h, m, s)
                            .astimezone()
                            .isoformat()
                        )

                    if user:
                        lines.append(
                            json.dumps(
                                {
                                    "role": "user",
                                    "text": user,
                                    "timestamp": msg_time_iso,
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
                                    "timestamp": msg_time_iso,
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

    # --- Background / Activity Task Persistence & Recovery ---

    def load_background_tasks(self) -> dict[str, dict[str, Any]]:
        """Load persisted Activity/background task records keyed by task_id."""
        if not self.background_tasks_path.exists():
            return {}
        try:
            data = json.loads(self.background_tasks_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to load background_tasks.json: %s", exc)
            return {}
        tasks = data.get("tasks") if isinstance(data, dict) else None
        if not isinstance(tasks, dict):
            return {}
        return {
            str(k): dict(v) for k, v in tasks.items() if isinstance(k, str) and isinstance(v, dict)
        }

    def save_background_tasks(self, tasks: dict[str, dict[str, Any]]) -> None:
        """Atomically persist Activity/background task records."""
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        # Keep all active tasks plus up to 200 most recent finished tasks
        active = {k: v for k, v in tasks.items() if v.get("status") in ("queued", "running")}
        terminal = [
            (k, v) for k, v in tasks.items() if v.get("status") not in ("queued", "running")
        ]
        if len(terminal) > 200:
            terminal.sort(
                key=lambda item: str(item[1].get("finished_at") or item[1].get("started_at") or ""),
                reverse=True,
            )
            terminal = terminal[:200]
        bounded = {**dict(terminal), **active}
        tmp = self.background_tasks_path.with_suffix(f".json.{uuid4().hex[:8]}.tmp")
        tmp.write_text(
            json.dumps({"tasks": bounded}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self.background_tasks_path)

    def record_background_task(
        self,
        *,
        task_id: str,
        title: str,
        status: str,
        started_at: str,
        finished_at: str | None = None,
        kind: str = "background_task",
        detail: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Insert or update a background task record on disk."""
        tasks = self.load_background_tasks()
        prev = tasks.get(task_id)
        effective_started_at = started_at
        if (
            prev
            and prev.get("status") in ("queued", "running")
            and isinstance(prev.get("started_at"), str)
            and prev["started_at"]
        ):
            effective_started_at = prev["started_at"]
        record: dict[str, Any] = {
            "task_id": task_id,
            "title": title,
            "status": status,
            "kind": kind,
            "started_at": effective_started_at,
            "finished_at": finished_at,
            "detail": detail,
            "session_id": session_id if session_id is not None else (prev or {}).get("session_id"),
        }
        tasks[task_id] = record
        self.save_background_tasks(tasks)
        return record

    def reconcile_orphaned_background_tasks(
        self,
        reason: str = "Interrupted by daemon restart",
        *,
        finished_at: str | None = None,
    ) -> list[dict[str, Any]]:
        """Mark any orphaned running/queued background tasks as failed on startup."""
        tasks = self.load_background_tasks()
        if not tasks:
            return []
        now_iso = finished_at or datetime.now().astimezone().isoformat(timespec="seconds")
        reconciled: list[dict[str, Any]] = []
        for task_id, record in list(tasks.items()):
            if record.get("status") in ("queued", "running"):
                updated = {
                    **record,
                    "status": "failed",
                    "finished_at": now_iso,
                    "detail": reason,
                }
                tasks[task_id] = updated
                reconciled.append(updated)
        if reconciled:
            self.save_background_tasks(tasks)
        return reconciled


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
