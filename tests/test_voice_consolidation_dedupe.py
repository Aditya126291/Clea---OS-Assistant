"""Tests for voice_companion consolidation deduplication, throttling, and delta slicing."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

from clea.core import auth
from clea.core.daemon import CleaDaemon, build_daemon
from clea.core.protocol import (
    TaskStatusEvent,
    VoiceEndCallParams,
    VoiceEndCallRequest,
    VoiceStartCallParams,
    VoiceStartCallRequest,
    parse_frame,
)
from clea.memory.consolidation import ConsolidationManager
from clea.memory.store import VOICE_COMPANION_SESSION_ID, MemoryStore
from clea.providers.base import LLMProvider, Message, StreamChunk


class _SlowSpyProvider(LLMProvider):
    """Fake provider that records prompts and optionally blocks on an asyncio.Event."""

    provider_name = "slow-spy"

    def __init__(self, *, release_event: asyncio.Event | None = None) -> None:
        self.call_count = 0
        self.prompts_received: list[str] = []
        self.entered_event = asyncio.Event()
        self.release_event = release_event

    async def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[Any],
        system_prompt: str,
        response_format: dict[str, Any] | None = None,
    ):
        self.call_count += 1
        self.prompts_received.append(messages[-1].content if messages else "")
        self.entered_event.set()
        if self.release_event is not None:
            await self.release_event.wait()
        payload = json.dumps(
            {
                "summary": f"Consolidated voice batch #{self.call_count}.",
                "operations": [
                    {
                        "op": "add",
                        "target_section": "Projects",
                        "text": f"Voice fact batch #{self.call_count}.",
                    }
                ],
            }
        )
        yield StreamChunk(type="text_delta", text=payload)
        yield StreamChunk(type="done")


def _append_voice_turns(
    mem: MemoryStore,
    *,
    call_id: str,
    pairs: int,
    prefix: str,
    start_dt: datetime | None = None,
) -> None:
    base = start_dt or datetime.now().astimezone()
    entries: list[tuple[str, str, datetime]] = []
    for idx in range(pairs):
        entries.append(("user", f"{prefix} user turn {idx + 1}", base + timedelta(seconds=idx * 2)))
        entries.append(
            (
                "assistant",
                f"{prefix} assistant turn {idx + 1}",
                base + timedelta(seconds=idx * 2 + 1),
            )
        )
    mem.append_voice_companion_call(call_id=call_id, entries=entries)


async def test_concurrent_schedule_consolidation_now_dedupes_to_single_task_and_activity(
    tmp_path: Path,
) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()

    release = asyncio.Event()
    provider = _SlowSpyProvider(release_event=release)
    daemon: CleaDaemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=provider,
        memory=mem,
        token_file=auth.token_path(tmp_path),
        lock_file=auth.lock_path(tmp_path),
    )
    await daemon.start()
    try:
        # Ensure startup recovery settles before appending voice turns
        if daemon._startup_task is not None:
            await asyncio.wait_for(daemon._startup_task, timeout=2.0)

        _append_voice_turns(mem, call_id="vc-conc-1", pairs=3, prefix="Concurrent")
        provider.call_count = 0
        provider.entered_event.clear()

        url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ws:
            tasks = [
                daemon._schedule_consolidation_now(VOICE_COMPANION_SESSION_ID) for _ in range(5)
            ]
            active_tasks = [t for t in tasks if t is not None]
            assert len(active_tasks) == 5
            assert len({id(t) for t in active_tasks}) == 1

            await asyncio.wait_for(provider.entered_event.wait(), timeout=2.0)
            assert provider.call_count == 1

            # Also verify direct ConsolidationManager.consolidate_session coalesces while in-flight
            extra_statuses: list[str] = []

            async def _dup_cb(st: str, det: str | None = None) -> None:
                extra_statuses.append(st)

            dup_ok = await daemon.consolidation.consolidate_session(
                VOICE_COMPANION_SESSION_ID,
                on_task_status=_dup_cb,
            )
            assert dup_ok is True
            assert extra_statuses == []

            release.set()
            await asyncio.wait_for(asyncio.gather(*active_tasks), timeout=2.0)

            events: list[TaskStatusEvent] = []
            deadline = asyncio.get_running_loop().time() + 1.0
            while asyncio.get_running_loop().time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.2)
                except TimeoutError:
                    break
                frame = parse_frame(raw)
                if (
                    isinstance(frame, TaskStatusEvent)
                    and frame.data.task_id == f"cons-{VOICE_COMPANION_SESSION_ID}"
                ):
                    events.append(frame)

            assert len(events) == 2
            assert [ev.data.status for ev in events] == ["running", "succeeded"]
            assert events[0].data.title == "Consolidating voice memory"
            assert events[1].data.title == "Consolidating voice memory"

        assert provider.call_count == 1
        bg_tasks = mem.load_background_tasks()
        vc_entries = [k for k in bg_tasks if VOICE_COMPANION_SESSION_ID in k]
        assert len(vc_entries) == 1
        assert bg_tasks["cons-voice_companion"]["title"] == "Consolidating voice memory"
        assert bg_tasks["cons-voice_companion"]["status"] == "succeeded"
    finally:
        release.set()
        await daemon.stop()


async def test_concurrent_direct_consolidate_session_coalesces(tmp_path: Path) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    _append_voice_turns(mem, call_id="vc-direct-1", pairs=3, prefix="Direct")

    release = asyncio.Event()
    provider = _SlowSpyProvider(release_event=release)
    manager = ConsolidationManager(memory=mem, provider=provider, min_user_turns=2)

    statuses: list[tuple[str, str | None]] = []

    async def _cb(st: str, det: str | None = None) -> None:
        statuses.append((st, det))

    async def _release_soon() -> None:
        await asyncio.wait_for(provider.entered_event.wait(), timeout=2.0)
        release.set()

    releaser = asyncio.create_task(_release_soon())
    results = await asyncio.gather(
        *[
            manager.consolidate_session(VOICE_COMPANION_SESSION_ID, on_task_status=_cb)
            for _ in range(5)
        ]
    )
    await releaser

    assert results == [True] * 5
    assert provider.call_count == 1
    assert statuses == [("running", None), ("succeeded", None)]


async def test_voice_companion_debounce_and_thresholds(tmp_path: Path) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    provider = _SlowSpyProvider()
    daemon: CleaDaemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=provider,
        memory=mem,
        token_file=auth.token_path(tmp_path),
        lock_file=auth.lock_path(tmp_path),
    )
    daemon.consolidation.min_user_turns = 1

    # Seed initial consolidated checkpoint at idx=2, 60 seconds ago
    now = datetime.now().astimezone()
    _append_voice_turns(mem, call_id="vc-init", pairs=1, prefix="Init")
    recent_iso = (now - timedelta(seconds=60)).isoformat()
    daemon.consolidation.save_state(
        {
            "last_consolidated_at": recent_iso,
            "sessions": {
                VOICE_COMPANION_SESSION_ID: {
                    "status": "completed",
                    "consolidated_at": recent_iso,
                    "last_consolidated_at": recent_iso,
                    "last_consolidated_turn": 2,
                    "last_consolidated_turn_count": 2,
                    "last_consolidated_message_idx": 2,
                }
            },
        }
    )

    # 1. Short 2-message call within 5-minute window -> MUST NOT schedule
    _append_voice_turns(mem, call_id="vc-short-1", pairs=1, prefix="Short1")
    assert daemon._schedule_consolidation_now(VOICE_COMPANION_SESSION_ID) is None
    assert len(daemon._consolidation_tasks) == 0
    assert mem.load_background_tasks() == {}
    assert provider.call_count == 0

    # 2. Another 2-message call (total delta = 4 < 6) within 5-minute window -> STILL skipped
    _append_voice_turns(mem, call_id="vc-short-2", pairs=1, prefix="Short2")
    assert daemon._schedule_consolidation_now(VOICE_COMPANION_SESSION_ID) is None
    assert mem.load_background_tasks() == {}
    assert provider.call_count == 0

    # 3. Third 2-message call (total delta = 6 >= 6) within 5-minute window -> TRIGGERS
    _append_voice_turns(mem, call_id="vc-short-3", pairs=1, prefix="Short3")
    t_batch = daemon._schedule_consolidation_now(VOICE_COMPANION_SESSION_ID)
    assert t_batch is not None
    await asyncio.wait_for(t_batch, timeout=2.0)
    assert provider.call_count == 1
    assert mem.load_background_tasks()["cons-voice_companion"]["title"] == (
        "Consolidating voice memory"
    )
    state_after_batch = daemon.consolidation.load_state()["sessions"][VOICE_COMPANION_SESSION_ID]
    assert state_after_batch["last_consolidated_message_idx"] == 8

    # 4. Unchanged state even after > 300s -> MUST NOT consolidate (< 2 new messages)
    stale_iso = (datetime.now().astimezone() - timedelta(seconds=305)).isoformat()
    state_obj = daemon.consolidation.load_state()
    state_obj["sessions"][VOICE_COMPANION_SESSION_ID]["consolidated_at"] = stale_iso
    state_obj["sessions"][VOICE_COMPANION_SESSION_ID]["last_consolidated_at"] = stale_iso
    daemon.consolidation.save_state(state_obj)

    assert daemon._schedule_consolidation_now(VOICE_COMPANION_SESSION_ID) is None
    # Single message (< 2 new messages) even after > 300s -> STILL skipped
    mem.append_voice_companion_call(
        call_id="vc-single",
        entries=[("user", "One lonely message", datetime.now().astimezone())],
    )
    assert daemon._schedule_consolidation_now(VOICE_COMPANION_SESSION_ID) is None
    assert provider.call_count == 1

    # 5. Adding 1 more message (now 2 new messages) with >= 300s elapsed -> TRIGGERS
    mem.append_voice_companion_call(
        call_id="vc-single-reply",
        entries=[("assistant", "Reply making 2 new messages", datetime.now().astimezone())],
    )
    t_elapsed = daemon._schedule_consolidation_now(VOICE_COMPANION_SESSION_ID)
    assert t_elapsed is not None
    await asyncio.wait_for(t_elapsed, timeout=2.0)
    assert provider.call_count == 2
    state_after_elapsed = daemon.consolidation.load_state()["sessions"][VOICE_COMPANION_SESSION_ID]
    assert state_after_elapsed["last_consolidated_message_idx"] == 10

    # 6. Short 2-message call within 5-minute window is skipped during runtime,
    #    then flushed on daemon.stop() with force=True!
    _append_voice_turns(mem, call_id="vc-shutdown", pairs=1, prefix="ShutdownFlush")
    assert daemon._schedule_consolidation_now(VOICE_COMPANION_SESSION_ID) is None
    assert provider.call_count == 2

    await daemon.stop()
    assert provider.call_count == 3
    state_after_stop = daemon.consolidation.load_state()["sessions"][VOICE_COMPANION_SESSION_ID]
    assert state_after_stop["last_consolidated_message_idx"] == 12
    assert "ShutdownFlush user turn 1" in provider.prompts_received[-1]
    assert "Short1 user turn 1" not in provider.prompts_received[-1]


async def test_incremental_delta_slicing_and_state_persistence(tmp_path: Path) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    provider = _SlowSpyProvider()
    manager = ConsolidationManager(memory=mem, provider=provider, min_user_turns=2)

    _append_voice_turns(mem, call_id="vc-delta-1", pairs=3, prefix="AlphaPhase")
    ok1 = await manager.consolidate_session(VOICE_COMPANION_SESSION_ID)
    assert ok1 is True
    assert provider.call_count == 1
    assert "AlphaPhase user turn 1" in provider.prompts_received[0]
    assert "AlphaPhase user turn 3" in provider.prompts_received[0]

    state1 = manager.load_state()["sessions"][VOICE_COMPANION_SESSION_ID]
    assert state1["status"] == "completed"
    assert state1["last_consolidated_message_idx"] == 6
    assert state1["last_consolidated_turn_count"] == 6
    assert state1["last_consolidated_turn"] == 6
    assert isinstance(state1["last_consolidated_at"], str) and state1["last_consolidated_at"]

    # Append 6 more messages (3 pairs) and consolidate again
    _append_voice_turns(mem, call_id="vc-delta-2", pairs=3, prefix="BetaPhase")
    ok2 = await manager.consolidate_session(VOICE_COMPANION_SESSION_ID)
    assert ok2 is True
    assert provider.call_count == 2

    second_prompt = provider.prompts_received[1]
    assert "BetaPhase user turn 1" in second_prompt
    assert "BetaPhase user turn 3" in second_prompt
    assert "AlphaPhase" not in second_prompt

    state2 = manager.load_state()["sessions"][VOICE_COMPANION_SESSION_ID]
    assert state2["last_consolidated_message_idx"] == 12
    assert state2["last_consolidated_turn_count"] == 12
    assert state2["last_consolidated_turn"] == 12

    # Crash recovery must see voice_companion is fully caught up and do zero extra LLM calls
    recovered = await manager.run_crash_recovery()
    assert recovered == 0
    assert provider.call_count == 2


async def test_activity_title_differentiates_voice_companion_from_chat_sessions(
    tmp_path: Path,
) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    provider = _SlowSpyProvider()
    daemon: CleaDaemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=provider,
        memory=mem,
        token_file=auth.token_path(tmp_path),
        lock_file=auth.lock_path(tmp_path),
    )

    assert (
        daemon._format_session_consolidation_title(VOICE_COMPANION_SESSION_ID)
        == "Consolidating voice memory"
    )
    mem.append_transcript_message(
        session_id="chat-title-test",
        role="user",
        text="Hello",
        timestamp=datetime(2026, 11, 14, 15, 42, 0).astimezone(),
    )
    assert (
        daemon._format_session_consolidation_title("chat-title-test")
        == "Summarising session 15:42"
    )


async def test_voice_end_call_canonicalizes_activity_and_accumulates_short_call_deltas(
    tmp_path: Path,
) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    provider = _SlowSpyProvider()
    daemon: CleaDaemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=provider,
        memory=mem,
        token_file=auth.token_path(tmp_path),
        lock_file=auth.lock_path(tmp_path),
    )
    daemon.consolidation.min_user_turns = 2
    await daemon.start()

    async def _rpc(ws: Any, req: Any) -> Any:
        await ws.send(req.model_dump_json())
        while True:
            frame = parse_frame(await asyncio.wait_for(ws.recv(), timeout=5.0))
            if getattr(frame, "frame_type", None) == "response" and frame.id == req.id:
                return frame

    try:
        url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ws:
            # Seed Call 1 and consolidate it so debounce window is active (< 300s)
            s1 = await _rpc(
                ws, VoiceStartCallRequest(params=VoiceStartCallParams(trigger="manual"))
            )
            c1 = s1.result["call_id"]
            now = datetime.now().astimezone()
            e1 = await _rpc(
                ws,
                VoiceEndCallRequest(
                    params=VoiceEndCallParams(
                        call_id=c1,
                        reason="user_done",
                        transcript=[
                            {"role": "user", "text": "Seed turn 1", "timestamp": now.isoformat()},
                            {
                                "role": "assistant",
                                "text": "Seed reply 1",
                                "timestamp": (now + timedelta(seconds=1)).isoformat(),
                            },
                            {
                                "role": "user",
                                "text": "Seed turn 2",
                                "timestamp": (now + timedelta(seconds=2)).isoformat(),
                            },
                            {
                                "role": "assistant",
                                "text": "Seed reply 2",
                                "timestamp": (now + timedelta(seconds=3)).isoformat(),
                            },
                        ],
                    )
                ),
            )
            assert e1.ok
            t1 = daemon._consolidation_tasks.get(VOICE_COMPANION_SESSION_ID)
            if t1 is not None:
                await asyncio.wait_for(t1, timeout=2.0)
            assert provider.call_count == 1

            # Call 2: 4 messages (delta=4 < 6) -> skipped during call-end
            s2 = await _rpc(
                ws, VoiceStartCallRequest(params=VoiceStartCallParams(trigger="manual"))
            )
            c2 = s2.result["call_id"]
            e2 = await _rpc(
                ws,
                VoiceEndCallRequest(
                    params=VoiceEndCallParams(
                        call_id=c2,
                        reason="user_done",
                        transcript=[
                            {"role": "user", "text": "Carryover topic alpha"},
                            {"role": "assistant", "text": "Noted alpha"},
                            {"role": "user", "text": "Carryover topic beta"},
                            {"role": "assistant", "text": "Noted beta"},
                        ],
                    )
                ),
            )
            assert e2.ok
            await asyncio.sleep(0.05)
            assert provider.call_count == 1

            # Call 3: 2 messages (delta=6 >= 6) -> MUST trigger across Call 2 + Call 3 combined!
            s3 = await _rpc(
                ws, VoiceStartCallRequest(params=VoiceStartCallParams(trigger="manual"))
            )
            c3 = s3.result["call_id"]
            e3 = await _rpc(
                ws,
                VoiceEndCallRequest(
                    params=VoiceEndCallParams(
                        call_id=c3,
                        reason="user_done",
                        transcript=[
                            {"role": "user", "text": "Trigger topic gamma"},
                            {"role": "assistant", "text": "Noted gamma"},
                        ],
                    )
                ),
            )
            assert e3.ok
            t3 = daemon._consolidation_tasks.get(VOICE_COMPANION_SESSION_ID)
            assert t3 is not None
            await asyncio.wait_for(t3, timeout=2.0)

        assert provider.call_count == 2
        latest_prompt = provider.prompts_received[-1]
        assert "Carryover topic alpha" in latest_prompt
        assert "Carryover topic beta" in latest_prompt
        assert "Trigger topic gamma" in latest_prompt
        assert "Seed turn 1" not in latest_prompt

        bg_tasks = mem.load_background_tasks()
        assert list(bg_tasks.keys()) == ["cons-voice_companion"]
        assert bg_tasks["cons-voice_companion"]["title"] == "Consolidating voice memory"
        assert bg_tasks["cons-voice_companion"]["status"] == "succeeded"
    finally:
        await daemon.stop()


async def test_daemon_stop_shields_already_running_idle_consolidation(tmp_path: Path) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    release = asyncio.Event()
    provider = _SlowSpyProvider(release_event=release)
    daemon: CleaDaemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=provider,
        memory=mem,
        token_file=auth.token_path(tmp_path),
        lock_file=auth.lock_path(tmp_path),
    )
    daemon.consolidation.min_user_turns = 1
    daemon.consolidation_idle_seconds = 0.02
    await daemon.start()
    if daemon._startup_task is not None:
        await asyncio.wait_for(daemon._startup_task, timeout=2.0)

    _append_voice_turns(mem, call_id="vc-idle-stop", pairs=3, prefix="IdleShield")
    daemon._reset_session_idle_timer(VOICE_COMPANION_SESSION_ID)
    await asyncio.wait_for(provider.entered_event.wait(), timeout=2.0)
    assert VOICE_COMPANION_SESSION_ID not in daemon._idle_tasks

    async def _release_soon() -> None:
        await asyncio.sleep(0.05)
        release.set()

    releaser = asyncio.create_task(_release_soon())
    await daemon.stop()
    await releaser

    state = daemon.consolidation.load_state()["sessions"][VOICE_COMPANION_SESSION_ID]
    assert state["status"] == "completed"
    assert state["last_consolidated_message_idx"] == 6

