"""Voice system instruction builder and clearly marked memory-integration stubs.

Memory injection, transcript storage (``logs/transcripts/``), and call-end
consolidation (``consolidation`` / ``episodes``) are intentionally deferred until
the memory/Activity/latency branch is merged.

To prevent live calls from crashing while memory hooks are deferred, safe helpers
(``resolve_voice_system_instruction``, ``safe_load_voice_memory_context``,
``safe_store_voice_transcript``, and ``safe_consolidate_voice_call``) log a single
warning when a hook is unready and gracefully continue execution.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

logger = logging.getLogger(__name__)

BASE_VOICE_SYSTEM_INSTRUCTION = (
    "You are Clea, a helpful, concise, natural-sounding personal desktop AI assistant "
    "talking with the user in a live voice call.\n"
    "- Match the user's language naturally: reply in English when they speak English, "
    "Hindi when they speak Hindi, and natural Hinglish (everyday conversational Hindi-English "
    "code-switching) when they mix both. Avoid overly formal or textbook Hindi unless requested.\n"
    "- Keep spoken replies crisp, conversational, and easy to follow out loud (no markdown lists "
    "or code blocks unless asked).\n"
    "- Use `delegate_task` for desktop, file, system, or multi-step work; and use "
    "`end_call` when the user asks to end the call.\n"
    "- Screen Vision & Freshness Rules (CRITICAL):\n"
    "  * NEVER answer questions about what is currently on the screen, which app is open, or "
    "what code/UI text says from memory of screenshots earlier in the call. ALWAYS call "
    "`look_at_screen` again on EVERY turn that asks about the current screen or app state.\n"
    "  * Prefer `look_at_screen(target='active_window', ...)` when reading code, editor tabs, "
    "terminal logs, or fine UI details so you receive a 1:1 native-resolution capture; use "
    "`target='full_screen'` only when the user asks about the whole desktop or multiple windows.\n"
    "  * Trust the structured ground-truth window metadata returned with every screenshot "
    "(`active_app_name`, `active_process_name`, `active_window_title`, `captured_at`) instead "
    "of guessing the application name visually from dark-mode pixels. Only state visual details "
    "you can actually read in the latest screenshot."
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


async def safe_load_voice_memory_context(
    *,
    loader: Callable[[], Awaitable[str]] = load_voice_memory_context,
) -> str | None:
    """Safely load voice memory context, returning None if unimplemented or failing."""
    try:
        return await loader()
    except NotImplementedError as exc:
        logger.warning(
            "Voice memory context hook is not implemented; proceeding with no memory excerpt: %s",
            exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed loading voice memory context; proceeding with no memory excerpt: %s",
            exc,
        )
        return None


async def resolve_voice_system_instruction(
    *,
    memory_context: str | None = None,
    memory_loader: Callable[[], Awaitable[str]] | None = load_voice_memory_context,
) -> str:
    """Resolve the voice system instruction, safely falling back if memory is unready.

    If ``memory_context`` is already provided, it is used directly.
    Otherwise, ``memory_loader`` is invoked. If it is unimplemented or fails,
    it falls back to ``BASE_VOICE_SYSTEM_INSTRUCTION`` with no memory excerpt,
    logs a single warning, and continues.
    """
    if memory_context is not None:
        return build_voice_system_instruction(memory_context=memory_context)

    if memory_loader is None:
        return BASE_VOICE_SYSTEM_INSTRUCTION

    context = await safe_load_voice_memory_context(loader=memory_loader)
    return build_voice_system_instruction(memory_context=context)


async def safe_store_voice_transcript(
    *,
    call_id: str,
    entries: Sequence[dict[str, Any]],
    handler: Callable[..., Awaitable[None]] = store_voice_transcript,
) -> None:
    """Persist transcript entries safely without failing the call if unimplemented."""
    try:
        await handler(call_id=call_id, entries=entries)
    except NotImplementedError as exc:
        logger.warning(
            "Voice transcript hook not implemented for call %s; skipping persistence: %s",
            call_id,
            exc,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed storing voice transcript for call %s; skipping persistence: %s",
            call_id,
            exc,
        )


async def safe_consolidate_voice_call(
    *,
    call_id: str,
    handler: Callable[..., Awaitable[None]] = consolidate_voice_call,
) -> None:
    """Trigger call-end consolidation safely without failing the call if unimplemented."""
    try:
        await handler(call_id=call_id)
    except NotImplementedError as exc:
        logger.warning(
            "Voice consolidation hook not implemented for call %s; skipping: %s",
            call_id,
            exc,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed consolidating voice call %s; skipping consolidation: %s",
            call_id,
            exc,
        )
