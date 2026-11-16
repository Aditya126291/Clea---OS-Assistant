"""Tests for persistent daemon-backed timers, reminders, and Windows toast alerts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from clea.core.agent import AgentLoop
from clea.core.daemon import CleaDaemon
from clea.core.protocol import ReminderFiredEvent, TimerFiredEvent
from clea.memory.store import MemoryStore
from clea.providers.base import LLMProvider, Message, StreamChunk
from clea.tools import (
    RiskLevel,
    TimerListResult,
    TimerManager,
    ToolDefinition,
    ToolRegistry,
    cancel_timer_or_reminder,
    get_default_timer_manager,
    list_timers_and_reminders,
    parse_duration,
    parse_when,
    register_timer_tools,
    send_windows_toast_and_chime,
    set_default_timer_manager,
    set_reminder,
    set_timer,
)

# --------------------------------------------------------------------------- Mock Provider


class MockLLMProvider(LLMProvider):
    """Minimal provider stub for daemon lifecycle testing."""

    async def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str,
        response_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(type="text_delta", text="OK")
        yield StreamChunk(type="done")


# --------------------------------------------------------------------------- Duration Parsing Tests


def test_parse_duration_units() -> None:
    assert parse_duration("25m") == 1500.0
    assert parse_duration("1h 15m") == 4500.0
    assert parse_duration("90") == 90.0
    assert parse_duration(90) == 90.0
    assert parse_duration("45s") == 45.0
    assert parse_duration("10m") == 600.0
    assert parse_duration("1h 30m") == 5400.0
    assert parse_duration("25 minutes") == 1500.0
    assert parse_duration("300") == 300.0
    assert parse_duration("1.5 hours") == 5400.0
    assert parse_duration("2 days") == 172800.0
    assert parse_duration("10 seconds") == 10.0
    assert parse_duration("15 sec") == 15.0
    assert parse_duration("1 hour and 30 minutes") == 5400.0


def test_parse_duration_invalid() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        parse_duration("")

    with pytest.raises(ValueError, match="cannot be empty"):
        parse_duration("   ")

    with pytest.raises(ValueError, match="must be positive"):
        parse_duration("0")

    with pytest.raises(ValueError, match="must be positive"):
        parse_duration("-10s")

    with pytest.raises(ValueError, match="Unknown time unit"):
        parse_duration("10 foos")

    with pytest.raises(ValueError, match="Could not parse duration"):
        parse_duration("invalid-text")

    with pytest.raises(ValueError, match="cannot be boolean"):
        parse_duration(True)

    with pytest.raises(ValueError, match="cannot be boolean"):
        parse_duration(False)

    with pytest.raises(ValueError, match="unexpected tokens"):
        parse_duration("10m 5")

    with pytest.raises(ValueError, match="unexpected tokens"):
        parse_duration("hello 10m")


# --------------------------------------------------------------------------- When Parsing Tests


def test_parse_when_relative() -> None:
    base = datetime(2026, 11, 16, 12, 0, 0).astimezone()
    res = parse_when("in 10m", now=base)
    assert res == base + timedelta(minutes=10)

    res = parse_when("in 20 minutes", now=base)
    assert res == base + timedelta(minutes=20)

    res = parse_when("in 2 hours", now=base)
    assert res == base + timedelta(hours=2)

    res = parse_when("after 45s", now=base)
    assert res == base + timedelta(seconds=45)

    res = parse_when("10m", now=base)
    assert res == base + timedelta(minutes=10)


def test_parse_when_iso() -> None:
    base = datetime(2026, 11, 16, 12, 0, 0).astimezone()
    res = parse_when("2026-11-16T18:30:00", now=base)
    assert res.year == 2026
    assert res.month == 11
    assert res.day == 16
    assert res.hour == 18
    assert res.minute == 30

    res_space = parse_when("2026-11-16 18:30:00", now=base)
    assert res_space.hour == 18
    assert res_space.minute == 30


def test_parse_when_clock_today_and_tomorrow() -> None:
    # 12:00 PM reference time
    base = datetime(2026, 11, 16, 12, 0, 0).astimezone()

    # 18:30 is in the future today
    res = parse_when("18:30", now=base)
    assert res == base.replace(hour=18, minute=30, second=0, microsecond=0)
    assert res.day == 16

    # 6:30 PM is in the future today
    res = parse_when("6:30 PM", now=base)
    assert res == base.replace(hour=18, minute=30, second=0, microsecond=0)
    assert res.day == 16

    # 09:00 was earlier today -> must roll forward to tomorrow
    res_passed = parse_when("09:00", now=base)
    assert res_passed == (base + timedelta(days=1)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    assert res_passed.day == 17

    # 6:30 AM was earlier today -> rolls to tomorrow
    res_am_passed = parse_when("6:30 AM", now=base)
    assert res_am_passed.day == 17
    assert res_am_passed.hour == 6
    assert res_am_passed.minute == 30


def test_parse_when_natural_phrases() -> None:
    base = datetime(2026, 11, 16, 12, 0, 0).astimezone()

    # Preposition "at "
    res_at_1830 = parse_when("at 18:30", now=base)
    assert res_at_1830 == base.replace(hour=18, minute=30, second=0, microsecond=0)

    res_at_pm = parse_when("at 6:30 PM", now=base)
    assert res_at_pm == base.replace(hour=18, minute=30, second=0, microsecond=0)

    # Periods in am/pm
    res_dots = parse_when("6:30 p.m.", now=base)
    assert res_dots == base.replace(hour=18, minute=30, second=0, microsecond=0)

    res_dots_am = parse_when("9:00 a.m.", now=base)
    assert res_dots_am.day == 17
    assert res_dots_am.hour == 9
    assert res_dots_am.minute == 0

    # Natural words: noon and midnight
    res_noon = parse_when("noon", now=base)
    # 12:00 PM at base (12:00 PM) rolls to tomorrow
    assert res_noon.day == 17
    assert res_noon.hour == 12

    res_midnight = parse_when("at midnight", now=base)
    assert res_midnight.day == 17
    assert res_midnight.hour == 0

    # Tomorrow specifiers
    res_tmr_at = parse_when("tomorrow at 9:00 AM", now=base)
    assert res_tmr_at.day == 17
    assert res_tmr_at.hour == 9
    assert res_tmr_at.minute == 0

    res_tmr_24 = parse_when("tomorrow 18:30", now=base)
    assert res_tmr_24.day == 17
    assert res_tmr_24.hour == 18
    assert res_tmr_24.minute == 30

    res_tonight = parse_when("tonight at 8pm", now=base)
    assert res_tonight.day == 16
    assert res_tonight.hour == 20


def test_parse_when_iso_timezone_conversion() -> None:
    base = datetime(2026, 11, 16, 12, 0, 0).astimezone()
    # UTC ISO timestamp converted to local timezone
    res = parse_when("2026-11-16T18:30:00Z", now=base)
    assert res.tzinfo == base.tzinfo


def test_parse_when_invalid() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        parse_when("")

    with pytest.raises(ValueError, match="Could not parse reminder time"):
        parse_when("sometime next week")


# ------------------------------------------------ Storage & Persistence Tests


def test_storage_atomic_and_persistence(tmp_path: Path) -> None:
    store_file = tmp_path / "timers.json"
    mgr1 = TimerManager(storage_path=store_file)

    t1 = mgr1.set_timer("10m", label="Coffee")
    r1 = mgr1.set_reminder("Call doctor", "in 2 hours")

    assert store_file.exists()
    assert t1["id"] in mgr1._items
    assert r1["id"] in mgr1._items

    # Instantiate new manager from same storage
    mgr2 = TimerManager(storage_path=store_file)
    active = mgr2.list_timers_and_reminders()

    assert len(active.timers) == 1
    assert active.timers[0]["id"] == t1["id"]
    assert active.timers[0]["label"] == "Coffee"
    assert active.timers[0]["remaining_seconds"] > 0

    assert len(active.reminders) == 1
    assert active.reminders[0]["id"] == r1["id"]
    assert active.reminders[0]["message"] == "Call doctor"


@pytest.mark.asyncio
async def test_recovery_of_overdue_items(tmp_path: Path) -> None:
    store_file = tmp_path / "timers.json"
    mgr1 = TimerManager(storage_path=store_file)

    # Set timer with fires_at in the past
    past_iso = (datetime.now().astimezone() - timedelta(minutes=5)).isoformat(timespec="seconds")
    mgr1._items["tmr_overdue"] = {
        "id": "tmr_overdue",
        "type": "timer",
        "label": "Overdue Bread",
        "duration_seconds": 60,
        "created_at": past_iso,
        "fires_at": past_iso,
        "status": "active",
    }
    mgr1._items["rem_overdue"] = {
        "id": "rem_overdue",
        "type": "reminder",
        "message": "Overdue Meeting",
        "created_at": past_iso,
        "fires_at": past_iso,
        "status": "active",
    }
    mgr1._save_storage()

    # Create new manager with mocked notification & event callback
    notified: list[tuple[str, str]] = []
    events: list[Any] = []

    def mock_notifier(title: str, body: str) -> None:
        notified.append((title, body))

    def mock_on_event(evt: Any) -> None:
        events.append(evt)

    mgr2 = TimerManager(
        storage_path=store_file,
        notifier=mock_notifier,
        on_event=mock_on_event,
    )

    # Starting mgr2 should gracefully recover and fire overdue items immediately
    await mgr2.start()
    try:
        assert len(notified) == 2
        assert len(events) == 2

        # Verify items are marked fired in storage
        mgr3 = TimerManager(storage_path=store_file)
        assert mgr3._items["tmr_overdue"]["status"] == "fired"
        assert mgr3._items["rem_overdue"]["status"] == "fired"
        assert mgr3._items["tmr_overdue"].get("recovered_on_startup") is True

        # Listing returns 0 active items
        active = mgr3.list_timers_and_reminders()
        assert len(active.timers) == 0
        assert len(active.reminders) == 0
    finally:
        await mgr2.stop()


# --------------------------------------------- Background Monitor Loop Tests


@pytest.mark.asyncio
async def test_background_loop_fires_due_items(tmp_path: Path) -> None:
    store_file = tmp_path / "timers.json"
    notified: list[tuple[str, str]] = []
    events: list[Any] = []

    def mock_notifier(title: str, body: str) -> None:
        notified.append((title, body))

    def mock_on_event(evt: Any) -> None:
        events.append(evt)

    mgr = TimerManager(
        storage_path=store_file,
        notifier=mock_notifier,
        on_event=mock_on_event,
        poll_interval=0.05,
    )
    await mgr.start()
    try:
        # Schedule a 0.1s timer
        timer_info = mgr.set_timer("0.1s", label="Quick Tea")
        assert timer_info["label"] == "Quick Tea"

        # Schedule a 0.1s reminder
        rem_info = mgr.set_reminder("Quick task", "in 0.1s")
        assert rem_info["message"] == "Quick task"

        # Wait for poll loop to fire items
        for _ in range(50):
            if len(notified) >= 2:
                break
            await asyncio.sleep(0.05)

        assert len(notified) == 2
        assert len(events) == 2

        # Verify event structures
        timer_events = [e for e in events if isinstance(e, TimerFiredEvent)]
        reminder_events = [e for e in events if isinstance(e, ReminderFiredEvent)]
        assert len(timer_events) == 1
        assert timer_events[0].data.timer_id == timer_info["id"]
        assert timer_events[0].data.label == "Quick Tea"

        assert len(reminder_events) == 1
        assert reminder_events[0].data.reminder_id == rem_info["id"]
        assert reminder_events[0].data.message == "Quick task"

        # Storage status updated to fired
        active = mgr.list_timers_and_reminders()
        assert len(active.timers) == 0
        assert len(active.reminders) == 0
        assert mgr._items[timer_info["id"]]["status"] == "fired"
        assert mgr._items[rem_info["id"]]["status"] == "fired"
    finally:
        await mgr.stop()


# ------------------------------------------------ Listing & Canceling Tests


def test_listing_and_canceling(tmp_path: Path) -> None:
    store_file = tmp_path / "timers.json"
    mgr = TimerManager(storage_path=store_file)

    t1 = mgr.set_timer("5m", label="Pasta")
    t2 = mgr.set_timer("15m", label="Roast")
    r1 = mgr.set_reminder("Submit report", "in 30m")

    listing = mgr.list_timers_and_reminders()
    assert isinstance(listing, TimerListResult)
    assert len(listing.timers) == 2
    assert len(listing.reminders) == 1
    assert len(listing.all) == 3

    # Check chronological order (5m comes before 15m)
    assert listing.timers[0]["id"] == t1["id"]
    assert listing.timers[1]["id"] == t2["id"]
    assert listing.timers[0]["remaining_seconds"] <= listing.timers[1]["remaining_seconds"]

    # Cancel by exact ID
    res1 = mgr.cancel_timer_or_reminder(t1["id"])
    assert res1["cancelled"] is True
    assert res1["id"] == t1["id"]

    # Cancel by case-insensitive substring on label
    res2 = mgr.cancel_timer_or_reminder("roast")
    assert res2["cancelled"] is True
    assert res2["id"] == t2["id"]

    # Cancel by case-insensitive substring on reminder message
    res3 = mgr.cancel_timer_or_reminder("report")
    assert res3["cancelled"] is True
    assert res3["id"] == r1["id"]

    # Non-existent target
    res_none = mgr.cancel_timer_or_reminder("non-existent-thing")
    assert res_none["cancelled"] is False

    # All items now cancelled
    after_cancel = mgr.list_timers_and_reminders()
    assert len(after_cancel.timers) == 0
    assert len(after_cancel.reminders) == 0
    assert len(after_cancel.all) == 0


# ------------------------------------- Standalone & Default Module Tools


def test_standalone_functions_and_default_manager(tmp_path: Path) -> None:
    custom_mgr = TimerManager(storage_path=tmp_path / "custom_timers.json")
    set_default_timer_manager(custom_mgr)
    try:
        t = set_timer("25m", label="Pomodoro")
        r = set_reminder("Walk dog", "in 45m")
        lst = list_timers_and_reminders()

        assert len(lst.timers) == 1
        assert lst.timers[0]["id"] == t["id"]
        assert len(lst.reminders) == 1
        assert lst.reminders[0]["id"] == r["id"]

        c = cancel_timer_or_reminder(t["id"])
        assert c["cancelled"] is True
    finally:
        set_default_timer_manager(None)


# ----------------------------------------------- Tool Registry Integration


@pytest.mark.asyncio
async def test_registry_integration(tmp_path: Path) -> None:
    registry = ToolRegistry()
    mgr = TimerManager(storage_path=tmp_path / "registry_timers.json")
    register_timer_tools(registry, mgr)

    # Check tools registered with READ_ONLY risk
    definitions = {d.name: d for d in registry.list_definitions()}
    assert "set_timer" in definitions
    assert "set_reminder" in definitions
    assert "list_timers_and_reminders" in definitions
    assert "cancel_timer_or_reminder" in definitions

    assert definitions["set_timer"].risk_level == RiskLevel.READ_ONLY
    assert definitions["set_reminder"].risk_level == RiskLevel.READ_ONLY
    assert definitions["list_timers_and_reminders"].risk_level == RiskLevel.READ_ONLY
    assert definitions["cancel_timer_or_reminder"].risk_level == RiskLevel.READ_ONLY

    # Execute tools through registry
    t_res = await registry.execute("set_timer", {"duration": "10m", "label": "Tea"})
    assert t_res["label"] == "Tea"
    assert "id" in t_res

    r_res = await registry.execute(
        "set_reminder", {"message": "Stretch", "when": "in 15m"}
    )
    assert r_res["message"] == "Stretch"

    l_res = await registry.execute("list_timers_and_reminders")
    assert len(l_res["timers"]) == 1
    assert len(l_res["reminders"]) == 1

    c_res = await registry.execute("cancel_timer_or_reminder", {"target": "tea"})
    assert c_res["cancelled"] is True

    l_res2 = await registry.execute("list_timers_and_reminders")
    assert len(l_res2["timers"]) == 0


# --------------------------------------------------------------------------- Toast & Sound Safety


def test_toast_and_chime_safe_execution() -> None:
    # Test that calling send_windows_toast_and_chime never raises
    with patch("subprocess.Popen", side_effect=OSError("PowerShell unavailable")):
        send_windows_toast_and_chime("Test Title", "Test Body <with special & 'chars'>")

    with patch("subprocess.Popen") as mock_popen:
        send_windows_toast_and_chime("Title & More", "Body 'quoted' <tag>")
        # If running on Windows, Popen was called safely
        if mock_popen.called:
            assert mock_popen.call_count == 1


# --------------------------------------------- Daemon Lifespan Integration


@pytest.mark.asyncio
async def test_daemon_lifespan_timer_integration(tmp_path: Path) -> None:
    mem_dir = tmp_path / "daemon_mem"
    mem = MemoryStore(base_dir=mem_dir)
    reg = ToolRegistry()
    provider = MockLLMProvider()
    timer_mgr = TimerManager(
        storage_path=mem_dir / "timers.json",
        poll_interval=0.1,
    )

    daemon = CleaDaemon(
        agent=AgentLoop(provider=provider, registry=reg, memory=mem),
        host="127.0.0.1",
        port=0,
        token="test_token",
        token_file=mem_dir / "session.token",
        lock_file=mem_dir / "daemon.lock",
        timer_manager=timer_mgr,
    )

    # Before start, timer loop is not running
    assert timer_mgr._loop_task is None

    # After start, timer loop is running
    await daemon.start()
    try:
        assert timer_mgr._loop_task is not None
        assert not timer_mgr._loop_task.done()

        # Tools are registered on daemon agent registry
        assert "set_timer" in [d.name for d in daemon.agent.registry.list_definitions()]
    finally:
        await daemon.stop()

    # After stop, timer loop is stopped cleanly
    assert timer_mgr._loop_task is None or timer_mgr._loop_task.done()


def test_chronological_sorting_with_tz_offsets(tmp_path: Path) -> None:
    store_file = tmp_path / "timers.json"
    mgr = TimerManager(storage_path=store_file)

    now = datetime.now().astimezone()
    # Item 1: due in 2 hours
    t1_dt = now + timedelta(hours=2)
    # Item 2: due in 30 minutes
    t2_dt = now + timedelta(minutes=30)

    mgr._items["t1"] = {
        "id": "t1",
        "type": "reminder",
        "message": "Later reminder",
        "created_at": now.isoformat(timespec="seconds"),
        "fires_at": t1_dt.isoformat(timespec="seconds"),
        "status": "active",
    }
    mgr._items["t2"] = {
        "id": "t2",
        "type": "timer",
        "label": "Earlier timer",
        "duration_seconds": 1800,
        "created_at": now.isoformat(timespec="seconds"),
        "fires_at": t2_dt.isoformat(timespec="seconds"),
        "status": "active",
    }
    mgr._save_storage()

    listing = mgr.list_timers_and_reminders()
    assert len(listing.all) == 2
    # Earlier item (t2) must appear before later item (t1)
    assert listing.all[0]["id"] == "t2"
    assert listing.all[1]["id"] == "t1"


def test_cancel_priority_exact_before_substring(tmp_path: Path) -> None:
    store_file = tmp_path / "timers.json"
    mgr = TimerManager(storage_path=store_file)

    # Insert "Green Tea" first, then "Tea"
    t_green = mgr.set_timer("15m", label="Green Tea")
    t_tea = mgr.set_timer("10m", label="Tea")

    # Cancel "Tea" should match exact label "Tea", not "Green Tea"
    res = mgr.cancel_timer_or_reminder("Tea")
    assert res["cancelled"] is True
    assert res["id"] == t_tea["id"]

    # "Green Tea" must still be active
    active = mgr.list_timers_and_reminders()
    assert len(active.timers) == 1
    assert active.timers[0]["id"] == t_green["id"]


@pytest.mark.asyncio
async def test_timer_manager_start_idempotent(tmp_path: Path) -> None:
    store_file = tmp_path / "timers.json"
    mgr = TimerManager(storage_path=store_file, poll_interval=0.1)

    await mgr.start()
    try:
        initial_task = mgr._loop_task
        assert initial_task is not None
        # Call start again
        await mgr.start()
        assert mgr._loop_task is initial_task
    finally:
        await mgr.stop()


@pytest.mark.asyncio
async def test_daemon_default_timer_manager_sync(tmp_path: Path) -> None:
    mem_dir = tmp_path / "daemon_mem"
    mem = MemoryStore(base_dir=mem_dir)
    reg = ToolRegistry()
    provider = MockLLMProvider()

    daemon = CleaDaemon(
        agent=AgentLoop(provider=provider, registry=reg, memory=mem),
        host="127.0.0.1",
        port=0,
        token="test_token",
        token_file=mem_dir / "session.token",
        lock_file=mem_dir / "daemon.lock",
    )

    # In production/default setup, storage path is in data dir
    expected_path = mem_dir / "timers.json"
    assert daemon.timer_manager.storage_path == expected_path
    assert get_default_timer_manager() is daemon.timer_manager

    await daemon.start()
    try:
        assert daemon.timer_manager._loop_task is not None
    finally:
        await daemon.stop()

    assert get_default_timer_manager() is not daemon.timer_manager
