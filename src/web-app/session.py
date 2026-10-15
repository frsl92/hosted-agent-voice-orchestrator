"""One browser session: bridges a WebSocket to a Voice Live connection bound to a hosted agent."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import time
from typing import Any, Protocol

import aiohttp
from azure.ai.voicelive.aio import VoiceLiveConnection, connect
from azure.ai.voicelive.models import (
    AudioEchoCancellation,
    AudioInputTranscriptionOptions,
    AudioNoiseReduction,
    AzureSemanticVadMultilingual,
    AzureStandardVoice,
    InputAudioFormat,
    InputTextContentPart,
    Modality,
    OutputAudioFormat,
    RequestSession,
    ServerEventType,
    ServerVad,
    UserMessageItem,
)
from azure.core.credentials_async import AsyncTokenCredential

import voices
from logtail import LogTailer, agent_session_id
from policy import DEFAULT_POLICY, call_start_message, normalize

logger = logging.getLogger("session")

# First language is the primary one; English stays recognised because callers may switch.
STT_LANGUAGE = os.environ.get("STT_LANGUAGE", "pt-PT,en-US")
VAD_TYPE = os.environ.get("VAD_TYPE", "azure_semantic_vad_multilingual")
VAD_LANGUAGES = [x.strip() for x in os.environ.get("VAD_LANGUAGES", "pt,en").split(",") if x.strip()]
# Silent first user turn that makes the agent greet the caller; the agents recognise it and never show it.
CALL_START = "[call connected]"


class Sink(Protocol):
    async def send_json(self, message: dict[str, Any]) -> None: ...
    async def send_bytes(self, data: bytes) -> None: ...


def now_ms() -> int:
    return round(time.time() * 1000)


def response_text(response: Any) -> str:
    """Spoken text of a ``response.done`` payload."""
    parts: list[str] = []
    for item in (response or {}).get("output") or []:
        for content in item.get("content") or []:
            text = content.get("transcript") or content.get("text")
            if text:
                parts.append(text)
    return " ".join(parts).replace("meta_end", "").strip()


def usage_of(response: Any) -> dict[str, Any]:
    usage = (response or {}).get("usage") or {}
    return {k: usage[k] for k in ("input_tokens", "output_tokens", "total_tokens") if k in usage}


class VoiceBridge:
    def __init__(
        self,
        sink: Sink,
        credential: AsyncTokenCredential,
        http: aiohttp.ClientSession,
        *,
        endpoint: str,
        project_name: str,
        project_endpoint: str,
        agent_name: str,
        mode: str = "text",
        audio_out: bool | None = None,
        voice: str | None = None,
        voice_style: str | None = None,
        policy: dict[str, str] | None = None,
    ) -> None:
        self._sink = sink
        self._cred = credential
        self._http = http
        self._endpoint = endpoint
        self._project_name = project_name
        self._project_endpoint = project_endpoint
        self.agent_name = agent_name
        self.mode = "voice" if mode == "voice" else "text"
        self.voice, self.voice_style = voices.resolve(voice, voice_style)
        self.policy = normalize(policy) if policy is not None else None
        # Voice sessions always speak; text sessions never do.
        self.audio_out = (self.mode == "voice") if audio_out is None else audio_out
        self._conn: VoiceLiveConnection | None = None
        self._lock = asyncio.Lock()
        self._tailer_task: asyncio.Task[None] | None = None
        self._responding = False
        self._greeted = False
        self._t_input: int | None = None
        self._first_token_sent = False
        self._first_audio_sent = False
        self.voice_session_id: str | None = None

    # ------------------------------------------------------------------ output helpers

    async def _json(self, message: dict[str, Any]) -> None:
        async with self._lock:
            await self._sink.send_json(message)

    async def trace(self, source: str, kind: str, **data: Any) -> None:
        await self._json({"type": "trace", "source": source, "kind": kind, "t": now_ms(), "data": data})

    async def _emit_event(self, event: dict[str, Any]) -> None:
        await self._json({"type": "trace", **event})

    # ------------------------------------------------------------------ lifecycle

    async def run(self, incoming: "asyncio.Queue[dict[str, Any] | bytes | None]") -> None:
        """Connect to Voice Live and pump events until the browser disconnects."""
        agent_config = {"agent_name": self.agent_name, "project_name": self._project_name}
        async with connect(endpoint=self._endpoint, credential=self._cred, agent_config=agent_config) as conn:
            self._conn = conn
            await conn.session.update(session=self._session_config())
            if self.mode == "voice":
                await self.trace("system", "voice.config", voice=self.voice, style=self.voice_style,
                                 locale=voices.LOCALE, policy=self.policy or dict(DEFAULT_POLICY))
            events = asyncio.create_task(self._pump_events(conn))
            commands = asyncio.create_task(self._pump_commands(conn, incoming))
            try:
                done, _ = await asyncio.wait({events, commands}, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in (events, commands, self._tailer_task):
                    if task:
                        task.cancel()
                for task in (events, commands, self._tailer_task):
                    if task:
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await task

    def _voice_config(self) -> AzureStandardVoice:
        kwargs: dict[str, Any] = {"name": self.voice, "locale": voices.LOCALE}
        if self.voice_style:
            kwargs["style"] = self.voice_style
        return AzureStandardVoice(**kwargs)

    @staticmethod
    def _turn_detection() -> Any:
        """Barge-in is on (``interrupt_response``); blips shorter than ``speech_duration_ms`` are ignored so
        a click or breath neither interrupts the agent nor produces an empty turn."""
        if VAD_TYPE == "server_vad":
            return ServerVad(threshold=0.5, prefix_padding_ms=300, silence_duration_ms=500, interrupt_response=True)
        return AzureSemanticVadMultilingual(
            threshold=0.5, prefix_padding_ms=300, silence_duration_ms=500, speech_duration_ms=250,
            languages=VAD_LANGUAGES, interrupt_response=True, create_response=True,
        )

    def _session_config(self) -> RequestSession:
        if self.mode == "text":
            return RequestSession(modalities=[Modality.TEXT])
        return RequestSession(
            modalities=[Modality.TEXT, Modality.AUDIO],
            voice=self._voice_config(),
            input_audio_format=InputAudioFormat.PCM16,
            output_audio_format=OutputAudioFormat.PCM16,
            input_audio_transcription=AudioInputTranscriptionOptions(model="azure-speech", language=STT_LANGUAGE),
            turn_detection=self._turn_detection(),
            input_audio_echo_cancellation=AudioEchoCancellation(),
            input_audio_noise_reduction=AudioNoiseReduction(type="azure_deep_noise_suppression"),
        )

    # ------------------------------------------------------------------ browser -> Voice Live

    async def _pump_commands(self, conn: VoiceLiveConnection, incoming: "asyncio.Queue[Any]") -> None:
        while True:
            msg = await incoming.get()
            if msg is None:
                return
            if isinstance(msg, bytes):
                if self.mode != "voice":
                    continue
                await conn.input_audio_buffer.append(audio=base64.b64encode(msg).decode())
                continue
            kind = msg.get("type")
            if kind == "text":
                if self.mode != "text":
                    continue
                text = str(msg.get("text", "")).strip()
                if not text:
                    continue
                if self._responding:
                    await self.trace("system", "busy", message="The agent is still answering; message not sent.")
                    await self._json({"type": "busy"})
                    continue
                self._t_input = now_ms()
                self._first_token_sent = self._first_audio_sent = False
                await self.trace("voice", "user.text", text=text)
                await self._json({"type": "user_text", "text": text})
                await conn.conversation.item.create(item=UserMessageItem(content=[InputTextContentPart(text=text)]))
                await conn.response.create()
            elif kind == "diag":
                data = {k: v for k, v in msg.items() if k not in ("type", "event")}
                if msg.get("event") != "mic.stats" or not msg.get("peak"):
                    logger.info("mic %s %s", msg.get("event"), data)
                await self.trace("mic", str(msg.get("event", "diag")), **data)
            elif kind == "cancel":
                with contextlib.suppress(Exception):
                    await conn.response.cancel()
                await self._json({"type": "interrupt"})

    # ------------------------------------------------------------------ Voice Live -> browser

    async def _pump_events(self, conn: VoiceLiveConnection) -> None:
        async for event in conn:
            try:
                await self._on_event(event)
            except (asyncio.CancelledError, ConnectionError):
                raise
            except Exception:
                logger.exception("failed to process %s", getattr(event, "type", "?"))

    async def _greet(self) -> None:
        """Once the session is configured, have the agent greet the caller like a call-center operator."""
        if self._greeted or self._conn is None or self._responding or self._t_input is not None:
            return
        self._greeted = True
        self._responding = True
        self._t_input = now_ms()
        self._first_token_sent = self._first_audio_sent = False
        await self.trace("voice", "call.connected")
        await self._json({"type": "greeting"})
        await self._conn.conversation.item.create(
            item=UserMessageItem(content=[InputTextContentPart(text=call_start_message(self.policy))])
        )
        await self._conn.response.create()

    async def _on_event(self, event: Any) -> None:
        t = event.type
        if t == ServerEventType.SESSION_CREATED:
            session = event.get("session") or {}
            self.voice_session_id = session.get("id")
            await self.trace(
                "voice", "session.created", session_id=self.voice_session_id, mode=self.mode,
                agent_session_id=agent_session_id(self.voice_session_id or ""), agent=self.agent_name,
            )
            await self._json(
                {"type": "ready", "voice_session_id": self.voice_session_id, "agent": self.agent_name, "mode": self.mode}
            )
            if self.voice_session_id:
                tailer = LogTailer(
                    self._cred, self._http, self._project_endpoint, self.agent_name, self.voice_session_id,
                    self._emit_event,
                )
                self._tailer_task = asyncio.create_task(tailer.run(), name="logtail")
        elif t == ServerEventType.SESSION_UPDATED:
            await self.trace("voice", "session.updated")
            await self._greet()
        elif t == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
            await self._json({"type": "interrupt"})
            await self.trace("voice", "speech.started")
        elif t == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
            self._t_input = now_ms()
            self._first_token_sent = self._first_audio_sent = False
            await self.trace("voice", "speech.stopped")
        elif t == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
            text = event.get("transcript", "")
            await self._json({"type": "user_transcript", "text": text})
            await self.trace("voice", "user.transcript", text=text)
        elif t == ServerEventType.RESPONSE_CREATED:
            self._responding = True
            await self._json({"type": "agent_start"})
            await self.trace("voice", "response.created")
        elif t in (ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA, ServerEventType.RESPONSE_TEXT_DELTA):
            if not self._first_token_sent and self._t_input:
                self._first_token_sent = True
                await self.trace("voice", "first.token", ms=now_ms() - self._t_input)
            delta = event.delta.replace("meta_end", "")
            if delta:
                await self._json({"type": "agent_delta", "text": delta})
        elif t == ServerEventType.RESPONSE_AUDIO_DELTA:
            if not self._first_audio_sent and self._t_input:
                self._first_audio_sent = True
                await self.trace("voice", "first.audio", ms=now_ms() - self._t_input)
            if self.audio_out:
                async with self._lock:
                    await self._sink.send_bytes(event.delta)
        elif t == ServerEventType.RESPONSE_DONE:
            self._responding = False
            response = event.get("response") or {}
            status = response.get("status")
            text = response_text(response)
            await self._json({"type": "agent_done", "text": text, "status": status})
            await self.trace(
                "voice", "response.done", status=status, text=text, usage=usage_of(response),
                total_ms=(now_ms() - self._t_input) if self._t_input else None,
            )
        elif t == ServerEventType.ERROR:
            err = event.get("error") or {}
            self._responding = False
            await self.trace("voice", "error", code=err.get("code"), message=err.get("message"))
            await self._json({"type": "error", "message": err.get("message", "Voice Live error")})
        elif t == ServerEventType.WARNING:
            await self.trace("voice", "warning", message=(event.get("warning") or {}).get("message"))
