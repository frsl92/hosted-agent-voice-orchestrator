"""Tails a hosted agent's console log stream for one Voice Live session.

The hosted agent session id is ``vl_<Voice Live session id>``. The session (and so its log stream) is
created lazily on the first ``response.create``, so a 404 simply means "not there yet": keep retrying.
Lines of the form ``TRACE {json}`` become structured agent trace events; everything else is a raw log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from azure.core.credentials_async import AsyncTokenCredential

logger = logging.getLogger("logtail")

TOKEN_SCOPE = "https://ai.azure.com/.default"
TRACE_RE = re.compile(r"TRACE (\{.*\})\s*$")
LEVEL_RE = re.compile(r"\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b")
SESSION_PREFIX = "vl_"

Emit = Callable[[dict[str, Any]], Awaitable[None]]


def agent_session_id(voice_session_id: str) -> str:
    return f"{SESSION_PREFIX}{voice_session_id}"


def parse_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """Turn one log-stream record into a trace event (or ``None`` for non-log records)."""
    message = record.get("message")
    if not isinstance(message, str):
        return None
    m = TRACE_RE.search(message)
    if m:
        try:
            payload = json.loads(m.group(1))
            kind = str(payload.pop("kind", "trace"))
            t = payload.pop("t", None)
            return {"source": "agent", "kind": kind, "t": t or _now_ms(), "data": payload}
        except ValueError:
            pass
    level = LEVEL_RE.search(message)
    return {
        "source": "log",
        "kind": (level.group(1) if level else "LOG").lower(),
        "t": _iso_to_ms(record.get("timestamp")) or _now_ms(),
        "data": {"message": message.rstrip(), "stream": record.get("stream")},
    }


def _now_ms() -> int:
    return round(time.time() * 1000)


def _iso_to_ms(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    from datetime import datetime

    try:
        return round(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


class LogTailer:
    def __init__(
        self,
        credential: AsyncTokenCredential,
        http: aiohttp.ClientSession,
        project_endpoint: str,
        agent_name: str,
        voice_session_id: str,
        emit: Emit,
        *,
        retry_delay: float = 1.0,
    ) -> None:
        self._cred = credential
        self._http = http
        self._emit = emit
        self._retry_delay = retry_delay
        self._seen: set[tuple[Any, str]] = set()
        self._warned = False
        self.session_id = agent_session_id(voice_session_id)
        self._url = f"{project_endpoint.rstrip('/')}/agents/{agent_name}/sessions/{self.session_id}:logstream"

    async def run(self) -> None:
        """Follow the log stream until cancelled, reconnecting as needed."""
        while True:
            try:
                await self._follow_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._warn(f"log stream error: {exc}")
            await asyncio.sleep(self._retry_delay)

    async def _warn(self, text: str) -> None:
        if not self._warned:
            self._warned = True
            await self._emit({"source": "system", "kind": "logstream", "t": _now_ms(), "data": {"message": text}})

    async def _follow_once(self) -> None:
        token = await self._cred.get_token(TOKEN_SCOPE)
        params = {"api-version": "v1", "kind": "console", "tail": "100", "follow": "true"}
        headers = {"Authorization": f"Bearer {token.token}", "Accept": "text/event-stream"}
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=300)
        async with self._http.get(self._url, params=params, headers=headers, timeout=timeout) as resp:
            if resp.status == 404:
                return
            if resp.status != 200:
                body = (await resp.text())[:200]
                await self._warn(f"log stream returned {resp.status}: {body}")
                return
            async for raw in resp.content:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    record = json.loads(line[5:].strip())
                except ValueError:
                    continue
                await self._handle(record)

    async def _handle(self, record: dict[str, Any]) -> None:
        if "session_state" in record and "message" not in record:
            await self._emit(
                {"source": "system", "kind": "session_state", "t": _now_ms(), "data": {"state": record["session_state"]}}
            )
            return
        key = (record.get("timestamp"), str(record.get("message")))
        if key in self._seen:
            return
        self._seen.add(key)
        event = parse_record(record)
        if event:
            await self._emit(event)
