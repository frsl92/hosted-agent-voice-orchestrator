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
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import auth
import voices
from samples import AGENTS, MODES, SAMPLES
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
AGENT_IDS = {a["id"] for a in AGENTS}
# Paths reachable without the password (the login page itself and its health probe).
PUBLIC_PATHS = {"/healthz", "/login"}


def app_password() -> str:
    return os.environ.get("APP_PASSWORD", "")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.credential = DefaultAzureCredential()
    app.state.http = aiohttp.ClientSession()
    app.state.active = 0
    app.state.throttle = auth.LoginThrottle()
    if not app_password():
        logger.warning("APP_PASSWORD is not set: every request except /healthz will be refused.")
    try:
        yield
    finally:
        await app.state.http.close()
        await app.state.credential.close()


app = FastAPI(title="Call center agent tester", lifespan=lifespan)


@app.middleware("http")
async def password_gate(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or auth.token_valid(request.cookies.get(auth.COOKIE), app_password()):
        return await call_next(request)
    if path.startswith("/api/") or path.startswith("/static/") or request.method != "GET":
        return JSONResponse({"error": "authentication required"}, status_code=401)
    return RedirectResponse("/login", status_code=302)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/login")
async def login_page() -> HTMLResponse:
    return HTMLResponse(auth.LOGIN_PAGE)


@app.post("/login")
async def login(request: Request) -> JSONResponse:
    throttle: auth.LoginThrottle = request.app.state.throttle
    ip = client_ip(request)
    if throttle.blocked(ip):
        return JSONResponse({"error": "too many attempts"}, status_code=429)
    try:
        body = await request.json()
        given = str(body.get("password", ""))
    except Exception:
        given = ""
    expected = app_password()
    if not expected or not auth.password_matches(given, expected):
        throttle.record_failure(ip)
        return JSONResponse({"error": "wrong password"}, status_code=401)
    throttle.reset(ip)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        auth.COOKIE, auth.make_token(expected), max_age=auth.TTL_SECONDS, httponly=True, samesite="lax",
        secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
    )
    return response


@app.post("/logout")
async def logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE)
    return response


@app.get("/api/config")
async def config() -> dict:
    return {
        "agents": AGENTS,
        "modes": MODES,
        "samples": SAMPLES,
        "voices": voices.VOICES,
        "defaultVoice": voices.DEFAULT_VOICE,
        "locale": voices.LOCALE,        "project": PROJECT_NAME,
        "maxSessionSeconds": MAX_SESSION_SECONDS,
    }


def origin_allowed(ws: WebSocket) -> bool:
    origin = ws.headers.get("origin")
    if not origin:
        return True
    host = ws.headers.get("x-forwarded-host") or ws.headers.get("host") or ""
    return origin.split("://", 1)[-1] == host


@app.websocket("/ws/session")
async def session_ws(ws: WebSocket, agent: str, mode: str = "text", voice: str = "", style: str = "") -> None:
    if not auth.token_valid(ws.cookies.get(auth.COOKIE), app_password()):
        await ws.close(code=4401, reason="authentication required")
        return
    if not origin_allowed(ws):
        await ws.close(code=4403, reason="origin not allowed")
        return
    if agent not in AGENT_IDS or mode not in {m["id"] for m in MODES}:
        await ws.close(code=4404, reason="unknown agent or mode")
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
        agent_name=agent, mode=mode, voice=voice, voice_style=style,
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
