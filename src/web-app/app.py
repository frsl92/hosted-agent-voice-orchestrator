"""FastAPI backend: serves the UI and bridges browser WebSockets to Voice Live + hosted-agent logs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
from azure.identity.aio import DefaultAzureCredential
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from samples import AGENTS, SAMPLES
from session import VoiceBridge

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("app")

STATIC = Path(__file__).parent / "static"
VOICELIVE_ENDPOINT = os.environ.get("VOICELIVE_ENDPOINT", "https://flopes-swc-foundry.services.ai.azure.com")
PROJECT_NAME = os.environ.get("FOUNDRY_PROJECT_NAME", "swc-project")
PROJECT_ENDPOINT = os.environ.get(
    "FOUNDRY_PROJECT_ENDPOINT", f"{VOICELIVE_ENDPOINT}/api/projects/{PROJECT_NAME}"
)
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "10"))
MAX_SESSION_SECONDS = int(os.environ.get("MAX_SESSION_SECONDS", "1800"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.credential = DefaultAzureCredential()
    app.state.http = aiohttp.ClientSession()
    app.state.active = 0
    try:
        yield
    finally:
        await app.state.http.close()
        await app.state.credential.close()


app = FastAPI(title="Call center agent tester", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
async def config() -> dict:
    return {
        "agents": [{**a, "samples": SAMPLES[a["id"]]} for a in AGENTS],
        "project": PROJECT_NAME,
        "maxSessionSeconds": MAX_SESSION_SECONDS,
    }


@app.websocket("/ws/session")
async def session_ws(ws: WebSocket, agent: str, audio: bool = False) -> None:
    if agent not in SAMPLES:
        await ws.close(code=4404, reason="unknown agent")
        return
    await ws.accept()
    state = ws.app.state
    if state.active >= MAX_SESSIONS:
        await ws.send_json({"type": "error", "message": "Too many active sessions, try again later."})
        await ws.close(code=4429)
        return

    class WsSink:
        async def send_json(self, message: dict) -> None:
            await ws.send_json(message)

        async def send_bytes(self, data: bytes) -> None:
            await ws.send_bytes(data)

    incoming: asyncio.Queue = asyncio.Queue()
    bridge = VoiceBridge(
        WsSink(), state.credential, state.http,
        endpoint=VOICELIVE_ENDPOINT, project_name=PROJECT_NAME, project_endpoint=PROJECT_ENDPOINT,
        agent_name=agent, audio_out=audio,
    )

    async def reader() -> None:
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("bytes") is not None:
                    await incoming.put(msg["bytes"])
                elif msg.get("text"):
                    with contextlib.suppress(ValueError):
                        await incoming.put(json.loads(msg["text"]))
        finally:
            await incoming.put(None)

    state.active += 1
    reader_task = asyncio.create_task(reader())
    try:
        await asyncio.wait_for(bridge.run(incoming), timeout=MAX_SESSION_SECONDS)
    except asyncio.TimeoutError:
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "error", "message": "Session time limit reached."})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.exception("session failed")
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        state.active -= 1
        reader_task.cancel()
        with contextlib.suppress(Exception):
            await ws.close()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
