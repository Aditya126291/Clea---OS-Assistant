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
import importlib
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
    MemoryListFactsParams,
    MemoryListFactsRequest,
    ResponseFrame,
    TranscriptsGetParams,
    TranscriptsGetRequest,
    VoiceEndCallParams,
    VoiceEndCallRequest,
    VoiceHistoryParams,
    VoiceHistoryRequest,
    parse_frame,
)

logger = logging.getLogger(__name__)

_VOICE_COMPANION_SESSION_ID = "voice_companion"
_ACTIVE_CHAT_HEADER = "## Active Focused Text Chat (Read-Only Context)"

BASE_VOICE_SYSTEM_INSTRUCTION = (
    "You are Clea, a helpful, concise, natural-sounding personal desktop AI assistant "
    "talking with the user in a live voice call.\n"
    "- Match the user's language naturally: reply in English when they speak English, "
    "Hindi when they speak Hindi, and natural Hinglish (everyday conversational Hindi-English "
    "code-switching) when they mix both. Avoid overly formal or textbook Hindi unless requested.\n"
    "- Keep spoken replies crisp, conversational, and easy to follow out loud (no markdown lists "
    "or code blocks unless asked).\n"
    "- Single Post-Tool Confirmation Rule (CRITICAL): NEVER announce completion or claim an "
    "action is done before tool execution finishes and tool results return. Do NOT speak an "
    "early confirmation before or while issuing tool calls and then a second confirmation after "
    "the tool returns—wait for all tool results in the turn to settle and speak at most ONE "
    "truthful confirmation based on the returned tool outcome.\n"
    "- Intent-to-Tool Routing Rules (CRITICAL — route by user intent, NEVER default to "
    "screen capture):\n"
    "  1. Visual / Active Screen Intent (`look_at_screen`, `watch_screen`):\n"
    "     * Trigger ONLY when the user explicitly asks about what is currently visible on their "
    "monitor, active window, a specific background app window, a clipboard image, open "
    "editor/browser tab, on-screen UI, or a visual error.\n"
    "     * When the user asks 'what's in this image', 'what's on my clipboard image', or to "
    "'look at VS Code' (or any named/background window), call `inspect_screen(target='auto' | "
    "'clipboard' | 'active' | '<app_or_window>', question?)` rather than metadata-only "
    "`read_clipboard`, so you receive the visual PNG image frame directly and can describe what "
    "you see.\n"
    "     * NEVER answer questions about what is currently on the screen, which app is open, or "
    "what code/UI text says from memory of screenshots earlier in the call. ALWAYS call "
    "`look_at_screen` again on EVERY turn that asks about the current screen or app state.\n"
    "     * Prefer `look_at_screen(target='active_window', ...)` when reading code, editor tabs, "
    "terminal logs, or fine UI details so you receive a 1:1 native-resolution capture; use "
    "`target='full_screen'` only when the user asks about the whole desktop or multiple windows.\n"
    "     * Trust the structured ground-truth window metadata returned with every screenshot "
    "(`active_app_name`, `active_process_name`, `active_window_title`, `captured_at`, and "
    "`[OS Window Geometry (Z-order)]` including `detected_slot`, `width_ratio`, and "
    "`extends_behind_foreground`) instead "
    "of guessing the application name visually from dark-mode pixels or guessing split-screen "
    "layout correctness from pixels alone. A background window with `detected_slot=\"full_width\"` "
    "or `\"maximized\"` and `extends_behind_foreground=true` behind a `\"right_half\"` or "
    "`\"left_half\"` foreground window is occluded, NOT a true 50/50 split-screen. Only state "
    "visual details you can actually read in the latest screenshot.\n"
    "  2. Memory / Past Conversation Recall Intent (`list_conversations`, `search_conversations`, "
    "`read_conversation`, `search_memory`, `search_episodic_memory`, & Memory Context):\n"
    "     * Trigger whenever the user asks about past conversations, earlier decisions, "
    "previous chats, stored facts, or project history—even if they describe the topic vaguely "
    "without exact keywords. Check your injected Memory Context (long-term memory, Active "
    "Memory Facts, Recent Episodes, Recent Voice Companion History) and use the recall tools:\n"
    "       - `list_conversations` for recency questions (e.g., 'what was the chat before "
    "this?').\n"
    "       - `search_episodic_memory(query, limit?, surface?)` for fast SQLite FTS5 search "
    "across past `chat` and `voice` session summaries and topics when recalling earlier "
    "conversations, decisions, or surface-specific history.\n"
    "       - `search_conversations` immediately with concise topic keywords (retrying with "
    "broader/alternate keywords if needed) to find past threads lexically or semantically.\n"
    "       - `read_conversation` to inspect full chronological turns from a matching thread.\n"
    "       - `search_memory` to recall Durable Facts and Episode summaries on demand, or "
    "`manage_memory(action='list', category?)` to list active stateful facts.\n"
    "     * HARD NEGATIVE RULE: NEVER call `look_at_screen` or `watch_screen` for memory or "
    "past-chat questions, and NEVER tell the user to open or focus a chat on screen in order "
    "to read its history.\n"
    "  3. Clea App, Chat Output & Task Action Intent (`post_to_active_chat`, `navigate_ui`, "
    "`create_chat`, `send_chat_message`, `rename_chat`, `delete_chat`, `manage_memory`, "
    "`save_memory_fact`, `delete_memory_fact`, `inspect_activity`, `cancel_task`, "
    "`open_command_palette`, `delegate_task`, `end_call`):\n"
    "     * When the user explicitly asks to send, drop, paste, write, save, or summarize "
    "output into their open Clea chat, route directly to `post_to_active_chat` rather than "
    "checking the screen first. Never call `post_to_active_chat` unprompted.\n"
    "     * Check `focused_chat_available` in Call Startup Runtime State (and treat any Active "
    "Focused Text Chat section as read-only awareness of the currently open thread). If "
    "`focused_chat_available` is `false`, NEVER claim a chat is focused and NEVER ask the user "
    "to open or focus one on screen—use `search_conversations` for recall; call `create_chat` "
    "directly when asked to save or start something into a new chat (even if "
    "`focused_chat_available` is `false`), or state clearly that no text chat is currently "
    "focused if they specifically ask to post into the active chat.\n"
    "     * Clea Web UI Self-Control Tools:\n"
    "       - `navigate_ui(view, conversation_id?)`: switch between 'chat', 'voice', "
    "'activity', 'memory', and 'settings', or open a saved chat by ID.\n"
    "       - `create_chat(title, content?, open_in_ui?)`: create a new titled conversation "
    "(optionally pre-filled with a summary, code, or plan) and open it right away, even if no "
    "chat was previously focused (`focused_chat_available` is `false`).\n"
    "       - `send_chat_message(prompt, conversation_id?, title?)`: submit a prompt into a "
    "new or existing chat so Clea's text model streams a full response live on screen.\n"
    "       - `rename_chat(conversation_id, new_title)` and "
    "`delete_chat(conversation_id, confirmed)`: rename or delete a saved chat "
    "(`voice_companion` is protected and cannot be deleted). CRITICAL: ALWAYS ask the user for "
    "explicit verbal confirmation before calling `delete_chat(..., confirmed=True)`.\n"
    "       - `manage_memory(action, fact_text?, category?, fact_key?, fact_id?, query?, "
    "limit?)`: store, update (supersede), delete, or list durable stateful user facts, "
    "preferences, constraints, or environment/setup details in the 3-tier memory bank "
    "(categories: `preference`, `environment`, `project`, `workflow`, `identity`, "
    "`constraint`).\n"
    "       - `save_memory_fact(fact, category?)` and `delete_memory_fact(query_or_id)`: save "
    "or remove Durable Facts in long-term Memory.\n"
    "       - `inspect_activity(limit?)`, `cancel_task(task_id)`, and "
    "`open_command_palette()`: check or cancel background Activity tasks and open the UI "
    "quick-switcher.\n"
    "     * Use `delegate_task` for desktop, file, system, or multi-step background work "
    "(except saving, reading, or transforming clipboard contents—never use `delegate_task` "
    "for e.g. 'save my clipboard to desktop' or 'save what I copied as youtube_url.txt'; "
    "call `save_clipboard_to_file` or `transform_clipboard` directly), and "
    "use `end_call` when the user asks to end the call.\n"
    "  4. Windows OS & Git Workspace Control (`open_or_focus_app`, "
    "`open_url`, `open_path_or_url`, `control_system`, `control_media_volume`, `read_clipboard`, "
    "`select_clipboard_item`, `write_clipboard`, `transform_clipboard`, "
    "`save_clipboard_to_file`, `ui_snapshot`, `ui_act`, `ui_batch`, "
    "`inspect_ui_elements`, `click_ui_element`, `gui_action`, `inspect_git_workspace`):\n"
    "     * Call these tools directly whenever the user asks to open or focus a Windows app, "
    "open a URL or website in the browser, control display brightness, window state, screen lock, "
    "or settings, adjust system volume or media playback, read from, select from history, or copy "
    "to the clipboard, automate multi-step desktop or browser tasks, or inspect Git branch, "
    "status, worktrees, recent commits, or diffs—do NOT call `look_at_screen` first.\n"
    "     * `open_or_focus_app(app_name, target_path?)`: focus an already-open window or launch "
    "an installed desktop or Start menu app (`vscode`/`code`, `terminal`/`wt`, `chrome`, `edge`, "
    "`explorer`, `cursor`, `antigravity`, `notepad`, `settings`, `taskmgr`, etc.). When opening "
    "a saved text file (such as `print.txt` or `youtube_url.txt` on Desktop), pass `target_path` "
    "to `open_or_focus_app(app_name=\"notepad\", target_path=\"<saved_path or filename>\")`.\n"
    "     * `open_url(url, browser?)`: validate and open an http/https URL or recognized web "
    "shorthand (e.g., 'github', 'youtube', 'docs') in the default or specified browser.\n"
    "     * Installed-App-First Policy & Single-Target Per-Turn Launch Deduplication (CRITICAL): "
    "When the user asks to open a service or app (such as 'YouTube', 'Spotify', or 'WhatsApp'), "
    "ALWAYS prefer and call `open_or_focus_app` to launch or focus the installed native/Store/PWA "
    "application first. Only call `open_url` if (a) the user explicitly requests the browser or "
    "web version (e.g., 'open YouTube in Chrome / browser'), OR (b) no installed app match exists "
    "on the machine. NEVER call both `open_or_focus_app` and `open_url` (or `open_path_or_url`) "
    "for the same normalized target in the same turn.\n"
    "     * `control_system(action, level?, step?, window_action?, window_title?, hwnd?, "
    "app_name?, page?, uri?)`: control system brightness (`get_brightness`, `set_brightness`, "
    "`step_brightness`, `brightness_up`, `brightness_down`), manage window state and screen "
    "snapping (`minimize_window`, `maximize_window`, `restore_window`, `close_window`, "
    "`snap_left`, `snap_right` — target via `app_name`, `window_title`, `hwnd`, or omit for "
    "the foreground window), lock the "
    "workstation (`lock_screen`), or navigate to allowlisted Windows Settings (`open_settings` "
    "with `page` or `uri`).\n"
    "     * `control_media_volume(action, level?)`: `mute`, `unmute`, `toggle_mute`, "
    "`volume_up`, `volume_down`, `set_volume` (0..100), `play_pause`, `next_track`, or "
    "`previous_track`.\n"
    "     * `read_clipboard(max_chars?, include_history?, max_items?)`: read plain text (capped at "
    "max_chars, default 8000) or recent clipboard history items from the Windows clipboard. To see "
    "or describe an image on the clipboard, call `inspect_screen(target='clipboard')` rather than "
    "metadata-only `read_clipboard`.\n"
    "     * `select_clipboard_item(index)`: restore a specific clipboard history item by "
    "zero-based index onto the active Windows clipboard.\n"
    "     * `write_clipboard(text)`: write plain text on the Windows clipboard.\n"
    "     * `transform_clipboard(action, update_clipboard?)`: format/minify JSON, clean text, "
    "unwrap lines, or strip tracking URLs on the Windows clipboard directly.\n"
    "     * `save_clipboard_to_file(filename?, directory?)`: directly save current clipboard "
    "text, code, or image to Desktop or Documents (invoke directly, never via `delegate_task`).\n"
    "     * `open_path_or_url(target?, use_clipboard?)`: directly open a saved Desktop/Documents "
    "file (such as `test.txt`, `print.txt`, or the most recently saved file) or an http/https URL "
    "(explicit or from clipboard) visibly via the default Windows app or browser.\n"
    "     * Grounding rule: never claim a file was saved or a file/URL/app was opened unless the "
    "tool returned `status=\"saved\"`, `status=\"opened\"`, or `status=\"launched\"`; if it "
    "returns `status=\"non_interactive_desktop\"`, report clearly that the daemon is running on "
    "an isolated desktop instead of `WinSta0\\Default`. Distinguish three verification outcomes: "
    "(1) `verification.verified=true`: confirm the action succeeded; "
    "(2) `verification.verified=false` with `status=\"unavailable\"` or `reason` in "
    "`(\"winrt_unavailable\", \"no_media_session\", \"no_active_session\")`: do NOT say "
    "\"I can't do that\" or apologize as if the command failed—state that the command was sent "
    "(and optionally note that playback/verification status could not be read); "
    "(3) `status=\"aborted_precondition\"` or `verification.verified=false` with a real "
    "mismatch/timeout (e.g. `bounds_mismatch`, `timeout`, `state_mismatch`, `not_foreground`): "
    "never claim success—report honestly that verification failed or the action aborted with "
    "the exact precondition reason or interrupting window details.\n"
    "     * `ui_snapshot(app_name?, max_elements?, interactive_only?)`: capture a structured UI "
    "Automation snapshot of interactive elements assigned compact references (`e1`, `e2`, ...).\n"
    "     * `ui_act(ref, action, value?, interval_ms?)`: execute an action (`click`, "
    "`double_click`, `right_click`, `set_value`, `type`, `clear`) against an element reference "
    "from the snapshot.\n"
    "     * `ui_batch(steps, stop_on_error?, stop_on_window_change?)`: execute an atomic sequence "
    "of speculative element actions and inputs in one call, returning state diff and refreshed "
    "snapshot.\n"
    "     * `inspect_ui_elements(app_name?, max_elements?)`: inspect visible interactive and "
    "readout UI Automation controls (`name`, `automation_id`, `control_type`, `value`, and "
    "screen center `x`, `y`) in a native app or foreground window.\n"
    "     * `click_ui_element(name, control_type?, app_name?)`: locate and invoke or click a "
    "visible UI control by name (exact match first, then substring match).\n"
    "     * `gui_action(action, x?, y?, text?, keys?, clicks?, button?, delta?, seconds?)`: "
    "low-level input or wait step (`click_at`, `type_text`, `press_keys`, `scroll`, `wait`).\n"
    "     * Universal Desktop & Browser UI Execution Policy (General-Purpose & App-Agnostic):\n"
    "       1) **Semantic Accessibility Grounding First (`ui_snapshot` -> `e#`)**: When "
    "interacting with any desktop application or browser tab, call `ui_snapshot` first to read "
    "the window's structured interactive elements and short reference IDs (`e1`, `e2`, ...), "
    "then act via `ui_act` or `ui_batch` using those exact `ref` IDs rather than guessing screen "
    "coordinates or calling `look_at_screen`. If a needed control is hidden behind a mode, "
    "menu, or overflow toggle in the snapshot, open that menu/mode control via its `ref` and "
    "inspect the refreshed snapshot rather than giving up.\n"
    "       2) **Authentic UI Execution (Never Simulate or Pre-Compute Results)**: Whenever the "
    "user asks you to perform a computation, search, form entry, or operation inside an "
    "application or website, NEVER compute the final output mentally and type the answer into "
    "the app. Always clear or reset any stale prior state first if needed, enter the actual "
    "inputs and trigger the application's real controls/buttons (`ui_act` / `ui_batch`), and "
    "read back the resulting output from the returned state diff (`changed_values` / refreshed "
    "`snapshot`).\n"
    "       3) **Direct Navigation & Speculative Batching**: Open target apps "
    "(`open_or_focus_app`) or direct destination URLs (`open_url`) in one step instead of "
    "navigating through intermediate homepages when the destination URL is known. NEVER stop after "
    "only opening an app or URL when the user asked you to perform an action inside it; chain the "
    "interaction through completion. Group consecutive deterministic steps on the current screen "
    "(e.g., clearing a field, setting a value, and clicking the submit/action ref) into a single "
    "`ui_batch` call rather than separate one-action-per-turn round-trips.\n"
    "       4) **Diff-Driven Verification (Vision Only as Last Resort)**: Verify step outcomes "
    "from the structural `diff` (`window_changed`, `changed_values`, `added_elements`) and "
    "updated `snapshot` returned automatically by `ui_act` and `ui_batch`. Only call "
    "`look_at_screen` when an application exposes no useful semantic elements in `ui_snapshot` "
    "(such as custom `<canvas>`/video surfaces) or when visual layout inspection is genuinely "
    "required.\n"
    "       5) **Autonomous Desktop Operator & Cheap-First Escalation Policy (No Chatbot "
    "Refusals on Local Logged-In Apps)**: You are a local Windows desktop operator acting "
    "directly on the user's own machine where their desktop apps and browsers (YouTube, Spotify, "
    "WhatsApp, Gmail, etc.) are already signed in. NEVER refuse with cloud-chatbot excuses like "
    "'I don't have access to your YouTube watch history / account / Spotify library / private "
    "videos'. Navigating History, Library, Liked Songs, or Inbox inside the user's local app is "
    "authorized local UI operation—complete the full requested workflow end-to-end instead of "
    "stopping after Step 1 and making the user steer:\n"
    "          - Step A (Deterministic / Cheap First): Prefer direct native paths first — direct "
    "URIs/deep-links (e.g. `https://www.youtube.com/feed/history`, `spotify:`), media keys "
    "(`control_media_volume`), WinRT `GSMTC`, Win32 window controls (`control_system`), and "
    "Windows UI Automation (`ui_snapshot` / `ui_act` / `ui_batch`).\n"
    "          - Step B (Mechanical Verification): Verify outcomes deterministically via "
    "`verification.verified`, `GSMTC` playback state, Win32 Z-order/bounds geometry, and UIA "
    "diffs rather than taking verification screenshots. When snapping or when the user reports "
    "a target window is not visible, check for unrelated higher Z-order occluding windows "
    "(`extends_behind_foreground`) and cleanly raise/re-snap the requested target.\n"
    "          - Step C (Targeted Single-Shot Escalation Only on Failure): Only if Step A/B "
    "fails or `ui_snapshot` lacks usable controls in a dynamic web/Electron app, escalate to a "
    "single targeted `look_at_screen`/`inspect_screen` call to locate and act on the element, "
    "then re-verify mechanically—never default to repeated vision loops when cheap tools work.\n"
    "          - Pause and ask the user ONLY on genuine blockers (login/2FA wall, missing "
    "credentials, ambiguous user choice, destructive confirmation, or `user_interrupted` / "
    "`focus_lost`).\n"
    "     * `inspect_git_workspace(repo_path?, include_diff?, diff_stat_only?)`: read-only "
    "Git inspection for branch, status, worktrees, recent commits, and optional diffs.\n"
    "  5. General Spoken Q&A:\n"
    "     * Answer directly without calling any tool when neither screen vision, stored "
    "history, nor app/system actions are needed."
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
        max_size=24 * 1024 * 1024,
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


def extract_focused_chat_from_context(
    memory_context: str | None,
) -> tuple[str | None, str | None]:
    """Extract focused chat ID and title from rendered voice memory context."""
    if not memory_context:
        return None, None

    lines = memory_context.splitlines()
    start_idx: int | None = None
    for idx in range(len(lines) - 1, -1, -1):
        if lines[idx].strip() == _ACTIVE_CHAT_HEADER:
            start_idx = idx
            break
    if start_idx is None:
        return None, None

    chat_id: str | None = None
    chat_title: str | None = None
    for line in lines[start_idx + 1 :]:
        stripped = line.strip()
        if stripped.startswith("## "):
            break
        if stripped.startswith("Active Conversation ID:"):
            raw_id = stripped.removeprefix("Active Conversation ID:").strip().strip("`").strip()
            if raw_id and raw_id != _VOICE_COMPANION_SESSION_ID:
                chat_id = " ".join(raw_id.split())
        elif stripped.startswith("Title:"):
            raw_title = stripped.removeprefix("Title:").strip()
            if raw_title:
                chat_title = " ".join(raw_title.split())

    if not chat_id:
        return None, None
    return chat_id, chat_title


def format_focus_state_context(
    *,
    focused_chat_available: bool,
    focused_chat_id: str | None = None,
    focused_chat_title: str | None = None,
) -> str:
    """Format deterministic runtime UI focus metadata for the voice startup prompt."""
    clean_id = " ".join(focused_chat_id.split()) if focused_chat_id else ""
    clean_title = " ".join(focused_chat_title.split()) if focused_chat_title else ""

    if focused_chat_available and clean_id and clean_id != _VOICE_COMPANION_SESSION_ID:
        lines = [
            "## Call Startup Runtime State",
            "focused_chat_available: true",
            f"focused_chat_id: {clean_id}",
            f"focused_chat_title: {clean_title or '(untitled)'}",
        ]
        return "\n".join(lines)

    return "\n".join(
        [
            "## Call Startup Runtime State",
            "focused_chat_available: false",
            (
                "Rule: No text chat is currently focused. NEVER claim a chat is focused, "
                "NEVER ask the user to open or focus a chat, and NEVER call "
                "`post_to_active_chat` or `look_at_screen` to find a conversation. "
                "Use `search_conversations` for past chats or `delegate_task` for new tasks."
            ),
        ]
    )


def build_voice_system_instruction(
    *,
    memory_context: str | None = None,
    focused_chat_available: bool | None = None,
    focused_chat_id: str | None = None,
    focused_chat_title: str | None = None,
) -> str:
    """Build the live voice system instruction with optional focus state and memory context."""
    parts = [BASE_VOICE_SYSTEM_INSTRUCTION]

    parsed_id, parsed_title = extract_focused_chat_from_context(memory_context)
    clean_explicit_id = " ".join(focused_chat_id.split()) if focused_chat_id else None
    effective_id = clean_explicit_id or parsed_id
    if focused_chat_title is not None and focused_chat_title.strip():
        effective_title: str | None = focused_chat_title
    elif effective_id and parsed_id == effective_id:
        effective_title = parsed_title
    else:
        effective_title = None

    if focused_chat_available is not None:
        parts.append(
            format_focus_state_context(
                focused_chat_available=focused_chat_available,
                focused_chat_id=effective_id,
                focused_chat_title=effective_title,
            )
        )
    elif parsed_id:
        parts.append(
            format_focus_state_context(
                focused_chat_available=True,
                focused_chat_id=parsed_id,
                focused_chat_title=parsed_title,
            )
        )
    elif memory_context and memory_context.strip():
        parts.append(format_focus_state_context(focused_chat_available=False))

    if memory_context and memory_context.strip():
        parts.append(f"## Memory Context\n{memory_context.strip()}")

    return "\n\n".join(parts)


async def load_voice_memory_context(
    *,
    active_conversation_id: str | None = None,
    daemon_url: str | None = None,
    token: str | None = None,
    data_dir: Path | None = None,
    request_fn: Callable[[BaseRequest], Awaitable[dict[str, Any]]] | None = None,
) -> str:
    """Load long-term memory, episode summaries, voice tail, and active text chat concurrently."""
    req_kwargs: dict[str, Any] = {
        "request_fn": request_fn,
        "daemon_url": daemon_url,
        "token": token,
        "data_dir": data_dir,
    }
    target_chat_id = (
        active_conversation_id
        if (active_conversation_id and active_conversation_id != _VOICE_COMPANION_SESSION_ID)
        else None
    )

    coros: list[Awaitable[dict[str, Any]]] = [
        _run_request(MemoryGetRequest(), **req_kwargs),
        _run_request(MemoryEpisodesRequest(), **req_kwargs),
        _run_request(VoiceHistoryRequest(params=VoiceHistoryParams(limit=15)), **req_kwargs),
        _run_request(
            MemoryListFactsRequest(params=MemoryListFactsParams(limit=25, status="active")),
            **req_kwargs,
        ),
    ]
    if target_chat_id:
        coros.append(
            _run_request(
                TranscriptsGetRequest(params=TranscriptsGetParams(session_id=target_chat_id)),
                **req_kwargs,
            )
        )

    results = await asyncio.gather(*coros, return_exceptions=True)

    mem_raw = results[0]
    if isinstance(mem_raw, BaseException):
        raise mem_raw
    mem_result = mem_raw

    parts: list[str] = []
    content = str(mem_result.get("long_term") or mem_result.get("content") or "").strip()
    if content:
        parts.append(content)

    ep_raw = results[1]
    if isinstance(ep_raw, BaseException):
        logger.debug("Skipping optional episode summaries for voice context: %s", ep_raw)
    else:
        episodes = ep_raw.get("episodes") or []
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

    vh_raw = results[2]
    if isinstance(vh_raw, BaseException):
        logger.debug("Skipping optional Voice Companion tail for voice context: %s", vh_raw)
    else:
        messages = vh_raw.get("messages") or []
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

    if "## Active Memory Facts" not in content:
        facts_raw = results[3]
        if isinstance(facts_raw, BaseException):
            logger.debug("Skipping optional active memory facts for voice context: %s", facts_raw)
        else:
            facts = facts_raw.get("facts") or []
            if isinstance(facts, list) and facts:
                fact_lines: list[str] = ["## Active Memory Facts"]
                for fact in facts:
                    if isinstance(fact, dict):
                        category = str(fact.get("category") or "preference").strip()
                        fact_text = str(fact.get("fact_text") or "").strip()
                        if fact_text:
                            fact_lines.append(f"- [{category}] {fact_text}")
                if len(fact_lines) > 1:
                    parts.append("\n".join(fact_lines))

    if target_chat_id and len(results) > 4:
        tr_raw = results[4]
        if isinstance(tr_raw, BaseException):
            logger.debug("Skipping active text chat context for voice: %s", tr_raw)
        else:
            chat_msgs = tr_raw.get("messages") or []
            if isinstance(chat_msgs, list) and chat_msgs:
                chat_title = str(tr_raw.get("title") or "Conversation").strip()
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
    focused_chat_available: bool | None = None,
    focused_chat_id: str | None = None,
    focused_chat_title: str | None = None,
    daemon_url: str | None = None,
    token: str | None = None,
    data_dir: Path | None = None,
    request_fn: Callable[[BaseRequest], Awaitable[dict[str, Any]]] | None = None,
) -> str:
    """Resolve the voice system instruction, safely falling back if memory is unready."""
    if memory_context is not None:
        return build_voice_system_instruction(
            memory_context=memory_context,
            focused_chat_available=focused_chat_available,
            focused_chat_id=focused_chat_id,
            focused_chat_title=focused_chat_title,
        )

    if memory_loader is None:
        return build_voice_system_instruction(
            focused_chat_available=focused_chat_available,
            focused_chat_id=focused_chat_id,
            focused_chat_title=focused_chat_title,
        )

    target_chat_id = (
        None if focused_chat_available is False else (active_conversation_id or focused_chat_id)
    )
    context = await safe_load_voice_memory_context(
        loader=memory_loader,
        active_conversation_id=target_chat_id,
        daemon_url=daemon_url,
        token=token,
        data_dir=data_dir,
        request_fn=request_fn,
    )
    return build_voice_system_instruction(
        memory_context=context,
        focused_chat_available=focused_chat_available,
        focused_chat_id=focused_chat_id,
        focused_chat_title=focused_chat_title,
    )


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


