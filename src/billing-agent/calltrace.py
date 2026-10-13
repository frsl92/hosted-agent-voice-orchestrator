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

MAX_TEXT = 2000


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


def _count(usage: Any, *names: str) -> int:
    for name in names:
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        if isinstance(value, int):
            return value
    return 0


def emit_usage(model: str, usage: Any, purpose: str = "") -> None:
    """Trace the token usage of one model call (MAF ``UsageDetails`` or a Decision-1 ``usage`` object)."""
    if not usage:
        return
    emit(
        "usage",
        model=model,
        purpose=purpose,
        input_tokens=_count(usage, "input_token_count", "input_tokens", "prompt_tokens"),
        output_tokens=_count(usage, "output_token_count", "output_tokens", "completion_tokens"),
    )


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
