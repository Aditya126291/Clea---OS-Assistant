"""Vendor-agnostic live voice backend interface and session/event models.

No Gemini-specific SDK types may be imported or referenced here. Concrete backends
(such as ``GeminiLiveBackend`` in ``voice/backends/gemini_live.py``) translate
these models to/from vendor-specific wire formats internally.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from clea.surfaces.voice.config import MediaResolution

INPUT_AUDIO_SAMPLE_RATE_HZ = 16_000
OUTPUT_AUDIO_SAMPLE_RATE_HZ = 24_000
AUDIO_CHANNELS = 1
AUDIO_SAMPLE_WIDTH_BYTES = 2

ToolBehavior = Literal["blocking", "non_blocking"]
ToolResponseScheduling = Literal["interrupt", "when_idle", "silent"]
TranscriptRole = Literal["user", "assistant"]
ScreenCaptureTarget = Literal["active_window", "full_screen"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VoiceToolDeclaration(_StrictModel):
    """Provider-agnostic function declaration for a live voice tool."""

    name: str
    description: str
    parameters_schema: dict[str, Any] = Field(default_factory=dict)
    behavior: ToolBehavior = "non_blocking"


def default_voice_tools() -> list[VoiceToolDeclaration]:
    """Return the canonical live voice tool declarations."""
    return [
        VoiceToolDeclaration(
            name="delegate_task",
            description=(
                "Delegate a desktop, file, system, or multi-step task to Clea's background "
                "agent loop. Runs asynchronously while conversation continues."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Clear natural-language description of the task to run.",
                    }
                },
                "required": ["description"],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="look_at_screen",
            description=(
                "Capture a fresh live screenshot RIGHT NOW along with ground-truth active "
                "application name, process executable, and window title metadata. Only use when "
                "the user asks about what is currently visible on their screen. Do NOT call when "
                "the user asks about past conversations, memory, stored facts, or Clea app "
                "actions. Always call this again on every visual question about the screen, "
                "active app, code, errors, or UI rather than relying on earlier screenshots. Use "
                "target='active_window' (1:1 native resolution) whenever reading code, tabs, "
                "menus, or small UI text, and target='full_screen' when whole-monitor context "
                "is needed."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "enum": ["active_window", "full_screen"],
                        "description": (
                            "Capture only the active foreground window at 1:1 native resolution "
                            "(best for reading code/text/UI) or the active display monitor."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": "Brief explanation of why fresh screen context is needed.",
                    },
                    "monitor": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Optional 1-based monitor index when target is full_screen.",
                    },
                },
                "required": ["target", "reason"],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="watch_screen",
            description=(
                "Stream fresh periodic screen frames for a bounded duration while guiding the "
                "user live through an on-screen workflow. Only use when the user asks about what "
                "is currently visible on their screen. Do NOT call when the user asks about past "
                "conversations, memory, stored facts, or Clea app actions."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "number",
                        "minimum": 1,
                        "description": "How many seconds to watch the screen (capped by config).",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Brief explanation of why continuous viewing is needed.",
                    },
                    "target": {
                        "type": "string",
                        "enum": ["active_window", "full_screen"],
                        "description": (
                            "Optional target ('active_window' or 'full_screen', defaults to "
                            "'full_screen')."
                        ),
                    },
                },
                "required": ["seconds", "reason"],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="list_conversations",
            description=(
                "List the most recent text conversations newest-first with title, updated "
                "timestamp, turn count, and Episode summary if available. Use this for recency "
                "questions such as 'what was the chat before this?'."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Maximum number of recent chats to return (default 10).",
                    },
                },
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="search_conversations",
            description=(
                "Run 3-layer hybrid (lexical + semantic Episode/Fact) search across ALL saved "
                "text conversations and the continuous Voice Companion history. Use this to "
                "locate past discussions by topic, concept, code snippets, commands, or errors. "
                "Call this immediately whenever the user asks about past discussions, earlier "
                "decisions, previous chats, stored facts, or project history—even without exact "
                "keywords. Never call `look_at_screen` or ask the user to focus a chat first to "
                "read history."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Descriptive query, keywords, or concept to search.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Maximum number of matching conversations (default 8).",
                    },
                },
                "required": ["query"],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="read_conversation",
            description=(
                "Read full chronological message turns and Episode summary for a specific "
                "conversation_id returned by list_conversations or search_conversations so you "
                "can pull exact code, commands, or architectural decisions."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "conversation_id": {
                        "type": "string",
                        "description": "The conversation_id / session_id to read.",
                    },
                    "max_turns": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "description": (
                            "Maximum chronological message turns to return (default 40)."
                        ),
                    },
                },
                "required": ["conversation_id"],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="search_memory",
            description=(
                "Run hybrid semantic + token-ranked search directly across all stored Durable "
                "Facts and Episode summaries on demand during the voice call."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Topic, preference, fact, or concept to search in memory.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Maximum number of memory matches to return (default 10).",
                    },
                },
                "required": ["query"],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="post_to_active_chat",
            description=(
                "Write a clean assistant message (such as code, shell commands, or a structured "
                "summary) directly into the user's currently focused text chat in the UI. Check "
                "`focused_chat_available` in startup state first; call directly without calling "
                "`look_at_screen` when the user asks to send, drop, paste, write, or save "
                "something into the focused chat."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Clean markdown/text to post into active text chat.",
                    },
                    "label": {
                        "type": "string",
                        "description": "Optional short heading prepended to posted message.",
                    },
                },
                "required": ["content"],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="navigate_ui",
            description=(
                "Switch the Clea Web UI between top-level views ('chat', 'voice', 'activity', "
                "'memory', 'settings') or open a specific saved conversation by ID."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "view": {
                        "type": "string",
                        "enum": ["chat", "voice", "activity", "memory", "settings"],
                        "description": "Target UI view to display.",
                    },
                    "conversation_id": {
                        "type": "string",
                        "description": (
                            "Optional conversation/session ID to open when switching to 'chat'."
                        ),
                    },
                },
                "required": ["view"],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="create_chat",
            description=(
                "Create a new titled chat thread (optionally pre-filled with a summary, code, "
                "or plan) and open it immediately in the Clea Web UI even if no chat was "
                "previously focused."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Concise title for the new chat conversation.",
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "Optional initial assistant message (markdown summary, code, or plan)."
                        ),
                    },
                    "open_in_ui": {
                        "type": "boolean",
                        "description": (
                            "Whether to switch the UI immediately to the created chat "
                            "(default true)."
                        ),
                    },
                },
                "required": ["title"],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="send_chat_message",
            description=(
                "Submit a prompt into a new or existing chat conversation so Clea's text model "
                "streams a full reasoning, analysis, or code response live on screen."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "User prompt or question to run in the text chat.",
                    },
                    "conversation_id": {
                        "type": "string",
                        "description": (
                            "Optional existing conversation ID. Creates a new chat if omitted."
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional title to use if creating a new chat.",
                    },
                },
                "required": ["prompt"],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="rename_chat",
            description="Rename an existing text chat conversation.",
            parameters_schema={
                "type": "object",
                "properties": {
                    "conversation_id": {
                        "type": "string",
                        "description": "Conversation/session ID to rename.",
                    },
                    "new_title": {
                        "type": "string",
                        "description": "New display title for the conversation.",
                    },
                },
                "required": ["conversation_id", "new_title"],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="delete_chat",
            description=(
                "Delete a saved text chat conversation (voice_companion is protected). Always ask "
                "the user for verbal confirmation before calling this tool with confirmed=true."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "conversation_id": {
                        "type": "string",
                        "description": "Conversation/session ID to delete.",
                    },
                    "confirmed": {
                        "type": "boolean",
                        "description": (
                            "Must be true only after the user explicitly confirms "
                            "deletion verbally."
                        ),
                    },
                },
                "required": ["conversation_id"],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="save_memory_fact",
            description=(
                "Save a persistent Durable Fact or preference to Clea's long-term Memory store "
                "and refresh the Memory view."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": (
                            "Concise fact, preference, project note, or decision to save."
                        ),
                    },
                    "category": {
                        "type": "string",
                        "description": (
                            "Optional category/section (preferences, facts, projects, people, "
                            "routines, decisions)."
                        ),
                    },
                },
                "required": ["fact"],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="delete_memory_fact",
            description=(
                "Remove one or more Durable Facts from Clea's long-term Memory by fact ID or "
                "matching search query."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "query_or_id": {
                        "type": "string",
                        "description": "Fact ID or search phrase identifying the fact to remove.",
                    }
                },
                "required": ["query_or_id"],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="inspect_activity",
            description=(
                "Inspect recent and currently running background Activity tasks with their "
                "status, description, and timestamps."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "description": "Maximum number of tasks to list (default 10).",
                    }
                },
                "required": [],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="cancel_task",
            description="Cancel a currently running background Activity task by task ID.",
            parameters_schema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "ID of the running Activity task to cancel.",
                    }
                },
                "required": ["task_id"],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="open_command_palette",
            description="Open the quick-switcher command palette overlay in the Clea Web UI.",
            parameters_schema={
                "type": "object",
                "properties": {},
                "required": [],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="open_or_focus_app",
            description=(
                "Focus an already-open Windows application window if visible, or safely launch "
                "a known developer or desktop application (e.g., vscode, terminal, chrome, edge, "
                "explorer, cursor, antigravity, notepad, settings, taskmgr)."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": (
                            "Application name or alias (e.g., vscode, code, terminal, wt, "
                            "chrome, edge, explorer, cursor, antigravity, notepad, settings, "
                            "taskmgr)."
                        ),
                    },
                    "target_path": {
                        "type": "string",
                        "description": (
                            "Optional existing local file or directory path to open when "
                            "launching the application."
                        ),
                    },
                },
                "required": ["app_name"],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="control_media_volume",
            description=(
                "Control Windows system audio volume or media playback without shell execution."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "mute",
                            "unmute",
                            "toggle_mute",
                            "volume_up",
                            "volume_down",
                            "set_volume",
                            "play_pause",
                            "next_track",
                            "previous_track",
                        ],
                        "description": "Volume or media playback action to perform.",
                    },
                    "level": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": (
                            "Target volume percentage (0-100); required when action is "
                            "'set_volume'."
                        ),
                    },
                },
                "required": ["action"],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="read_clipboard",
            description=(
                "Read plain text from the Windows clipboard, truncated to max_chars to keep "
                "voice context bounded."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "max_chars": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100000,
                        "description": "Maximum characters to return (default 8000).",
                    },
                    "include_history": {
                        "type": "boolean",
                        "description": (
                            "Whether to include recent clipboard history items (default false)."
                        ),
                    },
                    "max_items": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "description": (
                            "Maximum history items to inspect when include_history is true "
                            "(default 10)."
                        ),
                    },
                },
                "required": [],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="select_clipboard_item",
            description=(
                "Restore a specific item from Windows clipboard history by zero-based index "
                "onto the active clipboard."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Zero-based index of the clipboard history item to restore."
                        ),
                    },
                },
                "required": ["index"],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="open_url",
            description=(
                "Validate and open an http/https URL or common site shorthand (e.g. 'github', "
                "'youtube', 'docs') in the default or specified browser."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Web URL (http:// or https://) or site shorthand to open.",
                    },
                    "browser": {
                        "type": "string",
                        "description": (
                            "Optional browser name or executable (e.g., 'chrome', 'edge', "
                            "'firefox')."
                        ),
                    },
                },
                "required": ["url"],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="control_system",
            description=(
                "Control Windows display brightness, window state (minimize/maximize/restore/"
                "close), screen lock, or open allowlisted Windows Settings pages."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "get_brightness",
                            "set_brightness",
                            "step_brightness",
                            "brightness_up",
                            "brightness_down",
                            "minimize_window",
                            "maximize_window",
                            "restore_window",
                            "close_window",
                            "lock_screen",
                            "open_settings",
                        ],
                        "description": "System control action to perform.",
                    },
                    "level": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": (
                            "Target brightness level (0-100); required when action is "
                            "'set_brightness'."
                        ),
                    },
                    "step": {
                        "type": "integer",
                        "description": (
                            "Relative brightness step increment/decrement; used with "
                            "'step_brightness'."
                        ),
                    },
                    "window_action": {
                        "type": "string",
                        "enum": ["minimize", "maximize", "restore", "close"],
                        "description": "Optional window action when action is window-related.",
                    },
                    "window_title": {
                        "type": "string",
                        "description": "Window title or substring to target for window actions.",
                    },
                    "hwnd": {
                        "type": "integer",
                        "description": "Optional window handle (HWND) for window actions.",
                    },
                    "app_name": {
                        "type": "string",
                        "description": "Optional application name to target for window actions.",
                    },
                    "page": {
                        "type": "string",
                        "description": (
                            "Windows Settings sub-page (e.g. 'display', 'sound', 'network', "
                            "'appsfeatures', 'windowsupdate') when action is 'open_settings'."
                        ),
                    },
                    "uri": {
                        "type": "string",
                        "description": "Direct ms-settings: URI when action is 'open_settings'.",
                    },
                },
                "required": ["action"],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="write_clipboard",
            description="Write plain text to the Windows clipboard.",
            parameters_schema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Plain text content to place on the Windows clipboard.",
                    }
                },
                "required": ["text"],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="inspect_ui_elements",
            description=(
                "Inspect visible interactive and readout UI Automation controls (buttons, "
                "inputs, menu items, tabs, links, text values, and screen coordinates) in a "
                "Windows application or the active foreground window."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": (
                            "Optional application or window title to target and focus (e.g. "
                            "'Calculator', 'Notepad'). Defaults to the foreground window."
                        ),
                    },
                    "max_elements": {
                        "type": "integer",
                        "description": (
                            "Maximum number of visible UI elements to return (clamped 1..150, "
                            "default 60)."
                        ),
                    },
                },
                "required": [],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="click_ui_element",
            description=(
                "Find and invoke or left-click a visible UI control by name in a Windows "
                "application (case-insensitive exact match, then substring match)."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Visible label or name of the UI control to click (e.g. 'One', "
                            "'Equals', 'Factorial', 'Open Navigation')."
                        ),
                    },
                    "control_type": {
                        "type": "string",
                        "description": (
                            "Optional UI Automation control type filter such as 'Button', "
                            "'MenuItem', 'Edit', 'TabItem', or 'Hyperlink'."
                        ),
                    },
                    "app_name": {
                        "type": "string",
                        "description": (
                            "Optional target application or window title. Defaults to the "
                            "active foreground window."
                        ),
                    },
                },
                "required": ["name"],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="gui_action",
            description=(
                "Perform a low-level desktop GUI input or wait step: click_at screen coordinates, "
                "type_text via Unicode keystrokes, press_keys (single key or chord like "
                "['ctrl', 'l']), scroll wheel at optional coordinates, or wait for UI/page loads."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["click_at", "type_text", "press_keys", "scroll", "wait"],
                        "description": "GUI input or wait action to execute.",
                    },
                    "x": {
                        "type": "integer",
                        "description": (
                            "Horizontal screen coordinate (>= 0) for click_at or scroll."
                        ),
                    },
                    "y": {
                        "type": "integer",
                        "description": "Vertical screen coordinate (>= 0) for click_at or scroll.",
                    },
                    "text": {
                        "type": "string",
                        "description": (
                            "Unicode text (1..2000 chars) to type into the focused control for "
                            "'type_text'."
                        ),
                    },
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "List of key names for 'press_keys' (e.g. ['enter'], ['ctrl', 'l'], "
                            "['ctrl', 'a']). System termination chords are blocked."
                        ),
                    },
                    "clicks": {
                        "type": "integer",
                        "description": "Click count (1..3, default 1) for 'click_at'.",
                    },
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "double"],
                        "description": "Mouse button for 'click_at' (default 'left').",
                    },
                    "delta": {
                        "type": "integer",
                        "description": (
                            "Mouse wheel scroll delta for 'scroll' (e.g. -360 down, 360 up)."
                        ),
                    },
                    "seconds": {
                        "type": "number",
                        "description": (
                            "Wait duration in seconds for 'wait' (clamped 0.1..10.0, default 1.0)."
                        ),
                    },
                },
                "required": ["action"],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="inspect_git_workspace",
            description=(
                "Inspect a local Git repository or sibling worktree strictly read-only: current "
                "branch, working tree status, worktrees, recent commits, and optional diff."
            ),
            parameters_schema={
                "type": "object",
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": (
                            "Optional repository or sibling worktree path. Defaults to the Clea "
                            "workspace root."
                        ),
                    },
                    "include_diff": {
                        "type": "boolean",
                        "description": "Whether to include working-tree diff output.",
                    },
                    "diff_stat_only": {
                        "type": "boolean",
                        "description": (
                            "When include_diff is true, return diff --stat summary instead of "
                            "full patch (default true)."
                        ),
                    },
                },
                "required": [],
            },
            behavior="non_blocking",
        ),
        VoiceToolDeclaration(
            name="end_call",
            description="End the active voice call politely when the user asks to hang up.",
            parameters_schema={
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Short reason for ending the call.",
                    }
                },
                "required": ["reason"],
            },
            behavior="non_blocking",
        ),
    ]


class SessionResumptionConfig(_StrictModel):
    """Configures live session resumption across transient disconnects."""

    enabled: bool = True
    handle: str | None = None


class ContextCompressionConfig(_StrictModel):
    """Configures sliding-window context compression for long voice calls."""

    enabled: bool = True
    trigger_tokens: int | None = Field(default=None, gt=0)
    target_tokens: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _validate_thresholds(self) -> ContextCompressionConfig:
        if (
            self.trigger_tokens is not None
            and self.target_tokens is not None
            and self.target_tokens > self.trigger_tokens
        ):
            raise ValueError("target_tokens must be <= trigger_tokens.")
        return self


class VoiceSessionConfig(_StrictModel):
    """Vendor-agnostic session setup passed to ``VoiceBackend.connect``."""

    system_instruction: str
    tools: list[VoiceToolDeclaration] = Field(default_factory=default_voice_tools)
    enable_web_search: bool = False
    media_resolution: MediaResolution = "high"
    input_sample_rate_hz: int = Field(default=INPUT_AUDIO_SAMPLE_RATE_HZ, gt=0)
    output_sample_rate_hz: int = Field(default=OUTPUT_AUDIO_SAMPLE_RATE_HZ, gt=0)
    session_resumption: SessionResumptionConfig = Field(default_factory=SessionResumptionConfig)
    context_compression: ContextCompressionConfig = Field(default_factory=ContextCompressionConfig)
    voice_name: str | None = None
    # Intentionally None by default so the live model can code-switch (e.g. English/Hindi).
    language_code: str | None = None
    transcription_language: str | None = "en-IN"
    vad_mode: Literal["server", "client"] = "server"
    reconnect_limit: int = Field(default=3, ge=0)


class VoiceToolMediaPart(_StrictModel):
    """Inline binary media attachment delivered atomically with a tool response."""

    data: bytes = Field(min_length=1)
    mime_type: str = Field(default="image/jpeg", min_length=1)

    @field_validator("mime_type", mode="after")
    @classmethod
    def _validate_mime_type(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("mime_type must not be empty or whitespace.")
        return stripped


class VoiceToolResponse(_StrictModel):
    """Tool execution output sent back to the live voice session."""

    call_id: str
    name: str
    response: dict[str, Any] = Field(default_factory=dict)
    media_parts: list[VoiceToolMediaPart] = Field(default_factory=list)
    is_error: bool = False
    scheduling: ToolResponseScheduling = "when_idle"


# --------------------------------------------------------------------------- backend events


class AudioOutputEvent(_StrictModel):
    """24 kHz PCM audio chunk produced by the live model."""

    type: Literal["audio_output"] = "audio_output"
    pcm_bytes: bytes
    sample_rate_hz: int = Field(default=OUTPUT_AUDIO_SAMPLE_RATE_HZ, gt=0)


class TranscriptOutputEvent(_StrictModel):
    """Incremental or final speech transcript for user input or assistant output."""

    type: Literal["transcript"] = "transcript"
    role: TranscriptRole
    text: str
    final: bool = False


class InterruptedEvent(_StrictModel):
    """Emitted when user speech interrupts (barges in on) model output."""

    type: Literal["interrupted"] = "interrupted"


class TurnCompleteEvent(_StrictModel):
    """Emitted when the model finishes a spoken/tool turn."""

    type: Literal["turn_complete"] = "turn_complete"


class ToolCallEvent(_StrictModel):
    """Emitted when the live model requests a voice tool invocation."""

    type: Literal["tool_call"] = "tool_call"
    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolCallCancellationEvent(_StrictModel):
    """Emitted when in-flight tool calls are cancelled (e.g. due to barge-in)."""

    type: Literal["tool_call_cancellation"] = "tool_call_cancellation"
    call_ids: list[str]


class SessionResumptionUpdateEvent(_StrictModel):
    """New opaque resumption handle emitted by the backend for reconnect continuity."""

    type: Literal["session_resumption_update"] = "session_resumption_update"
    handle: str | None = None
    resumable: bool = True


class GoAwayEvent(_StrictModel):
    """Advance notice that the backend will close the connection soon."""

    type: Literal["go_away"] = "go_away"
    time_left_seconds: float | None = Field(default=None, ge=0.0)


class UsageUpdateEvent(_StrictModel):
    """Cumulative or per-turn token usage reported by the live backend."""

    type: Literal["usage"] = "usage"
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    thought_tokens: int | None = Field(default=None, ge=0)


class BackendErrorEvent(_StrictModel):
    """Recoverable or fatal error surfaced by the voice backend."""

    type: Literal["error"] = "error"
    message: str
    recoverable: bool = True


VoiceBackendEvent = Annotated[
    AudioOutputEvent
    | TranscriptOutputEvent
    | InterruptedEvent
    | TurnCompleteEvent
    | ToolCallEvent
    | ToolCallCancellationEvent
    | SessionResumptionUpdateEvent
    | GoAwayEvent
    | UsageUpdateEvent
    | BackendErrorEvent,
    Field(discriminator="type"),
]


class VoiceBackend(ABC):
    """Abstract streaming voice backend interface."""

    backend_name: str = "unknown"

    @property
    def model_name(self) -> str:
        """Display name of the live voice model in use."""
        return "unknown"

    @property
    def resumption_handle(self) -> str | None:
        """Current session resumption handle if available."""
        return None

    async def reconnect(self) -> None:
        """Attempt to reconnect using session resumption handle."""
        recon = getattr(self, "_reconnect", None)
        if callable(recon):
            await recon()
        return None

    @abstractmethod
    async def connect(self, config: VoiceSessionConfig) -> None:
        """Open a live bidirectional voice session with the given configuration."""
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        """Close the active live voice session and release resources."""
        raise NotImplementedError

    @abstractmethod
    async def send_audio(self, pcm_bytes: bytes) -> None:
        """Stream a chunk of 16 kHz mono 16-bit PCM microphone audio."""
        raise NotImplementedError

    async def end_audio_stream(self) -> None:
        """Signal an end/pause of microphone audio input (e.g. half-duplex mute)."""
        return None

    @abstractmethod
    async def send_video_frame(
        self,
        image_bytes: bytes,
        *,
        mime_type: str = "image/jpeg",
    ) -> None:
        """Send an encoded screen image frame to the live multimodal session."""
        raise NotImplementedError

    @abstractmethod
    async def send_text(self, text: str, *, end_of_turn: bool = True) -> None:
        """Send a text message or system nudge into the live session."""
        raise NotImplementedError

    @abstractmethod
    async def send_tool_responses(self, responses: list[VoiceToolResponse]) -> None:
        """Send function execution results (with scheduling hints) back to the model."""
        raise NotImplementedError

    @abstractmethod
    def events(self) -> AsyncIterator[VoiceBackendEvent]:
        """Yield normalized ``VoiceBackendEvent`` items for the active session."""
        raise NotImplementedError
