"""Voice system instruction builder and call memory/transcript integration hooks.

Implements voice call memory loading, chronological transcript persistence into
History, and asynchronous call-end memory consolidation over the typed Clea
daemon WebSocket protocol (preserving voice-process isolation from agent/memory
internals).

All ``safe_*`` wrappers catch and log exceptions (including offline daemon or
custom unimplemented handlers) so voice call transitions back to ``IDLE`` are
never delayed or crashed.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import websockets

from clea.core import auth
from clea.core.protocol import (
    BaseRequest,
    ConversationResumeParams,
    ConversationResumeRequest,
    MemoryEpisodesRequest,
    MemoryGetRequest,
    ResponseFrame,
    VoiceEndCallParams,
    VoiceEndCallRequest,
    VoiceHistoryParams,
    VoiceHistoryRequest,
    parse_frame,
)

logger = logging.getLogger(__name__)

_VOICE_COMPANION_SESSION_ID = "voice_companion"

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
    "- Cross-Chat Context & Tools:\n"
    "  * If an Active Focused Text Chat section is provided in your context, treat it as "
    "read-only awareness of what thread the user currently has open in the UI.\n"
    "  * Use `search_conversations` whenever the user asks about exact commands, code, "
    "decisions, or discussions from other chats or earlier in the Voice Companion log.\n"
    "  * Use `post_to_active_chat` ONLY when the user explicitly asks to send, drop, paste, "
    "write, or save something (such as code, commands, or a summary) into their currently open "
    "chat. Never call `post_to_active_chat` unprompted; if no text chat is focused, offer to "
    "start one with `create_chat` or `send_chat_message`.\n"
    "- Clea Web UI Self-Control Tools:\n"
    "  * `navigate_ui(view, conversation_id?)`: switch between 'chat', 'voice', 'activity', "
    "'memory', and 'settings', or open a saved chat by ID.\n"
    "  * `create_chat(title, content?, open_in_ui?)`: create a new titled conversation "
    "(optionally pre-filled with a summary, code, or plan) and open it right away, even if no "
    "chat was previously focused.\n"
    "  * `send_chat_message(prompt, conversation_id?, title?)`: submit a prompt into a new or "
    "existing chat so Clea's text model streams a full response live on screen.\n"
    "  * `rename_chat(conversation_id, new_title)` and `delete_chat(conversation_id, confirmed)`: "
    "rename or delete a saved chat (`voice_companion` cannot be deleted). CRITICAL: ALWAYS ask "
    "the user for explicit verbal confirmation before calling `delete_chat` with "
    "`confirmed=True`.\n"
    "  * `save_memory_fact(fact, category?)` and `delete_memory_fact(query_or_id)`: save or "
    "remove Durable Facts in long-term Memory.\n"
    "  * `inspect_activity(limit?)`, `cancel_task(task_id)`, and `open_command_palette()`: "
    "check or cancel background Activity tasks and open the UI quick-switcher.\n"
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


def _truncate_title(text: str, limit: int = 60) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _coerce_timestamp(
    raw_ts: Any,
    *,
    base_dt: datetime,
    base_monotonic: float | None,
) -> tuple[datetime, float | None]:
    """Convert raw entry timestamps (ISO string, epoch, or monotonic float) to aware datetime."""
    if isinstance(raw_ts, str) and raw_ts.strip():
        try:
            parsed = datetime.fromisoformat(raw_ts.strip())
            dt = parsed if parsed.tzinfo is not None else parsed.astimezone()
            return dt, base_monotonic
        except ValueError:
            pass
    if isinstance(raw_ts, (int, float)):
        val = float(raw_ts)
        # Unix timestamps since ~2001 are > 1e9; smaller numbers are likely time.monotonic() deltas.
        if val > 1_000_000_000:
            try:
                return datetime.fromtimestamp(val).astimezone(), base_monotonic
            except (OSError, OverflowError, ValueError):
                pass
        else:
            if base_monotonic is None:
                base_monotonic = val
            return base_dt + timedelta(seconds=max(0.0, val - base_monotonic)), base_monotonic
    return datetime.now().astimezone(), base_monotonic


def normalize_voice_transcript(entries: Sequence[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Coalesce streaming deltas, drop blank turns, and normalize chronological timestamps."""
    if not entries:
        return []

    coalesced: list[dict[str, Any]] = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        raw_role = str(raw.get("role", "assistant")).strip().lower()
        role = raw_role if raw_role in ("user", "assistant") else "assistant"
        raw_text = str(raw.get("text", ""))
        is_final = bool(raw["final"]) if "final" in raw else True

        if coalesced and coalesced[-1]["role"] == role and not coalesced[-1]["final"]:
            coalesced[-1]["text"] += raw_text
            coalesced[-1]["final"] = is_final
            if raw.get("timestamp") is not None:
                coalesced[-1]["timestamp"] = raw.get("timestamp")
            continue

        coalesced.append(
            {
                "role": role,
                "text": raw_text,
                "timestamp": raw.get("timestamp"),
                "final": is_final,
            }
        )

    base_dt = datetime.now().astimezone()
    base_mono: float | None = None
    normalized: list[dict[str, str]] = []
    prev_dt: datetime | None = None

    for item in coalesced:
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        dt, base_mono = _coerce_timestamp(
            item.get("timestamp"),
            base_dt=base_dt,
            base_monotonic=base_mono,
        )
        if prev_dt is not None and dt < prev_dt:
            dt = prev_dt
        prev_dt = dt
        normalized.append(
            {
                "role": str(item["role"]),
                "text": text,
                "timestamp": dt.isoformat(),
            }
        )

    return normalized


