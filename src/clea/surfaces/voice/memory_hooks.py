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
    MemoryEpisodesRequest,
    MemoryGetRequest,
    ResponseFrame,
    VoiceEndCallParams,
    VoiceEndCallRequest,
    parse_frame,
)

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
    daemon_url: str | None = None,
    token: str | None = None,
    data_dir: Path | None = None,
    request_fn: Callable[[BaseRequest], Awaitable[dict[str, Any]]] | None = None,
) -> str:
    """Load long-term memory and recent episode summaries from the Clea daemon."""
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
    daemon_url: str | None = None,
    token: str | None = None,
    data_dir: Path | None = None,
    request_fn: Callable[[BaseRequest], Awaitable[dict[str, Any]]] | None = None,
) -> str | None:
    """Safely load voice memory context, returning None if unimplemented or failing."""
    extra = _filter_supported_kwargs(
        loader,
        {
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
