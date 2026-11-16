"""Persistent daemon-backed timers, reminders, and Windows toast alerts."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import json
import logging
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from clea.core.protocol import (
    BaseEvent,
    ReminderFiredData,
    ReminderFiredEvent,
    TimerFiredData,
    TimerFiredEvent,
)
from clea.paths import default_data_dir
from clea.tools.registry import RiskLevel, ToolRegistry

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- duration parsing


_DURATION_UNIT_MULTIPLIERS: dict[str, float] = {
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "d": 86400.0,
    "day": 86400.0,
    "days": 86400.0,
    "w": 604800.0,
    "week": 604800.0,
    "weeks": 604800.0,
}


def parse_duration(duration: str | int | float) -> float:
    """Parse flexible duration strings into total seconds.

    Supports: "45s", "10m", "1h 30m", "25 minutes", "1.5 hours", "300", 90.
    """
    if isinstance(duration, bool):
        raise ValueError(f"Duration cannot be boolean, got {duration!r}")

    if isinstance(duration, (int, float)):
        val = float(duration)
        if val <= 0:
            raise ValueError(f"Duration must be positive, got {duration}")
        return val

    clean_str = str(duration).strip()
    if not clean_str:
        raise ValueError("Duration cannot be empty.")

    # 1. Plain numeric seconds (e.g. "300", "90", "45.5")
    parsed_numeric: float | None = None
    try:
        parsed_numeric = float(clean_str)
    except ValueError:
        pass

    if parsed_numeric is not None:
        if parsed_numeric <= 0:
            raise ValueError(f"Duration must be positive, got {duration!r}")
        return parsed_numeric

    # 2. Extract number + unit patterns (e.g. "1h 30m", "25 minutes")
    matches = re.findall(r"(-?\d+(?:\.\d+)?)\s*([a-zA-Z]+)", clean_str)
    if not matches:
        raise ValueError(f"Could not parse duration: {duration!r}")

    # Check for unmatched words or stray numbers without units
    residual = re.sub(r"(-?\d+(?:\.\d+)?)\s*([a-zA-Z]+)", "", clean_str)
    residual_words = [
        w for w in re.split(r"[\s,]+", residual.strip()) if w and w.lower() not in {"and", "&"}
    ]
    if residual_words:
        raise ValueError(
            f"Could not parse duration: unexpected tokens {residual_words!r} in {duration!r}"
        )

    total_seconds = 0.0
    for val_str, unit_str in matches:
        val_float = float(val_str)
        if val_float <= 0:
            raise ValueError(f"Duration must be positive, got {duration!r}")
        unit_lower = unit_str.lower()
        if unit_lower in _DURATION_UNIT_MULTIPLIERS:
            total_seconds += val_float * _DURATION_UNIT_MULTIPLIERS[unit_lower]
        else:
            raise ValueError(f"Unknown time unit '{unit_str}' in duration {duration!r}")

    if total_seconds <= 0:
        raise ValueError(f"Duration must be positive, got {duration!r}")

    return total_seconds


def parse_when(when: str, now: datetime | None = None) -> datetime:
    """Parse a reminder time string into an aware datetime in the local timezone.

    Supports:
    - Relative offsets: "in 20 minutes", "in 2 hours", "in 10m", "in 45s", "after 30m"
    - Local clock times: "18:30", "6:30 PM", "9am", "at 6:30 PM", "at 18:30", "at 9:00 AM"
    - Natural anchors: "noon", "at noon", "midnight", "tomorrow", "tonight at 8pm"
    - ISO timestamps: "2026-11-16T18:30:00", "2026-11-16 18:30:00", "2026-11-16T18:30:00Z"
    - Plain duration strings: "20 minutes", "10m"
    """
    if now is None:
        now = datetime.now().astimezone()
    elif now.tzinfo is None:
        now = now.astimezone()

    clean = str(when).strip()
    if not clean:
        raise ValueError("Reminder time 'when' cannot be empty.")

    # 1. Relative offset prefixed with "in " or "after "
    if re.match(r"^(in|after)\s+", clean, re.IGNORECASE):
        offset_str = re.sub(r"^(in|after)\s+", "", clean, flags=re.IGNORECASE).strip()
        secs = parse_duration(offset_str)
        return now + timedelta(seconds=secs)

    # 2. ISO timestamp format
    iso_clean = clean.replace(" ", "T")
    try:
        dt = datetime.fromisoformat(iso_clean)
        if dt.tzinfo is not None:
            return dt.astimezone(now.tzinfo)
        return dt.replace(tzinfo=now.tzinfo)
    except ValueError:
        pass

    # Normalize optional leading "at ", "on "
    normalized = re.sub(r"^(at|on)\s+", "", clean, flags=re.IGNORECASE).strip()

    # Check for day specifiers: "tomorrow", "tonight", "today"
    day_offset = 0
    if re.match(r"^tomorrow(\s+(at\s+)?|\s+|$)", normalized, re.IGNORECASE):
        day_offset = 1
        normalized = re.sub(
            r"^tomorrow(\s+(at\s+)?|\s*)", "", normalized, flags=re.IGNORECASE
        ).strip()
        if not normalized:
            normalized = "9:00 AM"
    elif re.match(r"^today(\s+(at\s+)?|\s+|$)", normalized, re.IGNORECASE):
        normalized = re.sub(
            r"^today(\s+(at\s+)?|\s*)", "", normalized, flags=re.IGNORECASE
        ).strip()
    elif re.match(r"^tonight(\s+(at\s+)?|\s+|$)", normalized, re.IGNORECASE):
        normalized = re.sub(
            r"^tonight(\s+(at\s+)?|\s*)", "", normalized, flags=re.IGNORECASE
        ).strip()
        if not normalized:
            normalized = "8:00 PM"

    # Normalize words like "noon", "midnight"
    if normalized.lower() == "noon":
        normalized = "12:00 PM"
    elif normalized.lower() == "midnight":
        normalized = "12:00 AM"
        if day_offset == 0:
            day_offset = 1

    # Normalize a.m. / p.m. with periods
    normalized = re.sub(r"\b([ap])\.m\.", r"\1m", normalized, flags=re.IGNORECASE)

    # 3. 12-hour clock time (e.g. "6:30 PM", "6:30pm", "9 AM", "9am", "11:45 pm")
    m_12 = re.match(
        r"^(\d{1,2})(?::(\d{2}))?(?::(\d{2}))?\s*(am|pm)$",
        normalized,
        re.IGNORECASE,
    )
    if m_12:
        hour = int(m_12.group(1))
        minute = int(m_12.group(2) or 0)
        second = int(m_12.group(3) or 0)
        meridiem = m_12.group(4).lower()
        if not (1 <= hour <= 12 and 0 <= minute <= 59 and 0 <= second <= 59):
            raise ValueError(f"Invalid 12-hour clock time: {when!r}")
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0

        target_date = now.date() + timedelta(days=day_offset)
        target = datetime(
            target_date.year,
            target_date.month,
            target_date.day,
            hour,
            minute,
            second,
            tzinfo=now.tzinfo,
        )
        if day_offset == 0 and target <= now:
            next_date = target_date + timedelta(days=1)
            target = datetime(
                next_date.year,
                next_date.month,
                next_date.day,
                hour,
                minute,
                second,
                tzinfo=now.tzinfo,
            )
        return target

    # 4. 24-hour clock time (e.g. "18:30", "18:30:00", "08:15")
    m_24 = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", normalized)
    if m_24:
        hour = int(m_24.group(1))
        minute = int(m_24.group(2))
        second = int(m_24.group(3) or 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
            raise ValueError(f"Invalid 24-hour clock time: {when!r}")

        target_date = now.date() + timedelta(days=day_offset)
        target = datetime(
            target_date.year,
            target_date.month,
            target_date.day,
            hour,
            minute,
            second,
            tzinfo=now.tzinfo,
        )
        if day_offset == 0 and target <= now:
            next_date = target_date + timedelta(days=1)
            target = datetime(
                next_date.year,
                next_date.month,
                next_date.day,
                hour,
                minute,
                second,
                tzinfo=now.tzinfo,
            )
        return target

    # 5. Plain duration fallback (e.g. "20 minutes", "10m")
    try:
        secs = parse_duration(clean)
        return now + timedelta(seconds=secs)
    except ValueError:
        pass

    raise ValueError(f"Could not parse reminder time: {when!r}")


# --------------------------------------------------------------------------- toast & chime


def _xml_escape_toast(text: str) -> str:
    """XML-escape special characters for Windows Toast notification XML."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _send_powershell_toast(title: str, body: str) -> None:
    """Send a Windows desktop Toast notification via PowerShell WinRT with XML-escaped content."""
    escaped_title = _xml_escape_toast(title).replace("'", "''")
    escaped_body = _xml_escape_toast(body).replace("'", "''")

    ps_script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] | Out-Null\n"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
        "ContentType = WindowsRuntime] | Out-Null\n"
        "$xml = [Windows.Data.Xml.Dom.XmlDocument]::new()\n"
        "$toastXml = '<toast><visual><binding template=''ToastText02''>' +\n"
        f"  '<text id=''1''>{escaped_title}</text>' +\n"
        f"  '<text id=''2''>{escaped_body}</text>' +\n"
        "  '</binding></visual></toast>'\n"
        "$xml.LoadXml($toastXml)\n"
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)\n"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Clea').Show($toast)\n"
    )
    encoded_cmd = base64.b64encode(ps_script.encode("utf-16le")).decode("ascii")

    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded_cmd,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )


