"""Tier 1 working-memory management: artifact TTL expiration and turn-boundary compaction."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from clea.providers.base import LLMProvider, Message

logger = logging.getLogger(__name__)

WORKING_SUMMARY_PREFIX = "[Session Working Summary]"

WORKING_SUMMARY_SYSTEM_PROMPT = (
    "You are Clea's working-memory compactor. Summarize the older conversation turns "
    "into a concise, factual working summary preserving key user goals, decisions, file paths, "
    "tool outcomes, and unresolved items. Output plain bullet points only (no preamble)."
)


class WorkingMemoryManager:
    """Manages Tier 1 per-session working memory, tool artifact TTL, and compaction."""

    def __init__(
        self,
        *,
        artifact_char_threshold: int = 1500,
        artifact_ttl_turns: int = 3,
        max_messages: int = 16,
        max_chars: int = 24000,
        keep_recent_turns: int = 6,
        llm_timeout_seconds: float = 15.0,
        provider: LLMProvider | None = None,
    ) -> None:
        self.artifact_char_threshold = max(0, int(artifact_char_threshold))
        self.artifact_ttl_turns = max(1, int(artifact_ttl_turns))
        self.max_messages = max(2, int(max_messages))
        self.max_chars = max(500, int(max_chars))
        self.keep_recent_turns = max(1, int(keep_recent_turns))
        self.llm_timeout_seconds = max(0.05, float(llm_timeout_seconds))
        self.provider = provider

    def tag_tool_artifact(
        self,
        msg: Message,
        *,
        turn_number: int,
        ttl_turns: int | None = None,
    ) -> bool:
        """Tag oversized tool output messages with turn index and expiration TTL."""
        if msg.role != "tool" or msg.artifact_expired:
            return False
        if len(msg.content or "") <= self.artifact_char_threshold:
            return False
        msg.turn_index = int(turn_number)
        msg.artifact_ttl_turns = (
            max(1, int(ttl_turns)) if ttl_turns is not None else self.artifact_ttl_turns
        )
        return True

    def expire_artifacts(self, messages: list[Message], *, current_turn: int) -> int:
        """Replace expired large tool artifacts with compact placeholders."""
        expired_count = 0
        for msg in messages:
            if msg.role != "tool" or msg.artifact_expired or msg.turn_index is None:
                continue
            is_tagged_artifact = (
                msg.artifact_ttl_turns is not None
                or len(msg.content or "") > self.artifact_char_threshold
            )
            if not is_tagged_artifact:
                continue
            effective_ttl = (
                msg.artifact_ttl_turns
                if msg.artifact_ttl_turns is not None
                else self.artifact_ttl_turns
            )
            if current_turn - msg.turn_index >= effective_ttl:
                tool_label = msg.tool_name or "tool"
                msg.content = (
                    f"[Expired tool artifact: {tool_label} from turn {msg.turn_index} "
                    f"\u2014 re-run if current state needed]"
                )
                msg.artifact_expired = True
                expired_count += 1
        return expired_count

    def needs_compaction(self, messages: Sequence[Message]) -> bool:
        """Return True when message count or total content chars exceed configured limits."""
        if len(messages) > self.max_messages:
            return True
        total_chars = sum(len(m.content or "") for m in messages)
        return total_chars > self.max_chars

    @staticmethod
    def _is_summary_message(msg: Message) -> bool:
        return msg.role == "user" and (msg.content or "").startswith(WORKING_SUMMARY_PREFIX)

    @staticmethod
    def _extract_prior_summary(messages: Sequence[Message]) -> str:
        parts: list[str] = []
        for msg in messages:
            if msg.role == "user" and (msg.content or "").startswith(WORKING_SUMMARY_PREFIX):
                body = msg.content[len(WORKING_SUMMARY_PREFIX) :].strip()
                if body:
                    parts.append(body)
        return "\n".join(parts).strip()

    @staticmethod
    def _format_turns_for_summary(messages: Sequence[Message]) -> str:
        lines: list[str] = []
        for msg in messages:
            content = (msg.content or "").strip()
            if msg.role == "user":
                lines.append(f"User: {content[:600]}")
            elif msg.role == "assistant":
                if content:
                    lines.append(f"Clea: {content[:600]}")
                if msg.tool_calls:
                    names = ", ".join(tc.name for tc in msg.tool_calls)
                    lines.append(f"Clea called tools: {names}")
            elif msg.role == "tool":
                snippet = content[:200].replace("\n", " ")
                lines.append(f"Tool ({msg.tool_name or 'tool'}): {snippet}")
        return "\n".join(lines)

    @staticmethod
    def _extractive_fallback(prior_summary: str, older_turns: Sequence[Message]) -> str:
        bullets: list[str] = []
        if prior_summary:
            for line in prior_summary.splitlines():
                cleaned = line.strip()
                if cleaned:
                    bullets.append(cleaned if cleaned.startswith("-") else f"- {cleaned}")

        for msg in older_turns:
            text = (msg.content or "").strip()
            if msg.role == "user" and text:
                first_line = " ".join(text.splitlines()[0].split())[:160]
                bullets.append(f"- User: {first_line}")
            elif msg.role == "assistant":
                if text:
                    first_line = " ".join(text.splitlines()[0].split())[:160]
                    bullets.append(f"- Assistant: {first_line}")
                elif msg.tool_calls:
                    names = ", ".join(tc.name for tc in msg.tool_calls)
                    bullets.append(f"- Tools used: {names}")

        if not bullets:
            return "- Earlier conversation context compacted."

        # Keep most recent compact bullets within a bounded working summary budget
        trimmed: list[str] = []
        char_count = 0
        for bullet in reversed(bullets):
            if char_count + len(bullet) + 1 > 1800 and trimmed:
                break
            trimmed.append(bullet)
            char_count += len(bullet) + 1
        trimmed.reverse()
        return "\n".join(trimmed)

    async def _summarize_with_provider(
        self,
        prior_summary: str,
        older_turns: Sequence[Message],
    ) -> str | None:
        if self.provider is None:
            return None

        prompt_parts: list[str] = []
        if prior_summary:
            prompt_parts.append(f"Prior Working Summary:\n{prior_summary}")
        transcript_block = self._format_turns_for_summary(older_turns)
        if transcript_block:
            prompt_parts.append(f"Older Turns to Merge & Summarize:\n{transcript_block}")
        prompt_parts.append(
            "Produce an updated, concise bullet-point working summary capturing "
            "all durable context needed for continuing this session."
        )
        prompt = "\n\n".join(prompt_parts)

        async def _collect() -> str:
            from clea.providers.base import Message

            parts: list[str] = []
            async for chunk in self.provider.stream_turn(  # type: ignore[union-attr]
                messages=[Message(role="user", content=prompt)],
                tools=[],
                system_prompt=WORKING_SUMMARY_SYSTEM_PROMPT,
            ):
                if chunk.type == "text_delta" and chunk.text:
                    parts.append(chunk.text)
            return "".join(parts).strip()

        try:
            summary = await asyncio.wait_for(_collect(), timeout=self.llm_timeout_seconds)
            return summary.strip() or None
        except TimeoutError:
            logger.warning(
                "Working memory LLM compaction timed out after %.1fs; using extractive fallback",
                self.llm_timeout_seconds,
            )
            return None
        except Exception as exc:
            logger.warning(
                "Working memory LLM compaction failed (%s); using extractive fallback", exc
            )
            return None

    async def compact_if_needed(
        self,
        messages: list[Message],
        *,
        keep_recent_turns: int | None = None,
    ) -> tuple[list[Message], str | None]:
        """Compact older turns into a [Session Working Summary] while keeping recent turns."""
        if not self.needs_compaction(messages):
            return list(messages), None

        keep_turns = max(
            1,
            int(keep_recent_turns) if keep_recent_turns is not None else self.keep_recent_turns,
        )
        user_indices = [
            idx
            for idx, msg in enumerate(messages)
            if msg.role == "user" and not self._is_summary_message(msg)
        ]

        if len(user_indices) <= keep_turns:
            return list(messages), None

        split_idx = user_indices[-keep_turns]
        older = messages[:split_idx]
        recent = messages[split_idx:]
        if not older:
            return list(messages), None

        prior_summary = self._extract_prior_summary(older)
        older_turns = [msg for msg in older if not self._is_summary_message(msg)]
        if not older_turns:
            return list(messages), None

        summary = await self._summarize_with_provider(prior_summary, older_turns)
        if not summary:
            summary = self._extractive_fallback(prior_summary, older_turns)

        from clea.providers.base import Message

        summary_msg = Message(
            role="user",
            content=f"{WORKING_SUMMARY_PREFIX}\n{summary}",
        )
        return [summary_msg, *recent], summary
