# ADR 0006: Windows WASAPI Sample-Rate Negotiation & openWakeWord 1280-Sample Framing

- **Date**: 2026-11-13
- **Status**: Accepted

## Context

During Step 1 hardware verification on Windows 11 across physical microphones (Realtek Microphone Array running at native 48000 Hz stereo, and Headset Earphone running at native 16000 Hz mono), two audio pipeline challenges were encountered:

1. **Windows WASAPI `PaErrorCode -9997` (Invalid sample rate)**:
   Attempting to open a `sounddevice.InputStream` directly at 16000 Hz on Windows WASAPI devices whose hardware endpoints operate natively at 48000 Hz or 44100 Hz fails with `PaErrorCode -9997` when PortAudio/WASAPI does not perform automatic driver-level conversion.
2. **`openWakeWord` 1280-Sample `int16` Framing Requirement**:
   The `openWakeWord` ONNX models (e.g. `hey_jarvis_v0.1.onnx`) strictly expect 16 kHz 16-bit mono PCM in non-overlapping 1280-sample (80 ms / 2560 bytes) windows formatted as 1D `numpy.int16` arrays. Passing unaligned chunks, multi-channel audio, float arrays, or incomplete frames degrades detection accuracy, causes shape mismatches, or triggers false rejections.
3. **Threshold Calibration**:
   Hardware probe measurements showed physical wake phrase peaks of **0.709** and **0.480** on Realtek (48 kHz resampled) and **0.383** on Headset (16 kHz native), against a background speech/noise ceiling of **0.176**. The previous default threshold of `0.50` caused false negatives on lower-sensitivity headsets.

## Decision

1. **Hardware Rate Negotiation (`negotiate_input_sample_rate`)**:
   - `AudioManager.start()` and diagnostic checks query the selected device's native `default_samplerate` and test candidate rates (`default_samplerate`, 16000, 48000, 44100) and channels (1 then 2) using `sounddevice.check_input_settings`.
   - The stream opens at the hardware's supported native rate and channel count with `blocksize = int(sample_rate * 0.08)`.

2. **Polyphase Rational Resampling (`resample_to_16k_mono`)**:
   - Audio callbacks downmix multi-channel frames to mono and resample to 16000 Hz using `scipy.signal.resample_poly` with integer up/down factors calculated from `math.gcd(orig_sr, 16000)`.
   - Output arrays are clipped to `[-32768, 32767]` and formatted as contiguous `np.int16`.

3. **Strict 1280-Sample FIFO Buffer (`AudioChunkBuffer`)**:
   - Resampled audio is extended into a thread-safe FIFO buffer (`AudioChunkBuffer(chunk_size=1280)`).
   - Only complete 1280-sample (2560 bytes) 1D `np.int16` chunks are popped and forwarded to `openWakeWord` during idle wake listening and to `GeminiLiveBackend` during active calls.

4. **WASAPI Prioritization & Substring Resolution (`resolve_audio_device`)**:
   - Supports device resolution by case-insensitive name substring or integer index.
   - Disambiguates duplicate Windows device endpoints by prioritizing `preferred_hostapi="WASAPI"` over MME and DirectSound.

5. **Calibrated Wake Threshold & Toggle Defaults (`voice_config.toml`)**:
   - Default `wake_threshold` is calibrated to **`0.30`** (safely above the 0.176 background ceiling and below the 0.383 headset detection score).
   - `vad_mode = "server"` is the default VAD mode (utilizing server-side Gemini Live VAD).
   - `mic_mute_while_speaking = false` is the default full-duplex setting.

6. **Stall Watchdog & UI Error Surfacing**:
   - Outbound audio streamed for `stall_timeout_seconds` (default 15 s) with zero server responses triggers loud logging and an automatic reconnection using the live session resumption handle. A second consecutive stall terminates the call cleanly.
   - The Clea daemon verifies voice process presence on `voice.start_call` from the UI; if offline or if a stream error occurs, an explicit error state is surfaced in `CallOverlay` rather than failing silently.
