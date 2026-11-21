"""Three-tier memory bank facade and deterministic extractive session summarization."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from clea.memory.database import (
    FactStatus,
    MemoryDatabase,
    MemoryFactRecord,
    SurfaceType,
    is_synthetic_episodic_session,
)
from clea.memory.facts import FactBankReconciler, ReconciliationResult
from clea.memory.retrieval import STOPWORDS
from clea.memory.working import WORKING_SUMMARY_PREFIX, WorkingMemoryManager

if TYPE_CHECKING:
    from clea.providers.base import LLMProvider, Message

EPISODIC_SUMMARY_SYSTEM_PROMPT = (
    "You are Clea's episodic session summarizer. Summarize the completed session concisely for "
    "future recall. STRICT GROUNDING: Summarize ONLY facts, actions, and topics literally "
    "stated in the transcript. NEVER infer, speculate, or invent unmentioned topics, entities, "
    "or details to fill gaps. If the session is trivial or too short, write a single literal "
    "sentence describing only what was actually said (or return empty if no meaningful exchange "
    "occurred)."
)


def _extract_message_fields(msg: Message | dict[str, Any]) -> tuple[str, str, list[str]]:
    if isinstance(msg, dict):
        role = str(msg.get("role") or "")
        raw_text = msg.get("text") if msg.get("text") is not None else msg.get("content")
        text = str(raw_text or "").strip()
        tools: list[str] = []
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict) and tc.get("name"):
                tools.append(str(tc["name"]))
        if role == "tool" and msg.get("tool_name"):
            tools.append(str(msg["tool_name"]))
        return role, text, tools

    role = str(getattr(msg, "role", "") or "")
    text = str(getattr(msg, "content", "") or "").strip()
    tools = [
        str(getattr(tc, "name", ""))
        for tc in (getattr(msg, "tool_calls", None) or [])
        if getattr(tc, "name", None)
    ]
    tool_name = getattr(msg, "tool_name", None)
    if role == "tool" and tool_name:
        tools.append(str(tool_name))
    return role, text, tools


def build_extractive_session_summary(
    messages: Sequence[Message | dict[str, Any]],
    fallback_title: str = "",
) -> tuple[str, str, str]:
    """Deterministically derive (title, summary, topics) from Message objects or dicts."""
    user_texts: list[str] = []
    assistant_texts: list[str] = []
    tool_names: list[str] = []
    topic_tokens: Counter[str] = Counter()

    for msg in messages:
        role, text, tools = _extract_message_fields(msg)
        tool_names.extend(tools)
        if text.startswith(WORKING_SUMMARY_PREFIX) or text.startswith(
            "[Earlier conversation summary:"
        ):
            continue

        if role == "user" and text:
            user_texts.append(text)
            for tok in re.findall(r"[A-Za-z0-9_.-]+", text.lower()):
                clean_tok = tok.strip("._-")
                if len(clean_tok) >= 3 and clean_tok not in STOPWORDS and not clean_tok.isdigit():
                    topic_tokens[clean_tok] += 2
        elif role == "assistant" and text:
            assistant_texts.append(text)
            for tok in re.findall(r"[A-Za-z0-9_.-]+", text.lower()):
                clean_tok = tok.strip("._-")
                if len(clean_tok) >= 4 and clean_tok not in STOPWORDS and not clean_tok.isdigit():
                    topic_tokens[clean_tok] += 1

    for tname in tool_names:
        clean_t = tname.strip().lower()
        if clean_t:
            topic_tokens[clean_t] += 2

    raw_title = (fallback_title or "").strip()
    if not raw_title and user_texts:
        first_line = " ".join(user_texts[0].splitlines()[0].split())
        raw_title = first_line[:80] if len(first_line) <= 80 else first_line[:77] + "..."
    if not raw_title:
        raw_title = "Conversation"

    summary_lines: list[str] = []
    if user_texts:
        first_q = " ".join(user_texts[0].split())[:200]
        summary_lines.append(f"User: {first_q}")
        if len(user_texts) > 1:
            last_q = " ".join(user_texts[-1].split())[:200]
            if last_q != first_q:
                summary_lines.append(f"Follow-up: {last_q}")
    if assistant_texts:
        last_a = " ".join(assistant_texts[-1].split())[:200]
        summary_lines.append(f"Outcome: {last_a}")
    if tool_names:
        unique_tools = list(dict.fromkeys(tool_names))[:5]
        summary_lines.append(f"Tools: {', '.join(unique_tools)}")

    summary = "\n".join(summary_lines).strip() or raw_title
    top_topics = [tok for tok, _ in topic_tokens.most_common(8)]
    topics_str = ", ".join(top_topics)
    return raw_title, summary, topics_str


class ThreeTierMemoryBank:
    """Unified facade combining Tier 1 working memory, Tier 2 FTS5 episodes, and Tier 3 facts."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        db: MemoryDatabase | None = None,
        working: WorkingMemoryManager | None = None,
        facts: FactBankReconciler | None = None,
        provider: LLMProvider | None = None,
    ) -> None:
        if db is not None:
            self.db = db
        elif db_path is not None:
            self.db = MemoryDatabase(Path(db_path))
        else:
            raise ValueError("Either db or db_path must be provided to ThreeTierMemoryBank.")
        self.working = working or WorkingMemoryManager(provider=provider)
        self.facts = facts or FactBankReconciler(self.db, provider=provider)

    def ensure_schema(self) -> None:
        """Initialize the backing SQLite schema."""
        self.db.ensure_schema()

    def close(self) -> None:
        """Release database WAL/resources cleanly."""
        self.db.close()

    # --- Tier 1: Working Memory ---

    def tag_tool_artifact(
        self,
        msg: Message,
        *,
        turn_number: int,
        ttl_turns: int | None = None,
    ) -> bool:
        return self.working.tag_tool_artifact(msg, turn_number=turn_number, ttl_turns=ttl_turns)

    def expire_artifacts(self, messages: list[Message], *, current_turn: int) -> int:
        return self.working.expire_artifacts(messages, current_turn=current_turn)

    def needs_compaction(self, messages: Sequence[Message]) -> bool:
        return self.working.needs_compaction(messages)

    async def compact_if_needed(
        self,
        messages: list[Message],
        *,
        keep_recent_turns: int | None = None,
    ) -> tuple[list[Message], str | None]:
        return await self.working.compact_if_needed(messages, keep_recent_turns=keep_recent_turns)

    # --- Tier 2: Episodic Memory ---

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
        return self.db.upsert_episodic_session(
            session_id=session_id,
            surface=surface,
            started_at=started_at,
            ended_at=ended_at,
            title=title,
            summary=summary,
            topics=topics,
            updated_at=updated_at,
        )

    def index_session_messages(
        self,
        *,
        session_id: str,
        messages: Sequence[Message | dict[str, Any]],
        surface: str = "chat",
        started_at: str = "",
        ended_at: str = "",
        fallback_title: str = "",
    ) -> dict[str, Any] | None:
        if is_synthetic_episodic_session(session_id):
            return None
        title, summary, topics = build_extractive_session_summary(
            messages, fallback_title=fallback_title
        )
        return self.db.upsert_episodic_session(
            session_id=session_id,
            surface=surface,
            started_at=started_at,
            ended_at=ended_at,
            title=title,
            summary=summary,
            topics=topics,
        )

    def search_episodic(
        self,
        query: str,
        *,
        limit: int = 5,
        surface: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.db.search_episodic(query, limit=limit, surface=surface)

    # --- Tier 3: Fact Bank ---

    def add_fact(self, fact_text: str, **kwargs: Any) -> MemoryFactRecord:
        return self.db.add_fact(fact_text, **kwargs)

    def update_fact(self, target_fact_id: str, **kwargs: Any) -> MemoryFactRecord:
        return self.db.update_fact(target_fact_id, **kwargs)

    def delete_fact(self, query_or_id: str | None = None, **kwargs: Any) -> list[str]:
        return self.db.delete_fact(query_or_id, **kwargs)

    def list_facts(
        self,
        status: FactStatus | None = "active",
        category: str | None = None,
        limit: int = 25,
    ) -> list[MemoryFactRecord]:
        return self.db.list_facts(status=status, category=category, limit=limit)

    def manage_fact(
        self,
        action: Literal["add", "update", "delete"],
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self.db.manage_fact(action, **kwargs)

    def format_active_facts_context(self, limit: int = 25) -> str:
        return self.db.format_active_facts_context(limit=limit)

    def reconcile_explicit(
        self,
        text: str,
        *,
        source_surface: SurfaceType = "chat",
        source_session_id: str = "",
    ) -> ReconciliationResult:
        return self.facts.reconcile_explicit_sync(
            text,
            source_surface=source_surface,
            source_session_id=source_session_id,
        )

    async def reconcile_text(
        self,
        text: str,
        *,
        source_surface: SurfaceType = "chat",
        source_session_id: str = "",
    ) -> ReconciliationResult:
        return await self.facts.reconcile_text(
            text,
            source_surface=source_surface,
            source_session_id=source_session_id,
        )

    async def reconcile_messages(
        self,
        messages: Sequence[Message | dict[str, Any]],
        *,
        source_surface: SurfaceType = "chat",
        source_session_id: str = "",
    ) -> ReconciliationResult:
        return await self.facts.reconcile_messages(
            messages,
            source_surface=source_surface,
            source_session_id=source_session_id,
        )
