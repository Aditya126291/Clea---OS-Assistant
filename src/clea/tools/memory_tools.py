"""Built-in memory management tools: remember, forget, search_episodic_memory, manage_memory."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from clea.memory.database import FACT_CATEGORIES, normalize_fact_category
from clea.memory.store import FIXED_SECTIONS, MemoryStore
from clea.tools.registry import RiskLevel, ToolRegistry

logger = logging.getLogger(__name__)


def register_memory_tools(registry: ToolRegistry, memory: MemoryStore) -> None:
    """Register memory read/write/search tools on the registry."""

    def _remember_sync(fact: str, section: str) -> str:
        sec = section if section in FIXED_SECTIONS else "Preferences"
        entry = memory.add_or_update_entry(text=fact, section=sec, source="stated")
        try:
            memory.ensure_initialized()
            memory.memory_db.manage_fact(
                "add",
                fact_text=entry.text,
                category=normalize_fact_category(sec),
                source_surface="chat",
            )
        except Exception as exc:
            logger.debug("Failed mirroring remember tool to SQLite fact bank: %s", exc)
        return f"Remembered: {entry.text} in section '{sec}' [{entry.id}]."

    async def remember_handler(fact: str, section: str = "Preferences") -> str:
        return await asyncio.to_thread(_remember_sync, fact, section)

    def _forget_sync(query: str) -> str:
        matches = memory.find_matches(query)
        if not matches:
            db_deleted: list[str] = []
            try:
                if memory.db_path.exists():
                    db_deleted = memory.memory_db.delete_fact(query=query)
            except Exception as exc:
                logger.debug("SQLite forget lookup failed: %s", exc)
            if db_deleted:
                return f"Forgot {len(db_deleted)} entries matching '{query}'."
            return f"No entries found matching '{query}'."

        removed = memory.remove_entries(query)
        try:
            if memory.db_path.exists():
                for entry in removed:
                    memory.memory_db.delete_fact(query=entry.text)
                memory.memory_db.delete_fact(query=query)
        except Exception as exc:
            logger.debug("Failed mirroring forget tool to SQLite fact bank: %s", exc)

        removed_lines = [e.format_line() for e in removed]
        return f"Forgot {len(removed)} entries matching '{query}':\n" + "\n".join(removed_lines)

    async def forget_handler(query: str) -> str:
        return await asyncio.to_thread(_forget_sync, query)

    async def search_episodic_memory_handler(
        query: str,
        limit: int = 5,
        surface: str | None = None,
    ) -> dict[str, Any]:
        capped_limit = max(1, min(int(limit or 5), 20))
        hits = await asyncio.to_thread(
            memory.search_episodic_memory,
            query,
            limit=capped_limit,
            surface=surface,
        )
        return {
            "query": query,
            "count": len(hits),
            "results": hits,
        }

    async def manage_memory_handler(
        action: str,
        fact_text: str | None = None,
        category: str | None = None,
        fact_key: str | None = None,
        fact_id: str | None = None,
        query: str | None = None,
        confidence: float = 0.9,
    ) -> dict[str, Any]:
        act = str(action or "").strip().lower()
        if act not in ("add", "update", "delete"):
            raise ValueError(f"Unsupported action: {action!r}. Expected add, update, or delete.")
        return await asyncio.to_thread(
            memory.manage_fact,
            act,  # type: ignore[arg-type]
            fact_text=fact_text,
            category=category,
            fact_key=fact_key,
            fact_id=fact_id,
            query=query,
            confidence=confidence,
            source_surface="chat",
        )

    registry.register(
        name="remember",
        description=(
            "Save a durable personal fact, preference, or context item explicitly stated "
            "by the user. Examples: user preferences, tools they use, project context."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "fact": {
                    "type": "string",
                    "description": "The exact fact or preference to remember.",
                },
                "section": {
                    "type": "string",
                    "description": "Target section in long-term memory.",
                    "enum": FIXED_SECTIONS,
                    "default": "Preferences",
                },
            },
            "required": ["fact"],
            "additionalProperties": False,
        },
        risk_level=RiskLevel.REVERSIBLE,
        handler=remember_handler,
    )

    registry.register(
        name="forget",
        description=(
            "Delete durable facts or preferences matching a description or entry ID. "
            "This is a destructive operation that requires user confirmation."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords, description, or entry ID of the memory to forget.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        risk_level=RiskLevel.DESTRUCTIVE,
        handler=forget_handler,
    )

    registry.register(
        name="search_episodic_memory",
        description=(
            "Search past chat and voice session summaries by topic, title, or keywords "
            "using local FTS5 BM25 retrieval."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords or topic phrase to search across past sessions.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of matching session summaries to return.",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20,
                },
                "surface": {
                    "type": "string",
                    "description": "Optional surface filter ('chat' or 'voice').",
                    "enum": ["chat", "voice"],
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        risk_level=RiskLevel.READ_ONLY,
        handler=search_episodic_memory_handler,
    )

    registry.register(
        name="manage_memory",
        description=(
            "Add, update (supersede), or delete structured durable facts in the local "
            "Tier 3 fact bank."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Operation to perform on the structured fact bank.",
                    "enum": ["add", "update", "delete"],
                },
                "fact_text": {
                    "type": "string",
                    "description": "Durable fact text for add or update.",
                },
                "category": {
                    "type": "string",
                    "description": "Canonical fact category.",
                    "enum": list(FACT_CATEGORIES),
                },
                "fact_key": {
                    "type": "string",
                    "description": "Optional stable snake_case key slug for the fact.",
                },
                "fact_id": {
                    "type": "string",
                    "description": "Target fact ID for update or delete.",
                },
                "query": {
                    "type": "string",
                    "description": "Search text to identify existing fact(s) for update or delete.",
                },
                "confidence": {
                    "type": "number",
                    "description": "Confidence score between 0.0 and 1.0.",
                    "default": 0.9,
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        risk_level=RiskLevel.REVERSIBLE,
        handler=manage_memory_handler,
    )
