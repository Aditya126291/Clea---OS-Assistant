"""Thread-safe SQLite WAL database backing Tier 2 episodic FTS5 and Tier 3 fact bank."""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel

logger = logging.getLogger(__name__)

FACT_CATEGORIES: tuple[str, ...] = (
    "preference",
    "environment",
    "project",
    "workflow",
    "identity",
    "constraint",
)

FactCategory = Literal[
    "preference",
    "environment",
    "project",
    "workflow",
    "identity",
    "constraint",
]
FactStatus = Literal["active", "superseded", "deleted"]
FactOperation = Literal["add", "update", "delete", "noop"]
SurfaceType = Literal["chat", "voice"]

SYNTHETIC_EPISODIC_SESSION_IDS: frozenset[str] = frozenset({"voice_companion"})


def is_synthetic_episodic_session(session_id: str | None) -> bool:
    """Return True if session_id is a synthetic UI container that must not be indexed."""
    return (session_id or "").strip().lower() in SYNTHETIC_EPISODIC_SESSION_IDS

_CATEGORY_ALIASES: dict[str, FactCategory] = {
    "preference": "preference",
    "preferences": "preference",
    "pref": "preference",
    "likes": "preference",
    "style": "preference",
    "environment": "environment",
    "env": "environment",
    "tools": "environment",
    "tool": "environment",
    "setup": "environment",
    "system": "environment",
    "hardware": "environment",
    "os": "environment",
    "software": "environment",
    "apps": "environment",
    "project": "project",
    "projects": "project",
    "repo": "project",
    "repos": "project",
    "workspace": "project",
    "workflow": "workflow",
    "workflows": "workflow",
    "decision": "workflow",
    "decisions": "workflow",
    "open threads": "workflow",
    "open_threads": "workflow",
    "open-threads": "workflow",
    "process": "workflow",
    "habit": "workflow",
    "routine": "workflow",
    "identity": "identity",
    "profile": "identity",
    "bio": "identity",
    "personal": "identity",
    "name": "identity",
    "location": "identity",
    "constraint": "constraint",
    "constraints": "constraint",
    "rule": "constraint",
    "rules": "constraint",
    "restriction": "constraint",
    "restrictions": "constraint",
    "limit": "constraint",
    "limits": "constraint",
    "boundary": "constraint",
}

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


def normalize_fact_category(raw: str | None) -> FactCategory:
    """Map arbitrary category/section aliases into canonical Tier 3 FactCategory."""
    if not raw:
        return "preference"
    cleaned = " ".join(str(raw).strip().lower().split())
    if cleaned in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[cleaned]
    underscored = cleaned.replace("-", "_").replace(" ", "_")
    if underscored in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[underscored]
    return "preference"


def slugify_fact_key(text: str, category: str = "") -> str:
    """Generate a deterministic, SQL-safe slug key from fact text."""
    raw = (text or "").strip().lower()
    tokens = [tok.strip("_") for tok in re.findall(r"[a-z0-9]+", raw) if tok.strip("_")]
    slug = "_".join(tokens[:8])[:64].strip("_")
    if slug:
        return slug
    cat = normalize_fact_category(category)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"{cat}_{digest}"


def sanitize_fts5_query(query: str) -> str:
    """Convert arbitrary user text into a safe FTS5 MATCH expression."""
    if not query:
        return ""
    raw_tokens = re.findall(r"\w+", query, flags=re.UNICODE)
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw_tok in raw_tokens:
        tok = raw_tok.strip("_").replace('"', "")
        if not tok:
            continue
        key = tok.lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(f'"{tok}"')
    return " OR ".join(cleaned)


