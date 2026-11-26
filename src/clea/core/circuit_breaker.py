"""Pure-Python per-turn state-diff computation and hard click circuit breaker.

Lives in ``clea.core`` with zero dependencies on ``clea.tools`` or vendor SDKs
so both Chat (`AgentLoop`) and Voice (`VoiceRunner`) can share identical
circuit-breaker and state-diff semantics without violating voice isolation.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

SUGGESTED_RECOVERY_ACTIONS: list[str] = [
    "Use open_url() for direct URL navigation",
    "Use CDP refs (b1..bN)",
    "Use navigate_back",
    "Ask the user for clarification",
]

_BLIND_GUI_CLICK_ACTIONS: frozenset[str] = frozenset(
    {"click_at", "double_click", "right_click"}
)
_BLIND_GUI_LOOP_ACTIONS: frozenset[str] = _BLIND_GUI_CLICK_ACTIONS | frozenset({"scroll", "move"})

_DETERMINISTIC_RECOVERY_TOOLS: frozenset[str] = frozenset(
    {
        "open_url",
        "os.open_url",
        "ui_act",
        "os.ui_act",
        "click_ui_element",
        "os.click_ui_element",
        "cdp_act",
        "cdp_navigate",
        "cdp_navigate_history",
    }
)

_STATE_DIFF_MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "gui_action",
        "os.gui_action",
        "open_url",
        "os.open_url",
        "ui_act",
        "os.ui_act",
        "ui_batch",
        "os.ui_batch",
        "click_ui_element",
        "os.click_ui_element",
        "cdp_act",
        "cdp_navigate",
        "cdp_navigate_history",
    }
)

StateCaptureFn = Callable[[], dict[str, Any]]


def _is_cdp_ref_str(ref: str | None) -> bool:
    if not isinstance(ref, str):
        return False
    return bool(re.fullmatch(r"b[0-9]+", ref.strip().lstrip("@").lower()))


def is_state_diff_tool(tool_name: str, arguments: dict[str, Any] | None = None) -> bool:
    """Return True if *tool_name* should capture lightweight before/after state diffs."""
    norm = (tool_name or "").strip().lower()
    if norm not in _STATE_DIFF_MUTATING_TOOLS:
        return False
    if norm in {"gui_action", "os.gui_action"}:
        action = str((arguments or {}).get("action") or "").strip().lower()
        if action == "wait":
            return False
    return True


def _batch_step_ops(arguments: dict[str, Any] | None) -> list[str]:
    steps = (arguments or {}).get("steps")
    if not isinstance(steps, list):
        return []
    ops: list[str] = []
    for item in steps:
        if isinstance(item, dict):
            ops.append(str(item.get("op") or "").strip().lower())
    return ops


def is_blind_gui_action(tool_name: str, arguments: dict[str, Any] | None) -> bool:
    """Return True for pixel/scroll gui_action (or blind ui_batch) calls lacking a verified ref."""
    norm = (tool_name or "").strip().lower()
    if norm in {"ui_batch", "os.ui_batch"}:
        ops = _batch_step_ops(arguments)
        return any(op in {"click_at", "scroll"} for op in ops) and "act" not in ops
    if norm not in {"gui_action", "os.gui_action"}:
        return False
    args = arguments or {}
    raw_ref = args.get("ref")
    if isinstance(raw_ref, str) and raw_ref.strip():
        return False
    action = str(args.get("action") or "").strip().lower()
    return action in _BLIND_GUI_LOOP_ACTIONS


def is_blind_click_action(tool_name: str, arguments: dict[str, Any] | None) -> bool:
    """Return True for blind pixel click actions (`click_at`/`double_click`/`right_click`)."""
    if not is_blind_gui_action(tool_name, arguments):
        return False
    norm = (tool_name or "").strip().lower()
    if norm in {"ui_batch", "os.ui_batch"}:
        return "click_at" in _batch_step_ops(arguments)
    action = str((arguments or {}).get("action") or "").strip().lower()
    return action in _BLIND_GUI_CLICK_ACTIONS


def is_deterministic_action(tool_name: str, arguments: dict[str, Any] | None) -> bool:
    """Return True for deterministic actions that reset the blind-click breaker on progress."""
    norm = (tool_name or "").strip().lower()
    if norm in _DETERMINISTIC_RECOVERY_TOOLS:
        return True
    if norm in {"ui_batch", "os.ui_batch"}:
        ops = _batch_step_ops(arguments)
        return "act" in ops and "click_at" not in ops
    if norm in {"gui_action", "os.gui_action"}:
        args = arguments or {}
        action = str(args.get("action") or "").strip().lower()
        if action in {"navigate_back", "navigate_forward"}:
            return True
        raw_ref = args.get("ref")
        if isinstance(raw_ref, str) and _is_cdp_ref_str(raw_ref):
            return True
    return False


def _compact_state(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Return a clean, compact state dict suitable for tool payloads and logs."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {
        "hwnd": raw.get("hwnd"),
        "title": str(raw.get("title") or ""),
        "process_name": str(raw.get("process_name") or ""),
    }
    if raw.get("url"):
        out["url"] = str(raw["url"])
    if raw.get("page_title"):
        out["page_title"] = str(raw["page_title"])
    if raw.get("target_id"):
        out["target_id"] = str(raw["target_id"])
    if raw.get("is_pwa") is not None:
        out["is_pwa"] = bool(raw["is_pwa"])
    if isinstance(raw.get("media"), dict):
        out["media"] = dict(raw["media"])
    return out


def compute_state_diff(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compute a compact ``before -> after`` desktop/CDP/media state diff."""
    b = _compact_state(before)
    a = _compact_state(after)

    b_hwnd = b.get("hwnd")
    a_hwnd = a.get("hwnd")
    b_win_title = str(b.get("title") or "")
    a_win_title = str(a.get("title") or "")
    b_proc = str(b.get("process_name") or "").lower()
    a_proc = str(a.get("process_name") or "").lower()

    b_url = str(b.get("url") or "")
    a_url = str(a.get("url") or "")
    b_page_title = str(b.get("page_title") or b_win_title)
    a_page_title = str(a.get("page_title") or a_win_title)

    b_media = b.get("media") if isinstance(b.get("media"), dict) else None
    a_media = a.get("media") if isinstance(a.get("media"), dict) else None

    window_changed = (b_hwnd != a_hwnd) or (b_proc != a_proc) or (b_win_title != a_win_title)
    url_changed = bool(b_url or a_url) and (b_url != a_url)
    title_changed = (b_page_title != a_page_title) or (b_win_title != a_win_title)
    media_changed = (b_media != a_media) and (b_media is not None or a_media is not None)
    changed = bool(window_changed or url_changed or title_changed or media_changed)

    return {
        "changed": changed,
        "window_changed": window_changed,
        "url_changed": url_changed,
        "title_changed": title_changed,
        "media_changed": media_changed,
        "before": b,
        "after": a,
    }


