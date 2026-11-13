"""Built-in memory management tools: remember and forget."""

from __future__ import annotations

from clea.memory.store import FIXED_SECTIONS, MemoryStore
from clea.tools.registry import RiskLevel, ToolRegistry


def register_memory_tools(registry: ToolRegistry, memory: MemoryStore) -> None:
    """Register 'remember' (reversible) and 'forget' (destructive) tools."""

    async def remember_handler(fact: str, section: str = "Preferences") -> str:
        sec = section if section in FIXED_SECTIONS else "Preferences"
        entry = memory.add_or_update_entry(text=fact, section=sec, source="stated")
        return f"Remembered: {entry.text} in section '{sec}' [{entry.id}]."

    async def forget_handler(query: str) -> str:
        matches = memory.find_matches(query)
        if not matches:
            return f"No entries found matching '{query}'."

        removed = memory.remove_entries(query)
        removed_lines = [e.format_line() for e in removed]
        return f"Forgot {len(removed)} entries matching '{query}':\n" + "\n".join(removed_lines)

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
