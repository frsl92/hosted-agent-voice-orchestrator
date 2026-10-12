"""Voice loopback test: feed the agent's own greeting audio back as microphone input.

Usage: python selftest_voice.py [agent-id] [port]

Checks the server -> Voice Live audio path without a browser: if the transcript of the looped audio is
sensible, problems are on the browser / microphone side. Password comes from APP_PASSWORD.
"""
import asyncio
import json
import os
import sys
import urllib.request

import websockets

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
AGENT = ARGS[0] if ARGS else "handoff-agent"
PORT = ARGS[1] if len(ARGS) > 1 else "8000"
FRAME = 960  # 20 ms of 24 kHz PCM16


def login() -> str:
    req = urllib.request.Request(
        f"http://localhost:{PORT}/login", method="POST", headers={"Content-Type": "application/json"},
        data=json.dumps({"password": os.environ["APP_PASSWORD"]}).encode(),
    )
    with urllib.request.urlopen(req) as resp:
        return resp.headers["Set-Cookie"].split(";")[0]


async def main() -> None:
    url = f"ws://localhost:{PORT}/ws/session?agent={AGENT}&mode=voice"
    audio = bytearray()
    loop = asyncio.get_running_loop()
    async with websockets.connect(url, max_size=None, additional_headers={"Cookie": login()}) as ws:
        done = False
        while not done:
            raw = await asyncio.wait_for(ws.recv(), 90)
            if isinstance(raw, bytes):
                audio += raw
                continue
            msg = json.loads(raw)
            if msg["type"] == "agent_done":
                done = True
            elif msg["type"] == "error":
                raise SystemExit(f"ERROR {msg['message']}")
        print(f"greeting captured: {len(audio) / 48000:.1f} s of audio")
        await asyncio.sleep(1)

        async def sender() -> None:
            silence = b"\x00" * (FRAME * 2)
            for _ in range(50):
                await ws.send(silence)
                await asyncio.sleep(0.02)
            for i in range(0, len(audio), FRAME * 2):
                await ws.send(bytes(audio[i : i + FRAME * 2]).ljust(FRAME * 2, b"\x00"))
                await asyncio.sleep(0.02)
            while True:
                await ws.send(silence)
                await asyncio.sleep(0.02)

        send_task = asyncio.create_task(sender())
        try:
            end = loop.time() + 30
            while loop.time() < end:
                raw = await asyncio.wait_for(ws.recv(), 30)
                if isinstance(raw, bytes):
                    continue
                msg = json.loads(raw)
                if msg["type"] == "user_transcript":
                    print("transcript:", repr(msg["text"]))
                elif msg["type"] == "trace" and msg["source"] == "voice" and msg["kind"].startswith("speech."):
                    print("  ", msg["kind"])
                elif msg["type"] == "error":
                    print("ERROR", msg["message"])
        except asyncio.TimeoutError:
            pass
        finally:
            send_task.cancel()


asyncio.run(main())
