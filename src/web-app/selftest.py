"""Smoke test: open text sessions against the running server and print what comes back.

Usage (inside the container or locally):
    python selftest.py [agent-id] [port] [sample-id|all] [--full]

The password is read from APP_PASSWORD (already set inside the container).
"""
import asyncio
import json
import os
import sys
import urllib.request

import websockets

from samples import SAMPLES

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
AGENT = ARGS[0] if len(ARGS) > 0 else "handoff-agent"
PORT = ARGS[1] if len(ARGS) > 1 else "8000"
WHICH = ARGS[2] if len(ARGS) > 2 else "order-tracking"
FULL = "--full" in sys.argv
SHOW = {"decision.call", "decision.analyze", "decision.guard", "decision.confirmation", "handoff", "phase",
        "task.queued", "task.start", "task.done", "task.delivered", "tool.call", "turn.end"}


def login() -> str:
    req = urllib.request.Request(
        f"http://localhost:{PORT}/login", method="POST", headers={"Content-Type": "application/json"},
        data=json.dumps({"password": os.environ["APP_PASSWORD"]}).encode(),
    )
    with urllib.request.urlopen(req) as resp:
        return resp.headers["Set-Cookie"].split(";")[0]


async def until_done(ws, seen):
    while True:
        raw = await asyncio.wait_for(ws.recv(), 120)
        if isinstance(raw, bytes):
            continue
        msg = json.loads(raw)
        if msg["type"] == "trace":
            if msg["source"] == "agent":
                seen.append(msg["kind"])
                if msg["kind"] in SHOW:
                    data = json.dumps(msg["data"])
                    print("    ", msg["kind"], data if FULL and msg["kind"] == "decision.call" else data[:150])
        elif msg["type"] == "agent_done":
            print("   agent:", msg["text"][:220])
            return
        elif msg["type"] == "error":
            print("   ERROR", msg["message"])
            raise SystemExit(1)


async def run(sample, cookie):
    print(f"== {AGENT} :: {sample['id']}")
    url = f"ws://localhost:{PORT}/ws/session?agent={AGENT}&mode=text"
    seen: list[str] = []
    async with websockets.connect(url, max_size=None, additional_headers={"Cookie": cookie}) as ws:
        while json.loads(await asyncio.wait_for(ws.recv(), 30))["type"] != "ready":
            pass
        for turn in sample["turns"]:
            print("  user:", turn)
            await ws.send(json.dumps({"type": "text", "text": turn}))
            await until_done(ws, seen)
            await asyncio.sleep(2)
    return seen


async def main():
    cookie = login()
    samples = SAMPLES if WHICH == "all" else [s for s in SAMPLES if s["id"] == WHICH]
    for sample in samples:
        seen = await run(sample, cookie)
        assert "decision.call" in seen, "no decision.call trace received"
    print("selftest OK")


asyncio.run(main())
