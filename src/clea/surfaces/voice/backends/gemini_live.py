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
        model_name: str = DEFAULT_LIVE_MODEL,
        client: Client | None = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("GEMINI_API_KEY", "")
        self._model_name = model_name
        self._client = client
        self._session: Any = None
        self._session_ctx: Any = None
        self._config: VoiceSessionConfig | None = None
        self._resumption_handle: str | None = None
        self._events_queue: asyncio.Queue[VoiceBackendEvent | None] = asyncio.Queue()
        self._receive_task: asyncio.Task[None] | None = None
        self._closed = True
        self._reconnect_count = 0

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
                "GEMINI_API_KEY is not set. Run 'clea voice doctor' to verify configuration."
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
        if config.context_compression.enabled:
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

        # Media resolution
        res_map = {
            "low": types.MediaResolution.MEDIA_RESOLUTION_LOW,
            "medium": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
            "high": types.MediaResolution.MEDIA_RESOLUTION_HIGH,
        }
        media_res = res_map.get(
            config.media_resolution, types.MediaResolution.MEDIA_RESOLUTION_MEDIUM
        )

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
        )

    async def _open_session(self, config: VoiceSessionConfig) -> None:
        assert self._client is not None
        live_cfg = self._build_live_config(config)

        self._session_ctx = self._client.aio.live.connect(
            model=self._model_name,
            config=live_cfg,
        )
        self._session = await self._session_ctx.__aenter__()
        self._receive_task = asyncio.create_task(
            self._receive_loop(),
            name="GeminiLiveBackendReceiveLoop",
        )
        logger.info(
            "GeminiLiveBackend connected (model=%s, web_search=%s).",
            self._model_name,
            config.enable_web_search,
        )

    async def _receive_loop(self) -> None:
        try:
            assert self._session is not None
            async for msg in self._session.receive():
                await self._handle_server_message(msg)
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
            await self._events_queue.put(
                BackendErrorEvent(message=f"Live session closed: {exc}", recoverable=False)
            )
        finally:
            if self._closed:
                await self._events_queue.put(None)

    async def _reconnect(self) -> None:
        if self._session_ctx is not None:
            with contextlib.suppress(Exception):
                await self._session_ctx.__aexit__(None, None, None)
        assert self._config is not None
        await self._open_session(self._config)

    async def _handle_server_message(self, msg: types.LiveServerMessage) -> None:
        # 1. Content and audio
        if msg.server_content is not None:
            sc = msg.server_content
            if sc.interrupted:
                await self._events_queue.put(InterruptedEvent())

            if sc.model_turn is not None and sc.model_turn.parts:
                for part in sc.model_turn.parts:
                    if part.inline_data is not None and part.inline_data.data:
                        await self._events_queue.put(
                            AudioOutputEvent(pcm_bytes=part.inline_data.data, sample_rate_hz=24000)
                        )
                    if part.text:
                        await self._events_queue.put(
                            TranscriptOutputEvent(role="assistant", text=part.text, final=False)
                        )

            if sc.input_transcription is not None and sc.input_transcription.text:
                await self._events_queue.put(
                    TranscriptOutputEvent(
                        role="user",
                        text=sc.input_transcription.text,
                        final=bool(sc.input_transcription.finished),
                    )
                )

            if sc.output_transcription is not None and sc.output_transcription.text:
                await self._events_queue.put(
                    TranscriptOutputEvent(
                        role="assistant",
                        text=sc.output_transcription.text,
                        final=bool(sc.output_transcription.finished),
                    )
                )

            if sc.turn_complete:
                await self._events_queue.put(TurnCompleteEvent())

        # 2. Tool calls
        if msg.tool_call is not None and msg.tool_call.function_calls:
            for fc in msg.tool_call.function_calls:
                call_id = fc.id or ""
                name = fc.name or ""
                args = dict(fc.args) if fc.args else {}
                await self._events_queue.put(
                    ToolCallEvent(call_id=call_id, name=name, arguments=args)
                )

        # 3. Tool call cancellations
        if msg.tool_call_cancellation is not None and msg.tool_call_cancellation.ids:
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
        if self._session is None or self._closed:
            return
        blob = types.Blob(data=pcm_bytes, mime_type="audio/pcm;rate=16000")
        await self._session.send_realtime_input(audio=blob)

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
        if self._session is None or self._closed:
            return
        blob = types.Blob(data=image_bytes, mime_type=mime_type)
        await self._session.send_realtime_input(video=blob)

    async def send_text(self, text: str, *, end_of_turn: bool = True) -> None:
        """Send a text prompt or system notification to the live session."""
        if self._session is None or self._closed:
            return
        turns = [types.Content(parts=[types.Part(text=text)], role="user")]
        await self._session.send_client_content(turns=turns, end_of_turn=end_of_turn)

    async def send_tool_responses(self, responses: list[VoiceToolResponse]) -> None:
        """Send tool execution responses back to the Live model."""
        if self._session is None or self._closed or not responses:
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
            formatted.append(
                types.FunctionResponse(
                    id=r.call_id,
                    name=r.name,
                    response=resp_payload,
                    scheduling=sched,
                )
            )
        await self._session.send_tool_response(function_responses=formatted)

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