def send_windows_toast_and_chime(title: str, body: str) -> None:
    """Send a non-blocking Windows desktop toast alert and gentle chime.

    Wrapped in safe exception handling so notification failures never crash.
    """
    # 1. Gentle chime
    if sys.platform == "win32":
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception as exc:
            logger.debug("Sound notification chime failed: %s", exc)

    # 2. Non-blocking toast notification
    if sys.platform == "win32":
        try:
            _send_powershell_toast(title, body)
        except Exception as exc:
            logger.warning("Windows desktop toast notification failed: %s", exc)


# --------------------------------------------------------------------------- models & result


class TimerListResult(dict[str, Any]):
    """Result container for list_timers_and_reminders with property access."""

    @property
    def timers(self) -> list[dict[str, Any]]:
        return self.get("timers", [])

    @property
    def reminders(self) -> list[dict[str, Any]]:
        return self.get("reminders", [])

    @property
    def all(self) -> list[dict[str, Any]]:
        return self.get("all", [])


# --------------------------------------------------------------------------- TimerManager


class TimerManager:
    """Persistent timer and reminder manager backed by JSON storage and an async monitor loop."""

    def __init__(
        self,
        storage_path: Path | None = None,
        *,
        on_event: Callable[[BaseEvent], Any] | None = None,
        notifier: Callable[[str, str], Any] | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        self.storage_path = (storage_path or (default_data_dir() / "timers.json")).resolve()
        self.on_event = on_event
        self.notifier = notifier or send_windows_toast_and_chime
        self.poll_interval = poll_interval
        self._items: dict[str, dict[str, Any]] = {}
        self._loop_task: asyncio.Task[None] | None = None
        self._stopped = False
        self._lock = asyncio.Lock()
        self._load_storage()

    # ------------------------------------------------------------------ storage

    def _load_storage(self) -> None:
        """Load stored items from disk."""
        if not self.storage_path.exists():
            self._items = {}
            return
        try:
            with self.storage_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            items_list = data.get("items", [])
            self._items = {item["id"]: item for item in items_list if "id" in item}
        except Exception as exc:
            logger.warning("Failed to load timers from %s: %s", self.storage_path, exc)
            self._items = {}

    def _save_storage(self) -> None:
        """Atomic write-and-replace of items to disk."""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "items": list(self._items.values()),
        }
        tmp_file = self.storage_path.with_suffix(f".tmp.{uuid.uuid4().hex}")
        try:
            with tmp_file.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, self.storage_path)
        finally:
            if tmp_file.exists():
                with contextlib.suppress(Exception):
                    tmp_file.unlink()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Start the background monitor loop and fire overdue items immediately."""
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._stopped = False
        self._load_storage()
        # Fire overdue items missed during downtime
        await self._check_due_items(graceful_recovery=True)
        self._loop_task = asyncio.create_task(
            self._run_monitor_loop(),
            name="clea-timer-manager",
        )

    async def stop(self) -> None:
        """Stop the background monitor loop cleanly."""
        self._stopped = True
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
        self._loop_task = None

    async def _run_monitor_loop(self) -> None:
        """Background polling loop checking for due items every poll_interval."""
        try:
            while not self._stopped:
                await asyncio.sleep(self.poll_interval)
                await self._check_due_items()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Unhandled error in TimerManager monitor loop: %s", exc, exc_info=True)

    async def _check_due_items(self, *, graceful_recovery: bool = False) -> list[dict[str, Any]]:
        """Check all active items and fire any that are due."""
        now = datetime.now().astimezone()
        now_ts = now.timestamp()
        due_items: list[dict[str, Any]] = []

        async with self._lock:
            for item in list(self._items.values()):
                if item.get("status") != "active":
                    continue
                try:
                    fires_at_dt = datetime.fromisoformat(item["fires_at"])
                    if fires_at_dt.tzinfo is None:
                        fires_at_dt = fires_at_dt.replace(tzinfo=now.tzinfo)
                    if fires_at_dt.timestamp() <= now_ts:
                        due_items.append(item)
                except Exception as exc:
                    logger.warning(
                        "Could not evaluate fires_at for item %s: %s",
                        item.get("id"),
                        exc,
                    )

            for item in due_items:
                item["status"] = "fired"
                item["fired_at"] = now.isoformat(timespec="seconds")
                if graceful_recovery:
                    item["recovered_on_startup"] = True

            if due_items:
                self._save_storage()

        # Fire notifications and events outside the lock
        for item in due_items:
            await self._trigger_fired_item(item, now)

        return due_items

    async def _trigger_fired_item(self, item: dict[str, Any], now: datetime) -> None:
        """Send desktop alert and emit event on Clea's daemon event bus."""
        is_timer = item.get("type") == "timer"
        now_iso = now.isoformat(timespec="seconds")

        # 1. Desktop alert notification
        if is_timer:
            label = item.get("label", "Timer")
            dur = item.get("duration_seconds", 0)
            title = f"Timer Finished: {label}"
            body = f"Your {label} timer ({int(dur)}s) has ended."
        else:
            msg = item.get("message", "Reminder")
            title = "Reminder"
            body = msg

        try:
            if self.notifier is not None:
                res = self.notifier(title, body)
                if inspect.isawaitable(res):
                    await res
        except Exception as exc:
            logger.warning("Notification failure for %s: %s", item.get("id"), exc)

        # 2. Emit structured event on daemon event bus
        if is_timer:
            event: BaseEvent = TimerFiredEvent(
                data=TimerFiredData(
                    timer_id=item["id"],
                    label=item.get("label", "Timer"),
                    duration_seconds=float(item.get("duration_seconds", 0)),
                    fires_at=item["fires_at"],
                    fired_at=now_iso,
                )
            )
        else:
            event = ReminderFiredEvent(
                data=ReminderFiredData(
                    reminder_id=item["id"],
                    message=item.get("message", ""),
                    due_at=item["fires_at"],
                    fired_at=now_iso,
                )
            )

        if self.on_event is not None:
            try:
                res = self.on_event(event)
                if inspect.isawaitable(res):
                    await res
            except Exception as exc:
                logger.warning("Failed to emit fired event for %s: %s", item.get("id"), exc)

    # ------------------------------------------------------------------ tool operations

    def set_timer(self, duration: str | int | float, label: str = "Timer") -> dict[str, Any]:
        """Schedule a countdown timer."""
        secs = parse_duration(duration)
        now = datetime.now().astimezone()
        fires_at_dt = now + timedelta(seconds=secs)
        fires_at = fires_at_dt.isoformat(timespec="seconds")
        clean_label = str(label).strip() or "Timer"
        if float(secs).is_integer():
            duration_value: int | float = int(round(secs))
        else:
            duration_value = round(secs, 2)

        timer_id = f"tmr_{uuid.uuid4().hex[:8]}"
        item: dict[str, Any] = {
            "id": timer_id,
            "type": "timer",
            "label": clean_label,
            "duration_seconds": duration_value,
            "created_at": now.isoformat(timespec="seconds"),
            "fires_at": fires_at,
            "status": "active",
        }
        self._items[timer_id] = item
        self._save_storage()

        return {
            "id": timer_id,
            "label": clean_label,
            "duration_seconds": duration_value,
            "fires_at": fires_at,
        }

    def set_reminder(self, message: str, when: str) -> dict[str, Any]:
        """Schedule a reminder at a relative offset, clock time, or ISO timestamp."""
        clean_msg = str(message).strip()
        if not clean_msg:
            raise ValueError("Reminder message cannot be empty.")

        now = datetime.now().astimezone()
        fires_at_dt = parse_when(when, now=now)
        fires_at = fires_at_dt.isoformat(timespec="seconds")

        reminder_id = f"rem_{uuid.uuid4().hex[:8]}"
        item: dict[str, Any] = {
            "id": reminder_id,
            "type": "reminder",
            "message": clean_msg,
            "created_at": now.isoformat(timespec="seconds"),
            "fires_at": fires_at,
            "status": "active",
        }
        self._items[reminder_id] = item
        self._save_storage()

        return {
            "id": reminder_id,
            "message": clean_msg,
            "fires_at": fires_at,
            "due_at": fires_at,
        }

    def list_timers_and_reminders(self) -> TimerListResult:
        """Return all active timers and upcoming reminders sorted chronologically."""
        now = datetime.now().astimezone()
        now_ts = now.timestamp()

        timers: list[dict[str, Any]] = []
        reminders: list[dict[str, Any]] = []

        for item in self._items.values():
            if item.get("status") != "active":
                continue
            try:
                dt = datetime.fromisoformat(item["fires_at"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=now.tzinfo)
                rem_sec = max(0, int(round(dt.timestamp() - now_ts)))
            except Exception:
                rem_sec = 0

            entry = dict(item)
            entry["remaining_seconds"] = rem_sec

            if item.get("type") == "timer":
                timers.append(entry)
            else:
                reminders.append(entry)

        def _sort_key(item: dict[str, Any]) -> float:
            try:
                dt = datetime.fromisoformat(item["fires_at"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=now.tzinfo)
                return dt.timestamp()
            except Exception:
                return 0.0

        timers.sort(key=_sort_key)
        reminders.sort(key=_sort_key)
        all_items = sorted(timers + reminders, key=_sort_key)

        return TimerListResult(
            {
                "timers": timers,
                "reminders": reminders,
                "all": all_items,
                "count": len(all_items),
            }
        )

    def cancel_timer_or_reminder(self, target: str) -> dict[str, Any]:
        """Cancel an active timer or reminder by ID, exact label, or substring match."""
        target_clean = str(target).strip()
        if not target_clean:
            return {
                "cancelled": False,
                "message": "Target identifier or label cannot be empty.",
            }

        target_lower = target_clean.lower()
        now_iso = datetime.now().astimezone().isoformat(timespec="seconds")

        # 1. Exact ID match (active items first)
        for item in self._items.values():
            if item.get("status") == "active" and item["id"].lower() == target_lower:
                item["status"] = "cancelled"
                item["cancelled_at"] = now_iso
                self._save_storage()
                return {
                    "cancelled": True,
                    "id": item["id"],
                    "type": item.get("type"),
                    "label": item.get("label") or item.get("message"),
                    "message": item.get("message") or item.get("label"),
                }

        # 2. Exact label or message match (case-insensitive)
        for item in self._items.values():
            if item.get("status") != "active":
                continue
            lbl = (item.get("label") or "").lower()
            msg = (item.get("message") or "").lower()
            if target_lower == lbl or target_lower == msg:
                item["status"] = "cancelled"
                item["cancelled_at"] = now_iso
                self._save_storage()
                return {
                    "cancelled": True,
                    "id": item["id"],
                    "type": item.get("type"),
                    "label": item.get("label") or item.get("message"),
                    "message": item.get("message") or item.get("label"),
                }

        # 3. Case-insensitive substring match on label or message
        for item in self._items.values():
            if item.get("status") != "active":
                continue
            lbl = (item.get("label") or "").lower()
            msg = (item.get("message") or "").lower()
            if target_lower in lbl or target_lower in msg:
                item["status"] = "cancelled"
                item["cancelled_at"] = now_iso
                self._save_storage()
                return {
                    "cancelled": True,
                    "id": item["id"],
                    "type": item.get("type"),
                    "label": item.get("label") or item.get("message"),
                    "message": item.get("message") or item.get("label"),
                }

        return {
            "cancelled": False,
            "message": f"No active timer or reminder found matching {target_clean!r}.",
        }


# ------------------------------------------------ default singleton & module tools


_default_manager: TimerManager | None = None


def get_default_timer_manager() -> TimerManager:
    """Return the process-wide default TimerManager instance."""
    global _default_manager
    if _default_manager is None:
        _default_manager = TimerManager()
    return _default_manager


def set_default_timer_manager(manager: TimerManager | None) -> None:
    """Set or reset the default TimerManager instance (useful for test isolation)."""
    global _default_manager
    _default_manager = manager


def set_timer(
    duration: str | int | float,
    label: str = "Timer",
    *,
    manager: TimerManager | None = None,
) -> dict[str, Any]:
    """Agent tool: Schedule a countdown timer."""
    mgr = manager or get_default_timer_manager()
    return mgr.set_timer(duration=duration, label=label)


def set_reminder(
    message: str,
    when: str,
    *,
    manager: TimerManager | None = None,
) -> dict[str, Any]:
    """Agent tool: Schedule a reminder at a given relative or clock time."""
    mgr = manager or get_default_timer_manager()
    return mgr.set_reminder(message=message, when=when)


def list_timers_and_reminders(*, manager: TimerManager | None = None) -> TimerListResult:
    """Agent tool: List all active timers and upcoming reminders."""
    mgr = manager or get_default_timer_manager()
    return mgr.list_timers_and_reminders()


def cancel_timer_or_reminder(
    target: str,
    *,
    manager: TimerManager | None = None,
) -> dict[str, Any]:
    """Agent tool: Cancel an active timer or reminder by ID or label/message match."""
    mgr = manager or get_default_timer_manager()
    return mgr.cancel_timer_or_reminder(target=target)


def register_timer_tools(registry: ToolRegistry, manager: TimerManager | None = None) -> None:
    """Register timer and reminder tools on a ToolRegistry instance."""
    mgr = manager or get_default_timer_manager()

    def _set_timer_handler(duration: str, label: str = "Timer") -> dict[str, Any]:
        return mgr.set_timer(duration=duration, label=label)

    def _set_reminder_handler(message: str, when: str) -> dict[str, Any]:
        return mgr.set_reminder(message=message, when=when)

    def _list_handler() -> dict[str, Any]:
        return dict(mgr.list_timers_and_reminders())

    def _cancel_handler(target: str) -> dict[str, Any]:
        return mgr.cancel_timer_or_reminder(target=target)

    tools_to_register = [
        (
            "set_timer",
            (
                "Set a countdown timer with a flexible duration (e.g. '45s', '10m', '1h 30m', "
                "'25 minutes', or plain seconds '300') and an optional label."
            ),
            {
                "type": "object",
                "properties": {
                    "duration": {
                        "type": "string",
                        "description": (
                            "Duration string (e.g. '45s', '10m', '1h 30m', '25 minutes', or '300')."
                        ),
                    },
                    "label": {
                        "type": "string",
                        "description": "Optional short label describing the timer.",
                        "default": "Timer",
                    },
                },
                "required": ["duration"],
                "additionalProperties": False,
            },
            _set_timer_handler,
        ),
        (
            "set_reminder",
            (
                "Set a reminder at a relative offset ('in 20 minutes', 'in 2 hours'), "
                "a local clock time ('18:30', '6:30 PM'), or an ISO timestamp."
            ),
            {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The reminder text or task to remember.",
                    },
                    "when": {
                        "type": "string",
                        "description": (
                            "When to remind: relative offset ('in 15m', 'in 2 hours'), "
                            "clock time ('18:30', '6:30 PM'), or ISO timestamp."
                        ),
                    },
                },
                "required": ["message", "when"],
                "additionalProperties": False,
            },
            _set_reminder_handler,
        ),
        (
            "list_timers_and_reminders",
            (
                "List all active timers (with remaining seconds) and "
                "upcoming reminders sorted chronologically."
            ),
            {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            _list_handler,
        ),
        (
            "cancel_timer_or_reminder",
            (
                "Cancel an active timer or reminder by exact ID or case-insensitive "
                "substring match on label or message."
            ),
            {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": (
                            "Timer/reminder ID or substring to match in label or message."
                        ),
                    },
                },
                "required": ["target"],
                "additionalProperties": False,
            },
            _cancel_handler,
        ),
    ]

    for name, desc, schema, handler in tools_to_register:
        try:
            registry.get(name)
        except KeyError:
            registry.register(
                name=name,
                description=desc,
                input_schema=schema,
                risk_level=RiskLevel.READ_ONLY,
                handler=handler,
            )
