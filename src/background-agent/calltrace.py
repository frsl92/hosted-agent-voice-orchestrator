"""Structured trace events, written to the console log as ``TRACE {json}`` lines.

The web app tails the hosted agent's console log for a session and turns these lines into the
trace timeline, so keep payloads small and JSON-serializable.
"""

from __future__ import annotations

import functools
import json
import logging
import time
from typing import Any

logger = logging.getLogger("calltrace")

MAX_TEXT = 600


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return value[:MAX_TEXT] + "…"
    return value


def emit(kind: str, **data: Any) -> None:
    """Log one trace event. Never raises."""
    try:
        payload = {"kind": kind, "t": round(time.time() * 1000), **{k: _clip(v) for k, v in data.items()}}
        logger.info("TRACE %s", json.dumps(payload, default=str, ensure_ascii=False))
    except Exception:  # tracing must never break a call
        pass


def elapsed_ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


def traced_tool(fn: Any) -> Any:
    """Wrap an async tool function so every call is traced (apply *under* ``@tool``)."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            result = await fn(*args, **kwargs)
        except Exception as exc:
            emit("tool.call", name=fn.__name__, args=kwargs, error=str(exc), ms=elapsed_ms(start))
            raise
        emit("tool.call", name=fn.__name__, args=kwargs, result=result, ms=elapsed_ms(start))
        return result

    return wrapper
