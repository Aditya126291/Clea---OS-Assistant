"""Gemini Live API voice backend implementation using google.genai.

All google.genai SDK types and wire models are strictly encapsulated inside this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from google.genai import Client, types

from clea.surfaces.voice.backends.base import (
    AudioOutputEvent,
    BackendErrorEvent,
    GoAwayEvent,
    InterruptedEvent,
    SessionResumptionUpdateEvent,
    ToolCallCancellationEvent,
    ToolCallEvent,
    TranscriptOutputEvent,
    TurnCompleteEvent,
    UsageUpdateEvent,
    VoiceBackend,
    VoiceBackendEvent,
    VoiceSessionConfig,
    VoiceToolResponse,
)

logger = logging.getLogger(__name__)

DEFAULT_LIVE_MODEL = "gemini-3.8-live"


class GeminiLiveBackend(VoiceBackend):
    """Voice backend communicating with Google Gemini Live API."""

    backend_name = "gemini-live"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_name: str | None = None,
        client: Client | None = None,
    ) -> None:
        resolved_key = (
            api_key
            if api_key is not None
            else (os.environ.get("GEMINI_LIVE_API_KEY") or os.environ.get("GEMINI_API_KEY", ""))
        )
        self._api_key = resolved_key
        self._model_name = (
            model_name
            if model_name is not None
            else os.environ.get("CLEA_LIVE_MODEL", DEFAULT_LIVE_MODEL)
        )
        self._client = client
        self._session: Any = None
        self._session_ctx: Any = None
        self._config: VoiceSessionConfig | None = None
        self._resumption_handle: str | None = None
        self._events_queue: asyncio.Queue[VoiceBackendEvent | None] = asyncio.Queue()
        self._receive_task: asyncio.Task[None] | None = None
        self._closed = True
        self._reconnect_count = 0
        self._last_assistant_transcript: tuple[str, bool] | None = None
        self._turn_completed: bool = False

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def resumption_handle(self) -> str | None:
        """Current session resumption handle if available."""
        return self._resumption_handle

    async def connect(self, config: VoiceSessionConfig) -> None:
        """Establish a live WebSocket session with Gemini Live API."""
        self._config = config
        if not self._api_key and self._client is None:
            raise ValueError(
                "GEMINI_LIVE_API_KEY or GEMINI_API_KEY is not set. "
                "Run 'clea voice doctor' to verify configuration."
            )

        if self._client is None:
            self._client = Client(
                api_key=self._api_key,
                http_options={"api_version": "v1beta"},
            )

        self._closed = False
        await self._open_session(config)

    def _build_live_config(self, config: VoiceSessionConfig) -> types.LiveConnectConfig:
        # Build tool definitions
        function_declarations: list[types.FunctionDeclaration] = []
        for t in config.tools:
            function_declarations.append(
                types.FunctionDeclaration(
                    name=t.name,
                    description=t.description,
                    parameters_json_schema=t.parameters_schema,
                    behavior=types.Behavior.NON_BLOCKING,
                )
            )

        tools_list: list[types.Tool] = []
        if function_declarations:
            tools_list.append(types.Tool(function_declarations=function_declarations))
        if config.enable_web_search:
            tools_list.append(types.Tool(google_search=types.GoogleSearch()))

        # Session resumption
        resumption_cfg = None
        if config.session_resumption.enabled:
            handle = self._resumption_handle or config.session_resumption.handle
            # Note: In google.genai 3.2.0, 'transparent=True' is only supported in
            # Enterprise/Vertex AI mode; in Gemini Developer API (API key) mode,
            # passing transparent raises ValueError.
            resumption_cfg = types.SessionResumptionConfig(handle=handle)

        # Context compression
        compression_cfg = None
        if (
            config.context_compression.enabled
            and config.context_compression.trigger_tokens is not None
        ):
            sliding = None
            if config.context_compression.target_tokens is not None:
                sliding = types.SlidingWindow(
                    target_tokens=config.context_compression.target_tokens
                )
            compression_cfg = types.ContextWindowCompressionConfig(
                trigger_tokens=config.context_compression.trigger_tokens,
                sliding_window=sliding,
            )

        # Speech config
        speech_cfg = None
        if config.voice_name:
            speech_cfg = types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=config.voice_name)
                )
            )

        # Media resolution (guarded cleanly across google-genai versions)
        media_res = None
        media_enum = getattr(types, "MediaResolution", None)
        if media_enum is not None:
            res_map = {
                "low": getattr(media_enum, "MEDIA_RESOLUTION_LOW", None),
                "medium": getattr(media_enum, "MEDIA_RESOLUTION_MEDIUM", None),
                "high": getattr(media_enum, "MEDIA_RESOLUTION_HIGH", None),
            }
            media_res = res_map.get(config.media_resolution) or getattr(
                media_enum, "MEDIA_RESOLUTION_HIGH", None
            )

        # VAD configuration
        if config.vad_mode == "client":
            realtime_input_cfg = types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)
            )
        else:
            silence_ms = int(config.vad_silence_duration_ms)
            if "vad_silence_duration_ms" not in config.model_fields_set:
                env_silence = os.environ.get("CLEA_VAD_SILENCE_DURATION_MS", "").strip()
                if env_silence:
                    try:
                        parsed_ms = int(env_silence)
                        if 100 <= parsed_ms <= 10000:
                            silence_ms = parsed_ms
                        else:
                            logger.warning(
                                "Ignoring out-of-range CLEA_VAD_SILENCE_DURATION_MS=%r "
                                "(expected 100..10000 ms); using %d ms.",
                                env_silence,
                                silence_ms,
                            )
                    except ValueError:
                        logger.warning(
                            "Ignoring invalid CLEA_VAD_SILENCE_DURATION_MS=%r; using %d ms.",
                            env_silence,
                            silence_ms,
                        )
            aad_kwargs: dict[str, Any] = {
                "disabled": False,
                "silence_duration_ms": silence_ms,
            }
            end_sens_enum = getattr(types, "EndSensitivity", None)
            if end_sens_enum is not None:
                end_low = getattr(end_sens_enum, "END_SENSITIVITY_LOW", None)
                if end_low is not None:
                    aad_kwargs["end_of_speech_sensitivity"] = end_low
            act_handling_enum = getattr(types, "ActivityHandling", None)
            activity_handling = (
                getattr(act_handling_enum, "START_OF_ACTIVITY_INTERRUPTS", None)
                if act_handling_enum is not None
                else None
            )
            realtime_input_cfg = types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(**aad_kwargs),
                activity_handling=activity_handling,
            )

        # Input audio transcription lock (guarded safely across google-genai versions)
        input_transcription_cfg = types.AudioTranscriptionConfig()
        tx_lang = (
            config.transcription_language.strip()
            if isinstance(config.transcription_language, str)
            and config.transcription_language.strip()
            else None
        )
        if tx_lang:
            tx_fields = getattr(types.AudioTranscriptionConfig, "model_fields", {}) or {}
            try:
                if "language_codes" in tx_fields:
                    input_transcription_cfg = types.AudioTranscriptionConfig(
                        language_codes=[tx_lang]
                    )
                elif "language_code" in tx_fields:
                    input_transcription_cfg = types.AudioTranscriptionConfig(
                        language_code=tx_lang  # type: ignore[call-arg]
                    )
                elif "language_hints" in tx_fields:
                    input_transcription_cfg = types.AudioTranscriptionConfig(
                        language_hints=[tx_lang]
                    )
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "AudioTranscriptionConfig does not support transcription_language=%r (%s); "
                    "using default AudioTranscriptionConfig().",
                    tx_lang,
                    exc,
                )
                input_transcription_cfg = types.AudioTranscriptionConfig()

        return types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=types.Content(
                parts=[types.Part(text=config.system_instruction)],
                role="system",
            ),
            tools=tools_list if tools_list else None,
            session_resumption=resumption_cfg,
            context_window_compression=compression_cfg,
            speech_config=speech_cfg,
            media_resolution=media_res,
            realtime_input_config=realtime_input_cfg,
            input_audio_transcription=input_transcription_cfg,
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )

    async def _open_session(self, config: VoiceSessionConfig) -> None:
        assert self._client is not None
        live_cfg = self._build_live_config(config)

        session_ctx = self._client.aio.live.connect(
            model=self._model_name,
            config=live_cfg,
        )
        session = await session_ctx.__aenter__()
        if self._closed:
            with contextlib.suppress(Exception):
                await session_ctx.__aexit__(None, None, None)
            self._session_ctx = None
            self._session = None
            return
        self._session_ctx = session_ctx
        self._session = session
        self._last_assistant_transcript = None
        self._turn_completed = False
        self._receive_task = asyncio.create_task(
            self._receive_loop(),
            name="GeminiLiveBackendReceiveLoop",
        )
        effective_aad = (
            live_cfg.realtime_input_config.automatic_activity_detection
            if live_cfg.realtime_input_config is not None
            else None
        )
        effective_vad_silence = (
            "disabled"
            if (effective_aad is None or effective_aad.disabled)
            else f"{effective_aad.silence_duration_ms}ms"
        )
        logger.info(
            "GeminiLiveBackend connected (model=%s, web_search=%s, vad_mode=%s, "
            "vad_silence_duration=%s).",
            self._model_name,
            config.enable_web_search,
            config.vad_mode,
            effective_vad_silence,
        )

    async def _receive_loop(self) -> None:
        should_close_queue = False
        try:
            while not self._closed and self._session is not None:
                # google-genai's session.receive() stops at the end of each model turn
                # (turn_complete / interaction_status == IDLE), so loop across turns
                # while the session remains open.
                received_any = False
                async for msg in self._session.receive():
                    received_any = True
                    await self._handle_server_message(msg)
                    if self._closed:
                        break
                if not received_any:
                    if not self._closed:
                        logger.warning(
                            "Gemini Live receive() ended with no messages; closing stream."
                        )
                        await self._events_queue.put(
                            BackendErrorEvent(
                                message="Live session stream ended unexpectedly.",
                                recoverable=False,
                            )
                        )
                        should_close_queue = True
                    break
                # Yield to the event loop between turns so synchronous mocks cannot starve it.
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("Error in Gemini Live receive loop: %s", exc)
            if not self._closed and self._config is not None:
                if self._reconnect_count < self._config.reconnect_limit:
                    self._reconnect_count += 1
                    logger.info(
                        "Attempting reconnection %d/%d with resumption handle %s...",
                        self._reconnect_count,
                        self._config.reconnect_limit,
                        self._resumption_handle,
                    )
                    await self._events_queue.put(
                        BackendErrorEvent(
                            message=f"Connection dropped ({exc}); reconnecting...",
                            recoverable=True,
                        )
                    )
                    await asyncio.sleep(0.5)
                    try:
                        await self._reconnect()
                        return
                    except Exception as rec_err:
                        logger.error("Reconnection failed: %s", rec_err)
            if not self._closed:
                await self._events_queue.put(
                    BackendErrorEvent(message=f"Live session closed: {exc}", recoverable=False)
                )
                should_close_queue = True
        finally:
            if self._closed or should_close_queue:
                await self._events_queue.put(None)

    async def reconnect(self) -> None:
        """Reconnect to Live API using current configuration and session resumption handle."""
        await self._reconnect()

    async def _reconnect(self) -> None:
        current = asyncio.current_task()
        if (
            self._receive_task is not None
            and not self._receive_task.done()
            and self._receive_task is not current
        ):
            self._receive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._receive_task
            self._receive_task = None
        old_ctx = self._session_ctx
        self._session_ctx = None
        self._session = None
        if old_ctx is not None:
            with contextlib.suppress(Exception):
                await old_ctx.__aexit__(None, None, None)
        if self._closed or self._config is None:
            self._receive_task = None
            return
        await self._open_session(self._config)

    async def _handle_server_message(self, msg: types.LiveServerMessage) -> None:
        has_tool_call = bool(msg.tool_call is not None and msg.tool_call.function_calls)

        # 1. Content and audio
        if msg.server_content is not None:
            sc = msg.server_content
            if sc.interrupted:
                self._last_assistant_transcript = None
                self._turn_completed = False
                await self._events_queue.put(InterruptedEvent())

            emitted_part_texts: set[str] = set()
            if not has_tool_call and sc.model_turn is not None and sc.model_turn.parts:
                for part in sc.model_turn.parts:
                    if part.inline_data is not None and part.inline_data.data:
                        self._turn_completed = False
                        await self._events_queue.put(
                            AudioOutputEvent(pcm_bytes=part.inline_data.data, sample_rate_hz=24000)
                        )
                    if part.text:
                        self._turn_completed = False
                        norm_part_text = part.text.strip()
                        key = (norm_part_text, False)
                        if norm_part_text and key != self._last_assistant_transcript:
                            self._last_assistant_transcript = key
                            emitted_part_texts.add(norm_part_text)
                            await self._events_queue.put(
                                TranscriptOutputEvent(role="assistant", text=part.text, final=False)
                            )

            if sc.input_transcription is not None and sc.input_transcription.text:
                self._turn_completed = False
                self._last_assistant_transcript = None
                await self._events_queue.put(
                    TranscriptOutputEvent(
                        role="user",
                        text=sc.input_transcription.text,
                        final=bool(sc.input_transcription.finished),
                    )
                )

            if (
                not has_tool_call
                and sc.output_transcription is not None
                and sc.output_transcription.text
            ):
                self._turn_completed = False
                out_text = sc.output_transcription.text
                norm_out_text = out_text.strip()
                out_final = bool(sc.output_transcription.finished)
                out_key = (norm_out_text, out_final)
                # Suppress duplicate assistant transcript when identical to model_turn part.text
                # or an identical consecutive output_transcription frame in the same turn.
                if (
                    norm_out_text
                    and out_key != self._last_assistant_transcript
                    and not (not out_final and norm_out_text in emitted_part_texts)
                ):
                    self._last_assistant_transcript = out_key
                    await self._events_queue.put(
                        TranscriptOutputEvent(
                            role="assistant",
                            text=out_text,
                            final=out_final,
                        )
                    )

            if not has_tool_call and (
                sc.turn_complete or sc.interaction_status == types.InteractionStatus.IDLE
            ):
                if not self._turn_completed:
                    self._turn_completed = True
                    self._last_assistant_transcript = None
                    await self._events_queue.put(TurnCompleteEvent())

        # 2. Tool calls
        if has_tool_call and msg.tool_call is not None:
            self._turn_completed = False
            self._last_assistant_transcript = None
            for fc in msg.tool_call.function_calls:
                call_id = fc.id or ""
                name = fc.name or ""
                args = dict(fc.args) if fc.args else {}
                await self._events_queue.put(
                    ToolCallEvent(call_id=call_id, name=name, arguments=args)
                )

        # 3. Tool call cancellations
        if msg.tool_call_cancellation is not None and msg.tool_call_cancellation.ids:
            self._turn_completed = False
            self._last_assistant_transcript = None
            await self._events_queue.put(
                ToolCallCancellationEvent(call_ids=list(msg.tool_call_cancellation.ids))
            )

        # 4. Session resumption update
        if msg.session_resumption_update is not None:
            update = msg.session_resumption_update
            if update.new_handle:
                self._resumption_handle = update.new_handle
            await self._events_queue.put(
                SessionResumptionUpdateEvent(
                    handle=update.new_handle,
                    resumable=bool(update.resumable),
                )
            )

        # 5. GoAway notice
        if msg.go_away is not None:
            time_left = None
            if msg.go_away.time_left:
                time_str = str(msg.go_away.time_left).rstrip("s")
                with contextlib.suppress(ValueError):
                    time_left = float(time_str)
            await self._events_queue.put(GoAwayEvent(time_left_seconds=time_left))

        # 6. Usage metadata
        if msg.usage_metadata is not None:
            um = msg.usage_metadata
            await self._events_queue.put(
                UsageUpdateEvent(
                    input_tokens=um.prompt_token_count or 0,
                    output_tokens=um.response_token_count or 0,
                    total_tokens=um.total_token_count or 0,
                    cached_tokens=um.cached_content_token_count,
                    thought_tokens=um.thoughts_token_count,
                )
            )

    async def send_audio(self, pcm_bytes: bytes) -> None:
        """Stream a 16 kHz 16-bit mono PCM microphone chunk."""
        session = self._session
        if session is None or self._closed:
            return
        blob = types.Blob(data=pcm_bytes, mime_type="audio/pcm;rate=16000")
        try:
            await session.send_realtime_input(audio=blob)
        except Exception as exc:
            if self._closed or self._session is not session:
                logger.debug("Ignoring send_audio failure during close/reconnect: %s", exc)
                return
            raise

    async def end_audio_stream(self) -> None:
        """Signal an end/pause of audio streaming (e.g. for half-duplex)."""
        if self._session is None or self._closed:
            return
        await self._session.send_realtime_input(audio_stream_end=True)

    async def send_video_frame(
        self,
        image_bytes: bytes,
        *,
        mime_type: str = "image/jpeg",
    ) -> None:
        """Stream an encoded screen capture frame."""
        session = self._session
        if session is None or self._closed:
            return
        blob = types.Blob(data=image_bytes, mime_type=mime_type)
        try:
            await session.send_realtime_input(video=blob)
        except Exception as exc:
            if self._closed or self._session is not session:
                logger.debug("Ignoring send_video_frame failure during close/reconnect: %s", exc)
                return
            raise

    async def send_text(self, text: str, *, end_of_turn: bool = True) -> None:
        """Send a text prompt or system notification to the live session."""
        session = self._session
        if session is None or self._closed:
            return
        await session.send_realtime_input(text=text)

    async def send_tool_responses(self, responses: list[VoiceToolResponse]) -> None:
        """Send tool execution responses back to the Live model."""
        session = self._session
        if session is None or self._closed or not responses:
            return

        formatted: list[types.FunctionResponse] = []
        for r in responses:
            sched = types.FunctionResponseScheduling.WHEN_IDLE
            if r.scheduling == "interrupt":
                sched = types.FunctionResponseScheduling.INTERRUPT
            elif r.scheduling == "silent":
                sched = types.FunctionResponseScheduling.SILENT

            resp_payload = (
                r.response if not r.is_error else {"error": r.response.get("error", "Error")}
            )
            parts: list[types.FunctionResponsePart] | None = None
            if r.media_parts and not r.is_error:
                parts = [
                    types.FunctionResponsePart(
                        inline_data=types.FunctionResponseBlob(
                            mime_type=part.mime_type,
                            data=part.data,
                        )
                    )
                    for part in r.media_parts
                ]
            formatted.append(
                types.FunctionResponse(
                    id=r.call_id,
                    name=r.name,
                    response=resp_payload,
                    scheduling=sched,
                    parts=parts,
                )
            )
        try:
            await session.send_tool_response(function_responses=formatted)
        except Exception as exc:
            if self._closed or self._session is not session:
                logger.debug(
                    "Ignoring send_tool_responses failure during close/reconnect: %s",
                    exc,
                )
                return
            raise

    def drain_contiguous_tool_calls(self, first_event: ToolCallEvent) -> list[ToolCallEvent]:
        """Drain any immediately queued contiguous ToolCallEvents from this backend."""
        batch: list[ToolCallEvent] = [first_event]
        internal_deque = getattr(self._events_queue, "_queue", None)
        while internal_deque and isinstance(internal_deque[0], ToolCallEvent):
            try:
                nxt = self._events_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(nxt, ToolCallEvent):
                batch.append(nxt)
        return batch

    async def events(self) -> AsyncIterator[VoiceBackendEvent]:
        """Yield normalized VoiceBackendEvent stream for active session."""
        while True:
            evt = await self._events_queue.get()
            if evt is None:
                break
            yield evt

    async def close(self) -> None:
        """Close the Live session and release resources."""
        if self._closed:
            return
        self._closed = True

        if self._receive_task is not None:
            self._receive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._receive_task
            self._receive_task = None

        if self._session_ctx is not None:
            with contextlib.suppress(Exception):
                await self._session_ctx.__aexit__(None, None, None)
            self._session_ctx = None
            self._session = None

        await self._events_queue.put(None)
        logger.info("GeminiLiveBackend closed.")
