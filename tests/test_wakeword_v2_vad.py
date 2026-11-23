"""Deterministic unit tests for livekit-wakeword v2, idle Bluetooth output fix,
cold Bluetooth SCO rebind warm-up, Gemini Live VAD tuning, and --diagnose-mic updates.
"""

from __future__ import annotations

import asyncio
import builtins
import inspect
import io
import logging
import sys
import threading
import time
import types
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
import sounddevice as sd
from google.genai import types as genai_types
from pydantic import ValidationError

from clea.surfaces.cli.voice import run_voice_diagnose_mic
from clea.surfaces.voice.audio import AudioManager
from clea.surfaces.voice.backends.base import (
    VoiceBackend,
    VoiceBackendEvent,
    VoiceSessionConfig,
    VoiceToolResponse,
)
from clea.surfaces.voice.backends.gemini_live import GeminiLiveBackend
from clea.surfaces.voice.config import VoiceConfig, load_voice_config
from clea.surfaces.voice.runner import VoiceRunner
from clea.surfaces.voice.wake_word import (
    DEFAULT_LIVEKIT_THRESHOLD,
    LIVEKIT_BUILTIN_WAKEWORDS,
    LIVEKIT_WAKEWORD_RELEASE_TAG,
    WakeWordDetector,
    WakeWordModelNotFoundError,
    livekit_wake_word_models_dir,
    resolve_livekit_wakeword_model,
    resolve_wake_threshold,
)


def _valid_fake_onnx_bytes(size: int = 2048) -> bytes:
    """Return deterministic synthetic bytes with a valid ONNX ModelProto protobuf header."""
    # Field 1 (ir_version, varint 7) + Field 2 (producer_name = "pytorch")
    header = bytes([0x08, 0x07, 0x12, 0x07]) + b"pytorch"
    if size <= len(header):
        return header
    return header + (b"\x00" * (size - len(header)))


def _seed_openwakeword_files(data_dir: Path) -> Path:
    models_dir = data_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    for fname in ("melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis_v0.1.onnx"):
        (models_dir / fname).write_bytes(_valid_fake_onnx_bytes(128))
    return models_dir


class _DummyBackend(VoiceBackend):
    backend_name = "dummy-vad-test"

    def __init__(self) -> None:
        self.connected_config: VoiceSessionConfig | None = None
        self.closed = False
        self._stop = asyncio.Event()

    async def connect(self, config: VoiceSessionConfig) -> None:
        self.connected_config = config

    async def send_audio(self, pcm_bytes: bytes) -> None:
        pass

    async def send_video_frame(self, image_bytes: bytes, *, mime_type: str = "image/jpeg") -> None:
        pass

    async def send_text(self, text: str, *, end_of_turn: bool = True) -> None:
        pass

    async def send_tool_responses(self, responses: list[VoiceToolResponse]) -> None:
        pass

    async def events(self) -> AsyncIterator[VoiceBackendEvent]:
        await self._stop.wait()
        if False:
            yield  # pragma: no cover

    async def close(self) -> None:
        self.closed = True
        self._stop.set()


# ===========================================================================
# 1. Single Active Engine: LiveKit Primary + Startup-Only openWakeWord Fallback
# ===========================================================================


