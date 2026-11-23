"""Deterministic unit tests for live wake-word reliability and dynamic 3-layer OS app routing.

Covers:
1. Guaranteed 16 kHz mono int16 1280-sample pipeline into openWakeWord (including float32
   [-1, 1] scaling, stereo downmix, 48 kHz & 44.1 kHz resampling, and multi-chunk stepping).
2. Bluetooth/earphone retention & resilience (never demoting quiet-room Bluetooth headsets to
   the laptop mic, primary WASAPI settle retry & native blocksize=0 fallback, DirectSound
   16 kHz negotiation, MME 31-char truncated headset matching, and OS default hot-swap
   tracking in VoiceRunner's health watchdog).
3. Wake-word armed state on startup, post-call, post-error, thread-safe reset/predict +
   read-only ``--diagnose-mic``.
4. Dynamic 3-layer OS URL-to-App resolver with zero hardcoded domain dictionaries:
   - Layer 1: Windows AppUriHandlers (Store / MSIX)
   - Layer 2: Windows Registry custom URL Protocol via winreg (install -> native; uninstall -> web)
   - Layer 3: Running standalone app/PWA windows + live get_installed_apps() & cache invalidation
5. Explicit browser bypass + strict single-instance reuse (installed apps, minimized PWAs,
   and existing browser windows without ``--new-window``).
6. Chat and Voice system prompt alignment (no browser-URL contradiction for installed apps).
"""

from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest
import sounddevice as sd

from clea.core.agent import _BASE_SYSTEM_PROMPT
from clea.core.protocol import OsOpenUrlParams, OsOpenUrlResult
from clea.surfaces.cli.main import build_parser
from clea.surfaces.cli.voice import run_voice_diagnose_mic
from clea.surfaces.voice.audio import (
    AudioManager,
    compute_audio_rms,
    list_candidate_audio_devices,
    resample_to_16k_mono,
)
from clea.surfaces.voice.backends.base import (
    VoiceBackend,
    VoiceBackendEvent,
    VoiceSessionConfig,
    VoiceToolResponse,
)
from clea.surfaces.voice.config import VoiceConfig
from clea.surfaces.voice.memory_hooks import BASE_VOICE_SYSTEM_INSTRUCTION
from clea.surfaces.voice.runner import VoiceRunner
from clea.surfaces.voice.wake_word import WakeWordDetector
from clea.tools import windows_os


@pytest.fixture(autouse=True)
def _isolate_env_and_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Never touch %LOCALAPPDATA%\\clea and keep installed-app cache clean per test."""
    data_dir = tmp_path / "clea_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLEA_DATA_DIR", str(data_dir))
    windows_os.invalidate_installed_apps_cache()
    yield data_dir
    windows_os.invalidate_installed_apps_cache()


# ===========================================================================
# 1. Wake-Word 16 kHz Mono int16 1280-Sample Pipeline
# ===========================================================================


def test_resample_and_wake_word_scale_float32_and_resample_48k_44k_to_1280_int16() -> None:
    """float32 [-1, 1], stereo 48 kHz, and 44.1 kHz feed exact 16 kHz mono int16 1280 chunks."""
    # 1. Normalized float32 [-1.0, 1.0] at 48 kHz stereo (3840 frames = 80 ms -> 1280 @ 16 kHz)
    t48 = np.arange(3840, dtype=np.float32) / 48000.0
    sine_f32 = (0.5 * np.sin(2.0 * np.pi * 440.0 * t48)).astype(np.float32)
    stereo_f32_48k = np.column_stack((sine_f32, sine_f32))

    res_48 = resample_to_16k_mono(stereo_f32_48k, orig_sr=48000, target_sr=16000)
    assert res_48.dtype == np.int16
    assert res_48.ndim == 1
    assert len(res_48) == 1280
    # 0.5 peak sine -> RMS ~= 0.5 * 32767 / sqrt(2) ~= 11585 (never truncated to 0!)
    assert 9000.0 < compute_audio_rms(res_48) < 13000.0

    # 2. DirectSound 44.1 kHz float32 mono (3528 frames = 80 ms -> 1280 @ 16 kHz)
    t44 = np.arange(3528, dtype=np.float32) / 44100.0
    mono_f32_44k = (0.4 * np.sin(2.0 * np.pi * 600.0 * t44)).astype(np.float32)
    res_44 = resample_to_16k_mono(mono_f32_44k, orig_sr=44100, target_sr=16000)
    assert res_44.dtype == np.int16
    assert len(res_44) == 1280
    assert 7000.0 < compute_audio_rms(res_44) < 11000.0

    # 3. WakeWordDetector.process_audio guarantees exact 1280-sample mono int16 into predict()
    seen_chunks: list[np.ndarray] = []

    class _RecordingModel:
        def predict(self, arr: np.ndarray) -> dict[str, float]:
            seen_chunks.append(arr.copy())
            return {"hey_jarvis_v0.1": 0.72 if len(seen_chunks) == 2 else 0.05}

        def reset(self) -> None:
            pass

    detector = WakeWordDetector(
        model_name="hey_jarvis",
        threshold=0.30,
        model_instance=_RecordingModel(),
    )

    # Feed 2 * 80 ms = 7680 samples of stereo 48 kHz float32 -> 2560 @ 16 kHz -> 2x1280 chunks
    t48_2 = np.arange(7680, dtype=np.float32) / 48000.0
    wave_2 = (0.3 * np.sin(2.0 * np.pi * 500.0 * t48_2)).astype(np.float32)
    stereo_2 = np.column_stack((wave_2, wave_2))

    detected, score, key = detector.process_audio(stereo_2, sample_rate=48000)
    assert detected is True
    assert score == pytest.approx(0.72)
    assert key == "hey_jarvis_v0.1"
    assert len(seen_chunks) == 2
    for chunk in seen_chunks:
        assert chunk.dtype == np.int16
        assert chunk.ndim == 1
        assert len(chunk) == 1280
        assert compute_audio_rms(chunk) > 4000.0


def test_audio_manager_streams_exact_1280_int16_chunks_from_48k_float32_and_int16() -> None:
    """AudioManager callback normalizes float32 and 48 kHz streams into 2560-byte int16 chunks."""
    received: list[bytes] = []
    mgr = AudioManager(chunk_size=1280, on_audio_input=received.append)
    mgr.input_sample_rate = 48000
    mgr.input_channels = 2

    t = np.arange(3840, dtype=np.float32) / 48000.0
    f32_stereo = np.column_stack(
        (
            (0.25 * np.sin(2.0 * np.pi * 440.0 * t)).astype(np.float32),
            (0.25 * np.sin(2.0 * np.pi * 440.0 * t)).astype(np.float32),
        )
    )
    mgr._input_stream_callback(f32_stereo, 3840, None, sd.CallbackFlags())

    assert len(received) == 1
    assert len(received[0]) == 1280 * 2  # 1280 int16 samples
    arr = np.frombuffer(received[0], dtype=np.int16)
    assert compute_audio_rms(arr) > 4000.0
    assert mgr.input_rms_telemetry()["peak_rms"] > 4000.0


# ===========================================================================
# 2. Bluetooth Earphone Retention, WASAPI Retry, 16 kHz DS & OS Default Hot-Swap
# ===========================================================================


def test_bluetooth_headset_never_demoted_in_quiet_room_and_prefers_16k_directsound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quiet-room Bluetooth HFP stays on Headset (never hijacks to laptop mic) at native 16 kHz."""
    fake_hostapis = [
        {"name": "Windows DirectSound", "default_input_device": 1, "default_output_device": 4},
        {"name": "Windows WASAPI", "default_input_device": 0, "default_output_device": 4},
    ]
    fake_devices = [
        {
            "name": "Headset (Earphone)",
            "hostapi": 1,
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 16000.0,
        },
        {
            "name": "Headset (Earphone)",
            "hostapi": 0,
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 44100.0,
        },
        {
            "name": "Stereo Mix (Realtek HD Audio)",
            "hostapi": 1,
            "max_input_channels": 2,
            "max_output_channels": 0,
            "default_samplerate": 48000.0,
        },
        {
            "name": "Microphone Array (Realtek(R) Audio)",
            "hostapi": 1,
            "max_input_channels": 2,
            "max_output_channels": 0,
            "default_samplerate": 48000.0,
        },
        {
            "name": "Headphones (Earphone)",
            "hostapi": 1,
            "max_input_channels": 0,
            "max_output_channels": 2,
            "default_samplerate": 48000.0,
        },
    ]

    opened_specs: list[tuple[int, int, int]] = []

    class _FakeStream:
        def __init__(
            self,
            device: int,
            samplerate: int = 16000,
            blocksize: int = 0,
            **_kw: Any,
        ) -> None:
            self.device = device
            self.samplerate = samplerate
            self.blocksize = blocksize
            self.active = True
            self.stopped = False

        def start(self) -> None:
            opened_specs.append((self.device, self.samplerate, self.blocksize))
            # Simulate Bluetooth HFP WASAPI failure (PaErrorCode -9999) on all WASAPI attempts
            if self.device == 0:
                raise sd.PortAudioError("WdmSyncIoctl GLE=0x490 [PaErrorCode -9999]")

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
        lambda: ("cap-earphone", "ren-earphone"),
    )
    monkeypatch.setattr("clea.surfaces.voice.audio.refresh_audio_devices", lambda: None)

    mgr = AudioManager(chunk_size=1280)
    mgr._wasapi_retry_delay = 0.0
    mgr.start()
    try:
        # 1. Fallback stays on the user's Headset (Earphone) over DirectSound at native 16 kHz
        # (avoiding 16 kHz -> 44.1 kHz -> 16 kHz double-resampling, NEVER jumping to laptop mic!)
        assert mgr.resolved_input_device is not None
        assert mgr.resolved_input_device["index"] == 1
        assert mgr.resolved_input_device["name"] == "Headset (Earphone)"
        assert mgr.input_sample_rate == 16000

        # 2. Quiet-room Bluetooth Headset (RMS = 1.0..2.0) MUST stay healthy forever and NEVER
        # fail over to the laptop's Microphone Array!
        quiet_earphone = np.ones((1280, 1), dtype=np.int16)
        for _ in range(150):
            mgr._input_stream_callback(quiet_earphone, 1280, None, sd.CallbackFlags())

        healthy, reason = mgr.is_input_stream_healthy()
        assert healthy is True and reason == "ok"

        # 3. Even if restart_streams() is invoked (e.g. after transient hang), it reopens the
        # user's active Headset (Earphone) rather than demoting it to Microphone Array.
        assert mgr.restart_streams(retry_delay=0.0) is True
        assert mgr.resolved_input_device is not None
        assert mgr.resolved_input_device["name"] == "Headset (Earphone)"

        # 4. Candidate order places virtual/loopback Stereo Mix AFTER real Microphone Array
        ordered = list_candidate_audio_devices(None, kind="input", preferred_hostapi="WASAPI")
        names = [d["name"] for d in ordered]
        assert names[0] == "Headset (Earphone)"
        assert names.index("Microphone Array (Realtek(R) Audio)") < names.index(
            "Stereo Mix (Realtek HD Audio)"
        )
    finally:
        mgr.stop()