def build_voice_call_title(
    entries: Sequence[dict[str, Any]],
    *,
    max_len: int = 60,
) -> str:
    """Derive a concise conversation title from the first user turn or call timestamp."""
    normalized = normalize_voice_transcript(entries)
    for entry in normalized:
        if entry["role"] == "user" and entry["text"]:
            return _truncate_title(entry["text"], limit=max_len)

    ref_dt = datetime.now().astimezone()
    if normalized:
        try:
            ref_dt = datetime.fromisoformat(normalized[0]["timestamp"]).astimezone()
        except ValueError:
            pass
    return f"Voice call ({ref_dt.strftime('%H:%M')})"


async def _daemon_rpc(
    req: BaseRequest,
    *,
    daemon_url: str | None = None,
    token: str | None = None,
    data_dir: Path | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Execute a single typed RPC call against the local Clea daemon."""
    resolved_token = token or auth.read_token(auth.token_path(data_dir))
    if not resolved_token:
        raise RuntimeError("No daemon auth token available.")

    resolved_url = daemon_url
    if not resolved_url:
        try:
            lock_info = auth._read_lock_metadata(auth.lock_path(data_dir))
            host = lock_info.get("host")
            port = lock_info.get("port")
            if host and port:
                resolved_url = f"ws://{host}:{port}"
        except Exception:
            resolved_url = None
    if not resolved_url:
        raise RuntimeError("No active daemon lock file / URL available.")

    async with websockets.connect(
        resolved_url,
        subprotocols=auth.client_subprotocols(resolved_token),
        open_timeout=timeout,
        close_timeout=2.0,
    ) as ws:
        await ws.send(req.model_dump_json())
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            frame = parse_frame(raw)
            if isinstance(frame, ResponseFrame) and frame.id == req.id:
                if not frame.ok:
                    raise RuntimeError(frame.error or f"Daemon request {req.method} failed.")
                return dict(frame.result or {})


async def _run_request(
    req: BaseRequest,
    *,
    request_fn: Callable[[BaseRequest], Awaitable[dict[str, Any]]] | None = None,
    daemon_url: str | None = None,
    token: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    if request_fn is not None:
        return await request_fn(req)
    return await _daemon_rpc(
        req,
        daemon_url=daemon_url,
        token=token,
        data_dir=data_dir,
    )


def _accepts_kwarg(fn: Callable[..., Any], kwarg: str) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD or param.name == kwarg:
            return True
    return False


def _filter_supported_kwargs(
    fn: Callable[..., Any],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if v is not None and _accepts_kwarg(fn, k)}


def build_voice_system_instruction(*, memory_context: str | None = None) -> str:
    """Build the live voice system instruction with optional memory context."""
    if memory_context and memory_context.strip():
        return f"{BASE_VOICE_SYSTEM_INSTRUCTION}\n\n## Memory Context\n{memory_context.strip()}"
    return BASE_VOICE_SYSTEM_INSTRUCTION


async def load_voice_memory_context(
    *,
    active_conversation_id: str | None = None,
    daemon_url: str | None = None,
    token: str | None = None,
    data_dir: Path | None = None,
    request_fn: Callable[[BaseRequest], Awaitable[dict[str, Any]]] | None = None,
) -> str:
    """Load long-term memory, episode summaries, voice tail, and active text chat context."""
    mem_result = await _run_request(
        MemoryGetRequest(),
        request_fn=request_fn,
        daemon_url=daemon_url,
        token=token,
        data_dir=data_dir,
    )
    parts: list[str] = []
    content = str(mem_result.get("long_term") or mem_result.get("content") or "").strip()
    if content:
        parts.append(content)

    try:
        ep_result = await _run_request(
            MemoryEpisodesRequest(),
            request_fn=request_fn,
            daemon_url=daemon_url,
            token=token,
            data_dir=data_dir,
        )
        episodes = ep_result.get("episodes") or []
        if isinstance(episodes, list) and episodes:
            ep_lines: list[str] = []
            for ep in episodes[:5]:
                if isinstance(ep, dict):
                    day = str(ep.get("date") or ep.get("day") or "").strip()
                    summary = str(ep.get("summary", "")).strip()
                    if summary:
                        ep_lines.append(f"- ({day}) {summary}" if day else f"- {summary}")
            if ep_lines:
                parts.append("### Recent Episodes\n" + "\n".join(ep_lines))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Skipping optional episode summaries for voice context: %s", exc)

    try:
        vh_result = await _run_request(
            VoiceHistoryRequest(params=VoiceHistoryParams(limit=15)),
            request_fn=request_fn,
            daemon_url=daemon_url,
            token=token,
            data_dir=data_dir,
        )
        messages = vh_result.get("messages") or []
        if isinstance(messages, list) and messages:
            vc_lines: list[str] = ["## Recent Voice Companion History"]
            last_call_id: str | None = None
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                text = str(msg.get("text") or "").strip()
                if not text:
                    continue
                cid = str(msg.get("call_id") or "").strip() or None
                ts = str(msg.get("timestamp") or "").strip()
                time_label = ts[:16] if ts else ""
                if cid and cid != last_call_id:
                    tag = f"{time_label} · call {cid[:8]}" if time_label else f"call {cid[:8]}"
                    vc_lines.append(f"--- [Voice Call {tag}] ---")
                    last_call_id = cid
                speaker = "User" if msg.get("role") == "user" else "Clea"
                prefix = f"[{time_label}] " if time_label else ""
                vc_lines.append(f"- {prefix}**{speaker}:** {text}")
            if len(vc_lines) > 1:
                parts.append("\n".join(vc_lines))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Skipping optional Voice Companion tail for voice context: %s", exc)

    target_chat_id = active_conversation_id
    if target_chat_id and target_chat_id != _VOICE_COMPANION_SESSION_ID:
        try:
            resume_res = await _run_request(
                ConversationResumeRequest(
                    params=ConversationResumeParams(session_id=target_chat_id)
                ),
                request_fn=request_fn,
                daemon_url=daemon_url,
                token=token,
                data_dir=data_dir,
            )
            chat_msgs = resume_res.get("messages") or []
            if isinstance(chat_msgs, list) and chat_msgs:
                chat_title = str(resume_res.get("title") or "Conversation").strip()
                chat_lines = [
                    "## Active Focused Text Chat (Read-Only Context)",
                    f"Active Conversation ID: `{target_chat_id}`",
                    f"Title: {chat_title}",
                    "Recent turns in this open text chat:",
                ]
                for msg in chat_msgs[-10:]:
                    if not isinstance(msg, dict):
                        continue
                    speaker = "User" if msg.get("role") == "user" else "Clea"
                    text = str(msg.get("text") or "").strip()
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
                if len(chat_lines) > 4:
                    parts.append("\n".join(chat_lines))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Skipping active text chat context for voice: %s", exc)

    return "\n\n".join(parts)


async def store_voice_transcript(
    *,
    call_id: str,
    entries: Sequence[dict[str, Any]],
    daemon_url: str | None = None,
    token: str | None = None,
    data_dir: Path | None = None,
    request_fn: Callable[[BaseRequest], Awaitable[dict[str, Any]]] | None = None,
) -> bool:
    """Persist completed non-empty voice call turns chronologically via the daemon.

    Returns ``True`` if a non-empty transcript was persisted, or ``False`` if skipped
    (e.g. empty call with 0 spoken turns).
    """
    normalized = normalize_voice_transcript(entries)
    if not normalized:
        logger.debug("Skipping empty voice call transcript for call %s", call_id)
        return False

    req = VoiceEndCallRequest(
        params=VoiceEndCallParams(
            call_id=call_id,
            reason="transcript_sync",
            transcript=normalized,
        )
    )
    await _run_request(
        req,
        request_fn=request_fn,
        daemon_url=daemon_url,
        token=token,
        data_dir=data_dir,
    )
    return True


async def consolidate_voice_call(
    *,
    call_id: str,
    entries: Sequence[dict[str, Any]] | None = None,
    daemon_url: str | None = None,
    token: str | None = None,
    data_dir: Path | None = None,
    request_fn: Callable[[BaseRequest], Awaitable[dict[str, Any]]] | None = None,
) -> bool:
    """Trigger asynchronous call-end memory consolidation on the Clea daemon."""
    if entries is not None and not normalize_voice_transcript(entries):
        logger.debug("Skipping memory consolidation for empty voice call %s", call_id)
        return False

    req = VoiceEndCallRequest(
        params=VoiceEndCallParams(
            call_id=call_id,
            reason="consolidate",
            transcript=None,
        )
    )
    await _run_request(
        req,
        request_fn=request_fn,
        daemon_url=daemon_url,
        token=token,
        data_dir=data_dir,
    )
    return True


async def safe_load_voice_memory_context(
    *,
    loader: Callable[..., Awaitable[str]] = load_voice_memory_context,
    active_conversation_id: str | None = None,
    daemon_url: str | None = None,
    token: str | None = None,
    data_dir: Path | None = None,
    request_fn: Callable[[BaseRequest], Awaitable[dict[str, Any]]] | None = None,
) -> str | None:
    """Safely load voice memory context, returning None if unimplemented or failing."""
    extra = _filter_supported_kwargs(
        loader,
        {
            "active_conversation_id": active_conversation_id,
            "daemon_url": daemon_url,
            "token": token,
            "data_dir": data_dir,
            "request_fn": request_fn,
        },
    )
    try:
        return await loader(**extra)
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
    memory_loader: Callable[..., Awaitable[str]] | None = load_voice_memory_context,
    active_conversation_id: str | None = None,
    daemon_url: str | None = None,
    token: str | None = None,
    data_dir: Path | None = None,
    request_fn: Callable[[BaseRequest], Awaitable[dict[str, Any]]] | None = None,
) -> str:
    """Resolve the voice system instruction, safely falling back if memory is unready."""
    if memory_context is not None:
        return build_voice_system_instruction(memory_context=memory_context)

    if memory_loader is None:
        return BASE_VOICE_SYSTEM_INSTRUCTION

    context = await safe_load_voice_memory_context(
        loader=memory_loader,
        active_conversation_id=active_conversation_id,
        daemon_url=daemon_url,
        token=token,
        data_dir=data_dir,
        request_fn=request_fn,
    )
    return build_voice_system_instruction(memory_context=context)


async def safe_store_voice_transcript(
    *,
    call_id: str,
    entries: Sequence[dict[str, Any]],
    handler: Callable[..., Awaitable[Any]] = store_voice_transcript,
    daemon_url: str | None = None,
    token: str | None = None,
    data_dir: Path | None = None,
    request_fn: Callable[[BaseRequest], Awaitable[dict[str, Any]]] | None = None,
) -> bool:
    """Persist transcript entries safely without failing or blocking call teardown."""
    extra = _filter_supported_kwargs(
        handler,
        {
            "daemon_url": daemon_url,
            "token": token,
            "data_dir": data_dir,
            "request_fn": request_fn,
        },
    )
    try:
        result = await handler(call_id=call_id, entries=entries, **extra)
        return True if result is None else bool(result)
    except NotImplementedError as exc:
        logger.warning(
            "Voice transcript hook not implemented for call %s; skipping persistence: %s",
            call_id,
            exc,
        )
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed storing voice transcript for call %s; skipping persistence: %s",
            call_id,
            exc,
        )
        return False


async def safe_consolidate_voice_call(
    *,
    call_id: str,
    entries: Sequence[dict[str, Any]] | None = None,
    handler: Callable[..., Awaitable[Any]] = consolidate_voice_call,
    daemon_url: str | None = None,
    token: str | None = None,
    data_dir: Path | None = None,
    request_fn: Callable[[BaseRequest], Awaitable[dict[str, Any]]] | None = None,
) -> bool:
    """Trigger call-end consolidation safely without failing or blocking call teardown."""
    extra = _filter_supported_kwargs(
        handler,
        {
            "entries": entries,
            "daemon_url": daemon_url,
            "token": token,
            "data_dir": data_dir,
            "request_fn": request_fn,
        },
    )
    try:
        result = await handler(call_id=call_id, **extra)
        return True if result is None else bool(result)
    except NotImplementedError as exc:
        logger.warning(
            "Voice consolidation hook not implemented for call %s; skipping: %s",
            call_id,
            exc,
        )
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed consolidating voice call %s; skipping consolidation: %s",
            call_id,
            exc,
        )
        return False
