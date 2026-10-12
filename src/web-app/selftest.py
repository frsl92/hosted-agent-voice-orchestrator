"""Smoke test: open a text session against the running server and print what comes back.

Usage (inside the container or locally):
    python selftest.py [agent-id] [port]
"""
import asyncio
import json
import sys

import websockets

AGENT = sys.argv[1] if len(sys.argv) > 1 else "handoff-agent"
PORT = sys.argv[2] if len(sys.argv) > 2 else "8000"
TURNS = {
    "handoff-agent": ["Hi, where is order 67890?"],
    "background-agent": ["Hi, can you check where order 12345 is?", "Yes please, go ahead."],
}[AGENT]


async def until_done(ws):
    while True:
        raw = await asyncio.wait_for(ws.recv(), 90)
        if isinstance(raw, bytes):
            continue
        msg = json.loads(raw)
        if msg["type"] == "trace":
            if msg["source"] != "log":
                print("  trace", msg["source"], msg["kind"], json.dumps(msg["data"])[:160])
        elif msg["type"] != "agent_delta":
            print(msg["type"], json.dumps(msg)[:200])
        if msg["type"] in ("agent_done", "error"):
            return msg


async def main():
    url = f"ws://localhost:{PORT}/ws/session?agent={AGENT}"
    async with websockets.connect(url, max_size=None) as ws:
        while json.loads(await asyncio.wait_for(ws.recv(), 30))["type"] != "ready":
            pass
        print("session ready")
        for turn in TURNS:
            await ws.send(json.dumps({"type": "text", "text": turn}))
            await until_done(ws)
            await asyncio.sleep(1.5)
        print("selftest OK")


asyncio.run(main())