def test_primary_wasapi_settle_retry_and_native_blocksize_fallback_avoid_directsound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient WASAPI -9999 recovers via settle retry or blocksize=0 without dropping to DS."""
    fake_hostapis = [
        {"name": "Windows DirectSound", "default_input_device": 1, "default_output_device": 2},
        {"name": "Windows WASAPI", "default_input_device": 0, "default_output_device": 2},
    ]
    fake_devices = [
        {
            "name": "Headset (Earphone)",
            "hostapi": 1,
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 16000.0,
        },
        {
            "name": "Headset (Earphone)",
            "hostapi": 0,
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 44100.0,
        },
        {
            "name": "Headphones (Earphone)",
            "hostapi": 1,
            "max_input_channels": 0,
            "max_output_channels": 2,
            "default_samplerate": 48000.0,
        },
    ]

    # Case A: Fails first 2 WASAPI attempts (auto_convert + None), succeeds on settle retry #3
    wasapi_attempts = [0]

    class _SettleRetryStream:
        def __init__(self, device: int, blocksize: int = 0, **_kw: Any) -> None:
            self.device = device
            self.blocksize = blocksize
            self.active = True
            self.stopped = False

        def start(self) -> None:
            if self.device == 0:
                wasapi_attempts[0] += 1
                if wasapi_attempts[0] <= 2:
                    raise sd.PortAudioError("Transient SCO wake [PaErrorCode -9999]")

        def stop(self) -> None:
            self.active = False
            self.stopped = True

        def close(self) -> None:
            pass

    monkeypatch.setattr(sd, "query_devices", lambda: fake_devices)
    monkeypatch.setattr(sd, "query_hostapis", lambda: fake_hostapis)
    monkeypatch.setattr(sd, "check_input_settings", lambda **_kw: None)
    monkeypatch.setattr(sd, "InputStream", _SettleRetryStream)
    monkeypatch.setattr(sd, "OutputStream", _SettleRetryStream)
    monkeypatch.setattr(
        "clea.surfaces.voice.audio.get_os_default_audio_endpoint_ids",
        lambda: ("cap-earphone", "ren-earphone"),
    )

    mgr = AudioManager(chunk_size=1280)
    mgr._wasapi_retry_delay = 0.0
    mgr.start()
    try:
        assert mgr.resolved_input_device is not None
        assert mgr.resolved_input_device["index"] == 0
        assert mgr.resolved_input_device["hostapi_name"] == "Windows WASAPI"
        assert mgr._input_stream is not None
        assert mgr._input_stream.blocksize == 0  # type: ignore[attr-defined]
        assert wasapi_attempts[0] == 3

        # Small 10ms (160-sample) zero callbacks under native WASAPI blocksize=0 count in
        # 80ms units so 1.6s of zeros = 20 blocks (< 125 threshold, never false-restarting!)
        zero_10ms = np.zeros((160, 1), dtype=np.int16)
        for _ in range(160):
            mgr._input_stream_callback(zero_10ms, 160, None, sd.CallbackFlags())
        assert mgr._consecutive_zero_callbacks == 20
        ok_short, _ = mgr.is_input_stream_healthy(max_zero_callbacks=125)
        assert ok_short is True
    finally:
        mgr.stop()


# ===========================================================================
# 3. Wake-Word Armed State on Startup / Post-Call / Post-Error & --diagnose-mic
# ===========================================================================


class _FlakyStartBackend(VoiceBackend):
    backend_name = "flaky-test"

    def __init__(self, *, fail_connect: bool = False) -> None:
        self.fail_connect = fail_connect
        self.connected = False
        self.closed = False
        self._stop = asyncio.Event()

    async def connect(self, config: VoiceSessionConfig) -> None:
        if self.fail_connect:
            raise RuntimeError("simulated connect error")
        self.connected = True

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


@pytest.mark.asyncio
async def test_voice_runner_wake_word_armed_on_startup_and_rearms_after_call_and_error(
    tmp_path: Path,
) -> None:
    """Wake-word starts armed immediately in IDLE and reliably re-arms after errors and calls."""
    score_box = [0.0]
    mock_model = MagicMock()
    mock_model.predict.side_effect = lambda _arr: {"hey_jarvis_v0.1": score_box[0]}
    mock_model.reset = MagicMock()
    detector = WakeWordDetector(model_name="hey_jarvis", threshold=0.25, model_instance=mock_model)

    audio = MagicMock(spec=AudioManager)
    audio._health_monitored = True
    audio.input_sample_rate = 16000
    audio.input_channels = 1
    audio.resolved_input_device = {
        "index": 9,
        "name": "Microphone Array",
        "hostapi_name": "Windows WASAPI",
        "default_samplerate": 48000,
    }
    audio.resolved_output_device = {
        "index": 8,
        "name": "Speakers",
        "hostapi_name": "Windows WASAPI",
        "default_samplerate": 48000,
    }
    audio.is_speaking = False
    audio.is_input_stream_healthy.return_value = (True, "ok")

    connect_fail_flag = [True]

    def _make_backend() -> VoiceBackend:
        return _FlakyStartBackend(fail_connect=connect_fail_flag[0])

    runner = VoiceRunner(
        config=VoiceConfig(wake_word_model="hey_jarvis", wake_threshold=0.25),
        data_dir=tmp_path,
        backend_factory=_make_backend,
        audio_manager=audio,
        wake_word_detector=detector,
        enable_hotkey=False,
    )
    runner._audio_health_poll_interval = 10.0

    await runner.start()
    try:
        # 1. Armed immediately on startup with alive wake-word worker thread
        assert runner.call_state == "idle"
        assert runner._wake_word_pending_trigger is False
        assert runner._wake_word_thread is not None and runner._wake_word_thread.is_alive()
        audio.flush_input_buffer.assert_called()

        # 2. First wake trigger fails during backend.connect() -> must recover & re-arm IDLE
        score_box[0] = 0.85
        runner._on_audio_chunk(b"\x01\x00" * 1280)
        for _ in range(40):
            if runner.call_state == "idle" and runner._wake_word_pending_trigger is False and (
                runner.voice_state == "error"
            ):
                break
            await asyncio.sleep(0.05)
        assert runner.call_state == "idle"
        assert runner._wake_word_pending_trigger is False

        # 3. Second wake trigger succeeds -> enters ACTIVE, then end_call re-arms IDLE cleanly
        connect_fail_flag[0] = False
        score_box[0] = 0.91
        runner._on_audio_chunk(b"\x02\x00" * 1280)
        for _ in range(40):
            if runner.call_state == "active":
                break
            await asyncio.sleep(0.05)
        assert runner.call_state == "active"

        await runner.end_call(reason="user_done")
        assert runner.call_state == "idle"
        assert runner._wake_word_pending_trigger is False
        assert runner._wake_word_thread is not None and runner._wake_word_thread.is_alive()
    finally:
        await runner.stop()


def test_diagnose_mic_cli_flag_is_read_only_and_reports_telemetry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`clea voice --diagnose-mic` parses cleanly, reports RMS + wake score, and writes nothing."""
    parser = build_parser()
    args = parser.parse_args(["voice", "--diagnose-mic"])
    assert args.command == "voice"
    assert args.diagnose_mic is True

    empty_data_dir = tmp_path / "readonly_clea"
    monkeypatch.setenv("CLEA_DATA_DIR", str(empty_data_dir))

    fake_candidates = [
        {
            "index": 9,
            "name": "Microphone Array (Realtek(R) Audio)",
            "hostapi_name": "Windows WASAPI",
            "default_samplerate": 48000.0,
            "max_input_channels": 2,
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
            self.input_sample_rate = 48000
            self.input_channels = 1

        def start(self) -> None:
            pcm = (np.full(1280, 1200, dtype=np.int16)).tobytes()
            if self._cb:
                self._cb(pcm)
                self._cb(pcm)

        def stop(self) -> None:
            pass

    mock_model = MagicMock()
    mock_model.predict.return_value = {"hey_jarvis_v0.1": 0.42}

    lines: list[str] = []
    result = run_voice_diagnose_mic(
        duration=0.2,
        print_fn=lines.append,
        audio_manager_factory=_FakeDiagAudio,  # type: ignore[arg-type]
        detector_factory=lambda **kw: WakeWordDetector(model_instance=mock_model, **kw),
    )

    assert result["ok"] is True
    assert result["frames"] == 2
    assert result["mean_rms"] == pytest.approx(1200.0, abs=1.0)
    assert result["peak_wake_score"] == pytest.approx(0.42)
    assert result["selected_device"]["index"] == 9
    assert any("Microphone Array" in line for line in lines)
    assert any("peak_score=0.420" in line for line in lines)
    # Must NEVER create or mutate %LOCALAPPDATA%\clea
    assert not empty_data_dir.exists()


# ===========================================================================
# 4. Dynamic 3-Layer OS URL Resolver (Zero Hardcoded Domains)
# ===========================================================================


def test_dynamic_url_token_extraction_without_hardcoded_domain_tables() -> None:
    """URL host normalization works structurally for any arbitrary domain."""
    assert windows_os.extract_url_app_tokens("https://open.spotify.com/track/123") == ["spotify"]
    assert windows_os.extract_url_app_tokens("https://web.whatsapp.com/") == ["whatsapp"]
    assert windows_os.extract_url_app_tokens("https://app.exampletool.com/boards/9") == [
        "exampletool"
    ]
    assert windows_os.extract_url_app_tokens("https://www.youtube.com/playlist?list=WL") == [
        "youtube"
    ]
    music_tokens = windows_os.extract_url_app_tokens("https://music.youtube.com/watch?v=1")
    assert music_tokens[0] == "youtube-music"
    assert "youtube music" in music_tokens
    assert "youtube" in music_tokens
    # Documentation subdomain does not fall back to bare brand name
    docs_tokens = windows_os.extract_url_app_tokens("https://docs.python.org/3/library/")
    assert "python" not in docs_tokens
    assert "python docs" in docs_tokens


def test_layer1_app_uri_handler_routes_deep_url_without_launching_browser() -> None:
    """Layer 1 (WinRT AppUriHandlers hit) routes directly to the Store/MSIX app."""
    popen_calls: list[list[str]] = []
    shell_calls: list[str] = []

    res = windows_os.open_url(
        "https://brandnewstoreapp.example/playlist/42",
        app_uri_handler_finder=lambda _u: [
            {
                "name": "BrandNew Store App",
                "app_id": "BrandNew.StoreApp_12345!App",
                "package_family_name": "BrandNew.StoreApp_12345",
            }
        ],
        url_protocol_checker=lambda _s: None,
        app_catalog_fetcher=list,
        window_enumerator=list,
        popen_fn=lambda cmd, **_kw: popen_calls.append(list(cmd)),
        startfile_fn=shell_calls.append,
        desktop_name_fn=lambda: "Default",
    )

    assert res["ok"] is True
    assert res["routed_to_installed_app"] is True
    assert res["launched_app"] is True
    assert res["app_name"] == "BrandNew Store App"
    assert res["resolution_layer"] == "app_uri_handler"
    assert "Do NOT call open_url again" in res["guidance"]
    assert shell_calls == []
    assert popen_calls == [["explorer.exe", r"shell:AppsFolder\BrandNew.StoreApp_12345!App"]]


def test_layer2_winreg_url_protocol_routes_arbitrary_app_and_falls_back_after_uninstall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Layer 2 queries winreg HKCU/HKCR dynamically: present -> native app; removed -> browser."""
    monkeypatch.setattr(sys, "platform", "win32")

    # Dynamic mock Windows Registry (simulates installing then uninstalling `exampletool`)
    registry_state: dict[tuple[int, str], dict[str, str]] = {
        (1, r"Software\Classes\exampletool"): {
            "URL Protocol": "",
            "": "URL:ExampleTool Protocol",
        },
        (1, r"Software\Classes\exampletool\shell\open\command"): {
            "": r'"C:\Program Files\ExampleTool\ExampleTool.exe" "%1"',
        },
    }

    class _FakeKey:
        def __init__(self, hive: int, path: str) -> None:
            self.hive = hive
            self.path = path

        def __enter__(self) -> _FakeKey:
            return self

        def __exit__(self, *_a: Any) -> None:
            pass

    fake_winreg = types.ModuleType("winreg")
    fake_winreg.HKEY_CURRENT_USER = 1  # type: ignore[attr-defined]
    fake_winreg.HKEY_CLASSES_ROOT = 2  # type: ignore[attr-defined]

    def _open_key(hive_or_key: Any, subkey: str) -> _FakeKey:
        if isinstance(hive_or_key, _FakeKey):
            full_path = f"{hive_or_key.path}\\{subkey}"
            hive = hive_or_key.hive
        else:
            full_path = subkey
            hive = int(hive_or_key)
        if (hive, full_path) not in registry_state:
            raise FileNotFoundError(full_path)
        return _FakeKey(hive, full_path)

    def _query_value_ex(key: _FakeKey, name: str) -> tuple[str, int]:
        vals = registry_state.get((key.hive, key.path), {})
        if name not in vals:
            raise FileNotFoundError(name)
        return vals[name], 1

    fake_winreg.OpenKey = _open_key  # type: ignore[attr-defined]
    fake_winreg.QueryValueEx = _query_value_ex  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    shell_targets: list[str] = []
    popen_calls: list[list[str]] = []

    # 1. App installed (`exampletool` URL Protocol exists in registry) -> routes natively!
    res_installed = windows_os.open_url(
        "https://app.exampletool.com/projects/alpha",
        app_uri_handler_finder=lambda _u: [],
        app_catalog_fetcher=list,
        window_enumerator=list,
        startfile_fn=shell_targets.append,
        popen_fn=lambda cmd, **_kw: popen_calls.append(list(cmd)),
        desktop_name_fn=lambda: "Default",
    )
    assert res_installed["ok"] is True
    assert res_installed["routed_to_installed_app"] is True
    assert res_installed["resolution_layer"] == "url_protocol"
    assert res_installed["protocol_scheme"] == "exampletool"
    assert res_installed["app_name"] == "ExampleTool"
    assert "Do NOT call open_url again" in res_installed["guidance"]
    assert shell_targets == ["exampletool:"]
    assert popen_calls == []

    # 2. App uninstalled (registry key disappears) -> automatically falls back to browser!
    registry_state.clear()
    shell_targets.clear()
    res_uninstalled = windows_os.open_url(
        "https://app.exampletool.com/projects/alpha",
        app_uri_handler_finder=lambda _u: [],
        app_catalog_fetcher=list,
        window_enumerator=list,
        startfile_fn=shell_targets.append,
        popen_fn=lambda cmd, **_kw: popen_calls.append(list(cmd)),
        desktop_name_fn=lambda: "Default",
    )
    assert res_uninstalled["ok"] is True
    assert res_uninstalled["routed_to_installed_app"] is False
    assert res_uninstalled["launched_app"] is False
    assert res_uninstalled["status"] == "opened"
    assert shell_targets == ["https://app.exampletool.com/projects/alpha"]


def test_layer3_running_pwa_intercepts_deep_url_and_cache_invalidates_on_install_uninstall() -> (
    None
):
    """Layer 3 focuses open standalone PWA on deep URL and invalidates cache on catalog changes."""
    focused_hwnds: list[int] = []
    restored_hwnds: list[int] = []
    popen_calls: list[list[str]] = []
    shell_calls: list[str] = []

    class _FakeU32:
        def GetForegroundWindow(self) -> int:
            return 900

        def IsIconic(self, hwnd: int) -> bool:
            return hwnd == 501

        def ShowWindow(self, hwnd: int, cmd: int) -> bool:
            if cmd == windows_os.SW_RESTORE:
                restored_hwnds.append(hwnd)
            return True

        def SetForegroundWindow(self, hwnd: int) -> bool:
            focused_hwnds.append(hwnd)
            return True

    # Both a Chrome browser tab and a standalone YouTube PWA exist; deep URL must pick PWA #501
    open_windows = [
        windows_os.EnumeratedWindow(
            hwnd=502,
            title="Watch Later - YouTube - Google Chrome",
            process_name="chrome.exe",
            is_visible=True,
        ),
        windows_os.EnumeratedWindow(
            hwnd=501,
            title="YouTube",
            process_name="chrome.exe",
            is_visible=True,
        ),
    ]

    res_pwa = windows_os.open_url(
        "https://www.youtube.com/playlist?list=WL",
        app_uri_handler_finder=lambda _u: [],
        url_protocol_checker=lambda _s: None,
        app_catalog_fetcher=lambda: [{"Name": "YouTube", "AppID": "Chrome._crx_yt"}],
        window_enumerator=lambda: open_windows,
        user32=_FakeU32(),
        popen_fn=lambda cmd, **_kw: popen_calls.append(list(cmd)),
        startfile_fn=shell_calls.append,
        desktop_name_fn=lambda: "Default",
    )

    assert res_pwa["ok"] is True
    assert res_pwa["status"] == "focused"
    assert res_pwa["routed_to_installed_app"] is True
    assert res_pwa["resolution_layer"] == "installed_app"
    assert res_pwa["window_hwnd"] == 501
    assert restored_hwnds == [501]
    assert focused_hwnds == [501]
    assert popen_calls == []
    assert shell_calls == []
    assert "Do NOT call open_url again" in res_pwa["guidance"]

    # Cache invalidation test 1: newly installed app picked up automatically without restart
    live_catalog: list[dict[str, str]] = []
    # Prime empty cache
    assert windows_os.get_installed_apps(app_catalog_fetcher=lambda: list(live_catalog)) == []
    # User installs "FreshNotes" while Clea is running
    live_catalog.append({"Name": "FreshNotes", "AppID": "FreshNotes.App!Main"})
    res_new = windows_os.open_or_focus_app(
        "FreshNotes",
        user32=_FakeU32(),
        window_enumerator=list,
        app_catalog_fetcher=lambda: list(live_catalog),
        popen_fn=lambda cmd, **_kw: popen_calls.append(list(cmd)),
        desktop_name_fn=lambda: "Default",
    )
    assert res_new["ok"] is True
    assert res_new["status"] == "launched"
    assert res_new["app"] == "FreshNotes"

    # Cache invalidation test 2: cached app uninstalled -> launch raises OSError -> invalidates
    # cache, refreshes, sees it's gone, and falls back cleanly to browser!
    live_catalog.clear()
    shell_calls.clear()
    popen_calls.clear()

    def _failing_cached_launch(cmd: list[str], **_kw: Any) -> None:
        popen_calls.append(list(cmd))
        raise OSError("AppsFolder target uninstalled")

    # Cache still thinks FreshNotes is installed, but live_catalog is now empty
    res_uninstalled = windows_os.open_url(
        "https://app.freshnotes.io/notes/1",
        app_uri_handler_finder=lambda _u: [],
        url_protocol_checker=lambda _s: None,
        app_catalog_fetcher=lambda: list(live_catalog),
        window_enumerator=list,
        user32=_FakeU32(),
        popen_fn=_failing_cached_launch,
        startfile_fn=shell_calls.append,
        desktop_name_fn=lambda: "Default",
    )
    assert res_uninstalled["ok"] is True
    assert res_uninstalled["routed_to_installed_app"] is False
    assert res_uninstalled["status"] == "opened"
    assert shell_calls == ["https://app.freshnotes.io/notes/1"]


# ===========================================================================
# 5. Explicit Browser Bypass & Strict Single-Instance Reuse
# ===========================================================================


def test_explicit_browser_request_bypasses_app_and_reuses_existing_browser_window() -> None:
    """'open Watch Later in Chrome' skips installed app and focuses/reuses existing Chrome HWND."""
    focused_hwnds: list[int] = []
    popen_calls: list[list[str]] = []

    class _FakeU32:
        def GetForegroundWindow(self) -> int:
            return 100

        def IsIconic(self, _hwnd: int) -> bool:
            return False

        def SetForegroundWindow(self, hwnd: int) -> bool:
            focused_hwnds.append(hwnd)
            return True

    # Two Chrome windows exist: #701 generic tab, #702 already on YouTube!
    windows = [
        windows_os.EnumeratedWindow(
            hwnd=701,
            title="New Tab - Google Chrome",
            process_name="chrome.exe",
            is_visible=True,
        ),
        windows_os.EnumeratedWindow(
            hwnd=702,
            title="Subscriptions - YouTube - Google Chrome",
            process_name="chrome.exe",
            is_visible=True,
        ),
        windows_os.EnumeratedWindow(
            hwnd=703,
            title="YouTube",
            process_name="chrome.exe",
            is_visible=True,
        ),
    ]

    res = windows_os.open_url(
        "https://www.youtube.com/playlist?list=WL",
        user_request="open Watch Later in Chrome",
        app_uri_handler_finder=lambda _u: [{"name": "YouTube", "app_id": "YT!App"}],
        url_protocol_checker=lambda _s: {"scheme": "youtube", "app_name": "YouTube"},
        app_catalog_fetcher=lambda: [{"Name": "YouTube", "AppID": "Chrome._crx_yt"}],
        window_enumerator=lambda: windows,
        user32=_FakeU32(),
        which_fn=lambda exe: r"C:\Chrome\chrome.exe" if exe == "chrome.exe" else None,
        popen_fn=lambda cmd, **_kw: popen_calls.append(list(cmd)),
        desktop_name_fn=lambda: "Default",
    )

    assert res["ok"] is True
    assert res["routed_to_installed_app"] is False
    assert res["launched_app"] is False
    assert res["browser"] == "chrome"
    assert res["reused_browser_window"] is True
    # Focused the existing Chrome window already on YouTube (#702), NOT standalone PWA #703
    assert res["window_hwnd"] == 702
    assert focused_hwnds == [702]
    assert len(popen_calls) == 1
    assert "--new-window" not in popen_calls[0]
    assert popen_calls[0] == [r"C:\Chrome\chrome.exe", "https://www.youtube.com/playlist?list=WL"]


def test_repeated_open_or_focus_app_restores_existing_hwnd_without_spawning_duplicate() -> None:
    """Repeated open_or_focus_app calls restore and focus the existing HWND with zero relaunch."""
    focused: list[int] = []
    restored: list[int] = []
    popen_calls: list[list[str]] = []

    class _FakeU32:
        def GetForegroundWindow(self) -> int:
            return 10

        def IsIconic(self, hwnd: int) -> bool:
            return hwnd == 888

        def ShowWindow(self, hwnd: int, cmd: int) -> bool:
            if cmd == windows_os.SW_RESTORE:
                restored.append(hwnd)
            return True

        def SetForegroundWindow(self, hwnd: int) -> bool:
            focused.append(hwnd)
            return True

    # Already-open minimized Spotify window whose title is the song name (matched via process)
    running = [
        windows_os.EnumeratedWindow(
            hwnd=888,
            title="Daft Punk - Instant Crush",
            process_name="Spotify.exe",
            is_visible=True,
        )
    ]

    for _ in range(3):
        res = windows_os.open_or_focus_app(
            "Spotify",
            user32=_FakeU32(),
            window_enumerator=lambda: running,
            app_catalog_fetcher=lambda: [{"Name": "Spotify Music", "AppID": "SpotifyAB!Spotify"}],
            popen_fn=lambda cmd, **_kw: popen_calls.append(list(cmd)),
            desktop_name_fn=lambda: "Default",
        )
        assert res["ok"] is True
        assert res["status"] == "focused"
        assert res["hwnd"] == 888

    assert popen_calls == []
    assert restored == [888, 888, 888]
    assert focused == [888, 888, 888]


# ===========================================================================
# 6. Protocol Schema & Chat/Voice Prompt Alignment
# ===========================================================================


def test_protocol_open_url_models_support_routed_app_and_browser_reuse_fields() -> None:
    params = OsOpenUrlParams(
        url="https://www.youtube.com/playlist?list=WL",
        prefer_browser=False,
        user_request="open watch later",
    )
    assert params.prefer_browser is False
    result = OsOpenUrlResult(
        ok=True,
        success=True,
        status="focused",
        url=params.url,
        routed_to_installed_app=True,
        app_name="YouTube",
        window_hwnd=501,
        resolution_layer="installed_app",
        guidance="Installed app is focused.",
        reused_browser_window=False,
    )
    assert result.routed_to_installed_app is True
    assert result.window_hwnd == 501
    assert result.resolution_layer == "installed_app"


def test_chat_and_voice_prompts_enforce_single_instance_app_first_continuation() -> None:
    """Both prompts remove the YouTube browser-URL contradiction and enforce single-window reuse."""
    for label, prompt in (
        ("chat", _BASE_SYSTEM_PROMPT),
        ("voice", BASE_VOICE_SYSTEM_INSTRUCTION),
    ):
        low = prompt.lower()
        assert "https://www.youtube.com/feed/history" not in low, (
            f"{label} prompt still encourages calling open_url for installed YouTube app"
        )
        assert "stay inside" in low and "single app window" in low, (
            f"{label} prompt must instruct staying inside the single installed app window"
        )
        assert "duplicate app or browser windows" in low, (
            f"{label} prompt must forbid spawning duplicate app or browser windows"
        )
        assert "explicitly" in low and "browser" in low, (
            f"{label} prompt must restrict browser usage to explicit requests or missing apps"
        )


# ===========================================================================
# 7. Adversarial Edge Cases (Review Pass Fixes)
# ===========================================================================


def test_resample_scales_float32_with_overshoot_or_nan_while_preserving_int16_scale() -> None:
    """Normalized float32 with slight overshoot (>1.0) or isolated NaN still scales to int16."""
    # 1. Slight driver/AGC overshoot (1.05 peak) at 48 kHz
    t48 = np.arange(3840, dtype=np.float32) / 48000.0
    overshoot = (0.5 * np.sin(2.0 * np.pi * 440.0 * t48)).astype(np.float32)
    overshoot[100] = 1.05
    overshoot[200] = -1.08
    res_over = resample_to_16k_mono(overshoot, orig_sr=48000, target_sr=16000)
    assert res_over.dtype == np.int16
    assert len(res_over) == 1280
    assert compute_audio_rms(res_over) > 9000.0

    # 2. Normalized float32 containing an isolated NaN still scales finite samples
    with_nan = np.array([0.5, np.nan, -0.5, 0.25], dtype=np.float32)
    res_nan = resample_to_16k_mono(with_nan, orig_sr=16000, target_sr=16000)
    assert res_nan.dtype == np.int16
    assert res_nan[0] == 16384
    assert res_nan[1] == 0
    assert res_nan[2] == -16384

    # 3. Already-int16-scaled float32 array is NOT re-scaled
    scaled_f32 = np.array([1000.0, np.nan, -1000.0, np.inf, -np.inf], dtype=np.float32)
    res_scaled = resample_to_16k_mono(scaled_f32, orig_sr=16000, target_sr=16000)
    assert list(res_scaled) == [1000, 0, -1000, 32767, -32768]


@pytest.mark.asyncio
async def test_os_default_audio_hotswap_and_mme_truncation_matching_and_thread_safe_wake_word(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OS default hot-swap rebinds streams to Bluetooth; MME truncation stays on same headset."""
    # 1. MME 31-char truncated headset stays grouped right after its full WASAPI name
    fake_hostapis = [
        {"name": "MME", "default_input_device": 0, "default_output_device": 5},
        {"name": "Windows DirectSound", "default_input_device": 2, "default_output_device": 5},
        {"name": "Windows WASAPI", "default_input_device": 3, "default_output_device": 5},
    ]
    fake_devices = [
        {
            "name": "Microsoft Sound Mapper - Input",
            "hostapi": 0,
            "max_input_channels": 2,
            "max_output_channels": 0,
            "default_samplerate": 44100.0,
        },
        {
            # MME 31-char truncated version of device #3
            "name": "Headset (Sony WH-1000XM5 Hands-",
            "hostapi": 0,
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 44100.0,
        },
        {
            "name": "Primary Sound Capture Driver",
            "hostapi": 1,
            "max_input_channels": 2,
            "max_output_channels": 0,
            "default_samplerate": 44100.0,
        },
        {
            "name": "Headset (Sony WH-1000XM5 Hands-Free AG Audio)",
            "hostapi": 2,
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 16000.0,
        },
        {
            "name": "Microphone Array (Realtek(R) Audio)",
            "hostapi": 2,
            "max_input_channels": 2,
            "max_output_channels": 0,
            "default_samplerate": 48000.0,
        },
        {
            "name": "Speakers (Realtek(R) Audio)",
            "hostapi": 2,
            "max_input_channels": 0,
            "max_output_channels": 2,
            "default_samplerate": 48000.0,
        },
    ]

    monkeypatch.setattr(sd, "query_devices", lambda: fake_devices)
    monkeypatch.setattr(sd, "query_hostapis", lambda: fake_hostapis)
    monkeypatch.setattr(sd.default, "device", [1, 5])
    monkeypatch.setattr(sd, "check_input_settings", lambda **_kw: None)
    monkeypatch.setattr("clea.surfaces.voice.audio.refresh_audio_devices", lambda: None)

    ordered = list_candidate_audio_devices(None, kind="input", preferred_hostapi="WASAPI")
    assert ordered[0]["index"] == 3
    assert ordered[1]["index"] == 1  # Same headset via truncated MME BEFORE laptop mic!

    # 2. OS default audio endpoint hot-swap (e.g. Bluetooth earphones connected after logon):
    # AudioManager detects default change while healthy, and VoiceRunner's watchdog rebinds!
    endpoint_state = [("cap-realtek", "ren-speakers")]
    monkeypatch.setattr(
        "clea.surfaces.voice.audio.get_os_default_audio_endpoint_ids",
        lambda: endpoint_state[0],
    )

    class _OkStream:
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

    monkeypatch.setattr(sd, "InputStream", _OkStream)
    monkeypatch.setattr(sd, "OutputStream", _OkStream)

    mgr = AudioManager(chunk_size=1280)
    mgr.start()
    try:
        assert mgr.has_default_audio_device_changed() == (False, "ok")

        # Transient None from Core Audio does not trigger spurious restart
        endpoint_state[0] = (None, None)  # type: ignore[list-item]
        assert mgr.has_default_audio_device_changed() == (False, "ok")

        # User connects Bluetooth earphones -> both capture & render default IDs change
        endpoint_state[0] = ("cap-earphone", "ren-earphone")
        assert mgr.has_default_audio_device_changed() == (True, "os_default_audio_changed")

        # Pinned devices ignore OS default changes
        mgr.input_device = "Headset"
        mgr.output_device = "Speakers"
        assert mgr.has_default_audio_device_changed() == (False, "pinned")
        mgr.input_device = None
        mgr.output_device = None

        # Wire into VoiceRunner watchdog and verify automatic hot-swap recovery & wake reset
        reset_calls = [0]
        mock_model = MagicMock()
        mock_model.predict.return_value = {"hey_jarvis_v0.1": 0.0}

        def _count_reset() -> None:
            reset_calls[0] += 1

        mock_model.reset.side_effect = _count_reset
        detector = WakeWordDetector(
            model_name="hey_jarvis",
            threshold=0.25,
            model_instance=mock_model,
        )

        restart_calls = [0]
        orig_restart = mgr.restart_streams

        def _spy_restart(*a: Any, **kw: Any) -> bool:
            restart_calls[0] += 1
            return orig_restart(*a, **kw)

        monkeypatch.setattr(mgr, "restart_streams", _spy_restart)

        runner = VoiceRunner(
            config=VoiceConfig(wake_word_model="hey_jarvis", wake_threshold=0.25),
            data_dir=tmp_path,
            backend_factory=_FlakyStartBackend,
            audio_manager=mgr,
            wake_word_detector=detector,
            enable_hotkey=False,
        )
        runner._audio_health_poll_interval = 0.05
        await runner.start()
        try:
            # Flip OS default again after start() so watchdog triggers hot-swap rebind
            endpoint_state[0] = ("cap-earphone-2", "ren-earphone-2")
            for _ in range(80):
                if restart_calls[0] >= 1 and reset_calls[0] >= 2:
                    break
                await asyncio.sleep(0.05)
            assert restart_calls[0] >= 1
            assert reset_calls[0] >= 2  # startup reset + post-hotswap reset
            assert mgr.has_default_audio_device_changed() == (False, "ok")
        finally:
            await runner.stop()
    finally:
        mgr.stop()

    # 3. Concurrent WakeWordDetector.process_audio() and reset() remain thread-safe under RLock
    import concurrent.futures

    conc_model = MagicMock()
    conc_model.predict.return_value = {"hey_jarvis_v0.1": 0.1}
    conc_model.reset = MagicMock()
    conc_det = WakeWordDetector(model_name="hey_jarvis", threshold=0.3, model_instance=conc_model)
    chunk_bytes = (np.full(1280, 500, dtype=np.int16)).tobytes()

    def _run_predicts() -> None:
        for _ in range(50):
            conc_det.process_audio(chunk_bytes)

    def _run_resets() -> None:
        for _ in range(50):
            conc_det.reset()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_run_predicts)
        f2 = pool.submit(_run_resets)
        f1.result()
        f2.result()


def test_layer3_open_url_invalidates_stale_cache_when_new_app_installed() -> None:
    """open_url refreshes a stale installed-app cache to discover a newly installed app."""
    live_catalog: list[dict[str, str]] = []
    # Prime empty cache before installation
    assert windows_os.get_installed_apps(app_catalog_fetcher=lambda: list(live_catalog)) == []

    # User installs FreshNotes while Clea is running
    live_catalog.append({"Name": "FreshNotes", "AppID": "FreshNotes.App!Main"})
    popen_calls: list[list[str]] = []
    shell_calls: list[str] = []

    res = windows_os.open_url(
        "https://app.freshnotes.io/notes/1",
        app_uri_handler_finder=lambda _u: [],
        url_protocol_checker=lambda _s: None,
        app_catalog_fetcher=lambda: list(live_catalog),
        window_enumerator=list,
        popen_fn=lambda cmd, **_kw: popen_calls.append(list(cmd)),
        startfile_fn=shell_calls.append,
        desktop_name_fn=lambda: "Default",
    )

    assert res["ok"] is True
    assert res["routed_to_installed_app"] is True
    assert res["resolution_layer"] == "installed_app"
    assert res["app_name"] == "FreshNotes"
    assert shell_calls == []
    assert popen_calls == [["explorer.exe", r"shell:AppsFolder\FreshNotes.App!Main"]]


def test_minimized_standalone_window_and_hyphenated_domain_match_without_duplicate() -> None:
    """Minimized windows (is_visible=False, IsIconic=True) and hyphenated domains restore."""
    restored: list[int] = []
    focused: list[int] = []
    popen_calls: list[list[str]] = []
    shell_calls: list[str] = []

    class _FakeU32:
        def GetForegroundWindow(self) -> int:
            return 100

        def IsIconic(self, hwnd: int) -> bool:
            return hwnd in (601, 602)

        def ShowWindow(self, hwnd: int, cmd: int) -> bool:
            if cmd == windows_os.SW_RESTORE:
                restored.append(hwnd)
            return True

        def SetForegroundWindow(self, hwnd: int) -> bool:
            focused.append(hwnd)
            return True

    windows = [
        windows_os.EnumeratedWindow(
            hwnd=601,
            title="Fresh Notes - Workspace",
            process_name="chrome.exe",
            is_visible=False,  # Minimized!
        ),
        windows_os.EnumeratedWindow(
            hwnd=602,
            title="notes.txt - Notepad",
            process_name="notepad.exe",
            is_visible=False,  # Minimized!
        ),
    ]

    assert "fresh notes" in windows_os.extract_url_app_tokens("https://app.fresh-notes.io/doc/1")

    res_pwa = windows_os.open_url(
        "https://app.fresh-notes.io/doc/1",
        app_uri_handler_finder=lambda _u: [],
        url_protocol_checker=lambda _s: None,
        app_catalog_fetcher=list,
        window_enumerator=lambda: windows,
        user32=_FakeU32(),
        popen_fn=lambda cmd, **_kw: popen_calls.append(list(cmd)),
        startfile_fn=shell_calls.append,
        desktop_name_fn=lambda: "Default",
    )
    assert res_pwa["ok"] is True
    assert res_pwa["status"] == "focused"
    assert res_pwa["routed_to_installed_app"] is True
    assert res_pwa["window_hwnd"] == 601
    assert restored == [601]
    assert focused == [601]
    assert popen_calls == []
    assert shell_calls == []

    res_np = windows_os.open_or_focus_app(
        "notepad",
        user32=_FakeU32(),
        window_enumerator=lambda: windows,
        popen_fn=lambda cmd, **_kw: popen_calls.append(list(cmd)),
        desktop_name_fn=lambda: "Default",
    )
    assert res_np["ok"] is True
    assert res_np["status"] == "focused"
    assert res_np["hwnd"] == 602
    assert 602 in restored
    assert popen_calls == []


@pytest.mark.asyncio
async def test_layer1_winrt_app_uri_handler_runs_safely_inside_active_asyncio_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_find_app_uri_handlers runs WinRT op safely on a worker thread inside a running loop."""
    monkeypatch.setattr(sys, "platform", "win32")
    caller_loop = asyncio.get_running_loop()

    class _FakeDisplayInfo:
        display_name = "LoopSafe Store App"

    class _FakeAppInfo:
        display_info = _FakeDisplayInfo()
        app_user_model_id = "LoopSafe.StoreApp_abc!App"
        package_family_name = "LoopSafe.StoreApp_abc"

    class _FakeUri:
        def __init__(self, raw: str) -> None:
            self.raw = raw
            # Must NOT be created on the caller's already-running loop!
            with pytest.raises(RuntimeError):
                asyncio.get_running_loop()

    class _FakeLauncher:
        @staticmethod
        def find_app_uri_handlers_async(uri: _FakeUri) -> Any:
            async def _coro() -> list[_FakeAppInfo]:
                assert asyncio.get_running_loop() is not caller_loop
                assert uri.raw == "https://loopsafe.example/item/1"
                return [_FakeAppInfo()]

            return _coro()

    winrt_mod = types.ModuleType("winrt")
    winrt_win_mod = types.ModuleType("winrt.windows")
    winrt_found_mod = types.ModuleType("winrt.windows.foundation")
    winrt_sys_mod = types.ModuleType("winrt.windows.system")
    winrt_found_mod.Uri = _FakeUri  # type: ignore[attr-defined]
    winrt_sys_mod.Launcher = _FakeLauncher  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "winrt", winrt_mod)
    monkeypatch.setitem(sys.modules, "winrt.windows", winrt_win_mod)
    monkeypatch.setitem(sys.modules, "winrt.windows.foundation", winrt_found_mod)
    monkeypatch.setitem(sys.modules, "winrt.windows.system", winrt_sys_mod)

    handlers = windows_os._find_app_uri_handlers("https://loopsafe.example/item/1")
    assert handlers == [
        {
            "name": "LoopSafe Store App",
            "app_id": "LoopSafe.StoreApp_abc!App",
            "package_family_name": "LoopSafe.StoreApp_abc",
        }
    ]


@pytest.mark.asyncio
async def test_open_or_focus_app_browser_reuse_and_voice_runner_open_url_context(
    tmp_path: Path,
) -> None:
    """open_or_focus_app propagates browser reuse HWND and VoiceRunner forwards user context."""
    focused: list[int] = []
    popen_calls: list[list[str]] = []

    class _FakeU32:
        def GetForegroundWindow(self) -> int:
            return 10

        def IsIconic(self, _hwnd: int) -> bool:
            return False

        def SetForegroundWindow(self, hwnd: int) -> bool:
            focused.append(hwnd)
            return True

    chrome_win = windows_os.EnumeratedWindow(
        hwnd=702,
        title="Home - YouTube - Google Chrome",
        process_name="chrome.exe",
        is_visible=True,
    )
    res_browser = windows_os.open_or_focus_app(
        "open YouTube in Chrome",
        user32=_FakeU32(),
        window_enumerator=lambda: [chrome_win],
        which_fn=lambda exe: r"C:\Chrome\chrome.exe" if exe == "chrome.exe" else None,
        popen_fn=lambda cmd, **_kw: popen_calls.append(list(cmd)),
        desktop_name_fn=lambda: "Default",
    )
    assert res_browser["ok"] is True
    assert res_browser["launched_app"] is False
    assert res_browser["reused_browser_window"] is True
    assert res_browser["hwnd"] == 702
    assert focused == [702]
    assert "--new-window" not in popen_calls[0]

    # VoiceRunner open_url forwards latest user transcript browser request AND does not lie
    # about routed_to_installed_app when open_or_focus_app fell back to browser.
    from clea.core.protocol import OsOpenOrFocusAppRequest, OsOpenUrlRequest
    from clea.surfaces.voice.backends.base import ToolCallEvent

    sent_responses: list[VoiceToolResponse] = []
    rpc_calls: list[Any] = []

    class _CapturingBackend(_FlakyStartBackend):
        async def send_tool_responses(self, responses: list[VoiceToolResponse]) -> None:
            sent_responses.extend(responses)

    runner = VoiceRunner(
        config=VoiceConfig(),
        data_dir=tmp_path,
        backend_factory=_CapturingBackend,
        enable_hotkey=False,
    )
    backend = _CapturingBackend()

    async def _fake_rpc(req: Any, *, timeout: float = 10.0) -> dict[str, Any]:
        rpc_calls.append(req)
        if isinstance(req, OsOpenOrFocusAppRequest):
            # Simulate fallback to browser (no installed app for github)
            return {
                "ok": True,
                "status": "launched",
                "app": "github",
                "launched_app": False,
                "browser": "default",
                "url": "https://github.com",
            }
        if isinstance(req, OsOpenUrlRequest):
            return {"ok": True, "status": "opened", "url": req.params.url, "browser": "chrome"}
        return {"ok": True}

    runner._rpc = _fake_rpc  # type: ignore[method-assign]

    # Case 1: Root URL where open_or_focus_app falls back to browser must NOT claim app routing
    await runner._handle_tool_call(
        ToolCallEvent(
            call_id="c-gh",
            name="open_url",
            arguments={"url": "https://github.com"},
        ),
        backend,
    )
    assert sent_responses[-1].response["routed_to_installed_app"] is False
    assert "guidance" not in sent_responses[-1].response

    # Case 2: Current user utterance explicitly requested Chrome -> forwarded to OsOpenUrlRequest
    runner.reset_turn_state()
    runner._transcript_history.append({"role": "user", "text": "open Watch Later in Chrome"})
    await runner._handle_tool_call(
        ToolCallEvent(
            call_id="c-yt-chrome",
            name="open_url",
            arguments={"url": "https://www.youtube.com/playlist?list=WL"},
        ),
        backend,
    )
    assert isinstance(rpc_calls[-1], OsOpenUrlRequest)
    assert rpc_calls[-1].params.prefer_browser is True
    assert rpc_calls[-1].params.user_request == "open Watch Later in Chrome"


def test_audio_manager_batches_variable_wasapi_periods_without_drift_or_snr_loss() -> None:
    """Native WASAPI 10ms / 448-frame callbacks batch before polyphase resampling (zero drift)."""
    for orig_sr, cb_frames in ((44100, 448), (48000, 480), (48000, 144)):
        t = np.arange(orig_sr, dtype=np.float32) / float(orig_sr)
        sine = (np.sin(2.0 * np.pi * 440.0 * t) * 16000.0).astype(np.int16)

        emitted: list[np.ndarray] = []

        def _on_chunk(pcm: bytes, _out: list[np.ndarray] = emitted) -> None:
            arr = np.frombuffer(pcm, dtype=np.int16)
            assert len(arr) == 1280
            _out.append(arr)

        mgr = AudioManager(chunk_size=1280, on_audio_input=_on_chunk)
        mgr.input_sample_rate = orig_sr
        mgr.input_channels = 1

        # Reuse a single buffer across callbacks just like PortAudio's C buffer
        reusable = np.empty((cb_frames, 1), dtype=np.int16)
        for off in range(0, len(sine) - cb_frames + 1, cb_frames):
            reusable[:, 0] = sine[off : off + cb_frames]
            mgr._input_stream_callback(reusable, cb_frames, None, sd.CallbackFlags())

        assert len(emitted) >= 11
        cat = np.concatenate(emitted)
        t_ref = np.arange(len(cat), dtype=np.float32) / 16000.0
        ref = np.sin(2.0 * np.pi * 440.0 * t_ref) * 16000.0
        err = cat.astype(np.float32) - ref
        snr_db = float(10.0 * np.log10(np.mean(ref**2) / (np.mean(err**2) + 1e-9)))
        assert snr_db > 38.0, f"SNR degraded at {orig_sr} Hz cb={cb_frames}: {snr_db:.2f} dB"

    # Empty callback must never clear an existing dead-zero streak
    mgr_z = AudioManager(chunk_size=1280)
    mgr_z.input_sample_rate = 16000
    zeros_1280 = np.zeros((1280, 1), dtype=np.int16)
    mgr_z._input_stream_callback(zeros_1280, 1280, None, sd.CallbackFlags())
    assert mgr_z._consecutive_zero_callbacks == 1
    mgr_z._input_stream_callback(np.empty((0, 1), dtype=np.int16), 0, None, sd.CallbackFlags())
    assert mgr_z._consecutive_zero_callbacks == 1


def test_audio_manager_start_failure_never_deadlocks_with_output_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OutputStream failure teardown does not deadlock against _output_stream_callback lock."""
    import threading
    import time

    deadlocked = threading.Event()
    fake_devices = [
        {
            "index": 0,
            "name": "Test Headset",
            "hostapi": 0,
            "hostapi_name": "MME",
            "max_input_channels": 1,
            "max_output_channels": 2,
            "default_samplerate": 16000.0,
        }
    ]

    class _DummyIn:
        def __init__(self, **_kw: Any) -> None:
            self.active = True
            self.stopped = False

        def start(self) -> None:
            pass

        def stop(self) -> None:
            self.active = False

        def close(self) -> None:
            pass

    class _CallbackRacingOut:
        def __init__(self, callback: Any = None, **_kw: Any) -> None:
            self._cb = callback
            self._stop = threading.Event()
            self._t: threading.Thread | None = None

        def start(self) -> None:
            def _run() -> None:
                out = np.zeros((240, 1), dtype=np.int16)
                while not self._stop.is_set():
                    self._cb(out, 240, None, sd.CallbackFlags())
                    time.sleep(0.002)

            self._t = threading.Thread(target=_run, daemon=True)
            self._t.start()
            time.sleep(0.03)
            raise sd.PortAudioError("Simulated post-start output failure")

        def stop(self) -> None:
            self._stop.set()
            if self._t is not None:
                self._t.join(timeout=0.5)
                if self._t.is_alive():
                    deadlocked.set()

        def close(self) -> None:
            pass

    monkeypatch.setattr("clea.surfaces.voice.audio.list_audio_devices", lambda: fake_devices)
    monkeypatch.setattr(sd, "check_input_settings", lambda **_kw: None)
    monkeypatch.setattr(sd, "InputStream", _DummyIn)
    monkeypatch.setattr(sd, "OutputStream", _CallbackRacingOut)
    monkeypatch.setattr(
        "clea.surfaces.voice.audio.get_os_default_audio_endpoint_ids",
        lambda: ("cap-0", "ren-0"),
    )

    # 1. Eager output startup failure tears down both streams cleanly without deadlocking
    mgr = AudioManager(input_device=0, output_device=0, lazy_output=False)
    with pytest.raises(sd.PortAudioError):
        mgr.start()

    assert not deadlocked.is_set()
    assert mgr._running is False
    assert mgr._input_stream is None
    assert mgr._output_stream is None

    # 2. Default lazy-output mode stays suspended in IDLE and fails cleanly on demand
    mgr_lazy = AudioManager(input_device=0, output_device=0)
    mgr_lazy.start()
    try:
        assert mgr_lazy._running is True
        assert mgr_lazy.is_output_stream_active is False
        assert mgr_lazy._output_stream is None
        with pytest.raises(sd.PortAudioError):
            mgr_lazy.start_output_stream()
        assert not deadlocked.is_set()
        assert mgr_lazy._output_stream is None
    finally:
        mgr_lazy.stop()


def test_get_os_default_audio_endpoint_ids_does_not_mutate_global_ole32() -> None:
    """Core Audio endpoint query uses private ole32 handle (never mutates ctypes.windll.ole32)."""
    import ctypes
    import sys

    if sys.platform != "win32":
        pytest.skip("Windows-only Core Audio test")

    from clea.surfaces.voice.audio import get_os_default_audio_endpoint_ids

    sentinel = (ctypes.c_void_p,)
    orig_argtypes = getattr(ctypes.windll.ole32.CoCreateInstance, "argtypes", None)
    ctypes.windll.ole32.CoCreateInstance.argtypes = sentinel
    try:
        cap_id, ren_id = get_os_default_audio_endpoint_ids()
        assert tuple(ctypes.windll.ole32.CoCreateInstance.argtypes) == sentinel
        cap_id2, ren_id2 = get_os_default_audio_endpoint_ids()
        assert (cap_id, ren_id) == (cap_id2, ren_id2)
    finally:
        ctypes.windll.ole32.CoCreateInstance.argtypes = orig_argtypes


@pytest.mark.asyncio
async def test_transient_bluetooth_fallback_to_laptop_mic_reclaims_earphone_when_idle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Bluetooth HFP fails transiently at connect and falls back to laptop mic, reclaim idle."""
    fake_hostapis = [
        {"name": "Windows DirectSound", "default_input_device": 1, "default_output_device": 3},
        {"name": "Windows WASAPI", "default_input_device": 0, "default_output_device": 3},
    ]
    fake_devices = [
        {
            "name": "Headset (Earphone)",
            "hostapi": 1,
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 16000.0,
        },
        {
            "name": "Headset (Earphone)",
            "hostapi": 0,
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 44100.0,
        },
        {
            "name": "Microphone Array (Realtek(R) Audio)",
            "hostapi": 1,
            "max_input_channels": 2,
            "max_output_channels": 0,
            "default_samplerate": 48000.0,
        },
        {
            "name": "Headphones (Earphone)",
            "hostapi": 1,
            "max_input_channels": 0,
            "max_output_channels": 2,
            "default_samplerate": 48000.0,
        },
    ]

    bt_ready = [False]

    class _BtHandshakeStream:
        def __init__(self, device: int, **_kw: Any) -> None:
            self.device = device
            self.active = True
            self.stopped = False

        def start(self) -> None:
            if self.device in (0, 1) and not bt_ready[0]:
                raise sd.PortAudioError("Bluetooth HFP SCO handshake still in progress")

        def stop(self) -> None:
            self.active = False
            self.stopped = True

        def close(self) -> None:
            pass

    monkeypatch.setattr(sd, "query_devices", lambda: fake_devices)
    monkeypatch.setattr(sd, "query_hostapis", lambda: fake_hostapis)
    monkeypatch.setattr(sd, "check_input_settings", lambda **_kw: None)
    monkeypatch.setattr(sd, "InputStream", _BtHandshakeStream)
    monkeypatch.setattr(sd, "OutputStream", _BtHandshakeStream)
    monkeypatch.setattr(
        "clea.surfaces.voice.audio.get_os_default_audio_endpoint_ids",
        lambda: ("cap-earphone", "ren-earphone"),
    )
    monkeypatch.setattr("clea.surfaces.voice.audio.refresh_audio_devices", lambda: None)

    mgr = AudioManager(chunk_size=1280)
    mgr._wasapi_retry_delay = 0.0
    mgr._fallback_reclaim_base_delay = 0.01

    mock_model = MagicMock()
    mock_model.predict.return_value = {"hey_jarvis_v0.1": 0.0}
    mock_model.reset = MagicMock()
    detector = WakeWordDetector(model_name="hey_jarvis", threshold=0.25, model_instance=mock_model)

    runner = VoiceRunner(
        config=VoiceConfig(wake_word_model="hey_jarvis", wake_threshold=0.25),
        data_dir=tmp_path,
        backend_factory=_FlakyStartBackend,
        audio_manager=mgr,
        wake_word_detector=detector,
        enable_hotkey=False,
    )
    runner._audio_health_poll_interval = 0.03
    await runner.start()
    try:
        # Initially fell back to Realtek because Bluetooth HFP wasn't ready yet
        assert mgr.resolved_input_device is not None
        assert mgr.resolved_input_device["name"] == "Microphone Array (Realtek(R) Audio)"
        assert mgr._input_fell_back_to_other_device is True

        # Once Bluetooth finishes initializing, idle watchdog reclaims Headset (Earphone)
        bt_ready[0] = True
        for _ in range(40):
            if (
                mgr.resolved_input_device is not None
                and mgr.resolved_input_device["name"] == "Headset (Earphone)"
            ):
                break
            await asyncio.sleep(0.03)
        assert mgr.resolved_input_device is not None
        assert mgr.resolved_input_device["name"] == "Headset (Earphone)"
        assert mgr._input_fell_back_to_other_device is False
        assert mgr.should_reclaim_primary_input() == (False, "ok")
    finally:
        await runner.stop()


@pytest.mark.asyncio
async def test_voice_runner_cancelled_start_call_restores_idle_and_wake_word(
    tmp_path: Path,
) -> None:
    """Cancelling start_call mid-connect closes backend and restores IDLE so wake word works."""
    connect_started = asyncio.Event()
    never_finish = asyncio.Event()

    class _HangingBackend(_FlakyStartBackend):
        async def connect(self, config: VoiceSessionConfig) -> None:
            connect_started.set()
            await never_finish.wait()

    mock_model = MagicMock()
    mock_model.predict.return_value = {"hey_jarvis_v0.1": 0.0}
    mock_model.reset = MagicMock()
    detector = WakeWordDetector(model_name="hey_jarvis", threshold=0.25, model_instance=mock_model)

    audio = MagicMock(spec=AudioManager)
    audio._health_monitored = False
    audio.resolved_input_device = None
    audio.resolved_output_device = None
    audio.is_speaking = False

    runner = VoiceRunner(
        config=VoiceConfig(wake_word_model="hey_jarvis", wake_threshold=0.25),
        data_dir=tmp_path,
        backend_factory=_HangingBackend,
        audio_manager=audio,
        wake_word_detector=detector,
        enable_hotkey=False,
    )
    await runner.start()
    try:
        task = asyncio.create_task(runner.start_call(trigger="wake_word"))
        await asyncio.wait_for(connect_started.wait(), timeout=2.0)
        assert runner.call_state == "connecting"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runner.call_state == "idle"
        assert runner._wake_word_pending_trigger is False
        assert runner.call_id is None
    finally:
        await runner.stop()
