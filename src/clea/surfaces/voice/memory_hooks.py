"""Voice system instruction builder and clearly marked memory-integration stubs.

Memory injection, transcript storage (``logs/transcripts/``), and call-end
consolidation (``consolidation`` / ``episodes``) are intentionally deferred until
the memory/Activity/latency branch is merged.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

BASE_VOICE_SYSTEM_INSTRUCTION = (
    "You are Clea, a helpful, concise, natural-sounding personal desktop AI assistant "
    "talking with the user in a live voice call.\n"
    "- Match the user's language naturally: reply in English when they speak English, "
    "Hindi when they speak Hindi, and natural Hinglish (everyday conversational Hindi-English "
    "code-switching) when they mix both. Avoid overly formal or textbook Hindi unless requested.\n"
    "- Keep spoken replies crisp, conversational, and easy to follow out loud (no markdown lists "
    "or code blocks unless asked).\n"
    "- Use `delegate_task` for desktop, file, system, or multi-step work; use "
    "`look_at_screen` or `watch_screen` only when screen context is actually needed; and use "
    "`end_call` when the user asks to end the call."
)


def build_voice_system_instruction(*, memory_context: str | None = None) -> str:
    """Build the live voice system instruction.

    TODO(phase3-memory): Once the memory branch is merged, load and inject
    long-term memory and recent episode summaries here instead of relying on an
    optional pre-supplied ``memory_context`` string.
    """
    if memory_context and memory_context.strip():
        return f"{BASE_VOICE_SYSTEM_INSTRUCTION}\n\n## Memory Context\n{memory_context.strip()}"
    return BASE_VOICE_SYSTEM_INSTRUCTION


async def load_voice_memory_context() -> str:
    """Stub for loading memory context for voice system instruction injection.

    TODO(phase3-memory): Implement over typed daemon protocol once memory/episodes
    endpoints are merged.
    """
    raise NotImplementedError(
        "Voice memory injection is deferred until the memory/Activity/latency branch is merged."
    )


async def store_voice_transcript(
    *,
    call_id: str,
    entries: Sequence[dict[str, Any]],
) -> None:
    """Stub for persisting live voice call transcripts (``logs/transcripts/``).

    TODO(phase3-memory): Implement transcript storage once the memory branch is merged.
    """
    raise NotImplementedError(
        "Voice transcript storage is deferred until the memory/Activity/latency branch is merged."
    )


async def consolidate_voice_call(*, call_id: str) -> None:
    """Stub for running call-end memory consolidation (``consolidation`` / ``episodes``).

    TODO(phase3-memory): Trigger call-end consolidation once the memory branch is merged.
    """
    raise NotImplementedError(
        "Voice call-end consolidation is deferred until the memory/Activity/latency branch "
        "is merged."
    )
