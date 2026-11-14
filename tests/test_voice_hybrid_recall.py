"""Tests for 3-layer hybrid conversation & memory recall and voice recall tools."""

from __future__ import annotations

import asyncio
import io
import json
import math
import urllib.request
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from clea.core.daemon import CleaDaemon, build_daemon
from clea.core.protocol import parse_frame
from clea.memory.consolidation import ConsolidationManager
from clea.memory.retrieval import EmbeddingProvider, cosine_similarity
from clea.memory.store import MemoryStore
from clea.providers.base import LLMProvider, Message, StreamChunk
from clea.surfaces.voice.audio import AudioManager
from clea.surfaces.voice.backends.base import (
    ToolCallEvent,
    VoiceBackend,
    VoiceBackendEvent,
    VoiceSessionConfig,
    VoiceToolResponse,
)
from clea.surfaces.voice.config import VoiceConfig
from clea.surfaces.voice.runner import VoiceRunner
from clea.surfaces.voice.wake_word import WakeWordDetector
from clea.tools import ToolDefinition


class _DummyLLMProvider(LLMProvider):
    """Minimal LLM provider for daemon/consolidation tests."""

    def __init__(self, canned_text: str = "{}") -> None:
        self.canned_text = canned_text

    async def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str,
    ) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(type="text_delta", text=self.canned_text)
        yield StreamChunk(type="done")


class _FakeVoiceBackend(VoiceBackend):
    backend_name = "fake-live"

    def __init__(self) -> None:
        self.closed = False
        self.sent_tool_responses: list[VoiceToolResponse] = []
        self.event_queue: asyncio.Queue[VoiceBackendEvent | None] = asyncio.Queue()

    @property
    def model_name(self) -> str:
        return "fake-model"

    async def connect(self, config: VoiceSessionConfig) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        await self.event_queue.put(None)

    async def send_audio(self, pcm_bytes: bytes) -> None:
        pass

    async def send_video_frame(
        self, image_bytes: bytes, *, mime_type: str = "image/jpeg"
    ) -> None:
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
    return audio


def _make_fake_wake_word() -> Any:
    mock_model = MagicMock()
    mock_model.predict.return_value = {"hey_jarvis_v0.1": 0.0}
    return WakeWordDetector(model_instance=mock_model)


def _keyword_axis_embedder(text: str) -> list[float]:
    """Deterministic unit-vector embedder mapping distinct semantic concepts to axes."""
    low = text.lower()
    vec = [0.0, 0.0, 0.0, 0.0]
    # Axis 0: container orchestration / deployment
    if any(
        w in low
        for w in ("kubernetes", "docker", "container", "cluster", "orchestration", "pods")
    ):
        vec[0] += 1.0
    # Axis 1: cooking / food
    if any(w in low for w in ("pasta", "marinara", "spaghetti", "recipe", "cooking", "dinner")):
        vec[1] += 1.0
    # Axis 2: time-series databases / telemetry
    if any(
        w in low
        for w in ("timescaledb", "telemetry", "time-series", "database", "time series")
    ):
        vec[2] += 1.0
    # Axis 3: audio / hardware troubleshooting
    if any(
        w in low for w in ("audio", "playback", "sound", "microphone", "bluetooth", "stutter")
    ):
        vec[3] += 1.0
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return [0.0, 0.0, 0.0, 0.0]
    return [x / norm for x in vec]