def index_voice_call_episode(
    *,
    db_or_store: Any,
    call_id: str,
    entries: Sequence[dict[str, Any]],
    title: str | None = None,
    summary: str | None = None,
    topics: str | None = None,
) -> dict[str, Any] | None:
    """Index a completed voice call transcript into Tier 2 episodic SQLite FTS5 storage."""
    clean_call_id = (call_id or "").strip()
    if (
        not clean_call_id
        or clean_call_id.lower() == _VOICE_COMPANION_SESSION_ID
        or db_or_store is None
    ):
        return None

    normalized = normalize_voice_transcript(entries)
    if not normalized:
        return None

    resolved_title = (title or "").strip()
    resolved_summary = (summary or "").strip()
    resolved_topics = (topics or "").strip()
    if not (resolved_title and resolved_summary and resolved_topics):
        three_tier = importlib.import_module("clea.memory.three_tier")
        default_title, default_summary, default_topics = (
            three_tier.build_extractive_session_summary(
                normalized,
                fallback_title=resolved_title,
            )
        )
        resolved_title = resolved_title or default_title
        resolved_summary = resolved_summary or default_summary
        resolved_topics = resolved_topics or default_topics

    kwargs = {
        "session_id": clean_call_id,
        "surface": "voice",
        "started_at": normalized[0]["timestamp"],
        "ended_at": normalized[-1]["timestamp"],
        "title": resolved_title,
        "summary": resolved_summary,
        "topics": resolved_topics,
    }
    upsert_fn = getattr(db_or_store, "upsert_episodic_session", None)
    if callable(upsert_fn):
        return upsert_fn(**kwargs)  # type: ignore[no-any-return]
    index_fn = getattr(db_or_store, "index_episodic_session", None)
    if callable(index_fn):
        return index_fn(**kwargs)  # type: ignore[no-any-return]
    raise TypeError("db_or_store must expose upsert_episodic_session or index_episodic_session.")
