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
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel

from clea.memory.database import (
    FactStatus,
    MemoryDatabase,
    MemoryFactRecord,
    is_secret_or_sensitive,
    is_synthetic_episodic_session,
)
from clea.memory.retrieval import (
    WEIGHT_EPISODE,
    WEIGHT_FACT,
    EmbeddingProvider,
    EmbeddingStore,
    EmbedFn,
    blend_scores,
    build_search_snippet,
    cosine_similarity,
    field_coverage,
    score_conversation_lexical,
    score_memory_lexical,
    tokenize_query,
)
from clea.memory.three_tier import build_extractive_session_summary
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

VOICE_COMPANION_SESSION_ID = "voice_companion"
VOICE_COMPANION_TITLE = "Voice Companion"

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


_DIR_PRUNE_LOCKS: dict[str, threading.Lock] = {}
_DIR_LOCKS_GUARD = threading.Lock()


def _get_dir_prune_lock(base_dir: Path) -> threading.Lock:
    """Return a process-wide thread lock keyed by data directory path."""
    resolved = str(base_dir.resolve())
    with _DIR_LOCKS_GUARD:
        lock = _DIR_PRUNE_LOCKS.get(resolved)
        if lock is None:
            lock = threading.Lock()
            _DIR_PRUNE_LOCKS[resolved] = lock
        return lock


def _atomic_replace_file(
    tmp_path: Path,
    target_path: Path,
    retries: int = 5,
    delay: float = 0.05,
) -> None:
    """Safely replace target_path with tmp_path on Windows with retries and temp cleanup."""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            tmp_path.replace(target_path)
            return
        except PermissionError as exc:
            last_exc = exc
            time.sleep(delay * (2**attempt))
        except Exception as exc:
            last_exc = exc
            break
    tmp_path.unlink(missing_ok=True)
    if last_exc:
        raise last_exc