def test_livekit_primary_loads_without_loading_openwakeword(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Primary engine loads livekit-wakeword v2 and NEVER imports/instantiates openWakeWord."""
    model_file = tmp_path / "models" / "wakeword" / "hey_jarvis.onnx"
    model_file.parent.mkdir(parents=True, exist_ok=True)
    model_file.write_bytes(_valid_fake_onnx_bytes(1_000_064))

    livekit_init_calls: list[dict[str, Any]] = []

    class _FakeLiveKitModel:
        def __init__(self, *, models: list[str], sample_rate: int = 16000) -> None:
            livekit_init_calls.append({"models": list(models), "sample_rate": sample_rate})
            self.reset_called = 0

        def predict(self, audio: np.ndarray) -> dict[str, float]:
            assert audio.dtype == np.int16
            assert audio.ndim == 1
            return {"hey_jarvis": 0.82}

        def reset(self) -> None:
            self.reset_called += 1

    fake_lk_mod = types.ModuleType("livekit.wakeword")
    fake_lk_mod.WakeWordModel = _FakeLiveKitModel  # type: ignore[attr-defined]
    fake_lk_pkg = types.ModuleType("livekit")
    fake_lk_pkg.wakeword = fake_lk_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "livekit", fake_lk_pkg)
    monkeypatch.setitem(sys.modules, "livekit.wakeword", fake_lk_mod)

    # Poison openwakeword so any import or instantiation fails loudly
    oww_touched = [False]

    class _ForbiddenOWW:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            oww_touched[0] = True
            raise AssertionError("openWakeWord must NEVER be instantiated when livekit loads")

    fake_oww_model_mod = types.ModuleType("openwakeword.model")
    fake_oww_model_mod.Model = _ForbiddenOWW  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openwakeword.model", fake_oww_model_mod)

    detector = WakeWordDetector(
        model_name="hey_jarvis",
        engine="auto",
        threshold=0.20,  # legacy value must NOT be applied to livekit
        livekit_threshold=0.50,
        data_dir=tmp_path,
        allow_download=False,
    )

    assert detector.active_engine == "livekit"
    assert detector.engine == "livekit"
    assert detector.model_path == model_file
    assert detector.model_key == "hey_jarvis"
    assert detector.threshold == pytest.approx(0.50)
    assert len(livekit_init_calls) == 1
    assert livekit_init_calls[0]["models"] == [str(model_file)]
    assert livekit_init_calls[0]["sample_rate"] == 16000
    assert oww_touched[0] is False

    det, score, key = detector.process_audio(np.full(1280, 1000, dtype=np.int16))
    assert det is True
    assert score == pytest.approx(0.82)
    assert key == "hey_jarvis"


def test_startup_only_fallback_to_openwakeword_when_livekit_import_or_model_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When livekit-wakeword import or model resolution fails, auto falls back once at startup."""
    isolated_dir = tmp_path / "isolated_fallback_data"
    _seed_openwakeword_files(isolated_dir)

    oww_init_calls: list[dict[str, Any]] = []

    class _FakeOWWModel:
        def __init__(self, **kwargs: Any) -> None:
            oww_init_calls.append(kwargs)

        def predict(self, audio: np.ndarray) -> dict[str, float]:
            return {"hey_jarvis_v0.1": 0.41}

        def reset(self) -> None:
            pass

    fake_oww_model_mod = types.ModuleType("openwakeword.model")
    fake_oww_model_mod.Model = _FakeOWWModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openwakeword.model", fake_oww_model_mod)

    # Case A: livekit model file missing (auto-download disabled) -> falls back to openWakeWord
    detector_missing = WakeWordDetector(
        model_name="hey_jarvis",
        engine="auto",
        threshold=0.25,
        livekit_threshold=0.50,
        data_dir=isolated_dir,
        allow_download=False,
    )
    assert detector_missing.active_engine == "openwakeword"
    assert detector_missing.threshold == pytest.approx(0.25)
    assert detector_missing.model_key == "hey_jarvis_v0.1"
    assert len(oww_init_calls) == 1

    # Case B: livekit.wakeword raises ImportError -> falls back to openWakeWord
    orig_import = builtins.__import__

    def _block_livekit(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "livekit" or name.startswith("livekit."):
            raise ImportError("simulated missing livekit-wakeword")
        return orig_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "livekit.wakeword", raising=False)
    monkeypatch.setattr(builtins, "__import__", _block_livekit)

    detector_import_fail = WakeWordDetector(
        model_name="hey_jarvis",
        engine="auto",
        threshold=0.20,
        livekit_threshold=0.50,
        data_dir=isolated_dir,
        allow_download=False,
    )
    assert detector_import_fail.active_engine == "openwakeword"
    assert detector_import_fail.threshold == pytest.approx(0.20)
    assert len(oww_init_calls) == 2

    # Case C: explicit engine="livekit" MUST NOT fall back to openWakeWord
    with pytest.raises(ImportError, match="simulated missing livekit-wakeword"):
        WakeWordDetector(
            model_name="hey_jarvis",
            engine="livekit",
            data_dir=isolated_dir,
            allow_download=False,
        )
    assert len(oww_init_calls) == 2

    # Case D: explicit engine="openwakeword" NEVER touches livekit.wakeword
    det_explicit_oww = WakeWordDetector(
        model_name="hey_jarvis",
        engine="openwakeword",
        threshold=0.22,
        data_dir=isolated_dir,
        allow_download=False,
    )
    assert det_explicit_oww.active_engine == "openwakeword"
    assert det_explicit_oww.threshold == pytest.approx(0.22)
    assert len(oww_init_calls) == 3


def test_resolve_livekit_wakeword_model_override_atomic_download_and_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify env override, atomic temp+rename download, timeout, and corrupt-payload rejection."""
    # 1. Verified GitHub v2 release URL check
    url = LIVEKIT_BUILTIN_WAKEWORDS["hey_jarvis"]["url"]
    assert url == (
        f"https://github.com/livekit/livekit-wakeword/releases/download/"
        f"{LIVEKIT_WAKEWORD_RELEASE_TAG}/hey_jarvis.onnx"
    )

    # 2. CLEA_WAKEWORD_MODEL_PATH override takes precedence
    custom_onnx = tmp_path / "custom_jarvis.onnx"
    custom_onnx.write_bytes(_valid_fake_onnx_bytes(256))
    monkeypatch.setenv("CLEA_WAKEWORD_MODEL_PATH", str(custom_onnx))
    assert resolve_livekit_wakeword_model("hey_jarvis", data_dir=tmp_path) == custom_onnx

    # Missing override raises clearly without downloading
    monkeypatch.setenv("CLEA_WAKEWORD_MODEL_PATH", str(tmp_path / "missing.onnx"))
    with pytest.raises(WakeWordModelNotFoundError, match="CLEA_WAKEWORD_MODEL_PATH"):
        resolve_livekit_wakeword_model("hey_jarvis", data_dir=tmp_path)
    monkeypatch.delenv("CLEA_WAKEWORD_MODEL_PATH")

    # 3. Atomic lazy download with mocked urlopen (valid ONNX payload)
    monkeypatch.setenv("CLEA_WAKEWORD_AUTO_DOWNLOAD", "1")
    payload = _valid_fake_onnx_bytes(4096)
    seen_requests: list[tuple[str, float]] = []

    class _FakeResp:
        def __init__(self, data: bytes, content_length: int | None = None) -> None:
            self._buf = io.BytesIO(data)
            self.headers = (
                {"Content-Length": str(content_length)} if content_length is not None else {}
            )

        def read(self, n: int = -1) -> bytes:
            return self._buf.read(n)

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *args: object) -> None:
            pass

    def _fake_urlopen(req: Any, timeout: float = 0.0) -> _FakeResp:
        seen_requests.append((req.full_url, timeout))
        return _FakeResp(payload, content_length=len(payload))

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        resolved = resolve_livekit_wakeword_model(
            "hey_jarvis",
            data_dir=tmp_path,
            allow_download=True,
            timeout=12.5,
            min_bytes=1024,
        )

    expected_path = livekit_wake_word_models_dir(tmp_path) / "hey_jarvis.onnx"
    assert resolved == expected_path
    assert resolved.is_file()
    assert resolved.read_bytes() == payload
    assert seen_requests == [(url, 12.5)]
    # No leftover temp files in cache dir
    assert list(expected_path.parent.glob("*.tmp")) == []

    # 4. Corrupt HTML error page is rejected and leaves no target or .tmp file
    bad_dir = tmp_path / "bad_cache"
    with (
        patch(
            "urllib.request.urlopen",
            return_value=_FakeResp(b"<!DOCTYPE html><html>404 Not Found</html>" * 100),
        ),
        pytest.raises(RuntimeError, match="HTML/JSON"),
    ):
        resolve_livekit_wakeword_model(
            "hey_jarvis",
            cache_dir=bad_dir,
            allow_download=True,
            min_bytes=64,
        )
    assert not (bad_dir / "hey_jarvis.onnx").exists()
    assert list(bad_dir.glob("*.tmp")) == []

    # 5. Truncated payload (Content-Length mismatch) is rejected cleanly
    trunc_dir = tmp_path / "trunc_cache"
    with (
        patch(
            "urllib.request.urlopen",
            return_value=_FakeResp(_valid_fake_onnx_bytes(1024), content_length=4096),
        ),
        pytest.raises(RuntimeError, match="size mismatch"),
    ):
        resolve_livekit_wakeword_model(
            "hey_jarvis",
            cache_dir=trunc_dir,
            allow_download=True,
            min_bytes=512,
        )
    assert not (trunc_dir / "hey_jarvis.onnx").exists()
    assert list(trunc_dir.glob("*.tmp")) == []


# ===========================================================================
# 2. Engine-Aware Threshold Resolution & Legacy 0.20 Protection
# ===========================================================================


def test_engine_aware_threshold_resolution_blocks_legacy_020_on_livekit(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Legacy voice_config.toml with wake_threshold = 0.20 never runs livekit at 0.20."""
    # 1. Existing TOML with legacy openWakeWord wake_threshold = 0.20 (no livekit_wake_threshold)
    cfg_path = tmp_path / "voice_config.toml"
    cfg_path.write_text('wake_word_model = "hey_jarvis"\nwake_threshold = 0.20\n', encoding="utf-8")
    cfg = load_voice_config(cfg_path)
    assert cfg.wake_word_engine == "auto"
    assert cfg.wake_threshold == pytest.approx(0.20)
    assert cfg.livekit_wake_threshold == pytest.approx(DEFAULT_LIVEKIT_THRESHOLD)

    # Engine-aware resolution uses 0.50 for livekit and 0.20 for openwakeword fallback
    assert resolve_wake_threshold(
        "livekit",
        threshold=cfg.wake_threshold,
        livekit_threshold=cfg.livekit_wake_threshold,
    ) == pytest.approx(0.50)
    assert resolve_wake_threshold(
        "openwakeword",
        threshold=cfg.wake_threshold,
        livekit_threshold=cfg.livekit_wake_threshold,
    ) == pytest.approx(0.20)

    # 2. Direct call passing legacy 0.20 (or 0.30) without livekit_threshold upgrades to 0.50
    caplog.set_level(logging.WARNING, logger="clea.surfaces.voice.wake_word")
    assert resolve_wake_threshold("livekit", threshold=0.20) == pytest.approx(0.50)
    assert resolve_wake_threshold("livekit", threshold=0.30) == pytest.approx(0.50)
    assert any("Legacy openWakeWord wake_threshold=0.20" in rec.message for rec in caplog.records)

    # 3. Explicit custom livekit_wake_threshold (e.g. 0.45) IS honored
    cfg_path.write_text(
        'wake_threshold = 0.20\nlivekit_wake_threshold = 0.45\n', encoding="utf-8"
    )
    cfg_custom = load_voice_config(cfg_path)
    assert resolve_wake_threshold(
        "livekit",
        threshold=cfg_custom.wake_threshold,
        livekit_threshold=cfg_custom.livekit_wake_threshold,
    ) == pytest.approx(0.45)

    # Non-legacy explicit threshold passed without livekit_threshold is preserved
    assert resolve_wake_threshold("livekit", threshold=0.62) == pytest.approx(0.62)


# ===========================================================================
# 3. Throttled INFO-Level Near-Miss Telemetry
# ===========================================================================


def test_throttled_info_near_miss_logging_includes_peak_rms(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Near-miss scores log at INFO with peak RMS, throttled to max once per interval."""
    now = [100.0]
    monkeypatch.setattr("clea.surfaces.voice.wake_word.time.monotonic", lambda: now[0])

    # A. LiveKit engine: floor is 0.15
    lk_score = [0.10]
    lk_mock = MagicMock()
    lk_mock.predict.side_effect = lambda _arr: {"hey_jarvis": lk_score[0]}
    lk_det = WakeWordDetector(
        model_name="hey_jarvis",
        engine="livekit",
        threshold=0.50,
        model_instance=lk_mock,
        near_miss_log_interval=2.0,
    )

    caplog.set_level(logging.INFO, logger="clea.surfaces.voice.wake_word")
    loud_chunk = np.full(1280, 1500, dtype=np.int16)

    # Below 0.15 -> NO INFO log
    lk_score[0] = 0.14
    assert lk_det.process_audio(loud_chunk)[0] is False
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert info_msgs == []

    # >= 0.15 and < 0.50 -> emits 1st INFO near-miss with peak_rms
    lk_score[0] = 0.28
    assert lk_det.process_audio(loud_chunk)[0] is False
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_msgs) == 1
    assert "Wake word near-miss" in info_msgs[0]
    assert "engine=livekit" in info_msgs[0]
    assert "score=0.280" in info_msgs[0]
    assert "peak_rms=1500.0" in info_msgs[0]

    # Within 2.0s window -> throttled (no second INFO log yet)
    now[0] = 101.0
    lk_score[0] = 0.37
    louder_chunk = np.full(1280, 2200, dtype=np.int16)
    assert lk_det.process_audio(louder_chunk)[0] is False
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_msgs) == 1

    # After 2.0s elapsed -> logs window peak score (0.370) and peak RMS (2200.0)
    now[0] = 102.1
    lk_score[0] = 0.21
    quiet_chunk = np.full(1280, 400, dtype=np.int16)
    assert lk_det.process_audio(quiet_chunk)[0] is False
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_msgs) == 2
    assert "score=0.370" in info_msgs[1]
    assert "peak_rms=2200.0" in info_msgs[1]

    # B. openWakeWord engine: floor is 0.08
    caplog.clear()
    oww_score = [0.09]
    oww_mock = MagicMock()
    oww_mock.predict.side_effect = lambda _arr: {"hey_jarvis_v0.1": oww_score[0]}
    oww_det = WakeWordDetector(
        model_name="hey_jarvis",
        engine="openwakeword",
        threshold=0.30,
        model_instance=oww_mock,
        near_miss_log_interval=2.0,
    )
    oww_score[0] = 0.07
    oww_det.process_audio(loud_chunk)
    assert [r for r in caplog.records if r.levelno == logging.INFO] == []

    oww_score[0] = 0.11
    oww_det.process_audio(loud_chunk)
    oww_infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(oww_infos) == 1
    assert "engine=openwakeword" in oww_infos[0]
    assert "score=0.110" in oww_infos[0]


# ===========================================================================
# 4. Idle Bluetooth Output Stream Suspension & On-Demand Lifecycle
# ===========================================================================


def test_audio_manager_suspends_output_in_idle_opens_on_demand_and_tracks_hotswap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OutputStream stays closed in IDLE, opens on demand/play_audio, suspends cleanly,
    and preserves OS default hot-swap detection while suspended.
    """
    fake_hostapis = [
        {"name": "Windows WASAPI", "default_input_device": 0, "default_output_device": 1},
    ]
    fake_devices = [
        {
            "name": "Headset (Earphone)",
            "hostapi": 0,
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 16000.0,
        },
        {
            "name": "Headphones (Earphone)",
            "hostapi": 0,
            "max_input_channels": 0,
            "max_output_channels": 2,
            "default_samplerate": 48000.0,
        },
    ]

    endpoint_state = [("cap-hfp-14", "ren-a2dp-12")]
    in_events: list[str] = []
    out_events: list[str] = []

    class _FakeIn:
        def __init__(self, **_kw: Any) -> None:
            self.active = False
            self.stopped = True

        def start(self) -> None:
            self.active = True
            self.stopped = False
            in_events.append("start")

        def stop(self) -> None:
            self.active = False
            self.stopped = True
            in_events.append("stop")

        def close(self) -> None:
            in_events.append("close")

    class _FakeOut:
        def __init__(self, callback: Any = None, **_kw: Any) -> None:
            self._cb = callback
            self.active = False
            self.stopped = True
            self._stop_evt = threading.Event()
            self._t: threading.Thread | None = None

        def start(self) -> None:
            self.active = True
            self.stopped = False
            out_events.append("start")
            if self._cb is not None:

                def _pump() -> None:
                    buf = np.zeros((240, 1), dtype=np.int16)
                    while not self._stop_evt.is_set():
                        self._cb(buf, 240, None, sd.CallbackFlags())
                        time.sleep(0.002)

                self._t = threading.Thread(target=_pump, daemon=True)
                self._t.start()

        def stop(self) -> None:
            self._stop_evt.set()
            if self._t is not None:
                self._t.join(timeout=0.5)
            self.active = False
            self.stopped = True
            out_events.append("stop")

        def close(self) -> None:
            out_events.append("close")

    monkeypatch.setattr(sd, "query_devices", lambda: fake_devices)
    monkeypatch.setattr(sd, "query_hostapis", lambda: fake_hostapis)
    monkeypatch.setattr(sd, "check_input_settings", lambda **_kw: None)
    monkeypatch.setattr(sd, "InputStream", _FakeIn)
    monkeypatch.setattr(sd, "OutputStream", _FakeOut)
    monkeypatch.setattr(
        "clea.surfaces.voice.audio.get_os_default_audio_endpoint_ids",
        lambda: endpoint_state[0],
    )
    monkeypatch.setattr("clea.surfaces.voice.audio.refresh_audio_devices", lambda: None)

    mgr = AudioManager(chunk_size=1280)
    mgr.start()
    try:
        # 1. IDLE: Input is open, output is resolved for telemetry but NOT opened/streaming zeros!
        assert in_events == ["start"]
        assert out_events == []
        assert mgr.is_output_stream_active is False
        assert mgr._output_stream is None
        assert mgr.resolved_output_device is not None
        assert mgr.resolved_output_device["name"] == "Headphones (Earphone)"

        # 2. OS default output endpoint change is still detected while output stream is stopped
        endpoint_state[0] = ("cap-hfp-14", "ren-speakers-new")
        assert mgr.has_default_audio_device_changed() == (True, "os_default_output_changed")

        # Hot-swap restart in IDLE keeps output suspended!
        assert mgr.restart_streams(retry_delay=0.0) is True
        assert out_events == []
        assert mgr.is_output_stream_active is False
        assert mgr.has_default_audio_device_changed() == (False, "ok")

        # 3. Calling play_audio() (e.g. wake chime / TTS) opens OutputStream on demand
        mgr.play_audio(b"\x10\x00" * 240)
        assert mgr.is_output_stream_active is True
        assert out_events == ["start"]

        # 4. Suspending output stops/closes OutputStream cleanly without deadlock while keeping mic
        mgr.suspend_output_stream(drain_timeout=0.2)
        assert mgr.is_output_stream_active is False
        assert mgr._output_stream is None
        assert out_events == ["start", "stop", "close"]
        assert mgr._input_stream is not None and mgr._input_stream.active is True

        # 5. Hot-swap restart while output IS active (mid-call) reopens OutputStream
        assert mgr.start_output_stream() is True
        assert mgr.is_output_stream_active is True
        assert mgr.restart_streams(retry_delay=0.0) is True
        assert mgr.is_output_stream_active is True
    finally:
        mgr.stop()


@pytest.mark.asyncio
async def test_voice_runner_opens_output_on_call_and_suspends_on_return_to_idle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VoiceRunner keeps output suspended in IDLE, opens on start_call, suspends on end_call."""
    fake_hostapis = [
        {"name": "Windows WASAPI", "default_input_device": 0, "default_output_device": 1},
    ]
    fake_devices = [
        {
            "name": "Headset (Earphone)",
            "hostapi": 0,
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 16000.0,
        },
        {
            "name": "Headphones (Earphone)",
            "hostapi": 0,
            "max_input_channels": 0,
            "max_output_channels": 2,
            "default_samplerate": 48000.0,
        },
    ]

    class _Stream:
        def __init__(self, **_kw: Any) -> None:
            self.active = True
            self.stopped = False

        def start(self) -> None:
            self.active = True
            self.stopped = False

        def stop(self) -> None:
            self.active = False
            self.stopped = True

        def close(self) -> None:
            pass

    monkeypatch.setattr(sd, "query_devices", lambda: fake_devices)
    monkeypatch.setattr(sd, "query_hostapis", lambda: fake_hostapis)
    monkeypatch.setattr(sd, "check_input_settings", lambda **_kw: None)
    monkeypatch.setattr(sd, "InputStream", _Stream)
    monkeypatch.setattr(sd, "OutputStream", _Stream)
    monkeypatch.setattr(
        "clea.surfaces.voice.audio.get_os_default_audio_endpoint_ids",
        lambda: ("cap-1", "ren-1"),
    )

    mgr = AudioManager(chunk_size=1280)
    mock_model = MagicMock()
    mock_model.predict.return_value = {"hey_jarvis": 0.0}
    detector = WakeWordDetector(
        model_name="hey_jarvis",
        engine="livekit",
        threshold=0.50,
        model_instance=mock_model,
    )

    runner = VoiceRunner(
        config=VoiceConfig(),
        data_dir=tmp_path,
        backend_factory=_DummyBackend,
        audio_manager=mgr,
        wake_word_detector=detector,
        enable_hotkey=False,
    )
    runner._audio_health_poll_interval = 10.0
    runner._input_warmup_timeout = 0.05

    await runner.start()
    try:
        # IDLE -> output stream is suspended
        assert runner.call_state == "idle"
        assert mgr.is_output_stream_active is False

        # start_call -> output stream opens on demand
        await runner.start_call(trigger="wake_word")
        assert runner.call_state == "active"
        assert mgr.is_output_stream_active is True

        # end_call -> output stream suspends cleanly back to IDLE
        await runner.end_call(reason="user_done")
        assert runner.call_state == "idle"
        assert mgr.is_output_stream_active is False
    finally:
        await runner.stop()


# ===========================================================================
# 5. Cold Bluetooth SCO Rebind Warm-Up Before Flush & WakeWord Reset
# ===========================================================================


@pytest.mark.asyncio
async def test_cold_bluetooth_sco_rebind_waits_for_callbacks_before_flush_and_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After os_default_audio_changed, runner waits for first callbacks before flush & reset."""
    fake_hostapis = [
        {"name": "Windows WASAPI", "default_input_device": 0, "default_output_device": 1},
    ]
    fake_devices = [
        {
            "name": "Headset (Earphone)",
            "hostapi": 0,
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 16000.0,
        },
        {
            "name": "Headphones (Earphone)",
            "hostapi": 0,
            "max_input_channels": 0,
            "max_output_channels": 2,
            "default_samplerate": 48000.0,
        },
    ]

    endpoint_state = [("cap-initial", "ren-initial")]
    rebind_stream_started = threading.Event()
    input_streams_created: list[Any] = []

    class _ColdScoInputStream:
        def __init__(self, callback: Any = None, **_kw: Any) -> None:
            self._cb = callback
            self.active = False
            self.stopped = True
            input_streams_created.append(self)

        def start(self) -> None:
            self.active = True
            self.stopped = False
            idx = len(input_streams_created)
            if idx == 1:
                # Initial bind delivers an active callback quickly
                def _deliver_initial() -> None:
                    time.sleep(0.03)
                    if self.active and self._cb is not None:
                        block = np.full((1280, 1), 600, dtype=np.int16)
                        self._cb(block, 1280, None, sd.CallbackFlags())

                threading.Thread(target=_deliver_initial, daemon=True).start()
            else:
                # Rebind simulates cold Bluetooth SCO delay (~180ms before 1st callback)
                rebind_stream_started.set()

                def _deliver_delayed_sco() -> None:
                    time.sleep(0.18)
                    if self.active and self._cb is not None:
                        block = np.full((1280, 1), 900, dtype=np.int16)
                        self._cb(block, 1280, None, sd.CallbackFlags())

                threading.Thread(target=_deliver_delayed_sco, daemon=True).start()

        def stop(self) -> None:
            self.active = False
            self.stopped = True

        def close(self) -> None:
            pass

    class _DummyOut:
        def __init__(self, **_kw: Any) -> None:
            self.active = True
            self.stopped = False

        def start(self) -> None:
            pass

        def stop(self) -> None:
            self.active = False
            self.stopped = True

        def close(self) -> None:
            pass

    monkeypatch.setattr(sd, "query_devices", lambda: fake_devices)
    monkeypatch.setattr(sd, "query_hostapis", lambda: fake_hostapis)
    monkeypatch.setattr(sd, "check_input_settings", lambda **_kw: None)
    monkeypatch.setattr(sd, "InputStream", _ColdScoInputStream)
    monkeypatch.setattr(sd, "OutputStream", _DummyOut)
    monkeypatch.setattr(
        "clea.surfaces.voice.audio.get_os_default_audio_endpoint_ids",
        lambda: endpoint_state[0],
    )
    monkeypatch.setattr("clea.surfaces.voice.audio.refresh_audio_devices", lambda: None)

    mgr = AudioManager(chunk_size=1280)

    reset_snapshots: list[tuple[int, float]] = []
    predict_calls_during_warmup = [0]

    mock_model = MagicMock()

    def _record_predict(arr: np.ndarray) -> dict[str, float]:
        if runner._wake_word_warming_up:
            predict_calls_during_warmup[0] += 1
        return {"hey_jarvis": 0.0}

    def _record_reset() -> None:
        reset_snapshots.append((mgr._input_callback_count, mgr._last_input_rms))

    mock_model.predict.side_effect = _record_predict
    mock_model.reset.side_effect = _record_reset

    detector = WakeWordDetector(
        model_name="hey_jarvis",
        engine="livekit",
        threshold=0.50,
        model_instance=mock_model,
    )

    flush_timestamps: list[float] = []
    orig_flush = mgr.flush_input_buffer

    def _spy_flush() -> None:
        flush_timestamps.append(time.monotonic())
        orig_flush()

    monkeypatch.setattr(mgr, "flush_input_buffer", _spy_flush)

    runner = VoiceRunner(
        config=VoiceConfig(),
        data_dir=tmp_path,
        backend_factory=_DummyBackend,
        audio_manager=mgr,
        wake_word_detector=detector,
        enable_hotkey=False,
    )
    runner._audio_health_poll_interval = 0.05
    runner._input_warmup_timeout = 1.5

    await runner.start()
    try:
        # 1. Startup reset ran AFTER initial input callback arrived (callback_count > 0)
        assert len(reset_snapshots) == 1
        assert reset_snapshots[0][0] >= 1
        assert reset_snapshots[0][1] > 0.0

        # 2. Trigger os_default_audio_changed hot-swap rebind
        endpoint_state[0] = ("cap-bt-2", "ren-bt-2")
        for _ in range(40):
            if rebind_stream_started.is_set():
                break
            await asyncio.sleep(0.02)
        assert rebind_stream_started.is_set()

        # Immediately after rebind starts (~40ms < 180ms SCO delay), reset MUST NOT have fired yet!
        await asyncio.sleep(0.04)
        assert len(reset_snapshots) == 1

        # Wait for cold SCO callback (~180ms) -> flush -> reset -> re-arm
        for _ in range(60):
            if len(reset_snapshots) >= 2 and not runner._wake_word_warming_up:
                break
            await asyncio.sleep(0.03)

        assert len(reset_snapshots) == 2
        # Post-rebind reset occurred strictly AFTER active input callbacks arrived!
        assert reset_snapshots[1][0] >= 1
        assert reset_snapshots[1][1] > 0.0
        # No warm-up audio was leaked into WakeWordDetector.process_audio during warm-up
        assert predict_calls_during_warmup[0] == 0
        # Flush ran both on startup and after cold SCO warm-up
        assert len(flush_timestamps) >= 2
        # Output stream remained suspended in IDLE across the hot-swap
        assert mgr.is_output_stream_active is False
    finally:
        await runner.stop()


def test_audio_manager_wait_for_input_callbacks_handles_arrival_timeout_and_stop() -> None:
    """wait_for_input_callbacks returns promptly on callback arrival, stop_event, or timeout."""
    mgr = AudioManager(chunk_size=1280)
    mgr._running = True

    # 1. Timeout when no callbacks arrive
    t0 = time.monotonic()
    assert mgr.wait_for_input_callbacks(timeout=0.06, poll_interval=0.01) is False
    assert (time.monotonic() - t0) < 0.5

    # 2. Early abort via stop_event
    stop_evt = threading.Event()
    stop_evt.set()
    t1 = time.monotonic()
    assert mgr.wait_for_input_callbacks(timeout=1.5, stop_event=stop_evt) is False
    assert (time.monotonic() - t1) < 0.2

    # 3. Succeeds immediately once callback arrives
    def _fire_cb() -> None:
        time.sleep(0.03)
        mgr._input_stream_callback(
            np.full((1280, 1), 300, dtype=np.int16), 1280, None, sd.CallbackFlags()
        )

    threading.Thread(target=_fire_cb, daemon=True).start()
    assert mgr.wait_for_input_callbacks(timeout=1.0, poll_interval=0.01) is True
    assert mgr._input_callback_count >= 1


# ===========================================================================
# 6. Gemini Live VAD Tuning (silence_duration_ms)
# ===========================================================================


@pytest.mark.asyncio
async def test_gemini_live_backend_configures_vad_silence_duration_and_barge_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GeminiLiveBackend passes AutomaticActivityDetection(silence_duration_ms=...) cleanly."""
    backend = GeminiLiveBackend(api_key="test-key")

    # 1. Default server VAD uses 1000 ms silence duration and responsive barge-in
    default_cfg = backend._build_live_config(VoiceSessionConfig(system_instruction="sys"))
    assert default_cfg.realtime_input_config is not None
    aad = default_cfg.realtime_input_config.automatic_activity_detection
    assert aad is not None
    assert aad.disabled is False
    assert aad.silence_duration_ms == 1000
    assert aad.end_of_speech_sensitivity == genai_types.EndSensitivity.END_SENSITIVITY_LOW
    assert (
        default_cfg.realtime_input_config.activity_handling
        == genai_types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS
    )

    # 1b. End-to-end connect() passes AutomaticActivityDetection into client.aio.live.connect
    mock_session = MagicMock()

    async def _empty_recv() -> Any:
        if False:
            yield None

    mock_session.receive.side_effect = _empty_recv
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_session
    mock_ctx.__aexit__.return_value = None
    mock_client = MagicMock()
    mock_client.aio.live.connect.return_value = mock_ctx

    connected_backend = GeminiLiveBackend(api_key="test-key", client=mock_client)
    await connected_backend.connect(
        VoiceSessionConfig(system_instruction="sys", vad_silence_duration_ms=1250)
    )
    try:
        wire_cfg = mock_client.aio.live.connect.call_args.kwargs["config"]
        assert wire_cfg.realtime_input_config is not None
        wire_aad = wire_cfg.realtime_input_config.automatic_activity_detection
        assert wire_aad is not None
        assert wire_aad.silence_duration_ms == 1250
    finally:
        await connected_backend.close()

    # 2. Custom silence duration from voice_config.toml flows through VoiceRunner -> Backend
    cfg_path = tmp_path / "voice_config.toml"
    cfg_path.write_text("vad_silence_duration_ms = 1350\n", encoding="utf-8")
    loaded = load_voice_config(cfg_path)
    assert loaded.vad_silence_duration_ms == 1350

    captured_backends: list[_DummyBackend] = []

    def _make_backend() -> _DummyBackend:
        b = _DummyBackend()
        captured_backends.append(b)
        return b

    audio = MagicMock(spec=AudioManager)
    audio._health_monitored = False
    audio.resolved_input_device = None
    audio.resolved_output_device = None
    audio.is_speaking = False
    mock_model = MagicMock()
    mock_model.predict.return_value = {"hey_jarvis": 0.0}
    runner = VoiceRunner(
        config=loaded,
        data_dir=tmp_path,
        backend_factory=_make_backend,
        audio_manager=audio,
        wake_word_detector=WakeWordDetector(model_instance=mock_model),
        enable_hotkey=False,
    )
    await runner.start()
    try:
        await runner.start_call(trigger="manual")
        assert captured_backends[0].connected_config is not None
        assert captured_backends[0].connected_config.vad_silence_duration_ms == 1350
        built = backend._build_live_config(captured_backends[0].connected_config)
        assert built.realtime_input_config is not None
        assert built.realtime_input_config.automatic_activity_detection is not None
        assert built.realtime_input_config.automatic_activity_detection.silence_duration_ms == 1350
    finally:
        await runner.stop()

    # 3. Environment variable override CLEA_VAD_SILENCE_DURATION_MS works
    monkeypatch.setenv("CLEA_VAD_SILENCE_DURATION_MS", "1450")
    env_loaded = load_voice_config(cfg_path)
    assert env_loaded.vad_silence_duration_ms == 1450
    env_built = backend._build_live_config(VoiceSessionConfig(system_instruction="sys"))
    assert env_built.realtime_input_config is not None
    assert env_built.realtime_input_config.automatic_activity_detection is not None
    assert env_built.realtime_input_config.automatic_activity_detection.silence_duration_ms == 1450

    # 4. Client VAD mode disables server-side automatic activity detection
    client_built = backend._build_live_config(
        VoiceSessionConfig(system_instruction="sys", vad_mode="client")
    )
    assert client_built.realtime_input_config is not None
    assert client_built.realtime_input_config.automatic_activity_detection is not None
    assert client_built.realtime_input_config.automatic_activity_detection.disabled is True

    # 5. Invalid VAD bounds and booleans are rejected by VoiceConfig
    for bad_toml in (
        "vad_silence_duration_ms = 50\n",
        "vad_silence_duration_ms = 20000\n",
        "vad_silence_duration_ms = true\n",
        'wake_word_engine = "invalid"\n',
        "livekit_wake_threshold = 1.5\n",
        "livekit_wake_threshold = true\n",
    ):
        bad_path = tmp_path / "bad_voice_config.toml"
        bad_path.write_text(bad_toml, encoding="utf-8")
        monkeypatch.delenv("CLEA_VAD_SILENCE_DURATION_MS", raising=False)
        with pytest.raises(ValidationError):
            load_voice_config(bad_path)


# ===========================================================================
# 7. --diagnose-mic Reporting Updates (Fix 5)
# ===========================================================================


def test_diagnose_mic_reports_engine_threshold_idle_output_and_4s_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_voice_diagnose_mic defaults to 4.0s and reports engine, threshold, idle_output_active."""
    sig = inspect.signature(run_voice_diagnose_mic)
    assert sig.parameters["duration"].default == 4.0

    readonly_dir = tmp_path / "never_created_clea"
    monkeypatch.setenv("CLEA_DATA_DIR", str(readonly_dir))

    fake_candidates = [
        {
            "index": 14,
            "name": "Headset (Earphone)",
            "hostapi_name": "Windows WASAPI",
            "default_samplerate": 16000.0,
            "max_input_channels": 1,
            "max_output_channels": 0,
        }
    ]
    monkeypatch.setattr(
        "clea.surfaces.cli.voice.list_candidate_audio_devices",
        lambda *_a, **_kw: fake_candidates,
    )

    class _FakeDiagAudio:
        def __init__(self, *, on_audio_input: Any = None, **_kw: Any) -> None:
            self._cb = on_audio_input
            self.resolved_input_device = fake_candidates[0]
            self.resolved_output_device = {
                "index": 12,
                "name": "Headphones (Earphone)",
                "hostapi_name": "Windows WASAPI",
            }
            self.input_sample_rate = 16000
            self.input_channels = 1
            self.is_output_stream_active = False

        def start(self) -> None:
            pcm = np.full(1280, 1400, dtype=np.int16).tobytes()
            if self._cb:
                self._cb(pcm)

        def stop(self) -> None:
            pass

    mock_model = MagicMock()
    mock_model.predict.return_value = {"hey_jarvis": 0.67}

    def _make_detector(**kw: Any) -> WakeWordDetector:
        assert kw["allow_download"] is False
        det = WakeWordDetector(
            model_name=kw["model_name"],
            engine="livekit",
            threshold=0.50,
            model_instance=mock_model,
        )
        det.model_path = Path("C:/cache/hey_jarvis.onnx")
        return det

    lines: list[str] = []
    result = run_voice_diagnose_mic(
        duration=0.2,
        print_fn=lines.append,
        audio_manager_factory=_FakeDiagAudio,  # type: ignore[arg-type]
        detector_factory=_make_detector,
    )

    assert result["ok"] is True
    assert result["wake_word_engine"] == "livekit"
    assert result["wake_word_model_path"] == str(Path("C:/cache/hey_jarvis.onnx"))
    assert result["wake_threshold"] == pytest.approx(0.50)
    assert result["idle_output_active"] is False
    assert result["peak_wake_score"] == pytest.approx(0.67)
    assert result["wake_triggered"] is True
    assert any("Active wake-word engine: livekit" in line for line in lines)
    assert any("threshold=0.50" in line for line in lines)
    assert any("idle_output_active=False" in line for line in lines)
    assert any("peak_score=0.670" in line and "triggered=True" in line for line in lines)
    assert not readonly_dir.exists()


# ===========================================================================
# 8. Concurrency, Stale Window, Corrupt ONNX, and Superseded-Call Hardening
# ===========================================================================


def test_play_audio_vs_suspend_output_stream_race_never_leaks_or_wipes_newer_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent play_audio() and suspend_output_stream() never leak an open OutputStream
    in IDLE and never wipe audio queued after the suspend.
    """
    fake_hostapis = [
        {"name": "Windows WASAPI", "default_input_device": 0, "default_output_device": 1},
    ]
    fake_devices = [
        {
            "name": "Headset (Earphone)",
            "hostapi": 0,
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 16000.0,
        },
        {
            "name": "Headphones (Earphone)",
            "hostapi": 0,
            "max_input_channels": 0,
            "max_output_channels": 2,
            "default_samplerate": 48000.0,
        },
    ]

    class _FakeStream:
        def __init__(self, **_kw: Any) -> None:
            self.active = False
            self.stopped = True

        def start(self) -> None:
            self.active = True
            self.stopped = False

        def stop(self) -> None:
            self.active = False
            self.stopped = True

        def close(self) -> None:
            pass

    monkeypatch.setattr(sd, "query_devices", lambda: fake_devices)
    monkeypatch.setattr(sd, "query_hostapis", lambda: fake_hostapis)
    monkeypatch.setattr(sd, "check_input_settings", lambda **_kw: None)
    monkeypatch.setattr(sd, "InputStream", _FakeStream)
    monkeypatch.setattr(sd, "OutputStream", _FakeStream)
    monkeypatch.setattr(
        "clea.surfaces.voice.audio.get_os_default_audio_endpoint_ids",
        lambda: ("cap-0", "ren-0"),
    )

    mgr = AudioManager()
    mgr.start()
    try:
        # Case 1: suspend_output_stream runs after play_audio queues audio & sets need_open,
        # BEFORE start_output_stream runs -> must NOT leak an open stream in IDLE.
        orig_start_out = mgr.start_output_stream
        entered = threading.Event()
        release = threading.Event()
        intercept_once = [True]

        def _intercepted_start_out(**kw: Any) -> bool:
            if intercept_once[0]:
                intercept_once[0] = False
                entered.set()
                release.wait(timeout=2.0)
            return orig_start_out(**kw)

        monkeypatch.setattr(mgr, "start_output_stream", _intercepted_start_out)
        t1 = threading.Thread(target=lambda: mgr.play_audio(b"\x01\x00" * 120))
        t1.start()
        assert entered.wait(timeout=2.0)

        mgr.suspend_output_stream()
        release.set()
        t1.join(timeout=2.0)

        assert mgr.is_output_stream_active is False
        assert mgr.is_speaking is False
        assert mgr._output_queue.empty()

        # Case 2: Thread A superseded by suspend, then Thread B queues new audio ->
        # Thread A aborting MUST NOT wipe Thread B's newly queued audio!
        entered2 = threading.Event()
        release2 = threading.Event()
        intercept_once[0] = True

        def _intercepted_start_out_2(**kw: Any) -> bool:
            if intercept_once[0]:
                intercept_once[0] = False
                entered2.set()
                release2.wait(timeout=2.0)
            return orig_start_out(**kw)

        monkeypatch.setattr(mgr, "start_output_stream", _intercepted_start_out_2)
        ta = threading.Thread(target=lambda: mgr.play_audio(b"\x02\x00" * 120))
        ta.start()
        assert entered2.wait(timeout=2.0)

        mgr.suspend_output_stream()
        mgr.play_audio(b"\x09\x00" * 120)
        assert mgr.is_output_stream_active is True
        assert mgr.is_speaking is True

        release2.set()
        ta.join(timeout=2.0)

        assert mgr.is_output_stream_active is True
        assert mgr.is_speaking is True
        assert not mgr._output_queue.empty()

        # Case 3: Multi-threaded concurrent play_audio + suspend stress
        monkeypatch.setattr(mgr, "start_output_stream", orig_start_out)
        errors: list[Exception] = []

        def _player() -> None:
            try:
                for _ in range(25):
                    mgr.play_audio(b"\x03\x00" * 60)
                    time.sleep(0.001)
            except Exception as exc:
                errors.append(exc)

        def _suspender() -> None:
            try:
                for _ in range(25):
                    mgr.suspend_output_stream()
                    time.sleep(0.001)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_player), threading.Thread(target=_suspender)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5.0)
        assert errors == []

        mgr.suspend_output_stream()
        assert mgr.is_output_stream_active is False
        assert mgr.is_speaking is False
        assert mgr._output_queue.empty()
    finally:
        mgr.stop()


@pytest.mark.asyncio
async def test_process_wake_word_chunk_respects_warming_up_guard(tmp_path: Path) -> None:
    """_process_wake_word_chunk must not score or trigger calls while _wake_word_warming_up."""
    mock_model = MagicMock()
    mock_model.predict.return_value = {"hey_jarvis": 0.99}
    det = WakeWordDetector(model_instance=mock_model, engine="livekit", threshold=0.50)
    runner = VoiceRunner(
        config=VoiceConfig(),
        data_dir=tmp_path,
        wake_word_detector=det,
        enable_hotkey=False,
    )
    runner._loop = asyncio.get_running_loop()
    runner._call_state = "idle"
    runner._wake_word_warming_up = True

    runner._process_wake_word_chunk(b"\x01\x00" * 1280)
    assert mock_model.predict.call_count == 0
    assert runner._wake_word_pending_trigger is False


@pytest.mark.asyncio
async def test_superseded_start_call_does_not_suspend_newer_active_call_output(
    tmp_path: Path,
) -> None:
    """When Call #1 is superseded by Call #2 during output open, Call #1 must not suspend
    Call #2's active output stream; when ended back to IDLE, it must suspend cleanly.
    """
    suspend_calls: list[int] = []
    entered_open = threading.Event()
    release_open = threading.Event()

    audio = MagicMock(spec=AudioManager)
    audio._health_monitored = False
    audio.resolved_input_device = None
    audio.resolved_output_device = None
    audio.is_speaking = False

    def _blocking_start_out() -> bool:
        entered_open.set()
        release_open.wait(timeout=2.0)
        return True

    audio.start_output_stream.side_effect = _blocking_start_out
    audio.suspend_output_stream.side_effect = lambda: suspend_calls.append(1)

    mock_model = MagicMock()
    mock_model.predict.return_value = {"hey_jarvis": 0.0}
    backends: list[_DummyBackend] = []

    def _make_backend() -> _DummyBackend:
        b = _DummyBackend()
        backends.append(b)
        return b

    runner = VoiceRunner(
        config=VoiceConfig(),
        data_dir=tmp_path,
        backend_factory=_make_backend,
        audio_manager=audio,
        wake_word_detector=WakeWordDetector(model_instance=mock_model),
        enable_hotkey=False,
    )
    await runner.start()
    try:
        # Case 1: Superseded by a newer active call -> do NOT suspend output stream
        task1 = asyncio.create_task(runner.start_call(trigger="wake_word"))
        await asyncio.to_thread(entered_open.wait, 2.0)
        runner._call_generation += 1
        runner._call_state = "active"
        release_open.set()
        await task1
        assert backends[0].closed is True
        assert suspend_calls == []

        # Case 2: Ended back to IDLE during output open -> MUST suspend output stream
        entered_open.clear()
        release_open.clear()
        runner._call_state = "idle"
        task2 = asyncio.create_task(runner.start_call(trigger="wake_word"))
        await asyncio.to_thread(entered_open.wait, 2.0)
        runner._call_generation += 1
        runner._call_state = "idle"
        runner._call_id = None
        release_open.set()
        await task2
        assert backends[1].closed is True
        assert len(suspend_calls) == 1
    finally:
        runner._call_state = "idle"
        await runner.stop()


def test_livekit_model_instance_respects_livekit_threshold_and_legacy_guard() -> None:
    """model_instance with engine='livekit' honors livekit_threshold and legacy 0.20 guard."""
    det_both = WakeWordDetector(
        model_instance=MagicMock(),
        engine="livekit",
        threshold=0.20,
        livekit_threshold=0.50,
    )
    assert det_both.threshold == pytest.approx(0.50)

    det_legacy = WakeWordDetector(
        model_instance=MagicMock(),
        engine="livekit",
        threshold=0.20,
    )
    assert det_legacy.threshold == pytest.approx(0.50)

    det_custom = WakeWordDetector(
        model_instance=MagicMock(),
        engine="livekit",
        threshold=0.20,
        livekit_threshold=0.45,
    )
    assert det_custom.threshold == pytest.approx(0.45)


def test_near_miss_window_expires_stale_rms_and_ancient_scores(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Near-miss telemetry expires stale peak_rms and ancient throttled scores across gaps."""
    now = [10.0]
    monkeypatch.setattr("clea.surfaces.voice.wake_word.time.monotonic", lambda: now[0])

    score = [0.01]
    mock_model = MagicMock()
    mock_model.predict.side_effect = lambda _arr: {"hey_jarvis": score[0]}
    det = WakeWordDetector(
        model_name="hey_jarvis",
        engine="livekit",
        threshold=0.50,
        model_instance=mock_model,
        near_miss_log_interval=2.0,
    )
    caplog.set_level(logging.INFO, logger="clea.surfaces.voice.wake_word")

    # Loud clap at t=10s must NOT pollute a quiet near-miss 30 seconds later
    det.process_audio(np.full(1280, 15000, dtype=np.int16))
    now[0] = 40.0
    score[0] = 0.30
    det.process_audio(np.full(1280, 250, dtype=np.int16))

    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1
    assert "score=0.300" in infos[0]
    assert "peak_rms=250.0" in infos[0]

    # Throttled near-miss at t=40.5s must expire before an unrelated near-miss 7 minutes later
    now[0] = 40.5
    score[0] = 0.42
    det.process_audio(np.full(1280, 2000, dtype=np.int16))
    assert len([r for r in caplog.records if r.levelno == logging.INFO]) == 1

    now[0] = 460.0
    score[0] = 0.18
    det.process_audio(np.full(1280, 300, dtype=np.int16))
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 2
    assert "score=0.180" in infos[1]
    assert "peak_rms=300.0" in infos[1]


def test_corrupt_cached_livekit_onnx_graph_falls_back_cleanly_to_openwakeword(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real livekit-wakeword + onnxruntime rejecting a >1MB corrupt ONNX graph falls back
    cleanly once at startup to openWakeWord under engine='auto'.
    """
    isolated = tmp_path / "corrupt_lk_fallback"
    _seed_openwakeword_files(isolated)
    corrupt_lk = isolated / "models" / "wakeword" / "hey_jarvis.onnx"
    corrupt_lk.parent.mkdir(parents=True, exist_ok=True)
    # Valid protobuf header byte + >1 MB size so header/size check passes,
    # but invalid ONNX graph protobuf so onnxruntime.InferenceSession raises!
    corrupt_lk.write_bytes(_valid_fake_onnx_bytes(1_050_000))

    oww_calls: list[dict[str, Any]] = []

    class _FakeOWW:
        def __init__(self, **kw: Any) -> None:
            oww_calls.append(kw)

        def predict(self, _arr: np.ndarray) -> dict[str, float]:
            return {"hey_jarvis_v0.1": 0.0}

        def reset(self) -> None:
            pass

    fake_oww_mod = types.ModuleType("openwakeword.model")
    fake_oww_mod.Model = _FakeOWW  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openwakeword.model", fake_oww_mod)

    det = WakeWordDetector(
        model_name="hey_jarvis",
        engine="auto",
        threshold=0.20,
        livekit_threshold=0.50,
        data_dir=isolated,
        allow_download=False,
    )
    assert det.active_engine == "openwakeword"
    assert det.threshold == pytest.approx(0.20)
    assert len(oww_calls) == 1


@pytest.mark.asyncio
async def test_gemini_live_open_session_logs_effective_vad_silence_duration(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GeminiLiveBackend._open_session logs the actual effective VAD silence duration."""
    mock_session = MagicMock()

    async def _empty_recv() -> Any:
        if False:
            yield None

    mock_session.receive.side_effect = _empty_recv
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_session
    mock_ctx.__aexit__.return_value = None
    mock_client = MagicMock()
    mock_client.aio.live.connect.return_value = mock_ctx

    caplog.set_level(logging.INFO, logger="clea.surfaces.voice.backends.gemini_live")

    monkeypatch.setenv("CLEA_VAD_SILENCE_DURATION_MS", "1450")
    b1 = GeminiLiveBackend(api_key="test-key", client=mock_client)
    await b1.connect(VoiceSessionConfig(system_instruction="sys"))
    await b1.close()

    b2 = GeminiLiveBackend(api_key="test-key", client=mock_client)
    await b2.connect(VoiceSessionConfig(system_instruction="sys", vad_mode="client"))
    await b2.close()

    msgs = [
        r.getMessage()
        for r in caplog.records
        if "GeminiLiveBackend connected" in r.getMessage()
    ]
    assert any("vad_mode=server, vad_silence_duration=1450ms" in m for m in msgs)
    assert any("vad_mode=client, vad_silence_duration=disabled" in m for m in msgs)