def merge_tool_state_diff(
    captured_diff: dict[str, Any] | None,
    tool_result: Any,
) -> dict[str, Any] | None:
    """Merge tool-reported CDP ``state_diff`` with harness-captured desktop state diff."""
    tool_diff: dict[str, Any] | None = None
    if isinstance(tool_result, dict) and isinstance(tool_result.get("state_diff"), dict):
        tool_diff = dict(tool_result["state_diff"])

    if captured_diff is None:
        return tool_diff
    if tool_diff is None:
        return captured_diff

    before = dict(captured_diff.get("before") or {})
    after = dict(captured_diff.get("after") or {})
    t_before = tool_diff.get("before") if isinstance(tool_diff.get("before"), dict) else {}
    t_after = tool_diff.get("after") if isinstance(tool_diff.get("after"), dict) else {}
    t_before_title = t_before.get("page_title") or t_before.get("title")
    t_after_title = t_after.get("page_title") or t_after.get("title")
    prefer_tool_url = bool(tool_diff.get("url_changed")) and not bool(
        captured_diff.get("url_changed")
    )
    if t_before.get("url") and (prefer_tool_url or not before.get("url")):
        before["url"] = t_before["url"]
    if t_before_title and (prefer_tool_url or not before.get("page_title")):
        before["page_title"] = t_before_title
    if t_before.get("target_id") and not before.get("target_id"):
        before["target_id"] = t_before["target_id"]
    if t_before.get("is_pwa") is not None and "is_pwa" not in before:
        before["is_pwa"] = bool(t_before["is_pwa"])
    if t_after.get("url") and (prefer_tool_url or not after.get("url")):
        after["url"] = t_after["url"]
    if t_after_title and (prefer_tool_url or not after.get("page_title")):
        after["page_title"] = t_after_title
    if t_after.get("target_id") and not after.get("target_id"):
        after["target_id"] = t_after["target_id"]
    if t_after.get("is_pwa") is not None and "is_pwa" not in after:
        after["is_pwa"] = bool(t_after["is_pwa"])

    window_changed = bool(captured_diff.get("window_changed") or tool_diff.get("window_changed"))
    url_changed = bool(captured_diff.get("url_changed") or tool_diff.get("url_changed"))
    title_changed = bool(captured_diff.get("title_changed") or tool_diff.get("title_changed"))
    media_changed = bool(captured_diff.get("media_changed") or tool_diff.get("media_changed"))
    changed = bool(
        captured_diff.get("changed")
        or tool_diff.get("changed")
        or window_changed
        or url_changed
        or title_changed
        or media_changed
    )
    return {
        "changed": changed,
        "window_changed": window_changed,
        "url_changed": url_changed,
        "title_changed": title_changed,
        "media_changed": media_changed,
        "before": before,
        "after": after,
    }


