#!/usr/bin/env python
"""Console Voice Live client for the hosted call-center agents.

Adapted from the Microsoft foundry-samples Voice Live client (MIT licensed).
The hosted agent (Responses protocol) is bound at connection time with an
``agent_config`` (``agent_name`` + ``project_name``); Voice Live does the
speech-to-text / text-to-speech and sends each utterance to the agent as text.

Usage:
    uv run --extra audio python voicelive_client.py --agent-name handoff-agent       # microphone
    uv run python voicelive_client.py --agent-name background-agent --text           # typed text, no audio

Defaults come from env vars VOICELIVE_ENDPOINT / FOUNDRY_PROJECT_NAME or the built-in project values.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
import queue
import sys
from typing import Optional

from azure.ai.voicelive.aio import VoiceLiveConnection, connect
from azure.ai.voicelive.models import (
    AudioEchoCancellation,
    AudioNoiseReduction,
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
from azure.identity.aio import DefaultAzureCredential

DEFAULT_ENDPOINT = "https://flopes-swc-foundry.services.ai.azure.com"
DEFAULT_PROJECT = "swc-project"
VOICE = "en-US-Ava:DragonHDLatestNeural"

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("voicelive_client")


class AudioProcessor:
    """PCM16 24 kHz mono capture/playback with sequence numbers for barge-in."""

    def __init__(self, connection: VoiceLiveConnection) -> None:
        import pyaudio

        self._pyaudio = pyaudio
        self.connection = connection
        self.audio = pyaudio.PyAudio()
        self.rate = 24000
        self.chunk = 1200
        self.input_stream = None
        self.output_stream = None
        self.q: queue.Queue = queue.Queue()
        self.base = 0
        self.seq = 0
        self.loop: asyncio.AbstractEventLoop | None = None

    def start_capture(self) -> None:
        if self.input_stream:
            return
        self.loop = asyncio.get_running_loop()
        pa = self._pyaudio

        def cb(data, *_):
            b64 = base64.b64encode(data).decode()
            asyncio.run_coroutine_threadsafe(self.connection.input_audio_buffer.append(audio=b64), self.loop)
            return (None, pa.paContinue)

        self.input_stream = self.audio.open(
            format=pa.paInt16, channels=1, rate=self.rate, input=True, frames_per_buffer=self.chunk, stream_callback=cb
        )

    def start_playback(self) -> None:
        if self.output_stream:
            return
        pa = self._pyaudio
        remaining = b""

        def cb(_in, frame_count, *_):
            nonlocal remaining
            need = frame_count * pa.get_sample_size(pa.paInt16)
            out, remaining = remaining[:need], remaining[need:]
            while len(out) < need:
                try:
                    pkt = self.q.get_nowait()
                except queue.Empty:
                    out += bytes(need - len(out))
                    break
                if pkt is None:
                    break
                seq, data = pkt
                if seq < self.base:
                    remaining = b""
                    continue
                take = need - len(out)
                out += data[:take]
                remaining = data[take:]
            return (out, pa.paContinue)

        self.output_stream = self.audio.open(
            format=pa.paInt16, channels=1, rate=self.rate, output=True, frames_per_buffer=self.chunk, stream_callback=cb
        )

    def queue_audio(self, data: bytes) -> None:
        self.q.put((self.seq, data))
        self.seq += 1

    def skip_pending(self) -> None:
        self.base = self.seq
        self.seq += 1

    def shutdown(self) -> None:
        for s in (self.input_stream, self.output_stream):
            if s:
                s.stop_stream()
                s.close()
        self.audio.terminate()


async def _handle_events(conn: VoiceLiveConnection, ap: Optional[AudioProcessor], text_mode: bool, turn_done: asyncio.Event) -> None:
    async for event in conn:
        t = event.type
        if t == ServerEventType.SESSION_UPDATED:
            if ap:
                ap.start_capture()
        elif t == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
            print(f"\nYou: {event.get('transcript', '')}")
        elif t in (ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA, ServerEventType.RESPONSE_TEXT_DELTA):
            print(event.delta, end="", flush=True)
        elif t == ServerEventType.RESPONSE_AUDIO_DELTA and ap:
            ap.queue_audio(event.delta)
        elif t == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED and ap:
            ap.skip_pending()
            print("\n[listening]")
        elif t == ServerEventType.RESPONSE_DONE:
            print()
            turn_done.set()
        elif t == ServerEventType.ERROR:
            print(f"\n[error] {event.error.message}")
            turn_done.set()
        elif t == ServerEventType.WARNING:
            print(f"\n[warning] {event.warning.message}")


async def run(endpoint: str, agent_name: str, project: str, text_mode: bool) -> None:
    credential = DefaultAzureCredential()
    agent_config = {"agent_name": agent_name, "project_name": project}
    async with connect(endpoint=endpoint, credential=credential, agent_config=agent_config) as conn:
        ap = None if text_mode else AudioProcessor(conn)
        if text_mode:
            session = RequestSession(
                modalities=[Modality.TEXT, Modality.AUDIO],
                voice=AzureStandardVoice(name=VOICE),
                output_audio_format=OutputAudioFormat.PCM16,
            )
        else:
            session = RequestSession(
                modalities=[Modality.TEXT, Modality.AUDIO],
                voice=AzureStandardVoice(name=VOICE),
                input_audio_format=InputAudioFormat.PCM16,
                output_audio_format=OutputAudioFormat.PCM16,
                turn_detection=ServerVad(threshold=0.5, prefix_padding_ms=300, silence_duration_ms=500),
                input_audio_echo_cancellation=AudioEchoCancellation(),
                input_audio_noise_reduction=AudioNoiseReduction(type="azure_deep_noise_suppression"),
            )
        await conn.session.update(session=session)
        turn_done = asyncio.Event()
        events = asyncio.create_task(_handle_events(conn, ap, text_mode, turn_done))
        try:
            if text_mode:
                print(f"Connected to '{agent_name}'. Type a message (empty line to quit).")
                loop = asyncio.get_running_loop()
                while True:
                    try:
                        line = (await loop.run_in_executor(None, lambda: input("\nYou: "))).strip()
                    except EOFError:
                        break
                    if not line:
                        break
                    turn_done.clear()
                    await conn.conversation.item.create(item=UserMessageItem(content=[InputTextContentPart(text=line)]))
                    await conn.response.create()
                    print("Agent: ", end="", flush=True)
                    await turn_done.wait()
            else:
                ap.start_playback()
                print(f"Connected to '{agent_name}'. Speak into the microphone; Ctrl+C to exit.")
                await events
        finally:
            events.cancel()
            if ap:
                ap.shutdown()
    await credential.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Voice Live console client for a Foundry hosted agent")
    p.add_argument("--endpoint", default=os.environ.get("VOICELIVE_ENDPOINT", DEFAULT_ENDPOINT))
    p.add_argument("--project-name", default=os.environ.get("FOUNDRY_PROJECT_NAME", DEFAULT_PROJECT))
    p.add_argument("--agent-name", required=True, help="handoff-agent or background-agent")
    p.add_argument("--text", action="store_true", help="type instead of speak (no audio dependencies)")
    a = p.parse_args()
    if not a.text:
        try:
            import pyaudio  # noqa: F401
        except ImportError:
            sys.exit("Microphone mode needs pyaudio: uv run --extra audio python voicelive_client.py ... (or use --text)")
    try:
        asyncio.run(run(a.endpoint, a.agent_name, a.project_name, a.text))
    except KeyboardInterrupt:
        print("\nGoodbye!")


if __name__ == "__main__":
    main()