def _normalize_surface(surface: str | None) -> SurfaceType:
    if surface and str(surface).strip().lower() in ("voice", "voice_companion"):
        return "voice"
    return "chat"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_text(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _tokenize_for_match(text: str) -> set[str]:
    return {tok for tok in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(tok) >= 2}


class MemoryFactRecord(BaseModel):
    """Structured Tier 3 durable fact record stored in SQLite."""

    id: str
    category: FactCategory
    fact_key: str
    fact_text: str
    confidence: float = 0.9
    source_surface: SurfaceType = "chat"
    source_session_id: str = ""
    status: FactStatus = "active"
    created_at: str = ""
    updated_at: str = ""


class MemoryDatabase:
    """Thread-safe SQLite WAL store for Tier 2 episodic sessions and Tier 3 facts."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self._lock = threading.RLock()
        self._schema_ensured = False

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), timeout=5.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
                conn.execute("PRAGMA busy_timeout=5000;")
                conn.execute("PRAGMA foreign_keys=ON;")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def ensure_schema(self) -> None:
        """Create Tier 2 and Tier 3 tables, FTS5 virtual table, triggers, and indexes."""
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS episodic_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL UNIQUE,
                    surface TEXT NOT NULL CHECK (surface IN ('chat', 'voice')),
                    started_at TEXT NOT NULL DEFAULT '',
                    ended_at TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    topics TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS episodic_sessions_fts USING fts5(
                    session_id UNINDEXED,
                    surface UNINDEXED,
                    title,
                    summary,
                    topics,
                    content='episodic_sessions',
                    content_rowid='id',
                    tokenize='porter unicode61'
                );

                CREATE TRIGGER IF NOT EXISTS episodic_sessions_ai
                AFTER INSERT ON episodic_sessions BEGIN
                    INSERT INTO episodic_sessions_fts(
                        rowid, session_id, surface, title, summary, topics
                    )
                    VALUES (
                        new.id, new.session_id, new.surface, new.title, new.summary, new.topics
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS episodic_sessions_ad
                AFTER DELETE ON episodic_sessions BEGIN
                    INSERT INTO episodic_sessions_fts(
                        episodic_sessions_fts, rowid, session_id, surface, title, summary, topics
                    )
                    VALUES (
                        'delete', old.id, old.session_id, old.surface, old.title, old.summary,
                        old.topics
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS episodic_sessions_au
                AFTER UPDATE ON episodic_sessions BEGIN
                    INSERT INTO episodic_sessions_fts(
                        episodic_sessions_fts, rowid, session_id, surface, title, summary, topics
                    )
                    VALUES (
                        'delete', old.id, old.session_id, old.surface, old.title, old.summary,
                        old.topics
                    );
                    INSERT INTO episodic_sessions_fts(
                        rowid, session_id, surface, title, summary, topics
                    )
                    VALUES (
                        new.id, new.session_id, new.surface, new.title, new.summary, new.topics
                    );
                END;

                CREATE TABLE IF NOT EXISTS memory_facts (
                    id TEXT PRIMARY KEY,
                    category TEXT NOT NULL CHECK (
                        category IN (
                            'preference', 'environment', 'project',
                            'workflow', 'identity', 'constraint'
                        )
                    ),
                    fact_key TEXT NOT NULL,
                    fact_text TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.9,
                    source_surface TEXT NOT NULL DEFAULT 'chat' CHECK (
                        source_surface IN ('chat', 'voice')
                    ),
                    source_session_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active' CHECK (
                        status IN ('active', 'superseded', 'deleted')
                    ),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_memory_facts_status_updated
                ON memory_facts(status, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_memory_facts_category_key_status
                ON memory_facts(category, fact_key, status);
                """
            )
            self._schema_ensured = True

    def _ensure_ready(self) -> None:
        if not self._schema_ensured or not self.db_path.exists():
            self.ensure_schema()

    def close(self) -> None:
        """Checkpoint WAL if database exists; connections are short-lived per call."""
        with self._lock:
            if not self.db_path.exists():
                return
            try:
                with self._connect() as conn:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            except Exception as exc:
                logger.debug("WAL checkpoint on close skipped for %s: %s", self.db_path, exc)

    # --- Tier 2: Episodic Sessions + FTS5 ---

    def upsert_episodic_session(
        self,
        *,
        session_id: str,
        surface: str = "chat",
        started_at: str = "",
        ended_at: str = "",
        title: str = "",
        summary: str = "",
        topics: str | Sequence[str] = "",
        updated_at: str | None = None,
    ) -> dict[str, Any] | None:
        """Insert or update an episodic session summary using ON CONFLICT DO UPDATE."""
        sid = (session_id or "").strip()
        if not sid:
            raise ValueError("session_id cannot be empty.")
        if is_synthetic_episodic_session(sid):
            logger.debug("Skipping episodic indexing for synthetic session '%s'", sid)
            return None

        self._ensure_ready()
        resolved_surface = _normalize_surface(surface)
        if isinstance(topics, (list, tuple, set)):
            topics_str = ", ".join(str(t).strip() for t in topics if str(t).strip())
        else:
            topics_str = str(topics or "").strip()
        ts_now = (updated_at or "").strip() or _utc_now_iso()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO episodic_sessions (
                    session_id, surface, started_at, ended_at, title, summary, topics, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    surface = excluded.surface,
                    started_at = CASE
                        WHEN excluded.started_at != '' THEN excluded.started_at
                        ELSE episodic_sessions.started_at
                    END,
                    ended_at = CASE
                        WHEN excluded.ended_at != '' THEN excluded.ended_at
                        ELSE episodic_sessions.ended_at
                    END,
                    title = CASE
                        WHEN excluded.title != '' THEN excluded.title
                        ELSE episodic_sessions.title
                    END,
                    summary = CASE
                        WHEN excluded.summary != '' THEN excluded.summary
                        ELSE episodic_sessions.summary
                    END,
                    topics = CASE
                        WHEN excluded.topics != '' THEN excluded.topics
                        ELSE episodic_sessions.topics
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    sid,
                    resolved_surface,
                    str(started_at or "").strip(),
                    str(ended_at or "").strip(),
                    str(title or "").strip(),
                    str(summary or "").strip(),
                    topics_str,
                    ts_now,
                ),
            )
            row = conn.execute(
                """
                SELECT session_id, surface, started_at, ended_at, title, summary, topics, updated_at
                FROM episodic_sessions
                WHERE session_id = ?
                """,
                (sid,),
            ).fetchone()
        return dict(row) if row is not None else {}

    def delete_episodic_session(self, session_id: str) -> bool:
        """Delete an episodic session row if present (triggers clean FTS5 sync)."""
        sid = (session_id or "").strip()
        if not sid:
            return False
        self._ensure_ready()
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM episodic_sessions WHERE session_id = ?", (sid,))
            return cur.rowcount > 0

    def search_episodic(
        self,
        query: str,
        *,
        limit: int = 5,
        surface: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search episodic sessions via FTS5 BM25 with parameterized LIKE fallback."""
        q_raw = (query or "").strip()
        if not q_raw:
            return []

        self._ensure_ready()
        cap = max(1, min(int(limit or 5), 50))
        surface_filter: SurfaceType | None = None
        if surface is not None and str(surface).strip():
            surface_filter = _normalize_surface(surface)

        fts_query = sanitize_fts5_query(q_raw)
        if fts_query:
            try:
                return self._search_episodic_fts(fts_query, limit=cap, surface=surface_filter)
            except sqlite3.OperationalError as exc:
                logger.debug("FTS5 search failed, falling back to LIKE: %s", exc)

        return self._search_episodic_like(q_raw, limit=cap, surface=surface_filter)

    def _search_episodic_fts(
        self,
        fts_query: str,
        *,
        limit: int,
        surface: SurfaceType | None,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT
                s.session_id,
                s.surface,
                s.started_at,
                s.ended_at,
                s.title,
                s.summary,
                s.topics,
                s.updated_at,
                bm25(episodic_sessions_fts, 0.0, 0.0, 5.0, 2.0, 3.0) AS bm25_score,
                snippet(episodic_sessions_fts, -1, '', '', '...', 32) AS fts_snippet
            FROM episodic_sessions_fts
            JOIN episodic_sessions s ON s.id = episodic_sessions_fts.rowid
            WHERE episodic_sessions_fts MATCH ?
        """
        params: list[Any] = [fts_query]
        if surface is not None:
            sql += " AND s.surface = ?"
            params.append(surface)
        sql += " ORDER BY bm25_score ASC, s.updated_at DESC LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            raw_bm25 = float(row["bm25_score"] or 0.0)
            score = round(max(0.0001, -raw_bm25), 6)
            summary = str(row["summary"] or "")
            snippet_text = str(row["fts_snippet"] or "").strip() or summary[:240]
            ts = str(row["ended_at"] or row["updated_at"] or row["started_at"] or "")
            results.append(
                {
                    "session_id": str(row["session_id"]),
                    "surface": str(row["surface"]),
                    "timestamp": ts,
                    "title": str(row["title"] or ""),
                    "summary": summary,
                    "topics": str(row["topics"] or ""),
                    "snippet": snippet_text,
                    "score": score,
                }
            )
        return results

    def _search_episodic_like(
        self,
        query: str,
        *,
        limit: int,
        surface: SurfaceType | None,
    ) -> list[dict[str, Any]]:
        tokens = [t.strip("_") for t in re.findall(r"\w+", query.lower()) if t.strip("_")]
        terms = tokens or [query.lower()]
        clauses: list[str] = []
        params: list[Any] = []
        for term in terms[:8]:
            like_pat = f"%{term}%"
            clauses.append(
                "(LOWER(title) LIKE ? OR LOWER(summary) LIKE ? OR LOWER(topics) LIKE ?)"
            )
            params.extend([like_pat, like_pat, like_pat])

        sql = f"""
            SELECT session_id, surface, started_at, ended_at, title, summary, topics, updated_at
            FROM episodic_sessions
            WHERE ({' OR '.join(clauses)})
        """
        if surface is not None:
            sql += " AND surface = ?"
            params.append(surface)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(limit * 4, 25))

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        scored: list[tuple[float, str, dict[str, Any]]] = []
        for row in rows:
            title = str(row["title"] or "")
            summary = str(row["summary"] or "")
            topics = str(row["topics"] or "")
            t_low, s_low, top_low = title.lower(), summary.lower(), topics.lower()
            score = 0.0
            for term in terms:
                if term in t_low:
                    score += 5.0
                if term in top_low:
                    score += 3.0
                if term in s_low:
                    score += 2.0
            if score <= 0.0:
                score = 0.5
            ts = str(row["ended_at"] or row["updated_at"] or row["started_at"] or "")
            hit = {
                "session_id": str(row["session_id"]),
                "surface": str(row["surface"]),
                "timestamp": ts,
                "title": title,
                "summary": summary,
                "topics": topics,
                "snippet": summary[:240] or title[:240],
                "score": round(score, 6),
            }
            scored.append((score, ts, hit))

        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in scored[:limit]]

    # --- Tier 3: Structured Fact Bank ---

    @staticmethod
    def _row_to_fact(row: sqlite3.Row) -> MemoryFactRecord:
        return MemoryFactRecord(
            id=str(row["id"]),
            category=row["category"],
            fact_key=str(row["fact_key"]),
            fact_text=str(row["fact_text"]),
            confidence=float(row["confidence"]),
            source_surface=row["source_surface"],
            source_session_id=str(row["source_session_id"] or ""),
            status=row["status"],
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
        )

    def add_fact(
        self,
        fact_text: str,
        *,
        category: str | None = None,
        fact_key: str | None = None,
        confidence: float = 0.9,
        source_surface: str = "chat",
        source_session_id: str = "",
        fact_id: str | None = None,
    ) -> MemoryFactRecord:
        """Insert a new active fact into memory_facts (rejecting secrets)."""
        clean_text = (fact_text or "").strip()
        if not clean_text:
            raise ValueError("Fact text cannot be empty.")
        if is_secret_or_sensitive(clean_text):
            raise ValueError("Fact contains sensitive credentials or secrets and was rejected.")

        self._ensure_ready()
        resolved_cat = normalize_fact_category(category)
        resolved_key = (
            fact_key.strip()
            if fact_key and fact_key.strip()
            else slugify_fact_key(clean_text, resolved_cat)
        )
        resolved_id = (
            fact_id.strip()
            if fact_id and fact_id.strip()
            else f"{resolved_cat[:4]}-{uuid4().hex[:8]}"
        )
        resolved_conf = max(0.0, min(1.0, float(confidence)))
        resolved_surface = _normalize_surface(source_surface)
        now_iso = _utc_now_iso()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_facts (
                    id, category, fact_key, fact_text, confidence,
                    source_surface, source_session_id, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    resolved_id,
                    resolved_cat,
                    resolved_key,
                    clean_text,
                    resolved_conf,
                    resolved_surface,
                    str(source_session_id or ""),
                    now_iso,
                    now_iso,
                ),
            )
            row = conn.execute("SELECT * FROM memory_facts WHERE id = ?", (resolved_id,)).fetchone()
        assert row is not None
        return self._row_to_fact(row)

    def update_fact(
        self,
        target_fact_id: str,
        *,
        fact_text: str,
        category: str | None = None,
        fact_key: str | None = None,
        confidence: float = 0.9,
        source_surface: str = "chat",
        source_session_id: str = "",
        new_fact_id: str | None = None,
    ) -> MemoryFactRecord:
        """Transactionally supersede an existing fact and insert a new active row."""
        clean_text = (fact_text or "").strip()
        if not clean_text:
            raise ValueError("Fact text cannot be empty.")
        if is_secret_or_sensitive(clean_text):
            raise ValueError("Fact contains sensitive credentials or secrets and was rejected.")

        self._ensure_ready()
        now_iso = _utc_now_iso()

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM memory_facts WHERE id = ?", (target_fact_id,)
            ).fetchone()
            if existing is None:
                raise ValueError(f"Fact '{target_fact_id}' not found.")

            resolved_cat = (
                normalize_fact_category(category) if category is not None else existing["category"]
            )
            resolved_key = (
                fact_key.strip()
                if fact_key and fact_key.strip()
                else str(existing["fact_key"])
            )
            resolved_id = (
                new_fact_id.strip()
                if new_fact_id and new_fact_id.strip()
                else f"{resolved_cat[:4]}-{uuid4().hex[:8]}"
            )
            resolved_conf = max(0.0, min(1.0, float(confidence)))
            resolved_surface = _normalize_surface(source_surface)

            conn.execute(
                """
                UPDATE memory_facts
                SET status = 'superseded', updated_at = ?
                WHERE id = ?
                """,
                (now_iso, target_fact_id),
            )
            conn.execute(
                """
                INSERT INTO memory_facts (
                    id, category, fact_key, fact_text, confidence,
                    source_surface, source_session_id, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    resolved_id,
                    resolved_cat,
                    resolved_key,
                    clean_text,
                    resolved_conf,
                    resolved_surface,
                    str(source_session_id or ""),
                    now_iso,
                    now_iso,
                ),
            )
            row = conn.execute("SELECT * FROM memory_facts WHERE id = ?", (resolved_id,)).fetchone()
        assert row is not None
        return self._row_to_fact(row)

    def touch_fact(self, fact_id: str) -> MemoryFactRecord | None:
        """Refresh updated_at on an existing fact and return the updated record."""
        self._ensure_ready()
        now_iso = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE memory_facts SET updated_at = ? WHERE id = ?",
                (now_iso, fact_id),
            )
            row = conn.execute("SELECT * FROM memory_facts WHERE id = ?", (fact_id,)).fetchone()
        return self._row_to_fact(row) if row is not None else None

    def delete_fact(
        self,
        query_or_id: str | None = None,
        *,
        fact_id: str | None = None,
        fact_key: str | None = None,
        query: str | None = None,
    ) -> list[str]:
        """Soft-delete matching active rows by id, fact_key, or text substring."""
        self._ensure_ready()
        target_id = (fact_id or "").strip()
        target_key = (fact_key or "").strip()
        target_query = (query if query is not None else (query_or_id or "")).strip()
        if not target_id and not target_key and not target_query:
            return []

        now_iso = _utc_now_iso()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, fact_key, fact_text FROM memory_facts WHERE status = 'active'"
            ).fetchall()

            matched_ids: list[str] = []
            q_low = target_query.lower()
            q_id = target_query.strip("[]").lower()
            for row in rows:
                rid = str(row["id"])
                rkey = str(row["fact_key"])
                rtext = str(row["fact_text"])

                is_match = False
                if target_id and rid == target_id:
                    is_match = True
                elif target_key and rkey.lower() == target_key.lower():
                    is_match = True
                elif target_query:
                    if (
                        rid.lower() == q_id
                        or rid.lower() == q_low
                        or rkey.lower() == q_low
                        or rtext.strip().lower() == q_low
                        or (len(q_low) >= 3 and q_low in rtext.lower())
                    ):
                        is_match = True

                if is_match:
                    matched_ids.append(rid)

            if matched_ids:
                placeholders = ", ".join("?" for _ in matched_ids)
                conn.execute(
                    f"""
                    UPDATE memory_facts
                    SET status = 'deleted', updated_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    [now_iso, *matched_ids],
                )

        return matched_ids

    def get_fact(self, fact_id: str) -> MemoryFactRecord | None:
        """Fetch a single fact row by ID regardless of status."""
        if not fact_id:
            return None
        self._ensure_ready()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM memory_facts WHERE id = ?", (fact_id,)).fetchone()
        return self._row_to_fact(row) if row is not None else None

    def list_facts(
        self,
        status: FactStatus | None = "active",
        category: str | None = None,
        limit: int = 25,
    ) -> list[MemoryFactRecord]:
        """List facts filtered by optional status and category, newest updated first."""
        self._ensure_ready()
        cap = max(1, min(int(limit or 25), 200))
        clauses: list[str] = []
        params: list[Any] = []

        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if category is not None and str(category).strip():
            clauses.append("category = ?")
            params.append(normalize_fact_category(category))

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT * FROM memory_facts
            {where_sql}
            ORDER BY updated_at DESC, created_at DESC
            LIMIT ?
        """
        params.append(cap)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_fact(row) for row in rows]

    def find_relevant_active_facts(
        self,
        query: str,
        *,
        category: str | None = None,
        limit: int = 10,
    ) -> list[MemoryFactRecord]:
        """Return active facts ranked by lexical relevance and optional category match."""
        self._ensure_ready()
        cap = max(1, min(int(limit or 10), 100))
        q_clean = (query or "").strip()
        norm_cat = normalize_fact_category(category) if category and str(category).strip() else None

        active = self.list_facts(status="active", limit=200)
        if not active:
            return []
        if not q_clean:
            if norm_cat:
                filtered = [f for f in active if f.category == norm_cat]
                return filtered[:cap]
            return active[:cap]

        q_low = q_clean.lower()
        q_tokens = _tokenize_for_match(q_clean)
        scored: list[tuple[float, str, MemoryFactRecord]] = []
        for fact in active:
            f_text_low = fact.fact_text.lower()
            f_key_low = fact.fact_key.lower()
            f_tokens = _tokenize_for_match(f"{fact.fact_key} {fact.fact_text}")

            score = 0.0
            if q_low in f_text_low or (len(q_low) >= 3 and q_low in f_key_low):
                score += 4.0
            if q_tokens and f_tokens:
                overlap = len(q_tokens & f_tokens)
                if overlap:
                    score += (overlap / len(q_tokens)) * 3.0 + overlap * 0.5
            if norm_cat is not None and fact.category == norm_cat:
                score += 0.5

            if score > 0.0:
                scored.append((score, fact.updated_at, fact))

        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in scored[:cap]]

    def format_active_facts_context(self, limit: int = 25) -> str:
        """Render active facts for system prompt injection, or '' if none exist."""
        facts = self.list_facts(status="active", limit=limit)
        if not facts:
            return ""
        lines = ["## Active Memory Facts"]
        for fact in facts:
            lines.append(f"- [{fact.category}] {fact.fact_text}")
        return "\n".join(lines)

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
        """High-level add/update/delete operation returning structured reconciliation info."""
        if fact_text and is_secret_or_sensitive(fact_text):
            raise ValueError("Fact contains sensitive credentials or secrets and was rejected.")

        self._ensure_ready()
        act = str(action).strip().lower()

        if act == "add":
            clean_text = (fact_text or "").strip()
            if not clean_text:
                raise ValueError("fact_text is required for action='add'.")
            resolved_cat = normalize_fact_category(category)
            resolved_key = (
                fact_key.strip()
                if fact_key and fact_key.strip()
                else slugify_fact_key(clean_text, resolved_cat)
            )
            norm_new = _normalize_text(clean_text)

            active_facts = self.list_facts(status="active", limit=200)
            for existing in active_facts:
                if _normalize_text(existing.fact_text) == norm_new:
                    touched = self.touch_fact(existing.id) or existing
                    return {
                        "ok": True,
                        "operation": "noop",
                        "fact": touched.model_dump(),
                        "superseded_id": None,
                        "affected_ids": [touched.id],
                        "message": "Matching active fact already exists.",
                    }

            for existing in active_facts:
                if existing.category == resolved_cat and existing.fact_key == resolved_key:
                    updated = self.update_fact(
                        existing.id,
                        fact_text=clean_text,
                        category=resolved_cat,
                        fact_key=resolved_key,
                        confidence=confidence,
                        source_surface=source_surface,
                        source_session_id=source_session_id,
                    )
                    return {
                        "ok": True,
                        "operation": "update",
                        "fact": updated.model_dump(),
                        "superseded_id": existing.id,
                        "affected_ids": [existing.id, updated.id],
                        "message": f"Superseded fact '{existing.id}' with '{updated.id}'.",
                    }

            created = self.add_fact(
                clean_text,
                category=resolved_cat,
                fact_key=resolved_key,
                confidence=confidence,
                source_surface=source_surface,
                source_session_id=source_session_id,
            )
            return {
                "ok": True,
                "operation": "add",
                "fact": created.model_dump(),
                "superseded_id": None,
                "affected_ids": [created.id],
                "message": f"Added fact '{created.id}'.",
            }

        if act == "update":
            clean_text = (fact_text or "").strip()
            if not clean_text:
                raise ValueError("fact_text is required for action='update'.")

            target: MemoryFactRecord | None = None
            if fact_id:
                cand = self.get_fact(fact_id.strip())
                if cand is not None and cand.status == "active":
                    target = cand

            if target is None and fact_key and fact_key.strip():
                norm_key = fact_key.strip().lower()
                norm_cat = normalize_fact_category(category) if category else None
                for item in self.list_facts(status="active", limit=200):
                    if item.fact_key.lower() == norm_key and (
                        norm_cat is None or item.category == norm_cat
                    ):
                        target = item
                        break

            if target is None and query and query.strip():
                q_matches = self.find_relevant_active_facts(query, category=category, limit=1)
                if q_matches:
                    target = q_matches[0]

            if target is None:
                created = self.add_fact(
                    clean_text,
                    category=category,
                    fact_key=fact_key,
                    confidence=confidence,
                    source_surface=source_surface,
                    source_session_id=source_session_id,
                )
                return {
                    "ok": True,
                    "operation": "add",
                    "fact": created.model_dump(),
                    "superseded_id": None,
                    "affected_ids": [created.id],
                    "message": f"No existing fact matched; added '{created.id}'.",
                }

            if _normalize_text(target.fact_text) == _normalize_text(clean_text):
                touched = self.touch_fact(target.id) or target
                return {
                    "ok": True,
                    "operation": "noop",
                    "fact": touched.model_dump(),
                    "superseded_id": None,
                    "affected_ids": [touched.id],
                    "message": "Fact unchanged.",
                }

            updated = self.update_fact(
                target.id,
                fact_text=clean_text,
                category=category if category is not None else target.category,
                fact_key=fact_key if fact_key is not None else target.fact_key,
                confidence=confidence,
                source_surface=source_surface,
                source_session_id=source_session_id,
            )
            return {
                "ok": True,
                "operation": "update",
                "fact": updated.model_dump(),
                "superseded_id": target.id,
                "affected_ids": [target.id, updated.id],
                "message": f"Updated fact '{target.id}' -> '{updated.id}'.",
            }

        if act == "delete":
            deleted_ids = self.delete_fact(
                fact_id=fact_id,
                fact_key=fact_key,
                query=query if query is not None else fact_text,
            )
            return {
                "ok": True,
                "operation": "delete" if deleted_ids else "noop",
                "fact": None,
                "superseded_id": None,
                "affected_ids": deleted_ids,
                "message": (
                    f"Deleted {len(deleted_ids)} fact(s)."
                    if deleted_ids
                    else "No matching active facts found."
                ),
            }

        raise ValueError(f"Unsupported action: {action!r}. Expected 'add', 'update', or 'delete'.")