def _state_signature(state: dict[str, Any] | None) -> tuple[Any, str, str, str]:
    if not isinstance(state, dict):
        return (None, "", "", "")
    return (
        state.get("hwnd"),
        str(state.get("title") or ""),
        str(state.get("url") or ""),
        str(state.get("page_title") or ""),
    )


def _is_empty_sig(sig: tuple[Any, str, str, str]) -> bool:
    return sig == (None, "", "", "")


@dataclass
class ActionCircuitBreaker:
    """Per-turn circuit breaker stopping blind pixel-click and UI-snapshot failure loops.

    Trip conditions:
      - 2 consecutive blind click actions (`click_at` / `double_click` / `right_click`)
        with no verified forward progress (unchanged state OR unintended bounce back to
        a prior state without a verified target match), OR
      - 3 consecutive `ui_snapshot` timeouts/failures in the same turn.

    Resets on every new user turn (`reset()`) or whenever a deterministic action
    (`open_url`, `cdp_act`, verified `ui_act` ref click, `navigate_back`/`forward`)
    succeeds and advances state.
    """

    max_blind_clicks_without_progress: int = 2
    max_snapshot_failures: int = 3
    blind_click_no_progress_count: int = 0
    blind_loop_no_progress_count: int = 0
    snapshot_failure_count: int = 0
    tripped: bool = False
    trip_reason: str = ""
    last_state: dict[str, Any] = field(default_factory=dict)
    _visited_signatures: list[tuple[Any, str, str, str]] = field(default_factory=list)
    _blind_streak_origin: tuple[Any, str, str, str] | None = None
    _streak_expected_process: str = ""
    _streak_expected_hwnd: int | None = None

    def reset(self) -> None:
        """Reset all breaker counters and tripped state for a new turn or after recovery."""
        self.blind_click_no_progress_count = 0
        self.blind_loop_no_progress_count = 0
        self.snapshot_failure_count = 0
        self.tripped = False
        self.trip_reason = ""
        self._visited_signatures.clear()
        self._blind_streak_origin = None
        self._streak_expected_process = ""
        self._streak_expected_hwnd = None

    def build_blocked_payload(self) -> dict[str, Any]:
        """Return the structured tool response when the circuit breaker blocks an action."""
        return {
            "ok": False,
            "status": "blocked_by_circuit_breaker",
            "circuit_breaker_tripped": True,
            "reason": self.trip_reason or "Blind GUI loop blocked by circuit breaker.",
            "last_state": dict(self.last_state),
            "suggested_recovery": list(SUGGESTED_RECOVERY_ACTIONS),
        }

    def check_before_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Return a blocking payload if the breaker is tripped and *tool_name* is a blind action."""
        if not self.tripped:
            return None
        if is_blind_gui_action(tool_name, arguments):
            return self.build_blocked_payload()
        norm = (tool_name or "").strip().lower()
        if (
            norm in {"ui_snapshot", "os.ui_snapshot"}
            and self.snapshot_failure_count >= self.max_snapshot_failures
        ):
            return self.build_blocked_payload()
        return None

    def record_outcome(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        *,
        result: Any = None,
        is_error: bool = False,
        state_diff: dict[str, Any] | None = None,
    ) -> None:
        """Update breaker counters from the outcome of a tool call."""
        norm = (tool_name or "").strip().lower()
        res_dict = result if isinstance(result, dict) else {}

        if isinstance(state_diff, dict) and isinstance(state_diff.get("after"), dict):
            self.last_state = dict(state_diff["after"])
        elif isinstance(res_dict.get("state_diff"), dict) and isinstance(
            res_dict["state_diff"].get("after"), dict
        ):
            self.last_state = dict(res_dict["state_diff"]["after"])

        # 1. Track consecutive ui_snapshot errors / timeouts / cdp_unavailable
        if norm in {"ui_snapshot", "os.ui_snapshot"}:
            status = str(res_dict.get("status") or "").strip().lower()
            snap_failed = (
                is_error
                or res_dict.get("ok") is False
                or status in {"error", "timeout", "cdp_unavailable", "cdp_error"}
            )
            if snap_failed:
                self.snapshot_failure_count += 1
                if self.snapshot_failure_count >= self.max_snapshot_failures:
                    self.tripped = True
                    self.trip_reason = (
                        f"{self.snapshot_failure_count} consecutive ui_snapshot failures/timeouts "
                        "in the same turn."
                    )
            else:
                self.snapshot_failure_count = 0
                if self.tripped and "ui_snapshot" in self.trip_reason:
                    self.tripped = False
                    self.trip_reason = ""
            return

        # 2. Deterministic recovery / navigation actions reset the breaker when successful
        if is_deterministic_action(norm, arguments):
            status = str(res_dict.get("status") or "").strip().lower()
            succeeded = (
                not is_error
                and res_dict.get("ok", True) is not False
                and status
                not in {
                    "error",
                    "failed",
                    "aborted_precondition",
                    "cdp_unavailable",
                    "cdp_error",
                    "stale_ref",
                    "target_not_found",
                    "no_history_entry",
                }
            )
            res_state_diff = res_dict.get("state_diff")
            effective_det_diff = (
                state_diff
                if isinstance(state_diff, dict)
                else (res_state_diff if isinstance(res_state_diff, dict) else None)
            )
            diff_changed = bool(
                isinstance(effective_det_diff, dict) and effective_det_diff.get("changed")
            )
            nav_or_act_status = status in {
                "navigated",
                "opened",
                "focused",
                "launched",
                "completed",
            }
            if succeeded and (
                diff_changed
                or (effective_det_diff is None and nav_or_act_status)
                or (status == "navigated" and bool(res_dict.get("cdp_used")))
            ):
                self.reset()
            return

        # 3. Blind pixel click / scroll / move actions
        if is_blind_gui_action(norm, arguments):
            effective_diff = state_diff
            if effective_diff is None and isinstance(res_dict.get("state_diff"), dict):
                effective_diff = res_dict["state_diff"]

            has_diff = isinstance(effective_diff, dict)
            before_raw = effective_diff.get("before") if has_diff else None
            after_raw = effective_diff.get("after") if has_diff else None
            before_state = before_raw if isinstance(before_raw, dict) else {}
            after_state = after_raw if isinstance(after_raw, dict) else {}

            before_sig = _state_signature(before_state)
            after_sig = _state_signature(after_state)

            before_proc = str(before_state.get("process_name") or "").strip().lower()
            after_proc = str(after_state.get("process_name") or "").strip().lower()

            if self._streak_expected_process == "" and before_proc:
                self._streak_expected_process = before_proc
                raw_hwnd = before_state.get("hwnd")
                self._streak_expected_hwnd = int(raw_hwnd) if isinstance(raw_hwnd, int) else None

            if self._blind_streak_origin is None and not _is_empty_sig(before_sig):
                self._blind_streak_origin = before_sig
                if before_sig not in self._visited_signatures:
                    self._visited_signatures.append(before_sig)

            changed = bool(isinstance(effective_diff, dict) and effective_diff.get("changed"))
            failed = is_error or res_dict.get("ok") is False

            is_click = is_blind_click_action(norm, arguments)
            expected_proc = self._streak_expected_process or before_proc
            focus_hijacked = bool(
                is_click
                and expected_proc
                and after_proc
                and after_proc != expected_proc
                and isinstance(effective_diff, dict)
                and effective_diff.get("window_changed")
                and not effective_diff.get("url_changed")
                and not effective_diff.get("media_changed")
            )

            # Detect unintended bounce back to an earlier visited state
            bounced = (
                changed
                and not focus_hijacked
                and not _is_empty_sig(after_sig)
                and (
                    after_sig == self._blind_streak_origin
                    or after_sig in self._visited_signatures
                )
            )
            no_forward_progress = failed or (not changed) or bounced or focus_hijacked

            if not _is_empty_sig(after_sig) and after_sig not in self._visited_signatures:
                self._visited_signatures.append(after_sig)

            if no_forward_progress:
                self.blind_loop_no_progress_count += 1
                if is_click:
                    if (bounced or focus_hijacked) and self.blind_click_no_progress_count == 0:
                        self.blind_click_no_progress_count = self.max_blind_clicks_without_progress
                    elif focus_hijacked:
                        self.blind_click_no_progress_count = max(
                            self.blind_click_no_progress_count + 1,
                            self.max_blind_clicks_without_progress,
                        )
                    else:
                        self.blind_click_no_progress_count += 1

                if self.blind_click_no_progress_count >= self.max_blind_clicks_without_progress:
                    self.tripped = True
                    if focus_hijacked:
                        self.trip_reason = (
                            f"unintended_focus_switch: blind click switched focus from "
                            f"{expected_proc!r} to unrelated process {after_proc!r}."
                        )
                    elif bounced:
                        self.trip_reason = (
                            f"{self.blind_click_no_progress_count} consecutive blind click actions "
                            "produced an unintended state bounce with no verified forward progress."
                        )
                    else:
                        self.trip_reason = (
                            f"{self.blind_click_no_progress_count} consecutive blind click actions "
                            "with no verified forward progress."
                        )
                elif self.blind_loop_no_progress_count >= (
                    self.max_blind_clicks_without_progress + 1
                ):
                    self.tripped = True
                    self.trip_reason = (
                        f"{self.blind_loop_no_progress_count} consecutive blind pixel/scroll "
                        "actions with no verified forward progress."
                    )
            else:
                self.blind_click_no_progress_count = 0
                self.blind_loop_no_progress_count = 0