class MemoryStore:
    """Manages MEMORY.md, episode summaries, and structured transcripts."""

    def __init__(
        self,
        base_dir: Path | None = None,
        token_cap: int = 2000,
        *,
        embed_fn: EmbedFn | None = None,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self.base_dir = (base_dir or default_data_dir()).expanduser().resolve()
        self.memory_dir = self.base_dir / "memory"
        self.episodes_dir = self.memory_dir / "episodes"
        self.transcripts_dir = self.base_dir / "logs" / "transcripts"
        self.trash_dir = self.base_dir / "logs" / "transcripts_trash"
        self.long_term_path = self.memory_dir / "MEMORY.md"
        self.archive_path = self.memory_dir / "MEMORY_ARCHIVE.md"
        self.backup_legacy_dir = self.memory_dir / "backup_legacy_logs"
        self.background_tasks_path = self.memory_dir / "background_tasks.json"
        self.embeddings_path = self.memory_dir / "embeddings.json"
        self.embedding_store = EmbeddingStore(self.embeddings_path)
        self.embedding_provider = embedding_provider or EmbeddingProvider(embed_fn=embed_fn)
        self.db_path = self.base_dir / "memory.db"
        self.memory_db = MemoryDatabase(self.db_path)
        self.token_cap = token_cap
    @property
    def _prune_lock(self) -> threading.Lock:
        return _get_dir_prune_lock(self.base_dir)

    @property
    def embed_fn(self) -> EmbedFn | None:
        """Optional embedding callback for Layer 3 semantic recall."""
        return self.embedding_provider.embed_fn

    @embed_fn.setter
    def embed_fn(self, fn: EmbedFn | None) -> None:
        self.embedding_provider.embed_fn = fn

    def _embed_item_if_needed(self, key: str, text: str, *, save: bool = True) -> bool:
        """Generate and store an embedding for key when missing or stale; never raises."""
        cleaned = (text or "").strip()
        if not cleaned:
            return False
        try:
            if self.embedding_store.get_vector(key, expected_text=cleaned) is not None:
                return False
            vec = self.embedding_provider.embed(cleaned)
            if vec is None:
                return False
            changed = self.embedding_store.set_vector(key, cleaned, vec)
            if save and changed:
                self.embedding_store.save()
            return changed
        except Exception as exc:
            logger.debug("Skipping embedding for %s: %s", key, exc)
            return False

    def backfill_embeddings(self) -> int:
        """Lazily embed any Episode summaries or Durable Facts missing cached vectors.

        Returns the count of newly generated or updated embeddings. Fails gracefully
        with 0 if no embedding provider is reachable or configured.
        """
        episodes = self.get_recent_episodes(limit=0)
        sections = self.parse_sections()
        items: list[tuple[str, str]] = []
        seen_keys: set[str] = set()

        for ep in episodes:
            sid = str(ep.get("session_id") or "").strip()
            summary = str(ep.get("summary") or "").strip()
            if not sid or not summary:
                continue
            key = f"episode:{sid}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            if self.embedding_store.get_vector(key, expected_text=summary) is None:
                items.append((key, summary))

        for entries in sections.values():
            for entry in entries:
                fact_text = (entry.text or "").strip()
                if not entry.id or not fact_text:
                    continue
                key = f"fact:{entry.id}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                if self.embedding_store.get_vector(key, expected_text=fact_text) is None:
                    items.append((key, fact_text))

        if not items:
            return 0

        updated = 0
        for key, text in items:
            try:
                vec = self.embedding_provider.embed(text)
            except Exception as exc:
                logger.debug("Embedding backfill stopped early at %s: %s", key, exc)
                break
            if vec is None:
                # Stop after the first unreachable/unconfigured failure so offline
                # searches never repeat network timeouts across all history items.
                break
            if self.embedding_store.set_vector(key, text, vec):
                updated += 1

        if updated > 0:
            self.embedding_store.save()
        return updated

    def _get_episode_summary_map(self) -> dict[str, str]:
        """Return mapping of session_id -> latest episode summary across all days."""
        summary_map: dict[str, str] = {}
        for ep in self.get_recent_episodes(limit=0):
            sid = str(ep.get("session_id") or "").strip()
            summary = str(ep.get("summary") or "").strip()
            if sid and summary and sid not in summary_map:
                summary_map[sid] = summary
        return summary_map

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

        self.memory_db.ensure_schema()

    def close(self) -> None:
        """Close/checkpoint the underlying SQLite memory database."""
        self.memory_db.close()

    def index_episodic_session(
        self,
        *,
        session_id: str,
        surface: Literal["chat", "voice"] = "chat",
        started_at: str = "",
        ended_at: str = "",
        title: str = "",
        summary: str = "",
        topics: str = "",
        updated_at: str | None = None,
    ) -> dict[str, Any] | None:
        """Index or update an episodic session summary in the SQLite FTS5 store."""
        if is_synthetic_episodic_session(session_id):
            return None
        self.ensure_initialized()
        return self.memory_db.upsert_episodic_session(
            session_id=session_id,
            surface=surface,
            started_at=started_at,
            ended_at=ended_at,
            title=title,
            summary=summary,
            topics=topics,
            updated_at=updated_at,
        )

    def search_episodic_memory(
        self,
        query: str,
        *,
        limit: int = 5,
        surface: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search Tier 2 episodic session summaries using FTS5 BM25."""
        self.ensure_initialized()
        return self.memory_db.search_episodic(query, limit=limit, surface=surface)

    def list_active_facts(
        self,
        *,
        category: str | None = None,
        limit: int = 25,
        status: FactStatus | None = "active",
    ) -> list[MemoryFactRecord]:
        """List structured Tier 3 facts from SQLite."""
        self.ensure_initialized()
        return self.memory_db.list_facts(status=status, category=category, limit=limit)

    def manage_fact(
        self,
        action: Literal["add", "update", "delete"],
        *,
        fact_text: str | None = None,
        category: str | None = None,
        fact_key: str | None = None,
        fact_id: str | None = None,
        query: str | None = None,
        confidence: float = 0.9,
        source_surface: str = "chat",
        source_session_id: str = "",
    ) -> dict[str, Any]:
        """Add, update, or delete a structured Tier 3 fact in SQLite."""
        self.ensure_initialized()
        return self.memory_db.manage_fact(
            action,
            fact_text=fact_text,
            category=category,
            fact_key=fact_key,
            fact_id=fact_id,
            query=query,
            confidence=confidence,
            source_surface=source_surface,
            source_session_id=source_session_id,
        )

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
        _atomic_replace_file(tmp_path, self.long_term_path)
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
        resolved_id = (
            entry_id.strip("[]")
            if entry_id and entry_id.strip("[]")
            else f"{target_section[:4].lower()}-{uuid4().hex[:4]}"
        )

        # Clean leading bullet symbols, duplicated bracketed IDs, and section tags from LLM text
        clean_text = text.strip()
        clean_text = re.sub(r"^[-*•]\s+", "", clean_text).strip()
        clean_text = re.sub(r"^\[[\w\-]+\]\s*", "", clean_text).strip()
        clean_text = re.sub(r"^\([\w\s\-]+\)\s*", "", clean_text).strip()
        if not clean_text:
            clean_text = text.strip()

        # If entry_id was specified and exists in another section, remove it from that section
        if entry_id:
            for s_name, s_entries in sections.items():
                if s_name != target_section:
                    sections[s_name] = [e for e in s_entries if e.id != resolved_id]

        existing_idx: int | None = None
        norm_new_text = " ".join(clean_text.lower().split())

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
            text=clean_text,
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
        self._embed_item_if_needed(f"fact:{new_entry.id}", new_entry.text)
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
                if (
                    norm_id == e.id.lower()
                    or norm_q == e.id.lower()
                    or (len(norm_q) >= 3 and norm_q in e.text.lower())
                    or norm_q == e.text.lower().strip()
                ):
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
                if (
                    norm_id == e.id.lower()
                    or norm_q == e.id.lower()
                    or (len(norm_q) >= 3 and norm_q in e.text.lower())
                    or norm_q == e.text.lower().strip()
                ):
                    removed.append(e)
                else:
                    kept.append(e)
            sections[sec_name] = kept

        if removed:
            logger.info("Removed %d memory entries matching '%s'", len(removed), query)
            self.write_long_term(self.format_sections(sections))
            changed_emb = False
            for entry in removed:
                if self.embedding_store.remove_key(f"fact:{entry.id}"):
                    changed_emb = True
            if changed_emb:
                self.embedding_store.save()
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

    def _sync_episodic_session_db(
        self,
        *,
        session_id: str,
        summary: str,
        dt: datetime,
    ) -> None:
        """Mirror an episode summary into the SQLite FTS5 episodic_sessions table."""
        if is_synthetic_episodic_session(session_id):
            return
        try:
            surface: Literal["chat", "voice"] = (
                "voice" if session_id == VOICE_COMPANION_SESSION_ID else "chat"
            )
            title = ""
            started_at = ""
            ended_at = dt.isoformat()
            topics = ""

            tr: dict[str, Any] | None = None
            try:
                tr = self.get_transcript(session_id)
            except Exception:
                tr = None

            if tr:
                if tr.get("is_voice_call") or tr.get("session_id") == VOICE_COMPANION_SESSION_ID:
                    surface = "voice"
                title = str(tr.get("title") or "").strip()
                started_at = str(tr.get("started_at") or "").strip()
                ended_at = str(tr.get("updated_at") or dt.isoformat()).strip()
                msgs = list(tr.get("messages", []))
                if msgs:
                    derived_title, _, topics = build_extractive_session_summary(
                        msgs, fallback_title=title
                    )
                    if not title:
                        title = derived_title

            if not title:
                title = (
                    VOICE_COMPANION_TITLE
                    if surface == "voice" and session_id == VOICE_COMPANION_SESSION_ID
                    else _truncate(summary)
                )
            if not topics:
                _, _, topics = build_extractive_session_summary(
                    [{"role": "assistant", "text": summary}], fallback_title=title
                )

            self.memory_db.upsert_episodic_session(
                session_id=session_id,
                surface=surface,
                started_at=started_at,
                ended_at=ended_at,
                title=title,
                summary=summary.strip(),
                topics=topics,
                updated_at=dt.isoformat(),
            )
        except Exception as exc:
            logger.debug("Skipping SQLite episodic upsert for %s: %s", session_id, exc)

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
                        self._embed_item_if_needed(f"episode:{s}", summary.strip())
                        self._sync_episodic_session_db(session_id=s, summary=summary, dt=dt)
                        return ep_file

        path = self.episodes_dir / f"{date_str}.md"

        chunks: list[str] = []
        if not path.exists():
            chunks.append(f"# Episodes — {date_str}\n")

        header = f"\n### Session `{session_id}` ({time_str}){trivial_suffix}\n\n"
        chunks.append(f"{header}{summary.strip()}\n")

        with path.open("a", encoding="utf-8") as handle:
            handle.write("".join(chunks))

        self._embed_item_if_needed(f"episode:{session_id}", summary.strip())
        self._sync_episodic_session_db(session_id=session_id, summary=summary, dt=dt)
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

    def build_injected_context(
        self,
        max_episodes: int = 5,
        token_cap: int = 2000,
        *,
        include_voice_tail: bool = False,
        voice_tail_turns: int = 15,
        active_conversation_id: str | None = None,
        active_chat_turns: int = 10,
    ) -> str:
        """Combine size-capped MEMORY.md and recent episodes, plus optional voice/chat context."""
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

        active_facts_ctx = ""
        if self.db_path.exists():
            try:
                active_facts_ctx = self.memory_db.format_active_facts_context(limit=25).strip()
            except Exception as exc:
                logger.debug("Skipping SQLite active facts injection: %s", exc)

        if active_facts_ctx:
            candidate = (
                f"{combined}\n\n{active_facts_ctx}".strip() if combined else active_facts_ctx
            )
            while episodes and estimate_tokens(candidate) > token_cap:
                episodes.pop()
                ep_lines = ["\n## Recent Episodes"]
                for ep in episodes:
                    ep_lines.append(
                        f"- **{ep['date']} {ep['time']} ({ep['session_id'][:8]}):** {ep['summary']}"
                    )
                combined = long_term + ("\n\n" + "\n".join(ep_lines) if episodes else "")
                candidate = (
                    f"{combined}\n\n{active_facts_ctx}".strip() if combined else active_facts_ctx
                )

            if estimate_tokens(candidate) <= token_cap:
                combined = candidate
            else:
                fact_lines = active_facts_ctx.splitlines()
                while len(fact_lines) > 2:
                    fact_lines.pop()
                    trimmed_facts = "\n".join(fact_lines)
                    candidate = (
                        f"{combined}\n\n{trimmed_facts}".strip() if combined else trimmed_facts
                    )
                    if estimate_tokens(candidate) <= token_cap:
                        combined = candidate
                        break
                else:
                    if not combined and estimate_tokens(candidate) <= token_cap:
                        combined = candidate

        extra_sections: list[str] = []
        if include_voice_tail and voice_tail_turns > 0:
            tail_msgs = self.get_voice_companion_tail(max_turns=voice_tail_turns)
            if tail_msgs:
                vc_lines = ["## Recent Voice Companion History"]
                last_call_id: str | None = None
                for msg in tail_msgs:
                    call_id = str(msg.get("call_id") or "").strip() or None
                    raw_ts = str(msg.get("timestamp") or "").strip()
                    time_label = ""
                    if raw_ts:
                        try:
                            time_label = _format_time_local(datetime.fromisoformat(raw_ts))
                        except Exception:
                            time_label = raw_ts[:16]
                    if call_id and call_id != last_call_id:
                        tag = (
                            f"{time_label} · call {call_id[:8]}"
                            if time_label
                            else f"call {call_id[:8]}"
                        )
                        vc_lines.append(f"--- [Voice Call {tag}] ---")
                        last_call_id = call_id
                    speaker = "User" if msg.get("role") == "user" else "Clea"
                    prefix = f"[{time_label}] " if time_label else ""
                    vc_lines.append(f"- {prefix}**{speaker}:** {str(msg.get('text', '')).strip()}")
                extra_sections.append("\n".join(vc_lines))

        if active_conversation_id and active_conversation_id != VOICE_COMPANION_SESSION_ID:
            try:
                active_tr = self.get_transcript(active_conversation_id)
            except Exception:
                active_tr = None
            if active_tr and not active_tr.get("is_voice_call") and active_tr.get("messages"):
                active_title = str(active_tr.get("title") or "Conversation").strip()
                recent_chat_msgs = list(active_tr.get("messages", []))[-max(1, active_chat_turns) :]
                active_sid = active_tr.get("session_id", active_conversation_id)
                chat_lines = [
                    "## Active Focused Text Chat (Read-Only Context)",
                    f"Active Conversation ID: `{active_sid}`",
                    f"Title: {active_title}",
                    "Recent turns in this open text chat:",
                ]
                for msg in recent_chat_msgs:
                    speaker = "User" if msg.get("role") == "user" else "Clea"
                    text = str(msg.get("text", "")).strip()
                    if not text:
                        tool_calls = msg.get("tool_calls") or []
                        if tool_calls:
                            names = [
                                str(tc.get("name", "tool"))
                                for tc in tool_calls
                                if isinstance(tc, dict)
                            ]
                            text = f"[Executed {', '.join(names)}]"
                    if text:
                        chat_lines.append(f"- **{speaker}:** {text}")
                extra_sections.append("\n".join(chat_lines))

        if extra_sections:
            combined = (combined.strip() + "\n\n" + "\n\n".join(extra_sections)).strip()

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
        target_files = files if limit <= 0 else files[:limit]
        for f in target_files:
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

    def append_voice_companion_call(
        self,
        *,
        call_id: str,
        entries: list[tuple[str, str, datetime]],
    ) -> bool:
        """Append non-empty voice call turns into continuous voice_companion stream."""
        self.validate_session_id(call_id)
        if not entries:
            return False

        self.ensure_initialized()
        path = self.transcript_path(VOICE_COMPANION_SESSION_ID)

        existing_vc = (
            self._read_transcript_file(path, VOICE_COMPANION_SESSION_ID) if path.exists() else None
        )
        existing_for_call = (
            [m for m in existing_vc.get("messages", []) if m.get("call_id") == call_id]
            if existing_vc
            else []
        )
        new_entries = entries[len(existing_for_call) :]
        if not new_entries:
            return False

        first_dt = new_entries[0][2]
        last_dt = new_entries[-1][2]

        if not path.exists():
            header = {
                "session_id": VOICE_COMPANION_SESSION_ID,
                "started_at": first_dt.isoformat(),
                "title": VOICE_COMPANION_TITLE,
                "pinned": False,
                "updated_at": last_dt.isoformat(),
            }
            with path.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(header, ensure_ascii=False) + "\n")

        with path.open("a", encoding="utf-8") as handle:
            for role, text, dt in new_entries:
                msg_obj = {
                    "role": role,
                    "text": text,
                    "timestamp": dt.isoformat(),
                    "call_id": call_id,
                    "tool_calls": [],
                }
                handle.write(json.dumps(msg_obj, ensure_ascii=False) + "\n")

        try:
            self.update_transcript_header(VOICE_COMPANION_SESSION_ID)
        except Exception:
            pass
        return True

    def get_voice_companion_tail(self, max_turns: int = 15) -> list[dict[str, Any]]:
        """Return the most recent voice companion turns for rolling startup context."""
        if max_turns <= 0:
            return []
        path = self.transcript_path(VOICE_COMPANION_SESSION_ID)
        if not path.exists():
            return []
        vc = self._read_transcript_file(path, VOICE_COMPANION_SESSION_ID)
        if not vc:
            return []
        messages = list(vc.get("messages", []))
        return messages[-max_turns:]

    def _read_transcript_file(self, path: Path, fallback_sid: str) -> dict[str, Any] | None:
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
            logger.warning("Failed to read transcript for %s: %s", fallback_sid, exc)
            return None

        return {
            "session_id": header.get("session_id", fallback_sid),
            "started_at": header.get("started_at", ""),
            "title": header.get("title", "Conversation"),
            "pinned": bool(header.get("pinned", False)),
            "updated_at": header.get("updated_at", header.get("started_at", "")),
            "messages": messages,
        }

    def get_transcript(self, session_id: str) -> dict[str, Any] | None:
        """Read full structured transcript for a session (or voice call inside voice_companion)."""
        resolved_id = self.resolve_session_id(session_id)
        path = self.transcript_path(resolved_id)
        if path.exists():
            return self._read_transcript_file(path, resolved_id)

        if resolved_id != VOICE_COMPANION_SESSION_ID:
            vc_path = self.transcript_path(VOICE_COMPANION_SESSION_ID)
            if vc_path.exists():
                vc = self._read_transcript_file(vc_path, VOICE_COMPANION_SESSION_ID)
                if vc:
                    call_msgs = [
                        m
                        for m in vc.get("messages", [])
                        if m.get("call_id") in (session_id, resolved_id)
                    ]
                    if call_msgs:
                        first_user = next(
                            (
                                str(m.get("text", "")).strip()
                                for m in call_msgs
                                if m.get("role") == "user" and str(m.get("text", "")).strip()
                            ),
                            "",
                        )
                        started_at = str(call_msgs[0].get("timestamp") or vc.get("started_at", ""))
                        updated_at = str(call_msgs[-1].get("timestamp") or started_at)
                        title = _truncate(first_user) if first_user else VOICE_COMPANION_TITLE
                        return {
                            "session_id": session_id,
                            "started_at": started_at,
                            "title": title,
                            "pinned": False,
                            "updated_at": updated_at,
                            "messages": call_msgs,
                            "is_voice_call": True,
                        }
        return None

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
        _atomic_replace_file(tmp, path)
        return header

    def delete_transcript(self, session_id: str, *, delete_episodes: bool = False) -> Path:
        """Soft delete a transcript into the trash folder, optionally deleting episode summary."""
        self.validate_session_id(session_id)
        if session_id == VOICE_COMPANION_SESSION_ID:
            raise ValueError("Cannot delete the Voice Companion stream.")
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
        """Soft delete all transcripts into trash, excluding Voice Companion."""
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        now_ts = time.time()
        count = 0
        if self.transcripts_dir.is_dir():
            for p in self.transcripts_dir.glob("*.jsonl"):
                if p.stem == VOICE_COMPANION_SESSION_ID:
                    continue
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

    def prune_diagnostic_logs(
        self,
        max_age_days: int = 7,
        max_entries: int = 500,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Prune raw voice companion turns and diagnostic logs by TTL and max entry cap.

        Strict Guarantees:
        - NEVER modifies curated Memory entries (MEMORY.md, episodes, daily memory).
        - NEVER touches saved HISTORY conversation threads
          (<session_id>.jsonl where session_id != voice_companion).
        - Idempotent and safe against concurrent execution and Windows file contention.
        """
        with self._prune_lock:
            ref_now = now or datetime.now().astimezone()
            if ref_now.tzinfo is None:
                ref_now = ref_now.astimezone()
            effective_max_age_days = max(0, max_age_days)
            cutoff = ref_now - timedelta(days=effective_max_age_days)
            cutoff_epoch = cutoff.timestamp()
            effective_max_entries = max(0, max_entries)

            pruned_companion = 0
            pruned_metrics = 0
            pruned_audit = 0
            pruned_logs = 0
            pruned_trash = 0

            # 1. Prune voice_companion.jsonl stream
            vc_path = self.transcript_path(VOICE_COMPANION_SESSION_ID)
            if vc_path.exists():
                try:
                    with vc_path.open("r", encoding="utf-8") as handle:
                        lines = [line.strip() for line in handle if line.strip()]
                    if lines:
                        header_line = lines[0]
                        msg_lines = lines[1:]
                        kept_msg_lines: list[str] = []
                        for line in msg_lines:
                            try:
                                msg_obj = json.loads(line)
                                ts_str = msg_obj.get("timestamp")
                                if ts_str:
                                    dt = datetime.fromisoformat(ts_str)
                                    if dt.tzinfo is None:
                                        dt = dt.astimezone()
                                    if dt < cutoff:
                                        pruned_companion += 1
                                        continue
                                kept_msg_lines.append(line)
                            except Exception:
                                kept_msg_lines.append(line)

                        if len(kept_msg_lines) > effective_max_entries:
                            excess = len(kept_msg_lines) - effective_max_entries
                            pruned_companion += excess
                            kept_msg_lines = (
                                kept_msg_lines[-effective_max_entries:]
                                if effective_max_entries > 0
                                else []
                            )

                        if pruned_companion > 0 or len(kept_msg_lines) + 1 < len(lines):
                            tmp_path = vc_path.with_name(
                                f"{VOICE_COMPANION_SESSION_ID}.tmp.{uuid4().hex}"
                            )
                            with tmp_path.open("w", encoding="utf-8") as handle:
                                handle.write(header_line + "\n")
                                for line in kept_msg_lines:
                                    handle.write(line + "\n")
                            _atomic_replace_file(tmp_path, vc_path)
                except Exception as exc:
                    logger.warning("Error pruning companion logs in %s: %s", vc_path, exc)

            # 1b. If companion messages were pruned from front of voice_companion.jsonl,
            # adjust last_consolidated_message_idx in consolidation_state.json so that
            # checkpoint indexes remain valid and previously consolidated messages are
            # never erroneously re-consolidated.
            if pruned_companion > 0:
                state_path = self.memory_dir / "consolidation_state.json"
                if state_path.exists():
                    try:
                        c_state = json.loads(state_path.read_text(encoding="utf-8"))
                        sessions_map = c_state.get("sessions")
                        if isinstance(sessions_map, dict):
                            vc_info = sessions_map.get(VOICE_COMPANION_SESSION_ID)
                            if isinstance(vc_info, dict):
                                for key in (
                                    "last_consolidated_message_idx",
                                    "last_consolidated_turn",
                                    "last_consolidated_turn_count",
                                ):
                                    if key in vc_info and isinstance(vc_info[key], (int, float)):
                                        vc_info[key] = max(0, int(vc_info[key]) - pruned_companion)
                                tmp_state = state_path.with_name(
                                    f"consolidation_state.tmp.{uuid4().hex}"
                                )
                                tmp_state.write_text(
                                    json.dumps(c_state, indent=2, ensure_ascii=False),
                                    encoding="utf-8",
                                )
                                _atomic_replace_file(tmp_state, state_path)
                    except Exception as exc:
                        logger.warning(
                            "Failed adjusting consolidation_state after pruning: %s", exc
                        )

            # 2. Prune logs/voice_metrics.jsonl
            logs_dir = self.base_dir / "logs"
            metrics_path = logs_dir / "voice_metrics.jsonl"
            if metrics_path.exists():
                try:
                    with metrics_path.open("r", encoding="utf-8") as handle:
                        lines = [line.strip() for line in handle if line.strip()]
                    kept_metrics: list[str] = []
                    for line in lines:
                        try:
                            record = json.loads(line)
                            ts_str = record.get("timestamp") or record.get("created_at")
                            if ts_str:
                                dt = datetime.fromisoformat(ts_str)
                                if dt.tzinfo is None:
                                    dt = dt.astimezone()
                                if dt < cutoff:
                                    pruned_metrics += 1
                                    continue
                            kept_metrics.append(line)
                        except Exception:
                            kept_metrics.append(line)

                    if len(kept_metrics) > effective_max_entries:
                        excess = len(kept_metrics) - effective_max_entries
                        pruned_metrics += excess
                        kept_metrics = (
                            kept_metrics[-effective_max_entries:]
                            if effective_max_entries > 0
                            else []
                        )

                    if pruned_metrics > 0 or len(kept_metrics) < len(lines):
                        tmp_metrics = metrics_path.with_name(f"voice_metrics.tmp.{uuid4().hex}")
                        with tmp_metrics.open("w", encoding="utf-8") as handle:
                            for line in kept_metrics:
                                handle.write(line + "\n")
                        _atomic_replace_file(tmp_metrics, metrics_path)
                except Exception as exc:
                    logger.warning("Error pruning voice metrics in %s: %s", metrics_path, exc)

            # 3. Prune logs/audit.jsonl
            audit_path = logs_dir / "audit.jsonl"
            if audit_path.exists():
                try:
                    with audit_path.open("r", encoding="utf-8") as handle:
                        lines = [line.strip() for line in handle if line.strip()]
                    kept_audit: list[str] = []
                    for line in lines:
                        try:
                            record = json.loads(line)
                            ts_str = record.get("timestamp") or record.get("created_at")
                            if ts_str:
                                dt = datetime.fromisoformat(ts_str)
                                if dt.tzinfo is None:
                                    dt = dt.astimezone()
                                if dt < cutoff:
                                    pruned_audit += 1
                                    continue
                            kept_audit.append(line)
                        except Exception:
                            kept_audit.append(line)

                    if len(kept_audit) > effective_max_entries:
                        excess = len(kept_audit) - effective_max_entries
                        pruned_audit += excess
                        kept_audit = (
                            kept_audit[-effective_max_entries:]
                            if effective_max_entries > 0
                            else []
                        )

                    if pruned_audit > 0 or len(kept_audit) < len(lines):
                        tmp_audit = audit_path.with_name(f"audit.tmp.{uuid4().hex}")
                        with tmp_audit.open("w", encoding="utf-8") as handle:
                            for line in kept_audit:
                                handle.write(line + "\n")
                        _atomic_replace_file(tmp_audit, audit_path)
                except Exception as exc:
                    logger.warning("Error pruning audit logs in %s: %s", audit_path, exc)

            # 4. Prune rotated diagnostic files: logs/daemon.log.*, logs/voice.log.*
            if logs_dir.is_dir():
                for pat in ("daemon.log.*", "voice.log.*", "daemon_*.log", "voice_*.log"):
                    for p in logs_dir.glob(pat):
                        try:
                            if p.stat().st_mtime < cutoff_epoch:
                                p.unlink(missing_ok=True)
                                pruned_logs += 1
                        except Exception:
                            pass

                # Clean up stale temporary files older than 1 hour left from interrupted runs
                tmp_cutoff = ref_now.timestamp() - 3600.0
                for tmp_file in list(logs_dir.glob("*.tmp.*")) + list(
                    self.transcripts_dir.glob("*.tmp.*")
                ):
                    try:
                        if tmp_file.stat().st_mtime < tmp_cutoff:
                            tmp_file.unlink(missing_ok=True)
                    except Exception:
                        pass

            # 5. Prune trash directory if files older than max_age_days
            try:
                pruned_trash = self.purge_trash(min_age_seconds=max_age_days * 86400.0)
            except Exception:
                pass

            result = {
                "pruned_companion_turns": pruned_companion,
                "pruned_voice_metrics": pruned_metrics,
                "pruned_audit_records": pruned_audit,
                "pruned_log_files": pruned_logs,
                "pruned_trash_files": pruned_trash,
            }
            result["total_pruned"] = sum(result.values())
            return result

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
        if modified:
            if self.embedding_store.remove_key(f"episode:{session_id}"):
                self.embedding_store.save()
        if self.db_path.exists():
            try:
                self.memory_db.delete_episodic_session(session_id)
            except Exception as exc:
                logger.debug("SQLite episode delete skipped for %s: %s", session_id, exc)
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

    def list_conversations(self, limit: int | None = None) -> list[dict[str, Any]]:
        """List conversations newest-first across 100% of stored transcripts.

        When ``limit`` is None (sidebar UI default), pinned conversations stay grouped
        first and all conversations are returned. When ``limit`` is provided (e.g.
        ``limit=10`` from voice recall), returns the ``limit`` most recent
        conversations ordered strictly newest-first regardless of pin status,
        including associated Episode summaries.
        """
        conversations: list[dict[str, Any]] = []
        transcripts = [
            t
            for t in self.list_transcripts(limit=0)
            if t.get("session_id") != VOICE_COMPANION_SESSION_ID
        ]
        episode_map = self._get_episode_summary_map()
        cap = max(1, min(int(limit), 100)) if limit is not None else None

        if transcripts:
            for item in transcripts:
                sid = str(item["session_id"])
                full = self.get_transcript(sid)
                if not full:
                    continue
                started = str(item.get("started_at", ""))
                dt_obj: datetime | None = None
                try:
                    dt_obj = datetime.fromisoformat(started)
                except Exception:
                    pass

                date_str = dt_obj.date().isoformat() if dt_obj else "unknown"
                last_time_str = ""
                messages = full.get("messages", [])
                last_msg_ts = ""
                if messages:
                    last_msg_ts = str(messages[-1].get("timestamp", ""))
                    try:
                        last_time_str = _format_time_local(datetime.fromisoformat(last_msg_ts))
                    except Exception:
                        pass
                if not last_time_str:
                    last_time_str = _format_time_local(dt_obj) if dt_obj else ""

                header_updated = str(item.get("updated_at") or started)
                effective_updated_at = (
                    _latest_iso_timestamp(header_updated, last_msg_ts)
                    if last_msg_ts
                    else header_updated
                )
                resolved_sid = self.resolve_session_id(sid)
                ep_summary = episode_map.get(sid) or episode_map.get(resolved_sid) or ""
                turn_count = (int(item.get("message_count", 0)) + 1) // 2

                conversations.append(
                    {
                        "date": date_str,
                        "session_id": sid,
                        "conversation_id": sid,
                        "title": item.get("title", "Conversation"),
                        "turns": turn_count,
                        "turn_count": turn_count,
                        "last_time": last_time_str,
                        "pinned": bool(item.get("pinned", False)),
                        "updated_at": effective_updated_at,
                        "episode_summary": ep_summary,
                        "_sort_tuple": (
                            _iso_sort_value(effective_updated_at),
                            date_str,
                            started,
                            sid,
                        ),
                    }
                )
            conversations.sort(key=lambda c: c["_sort_tuple"], reverse=True)
            for c in conversations:
                c.pop("_sort_tuple", None)
            if cap is not None:
                return conversations[:cap]
            pinned_list = [c for c in conversations if c.get("pinned")]
            unpinned_list = [c for c in conversations if not c.get("pinned")]
            return pinned_list + unpinned_list

        for day in self.list_days():
            by_session: dict[str, dict[str, Any]] = {}
            for sid, time_str, user, _assistant in _parse_turns(self.read_daily_on(day)):
                if sid == VOICE_COMPANION_SESSION_ID:
                    continue
                entry = by_session.get(sid)
                t_match = re.search(r"(\d{1,2}:\d{2}(?::\d{2})?)", time_str)
                fmt_time = t_match.group(1)[:5] if t_match else time_str
                if entry is None:
                    entry = {
                        "date": day,
                        "session_id": sid,
                        "conversation_id": sid,
                        "title": _truncate(user),
                        "turns": 0,
                        "turn_count": 0,
                        "last_time": fmt_time,
                        "pinned": False,
                        "updated_at": f"{day}T00:00:00",
                        "episode_summary": episode_map.get(sid, ""),
                    }
                    by_session[sid] = entry
                entry["turns"] = int(entry["turns"]) + 1
                entry["turn_count"] = entry["turns"]
                entry["last_time"] = fmt_time
            conversations.extend(reversed(by_session.values()))
        if cap is not None:
            return conversations[:cap]
        pinned_list = [c for c in conversations if c.get("pinned")]
        unpinned_list = [c for c in conversations if not c.get("pinned")]
        return pinned_list + unpinned_list

    def _collect_conversation_episodes(
        self,
        *,
        session_id: str,
        resolved_sid: str,
        messages: list[dict[str, Any]],
        episode_map: dict[str, str],
    ) -> tuple[str, list[list[float]]]:
        """Collect Episode summary text and cached vectors associated with a conversation."""
        candidate_sids: list[str] = []
        for s in (session_id, resolved_sid):
            if s and s not in candidate_sids:
                candidate_sids.append(s)
        if session_id == VOICE_COMPANION_SESSION_ID:
            for m in messages:
                cid = str(m.get("call_id") or "").strip()
                if cid and cid not in candidate_sids:
                    candidate_sids.append(cid)

        summaries: list[str] = []
        vectors: list[list[float]] = []
        seen_summaries: set[str] = set()
        for s in candidate_sids:
            ep_text = episode_map.get(s, "").strip()
            if not ep_text:
                continue
            if ep_text not in seen_summaries:
                summaries.append(ep_text)
                seen_summaries.add(ep_text)
            vec = self.embedding_store.get_vector(f"episode:{s}", expected_text=ep_text)
            if vec is not None:
                vectors.append(vec)
        return "\n".join(summaries), vectors

    def search_conversations(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """3-layer hybrid search across 100% of transcripts, Episodes, and Durable Facts.

        Layer 1: Searches 100% of stored transcripts, Episodes, and Durable Facts (zero cap).
        Layer 2: Multi-term token-ranked weighted scoring across Title (+5.0),
                 Episode summary (+3.5), Durable Facts (+3.0), and raw turns (+1.0).
        Layer 3: Cosine similarity against Episode & Durable Fact float32 embeddings
                 blended as 0.6 * semantic + 0.4 * lexical (graceful fallback to 1.0 * lexical).
        """
        q_raw = (query or "").strip()
        if not q_raw:
            return []

        cap = max(1, min(int(limit or 8), 50))
        q_lower = q_raw.lower()
        q_tokens = tokenize_query(q_raw)

        # Lazily backfill any un-embedded Episodes or Durable Facts and embed query.
        try:
            self.backfill_embeddings()
        except Exception as exc:
            logger.debug("Lazy embedding backfill skipped: %s", exc)

        query_vec: list[float] | None = None
        try:
            query_vec = self.embedding_provider.embed(q_raw)
        except Exception as exc:
            logger.debug("Query embedding skipped: %s", exc)

        if not self.transcripts_dir.is_dir():
            return []

        episode_map = self._get_episode_summary_map()

        # Precompute Durable Facts (lexical coverage, tokens, and cached vectors) once.
        fact_records: list[dict[str, Any]] = []
        for entries in self.parse_sections().values():
            for entry in entries:
                f_text = (entry.text or "").strip()
                if not f_text:
                    continue
                f_tokens = set(tokenize_query(f_text))
                fact_records.append(
                    {
                        "date": entry.date,
                        "text": f_text,
                        "tokens": f_tokens,
                        "cov": field_coverage(q_tokens, q_lower, f_text),
                        "vector": self.embedding_store.get_vector(
                            f"fact:{entry.id}", expected_text=f_text
                        ),
                    }
                )

        files = sorted(
            self.transcripts_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        scored_hits: list[tuple[float, float, str, dict[str, Any]]] = []
        for f in files:
            tr = self._read_transcript_file(f, f.stem)
            if not tr:
                continue
            sid = str(tr.get("session_id") or f.stem)
            resolved_sid = self.resolve_session_id(sid)
            default_title = (
                VOICE_COMPANION_TITLE if sid == VOICE_COMPANION_SESSION_ID else "Conversation"
            )
            title = str(tr.get("title") or default_title)
            source = "voice_companion" if sid == VOICE_COMPANION_SESSION_ID else "chat"
            msgs = list(tr.get("messages", []))

            header_updated = str(tr.get("updated_at") or tr.get("started_at") or "")
            last_msg_ts = str(msgs[-1].get("timestamp") or "") if msgs else ""
            effective_updated_at = (
                _latest_iso_timestamp(header_updated, last_msg_ts)
                if last_msg_ts
                else header_updated
            )
            conv_date = effective_updated_at[:10] if len(effective_updated_at) >= 10 else ""

            episode_summary, ep_vecs = self._collect_conversation_episodes(
                session_id=sid,
                resolved_sid=resolved_sid,
                messages=msgs,
                episode_map=episode_map,
            )

            turn_texts: list[str] = []
            turn_candidates: list[tuple[float, float, str, str, str, str | None]] = []
            for msg in msgs:
                searchable_text = _format_searchable_message_text(msg)
                if not searchable_text:
                    continue
                turn_texts.append(searchable_text)
                turn_cov = field_coverage(q_tokens, q_lower, searchable_text)
                if turn_cov <= 0.0:
                    continue
                role = str(msg.get("role") or "assistant")
                ts = str(msg.get("timestamp") or effective_updated_at)
                snip = build_search_snippet(searchable_text, q_lower, q_tokens)
                call_id = str(msg["call_id"]) if msg.get("call_id") else None
                turn_candidates.append((turn_cov, _iso_sort_value(ts), ts, role, snip, call_id))

            q_set = set(q_tokens)
            conv_tokens = set(tokenize_query("\n".join([title, episode_summary, *turn_texts])))
            linked_fact_covs: list[float] = []
            linked_fact_texts: list[str] = []
            linked_fact_vecs: list[list[float]] = []
            for rec in fact_records:
                shared = rec["tokens"] & conv_tokens
                if not shared:
                    continue
                non_query_fact = rec["tokens"] - q_set
                has_topic_overlap = (
                    len(shared) >= 2
                    or rec["date"] == conv_date
                    or any(len(tok) >= 5 for tok in shared)
                )
                is_linked = (
                    bool(non_query_fact & conv_tokens) and has_topic_overlap
                    if non_query_fact
                    else has_topic_overlap
                )
                if is_linked:
                    linked_fact_covs.append(float(rec["cov"]))
                    linked_fact_texts.append(str(rec["text"]))
                    if rec["vector"] is not None:
                        linked_fact_vecs.append(rec["vector"])

            fact_cov = max(linked_fact_covs, default=0.0)
            max_turn_cov = max((c[0] for c in turn_candidates), default=0.0)
            lexical_score, _, _ = score_conversation_lexical(
                query_tokens=q_tokens,
                query_lower=q_lower,
                title=title,
                episode_summary=episode_summary,
                fact_cov=fact_cov,
                max_turn_cov=max_turn_cov,
                turn_texts_for_cov=turn_texts,
                fact_texts_for_cov=linked_fact_texts,
            )

            candidate_vecs = ep_vecs + linked_fact_vecs
            semantic_score: float | None = None
            if query_vec is not None and candidate_vecs:
                semantic_score = max(cosine_similarity(query_vec, vec) for vec in candidate_vecs)

            if lexical_score <= 0.0 and (semantic_score is None or semantic_score < 0.25):
                continue

            final_score = blend_scores(lexical_score, semantic_score)

            turn_candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
            matching_snippets: list[dict[str, str]] = []
            seen_snips: set[tuple[str, str, str]] = set()
            top_role = "user"
            top_ts = effective_updated_at
            top_snippet = ""
            top_call_id: str | None = None

            for _cov, _ts_val, ts, role, snip, call_id in turn_candidates:
                if not top_snippet:
                    top_role = role
                    top_ts = ts
                    top_snippet = snip
                    top_call_id = call_id
                dedup_key = (role, ts, snip)
                if dedup_key in seen_snips:
                    continue
                seen_snips.add(dedup_key)
                matching_snippets.append(
                    {
                        "role": role,
                        "timestamp": ts,
                        "excerpt": snip,
                        "snippet": snip,
                    }
                )
                if len(matching_snippets) >= 3:
                    break

            if not matching_snippets:
                if episode_summary:
                    ep_snip = build_search_snippet(episode_summary, q_lower, q_tokens)
                    top_role = "episode"
                    top_ts = effective_updated_at
                    top_snippet = ep_snip
                    matching_snippets.append(
                        {
                            "role": "episode",
                            "timestamp": effective_updated_at,
                            "excerpt": ep_snip,
                            "snippet": ep_snip,
                        }
                    )
                else:
                    rep_msg = next(
                        (
                            m
                            for m in msgs
                            if m.get("role") == "user" and str(m.get("text", "")).strip()
                        ),
                        msgs[0] if msgs else None,
                    )
                    if rep_msg is not None:
                        rep_text = _format_searchable_message_text(rep_msg) or title
                        top_role = str(rep_msg.get("role") or "user")
                        top_ts = str(rep_msg.get("timestamp") or effective_updated_at)
                        top_snippet = build_search_snippet(rep_text, q_lower, q_tokens)
                        top_call_id = str(rep_msg["call_id"]) if rep_msg.get("call_id") else None
                    else:
                        top_snippet = build_search_snippet(title, q_lower, q_tokens)
                    matching_snippets.append(
                        {
                            "role": top_role,
                            "timestamp": top_ts,
                            "excerpt": top_snippet,
                            "snippet": top_snippet,
                        }
                    )

            hit: dict[str, Any] = {
                "conversation_id": sid,
                "session_id": sid,
                "title": title,
                "updated_at": effective_updated_at,
                "episode_summary": episode_summary,
                "matching_snippets": matching_snippets,
                "score": round(final_score, 4),
                "semantic_score": round(semantic_score, 4) if semantic_score is not None else None,
                "lexical_score": round(lexical_score, 4),
                "role": top_role,
                "timestamp": top_ts,
                "snippet": top_snippet,
                "source": source,
            }
            if top_call_id:
                hit["call_id"] = top_call_id

            scored_hits.append(
                (
                    round(final_score, 6),
                    _iso_sort_value(effective_updated_at),
                    effective_updated_at,
                    hit,
                )
            )

        scored_hits.sort(
            key=lambda item: (item[0], item[1], item[2], item[3]["session_id"]),
            reverse=True,
        )
        return [item[3] for item in scored_hits[:cap]]

    def read_conversation(self, conversation_id: str, max_turns: int = 40) -> dict[str, Any] | None:
        """Read full chronological message turns and Episode summary for a conversation."""
        cid = (conversation_id or "").strip()
        if not cid:
            return None

        tr = self.get_transcript(cid)
        if tr is None:
            return None

        sid = str(tr.get("session_id") or cid)
        resolved_sid = self.resolve_session_id(sid)
        messages = list(tr.get("messages", []))
        cap = max(1, min(int(max_turns or 40), 500))
        truncated = len(messages) > cap
        selected = messages[-cap:] if truncated else messages

        turns_out: list[dict[str, str]] = []
        for msg in selected:
            raw_text = str(msg.get("text") or "").strip()
            tool_calls = msg.get("tool_calls") or []
            if raw_text:
                content = raw_text
            elif tool_calls:
                content = _format_searchable_message_text(msg)
            else:
                content = ""
            turns_out.append(
                {
                    "role": str(msg.get("role") or "assistant"),
                    "content": content,
                    "timestamp": str(
                        msg.get("timestamp") or tr.get("updated_at") or tr.get("started_at") or ""
                    ),
                }
            )

        episode_map = self._get_episode_summary_map()
        episode_summary, _ = self._collect_conversation_episodes(
            session_id=sid,
            resolved_sid=resolved_sid,
            messages=messages,
            episode_map=episode_map,
        )

        header_updated = str(tr.get("updated_at") or tr.get("started_at") or "")
        last_msg_ts = str(messages[-1].get("timestamp") or "") if messages else ""
        effective_updated_at = (
            _latest_iso_timestamp(header_updated, last_msg_ts) if last_msg_ts else header_updated
        )

        return {
            "conversation_id": sid,
            "session_id": sid,
            "title": str(tr.get("title") or "Conversation"),
            "updated_at": effective_updated_at,
            "episode_summary": episode_summary,
            "total_turns": len(messages),
            "truncated": truncated,
            "turns": turns_out,
        }

    def search_memory(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Hybrid semantic + token-ranked search directly across Durable Facts and Episodes."""
        q_raw = (query or "").strip()
        if not q_raw:
            return []

        cap = max(1, min(int(limit or 10), 50))
        q_lower = q_raw.lower()
        q_tokens = tokenize_query(q_raw)

        try:
            self.backfill_embeddings()
        except Exception as exc:
            logger.debug("Lazy embedding backfill skipped in search_memory: %s", exc)

        query_vec: list[float] | None = None
        try:
            query_vec = self.embedding_provider.embed(q_raw)
        except Exception as exc:
            logger.debug("Query embedding skipped in search_memory: %s", exc)

        candidates: list[tuple[float, str, str, dict[str, Any]]] = []

        for section_name, entries in self.parse_sections().items():
            for entry in entries:
                f_text = (entry.text or "").strip()
                if not f_text:
                    continue
                lex = score_memory_lexical(
                    query_tokens=q_tokens,
                    query_lower=q_lower,
                    text=f_text,
                    weight=WEIGHT_FACT,
                )
                vec = self.embedding_store.get_vector(f"fact:{entry.id}", expected_text=f_text)
                sem: float | None = None
                if query_vec is not None and vec is not None:
                    sem = cosine_similarity(query_vec, vec)

                if lex <= 0.0 and (sem is None or sem < 0.25):
                    continue

                score = blend_scores(lex, sem)
                candidates.append(
                    (
                        round(score, 6),
                        str(entry.date or ""),
                        f"fact:{entry.id}",
                        {
                            "kind": "fact",
                            "item_id": entry.id,
                            "section": entry.section or section_name,
                            "date": str(entry.date or ""),
                            "source": str(entry.source or "stated"),
                            "session_id": "",
                            "conversation_id": "",
                            "text": f_text,
                            "score": round(score, 4),
                            "semantic_score": round(sem, 4) if sem is not None else None,
                            "lexical_score": round(lex, 4),
                        },
                    )
                )

        for ep in self.get_recent_episodes(limit=0):
            sid = str(ep.get("session_id") or "").strip()
            summary = str(ep.get("summary") or "").strip()
            if not sid or not summary:
                continue
            lex = score_memory_lexical(
                query_tokens=q_tokens,
                query_lower=q_lower,
                text=summary,
                weight=WEIGHT_EPISODE,
            )
            vec = self.embedding_store.get_vector(f"episode:{sid}", expected_text=summary)
            sem = None
            if query_vec is not None and vec is not None:
                sem = cosine_similarity(query_vec, vec)

            if lex <= 0.0 and (sem is None or sem < 0.25):
                continue

            score = blend_scores(lex, sem)
            ep_date = str(ep.get("date") or "")
            candidates.append(
                (
                    round(score, 6),
                    ep_date,
                    f"episode:{sid}",
                    {
                        "kind": "episode",
                        "item_id": sid,
                        "section": "Episodes",
                        "date": ep_date,
                        "source": "episode",
                        "session_id": sid,
                        "conversation_id": sid,
                        "text": summary,
                        "score": round(score, 4),
                        "semantic_score": round(sem, 4) if sem is not None else None,
                        "lexical_score": round(lex, 4),
                    },
                )
            )

        candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        return [item[3] for item in candidates[:cap]]

    def append_to_active_chat(
        self,
        session_id: str,
        *,
        content: str,
        label: str | None = None,
        timestamp: datetime | None = None,
    ) -> dict[str, Any]:
        """Write an assistant message directly into the user's active focused text chat."""
        self.validate_session_id(session_id)
        if session_id == VOICE_COMPANION_SESSION_ID:
            raise ValueError("Cannot post active chat content to voice_companion.")
        body = (content or "").strip()
        if not body:
            raise ValueError("Content cannot be empty.")

        clean_label = label.strip() if isinstance(label, str) and label.strip() else None
        formatted_text = f"**{clean_label}**\n\n{body}" if clean_label else body
        dt = timestamp or datetime.now().astimezone()

        self.append_transcript_message(
            session_id=session_id,
            role="assistant",
            text=formatted_text,
            title=clean_label or body,
            timestamp=dt,
        )
        try:
            self.update_transcript_header(session_id)
        except Exception:
            pass

        tr = self.get_transcript(session_id)
        msgs = tr.get("messages", []) if tr else []
        title = str(tr.get("title") or "Conversation") if tr else _truncate(clean_label or body)
        updated_at = str(tr.get("updated_at") or dt.isoformat()) if tr else dt.isoformat()
        return {
            "session_id": session_id,
            "title": title,
            "updated_at": updated_at,
            "message": formatted_text,
            "turns": max(1, (len(msgs) + 1) // 2),
        }

    def create_conversation(
        self,
        *,
        session_id: str,
        title: str,
        initial_assistant_message: str = "",
        timestamp: datetime | None = None,
    ) -> dict[str, Any]:
        """Create a titled transcript file and optionally append an initial assistant message."""
        self.validate_session_id(session_id)
        if session_id == VOICE_COMPANION_SESSION_ID:
            raise ValueError("Cannot create a standard chat conversation for voice_companion.")
        self.ensure_initialized()
        dt = timestamp or datetime.now().astimezone()
        clean_title = re.sub(r"<[^>]*>", "", " ".join((title or "").split())).strip()[:100]
        if not clean_title:
            clean_title = "New conversation"

        path = self.transcript_path(session_id)
        if not path.exists():
            header = {
                "session_id": session_id,
                "started_at": dt.isoformat(),
                "title": clean_title,
                "pinned": False,
                "updated_at": dt.isoformat(),
            }
            with path.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(header, ensure_ascii=False) + "\n")
        else:
            self.update_transcript_header(session_id, title=clean_title)

        body = (initial_assistant_message or "").strip()
        if body:
            self.append_transcript_message(
                session_id=session_id,
                role="assistant",
                text=body,
                title=clean_title,
                timestamp=dt,
            )
            try:
                self.update_transcript_header(session_id, title=clean_title)
            except Exception:
                pass

        tr = self.get_transcript(session_id)
        msgs = tr.get("messages", []) if tr else []
        started_at = str((tr or {}).get("started_at") or dt.isoformat())
        updated_at = str((tr or {}).get("updated_at") or dt.isoformat())
        return {
            "session_id": session_id,
            "title": str((tr or {}).get("title") or clean_title),
            "started_at": started_at,
            "updated_at": updated_at,
            "message": body,
            "turns": (len(msgs) + 1) // 2,
        }

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
                    if user_msg:
                        turns.append(
                            {
                                "time": user_time or time_fmt,
                                "user": user_msg,
                                "assistant": "",
                            }
                        )
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
                    user_time = ""
            if user_msg:
                turns.append(
                    {
                        "time": user_time,
                        "user": user_msg,
                        "assistant": "",
                    }
                )
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
        _atomic_replace_file(tmp, self.background_tasks_path)

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


def _build_search_snippet(text: str, q_lower: str, q_tokens: list[str], window: int = 240) -> str:
    """Extract a readable snippet centered around the first query match."""
    return build_search_snippet(text, q_lower, q_tokens, window=window)


def _iso_sort_value(ts: str) -> float:
    """Convert an ISO-8601 timestamp string into a numeric epoch key for sorting."""
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return 0.0


def _latest_iso_timestamp(ts_a: str, ts_b: str) -> str:
    """Return whichever non-empty ISO-8601 timestamp represents the later instant."""
    a = (ts_a or "").strip()
    b = (ts_b or "").strip()
    if not a:
        return b
    if not b:
        return a
    val_a = _iso_sort_value(a)
    val_b = _iso_sort_value(b)
    if val_a > 0.0 or val_b > 0.0:
        return b if val_b >= val_a else a
    return max(a, b)


def _format_searchable_message_text(msg: dict[str, Any]) -> str:
    """Combine message text and any embedded tool call invocations/outputs."""
    text = str(msg.get("text") or "").strip()
    tool_calls = msg.get("tool_calls") or []
    tool_parts: list[str] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        tname = str(tc.get("name") or "")
        targs = tc.get("arguments")
        targs_str = json.dumps(targs, ensure_ascii=False) if targs else ""
        tout = str(tc.get("output") or "")
        tool_parts.append(f"{tname} {targs_str} {tout[:400]}")
    tool_text = " ".join(tool_parts).strip()
    return f"{text} {tool_text}".strip() if tool_text else text


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


def prune_diagnostic_logs(
    max_age_days: int = 7,
    max_entries: int = 500,
    data_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Convenience wrapper for MemoryStore.prune_diagnostic_logs."""
    return MemoryStore(base_dir=data_dir).prune_diagnostic_logs(
        max_age_days=max_age_days,
        max_entries=max_entries,
        now=now,
    )
