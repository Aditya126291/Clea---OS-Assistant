"""Tests for Ambient Voice Companion Architecture (Option 1).

Covers:
1. Multiple voice calls appending into one continuous Voice Companion stream without creating
   multiple sidebar History entries.
2. Focused-chat context injection at call start (verifying Clea receives active chat turns
   while the text chat itself remains unmodified).
3. search_conversations matching across multiple threads and post_to_active_chat appending
   to the active chat (plus graceful handling when no chat is focused).
4. VoiceConfig transcription language setting and Gemini Live audio transcription lock.
5. Rolling context tail loading at call start.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from websockets.asyncio.client import connect

from clea.core import auth
from clea.core.daemon import CleaDaemon, build_daemon
from clea.core.protocol import (
    BaseRequest,
    ChatFocusParams,
    ChatFocusRequest,
    ConversationsListRequest,
    ResponseFrame,
    VoiceEndCallParams,
    VoiceEndCallRequest,
    VoiceHistoryParams,
    VoiceHistoryRequest,
    VoicePostToActiveChatParams,
    VoicePostToActiveChatRequest,
    VoiceSearchConversationsParams,
    VoiceSearchConversationsRequest,
    VoiceStartCallParams,
    VoiceStartCallRequest,
    parse_frame,
)
from clea.memory.store import VOICE_COMPANION_SESSION_ID, MemoryStore
from clea.providers.gemini import GeminiProvider
from clea.surfaces.voice.audio import AudioManager
from clea.surfaces.voice.backends.base import (
    TranscriptOutputEvent,
    TurnCompleteEvent,
    VoiceBackend,
    VoiceBackendEvent,
    VoiceSessionConfig,
    VoiceToolResponse,
    default_voice_tools,
)
from clea.surfaces.voice.config import VoiceConfig
from clea.surfaces.voice.runner import VoiceRunner
from clea.surfaces.voice.wake_word import WakeWordDetector


class FakeLiveBackend(VoiceBackend):
    backend_name = "fake-live"

    def __init__(self) -> None:
        self.connected_config: VoiceSessionConfig | None = None
        self.closed = False
        self.event_queue: asyncio.Queue[VoiceBackendEvent | None] = asyncio.Queue()
        self.sent_tool_responses: list[VoiceToolResponse] = []

    @property
    def model_name(self) -> str:
        return "fake-live-model"

    async def connect(self, config: VoiceSessionConfig) -> None:
        self.connected_config = config
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        await self.event_queue.put(None)

    async def send_audio(self, pcm_bytes: bytes) -> None:
        pass

    async def send_video_frame(self, image_bytes: bytes, *, mime_type: str = "image/jpeg") -> None:
        pass

    async def send_text(self, text: str, *, end_of_turn: bool = True) -> None:
        pass

    async def send_tool_responses(self, responses: list[VoiceToolResponse]) -> None:
        self.sent_tool_responses.extend(responses)

    async def events(self) -> AsyncIterator[VoiceBackendEvent]:
        while True:
            evt = await self.event_queue.get()
            if evt is None:
                break
            yield evt


def _make_fake_audio() -> AudioManager:
    audio = AudioManager()
    audio.start = MagicMock()  # type: ignore[method-assign]
    audio.stop = MagicMock()  # type: ignore[method-assign]
    audio.play_audio = MagicMock()  # type: ignore[method-assign]
    audio.stop_output = MagicMock()  # type: ignore[method-assign]
    audio.set_input_callback = MagicMock()  # type: ignore[method-assign]
    return audio


def _make_fake_wake_word() -> WakeWordDetector:
    mock_model = MagicMock()
    mock_model.predict.return_value = {"hey_jarvis_v0.1": 0.0}
    return WakeWordDetector(model_instance=mock_model)


class FakeConsolidationProvider(GeminiProvider):
    def __init__(self) -> None:
        pass

    async def run_chat(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


async def _wait_until(pred: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError("Predicate not satisfied within timeout.")


async def _send_rpc(ws: Any, req: BaseRequest) -> ResponseFrame:
    await ws.send(req.model_dump_json())
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        frame = parse_frame(raw)
        if isinstance(frame, ResponseFrame) and frame.id == req.id:
            return frame


# --- 1. Multiple Voice Calls Append to Single Voice Companion Stream ---


async def test_multiple_voice_calls_append_into_single_voice_companion_stream(
    tmp_path: Path,
) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    provider = FakeConsolidationProvider()
    daemon: CleaDaemon = build_daemon(host="127.0.0.1", port=0, provider=provider, memory=mem)
    daemon.consolidation.min_user_turns = 2
    await daemon.start()

    url = f"ws://127.0.0.1:{daemon.bound_port}"
    backends: list[FakeLiveBackend] = []

    def _backend_factory() -> FakeLiveBackend:
        b = FakeLiveBackend()
        backends.append(b)
        return b

    runner = VoiceRunner(
        config=VoiceConfig(silence_timeout_seconds=60.0),
        data_dir=tmp_path,
        daemon_url=url,
        token=daemon.token,
        backend_factory=_backend_factory,
        audio_manager=_make_fake_audio(),
        wake_word_detector=_make_fake_wake_word(),
        enable_hotkey=False,
    )

    runner_task = asyncio.create_task(runner.run_forever())
    try:
        await _wait_until(lambda: runner._daemon_listener_active)

        async with connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ui_ws:
            # === Call 1 ===
            await runner.start_call(trigger="manual")
            call1_id = runner.call_id
            assert call1_id is not None
            backend1 = backends[-1]

            await backend1.event_queue.put(
                TranscriptOutputEvent(role="user", text="First call: Hello Clea!", final=True)
            )
            await backend1.event_queue.put(
                TranscriptOutputEvent(role="assistant", text="Hello! How can I help?", final=True)
            )
            await backend1.event_queue.put(TurnCompleteEvent())
            await _wait_until(lambda: len(runner._transcript_history) == 2)

            await runner.end_call(reason="user_done")
            await _wait_until(lambda: runner.call_state == "idle")

            # Verify sidebar history is completely clean (0 entries)
            list_resp1 = await _send_rpc(ui_ws, ConversationsListRequest())
            assert list_resp1.ok
            assert len(list_resp1.result["conversations"]) == 0

            # Verify voice.history returns Call 1 turns
            vh_resp1 = await _send_rpc(
                ui_ws, VoiceHistoryRequest(params=VoiceHistoryParams(limit=20))
            )
            assert vh_resp1.ok
            msgs1 = vh_resp1.result["messages"]
            assert len(msgs1) == 2
            assert msgs1[0]["call_id"] == call1_id
            assert msgs1[0]["text"] == "First call: Hello Clea!"
            assert msgs1[1]["text"] == "Hello! How can I help?"

            # === Call 2 ===
            await runner.start_call(trigger="manual")
            call2_id = runner.call_id
            assert call2_id is not None
            assert call2_id != call1_id
            backend2 = backends[-1]

            await backend2.event_queue.put(
                TranscriptOutputEvent(
                    role="user", text="Second call: Remember we use uv.", final=True
                )
            )
            await backend2.event_queue.put(
                TranscriptOutputEvent(role="assistant", text="Got it, using uv.", final=True)
            )
            await backend2.event_queue.put(TurnCompleteEvent())
            await _wait_until(lambda: len(runner._transcript_history) == 2)

            await runner.end_call(reason="user_done")
            await _wait_until(lambda: runner.call_state == "idle")

            # Verify sidebar history STILL contains 0 entries (no clutter from Call 1 or Call 2)
            list_resp2 = await _send_rpc(ui_ws, ConversationsListRequest())
            assert list_resp2.ok
            assert len(list_resp2.result["conversations"]) == 0

            # Verify voice.history now contains ALL 4 messages chronologically in one single stream
            vh_resp2 = await _send_rpc(
                ui_ws, VoiceHistoryRequest(params=VoiceHistoryParams(limit=20))
            )
            assert vh_resp2.ok
            msgs2 = vh_resp2.result["messages"]
            assert len(msgs2) == 4
            assert msgs2[0]["call_id"] == call1_id
            assert msgs2[0]["text"] == "First call: Hello Clea!"
            assert msgs2[2]["call_id"] == call2_id
            assert msgs2[2]["text"] == "Second call: Remember we use uv."

            # Verify underlying MemoryStore structured file is exclusively voice_companion.jsonl
            assert mem.transcript_path(VOICE_COMPANION_SESSION_ID).exists()
            assert not mem.transcript_path(call1_id).exists()
            assert not mem.transcript_path(call2_id).exists()

            # Verify get_transcript(call_id) can reconstruct individual call turns for consolidation
            call1_tr = mem.get_transcript(call1_id)
            assert call1_tr is not None
            assert len(call1_tr["messages"]) == 2
            assert call1_tr["is_voice_call"] is True

            call2_tr = mem.get_transcript(call2_id)
            assert call2_tr is not None
            assert len(call2_tr["messages"]) == 2
            assert call2_tr["is_voice_call"] is True
    finally:
        await runner.stop()
        runner_task.cancel()
        await daemon.stop()


# --- 2. Live Focused-Chat Context Binding (Read-Only Awareness) ---


async def test_focused_chat_context_injection_at_call_start(tmp_path: Path) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()

    # Create a focused text chat with messages
    chat_sid = "focused-chat-1234"
    mem.append_transcript_message(
        session_id=chat_sid,
        role="user",
        text="Can we refactor the payment gateway to use Stripe?",
        title="Payment Gateway Stripe Refactor",
        timestamp=datetime.now().astimezone(),
    )
    mem.append_transcript_message(
        session_id=chat_sid,
        role="assistant",
        text="I analyzed the payment gateway. We need to touch api/checkout.py.",
        title="Payment Gateway Stripe Refactor",
        timestamp=datetime.now().astimezone(),
    )

    provider = FakeConsolidationProvider()
    daemon: CleaDaemon = build_daemon(host="127.0.0.1", port=0, provider=provider, memory=mem)
    await daemon.start()

    url = f"ws://127.0.0.1:{daemon.bound_port}"
    async with connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ws:
        # Initial call start without focused chat: no active chat section
        start_res1 = await _send_rpc(
            ws, VoiceStartCallRequest(params=VoiceStartCallParams(trigger="manual"))
        )
        assert start_res1.ok
        ctx1 = start_res1.result.get("memory_context") or ""
        assert "Active Focused Text Chat" not in ctx1
        await _send_rpc(
            ws, VoiceEndCallRequest(params=VoiceEndCallParams(call_id=start_res1.result["call_id"]))
        )

        # Notify daemon that user opened the text chat tab
        focus_res = await _send_rpc(
            ws, ChatFocusRequest(params=ChatFocusParams(session_id=chat_sid))
        )
        assert focus_res.ok
        assert focus_res.result["active_conversation_id"] == chat_sid

        # Voice call starts: should inject focused chat turns into system context
        start_res2 = await _send_rpc(
            ws, VoiceStartCallRequest(params=VoiceStartCallParams(trigger="manual"))
        )
        assert start_res2.ok
        ctx2 = start_res2.result.get("memory_context") or ""
        assert "Active Focused Text Chat (Read-Only Context)" in ctx2
        assert chat_sid in ctx2
        assert "Payment Gateway Stripe Refactor" in ctx2
        assert "Can we refactor the payment gateway to use Stripe?" in ctx2
        assert "I analyzed the payment gateway. We need to touch api/checkout.py." in ctx2

        # Verify the text chat in MemoryStore remained completely unmodified (still 2 messages)
        chat_tr = mem.get_transcript(chat_sid)
        assert chat_tr is not None
        assert len(chat_tr["messages"]) == 2

        # User navigates away to blank/activity/settings
        clear_focus = await _send_rpc(ws, ChatFocusRequest(params=ChatFocusParams(clear=True)))
        assert clear_focus.ok
        assert clear_focus.result["active_conversation_id"] is None

        await _send_rpc(
            ws, VoiceEndCallRequest(params=VoiceEndCallParams(call_id=start_res2.result["call_id"]))
        )

        # Next call start no longer includes active chat context
        start_res3 = await _send_rpc(
            ws, VoiceStartCallRequest(params=VoiceStartCallParams(trigger="manual"))
        )
        assert start_res3.ok
        ctx3 = start_res3.result.get("memory_context") or ""
        assert "Active Focused Text Chat" not in ctx3
        await _send_rpc(
            ws, VoiceEndCallRequest(params=VoiceEndCallParams(call_id=start_res3.result["call_id"]))
        )

    await daemon.stop()


# --- 3. Cross-Chat Bridge Tools: search_conversations & post_to_active_chat ---


async def test_search_conversations_and_post_to_active_chat_tools(tmp_path: Path) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()

    # Text chat 1: deployment discussion
    mem.append_transcript_message(
        session_id="chat-k8s",
        role="user",
        text="How do we deploy to Kubernetes cluster staging-us-east?",
        title="Kubernetes Staging Deployment",
    )
    mem.append_transcript_message(
        session_id="chat-k8s",
        role="assistant",
        text="Run `kubectl apply -f k8s/staging/` with KUBECONFIG=~/.kube/staging.yaml.",
        title="Kubernetes Staging Deployment",
    )

    # Voice Companion stream: spoken note about database migration
    mem.append_voice_companion_call(
        call_id="call-voice-1",
        entries=[
            (
                "user",
                "Remember to run alembic upgrade head before deploying.",
                datetime.now().astimezone(),
            ),
            (
                "assistant",
                "Noted, database migrations must precede deploy.",
                datetime.now().astimezone(),
            ),
        ],
    )

    provider = FakeConsolidationProvider()
    daemon: CleaDaemon = build_daemon(host="127.0.0.1", port=0, provider=provider, memory=mem)
    await daemon.start()

    url = f"ws://127.0.0.1:{daemon.bound_port}"
    async with connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ws:
        # 3.1 Search conversations across text chat and voice companion
        search_res1 = await _send_rpc(
            ws,
            VoiceSearchConversationsRequest(
                params=VoiceSearchConversationsParams(query="kubectl apply", limit=5)
            ),
        )
        assert search_res1.ok
        hits1 = search_res1.result["results"]
        assert len(hits1) >= 1
        assert hits1[0]["session_id"] == "chat-k8s"
        assert "kubectl apply" in hits1[0]["snippet"]

        # Search voice companion content
        search_res2 = await _send_rpc(
            ws,
            VoiceSearchConversationsRequest(
                params=VoiceSearchConversationsParams(query="alembic upgrade", limit=5)
            ),
        )
        assert search_res2.ok
        hits2 = search_res2.result["results"]
        assert len(hits2) >= 1
        assert hits2[0]["session_id"] == VOICE_COMPANION_SESSION_ID
        assert "alembic upgrade" in hits2[0]["snippet"]

        # 3.2 post_to_active_chat when no active chat is focused -> returns helpful error
        post_unfocused = await _send_rpc(
            ws,
            VoicePostToActiveChatRequest(
                params=VoicePostToActiveChatParams(content="Here is your script.")
            ),
        )
        assert post_unfocused.ok
        assert post_unfocused.result["ok"] is False
        assert "No active text chat is currently focused" in post_unfocused.result["message"]

        # 3.3 Set focused chat and post content into it
        await _send_rpc(ws, ChatFocusRequest(params=ChatFocusParams(session_id="chat-k8s")))

        post_focused = await _send_rpc(
            ws,
            VoicePostToActiveChatRequest(
                params=VoicePostToActiveChatParams(
                    content="export KUBECONFIG=~/.kube/staging.yaml\nkubectl get pods -n staging",
                    label="Deploy Commands",
                )
            ),
        )
        assert post_focused.ok
        assert post_focused.result["ok"] is True
        assert post_focused.result["session_id"] == "chat-k8s"

        # Verify chat-k8s transcript in MemoryStore now has the assistant message appended
        updated_tr = mem.get_transcript("chat-k8s")
        assert updated_tr is not None
        assert len(updated_tr["messages"]) == 3
        last_msg = updated_tr["messages"][-1]
        assert last_msg["role"] == "assistant"
        assert "**Deploy Commands**" in last_msg["text"]
        assert "export KUBECONFIG" in last_msg["text"]

    await daemon.stop()


# --- 4. VoiceConfig Transcription Language Setting & Default Tools ---


def test_voice_config_transcription_language_and_default_tools() -> None:
    # Default transcription_language is "en-IN"
    cfg_default = VoiceConfig()
    assert cfg_default.transcription_language == "en-IN"

    # Custom English transcription lock
    cfg_us = VoiceConfig(transcription_language="en-US")
    assert cfg_us.transcription_language == "en-US"

    # Auto-detect when explicitly empty string or None
    cfg_auto = VoiceConfig(transcription_language="")
    assert cfg_auto.transcription_language is None

    cfg_none = VoiceConfig(transcription_language=None)
    assert cfg_none.transcription_language is None

    # Canonical default voice tools include the 2 new bridge tools
    tools = default_voice_tools()
    by_name = {t.name: t for t in tools}
    assert "search_conversations" in by_name
    assert "post_to_active_chat" in by_name
    assert by_name["search_conversations"].parameters_schema["required"] == ["query"]
    assert by_name["post_to_active_chat"].parameters_schema["required"] == ["content"]


# --- 5. Rolling Voice Companion Tail Context Injection ---


def test_voice_companion_rolling_context_tail_sizing(tmp_path: Path) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()

    # Append 30 turns across 3 calls
    entries: list[tuple[str, str, datetime]] = []
    for i in range(30):
        role = "user" if i % 2 == 0 else "assistant"
        entries.append((role, f"Voice turn #{i + 1}", datetime.now().astimezone()))

    mem.append_voice_companion_call(call_id="call-bulk", entries=entries)

    # get_voice_companion_tail with default max_turns=15 should return exactly last 15
    tail = mem.get_voice_companion_tail(max_turns=15)
    assert len(tail) == 15
    assert tail[0]["text"] == "Voice turn #16"
    assert tail[-1]["text"] == "Voice turn #30"

    # build_injected_context with include_voice_tail=True incorporates recent turns only
    ctx = mem.build_injected_context(include_voice_tail=True, voice_tail_turns=15)
    assert "## Recent Voice Companion History" in ctx
    assert "Voice turn #30" in ctx
    assert "Voice turn #10" not in ctx  # older turns rolling-trimmed
    assert "Voice turn #5" not in ctx


# --- 6. Robustness & Edge Cases: Chat Focus Safety, Deletion, Tool Search & Multi-KB Code ---


async def test_chat_focus_query_safety_and_voice_companion_rejection(tmp_path: Path) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    provider = FakeConsolidationProvider()
    daemon: CleaDaemon = build_daemon(host="127.0.0.1", port=0, provider=provider, memory=mem)
    await daemon.start()

    url = f"ws://127.0.0.1:{daemon.bound_port}"
    async with connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ws:
        # 6.1 Set active chat
        set_res = await _send_rpc(ws, ChatFocusRequest(params=ChatFocusParams(session_id="chat-1")))
        assert set_res.ok
        assert set_res.result["active_conversation_id"] == "chat-1"

        # 6.2 Query active chat without params (should NOT clear active chat)
        query_res = await _send_rpc(ws, ChatFocusRequest(params=ChatFocusParams()))
        assert query_res.ok
        assert query_res.result["active_conversation_id"] == "chat-1"

        # 6.3 Attempt to set voice_companion as focused chat (should be rejected/cleared)
        vc_focus_res = await _send_rpc(
            ws, ChatFocusRequest(params=ChatFocusParams(session_id=VOICE_COMPANION_SESSION_ID))
        )
        assert vc_focus_res.ok
        assert vc_focus_res.result["active_conversation_id"] is None

    await daemon.stop()


async def test_conversation_deletion_clears_active_focused_chat(tmp_path: Path) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    mem.append_transcript_message(
        session_id="chat-to-delete",
        role="user",
        text="Temporary conversation to delete.",
    )
    provider = FakeConsolidationProvider()
    daemon: CleaDaemon = build_daemon(host="127.0.0.1", port=0, provider=provider, memory=mem)
    await daemon.start()

    url = f"ws://127.0.0.1:{daemon.bound_port}"
    async with connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ws:
        from clea.core.protocol import ConversationDeleteParams, ConversationDeleteRequest

        # Focus the chat
        await _send_rpc(ws, ChatFocusRequest(params=ChatFocusParams(session_id="chat-to-delete")))
        assert daemon._active_conversation_id == "chat-to-delete"

        # Delete it via daemon RPC
        del_res = await _send_rpc(
            ws,
            ConversationDeleteRequest(
                params=ConversationDeleteParams(session_id="chat-to-delete", delete_episodes=False)
            ),
        )
        assert del_res.ok

        # Active conversation id must be reset to None (no zombie focused chat)
        assert daemon._active_conversation_id is None

    await daemon.stop()


async def test_post_to_active_chat_synchronizes_in_memory_session(tmp_path: Path) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    chat_sid = "chat-sync-test"
    mem.append_transcript_message(
        session_id=chat_sid,
        role="user",
        text="What is the plan?",
    )
    provider = FakeConsolidationProvider()
    daemon: CleaDaemon = build_daemon(host="127.0.0.1", port=0, provider=provider, memory=mem)
    await daemon.start()

    # Pre-load session into memory
    active_sess = daemon.sessions.get_or_create(chat_sid)
    assert len(active_sess.messages) == 0

    url = f"ws://127.0.0.1:{daemon.bound_port}"
    async with connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ws:
        await _send_rpc(ws, ChatFocusRequest(params=ChatFocusParams(session_id=chat_sid)))

        # Post multi-kilobyte code block with markdown table
        multi_kb_code = "```python\n" + "# Large generated script\n" + ("x = 1\n" * 500) + "```"
        post_res = await _send_rpc(
            ws,
            VoicePostToActiveChatRequest(
                params=VoicePostToActiveChatParams(content=multi_kb_code, label="Generated Script")
            ),
        )
        assert post_res.ok
        assert post_res.result["ok"] is True

        # In-memory session messages list must be updated
        assert len(active_sess.messages) == 1
        assert active_sess.messages[0].role == "assistant"
        assert "Generated Script" in active_sess.messages[0].content
        assert "x = 1" in active_sess.messages[0].content

        # On-disk transcript must have the turn
        disk_tr = mem.get_transcript(chat_sid)
        assert disk_tr is not None
        assert len(disk_tr["messages"]) == 2

    await daemon.stop()


def test_search_conversations_tool_calls_and_title_dedup(tmp_path: Path) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()

    # Chat 1: Tool execution with curl command and output
    mem.append_transcript_message(
        session_id="chat-curl",
        role="assistant",
        text="I queried the status endpoint for you.",
        tool_calls=[
            {
                "name": "run_command",
                "arguments": {"command": "curl -s http://localhost:8080/v1/health"},
                "output": '{"status": "UP", "database": "connected"}',
            }
        ],
        title="API Health Check",
    )

    # Chat 2: Long conversation matching title "API Health Check" but no message matching curl
    for i in range(10):
        mem.append_transcript_message(
            session_id="chat-long-title",
            role="user" if i % 2 == 0 else "assistant",
            text=f"General discussion message {i}",
            title="API Health Check",
        )

    # 1. Search for command inside tool call arguments
    hits = mem.search_conversations(query="curl -s http://localhost:8080/v1/health", limit=5)
    assert len(hits) >= 1
    assert hits[0]["session_id"] == "chat-curl"
    assert "curl -s http://localhost:8080/v1/health" in hits[0]["snippet"]

    # 2. Search for output inside tool call
    hits_out = mem.search_conversations(query="database connected", limit=5)
    assert len(hits_out) >= 1
    assert hits_out[0]["session_id"] == "chat-curl"

    # 3. Title match does not flood 10 messages from chat-long-title
    hits_title = mem.search_conversations(query="API Health Check", limit=5)
    long_title_hits = [h for h in hits_title if h["session_id"] == "chat-long-title"]
    assert len(long_title_hits) == 1  # exactly 1 representative hit, not 10!


async def test_safe_load_voice_memory_context_injects_focused_chat(tmp_path: Path) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()

    chat_sid = "chat-safe-loader"
    mem.append_transcript_message(
        session_id=chat_sid,
        role="user",
        text="Let's build a Redis cache wrapper.",
        title="Redis Cache Wrapper",
    )
    mem.append_transcript_message(
        session_id=chat_sid,
        role="assistant",
        text="Created cache/redis.py with TTL support.",
        title="Redis Cache Wrapper",
    )

    provider = FakeConsolidationProvider()
    daemon: CleaDaemon = build_daemon(host="127.0.0.1", port=0, provider=provider, memory=mem)
    await daemon.start()

    url = f"ws://127.0.0.1:{daemon.bound_port}"
    async with connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ws:
        from clea.surfaces.voice.memory_hooks import safe_load_voice_memory_context

        # Set focus
        await _send_rpc(ws, ChatFocusRequest(params=ChatFocusParams(session_id=chat_sid)))

        # Call safe_load_voice_memory_context with daemon connection and active conversation
        context = await safe_load_voice_memory_context(
            active_conversation_id=chat_sid,
            daemon_url=url,
            token=daemon.token,
            data_dir=tmp_path,
        )
        assert context is not None
        assert "## Active Focused Text Chat (Read-Only Context)" in context
        assert chat_sid in context
        assert "Redis Cache Wrapper" in context
        assert "Let's build a Redis cache wrapper." in context

    await daemon.stop()


# --- 7. Voice Companion Stream Protection & Collision Hardening ---


async def test_delete_all_transcripts_preserves_voice_companion_stream(tmp_path: Path) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()

    # 1. Add normal text chat
    mem.append_transcript_message(
        session_id="chat-to-delete",
        role="user",
        text="Normal chat to be deleted",
        title="Chat to Delete",
    )
    # 2. Add continuous voice companion stream
    mem.append_voice_companion_call(
        call_id="call-voice-preserve",
        entries=[("user", "Preserve this voice turn", datetime.now().astimezone())],
    )

    assert mem.transcript_path("chat-to-delete").exists()
    assert mem.transcript_path(VOICE_COMPANION_SESSION_ID).exists()

    # Direct memory store delete_all_transcripts should NOT delete voice_companion.jsonl
    deleted_count = mem.delete_all_transcripts()
    assert deleted_count == 1
    assert not mem.transcript_path("chat-to-delete").exists()
    assert mem.transcript_path(VOICE_COMPANION_SESSION_ID).exists()

    # Attempting to delete voice companion directly via delete_transcript must raise ValueError
    import pytest

    with pytest.raises(ValueError, match="Cannot delete the Voice Companion stream"):
        mem.delete_transcript(VOICE_COMPANION_SESSION_ID)


async def test_voice_companion_stream_protected_from_chat_rpcs(tmp_path: Path) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()

    # Add continuous voice companion stream
    mem.append_voice_companion_call(
        call_id="call-protect-test",
        entries=[("user", "Hello Voice", datetime.now().astimezone())],
    )

    provider = FakeConsolidationProvider()
    daemon: CleaDaemon = build_daemon(host="127.0.0.1", port=0, provider=provider, memory=mem)
    await daemon.start()

    url = f"ws://127.0.0.1:{daemon.bound_port}"
    async with connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ws:
        from clea.core.protocol import (
            ChatSendParams,
            ChatSendRequest,
            ConversationDeleteAllParams,
            ConversationDeleteAllRequest,
            ConversationDeleteParams,
            ConversationDeleteRequest,
            ConversationPinParams,
            ConversationPinRequest,
            ConversationRenameParams,
            ConversationRenameRequest,
            ConversationResumeParams,
            ConversationResumeRequest,
        )

        # 1. conversation.resume should be rejected
        res_resume = await _send_rpc(
            ws,
            ConversationResumeRequest(
                params=ConversationResumeParams(session_id=VOICE_COMPANION_SESSION_ID)
            ),
        )
        assert not res_resume.ok
        assert "Voice Companion cannot be resumed" in (res_resume.error or "")

        # 2. conversation.rename should be rejected
        res_rename = await _send_rpc(
            ws,
            ConversationRenameRequest(
                params=ConversationRenameParams(
                    session_id=VOICE_COMPANION_SESSION_ID, title="Hacked"
                )
            ),
        )
        assert not res_rename.ok
        assert "Cannot rename the Voice Companion stream" in (res_rename.error or "")

        # 3. conversation.pin should be rejected
        res_pin = await _send_rpc(
            ws,
            ConversationPinRequest(
                params=ConversationPinParams(session_id=VOICE_COMPANION_SESSION_ID, pinned=True)
            ),
        )
        assert not res_pin.ok
        assert "Cannot pin the Voice Companion stream" in (res_pin.error or "")

        # 4. conversation.delete should be rejected
        res_del = await _send_rpc(
            ws,
            ConversationDeleteRequest(
                params=ConversationDeleteParams(
                    session_id=VOICE_COMPANION_SESSION_ID, delete_episodes=False
                )
            ),
        )
        assert not res_del.ok
        assert "Cannot delete the Voice Companion stream" in (res_del.error or "")

        # 5. chat.send should be rejected
        res_chat = await _send_rpc(
            ws,
            ChatSendRequest(
                session_id=VOICE_COMPANION_SESSION_ID,
                params=ChatSendParams(text="Text into voice stream"),
            ),
        )
        assert not res_chat.ok
        assert "Cannot send chat messages to the Voice Companion stream" in (res_chat.error or "")

        # 6. conversation.delete_all should preserve voice_companion.jsonl
        res_del_all = await _send_rpc(
            ws,
            ConversationDeleteAllRequest(params=ConversationDeleteAllParams(confirmation="DELETE")),
        )
        assert res_del_all.ok
        assert mem.transcript_path(VOICE_COMPANION_SESSION_ID).exists()

    await daemon.stop()


async def test_concurrent_call_start_trigger_collision_safety(tmp_path: Path) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    provider = FakeConsolidationProvider()
    daemon: CleaDaemon = build_daemon(host="127.0.0.1", port=0, provider=provider, memory=mem)
    await daemon.start()

    url = f"ws://127.0.0.1:{daemon.bound_port}"
    backend_starts = 0

    def _backend_factory() -> FakeLiveBackend:
        nonlocal backend_starts
        backend_starts += 1
        return FakeLiveBackend()

    runner = VoiceRunner(
        config=VoiceConfig(),
        data_dir=tmp_path,
        daemon_url=url,
        token=daemon.token,
        backend_factory=_backend_factory,
        audio_manager=_make_fake_audio(),
        wake_word_detector=_make_fake_wake_word(),
        enable_hotkey=False,
    )
    runner_task = asyncio.create_task(runner.run_forever())
    try:
        await _wait_until(lambda: runner._daemon_listener_active)

        # Fire two concurrent start_call triggers (e.g. hotkey + wake word simultaneously)
        t1 = asyncio.create_task(runner.start_call(trigger="wake_word"))
        t2 = asyncio.create_task(runner.start_call(trigger="hotkey"))
        await asyncio.gather(t1, t2)

        # Only one backend instance must be created and call must be active
        assert backend_starts == 1
        assert runner.call_state in ("connecting", "active")
        await runner.end_call(reason="user_done")
    finally:
        await runner.stop()
        runner_task.cancel()
        await daemon.stop()
