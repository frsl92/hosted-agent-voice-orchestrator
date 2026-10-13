"""Runs the billing / orders workers as separate Foundry hosted agents, over the Responses API.

Each call to a topic goes to its own hosted agent (``AIProjectClient.get_openai_client(agent_name=...)``).
The first call of a conversation lets Foundry create a session for that agent; its id and the last response id
are kept per (conversation, topic), so a later task for the same topic lands in the same session and the
worker still remembers what it already looked up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from calltrace import elapsed_ms, emit, emit_usage
from decision import TOPIC_BILLING, TOPIC_ORDERS
from workers.billing import billing_prompt
from workers.orders import orders_prompt

logger = logging.getLogger(__name__)

DEFAULT_AGENT_NAMES = {TOPIC_BILLING: "billing-agent", TOPIC_ORDERS: "orders-agent"}
MAX_CONVERSATIONS = 500

# The hosted agents are conversational specialists; this turns one call into a silent background lookup.
BACKGROUND_DIRECTIVE = (
    "Instruction for this turn: this is a background lookup whose answer is read to the customer later by "
    "someone else. Use your tools, then reply with only the facts in two to four short spoken sentences, in the "
    "language of the request. Do not greet, do not say you checked or looked something up, do not ask questions "
    "and do not offer more help."
)


def agent_names_from_env() -> dict[str, str]:
    return {
        TOPIC_BILLING: os.environ.get("BILLING_AGENT_NAME", DEFAULT_AGENT_NAMES[TOPIC_BILLING]),
        TOPIC_ORDERS: os.environ.get("ORDERS_AGENT_NAME", DEFAULT_AGENT_NAMES[TOPIC_ORDERS]),
    }


def _extra(obj: Any, key: str) -> Any:
    """Read a field the OpenAI SDK model does not declare (e.g. Foundry's ``agent_session_id``)."""
    value = getattr(obj, key, None)
    if value is None:
        value = (getattr(obj, "model_extra", None) or {}).get(key)
    return value


def _parse_args(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {"raw": raw}
    return value if isinstance(value, dict) else {"value": value}


def _prompt(topic: str, params: dict[str, Any]) -> str:
    request = billing_prompt(params) if topic == TOPIC_BILLING else orders_prompt(params)
    return f"{BACKGROUND_DIRECTIVE}\n\nCustomer's request: {request}"


class RemoteWorkerRunner:
    """``WorkerRunner`` that sends each background task to the topic's hosted agent, one session per conversation."""

    def __init__(self, openai_for: Callable[[str], Any], agent_names: dict[str, str] | None = None) -> None:
        self._openai_for = openai_for
        self._names = agent_names or dict(DEFAULT_AGENT_NAMES)
        self._entries: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        # Tasks of one topic in one conversation share a session, so they run one at a time.
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def __call__(self, topic: str, params: dict[str, Any], session_key: str = "") -> str:
        if topic not in self._names:
            raise ValueError(f"unknown topic {topic!r}")
        slot = (session_key, topic)
        entry = self._entries.setdefault(slot, {})
        self._entries.move_to_end(slot)
        while len(self._entries) > MAX_CONVERSATIONS:
            old, _ = self._entries.popitem(last=False)
            self._locks.pop(old, None)
        async with self._locks.setdefault(slot, asyncio.Lock()):
            for attempt in (1, 2):
                had_session = bool(entry.get("agent_session_id"))
                try:
                    return await self._call(topic, self._names[topic], params, entry)
                except Exception as exc:
                    # A session that expired (idle timeout) is replaced by a fresh one.
                    if attempt == 1 and had_session:
                        emit("subagent.session_lost", agent=self._names[topic], agent_session_id=entry.get("agent_session_id"), error=str(exc))
                        entry.clear()
                        continue
                    raise
        raise AssertionError("unreachable")

    async def _call(self, topic: str, name: str, params: dict[str, Any], entry: dict[str, Any]) -> str:
        session_id = entry.get("agent_session_id")
        previous = entry.get("previous_response_id")
        text = _prompt(topic, params)
        kwargs: dict[str, Any] = {"input": text}
        if previous:
            kwargs["previous_response_id"] = previous
        if session_id:
            kwargs["extra_body"] = {"agent_session_id": session_id}
        started = time.perf_counter()
        emit("subagent.call", agent=name, new_session=not session_id, agent_session_id=session_id, previous_response_id=previous, input=text)
        response = await self._openai_for(name).responses.create(**kwargs)
        if getattr(response, "error", None):
            raise RuntimeError(f"worker {name} failed: {response.error}")

        calls: dict[str, tuple[str, str]] = {}
        for item in getattr(response, "output", None) or []:
            kind = getattr(item, "type", "")
            if kind == "function_call":
                calls[item.call_id] = (item.name, getattr(item, "arguments", "") or "")
            elif kind == "function_call_output":
                tool, args = calls.get(item.call_id, ("?", ""))
                emit("tool.call", name=tool, args=_parse_args(args), result=getattr(item, "output", ""), remote=name)

        session_id = _extra(response, "agent_session_id") or session_id
        if session_id:
            entry["agent_session_id"] = session_id
        entry["previous_response_id"] = getattr(response, "id", None) or previous
        emit("subagent.done", agent=name, agent_session_id=session_id, response_id=entry["previous_response_id"], new_session=not previous, ms=elapsed_ms(started))
        usage = getattr(response, "usage", None)
        if usage:
            emit_usage(
                os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.6-luna"),
                {"input_tokens": getattr(usage, "input_tokens", 0) or 0, "output_tokens": getattr(usage, "output_tokens", 0) or 0},
                f"worker:{topic}",
            )
        return response.output_text or ""


def build_openai_for(endpoint: str) -> Callable[[str], Any]:
    """Lazily build one async OpenAI client per worker agent, all sharing one Entra-authenticated project client."""
    from azure.ai.projects.aio import AIProjectClient
    from azure.identity.aio import DefaultAzureCredential

    project = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential(), allow_preview=True)
    clients: dict[str, Any] = {}

    def openai_for(agent_name: str) -> Any:
        if agent_name not in clients:
            clients[agent_name] = project.get_openai_client(agent_name=agent_name)
        return clients[agent_name]

    return openai_for