def test_layer1_full_history_search_beyond_top_10_and_list_conversations(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    now_dt = datetime.now(UTC)
    past_base = now_dt - timedelta(hours=20)
    # Seed 16 conversations; chat-00..chat-02 are older than chat-03..chat-15.
    for idx in range(16):
        dt = (
            past_base + timedelta(hours=idx)
            if idx <= 2
            else now_dt + timedelta(minutes=idx * 5)
        )
        sid = f"chat-{idx:02d}"
        if idx == 0:
            title = "Legacy Database Engine"
            user_txt = "How do we stop sqlite wal checkpoint contention under heavy writes?"
            asst_txt = "Set PRAGMA wal_autocheckpoint = 2000 and batch transactions."
        else:
            title = f"General Topic {idx:02d}"
            user_txt = f"Routine check-in number {idx} about weather and notes."
            asst_txt = f"All good for topic {idx}."

        store.append_transcript_message(
            session_id=sid,
            role="user",
            text=user_txt,
            timestamp=dt,
            title=title,
        )
        store.append_transcript_message(
            session_id=sid,
            role="assistant",
            text=asst_txt,
            timestamp=dt + timedelta(minutes=1),
            title=title,
        )
        if idx == 0:
            store.append_episode(
                session_id=sid,
                summary="Resolved sqlite wal checkpoint contention with PRAGMA tuning.",
                now=dt + timedelta(minutes=2),
            )
        elif idx == 2:
            # Pin chat-02 while it is still older than chat-03..chat-15.
            store.update_transcript_header("chat-02", pinned=True)
        elif idx == 15:
            store.append_episode(
                session_id=sid,
                summary="Latest status summary on routine checks.",
                now=dt + timedelta(minutes=2),
            )

    recent_10 = store.list_conversations(limit=10)
    assert len(recent_10) == 10
    assert recent_10[0]["conversation_id"] == "chat-15"
    assert recent_10[0]["episode_summary"] == "Latest status summary on routine checks."
    assert recent_10[0]["turn_count"] == 1
    assert recent_10[-1]["conversation_id"] == "chat-06"
    assert "chat-02" not in [c["conversation_id"] for c in recent_10]

    # Full-history sidebar listing still keeps pinned first and returns all 16.
    all_chats = store.list_conversations()
    assert len(all_chats) == 16
    assert all_chats[0]["conversation_id"] == "chat-02"

    # Search must find chat-00 even though it sits at position 16 (beyond top 10).
    hits = store.search_conversations("sqlite wal checkpoint contention", limit=8)
    assert len(hits) >= 1
    assert hits[0]["conversation_id"] == "chat-00"
    assert "sqlite wal checkpoint" in hits[0]["episode_summary"].lower()
    assert len(hits[0]["matching_snippets"]) >= 1
    assert hits[0]["score"] > 0.0

    # read_conversation returns full chronological turns + episode summary.
    full = store.read_conversation("chat-00", max_turns=40)
    assert full is not None
    assert full["conversation_id"] == "chat-00"
    assert full["episode_summary"].startswith("Resolved sqlite wal checkpoint")
    assert [t["role"] for t in full["turns"]] == ["user", "assistant"]
    assert "wal_autocheckpoint" in full["turns"][1]["content"]


def test_layer2_token_ranked_scoring_weights_stopwords_and_recency_ties(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    t0 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)

    # 1. Title hit (+5.0)
    store.append_transcript_message(
        session_id="chat-title",
        role="user",
        text="Tracking regression ticket 412 today.",
        timestamp=t0,
        title="Fixed Audio Freeze Regression",
    )

    # 2. Episode hit (+3.5)
    store.append_transcript_message(
        session_id="chat-episode",
        role="user",
        text="Looking into sound buffer underrun issues.",
        timestamp=t0 + timedelta(minutes=1),
        title="Buffer Notes",
    )
    store.append_episode(
        session_id="chat-episode",
        summary="Investigated and fixed the audio freeze inside driver callbacks.",
        now=t0 + timedelta(minutes=2),
    )

    # 3. Linked Durable Fact hit (+3.0)
    store.append_transcript_message(
        session_id="chat-fact",
        role="user",
        text="Testing Bluetooth headphones driver on workstation.",
        timestamp=t0 + timedelta(minutes=3),
        title="Workstation Devices",
    )
    store.add_or_update_entry(
        text="Bluetooth headphones audio freeze was fixed cleanly.",
        section="Environment",
        now=t0 + timedelta(minutes=3),
    )

    # 4. Raw turn hit only (+1.0)
    store.append_transcript_message(
        session_id="chat-turn",
        role="user",
        text="By the way we fixed that audio freeze earlier before lunch.",
        timestamp=t0 + timedelta(minutes=4),
        title="Lunch Sync",
    )

    # Descriptive query containing stopwords ("that", "chat", "where", "we", "the")
    hits = store.search_conversations("that chat where we fixed the audio freeze", limit=8)
    ranked_ids = [h["conversation_id"] for h in hits]
    assert ranked_ids == ["chat-title", "chat-episode", "chat-fact", "chat-turn"]
    assert hits[0]["score"] > hits[1]["score"] > hits[2]["score"] > hits[3]["score"]
    assert hits[0]["matching_snippets"][0]["excerpt"]

    # Recency breaks exact score ties cleanly.
    store2 = MemoryStore(base_dir=tmp_path / "tie")
    store2.ensure_initialized()
    store2.append_transcript_message(
        session_id="older-tie",
        role="user",
        text="Configuring Redis Sentinel failover.",
        timestamp=t0,
        title="Redis Failover",
    )
    store2.append_transcript_message(
        session_id="newer-tie",
        role="user",
        text="Configuring Redis Sentinel failover.",
        timestamp=t0 + timedelta(hours=5),
        title="Redis Failover",
    )
    tie_hits = store2.search_conversations("Redis Failover", limit=8)
    assert [h["conversation_id"] for h in tie_hits] == ["newer-tie", "older-tie"]


def test_layer3_semantic_embeddings_backfill_and_fallback(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()
    dt = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)

    # Pre-seed conversations WITHOUT embed_fn first to test lazy backfill.
    store.append_transcript_message(
        session_id="chat-infra",
        role="user",
        text="Can you check ingress routing?",
        timestamp=dt,
        title="Ingress Check",
    )
    store.append_episode(
        session_id="chat-infra",
        summary="Managed container orchestration pods on the production cluster.",
        now=dt + timedelta(minutes=1),
    )

    store.append_transcript_message(
        session_id="chat-food",
        role="user",
        text="What should we cook tonight?",
        timestamp=dt + timedelta(minutes=10),
        title="Dinner Idea",
    )
    store.append_episode(
        session_id="chat-food",
        summary="Homemade marinara sauce and spaghetti dinner recipe.",
        now=dt + timedelta(minutes=11),
    )

    store.add_or_update_entry(
        text="Stores system telemetry inside TimescaleDB metrics tables.",
        section="Projects",
        now=dt,
    )

    # Attach semantic embedder lazily -> backfill runs automatically on search.
    store.embed_fn = _keyword_axis_embedder

    # Zero keyword overlap between query ("docker") and episode ("container orchestration ...").
    hits = store.search_conversations("docker", limit=8)
    assert len(hits) == 1
    assert hits[0]["conversation_id"] == "chat-infra"
    assert hits[0]["lexical_score"] == 0.0
    assert hits[0]["semantic_score"] is not None
    assert hits[0]["semantic_score"] > 0.7

    # Embeddings persisted to memory/embeddings.json for episodes & facts only (not raw turns).
    assert store.embeddings_path.exists()
    persisted = json.loads(store.embeddings_path.read_text(encoding="utf-8"))
    keys = set(persisted["items"].keys())
    assert "episode:chat-infra" in keys
    assert "episode:chat-food" in keys
    assert any(k.startswith("fact:") for k in keys)
    assert not any(k.startswith("turn:") for k in keys)

    # search_memory matches both Facts and Episodes semantically (zero overlapping words).
    mem_hits = store.search_memory("time series metrics", limit=10)
    assert len(mem_hits) >= 1
    assert mem_hits[0]["kind"] == "fact"
    assert "TimescaleDB" in mem_hits[0]["text"]
    assert mem_hits[0]["semantic_score"] > 0.5

    # Graceful offline fallback: broken embedder must NOT raise and falls back cleanly.
    def _failing_embedder(_text: str) -> list[float]:
        raise RuntimeError("Network offline")

    store.embed_fn = _failing_embedder
    fallback_hits = store.search_conversations("container orchestration", limit=8)
    assert len(fallback_hits) >= 1
    assert fallback_hits[0]["conversation_id"] == "chat-infra"
    assert fallback_hits[0]["semantic_score"] is None
    assert fallback_hits[0]["score"] == fallback_hits[0]["lexical_score"]

    fallback_mem = store.search_memory("TimescaleDB", limit=10)
    assert len(fallback_mem) >= 1
    assert fallback_mem[0]["semantic_score"] is None
    assert fallback_mem[0]["score"] == fallback_mem[0]["lexical_score"]


async def test_consolidation_generates_and_persists_embeddings(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path, embed_fn=_keyword_axis_embedder)
    store.ensure_initialized()
    dt = datetime(2026, 11, 8, 15, 0, tzinfo=UTC)

    store.append_transcript_message(
        session_id="sess-cons",
        role="user",
        text="We set up Kubernetes pods with ingress.",
        timestamp=dt,
        title="Setup",
    )
    store.append_transcript_message(
        session_id="sess-cons",
        role="assistant",
        text="Ingress controller is healthy.",
        timestamp=dt + timedelta(seconds=10),
        title="Setup",
    )
    store.append_transcript_message(
        session_id="sess-cons",
        role="user",
        text="Remember we also store telemetry in TimescaleDB.",
        timestamp=dt + timedelta(seconds=20),
        title="Setup",
    )

    canned = json.dumps(
        {
            "summary": "Deployed Kubernetes pods and confirmed ingress routing.",
            "operations": [
                {
                    "op": "add",
                    "target_section": "Projects",
                    "source_tag": "stated",
                    "text": "Telemetry is stored in TimescaleDB.",
                }
            ],
        }
    )
    mgr = ConsolidationManager(memory=store, provider=_DummyLLMProvider(canned))
    res = await mgr.consolidate_session("sess-cons")
    assert res is True

    ep_vec = store.embedding_store.get_vector("episode:sess-cons")
    assert ep_vec is not None
    assert cosine_similarity(ep_vec, _keyword_axis_embedder("docker")) > 0.9


def test_embedding_provider_gemini_and_ollama_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Gemini provider mocked via google.genai.Client
    import google.genai as genai_mod

    fake_embedding = MagicMock()
    fake_embedding.values = [0.25] * 768
    fake_resp = MagicMock()
    fake_resp.embeddings = [fake_embedding]
    fake_client = MagicMock()
    fake_client.models.embed_content.return_value = fake_resp
    mock_client_cls = MagicMock(return_value=fake_client)

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(genai_mod, "Client", mock_client_cls)

    provider = EmbeddingProvider()
    vec = provider.embed("hello semantic world")
    assert vec is not None
    assert len(vec) == 768
    mock_client_cls.assert_called_once_with(api_key="test-key")

    # 2. Ollama fallback when Gemini fails
    fake_client.models.embed_content.side_effect = RuntimeError("Gemini quota")
    ollama_payload = json.dumps({"embeddings": [[0.5, 0.5, 0.5, 0.5]]}).encode("utf-8")

    class _FakeHttpResp(io.BytesIO):
        def __enter__(self) -> _FakeHttpResp:
            return self

        def __exit__(self, *_args: object) -> None:
            self.close()

    mock_urlopen = MagicMock(return_value=_FakeHttpResp(ollama_payload))
    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    ollama_vec = provider.embed("hello ollama fallback")
    assert ollama_vec == [0.5, 0.5, 0.5, 0.5]

    # 3. Both fail -> graceful None
    mock_urlopen.side_effect = OSError("Ollama unreachable")
    assert provider.embed("totally offline") is None


async def test_e2e_daemon_and_voice_runner_all_four_recall_tools(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path, embed_fn=_keyword_axis_embedder)
    store.ensure_initialized()

    base_dt = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
    store.append_transcript_message(
        session_id="chat-old",
        role="user",
        text="Check container pods status.",
        timestamp=base_dt,
        title="Infra Discussion",
    )
    store.append_transcript_message(
        session_id="chat-old",
        role="assistant",
        text="Run `kubectl get pods -A` to verify pod readiness.",
        timestamp=base_dt + timedelta(minutes=1),
        title="Infra Discussion",
    )
    store.append_episode(
        session_id="chat-old",
        summary="Checked container orchestration pods across staging.",
        now=base_dt + timedelta(minutes=2),
    )
    store.add_or_update_entry(
        text="Stores cluster metrics in TimescaleDB.",
        section="Projects",
        now=base_dt,
    )

    for idx in (1, 2):
        dt = base_dt + timedelta(hours=idx)
        store.append_transcript_message(
            session_id=f"chat-recent-{idx}",
            role="user",
            text=f"Recent thread {idx}",
            timestamp=dt,
            title=f"Recent Thread {idx}",
        )
        store.append_episode(
            session_id=f"chat-recent-{idx}",
            summary=f"Summary of recent thread {idx}.",
            now=dt + timedelta(minutes=1),
        )

    # Mark pre-seeded chats completed so daemon boot crash-recovery preserves their episodes.
    ConsolidationManager(memory=store, provider=_DummyLLMProvider()).save_state(
        {
            "sessions": {
                "chat-old": {
                    "status": "completed",
                    "consolidated_at": base_dt.isoformat(),
                    "last_consolidated_turn": 2,
                },
                "chat-recent-1": {
                    "status": "completed",
                    "consolidated_at": base_dt.isoformat(),
                    "last_consolidated_turn": 1,
                },
                "chat-recent-2": {
                    "status": "completed",
                    "consolidated_at": base_dt.isoformat(),
                    "last_consolidated_turn": 1,
                },
            }
        }
    )

    daemon: CleaDaemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=_DummyLLMProvider(),
        memory=store,
    )
    await daemon.start()

    backend = _FakeVoiceBackend()
    runner = VoiceRunner(
        config=VoiceConfig(silence_timeout_seconds=60.0),
        data_dir=tmp_path,
        daemon_url=f"ws://127.0.0.1:{daemon.bound_port}",
        token=daemon.token,
        backend_factory=lambda: backend,
        audio_manager=_make_fake_audio(),
        wake_word_detector=_make_fake_wake_word(),
        enable_hotkey=False,
        daemon_retry_delay=0.05,
    )

    runner_task = asyncio.create_task(runner.run_forever())
    try:
        for _ in range(40):
            if any(c.is_voice for c in daemon._connections):
                break
            await asyncio.sleep(0.05)
        assert runner._daemon_ever_connected is True

        await runner.start_call(trigger="manual")

        # 1. list_conversations
        await backend.event_queue.put(
            ToolCallEvent(call_id="tc-1", name="list_conversations", arguments={"limit": 2})
        )
        # 2. search_conversations (semantic match on "docker" -> "chat-old")
        await backend.event_queue.put(
            ToolCallEvent(
                call_id="tc-2",
                name="search_conversations",
                arguments={"query": "docker", "limit": 8},
            )
        )
        # 3. read_conversation
        await backend.event_queue.put(
            ToolCallEvent(
                call_id="tc-3",
                name="read_conversation",
                arguments={"conversation_id": "chat-old", "max_turns": 40},
            )
        )
        # 4. search_memory
        await backend.event_queue.put(
            ToolCallEvent(
                call_id="tc-4",
                name="search_memory",
                arguments={"query": "time series metrics", "limit": 10},
            )
        )
        # 5. read_conversation on missing ID handles gracefully
        await backend.event_queue.put(
            ToolCallEvent(
                call_id="tc-5",
                name="read_conversation",
                arguments={"conversation_id": "does-not-exist"},
            )
        )

        for _ in range(60):
            if len(backend.sent_tool_responses) >= 5:
                break
            await asyncio.sleep(0.05)

        by_cid = {r.call_id: r for r in backend.sent_tool_responses}
        assert set(by_cid) == {"tc-1", "tc-2", "tc-3", "tc-4", "tc-5"}

        r1 = by_cid["tc-1"]
        assert r1.is_error is False
        convs = r1.response["conversations"]
        assert [c["conversation_id"] for c in convs] == ["chat-recent-2", "chat-recent-1"]
        assert convs[0]["episode_summary"] == "Summary of recent thread 2."

        r2 = by_cid["tc-2"]
        assert r2.is_error is False
        assert r2.response["results"][0]["conversation_id"] == "chat-old"
        assert r2.response["results"][0]["semantic_score"] > 0.7

        r3 = by_cid["tc-3"]
        assert r3.is_error is False
        assert r3.response["found"] is True
        assert r3.response["conversation_id"] == "chat-old"
        assert "kubectl get pods -A" in r3.response["turns"][1]["content"]

        r4 = by_cid["tc-4"]
        assert r4.is_error is False
        assert any("TimescaleDB" in h["text"] for h in r4.response["results"])

        r5 = by_cid["tc-5"]
        assert r5.is_error is False
        assert r5.response["found"] is False
    finally:
        await runner.end_call(reason="test_done")
        runner._stop_event.set()
        await asyncio.wait_for(runner_task, timeout=3.0)
        await daemon.stop()


def test_new_voice_protocol_frames_parse_cleanly() -> None:
    req_list = parse_frame(
        json.dumps(
            {
                "frame_type": "request",
                "method": "voice.list_conversations",
                "params": {"limit": 5},
            }
        )
    )
    assert req_list.method == "voice.list_conversations"
    req_read = parse_frame(
        json.dumps(
            {
                "frame_type": "request",
                "method": "voice.read_conversation",
                "params": {"conversation_id": "c1", "max_turns": 20},
            }
        )
    )
    assert req_read.method == "voice.read_conversation"
    req_mem = parse_frame(
        json.dumps(
            {
                "frame_type": "request",
                "method": "voice.search_memory",
                "params": {"query": "q"},
            }
        )
    )
    assert req_mem.method == "voice.search_memory"
